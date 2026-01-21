from flask import Flask, jsonify
from flask_cors import CORS
import requests
import threading
import time
from kubernetes import client, config

app = Flask(__name__)
CORS(app)  # Allow Frontend to connect

# Cache to store merged data
GLOBAL_DATA = {
    "metrics": {},
    "topology": {}
}

# Load K8s Config
try: config.load_incluster_config()
except: config.load_kube_config()
v1 = client.CoreV1Api()

def fetch_from_agents():
    """
    Background loop:
    1. Find all pods labeled 'app=bpf-agent'
    2. Query each one
    3. Merge the results
    """
    global GLOBAL_DATA
    while True:
        try:
            # 1. Discover Agents
            pods = v1.list_namespaced_pod("default", label_selector="app=bpf-agent")
            
            merged_metrics = {}
            merged_topology = {}

            for pod in pods.items:
                pod_ip = pod.status.pod_ip
                if not pod_ip: continue
                
                try:
                    # 2. Query Agent
                    url = f"http://{pod_ip}:5000"
                    data = requests.get(url, timeout=2).json()
                    
                    # 3. Merge Metrics (Average them if multiple nodes report same service)
                    # Note: For simplicity, we just overwrite/update here. 
                    # In production, you might average them.
                    merged_metrics.update(data.get("metrics", {}))
                    
                    # 4. Merge Topology (Union of sets)
                    topo = data.get("topology", {})
                    for src, dests in topo.items():
                        if src not in merged_topology:
                            merged_topology[src] = set()
                        merged_topology[src].update(dests)
                        
                except Exception as e:
                    print(f"Failed to query agent at {pod_ip}: {e}")

            # Convert sets to list for JSON serialization
            final_topo = {k: list(v) for k, v in merged_topology.items()}
            
            GLOBAL_DATA = {
                "metrics": merged_metrics,
                "topology": final_topo
            }
            print(f"✅ Aggregated data from {len(pods.items)} agents", flush=True)

        except Exception as e:
            print(f"Aggregation Loop Error: {e}")
        
        time.sleep(2)

# Start background thread
threading.Thread(target=fetch_from_agents, daemon=True).start()

@app.route('/api/graph')
def get_graph():
    return jsonify(GLOBAL_DATA)

@app.route('/health')
def health():
    return "OK"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8000)
