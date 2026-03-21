import argparse
import asyncio
import csv
import json
import math
import random
import time
from dataclasses import dataclass
from collections import deque
from datetime import datetime, timezone
from statistics import mean
from typing import Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import urlopen

try:
    import aiohttp
except ImportError as exc:
    raise SystemExit(
        "Missing dependency 'aiohttp'. Install with: pip3 install aiohttp kubernetes pyyaml"
    ) from exc

try:
    import yaml
except ImportError:
    yaml = None

from kubernetes import client, config


@dataclass
class Sample:
    ts: float
    latency_ms: float
    ok: bool
    status_code: int
    route_group: str
    route_path: str
    truth_service: str
    failure_category: str


@dataclass
class RouteDef:
    name: str
    group: str
    service: str
    method: str
    path: str
    weight: float
    body: Optional[dict]
    headers: Dict[str, str]


@dataclass
class PhaseDef:
    start_s: float
    end_s: float
    target_rps: float


class StatsWindow:
    def __init__(self) -> None:
        self._samples: List[Sample] = []

    def record(self, sample: Sample) -> None:
        self._samples.append(sample)

    def snapshot_since(self, since_ts: float) -> Tuple[List[Sample], int, int, int, Dict[str, int], Dict[str, int]]:
        win = [s for s in self._samples if s.ts >= since_ts]
        sent = len(win)
        ok = sum(1 for s in win if s.ok)
        err = sent - ok
        groups: Dict[str, int] = {}
        failures: Dict[str, int] = {}
        for s in win:
            groups[s.route_group] = groups.get(s.route_group, 0) + 1
            if not s.ok:
                failures[s.failure_category] = failures.get(s.failure_category, 0) + 1
        return win, sent, ok, err, groups, failures


