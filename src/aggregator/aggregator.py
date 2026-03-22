import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import redis
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from aggregator_benchmark import (
    current_ts_name,
    derive_routes_from_slos,
    detect_benchmark_profile,
    join_target_url,
    route_to_path,
    sanitize_name_part,
)
from aggregator_metrics import (
    TruthWindowStore,
    aggregate_service_metrics,
    compute_exclusive_delays,
    derive_latency_split,
    get_latest_event_age_seconds,
    get_latest_topology_age_seconds,
    ingest_events,
    is_infra_service,
    learn_runq_baseline,
    persist_service_evidence,
    read_event_watermark,
    runq_learning_enabled,
    service_activity_state,
)

app = Flask(__name__)
CORS(app)

TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "default")
SCRAPE_INTERVAL_SECONDS = float(os.getenv("SCRAPE_INTERVAL_SECONDS", "2"))
WINDOW_SHORT_SECONDS = float(os.getenv("WINDOW_SHORT_SECONDS", os.getenv("WINDOW_SECONDS", "15")))
WINDOW_LONG_SECONDS = float(os.getenv("WINDOW_LONG_SECONDS", "180"))
METRIC_STALE_AFTER_SECONDS = float(os.getenv("METRIC_STALE_AFTER_SECONDS", "12"))
TOPOLOGY_STALE_AFTER_SECONDS = float(os.getenv("TOPOLOGY_STALE_AFTER_SECONDS", str(max(30, int(WINDOW_SHORT_SECONDS * 2)))))
AGENT_PORT = int(os.getenv("AGENT_PORT", "5000"))
AGENT_LABEL_SELECTOR = os.getenv("AGENT_LABEL_SELECTOR", "app=bpf-agent")
AGENT_NAMESPACE = os.getenv("AGENT_NAMESPACE", "default")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "50"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "0.8"))
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", "64"))
REDIS_POOL_TIMEOUT = float(os.getenv("REDIS_POOL_TIMEOUT", "5"))
REDIS_STARTUP_RETRIES = int(os.getenv("REDIS_STARTUP_RETRIES", "12"))
REDIS_STARTUP_DELAY_SECONDS = float(os.getenv("REDIS_STARTUP_DELAY_SECONDS", "2"))
TRACE_REDIS_KEY = os.getenv("TRACE_REDIS_KEY", "decision_traces")
TRACE_RETURN_LIMIT = int(os.getenv("TRACE_RETURN_LIMIT", "120"))
AUDIT_REDIS_KEY = os.getenv("AUDIT_REDIS_KEY", "audit_events")
AUDIT_REDIS_MAX = int(os.getenv("AUDIT_REDIS_MAX", "300"))
AUDIT_RETURN_LIMIT = int(os.getenv("AUDIT_RETURN_LIMIT", "120"))
SUPPORT_TICKET_KEY_PREFIX = os.getenv("SUPPORT_TICKET_KEY_PREFIX", "support:ticket:")
SUPPORT_TICKET_ORDER_KEY = os.getenv("SUPPORT_TICKET_ORDER_KEY", "support:tickets")
SUPPORT_TICKET_SEQ_KEY = os.getenv("SUPPORT_TICKET_SEQ_KEY", "support:ticket:seq")
SUPPORT_TICKET_RETURN_LIMIT = int(os.getenv("SUPPORT_TICKET_RETURN_LIMIT", "120"))
CONTROL_API_TOKEN = os.getenv("CONTROL_API_TOKEN", "").strip()
TRAFFIC_JOB_IMAGE = os.getenv("TRAFFIC_JOB_IMAGE", "curlimages/curl:8.12.1")
TRAFFIC_TARGET_BASE_URL = os.getenv("TRAFFIC_TARGET_BASE_URL", "http://gateway")
TRAFFIC_JOB_TTL_SECONDS = int(os.getenv("TRAFFIC_JOB_TTL_SECONDS", "180"))
RUNQ_ZSCORE_K = float(os.getenv("RUNQ_ZSCORE_K", "3.0"))
RUNQ_CONF_MIN_SAMPLES = int(os.getenv("RUNQ_CONF_MIN_SAMPLES", "20"))
RUNQ_CONF_MIN_HEALTHY_WINDOWS = int(os.getenv("RUNQ_CONF_MIN_HEALTHY_WINDOWS", str(RUNQ_CONF_MIN_SAMPLES)))
RUNQ_CONF_MIN_RUNQ_SAMPLES = int(os.getenv("RUNQ_CONF_MIN_RUNQ_SAMPLES", "60"))
INFRA_EXACT_ENV = os.getenv("INFRA_EXACT_SERVICES", "")
INFRA_PREFIX_ENV = os.getenv("INFRA_PREFIXES", "")
TOPO_EDGE_TTL_SECONDS = int(os.getenv("TOPO_EDGE_TTL_SECONDS", "120"))
TOPO_MAX_CHILDREN = int(os.getenv("TOPO_MAX_CHILDREN", "12"))
RUNQ_LEARNING_ENABLED_KEY = os.getenv("RUNQ_LEARNING_ENABLED_KEY", "runq:learning_enabled")
TRUTH_STALE_AFTER_SECONDS = float(os.getenv("TRUTH_STALE_AFTER_SECONDS", str(METRIC_STALE_AFTER_SECONDS)))


