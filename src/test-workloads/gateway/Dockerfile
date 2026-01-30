# Build Stage
FROM golang:1.22-alpine AS builder
WORKDIR /app
# Initialize module if not exists (prevents build errors)
RUN [ -f go.mod ] || go mod init gateway
COPY main.go .
# Build static binary
RUN CGO_ENABLED=0 GOOS=linux go build -o app main.go

# Run Stage (Distroless for smallest size)
FROM gcr.io/distroless/static:nonroot
WORKDIR /
COPY --from=builder /app/app .
USER 65532:65532
ENTRYPOINT ["/app"]
