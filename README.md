# Kernel-Aware Autoscaling Framework using eBPF 🔬🔧

A research-driven framework that brings **kernel-level visibility** to Kubernetes autoscaling. By leveraging eBPF, this system distinguishes between "working hard" (CPU-bound) and "waiting" (I/O or network-bound) behaviors, enabling smarter scaling decisions that standard Horizontal Pod Autoscalers (HPA) cannot achieve.



---

## 1. Problem Statement ❗

Standard Kubernetes HPA relies on high-level metrics like CPU and memory usage. However, high CPU usage metrics can be misleading. They cannot distinguish between:
* **Scalable Load:** A process actively calculating (CPU-saturated).
* **Non-Scalable Wait:** A process blocked waiting on disk I/O, locks, or network responses.

**The Result:** HPA often scales the wrong workloads or fails to scale when needed, leading to resource thrashing or high user latency. This framework solves this by observing the **cause** of the load at the kernel level.

---

## 2. High-Level Architecture 🏗️

The system operates across three distinct layers:

### Layer 1: Operating System (Truth Source)
eBPF probes run directly in the kernel to capture micro-events such as function samples, block I/O requests, and scheduler switches. This layer provides the raw "ground truth."

### Layer 2: eBPF Agent (Translator)
A privileged **DaemonSet** running on every node. Its responsibilities include:
* Reading BPF maps populated by the kernel probes.
* Mapping OS-level identifiers (cgroups) to Kubernetes Pod UIDs.
* Filtering noise (e.g., ignoring `kube-system` pods).
* Exposing aggregated metrics via an HTTP endpoint (Default: `http://<node-ip>:5000/`).

### Layer 3: Custom Controller (Brain)
A centralized decision engine that:
* Polls the Agents for metrics.
* Applies logic to distinguish resource saturation from bottlenecks.
* Executes scaling actions via the Kubernetes API.

---

## 3. The “Big Three” Metrics 🎯

This framework focuses on three specific metric types to drive decisions:

| Metric Type | Measurement Source | Meaning | Autoscaling Decision |
| :--- | :--- | :--- | :--- |
| **On-CPU** | Instruction pointer profiling | Pod is actively processing. | ✅ **Scale Up** (Add Replicas) |
| **Block I/O** | `tracepoint:block:block_rq_issue` | Pod is waiting on disk. | ❌ **Do Not Scale** (Scaling adds contention) |
| **Off-CPU** | `tracepoint:sched:sched_switch` | Pod is waiting on network/locks. | ⚠️ **Analyze Dependency** (Scale the bottleneck, not the waiter) |

---

## 4. Environment Setup (K3s) 🔧

This project is validated on **K3s**. Follow these steps to set up a clean cluster environment on your machine.

### Step 1: Clean Install K3s
If you have an existing installation, it is recommended to uninstall it first to ensure a clean state.

```bash
# 1. Uninstall old version (if applicable)
/usr/local/bin/k3s-uninstall.sh

# 2. Install K3s (Stable Release)
curl -sfL [https://get.k3s.io](https://get.k3s.io) | sh -