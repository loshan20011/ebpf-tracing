import subprocess
import re
import time
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from kubernetes import client, config

# --- CONFIGURATION ---
TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "default")
MY_POD_NAME = os.getenv("MY_POD_NAME", "")
print(f"[*] Agent v29 (True SLO Tracing) - Namespace: {TARGET_NAMESPACE}", flush=True)

LATEST_METRICS = {}
ALLOWED_POD_UIDS = set()
K8S_CONNECTED = False

# --- BPF SCRIPT v29 (Request Duration) ---
BPF_SCRIPT = """
// 1. Map to store when a process finished reading a request
struct start_t {
    u64 ts;
}
BPF_HASH(start_times, u32, struct start_t);

// 2. Hook: Return from 'read' or 'recvfrom' (Request Received)
tracepoint:syscalls:sys_exit_read {
    u32 pid = pid;
    struct start_t s = {};
    s.ts = nsecs;
    start_times.update(&pid, &s);
}
tracepoint:syscalls:sys_exit_recvfrom {
    u32 pid = pid;
    struct start_t s = {};
    s.ts = nsecs;
    start_times.update(&pid, &s);
}

// 3. Hook: Enter 'write' or 'sendto' (Response Sending)
tracepoint:syscalls:sys_enter_write {
    u32 pid = pid;
    struct start_t *s = start_times.lookup(&pid);
    if (s != 0) {
        u64 delta = (nsecs - s->ts) / 1000000; // Convert to Milliseconds
        // Only report if latency is significant (>5ms) to filter noise
        if (delta > 5 && delta < 10000) { 
            @req_latency[comm, cgroup] = hist(delta);
        }
        start_times.delete(&pid);
    }
}
tracepoint:syscalls:sys_enter_sendto {
    u32 pid = pid;
    struct start_t *s = start_times.lookup(&pid);
    if (s != 0) {
        u64 delta = (nsecs - s->ts) / 1000000;
        if (delta > 5 && delta < 10000) {
            @req_latency[comm, cgroup] = hist(delta);
        }
        start_times.delete(&pid);
    }
}

// Keep CPU profiling for saturation check
profile:hz:99 { 
    if (comm != "bpftrace" && comm != "find" && reg("ip") < 0xffffffff00000000) {
        @cpu_stacks[comm, cgroup] = count(); 
    }
}

interval:s:2 { 
    print(@req_latency); 
    print(@cpu_stacks);
    clear(@req_latency); 
    clear(@cpu_stacks);
}
"""

# Note: The above is pseudo-code logic. 
# bpftrace syntax is simpler. Let's write the EXACT bpftrace script below.
# We use 'map' instead of BPF_HASH for bpftrace.

REAL_BPF_SCRIPT = """
tracepoint:syscalls:sys_exit_read,
tracepoint:syscalls:sys_exit_recvfrom
{
    @start[pid] = nsecs;
}

tracepoint:syscalls:sys_enter_write,
tracepoint:syscalls:sys_enter_sendto
{
    $s = @start[pid];
    if ($s != 0) {
        $delta = (nsecs - $s) / 1000000; // ms
        if ($delta > 10) { // Filter tiny noise
            @req_latency[comm, cgroup] = max($delta);
        }
        delete(@start[pid]);
    }
}

profile:hz:99 { 
    if (comm != "bpftrace" && comm != "find" && reg("ip") < 0xffffffff00000000) {
        @cpu_stacks[comm, cgroup] = count(); 
    }
}

interval:s:2 { 
    print(@req_latency); 
    print(@cpu_stacks);
    clear(@req_latency); 
    clear(@cpu_stacks);
}
"""

def k8s_watcher_loop():
    global ALLOWED_POD_UIDS, K8S_CONNECTED
    while True:
        try:
            try: config.load_incluster_config()
            except: config.load_kube_config()
            v1 = client.CoreV1Api()
            pods = v1.list_namespaced_pod(TARGET_NAMESPACE, _request_timeout=5)
            new_set = set()
            for pod in pods.items:
                if pod.metadata.name == MY_POD_NAME: continue 
                if pod.metadata.uid: new_set.add(pod.metadata.uid)
            ALLOWED_POD_UIDS = new_set
            if not K8S_CONNECTED: print(f"[*] K8s Connected! Tracking {len(new_set)} pods.", flush=True)
            K8S_CONNECTED = True
        except: 
            K8S_CONNECTED = False
        time.sleep(5)

def get_pod_uid_from_cgroup(cgroupid):
    try:
        cmd = ["find", "/sys/fs/cgroup", "-maxdepth", "6", "-inum", str(cgroupid)]
        path = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
        if not path: return None
        match = re.search(r'pod([a-f0-9-_]+)', path)
        if match: return match.group(1).replace('_', '-')
        return None
    except: return None

class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(LATEST_METRICS).encode())
    def log_message(self, format, *args): return

def start_http_server():
    server = HTTPServer(('0.0.0.0', 5000), MetricsHandler)
    server.serve_forever()

def main():
    print("[*] Starting Agent v29...", flush=True)
    threading.Thread(target=k8s_watcher_loop, daemon=True).start()
    threading.Thread(target=start_http_server, daemon=True).start()

    with open("sensor.bt", "w") as f: f.write(REAL_BPF_SCRIPT)
    process = subprocess.Popen(["bpftrace", "sensor.bt"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    
    threading.Thread(target=lambda: process.stderr.read(), daemon=True).start()

    print("[*] Sensor Running...", flush=True)
    id_cache = {}

    def is_valid_k8s_workload(cgroup):
        uid = id_cache.get(cgroup)
        if not uid:
            uid = get_pod_uid_from_cgroup(cgroup)
            if uid: id_cache[cgroup] = uid
            else: 
                id_cache[cgroup] = "NO_MATCH"
                return None
        if uid == "NO_MATCH": return None
        if K8S_CONNECTED and uid in ALLOWED_POD_UIDS: return uid
        if not K8S_CONNECTED and uid: return uid # Fallback
        return None

    while True:
        line = process.stdout.readline()
        if not line: break
        
        # 1. CPU (Existing)
        cpu = re.search(r'@cpu_stacks\[(.*?), (\d+)\]: (\d+)', line)
        if cpu:
            comm, cgroup, count = cpu.groups()
            uid = is_valid_k8s_workload(cgroup)
            if uid and int(count) > 100:
                # print(f"🔥 [CPU] Pod: {uid} | App: {comm}", flush=True)
                LATEST_METRICS[uid] = {"type": "CPU", "value": count}

        # 2. REQUEST LATENCY (New SLO Metric)
        # Matches: @req_latency[svc-cpu, 234823]: 500
        lat = re.search(r'@req_latency\[(.*?), (\d+)\]: (\d+)', line)
        if lat:
            comm, cgroup, ms = lat.groups()
            uid = is_valid_k8s_workload(cgroup)
            if uid:
                print(f"⏱️ [SLO] Pod: {uid} | App: {comm} | Duration: {ms}ms", flush=True)
                # We overwrite the metric type to "SLO_LATENCY"
                LATEST_METRICS[uid] = {"type": "SLO_LATENCY", "value": ms}

if __name__ == "__main__":
    main()