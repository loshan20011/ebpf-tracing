package main

import (
	"fmt"
	"io"
	"net/http"
)

func handler(w http.ResponseWriter, r *http.Request) {
	fmt.Println("Chain Service: Calling CPU Service...")
	
	// KUBERNETES FIX: Call svc-cpu, not localhost
	resp, err := http.Get("http://svc-cpu:8081")
	if err != nil {
		fmt.Fprintf(w, "Error calling downstream: %s", err)
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	
	fmt.Fprintf(w, "Chain Complete. Downstream said: %s", body)
}

func main() {
	http.HandleFunc("/", handler)
	fmt.Println("Chain Service running on :8086")
	http.ListenAndServe(":8086", nil)
}