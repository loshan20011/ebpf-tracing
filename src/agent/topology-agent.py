import subprocess
import re
import sys
import os

print("[*] Starting Thesis Topology Mapper (Env Var Mode)...", flush=True)

PID_CACHE = {}

def get_source_name(pid):
    # 1. Check Cache
    if pid in PID_CACHE: return PID_CACHE[pid]
    
    try:
        # 2. Read the Environment Variables of the PID
        # This contains HOSTNAME=svc-chain-xyz injected by Kubernetes
        with open(f"/proc/{pid}/environ", "rb") as f:
            # Use 'ignore' to skip binary garbage, env vars are null-separated strings
            content = f.read().decode('utf-8', errors='ignore')
        
        # 3. Look for HOSTNAME
        match = re.search(r'HOSTNAME=([a-zA-Z0-9-]+)', content)
        if match:
            full_name = match.group(1)
            
            # Cleaning Logic: svc-chain-56447d... -> svc-chain
            if "gateway" in full_name.lower() or "traefik" in full_name.lower():
                name = "Gateway"
            else:
                parts = full_name.split("-")
                # If name is long (svc-chain-xyz-123), strip last 2 parts
                if len(parts) > 2:
                    name = "-".join(parts[:-2])
                else:
                    name = full_name
            
            PID_CACHE[pid] = name
            return name
            
        # 4. Fallback for Gateway (System Process without HOSTNAME)
        # We check the command line for 'traefik' or 'k3s'
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode('utf-8', errors='ignore')
            if "traefik" in cmd or "k3s" in cmd:
                PID_CACHE[pid] = "Gateway"
                return "Gateway"

    except Exception:
        pass
    
    return "Unknown"

# --- BPF SCRIPT ---
BPF_SCRIPT = """
#include <linux/in.h>
tracepoint:syscalls:sys_enter_connect {
    $addr = (struct sockaddr_in *)args->uservaddr;
    if ($addr->sin_family == 2) {
        printf("CONN PID:%d DEST:%s\\n", pid, ntop($addr->sin_addr.s_addr));
    }
}
"""

def main():
    # Write BPF
    with open("env.bt", "w") as f: f.write(BPF_SCRIPT)
    
    # Run BPF
    process = subprocess.Popen(["bpftrace", "env.bt"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    
    print("[*] Sniffing Traffic...", flush=True)

    while True:
        line = process.stdout.readline()
        if not line: break
        
        match = re.search(r'CONN PID:(\d+) DEST:([0-9\.]+)', line)
        if match:
            pid = int(match.group(1))
            dest_ip = match.group(2)
            
            # Get Source from Env Var
            source = get_source_name(pid)
            
            if not dest_ip.startswith("127.") and source != "Unknown":
                 print(f"🔗 [GRAPH] {source} --> {dest_ip}", flush=True)

if __name__ == "__main__":
    main()