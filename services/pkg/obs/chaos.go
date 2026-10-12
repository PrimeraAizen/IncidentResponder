package obs

// Internal fault hooks for the test harness (SPEC 5.4):
//
//	POST /admin/chaos {"leak_memory": bool, "leak_goroutines": bool, "cpu_burn": bool}
//	GET  /admin/chaos  -> current flags
//
// Served on a SEPARATE listener (ADMIN_ADDR, default :9000) that is not
// published to the host, so only the harness reaches it (`docker compose exec`).
// Toggling a fault is deliberately NOT logged: the agents must discover the
// fault from its effects (heap, goroutines, latency), not from a log line.

import (
	"crypto/sha256"
	"net/http"
	"os"
	"runtime"
	"runtime/debug"
	"sync"
	"time"
)

type chaos struct {
	mu      sync.Mutex
	running map[string]chan struct{} // fault name -> channel closed to stop it
	leaked  [][]byte                 // memory held by leak_memory
}

func newChaos() *chaos {
	return &chaos{running: map[string]chan struct{}{}}
}

// faults maps each flag to the function that produces it.
func (c *chaos) faults() map[string]func(stop <-chan struct{}) {
	return map[string]func(stop <-chan struct{}){
		"leak_memory":     c.leakMemory,
		"leak_goroutines": leakGoroutines,
		"cpu_burn":        burnCPU,
	}
}

// set starts or stops one fault.
func (c *chaos) set(name string, on bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	stop, running := c.running[name]
	switch {
	case on && !running:
		stop = make(chan struct{})
		c.running[name] = stop
		go c.faults()[name](stop)
	case !on && running:
		close(stop)
		delete(c.running, name)
	}
}

func (c *chaos) state() map[string]bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := map[string]bool{}
	for name := range c.faults() {
		_, out[name] = c.running[name]
	}
	return out
}

// leakMemory keeps 2 MiB/s reachable, so the heap (and RSS) grows until the
// container's memory limit kills the process.
func (c *chaos) leakMemory(stop <-chan struct{}) {
	tick := time.NewTicker(500 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			c.mu.Lock()
			c.leaked = nil
			c.mu.Unlock()
			debug.FreeOSMemory()
			return
		case <-tick.C:
			chunk := make([]byte, 1<<20)
			for i := range chunk {
				chunk[i] = byte(i) // touch every page so it really occupies memory
			}
			c.mu.Lock()
			c.leaked = append(c.leaked, chunk)
			c.mu.Unlock()
		}
	}
}

// leakGoroutines starts 200 goroutines per second that never finish. Each one
// wakes up periodically and does a little work, so CPU usage (and latency of
// real requests) grows with the number of leaked goroutines.
func leakGoroutines(stop <-chan struct{}) {
	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return
		case <-tick.C:
			for i := 0; i < 20; i++ {
				go stuckWorker(stop)
			}
		}
	}
}

func stuckWorker(stop <-chan struct{}) {
	buf := make([]byte, 4096)
	tick := time.NewTicker(20 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return
		case <-tick.C:
			sha256.Sum256(buf)
		}
	}
}

// burnCPU keeps every CPU core busy.
func burnCPU(stop <-chan struct{}) {
	for i := 0; i < runtime.NumCPU(); i++ {
		go func() {
			for {
				select {
				case <-stop:
					return
				default:
					for j := 0; j < 1_000_000; j++ {
					}
				}
			}
		}()
	}
	<-stop
}

// serveAdmin starts the internal admin listener in the background.
func (o *Obs) serveAdmin() {
	addr := os.Getenv("ADMIN_ADDR")
	if addr == "" {
		addr = ":9000"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /admin/chaos", func(w http.ResponseWriter, r *http.Request) {
		WriteJSON(w, http.StatusOK, o.chaos.state())
	})
	mux.HandleFunc("POST /admin/chaos", func(w http.ResponseWriter, r *http.Request) {
		var flags map[string]bool
		if err := ReadJSON(r, &flags); err != nil {
			WriteJSON(w, http.StatusBadRequest, map[string]string{"error": "body must be a JSON object of booleans"})
			return
		}
		known := o.chaos.faults()
		for name, on := range flags {
			if _, ok := known[name]; !ok {
				WriteJSON(w, http.StatusBadRequest, map[string]string{"error": "unknown flag " + name})
				return
			}
			o.chaos.set(name, on)
		}
		WriteJSON(w, http.StatusOK, o.chaos.state())
	})
	go func() { _ = http.ListenAndServe(addr, mux) }()
}
