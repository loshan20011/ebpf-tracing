// svc-cpu.go
package main

import (
	"fmt"
	"net/http"
)

// Layer 4: The actual work
func burnCycles(n int) bool {
	if n <= 1 { return false }
	for i := 2; i*i <= n; i++ {
		if n%i == 0 { return false }
	}
	return true
}

// Layer 3
func mathLogic(n int) bool {
	return burnCycles(n)
}

// Layer 2
func businessLogic() int {
	count := 0
	for i := 0; i < 100000; i++ {
		if mathLogic(i) {
			count++
		}
	}
	return count
}

// Layer 1
func processRequest() int {
	return businessLogic()
}

func handler(w http.ResponseWriter, r *http.Request) {
	fmt.Println("Received request: Burning CPU (Deep Stack)...")
	result := processRequest()
	fmt.Fprintf(w, "CPU Task Done. Found %d primes.\n", result)
}

func main() {
	http.HandleFunc("/", handler)
	fmt.Println("CPU Service running on :8081")
	http.ListenAndServe(":8081", nil)
}