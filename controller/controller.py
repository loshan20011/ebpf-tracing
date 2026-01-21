import time
import requests
import logging
import os
import math
from kubernetes import client, config

# CONFIGURATION
# NOW POINTS TO THE AGGREGATOR SERVICE
AGGREGATOR_URL = os.getenv("AGGREGATOR_URL", "http://aggregator:8000")
COOLDOWN = 15  # Seconds between checks

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("Brain")

# Load K8s Config
try: config.load_incluster_config()
except: config.load_kube_config()

app_api = client.AppsV1Api()
custom_api = client.CustomObjectsApi()

def get_slo_configs():
    """
    Reads all ServiceSLO resources from Kubernetes.
    """
    configs = {}
    try:
        raw = custom_api.list_namespaced_custom_object(
            group="autoscaling.fyp.io",
            version="v1alpha1",
            namespace="default",
            plural="serviceslos"
        )
        for item in raw.get('items', []):
            spec = item.get('spec', {})
            deploy = spec.get('targetDeployment')
            if deploy:
                configs[deploy] = {
                    "slo": spec.get('sloLatency', 30),
                    "min": spec.get('minReplicas', 1),
                    "max": spec.get('maxReplicas', 10)
                }
    except Exception as e:
        logger.error(f"Failed to fetch SLO CRDs: {e}")
    return configs

def scale_deployment(deploy_name, current_replicas, desired_replicas):
    if desired_replicas == current_replicas: return False
    
    try:
        app_api.patch_namespaced_deployment_scale(
            deploy_name, "default", {"spec": {"replicas": desired_replicas}}
        )
        logger.info(f"⚡ SCALING {deploy_name}: {current_replicas} -> {desired_replicas}")
        return True
    except Exception as e:
        logger.warning(f"Failed to scale {deploy_name}: {e}")
        return False

def calculate_replicas(current_replicas, current_latency, target_slo, rps):
    """
    RESEARCH GAP 3 SOLUTION: Deterministic Calculation
    Instead of 'current + 1', we use the deviation ratio.
    Formula: New = Current * (Current_Latency / Target_SLO)
    """
    if current_latency <= target_slo:
        return current_replicas
    
    # Calculate scale factor (e.g., if Latency is 100ms and Target is 50ms, factor is 2.0)
    # We add a small buffer (1.1) to be proactive
    ratio = current_latency / target_slo
    new_count = math.ceil(current_replicas * ratio)
    
    return new_count

def main():
    logger.info(f"🤖 Controller V2 (Aggregator Mode) Started - Connecting to {AGGREGATOR_URL}")
    last_scale = {}

    while True:
        try:
            # 1. Fetch Live Configs
            slo_configs = get_slo_configs()
            
            # 2. Fetch Live Metrics (FROM AGGREGATOR)
            try:
                # UPDATED: Correct Endpoint
                response = requests.get(f"{AGGREGATOR_URL}/api/graph", timeout=2)
                data = response.json()
                metrics = data.get("metrics", {})
                topology = data.get("topology", {})
            except Exception as e:
                logger.error(f"Waiting for Aggregator at {AGGREGATOR_URL}... {e}")
                time.sleep(5)
                continue

            # 3. Analyze
            for svc_name, metric_data in metrics.items():
                
                config = slo_configs.get(svc_name)
                if not config: continue

                target_slo = config['slo']
                
                # Handle data format
                if isinstance(metric_data, int):
                    latency = metric_data
                    rps = 0
                else:
                    latency = metric_data.get("latency", 0)
                    rps = metric_data.get("rps", 0)
                
                # logger.info(f"Checking {svc_name}: {latency}ms (SLO: {target_slo}ms)")
                logger.info(f"🔍 Seeing {svc_name} | Latency: {latency}ms | RPS: {rps}")        

                if rps < 1.0:
                    logger.debug(f"Skipping {svc_name}: Low traffic ({rps} rps) - ignoring latency")
                    continue

                if latency <= target_slo: continue

                # ROOT CAUSE ANALYSIS (Downstream Check)
                dependencies = topology.get(svc_name, [])
                blame_downstream = None

                for child_svc in dependencies:
                    child_data = metrics.get(child_svc, {})
                    child_lat = child_data.get("latency", 0) if isinstance(child_data, dict) else child_data
                    
                    child_cfg = slo_configs.get(child_svc)
                    
                    if child_cfg and child_lat > child_cfg['slo']:
                        blame_downstream = child_svc
                        # RESEARCH GAP 2: In future, calculate impact score here
                        break 
                
                # DECISION
                target_svc = blame_downstream if blame_downstream else svc_name
                target_cfg = slo_configs.get(target_svc)
                
                if not target_cfg: continue

                # Check Cooldown
                if time.time() - last_scale.get(target_svc, 0) < COOLDOWN:
                    continue

                # Calculate Scale Up
                try:
                    scale_obj = app_api.read_namespaced_deployment_scale(target_svc, "default")
                    curr_replicas = scale_obj.spec.replicas
                    
                    # USE DETERMINISTIC LOGIC (Gap 3)
                    ideal_replicas = calculate_replicas(curr_replicas, latency, target_slo, rps)
                    
                    # Apply Limits (Min/Max)
                    new_replicas = min(target_cfg['max'], max(target_cfg['min'], ideal_replicas))
                    
                    if new_replicas > curr_replicas:
                        reason = f"Bottleneck in {target_svc}" if blame_downstream else f"{target_svc} Latency {latency}ms > {target_slo}ms"
                        logger.info(f"⚠️  Logic: {reason} | RPS: {rps} | Calculating: {curr_replicas} -> {new_replicas}")
                        
                        if scale_deployment(target_svc, curr_replicas, new_replicas):
                            last_scale[target_svc] = time.time()
                            
                except Exception as e:
                    logger.error(f"Error processing {target_svc}: {e}")

        except Exception as e:
            logger.error(f"Loop Error: {e}")
        
        time.sleep(2)

if __name__ == "__main__":
    main()