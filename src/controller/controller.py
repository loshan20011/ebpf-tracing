import json
import logging
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import redis
import requests
from kubernetes import client, config

AGGREGATOR_URL = os.getenv("AGGREGATOR_URL", "http://aggregator:8000")
AGGREGATOR_TIMEOUT_S = float(os.getenv("AGGREGATOR_TIMEOUT_S", "5"))
TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "default")
ROOT_SERVICE = os.getenv("ROOT_SERVICE", "front-end")
LOOP_SECONDS = float(os.getenv("LOOP_SECONDS", "5"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8081"))

SHORT_TRAFFIC_WINDOW_SECONDS = float(os.getenv("SHORT_TRAFFIC_WINDOW_SECONDS", "5"))
LONG_TRAFFIC_WINDOW_SECONDS = float(os.getenv("LONG_TRAFFIC_WINDOW_SECONDS", "15"))
DOWNSCALE_DECISION_WINDOW_SECONDS = float(os.getenv("DOWNSCALE_DECISION_WINDOW_SECONDS", "20"))

TRACE_LOGS = os.getenv("TRACE_LOGS", "true").lower() == "true"
TRACE_REDIS_KEY = os.getenv("TRACE_REDIS_KEY", "decision_traces")
TRACE_REDIS_MAX = int(os.getenv("TRACE_REDIS_MAX", "500"))

ACTIVE_RPS_THRESHOLD = float(os.getenv("ACTIVE_RPS_THRESHOLD", "0.5"))
DOWNSCALE_RPS_THRESHOLD = float(os.getenv("DOWNSCALE_RPS_THRESHOLD", "0.5"))
DOWNSCALE_RPS_PER_REPLICA_THRESHOLD = float(os.getenv("DOWNSCALE_RPS_PER_REPLICA_THRESHOLD", "1.0"))
TARGET_RPS_PER_REPLICA = float(os.getenv("TARGET_RPS_PER_REPLICA", "20.0"))
UPSCALE_COOLDOWN_S = float(os.getenv("UPSCALE_COOLDOWN_S", str(max(1.0, LOOP_SECONDS))))
PRESSURE_WINDOW_COUNT = 2
DOWNSCALE_WINDOW_COUNT = max(2, int(math.ceil(DOWNSCALE_DECISION_WINDOW_SECONDS / max(LOOP_SECONDS, 1.0))))
RUNQ_ONLY_MIN_RPS = float(os.getenv("RUNQ_ONLY_MIN_RPS", "3.0"))
RUNQ_ONLY_MIN_RPS_PER_REPLICA_FACTOR = float(os.getenv("RUNQ_ONLY_MIN_RPS_PER_REPLICA_FACTOR", "0.5"))
LOW_DEMAND_STREAK_REQUIRED = int(
    os.getenv(
        "LOW_DEMAND_STREAK_REQUIRED",
        str(max(1, int(math.ceil(DOWNSCALE_DECISION_WINDOW_SECONDS / max(LOOP_SECONDS, 1.0))))),
    )
)
SECONDARY_UPSCALE_MIN_RPS = float(os.getenv("SECONDARY_UPSCALE_MIN_RPS", "3.0"))
SECONDARY_UPSCALE_MIN_RPS_PER_REPLICA = float(os.getenv("SECONDARY_UPSCALE_MIN_RPS_PER_REPLICA", "1.0"))
RUNQ_P90_PRESSURE_STREAK_REQUIRED = int(os.getenv("RUNQ_P90_PRESSURE_STREAK_REQUIRED", "2"))
RUNQ_AVG_SUPPORT_FACTOR = float(os.getenv("RUNQ_AVG_SUPPORT_FACTOR", "0.5"))
RUNQ_P90_STRONG_FACTOR = float(os.getenv("RUNQ_P90_STRONG_FACTOR", "1.25"))
RUNQ_BORDERLINE_MS = float(os.getenv("RUNQ_BORDERLINE_MS", "2.5"))
LEAF_CPU_CONFIRM_RUNQ_MS = float(os.getenv("LEAF_CPU_CONFIRM_RUNQ_MS", "2.7"))
CPU_THROTTLE_RATIO_THRESHOLD = float(os.getenv("CPU_THROTTLE_RATIO_THRESHOLD", "0.10"))
NEAR_BREACH_RATIO_PRIMARY = float(os.getenv("NEAR_BREACH_RATIO_PRIMARY", "0.90"))
NEAR_BREACH_RATIO_SECONDARY = float(os.getenv("NEAR_BREACH_RATIO_SECONDARY", "1.00"))
PRIMARY_MODERATE_SUSTAIN_LOOPS = int(os.getenv("PRIMARY_MODERATE_SUSTAIN_LOOPS", "6"))
PRIMARY_SEVERE_SUSTAIN_LOOPS = int(os.getenv("PRIMARY_SEVERE_SUSTAIN_LOOPS", "9"))
CHILD_SIMILARITY_FLOOR = float(os.getenv("CHILD_SIMILARITY_FLOOR", "0.70"))
CHILD_SIMILARITY_CEILING = float(os.getenv("CHILD_SIMILARITY_CEILING", "1.30"))
LOCAL_FRACTION_MIN = float(os.getenv("LOCAL_FRACTION_MIN", "0.40"))
DEPENDENCY_FRACTION_MAX = float(os.getenv("DEPENDENCY_FRACTION_MAX", "0.50"))
PRIMARY_PROTECTIVE_STREAK_REQUIRED = int(os.getenv("PRIMARY_PROTECTIVE_STREAK_REQUIRED", "1"))
PRIMARY_PROTECTIVE_MIN_RPS_DELTA = float(os.getenv("PRIMARY_PROTECTIVE_MIN_RPS_DELTA", "0.5"))
DOWNSCALE_COOLDOWN_S = int(os.getenv("DOWNSCALE_COOLDOWN_S", "20"))
RUNQ_FIXED_THRESHOLD_MS = float(os.getenv("RUNQ_FIXED_THRESHOLD_MS", "3.0"))
DOWNSCALE_RUNQ_FACTOR = float(os.getenv("DOWNSCALE_RUNQ_FACTOR", "0.5"))
DOWNSCALE_RUNQ_MARGIN_MS = float(os.getenv("DOWNSCALE_RUNQ_MARGIN_MS", "1.0"))
OVERLOAD_ERROR_RATE_THRESHOLD = float(os.getenv("OVERLOAD_ERROR_RATE_THRESHOLD", "0.1"))
OVERLOAD_TIMEOUT_RATE_THRESHOLD = float(os.getenv("OVERLOAD_TIMEOUT_RATE_THRESHOLD", "0.02"))
BREACH_STREAK_REQUIRED = int(os.getenv("BREACH_STREAK_REQUIRED", "2"))
PRIMARY_BREACH_STREAK_REQUIRED = int(os.getenv("PRIMARY_BREACH_STREAK_REQUIRED", "1"))
RECENT_BREACH_HOLD_S = float(os.getenv("RECENT_BREACH_HOLD_S", str(max(4.0, LOOP_SECONDS * 1.5))))
MAX_ROOT_CAUSE_DEPTH = max(1, int(os.getenv("MAX_ROOT_CAUSE_DEPTH", "5")))
WARMUP_READY_GAP_RATIO = float(os.getenv("WARMUP_READY_GAP_RATIO", "0.1"))
PRIMARY_PROTECTIVE_FRONTEND_SCALE = os.getenv("PRIMARY_PROTECTIVE_FRONTEND_SCALE", "true").lower() == "true"
DEPENDENCY_DOMINANCE_RATIO = float(os.getenv("DEPENDENCY_DOMINANCE_RATIO", "1.25"))
EXTERNAL_DOMINANCE_RATIO = float(os.getenv("EXTERNAL_DOMINANCE_RATIO", "1.25"))
PRIMARY_CONFIDENT_FIRST_UPSCALE_STEP = int(os.getenv("PRIMARY_CONFIDENT_FIRST_UPSCALE_STEP", "2"))
PRIMARY_CONFIDENT_UPSCALE_RATIO = float(os.getenv("PRIMARY_CONFIDENT_UPSCALE_RATIO", "1.15"))
PRIMARY_REACTIVE_MIN_UPSCALE_STEP = int(os.getenv("PRIMARY_REACTIVE_MIN_UPSCALE_STEP", "2"))
PRIMARY_REACTIVE_SEVERE_UPSCALE_STEP = int(os.getenv("PRIMARY_REACTIVE_SEVERE_UPSCALE_STEP", "3"))
PRIMARY_SEVERE_BREACH_RATIO = float(os.getenv("PRIMARY_SEVERE_BREACH_RATIO", "1.40"))
PRIMARY_TARGET_STICKY_SECONDS = float(os.getenv("PRIMARY_TARGET_STICKY_SECONDS", "5"))
PRIMARY_EARLY_WARNING_RATIO = float(os.getenv("PRIMARY_EARLY_WARNING_RATIO", "0.85"))
BOUNDED_GROWTH_FACTOR = float(os.getenv("BOUNDED_GROWTH_FACTOR", "0.25"))
POST_SCALE_FEEDBACK_WAIT_SECONDS = float(os.getenv("POST_SCALE_FEEDBACK_WAIT_SECONDS", str(max(20.0, LOOP_SECONDS * 2.0))))
ACTIVE_TARGET_STICKY_SECONDS = float(os.getenv("ACTIVE_TARGET_STICKY_SECONDS", str(max(6.0, LOOP_SECONDS * 1.5))))
RECENT_TARGET_HOLD_S = float(os.getenv("RECENT_TARGET_HOLD_S", str(max(15.0, LOOP_SECONDS * 3.0))))

READY_STATE = {"ready": False, "last_error": "", "last_loop_ts": 0.0}
DOWNSCALE_COOLDOWNS: Dict[str, float] = {}
UPSCALE_COOLDOWNS: Dict[str, float] = {}
BREACH_STREAKS: Dict[str, int] = {}
LAST_BREACH_AT: Dict[str, float] = {}
LOW_DEMAND_STREAKS: Dict[str, int] = {}
PRESSURE_WINDOWS: Dict[str, List[bool]] = {}
LOW_DEMAND_WINDOWS: Dict[str, List[bool]] = {}
RUNQ_PRESSURE_STREAKS: Dict[str, int] = {}
PRIMARY_PROTECTIVE_STREAKS: Dict[str, int] = {}
LAST_SHORT_RPS: Dict[str, float] = {}
PRIMARY_LAST_LOCAL_TARGET: Dict[str, str] = {}
PRIMARY_LAST_LOCAL_TARGET_AT: Dict[str, float] = {}
LAST_SCALE_EVENTS: Dict[str, dict] = {}
ACTIVE_SCALE_TARGETS: Dict[str, str] = {}
ACTIVE_SCALE_TARGET_AT: Dict[str, float] = {}
RECENT_TRUE_TARGET_AT: Dict[str, float] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SimpleController")

try:
    config.load_incluster_config()
except Exception:
    config.load_kube_config()

app_api = client.AppsV1Api()
custom_api = client.CustomObjectsApi()


def parse_service_priority_map(raw: str) -> Dict[str, int]:
    priorities: Dict[str, int] = {}
    for chunk in str(raw or "").split(","):
        item = chunk.strip()
        if not item or ":" not in item:
            continue
        service, score = item.split(":", 1)
        service = service.strip()
        if not service:
            continue
        try:
            priorities[service] = int(score.strip())
        except Exception:
            priorities[service] = 0
    return priorities


BENCHMARK_PATH_PRIORITY = parse_service_priority_map(
    os.getenv("BENCHMARK_PATH_PRIORITY", "front-end:100,user:90,catalogue:40,carts:10")
)


def resolve_redis_endpoint() -> Tuple[str, int]:
    explicit_host = os.getenv("REDIS_HOST")
    explicit_port = os.getenv("REDIS_PORT")
    if explicit_host and explicit_port and explicit_port.isdigit():
        return explicit_host, int(explicit_port)

    redis_port_var = os.getenv("REDIS_PORT", "")
    if redis_port_var.startswith("tcp://"):
        parsed = urlparse(redis_port_var)
        if parsed.hostname and parsed.port:
            return parsed.hostname, int(parsed.port)

    svc_host = os.getenv("REDIS_SERVICE_HOST")
    svc_port = os.getenv("REDIS_SERVICE_PORT")
    if svc_host and svc_port and svc_port.isdigit():
        return svc_host, int(svc_port)

    return explicit_host or "redis", int(explicit_port) if explicit_port and explicit_port.isdigit() else 6379


REDIS_HOST, REDIS_PORT = resolve_redis_endpoint()
TRACE_REDIS = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)


def safe_int(value, default):
    try:
        return int(value)
    except Exception:
        return int(default)


def safe_float(value, default):
    try:
        return float(value)
    except Exception:
        return float(default)


def metric_obj(metrics, svc: str) -> dict:
    item = metrics.get(svc, {})
    return item if isinstance(item, dict) else {}


def preferred_truth_or_ebpf_p90(metrics, svc: str) -> Tuple[float, str, bool]:
    m = metric_obj(metrics, svc)
    if bool(m.get("latency_fresh", False)) and bool(m.get("evaluable_for_slo", False)):
        return safe_float(m.get("p90_latency", m.get("latency", 0.0)), 0.0), "aggregated", True
    return 0.0, "none", False


def preferred_rps(metrics, svc: str) -> float:
    return safe_float(metric_obj(metrics, svc).get("rps", 0.0), 0.0)


def preferred_long_rps(metrics, svc: str) -> float:
    m = metric_obj(metrics, svc)
    return safe_float(m.get("truth_rps_long", m.get("rps_long", m.get("rps", 0.0))), 0.0)


def preferred_long_p90(metrics, svc: str) -> Tuple[float, str, bool]:
    m = metric_obj(metrics, svc)
    if bool(m.get("latency_fresh", False)) and bool(m.get("latency_valid_long", False)):
        return safe_float(m.get("p90_latency_long", m.get("latency_long", 0.0)), 0.0), "aggregated_long", True
    return 0.0, "none", False


def target_rps_per_replica(service: str) -> float:
    normalized = service.upper().replace("-", "_")
    specific = os.getenv(f"TARGET_RPS_PER_REPLICA_{normalized}")
    if specific:
        return max(0.1, safe_float(specific, TARGET_RPS_PER_REPLICA))
    return max(0.1, TARGET_RPS_PER_REPLICA)


def service_priority(service: str) -> int:
    return safe_int(BENCHMARK_PATH_PRIORITY.get(service, 0), 0)


def meaningful_demand(service: str, rps: float, current_replicas: int) -> bool:
    per_replica = rps / max(current_replicas, 1)
    demand_floor = target_rps_per_replica(service) * RUNQ_ONLY_MIN_RPS_PER_REPLICA_FACTOR
    return bool(rps >= RUNQ_ONLY_MIN_RPS or per_replica >= demand_floor)


def recent_true_target_hold_active(service: str) -> bool:
    ts = float(RECENT_TRUE_TARGET_AT.get(service, 0.0) or 0.0)
    return ts > 0.0 and (time.time() - ts) < RECENT_TARGET_HOLD_S


def runq_latency_ms(metrics, svc: str) -> float:
    m = metric_obj(metrics, svc)
    p90_val = safe_float(m.get("runq_p90_latency", 0.0), 0.0)
    if p90_val > 0:
        return p90_val
    return safe_float(m.get("avg_runq_latency", 0.0), 0.0)


def runq_p90_latency_ms(metrics, svc: str) -> float:
    return safe_float(metric_obj(metrics, svc).get("runq_p90_latency", 0.0), 0.0)


def avg_runq_latency_ms(metrics, svc: str) -> float:
    return safe_float(metric_obj(metrics, svc).get("avg_runq_latency", 0.0), 0.0)


def cpu_throttle_ratio(metrics, svc: str) -> float:
    return safe_float(metric_obj(metrics, svc).get("cpu_throttle_ratio", 0.0), 0.0)


def runq_threshold_ms(_metrics, _svc: str) -> float:
    return RUNQ_FIXED_THRESHOLD_MS


def runq_low_threshold_ms(metrics, svc: str) -> float:
    fixed = RUNQ_FIXED_THRESHOLD_MS * DOWNSCALE_RUNQ_FACTOR
    _unused = (metrics, svc)
    return max(fixed, DOWNSCALE_RUNQ_MARGIN_MS)


def timeout_rate(metrics, svc: str) -> float:
    m = metric_obj(metrics, svc)
    return safe_float(m.get("truth_timeout_rate", m.get("timeout_rate", m.get("truth_timeout_rate_long", 0.0))), 0.0)


def error_rate_5xx(metrics, svc: str) -> float:
    m = metric_obj(metrics, svc)
    return safe_float(m.get("truth_5xx_rate", m.get("error_5xx_rate", m.get("truth_5xx_rate_long", 0.0))), 0.0)


def local_handling_latency_ms(metrics, svc: str) -> float:
    return safe_float(metric_obj(metrics, svc).get("service_handling_latency", metric_obj(metrics, svc).get("exclusive_delay", 0.0)), 0.0)


def dependency_latency_ms(metrics, svc: str) -> float:
    return safe_float(metric_obj(metrics, svc).get("dependency_attributed_latency", 0.0), 0.0)


def external_wait_ms(metrics, svc: str) -> float:
    return safe_float(metric_obj(metrics, svc).get("external_wait_latency", 0.0), 0.0)


def total_latency_ms(metrics, svc: str) -> float:
    p90_ms, _src, sufficient = preferred_truth_or_ebpf_p90(metrics, svc)
    if sufficient and p90_ms > 0:
        return p90_ms
    local_ms = local_handling_latency_ms(metrics, svc)
    dep_ms = dependency_latency_ms(metrics, svc)
    ext_ms = external_wait_ms(metrics, svc)
    return max(local_ms + dep_ms + ext_ms, 0.0)


def local_fraction(metrics, svc: str) -> float:
    total = max(total_latency_ms(metrics, svc), 0.001)
    return local_handling_latency_ms(metrics, svc) / total


def dependency_fraction(metrics, svc: str) -> float:
    total = max(total_latency_ms(metrics, svc), 0.001)
    return dependency_latency_ms(metrics, svc) / total


def runq_pct(metrics, svc: str) -> float:
    return runq_p90_latency_ms(metrics, svc) / max(RUNQ_FIXED_THRESHOLD_MS, 0.001)


def throttle_pct(metrics, svc: str) -> float:
    return cpu_throttle_ratio(metrics, svc) / max(CPU_THROTTLE_RATIO_THRESHOLD, 0.001)


def local_resource_support(metrics, svc: str) -> str:
    rq = runq_pct(metrics, svc)
    th = throttle_pct(metrics, svc)
    if rq >= 1.0 or th >= 1.0:
        return "strong"
    if rq >= 0.7 or th >= 0.7:
        return "medium"
    return "weak"


def resource_support_multiplier(metrics, svc: str) -> float:
    support = local_resource_support(metrics, svc)
    if support == "strong":
        return 1.10
    if support == "medium":
        return 1.00
    return 0.95


def get_slo_configs() -> Dict[str, Dict[str, object]]:
    configs: Dict[str, Dict[str, object]] = {}
    raw = custom_api.list_namespaced_custom_object(
        group="autoscaling.fyp.io",
        version="v1alpha1",
        namespace=TARGET_NAMESPACE,
        plural="serviceslos",
    )
    for item in raw.get("items", []):
        spec = item.get("spec", {})
        deploy = str(spec.get("targetDeployment", "")).strip()
        if not deploy:
            continue
        priority = str(spec.get("priority", "secondary")).strip().lower()
        if priority not in {"primary", "secondary"}:
            priority = "secondary"
        configs[deploy] = {
            "slo": safe_float(spec.get("sloLatency", 50), 50),
            "min": max(1, safe_int(spec.get("minReplicas", 1), 1)),
            "max": max(1, safe_int(spec.get("maxReplicas", 10), 10)),
            "priority": priority,
        }
    return configs


def fetch_graph_payload() -> dict:
    resp = requests.get(f"{AGGREGATOR_URL.rstrip('/')}/api/graph", timeout=AGGREGATOR_TIMEOUT_S)
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def read_replicas(service: str) -> Tuple[int, int]:
    dep = app_api.read_namespaced_deployment(name=service, namespace=TARGET_NAMESPACE)
    desired = safe_int(dep.spec.replicas if dep.spec else 0, 0)
    ready = safe_int(dep.status.ready_replicas if dep.status else 0, 0)
    return desired, ready


def patch_replicas(service: str, replicas: int) -> None:
    app_api.patch_namespaced_deployment_scale(
        name=service,
        namespace=TARGET_NAMESPACE,
        body={"spec": {"replicas": int(replicas)}},
    )


def cooldown_active(store: Dict[str, float], service: str) -> bool:
    return time.time() < float(store.get(service, 0.0) or 0.0)


def set_cooldown(store: Dict[str, float], service: str, seconds: int) -> None:
    store[service] = time.time() + max(0, int(seconds))


def is_active_for_control(metrics, svc: str) -> bool:
    m = metric_obj(metrics, svc)
    rps = preferred_rps(metrics, svc)
    return bool(m.get("active_short", False)) and rps >= ACTIVE_RPS_THRESHOLD


def dependency_dominant_for_service(metrics, svc: str) -> bool:
    local_ms = local_handling_latency_ms(metrics, svc)
    dep_ms = dependency_latency_ms(metrics, svc)
    return dep_ms > 0 and dep_ms >= max(local_ms * DEPENDENCY_DOMINANCE_RATIO, local_ms + 1.0)


def external_dominant_for_service(metrics, svc: str) -> bool:
    local_ms = local_handling_latency_ms(metrics, svc)
    dep_ms = dependency_latency_ms(metrics, svc)
    ext_ms = external_wait_ms(metrics, svc)
    return ext_ms > 0 and ext_ms >= max(local_ms * EXTERNAL_DOMINANCE_RATIO, dep_ms * EXTERNAL_DOMINANCE_RATIO)


def runq_pressure_candidate(metrics, svc: str) -> bool:
    local_ms = local_handling_latency_ms(metrics, svc)
    dep_ms = dependency_latency_ms(metrics, svc)
    ext_ms = external_wait_ms(metrics, svc)
    runq_p90_ms = runq_p90_latency_ms(metrics, svc)
    if runq_p90_ms <= 0 or runq_p90_ms < runq_threshold_ms(metrics, svc):
        return False
    if not is_active_for_control(metrics, svc):
        return False
    if local_ms <= 0 or local_ms < max(dep_ms * 1.1, ext_ms * 1.1, 1.0):
        return False
    if dependency_dominant_for_service(metrics, svc):
        return False
    if external_dominant_for_service(metrics, svc):
        return False
    return True


def local_handling_dominant(metrics, svc: str) -> bool:
    local_ms = local_handling_latency_ms(metrics, svc)
    dep_ms = dependency_latency_ms(metrics, svc)
    ext_ms = external_wait_ms(metrics, svc)
    return local_ms >= max(dep_ms * 1.1, ext_ms * 1.1, 1.0)


def meaningful_runq_cpu_evidence(metrics, svc: str) -> bool:
    return runq_p90_latency_ms(metrics, svc) >= LEAF_CPU_CONFIRM_RUNQ_MS


def borderline_runq_cpu_evidence(metrics, svc: str) -> bool:
    runq_ms = runq_p90_latency_ms(metrics, svc)
    return RUNQ_BORDERLINE_MS <= runq_ms < LEAF_CPU_CONFIRM_RUNQ_MS


def cpu_throttle_elevated(metrics, svc: str) -> bool:
    return cpu_throttle_ratio(metrics, svc) >= CPU_THROTTLE_RATIO_THRESHOLD


def local_cpu_scaleable(metrics, svc: str) -> bool:
    return (
        local_fraction(metrics, svc) >= LOCAL_FRACTION_MIN
        and dependency_fraction(metrics, svc) < DEPENDENCY_FRACTION_MAX
        and local_handling_dominant(metrics, svc)
        and meaningful_runq_cpu_evidence(metrics, svc)
    )


def dependency_or_unexplained_delay_high(metrics, svc: str) -> bool:
    dep_ms = dependency_latency_ms(metrics, svc)
    ext_ms = external_wait_ms(metrics, svc)
    local_ms = local_handling_latency_ms(metrics, svc)
    return max(dep_ms, ext_ms) >= max(local_ms * 1.1, 1.0)


def child_match_score(metrics, current: str, child: str) -> float:
    current_dep_ms = dependency_latency_ms(metrics, current)
    expected_child_ms = max(current_dep_ms, 0.0)
    child_local_ms = local_handling_latency_ms(metrics, child)
    child_p90_ms, _src, sufficient = preferred_truth_or_ebpf_p90(metrics, child)
    child_runq_ms = runq_p90_latency_ms(metrics, child)
    score = 0.0
    if expected_child_ms > 0:
        low = max(expected_child_ms * CHILD_SIMILARITY_FLOOR, 1.0)
        high = max(expected_child_ms * CHILD_SIMILARITY_CEILING, low)
        if low <= child_p90_ms <= high:
            score += 3.0
        if low <= child_local_ms <= high:
            score += 3.0
        if child_p90_ms >= max(expected_child_ms * 0.7, 1.0):
            score += 1.5
        if child_local_ms >= max(expected_child_ms * 0.7, 1.0):
            score += 1.5
    if expected_child_ms > 0 and child_local_ms >= max(expected_child_ms * 0.7, 1.0):
        score += 3.0
    if expected_child_ms > 0 and child_runq_ms >= runq_threshold_ms(metrics, child):
        score += 0.75
    if current_dep_ms >= max(total_latency_ms(metrics, current) * DEPENDENCY_FRACTION_MAX, 1.0):
        score += 0.5
    if sufficient and child_p90_ms > 0:
        score += 0.5
    return score


def best_matching_monitored_child(current: str, metrics: dict, topology: dict, monitored_services: set) -> Optional[str]:
    children = [child for child in topology.get(current, []) if child in monitored_services]
    if not children:
        return None
    ranked = sorted(
        ((child_match_score(metrics, current, child), child) for child in children),
        key=lambda row: (-row[0], row[1]),
    )
    best_score, best_child = ranked[0]
    return best_child if best_score >= 3.0 else None


def record_runq_pressure_streak(service: str, pressure_candidate: bool) -> int:
    if pressure_candidate:
        RUNQ_PRESSURE_STREAKS[service] = RUNQ_PRESSURE_STREAKS.get(service, 0) + 1
    else:
        RUNQ_PRESSURE_STREAKS[service] = 0
    return RUNQ_PRESSURE_STREAKS.get(service, 0)


def record_primary_protective_streak(service: str, candidate: bool) -> int:
    if candidate:
        PRIMARY_PROTECTIVE_STREAKS[service] = PRIMARY_PROTECTIVE_STREAKS.get(service, 0) + 1
    else:
        PRIMARY_PROTECTIVE_STREAKS[service] = 0
    return PRIMARY_PROTECTIVE_STREAKS.get(service, 0)


def local_pressure_present(metrics, svc: str) -> bool:
    runq_p90_ms = runq_p90_latency_ms(metrics, svc)
    avg_runq_ms = avg_runq_latency_ms(metrics, svc)
    threshold_ms = runq_threshold_ms(metrics, svc)
    streak = RUNQ_PRESSURE_STREAKS.get(svc, 0)
    avg_support = avg_runq_ms >= max(threshold_ms * RUNQ_AVG_SUPPORT_FACTOR, 1.0)
    strong_runq = runq_p90_ms >= (threshold_ms * RUNQ_P90_STRONG_FACTOR)
    if runq_pressure_candidate(metrics, svc) and (
        streak >= RUNQ_P90_PRESSURE_STREAK_REQUIRED
        or (streak >= 1 and avg_support and strong_runq)
    ):
        return True
    if timeout_rate(metrics, svc) >= OVERLOAD_TIMEOUT_RATE_THRESHOLD:
        return True
    if error_rate_5xx(metrics, svc) >= OVERLOAD_ERROR_RATE_THRESHOLD:
        return True
    return False


def warmup_active(service: str) -> Tuple[bool, int, int]:
    desired, ready = read_replicas(service)
    active = desired > max(ready, 0) and (desired - ready) >= max(1, math.ceil(desired * WARMUP_READY_GAP_RATIO))
    return active, desired, ready


def metric_fresh_enough_for_control(metrics, svc: str) -> bool:
    m = metric_obj(metrics, svc)
    demand_fresh = bool(m.get("latency_fresh", False)) and (
        safe_int(m.get("count", 0), 0) > 0
        or safe_float(m.get("rps", 0.0), 0.0) > 0.0
        or safe_int(m.get("ebpf_req_count", 0), 0) > 0
    )
    return demand_fresh and bool(m.get("evaluable_for_slo", False))


def demand_fresh_enough_for_downscale(metrics, svc: str) -> bool:
    m = metric_obj(metrics, svc)
    if not bool(m.get("latency_fresh", False)):
        return False
    return (
        safe_int(m.get("count", 0), 0) > 0
        or safe_float(m.get("rps", 0.0), 0.0) > 0.0
        or safe_int(m.get("ebpf_req_count", 0), 0) > 0
    )


def breach_ratio(metrics, svc: str, slo_cfgs: dict) -> float:
    cfg = slo_cfgs.get(svc, {})
    slo_ms = safe_float(cfg.get("slo", 0.0), 0.0)
    p90_ms, _src, sufficient = preferred_truth_or_ebpf_p90(metrics, svc)
    if not sufficient or slo_ms <= 0:
        return 0.0
    return p90_ms / max(slo_ms, 1.0)


def classify_traffic_pattern(
    trigger: dict,
    metrics: dict,
    target: str,
    final_reason: str,
    current_replicas: int,
) -> dict:
    short_rps = preferred_rps(metrics, target)
    long_rps = preferred_long_rps(metrics, target)
    short_p90, _short_src, short_sufficient = preferred_truth_or_ebpf_p90(metrics, target)
    long_p90, _long_src, long_sufficient = preferred_long_p90(metrics, target)
    short_p90 = short_p90 if short_sufficient else float(trigger.get("p90_ms", 0.0) or 0.0)
    long_p90 = long_p90 if long_sufficient else short_p90
    delta = short_rps - long_rps
    rise_ratio = short_rps / max(long_rps, 0.001) if short_rps > 0 else 0.0
    pressure = bool(trigger.get("under_pressure", False))
    easing = short_p90 <= max(long_p90 * 0.95, safe_float(trigger.get("slo_ms", 0.0), 0.0) * 1.05)
    high_floor = max(float(current_replicas) * target_rps_per_replica(target) * 0.6, 1.0)

    if not pressure and short_rps <= high_floor * 0.5 and long_rps <= high_floor * 0.5:
        pattern = "low_demand"
    elif short_rps >= max(long_rps * 1.2, long_rps + 1.0, 1.0):
        pattern = "spike"
    elif pressure and short_rps >= high_floor and long_rps >= high_floor * 0.8:
        pattern = "sustained_high"
    elif short_rps < long_rps and easing:
        pattern = "recovery"
    else:
        pattern = "sustained_high" if pressure else "low_demand"

    _unused = final_reason
    return {
        "pattern": pattern,
        "short_rps": round(short_rps, 3),
        "long_rps": round(long_rps, 3),
        "rps_growth": round(delta, 3),
        "short_p90_ms": round(short_p90, 3),
        "long_p90_ms": round(long_p90, 3),
        "severe": bool(trigger.get("breached", False)),
        "moderate": bool(pressure),
    }


def compute_dynamic_upscale_target(
    metrics: dict,
    target: str,
    trigger: dict,
    cfg: dict,
    traffic: dict,
    current_replicas: int,
) -> dict:
    min_replicas = max(1, safe_int(cfg.get("min", 1), 1))
    max_replicas = max(1, safe_int(cfg.get("max", 1), 1))
    target_per_replica = max(
        0.1,
        safe_float(
            traffic.get("target_rps_per_replica_override", target_rps_per_replica(target)),
            target_rps_per_replica(target),
        ),
    )
    long_rps = safe_float(traffic.get("long_rps", 0.0), 0.0)
    short_p90 = safe_float(traffic.get("short_p90_ms", trigger.get("p90_ms", 0.0)), 0.0)
    slo_ms = max(1.0, safe_float(trigger.get("slo_ms", 0.0), 0.0))
    breach_ratio_value = short_p90 / slo_ms if slo_ms > 0 else 0.0
    target_from_traffic = max(min_replicas, int(math.ceil(long_rps / max(target_per_replica, 0.1)))) if long_rps > 0 else min_replicas
    target_from_slo = max(min_replicas, int(math.ceil(current_replicas * max(1.0, breach_ratio_value))))
    raw_target = max(target_from_traffic, target_from_slo)
    growth_factor = max(0.1, safe_float(traffic.get("growth_factor", BOUNDED_GROWTH_FACTOR), BOUNDED_GROWTH_FACTOR))
    max_growth = max(1, int(math.ceil(max(current_replicas, 1) * growth_factor)))
    bounded_target = min(raw_target, current_replicas + max_growth)
    clamped_target = max(min_replicas, min(max_replicas, bounded_target))
    clamp_applied = clamped_target != bounded_target
    _unused = metrics
    return {
        "target_from_traffic": int(target_from_traffic),
        "target_from_slo": int(target_from_slo),
        "trigger_slo": round(slo_ms, 3),
        "short_p90_ms": round(short_p90, 3),
        "breach_ratio": round(breach_ratio_value, 3),
        "target_rps_per_replica": round(target_per_replica, 3),
        "raw_target": int(raw_target),
        "max_growth": int(max_growth),
        "target_replicas": int(bounded_target),
        "clamped_target_replicas": int(clamped_target),
        "desired_replicas": int(clamped_target),
        "min_replicas": int(min_replicas),
        "max_replicas": int(max_replicas),
        "clamp_applied": bool(clamp_applied),
    }


def strongest_internal_child(
    current: str,
    metrics: dict,
    topology: dict,
    topology_meta: dict,
    monitored_services: set,
    slo_cfgs: dict,
) -> Optional[str]:
    dep_ms = dependency_latency_ms(metrics, current)
    children = [child for child in topology.get(current, []) if child in monitored_services]
    if dep_ms <= 0 or not children:
        return None

    ranked = []
    for child in children:
        child_local_ms = local_handling_latency_ms(metrics, child)
        child_total_ms = total_latency_ms(metrics, child)
        score = max(child_local_ms, child_total_ms)
        diff = abs(dep_ms - score)
        ranked.append((diff, -score, child))

    _unused = (topology_meta, slo_cfgs)
    ranked.sort()
    best_diff, _neg_score, best_child = ranked[0]
    best_score = max(local_handling_latency_ms(metrics, best_child), total_latency_ms(metrics, best_child))
    if best_score < 1.0:
        return None
    if best_score >= max(dep_ms * 0.35, 1.0) or best_diff <= max(dep_ms * 0.75, 2.0):
        return best_child
    return None


def first_unready_monitored_child(current: str, topology: dict, monitored_services: set) -> Optional[str]:
    children = [child for child in topology.get(current, []) if child in monitored_services]
    for child in sorted(children):
        try:
            warm, desired, ready = warmup_active(child)
        except Exception:
            continue
        if warm or desired > ready:
            return child
    return None


def child_supports_internal_traversal(current: str, child: str, metrics: dict) -> bool:
    dep_ms = dependency_latency_ms(metrics, current)
    child_local_ms = local_handling_latency_ms(metrics, child)
    child_total_ms = total_latency_ms(metrics, child)
    explained_ms = max(child_local_ms, child_total_ms)
    return explained_ms >= max(dep_ms * 0.35, 1.0)


def resolve_root_cause(
    start_service: str,
    metrics: dict,
    topology: dict,
    topology_meta: dict,
    monitored_services: set,
    slo_cfgs: dict,
) -> dict:
    current = start_service
    path = [current]
    seen = {current}
    path_reason: Optional[str] = None

    for _depth in range(MAX_ROOT_CAUSE_DEPTH):
        dep_ms = dependency_latency_ms(metrics, current)
        local_ms = local_handling_latency_ms(metrics, current)
        ext_ms = external_wait_ms(metrics, current)
        child = strongest_internal_child(current, metrics, topology, topology_meta, monitored_services, slo_cfgs)

        if child and dep_ms >= max(local_ms * 0.4, 1.0) and child_supports_internal_traversal(current, child, metrics):
            if child in seen:
                break
            path_reason = "downstream_delay"
            seen.add(child)
            path.append(child)
            current = child
            continue

        if local_ms >= max(dep_ms, ext_ms, 1.0):
            leaf_classification = "local_bottleneck"
            return {
                "classification": path_reason or leaf_classification,
                "path_classification": path_reason,
                "leaf_classification": leaf_classification,
                "target": current,
                "path": path,
                "reason": leaf_classification,
            }

        if ext_ms >= max(local_ms, dep_ms, 1.0) or dep_ms > 0:
            leaf_classification = "external_or_unmonitored_delay"
            return {
                "classification": path_reason or leaf_classification,
                "path_classification": path_reason,
                "leaf_classification": leaf_classification,
                "target": current,
                "path": path,
                "reason": leaf_classification,
            }

        leaf_classification = "local_bottleneck"
        return {
            "classification": path_reason or leaf_classification,
            "path_classification": path_reason,
            "leaf_classification": leaf_classification,
            "target": current,
            "path": path,
            "reason": leaf_classification,
        }

    return {
        "classification": path_reason or "local_bottleneck",
        "path_classification": path_reason,
        "leaf_classification": "local_bottleneck",
        "target": current,
        "path": path,
        "reason": "max_depth_reached",
    }


def record_breach_streak(service: str, breached: bool) -> int:
    if breached:
        BREACH_STREAKS[service] = BREACH_STREAKS.get(service, 0) + 1
        LAST_BREACH_AT[service] = time.time()
    else:
        BREACH_STREAKS[service] = 0
    return BREACH_STREAKS.get(service, 0)


def recent_breach_hold_active(service: str) -> bool:
    last_breach = float(LAST_BREACH_AT.get(service, 0.0) or 0.0)
    return last_breach > 0.0 and (time.time() - last_breach) < RECENT_BREACH_HOLD_S


def record_low_demand_streak(service: str, low_demand: bool) -> int:
    if low_demand:
        LOW_DEMAND_STREAKS[service] = LOW_DEMAND_STREAKS.get(service, 0) + 1
    else:
        LOW_DEMAND_STREAKS[service] = 0
    return LOW_DEMAND_STREAKS.get(service, 0)


def breach_snapshot(service: str, metrics: dict, cfg: dict) -> dict:
    p90_ms, p90_source, sufficient = preferred_truth_or_ebpf_p90(metrics, service)
    rps = preferred_rps(metrics, service)
    active = is_active_for_control(metrics, service)
    slo_ms = safe_float(cfg.get("slo", 0.0), 0.0)
    ratio = (p90_ms / max(slo_ms, 1.0)) if slo_ms > 0 else 0.0
    priority = str(cfg.get("priority", "secondary"))
    near_breach_ratio = (
        min(NEAR_BREACH_RATIO_PRIMARY, PRIMARY_EARLY_WARNING_RATIO)
        if priority == "primary"
        else NEAR_BREACH_RATIO_SECONDARY
    )
    under_pressure = bool(active and sufficient and slo_ms > 0 and ratio >= near_breach_ratio)
    breached = bool(active and sufficient and slo_ms > 0 and p90_ms > slo_ms)
    streak = record_breach_streak(service, under_pressure)
    return {
        "service": service,
        "priority": priority,
        "p90_ms": p90_ms,
        "p90_source": p90_source,
        "fresh_enough": bool(sufficient),
        "active_enough": bool(active),
        "evidence_sufficient": bool(sufficient),
        "rps": rps,
        "active": bool(active),
        "slo_ms": slo_ms,
        "ratio": ratio,
        "under_pressure": under_pressure,
        "breached": breached,
        "streak": streak,
    }


def blocked_by_from_snapshot(snapshot: dict) -> str:
    if not bool(snapshot.get("fresh_enough", False)):
        return "not_fresh"
    if not bool(snapshot.get("active_enough", False)):
        return "not_active"
    if not bool(snapshot.get("under_pressure", False)):
        return "not_breached"
    return "unknown"


def update_window_history(store: Dict[str, List[bool]], service: str, value: bool, size: int) -> List[bool]:
    history = list(store.get(service, []))
    history.append(bool(value))
    history = history[-max(1, int(size)) :]
    store[service] = history
    return history


def pressure_snapshot(service: str, cfg: dict, metrics: dict) -> dict:
    current_replicas, _ready = read_replicas(service)
    p90_ms, _src, sufficient = preferred_truth_or_ebpf_p90(metrics, service)
    slo_ms = max(1.0, safe_float(cfg.get("slo", 0.0), 0.0))
    rps = preferred_rps(metrics, service)
    runq_ms = runq_latency_ms(metrics, service)
    runq_threshold = runq_threshold_ms(metrics, service)
    err5xx = error_rate_5xx(metrics, service)
    timeout = timeout_rate(metrics, service)
    active = bool(rps > ACTIVE_RPS_THRESHOLD)
    has_meaningful_demand = meaningful_demand(service, rps, current_replicas)
    p90_breach = bool(sufficient and p90_ms > slo_ms)
    runq_breach = bool(runq_ms > runq_threshold)
    err_breach = bool(err5xx >= OVERLOAD_ERROR_RATE_THRESHOLD)
    timeout_breach = bool(timeout >= OVERLOAD_TIMEOUT_RATE_THRESHOLD)
    raw_pressure = bool(active and (p90_breach or runq_breach or err_breach or timeout_breach))
    scaleout_eligible_pressure = bool(
        active and (p90_breach or err_breach or timeout_breach or (runq_breach and has_meaningful_demand))
    )
    pressure = bool(scaleout_eligible_pressure)
    reason_bits = []
    if p90_breach:
        reason_bits.append("p90_slo")
    if runq_breach:
        reason_bits.append("runq")
    if err_breach:
        reason_bits.append("error_5xx")
    if timeout_breach:
        reason_bits.append("timeout")
    trigger_reason = "+".join(reason_bits) if reason_bits else "none"
    if pressure:
        LAST_BREACH_AT[service] = time.time()
    scaleout_block_reason = ""
    if raw_pressure and not scaleout_eligible_pressure:
        scaleout_block_reason = "runq_without_meaningful_demand"
    return {
        "service": service,
        "priority": str(cfg.get("priority", "secondary")),
        "p90_ms": p90_ms,
        "slo_ms": slo_ms,
        "rps": rps,
        "rps_per_replica": (rps / max(current_replicas, 1)),
        "active": active,
        "raw_pressure": raw_pressure,
        "pressure": pressure,
        "scaleout_eligible_pressure": scaleout_eligible_pressure,
        "pressure_confirmed": False,
        "pressure_window_1": False,
        "pressure_window_2": False,
        "trigger_reason": trigger_reason,
        "ratio": (p90_ms / slo_ms) if slo_ms > 0 else 0.0,
        "runq_ms": runq_ms,
        "runq_threshold_ms": runq_threshold,
        "error_rate_5xx": err5xx,
        "timeout_rate": timeout,
        "fresh_enough": bool(sufficient),
        "current_replicas": current_replicas,
        "has_meaningful_demand": has_meaningful_demand,
        "p90_breach": p90_breach,
        "runq_breach": runq_breach,
        "err_breach": err_breach,
        "timeout_breach": timeout_breach,
        "scaleout_eligible": scaleout_eligible_pressure,
        "scaleout_block_reason": scaleout_block_reason,
    }


def attach_pressure_windows(snapshot: dict) -> dict:
    history = update_window_history(PRESSURE_WINDOWS, snapshot["service"], bool(snapshot.get("pressure", False)), PRESSURE_WINDOW_COUNT)
    padded = ([False] * max(0, PRESSURE_WINDOW_COUNT - len(history))) + history
    snapshot["pressure_window_1"] = bool(padded[-2]) if len(padded) >= 2 else False
    snapshot["pressure_window_2"] = bool(padded[-1]) if len(padded) >= 1 else False
    snapshot["pressure_confirmed"] = len(history) >= PRESSURE_WINDOW_COUNT and all(history[-PRESSURE_WINDOW_COUNT:])
    return snapshot


def remember_primary_local_target(trigger_service: str, target_service: str) -> None:
    PRIMARY_LAST_LOCAL_TARGET[trigger_service] = str(target_service)
    PRIMARY_LAST_LOCAL_TARGET_AT[trigger_service] = time.time()


def recent_primary_local_target(trigger_service: str, slo_cfgs: dict) -> Optional[str]:
    target = str(PRIMARY_LAST_LOCAL_TARGET.get(trigger_service, "") or "").strip()
    ts = float(PRIMARY_LAST_LOCAL_TARGET_AT.get(trigger_service, 0.0) or 0.0)
    if not target or ts <= 0.0:
        return None
    if (time.time() - ts) > PRIMARY_TARGET_STICKY_SECONDS:
        return None
    if target not in slo_cfgs:
        return None
    return target


def traffic_pattern_blocked_by(pattern: str) -> str:
    if pattern == "recovery":
        return "recovery_hold"
    if pattern == "low_demand":
        return "low_demand_hold"
    if pattern == "spike":
        return "protective_only"
    return "reason_not_scalable"


def rising_traffic_present(trigger: dict, traffic: dict) -> bool:
    short_rps = safe_float(traffic.get("short_rps", trigger.get("rps", 0.0)), 0.0)
    long_rps = safe_float(traffic.get("long_rps", 0.0), 0.0)
    return short_rps >= max(long_rps * 1.05, long_rps + 0.5, 1.0)


def active_scale_target_for(trigger_service: str, slo_cfgs: dict) -> Optional[str]:
    target = str(ACTIVE_SCALE_TARGETS.get(trigger_service, "") or "").strip()
    ts = float(ACTIVE_SCALE_TARGET_AT.get(trigger_service, 0.0) or 0.0)
    if not target or ts <= 0.0:
        return None
    if (time.time() - ts) > ACTIVE_TARGET_STICKY_SECONDS:
        return None
    if target not in slo_cfgs:
        return None
    return target


def remember_active_scale_target(trigger_service: str, target_service: str) -> None:
    ACTIVE_SCALE_TARGETS[trigger_service] = str(target_service)
    ACTIVE_SCALE_TARGET_AT[trigger_service] = time.time()


def clear_active_scale_target(trigger_service: str) -> None:
    ACTIVE_SCALE_TARGETS.pop(trigger_service, None)
    ACTIVE_SCALE_TARGET_AT.pop(trigger_service, None)


def rollout_ready_for(service: str) -> Tuple[bool, int, int]:
    desired, ready = read_replicas(service)
    return ready >= max(desired, 0), desired, ready


def record_scale_event(service: str, before: int, after: int, reference_p90: float, reference_violation: float) -> None:
    LAST_SCALE_EVENTS[service] = {
        "last_scale_time": time.time(),
        "last_scale_replicas_before": int(before),
        "last_scale_replicas_after": int(after),
        "last_scale_reference_p90": float(reference_p90),
        "last_scale_reference_violation": float(reference_violation),
        "last_scale_error_rate": 0.0,
        "last_scale_timeout_rate": 0.0,
        "last_scale_runq_ms": 0.0,
        "scale_effect": "pending",
        "flat_or_worse_count": safe_int(LAST_SCALE_EVENTS.get(service, {}).get("flat_or_worse_count", 0), 0),
    }


def recent_scale_improving(service: str, p90_ms: float, ratio: float, err5xx: float, timeout: float, runq_ms: float) -> bool:
    state = LAST_SCALE_EVENTS.get(service)
    if not state:
        return False
    elapsed = time.time() - float(state.get("last_scale_time", 0.0) or 0.0)
    if elapsed > max(15.0, LOOP_SECONDS * 4.0):
        return False

    ref_p90 = max(1.0, safe_float(state.get("last_scale_reference_p90", p90_ms), p90_ms))
    ref_ratio = max(0.0, safe_float(state.get("last_scale_reference_violation", ratio), ratio))
    ref_err = max(0.0, safe_float(state.get("last_scale_error_rate", err5xx), err5xx))
    ref_timeout = max(0.0, safe_float(state.get("last_scale_timeout_rate", timeout), timeout))
    ref_runq = max(0.0, safe_float(state.get("last_scale_runq_ms", runq_ms), runq_ms))

    p90_improving = p90_ms <= (ref_p90 * 0.92)
    ratio_improving = ratio <= max(0.0, ref_ratio - 0.08)
    errors_stable = err5xx <= max(ref_err, OVERLOAD_ERROR_RATE_THRESHOLD * 0.25)
    timeout_stable = timeout <= max(ref_timeout, OVERLOAD_TIMEOUT_RATE_THRESHOLD * 0.5)
    runq_not_worse = runq_ms <= max(ref_runq * 1.05, ref_runq + 0.5, RUNQ_FIXED_THRESHOLD_MS)
    return bool((p90_improving or ratio_improving) and errors_stable and timeout_stable and runq_not_worse)


def target_admissibility(
    final_reason: str,
    target: str,
    target_cfg: dict,
    metrics: dict,
    trigger: dict,
) -> Tuple[bool, str, dict]:
    current_replicas, _ready = read_replicas(target)
    target_p90, _src, target_sufficient = preferred_truth_or_ebpf_p90(metrics, target)
    target_p90 = target_p90 if target_sufficient else safe_float(trigger.get("p90_ms", 0.0), 0.0)
    target_slo = max(1.0, safe_float(target_cfg.get("slo", trigger.get("slo_ms", 0.0)), trigger.get("slo_ms", 1.0)))
    scale_rps = max(preferred_rps(metrics, target), safe_float(trigger.get("rps", 0.0), 0.0))
    runq_ms = runq_latency_ms(metrics, target)
    runq_threshold = runq_threshold_ms(metrics, target)
    err5xx = error_rate_5xx(metrics, target)
    timeout = timeout_rate(metrics, target)
    demand_ok = meaningful_demand(target, scale_rps, current_replicas)
    p90_breach = bool(target_sufficient and target_p90 > target_slo)
    error_breach = bool(err5xx >= OVERLOAD_ERROR_RATE_THRESHOLD)
    timeout_breach = bool(timeout >= OVERLOAD_TIMEOUT_RATE_THRESHOLD)
    runq_breach = bool(runq_ms > runq_threshold)

    if final_reason == "external_or_unmonitored_delay":
        return False, "external_or_unmonitored_delay", {
            "p90_ms": target_p90,
            "slo_ms": target_slo,
            "rps": scale_rps,
            "runq_ms": runq_ms,
            "runq_threshold_ms": runq_threshold,
            "error_rate_5xx": err5xx,
            "timeout_rate": timeout,
            "current_replicas": current_replicas,
            "demand_ok": demand_ok,
            "p90_breach": p90_breach,
            "error_breach": error_breach,
            "timeout_breach": timeout_breach,
            "runq_breach": runq_breach,
        }

    if p90_breach or error_breach or timeout_breach:
        reason = "p90_or_error_signal"
        return True, reason, {
            "p90_ms": target_p90,
            "slo_ms": target_slo,
            "rps": scale_rps,
            "runq_ms": runq_ms,
            "runq_threshold_ms": runq_threshold,
            "error_rate_5xx": err5xx,
            "timeout_rate": timeout,
            "current_replicas": current_replicas,
            "demand_ok": demand_ok,
            "p90_breach": p90_breach,
            "error_breach": error_breach,
            "timeout_breach": timeout_breach,
            "runq_breach": runq_breach,
        }

    if runq_breach and demand_ok:
        return True, "runq_with_meaningful_demand", {
            "p90_ms": target_p90,
            "slo_ms": target_slo,
            "rps": scale_rps,
            "runq_ms": runq_ms,
            "runq_threshold_ms": runq_threshold,
            "error_rate_5xx": err5xx,
            "timeout_rate": timeout,
            "current_replicas": current_replicas,
            "demand_ok": demand_ok,
            "p90_breach": p90_breach,
            "error_breach": error_breach,
            "timeout_breach": timeout_breach,
            "runq_breach": runq_breach,
        }

    block_reason = "low_target_demand"
    if runq_breach and not demand_ok:
        block_reason = "runq_without_meaningful_target_demand"
    return False, block_reason, {
        "p90_ms": target_p90,
        "slo_ms": target_slo,
        "rps": scale_rps,
        "runq_ms": runq_ms,
        "runq_threshold_ms": runq_threshold,
        "error_rate_5xx": err5xx,
        "timeout_rate": timeout,
        "current_replicas": current_replicas,
        "demand_ok": demand_ok,
        "p90_breach": p90_breach,
        "error_breach": error_breach,
        "timeout_breach": timeout_breach,
        "runq_breach": runq_breach,
    }


def overprovisioned_for_downscale(service: str, snapshot: dict, current_replicas: int, target_per_replica: float, previous_rps: float) -> bool:
    short_rps = safe_float(snapshot.get("rps", 0.0), 0.0)
    p90_ms = safe_float(snapshot.get("p90_ms", 0.0), 0.0)
    slo_ms = max(1.0, safe_float(snapshot.get("slo_ms", 0.0), 0.0))
    err5xx = safe_float(snapshot.get("error_rate_5xx", 0.0), 0.0)
    timeout = safe_float(snapshot.get("timeout_rate", 0.0), 0.0)
    current_runq = safe_float(snapshot.get("runq_ms", 0.0), 0.0)
    rps_per_replica = short_rps / max(current_replicas, 1)
    rps_not_rising = short_rps <= max(previous_rps, previous_rps * 1.02 + 0.2)
    return bool(
        p90_ms > 0.0
        and p90_ms <= (slo_ms * 0.70)
        and err5xx <= (OVERLOAD_ERROR_RATE_THRESHOLD * 0.25)
        and timeout <= (OVERLOAD_TIMEOUT_RATE_THRESHOLD * 0.25)
        and current_runq <= max(runq_threshold_ms({}, service) * 0.5, 1.0)
        and rps_per_replica <= (target_per_replica * 0.60)
        and rps_not_rising
    )


def scale_feedback_state(service: str, trigger: dict) -> dict:
    state = LAST_SCALE_EVENTS.get(service)
    rollout_ready, desired, ready = rollout_ready_for(service)
    payload = {
        "rollout_ready": bool(rollout_ready),
        "desired_replicas": int(desired),
        "ready_replicas": int(ready),
        "waiting_for_post_scale_feedback": False,
        "scale_effect": "none",
        "flat_or_worse_count": 0,
    }
    if not state:
        return payload

    elapsed = time.time() - float(state.get("last_scale_time", 0.0) or 0.0)
    payload["flat_or_worse_count"] = safe_int(state.get("flat_or_worse_count", 0), 0)
    if not rollout_ready or elapsed < POST_SCALE_FEEDBACK_WAIT_SECONDS:
        payload["waiting_for_post_scale_feedback"] = True
        payload["scale_effect"] = str(state.get("scale_effect", "pending") or "pending")
        return payload

    current_p90 = safe_float(trigger.get("p90_ms", 0.0), 0.0)
    current_violation = safe_float(trigger.get("ratio", 0.0), 0.0)
    ref_p90 = max(1.0, safe_float(state.get("last_scale_reference_p90", current_p90), current_p90))
    ref_violation = safe_float(state.get("last_scale_reference_violation", current_violation), current_violation)

    if current_p90 <= (ref_p90 * 0.90) or current_violation <= max(0.0, ref_violation - 0.10):
        effect = "improved"
        flat_or_worse_count = 0
    elif current_p90 >= (ref_p90 * 1.05) and current_violation >= (ref_violation + 0.05):
        effect = "worse"
        flat_or_worse_count = safe_int(state.get("flat_or_worse_count", 0), 0) + 1
    else:
        effect = "flat"
        flat_or_worse_count = safe_int(state.get("flat_or_worse_count", 0), 0) + 1

    state["scale_effect"] = effect
    state["flat_or_worse_count"] = flat_or_worse_count
    payload["scale_effect"] = effect
    payload["flat_or_worse_count"] = flat_or_worse_count
    return payload


def no_scale_resolution(
    final_reason: str,
    path_reason: Optional[str],
    target: str,
    path: List[str],
    reason: str,
    blocked_by: str,
    traffic: Optional[dict] = None,
    trigger: Optional[dict] = None,
    desired: Optional[dict] = None,
) -> dict:
    traffic = traffic or {}
    trigger = trigger or {}
    desired = desired or {}
    return {
        "classification": final_reason,
        "path_classification": path_reason,
        "leaf_classification": final_reason,
        "target": target,
        "path": path,
        "reason": reason,
        "blocked_by": blocked_by,
        "upscale_eligible": False,
        "traffic_pattern": traffic.get("pattern", ""),
        "short_rps": safe_float(traffic.get("short_rps", trigger.get("rps", 0.0)), 0.0),
        "long_rps": safe_float(traffic.get("long_rps", 0.0), 0.0),
        "rps_growth": safe_float(traffic.get("rps_growth", 0.0), 0.0),
        "short_p90_ms": safe_float(traffic.get("short_p90_ms", trigger.get("p90_ms", 0.0)), 0.0),
        "target_from_traffic": safe_int(desired.get("target_from_traffic", 0), 0),
        "target_from_slo": safe_int(desired.get("target_from_slo", 0), 0),
        "desired_replicas_from_traffic": safe_int(desired.get("target_from_traffic", 0), 0),
        "target_rps_per_replica": safe_float(desired.get("target_rps_per_replica", 0.0), 0.0),
        "trigger_slo": safe_float(desired.get("trigger_slo", trigger.get("slo_ms", 0.0)), 0.0),
        "raw_target": safe_int(desired.get("raw_target", 0), 0),
        "max_growth": safe_int(desired.get("max_growth", 0), 0),
        "target_replicas": safe_int(desired.get("target_replicas", 0), 0),
        "clamped_target_replicas": safe_int(desired.get("clamped_target_replicas", 0), 0),
        "min_replicas": safe_int(desired.get("min_replicas", 1), 1),
        "max_replicas": safe_int(desired.get("max_replicas", 1), 1),
        "clamp_applied": bool(desired.get("clamp_applied", False)),
    }


def can_scale_now(service: str, cfg: dict, trigger: Optional[dict] = None) -> Tuple[bool, str, int, int]:
    current_replicas, ready_replicas = read_replicas(service)
    max_replicas = max(1, safe_int(cfg.get("max", 1), 1))
    if current_replicas >= max_replicas:
        return False, "at_max", current_replicas, ready_replicas
    ready_gap = current_replicas - max(ready_replicas, 0)
    warm = current_replicas > max(ready_replicas, 0) and ready_gap >= max(1, math.ceil(current_replicas * WARMUP_READY_GAP_RATIO))
    if warm:
        severe_pressure = bool(
            trigger
            and bool(trigger.get("breached", False))
            and safe_float(trigger.get("ratio", 0.0), 0.0) >= 1.1
            and ready_gap <= 1
        )
        if severe_pressure:
            return True, "", current_replicas, ready_replicas
        return False, "warmup_active", current_replicas, ready_replicas
    return True, "", current_replicas, ready_replicas


def propose_upscale_action(
    trigger: dict,
    metrics: dict,
    topology: dict,
    topology_meta: dict,
    slo_cfgs: dict,
) -> Tuple[Optional[dict], dict]:
    service = trigger["service"]
    resolution = resolve_root_cause(service, metrics, topology, topology_meta, set(slo_cfgs.keys()), slo_cfgs)
    target = resolution["target"]
    path_reason = resolution.get("path_classification")
    final_reason = resolution.get("leaf_classification", resolution.get("classification"))
    target_cfg = slo_cfgs.get(target)
    if not target_cfg:
        return None, {"classification": "external_or_unmonitored_delay", "path_classification": path_reason, "leaf_classification": final_reason, "target": target, "path": resolution.get("path", [service]), "reason": "target_not_monitored", "blocked_by": "unknown", "upscale_eligible": False}
    current_replicas, _ready_replicas = read_replicas(target)
    max_replicas = max(1, safe_int(target_cfg.get("max", 1), 1))
    min_replicas = max(1, safe_int(target_cfg.get("min", 1), 1))
    if final_reason == "external_or_unmonitored_delay":
        resolution["blocked_by"] = "external_or_unmonitored_delay"
        resolution["upscale_eligible"] = False
        return None, resolution
    if not bool(trigger.get("pressure_confirmed", False)):
        resolution["blocked_by"] = str(trigger.get("scaleout_block_reason", "") or "pressure_not_confirmed")
        resolution["upscale_eligible"] = False
        return None, resolution
    if not bool(trigger.get("scaleout_eligible", False)):
        resolution["blocked_by"] = str(trigger.get("scaleout_block_reason", "") or "scaleout_not_eligible")
        resolution["upscale_eligible"] = False
        return None, resolution
    if current_replicas >= max_replicas:
        resolution["blocked_by"] = "at_max"
        resolution["upscale_eligible"] = False
        return None, resolution
    if cooldown_active(UPSCALE_COOLDOWNS, target):
        resolution["blocked_by"] = "cooldown"
        resolution["upscale_eligible"] = False
        return None, resolution

    admissible, admissible_reason, target_signal = target_admissibility(final_reason, target, target_cfg, metrics, trigger)
    if not admissible:
        resolution["blocked_by"] = admissible_reason
        resolution["upscale_eligible"] = False
        return None, resolution

    target_p90 = safe_float(target_signal.get("p90_ms", 0.0), 0.0)
    target_slo = max(1.0, safe_float(target_signal.get("slo_ms", 0.0), 1.0))
    scale_rps = safe_float(target_signal.get("rps", 0.0), 0.0)
    runq_ms = safe_float(target_signal.get("runq_ms", 0.0), 0.0)
    runq_threshold = safe_float(target_signal.get("runq_threshold_ms", 0.0), 0.0)
    err5xx = safe_float(target_signal.get("error_rate_5xx", 0.0), 0.0)
    timeout = safe_float(target_signal.get("timeout_rate", 0.0), 0.0)
    current_replicas = safe_int(target_signal.get("current_replicas", current_replicas), current_replicas)
    rps_per_replica = scale_rps / max(current_replicas, 1)
    ratio = target_p90 / max(target_slo, 1.0)
    improving_after_recent_scale = recent_scale_improving(target, target_p90, ratio, err5xx, timeout, runq_ms)
    runq_only_signal = bool(
        target_signal.get("runq_breach", False)
        and not target_signal.get("p90_breach", False)
        and not target_signal.get("error_breach", False)
        and not target_signal.get("timeout_breach", False)
    )

    if (
        ratio >= 1.75
        or runq_ms >= (runq_threshold * 3.0)
        or err5xx >= (OVERLOAD_ERROR_RATE_THRESHOLD * 2.0)
        or timeout >= (OVERLOAD_TIMEOUT_RATE_THRESHOLD * 2.0)
    ):
        step = 4
        scale_mode = "very_severe"
    elif (
        ratio >= 1.35
        or runq_ms >= (runq_threshold * 2.0)
        or err5xx >= OVERLOAD_ERROR_RATE_THRESHOLD
        or timeout >= OVERLOAD_TIMEOUT_RATE_THRESHOLD
    ):
        step = 3
        scale_mode = "strong"
    else:
        step = 2
        scale_mode = "moderate"

    if improving_after_recent_scale:
        step = max(2, step - 1)
        scale_mode = "moderate" if step == 2 else scale_mode
    if runq_only_signal and scale_rps < max(RUNQ_ONLY_MIN_RPS * 2.0, target_rps_per_replica(target)):
        step = min(step, 2)
        scale_mode = "moderate"
    if current_replicas >= 8:
        step = min(step, 2)
        scale_mode = "moderate"
    elif current_replicas >= 5:
        step = min(step, 3)
        if scale_mode == "very_severe" and step < 4:
            scale_mode = "strong"

    proposed_target = current_replicas + step
    target_replicas = max(min_replicas, min(max_replicas, proposed_target))
    if target_replicas <= current_replicas:
        resolution["blocked_by"] = "target_not_above_current"
        resolution["upscale_eligible"] = False
        return None, resolution

    action = {
        "trigger_service": service,
        "target_service": target,
        "priority": trigger["priority"],
        "classification": final_reason,
        "path_reason": path_reason,
        "final_reason": final_reason,
        "reason": str(trigger.get("trigger_reason", "pressure")),
        "path": list(resolution.get("path", [service])),
        "step": int(target_replicas - current_replicas),
        "current_replicas": current_replicas,
        "target_replicas": target_replicas,
        "breach_streak": PRESSURE_WINDOW_COUNT,
        "ratio": float(ratio),
        "p90_ms": float(target_p90),
        "slo_ms": float(target_slo),
        "rps": float(scale_rps),
        "runq_ms": runq_ms,
        "cpu_throttle_ratio": cpu_throttle_ratio(metrics, target),
        "runq_threshold_ms": runq_threshold,
        "downstream_ms": dependency_latency_ms(metrics, target),
        "service_handling_ms": local_handling_latency_ms(metrics, target),
        "external_wait_ms": external_wait_ms(metrics, target),
        "under_pressure": bool(trigger.get("pressure", False)),
        "protective_root_used": False,
        "upscale_eligible": True,
        "blocked_by": "",
        "fresh_enough": True,
        "active_enough": bool(trigger.get("active", False)),
        "trigger_breached": bool(trigger.get("pressure", False)),
        "local_fraction": local_fraction(metrics, target),
        "dependency_fraction": dependency_fraction(metrics, target),
        "runq_p90_ms": runq_p90_latency_ms(metrics, target),
        "traffic_pattern": "",
        "short_rps": float(scale_rps),
        "long_rps": 0.0,
        "rps_growth": 0.0,
        "short_p90_ms": float(target_p90),
        "trigger_slo": float(target_slo),
        "target_from_traffic": 0,
        "target_from_slo": 0,
        "raw_target": int(target_replicas),
        "max_growth": int(target_replicas - current_replicas),
        "clamped_target_replicas": int(target_replicas),
        "min_replicas": min_replicas,
        "max_replicas": max_replicas,
        "clamp_applied": bool(target_replicas != proposed_target),
        "desired_replicas_from_traffic": 0,
        "target_rps_per_replica": float(target_rps_per_replica(target)),
        "runq_pct": runq_pct(metrics, target),
        "throttle_pct": throttle_pct(metrics, target),
        "local_resource_support": local_resource_support(metrics, target),
        "rollout_ready": False,
        "waiting_for_post_scale_feedback": False,
        "scale_effect": "",
        "active_scale_target": target,
        "target_changed": False,
        "pressure_window_1": bool(trigger.get("pressure_window_1", False)),
        "pressure_window_2": bool(trigger.get("pressure_window_2", False)),
        "pressure_confirmed": bool(trigger.get("pressure_confirmed", False)),
        "scale_mode": scale_mode,
        "trigger_reason": str(trigger.get("trigger_reason", "pressure")),
        "rps_per_replica": float(rps_per_replica),
        "error_rate_5xx": float(err5xx),
        "timeout_rate": float(timeout),
        "improving_after_recent_scale": bool(improving_after_recent_scale),
        "scaleout_eligible": bool(trigger.get("scaleout_eligible", False)),
        "scaleout_block_reason": str(trigger.get("scaleout_block_reason", "")),
        "target_admissible": True,
        "target_admissible_reason": str(admissible_reason),
        "service_priority": service_priority(target),
    }
    return action, resolution


def pick_best_action(actions: List[dict]) -> Optional[dict]:
    if not actions:
        return None

    def action_key(action: dict):
        return (
            0 if str(action.get("path_reason", "")) == "downstream_delay" else 1,
            -len(list(action.get("path", []))),
            -safe_int(action.get("service_priority", 0), 0),
            safe_int(action.get("step", 0), 0),
            -safe_float(action.get("ratio", 0.0), 0.0),
            str(action.get("trigger_service", "")),
            str(action.get("target_service", "")),
        )

    return sorted(actions, key=action_key)[0]


def emit_trace(
    trigger_service: str,
    final_target_service: str,
    priority_type: str,
    classification: str,
    path_reason: str,
    final_reason: str,
    reason: str,
    path: List[str],
    decision: str,
    current_replicas: int,
    target_replicas: int,
    p90_ms: float,
    slo_ms: float,
    rps: float,
    runq_ms: float,
    cpu_throttle_ratio_value: float,
    runq_threshold_ms_value: float,
    downstream_ms: float,
    service_handling_ms: float,
    external_wait_ms_value: float,
    under_pressure: bool,
    breach_streak: int,
    protective_root_used: bool,
    applied_scale_step: int,
    trigger_breached: bool = False,
    fresh_enough: bool = False,
    active_enough: bool = False,
    local_fraction_value: float = 0.0,
    dependency_fraction_value: float = 0.0,
    runq_p90_ms: float = 0.0,
    upscale_eligible: bool = False,
    blocked_by: str = "",
    traffic_pattern: str = "",
    short_rps: float = 0.0,
    long_rps: float = 0.0,
    actual_replica_change: int = 0,
    target_rps_per_replica_value: float = 0.0,
    desired_replicas_from_traffic: int = 0,
    target_from_traffic: int = 0,
    target_from_slo: int = 0,
    raw_target: int = 0,
    max_growth: int = 0,
    clamped_target_replicas: int = 0,
    min_replicas: int = 1,
    max_replicas: int = 1,
    clamp_applied: bool = False,
    short_p90_ms: float = 0.0,
    trigger_slo: float = 0.0,
    rps_growth: float = 0.0,
    runq_pct_value: float = 0.0,
    throttle_pct_value: float = 0.0,
    local_resource_support_value: str = "",
    rollout_ready: bool = False,
    waiting_for_post_scale_feedback: bool = False,
    scale_effect: str = "",
    active_scale_target: str = "",
    target_changed: bool = False,
    downscale_step: int = 0,
    downscale_reason: str = "",
    pressure_window_1: bool = False,
    pressure_window_2: bool = False,
    pressure_confirmed: bool = False,
    scale_mode: str = "none",
    trigger_reason: str = "",
    rps_per_replica: float = 0.0,
    error_rate_5xx_value: float = 0.0,
    timeout_rate_value: float = 0.0,
    scaleout_eligible: bool = False,
    scaleout_block_reason: str = "",
    target_admissible: bool = False,
    target_admissible_reason: str = "",
) -> None:
    if decision == "scale_up":
        action = f"scale_to_{target_replicas}"
    elif decision == "scale_down":
        action = f"downscale_to_{target_replicas}"
    else:
        action = f"no_scale_{reason}"

    payload = {
        "type": "decision_trace",
        "ts_unix_ms": int(time.time() * 1000),
        "trigger_service": trigger_service,
        "identified_target": final_target_service,
        "final_bottleneck_service": final_target_service,
        "final_bottleneck_type": final_reason,
        "traversal_path": list(path),
        "decision_action": decision,
        "blocked_by": str(blocked_by or ""),
        "scale_mode": str(scale_mode or "none"),
        "trigger_reason": str(trigger_reason or reason or ""),
        "pressure_window_1": bool(pressure_window_1),
        "pressure_window_2": bool(pressure_window_2),
        "pressure_confirmed": bool(pressure_confirmed),
        "scaleout_eligible": bool(scaleout_eligible),
        "scaleout_block_reason": str(scaleout_block_reason or ""),
        "target_admissible": bool(target_admissible),
        "target_admissible_reason": str(target_admissible_reason or ""),
        "current_replicas": int(current_replicas),
        "target_replicas": int(target_replicas),
        "actual_replica_change": int(actual_replica_change),
        "slo_ms": round(float(slo_ms), 3),
        "p90_ms": round(float(p90_ms), 3),
        "rps": round(float(rps), 3),
        "rps_per_replica": round(float(rps_per_replica), 3),
        "runq_pct": round(float(runq_pct_value), 3),
        "throttle_pct": round(float(throttle_pct_value), 3),
        "local_resource_support": str(local_resource_support_value or ""),
        "runq_latency_ms": round(float(runq_ms), 3),
        "error_rate_5xx": round(float(error_rate_5xx_value), 6),
        "timeout_rate": round(float(timeout_rate_value), 6),
        "action": action,
        "service_handling_ms": round(float(service_handling_ms), 3),
        "dependency_delay_ms": round(float(downstream_ms), 3),
        "external_wait_ms": round(float(external_wait_ms_value), 3),
        "min_replicas": int(min_replicas),
        "max_replicas": int(max_replicas),
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if TRACE_LOGS:
        logger.info("TRACE %s", serialized)
    try:
        TRACE_REDIS.lpush(TRACE_REDIS_KEY, serialized)
        TRACE_REDIS.ltrim(TRACE_REDIS_KEY, 0, TRACE_REDIS_MAX - 1)
    except Exception:
        pass


def propose_downscale_action(service: str, metrics: dict, cfg: dict, snapshot: Optional[dict] = None) -> Optional[dict]:
    current_replicas, _ready_replicas = read_replicas(service)
    min_replicas = max(1, safe_int(cfg.get("min", 1), 1))
    if current_replicas <= min_replicas:
        return None
    if snapshot and bool(snapshot.get("pressure", False)):
        return None
    if recent_breach_hold_active(service):
        return None
    if recent_true_target_hold_active(service):
        return None
    short_rps = safe_float((snapshot or {}).get("rps", preferred_rps(metrics, service)), 0.0)
    p90_ms = safe_float((snapshot or {}).get("p90_ms", preferred_truth_or_ebpf_p90(metrics, service)[0]), 0.0)
    slo_ms = max(1.0, safe_float((snapshot or {}).get("slo_ms", cfg.get("slo", 0.0)), 0.0))
    err5xx = safe_float((snapshot or {}).get("error_rate_5xx", error_rate_5xx(metrics, service)), 0.0)
    timeout = safe_float((snapshot or {}).get("timeout_rate", timeout_rate(metrics, service)), 0.0)
    runq_ms = runq_latency_ms(metrics, service)
    target_per_replica = target_rps_per_replica(service)
    needed_replicas = max(min_replicas, int(math.ceil(short_rps / max(target_per_replica, 0.1)))) if short_rps > 0 else min_replicas
    previous_rps = LAST_SHORT_RPS.get(service, short_rps)
    rising_again = short_rps > max(previous_rps * 1.1, previous_rps + 0.5)
    healthy = bool(
        p90_ms > 0.0
        and p90_ms <= (slo_ms * 0.75)
        and err5xx <= (OVERLOAD_ERROR_RATE_THRESHOLD * 0.25)
        and timeout <= (OVERLOAD_TIMEOUT_RATE_THRESHOLD * 0.25)
        and runq_ms <= max(runq_threshold_ms(metrics, service) * 0.5, 1.0)
    )
    low_demand = bool(needed_replicas < current_replicas and not rising_again and short_rps < max(current_replicas * target_per_replica * 0.8, 1.0))
    history = update_window_history(LOW_DEMAND_WINDOWS, service, low_demand, DOWNSCALE_WINDOW_COUNT)
    if len(history) < DOWNSCALE_WINDOW_COUNT or not all(history):
        return None
    if cooldown_active(DOWNSCALE_COOLDOWNS, service):
        return None
    overprovisioned = bool(
        healthy
        and needed_replicas <= max(min_replicas, current_replicas - 1)
        and short_rps / max(current_replicas, 1) <= (target_per_replica * 0.65)
    )
    downscale_step = 2 if current_replicas >= 6 and (overprovisioned or needed_replicas <= current_replicas - 2) else 1
    target = max(min_replicas, current_replicas - downscale_step)
    target = max(target, needed_replicas)
    if target >= current_replicas:
        return None

    return {
        "service": service,
        "current_replicas": current_replicas,
        "target_replicas": target,
        "step": int(current_replicas - target),
        "rps": short_rps,
        "short_rps": short_rps,
        "long_rps": 0.0,
        "target_rps_per_replica": target_per_replica,
        "desired_replicas_from_traffic": needed_replicas,
        "runq_ms": runq_ms,
        "reason": "low_demand_confirmed",
        "downscale_reason": "overprovisioned_downscale" if overprovisioned else "low_demand_confirmed",
        "downscale_step": int(current_replicas - target),
        "rps_growth": short_rps - previous_rps,
        "short_p90_ms": p90_ms,
        "trigger_slo": slo_ms,
        "target_from_traffic": needed_replicas,
        "target_from_slo": current_replicas,
        "runq_pct": runq_pct(metrics, service),
        "throttle_pct": throttle_pct(metrics, service),
        "local_resource_support": local_resource_support(metrics, service),
        "low_demand_streak": len(history),
        "pressure_window_1": False,
        "pressure_window_2": False,
        "pressure_confirmed": False,
        "scale_mode": "none",
        "trigger_reason": "overprovisioned" if overprovisioned else "low_demand",
        "rps_per_replica": short_rps / max(current_replicas, 1),
        "error_rate_5xx": err5xx,
        "timeout_rate": timeout,
        "overprovisioned": overprovisioned,
        "scaleout_eligible": False,
        "scaleout_block_reason": "",
        "target_admissible": False,
        "target_admissible_reason": "",
    }


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        fresh = (time.time() - float(READY_STATE.get("last_loop_ts", 0.0) or 0.0)) <= max(10.0, LOOP_SECONDS * 4.0)
        ready = bool(READY_STATE.get("ready")) and fresh
        payload = {
            "ready": ready,
            "last_error": READY_STATE.get("last_error", ""),
            "last_loop_ts": READY_STATE.get("last_loop_ts", 0.0),
            "target_namespace": TARGET_NAMESPACE,
            "root_service": ROOT_SERVICE,
            "loop_seconds": LOOP_SECONDS,
        }
        if path == "/healthz":
            self.send_response(200)
        elif path == "/readyz":
            self.send_response(200 if ready else 503)
        else:
            self.send_response(404)
            payload = {"error": "not found"}
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def log_message(self, fmt, *args):
        return


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def start_health_server():
    try:
        server = ReusableThreadingHTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    except OSError as exc:
        logger.warning("Health server disabled on :%d (%s)", HEALTH_PORT, exc)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health server listening on :%d", HEALTH_PORT)


def main():
    logger.info(
        "Runtime config: target_ns=%s root=%s loop=%.1fs active_rps=%.2f runq_fixed=%.2fms",
        TARGET_NAMESPACE,
        ROOT_SERVICE,
        LOOP_SECONDS,
        ACTIVE_RPS_THRESHOLD,
        RUNQ_FIXED_THRESHOLD_MS,
    )
    start_health_server()

    while True:
        loop_start = time.time()
        try:
            slo_cfgs = get_slo_configs()
            graph = fetch_graph_payload()
            metrics = graph.get("metrics", {}) if isinstance(graph, dict) else {}
            topology = graph.get("topology", {}) if isinstance(graph, dict) else {}
            topology_meta = graph.get("topology_meta", {}) if isinstance(graph, dict) else {}

            upscale_actions: List[dict] = []
            downscale_actions: List[dict] = []
            snapshots: Dict[str, dict] = {}

            ordered_services = sorted(slo_cfgs.keys())
            if ROOT_SERVICE in ordered_services:
                ordered_services.remove(ROOT_SERVICE)
                ordered_services.insert(0, ROOT_SERVICE)

            for service in ordered_services:
                cfg = slo_cfgs[service]
                snapshot = attach_pressure_windows(pressure_snapshot(service, cfg, metrics))
                snapshots[service] = snapshot
                action, resolution = propose_upscale_action(snapshot, metrics, topology, topology_meta, slo_cfgs)

                if action:
                    upscale_actions.append(action)
                else:
                    resolution = resolution or {
                        "classification": "external_or_unmonitored_delay",
                        "path_classification": "",
                        "leaf_classification": "external_or_unmonitored_delay",
                        "target": service,
                        "path": [service],
                        "reason": snapshot.get("trigger_reason", "none"),
                        "blocked_by": blocked_by_from_snapshot({"fresh_enough": snapshot.get("fresh_enough", True), "active_enough": snapshot.get("active", False), "under_pressure": snapshot.get("pressure", False)}),
                    }
                    resolution_target = resolution.get("target", service)
                    resolution_replicas, _resolution_ready = read_replicas(resolution_target)
                    resolution_cfg = slo_cfgs.get(resolution_target, cfg)
                    emit_trace(
                        trigger_service=service,
                        final_target_service=resolution_target,
                        priority_type=snapshot["priority"],
                        classification=resolution.get("classification", "external_or_unmonitored_delay"),
                        path_reason=str(resolution.get("path_classification") or ""),
                        final_reason=str(resolution.get("leaf_classification") or resolution.get("classification", "external_or_unmonitored_delay")),
                        reason=str(resolution.get("reason", snapshot.get("trigger_reason", "none"))),
                        path=list(resolution.get("path", [service])),
                        decision="no_scale",
                        current_replicas=resolution_replicas,
                        target_replicas=resolution_replicas,
                        p90_ms=snapshot["p90_ms"],
                        slo_ms=snapshot["slo_ms"],
                        rps=snapshot["rps"],
                        runq_ms=snapshot["runq_ms"],
                        cpu_throttle_ratio_value=cpu_throttle_ratio(metrics, resolution_target),
                        runq_threshold_ms_value=snapshot["runq_threshold_ms"],
                        downstream_ms=dependency_latency_ms(metrics, resolution_target),
                        service_handling_ms=local_handling_latency_ms(metrics, resolution_target),
                        external_wait_ms_value=external_wait_ms(metrics, resolution_target),
                        under_pressure=bool(snapshot.get("pressure", False)),
                        breach_streak=2 if snapshot.get("pressure_confirmed", False) else int(snapshot.get("pressure_window_2", False)),
                        protective_root_used=False,
                        applied_scale_step=0,
                        trigger_breached=bool(snapshot.get("pressure", False)),
                        fresh_enough=bool(snapshot.get("fresh_enough", True)),
                        active_enough=bool(snapshot.get("active", False)),
                        local_fraction_value=local_fraction(metrics, resolution_target),
                        dependency_fraction_value=dependency_fraction(metrics, resolution_target),
                        runq_p90_ms=runq_p90_latency_ms(metrics, resolution_target),
                        upscale_eligible=False,
                        blocked_by=str(resolution.get("blocked_by", "none") or "none"),
                        runq_pct_value=runq_pct(metrics, resolution_target),
                        throttle_pct_value=throttle_pct(metrics, resolution_target),
                        local_resource_support_value=local_resource_support(metrics, resolution_target),
                        actual_replica_change=0,
                        pressure_window_1=bool(snapshot.get("pressure_window_1", False)),
                        pressure_window_2=bool(snapshot.get("pressure_window_2", False)),
                        pressure_confirmed=bool(snapshot.get("pressure_confirmed", False)),
                        scale_mode="none",
                        trigger_reason=str(snapshot.get("trigger_reason", "none")),
                        rps_per_replica=(snapshot["rps"] / max(read_replicas(resolution_target)[0], 1)),
                        error_rate_5xx_value=float(snapshot.get("error_rate_5xx", 0.0)),
                        timeout_rate_value=float(snapshot.get("timeout_rate", 0.0)),
                        min_replicas=max(1, safe_int(resolution_cfg.get("min", 1), 1)),
                        max_replicas=max(1, safe_int(resolution_cfg.get("max", 1), 1)),
                        scaleout_eligible=bool(snapshot.get("scaleout_eligible", False)),
                        scaleout_block_reason=str(snapshot.get("scaleout_block_reason", resolution.get("blocked_by", ""))),
                        target_admissible=False,
                        target_admissible_reason=str(resolution.get("blocked_by", "")),
                    )

                downscale = propose_downscale_action(service, metrics, cfg, snapshot=snapshot)
                if downscale:
                    downscale_actions.append(downscale)
                LAST_SHORT_RPS[service] = snapshot["rps"]

            chosen = pick_best_action(upscale_actions)

            if chosen:
                patch_replicas(chosen["target_service"], chosen["target_replicas"])
                set_cooldown(UPSCALE_COOLDOWNS, chosen["target_service"], int(max(1.0, UPSCALE_COOLDOWN_S)))
                RECENT_TRUE_TARGET_AT[chosen["target_service"]] = time.time()
                record_scale_event(
                    chosen["target_service"],
                    chosen["current_replicas"],
                    chosen["target_replicas"],
                    chosen["p90_ms"],
                    chosen["ratio"],
                )
                LAST_SCALE_EVENTS[chosen["target_service"]]["last_scale_error_rate"] = safe_float(chosen.get("error_rate_5xx", 0.0), 0.0)
                LAST_SCALE_EVENTS[chosen["target_service"]]["last_scale_timeout_rate"] = safe_float(chosen.get("timeout_rate", 0.0), 0.0)
                LAST_SCALE_EVENTS[chosen["target_service"]]["last_scale_runq_ms"] = safe_float(chosen.get("runq_ms", 0.0), 0.0)
                logger.info(
                    "SCALING %s up for trigger %s: %d -> %d (mode=%s ratio=%.3f path=%s)",
                    chosen["target_service"],
                    chosen["trigger_service"],
                    chosen["current_replicas"],
                    chosen["target_replicas"],
                    chosen["scale_mode"],
                    chosen["ratio"],
                    " -> ".join(chosen["path"]),
                )
                emit_trace(
                    trigger_service=chosen["trigger_service"],
                    final_target_service=chosen["target_service"],
                    priority_type=chosen["priority"],
                    classification=chosen["classification"],
                    path_reason=str(chosen.get("path_reason") or ""),
                    final_reason=str(chosen.get("final_reason") or chosen["classification"]),
                    reason=chosen["reason"],
                    path=chosen["path"],
                    decision="scale_up",
                    current_replicas=chosen["current_replicas"],
                    target_replicas=chosen["target_replicas"],
                    p90_ms=chosen["p90_ms"],
                    slo_ms=chosen["slo_ms"],
                    rps=chosen["rps"],
                    runq_ms=chosen["runq_ms"],
                    cpu_throttle_ratio_value=chosen.get("cpu_throttle_ratio", 0.0),
                    runq_threshold_ms_value=chosen["runq_threshold_ms"],
                    downstream_ms=chosen["downstream_ms"],
                    service_handling_ms=chosen.get("service_handling_ms", 0.0),
                    external_wait_ms_value=chosen.get("external_wait_ms", 0.0),
                    under_pressure=bool(chosen.get("under_pressure", False)),
                    breach_streak=int(chosen.get("breach_streak", 0)),
                    protective_root_used=bool(chosen.get("protective_root_used", False)),
                    applied_scale_step=chosen["step"],
                    trigger_breached=bool(chosen.get("trigger_breached", False)),
                    fresh_enough=bool(chosen.get("fresh_enough", False)),
                    active_enough=bool(chosen.get("active_enough", False)),
                    local_fraction_value=safe_float(chosen.get("local_fraction", 0.0), 0.0),
                    dependency_fraction_value=safe_float(chosen.get("dependency_fraction", 0.0), 0.0),
                    runq_p90_ms=safe_float(chosen.get("runq_p90_ms", 0.0), 0.0),
                    upscale_eligible=bool(chosen.get("upscale_eligible", True)),
                    blocked_by=str(chosen.get("blocked_by", "") or ""),
                    min_replicas=safe_int(chosen.get("min_replicas", 1), 1),
                    max_replicas=safe_int(chosen.get("max_replicas", chosen.get("target_replicas", 1)), chosen.get("target_replicas", 1)),
                    runq_pct_value=safe_float(chosen.get("runq_pct", 0.0), 0.0),
                    throttle_pct_value=safe_float(chosen.get("throttle_pct", 0.0), 0.0),
                    local_resource_support_value=str(chosen.get("local_resource_support", "")),
                    actual_replica_change=int(chosen["step"]),
                    pressure_window_1=bool(chosen.get("pressure_window_1", False)),
                    pressure_window_2=bool(chosen.get("pressure_window_2", False)),
                    pressure_confirmed=bool(chosen.get("pressure_confirmed", False)),
                    scale_mode=str(chosen.get("scale_mode", "none")),
                    trigger_reason=str(chosen.get("trigger_reason", chosen.get("reason", "pressure"))),
                    rps_per_replica=safe_float(chosen.get("rps_per_replica", 0.0), 0.0),
                    error_rate_5xx_value=safe_float(chosen.get("error_rate_5xx", 0.0), 0.0),
                    timeout_rate_value=safe_float(chosen.get("timeout_rate", 0.0), 0.0),
                    scaleout_eligible=bool(chosen.get("scaleout_eligible", True)),
                    scaleout_block_reason=str(chosen.get("scaleout_block_reason", "")),
                    target_admissible=bool(chosen.get("target_admissible", True)),
                    target_admissible_reason=str(chosen.get("target_admissible_reason", "")),
                )
            elif downscale_actions:
                chosen_downscale = sorted(downscale_actions, key=lambda row: (safe_float(row["rps"], 0.0), row["service"]))[0]
                patch_replicas(chosen_downscale["service"], chosen_downscale["target_replicas"])
                set_cooldown(DOWNSCALE_COOLDOWNS, chosen_downscale["service"], DOWNSCALE_COOLDOWN_S)
                logger.info(
                    "SCALING %s down: %d -> %d (rps=%.2f runq=%.3fms)",
                    chosen_downscale["service"],
                    chosen_downscale["current_replicas"],
                    chosen_downscale["target_replicas"],
                    chosen_downscale["rps"],
                    chosen_downscale["runq_ms"],
                )
                emit_trace(
                    trigger_service=chosen_downscale["service"],
                    final_target_service=chosen_downscale["service"],
                    priority_type=str(slo_cfgs[chosen_downscale["service"]].get("priority", "secondary")),
                    classification="local_bottleneck",
                    path_reason="",
                    final_reason="local_bottleneck",
                    reason=str(chosen_downscale.get("downscale_reason", "traffic_oversized_downscale")),
                    path=[chosen_downscale["service"]],
                    decision="scale_down",
                    current_replicas=chosen_downscale["current_replicas"],
                    target_replicas=chosen_downscale["target_replicas"],
                    p90_ms=preferred_truth_or_ebpf_p90(metrics, chosen_downscale["service"])[0],
                    slo_ms=safe_float(slo_cfgs[chosen_downscale["service"]].get("slo", 0.0), 0.0),
                    rps=chosen_downscale["rps"],
                    runq_ms=chosen_downscale["runq_ms"],
                    cpu_throttle_ratio_value=cpu_throttle_ratio(metrics, chosen_downscale["service"]),
                    runq_threshold_ms_value=runq_threshold_ms(metrics, chosen_downscale["service"]),
                    downstream_ms=dependency_latency_ms(metrics, chosen_downscale["service"]),
                    service_handling_ms=local_handling_latency_ms(metrics, chosen_downscale["service"]),
                    external_wait_ms_value=external_wait_ms(metrics, chosen_downscale["service"]),
                    under_pressure=False,
                    breach_streak=0,
                    protective_root_used=False,
                    applied_scale_step=chosen_downscale["step"],
                    runq_pct_value=safe_float(chosen_downscale.get("runq_pct", 0.0), 0.0),
                    throttle_pct_value=safe_float(chosen_downscale.get("throttle_pct", 0.0), 0.0),
                    local_resource_support_value=str(chosen_downscale.get("local_resource_support", "")),
                    actual_replica_change=-int(chosen_downscale["step"]),
                    pressure_window_1=False,
                    pressure_window_2=False,
                    pressure_confirmed=False,
                    scale_mode="none",
                    trigger_reason=str(chosen_downscale.get("trigger_reason", "low_demand")),
                    rps_per_replica=safe_float(chosen_downscale.get("rps_per_replica", 0.0), 0.0),
                    error_rate_5xx_value=safe_float(chosen_downscale.get("error_rate_5xx", 0.0), 0.0),
                    timeout_rate_value=safe_float(chosen_downscale.get("timeout_rate", 0.0), 0.0),
                    min_replicas=max(1, safe_int(slo_cfgs[chosen_downscale["service"]].get("min", 1), 1)),
                    max_replicas=max(1, safe_int(slo_cfgs[chosen_downscale["service"]].get("max", 1), 1)),
                    scaleout_eligible=False,
                    scaleout_block_reason="downscale_path",
                    target_admissible=False,
                    target_admissible_reason="downscale_path",
                )

            READY_STATE["ready"] = True
            READY_STATE["last_error"] = ""
        except Exception as exc:
            READY_STATE["ready"] = False
            READY_STATE["last_error"] = str(exc)
            logger.exception("Controller loop failed: %s", exc)
        finally:
            READY_STATE["last_loop_ts"] = time.time()

        elapsed = time.time() - loop_start
        time.sleep(max(0.2, LOOP_SECONDS - elapsed))


if __name__ == "__main__":
    main()
