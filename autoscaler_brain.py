import requests
import time
import subprocess
import numpy as np
import re

GATEWAY_URL = "http://localhost:8080"
SLA_THRESHOLD_MS = 200  # If latency > 200ms, we have a problem
BPF_SCRIPT = "diagnose.bt"

def get_latency(endpoint):
    try:
        start = time.time()
        # We assume the gateway passes the request to the microservice
        resp = requests.get(f"{GATEWAY_URL}/{endpoint}")
        # Return latency in milliseconds
        return (time.time() - start) * 1000
    except:
        return 0

def run_diagnosis():
    print("\n[!] SLA VIOLATION DETECTED! TRIGGERING eBPF PROFILER...")
    print("    Running diagnosis for 5 seconds...")
    
    # Run bpftrace and capture stdout
    cmd = ["sudo", "bpftrace", BPF_SCRIPT]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    analyze_report(result.stdout)

def analyze_report(output):
    print("\n--- ROOT CAUSE ANALYSIS REPORT ---")
    
    # FILTER: We only care about our apps
    target_apps = ["svc-cpu", "svc-io", "svc-net", "svc-mem", "svc-chain"]

    # 1. Check for Disk I/O (Smarter Logic)
    disk_matches = re.findall(r"@disk_io\[(.*?)\]: (\d+)", output)
    
    # Filter: Only keep matches that are in our target_apps list
    relevant_disk = [m for m in disk_matches if m[0] in target_apps]
    
    if relevant_disk:
        top_disk = max(relevant_disk, key=lambda x: int(x[1]))
        if int(top_disk[1]) > 50:
            print(f"🔴 CRITICAL: DISK I/O BOTTLENECK detected in service: {top_disk[0]}")
            print(f"    Evidence: {top_disk[1]} block requests detected.")
            return

    # 2. Check for CPU
    # This is trickier because stacks are multi-line. 
    # For now, we look for high counts in the stack map.
    if "svc-cpu" in output:
        print(f"🔴 CRITICAL: CPU SATURATION detected in service: svc-cpu")
        print("    Evidence: High CPU profile samples captured.")
        # Extract function name if possible
        if "isPrime" in output:
            print("    Culprit Function: main.isPrime")
        return

    # 3. Check for Network/External (High Wait but No Disk)
    wait_matches = re.findall(r"@total_wait\[(.*?)\]: (\d+)", output)
    if wait_matches:
        top_wait = max(wait_matches, key=lambda x: int(x[1]))
        if "svc-net" in top_wait[0]:
            print(f"🟡 WARNING: EXTERNAL/NETWORK LATENCY detected in service: {top_wait[0]}")
            print("    Reason: Service is sleeping but NOT using Disk. Likely waiting for API.")
            return

    print("🟢 Analysis Inconclusive. System noise might be high.")

def main():
    print(f"[*] Autoscaler Brain Started. Monitoring P99 Latency (Threshold: {SLA_THRESHOLD_MS}ms)...")
    
    latencies = []
    window_size = 5  # Keep last 5 requests
    
    while True:
        # Simulate Traffic (Randomly pick an endpoint to test)
        # You can change this manually to test specific scenarios
        # For this demo, let's ping all of them
        
        for svc in ["cpu", "io", "net", "chain"]:
            lat = get_latency(svc)
            latencies.append(lat)
            if len(latencies) > window_size:
                latencies.pop(0)
            
            # Calculate P99 of the rolling window
            p99 = np.percentile(latencies, 99)
            
            print(f"\rCurrent P99: {p99:.2f}ms | Last Request ({svc}): {lat:.2f}ms", end="")
            
            if p99 > SLA_THRESHOLD_MS:
                run_diagnosis()
                # Clear history after diagnosis to reset state
                latencies = []
                print("\n[*] Resume Monitoring...")
                time.sleep(2) # Give system a moment to recover

        time.sleep(0.1)

if __name__ == "__main__":
    main()
