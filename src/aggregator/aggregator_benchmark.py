import os
from datetime import datetime, timezone


TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "default")
TRAFFIC_TARGET_BASE_URL = os.getenv("TRAFFIC_TARGET_BASE_URL", "http://gateway")


def route_to_path(route):
    if not route:
        return ""
    path = str(route).strip().lower()
    if path.startswith("svc-"):
        path = path[4:]
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
        if norm.startswith("svc-") and len(norm) > 4:
            routes.add(norm[4:])
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


def traffic_route(name, label, path, target_deployment=""):
    return {
        "name": str(name),
        "label": str(label),
        "path": str(path),
        "targetDeployment": str(target_deployment or ""),
    }


def detect_benchmark_profile(namespace, deployment_rows, slo_cfg):
    names = {str(row.get("name", "")).strip() for row in deployment_rows if row.get("name")}
    namespace = str(namespace or TARGET_NAMESPACE).strip() or TARGET_NAMESPACE

    if namespace == "sock-shop" or "front-end" in names:
        routes = [
            traffic_route("home", "Home", "/", "front-end"),
            traffic_route("catalogue", "Catalogue API", "/catalogue", "catalogue"),
            traffic_route("category", "Category Page", "/category.html", "front-end"),
            traffic_route("basket_view", "Basket Page", "/basket.html", "carts"),
            traffic_route("login", "Login", "/login", "user"),
            traffic_route("customer_orders", "Customer Orders", "/customer-orders.html", "orders"),
        ]
        return {
            "id": "sock-shop",
            "name": "Sock Shop",
            "entryService": "front-end",
            "trafficBaseUrl": in_cluster_service_url("front-end", namespace),
            "trafficRoutes": routes,
            "note": "Traffic jobs should target the Sock Shop front-end service inside the cluster.",
        }

    if "gateway" in names or any(name.startswith("svc-") for name in names):
        routes = []
        for name in sorted(slo_cfg.keys()):
            path_name = route_to_path(name)
            route_path = "/" if not path_name else f"/{path_name}"
            routes.append(traffic_route(name, name, route_path, name))
        return {
            "id": "thrive-demo",
            "name": "ThriveScale Synthetic Demo",
            "entryService": "gateway",
            "trafficBaseUrl": in_cluster_service_url("gateway", namespace),
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
        base = TRAFFIC_TARGET_BASE_URL
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
