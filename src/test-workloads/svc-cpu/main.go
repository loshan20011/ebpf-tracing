package main

import (
	"fmt"
	"net/http"
	"strconv"
	"sync"
	"time"
)

var (
	requestCount int
	mu           sync.Mutex
	sink         uint64
)

const (
	defaultIterations = 500000
	maxIterations     = 100000000
)

func burnCycles(iterations int) uint64 {
	var acc uint64 = 1
	for i := 0; i < iterations; i++ {
		x := uint64(i + 1)
		acc += (x * x) ^ (x << 3)
		acc ^= (acc >> 7)
	}
	return acc
}

func parseIterations(r *http.Request) int {
	raw := r.URL.Query().Get("count")
	if raw == "" {
		return defaultIterations
	}
	val, err := strconv.Atoi(raw)
	if err != nil || val <= 0 {
		return defaultIterations
	}
	if val > maxIterations {
		return maxIterations
	}
	return val
}

func handler(w http.ResponseWriter, r *http.Request) {
	mu.Lock()
	requestCount++
	currentID := requestCount
	mu.Unlock()

	start := time.Now()
	iterations := parseIterations(r)

	sink = burnCycles(iterations)

	duration := time.Since(start)
	ms := float64(duration.Microseconds()) / 1000.0

	fmt.Printf("REQ_ID: %d | ITERATIONS: %d | APP_INTERNAL_LATENCY: %.3f ms | SINK: %d\n", currentID, iterations, ms, sink)
	fmt.Fprintf(w, "Request #%d Done. Took %v with %d iterations (sink=%d)\n", currentID, duration, iterations, sink)
}

func main() {
	http.HandleFunc("/", handler)
	fmt.Println("CPU Service running on :8081")
	http.ListenAndServe(":8081", nil)
}
