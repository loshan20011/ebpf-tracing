import threading
import requests
import time
import argparse
import sys

def fire_request(url, req_id):
    try:
        # Measures total client-side round-trip time
        resp = requests.get(url)
        # print(f"Thread #{req_id}: {resp.status_code} (Size: {len(resp.content)})")
    except Exception as e:
        print(f"Thread #{req_id} Error: {e}")

def main():
    parser = argparse.ArgumentParser(description='Thesis Load Generator')
    parser.add_argument('--host', type=str, default="localhost", help='Gateway/Node IP')
    parser.add_argument('--port', type=str, required=True, help='Service NodePort')
    parser.add_argument('--service', type=str, default="cpu", help='Target Service Endpoint (cpu, io, mem, chain)')
    parser.add_argument('--requests', type=int, default=20, help='Number of concurrent requests')
    parser.add_argument('--params', type=str, default="", help='Query params (e.g., length=5000)')

    args = parser.parse_args()

    # Construct URL
    query = f"?{args.params}" if args.params else ""
    url = f"http://{args.host}:{args.port}/{args.service}{query}"
    
    print(f"🚀 Launching {args.requests} parallel requests to: {url}")

    threads = []
    start_time = time.time()

    # Spawn threads
    for i in range(args.requests):
        t = threading.Thread(target=fire_request, args=(url, i))
        threads.append(t)
        t.start()

    # Wait for completion
    for t in threads:
        t.join()

    total_time = time.time() - start_time
    print(f"✅ Test Complete in {total_time:.2f}s")
    print(f"📊 Effective RPS: {args.requests / total_time:.2f}")

if __name__ == "__main__":
    main()
