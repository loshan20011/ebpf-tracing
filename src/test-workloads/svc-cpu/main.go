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
)

const defaultIterations = 500000

func burnCycles(iterations int) {
	for i := 0; i < iterations; i++ {
		_ = i * i * i
	}
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
	if val > 20000000 {
		return 20000000
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

	burnCycles(iterations)

	duration := time.Since(start)
	ms := float64(duration.Microseconds()) / 1000.0

	fmt.Printf("REQ_ID: %d | ITERATIONS: %d | APP_INTERNAL_LATENCY: %.3f ms\n", currentID, iterations, ms)
	fmt.Fprintf(w, "Request #%d Done. Took %v with %d iterations\n", currentID, duration, iterations)
}

func main() {
	http.HandleFunc("/", handler)
	fmt.Println("CPU Service running on :8081")
	http.ListenAndServe(":8081", nil)
}
