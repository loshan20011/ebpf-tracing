FROM ubuntu:22.04

# Install tools
RUN apt-get update && apt-get install -y \
    bpftrace \
    python3 \
    python3-pip \
    curl \
    kmod \
    linux-headers-generic \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python libs
RUN pip3 install requests numpy kubernetes

# Copy the main autoscaling agent
COPY agent.py .

# --- NEW: Copy the Topology Mapper script ---
COPY topology-agent.py .

# Run the Main Agent by default (Autoscaling)
CMD ["python3", "-u", "agent.py"]