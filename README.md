<!--- Rewritten README: formatted, structured, and clarified -->
# Kernel-Aware Autoscaling Framework using eBPF 🔬🔧

A system that brings kernel-level visibility to Kubernetes autoscaling decisions. Instead of relying solely on CPU or memory, this framework uses eBPF to distinguish between "working hard" (CPU-bound) and "waiting" (I/O or network-bound) behaviours so autoscaling decisions are smarter and avoid thrashing shared resources.

---

## 1. Problem Statement ❗

Standard Kubernetes Horizontal Pod Autoscalers (HPA) look at high-level metrics such as CPU and memory. These metrics cannot distinguish between a process actively using CPU and a process that is blocked waiting on disk or network. The result: HPA may scale incorrectly (or scale down) while users experience high latency.

This project adds kernel-level observability so the system can tell whether a pod is truly CPU-saturated (scalable) or waiting on shared resources (non-scalable).

---

## 2. High-Level Architecture 🏗️

The system is composed of three layers:

- **Layer 1 — Operating System (Truth Source)**
	- eBPF probes run in the kernel and capture micro-events (function samples, I/O events, schedule switches).

- **Layer 2 — eBPF Agent (Translator)**
	- A privileged Python DaemonSet running on each node. It reads BPF maps, maps cgroups → Pod UIDs, filters noise, and exposes metrics via an HTTP endpoint (default: `http://<node-ip>:5000/`).

- **Layer 3 — Custom Controller (Brain)**
	- A centralized controller polls Agents, applies decision rules, and calls the Kubernetes API to scale workloads when appropriate.

---

## 3. How It Works — Life of a Metric 🔁

1. **Event (Kernel / eBPF)**
	 - Example: `svc-cpu` runs `main.burnCycles`.
	 - A sampler (e.g., `profile:hz:99`) records instruction pointer hits and increments counters in a BPF map.

2. **Translation (Agent)**
	 - The Agent polls BPF maps (e.g., every 2s), sees a cgroup ID with hits, resolves `/sys/fs/cgroup -inum <id>` → Pod UID, filters system pods, and publishes the observation.

3. **Decision (Controller)**
	 - Controller receives events like `{"type":"CPU","detail":"burnCycles","value":50}` and applies rules (e.g., `if type==CPU and value>10 → scale up`) to adjust replica counts via the Kubernetes API.

---

## 4. The “Big Three” Metrics (Research Focus) 🎯

- **On-CPU (CPU Saturation)** — sampled via instruction-pointer profiling (e.g., `profile:hz:99`).
	- Meaning: Pod is doing work. **Decision:** ✅ Scale up (add replicas).

- **Block (Disk I/O Bottleneck)** — traced via block request tracepoints (e.g., `tracepoint:block:block_rq_issue`).
	- Meaning: Pod is waiting on disk. **Decision:** ❌ Do not scale (scaling increases contention).

- **Off-CPU (Network / External Wait)** — measured via schedule switch timings (e.g., `tracepoint:sched:sched_switch`).
	- Meaning: Pod is waiting on external resources (DB, API, locks). **Decision:** ⚠️ Analyze/scale the dependency instead of the waiting pod.

---

## 5. Key Concepts (Plain English) 🧠

- **eBPF:** Safe, in-kernel programs used to observe fine-grained OS events without modifying kernel sources.
- **Cgroups:** Kernel mechanism that groups processes — used to map kernel observations back to Kubernetes Pods.
- **Context Switch:** Used to measure time spent waiting (sleep/wakeup latency).
- **Namespace Filtering:** Agent technique to ignore system namespaces (e.g., `kube-system`) and reduce noise.

---

## 6. Achievements & Milestones ✅

- Migrated from a simulated Docker environment to a real K3s cluster on bare metal.
- Achieved function-level visibility into Go applications (e.g., `main.burnCycles`).
- Implemented a language-aware Agent that avoids monitoring its own overhead.
- Built decision logic to distinguish scalable CPU load from non-scalable I/O waits.

---

## 7. Quick Start & Deployment 🔧

- Agent runs as a privileged **DaemonSet** (`bpf-agent` folder contains `agent.py`, `agent.yaml`, `rbac.yaml`, `Dockerfile`).
- Controller is a central Python process (see `autoscaler_brain.py` / `autoscaler_brain_v2.py`).
- Example: Agent exposes metrics on port `5000`; controller polls those endpoints and interacts with the Kubernetes API using Pod UIDs.

For development, see the repository files and the `deploy.yaml` for cluster resources.

---

## 8. Project Structure 📁

Key files and folders:

```
autoscaler_brain.py
autoscaler_brain_v2.py
deploy.yaml
bpf-agent/
	├─ agent.py
	├─ agent.yaml
	├─ rbac.yaml
	└─ Dockerfile
svc-*/ (sample services used for experiments)
```

---

## 9. Troubleshooting & Notes ⚠️

- Ensure the Agent has the required privileges and RBAC (`bpf-agent/rbac.yaml`) to read Pod metadata.
- Verify kernel supports required tracepoints and eBPF features on your distribution.

> Tip: Run the Agent on a test node first and confirm it serves metrics at `http://<node-ip>:5000/` before deploying the controller.

---

## License

This repository is available for research and educational use. Include your preferred license here.

---

If you'd like, I can also add a short example `kubectl` deployment snippet and a quick diagram showing data flow. 💡