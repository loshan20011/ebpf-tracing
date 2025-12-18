// svc-net.go
package main

import (
	"fmt"
	"net/http"
)

func handler(w http.ResponseWriter, r *http.Request) {
	fmt.Println("Received request: Calling Google...")
	
	// This causes latency, but it is NETWORK latency, not DISK.
	resp, err := http.Get("https://www.google.com")
	if err != nil {
		fmt.Fprintf(w, "Error: %s\n", err)
		return
	}
	defer resp.Body.Close()
	
	fmt.Fprintf(w, "Network Task Done. Status: %s\n", resp.Status)
}

func main() {
	http.HandleFunc("/", handler)
	fmt.Println("Network Service running on :8085")
	http.ListenAndServe(":8085", nil)
}
