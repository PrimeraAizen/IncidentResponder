// Load generator: steady background traffic against the gateway.
//
// The agents need "normal" traffic to see a baseline and then a change when a
// fault is injected, exactly like production. Runs as a compose service forever
// (--duration 0) and can also be run once with a higher --rps for load-spike scenarios.
//
// --mix weights the request types, e.g. "products=60,create=30,get=10".
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math/rand/v2"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

type stats struct {
	mu     sync.Mutex
	counts map[string]int // "<kind> <status>" -> count
}

func (s *stats) add(kind string, status int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.counts[kind+" "+strconv.Itoa(status)]++
}

func (s *stats) flush() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out, _ := json.Marshal(s.counts)
	s.counts = map[string]int{}
	return string(out)
}

func main() {
	target := flag.String("target", "http://localhost:8080", "gateway base URL")
	rps := flag.Float64("rps", 10, "requests per second")
	duration := flag.Duration("duration", 0, "how long to run (0 = until stopped)")
	mixFlag := flag.String("mix", "products=60,create=30,get=10", "request mix weights")
	flag.Parse()

	mix, err := parseMix(*mixFlag)
	if err != nil {
		log.Fatalf("bad --mix: %v", err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if *duration > 0 {
		ctx, stop = context.WithTimeout(ctx, *duration)
		defer stop()
	}

	client := &http.Client{Timeout: 10 * time.Second}
	st := &stats{counts: map[string]int{}}
	var lastOrderID atomic.Int64 // most recent created order, used by "get"
	var wg sync.WaitGroup

	ticker := time.NewTicker(time.Duration(float64(time.Second) / *rps))
	defer ticker.Stop()
	report := time.NewTicker(30 * time.Second)
	defer report.Stop()
	log.Printf("loadgen: target=%s rps=%.1f mix=%s duration=%s", *target, *rps, *mixFlag, *duration)

	for {
		select {
		case <-ctx.Done():
			wg.Wait()
			log.Printf("loadgen done: %s", st.flush())
			return
		case <-report.C:
			log.Printf("last 30s: %s", st.flush())
		case <-ticker.C:
			kind := pick(mix)
			wg.Add(1)
			go func() {
				defer wg.Done()
				st.add(kind, send(client, *target, kind, &lastOrderID))
			}()
		}
	}
}

// send performs one request of the given kind and returns the HTTP status (0 on network error).
func send(c *http.Client, target, kind string, lastOrderID *atomic.Int64) int {
	var req *http.Request
	switch kind {
	case "products":
		req, _ = http.NewRequest("GET", target+"/api/products", nil)
	case "create":
		body, _ := json.Marshal(map[string]int{
			"customer_id": 1 + rand.IntN(500), "product_id": 1 + rand.IntN(20), "quantity": 1 + rand.IntN(3),
		})
		req, _ = http.NewRequest("POST", target+"/api/orders", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
	case "get":
		id := lastOrderID.Load()
		if id == 0 {
			id = 1 + rand.Int64N(1000) // seeded historical orders
		}
		req, _ = http.NewRequest("GET", fmt.Sprintf("%s/api/orders/%d", target, id), nil)
	}
	resp, err := c.Do(req)
	if err != nil {
		return 0
	}
	defer resp.Body.Close()
	if kind == "create" && resp.StatusCode == http.StatusCreated {
		var o struct {
			ID int64 `json:"id"`
		}
		if json.NewDecoder(resp.Body).Decode(&o) == nil && o.ID > 0 {
			lastOrderID.Store(o.ID)
		}
	}
	return resp.StatusCode
}

type weighted struct {
	kind   string
	weight int
}

func parseMix(s string) ([]weighted, error) {
	var mix []weighted
	for _, part := range strings.Split(s, ",") {
		k, v, ok := strings.Cut(strings.TrimSpace(part), "=")
		w, err := strconv.Atoi(v)
		if !ok || err != nil || w < 0 || (k != "products" && k != "create" && k != "get") {
			return nil, fmt.Errorf("invalid entry %q", part)
		}
		mix = append(mix, weighted{k, w})
	}
	return mix, nil
}

func pick(mix []weighted) string {
	total := 0
	for _, m := range mix {
		total += m.weight
	}
	n := rand.IntN(total)
	for _, m := range mix {
		if n < m.weight {
			return m.kind
		}
		n -= m.weight
	}
	return mix[0].kind
}