def resolve_redis_endpoint():
    # Preferred explicit vars.
    explicit_host = os.getenv("REDIS_HOST")
    explicit_port = os.getenv("REDIS_PORT")
    if explicit_host and explicit_port and explicit_port.isdigit():
        return explicit_host, int(explicit_port)

    # K8s service env style: REDIS_PORT=tcp://10.x.x.x:6379
    redis_port_var = os.getenv("REDIS_PORT", "")
    if redis_port_var.startswith("tcp://"):
        parsed = urlparse(redis_port_var)
        if parsed.hostname and parsed.port:
            return parsed.hostname, int(parsed.port)

    # Fallback to standard Kubernetes injected host + numeric port vars.
    svc_host = os.getenv("REDIS_SERVICE_HOST")
    svc_port = os.getenv("REDIS_SERVICE_PORT")
    if svc_host and svc_port and svc_port.isdigit():
        return svc_host, int(svc_port)

    # Final defaults.
    host = explicit_host or "redis"
    port = 6379
    if explicit_port and explicit_port.isdigit():
        port = int(explicit_port)
    return host, port


REDIS_HOST, REDIS_PORT = resolve_redis_endpoint()

POOL = redis.BlockingConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=0,
    decode_responses=True,
    max_connections=REDIS_MAX_CONNECTIONS,
    timeout=REDIS_POOL_TIMEOUT,
)
REDIS_CLIENT = redis.Redis(connection_pool=POOL)
TRUTH_STORE = TruthWindowStore(WINDOW_LONG_SECONDS, TRUTH_STALE_AFTER_SECONDS)

SEQ_LOCK = threading.Lock()
SEQ = 0

K8S_MAP_LOCK = threading.Lock()
IP_TO_SVC = {}
LAST_MAP_REFRESH = 0.0
SLO_MAP = {}
SLO_CONFIG_MAP = {}
LAST_SLO_REFRESH = 0.0


def get_redis():
    return REDIS_CLIENT


def get_truth_store():
    return TRUTH_STORE


def control_auth_enabled():
    return bool(CONTROL_API_TOKEN)


def operator_identity():
    for key in ("X-Operator", "X-Forwarded-User", "X-Real-IP"):
        value = request.headers.get(key, "").strip()
        if value:
            return value
    return request.remote_addr or "unknown"


def append_audit_event(kind, outcome, details=None):
    redis_cli = get_redis()
    payload = {
        "ts_unix_ms": int(time.time() * 1000),
        "kind": str(kind),
        "outcome": str(outcome),
        "actor": operator_identity(),
        "path": request.path,
        "method": request.method,
        "details": details or {},
    }
    redis_cli.lpush(AUDIT_REDIS_KEY, json.dumps(payload))
    redis_cli.ltrim(AUDIT_REDIS_KEY, 0, max(AUDIT_REDIS_MAX - 1, 0))
    return payload


def control_auth_failed_response():
    payload = {
        "ok": False,
        "error": "control authorization failed",
        "control_auth_enabled": True,
    }
    append_audit_event("auth", "rejected", {"reason": "invalid_control_token"})
    return jsonify(payload), 403


def require_control_auth():
    if not control_auth_enabled():
        return None
    supplied = request.headers.get("X-Control-Token", "")
    if supplied and supplied == CONTROL_API_TOKEN:
        return None
    return control_auth_failed_response()


def recent_audit_events(limit=AUDIT_RETURN_LIMIT):
    redis_cli = get_redis()
    rows = redis_cli.lrange(AUDIT_REDIS_KEY, 0, max(int(limit) - 1, 0))
    out = []
    for row in rows:
        try:
            out.append(json.loads(row))
        except Exception:
            continue
    return out


def support_ticket_key(ticket_id):
    return f"{SUPPORT_TICKET_KEY_PREFIX}{ticket_id}"


def next_ticket_id(redis_cli):
    seq = int(redis_cli.incr(SUPPORT_TICKET_SEQ_KEY))
    return f"T-{seq:05d}"


