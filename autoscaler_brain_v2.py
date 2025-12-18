import requests
import time
import subprocess
import re
import numpy as np

GATEWAY_URL = "http://localhost:8080"
SLA_THRESHOLD_MS = 200
BPF_SCRIPT = "diagnose.bt"

# List of our known services to filter noise
TARGET_APPS = ["svc-cpu", "svc-io", "svc-net", "svc-mem", "svc-chain", "svc-fanout"]

def get_latency(endpoint):
    try:
        start = time.time()
        requests.get(f"{GATEWAY_URL}/{endpoint}", timeout=2)
        return (time.time() - start) * 1000
    except:
        return 0

def parse_bpf_output(output):
    """
    Parses bpftrace output to find the 'Top' offender and their Stack Trace.
    """
    analysis = {"critical": False, "type": "UNKNOWN", "victim": "UNKNOWN", "details": ""}

    # 1. CHECK DISK I/O (Looking for @disk_io[svc-name]: count)
    disk_matches = re.findall(r"@disk_io\[(.*?)\]: (\d+)", output)
    relevant_disk = [m for m in disk_matches if m[0] in TARGET_APPS]
    
    if relevant_disk:
        # Sort by count (highest first)
        top_disk = sorted(relevant_disk, key=lambda x: int(x[1]), reverse=True)[0]
        if int(top_disk[1]) > 50:
            analysis["critical"] = True
            analysis["type"] = "DISK_IO_BOTTLENECK"
            analysis["victim"] = top_disk[0]
            analysis["details"] = f"Block Layer Requests: {top_disk[1]}"
            return analysis

    # 2. CHECK CPU (Looking for @cpu_stacks[svc-name, stack]: count)
    # We need to extract the Function Name from the stack block
    
    # Regex explanation:
    # @cpu_stacks[svc-...,  <-- Match header
    # (.*?)                 <-- Capture the stack trace (newlines included)
    # ]: (\d+)              <-- Capture the count
    cpu_blocks = re.findall(r"@cpu_stacks\[(svc-.*?), \n(.*?)\]: (\d+)", output, re.DOTALL)
    
    # UPDATE THIS SECTION
    if cpu_blocks:
        top_cpu = sorted(cpu_blocks, key=lambda x: int(x[2]), reverse=True)[0]
        svc_name = top_cpu[0]
        stack_trace = top_cpu[1]
        count = top_cpu[2]

        culprit_func = "Unknown"
        
        # New Logic: Just grab the first line that isn't 'runtime' or 'bpftrace'
        for line in stack_trace.split("\n"):
            clean = line.strip()
            if not clean: continue
            # Skip Go runtime internals if you want, or just take the top one
            if "runtime." not in clean and "net/http" not in clean:
                culprit_func = clean.split("+")[0]
                break
        
        # If still unknown, just take the absolute top function
        if culprit_func == "Unknown":
             first_line = stack_trace.strip().split("\n")[0]
             culprit_func = first_line.split("+")[0]
        
        analysis["critical"] = True
        analysis["type"] = "CPU_SATURATION"
        analysis["victim"] = svc_name
        analysis["details"] = f"Samples: {count} | Culprit: {culprit_func}"
        return analysis

    return analysis

def run_diagnosis():
    print(f"\n[!] SLA VIOLATION! Running eBPF diagnosis ({BPF_SCRIPT})...")
    cmd = ["sudo", "bpftrace", BPF_SCRIPT]
    try:
        # Run for 5 seconds (script has exit() built-in)
        result = subprocess.run(cmd, capture_output=True, text=True)
        report = parse_bpf_output(result.stdout)
        
        if report["critical"]:
            print("┌──────────────────────────────────────────────────┐")
            print(f"│ 🔴 ROOT CAUSE IDENTIFIED                         │")
            print("├──────────────────────────────────────────────────┤")
            print(f"│ TYPE     : {report['type']:<30}│")
            print(f"│ VICTIM   : {report['victim']:<30}│")
            print(f"│ DETAILS  : {report['details']:<30}│")
            print("└──────────────────────────────────────────────────┘")
        else:
            print("🟢 Diagnosis Inconclusive (Transient Network/External Latency?)")

    except Exception as e:
        print(f"Error running BPF: {e}")

def main():
    print(f"[*] Brain v2 Started. Targets: {TARGET_APPS}")
    
    latencies = []
    window = 10 

    while True:
        # Round-robin check all endpoints
        for svc in ["cpu", "io", "chain", "fanout"]:
            lat = get_latency(svc)
            
            # Simple rolling window
            latencies.append(lat)
            if len(latencies) > window: latencies.pop(0)
            
            p99 = np.percentile(latencies, 99) if latencies else 0
            
            print(f"\rMonitoring... P99: {p99:.0f}ms | Checking: {svc:<8}", end="")

            if p99 > SLA_THRESHOLD_MS:
                run_diagnosis()
                latencies = [] # Reset
                print("[*] Resume Monitoring...")
                time.sleep(2)

        time.sleep(0.1)

if __name__ == "__main__":
    main()