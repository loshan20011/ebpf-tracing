import subprocess
import threading
import json
import time
import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from kubernetes import client, config

# --- V40 SMART MATCH MODE ---
TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "default")
print(f"[*] Unified Agent V40 (SMART MATCH) - Namespace: {TARGET_NAMESPACE}", flush=True)

METRICS_STORE = {}
TOPOLOGY_STORE = {}
IP_TO_SVC = {}
UID_TO_SVC = {}
CGROUP_TO_SVC = {}

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
            count = 0
            for pod in pods.items:
                if not pod.metadata.labels: continue
                app = pod.metadata.labels.get("app")
                if not app: continue
                
                # IP -> App
                if pod.status.pod_ip: new_ip_map[pod.status.pod_ip] = app
                
                # UID -> App (Store multiple formats)
                if pod.metadata.uid:
                    uid = pod.metadata.uid
                    new_uid_map[uid] = app                 # Normal: a1-b2...
                    new_uid_map[uid.replace("-", "_")] = app # Underscore: a1_b2...
                    new_uid_map[uid.replace("-", "")] = app  # No Dash: a1b2...
                    count += 1
            
            services = v1.list_namespaced_service(TARGET_NAMESPACE)
            for svc in services.items:
                if not svc.metadata.labels: continue
                app = svc.metadata.labels.get("app")
                if not app: app = svc.metadata.name 
                if svc.spec.cluster_ip and svc.spec.cluster_ip != "None":
                    new_ip_map[svc.spec.cluster_ip] = app
            
            IP_TO_SVC = new_ip_map
            UID_TO_SVC = new_uid_map
            # Heartbeat to confirm we have data
            if count > 0:
                print(f"✅ Metadata Sync: Tracking {count} Pods & {len(services.items)} Services", flush=True)
            else:
                print(f"⚠️ Metadata Sync: Found 0 Pods (Check Namespace/Labels)", flush=True)

        except Exception as e:
            print(f"K8s Error: {e}")
        time.sleep(5)

def get_service_from_pid(pid):
    if pid in CGROUP_TO_SVC: return CGROUP_TO_SVC[pid]
    try:
        # Read the FULL cgroup path
        with open(f"/proc/{pid}/cgroup", "r") as f: content = f.read()
        
        # Heuristic: If it's a kubepods cgroup, try hard to find a match
        for uid, app in UID_TO_SVC.items():
            # Case-insensitive substring match
            if uid.lower() in content.lower():
                CGROUP_TO_SVC[pid] = app
                return app
    except: pass
    return None

class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        final_data = {
            "metrics": {},
            "topology": {k: list(v) for k, v in TOPOLOGY_STORE.items()}
        }
        for svc, data in METRICS_STORE.items():
            if data["count"] > 0:
                final_data["metrics"][svc] = int(data["sum"] / data["count"])
                data["sum"] = 0
                data["count"] = 0
        self.wfile.write(json.dumps(final_data).encode())
    def log_message(self, format, *args): return

def run_agent():
    BPF_CODE = """
    #include <linux/in.h>
    tracepoint:syscalls:sys_exit_read, tracepoint:syscalls:sys_exit_recvfrom { @start[pid] = nsecs; }
    
    tracepoint:syscalls:sys_enter_write, tracepoint:syscalls:sys_enter_sendto {
        $s = @start[pid];
        if ($s != 0) {
            $delta = (nsecs - $s) / 1000000;
            if ($delta > 2) { printf("LAT %d %d\\n", pid, $delta); }
            delete(@start[pid]);
        }
    }
    
    tracepoint:syscalls:sys_enter_connect {
        $addr = (struct sockaddr_in *)args->uservaddr;
        if ($addr->sin_family == 2) { printf("CONN %d %s\\n", pid, ntop($addr->sin_addr.s_addr)); }
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
            
            # --- DEBUG LOGGING ---
            if event == "CONN":
                dest_ip = parts[2]
                dest_svc = IP_TO_SVC.get(dest_ip)
                
                # Only log relevant connections (ignore localhost/unknowns for clarity)
                if svc and dest_svc and svc != dest_svc:
                    print(f"🔗 DETECTED EDGE: {svc} -> {dest_svc}", flush=True)
                    if svc not in TOPOLOGY_STORE: TOPOLOGY_STORE[svc] = set()
                    TOPOLOGY_STORE[svc].add(dest_svc)
                elif svc and not dest_svc and not dest_ip.startswith("127."):
                     # Log unidentified external calls to help debugging
                     print(f"❓ {svc} -> {dest_ip} (Unknown Dest)", flush=True)
                elif not svc and "kubepods" in str(pid): # Pseudo-check if we missed a pod
                     pass 

            if not svc: continue
            
            if event == "LAT":
                lat = int(parts[2])
                if svc not in METRICS_STORE: METRICS_STORE[svc] = {"sum": 0, "count": 0}
                METRICS_STORE[svc]["sum"] += lat
                METRICS_STORE[svc]["count"] += 1
                
        except Exception as e:
            pass

def main():
    threading.Thread(target=k8s_metadata_updater, daemon=True).start()
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', 5000), MetricsHandler).serve_forever(), daemon=True).start()
    run_agent()

if __name__ == "__main__":
    main()