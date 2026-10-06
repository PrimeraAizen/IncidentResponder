// Payment service: wraps a (mock) external payment provider.
//
// POST /charge simulates the provider call: base latency + jitter and a
// configurable failure rate (config.yaml). It has no dependencies of its own,
// so incidents "in payment" are injected between gateway and payment (Toxiproxy).
package main

import (
	crand "crypto/rand"
	"encoding/hex"
	"math/rand/v2"
	"net/http"
	"os"
	"time"

	"shop/pkg/obs"
)

// Set at build time via -ldflags (see Dockerfile).
var (
	version = "dev"
	commit  = "unknown"
)

type config struct {
	ListenAddr    string  `yaml:"listen_addr"`
	BaseLatencyMS int     `yaml:"base_latency_ms"`
	JitterMS      int     `yaml:"jitter_ms"`
	FailureRate   float64 `yaml:"failure_rate"`
}

type server struct {
	o   *obs.Obs
	cfg config
}

type chargeRequest struct {
	CustomerID int     `json:"customer_id"`
	Amount     float64 `json:"amount"`
}

func main() {
	o := obs.New("payment", version, commit)
	var cfg config
	if err := obs.LoadConfig(&cfg); err != nil {
		o.Log.Error("cannot load config", "error", err.Error())
		os.Exit(1)
	}
	s := &server{o: o, cfg: cfg}

	mux := o.NewMux()
	mux.HandleFunc("POST /charge", s.charge)
	o.Serve(cfg.ListenAddr, mux)
}

func (s *server) charge(w http.ResponseWriter, r *http.Request) {
	var req chargeRequest
	if err := obs.ReadJSON(r, &req); err != nil || req.Amount <= 0 {
		obs.WriteJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid charge request"})
		return
	}

	// Simulated provider round-trip.
	delay := time.Duration(s.cfg.BaseLatencyMS) * time.Millisecond
	if s.cfg.JitterMS > 0 {
		delay += time.Duration(rand.IntN(s.cfg.JitterMS)) * time.Millisecond
	}
	select {
	case <-time.After(delay):
	case <-r.Context().Done():
		return // caller gave up
	}

	if rand.Float64() < s.cfg.FailureRate {
		s.o.Log.ErrorContext(r.Context(), "payment provider error", "upstream", "payment-provider",
			"error", "provider returned 502", "trace_id", obs.TraceID(r.Context()))
		obs.WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "payment provider error"})
		return
	}
	obs.WriteJSON(w, http.StatusOK, map[string]any{
		"charge_id": "ch_" + randomHex(6), "status": "approved", "amount": req.Amount,
	})
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = crand.Read(b)
	return hex.EncodeToString(b)
}
