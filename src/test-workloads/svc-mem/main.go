package main

import (
    "fmt"
    "net/http"
    "sync"
    "time"
)

var (
    requestCount int
    mu           sync.Mutex
    memoryHog    [][]byte // The leak
)

func handler(w http.ResponseWriter, r *http.Request) {
    mu.Lock()
    requestCount++
    currentID := requestCount
    mu.Unlock()

    start := time.Now()
    fmt.Printf("REQ_ID: %d | Memory Service: Allocating 100MB...\n", currentID)
    
    // Allocate 10MB chunks repeatedly
    for i := 0; i < 10; i++ {
        chunk := make([]byte, 10*1024*1024) 
        memoryHog = append(memoryHog, chunk)
        time.Sleep(5 * time.Millisecond) 
    }
    
    duration := time.Since(start)
    fmt.Printf("REQ_ID: %d | APP_INTERNAL_LATENCY: %.3f ms\n", currentID, float64(duration.Microseconds())/1000.0)
    
    fmt.Fprintf(w, "Mem Stress #%d Done. Total Chunks: %d\n", currentID, len(memoryHog))
}

func main() {
    http.HandleFunc("/", handler)
    fmt.Println("Memory Service (Enhanced) running on :8083")
    http.ListenAndServe(":8083", nil)
}