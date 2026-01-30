package main

import (
	"fmt"
	"net/http"
	"sync"
)

func callService(serviceName string, port string, wg *sync.WaitGroup) {
	defer wg.Done()
	// KUBERNETES FIX: Use service name
	http.Get(fmt.Sprintf("http://%s:%s", serviceName, port))
}

func handler(w http.ResponseWriter, r *http.Request) {
	fmt.Println("Fanout Service: Broadcasting to CPU and IO services...")
	
	var wg sync.WaitGroup
	wg.Add(2)
	
	go callService("svc-cpu", "8081", &wg)
	go callService("svc-io", "8082", &wg)
	
	wg.Wait()
	fmt.Fprintf(w, "Fanout Complete.\n")
}

func main() {
	http.HandleFunc("/", handler)
	fmt.Println("Fanout Service running on :8087")
	http.ListenAndServe(":8087", nil)
}