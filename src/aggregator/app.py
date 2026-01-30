from flask import Flask, jsonify
from flask_cors import CORS
import requests
import threading
import time
import redis
from kubernetes import client, config

app = Flask(__name__)
CORS(app)

# Connect to Redis
r = None
def get_redis():
    global r
    try:
        if r is None:
            r = redis.Redis(host='redis', port=6379, db=0, decode_responses=True)
            r.ping()
        return r
    except:
        return None

try: config.load_incluster_config()
except: config.load_kube_config()
v1 = client.CoreV1Api()

def fetch_from_agents():
    while True:
        try:
            redis_conn = get_redis()
            if not redis_conn:
                time.sleep(2)
                continue

            pods = v1.list_namespaced_pod("default", label_selector="app=bpf-agent")
            
            for pod in pods.items:
                pod_ip = pod.status.pod_ip
                if not pod_ip: continue
                
                try:
                    # 1. SCRAPE
                    url = f"http://{pod_ip}:5000"
                    response = requests.get(url, timeout=1)
                    if response.status_code != 200: continue
                    data = response.json()
                    
                    # 2. DUMP METRICS TO REDIS
                    metrics = data.get("metrics", {})
                    for svc, m in metrics.items():
                        # Save metric data
                        redis_conn.hset(f"metric:{svc}", mapping={
                            "latency": str(m["latency"]),
                            "rps": str(m["rps"]),
                            "error_rate": str(m["error_rate"]),
                            "count": str(m["count"])
                        })
                        # Mark service as active
                        redis_conn.sadd("services", svc)
                        # Set expiry so old dead nodes eventually disappear (30s)
                        redis_conn.expire(f"metric:{svc}", 30)

                    # 3. DUMP TOPOLOGY TO REDIS
                    topo = data.get("topology", {})
                    for src, dests in topo.items():
                        for dst in dests:
                            redis_conn.sadd(f"topo:{src}", dst)
                            # Ensure both sides are in the service list
                            redis_conn.sadd("services", src)
                            redis_conn.sadd("services", dst)

                except Exception as e:
                    pass
            
            print(f"✅ Synced {len(pods.items)} agents to Redis", flush=True)

        except Exception as e:
            print(f"Loop Error: {e}")
        
        time.sleep(2)

threading.Thread(target=fetch_from_agents, daemon=True).start()

@app.route('/api/graph')
def get_graph():
    redis_conn = get_redis()
    if not redis_conn: return jsonify({"error": "Redis unavailable"}), 500

    resp_metrics = {}
    resp_topo = {}

    # 1. Get All Services
    services = redis_conn.smembers("services")
    
    for svc in services:
        # Fetch Metrics
        m = redis_conn.hgetall(f"metric:{svc}")
        if m:
            resp_metrics[svc] = {
                "latency": float(m["latency"]),
                "rps": float(m["rps"]),
                "error_rate": float(m["error_rate"]),
                "count": int(m["count"])
            }
        else:
            # Default if no traffic right now
            resp_metrics[svc] = {"latency": 0, "rps": 0, "error_rate": 0, "count": 0}

        # Fetch Topology
        links = redis_conn.smembers(f"topo:{svc}")
        if links:
            resp_topo[svc] = list(links)

    return jsonify({
        "metrics": resp_metrics,
        "topology": resp_topo
    })

@app.route('/api/reset')
def reset():
    get_redis().flushdb()
    return "OK"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8000)