def p90(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(math.ceil(0.90 * len(ordered))) - 1
    idx = max(0, min(idx, len(ordered) - 1))
    return float(ordered[idx])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def target_rps_mode(mode: str, elapsed_s: float, base_rps: float, burst_rps: float, burst_at_s: float) -> float:
    if mode == "steady":
        return base_rps
    if mode == "sudden-burst":
        return base_rps if elapsed_s < burst_at_s else burst_rps
    if mode == "spike-then-recover":
        if elapsed_s < burst_at_s:
            return base_rps
        if elapsed_s < burst_at_s + 30:
            return burst_rps
        return base_rps
    return base_rps


def target_rps_phase(phases: List[PhaseDef], elapsed_s: float, fallback: float) -> float:
    for ph in phases:
        if ph.start_s <= elapsed_s < ph.end_s:
            return ph.target_rps
    return fallback


def weighted_pick(routes: List[RouteDef], rng: random.Random) -> RouteDef:
    total = sum(max(0.0, r.weight) for r in routes)
    if total <= 0:
        return routes[0]
    x = rng.uniform(0.0, total)
    acc = 0.0
    for r in routes:
        acc += max(0.0, r.weight)
        if x <= acc:
            return r
    return routes[-1]


def infer_truth_service(route_group: str, route_path: str, fallback: str) -> str:
    group = str(route_group or "").strip().lower()
    path = str(route_path or "").strip().lower()
    fb = str(fallback or "").strip() or "front-end"
    if group in {"front-end", "catalogue"}:
        return group
    if path.startswith("/catalogue") or path.startswith("/detail.html"):
        return "catalogue"
    if path == "/" or path.startswith("/category.html"):
        return "front-end"
    return fb


def load_mix_file(path: str) -> Tuple[List[PhaseDef], List[RouteDef]]:
    raw_text = open(path, "r", encoding="utf-8").read()
    if path.endswith(".json"):
        cfg = json.loads(raw_text)
    else:
        if yaml is None:
            raise SystemExit("Missing dependency 'pyyaml'. Install with: pip3 install pyyaml")
        cfg = yaml.safe_load(raw_text)

    phases = []
    for p in cfg.get("phases", []):
        phases.append(
            PhaseDef(
                start_s=float(p.get("start_s", 0.0)),
                end_s=float(p.get("end_s", 0.0)),
                target_rps=float(p.get("target_rps", 0.0)),
            )
        )

    routes = []
    for r in cfg.get("routes", []):
        routes.append(
            RouteDef(
                name=str(r.get("name", "route")),
                group=str(r.get("group", "browse")),
                service=str(r.get("service", "")).strip(),
                method=str(r.get("method", "GET")).upper(),
                path=str(r.get("path", "/")),
                weight=float(r.get("weight", 1.0)),
                body=r.get("body") if isinstance(r.get("body"), dict) else None,
                headers=r.get("headers") if isinstance(r.get("headers"), dict) else {},
            )
        )

    if not routes:
        raise SystemExit(f"No routes found in mix file: {path}")
    if not phases:
        raise SystemExit(f"No phases found in mix file: {path}")

    return phases, routes


def load_k8s_clients():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.AppsV1Api(), client.CoreV1Api(), client.CustomObjectsApi()


def discover_slo_latency_ms(custom_api, namespace: str, deployment: str) -> float:
    try:
        raw = custom_api.list_namespaced_custom_object(
            group="autoscaling.fyp.io",
            version="v1alpha1",
            namespace=namespace,
            plural="serviceslos",
        )
    except Exception:
        return 0.0
    for item in raw.get("items", []):
        spec = item.get("spec", {})
        if str(spec.get("targetDeployment", "")).strip() == deployment:
            try:
                return float(spec.get("sloLatency", 0.0))
            except Exception:
                return 0.0
    return 0.0


def read_pod_counts(apps_api, core_api, namespace: str, deployment: str) -> Tuple[int, int]:
    spec_replicas = 0
    ready_replicas = 0
    try:
        dep = apps_api.read_namespaced_deployment(name=deployment, namespace=namespace)
        spec_replicas = int(dep.spec.replicas or 0)
        selector = dep.spec.selector.match_labels or {}
        selector_str = ",".join([f"{k}={v}" for k, v in selector.items()])
        pods = core_api.list_namespaced_pod(namespace=namespace, label_selector=selector_str)
        ready = 0
        for p in pods.items:
            for c in (p.status.conditions or []):
                if c.type == "Ready" and c.status == "True":
                    ready += 1
                    break
        ready_replicas = ready
    except Exception:
        pass
    return spec_replicas, ready_replicas


def read_namespace_deployment_counts(apps_api, namespace: str) -> Tuple[Dict[str, int], Dict[str, int]]:
    spec_counts: Dict[str, int] = {}
    ready_counts: Dict[str, int] = {}
    try:
        deps = apps_api.list_namespaced_deployment(namespace=namespace)
    except Exception:
        return spec_counts, ready_counts

    for dep in deps.items:
        name = str(getattr(dep.metadata, "name", "") or "").strip()
        if not name or name == "traffic-gen":
            continue
        spec_counts[name] = int(dep.spec.replicas or 0)
        ready_counts[name] = int(dep.status.ready_replicas or 0)
    return spec_counts, ready_counts


def join_url(base_url: str, path: str) -> str:
    b = base_url.rstrip("/")
    p = path if path.startswith("/") else f"/{path}"
    return f"{b}{p}"


async def one_request(
    session: aiohttp.ClientSession,
    timeout_s: float,
    sem: asyncio.Semaphore,
    stats: StatsWindow,
    url: str,
    method: str,
    route_group: str,
    route_path: str,
    truth_service: str,
    body: Optional[dict],
    headers: Dict[str, str],
):
    async with sem:
        start = time.perf_counter()
        ok = False
        status_code = 0
        failure_category = "none"
        try:
            kwargs = {
                "timeout": aiohttp.ClientTimeout(total=timeout_s),
                "headers": headers or None,
            }
            if method in {"POST", "PUT", "PATCH"} and body is not None:
                kwargs["json"] = body
            async with session.request(method, url, **kwargs) as resp:
                await resp.read()
                status_code = int(resp.status)
                ok = 200 <= resp.status < 300
                if not ok:
                    if 500 <= int(resp.status) < 600:
                        failure_category = "5xx"
                    else:
                        failure_category = "http_non_2xx"
        except asyncio.TimeoutError:
            ok = False
            failure_category = "timeout"
        except aiohttp.ClientConnectorError as exc:
            msg = str(exc).lower()
            ok = False
            failure_category = "connection_refused" if "refused" in msg else "connect_error"
        except asyncio.CancelledError:
            ok = False
            failure_category = "client_dropped_request"
            raise
        except Exception:
            ok = False
            failure_category = "other_error"
        latency_ms = (time.perf_counter() - start) * 1000.0
        stats.record(
            Sample(
                ts=time.time(),
                latency_ms=latency_ms,
                ok=ok,
                status_code=status_code,
                route_group=route_group,
                route_path=route_path,
                truth_service=truth_service,
                failure_category=failure_category,
            )
        )


async def fire_second(
    sessions: List[aiohttp.ClientSession],
    timeout_s: float,
    sem: asyncio.Semaphore,
    stats: StatsWindow,
    rps: float,
    route_picker,
    saturation_counter: Dict[str, int],
):
    n = int(max(0, round(rps)))
    if n <= 0:
        await asyncio.sleep(1.0)
        return

    interval = 1.0 / float(n)
    tasks = []
    started = time.perf_counter()
    sess_n = len(sessions)
    for i in range(n):
        if sem.locked():
            saturation_counter["queue_saturation"] = saturation_counter.get("queue_saturation", 0) + 1
        sess = sessions[i % sess_n]
        method, url, route_group, route_path, truth_service, body, headers = route_picker()
        tasks.append(
            asyncio.create_task(
                one_request(
                    sess,
                    timeout_s,
                    sem,
                    stats,
                    url,
                    method,
                    route_group,
                    route_path,
                    truth_service,
                    body,
                    headers,
                )
            )
        )
        next_at = started + (i + 1) * interval
        sleep_s = next_at - time.perf_counter()
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)

    await asyncio.gather(*tasks, return_exceptions=True)


