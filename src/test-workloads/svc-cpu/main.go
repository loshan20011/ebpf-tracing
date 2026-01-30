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

// Layer 4: The actual work (CPU Intensive)
func burnCycles(n int) bool {
    if n <= 1 { return false }
    for i := 2; i*i <= n; i++ {
        if n%i == 0 { return false }
    }
    return true
}

// Layer 3
func mathLogic(n int) bool {
    return burnCycles(n)
}

// Layer 2
func businessLogic() int {
    count := 0
    for i := 0; i < 50000; i++ { 
        if mathLogic(i) {
            count++
        }
    }
    return count
}

// Layer 1
func processRequest() int {
    return businessLogic()
}

func handler(w http.ResponseWriter, r *http.Request) {
    // 0. INCREMENT REQUEST COUNTER (Thread Safe)
    mu.Lock()
    requestCount++
    currentID := requestCount // Capture current value for logging
    mu.Unlock()

    // 1. START TIMER
    start := time.Now()
    
    // 2. DO WORK
    result := processRequest()
    
    // 3. STOP TIMER
    duration := time.Since(start)
    
    // 4. LOG "GROUND TRUTH"
    // Format: REQUEST_ID | LATENCY
    fmt.Printf("REQ_ID: %d | APP_INTERNAL_LATENCY: %.3f ms\n", currentID, float64(duration.Microseconds())/1000.0)

    // 5. Send Response
    fmt.Fprintf(w, "Request #%d Done. Found %d primes. Took %v\n", currentID, result, duration)
}

func main() {
    http.HandleFunc("/", handler)
    fmt.Println("CPU Service (Go Version) running on :8081")
    http.ListenAndServe(":8081", nil)
}