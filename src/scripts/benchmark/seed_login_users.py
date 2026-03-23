#!/usr/bin/env python3
import argparse
import base64
import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def post_json(url: str, payload: dict, timeout: float) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "sockshop-login-seeder/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return int(response.status or 0), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code or 0), body


def post_json_with_retry(url: str, payload: dict, timeout: float, retries: int, retry_delay_ms: float) -> tuple[int, str]:
    last_error = ""
    for attempt in range(max(1, retries)):
        try:
            return post_json(url, payload, timeout)
        except (urllib.error.URLError, http.client.RemoteDisconnected, ConnectionResetError, socket.timeout, TimeoutError) as exc:
            last_error = str(exc)
            if attempt + 1 < max(1, retries):
                time.sleep(max(0.0, retry_delay_ms) / 1000.0)
    return (0, last_error)


def login_ok(base_url: str, username: str, password: str, timeout: float) -> bool:
    login_url = urllib.parse.urljoin(base_url.rstrip("/") + "/", "login")
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        login_url,
        headers={"Authorization": f"Basic {auth}", "User-Agent": "sockshop-login-seeder/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status or 0) == 200
    except urllib.error.HTTPError:
        return False
    except (urllib.error.URLError, http.client.RemoteDisconnected, ConnectionResetError, socket.timeout, TimeoutError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Sock Shop login-capable users via /register.")
    parser.add_argument("--base-url", default="http://127.0.0.1:30001", help="Sock Shop front-end base URL")
    parser.add_argument("--count", type=int, default=100, help="Number of users to register")
    parser.add_argument("--prefix", default="autoscale_login", help="Username/email prefix")
    parser.add_argument("--password", default="passw0rd", help="Password for all generated users")
    parser.add_argument("--sleep-ms", type=float, default=0.0, help="Pause between registrations")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    parser.add_argument("--retries", type=int, default=5, help="Retries per user on transient disconnects")
    parser.add_argument("--retry-delay-ms", type=float, default=250.0, help="Delay between retries")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional file to write created credentials as JSON lines",
    )
    args = parser.parse_args()

    register_url = urllib.parse.urljoin(args.base_url.rstrip("/") + "/", "register")
    created = 0
    failures = 0
    duplicates = 0
    records = []

    for index in range(1, args.count + 1):
        username = f"{args.prefix}_{index:05d}"
        payload = {
            "username": username,
            "password": args.password,
            "firstName": "Seed",
            "lastName": f"LoginUser{index:05d}",
            "email": f"{username}@example.com",
        }
        status, body = post_json_with_retry(register_url, payload, args.timeout, args.retries, args.retry_delay_ms)
        if status == 200:
            created += 1
            records.append({"username": username, "password": args.password, "response": body})
        else:
            lowered = (body or "").lower()
            if "duplicate" in lowered or "exists" in lowered or status == 409:
                duplicates += 1
            elif login_ok(args.base_url, username, args.password, args.timeout):
                created += 1
                records.append({"username": username, "password": args.password, "response": "verified_via_login"})
            else:
                failures += 1
        if args.sleep_ms > 0:
            time.sleep(args.sleep_ms / 1000.0)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    summary = {
        "base_url": args.base_url,
        "requested": args.count,
        "registered": created,
        "duplicates": duplicates,
        "failures": failures,
        "prefix": args.prefix,
        "password": args.password,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
