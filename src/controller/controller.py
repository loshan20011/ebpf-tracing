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
LOOP_SECONDS = float(os.getenv("LOOP_SECONDS", "2"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8081"))

TRACE_LOGS = os.getenv("TRACE_LOGS", "true").lower() == "true"
TRACE_REDIS_KEY = os.getenv("TRACE_REDIS_KEY", "decision_traces")
TRACE_REDIS_MAX = int(os.getenv("TRACE_REDIS_MAX", "500"))

ACTIVE_RPS_THRESHOLD = float(os.getenv("ACTIVE_RPS_THRESHOLD", "0.5"))
DOWNSCALE_RPS_THRESHOLD = float(os.getenv("DOWNSCALE_RPS_THRESHOLD", "0.5"))
DOWNSCALE_RPS_PER_REPLICA_THRESHOLD = float(os.getenv("DOWNSCALE_RPS_PER_REPLICA_THRESHOLD", "1.0"))
LOW_DEMAND_STREAK_REQUIRED = int(os.getenv("LOW_DEMAND_STREAK_REQUIRED", "4"))
STRONG_DOWNSCALE_STREAK_REQUIRED = int(os.getenv("STRONG_DOWNSCALE_STREAK_REQUIRED", "10"))
SECONDARY_UPSCALE_MIN_RPS = float(os.getenv("SECONDARY_UPSCALE_MIN_RPS", "3.0"))
SECONDARY_UPSCALE_MIN_RPS_PER_REPLICA = float(os.getenv("SECONDARY_UPSCALE_MIN_RPS_PER_REPLICA", "1.0"))
SECONDARY_LOCAL_PRESSURE_STREAK_REQUIRED = int(os.getenv("SECONDARY_LOCAL_PRESSURE_STREAK_REQUIRED", "2"))
LATENCY_ONLY_LOCAL_PRESSURE_MIN_RPS = float(os.getenv("LATENCY_ONLY_LOCAL_PRESSURE_MIN_RPS", "3.0"))
UPSCALE_COOLDOWN_S = int(os.getenv("UPSCALE_COOLDOWN_S", "4"))
DOWNSCALE_COOLDOWN_S = int(os.getenv("DOWNSCALE_COOLDOWN_S", "10"))
RUNQ_FIXED_THRESHOLD_MS = float(os.getenv("RUNQ_FIXED_THRESHOLD_MS", "3.0"))
DOWNSCALE_RUNQ_FACTOR = float(os.getenv("DOWNSCALE_RUNQ_FACTOR", "0.5"))
DOWNSCALE_RUNQ_MARGIN_MS = float(os.getenv("DOWNSCALE_RUNQ_MARGIN_MS", "1.0"))
OVERLOAD_ERROR_RATE_THRESHOLD = float(os.getenv("OVERLOAD_ERROR_RATE_THRESHOLD", "0.1"))
OVERLOAD_TIMEOUT_RATE_THRESHOLD = float(os.getenv("OVERLOAD_TIMEOUT_RATE_THRESHOLD", "0.02"))
BREACH_STREAK_REQUIRED = int(os.getenv("BREACH_STREAK_REQUIRED", "2"))
RECENT_BREACH_HOLD_S = float(os.getenv("RECENT_BREACH_HOLD_S", str(max(4.0, LOOP_SECONDS * 2.0))))
MAX_ROOT_CAUSE_DEPTH = max(1, int(os.getenv("MAX_ROOT_CAUSE_DEPTH", "5")))
WARMUP_READY_GAP_RATIO = float(os.getenv("WARMUP_READY_GAP_RATIO", "0.1"))
PRIMARY_PROTECTIVE_FRONTEND_SCALE = os.getenv("PRIMARY_PROTECTIVE_FRONTEND_SCALE", "true").lower() == "true"
DEPENDENCY_DOMINANCE_RATIO = float(os.getenv("DEPENDENCY_DOMINANCE_RATIO", "1.25"))
EXTERNAL_DOMINANCE_RATIO = float(os.getenv("EXTERNAL_DOMINANCE_RATIO", "1.25"))

READY_STATE = {"ready": False, "last_error": "", "last_loop_ts": 0.0}
UPSCALE_COOLDOWNS: Dict[str, float] = {}
DOWNSCALE_COOLDOWNS: Dict[str, float] = {}
BREACH_STREAKS: Dict[str, int] = {}
LAST_BREACH_AT: Dict[str, float] = {}
LOW_DEMAND_STREAKS: Dict[str, int] = {}
LOCAL_PRESSURE_STREAKS: Dict[str, int] = {}

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


def truth_window_present(m: dict, long_window: bool = False) -> bool:
    fresh_key = "truth_fresh_long" if long_window else "truth_fresh"
    count_key = "truth_req_count_long" if long_window else "truth_req_count"
    count = safe_int(m.get(count_key, 0), 0)
    fresh = bool(m.get(fresh_key, False))
    return fresh and count > 0


def truth_value_or_zero(metrics, svc: str, key: str, long_window: bool = False) -> float:
    m = metric_obj(metrics, svc)
    if not truth_window_present(m, long_window):
        return 0.0
    return safe_float(m.get(key, 0.0), 0.0)


def preferred_truth_or_ebpf_p90(metrics, svc: str) -> Tuple[float, str, bool]:
    m = metric_obj(metrics, svc)
    if bool(m.get("latency_fresh", False)) and bool(m.get("evaluable_for_slo", False)):
        return safe_float(m.get("p90_latency", m.get("latency", 0.0)), 0.0), "aggregated", True
    if truth_window_present(m, False):
        return safe_float(m.get("truth_p90_latency_ms", 0.0), 0.0), "truth", True
    return 0.0, "none", False


def preferred_rps(metrics, svc: str) -> float:
    m = metric_obj(metrics, svc)
    truth = truth_value_or_zero(metrics, svc, "truth_rps", False)
    if truth_window_present(m, False):
        return truth
    return safe_float(m.get("rps", 0.0), 0.0)


def runq_latency_ms(metrics, svc: str) -> float:
    m = metric_obj(metrics, svc)
    p90_val = safe_float(m.get("runq_p90_latency", 0.0), 0.0)
    if p90_val > 0:
        return p90_val
    return safe_float(m.get("avg_runq_latency", 0.0), 0.0)


def runq_threshold_ms(_metrics, _svc: str) -> float:
    return RUNQ_FIXED_THRESHOLD_MS


def runq_low_threshold_ms(metrics, svc: str) -> float:
    fixed = RUNQ_FIXED_THRESHOLD_MS * DOWNSCALE_RUNQ_FACTOR
    _unused = (metrics, svc)
    return max(fixed, DOWNSCALE_RUNQ_MARGIN_MS)


def timeout_rate(metrics, svc: str) -> float:
    return max(
        truth_value_or_zero(metrics, svc, "truth_timeout_rate", False),
        truth_value_or_zero(metrics, svc, "truth_timeout_rate_long", True),
    )


def error_rate_5xx(metrics, svc: str) -> float:
    return max(
        truth_value_or_zero(metrics, svc, "truth_5xx_rate", False),
        truth_value_or_zero(metrics, svc, "truth_5xx_rate_long", True),
    )


def local_handling_latency_ms(metrics, svc: str) -> float:
    return safe_float(metric_obj(metrics, svc).get("service_handling_latency", metric_obj(metrics, svc).get("exclusive_delay", 0.0)), 0.0)


def dependency_latency_ms(metrics, svc: str) -> float:
    return safe_float(metric_obj(metrics, svc).get("dependency_attributed_latency", 0.0), 0.0)


def external_wait_ms(metrics, svc: str) -> float:
    return safe_float(metric_obj(metrics, svc).get("external_wait_latency", 0.0), 0.0)


def scale_step_primary(ratio: float, streak: int) -> int:
    if ratio >= 2.0 and streak >= 3:
        return 3
    if ratio >= 1.5 and streak >= BREACH_STREAK_REQUIRED:
        return 2
    return 1


def scale_step_secondary(_ratio: float, _streak: int) -> int:
    return 1


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


def local_pressure_present(metrics, svc: str) -> bool:
    local_ms = local_handling_latency_ms(metrics, svc)
    dep_ms = dependency_latency_ms(metrics, svc)
    ext_ms = external_wait_ms(metrics, svc)
    runq_ms = runq_latency_ms(metrics, svc)
    if runq_ms >= runq_threshold_ms(metrics, svc) and runq_ms > 0:
        return True
    if timeout_rate(metrics, svc) >= OVERLOAD_TIMEOUT_RATE_THRESHOLD:
        return True
    if error_rate_5xx(metrics, svc) >= OVERLOAD_ERROR_RATE_THRESHOLD:
        return True
    rps = preferred_rps(metrics, svc)
    return (
        local_ms > 0
        and rps >= LATENCY_ONLY_LOCAL_PRESSURE_MIN_RPS
        and local_ms >= max(dep_ms * 1.1, ext_ms * 1.1, 1.0)
    )


def warmup_active(service: str) -> Tuple[bool, int, int]:
    desired, ready = read_replicas(service)
    active = desired > max(ready, 0) and (desired - ready) >= max(1, math.ceil(desired * WARMUP_READY_GAP_RATIO))
    return active, desired, ready


def metric_fresh_enough_for_control(metrics, svc: str) -> bool:
    m = metric_obj(metrics, svc)
    if truth_window_present(m, False):
        return True
    demand_fresh = bool(m.get("latency_fresh", False)) and (
        safe_int(m.get("count", 0), 0) > 0
        or safe_float(m.get("rps", 0.0), 0.0) > 0.0
        or safe_int(m.get("ebpf_req_count", 0), 0) > 0
    )
    return demand_fresh and bool(m.get("evaluable_for_slo", False))


def demand_fresh_enough_for_downscale(metrics, svc: str) -> bool:
    m = metric_obj(metrics, svc)
    if truth_window_present(m, False):
        return True
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


def strongest_internal_child(
    current: str,
    metrics: dict,
    topology: dict,
    topology_meta: dict,
    monitored_services: set,
    slo_cfgs: dict,
) -> Optional[str]:
    children = [child for child in topology.get(current, []) if child in monitored_services]
    if not children:
        return None

    rows = topology_meta.get(current, {}) if isinstance(topology_meta.get(current, {}), dict) else {}

    def child_key(child: str):
        meta = rows.get(child, {}) if isinstance(rows.get(child, {}), dict) else {}
        weight = safe_float(meta.get("weight", 0.0), 0.0)
        child_slo = safe_float(slo_cfgs.get(child, {}).get("slo", 0.0), 0.0)
        child_p90, _source, sufficient = preferred_truth_or_ebpf_p90(metrics, child)
        ratio = (child_p90 / max(child_slo, 1.0)) if sufficient and child_slo > 0 else 0.0
        breached = 1 if ratio > 1.0 else 0
        local_pressure = 1 if local_pressure_present(metrics, child) else 0
        local_ms = local_handling_latency_ms(metrics, child)
        return (-breached, -local_pressure, -ratio, -weight, -local_ms, child)

    return sorted(children, key=child_key)[0]


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

    for _depth in range(MAX_ROOT_CAUSE_DEPTH):
        m = metric_obj(metrics, current)
        topo_fresh = bool(m.get("topology_fresh", False))
        local_ms = local_handling_latency_ms(metrics, current)
        dep_ms = dependency_latency_ms(metrics, current)
        ext_ms = external_wait_ms(metrics, current)
        local_pressure = local_pressure_present(metrics, current)
        dependency_dominant = dep_ms > 0 and dep_ms >= max(local_ms * DEPENDENCY_DOMINANCE_RATIO, local_ms + 1.0)
        external_dominant = ext_ms > 0 and ext_ms >= max(local_ms * EXTERNAL_DOMINANCE_RATIO, dep_ms * EXTERNAL_DOMINANCE_RATIO)

        if external_dominant:
            return {"classification": "external", "target": current, "path": path, "reason": "external_wait_dominant"}

        if local_pressure and not dependency_dominant:
            return {"classification": "local", "target": current, "path": path, "reason": "local_pressure"}

        if dependency_dominant:
            if not topo_fresh:
                return {"classification": "unclear", "target": current, "path": path, "reason": "topology_stale"}
            child = strongest_internal_child(current, metrics, topology, topology_meta, monitored_services, slo_cfgs)
            if not child:
                return {"classification": "external", "target": current, "path": path, "reason": "dependency_not_monitored"}
            if child in seen:
                return {"classification": "unclear", "target": current, "path": path, "reason": "loop_detected"}
            seen.add(child)
            path.append(child)
            current = child
            continue

        if local_pressure:
            return {"classification": "local", "target": current, "path": path, "reason": "local_pressure"}

        return {"classification": "unclear", "target": current, "path": path, "reason": "unclear"}

    return {"classification": "unclear", "target": current, "path": path, "reason": "max_depth_reached"}


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


def record_local_pressure_streak(service: str, local_pressure: bool) -> int:
    if local_pressure:
        LOCAL_PRESSURE_STREAKS[service] = LOCAL_PRESSURE_STREAKS.get(service, 0) + 1
    else:
        LOCAL_PRESSURE_STREAKS[service] = 0
    return LOCAL_PRESSURE_STREAKS.get(service, 0)


def breach_snapshot(service: str, metrics: dict, cfg: dict) -> dict:
    p90_ms, p90_source, sufficient = preferred_truth_or_ebpf_p90(metrics, service)
    rps = preferred_rps(metrics, service)
    active = is_active_for_control(metrics, service)
    slo_ms = safe_float(cfg.get("slo", 0.0), 0.0)
    ratio = (p90_ms / max(slo_ms, 1.0)) if slo_ms > 0 else 0.0
    breached = bool(active and sufficient and slo_ms > 0 and p90_ms > slo_ms)
    streak = record_breach_streak(service, breached)
    return {
        "service": service,
        "priority": str(cfg.get("priority", "secondary")),
        "p90_ms": p90_ms,
        "p90_source": p90_source,
        "evidence_sufficient": bool(sufficient),
        "rps": rps,
        "active": bool(active),
        "slo_ms": slo_ms,
        "ratio": ratio,
        "breached": breached,
        "streak": streak,
    }


def can_scale_now(service: str, cfg: dict) -> Tuple[bool, str, int, int]:
    current_replicas, ready_replicas = read_replicas(service)
    max_replicas = max(1, safe_int(cfg.get("max", 1), 1))
    if current_replicas >= max_replicas:
        return False, "at_max", current_replicas, ready_replicas
    if cooldown_active(UPSCALE_COOLDOWNS, service):
        return False, "cooldown_active", current_replicas, ready_replicas
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

    if trigger["streak"] < BREACH_STREAK_REQUIRED:
        resolution["classification"] = "unclear"
        resolution["reason"] = "breach_streak_too_short"
        return None, resolution

    classification = resolution["classification"]
    target = resolution["target"]

    if classification == "external":
        return None, resolution
    if classification == "unclear":
        if priority == "primary" and PRIMARY_PROTECTIVE_FRONTEND_SCALE and trigger["ratio"] >= 2.0 and trigger["streak"] >= 3:
            target = service
            resolution = {
                "classification": "local",
                "target": target,
                "path": list(resolution.get("path", [service])),
                "reason": "protective_primary_fallback",
            }
        else:
            return None, resolution

    target_cfg = slo_cfgs.get(target)
    if not target_cfg:
        return None, {"classification": "external", "target": target, "path": resolution.get("path", [service]), "reason": "target_not_monitored"}

    if classification != "local":
        record_local_pressure_streak(target, False)
        return None, resolution

    local_pressure_streak = record_local_pressure_streak(target, True)

    allowed, gate_reason, current_replicas, _ready_replicas = can_scale_now(target, target_cfg)
    if not allowed:
        return None, {
            "classification": "local",
            "target": target,
            "path": resolution.get("path", [service]),
            "reason": gate_reason,
        }

    if priority == "secondary":
        target_rps = preferred_rps(metrics, target)
        demand_per_replica = target_rps / max(current_replicas, 1)
        if not metric_fresh_enough_for_control(metrics, target):
            return None, {
                "classification": "local",
                "target": target,
                "path": resolution.get("path", [service]),
                "reason": "secondary_metrics_not_fresh",
            }
        if (
            target_rps < SECONDARY_UPSCALE_MIN_RPS
            or demand_per_replica < SECONDARY_UPSCALE_MIN_RPS_PER_REPLICA
        ):
            return None, {
                "classification": "local",
                "target": target,
                "path": resolution.get("path", [service]),
                "reason": "secondary_low_demand",
            }
        if local_pressure_streak < SECONDARY_LOCAL_PRESSURE_STREAK_REQUIRED:
            return None, {
                "classification": "local",
                "target": target,
                "path": resolution.get("path", [service]),
                "reason": "secondary_local_pressure_too_short",
            }

    if priority == "primary":
        step = scale_step_primary(trigger["ratio"], trigger["streak"])
    else:
        step = scale_step_secondary(trigger["ratio"], trigger["streak"])

    max_replicas = max(1, safe_int(target_cfg.get("max", 1), 1))
    target_replicas = min(max_replicas, current_replicas + max(1, step))
    if target_replicas <= current_replicas:
        return None, {
            "classification": "local",
            "target": target,
            "path": resolution.get("path", [service]),
            "reason": "gated",
        }

    action = {
        "trigger_service": service,
        "target_service": target,
        "priority": priority,
        "classification": "local",
        "reason": resolution.get("reason", "local_pressure"),
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
        "runq_threshold_ms": runq_threshold_ms(metrics, target),
        "downstream_ms": dependency_latency_ms(metrics, service),
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
    reason: str,
    path: List[str],
    decision: str,
    current_replicas: int,
    target_replicas: int,
    p90_ms: float,
    slo_ms: float,
    rps: float,
    runq_ms: float,
    runq_threshold_ms_value: float,
    downstream_ms: float,
    applied_scale_step: int,
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
        "decision": decision,
        "reason": reason,
        "priority_type": priority_type,
        "root_cause_classification": classification,
        "bottleneck_kind": classification,
        "bottleneck_path": list(path),
        "current_replicas": int(current_replicas),
        "target_replicas": int(target_replicas),
        "applied_scale_step": int(applied_scale_step),
        "slo_ms": round(float(slo_ms), 3),
        "p90_ms": round(float(p90_ms), 3),
        "rps": round(float(rps), 3),
        "runq_latency_ms": round(float(runq_ms), 3),
        "runq_threshold_ms": round(float(runq_threshold_ms_value), 3),
        "downstream_latency_ms": round(float(downstream_ms), 3),
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

    if snapshot and (bool(snapshot.get("breached", False)) or safe_int(snapshot.get("streak", 0), 0) > 0):
        record_low_demand_streak(service, False)
        return None
    if recent_breach_hold_active(service):
        record_low_demand_streak(service, False)
        return None
    if cooldown_active(UPSCALE_COOLDOWNS, service):
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

    rps = preferred_rps(metrics, service)
    runq_ms = runq_latency_ms(metrics, service)
    runq_low = runq_ms <= runq_low_threshold_ms(metrics, service)
    demand_per_replica = rps / max(current_replicas, 1)
    clearly_low_total = rps < DOWNSCALE_RPS_THRESHOLD
    clearly_low_per_replica = demand_per_replica < DOWNSCALE_RPS_PER_REPLICA_THRESHOLD
    very_low_demand = rps < 0.1 or demand_per_replica < (DOWNSCALE_RPS_PER_REPLICA_THRESHOLD / 2.0)
    sustained_low_demand = bool(runq_low and ((clearly_low_total and clearly_low_per_replica) or very_low_demand))
    low_demand_streak = record_low_demand_streak(service, sustained_low_demand)
    if not sustained_low_demand:
        return None
    if cooldown_active(DOWNSCALE_COOLDOWNS, service):
        return None
    if low_demand_streak < LOW_DEMAND_STREAK_REQUIRED:
        return None

    if (
        low_demand_streak >= STRONG_DOWNSCALE_STREAK_REQUIRED
        and (rps < 0.1 or demand_per_replica < (DOWNSCALE_RPS_PER_REPLICA_THRESHOLD / 2.0))
    ):
        step = 2
    else:
        step = 1
    target = max(min_replicas, current_replicas - step)
    if target >= current_replicas:
        return None

    return {
        "service": service,
        "current_replicas": current_replicas,
        "target_replicas": target,
        "step": current_replicas - target,
        "rps": rps,
        "runq_ms": runq_ms,
        "reason": "low_demand_sustained",
        "low_demand_streak": low_demand_streak,
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
                cfg = slo_cfgs[service]
                snapshot = breach_snapshot(service, metrics, cfg)
                action, resolution = propose_upscale_action(snapshot, metrics, topology, topology_meta, slo_cfgs) if snapshot["breached"] else (None, None)

                if action:
                    if snapshot["priority"] == "primary":
                        primary_actions.append(action)
                    else:
                        secondary_actions.append(action)
                elif snapshot["breached"]:
                    resolution_target = resolution.get("target", service)
                    resolution_replicas, _resolution_ready = read_replicas(resolution_target)
                    emit_trace(
                        trigger_service=service,
                        final_target_service=resolution_target,
                        priority_type=snapshot["priority"],
                        classification=resolution.get("classification", "unclear"),
                        reason=resolution.get("reason", "hold"),
                        path=list(resolution.get("path", [service])),
                        decision="no_scale",
                        current_replicas=resolution_replicas,
                        target_replicas=resolution_replicas,
                        p90_ms=snapshot["p90_ms"],
                        slo_ms=snapshot["slo_ms"],
                        rps=snapshot["rps"],
                        runq_ms=runq_latency_ms(metrics, resolution_target),
                        runq_threshold_ms_value=runq_threshold_ms(metrics, resolution_target),
                        downstream_ms=dependency_latency_ms(metrics, service),
                        applied_scale_step=0,
                    )

                downscale = propose_downscale_action(service, metrics, cfg, snapshot=snapshot)
                if downscale:
                    downscale_actions.append(downscale)

            chosen = pick_best_action(primary_actions)
            if not chosen:
                chosen = pick_best_action(secondary_actions)

            if chosen:
                patch_replicas(chosen["target_service"], chosen["target_replicas"])
                set_cooldown(UPSCALE_COOLDOWNS, chosen["target_service"], UPSCALE_COOLDOWN_S)
                logger.info(
                    "SCALING %s up for trigger %s: %d -> %d (priority=%s ratio=%.3f streak=%d path=%s)",
                    chosen["target_service"],
                    chosen["trigger_service"],
                    chosen["current_replicas"],
                    chosen["target_replicas"],
                    chosen["priority"],
                    chosen["ratio"],
                    chosen["breach_streak"],
                    " -> ".join(chosen["path"]),
                )
                emit_trace(
                    trigger_service=chosen["trigger_service"],
                    final_target_service=chosen["target_service"],
                    priority_type=chosen["priority"],
                    classification=chosen["classification"],
                    reason=chosen["reason"],
                    path=chosen["path"],
                    decision="scale_up",
                    current_replicas=chosen["current_replicas"],
                    target_replicas=chosen["target_replicas"],
                    p90_ms=chosen["p90_ms"],
                    slo_ms=chosen["slo_ms"],
                    rps=chosen["rps"],
                    runq_ms=chosen["runq_ms"],
                    runq_threshold_ms_value=chosen["runq_threshold_ms"],
                    downstream_ms=chosen["downstream_ms"],
                    applied_scale_step=chosen["step"],
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
                    classification="local",
                    reason="low_demand",
                    path=[chosen_downscale["service"]],
                    decision="scale_down",
                    current_replicas=chosen_downscale["current_replicas"],
                    target_replicas=chosen_downscale["target_replicas"],
                    p90_ms=preferred_truth_or_ebpf_p90(metrics, chosen_downscale["service"])[0],
                    slo_ms=safe_float(slo_cfgs[chosen_downscale["service"]].get("slo", 0.0), 0.0),
                    rps=chosen_downscale["rps"],
                    runq_ms=chosen_downscale["runq_ms"],
                    runq_threshold_ms_value=runq_threshold_ms(metrics, chosen_downscale["service"]),
                    downstream_ms=dependency_latency_ms(metrics, chosen_downscale["service"]),
                    applied_scale_step=chosen_downscale["step"],
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
