// Order service: stores orders in PostgreSQL and announces them on NATS.
//
// POST /orders runs a fraud check (recent orders of the customer) and the
// insert in one transaction, then publishes `orders.created`.
// GET /orders/{id} reads one order.
// DB and NATS traffic go through Toxiproxy (order→postgres, order→nats) so
// faults can be injected; the pool size comes from config.yaml.
// Database migrations (migrations/*.sql) are applied at startup.
package main

import (
	"context"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"sort"
	"strconv"
	"time"

	"github.com/nats-io/nats.go"

	"shop/pkg/obs"
)

var (
	version = "dev"
	commit  = "unknown"
)

type config struct {
	ListenAddr string       `yaml:"listen_addr"`
	DB         obs.DBConfig `yaml:"db"`
	NATSURL    string       `yaml:"nats_url"`
	Subject    string       `yaml:"subject"` // event published for every new order
	// A customer with more orders than this in the last minute is rejected.
	MaxOrdersPerMinute int `yaml:"max_orders_per_minute"`
}

type server struct {
	o   *obs.Obs
	cfg config
	db  *obs.DB
	nc  *nats.Conn
}

type orderRequest struct {
	CustomerID int     `json:"customer_id"`
	ProductID  int     `json:"product_id"`
	Quantity   int     `json:"quantity"`
	Amount     float64 `json:"amount"`
	ChargeID   string  `json:"charge_id"`
}

type order struct {
	ID         int64     `json:"id"`
	CustomerID int       `json:"customer_id"`
	ProductID  int       `json:"product_id"`
	Quantity   int       `json:"quantity"`
	Amount     float64   `json:"amount"`
	Status     string    `json:"status"`
	CreatedAt  time.Time `json:"created_at"`
}

var errTooManyOrders = errors.New("too many orders")

//go:embed migrations/*.sql
var migrations embed.FS

func main() {
	o := obs.New("order", version, commit)
	var cfg config
	if err := obs.LoadConfig(&cfg); err != nil {
		o.Log.Error("cannot load config", "error", err.Error())
		os.Exit(1)
	}
	db, err := o.NewDB(context.Background(), cfg.DB)
	if err != nil {
		o.Log.Error("invalid database config", "error", err.Error())
		os.Exit(1)
	}
	migrate(o, db)
	nc, err := o.ConnectNATS(cfg.NATSURL)
	if err != nil {
		o.Log.Error("invalid nats config", "error", err.Error())
		os.Exit(1)
	}
	s := &server{o: o, cfg: cfg, db: db, nc: nc}

	mux := o.NewMux()
	mux.HandleFunc("POST /orders", s.createOrder)
	mux.HandleFunc("GET /orders/{id}", s.getOrder)
	o.Serve(cfg.ListenAddr, mux)
}

func (s *server) createOrder(w http.ResponseWriter, r *http.Request) {
	var req orderRequest
	if err := obs.ReadJSON(r, &req); err != nil || req.CustomerID <= 0 || req.ProductID <= 0 || req.Quantity <= 0 {
		obs.WriteJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid order"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), s.cfg.DB.QueryTimeout())
	defer cancel()

	o := order{CustomerID: req.CustomerID, ProductID: req.ProductID, Quantity: req.Quantity, Amount: req.Amount, Status: "confirmed"}
	err := s.db.InTx(ctx, func(tx *obs.Tx) error {
		// Fraud check: uses the (customer_id, created_at) index on orders.
		var recent int
		if err := tx.QueryRow(ctx, "recent_orders",
			`SELECT count(*) FROM orders WHERE customer_id = $1 AND created_at > now() - interval '1 minute'`,
			[]any{req.CustomerID}, &recent); err != nil {
			return err
		}
		if recent >= s.cfg.MaxOrdersPerMinute {
			return errTooManyOrders
		}
		return tx.QueryRow(ctx, "insert_order",
			`INSERT INTO orders (customer_id, product_id, quantity, amount, charge_id, status)
			 VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, created_at`,
			[]any{o.CustomerID, o.ProductID, o.Quantity, o.Amount, req.ChargeID, o.Status}, &o.ID, &o.CreatedAt)
	})
	if errors.Is(err, errTooManyOrders) {
		obs.WriteJSON(w, http.StatusTooManyRequests, map[string]string{"error": "too many orders"})
		return
	}
	if err != nil {
		s.dbError(w, r, "create_order", err)
		return
	}
	s.publishCreated(r.Context(), o)
	obs.WriteJSON(w, http.StatusCreated, o)
}

// publishCreated announces the order. A failure is logged but does not fail
// the request: the order is already stored.
func (s *server) publishCreated(ctx context.Context, o order) {
	payload, _ := json.Marshal(o)
	if err := s.nc.Publish(s.cfg.Subject, payload); err != nil {
		obs.EventsPublished.WithLabelValues("order", "error").Inc()
		s.o.Log.ErrorContext(ctx, "event publish failed", "upstream", "nats", "subject", s.cfg.Subject,
			"error", err.Error(), "trace_id", obs.TraceID(ctx))
		return
	}
	obs.EventsPublished.WithLabelValues("order", "ok").Inc()
}

func (s *server) getOrder(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		obs.WriteJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), s.cfg.DB.QueryTimeout())
	defer cancel()

	var o order
	err = s.db.QueryRow(ctx, "get_order",
		`SELECT id, customer_id, product_id, quantity, amount, status, created_at FROM orders WHERE id = $1`,
		[]any{id}, &o.ID, &o.CustomerID, &o.ProductID, &o.Quantity, &o.Amount, &o.Status, &o.CreatedAt)
	if errors.Is(err, obs.ErrNoRows) {
		obs.WriteJSON(w, http.StatusNotFound, map[string]string{"error": "order not found"})
		return
	}
	if err != nil {
		s.dbError(w, r, "get_order", err)
		return
	}
	obs.WriteJSON(w, http.StatusOK, o)
}

// migrate applies every migrations/*.sql file in name order. Migrations are
// written to be idempotent (IF NOT EXISTS / IF EXISTS), so each start re-applies
// them safely. The database may still be starting, so failures are retried for ~1 min.
func migrate(o *obs.Obs, db *obs.DB) {
	files, _ := fs.Glob(migrations, "migrations/*.sql")
	sort.Strings(files)
	for attempt := 1; attempt <= 30; attempt++ {
		err := applyMigrations(o, db, files)
		if err == nil {
			return
		}
		o.Log.Warn("migrations not applied yet, retrying", "upstream", "postgres", "attempt", attempt,
			"error", err.Error())
		time.Sleep(2 * time.Second)
	}
	o.Log.Error("migrations failed, continuing without them", "upstream", "postgres")
}

func applyMigrations(o *obs.Obs, db *obs.DB, files []string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	for _, file := range files {
		sql, _ := migrations.ReadFile(file)
		if _, err := db.Exec(ctx, "migration", string(sql)); err != nil {
			return fmt.Errorf("%s: %w", file, err)
		}
		o.Log.Info("migration applied", "file", file)
	}
	return nil
}

// dbError logs the failure with upstream=postgres and returns 503.
func (s *server) dbError(w http.ResponseWriter, r *http.Request, query string, err error) {
	s.o.Log.ErrorContext(r.Context(), "database query failed", "upstream", "postgres", "query", query,
		"error", err.Error(), "trace_id", obs.TraceID(r.Context()))
	obs.WriteJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "database unavailable"})
}