def load_ticket(redis_cli, ticket_id):
    raw = redis_cli.get(support_ticket_key(ticket_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_ticket(redis_cli, ticket):
    ticket_id = str(ticket["id"])
    redis_cli.set(support_ticket_key(ticket_id), json.dumps(ticket))
    redis_cli.lrem(SUPPORT_TICKET_ORDER_KEY, 0, ticket_id)
    redis_cli.lpush(SUPPORT_TICKET_ORDER_KEY, ticket_id)
    redis_cli.ltrim(SUPPORT_TICKET_ORDER_KEY, 0, max(SUPPORT_TICKET_RETURN_LIMIT - 1, 0))


def list_tickets(redis_cli, limit=SUPPORT_TICKET_RETURN_LIMIT):
    ids = redis_cli.lrange(SUPPORT_TICKET_ORDER_KEY, 0, max(int(limit) - 1, 0))
    out = []
    for ticket_id in ids:
        ticket = load_ticket(redis_cli, ticket_id)
        if ticket:
            out.append(ticket)
    return out


def next_seq():
    global SEQ
    with SEQ_LOCK:
        SEQ += 1
        return SEQ


def percentile_p90(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int((len(ordered) * 0.9) - 1)
    if idx < 0:
        idx = 0
    if idx >= len(ordered):
        idx = len(ordered) - 1
    return float(ordered[idx])


def load_k8s_config():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()


load_k8s_config()
v1 = client.CoreV1Api()
custom_api = client.CustomObjectsApi()
apps_api = client.AppsV1Api()
batch_api = client.BatchV1Api()


def refresh_ip_map():
    global IP_TO_SVC, LAST_MAP_REFRESH
    now = time.time()
    if now - LAST_MAP_REFRESH < 2:
        return
    try:
        new_map = {}

        namespaces = {TARGET_NAMESPACE}
        if AGENT_NAMESPACE:
            namespaces.add(AGENT_NAMESPACE)

        for namespace in namespaces:
            pods = v1.list_namespaced_pod(namespace)
            for pod in pods.items:
                if not pod.status.pod_ip:
                    continue
                app_label = (pod.metadata.labels.get("app") or pod.metadata.labels.get("name")) if pod.metadata and pod.metadata.labels else None
                if not app_label and pod.metadata and pod.metadata.name:
                    app_label = pod.metadata.name
                if app_label:
                    new_map[pod.status.pod_ip] = app_label

            services = v1.list_namespaced_service(namespace)
            for svc in services.items:
                if not svc.spec.cluster_ip or svc.spec.cluster_ip == "None":
                    continue
                app_label = (svc.metadata.labels.get("app") or svc.metadata.labels.get("name")) if svc.metadata and svc.metadata.labels else svc.metadata.name
                if app_label:
                    new_map[svc.spec.cluster_ip] = app_label

        with K8S_MAP_LOCK:
            IP_TO_SVC = new_map
            LAST_MAP_REFRESH = now
    except Exception as exc:
        print(f"IP map refresh error: {exc}", flush=True)


def refresh_slo_map():
    global SLO_MAP, SLO_CONFIG_MAP, LAST_SLO_REFRESH
    now = time.time()
    if now - LAST_SLO_REFRESH < 5:
        return
    try:
        raw = custom_api.list_namespaced_custom_object(
            group="autoscaling.fyp.io",
            version="v1alpha1",
            namespace=TARGET_NAMESPACE,
            plural="serviceslos",
        )
        new_map = {}
        new_cfg = {}
        for item in raw.get("items", []):
            metadata = item.get("metadata", {})
            spec = item.get("spec", {})
            target = spec.get("targetDeployment")
            if not target:
                continue
            new_map[target] = float(spec.get("sloLatency", 0))
            priority = str(spec.get("priority", "secondary")).strip().lower() or "secondary"
            if priority not in {"primary", "secondary"}:
                priority = "secondary"
            new_cfg[target] = {
                "name": metadata.get("name", ""),
                "sloLatency": float(spec.get("sloLatency", 0)),
                "minReplicas": int(spec.get("minReplicas", 1)),
                "maxReplicas": int(spec.get("maxReplicas", 10)),
                "priority": priority,
            }
        with K8S_MAP_LOCK:
            SLO_MAP = new_map
            SLO_CONFIG_MAP = new_cfg
            LAST_SLO_REFRESH = now
    except Exception as exc:
        print(f"SLO map refresh error: {exc}", flush=True)


def ip_to_service(ip):
    with K8S_MAP_LOCK:
        return IP_TO_SVC.get(ip)


def scrape_agent_events(pod_ip):
    try:
        url = f"http://{pod_ip}:{AGENT_PORT}/api/events"
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


INFRA_EXACT = {
    "aggregator",
    "autoscaler",
    "custom-autoscaler",
    "bpf-agent",
    "coredns",
    "traefik",
    "metrics-server",
    "local-path-provisioner",
    "kubernetes",
    "redis",
    "redis-cart",
    "mycurlpod",
}

INFRA_PREFIXES = (
    "kube-",
    "aws-",
    "envoy-",
    "load-",
)

if INFRA_EXACT_ENV.strip():
    INFRA_EXACT = {s.strip().lower() for s in INFRA_EXACT_ENV.split(",") if s.strip()}

if INFRA_PREFIX_ENV.strip():
    INFRA_PREFIXES = tuple(p.strip().lower() for p in INFRA_PREFIX_ENV.split(",") if p.strip())


def parse_int(payload, key, default, lo, hi):
    try:
        val = int(payload.get(key, default))
    except Exception:
        val = default
    return max(lo, min(hi, val))


def fetch_from_agents_loop():
    print(
        f"[*] Aggregator loop started (raw /api/events mode) target_ns={TARGET_NAMESPACE} agent_ns={AGENT_NAMESPACE} "
        f"runq_conf_healthy_windows={RUNQ_CONF_MIN_HEALTHY_WINDOWS} "
        f"runq_conf_runq_samples={RUNQ_CONF_MIN_RUNQ_SAMPLES}",
        flush=True,
    )
    while True:
        try:
            refresh_ip_map()
            refresh_slo_map()
            pods = v1.list_namespaced_pod(AGENT_NAMESPACE, label_selector=AGENT_LABEL_SELECTOR)
            pod_ips = [p.status.pod_ip for p in pods.items if p.status.pod_ip]

            if not pod_ips:
                time.sleep(SCRAPE_INTERVAL_SECONDS)
                continue

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                responses = list(executor.map(scrape_agent_events, pod_ips))

            redis_cli = get_redis()
            batches = 0
            ingested = 0
            dropped_total = 0

            for data in responses:
                if not data:
                    continue
                events = data.get("events", [])
                dropped_total += int(data.get("dropped_events", 0))
                ingest_events(redis_cli, events, next_seq, ip_to_service, INFRA_EXACT, INFRA_PREFIXES)
                batches += 1
                ingested += len(events)

            if batches > 0:
                print(
                    f"✅ Aggregated {ingested} events from {batches}/{len(pod_ips)} agents (agent_dropped={dropped_total})",
                    flush=True,
                )
        except Exception as exc:
            print(f"Aggregator loop error: {exc}", flush=True)

        time.sleep(SCRAPE_INTERVAL_SECONDS)


def aggregator_startup_checks():
    redis_cli = get_redis()
    last_exc = None
    for attempt in range(max(1, REDIS_STARTUP_RETRIES)):
        try:
            redis_cli.ping()
            last_exc = None
            break
        except redis.exceptions.RedisError as exc:
            last_exc = exc
            if attempt == max(1, REDIS_STARTUP_RETRIES) - 1:
                raise
            print(
                f"Aggregator startup waiting for Redis ({attempt + 1}/{max(1, REDIS_STARTUP_RETRIES)}): {exc}",
                flush=True,
            )
            time.sleep(max(0.2, REDIS_STARTUP_DELAY_SECONDS))
    if last_exc is not None:
        raise last_exc
    refresh_slo_map()
    refresh_ip_map()
    # Verify target namespace is readable using namespaced resources the
    # service account is already allowed to access. Avoid cluster-scoped
    # namespace reads that require broader RBAC.
    apps_api.list_namespaced_deployment(TARGET_NAMESPACE, limit=1)
    return True


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "component": "aggregator"})


@app.route("/readyz")
def readyz():
    try:
        aggregator_startup_checks()
        return jsonify(
            {
                "ok": True,
                "component": "aggregator",
                "target_namespace": TARGET_NAMESPACE,
                "redis_host": REDIS_HOST,
                "redis_port": REDIS_PORT,
                "slo_count": len(SLO_CONFIG_MAP),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "component": "aggregator", "error": str(exc)}), 503


@app.route("/api/graph")
def get_graph():
    redis_cli = get_redis()
    truth_store = get_truth_store()
    response_metrics = {}
    response_topology = {}
    response_topology_meta = {}

    try:
        refresh_slo_map()
        with K8S_MAP_LOCK:
            slo_map = SLO_MAP.copy()
            k8s_services = {str(svc).strip() for svc in IP_TO_SVC.values() if str(svc).strip()}

        discovered = set(redis_cli.smembers("services"))
        discovered.update(slo_map.keys())
        discovered.update(k8s_services)
        all_services = sorted(list(discovered))
        now_ms, now_sec = read_event_watermark(redis_cli)
        learning_enabled = runq_learning_enabled(redis_cli)
        cutoff_short_ms = now_ms - int(WINDOW_SHORT_SECONDS * 1000)
        cutoff_long_ms = now_ms - int(WINDOW_LONG_SECONDS * 1000)

        for svc in all_services:
            short_m = aggregate_service_metrics(redis_cli, svc, cutoff_short_ms, now_sec, WINDOW_SHORT_SECONDS, truth={})
            long_m = aggregate_service_metrics(redis_cli, svc, cutoff_long_ms, now_sec, WINDOW_LONG_SECONDS, truth={})
            slo_latency = float(slo_map.get(svc, 0.0))
            runq_baseline, runq_std_dev, runq_healthy_windows, runq_baseline_samples = learn_runq_baseline(
                redis_cli, svc, short_m, slo_latency, learning_enabled=learning_enabled
            )
            runq_confident = (
                runq_healthy_windows >= RUNQ_CONF_MIN_HEALTHY_WINDOWS
                and runq_baseline_samples >= RUNQ_CONF_MIN_RUNQ_SAMPLES
            )
            dynamic_runq_threshold = round(runq_baseline + (RUNQ_ZSCORE_K * runq_std_dev), 3) if runq_confident else 0.0

            metric = {
                **short_m,
                "slo_latency": slo_latency,
                "runq_baseline": runq_baseline,
                "runq_std_dev": runq_std_dev,
                "runq_baseline_count": int(runq_healthy_windows),
                "runq_healthy_windows": int(runq_healthy_windows),
                "runq_baseline_sample_count": int(runq_baseline_samples),
                "runq_confident": bool(runq_confident),
                "runq_dynamic_threshold": dynamic_runq_threshold,
                "latency_long": long_m["p90_latency"],
                "p90_latency_long": long_m["p90_latency"],
                "raw_p90_latency_long": long_m.get("raw_p90_latency", 0.0),
                "avg_runq_latency_long": long_m["avg_runq_latency"],
                "cpu_throttle_ratio_long": long_m.get("cpu_throttle_ratio", 0.0),
                "runq_p90_latency": short_m.get("runq_p90_latency", 0.0),
                "runq_max_latency": short_m.get("runq_max_latency", 0.0),
                "runq_p90_latency_long": long_m.get("runq_p90_latency", 0.0),
                "runq_max_latency_long": long_m.get("runq_max_latency", 0.0),
                "rps_long": long_m["rps"],
                "count_long": long_m["count"],
                "truth_rps_long": 0.0,
                "truth_req_count_long": 0,
                "truth_timeout_rate_long": 0.0,
                "truth_connect_refused_rate_long": 0.0,
                "truth_5xx_rate_long": 0.0,
                "truth_p90_latency_ms_long": 0.0,
                "truth_last_update_ts_ms": 0,
                "truth_last_update_ts_ms_long": 0,
                "truth_age_seconds": None,
                "truth_age_seconds_long": None,
                "truth_fresh": False,
                "truth_fresh_long": False,
                "latency_valid_long": bool(long_m.get("latency_valid", False)),
                "latency_valid_reason_long": long_m.get("latency_valid_reason", ""),
                "exclusive_delay_long": 0.0,
                "window_short_seconds": WINDOW_SHORT_SECONDS,
                "window_long_seconds": WINDOW_LONG_SECONDS,
                "runq_learning_enabled": bool(learning_enabled),
            }
            response_metrics[svc] = metric
            edge_last_map = redis_cli.hgetall(f"topo:edge:last_ts:{svc}")
            edge_hits_map = redis_cli.hgetall(f"topo:edge:hits:{svc}")
            edge_type_map = redis_cli.hgetall(f"topo:edge:type:{svc}")
            edges = []
            for dst, sec_raw in edge_last_map.items():
                try:
                    last_sec = int(sec_raw)
                except Exception:
                    continue
                age_sec = max(0, int(now_sec - last_sec))
                if age_sec > TOPO_EDGE_TTL_SECONDS:
                    continue
                try:
                    hits = max(0, int(edge_hits_map.get(dst, "0")))
                except Exception:
                    hits = 0
                etype = str(edge_type_map.get(dst, "internal")).strip().lower()
                if etype not in {"internal", "external"}:
                    etype = "internal"
                recency_boost = max(0.0, 1.0 - (age_sec / max(1.0, float(TOPO_EDGE_TTL_SECONDS))))
                weight = (hits + 1.0) * (0.5 + recency_boost)
                edges.append((dst, etype, last_sec, age_sec, hits, weight))

            if edges:
                edges.sort(key=lambda e: (e[5], e[4], -e[3]), reverse=True)
                meta_rows = {}
                internal_neighbors = []
                for dst, etype, last_sec, age_sec, hits, weight in edges[: max(1, TOPO_MAX_CHILDREN)]:
                    meta_rows[dst] = {
                        "type": etype,
                        "last_seen_sec": int(last_sec),
                        "age_sec": int(age_sec),
                        "hit_count": int(hits),
                        "weight": round(float(weight), 3),
                    }
                    if etype == "internal" and dst in all_services:
                        internal_neighbors.append(dst)
                if internal_neighbors:
                    response_topology[svc] = sorted(set(internal_neighbors))
                if meta_rows:
                    response_topology_meta[svc] = meta_rows

        ex_map = compute_exclusive_delays(response_metrics, response_topology, p90_field="p90_latency")
        ex_map_long = compute_exclusive_delays(response_metrics, response_topology, p90_field="p90_latency_long")
        for svc, ex in ex_map.items():
            if svc in response_metrics:
                response_metrics[svc]["exclusive_delay"] = round(float(ex), 3)
        for svc, ex in ex_map_long.items():
            if svc in response_metrics:
                response_metrics[svc]["exclusive_delay_long"] = round(float(ex), 3)

        for svc, metric in response_metrics.items():
            topo_rows = response_topology_meta.get(svc, {})
            freshness = {
                "latency_age_seconds": get_latest_event_age_seconds(redis_cli, svc, now_ms, "net"),
                "runq_age_seconds": get_latest_event_age_seconds(redis_cli, svc, now_ms, "runq"),
                "truth_age_seconds": None,
                "truth_age_seconds_long": None,
                "topology_age_seconds": get_latest_topology_age_seconds(topo_rows),
            }
            freshness["latency_fresh"] = (
                freshness["latency_age_seconds"] is not None
                and freshness["latency_age_seconds"] <= METRIC_STALE_AFTER_SECONDS
            )
            freshness["runq_fresh"] = (
                freshness["runq_age_seconds"] is not None
                and freshness["runq_age_seconds"] <= METRIC_STALE_AFTER_SECONDS
            )
            freshness["truth_fresh"] = False
            freshness["truth_fresh_long"] = False
            freshness["topology_fresh"] = (
                freshness["topology_age_seconds"] is not None
                and freshness["topology_age_seconds"] <= TOPOLOGY_STALE_AFTER_SECONDS
            )
            short_metric = metric
            long_metric = {
                "truth_req_count": metric.get("truth_req_count_long", 0),
                "count": metric.get("count_long", 0),
                "truth_rps": metric.get("truth_rps_long", 0.0),
                "rps": metric.get("rps_long", 0.0),
                "p90_latency": metric.get("p90_latency_long", 0.0),
                "latency_valid": metric.get("latency_valid_long", False),
                "truth_p90_latency_ms": 0.0,
            }
            activity = service_activity_state(short_metric, long_metric, freshness)
            metric.update(freshness)
            metric.update(activity)
            child_metric_map = {
                child: response_metrics.get(child, {})
                for child in response_topology.get(svc, [])
                if child in response_metrics
            }
            metric.update(derive_latency_split(metric, topo_rows, child_metric_map))
            metric["dependency_attributed_latency_long"] = round(
                max(
                    0.0,
                    float(metric.get("p90_latency_long", 0.0) or 0.0)
                    - float(metric.get("exclusive_delay_long", 0.0) or 0.0),
                ),
                3,
            )
            persist_service_evidence(redis_cli, svc, now_sec, metric)

        include_infra = request.args.get("include_infra", "0").lower() in {"1", "true", "yes", "on"}
        if not include_infra:
            # Keep dashboard/controller focused on app services while preserving all
            # explicit SLO targets, regardless of naming conventions.
            keep = set()
            for svc in response_metrics.keys():
                if svc in slo_map or not is_infra_service(svc, INFRA_EXACT, INFRA_PREFIXES):
                    keep.add(svc)

            filtered_topo = {}
            filtered_topo_meta = {}
            for src, dsts in response_topology.items():
                if src not in keep:
                    continue
                kept_dsts = [d for d in dsts if d in keep]
                if kept_dsts:
                    filtered_topo[src] = kept_dsts
            for src, edge_rows in response_topology_meta.items():
                if src not in keep:
                    continue
                kept_rows = {}
                for dst, row in edge_rows.items():
                    if dst in keep or str(row.get("type", "")).lower() == "external":
                        kept_rows[dst] = row
                if kept_rows:
                    filtered_topo_meta[src] = kept_rows

            response_metrics = {k: response_metrics[k] for k in sorted(keep)}
            response_topology = filtered_topo
            response_topology_meta = filtered_topo_meta
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"metrics": response_metrics, "topology": response_topology, "topology_meta": response_topology_meta})


