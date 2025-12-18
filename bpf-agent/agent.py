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
print(f"[*] Monitoring Namespace: {TARGET_NAMESPACE}", flush=True)
print(f"[*] Ignoring Self: {MY_POD_NAME}", flush=True)

# --- BPF SCRIPT v17 (Language Safe) ---
# REMOVED: "python3" from the exclude list so we can monitor Python apps.
BPF_SCRIPT = """
// 1. CPU Profiling
profile:hz:99 { 
    // We strictly filter Infrastructure & Internal Tools, but allow Application Runtimes (java, python, node, go)
    if (comm != "bpftrace" && comm != "find" && 
        comm != "containerd" && comm != "dockerd" && comm != "runc" && 
        comm != "containerd-shim" && reg("ip") < 0xffffffff00000000) {
        
        @cpu_stacks[comm, cgroup, usym(reg("ip"))] = count(); 
    }
}

// 2. Disk I/O
tracepoint:block:block_rq_issue {
    @disk_io[cgroup] = count();
}

// 3. Memory Pressure
tracepoint:exceptions:page_fault_user {
    if (comm != "bpftrace" && comm != "find" && 
        comm != "containerd" && comm != "dockerd") {
        @mem_pressure[comm, cgroup] = count();
    }
}

// 4. Off-CPU (Wait Time)
tracepoint:sched:sched_switch {
    if (args->prev_state != 0) { 
        if (args->prev_comm != "bpftrace" && args->prev_comm != "find" && 
            args->prev_comm != "containerd" && args->prev_comm != "dockerd" && 
            args->prev_comm != "kworker" && args->prev_comm != "swapper/0" && args->prev_comm != "swapper/1") {
            
            @start[args->prev_pid] = nsecs;
        }
    }
    
    if (@start[args->next_pid]) {
        if (args->next_comm != "bpftrace" && args->next_comm != "find" && 
            args->next_comm != "containerd" && args->next_comm != "dockerd") {
            
            $delta = (nsecs - @start[args->next_pid]) / 1000;
            @off_cpu[args->next_comm, cgroup] = sum($delta);
        }
        delete(@start[args->next_pid]);
    }
}

interval:s:2 { 
    print(@cpu_stacks); 
    print(@disk_io); 
    print(@mem_pressure); 
    print(@off_cpu);
    clear(@cpu_stacks); 
    clear(@disk_io); 
    clear(@mem_pressure); 
    clear(@off_cpu);
}
"""

LATEST_METRICS = {}
ALLOWED_POD_UIDS = set()

def watch_kubernetes_pods():
    try:
        config.load_incluster_config()
        v1 = client.CoreV1Api()
        while True:
            try:
                pods = v1.list_namespaced_pod(TARGET_NAMESPACE)
                new_set = set()
                for pod in pods.items:
                    # 1. Ignore Self (The Agent)
                    if pod.metadata.name == MY_POD_NAME: 
                        continue 
                    
                    # 2. Add everyone else in the namespace
                    if pod.metadata.uid: 
                        new_set.add(pod.metadata.uid)
                
                global ALLOWED_POD_UIDS
                ALLOWED_POD_UIDS = new_set
            except Exception as e:
                print(f"Error listing pods: {e}", flush=True)
            time.sleep(5)
    except Exception as e:
        print(f"FATAL: Could not connect to K8s API: {e}", flush=True)

def get_pod_uid_from_cgroup(cgroupid):
    try:
        cmd = ["find", "/sys/fs/cgroup", "-maxdepth", "4", "-inum", str(cgroupid)]
        path = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
        if not path: return None
        match = re.search(r'pod([a-f0-9-_]+)', path)
        if match: return match.group(1).replace('_', '-')
        return None
    except:
        return None

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
    print("[*] eBPF Agent v17 (Language Safe) Started...", flush=True)
    t_k8s = threading.Thread(target=watch_kubernetes_pods); t_k8s.daemon = True; t_k8s.start()
    t_http = threading.Thread(target=start_http_server); t_http.daemon = True; t_http.start()

    with open("sensor.bt", "w") as f: f.write(BPF_SCRIPT)
    
    process = subprocess.Popen(["bpftrace", "sensor.bt"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    print("[*] Sensor Running...", flush=True)
    
    def print_stderr():
        for line in process.stderr: print(f"BPF ERROR: {line.strip()}", flush=True)
    threading.Thread(target=print_stderr, daemon=True).start()

    id_cache = {}

    def resolve_valid_uid(cgroup):
        uid = id_cache.get(cgroup)
        if not uid:
            uid = get_pod_uid_from_cgroup(cgroup)
            id_cache[cgroup] = uid if uid else "UNKNOWN"
        
        # KEY LOGIC: Even if BPF captured 'python3' (us), 
        # this check will fail because our UID is not in ALLOWED_POD_UIDS.
        # So we effectively drop our own traffic here.
        if uid in ALLOWED_POD_UIDS: return uid
        return None

    while True:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                print(f"CRITICAL: bpftrace died code {process.returncode}", flush=True)
                break
            continue
        
        # 1. CPU
        cpu_match = re.search(r'@cpu_stacks\[(.*?), (\d+), (.*?)\]: (\d+)', line)
        if cpu_match:
            comm, cgroup, func, count = cpu_match.groups()
            if int(count) > 5:
                uid = resolve_valid_uid(cgroup)
                if uid:
                    func = func.split("+")[0].strip()
                    print(f"🔥 [CPU] Pod: {uid} | App: {comm} | Func: {func}", flush=True)
                    LATEST_METRICS[uid] = {"type": "CPU", "detail": func, "value": count}

        # 2. DISK
        disk_match = re.search(r'@disk_io\[(\d+)\]: (\d+)', line)
        if disk_match:
            cgroup, count = disk_match.groups()
            if int(count) > 10:
                uid = resolve_valid_uid(cgroup)
                if uid:
                    print(f"🚨 [DISK] Pod: {uid} | IOPS: {count}", flush=True)
                    LATEST_METRICS[uid] = {"type": "DISK", "detail": "High IOPS", "value": count}

        # 3. MEMORY
        mem_match = re.search(r'@mem_pressure\[(.*?), (\d+)\]: (\d+)', line)
        if mem_match:
            comm, cgroup, count = mem_match.groups()
            if int(count) > 100:
                uid = resolve_valid_uid(cgroup)
                if uid:
                    print(f"⚠️ [MEM] Pod: {uid} | App: {comm} | Faults: {count}", flush=True)
                    LATEST_METRICS[uid] = {"type": "MEMORY", "detail": "Page Faults", "value": count}

        # 4. OFF-CPU
        off_match = re.search(r'@off_cpu\[(.*?), (\d+)\]: (\d+)', line)
        if off_match:
            comm, cgroup, usecs = off_match.groups()
            wait_time = int(usecs)
            if wait_time > 200000 and wait_time < 2000000:
                uid = resolve_valid_uid(cgroup)
                if uid:
                    print(f"☁️ [WAIT] Pod: {uid} | App: {comm} | Latency: {wait_time/1000}ms", flush=True)
                    LATEST_METRICS[uid] = {"type": "NETWORK", "detail": "Latency", "value": wait_time}

if __name__ == "__main__":
    main()