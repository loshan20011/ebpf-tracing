# Use a lightweight Go base
FROM golang:1.22-alpine

WORKDIR /app

# Copy source code
COPY . .

# Build all services with symbol tables PRESERVED (-N -l)
# This fixes the "0x6250f2" hex code issue!
RUN go build -gcflags="-N -l" -o svc-cpu svc-cpu.go
RUN go build -gcflags="-N -l" -o svc-io svc-io.go
RUN go build -gcflags="-N -l" -o svc-mem svc-mem.go
RUN go build -gcflags="-N -l" -o svc-chain svc-chain.go
RUN go build -gcflags="-N -l" -o svc-fanout svc-fanout.go
RUN go build -o gateway gateway.go

# Start gateway by default (we will override this in K8s)
CMD ["./gateway"]