def fetch_traces(aggregator_url: str) -> List[dict]:
    if not aggregator_url:
        return []
    target = f"{aggregator_url.rstrip('/')}/api/traces"
    try:
        with urlopen(target, timeout=1.5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, ValueError):
        return []
    traces = payload.get("traces", [])
    if not isinstance(traces, list):
        return []
    return traces


def fetch_graph_snapshot(aggregator_url: str) -> dict:
    if not aggregator_url:
        return {}
    target = f"{aggregator_url.rstrip('/')}/api/graph"
    try:
        with urlopen(target, timeout=1.5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


async def post_truth_records(
    session: aiohttp.ClientSession,
    aggregator_url: str,
    service: str,
    samples: List[Sample],
) -> None:
    if not aggregator_url or not service or not samples:
        return
    target = f"{aggregator_url.rstrip('/')}/api/truth/ingest"
    records = []
    for sample in samples:
        records.append(
            {
                "service": service,
                "route": sample.route_path,
                "ts_ns": int(sample.ts * 1_000_000_000),
                "latency_ms": float(sample.latency_ms),
                "status_code": int(sample.status_code),
                "timeout": sample.failure_category == "timeout",
                "connect_refused": sample.failure_category == "connection_refused",
                "failure_category": sample.failure_category,
            }
        )
    try:
        async with session.post(target, json={"records": records}, timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
            await resp.read()
    except Exception:
        return


async def post_truth_records_by_service(
    session: aiohttp.ClientSession,
    aggregator_url: str,
    samples: List[Sample],
    fallback_service: str,
) -> None:
    if not aggregator_url or not samples:
        return
    grouped: Dict[str, List[Sample]] = {}
    for sample in samples:
        svc = str(sample.truth_service or "").strip() or fallback_service
        grouped.setdefault(svc, []).append(sample)
    for service, service_samples in grouped.items():
        await post_truth_records(session, aggregator_url, service, service_samples)


def parse_scale_target(action: str) -> Optional[int]:
    raw = str(action or "")
    if not raw.startswith("scale_to_"):
        return None
    try:
        return int(raw.split("scale_to_", 1)[1])
    except Exception:
        return None


async def sampler_loop(
    csv_path: str,
    stats: StatsWindow,
    sample_interval_s: float,
    start_ts: float,
    stop_event: asyncio.Event,
    apps_api,
    core_api,
    namespace: str,
    deployment: str,
    rps_fn,
    warmup_seconds: float,
    slo_latency_ms: float,
    aggregator_url: str,
    control_target: str,
    truth_service: str,
    breach_csv_path: str,
    saturation_counter: Dict[str, int],
):
    last_cut = start_ts
    run_start_unix_ms = int(start_ts * 1000)
    trace_seen: set = set()
    trace_order = deque(maxlen=4000)
    breach_id = 0
    active_breach = None

    def note_trace_seen(tid):
        if tid in trace_seen:
            return
        trace_seen.add(tid)
        trace_order.append(tid)
        while len(trace_order) == trace_order.maxlen:
            old = trace_order.popleft()
            trace_seen.discard(old)

    def write_breach_row(writer, row):
        writer.writerow(
            [
                row.get("breach_id"),
                row.get("first_slo_breach_s"),
                row.get("first_controller_trigger_s"),
                row.get("first_scale_decision_s"),
                row.get("first_pod_ready_s"),
                row.get("recovery_s"),
                row.get("time_to_recovery_s"),
                row.get("scale_target"),
                row.get("breach_ready_replicas"),
                json.dumps(row.get("failure_counts", {}), separators=(",", ":"), sort_keys=True),
            ]
        )

    async with aiohttp.ClientSession() as truth_session:
        with open(csv_path, "w", newline="") as f:
            with open(breach_csv_path, "w", newline="") as breach_f:
                breach_writer = csv.writer(breach_f)
                breach_writer.writerow(
                    [
                        "breach_id",
                        "first_slo_breach_s",
                        "first_controller_trigger_s",
                        "first_scale_decision_s",
                        "first_pod_ready_s",
                        "recovery_s",
                        "time_to_recovery_s",
                        "scale_target",
                        "breach_ready_replicas",
                        "failure_counts_json",
                    ]
                )
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "timestamp_utc",
                        "elapsed_s",
                        "target_rps",
                        "latency_p90_ms",
                        "latency_avg_ms",
                        "requests_sent",
                        "requests_ok",
                        "requests_err",
                        "route_group_counts_json",
                        "in_score_window",
                        "deployment",
                        "pod_spec_replicas",
                        "pod_ready_replicas",
                        "all_spec_replicas",
                        "all_ready_replicas",
                        "all_deployment_spec_replicas_json",
                        "all_deployment_ready_replicas_json",
                        "slo_latency_ms",
                        "service_p90_json",
                        "service_state_json",
                        "failure_categories_json",
                        "active_breach_id",
                        "queue_saturation",
                    ]
                )

                while not stop_event.is_set():
                    await asyncio.sleep(sample_interval_s)
                    now_ts = time.time()
                    elapsed_s = now_ts - start_ts
                    rps_now = float(rps_fn(elapsed_s))

                    win, sent, ok, err, groups, failures = stats.snapshot_since(last_cut)
                    last_cut = now_ts
                    if aggregator_url and win:
                        await post_truth_records_by_service(truth_session, aggregator_url, win, truth_service)
                    lats = [s.latency_ms for s in win]
                    p90_ms = p90(lats)
                    avg_ms = mean(lats) if lats else 0.0

                    spec_repl, ready_repl = read_pod_counts(apps_api, core_api, namespace, deployment)
                    all_spec_map, all_ready_map = read_namespace_deployment_counts(apps_api, namespace)
                    all_spec_total = int(sum(all_spec_map.values()))
                    all_ready_total = int(sum(all_ready_map.values()))
                    service_p90_map: Dict[str, float] = {}
                    service_state_map: Dict[str, Dict[str, bool]] = {}
                    if aggregator_url:
                        graph = fetch_graph_snapshot(aggregator_url)
                        graph_metrics = graph.get("metrics", {}) if isinstance(graph, dict) else {}
                        if isinstance(graph_metrics, dict):
                            for svc, metric in graph_metrics.items():
                                if not isinstance(metric, dict):
                                    continue
                                try:
                                    service_p90_map[str(svc)] = float(
                                        metric.get(
                                            "truth_p90_latency_ms",
                                            metric.get("p90_latency", metric.get("latency", 0.0)),
                                        )
                                        or 0.0
                                    )
                                except Exception:
                                    service_p90_map[str(svc)] = 0.0
                                service_state_map[str(svc)] = {
                                    "active": bool(metric.get("active_short", False)),
                                    "evaluable": bool(metric.get("evaluable_for_slo", False)),
                                    "truth_fresh": bool(metric.get("truth_fresh", False)),
                                }
                    in_score = 1 if elapsed_s >= warmup_seconds else 0
                    queue_sat = int(saturation_counter.get("queue_saturation", 0))
                    saturation_counter["queue_saturation"] = 0
                    if queue_sat > 0:
                        failures["queue_saturation"] = failures.get("queue_saturation", 0) + queue_sat

                    current_breach = in_score == 1 and slo_latency_ms > 0 and p90_ms > slo_latency_ms
                    if current_breach and active_breach is None:
                        breach_id += 1
                        active_breach = {
                            "breach_id": breach_id,
                            "first_slo_breach_s": round(elapsed_s, 3),
                            "breach_unix_ms": run_start_unix_ms + int(elapsed_s * 1000.0),
                            "first_controller_trigger_s": None,
                            "first_scale_decision_s": None,
                            "first_pod_ready_s": None,
                            "recovery_s": None,
                            "time_to_recovery_s": None,
                            "scale_target": None,
                            "breach_ready_replicas": int(ready_repl),
                            "failure_counts": {},
                        }

                    if aggregator_url and active_breach is not None:
                        traces = fetch_traces(aggregator_url)
                        traces.sort(key=lambda t: int(t.get("ts_unix_ms", 0)))
                        for tr in traces:
                            ts_ms = int(tr.get("ts_unix_ms", 0) or 0)
                            action = str(tr.get("action", ""))
                            root = str(tr.get("root", ""))
                            node = str(tr.get("node", ""))
                            tid = (ts_ms, root, node, action)
                            if tid in trace_seen:
                                continue
                            note_trace_seen(tid)
                            if ts_ms < int(active_breach["breach_unix_ms"]):
                                continue
                            if root != control_target:
                                continue
                            rel_s = round((ts_ms - run_start_unix_ms) / 1000.0, 3)
                            if active_breach["first_controller_trigger_s"] is None:
                                active_breach["first_controller_trigger_s"] = rel_s
                            if action.startswith("scale_to_") and active_breach["first_scale_decision_s"] is None:
                                active_breach["first_scale_decision_s"] = rel_s
                                active_breach["scale_target"] = parse_scale_target(action)

                    if active_breach is not None:
                        for k, v in failures.items():
                            active_breach["failure_counts"][k] = active_breach["failure_counts"].get(k, 0) + int(v)
                        if active_breach["first_pod_ready_s"] is None:
                            ready_threshold = int(active_breach["breach_ready_replicas"]) + 1
                            scale_target = active_breach.get("scale_target")
                            if isinstance(scale_target, int) and scale_target > int(active_breach["breach_ready_replicas"]):
                                ready_threshold = scale_target
                            if int(ready_repl) >= ready_threshold:
                                active_breach["first_pod_ready_s"] = round(elapsed_s, 3)

                    if active_breach is not None and not current_breach:
                        active_breach["recovery_s"] = round(elapsed_s, 3)
                        active_breach["time_to_recovery_s"] = round(
                            float(active_breach["recovery_s"]) - float(active_breach["first_slo_breach_s"]),
                            3,
                        )
                        write_breach_row(breach_writer, active_breach)
                        breach_f.flush()
                        active_breach = None

                    writer.writerow(
                        [
                            now_iso(),
                            f"{elapsed_s:.3f}",
                            f"{rps_now:.2f}",
                            f"{p90_ms:.3f}",
                            f"{avg_ms:.3f}",
                            sent,
                            ok,
                            err,
                            json.dumps(groups, separators=(",", ":"), sort_keys=True),
                            in_score,
                            deployment,
                            spec_repl,
                            ready_repl,
                            all_spec_total,
                            all_ready_total,
                            json.dumps(all_spec_map, separators=(",", ":"), sort_keys=True),
                            json.dumps(all_ready_map, separators=(",", ":"), sort_keys=True),
                            f"{slo_latency_ms:.3f}",
                            json.dumps(service_p90_map, separators=(",", ":"), sort_keys=True),
                            json.dumps(service_state_map, separators=(",", ":"), sort_keys=True),
                            json.dumps(failures, separators=(",", ":"), sort_keys=True),
                            active_breach["breach_id"] if active_breach is not None else "",
                            queue_sat,
                        ]
                    )
                    f.flush()
                    print(
                        f"[sample] t={elapsed_s:6.1f}s target_rps={rps_now:6.1f} "
                        f"p90={p90_ms:8.2f}ms avg={avg_ms:8.2f}ms "
                        f"sent={sent:5d} ok={ok:5d} err={err:4d} pods={ready_repl}/{spec_repl} "
                        f"fails={json.dumps(failures, separators=(',', ':'), sort_keys=True)}",
                        flush=True,
                    )

                if active_breach is not None:
                    write_breach_row(breach_writer, active_breach)
                    breach_f.flush()


