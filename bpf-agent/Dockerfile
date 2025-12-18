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

# Install python libs (Added 'kubernetes')
RUN pip3 install requests numpy kubernetes

# Copy the agent script
COPY agent.py .

# Run the Python agent immediately
CMD ["python3", "-u", "agent.py"]