package main

import (
    "fmt"
    "io"
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
    fmt.Printf("REQ_ID: %d | Chain Service: Calling CPU Service...\n", currentID)
    
    targetURL := "http://svc-cpu:8081"
    if rawQuery := r.URL.RawQuery; rawQuery != "" {
        targetURL = fmt.Sprintf("%s?%s", targetURL, rawQuery)
    }
    resp, err := http.Get(targetURL)
    if err != nil {
        fmt.Fprintf(w, "Error calling downstream: %s", err)
        return
    }
    defer resp.Body.Close()
    body, _ := io.ReadAll(resp.Body)
    
    duration := time.Since(start)
    // GROUND TRUTH LOG
    fmt.Printf("REQ_ID: %d | APP_INTERNAL_LATENCY: %.3f ms\n", currentID, float64(duration.Microseconds())/1000.0)
    
    fmt.Fprintf(w, "Chain Complete #%d. Downstream said: %s", currentID, body)
}

func main() {
    http.HandleFunc("/", handler)
    fmt.Println("Chain Service (Enhanced) running on :8086")
    http.ListenAndServe(":8086", nil)
}
