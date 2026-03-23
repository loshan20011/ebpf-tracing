#!/usr/bin/env python3
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def post_json(url: str, payload: dict, timeout: float) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "sockshop-seed/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, str(exc)


def get_json(url: str, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "sockshop-seed/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Sock Shop customers via the front-end API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:30001", help="Sock Shop front-end base URL")
    parser.add_argument("--count", type=int, default=100, help="Number of customers to create")
    parser.add_argument("--prefix", default="autoscale_seed", help="Username/email prefix")
    parser.add_argument("--password", default="passw0rd", help="Password for seeded customers")
    parser.add_argument("--sleep-ms", type=float, default=0.0, help="Optional delay between creates")
    parser.add_argument("--timeout", type=float, default=10.0, help="Request timeout in seconds")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    create_url = urllib.parse.urljoin(base_url + "/", "customers")
    seeded = 0
    duplicates = 0
    failures = 0

    for idx in range(1, args.count + 1):
        username = f"{args.prefix}_{idx:05d}"
        payload = {
            "firstName": "Seed",
            "lastName": f"User{idx:05d}",
            "username": username,
            "password": args.password,
            "email": f"{username}@example.com",
        }
        status, body = post_json(create_url, payload, args.timeout)
        if 200 <= status < 300:
            seeded += 1
        elif status in {409, 412} or "Duplicate" in body or "exists" in body:
            duplicates += 1
        else:
            failures += 1
            print(f"[seed] failed idx={idx} status={status} body={body[:200]}", file=sys.stderr)
        if args.sleep_ms > 0:
            time.sleep(args.sleep_ms / 1000.0)

    status, body = get_json(create_url, args.timeout)
    embedded_count = None
    if status == 200:
        try:
            payload = json.loads(body)
            embedded_count = len((payload.get("_embedded") or {}).get("customer") or [])
        except Exception:
            embedded_count = None

    print(
        json.dumps(
            {
                "base_url": base_url,
                "requested": args.count,
                "seeded": seeded,
                "duplicates": duplicates,
                "failures": failures,
                "customers_get_status": status,
                "visible_customers_in_response": embedded_count,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
