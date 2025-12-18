// svc-io.go
package main

import (
	"fmt"
	"net/http"
	"os"
)

func handler(w http.ResponseWriter, r *http.Request) {
	fmt.Println("Received request: Writing to Disk...")
	
	// Force a synchronous write to disk
	f, err := os.Create("junk.dat")
	if err != nil { panic(err) }
	
	// Write 50MB of data
	data := make([]byte, 1024*1024) // 1MB chunk
	for i := 0; i < 50; i++ {
		f.Write(data)
		f.Sync() // Force OS to flush to disk (The Bottleneck)
	}
	f.Close()
	
	fmt.Fprintf(w, "I/O Task Done. Wrote 50MB.\n")
}

func main() {
	http.HandleFunc("/", handler)
	fmt.Println("I/O Service running on :8082")
	http.ListenAndServe(":8082", nil)
}
