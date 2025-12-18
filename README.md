Project Title: Kernel-Aware Autoscaling Framework using eBPF
1. The Core Problem
Why doesn't Kubernetes work like this out of the box?

Standard Kubernetes Autoscalers (HPA) act like a simple thermometer. They only check:

CPU: Is the processor hot?

Memory: Is the RAM full?

The Blind Spot: Imagine a database is writing to a slow hard drive. The application freezes waiting for the disk.

CPU Usage: Drops to 0% (because the app is waiting, not working).

Kubernetes HPA says: "CPU is low, everything is fine!" or even "Let's scale down!"

The Reality: Users are facing 10-second latency.

Your Solution: Your framework acts like an MRI Scanner using eBPF. It looks inside the Operating System Kernel to see why an application is slow. It distinguishes between "Working Hard" (CPU) and "Waiting" (Disk/Network).

2. High-Level Architecture
Your system consists of three distinct layers.

Layer 1: The Operating System (The Truth Source)
This is where the actual code executes. We use eBPF (Extended Berkeley Packet Filter) to hook into tiny events inside the Linux Kernel.

Location: Runs safely inside the Linux Kernel.

Role: The "Spy." It watches every function call, disk write, and network packet without slowing down the system.

Layer 2: The eBPF Agent (The Translator)
This is the Python program running as a DaemonSet (one per node).

Location: A Privileged Pod on every node.

Role: The "Interpreter." It takes raw numbers from the Kernel (e.g., "Cgroup 12345 is waiting") and translates them into Kubernetes concepts (e.g., "Pod svc-io is disk-bound").

Layer 3: The Custom Controller (The Brain)
This is a centralized Python script.

Location: A standard Deployment.

Role: The "Decision Maker." It polls the Agent and decides whether to scale up, scale down, or do nothing based on context.

3. How It Works: The "Life of a Metric"
Here is the step-by-step flow of how your system detects a bottleneck.

Step A: The Event (Inside the Kernel)
A microservice (e.g., svc-cpu) starts a heavy calculation.

Execution: The CPU executes the function main.burnCycles.

eBPF Hook: Your BPF script running at profile:hz:99 wakes up 99 times a second.

Capture: It records:

Who: The Cgroup ID (e.g., 45218).

What: The function name (main.burnCycles).

Action: Increments a counter in a BPF Map (a super-fast in-memory hash table).

Step B: The Translation (Inside the Agent)
Every 2 seconds, your Python Agent wakes up and reads the BPF Map.

Reading: It sees Cgroup 45218 has 50 CPU hits.

Mapping (The "Rosetta Stone"): The Kernel only knows "Cgroups." Kubernetes only knows "Pods."

The Agent runs find /sys/fs/cgroup -inum 45218.

It finds the path: .../kubepods-pod<UID>....

It extracts the Pod UID.

Filtering: It checks ALLOWED_POD_UIDS (from the K8s API) to ensure we ignore system noise (like coredns or the Agent itself).

Publishing: It exposes this data at http://<node-ip>:5000/.

Step C: The Decision (Inside the Controller)
The Controller polls the Agent's API.

Input: It receives {"type": "CPU", "detail": "burnCycles", "value": 50} for svc-cpu.

Logic Rule: "If Type is CPU and Value > 10 → SCALE UP."

Action: It calls the Kubernetes API to change replicas: 1 to replicas: 2.

4. The "Big Three" Metrics (Your Research Novelty)
This is the technical core of your thesis. You are not just counting usage; you are categorizing behavior.

1. CPU Saturation (The "On-CPU" Metric)
How we track it: Sampling the instruction pointer 99 times/sec (profile:hz:99).

What it means: The app is processing logic (Math, JSON parsing, Encryption).

Auto-scaling Decision: ✅ SCALE UP. Adding more replicas splits the workload.

2. Disk I/O Bottleneck (The "Block" Metric)
How we track it: Hooking into the disk request queue (tracepoint:block:block_rq_issue).

What it means: The app is waiting for the hard drive to write data.

Auto-scaling Decision: ❌ DO NOT SCALE.

Why? The hard drive is a shared physical resource. If one pod is clogging the disk, adding more pods will just clog it faster. This prevents "Resource Thrashing."

3. Network/External Wait (The "Off-CPU" Metric)
How we track it: Measuring the time difference between a process going to sleep and waking up (tracepoint:sched:sched_switch).

What it means: The process is idle, waiting for something else (a database response, an API call, or a mutex lock).

Auto-scaling Decision: ⚠️ Analyze Dependency.

If svc-chain is waiting, scaling svc-chain won't help. We need to scale the downstream service (svc-cpu) that acts as the bottleneck.

5. Key Technical Concepts Simplified
Use these definitions in your report/viva:

eBPF (Extended Berkeley Packet Filter): A technology that allows us to run sandboxed programs inside the Linux kernel without changing kernel source code or crashing the system. It's like adding a plugin to the OS kernel at runtime.

Cgroups (Control Groups): The Linux feature that isolates processes. Kubernetes uses Cgroups to create the "walls" of a container. We use the Cgroup ID to link kernel events back to specific containers.

Context Switch: The moment the CPU stops working on Task A and starts working on Task B. We measure the time between switches to calculate Latency.

Namespace Filtering: A technique we implemented in the Agent to ignore system noise (kube-system) and focus only on the user's application (default namespace).

6. Summary of Achieved Milestones
Environment: Successfully moved from a simulated Docker environment to a real K3s Kubernetes cluster on bare-metal Linux.

Visibility: Achieved "X-Ray Vision" into pods. We can see exactly which Go function (main.burnCycles) is causing lag.

Safety: Implemented a "Language-Safe" Agent that monitors Python/Go apps but ignores its own overhead to prevent feedback loops.

Intelligence: The system can now mathematically differentiate between a slow CPU (scalable) and a slow Disk (non-scalable).