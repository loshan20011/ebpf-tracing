import json
import math
import os
import threading
import time
from collections import defaultdict


WINDOW_LONG_SECONDS = float(os.getenv("WINDOW_LONG_SECONDS", "180"))
EVIDENCE_HISTORY_KEY = os.getenv("EVIDENCE_HISTORY_KEY", "svc:evidence")
EVIDENCE_HISTORY_MAX = int(os.getenv("EVIDENCE_HISTORY_MAX", "30"))
RUNQ_BASELINE_ALPHA = float(os.getenv("RUNQ_BASELINE_ALPHA", "0.20"))
RUNQ_BASELINE_MIN_COUNT = int(os.getenv("RUNQ_BASELINE_MIN_COUNT", "1"))
RUNQ_BASELINE_MIN_RUNQ_SAMPLES_PER_WINDOW = int(os.getenv("RUNQ_BASELINE_MIN_RUNQ_SAMPLES_PER_WINDOW", "1"))
HEALTHY_SLO_FACTOR = float(os.getenv("HEALTHY_SLO_FACTOR", "1.0"))
TOPO_EDGE_TTL_SECONDS = int(os.getenv("TOPO_EDGE_TTL_SECONDS", "120"))
RUNQ_LEARNING_ENABLED_KEY = os.getenv("RUNQ_LEARNING_ENABLED_KEY", "runq:learning_enabled")
EVENT_MAX_TS_MS_KEY = os.getenv("EVENT_MAX_TS_MS_KEY", "events:max_ts_ms")
EVENT_MAX_TS_SEC_KEY = os.getenv("EVENT_MAX_TS_SEC_KEY", "events:max_ts_sec")
EBPF_REQ_MIN_COUNT = int(os.getenv("EBPF_REQ_MIN_COUNT", "3"))
EBPF_REQ_MIN_RPS = float(os.getenv("EBPF_REQ_MIN_RPS", "1.0"))
EBPF_REQ_MIN_NET_SAMPLES = int(os.getenv("EBPF_REQ_MIN_NET_SAMPLES", "3"))
EBPF_REQ_MIN_RUNQ_SAMPLES = int(os.getenv("EBPF_REQ_MIN_RUNQ_SAMPLES", "3"))


def empty_truth_bucket():
    return {
        "count": 0,
        "timeout": 0,
        "conn_refused": 0,
        "err5xx": 0,
        "latencies_ms": [],
    }


