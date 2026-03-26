import json
import os
from datetime import datetime, timezone


TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "default")
TRAFFIC_TARGET_BASE_URL = os.getenv("TRAFFIC_TARGET_BASE_URL", "").strip()
BENCHMARK_PROFILE = os.getenv("BENCHMARK_PROFILE", "").strip().lower()
SOCKSHOP_ENTRY_SERVICE = os.getenv("SOCKSHOP_ENTRY_SERVICE", "").strip()
SYNTHETIC_ENTRY_SERVICE = os.getenv("SYNTHETIC_ENTRY_SERVICE", "").strip()
SYNTHETIC_SERVICE_PREFIX = os.getenv("SYNTHETIC_SERVICE_PREFIX", "").strip()


def traffic_route(name, label, path, target_deployment=""):
    return {
        "name": str(name),
        "label": str(label),
        "path": str(path),
        "targetDeployment": str(target_deployment or ""),
    }


def parse_routes_json(raw, default_routes):
    if not str(raw or "").strip():
        return list(default_routes)
    try:
        parsed = json.loads(raw)
    except Exception:
        return list(default_routes)
    if not isinstance(parsed, list):
        return list(default_routes)
    routes = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        routes.append(
            traffic_route(
                item.get("name", ""),
                item.get("label", item.get("name", "")),
                item.get("path", "/"),
                item.get("targetDeployment", item.get("target_deployment", "")),
            )
        )
    return routes or list(default_routes)


DEFAULT_SOCKSHOP_ROUTES = [
    traffic_route("home", "Home", "/", "front-end"),
    traffic_route("catalogue", "Catalogue API", "/catalogue", "catalogue"),
    traffic_route("category", "Category Page", "/category.html", "front-end"),
    traffic_route("basket_view", "Basket Page", "/basket.html", "carts"),
    traffic_route("login", "Login", "/login", "user"),
    traffic_route("customer_orders", "Customer Orders", "/customer-orders.html", "orders"),
]
SOCKSHOP_TRAFFIC_ROUTES = parse_routes_json(os.getenv("SOCKSHOP_TRAFFIC_ROUTES_JSON", ""), DEFAULT_SOCKSHOP_ROUTES)


def route_to_path(route):
    if not route:
        return ""
    path = str(route).strip().lower()
    if SYNTHETIC_SERVICE_PREFIX and path.startswith(SYNTHETIC_SERVICE_PREFIX):
        path = path[len(SYNTHETIC_SERVICE_PREFIX) :]
    if path.startswith("/"):
        path = path[1:]
    return path


def derive_routes_from_slos(slo_cfg):
    routes = set()
    for name in slo_cfg.keys():
        if not name:
            continue
        norm = str(name).strip()
        routes.add(norm)
        if SYNTHETIC_SERVICE_PREFIX and norm.startswith(SYNTHETIC_SERVICE_PREFIX) and len(norm) > len(SYNTHETIC_SERVICE_PREFIX):
            routes.add(norm[len(SYNTHETIC_SERVICE_PREFIX) :])
    return sorted(routes)


def in_cluster_service_url(service_name, namespace):
    svc = str(service_name or "").strip()
    ns = str(namespace or TARGET_NAMESPACE).strip() or TARGET_NAMESPACE
    if not svc:
        return TRAFFIC_TARGET_BASE_URL
    if "://" in svc:
        return svc
    if "." in svc:
        return f"http://{svc}"
    return f"http://{svc}.{ns}"


def detect_benchmark_profile(namespace, deployment_rows, slo_cfg):
    _unused = deployment_rows
    namespace = str(namespace or TARGET_NAMESPACE).strip() or TARGET_NAMESPACE

    if BENCHMARK_PROFILE == "sock-shop":
        return {
            "id": "sock-shop",
            "name": "Sock Shop",
            "entryService": SOCKSHOP_ENTRY_SERVICE,
            "trafficBaseUrl": in_cluster_service_url(SOCKSHOP_ENTRY_SERVICE, namespace),
            "trafficRoutes": list(SOCKSHOP_TRAFFIC_ROUTES),
            "note": "Traffic jobs should target the Sock Shop front-end service inside the cluster.",
        }

    if BENCHMARK_PROFILE in {"synthetic", "thrive-demo"}:
        routes = []
        for name in sorted(slo_cfg.keys()):
            path_name = route_to_path(name)
            route_path = "/" if not path_name else f"/{path_name}"
            routes.append(traffic_route(name, name, route_path, name))
        return {
            "id": "thrive-demo",
            "name": "ThriveScale Synthetic Demo",
            "entryService": SYNTHETIC_ENTRY_SERVICE,
            "trafficBaseUrl": in_cluster_service_url(SYNTHETIC_ENTRY_SERVICE, namespace),
            "trafficRoutes": routes,
            "note": "Traffic jobs target the synthetic gateway service used for controlled autoscaling validation.",
        }

    generic_routes = []
    for name in sorted(slo_cfg.keys()):
        path_name = route_to_path(name)
        route_path = "/" if not path_name else f"/{path_name}"
        generic_routes.append(traffic_route(name, name, route_path, name))
    return {
        "id": "generic",
        "name": "Generic Kubernetes App",
        "entryService": "",
        "trafficBaseUrl": TRAFFIC_TARGET_BASE_URL,
        "trafficRoutes": generic_routes,
        "note": "Review the traffic base URL and route paths before starting controlled load.",
    }


def join_target_url(base_url, route_path):
    base = str(base_url or TRAFFIC_TARGET_BASE_URL).strip()
    if not base:
        return str(route_path or "").strip()
    base = base.rstrip("/")
    path = str(route_path or "").strip()
    if not path or path == "/":
        return f"{base}/"
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"


def sanitize_name_part(text):
    value = "".join(ch if (ch.isalnum() or ch == "-") else "-" for ch in str(text).lower())
    value = value.strip("-")
    return value[:40] if value else "x"


def current_ts_name():
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
