import json
import logging
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Tuple
from urllib.parse import urlparse

import redis
import requests
from kubernetes import client, config

AGGREGATOR_URL = os.getenv("AGGREGATOR_URL", "http://aggregator:8000")
AGGREGATOR_TIMEOUT_S = float(os.getenv("AGGREGATOR_TIMEOUT_S", "5"))
TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "default")
LOOP_SECONDS = float(os.getenv("LOOP_SECONDS", "2"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8081"))

TRACE_LOGS = os.getenv("TRACE_LOGS", "true").lower() == "true"
TRACE_REDIS_KEY = os.getenv("TRACE_REDIS_KEY", "decision_traces")
TRACE_REDIS_MAX = int(os.getenv("TRACE_REDIS_MAX", "500"))

ACTIVE_RPS_THRESHOLD = float(os.getenv("ACTIVE_RPS_THRESHOLD", "0.5"))
DOWNSCALE_RPS_THRESHOLD = float(os.getenv("DOWNSCALE_RPS_THRESHOLD", "0.5"))
DOWNSCALE_RPS_PER_REPLICA_THRESHOLD = float(os.getenv("DOWNSCALE_RPS_PER_REPLICA_THRESHOLD", "1.0"))
UPSCALE_COOLDOWN_S = int(os.getenv("UPSCALE_COOLDOWN_S", "4"))
DOWNSCALE_COOLDOWN_S = int(os.getenv("DOWNSCALE_COOLDOWN_S", "10"))
RUNQ_FIXED_THRESHOLD_MS = float(os.getenv("RUNQ_FIXED_THRESHOLD_MS", "3.0"))
RUNQ_BASELINE_MARGIN_MS = float(os.getenv("RUNQ_BASELINE_MARGIN_MS", "0.5"))
RUNQ_BASELINE_MULTIPLIER = float(os.getenv("RUNQ_BASELINE_MULTIPLIER", "1.25"))
DOWNSCALE_RUNQ_FACTOR = float(os.getenv("DOWNSCALE_RUNQ_FACTOR", "0.5"))
DOWNSCALE_RUNQ_MARGIN_MS = float(os.getenv("DOWNSCALE_RUNQ_MARGIN_MS", "1.0"))
DEPENDENCY_DOMINANCE_RATIO = float(os.getenv("DEPENDENCY_DOMINANCE_RATIO", "1.1"))
DEPENDENCY_DOMINANCE_MIN_FRACTION = float(os.getenv("DEPENDENCY_DOMINANCE_MIN_FRACTION", "0.4"))
OVERLOAD_ERROR_RATE_THRESHOLD = float(os.getenv("OVERLOAD_ERROR_RATE_THRESHOLD", "0.1"))
OVERLOAD_TIMEOUT_RATE_THRESHOLD = float(os.getenv("OVERLOAD_TIMEOUT_RATE_THRESHOLD", "0.02"))
UPSCALE_HYSTERESIS_RATIO = float(os.getenv("UPSCALE_HYSTERESIS_RATIO", "1.1"))
SEVERE_SLO_RATIO = float(os.getenv("SEVERE_SLO_RATIO", "1.5"))
SEVERE_RUNQ_RATIO = float(os.getenv("SEVERE_RUNQ_RATIO", "1.5"))
PRESSURE_STREAK_FOR_MODERATE = int(os.getenv("PRESSURE_STREAK_FOR_MODERATE", "2"))
PRESSURE_STREAK_FOR_SEVERE = int(os.getenv("PRESSURE_STREAK_FOR_SEVERE", "3"))
MAX_UPSCALE_STEP_PODS = int(os.getenv("MAX_UPSCALE_STEP_PODS", "3"))
MAX_UPSCALE_STEP_FACTOR = float(os.getenv("MAX_UPSCALE_STEP_FACTOR", "1.5"))
WARMUP_READY_GAP_RATIO = float(os.getenv("WARMUP_READY_GAP_RATIO", "0.1"))
UPSCALE_GAP_FRACTION_MILD = float(os.getenv("UPSCALE_GAP_FRACTION_MILD", "0.35"))
UPSCALE_GAP_FRACTION_MODERATE = float(os.getenv("UPSCALE_GAP_FRACTION_MODERATE", "0.5"))
UPSCALE_GAP_FRACTION_SEVERE = float(os.getenv("UPSCALE_GAP_FRACTION_SEVERE", "0.7"))
WARMUP_GAP_FRACTION_CAP = float(os.getenv("WARMUP_GAP_FRACTION_CAP", "0.35"))

READY_STATE = {"ready": False, "last_error": "", "last_loop_ts": 0.0}
UPSCALE_COOLDOWNS: Dict[str, float] = {}
DOWNSCALE_COOLDOWNS: Dict[str, float] = {}
PRESSURE_STREAKS: Dict[str, int] = {}

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


def clamp_min(value: float, minimum: float) -> float:
    return value if value >= minimum else minimum


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


def preferred_p90_ms(metrics, svc: str, long_window: bool = False) -> float:
    m = metric_obj(metrics, svc)
    truth_key = "truth_p90_latency_ms_long" if long_window else "truth_p90_latency_ms"
    p90_key = "p90_latency_long" if long_window else "p90_latency"
    truth = truth_value_or_zero(metrics, svc, truth_key, long_window)
    if truth_window_present(m, long_window):
        return truth
    return safe_float(m.get(p90_key, m.get("latency", 0.0)), 0.0)


def preferred_rps(metrics, svc: str, long_window: bool = False) -> float:
    m = metric_obj(metrics, svc)
    truth_key = "truth_rps_long" if long_window else "truth_rps"
    rps_key = "rps_long" if long_window else "rps"
    truth = truth_value_or_zero(metrics, svc, truth_key, long_window)
    if truth_window_present(m, long_window):
        return truth
    return safe_float(m.get(rps_key, 0.0), 0.0)


def runq_latency_ms(metrics, svc: str, long_window: bool = False) -> float:
    m = metric_obj(metrics, svc)
    p90_key = "runq_p90_latency_long" if long_window else "runq_p90_latency"
    avg_key = "avg_runq_latency_long" if long_window else "avg_runq_latency"
    p90_val = safe_float(m.get(p90_key, 0.0), 0.0)
    if p90_val > 0:
        return p90_val
    return safe_float(m.get(avg_key, 0.0), 0.0)


def timeout_rate(metrics, svc: str, long_window: bool = False) -> float:
    key = "truth_timeout_rate_long" if long_window else "truth_timeout_rate"
    return truth_value_or_zero(metrics, svc, key, long_window)


def error_rate_5xx(metrics, svc: str, long_window: bool = False) -> float:
    key = "truth_5xx_rate_long" if long_window else "truth_5xx_rate"
    return truth_value_or_zero(metrics, svc, key, long_window)


def downstream_latency_ms(metrics, svc: str, long_window: bool = False) -> float:
    key = "dependency_attributed_latency_long" if long_window else "dependency_attributed_latency"
    return safe_float(metric_obj(metrics, svc).get(key, 0.0), 0.0)


def local_handling_latency_ms(metrics, svc: str, long_window: bool = False) -> float:
    key = "exclusive_delay_long" if long_window else "exclusive_delay"
    return safe_float(metric_obj(metrics, svc).get(key, 0.0), 0.0)


def runq_threshold_ms(metrics, svc: str) -> float:
    m = metric_obj(metrics, svc)
    dynamic_thr = safe_float(m.get("runq_dynamic_threshold", 0.0), 0.0)
    if dynamic_thr > 0:
        return max(dynamic_thr, RUNQ_FIXED_THRESHOLD_MS)

    baseline = safe_float(m.get("runq_baseline", 0.0), 0.0)
    if baseline > 0:
        return max(
            RUNQ_FIXED_THRESHOLD_MS,
            baseline + RUNQ_BASELINE_MARGIN_MS,
            baseline * RUNQ_BASELINE_MULTIPLIER,
        )
    return RUNQ_FIXED_THRESHOLD_MS


def runq_low_threshold_ms(metrics, svc: str) -> float:
    baseline = safe_float(metric_obj(metrics, svc).get("runq_baseline", 0.0), 0.0)
    fixed = RUNQ_FIXED_THRESHOLD_MS * DOWNSCALE_RUNQ_FACTOR
    if baseline > 0:
        return max(fixed, baseline + DOWNSCALE_RUNQ_MARGIN_MS)
    return fixed


def calculate_upscale_target(
    current_replicas: int,
    max_replicas: int,
    p90_ms: float,
    slo_ms: float,
    runq_ms: float,
    runq_threshold_ms: float,
    rps: float,
    demand_threshold: float,
) -> int:
    latency_ratio = p90_ms / max(slo_ms, 1.0)
    runq_ratio = runq_ms / max(runq_threshold_ms, 0.001)
    rps_per_replica = rps / max(current_replicas, 1)
    demand_ratio = rps_per_replica / max(demand_threshold, 0.1)

    scale_factor = max(latency_ratio, runq_ratio, demand_ratio)
    scale_factor = min(scale_factor, 2.0)

    required = math.ceil(current_replicas * scale_factor)
    return min(max(required, current_replicas + 1), max_replicas)


def classify_pressure_severity(
    slo_violated: bool,
    active: bool,
    low_demand: bool,
    p90_ms: float,
    slo_ms: float,
    runq_ms: float,
    runq_threshold_ms: float,
    local_runq_pressure: bool,
    overload_support: bool,
) -> str:
    if not slo_violated:
        return "none"

    latency_ratio = p90_ms / max(slo_ms, 1.0)
    runq_ratio = runq_ms / max(runq_threshold_ms, 0.001)

    if local_runq_pressure and latency_ratio >= SEVERE_SLO_RATIO and runq_ratio >= SEVERE_RUNQ_RATIO:
        return "severe"
    if local_runq_pressure and (latency_ratio >= UPSCALE_HYSTERESIS_RATIO or runq_ratio >= 1.0):
        return "moderate"
    if overload_support:
        return "mild"
    return "mild"


def cap_upscale_target(
    current_replicas: int,
    estimated_target: int,
    max_replicas: int,
    severity: str,
    pressure_streak: int,
    warmup_active: bool,
) -> int:
    if estimated_target <= current_replicas:
        return current_replicas

    if severity == "mild":
        max_step = min(2, MAX_UPSCALE_STEP_PODS)
        gap_fraction = UPSCALE_GAP_FRACTION_MILD
    elif severity == "moderate":
        max_step = 1 if pressure_streak < PRESSURE_STREAK_FOR_MODERATE else min(2, MAX_UPSCALE_STEP_PODS)
        gap_fraction = UPSCALE_GAP_FRACTION_MODERATE
    else:
        if pressure_streak < PRESSURE_STREAK_FOR_SEVERE:
            max_step = min(2, MAX_UPSCALE_STEP_PODS)
        else:
            max_step = MAX_UPSCALE_STEP_PODS
        gap_fraction = UPSCALE_GAP_FRACTION_SEVERE

    if warmup_active:
        max_step = min(max_step, 1)
        gap_fraction = min(gap_fraction, WARMUP_GAP_FRACTION_CAP)

    gap = max(1, estimated_target - current_replicas)
    proportional_step = max(1, math.ceil(gap * max(gap_fraction, 0.0)))
    factor_cap_target = math.ceil(current_replicas * max(MAX_UPSCALE_STEP_FACTOR, 1.0))
    step_cap_target = current_replicas + min(max_step, proportional_step)
    allowed_target = min(estimated_target, factor_cap_target, step_cap_target, max_replicas)
    return max(current_replicas + 1, allowed_target)


def calculate_downscale_target(
    current_replicas: int,
    min_replicas: int,
    rps: float,
    rps_per_replica: float,
) -> int:
    if current_replicas <= min_replicas:
        return current_replicas

    if rps < 0.1:
        step = 2
    elif rps_per_replica < (DOWNSCALE_RPS_PER_REPLICA_THRESHOLD / 2):
        step = 2
    else:
        step = 1

    return max(current_replicas - step, min_replicas)


def get_slo_configs() -> Dict[str, Dict[str, float]]:
    configs = {}
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
        configs[deploy] = {
            "slo": safe_float(spec.get("sloLatency", 50), 50),
            "min": max(1, safe_int(spec.get("minReplicas", 1), 1)),
            "max": max(1, safe_int(spec.get("maxReplicas", 10), 10)),
        }
    return configs


def fetch_graph():
    resp = requests.get(f"{AGGREGATOR_URL.rstrip('/')}/api/graph", timeout=AGGREGATOR_TIMEOUT_S)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("metrics", {}) if isinstance(payload, dict) else {}


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


def emit_trace(
    service: str,
    decision: str,
    decision_class: str,
    reason: str,
    current_replicas: int,
    target_replicas: int,
    p90_ms: float,
    rps: float,
    runq_ms: float,
    downstream_ms: float,
    timeout_rate_val: float,
    error_rate_5xx_val: float,
    slo_ms: float,
    local_cpu_pressure: bool,
    dependency_dominant: bool,
    low_demand: bool,
    runq_threshold_val: float,
    dominant_signal: str = "",
    pressure_streak: int = 0,
    severity: str = "none",
    estimated_target: int = 0,
    applied_target: int = 0,
    warmup_active: bool = False,
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
        "root": service,
        "node": service,
        "service": service,
        "action": action,
        "decision": decision,
        "decision_class": decision_class,
        "reason": reason,
        "current_replicas": int(current_replicas),
        "target_replicas": int(target_replicas),
        "slo_ms": round(float(slo_ms), 3),
        "p90_ms": round(float(p90_ms), 3),
        "rps": round(float(rps), 3),
        "runq_latency_ms": round(float(runq_ms), 3),
        "runq_threshold_ms": round(float(runq_threshold_val), 3),
        "downstream_latency_ms": round(float(downstream_ms), 3),
        "timeout_rate": round(float(timeout_rate_val), 6),
        "error_rate_5xx": round(float(error_rate_5xx_val), 6),
        "local_cpu_pressure": bool(local_cpu_pressure),
        "dependency_dominant": bool(dependency_dominant),
        "low_demand": bool(low_demand),
        "dominant_signal": str(dominant_signal or ""),
        "pressure_streak": int(pressure_streak),
        "severity": str(severity),
        "estimated_target": int(estimated_target),
        "applied_target": int(applied_target),
        "warmup_active": bool(warmup_active),
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if TRACE_LOGS:
        logger.info("TRACE %s", serialized)
    try:
        TRACE_REDIS.lpush(TRACE_REDIS_KEY, serialized)
        TRACE_REDIS.ltrim(TRACE_REDIS_KEY, 0, TRACE_REDIS_MAX - 1)
    except Exception:
        pass


def evaluate_service(service: str, metrics: dict, cfg: dict) -> None:
    current_replicas, ready_replicas = read_replicas(service)

    slo_ms = safe_float(cfg.get("slo", 0.0), 0.0)
    min_replicas = max(1, safe_int(cfg.get("min", 1), 1))
    max_replicas = max(min_replicas, safe_int(cfg.get("max", 1), 1))

    short_p90_ms = preferred_p90_ms(metrics, service, long_window=False)
    short_rps = preferred_rps(metrics, service, long_window=False)
    p90_ms = short_p90_ms
    rps = short_rps
    runq_ms = runq_latency_ms(metrics, service, long_window=False)
    downstream_ms = downstream_latency_ms(metrics, service, long_window=False)
    local_ms = local_handling_latency_ms(metrics, service, long_window=False)
    timeout_rate_val = max(timeout_rate(metrics, service, False), timeout_rate(metrics, service, True))
    error_rate_5xx_val = max(error_rate_5xx(metrics, service, False), error_rate_5xx(metrics, service, True))
    truth_active = truth_window_present(metric_obj(metrics, service), False)

    runq_threshold_val = runq_threshold_ms(metrics, service)
    runq_low_threshold_val = runq_low_threshold_ms(metrics, service)

    demand_per_replica = short_rps / max(1, current_replicas)
    active = short_rps >= ACTIVE_RPS_THRESHOLD
    low_demand = (
        short_rps < DOWNSCALE_RPS_THRESHOLD
        or demand_per_replica < DOWNSCALE_RPS_PER_REPLICA_THRESHOLD
    )
    slo_violated = truth_active and active and slo_ms > 0 and p90_ms > slo_ms
    latency_ratio = p90_ms / max(slo_ms, 1.0) if slo_ms > 0 else 0.0
    runq_ratio = runq_ms / max(runq_threshold_val, 0.001)
    overload_symptoms = (
        truth_active
        and
        active
        and not low_demand
        and (error_rate_5xx_val >= OVERLOAD_ERROR_RATE_THRESHOLD or timeout_rate_val >= OVERLOAD_TIMEOUT_RATE_THRESHOLD)
    )
    local_runq_pressure = runq_ms >= runq_threshold_val
    overload_support = overload_symptoms and not local_runq_pressure
    local_cpu_pressure = local_runq_pressure
    dependency_dominant = (
        slo_violated
        and active
        and not local_cpu_pressure
        and p90_ms > 0
        and downstream_ms > 0
        and downstream_ms >= (p90_ms * DEPENDENCY_DOMINANCE_MIN_FRACTION)
        and downstream_ms >= (local_ms * DEPENDENCY_DOMINANCE_RATIO if local_ms > 0 else downstream_ms)
    )
    runq_low = runq_ms <= clamp_min(runq_low_threshold_val, 0.0)
    warmup_active = current_replicas > max(ready_replicas, 0) and (current_replicas - ready_replicas) >= max(1, math.ceil(current_replicas * WARMUP_READY_GAP_RATIO))
    severity = classify_pressure_severity(
        slo_violated=slo_violated,
        active=active,
        low_demand=low_demand,
        p90_ms=p90_ms,
        slo_ms=slo_ms,
        runq_ms=runq_ms,
        runq_threshold_ms=runq_threshold_val,
        local_runq_pressure=local_runq_pressure,
        overload_support=overload_support,
    )
    pressure_signal_active = slo_violated and active and not low_demand and not dependency_dominant and severity != "none"
    pressure_streak = PRESSURE_STREAKS.get(service, 0)
    if pressure_signal_active:
        pressure_streak += 1
    else:
        pressure_streak = 0
    PRESSURE_STREAKS[service] = pressure_streak

    dominant_signal = "none"
    if local_runq_pressure and runq_ratio >= latency_ratio:
        dominant_signal = "runq"
    elif slo_violated:
        dominant_signal = "latency"
    elif overload_support:
        dominant_signal = "overload"

    decision = "no_scale"
    decision_class = "stable"
    reason = "stable"
    target_replicas = current_replicas
    estimated_target = current_replicas

    if low_demand and runq_low and not local_cpu_pressure:
        decision_class = "low_demand"
        PRESSURE_STREAKS[service] = 0
        if current_replicas <= min_replicas:
            reason = "at_min"
        elif cooldown_active(DOWNSCALE_COOLDOWNS, service):
            reason = "cooldown_active"
        else:
            decision = "scale_down"
            reason = "low_demand"
            target_replicas = calculate_downscale_target(
                current_replicas=current_replicas,
                min_replicas=min_replicas,
                rps=short_rps,
                rps_per_replica=demand_per_replica,
            )
    elif active and not low_demand and not slo_violated:
        PRESSURE_STREAKS[service] = 0
        pressure_streak = 0
        if overload_support and not local_runq_pressure and not dependency_dominant:
            decision_class = "overload_uncertain"
            reason = "awaiting_local_or_slo_evidence"
        elif local_runq_pressure and not dependency_dominant:
            decision_class = "pressure_pre_slo"
            reason = "awaiting_slo_violation"
        else:
            decision_class = "stable"
            reason = "under_slo"
    elif slo_violated:
        if active and not low_demand and (local_runq_pressure or overload_support) and not dependency_dominant:
            decision_class = "local_cpu_pressure" if local_runq_pressure else "overload_guarded"
            if current_replicas >= max_replicas:
                reason = "at_max"
            elif cooldown_active(UPSCALE_COOLDOWNS, service):
                reason = "cooldown_active"
            elif warmup_active and pressure_streak < PRESSURE_STREAK_FOR_SEVERE:
                reason = "warmup_active"
            else:
                estimated_target = calculate_upscale_target(
                    current_replicas=current_replicas,
                    max_replicas=max_replicas,
                    p90_ms=p90_ms,
                    slo_ms=slo_ms,
                    runq_ms=runq_ms,
                    runq_threshold_ms=runq_threshold_val,
                    rps=short_rps,
                    demand_threshold=DOWNSCALE_RPS_PER_REPLICA_THRESHOLD,
                )
                target_replicas = cap_upscale_target(
                    current_replicas=current_replicas,
                    estimated_target=estimated_target,
                    max_replicas=max_replicas,
                    severity=severity,
                    pressure_streak=pressure_streak,
                    warmup_active=warmup_active,
                )
                if target_replicas > current_replicas:
                    decision = "scale_up"
                    reason = "local_cpu_pressure" if local_runq_pressure else "guarded_overload"
                else:
                    reason = "gated"
        elif dependency_dominant and not local_cpu_pressure:
            decision_class = "dependency_propagated"
            reason = "dependency_propagated"
        else:
            decision_class = "insufficient_evidence"
            reason = "insufficient_evidence"

    if decision == "scale_up" and target_replicas != current_replicas:
        patch_replicas(service, target_replicas)
        set_cooldown(UPSCALE_COOLDOWNS, service, UPSCALE_COOLDOWN_S)
        logger.info(
            "SCALING %s up: %d -> %d (p90=%.3fms slo=%.3fms rps=%.2f runq=%.3fms dep=%.3fms)",
            service,
            current_replicas,
            target_replicas,
            p90_ms,
            slo_ms,
            rps,
            runq_ms,
            downstream_ms,
        )
    elif decision == "scale_down" and target_replicas != current_replicas:
        patch_replicas(service, target_replicas)
        set_cooldown(DOWNSCALE_COOLDOWNS, service, DOWNSCALE_COOLDOWN_S)
        logger.info(
            "SCALING %s down: %d -> %d (p90=%.3fms slo=%.3fms rps=%.2f runq=%.3fms)",
            service,
            current_replicas,
            target_replicas,
            p90_ms,
            slo_ms,
            rps,
            runq_ms,
        )

    emit_trace(
        service=service,
        decision=decision,
        decision_class=decision_class,
        reason=reason,
        current_replicas=current_replicas,
        target_replicas=target_replicas,
        p90_ms=p90_ms,
        rps=rps,
        runq_ms=runq_ms,
        downstream_ms=downstream_ms,
        timeout_rate_val=timeout_rate_val,
        error_rate_5xx_val=error_rate_5xx_val,
        slo_ms=slo_ms,
        local_cpu_pressure=local_cpu_pressure,
        dependency_dominant=dependency_dominant,
        low_demand=low_demand,
        runq_threshold_val=runq_threshold_val,
        dominant_signal=dominant_signal,
        pressure_streak=pressure_streak,
        severity=severity,
        estimated_target=estimated_target,
        applied_target=target_replicas,
        warmup_active=warmup_active,
    )


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
        "Runtime config: target_ns=%s loop=%.1fs active_rps=%.2f runq_fixed=%.2fms up_cd=%ds down_cd=%ds",
        TARGET_NAMESPACE,
        LOOP_SECONDS,
        ACTIVE_RPS_THRESHOLD,
        RUNQ_FIXED_THRESHOLD_MS,
        UPSCALE_COOLDOWN_S,
        DOWNSCALE_COOLDOWN_S,
    )
    start_health_server()

    while True:
        loop_start = time.time()
        try:
            slo_cfgs = get_slo_configs()
            metrics = fetch_graph()
            for service in sorted(slo_cfgs.keys()):
                evaluate_service(service, metrics, slo_cfgs[service])
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