async def main_async(args):
    apps_api, core_api, custom_api = load_k8s_clients()
    stats = StatsWindow()
    sem = asyncio.Semaphore(args.max_in_flight)
    stop_event = asyncio.Event()
    rng = random.Random(args.seed)
    saturation_counter: Dict[str, int] = {"queue_saturation": 0}
    slo_latency_ms = float(args.slo_latency_ms)
    if slo_latency_ms <= 0:
        slo_latency_ms = discover_slo_latency_ms(custom_api, args.namespace, args.deployment)
    if slo_latency_ms <= 0:
        print("[warn] SLO latency could not be discovered; breach metrics will be disabled", flush=True)
    control_target = args.control_target.strip() or args.deployment
    breach_csv_path = args.breach_csv.strip()
    if not breach_csv_path:
        if args.csv.lower().endswith(".csv"):
            breach_csv_path = f"{args.csv[:-4]}.breaches.csv"
        else:
            breach_csv_path = f"{args.csv}.breaches.csv"

    phases: List[PhaseDef] = []
    routes: List[RouteDef] = []

    if args.profile == "sockshop":
        if not args.mix_file:
            raise SystemExit("--mix-file is required when --profile sockshop")
        phases, routes = load_mix_file(args.mix_file)
        base_url = args.url.rstrip("/")

        def route_picker():
            r = weighted_pick(routes, rng)
            truth_service = r.service or infer_truth_service(r.group, r.path, control_target)
            return r.method, join_url(base_url, r.path), r.group, r.path, truth_service, r.body, r.headers

        def rps_fn(elapsed):
            return target_rps_phase(phases, elapsed, args.base_rps)

    else:
        # Generic mode now drives a default Sock Shop-like mixed path workload.
        base_url = args.url.rstrip("/")
        default_routes = [
            RouteDef(
                name="catalogue",
                group="catalogue",
                service="catalogue",
                method="GET",
                path="/catalogue",
                weight=0.60,
                body=None,
                headers={},
            ),
            RouteDef(
                name="cart",
                group="cart",
                service="front-end",
                method="GET",
                path="/cart",
                weight=0.30,
                body=None,
                headers={},
            ),
            RouteDef(
                name="root",
                group="static",
                service="front-end",
                method="GET",
                path="/",
                weight=0.10,
                body=None,
                headers={},
            ),
        ]

        def route_picker():
            r = weighted_pick(default_routes, rng)
            truth_service = r.service or infer_truth_service(r.group, r.path, control_target)
            return r.method, join_url(base_url, r.path), r.group, r.path, truth_service, r.body, r.headers

        def rps_fn(elapsed):
            return target_rps_mode(args.mode, elapsed, args.base_rps, args.burst_rps, args.burst_at)

    start_ts = time.time()
    sampler_task = asyncio.create_task(
        sampler_loop(
            csv_path=args.csv,
            stats=stats,
            sample_interval_s=args.sample_interval,
            start_ts=start_ts,
            stop_event=stop_event,
            apps_api=apps_api,
            core_api=core_api,
            namespace=args.namespace,
            deployment=args.deployment,
            rps_fn=rps_fn,
            warmup_seconds=args.warmup_seconds,
            slo_latency_ms=slo_latency_ms,
            aggregator_url=args.aggregator_url.strip(),
            control_target=control_target,
            truth_service=control_target,
            breach_csv_path=breach_csv_path,
            saturation_counter=saturation_counter,
        )
    )

    sessions = []
    for _ in range(max(1, args.session_pool)):
        sessions.append(aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0), cookie_jar=aiohttp.CookieJar()))

    try:
        while True:
            elapsed_s = time.time() - start_ts
            if elapsed_s >= args.duration:
                break
            rps_now = float(rps_fn(elapsed_s))
            await fire_second(sessions, args.timeout, sem, stats, rps_now, route_picker, saturation_counter)
    finally:
        stop_event.set()
        await sampler_task
        for sess in sessions:
            await sess.close()