@app.route("/api/traces")
def get_traces():
    redis_cli = get_redis()
    try:
        rows = redis_cli.lrange(TRACE_REDIS_KEY, 0, max(TRACE_RETURN_LIMIT - 1, 0))
        traces = []
        for row in rows:
            try:
                traces.append(json.loads(row))
            except Exception:
                continue
        return jsonify({"traces": traces})
    except Exception as exc:
        return jsonify({"error": str(exc), "traces": []}), 500


@app.route("/api/control/state")
def control_state():
    try:
        refresh_slo_map()
        with K8S_MAP_LOCK:
            slo_cfg = SLO_CONFIG_MAP.copy()

        deployments = apps_api.list_namespaced_deployment(TARGET_NAMESPACE).items
        deployment_rows = []
        for d in deployments:
            name = d.metadata.name
            spec = d.spec.replicas if d.spec and d.spec.replicas is not None else 0
            status = d.status.replicas if d.status and d.status.replicas is not None else 0
            row = {
                "name": name,
                "spec_replicas": int(spec),
                "status_replicas": int(status),
            }
            if name in slo_cfg:
                row["slo"] = slo_cfg[name]
            deployment_rows.append(row)

        benchmark = detect_benchmark_profile(TARGET_NAMESPACE, deployment_rows, slo_cfg)
        routes = [row["name"] for row in benchmark.get("trafficRoutes", [])] or derive_routes_from_slos(slo_cfg)
        return jsonify(
            {
                "namespace": TARGET_NAMESPACE,
                "deployments": sorted(deployment_rows, key=lambda x: x["name"]),
                "routes": routes,
                "trafficBaseUrl": benchmark.get("trafficBaseUrl", TRAFFIC_TARGET_BASE_URL),
                "trafficRoutes": benchmark.get("trafficRoutes", []),
                "benchmark": benchmark,
                "slos": slo_cfg,
                "controlAuthEnabled": control_auth_enabled(),
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/audit")
def get_audit_events():
    try:
        return jsonify({"events": recent_audit_events()})
    except Exception as exc:
        return jsonify({"error": str(exc), "events": []}), 500


@app.route("/api/support/tickets", methods=["GET", "POST"])
def support_tickets():
    redis_cli = get_redis()
    if request.method == "GET":
        try:
            return jsonify({"tickets": list_tickets(redis_cli)})
        except Exception as exc:
            return jsonify({"error": str(exc), "tickets": []}), 500

    auth_error = require_control_auth()
    if auth_error:
        return auth_error

    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    if not title or not description:
        return jsonify({"ok": False, "error": "title and description are required"}), 400

    ticket = {
        "id": next_ticket_id(redis_cli),
        "title": title,
        "description": description,
        "status": "open",
        "created_by": operator_identity(),
        "assigned_to": str(payload.get("assignedTo") or "").strip(),
        "response": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_ticket(redis_cli, ticket)
    append_audit_event("support_ticket", "created", {"ticket_id": ticket["id"], "title": title})
    return jsonify({"ok": True, "ticket": ticket})


@app.route("/api/support/tickets/<ticket_id>", methods=["PATCH"])
def update_support_ticket(ticket_id):
    auth_error = require_control_auth()
    if auth_error:
        return auth_error

    redis_cli = get_redis()
    ticket = load_ticket(redis_cli, ticket_id)
    if not ticket:
        return jsonify({"ok": False, "error": "ticket not found"}), 404

    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status") or ticket.get("status") or "open").strip().lower()
    if status not in {"open", "in_progress", "resolved", "closed"}:
        return jsonify({"ok": False, "error": "invalid status"}), 400

    ticket["status"] = status
    ticket["assigned_to"] = str(payload.get("assignedTo") or ticket.get("assigned_to") or "").strip()
    ticket["response"] = str(payload.get("response") or ticket.get("response") or "").strip()
    ticket["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_ticket(redis_cli, ticket)
    append_audit_event(
        "support_ticket",
        "updated",
        {"ticket_id": ticket_id, "status": ticket["status"], "assigned_to": ticket["assigned_to"]},
    )
    return jsonify({"ok": True, "ticket": ticket})


@app.route("/api/control/traffic/start", methods=["POST"])
def start_traffic():
    auth_error = require_control_auth()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    route_name = str(payload.get("route") or payload.get("service") or "").strip()
    route = route_to_path(route_name)
    route_path = str(payload.get("path") or "").strip()
    if not route_path:
        if route_name == "/":
            route_path = "/"
        elif route:
            route_path = f"/{route}"
    if not route_name and not route_path:
        return jsonify({"error": "route or path is required"}), 400

    req_n = parse_int(payload, "requests", 2000, 1, 5000000)
    conc = parse_int(payload, "concurrency", 20, 1, 2000)
    if conc > req_n:
        conc = req_n

    base = str(payload.get("base_url") or TRAFFIC_TARGET_BASE_URL).rstrip("/")
    target_url = payload.get("url") or join_target_url(base, route_path)
    safe_route = sanitize_name_part(route_name or route_path.strip("/").replace("/", "-") or "root")
    job_name = f"traffic-{safe_route}-{current_ts_name()}".replace("_", "-")
    shell_url = str(target_url).replace("'", "%27")
    # Parallel curl burst with bounded in-flight workers approximates hey -n/-c semantics.
    script = (
        f"n={req_n}; c={conc}; i=0; "
        f"while [ $i -lt $n ]; do "
        f"curl -sS -o /dev/null '{shell_url}' & "
        f"i=$((i+1)); "
        f"if [ $((i % c)) -eq 0 ]; then wait; fi; "
        f"done; wait"
    )

    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=job_name,
            labels={
                "app": "traffic-job",
                "managed-by": "dashboard-control",
                "traffic-route": safe_route,
            },
        ),
        spec=client.V1JobSpec(
            ttl_seconds_after_finished=TRAFFIC_JOB_TTL_SECONDS,
            backoff_limit=0,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"app": "traffic-job", "managed-by": "dashboard-control", "traffic-route": safe_route}
                ),
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    containers=[
                        client.V1Container(
                            name="hey",
                            image=TRAFFIC_JOB_IMAGE,
                            command=["/bin/sh", "-c"],
                            args=[script],
                        )
                    ],
                ),
            ),
        ),
    )
    try:
        batch_api.create_namespaced_job(namespace=TARGET_NAMESPACE, body=job)
        append_audit_event(
            "traffic_job",
            "started",
            {
                "job": job_name,
                "route": route_name or route_path,
                "path": route_path or "/",
                "requests": req_n,
                "concurrency": conc,
                "target_url": target_url,
            },
        )
        return jsonify(
            {
                "ok": True,
                "job": job_name,
                "route": route_name or route_path,
                "path": route_path or "/",
                "requests": req_n,
                "concurrency": conc,
                "target_url": target_url,
            }
        )
    except ApiException as exc:
        return jsonify({"ok": False, "error": exc.reason, "body": exc.body}), int(exc.status or 500)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/control/traffic/stop", methods=["POST"])
