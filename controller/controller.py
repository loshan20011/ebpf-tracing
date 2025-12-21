import time
import requests
import math
import logging
from kubernetes import client, config

# --- CONFIGURATION ---
AGENT_URL = "http://10.42.0.107:5000"
SLO_LATENCY_MS = 200
SLO_CPU_THRESHOLD = 5  # Sensitive threshold for demo
MIN_REPLICAS = 1
MAX_REPLICAS = 10
COOLDOWN_SECONDS = 30

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("Brain")

def get_k8s_client():
    try:
        config.load_incluster_config()
    except:
        config.load_kube_config()
    return client.AppsV1Api(), client.CoreV1Api()

def get_deployment_name(pod_uid, core_v1, apps_v1):
    """ 
    Maps a Pod UID -> ReplicaSet -> Deployment 
    Fixed: Uses apps_v1 for ReplicaSet lookup
    """
    try:
        # 1. Find the Pod
        all_pods = core_v1.list_namespaced_pod("default")
        target_pod = None
        for pod in all_pods.items:
            if pod.metadata.uid == pod_uid:
                target_pod = pod
                break
        
        if not target_pod: return None

        # 2. Get the Parent (ReplicaSet)
        if not target_pod.metadata.owner_references: return None
        rs_name = target_pod.metadata.owner_references[0].name
        
        # 3. Look up the ReplicaSet using APPS_V1 (The Fix)
        rs = apps_v1.read_namespaced_replica_set(rs_name, "default")
        
        # 4. Get the Grandparent (Deployment)
        if rs.metadata.owner_references:
            return rs.metadata.owner_references[0].name
            
    except Exception as e:
        # Uncomment to see specific API errors
        # logger.error(f"Mapping Error: {e}")
        return None
    return None

def calculate_replicas_queueing_theory(current_replicas, measured_value, target_value):
    if measured_value <= target_value: return current_replicas
    ratio = measured_value / target_value
    needed = math.ceil(current_replicas * ratio)
    return min(MAX_REPLICAS, int(needed))

def main():
    logger.info("🤖 Queueing-Theoretic Controller Started (v5 Fixed)")
    apps_v1, core_v1 = get_k8s_client()
    last_scale_time = {}

    while True:
        try:
            try:
                response = requests.get(AGENT_URL, timeout=2)
                metrics = response.json()
            except:
                logger.error("❌ Agent Connection Failed")
                time.sleep(5)
                continue

            for uid, data in metrics.items():
                m_type = data.get("type")
                val = int(data.get("value", 0))

                # Pass BOTH clients here
                deploy_name = get_deployment_name(uid, core_v1, apps_v1)
                
                if not deploy_name: 
                    # Only log warning if we have a high value, otherwise ignore noise
                    if val > 50: 
                        logger.warning(f"⚠️ High load on {uid} but cannot map to Deployment.")
                    continue

                if time.time() - last_scale_time.get(deploy_name, 0) < COOLDOWN_SECONDS:
                    logger.info(f"❄️ {deploy_name} Cooldown.")
                    continue

                # --- CHECK SLO ---
                scale = apps_v1.read_namespaced_deployment_scale(deploy_name, "default")
                current_replicas = scale.spec.replicas
                
                if m_type == "CPU" and val > SLO_CPU_THRESHOLD:
                    logger.warning(f"🔥 SLO VIOLATION: {deploy_name} Score {val} > {SLO_CPU_THRESHOLD}")
                    target = calculate_replicas_queueing_theory(current_replicas, val, SLO_CPU_THRESHOLD)
                    
                    if target > current_replicas:
                        logger.info(f"⚡ SCALING: {deploy_name} {current_replicas} -> {target}")
                        patch = {"spec": {"replicas": target}}
                        apps_v1.patch_namespaced_deployment_scale(deploy_name, "default", patch)
                        last_scale_time[deploy_name] = time.time()
                
                elif m_type == "CPU":
                     logger.info(f"✅ {deploy_name} Healthy (Score: {val})")

        except Exception as e:
            logger.error(f"Loop Error: {e}")
        
        time.sleep(5)

if __name__ == "__main__":
    main()