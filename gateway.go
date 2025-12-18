package main

import (
	"fmt"
	"io"
	"net/http"
	"time"
)

func callService(url string) (string, time.Duration) {
	start := time.Now()
	resp, err := http.Get(url)
	if err != nil {
		return fmt.Sprintf("Error: %s", err), time.Since(start)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return string(body), time.Since(start)
}

func handleRequest(w http.ResponseWriter, r *http.Request, serviceName string, port string) {
	// KUBERNETES FIX: Use service name instead of localhost
	url := fmt.Sprintf("http://%s:%s", serviceName, port)
	
	resp, duration := callService(url)
	w.Header().Set("X-Backend-Latency", fmt.Sprintf("%d", duration.Milliseconds()))
	fmt.Fprintf(w, resp)
}

func main() {
	// Update routes to pass Service Names
	http.HandleFunc("/cpu", func(w http.ResponseWriter, r *http.Request) { handleRequest(w, r, "svc-cpu", "8081") })
	http.HandleFunc("/io",  func(w http.ResponseWriter, r *http.Request) { handleRequest(w, r, "svc-io", "8082") })
	http.HandleFunc("/mem", func(w http.ResponseWriter, r *http.Request) { handleRequest(w, r, "svc-mem", "8083") })
	http.HandleFunc("/chain", func(w http.ResponseWriter, r *http.Request) { handleRequest(w, r, "svc-chain", "8086") })
	http.HandleFunc("/fanout", func(w http.ResponseWriter, r *http.Request) { handleRequest(w, r, "svc-fanout", "8087") })
    
	fmt.Println("Gateway running on :8080")
	http.ListenAndServe(":8080", nil)
}
