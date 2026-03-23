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
LOOP_SECONDS = float(os.getenv("LOOP_SECONDS", "10"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8081"))

SHORT_TRAFFIC_WINDOW_SECONDS = float(os.getenv("SHORT_TRAFFIC_WINDOW_SECONDS", "10"))
LONG_TRAFFIC_WINDOW_SECONDS = float(os.getenv("LONG_TRAFFIC_WINDOW_SECONDS", "30"))
DOWNSCALE_DECISION_WINDOW_SECONDS = float(os.getenv("DOWNSCALE_DECISION_WINDOW_SECONDS", "60"))

TRACE_LOGS = os.getenv("TRACE_LOGS", "true").lower() == "true"
TRACE_REDIS_KEY = os.getenv("TRACE_REDIS_KEY", "decision_traces")
TRACE_REDIS_MAX = int(os.getenv("TRACE_REDIS_MAX", "500"))

ACTIVE_RPS_THRESHOLD = float(os.getenv("ACTIVE_RPS_THRESHOLD", "0.5"))
DOWNSCALE_RPS_THRESHOLD = float(os.getenv("DOWNSCALE_RPS_THRESHOLD", "0.5"))
DOWNSCALE_RPS_PER_REPLICA_THRESHOLD = float(os.getenv("DOWNSCALE_RPS_PER_REPLICA_THRESHOLD", "1.0"))
TARGET_RPS_PER_REPLICA = float(os.getenv("TARGET_RPS_PER_REPLICA", "20.0"))
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
RECENT_BREACH_HOLD_S = float(os.getenv("RECENT_BREACH_HOLD_S", str(max(4.0, LOOP_SECONDS * 2.0))))
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
PRIMARY_TARGET_STICKY_SECONDS = float(os.getenv("PRIMARY_TARGET_STICKY_SECONDS", "20"))

READY_STATE = {"ready": False, "last_error": "", "last_loop_ts": 0.0}
DOWNSCALE_COOLDOWNS: Dict[str, float] = {}
BREACH_STREAKS: Dict[str, int] = {}
LAST_BREACH_AT: Dict[str, float] = {}
LOW_DEMAND_STREAKS: Dict[str, int] = {}
RUNQ_PRESSURE_STREAKS: Dict[str, int] = {}
PRIMARY_PROTECTIVE_STREAKS: Dict[str, int] = {}
LAST_SHORT_RPS: Dict[str, float] = {}
PRIMARY_LAST_LOCAL_TARGET: Dict[str, str] = {}
PRIMARY_LAST_LOCAL_TARGET_AT: Dict[str, float] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SimpleController")

try:
    config.load_incluster_config()
except Exception:
    config.load_kube_config()

app_api = client.AppsV1Api()
custom_api = client.CustomObjectsApi()


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
    _unused = (metrics, svc)
    return 0.0


def error_rate_5xx(metrics, svc: str) -> float:
    _unused = (metrics, svc)
    return 0.0


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
    ratio = float(trigger.get("ratio", 0.0) or 0.0)
    streak = int(trigger.get("streak", 0) or 0)
    breached = bool(trigger.get("breached", False))
    under_pressure = bool(trigger.get("under_pressure", False))
    near_breach = under_pressure and not breached

    delta = short_rps - long_rps
    rise_ratio = short_rps / max(long_rps, 0.001) if short_rps > 0 else 0.0
    fall_ratio = short_rps / max(long_rps, 0.001) if long_rps > 0 else 1.0
    recent_sustained = max(long_rps, short_rps, float(current_replicas) * target_rps_per_replica(target))
    both_low = short_rps <= recent_sustained * 0.45 and long_rps <= recent_sustained * 0.45 and not under_pressure
    easing = short_p90 <= max(long_p90 * 0.95, trigger.get("slo_ms", 0.0) * 1.05)
    current_replicas = max(1, int(current_replicas))

    if both_low:
        pattern = "low_demand"
    elif (
        long_rps > 0
        and fall_ratio <= 0.8
        and (not breached or ratio < 1.0)
        and easing
    ):
        pattern = "recovery"
    elif under_pressure and short_rps >= max(SECONDARY_UPSCALE_MIN_RPS, 1.0) and long_rps >= max(SECONDARY_UPSCALE_MIN_RPS, 1.0) and 0.9 <= fall_ratio <= 1.15:
        pattern = "stable_high"
    elif under_pressure and rise_ratio >= 1.1 and streak >= 2:
        pattern = "sustained_increase"
    elif (breached or near_breach) and rise_ratio >= 1.25 and streak <= 2:
        pattern = "spike"
    elif under_pressure:
        pattern = "stable_high" if streak >= 3 else "sustained_increase"
    elif short_rps < long_rps and easing:
        pattern = "recovery"
    else:
        pattern = "low_demand" if both_low else "spike"

    severe = ratio >= PRIMARY_SEVERE_BREACH_RATIO
    moderate = ratio >= 1.0 or under_pressure
    _unused = (final_reason, current_replicas)
    return {
        "pattern": pattern,
        "short_rps": round(short_rps, 3),
        "long_rps": round(long_rps, 3),
        "rps_growth": round(delta, 3),
        "short_p90_ms": round(short_p90, 3),
        "long_p90_ms": round(long_p90, 3),
        "severe": bool(severe),
        "moderate": bool(moderate),
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
    target_per_replica = target_rps_per_replica(target)
    long_rps = safe_float(traffic.get("long_rps", 0.0), 0.0)
    short_rps = safe_float(traffic.get("short_rps", 0.0), 0.0)
    short_p90 = safe_float(traffic.get("short_p90_ms", trigger.get("p90_ms", 0.0)), 0.0)
    slo_ms = max(1.0, safe_float(trigger.get("slo_ms", 0.0), 0.0))
    breach_ratio_value = short_p90 / slo_ms if slo_ms > 0 else 0.0
    target_from_traffic = max(min_replicas, int(math.ceil(long_rps / max(target_per_replica, 0.1)))) if long_rps > 0 else min_replicas
    target_from_slo = max(min_replicas, int(math.ceil(current_replicas * max(1.0, breach_ratio_value))))
    pattern = str(traffic.get("pattern", "") or "")
    long_base = max(long_rps, 1.0)
    if pattern == "spike":
        pattern_multiplier = max(1.0, short_rps / long_base)
    elif pattern == "sustained_increase":
        pattern_multiplier = max(1.0, (short_rps / long_base) * 1.15)
    elif pattern == "stable_high":
        pattern_multiplier = max(1.0, breach_ratio_value)
    else:
        pattern_multiplier = 1.0
    support_multiplier = resource_support_multiplier(metrics, target)
    desired = int(
        math.ceil(
            max(target_from_traffic, target_from_slo)
            * pattern_multiplier
            * support_multiplier
        )
    )
    desired = max(min_replicas, min(max_replicas, desired))
    return {
        "target_from_traffic": int(target_from_traffic),
        "target_from_slo": int(target_from_slo),
        "trigger_slo": round(slo_ms, 3),
        "short_p90_ms": round(short_p90, 3),
        "breach_ratio": round(breach_ratio_value, 3),
        "pattern_multiplier": round(pattern_multiplier, 3),
        "resource_support_multiplier": round(support_multiplier, 3),
        "target_rps_per_replica": round(target_per_replica, 3),
        "desired_replicas": int(desired),
    }


def strongest_internal_child(
    current: str,
    metrics: dict,
    topology: dict,
    topology_meta: dict,
    monitored_services: set,
    slo_cfgs: dict,
) -> Optional[str]:
    matched = best_matching_monitored_child(current, metrics, topology, monitored_services)
    _unused = (topology_meta, slo_cfgs)
    return matched


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
    current_total_ms = max(total_latency_ms(metrics, current), 0.001)
    child_local_ms = local_handling_latency_ms(metrics, child)
    child_total_ms = total_latency_ms(metrics, child)
    if local_handling_dominant(metrics, child) and child_local_ms >= max(current_total_ms * 0.35, 1.0):
        return True
    if child_total_ms >= max(current_total_ms * 0.45, 1.0):
        return True
    return False


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
        m = metric_obj(metrics, current)
        topo_fresh = bool(m.get("topology_fresh", False))
        dependency_dominant = dependency_dominant_for_service(metrics, current)
        external_dominant = external_dominant_for_service(metrics, current)
        child = strongest_internal_child(current, metrics, topology, topology_meta, monitored_services, slo_cfgs)
        unready_child = first_unready_monitored_child(current, topology, monitored_services)

        if unready_child and (dependency_dominant or dependency_or_unexplained_delay_high(metrics, current)):
            return {
                "classification": "downstream_delay",
                "path_classification": "downstream_delay",
                "leaf_classification": "downstream_delay",
                "target": unready_child,
                "path": path + ([unready_child] if unready_child not in path else []),
                "reason": "downstream_child_unready",
            }

        if child and (
            dependency_dominant
            or (
                dependency_or_unexplained_delay_high(metrics, current)
                and child_supports_internal_traversal(current, child, metrics)
            )
            or (
                external_dominant
                and child_supports_internal_traversal(current, child, metrics)
            )
        ):
            if not topo_fresh:
                return {
                    "classification": path_reason or "local_bottleneck",
                    "path_classification": path_reason,
                    "leaf_classification": "local_bottleneck",
                    "target": current,
                    "path": path,
                    "reason": "topology_stale",
                }
            if child in seen:
                return {
                    "classification": path_reason or "local_bottleneck",
                    "path_classification": path_reason,
                    "leaf_classification": "local_bottleneck",
                    "target": current,
                    "path": path,
                    "reason": "loop_detected",
                }
            path_reason = "downstream_delay"
            seen.add(child)
            path.append(child)
            current = child
            continue

        if (
            local_fraction(metrics, current) >= LOCAL_FRACTION_MIN
            and local_handling_dominant(metrics, current)
            and not dependency_dominant
            and not external_dominant
        ):
            leaf_classification = "local_bottleneck"
            return {
                "classification": path_reason or leaf_classification,
                "path_classification": path_reason,
                "leaf_classification": leaf_classification,
                "target": current,
                "path": path,
                "reason": "local_bottleneck",
            }

        if local_handling_dominant(metrics, current) and not child:
            leaf_classification = "local_bottleneck"
            return {
                "classification": path_reason or leaf_classification,
                "path_classification": path_reason,
                "leaf_classification": leaf_classification,
                "target": current,
                "path": path,
                "reason": leaf_classification,
            }

        if dependency_or_unexplained_delay_high(metrics, current):
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
    near_breach_ratio = NEAR_BREACH_RATIO_PRIMARY if priority == "primary" else NEAR_BREACH_RATIO_SECONDARY
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
    }


def can_scale_now(service: str, cfg: dict) -> Tuple[bool, str, int, int]:
    current_replicas, ready_replicas = read_replicas(service)
    max_replicas = max(1, safe_int(cfg.get("max", 1), 1))
    if current_replicas >= max_replicas:
        return False, "at_max", current_replicas, ready_replicas
    warm = current_replicas > max(ready_replicas, 0) and (
        current_replicas - ready_replicas
    ) >= max(1, math.ceil(current_replicas * WARMUP_READY_GAP_RATIO))
    if warm:
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
    priority = trigger["priority"]
    resolution = resolve_root_cause(service, metrics, topology, topology_meta, set(slo_cfgs.keys()), slo_cfgs)

    required_streak = PRIMARY_BREACH_STREAK_REQUIRED if priority == "primary" else BREACH_STREAK_REQUIRED
    if trigger["streak"] < required_streak:
        resolution["classification"] = "unclear"
        resolution["reason"] = "breach_streak_too_short"
        resolution["blocked_by"] = "streak_too_short"
        resolution["upscale_eligible"] = False
        return None, resolution

    path_reason = resolution.get("path_classification")
    final_reason = resolution.get("leaf_classification", resolution.get("classification"))
    target = resolution["target"]

    if resolution.get("reason") == "downstream_child_unready":
        resolution["blocked_by"] = "child_warmup"
        resolution["upscale_eligible"] = False
        return None, resolution

    if priority == "primary" and final_reason == "local_bottleneck":
        remember_primary_local_target(service, target)

    current_rps = preferred_rps(metrics, service)
    previous_rps = LAST_SHORT_RPS.get(service, current_rps)
    protective_candidate = (
        priority == "primary"
        and PRIMARY_PROTECTIVE_FRONTEND_SCALE
        and service == ROOT_SERVICE
        and trigger["under_pressure"]
        and (current_rps - previous_rps) >= PRIMARY_PROTECTIVE_MIN_RPS_DELTA
        and target == service
        and not path_reason
    )
    protective_streak = record_primary_protective_streak(service, protective_candidate)

    if final_reason == "external_or_unmonitored_delay":
        traffic = classify_traffic_pattern(trigger, metrics, target, final_reason, read_replicas(target)[0])
        desired = compute_dynamic_upscale_target(metrics, target, trigger, slo_cfgs.get(target, {}), traffic, read_replicas(target)[0]) if target in slo_cfgs else {}
        return None, no_scale_resolution(
            final_reason,
            path_reason,
            target,
            resolution.get("path", [service]),
            "external_or_unmonitored_delay",
            "external_or_unmonitored_delay",
            traffic=traffic,
            trigger=trigger,
            desired=desired,
        )
    if final_reason != "local_bottleneck":
        traffic = classify_traffic_pattern(trigger, metrics, target, final_reason, read_replicas(target)[0])
        desired = compute_dynamic_upscale_target(metrics, target, trigger, slo_cfgs.get(target, {}), traffic, read_replicas(target)[0]) if target in slo_cfgs else {}
        return None, no_scale_resolution(
            final_reason,
            path_reason,
            target,
            resolution.get("path", [service]),
            "reason_not_scalable",
            "reason_not_scalable",
            traffic=traffic,
            trigger=trigger,
            desired=desired,
        )

    target_cfg = slo_cfgs.get(target)
    if not target_cfg:
        return None, {"classification": "external_or_unmonitored_delay", "path_classification": path_reason, "leaf_classification": final_reason, "target": target, "path": resolution.get("path", [service]), "reason": "target_not_monitored", "blocked_by": "unknown", "upscale_eligible": False}

    allowed, gate_reason, current_replicas, _ready_replicas = can_scale_now(target, target_cfg)
    traffic = classify_traffic_pattern(trigger, metrics, target, final_reason, current_replicas)
    desired = compute_dynamic_upscale_target(metrics, target, trigger, target_cfg, traffic, current_replicas)
    support = local_resource_support(metrics, target)
    if not allowed:
        hold_block = "at_max" if gate_reason == "at_max" else ("warmup" if gate_reason == "warmup_active" else "unknown")
        no_scale = no_scale_resolution(
            final_reason,
            path_reason,
            target,
            resolution.get("path", [service]),
            gate_reason,
            hold_block,
            traffic=traffic,
            trigger=trigger,
            desired=desired,
        )
        no_scale["local_resource_support"] = support
        return None, no_scale

    if priority == "secondary":
        target_rps = preferred_rps(metrics, target)
        demand_per_replica = target_rps / max(current_replicas, 1)
        if not metric_fresh_enough_for_control(metrics, target):
            no_scale = no_scale_resolution(final_reason, path_reason, target, resolution.get("path", [service]), "secondary_metrics_not_fresh", "not_fresh", traffic=traffic, trigger=trigger, desired=desired)
            no_scale["local_resource_support"] = support
            return None, no_scale
        if (
            target_rps < SECONDARY_UPSCALE_MIN_RPS
            or demand_per_replica < SECONDARY_UPSCALE_MIN_RPS_PER_REPLICA
        ):
            no_scale = no_scale_resolution(final_reason, path_reason, target, resolution.get("path", [service]), "secondary_low_demand", "demand_gate", traffic=traffic, trigger=trigger, desired=desired)
            no_scale["local_resource_support"] = support
            return None, no_scale
        if not trigger.get("breached", False) or not trigger.get("active", False):
            no_scale = no_scale_resolution(final_reason, path_reason, target, resolution.get("path", [service]), "secondary_hold", traffic_pattern_blocked_by(traffic["pattern"]), traffic=traffic, trigger=trigger, desired=desired)
            no_scale["local_resource_support"] = support
            return None, no_scale
        desired_target = max(current_replicas + 1, desired["desired_replicas"])
    else:
        pattern = traffic["pattern"]
        if pattern == "spike":
            if protective_candidate and protective_streak >= PRIMARY_PROTECTIVE_STREAK_REQUIRED:
                desired_target = max(current_replicas + 1, min(current_replicas + 1, desired["desired_replicas"]))
                resolution["reason"] = "protective_primary_fallback"
            else:
                no_scale = no_scale_resolution(final_reason, path_reason, target, resolution.get("path", [service]), "spike_hold", traffic_pattern_blocked_by(pattern), traffic=traffic, trigger=trigger, desired=desired)
                no_scale["local_resource_support"] = support
                return None, no_scale
        elif pattern in {"sustained_increase", "stable_high"}:
            desired_target = desired["desired_replicas"]
        elif pattern in {"recovery", "low_demand"}:
            no_scale = no_scale_resolution(final_reason, path_reason, target, resolution.get("path", [service]), f"{pattern}_hold", traffic_pattern_blocked_by(pattern), traffic=traffic, trigger=trigger, desired=desired)
            no_scale["local_resource_support"] = support
            return None, no_scale
        else:
            desired_target = max(current_replicas + 1, desired["desired_replicas"])

    target_replicas = min(max(1, safe_int(target_cfg.get("max", 1), 1)), desired_target)
    if target_replicas <= current_replicas:
        no_scale = no_scale_resolution(final_reason, path_reason, target, resolution.get("path", [service]), "gated", "target_not_above_current", traffic=traffic, trigger=trigger, desired=desired)
        no_scale["local_resource_support"] = support
        return None, no_scale

    action = {
        "trigger_service": service,
        "target_service": target,
        "priority": priority,
        "classification": final_reason,
        "path_reason": path_reason,
        "final_reason": final_reason,
        "reason": resolution.get("reason", final_reason),
        "path": list(resolution.get("path", [service])),
        "step": int(target_replicas - current_replicas),
        "current_replicas": current_replicas,
        "target_replicas": target_replicas,
        "breach_streak": int(trigger["streak"]),
        "ratio": float(trigger["ratio"]),
        "p90_ms": float(trigger["p90_ms"]),
        "slo_ms": float(trigger["slo_ms"]),
        "rps": float(trigger["rps"]),
        "runq_ms": runq_latency_ms(metrics, target),
        "cpu_throttle_ratio": cpu_throttle_ratio(metrics, target),
        "runq_threshold_ms": runq_threshold_ms(metrics, target),
        "downstream_ms": dependency_latency_ms(metrics, service),
        "service_handling_ms": local_handling_latency_ms(metrics, target),
        "external_wait_ms": external_wait_ms(metrics, target),
        "under_pressure": bool(trigger.get("under_pressure", False)),
        "protective_root_used": bool(resolution.get("reason") == "protective_primary_fallback"),
        "upscale_eligible": True,
        "blocked_by": "",
        "fresh_enough": bool(trigger.get("fresh_enough", False)),
        "active_enough": bool(trigger.get("active_enough", False)),
        "trigger_breached": bool(trigger.get("breached", False)),
        "local_fraction": local_fraction(metrics, target),
        "dependency_fraction": dependency_fraction(metrics, target),
        "runq_p90_ms": runq_p90_latency_ms(metrics, target),
        "traffic_pattern": traffic["pattern"],
        "short_rps": traffic["short_rps"],
        "long_rps": traffic["long_rps"],
        "rps_growth": traffic["rps_growth"],
        "short_p90_ms": desired["short_p90_ms"],
        "trigger_slo": desired["trigger_slo"],
        "target_from_traffic": desired["target_from_traffic"],
        "target_from_slo": desired["target_from_slo"],
        "desired_replicas_from_traffic": desired["target_from_traffic"],
        "target_rps_per_replica": desired["target_rps_per_replica"],
        "runq_pct": runq_pct(metrics, target),
        "throttle_pct": throttle_pct(metrics, target),
        "local_resource_support": support,
    }
    return action, resolution


def pick_best_action(actions: List[dict]) -> Optional[dict]:
    if not actions:
        return None

    def action_key(action: dict):
        return (
            -safe_int(action.get("breach_streak", 0), 0),
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
    short_p90_ms: float = 0.0,
    trigger_slo: float = 0.0,
    rps_growth: float = 0.0,
    runq_pct_value: float = 0.0,
    throttle_pct_value: float = 0.0,
    local_resource_support_value: str = "",
    downscale_step: int = 0,
    downscale_reason: str = "",
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
        "root": trigger_service,
        "trigger_service": trigger_service,
        "node": final_target_service,
        "service": final_target_service,
        "action": action,
        "decision_action": decision,
        "decision": decision,
        "reason": reason,
        "priority_type": priority_type,
        "mode": priority_type,
        "root_cause_classification": classification,
        "bottleneck_kind": classification,
        "path_reason": path_reason,
        "final_reason": final_reason,
        "bottleneck_path": list(path),
        "traversal_path": list(path),
        "final_bottleneck_service": final_target_service,
        "final_bottleneck_type": final_reason,
        "current_replicas": int(current_replicas),
        "target_replicas": int(target_replicas),
        "proposed_step": int(applied_scale_step),
        "applied_scale_step": int(applied_scale_step),
        "actual_replica_change": int(actual_replica_change),
        "protective_root_used": bool(protective_root_used),
        "slo_ms": round(float(slo_ms), 3),
        "p90_ms": round(float(p90_ms), 3),
        "rps": round(float(rps), 3),
        "short_rps": round(float(short_rps), 3),
        "long_rps": round(float(long_rps), 3),
        "rps_growth": round(float(rps_growth), 3),
        "short_p90": round(float(short_p90_ms), 3),
        "trigger_slo": round(float(trigger_slo), 3),
        "traffic_pattern": str(traffic_pattern or ""),
        "target_rps_per_replica": round(float(target_rps_per_replica_value), 3),
        "desired_replicas_from_traffic": int(desired_replicas_from_traffic),
        "target_from_traffic": int(target_from_traffic),
        "target_from_slo": int(target_from_slo),
        "runq_pct": round(float(runq_pct_value), 3),
        "throttle_pct": round(float(throttle_pct_value), 3),
        "local_resource_support": str(local_resource_support_value or ""),
        "downscale_step": int(downscale_step),
        "downscale_reason": str(downscale_reason or ""),
        "under_pressure": bool(under_pressure),
        "trigger_breached": bool(trigger_breached),
        "fresh_enough": bool(fresh_enough),
        "active_enough": bool(active_enough),
        "breach_streak": int(breach_streak),
        "runq_latency_ms": round(float(runq_ms), 3),
        "runq_p90_ms": round(float(runq_p90_ms), 3),
        "cpu_throttle_ratio": round(float(cpu_throttle_ratio_value), 4),
        "runq_threshold_ms": round(float(runq_threshold_ms_value), 3),
        "downstream_latency_ms": round(float(downstream_ms), 3),
        "service_handling_ms": round(float(service_handling_ms), 3),
        "external_wait_ms": round(float(external_wait_ms_value), 3),
        "local_fraction": round(float(local_fraction_value), 4),
        "dependency_fraction": round(float(dependency_fraction_value), 4),
        "upscale_eligible": bool(upscale_eligible),
        "blocked_by": str(blocked_by or ""),
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
        record_low_demand_streak(service, False)
        return None

    if snapshot and (bool(snapshot.get("under_pressure", False)) or safe_int(snapshot.get("streak", 0), 0) > 0):
        record_low_demand_streak(service, False)
        return None
    if recent_breach_hold_active(service):
        record_low_demand_streak(service, False)
        return None
    warm, _desired, _ready = warmup_active(service)
    if warm:
        record_low_demand_streak(service, False)
        return None
    if not metric_fresh_enough_for_control(metrics, service):
        record_low_demand_streak(service, False)
        return None
    if not demand_fresh_enough_for_downscale(metrics, service):
        record_low_demand_streak(service, False)
        return None

    short_rps = preferred_rps(metrics, service)
    long_rps = preferred_long_rps(metrics, service)
    target_per_replica = target_rps_per_replica(service)
    desired_replicas_from_traffic = max(min_replicas, int(math.ceil(long_rps / max(target_per_replica, 0.1)))) if long_rps > 0 else min_replicas
    runq_ms = runq_latency_ms(metrics, service)
    runq_low = runq_ms <= runq_low_threshold_ms(metrics, service)
    oversized_gap = max(0, current_replicas - desired_replicas_from_traffic)
    short_confirms_no_bounce = short_rps <= max(long_rps * 1.05, target_per_replica * desired_replicas_from_traffic)
    no_breach = not bool(snapshot and snapshot.get("under_pressure", False))
    relative_downscale_ready = bool(
        no_breach
        and runq_low
        and desired_replicas_from_traffic < current_replicas
        and short_confirms_no_bounce
    )
    sustained_low_demand = bool(relative_downscale_ready)
    downscale_streak = record_low_demand_streak(service, sustained_low_demand)
    if not sustained_low_demand:
        return None
    if cooldown_active(DOWNSCALE_COOLDOWNS, service):
        return None
    if downscale_streak < LOW_DEMAND_STREAK_REQUIRED:
        return None

    step = max(1, int(math.ceil(oversized_gap * 0.5)))
    target = max(min_replicas, current_replicas - step)
    target = max(desired_replicas_from_traffic, target)
    if target >= current_replicas:
        return None

    return {
        "service": service,
        "current_replicas": current_replicas,
        "target_replicas": target,
        "step": current_replicas - target,
        "rps": short_rps,
        "short_rps": short_rps,
        "long_rps": long_rps,
        "target_rps_per_replica": target_per_replica,
        "desired_replicas_from_traffic": desired_replicas_from_traffic,
        "runq_ms": runq_ms,
        "reason": "traffic_oversized_downscale",
        "downscale_reason": "traffic_oversized_downscale",
        "downscale_step": step,
        "traffic_pattern": "low_demand",
        "rps_growth": short_rps - long_rps,
        "short_p90_ms": safe_float(snapshot.get("p90_ms", 0.0), 0.0) if snapshot else 0.0,
        "trigger_slo": safe_float(snapshot.get("slo_ms", 0.0), 0.0) if snapshot else 0.0,
        "target_from_traffic": desired_replicas_from_traffic,
        "target_from_slo": current_replicas,
        "runq_pct": runq_pct(metrics, service),
        "throttle_pct": throttle_pct(metrics, service),
        "local_resource_support": local_resource_support(metrics, service),
        "low_demand_streak": downscale_streak,
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

            primary_actions: List[dict] = []
            secondary_actions: List[dict] = []
            downscale_actions: List[dict] = []

            ordered_services = sorted(slo_cfgs.keys())
            if ROOT_SERVICE in ordered_services:
                ordered_services.remove(ROOT_SERVICE)
                ordered_services.insert(0, ROOT_SERVICE)

            for service in ordered_services:
                record_runq_pressure_streak(service, runq_pressure_candidate(metrics, service))

            for service in ordered_services:
                cfg = slo_cfgs[service]
                snapshot = breach_snapshot(service, metrics, cfg)
                action, resolution = propose_upscale_action(snapshot, metrics, topology, topology_meta, slo_cfgs) if snapshot["under_pressure"] else (None, None)

                if action:
                    if snapshot["priority"] == "primary":
                        primary_actions.append(action)
                    else:
                        secondary_actions.append(action)
                elif snapshot["under_pressure"]:
                    resolution_target = resolution.get("target", service)
                    resolution_replicas, _resolution_ready = read_replicas(resolution_target)
                    trace_short_rps = safe_float(resolution.get("short_rps", preferred_rps(metrics, resolution_target)), 0.0)
                    trace_long_rps = safe_float(resolution.get("long_rps", preferred_long_rps(metrics, resolution_target)), 0.0)
                    emit_trace(
                        trigger_service=service,
                        final_target_service=resolution_target,
                        priority_type=snapshot["priority"],
                        classification=resolution.get("classification", "unclear"),
                        path_reason=str(resolution.get("path_classification") or ""),
                        final_reason=str(resolution.get("leaf_classification") or resolution.get("classification", "unclear")),
                        reason=resolution.get("reason", "hold"),
                        path=list(resolution.get("path", [service])),
                        decision="no_scale",
                        current_replicas=resolution_replicas,
                        target_replicas=resolution_replicas,
                        p90_ms=snapshot["p90_ms"],
                        slo_ms=snapshot["slo_ms"],
                        rps=snapshot["rps"],
                        runq_ms=runq_latency_ms(metrics, resolution_target),
                        cpu_throttle_ratio_value=cpu_throttle_ratio(metrics, resolution_target),
                        runq_threshold_ms_value=runq_threshold_ms(metrics, resolution_target),
                        downstream_ms=dependency_latency_ms(metrics, service),
                        service_handling_ms=local_handling_latency_ms(metrics, resolution_target),
                        external_wait_ms_value=external_wait_ms(metrics, resolution_target),
                        under_pressure=bool(snapshot.get("under_pressure", False)),
                        breach_streak=int(snapshot.get("streak", 0)),
                        protective_root_used=bool(resolution.get("protective_root_used", False)),
                        applied_scale_step=0,
                        trigger_breached=bool(snapshot.get("breached", False)),
                        fresh_enough=bool(snapshot.get("fresh_enough", False)),
                        active_enough=bool(snapshot.get("active_enough", False)),
                        local_fraction_value=local_fraction(metrics, resolution_target),
                        dependency_fraction_value=dependency_fraction(metrics, resolution_target),
                        runq_p90_ms=runq_p90_latency_ms(metrics, resolution_target),
                        upscale_eligible=bool(resolution.get("upscale_eligible", False)),
                        blocked_by=str(resolution.get("blocked_by", "unknown") or "unknown"),
                        traffic_pattern=str(resolution.get("traffic_pattern", "")),
                        short_rps=trace_short_rps,
                        long_rps=trace_long_rps,
                        rps_growth=safe_float(resolution.get("rps_growth", trace_short_rps - trace_long_rps), 0.0),
                        short_p90_ms=safe_float(resolution.get("short_p90_ms", snapshot.get("p90_ms", 0.0)), 0.0),
                        trigger_slo=safe_float(resolution.get("trigger_slo", snapshot.get("slo_ms", 0.0)), 0.0),
                        target_from_traffic=safe_int(resolution.get("target_from_traffic", 0), 0),
                        target_from_slo=safe_int(resolution.get("target_from_slo", 0), 0),
                        desired_replicas_from_traffic=safe_int(resolution.get("desired_replicas_from_traffic", 0), 0),
                        target_rps_per_replica_value=safe_float(resolution.get("target_rps_per_replica", 0.0), 0.0),
                        runq_pct_value=runq_pct(metrics, resolution_target),
                        throttle_pct_value=throttle_pct(metrics, resolution_target),
                        local_resource_support_value=str(resolution.get("local_resource_support", local_resource_support(metrics, resolution_target))),
                        actual_replica_change=0,
                    )
                elif snapshot["priority"] == "primary":
                    short_rps = preferred_rps(metrics, service)
                    long_rps = preferred_long_rps(metrics, service)
                    emit_trace(
                        trigger_service=service,
                        final_target_service=service,
                        priority_type=snapshot["priority"],
                        classification="unclear",
                        path_reason="",
                        final_reason="unclear",
                        reason=blocked_by_from_snapshot(snapshot),
                        path=[service],
                        decision="no_scale",
                        current_replicas=read_replicas(service)[0],
                        target_replicas=read_replicas(service)[0],
                        p90_ms=snapshot["p90_ms"],
                        slo_ms=snapshot["slo_ms"],
                        rps=snapshot["rps"],
                        runq_ms=runq_latency_ms(metrics, service),
                        cpu_throttle_ratio_value=cpu_throttle_ratio(metrics, service),
                        runq_threshold_ms_value=runq_threshold_ms(metrics, service),
                        downstream_ms=dependency_latency_ms(metrics, service),
                        service_handling_ms=local_handling_latency_ms(metrics, service),
                        external_wait_ms_value=external_wait_ms(metrics, service),
                        under_pressure=bool(snapshot.get("under_pressure", False)),
                        breach_streak=int(snapshot.get("streak", 0)),
                        protective_root_used=False,
                        applied_scale_step=0,
                        trigger_breached=bool(snapshot.get("breached", False)),
                        fresh_enough=bool(snapshot.get("fresh_enough", False)),
                        active_enough=bool(snapshot.get("active_enough", False)),
                        local_fraction_value=local_fraction(metrics, service),
                        dependency_fraction_value=dependency_fraction(metrics, service),
                        runq_p90_ms=runq_p90_latency_ms(metrics, service),
                        upscale_eligible=False,
                        blocked_by=blocked_by_from_snapshot(snapshot),
                        traffic_pattern="low_demand" if (short_rps < DOWNSCALE_RPS_THRESHOLD and long_rps < DOWNSCALE_RPS_THRESHOLD) else "",
                        short_rps=short_rps,
                        long_rps=long_rps,
                        rps_growth=short_rps - long_rps,
                        short_p90_ms=snapshot["p90_ms"],
                        trigger_slo=snapshot["slo_ms"],
                        runq_pct_value=runq_pct(metrics, service),
                        throttle_pct_value=throttle_pct(metrics, service),
                        local_resource_support_value=local_resource_support(metrics, service),
                        actual_replica_change=0,
                    )

                downscale = propose_downscale_action(service, metrics, cfg, snapshot=snapshot)
                if downscale:
                    downscale_actions.append(downscale)
                LAST_SHORT_RPS[service] = preferred_rps(metrics, service)

            chosen = pick_best_action(primary_actions)
            if not chosen:
                chosen = pick_best_action(secondary_actions)

            if chosen:
                patch_replicas(chosen["target_service"], chosen["target_replicas"])
                logger.info(
                    "SCALING %s up for trigger %s: %d -> %d (priority=%s ratio=%.3f streak=%d pattern=%s path=%s)",
                    chosen["target_service"],
                    chosen["trigger_service"],
                    chosen["current_replicas"],
                    chosen["target_replicas"],
                    chosen["priority"],
                    chosen["ratio"],
                    chosen["breach_streak"],
                    chosen.get("traffic_pattern", ""),
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
                    traffic_pattern=str(chosen.get("traffic_pattern", "")),
                    short_rps=safe_float(chosen.get("short_rps", chosen.get("rps", 0.0)), 0.0),
                    long_rps=safe_float(chosen.get("long_rps", 0.0), 0.0),
                    rps_growth=safe_float(chosen.get("rps_growth", 0.0), 0.0),
                    short_p90_ms=safe_float(chosen.get("short_p90_ms", chosen.get("p90_ms", 0.0)), 0.0),
                    trigger_slo=safe_float(chosen.get("trigger_slo", chosen.get("slo_ms", 0.0)), 0.0),
                    target_from_traffic=safe_int(chosen.get("target_from_traffic", 0), 0),
                    target_from_slo=safe_int(chosen.get("target_from_slo", 0), 0),
                    desired_replicas_from_traffic=safe_int(chosen.get("desired_replicas_from_traffic", 0), 0),
                    target_rps_per_replica_value=safe_float(chosen.get("target_rps_per_replica", 0.0), 0.0),
                    runq_pct_value=safe_float(chosen.get("runq_pct", 0.0), 0.0),
                    throttle_pct_value=safe_float(chosen.get("throttle_pct", 0.0), 0.0),
                    local_resource_support_value=str(chosen.get("local_resource_support", "")),
                    actual_replica_change=int(chosen["step"]),
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
                    traffic_pattern=str(chosen_downscale.get("traffic_pattern", "low_demand")),
                    short_rps=safe_float(chosen_downscale.get("short_rps", chosen_downscale.get("rps", 0.0)), 0.0),
                    long_rps=safe_float(chosen_downscale.get("long_rps", 0.0), 0.0),
                    rps_growth=safe_float(chosen_downscale.get("rps_growth", 0.0), 0.0),
                    short_p90_ms=safe_float(chosen_downscale.get("short_p90_ms", 0.0), 0.0),
                    trigger_slo=safe_float(chosen_downscale.get("trigger_slo", safe_float(slo_cfgs[chosen_downscale["service"]].get("slo", 0.0), 0.0)), 0.0),
                    target_from_traffic=safe_int(chosen_downscale.get("target_from_traffic", chosen_downscale.get("desired_replicas_from_traffic", 0)), 0),
                    target_from_slo=safe_int(chosen_downscale.get("target_from_slo", chosen_downscale.get("current_replicas", 0)), 0),
                    target_rps_per_replica_value=safe_float(chosen_downscale.get("target_rps_per_replica", 0.0), 0.0),
                    desired_replicas_from_traffic=safe_int(chosen_downscale.get("desired_replicas_from_traffic", 0), 0),
                    runq_pct_value=safe_float(chosen_downscale.get("runq_pct", 0.0), 0.0),
                    throttle_pct_value=safe_float(chosen_downscale.get("throttle_pct", 0.0), 0.0),
                    local_resource_support_value=str(chosen_downscale.get("local_resource_support", "")),
                    downscale_step=safe_int(chosen_downscale.get("downscale_step", chosen_downscale.get("step", 0)), 0),
                    downscale_reason=str(chosen_downscale.get("downscale_reason", "traffic_oversized_downscale")),
                    actual_replica_change=-int(chosen_downscale["step"]),
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
