package main

import (
	"fmt"
	"net/http"
	"os"
)

func handler(w http.ResponseWriter, r *http.Request) {
	fmt.Println("Received request: Writing to Disk...")

	// Write to /tmp because the container filesystem is read-only for non-root users
	f, err := os.Create("/tmp/junk.dat") 
	if err != nil {
		fmt.Printf("Error creating file: %v\n", err)
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	
	// Write junk data
	data := make([]byte, 1024*1024) // 1MB
	for i := 0; i < 100; i++ {      // Write 100MB
		f.Write(data)
	}
	f.Close()
	
	// Clean up to save space
	os.Remove("/tmp/junk.dat") 

	w.Write([]byte("I/O Stress Test Done\n"))
}

func main() {
	http.HandleFunc("/", handler)
	fmt.Println("I/O Service running on :8082")
	http.ListenAndServe(":8082", nil)
}
