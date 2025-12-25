import time
import requests
import logging
from kubernetes import client, config

AGENT_URL = "http://bpf-agent:5000"
SLO_LATENCY_MS = 20
MAX_REPLICAS = 10
COOLDOWN = 30
# IGNORE LIST: Don't try to scale these
IGNORE_SVC = ["bpf-agent", "kubernetes", "coredns"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("Brain")

def get_k8s_apps():
    try: config.load_incluster_config()
    except: config.load_kube_config()
    return client.AppsV1Api()

def scale_deployment(api, deploy_name, current_replicas, reason):
    new_replicas = min(MAX_REPLICAS, current_replicas + 1)
    if new_replicas > current_replicas:
        logger.info(f"⚡ SCALING {deploy_name}: {current_replicas} -> {new_replicas} ({reason})")
        api.patch_namespaced_deployment_scale(
            deploy_name, "default", {"spec": {"replicas": new_replicas}}
        )
        return True
    return False

def main():
    logger.info("🤖 Graph-Aware Controller Started")
    api = get_k8s_apps()
    last_scale = {}

    while True:
        try:
            try:
                data = requests.get(AGENT_URL, timeout=2).json()
            except:
                logger.error("Waiting for Agent connection...")
                time.sleep(5)
                continue

            metrics = data.get("metrics", {})
            topology = data.get("topology", {})

            for svc_name, latency in metrics.items():
                # 1. FILTER: Skip system components
                if svc_name in IGNORE_SVC: continue
                
                if latency <= SLO_LATENCY_MS: continue

                # 2. ROOT CAUSE ANALYSIS
                dependencies = topology.get(svc_name, [])
                blame_downstream = None

                for child_svc in dependencies:
                    child_latency = metrics.get(child_svc, 0)
                    if child_latency > SLO_LATENCY_MS and child_svc not in IGNORE_SVC:
                        blame_downstream = child_svc
                        break
                
                # 3. DECISION
                target_svc = blame_downstream if blame_downstream else svc_name
                reason = f"{svc_name} Latency {latency}ms > SLO"
                if blame_downstream:
                    reason = f"Bottleneck in {target_svc} (blocking {svc_name})"

                if time.time() - last_scale.get(target_svc, 0) > COOLDOWN:
                    try:
                        scale_obj = api.read_namespaced_deployment_scale(target_svc, "default")
                        if scale_deployment(api, target_svc, scale_obj.spec.replicas, reason):
                            last_scale[target_svc] = time.time()
                    except client.exceptions.ApiException as e:
                        if e.status == 404:
                            # Silently ignore if deployment doesn't exist (e.g. it's a DaemonSet or StatefulSet)
                            pass
                        else:
                            logger.warning(f"K8s API Error for {target_svc}: {e}")

        except Exception as e:
            logger.error(f"Loop Error: {e}")
        
        time.sleep(2)

if __name__ == "__main__":
    main()