def parse_args():
    p = argparse.ArgumentParser(description="ThriveScale Evaluation Harness")
    p.add_argument("--url", required=True, help="Target base URL, e.g. http://front-end.sock-shop")
    p.add_argument("--deployment", required=True, help="Deployment to track for pod count")
    p.add_argument("--namespace", default="default", help="Kubernetes namespace")
    p.add_argument("--profile", choices=["generic", "sockshop"], default="generic")
    p.add_argument("--mix-file", default="", help="Path to workload mix YAML/JSON (required for sockshop)")
    p.add_argument("--mode", choices=["steady", "sudden-burst", "spike-then-recover"], default="sudden-burst")
    p.add_argument("--duration", type=float, default=120, help="Total test duration in seconds")
    p.add_argument("--sample-interval", type=float, default=2, help="CSV sample interval seconds")
    p.add_argument("--base-rps", type=float, default=5, help="Base RPS before burst")
    p.add_argument("--burst-rps", type=float, default=200, help="Burst RPS after burst-at")
    p.add_argument("--burst-at", type=float, default=20, help="Second when burst begins")
    p.add_argument("--timeout", type=float, default=5, help="Per-request timeout seconds")
    p.add_argument("--max-in-flight", type=int, default=1000, help="Max concurrent in-flight requests")
    p.add_argument("--session-pool", type=int, default=50, help="Number of independent HTTP sessions")
    p.add_argument("--warmup-seconds", type=float, default=20, help="Exclude initial seconds from score window")
    p.add_argument("--seed", type=int, default=42, help="Random seed for deterministic mix replay")
    p.add_argument("--csv", default="eval_results.csv", help="Output CSV path")
    p.add_argument("--breach-csv", default="", help="Per-breach timing output CSV path")
    p.add_argument("--slo-latency-ms", type=float, default=0.0, help="SLO latency threshold ms (auto-discover if 0)")
    p.add_argument("--aggregator-url", default="", help="Aggregator URL for control-path timing (e.g., http://x.x.x.x:8000)")
    p.add_argument("--control-target", default="", help="Service root name to match controller traces (defaults to --deployment)")
    return p.parse_args()


def main():
    args = parse_args()
    print(
        f"[start] profile={args.profile} mode={args.mode} url={args.url} deployment={args.deployment} "
        f"duration={args.duration}s warmup={args.warmup_seconds}s base_rps={args.base_rps} "
        f"burst_rps={args.burst_rps} burst_at={args.burst_at}s mix={args.mix_file or '-'} csv={args.csv}",
        flush=True,
    )
    asyncio.run(main_async(args))
    print(f"[done] results written to {args.csv}", flush=True)


if __name__ == "__main__":
    main()
