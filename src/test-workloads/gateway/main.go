package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"sync"
	"time"
)

var (
    requestCount int
    mu           sync.Mutex
    backendHTTPClient = &http.Client{Timeout: 5 * time.Second}
    truthHTTPClient   = &http.Client{Timeout: 750 * time.Millisecond}
    truthURL          = os.Getenv("AGGREGATOR_TRUTH_URL")
)

type truthRecord struct {
    Service          string  `json:"service"`
    Route            string  `json:"route"`
    TsNs             int64   `json:"ts_ns"`
    StatusCode       int     `json:"status_code"`
    LatencyMS        float64 `json:"latency_ms"`
    Timeout          bool    `json:"timeout"`
    ConnectRefused   bool    `json:"connect_refused"`
    FailureCategory  string  `json:"failure_category,omitempty"`
}

type truthEnvelope struct {
    Records []truthRecord `json:"records"`
}

func callService(url string) (string, int, error, time.Duration) {
    start := time.Now()
    resp, err := backendHTTPClient.Get(url)
    if err != nil {
        return fmt.Sprintf("Error: %s", err), 0, err, time.Since(start)
    }
    defer resp.Body.Close()
    body, _ := io.ReadAll(resp.Body)
    return string(body), resp.StatusCode, nil, time.Since(start)
}

func classifyError(err error) (bool, bool, string) {
    if err == nil {
        return false, false, ""
    }
    if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
        return true, false, "timeout"
    }
    if opErr, ok := err.(*net.OpError); ok {
        if opErr.Err != nil && opErr.Err.Error() == "connect: connection refused" {
            return false, true, "connection_refused"
        }
    }
    return false, false, "request_error"
}

func publishTruthAsync(rec truthRecord) {
    if truthURL == "" {
        return
    }
    go func() {
        payload, err := json.Marshal(truthEnvelope{Records: []truthRecord{rec}})
        if err != nil {
            return
        }
        req, err := http.NewRequest(http.MethodPost, truthURL, bytes.NewReader(payload))
        if err != nil {
            return
        }
        req.Header.Set("Content-Type", "application/json")
        resp, err := truthHTTPClient.Do(req)
        if err != nil {
            return
        }
        defer resp.Body.Close()
        io.Copy(io.Discard, resp.Body)
    }()
}

func handleRequest(w http.ResponseWriter, r *http.Request, serviceName string, port string) {
    mu.Lock()
    requestCount++
    currentID := requestCount
    mu.Unlock()

	totalStart := time.Now()
	url := fmt.Sprintf("http://%s:%s", serviceName, port)
	if rawQuery := r.URL.RawQuery; rawQuery != "" {
		url = fmt.Sprintf("%s?%s", url, rawQuery)
	}
    
    fmt.Printf("GATEWAY_REQ: %d | Routing to %s\n", currentID, serviceName)
    resp, statusCode, err, backendDuration := callService(url)
    
    totalDuration := time.Since(totalStart)
    timeout, connRefused, failureCategory := classifyError(err)
    publishTruthAsync(truthRecord{
        Service:         serviceName,
        Route:           r.URL.Path,
        TsNs:            time.Now().UnixNano(),
        StatusCode:      statusCode,
        LatencyMS:       float64(totalDuration.Microseconds()) / 1000.0,
        Timeout:         timeout,
        ConnectRefused:  connRefused,
        FailureCategory: failureCategory,
    })
    
    // Log Gateway's view of the world
    fmt.Printf("GATEWAY_REQ: %d | BACKEND_LATENCY: %.3f ms | TOTAL_GATEWAY_LATENCY: %.3f ms\n", 
        currentID, 
        float64(backendDuration.Microseconds())/1000.0, 
        float64(totalDuration.Microseconds())/1000.0)

    w.Header().Set("X-Gateway-ID", fmt.Sprintf("%d", currentID))
    if err != nil {
        w.WriteHeader(http.StatusBadGateway)
        fmt.Fprintf(w, resp)
        return
    }
    if statusCode >= 400 {
        w.WriteHeader(statusCode)
    }
    fmt.Fprintf(w, resp)
}

func main() {
    http.HandleFunc("/cpu", func(w http.ResponseWriter, r *http.Request) { handleRequest(w, r, "svc-cpu", "8081") })
    http.HandleFunc("/io",  func(w http.ResponseWriter, r *http.Request) { handleRequest(w, r, "svc-io", "8082") })
    http.HandleFunc("/mem", func(w http.ResponseWriter, r *http.Request) { handleRequest(w, r, "svc-mem", "8083") })
    http.HandleFunc("/net", func(w http.ResponseWriter, r *http.Request) { handleRequest(w, r, "svc-net", "8084") })
    
    http.HandleFunc("/chain", func(w http.ResponseWriter, r *http.Request) { handleRequest(w, r, "svc-chain", "8086") })
    http.HandleFunc("/fanout", func(w http.ResponseWriter, r *http.Request) { handleRequest(w, r, "svc-fanout", "8087") })
    
    log.Printf("Gateway (Enhanced) running on :8080 truth_url=%s", truthURL)
    log.Fatal(http.ListenAndServe(":8080", nil))
}