class TruthWindowStore:
    def __init__(self, long_window_seconds, stale_after_seconds):
        self._window_seconds = max(1, int(math.ceil(float(long_window_seconds))))
        self._stale_after_seconds = max(0.1, float(stale_after_seconds))
        self._lock = threading.Lock()
        self._services = set()
        self._latest_update_ms = {}
        self._service_buckets = defaultdict(dict)
        self._route_buckets = defaultdict(dict)

    def reset(self):
        with self._lock:
            self._services.clear()
            self._latest_update_ms.clear()
            self._service_buckets.clear()
            self._route_buckets.clear()

    def services(self):
        with self._lock:
            return set(self._services)

    def ingest_records(self, records):
        if not records:
            return 0

        accepted = 0
        wall_ms = int(time.time() * 1000)

        with self._lock:
            for rec in records:
                svc = str(rec.get("service", "")).strip()
                if not svc:
                    continue

                ts_ns = rec.get("ts_ns")
                try:
                    event_ns = int(ts_ns) if ts_ns is not None else time.time_ns()
                except Exception:
                    event_ns = time.time_ns()
                if event_ns <= 0:
                    event_ns = time.time_ns()

                event_ms = int(event_ns // 1_000_000)
                event_sec = int(event_ns // 1_000_000_000)
                if event_ms <= 0:
                    event_ms = wall_ms
                if event_sec <= 0:
                    event_sec = max(0, event_ms // 1000)

                route = canonical_route(rec.get("route"))
                timeout = parse_bool(rec.get("timeout"))
                connect_refused = parse_bool(rec.get("connect_refused")) or parse_bool(rec.get("connection_refused"))
                failure_category = str(rec.get("failure_category", "")).strip().lower()
                if not connect_refused and failure_category in {"connection_refused", "connect_refused"}:
                    connect_refused = True

                status_code_raw = rec.get("status_code")
                try:
                    status_code = int(status_code_raw) if status_code_raw is not None else 0
                except Exception:
                    status_code = 0

                latency_ms = rec.get("latency_ms")
                latency_val = None
                if latency_ms is not None:
                    try:
                        latency_val = max(0.0, float(latency_ms))
                    except Exception:
                        latency_val = None

                self._services.add(svc)
                self._latest_update_ms[svc] = max(event_ms, int(self._latest_update_ms.get(svc, 0) or 0))

                svc_sec_map = self._service_buckets.setdefault(svc, {})
                svc_bucket = svc_sec_map.setdefault(event_sec, empty_truth_bucket())
                svc_bucket["count"] += 1
                if timeout:
                    svc_bucket["timeout"] += 1
                if connect_refused:
                    svc_bucket["conn_refused"] += 1
                if 500 <= status_code < 600:
                    svc_bucket["err5xx"] += 1
                if latency_val is not None:
                    svc_bucket["latencies_ms"].append(latency_val)

                svc_route_map = self._route_buckets.setdefault(svc, {})
                route_sec_map = svc_route_map.setdefault(route, {})
                route_bucket = route_sec_map.setdefault(event_sec, empty_truth_bucket())
                route_bucket["count"] += 1
                if timeout:
                    route_bucket["timeout"] += 1
                if connect_refused:
                    route_bucket["conn_refused"] += 1
                if 500 <= status_code < 600:
                    route_bucket["err5xx"] += 1
                if latency_val is not None:
                    route_bucket["latencies_ms"].append(latency_val)

                self._trim_service_locked(svc, event_sec - self._window_seconds)
                accepted += 1

        return accepted

    def _trim_service_locked(self, svc, cutoff_sec):
        svc_sec_map = self._service_buckets.get(svc, {})
        for sec in list(svc_sec_map.keys()):
            if int(sec) < cutoff_sec:
                del svc_sec_map[sec]
        route_map = self._route_buckets.get(svc, {})
        for route, sec_map in list(route_map.items()):
            for sec in list(sec_map.keys()):
                if int(sec) < cutoff_sec:
                    del sec_map[sec]
            if not sec_map:
                del route_map[route]

    def _zero_metrics(self, last_update_ms, now_ms):
        age_seconds = round(max(0.0, (float(now_ms) - float(last_update_ms)) / 1000.0), 3) if last_update_ms else None
        return {
            "count": 0,
            "rps": 0.0,
            "p90_latency_ms": 0.0,
            "timeout_rate": 0.0,
            "connect_refused_rate": 0.0,
            "error_5xx_rate": 0.0,
            "routes": {},
            "last_update_ts_ms": int(last_update_ms) if last_update_ms else 0,
            "age_seconds": age_seconds,
            "fresh": False,
        }

    def _aggregate_bucket_window(self, sec_map, now_sec, window_seconds):
        cutoff_sec = int(now_sec - int(math.ceil(float(window_seconds))))
        count = 0
        timeout_count = 0
        conn_refused_count = 0
        err5xx_count = 0
        latencies_ms = []
        for sec, bucket in sec_map.items():
            try:
                sec_i = int(sec)
            except Exception:
                continue
            if not (cutoff_sec <= sec_i <= int(now_sec)):
                continue
            count += int(bucket.get("count", 0) or 0)
            timeout_count += int(bucket.get("timeout", 0) or 0)
            conn_refused_count += int(bucket.get("conn_refused", 0) or 0)
            err5xx_count += int(bucket.get("err5xx", 0) or 0)
            latencies_ms.extend(bucket.get("latencies_ms", []) or [])

        return {
            "count": int(count),
            "rps": round(count / float(window_seconds), 3) if window_seconds > 0 and count > 0 else 0.0,
            "p90_latency_ms": round(percentile_p90(latencies_ms), 3) if latencies_ms else 0.0,
            "timeout_rate": round(timeout_count / float(count), 6) if count > 0 else 0.0,
            "connect_refused_rate": round(conn_refused_count / float(count), 6) if count > 0 else 0.0,
            "error_5xx_rate": round(err5xx_count / float(count), 6) if count > 0 else 0.0,
        }

    def aggregate_service_metrics(self, svc, now_ms, now_sec, window_seconds):
        with self._lock:
            last_update_ms = int(self._latest_update_ms.get(svc, 0) or 0)
            if last_update_ms <= 0:
                return self._zero_metrics(0, now_ms)

            age_seconds = round(max(0.0, (float(now_ms) - float(last_update_ms)) / 1000.0), 3)
            if age_seconds > self._stale_after_seconds:
                return self._zero_metrics(last_update_ms, now_ms)

            aggregated = self._aggregate_bucket_window(self._service_buckets.get(svc, {}), now_sec, window_seconds)
            routes = {}
            for route, sec_map in self._route_buckets.get(svc, {}).items():
                route_metric = self._aggregate_bucket_window(sec_map, now_sec, window_seconds)
                if int(route_metric.get("count", 0) or 0) <= 0:
                    continue
                routes[route] = route_metric

            aggregated["routes"] = routes
            aggregated["last_update_ts_ms"] = last_update_ms
            aggregated["age_seconds"] = age_seconds
            aggregated["fresh"] = True
            return aggregated


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


def trim_window(pipe, svc, cutoff_ms):
    pipe.zremrangebyscore(f"ts:net:{svc}", "-inf", cutoff_ms)
    pipe.zremrangebyscore(f"ts:runq:{svc}", "-inf", cutoff_ms)


def trim_req_buckets(pipe, redis_cli, svc, cutoff_sec):
    key = f"bucket:req:{svc}"
    fields = redis_cli.hkeys(key)
    stale = []
    for field in fields:
        try:
            sec = int(field)
            if sec < cutoff_sec:
                stale.append(field)
        except ValueError:
            stale.append(field)
    if stale:
        pipe.hdel(key, *stale)
    pipe.expire(key, int(WINDOW_LONG_SECONDS * 6))


def trim_hash_bucket(pipe, redis_cli, key, cutoff_sec):
    fields = redis_cli.hkeys(key)
    stale = []
    for field in fields:
        try:
            sec = int(field)
            if sec < cutoff_sec:
                stale.append(field)
        except ValueError:
            stale.append(field)
    if stale:
        pipe.hdel(key, *stale)
    pipe.expire(key, int(WINDOW_LONG_SECONDS * 6))


def canonical_route(route):
    raw = str(route or "").strip().lower()
    if not raw:
        return "_all"
    raw = raw[:180]
    return raw.replace(" ", "_")


def parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def canonical_external_target(raw_dst):
    raw = str(raw_dst or "").strip().lower()
    if not raw:
        return "external:unknown"
    safe = "".join(ch if (ch.isalnum() or ch in {".", ":", "-"}) else "-" for ch in raw)
    safe = safe.strip("-")
    if not safe:
        safe = "unknown"
    return f"external:{safe[:80]}"


def trim_topology_edges(pipe, redis_cli, src_svc, cutoff_sec):
    last_map = redis_cli.hgetall(f"topo:edge:last_ts:{src_svc}")
    stale = []
    for dst, sec_raw in last_map.items():
        try:
            sec = int(sec_raw)
            if sec < cutoff_sec:
                stale.append(dst)
        except Exception:
            stale.append(dst)
    if stale:
        pipe.hdel(f"topo:edge:last_ts:{src_svc}", *stale)
        pipe.hdel(f"topo:edge:hits:{src_svc}", *stale)
        pipe.hdel(f"topo:edge:type:{src_svc}", *stale)
        pipe.srem(f"topo:{src_svc}", *stale)
    pipe.expire(f"topo:{src_svc}", int(max(60, TOPO_EDGE_TTL_SECONDS * 2)))
    pipe.expire(f"topo:edge:last_ts:{src_svc}", int(max(60, TOPO_EDGE_TTL_SECONDS * 2)))
    pipe.expire(f"topo:edge:hits:{src_svc}", int(max(60, TOPO_EDGE_TTL_SECONDS * 2)))
    pipe.expire(f"topo:edge:type:{src_svc}", int(max(60, TOPO_EDGE_TTL_SECONDS * 2)))


def ingest_events(redis_cli, events, next_seq, ip_to_service):
    if not events:
        return

    wall_ms = int(time.time() * 1000)
    wall_sec = int(time.time())
    max_event_ms = 0
    max_event_sec = 0
    pipe = redis_cli.pipeline()
    touched_services = set()

    for event in events:
        svc = event.get("service")
        if not svc:
            continue

        event_type = event.get("event_type")
        data = event.get("data") or {}
        seq = next_seq()
        ts_ns = event.get("ts_ns")
        try:
            event_ns = int(ts_ns) if ts_ns is not None else time.time_ns()
        except Exception:
            event_ns = time.time_ns()
        if event_ns <= 0:
            event_ns = time.time_ns()
        event_ms = int(event_ns // 1_000_000)
        event_sec = int(event_ns // 1_000_000_000)
        max_event_ms = max(max_event_ms, event_ms)
        max_event_sec = max(max_event_sec, event_sec)

        if event_type == "net_latency":
            latency_us = int(data.get("latency_us", 0))
            pipe.zadd(f"ts:net:{svc}", {f"{latency_us}:{seq}": event_ms})
            touched_services.add(svc)
        elif event_type == "request":
            pipe.hincrby(f"bucket:req:{svc}", str(event_sec), 1)
            touched_services.add(svc)
        elif event_type == "runq_latency":
            delay_us = int(data.get("delay_us", 0))
            pipe.zadd(f"ts:runq:{svc}", {f"{delay_us}:{seq}": event_ms})
            touched_services.add(svc)
        elif event_type == "connect":
            dst_ip = data.get("dst_ip")
            if dst_ip:
                dst_svc = ip_to_service(dst_ip)
                if dst_svc and dst_svc != svc:
                    edge_dst = dst_svc
                    edge_type = "internal"
                    touched_services.add(dst_svc)
                else:
                    edge_dst = canonical_external_target(dst_ip)
                    edge_type = "external"
                pipe.sadd(f"topo:{svc}", edge_dst)
                pipe.hincrby(f"topo:edge:hits:{svc}", edge_dst, 1)
                pipe.hset(f"topo:edge:last_ts:{svc}", edge_dst, int(event_sec))
                pipe.hset(f"topo:edge:type:{svc}", edge_dst, edge_type)
                pipe.expire(f"topo:{svc}", int(max(60, TOPO_EDGE_TTL_SECONDS * 2)))
                pipe.expire(f"topo:edge:last_ts:{svc}", int(max(60, TOPO_EDGE_TTL_SECONDS * 2)))
                pipe.expire(f"topo:edge:hits:{svc}", int(max(60, TOPO_EDGE_TTL_SECONDS * 2)))
                pipe.expire(f"topo:edge:type:{svc}", int(max(60, TOPO_EDGE_TTL_SECONDS * 2)))
                touched_services.add(svc)

    if max_event_ms <= 0:
        max_event_ms = wall_ms
    if max_event_sec <= 0:
        max_event_sec = wall_sec
    cutoff_ms = max_event_ms - int(WINDOW_LONG_SECONDS * 1000)
    cutoff_sec = max_event_sec - int(WINDOW_LONG_SECONDS)
    pipe.set(EVENT_MAX_TS_MS_KEY, str(max_event_ms))
    pipe.set(EVENT_MAX_TS_SEC_KEY, str(max_event_sec))

    for svc in touched_services:
        pipe.sadd("services", svc)
        trim_window(pipe, svc, cutoff_ms)
        trim_req_buckets(pipe, redis_cli, svc, cutoff_sec)
        trim_topology_edges(pipe, redis_cli, svc, cutoff_sec=max(0, cutoff_sec - TOPO_EDGE_TTL_SECONDS))

    pipe.execute()


def sum_bucket_in_window(bucket, now_sec, window_seconds):
    total = 0
    cutoff_sec = now_sec - int(window_seconds)
    for sec_str, cnt_str in bucket.items():
        try:
            sec = int(sec_str)
            cnt = int(cnt_str)
            if cutoff_sec <= sec <= now_sec:
                total += cnt
        except ValueError:
            continue
    return total


def aggregate_service_metrics(redis_cli, svc, cutoff_ms, now_sec, window_seconds, truth=None):
    net_rows = redis_cli.zrangebyscore(f"ts:net:{svc}", cutoff_ms, "+inf")
    runq_rows = redis_cli.zrangebyscore(f"ts:runq:{svc}", cutoff_ms, "+inf")
    req_buckets = redis_cli.hgetall(f"bucket:req:{svc}")

    latencies_ms = []
    for row in net_rows:
        try:
            latency_us = int(row.split(":", 1)[0])
            latencies_ms.append(latency_us / 1000.0)
        except Exception:
            continue

    runq_ms = []
    for row in runq_rows:
        try:
            delay_us = int(row.split(":", 1)[0])
            runq_ms.append(delay_us / 1000.0)
        except Exception:
            continue

    p90_latency = round(percentile_p90(latencies_ms), 3) if latencies_ms else 0.0
    avg_runq = round(sum(runq_ms) / len(runq_ms), 3) if runq_ms else 0.0
    runq_p90 = round(percentile_p90(runq_ms), 3) if runq_ms else 0.0
    runq_max = round(max(runq_ms), 3) if runq_ms else 0.0
    req_count_ebpf = sum_bucket_in_window(req_buckets, now_sec, window_seconds)
    req_rps_ebpf = round(req_count_ebpf / float(window_seconds), 3) if window_seconds > 0 else 0.0
    truth = truth or {}
    truth_req_count = int(truth.get("count", 0))
    corroborated_ebpf_req = (
        req_count_ebpf >= EBPF_REQ_MIN_COUNT
        and req_rps_ebpf >= EBPF_REQ_MIN_RPS
        and (
            len(latencies_ms) >= EBPF_REQ_MIN_NET_SAMPLES
            or len(runq_ms) >= EBPF_REQ_MIN_RUNQ_SAMPLES
        )
    )
    if truth_req_count > 0:
        req_count = truth_req_count
        rps = float(truth.get("rps", 0.0))
        rps_source = "truth"
    elif corroborated_ebpf_req:
        req_count = req_count_ebpf
        rps = req_rps_ebpf
        rps_source = "ebpf_req"
    else:
        req_count = 0
        rps = 0.0
        rps_source = "none" if req_count_ebpf <= 0 else "ebpf_req_uncorroborated"

    return {
        "latency": p90_latency,
        "p90_latency": p90_latency,
        "exclusive_delay": 0.0,
        "avg_runq_latency": avg_runq,
        "runq_p90_latency": runq_p90,
        "runq_max_latency": runq_max,
        "rps": rps,
        "count": req_count,
        "runq_sample_count": int(len(runq_ms)),
        "net_sample_count": int(len(latencies_ms)),
        "ebpf_req_count": int(req_count_ebpf),
        "ebpf_req_corroborated": bool(corroborated_ebpf_req),
        "truth_req_count": int(truth_req_count),
        "truth_rps": float(truth.get("rps", 0.0)),
        "rps_source": rps_source,
        "truth_timeout_rate": float(truth.get("timeout_rate", 0.0)),
        "truth_connect_refused_rate": float(truth.get("connect_refused_rate", 0.0)),
        "truth_5xx_rate": float(truth.get("error_5xx_rate", 0.0)),
        "truth_p90_latency_ms": float(truth.get("p90_latency_ms", 0.0)),
        "truth_routes": truth.get("routes", {}),
        "truth_last_update_ts_ms": int(truth.get("last_update_ts_ms", 0) or 0),
        "truth_age_seconds": truth.get("age_seconds", None),
        "truth_fresh": bool(truth.get("fresh", False)),
    }


def get_latest_event_age_seconds(redis_cli, svc, now_ms, kind):
    key_map = {
        "net": f"ts:net:{svc}",
        "runq": f"ts:runq:{svc}",
        "truth": f"ts:truth:lat:{svc}",
    }
    key = key_map.get(kind)
    if not key:
        return None
    try:
        rows = redis_cli.zrevrangebyscore(key, "+inf", "-inf", start=0, num=1, withscores=True)
    except Exception:
        return None
    if not rows:
        return None
    try:
        _member, score = rows[0]
        return round(max(0.0, (float(now_ms) - float(score)) / 1000.0), 3)
    except Exception:
        return None


def get_latest_topology_age_seconds(topology_meta_rows):
    if not isinstance(topology_meta_rows, dict) or not topology_meta_rows:
        return None
    ages = []
    for row in topology_meta_rows.values():
        if not isinstance(row, dict):
            continue
        try:
            ages.append(max(0.0, float(row.get("age_sec", 0.0))))
        except Exception:
            continue
    if not ages:
        return None
    return round(min(ages), 3)


def service_activity_state(short_metric, long_metric, freshness):
    short_req = max(int(short_metric.get("count", 0) or 0), int(short_metric.get("truth_req_count", 0) or 0))
    long_req = max(int(long_metric.get("count", 0) or 0), int(long_metric.get("truth_req_count", 0) or 0))
    short_rps = max(float(short_metric.get("rps", 0.0) or 0.0), float(short_metric.get("truth_rps", 0.0) or 0.0))
    long_rps = max(float(long_metric.get("rps", 0.0) or 0.0), float(long_metric.get("truth_rps", 0.0) or 0.0))
    short_net_samples = int(short_metric.get("net_sample_count", 0) or 0)
    truth_short = int(short_metric.get("truth_req_count", 0) or 0)
    truth_long = int(long_metric.get("truth_req_count", 0) or 0)
    short_p90 = max(
        float(short_metric.get("p90_latency", 0.0) or 0.0),
        float(short_metric.get("truth_p90_latency_ms", 0.0) or 0.0),
    )
    long_p90 = max(
        float(long_metric.get("p90_latency", 0.0) or 0.0),
        float(long_metric.get("truth_p90_latency_ms", 0.0) or 0.0),
    )
    latency_fresh = freshness.get("latency_fresh", False)
    truth_fresh = freshness.get("truth_fresh", False)
    runq_fresh = freshness.get("runq_fresh", False)

    observed_short = short_req > 0 or short_rps > 0.0 or short_net_samples > 0 or truth_short > 0 or short_p90 > 0.0
    observed_long = long_req > 0 or long_rps > 0.0 or truth_long > 0 or long_p90 > 0.0

    active_short = observed_short and (latency_fresh or truth_fresh or runq_fresh)
    active_long = observed_long and (latency_fresh or truth_fresh or runq_fresh)
    evaluable_for_slo = (
        (
            active_short
            and (
                (latency_fresh and (short_p90 > 0.0 or short_net_samples > 0 or short_req > 0 or short_rps > 0.0))
                or (truth_fresh and truth_short > 0)
            )
        )
        or (active_short and (long_p90 > 0.0 or truth_long > 0) and (observed_short or runq_fresh))
    )

    evidence_confidence = 0.0
    if active_short:
        evidence_confidence += 0.40
    if active_long:
        evidence_confidence += 0.20
    if latency_fresh:
        evidence_confidence += 0.20
    if truth_fresh and truth_short > 0:
        evidence_confidence += 0.15
    elif runq_fresh and observed_short:
        evidence_confidence += 0.10
    if evaluable_for_slo:
        evidence_confidence += 0.05

    return {
        "active_short": bool(active_short),
        "active_long": bool(active_long),
        "evaluable_for_slo": bool(evaluable_for_slo),
        "evidence_confidence": round(min(1.0, evidence_confidence), 3),
    }


def downstream_wait_hint(edge_rows):
    if not isinstance(edge_rows, dict) or not edge_rows:
        return 0.0
    total_weight = 0.0
    external_weight = 0.0
    for row in edge_rows.values():
        if not isinstance(row, dict):
            continue
        try:
            weight = max(0.0, float(row.get("weight", 0.0)))
        except Exception:
            weight = 0.0
        total_weight += weight
        if str(row.get("type", "internal")).lower() == "external":
            external_weight += weight
    if total_weight <= 0:
        return 0.0
    return max(0.0, min(1.0, external_weight / total_weight))


def preferred_metric_latency(metric, p90_field="p90_latency"):
    long_window = str(p90_field).endswith("_long")
    primary = float(metric.get(p90_field, metric.get("latency", 0.0)) or 0.0)
    long_field = f"{p90_field}_long" if not long_window else p90_field
    fallback = float(metric.get(long_field, 0.0) or 0.0)

    truth_key = "truth_p90_latency_ms_long" if long_window else "truth_p90_latency_ms"
    truth_fresh_key = "truth_fresh_long" if long_window else "truth_fresh"
    truth_count_key = "truth_req_count_long" if long_window else "truth_req_count"
    truth_val = float(metric.get(truth_key, 0.0) or 0.0)
    truth_fresh = bool(metric.get(truth_fresh_key, False))
    truth_count = int(metric.get(truth_count_key, 0) or 0)

    # Match controller preference: use request-truth latency when that window is fresh.
    if truth_fresh and truth_count > 0:
        return truth_val
    if primary > 0:
        return primary
    if fallback > 0:
        return fallback
    if truth_val > 0:
        return truth_val
    return float(metric.get("truth_p90_latency_ms_long", 0.0) or 0.0)


def derive_latency_split(metric, topology_meta_rows):
    total = preferred_metric_latency(metric, "p90_latency")
    exclusive = float(metric.get("exclusive_delay", 0.0) or 0.0)
    dependency_delay = max(0.0, total - exclusive)
    external_wait = downstream_wait_hint(topology_meta_rows)
    external_wait_latency = round(dependency_delay * external_wait, 3)
    dependency_internal_latency = round(max(0.0, dependency_delay - external_wait_latency), 3)
    return {
        "service_handling_latency": round(exclusive, 3),
        "dependency_attributed_latency": round(dependency_internal_latency, 3),
        "external_wait_latency": external_wait_latency,
        "exclusive_delay_source": "topology" if topology_meta_rows else "sparse_fallback",
    }


def persist_service_evidence(redis_cli, svc, now_sec, metric):
    payload = {
        "ts_sec": int(now_sec),
        "p90_latency": float(metric.get("p90_latency", 0.0) or 0.0),
        "rps": float(metric.get("rps", 0.0) or 0.0),
        "truth_rps": float(metric.get("truth_rps", 0.0) or 0.0),
        "truth_req_count": int(metric.get("truth_req_count", 0) or 0),
        "active_short": bool(metric.get("active_short", False)),
        "active_long": bool(metric.get("active_long", False)),
        "evaluable_for_slo": bool(metric.get("evaluable_for_slo", False)),
        "evidence_confidence": float(metric.get("evidence_confidence", 0.0) or 0.0),
    }
    key = f"{EVIDENCE_HISTORY_KEY}:{svc}"
    try:
        redis_cli.lpush(key, json.dumps(payload, separators=(",", ":")))
        redis_cli.ltrim(key, 0, max(0, EVIDENCE_HISTORY_MAX - 1))
    except Exception:
        pass


def runq_baseline_key(svc):
    return f"runq:baseline:{svc}"


def runq_variance_key(svc):
    return f"runq:variance:{svc}"


def runq_healthy_count_key(svc):
    return f"runq:healthy_count:{svc}"


def runq_sample_count_key(svc):
    return f"runq:sample_count:{svc}"


def read_runq_baseline(redis_cli, svc):
    raw = redis_cli.get(runq_baseline_key(svc))
    try:
        return round(float(raw), 3) if raw is not None else 0.0
    except Exception:
        return 0.0


def read_runq_variance(redis_cli, svc):
    raw = redis_cli.get(runq_variance_key(svc))
    try:
        return max(float(raw), 0.0) if raw is not None else 0.0
    except Exception:
        return 0.0


def read_runq_healthy_count(redis_cli, svc):
    raw = redis_cli.get(runq_healthy_count_key(svc))
    try:
        return max(int(raw), 0) if raw is not None else 0
    except Exception:
        return 0


def read_runq_sample_count(redis_cli, svc):
    raw = redis_cli.get(runq_sample_count_key(svc))
    try:
        return max(int(raw), 0) if raw is not None else 0
    except Exception:
        return 0


def runq_learning_enabled(redis_cli):
    raw = redis_cli.get(RUNQ_LEARNING_ENABLED_KEY)
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def learn_runq_baseline(redis_cli, svc, short_metric, slo_ms, learning_enabled=True):
    req_count = int(short_metric.get("count", 0))
    runq_sample_count = int(short_metric.get("runq_sample_count", 0))
    p90_ms = float(short_metric.get("p90_latency", 0.0))
    runq_ms = float(short_metric.get("avg_runq_latency", 0.0))

    if not learning_enabled:
        baseline = read_runq_baseline(redis_cli, svc)
        variance = read_runq_variance(redis_cli, svc)
        std_dev = math.sqrt(variance)
        healthy_windows = read_runq_healthy_count(redis_cli, svc)
        learned_samples = read_runq_sample_count(redis_cli, svc)
        return round(baseline, 3), round(std_dev, 3), healthy_windows, learned_samples

    healthy = (
        slo_ms > 0
        and req_count >= RUNQ_BASELINE_MIN_COUNT
        and runq_sample_count >= RUNQ_BASELINE_MIN_RUNQ_SAMPLES_PER_WINDOW
        and p90_ms > 0
        and p90_ms <= (slo_ms * HEALTHY_SLO_FACTOR)
    )
    if not healthy:
        baseline = read_runq_baseline(redis_cli, svc)
        variance = read_runq_variance(redis_cli, svc)
        std_dev = math.sqrt(variance)
        healthy_windows = read_runq_healthy_count(redis_cli, svc)
        learned_samples = read_runq_sample_count(redis_cli, svc)
        return round(baseline, 3), round(std_dev, 3), healthy_windows, learned_samples

    prev = read_runq_baseline(redis_cli, svc)
    prev_var = read_runq_variance(redis_cli, svc)
    if prev <= 0:
        learned = runq_ms
        learned_var = 0.0
    else:
        delta = runq_ms - prev
        learned = prev + (RUNQ_BASELINE_ALPHA * delta)
        learned_var = (1.0 - RUNQ_BASELINE_ALPHA) * (prev_var + (RUNQ_BASELINE_ALPHA * delta * delta))

    learned = max(0.0, learned)
    learned_var = max(0.0, learned_var)
    redis_cli.set(runq_baseline_key(svc), f"{learned:.6f}")
    redis_cli.set(runq_variance_key(svc), f"{learned_var:.6f}")
    healthy_windows = read_runq_healthy_count(redis_cli, svc) + 1
    learned_samples = read_runq_sample_count(redis_cli, svc) + runq_sample_count
    redis_cli.set(runq_healthy_count_key(svc), str(healthy_windows))
    redis_cli.set(runq_sample_count_key(svc), str(learned_samples))
    return round(learned, 3), round(math.sqrt(learned_var), 3), healthy_windows, learned_samples


def read_event_watermark(redis_cli):
    wall_ms = int(time.time() * 1000)
    wall_sec = int(time.time())
    raw_ms = redis_cli.get(EVENT_MAX_TS_MS_KEY)
    raw_sec = redis_cli.get(EVENT_MAX_TS_SEC_KEY)
    event_ms = 0
    event_sec = 0
    try:
        event_ms = int(raw_ms) if raw_ms is not None else 0
    except Exception:
        event_ms = 0
    try:
        event_sec = int(raw_sec) if raw_sec is not None else 0
    except Exception:
        event_sec = 0

    if event_ms > 0 and event_sec <= 0:
        event_sec = event_ms // 1000
    if event_sec >= 946684800:
        if event_ms <= 0:
            event_ms = event_sec * 1000
        return max(wall_ms, int(event_ms)), max(wall_sec, int(event_sec))
    return wall_ms, wall_sec


def is_infra_service(svc, infra_exact, infra_prefixes):
    if not svc:
        return True
    name = str(svc).strip().lower()
    if not name:
        return True
    if name in infra_exact:
        return True
    for prefix in infra_prefixes:
        if name.startswith(prefix):
            return True
    return False


def compute_exclusive_delays(metrics, topology, p90_field="p90_latency"):
    ex_map = {}
    for svc, metric in metrics.items():
        p90 = preferred_metric_latency(metric, p90_field)
        children = topology.get(svc, [])
        if not children:
            ex_map[svc] = max(p90, 0.0)
            continue
        child_max = 0.0
        for child in children:
            child_metric = metrics.get(child, {})
            child_p90 = preferred_metric_latency(child_metric, p90_field)
            child_max = max(child_max, child_p90)
        ex_map[svc] = max(p90 - child_max, 0.0)
    return ex_map
