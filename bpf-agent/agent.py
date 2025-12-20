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
print(f"[*] Agent v28 (Heuristic Fallback) - Namespace: {TARGET_NAMESPACE}", flush=True)

LATEST_METRICS = {}
ALLOWED_POD_UIDS = set()
K8S_CONNECTED = False

# --- BPF SCRIPT v28 (Standard) ---
BPF_SCRIPT = """
profile:hz:99 { 
    if (comm != "bpftrace" && comm != "find" && reg("ip") < 0xffffffff00000000) {
        @cpu_stacks[comm, cgroup, usym(reg("ip"))] = count(); 
    }
}
tracepoint:block:block_rq_issue { @disk_io[cgroup] = count(); }
tracepoint:exceptions:page_fault_user { 
    if (comm != "bpftrace" && comm != "find") { @mem_pressure[comm, cgroup] = count(); }
}
tracepoint:sched:sched_switch {
    if (args->prev_pid != 0) { @start[args->prev_pid] = nsecs; }
    if (@start[args->next_pid]) {
        $delta = (nsecs - @start[args->next_pid]) / 1000;
        @off_cpu[args->next_comm, cgroup] = sum($delta);
        delete(@start[args->next_pid]);
    }
}
interval:s:2 { 
    print(@cpu_stacks); print(@disk_io); print(@mem_pressure); print(@off_cpu);
    clear(@cpu_stacks); clear(@disk_io); clear(@mem_pressure); clear(@off_cpu);
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
            
            if not K8S_CONNECTED: 
                print(f"[*] K8s Connected! Tracking {len(new_set)} pods.", flush=True)
            K8S_CONNECTED = True
        except Exception as e:
            if K8S_CONNECTED: print(f"[!] Lost K8s Connection: {e}", flush=True)
            K8S_CONNECTED = False # Mark as disconnected to trigger fallback
        time.sleep(10)

def get_pod_uid_from_cgroup(cgroupid):
    try:
        cmd = ["find", "/sys/fs/cgroup", "-maxdepth", "6", "-inum", str(cgroupid)]
        path = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
        if not path: return None
        # Strict Regex: Must match Kubernetes Cgroup pattern
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
    print("[*] Starting Agent v28...", flush=True)
    threading.Thread(target=k8s_watcher_loop, daemon=True).start()
    threading.Thread(target=start_http_server, daemon=True).start()

    with open("sensor.bt", "w") as f: f.write(BPF_SCRIPT)
    process = subprocess.Popen(["bpftrace", "sensor.bt"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    
    # Debug: Print BPF errors
    threading.Thread(target=lambda: process.stderr.read(), daemon=True).start()

    print("[*] Sensor Running...", flush=True)
    id_cache = {}

    def is_valid_workload(cgroup):
        # 1. Resolve UID
        uid = id_cache.get(cgroup)
        if not uid:
            uid = get_pod_uid_from_cgroup(cgroup)
            if uid: id_cache[cgroup] = uid
            else: 
                id_cache[cgroup] = "NO_MATCH"
                return None

        if uid == "NO_MATCH": return None
        
        # 2. Logic Gate
        if K8S_CONNECTED:
            # Gold Standard: Check against API list
            if uid in ALLOWED_POD_UIDS: return uid
        else:
            # Fallback: If we extracted a valid UID, trust it.
            # This filters out Chrome/Host apps because they don't have 'pod<UID>' cgroups.
            return uid
            
        return None

    while True:
        line = process.stdout.readline()
        if not line: break
        
        # 1. CPU
        cpu = re.search(r'@cpu_stacks\[(.*?), (\d+), (.*?)\]: (\d+)', line)
        if cpu:
            comm, cgroup, func, count = cpu.groups()
            uid = is_valid_workload(cgroup)
            if uid and int(count) > 5:
                func = func.split("+")[0].strip()
                print(f"🔥 [CPU] Pod: {uid} | App: {comm} | Func: {func}", flush=True)
                LATEST_METRICS[uid] = {"type": "CPU", "value": count}

        # 2. DISK IO
        disk = re.search(r'@disk_io\[(\d+)\]: (\d+)', line)
        if disk:
            cgroup, count = disk.groups()
            uid = is_valid_workload(cgroup)
            if uid and int(count) > 10:
                print(f"🚨 [DISK] Pod: {uid} | IOPS: {count}", flush=True)
                LATEST_METRICS[uid] = {"type": "DISK", "value": count}

        # 3. MEMORY
        mem = re.search(r'@mem_pressure\[(.*?), (\d+)\]: (\d+)', line)
        if mem:
            comm, cgroup, count = mem.groups()
            uid = is_valid_workload(cgroup)
            if uid and int(count) > 100:
                print(f"⚠️ [MEM] Pod: {uid} | App: {comm} | Faults: {count}", flush=True)
                LATEST_METRICS[uid] = {"type": "MEMORY", "value": count}

        # 4. NETWORK WAIT
        off = re.search(r'@off_cpu\[(.*?), (\d+)\]: (\d+)', line)
        if off:
            comm, cgroup, usecs = off.groups()
            uid = is_valid_workload(cgroup)
            wait = int(usecs)
            if uid and wait > 200000 and wait < 2000000:
                print(f"☁️ [WAIT] Pod: {uid} | App: {comm} | Latency: {wait/1000}ms", flush=True)
                LATEST_METRICS[uid] = {"type": "NETWORK", "value": wait}

if __name__ == "__main__":
    main()