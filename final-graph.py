import requests
import time
import os
import sys

AGENT_URL = "http://localhost:5000"

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def get_status_symbol(latency):
    if latency < 20: return "🟢"
    if latency < 50: return "🟡"
    return "🔴"

def draw_dashboard():
    print("Waiting for Agent data...", flush=True)
    while True:
        try:
            response = requests.get(AGENT_URL, timeout=1)
            data = response.json()
            metrics = data.get("metrics", {})
            topology = data.get("topology", {})
            
            clear_screen()
            print("\n╔══════════════════════════════════════════════════╗")
            print("║   🔌  MICROSERVICE TOPOLOGY & LATENCY MAP        ║")
            print("╚══════════════════════════════════════════════════╝")
            
            print("\n [ SERVICE HEALTH ]")
            all_services = set(metrics.keys()) | set(topology.keys())
            for deps in topology.values(): all_services.update(deps)
            
            if not all_services: print("  (No traffic detected yet...)")
            
            for svc in sorted(list(all_services)):
                if svc == "bpf-agent": continue # Hide agent from UI
                lat = metrics.get(svc, 0) 
                symbol = get_status_symbol(lat)
                print(f"  {symbol} {svc:<15} : {lat} ms")

            print("\n [ DEPENDENCY GRAPH ]")
            print("graph LR")
            for source, targets in topology.items():
                if source == "autoscaler": continue # Hide autoscaler noise
                for target in targets:
                    if "10." in target: continue # Hide IPs
                    print(f"    {source} --> {target}")

            print("\n" + "="*50)
            print(" Press Ctrl+C to exit")
            time.sleep(1)

        except requests.exceptions.ConnectionError:
            print("❌ Cannot connect to Agent. Is port-forwarding active?")
            print("   Run: kubectl port-forward ds/bpf-agent 5000:5000")
            time.sleep(2)
        except KeyboardInterrupt:
            print("\n👋 Exiting Dashboard.")
            sys.exit(0)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    try:
        draw_dashboard()
    except KeyboardInterrupt:
        print("\n👋 Exiting.")