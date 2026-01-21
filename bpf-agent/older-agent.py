import subprocess
import threading
import json
import time
import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from kubernetes import client, config

TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "default")
print(f"[*] Unified Agent V5.2 (Syntax Fixed) - Namespace: {TARGET_NAMESPACE}", flush=True)

METRICS_STORE = {}
TOPOLOGY_STORE = {}
IP_TO_SVC = {}
UID_TO_SVC = {}
CGROUP_TO_SVC = {}
LAST_SCRAPE_TIME = time.time()

def get_k8s_client():
    try: config.load_incluster_config()
    except: config.load_kube_config()
    return client.CoreV1Api()

def k8s_metadata_updater():
    global IP_TO_SVC, UID_TO_SVC
    v1 = get_k8s_client()
    while True:
        try:
            new_ip_map = {}
            new_uid_map = {}
            pods = v1.list_namespaced_pod(TARGET_NAMESPACE)
            for pod in pods.items:
                if not pod.metadata.labels: continue
                app = pod.metadata.labels.get("app")
                if not app: continue
                if pod.status.pod_ip: new_ip_map[pod.status.pod_ip] = app
                if pod.metadata.uid:
                    uid = pod.metadata.uid
                    new_uid_map[uid] = app
                    new_uid_map[uid.replace("-", "_")] = app
                    new_uid_map[uid.replace("-", "")] = app
            
            services = v1.list_namespaced_service(TARGET_NAMESPACE)
            for svc in services.items:
                if not svc.metadata.labels: continue
                app = svc.metadata.labels.get("app")
                if not app: app = svc.metadata.name 
                if svc.spec.cluster_ip and svc.spec.cluster_ip != "None":
                    new_ip_map[svc.spec.cluster_ip] = app
            
            IP_TO_SVC = new_ip_map
            UID_TO_SVC = new_uid_map
        except Exception as e:
            print(f"K8s Error: {e}")
        time.sleep(5)

def get_service_from_pid(pid):
    if pid in CGROUP_TO_SVC: return CGROUP_TO_SVC[pid]
    try:
        with open(f"/proc/{pid}/cgroup", "r") as f: content = f.read()
        for uid, app in UID_TO_SVC.items():
            if uid.lower() in content.lower():
                CGROUP_TO_SVC[pid] = app
                return app
    except: pass
    return None

class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global LAST_SCRAPE_TIME
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()

        current_time = time.time()
        time_delta = current_time - LAST_SCRAPE_TIME
        if time_delta < 1: time_delta = 1 

        final_data = {
            "metrics": {},
            "topology": {k: list(v) for k, v in TOPOLOGY_STORE.items()}
        }
        
        for svc, data in METRICS_STORE.items():
            count = data["count"]
            if count > 0:
                # Average Microseconds -> Milliseconds
                avg_latency_ms = round((data["sum_us"] / count) / 1000.0, 3)
                rps = round(count / time_delta, 2)
                error_rate = round(data["errors"] / time_delta, 2)
                
                final_data["metrics"][svc] = {
                    "latency": avg_latency_ms,
                    "rps": rps,
                    "error_rate": error_rate,
                    "count": count
                }
                data["errors"] = 0 
                data["sum_us"] = 0
                data["count"] = 0

        LAST_SCRAPE_TIME = current_time
        self.wfile.write(json.dumps(final_data).encode())
    def log_message(self, format, *args): return

def run_agent():
    # CORRECT BPFTRACE SYNTAX
    BPF_CODE = """
    #include <linux/in.h>
    
    // In bpftrace, maps are defined implicitly like @start[key]
    
    // 1. REQUEST START (READ)
    tracepoint:syscalls:sys_exit_read, tracepoint:syscalls:sys_exit_recvfrom { 
        // Only set start time if it DOES NOT exist (First read wins)
        // This effectively ignores internal file reads occurring during the request
        if (@start[tid] == 0) {
            @start[tid] = nsecs;
        }
    }    
    
    // 2. REQUEST END (WRITE)
    tracepoint:syscalls:sys_enter_write, tracepoint:syscalls:sys_enter_sendto {
        if (@start[tid] != 0) {
            $delta_us = (nsecs - @start[tid]) / 1000;
            
            // Filter crazy outliers
            if ($delta_us > 0 && $delta_us < 60000000) { 
                printf("LAT %d %d\\n", pid, $delta_us);
            }
            
            // Clear the timer so next request is fresh
            delete(@start[tid]);
        }
    }
    
    // 3. ERROR TRACKING
    tracepoint:syscalls:sys_exit_write, tracepoint:syscalls:sys_exit_sendto,
    tracepoint:syscalls:sys_exit_read, tracepoint:syscalls:sys_exit_recvfrom {
        if (args->ret < 0) {
            printf("ERR %d %ld\\n", pid, args->ret);
        }
    }
    
    // 4. TOPOLOGY
    tracepoint:syscalls:sys_enter_connect {
        $addr = (struct sockaddr_in *)args->uservaddr;
        if ($addr->sin_family == 2) { 
            printf("CONN %d %s\\n", pid, ntop($addr->sin_addr.s_addr)); 
        }
    }
    """
    with open("sensor.bt", "w") as f: f.write(BPF_CODE)
    
    process = subprocess.Popen(["bpftrace", "sensor.bt"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    print("[*] Unified Sensor Running...", flush=True)

    def log_stderr():
        for line in process.stderr: print(f"BPF ERROR: {line.strip()}", flush=True)
    threading.Thread(target=log_stderr, daemon=True).start()
    
    while True:
        line = process.stdout.readline()
        if not line: break
        try:
            parts = line.split()
            if len(parts) < 3: continue
            event = parts[0]
            pid = int(parts[1])
            svc = get_service_from_pid(pid)
            
            if event == "CONN":
                dest_ip = parts[2]
                dest_svc = IP_TO_SVC.get(dest_ip)
                if svc and dest_svc and svc != dest_svc:
                    if svc not in TOPOLOGY_STORE: TOPOLOGY_STORE[svc] = set()
                    TOPOLOGY_STORE[svc].add(dest_svc)

            if not svc: continue
            
            if event == "LAT":
                lat_us = int(parts[2])
                if svc not in METRICS_STORE: METRICS_STORE[svc] = {"sum_us": 0, "count": 0, "errors": 0}
                METRICS_STORE[svc]["sum_us"] += lat_us
                METRICS_STORE[svc]["count"] += 1
                
            if event == "ERR":
                if svc not in METRICS_STORE: METRICS_STORE[svc] = {"sum_us": 0, "count": 0, "errors": 0}
                METRICS_STORE[svc]["errors"] += 1
            
        except Exception as e:
            pass

def main():
    threading.Thread(target=k8s_metadata_updater, daemon=True).start()
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', 5000), MetricsHandler).serve_forever(), daemon=True).start()
    run_agent()

if __name__ == "__main__":
    main()