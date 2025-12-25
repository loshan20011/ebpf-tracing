import subprocess
import re
import threading
import sys
import time
import os
from kubernetes import client, config

print("[*] Initializing Full Chain Visualizer...", flush=True)

try:
    config.load_kube_config()
    v1 = client.CoreV1Api()
except:
    sys.exit(1)

IP_MAP = {}
GRAPH_EDGES = set()

def update_map():
    global IP_MAP
    while True:
        try:
            new_map = {}
            for svc in v1.list_service_for_all_namespaces().items:
                if svc.spec.cluster_ip: new_map[svc.spec.cluster_ip] = svc.metadata.name
            for pod in v1.list_pod_for_all_namespaces().items:
                if pod.status.pod_ip: new_map[pod.status.pod_ip] = pod.metadata.name
            IP_MAP = new_map
        except: pass
        time.sleep(5)

def clean_name(name):
    # Simplify names for the diagram
    if "svc-cpu" in name: return "CPU"
    if "svc-mem" in name: return "Memory"
    if "svc-io" in name: return "IO"
    if "svc-chain" in name: return "Chain"
    if "gateway" in name: return "Gateway"
    return name

def draw_graph():
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\n╔══════════════════════════════════════════╗")
    print("║   ⛓️  FULL MICROSERVICE DEPENDENCY MAP    ║")
    print("╚══════════════════════════════════════════╝")
    print("graph LR")
    
    for edge in sorted(list(GRAPH_EDGES)):
        print(f"    {edge}")

def main():
    threading.Thread(target=update_map, daemon=True).start()
    
    cmd = ["kubectl", "exec", "ds/bpf-agent", "--", "python3", "-u", "topology-agent.py"]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    while True:
        line = process.stdout.readline()
        if not line: break
        
        # New Format: [GRAPH] SourceName --> DestIP
        match = re.search(r'\[GRAPH\] (.*?) --> ([0-9\.]+)', line)
        if match:
            source_raw = match.group(1)
            dest_ip = match.group(2)
            
            dest_raw = IP_MAP.get(dest_ip)
            
            if dest_raw:
                src = clean_name(source_raw)
                dst = clean_name(dest_raw)
                
                # Prevent self-loops or boring traffic
                if src != dst and "kube" not in dst:
                    edge = f"{src} --> {dst}"
                    if edge not in GRAPH_EDGES:
                        GRAPH_EDGES.add(edge)
                        draw_graph()

if __name__ == "__main__":
    main()