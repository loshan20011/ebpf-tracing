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
)

func handler(w http.ResponseWriter, r *http.Request) {
    mu.Lock()
    requestCount++
    currentID := requestCount
    mu.Unlock()

    start := time.Now()
    fmt.Printf("REQ_ID: %d | Network Service: Calling Google...\n", currentID)
    
    // External call to test egress tracking
    resp, err := http.Get("https://www.google.com")
    if err != nil {
        fmt.Fprintf(w, "Error calling external: %s\n", err)
        return
    }
    defer resp.Body.Close()
    
    duration := time.Since(start)
    fmt.Printf("REQ_ID: %d | APP_INTERNAL_LATENCY: %.3f ms\n", currentID, float64(duration.Microseconds())/1000.0)
    
    fmt.Fprintf(w, "Net Task #%d Done. Google Status: %s\n", currentID, resp.Status)
}

func main() {
    http.HandleFunc("/", handler)
    // MATCHING KUBERNETES SERVICE PORT: 8084
    fmt.Println("Network Service (Enhanced) running on :8084")
    http.ListenAndServe(":8084", nil)
}