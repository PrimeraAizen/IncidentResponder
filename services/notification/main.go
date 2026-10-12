// Notification service: consumes `orders.created` events from NATS and sends
// the customer a confirmation (simulated: one log line per notification).
// It has no business HTTP API, only /healthz and /metrics.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/nats-io/nats.go"

	"shop/pkg/obs"
)

var (
	version = "dev"
	commit  = "unknown"
)

type config struct {
	ListenAddr string `yaml:"listen_addr"`
	NATSURL    string `yaml:"nats_url"`
	Subject    string `yaml:"subject"`
	Template   string `yaml:"template"` // placeholders: {order_id}, {amount}
}

type orderEvent struct {
	ID         int64   `json:"id"`
	CustomerID int     `json:"customer_id"`
	Amount     float64 `json:"amount"`
}

func main() {
	o := obs.New("notification", version, commit)
	var cfg config
	if err := obs.LoadConfig(&cfg); err != nil {
		o.Log.Error("cannot load config", "error", err.Error())
		os.Exit(1)
	}
	nc, err := o.ConnectNATS(cfg.NATSURL)
	if err != nil {
		o.Log.Error("invalid nats config", "error", err.Error())
		os.Exit(1)
	}
	if _, err := nc.Subscribe(cfg.Subject, func(m *nats.Msg) { notify(o, cfg, m) }); err != nil {
		o.Log.Error("cannot subscribe", "upstream", "nats", "subject", cfg.Subject, "error", err.Error())
		os.Exit(1)
	}
	o.Serve(cfg.ListenAddr, o.NewMux())
}

func notify(o *obs.Obs, cfg config, m *nats.Msg) {
	obs.EventsConsumed.WithLabelValues("notification").Inc()
	var ev orderEvent
	if err := json.Unmarshal(m.Data, &ev); err != nil {
		o.Log.Error("invalid event", "subject", m.Subject, "error", err.Error())
		return
	}
	text := strings.NewReplacer("{order_id}", fmt.Sprint(ev.ID), "{amount}", fmt.Sprintf("%.2f", ev.Amount)).
		Replace(cfg.Template)
	o.Log.Info("notification sent", "order_id", ev.ID, "customer_id", ev.CustomerID, "chars", len(text))
}
