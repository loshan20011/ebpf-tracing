import time
import requests
import math
import logging
from kubernetes import client, config

# --- CONFIGURATION ---
AGENT_URL = "http://bpf-agent:5000"
SLO_LATENCY_MS = 500
SLO_CPU_THRESHOLD = 50
MIN_REPLICAS = 1
MAX_REPLICAS = 10
COOLDOWN_SECONDS = 30

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("Brain")

def get_k8s_client():
    try:
        config.load_incluster_config()
    except:
        config.load_kube_config()
    return client.AppsV1Api(), client.CoreV1Api()

def get_deployment_name(pod_uid, core_v1, apps_v1):
    try:
        # 1. Find Pod
        all_pods = core_v1.list_namespaced_pod("default")
        target_pod = None
        for pod in all_pods.items:
            if pod.metadata.uid == pod_uid:
                target_pod = pod
                break
        
        if not target_pod or not target_pod.metadata.owner_references: 
            return None

        # 2. Check Owner Type (CRITICAL FIX)
        owner = target_pod.metadata.owner_references[0]
        
        # IGNORE DaemonSets (like bpf-agent) and StatefulSets
        if owner.kind != "ReplicaSet":
            return None 
            
        # 3. Get ReplicaSet
        rs_name = owner.name
        rs = apps_v1.read_namespaced_replica_set(rs_name, "default")
        
        # 4. Get Deployment
        if rs.metadata.owner_references:
            return rs.metadata.owner_references[0].name
            
    except Exception as e:
        # logger.error(f"Mapping Error: {e}")
        return None
    return None

def calculate_replicas_queueing_theory(current_replicas, measured_value, target_value):
    if measured_value <= target_value: return current_replicas
    ratio = measured_value / target_value
    needed = math.ceil(current_replicas * ratio)
    return min(MAX_REPLICAS, int(needed))

def main():
    logger.info(f"🤖 Controller v7 Started (Fix: Ignoring DaemonSets)")
    apps_v1, core_v1 = get_k8s_client()
    last_scale_time = {}

    while True:
        try:
            try:
                response = requests.get(AGENT_URL, timeout=2)
                metrics = response.json()
            except:
                logger.error(f"❌ Cannot connect to Agent at {AGENT_URL}")
                time.sleep(5)
                continue

            for uid, data in metrics.items():
                m_type = data.get("type")
                val = float(data.get("value", 0))

                deploy_name = get_deployment_name(uid, core_v1, apps_v1)
                
                # If it returns None (e.g. it's the Agent itself), SKIP IT
                if not deploy_name: 
                    continue

                if time.time() - last_scale_time.get(deploy_name, 0) < COOLDOWN_SECONDS:
                    continue

                # --- CHECK SLO ---
                if m_type == "SLO_LATENCY":
                    if val > SLO_LATENCY_MS:
                        logger.warning(f"🚨 SLO VIOLATION: {deploy_name} Latency {int(val)}ms > {SLO_LATENCY_MS}ms")
                        scale = apps_v1.read_namespaced_deployment_scale(deploy_name, "default")
                        curr = scale.spec.replicas
                        target = calculate_replicas_queueing_theory(curr, val, SLO_LATENCY_MS)
                        
                        if target > curr:
                            logger.info(f"⚡ SCALING: {deploy_name} {curr} -> {target} Replicas")
                            patch = {"spec": {"replicas": target}}
                            apps_v1.patch_namespaced_deployment_scale(deploy_name, "default", patch)
                            last_scale_time[deploy_name] = time.time()

        except Exception as e:
            logger.error(f"Loop Error: {e}")
        
        time.sleep(2)

if __name__ == "__main__":
    main()