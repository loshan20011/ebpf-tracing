// svc-mem.go
package main

import (
	"fmt"
	"net/http"
	"time"
)

// A global slice that never gets cleaned up (THE LEAK)
var memoryHog [][]byte

func handler(w http.ResponseWriter, r *http.Request) {
	fmt.Println("Received request: Leaking Memory...")
	
	// Allocate 10MB chunks repeatedly
	for i := 0; i < 10; i++ {
		chunk := make([]byte, 10*1024*1024) 
		memoryHog = append(memoryHog, chunk)
		time.Sleep(10 * time.Millisecond) // Simulate slow growth
	}
	
	fmt.Fprintf(w, "Leaked 100MB. Total chunks: %d\n", len(memoryHog))
}

func main() {
	http.HandleFunc("/", handler)
	fmt.Println("Memory Service running on :8083")
	http.ListenAndServe(":8083", nil)
}