def stop_traffic():
    auth_error = require_control_auth()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    stop_all = bool(payload.get("all"))
    route = "" if stop_all else sanitize_name_part(route_to_path(payload.get("route") or ""))
    selector = "managed-by=dashboard-control"
    if route:
        selector = f"{selector},traffic-route={route}"
    deleted = []
    errors = []
    try:
        jobs = batch_api.list_namespaced_job(namespace=TARGET_NAMESPACE, label_selector=selector).items
        for job in jobs:
            name = job.metadata.name
            try:
                batch_api.delete_namespaced_job(
                    name=name,
                    namespace=TARGET_NAMESPACE,
                    propagation_policy="Background",
                    grace_period_seconds=0,
                )
                deleted.append(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        append_audit_event("traffic_job", "stopped", {"route": route or "*", "deleted": deleted, "errors": errors})
        return jsonify({"ok": True, "deleted": deleted, "errors": errors})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/control/slo/update", methods=["POST"])
def update_slo():
    auth_error = require_control_auth()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    target = (payload.get("targetDeployment") or payload.get("service") or "").strip()
    if not target:
        return jsonify({"error": "targetDeployment is required"}), 400

    try:
        slo_latency = float(payload.get("sloLatency"))
    except Exception:
        return jsonify({"error": "sloLatency must be numeric"}), 400
    min_replicas = parse_int(payload, "minReplicas", 1, 1, 200)
    max_replicas = parse_int(payload, "maxReplicas", max(min_replicas, 5), min_replicas, 500)
    refresh_slo_map()
    with K8S_MAP_LOCK:
        existing = SLO_CONFIG_MAP.get(target, {})
    priority = str(payload.get("priority") or existing.get("priority") or "secondary").strip().lower()
    if priority not in {"primary", "secondary"}:
        return jsonify({"error": "priority must be primary or secondary"}), 400
    slo_name = payload.get("name") or existing.get("name")
    if not slo_name:
        return jsonify({"error": f"No ServiceSLO found for targetDeployment={target}"}), 404

    patch = {
        "spec": {
            "targetDeployment": target,
            "sloLatency": slo_latency,
            "minReplicas": min_replicas,
            "maxReplicas": max_replicas,
            "priority": priority,
        }
    }
    try:
        custom_api.patch_namespaced_custom_object(
            group="autoscaling.fyp.io",
            version="v1alpha1",
            namespace=TARGET_NAMESPACE,
            plural="serviceslos",
            name=slo_name,
            body=patch,
        )
        refresh_slo_map()
        append_audit_event(
            "slo_update",
            "updated",
            {
                "target": target,
                "sloLatency": slo_latency,
                "minReplicas": min_replicas,
                "maxReplicas": max_replicas,
                "priority": priority,
            },
        )
        return jsonify(
            {
                "ok": True,
                "name": slo_name,
                "spec": patch["spec"],
            }
        )
    except ApiException as exc:
        return jsonify({"ok": False, "error": exc.reason, "body": exc.body}), int(exc.status or 500)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/control/scale", methods=["POST"])
def manual_scale():
    auth_error = require_control_auth()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    deploy = (payload.get("deployment") or payload.get("service") or "").strip()
    if not deploy:
        return jsonify({"error": "deployment is required"}), 400
    replicas = parse_int(payload, "replicas", 1, 0, 500)

    patch = {"spec": {"replicas": replicas}}
    try:
        apps_api.patch_namespaced_deployment_scale(
            name=deploy,
            namespace=TARGET_NAMESPACE,
            body=patch,
        )
        append_audit_event("manual_scale", "scaled", {"deployment": deploy, "replicas": replicas})
        return jsonify({"ok": True, "deployment": deploy, "replicas": replicas})
    except ApiException as exc:
        return jsonify({"ok": False, "error": exc.reason, "body": exc.body}), int(exc.status or 500)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/control/scale/down", methods=["POST"])
def manual_scale_down():
    auth_error = require_control_auth()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    deploy = (payload.get("deployment") or payload.get("service") or "").strip()
    if not deploy:
        return jsonify({"error": "deployment is required"}), 400
    mode = (payload.get("mode") or "one").strip().lower()

    refresh_slo_map()
    with K8S_MAP_LOCK:
        slo_cfg = SLO_CONFIG_MAP.get(deploy, {})
    min_replicas = int(slo_cfg.get("minReplicas", 1))
    scale_obj = apps_api.read_namespaced_deployment_scale(deploy, TARGET_NAMESPACE)
    current = int(scale_obj.spec.replicas or 0)

    if mode == "min":
        target = min_replicas
    else:
        target = max(min_replicas, current - 1)

    apps_api.patch_namespaced_deployment_scale(
        name=deploy,
        namespace=TARGET_NAMESPACE,
        body={"spec": {"replicas": target}},
    )
    append_audit_event("manual_scale_down", "scaled", {"deployment": deploy, "current": current, "target": target, "mode": mode})
    return jsonify({"ok": True, "deployment": deploy, "current": current, "target": target, "mode": mode})


@app.route("/api/truth/ingest", methods=["POST"])
def ingest_truth():
    payload = request.get_json(silent=True) or {}
    records = payload.get("records")
    if records is None:
        records = payload.get("events")
    if not isinstance(records, list):
        return jsonify({"ok": False, "error": "records/events list is required"}), 400
    accepted = get_truth_store().ingest_records(records)
    return jsonify({"ok": True, "accepted": int(accepted), "received": len(records)})


@app.route("/api/reset")
def reset():
    auth_error = require_control_auth()
    if auth_error:
        return auth_error
    redis_cli = get_redis()
    redis_cli.flushdb()
    get_truth_store().reset()
    # After reset, keep baseline learning disabled until explicit warm-up starts.
    redis_cli.set(RUNQ_LEARNING_ENABLED_KEY, "0")
    append_audit_event("control_reset", "completed", {"runq_learning_enabled": False})
    return jsonify({"ok": True, "runq_learning_enabled": False})


@app.route("/api/control/runq-baseline/start", methods=["POST"])
def runq_baseline_start():
    auth_error = require_control_auth()
    if auth_error:
        return auth_error
    redis_cli = get_redis()
    redis_cli.set(RUNQ_LEARNING_ENABLED_KEY, "1")
    append_audit_event("runq_baseline", "started", {"runq_learning_enabled": True})
    return jsonify({"ok": True, "runq_learning_enabled": True})


@app.route("/api/control/runq-baseline/stop", methods=["POST"])
def runq_baseline_stop():
    auth_error = require_control_auth()
    if auth_error:
        return auth_error
    redis_cli = get_redis()
    redis_cli.set(RUNQ_LEARNING_ENABLED_KEY, "0")
    append_audit_event("runq_baseline", "stopped", {"runq_learning_enabled": False})
    return jsonify({"ok": True, "runq_learning_enabled": False})


@app.route("/api/control/runq-baseline/state")
def runq_baseline_state():
    redis_cli = get_redis()
    return jsonify({"runq_learning_enabled": bool(runq_learning_enabled(redis_cli))})


if __name__ == "__main__":
    aggregator_startup_checks()
    threading.Thread(target=fetch_from_agents_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
