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

func callService(serviceName string, port string, rawQuery string, wg *sync.WaitGroup) {
    defer wg.Done()
    targetURL := fmt.Sprintf("http://%s:%s", serviceName, port)
    if rawQuery != "" {
        targetURL = fmt.Sprintf("%s?%s", targetURL, rawQuery)
    }
    http.Get(targetURL)
}

func handler(w http.ResponseWriter, r *http.Request) {
    mu.Lock()
    requestCount++
    currentID := requestCount
    mu.Unlock()

    start := time.Now()
    fmt.Printf("REQ_ID: %d | Fanout: CPU & IO...\n", currentID)
    
    var wg sync.WaitGroup
    wg.Add(2)
    
    rawQuery := r.URL.RawQuery
    go callService("svc-cpu", "8081", rawQuery, &wg)
    go callService("svc-io", "8082", rawQuery, &wg)
    
    wg.Wait()
    
    duration := time.Since(start)
    fmt.Printf("REQ_ID: %d | APP_INTERNAL_LATENCY: %.3f ms\n", currentID, float64(duration.Microseconds())/1000.0)
    fmt.Fprintf(w, "Fanout #%d Complete.\n", currentID)
}

func main() {
    http.HandleFunc("/", handler)
    fmt.Println("Fanout Service (Enhanced) running on :8087")
    http.ListenAndServe(":8087", nil)
}
