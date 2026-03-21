package main

import (
    "fmt"
    "net/http"
    "sync"
    "time"
)

// GLOBAL COUNTER
var (
    requestCount int
    mu           sync.Mutex // Protects the counter from concurrent writes
)

func handler(w http.ResponseWriter, r *http.Request) {
    mu.Lock()
    requestCount++
    currentID := requestCount
    mu.Unlock()

    start := time.Now()

    // Keep the I/O path near the SLO edge so contention and scaling matter.
    time.Sleep(80 * time.Millisecond)

    duration := time.Since(start)

    fmt.Printf("REQ_ID: %d | APP_INTERNAL_LATENCY: %.3f ms\n", currentID, float64(duration.Microseconds())/1000.0)
    w.Write([]byte(fmt.Sprintf("I/O Stress Test #%d Done. Took %v\n", currentID, duration)))
}

func main() {
    http.HandleFunc("/", handler)
    fmt.Println("I/O Service (Enhanced) running on :8082")
    http.ListenAndServe(":8082", nil)
}
