import json
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kubernetes import client, config

TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "default")
AGENT_PORT = int(os.getenv("AGENT_PORT", "5000"))
RAW_BUFFER_MAX_EVENTS = int(os.getenv("RAW_BUFFER_MAX_EVENTS", "50000"))
RUNQ_MIN_US = int(os.getenv("RUNQ_MIN_US", "250"))
CPU_THROTTLE_POLL_SECONDS = float(os.getenv("CPU_THROTTLE_POLL_SECONDS", "2.0"))
CPU_THROTTLE_MIN_RATIO = float(os.getenv("CPU_THROTTLE_MIN_RATIO", "0.01"))
FUNCTIONAL_TEST_MODE = os.getenv("FUNCTIONAL_TEST_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
CMDLINE_SERVICE_FALLBACK_ENABLED = os.getenv("CMDLINE_SERVICE_FALLBACK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "sensor.bt")
MY_PID = os.getpid()
NODE_NAME = os.getenv("MY_NODE_NAME", socket.gethostname())

EVENT_RE = re.compile(r"(\w+)=([^\s]+)")

EVENT_QUEUE = deque()
EVENT_LOCK = threading.Lock()
DROPPED_EVENTS = 0

IP_TO_SVC = {}
UID_TO_SVC = {}
PODNAME_TO_SVC = {}
KNOWN_SERVICES = set()
PID_CACHE = {}
MAP_LOCK = threading.Lock()

PROBE_PROCESS = None
PROBE_LOCK = threading.Lock()
LAST_PARSE_TS = 0.0
MONOTONIC_TO_EPOCH_OFFSET_NS = time.time_ns() - time.monotonic_ns()

STATS_LOCK = threading.Lock()
PARSED_EVENTS = 0
FILTERED_EVENTS = 0

THROTTLE_STATE = {}
THROTTLE_LOCK = threading.Lock()


def get_k8s_client():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.CoreV1Api()


def k8s_metadata_updater():
    global IP_TO_SVC, UID_TO_SVC, PODNAME_TO_SVC, KNOWN_SERVICES
    v1 = get_k8s_client()
    while True:
        try:
            new_ip_map = {}
            new_uid_map = {}
            new_pod_map = {}
            new_known_svcs = set()

            pods = v1.list_namespaced_pod(TARGET_NAMESPACE)
            for pod in pods.items:
                if not pod.metadata.labels:
                    continue
                app = pod.metadata.labels.get("app") or pod.metadata.labels.get("name")
                if not app and pod.metadata and pod.metadata.name:
                    app = pod.metadata.name
                if not app:
                    continue
                new_known_svcs.add(app)
                if pod.status.pod_ip:
                    new_ip_map[pod.status.pod_ip] = app
                if pod.metadata.uid:
                    uid = pod.metadata.uid
                    new_uid_map[uid] = app
                    new_uid_map[uid.replace("-", "_")] = app
                    new_uid_map[uid.replace("-", "")] = app
                if pod.metadata.name:
                    new_pod_map[pod.metadata.name] = app
                    parts = pod.metadata.name.split("-")
                    if len(parts) > 2:
                        new_pod_map["-".join(parts[:-2])] = app

            services = v1.list_namespaced_service(TARGET_NAMESPACE)
            for svc in services.items:
                app = None
                if svc.metadata and svc.metadata.labels:
                    app = svc.metadata.labels.get("app") or svc.metadata.labels.get("name")
                # Preserve the old agent's helpful fallback: if a Service does not
                # carry an app/name label, still map its ClusterIP to the Service
                # name so topology edges do not get dropped as "unmapped".
                if not app and svc.metadata:
                    app = svc.metadata.name
                if svc.spec.cluster_ip and svc.spec.cluster_ip != "None":
                    new_ip_map[svc.spec.cluster_ip] = app
                if app:
                    new_known_svcs.add(app)
                if svc.metadata and svc.metadata.name:
                    new_known_svcs.add(svc.metadata.name)

            with MAP_LOCK:
                IP_TO_SVC = new_ip_map
                UID_TO_SVC = new_uid_map
                PODNAME_TO_SVC = new_pod_map
                KNOWN_SERVICES = new_known_svcs
                # Clear cache on refresh to avoid stale namespace mappings.
                PID_CACHE.clear()
        except Exception as exc:
            print(f"K8s metadata updater error: {exc}", flush=True)

        time.sleep(2)


def get_service_from_pid(pid):
    with MAP_LOCK:
        cached = PID_CACHE.get(pid)
        uid_to_svc = UID_TO_SVC.copy()
        pod_to_svc = PODNAME_TO_SVC.copy()
        known_svcs = KNOWN_SERVICES.copy()
    if cached:
        return cached

    try:
        with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8", errors="ignore") as f:
            content = f.read().lower()
        for uid, app in uid_to_svc.items():
            if uid.lower() in content:
                with MAP_LOCK:
                    PID_CACHE[pid] = app
                return app
    except Exception:
        pass

    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            content = f.read().decode("utf-8", errors="ignore")
        match = re.search(r"HOSTNAME=([a-zA-Z0-9-]+)", content)
        if match:
            full_name = match.group(1)
            mapped = pod_to_svc.get(full_name)
            if not mapped:
                parts = full_name.split("-")
                if len(parts) > 2:
                    mapped = pod_to_svc.get("-".join(parts[:-2]))
            if mapped:
                with MAP_LOCK:
                    PID_CACHE[pid] = mapped
                return mapped
    except Exception:
        pass

    if CMDLINE_SERVICE_FALLBACK_ENABLED and not FUNCTIONAL_TEST_MODE:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode("utf-8", errors="ignore").lower()
            for svc in known_svcs:
                if svc and svc.lower() in cmdline:
                    with MAP_LOCK:
                        PID_CACHE[pid] = svc
                    return svc
        except Exception:
            pass

    return None


def parse_kv_pairs(line):
    return {k: v for k, v in EVENT_RE.findall(line)}


def append_event(event):
    global DROPPED_EVENTS
    with EVENT_LOCK:
        if len(EVENT_QUEUE) >= RAW_BUFFER_MAX_EVENTS:
            EVENT_QUEUE.popleft()
            DROPPED_EVENTS += 1
        EVENT_QUEUE.append(event)


def cpu_stat_path_for_pid(pid):
    try:
        with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                parts = line.strip().split(":", 2)
                if len(parts) != 3:
                    continue
                controllers = parts[1]
                rel_path = parts[2].strip()
                if controllers == "" or "cpu" in controllers.split(","):
                    full_path = os.path.join("/sys/fs/cgroup", rel_path.lstrip("/"), "cpu.stat")
                    if os.path.exists(full_path):
                        return full_path
    except Exception:
        pass
    return None


def read_cpu_stat(stat_path):
    stats = {}
    try:
        with open(stat_path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                parts = line.strip().split()
                if len(parts) == 2:
                    try:
                        stats[parts[0]] = int(parts[1])
                    except Exception:
                        continue
    except Exception:
        return None

    periods = int(stats.get("nr_periods", 0) or 0)
    throttled = int(stats.get("nr_throttled", 0) or 0)
    throttled_usec = int(stats.get("throttled_usec", stats.get("throttled_time", 0)) or 0)
    return {
        "nr_periods": periods,
        "nr_throttled": throttled,
        "throttled_usec": throttled_usec,
    }


def cpu_throttle_sampler():
    while True:
        now_ns = time.time_ns()
        service_deltas = {}
        seen_paths = {}

        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                service = get_service_from_pid(pid)
                if not service:
                    continue
                stat_path = cpu_stat_path_for_pid(pid)
                if not stat_path:
                    continue
                seen_paths[stat_path] = service

            for stat_path, service in seen_paths.items():
                current = read_cpu_stat(stat_path)
                if not current:
                    continue
                with THROTTLE_LOCK:
                    previous = THROTTLE_STATE.get(stat_path)
                    THROTTLE_STATE[stat_path] = {
                        "service": service,
                        "nr_periods": current["nr_periods"],
                        "nr_throttled": current["nr_throttled"],
                        "throttled_usec": current["throttled_usec"],
                    }
                if not previous:
                    continue

                delta_periods = current["nr_periods"] - int(previous.get("nr_periods", 0) or 0)
                delta_throttled = current["nr_throttled"] - int(previous.get("nr_throttled", 0) or 0)
                delta_throttled_usec = current["throttled_usec"] - int(previous.get("throttled_usec", 0) or 0)
                if delta_periods <= 0 and delta_throttled_usec <= 0:
                    continue

                bucket = service_deltas.setdefault(service, {"periods": 0, "throttled": 0, "throttled_usec": 0})
                bucket["periods"] += max(0, delta_periods)
                bucket["throttled"] += max(0, delta_throttled)
                bucket["throttled_usec"] += max(0, delta_throttled_usec)

            for service, bucket in service_deltas.items():
                periods = int(bucket["periods"])
                throttled = int(bucket["throttled"])
                throttled_usec = int(bucket["throttled_usec"])
                ratio = (float(throttled) / float(periods)) if periods > 0 else 0.0
                if ratio < CPU_THROTTLE_MIN_RATIO and throttled_usec <= 0:
                    continue
                append_event(
                    {
                        "ts_ns": now_ns,
                        "service": service,
                        "pid": 0,
                        "tid": 0,
                        "node": NODE_NAME,
                        "event_type": "cpu_throttle",
                        "data": {
                            "throttle_ratio": round(ratio, 6),
                            "throttled_usec": throttled_usec,
                            "periods": periods,
                        },
                    }
                )
        except Exception as exc:
            print(f"CPU throttle sampler error: {exc}", flush=True)

        time.sleep(max(1.0, CPU_THROTTLE_POLL_SECONDS))


def inc_stat(parsed=0, filtered=0):
    global PARSED_EVENTS, FILTERED_EVENTS
    with STATS_LOCK:
        PARSED_EVENTS += parsed
        FILTERED_EVENTS += filtered


def parse_event_line(line):
    global LAST_PARSE_TS
    if not line.startswith("EVT "):
        return
    parts = line.split()
    if len(parts) < 2:
        return

    kind = parts[1]
    if kind == "READY":
        LAST_PARSE_TS = time.time()
        return

    kv = parse_kv_pairs(line)
    try:
        pid = int(kv.get("pid", "0"))
        tid = int(kv.get("tid", str(pid)))
        ts_ns = int(kv.get("ts", str(time.time_ns())))
    except ValueError:
        return

    if 0 < ts_ns < 946684800000000000:
        ts_ns += MONOTONIC_TO_EPOCH_OFFSET_NS

    if pid == MY_PID:
        return

    service = get_service_from_pid(pid)
    if service is None:
        inc_stat(filtered=1)
        return

    event = {
        "ts_ns": ts_ns,
        "service": service,
        "pid": pid,
        "tid": tid,
        "node": NODE_NAME,
        "event_type": "",
        "data": {},
    }

    try:
        if kind == "NET":
            event["event_type"] = "net_latency"
            event["data"] = {
                "latency_us": int(kv.get("latency_us", "0")),
                "fd": int(kv.get("fd", "-1")),
            }
        elif kind == "REQ":
            event["event_type"] = "request"
            event["data"] = {
                "fd": int(kv.get("fd", "-1")),
            }
        elif kind == "CONN":
            dst_ip = kv.get("dst_ip", "")
            if dst_ip.startswith("::ffff:"):
                dst_ip = dst_ip.replace("::ffff:", "")
            event["event_type"] = "connect"
            event["data"] = {
                "dst_ip": dst_ip,
                "dst_port": int(kv.get("dst_port", "0")),
            }
        elif kind == "RUNQ":
            delay_us = int(kv.get("delay_us", "0"))
            if delay_us < RUNQ_MIN_US:
                inc_stat(filtered=1)
                return
            event["event_type"] = "runq_latency"
            event["data"] = {
                "delay_us": delay_us,
                "wakeup_cpu": int(kv.get("wakeup_cpu", "-1")),
                "run_cpu": int(kv.get("run_cpu", "-1")),
            }
        else:
            return
    except ValueError:
        return

    LAST_PARSE_TS = time.time()
    inc_stat(parsed=1)
    append_event(event)


def stderr_logger(process):
    for line in process.stderr:
        print(f"BPF STDERR: {line.rstrip()}", flush=True)


def probe_reader(process):
    while True:
        line = process.stdout.readline()
        if not line:
            break
        parse_event_line(line.strip())


def run_probe():
    global PROBE_PROCESS
    while True:
        try:
            cmd = ["bpftrace", "-q", SCRIPT_PATH]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            with PROBE_LOCK:
                PROBE_PROCESS = process
            print(f"[*] Probe started: {' '.join(cmd)}", flush=True)

            t_err = threading.Thread(target=stderr_logger, args=(process,), daemon=True)
            t_out = threading.Thread(target=probe_reader, args=(process,), daemon=True)
            t_err.start()
            t_out.start()
            code = process.wait()
            print(f"Probe exited with code {code}; restarting in 1s", flush=True)
        except Exception as exc:
            print(f"Probe launch error: {exc}", flush=True)
        time.sleep(1)


def is_probe_running():
    with PROBE_LOCK:
        proc = PROBE_PROCESS
    return proc is not None and proc.poll() is None


def stats_logger():
    while True:
        with EVENT_LOCK:
            q_len = len(EVENT_QUEUE)
            dropped = DROPPED_EVENTS
        with STATS_LOCK:
            parsed = PARSED_EVENTS
            filtered = FILTERED_EVENTS
        print(
            f"[*] agent-stats parsed={parsed} filtered={filtered} queue={q_len} dropped={dropped}",
            flush=True,
        )
        time.sleep(5)


class MetricsHandler(BaseHTTPRequestHandler):
    def write_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/events":
            with EVENT_LOCK:
                drained = list(EVENT_QUEUE)
                EVENT_QUEUE.clear()
                dropped = DROPPED_EVENTS
            payload = {
                "events": drained,
                "dropped_events": dropped,
                "agent_time_unix_ms": int(time.time() * 1000),
            }
            self.write_json(200, payload)
            return

        if self.path == "/healthz":
            running = is_probe_running()
            status = 200 if running else 503
            payload = {
                "ok": running,
                "probe_running": running,
                "last_parse_unix_ms": int(LAST_PARSE_TS * 1000) if LAST_PARSE_TS else 0,
                "node": NODE_NAME,
            }
            self.write_json(status, payload)
            return

        self.write_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        return


def main():
    print(
        f"[*] Agent starting on :{AGENT_PORT} namespace={TARGET_NAMESPACE} node={NODE_NAME}",
        flush=True,
    )
    threading.Thread(target=k8s_metadata_updater, daemon=True).start()
    threading.Thread(target=run_probe, daemon=True).start()
    threading.Thread(target=cpu_throttle_sampler, daemon=True).start()
    threading.Thread(target=stats_logger, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", AGENT_PORT), MetricsHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
