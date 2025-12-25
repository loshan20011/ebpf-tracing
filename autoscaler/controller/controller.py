from flask import Flask, request, jsonify
import threading
import time
import logging
from kubernetes import client, config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger()
app = Flask(__name__)

# --- CONFIG ---
SLO_LIMIT = 5  # Aggressive limit (5ms) to force scaling in demo
METRICS = {}       # {'svc-cpu': 15}
DEPENDENCIES = {}  # {'svc-chain': 'svc-cpu'}

# Connect to K8s
try:
    config.load_incluster_config()
    k8s_apps = client.AppsV1Api()
    logger.info("✅ Connected to Kubernetes API")
except:
    logger.warning("⚠️ Running locally (No K8s connection)")

@app.route('/metrics', methods=['POST'])
def receive_metrics():
    data = request.json
    if data and 'service' in data:
        METRICS[data['service']] = data['latency']
    return jsonify({"status": "ok"})

@app.route('/topology', methods=['POST'])
def receive_topology():
    data = request.json
    src = data.get('source')
    dst = data.get('dest')
    if src and dst and src != dst:
        if DEPENDENCIES.get(src) != dst:
            logger.info(f"🔗 NEW DEPENDENCY: {src} depends on {dst}")
            DEPENDENCIES[src] = dst
    return jsonify({"status": "ok"})

def autoscaler_brain():
    logger.info(f"🤖 Brain Started. SLO Limit: {SLO_LIMIT}ms")
    while True:
        time.sleep(2) # Fast loop
        violators = [s for s, l in METRICS.items() if l > SLO_LIMIT]
        
        if not violators: continue

        final_decision = []
        for v in violators:
            root_cause = DEPENDENCIES.get(v)
            if root_cause and root_cause in violators:
                logger.info(f"🛡️ IGNORING {v} (Victim) -> Root Cause is {root_cause}")
            else:
                final_decision.append(v)
        
        for s in final_decision:
            logger.info(f"🚨 SLO VIOLATION: {s} ({METRICS[s]}ms) > {SLO_LIMIT}ms")
            logger.info(f"⚡ SCALING: {s}")
            # Actual scaling call
            try:
                k8s_apps.patch_namespaced_deployment_scale(
                    name=s, namespace="default", body={"spec": {"replicas": 5}} # Scale to 5
                )
            except: pass

if __name__ == '__main__':
    threading.Thread(target=autoscaler_brain, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)