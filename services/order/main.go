// Order service: stores orders in PostgreSQL.
//
// POST /orders  — fraud check (recent orders of the customer) + insert.
// GET  /orders/{id}
// All DB access goes through Toxiproxy (order→postgres) so DB faults can be
// injected; the pool size comes from config.yaml (resource-exhaustion scenario).
package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"time"

	"shop/pkg/obs"
)

var (
	version = "dev"
	commit  = "unknown"
)

type config struct {
	ListenAddr string `yaml:"listen_addr"`
	DB         struct {
		Host           string `yaml:"host"`
		Port           int    `yaml:"port"`
		Name           string `yaml:"name"`
		User           string `yaml:"user"`
		MaxConns       int32  `yaml:"max_conns"`
		QueryTimeoutMS int    `yaml:"query_timeout_ms"`
	} `yaml:"db"`
	// A customer with more orders than this in the last minute is rejected.
	MaxOrdersPerMinute int `yaml:"max_orders_per_minute"`
}

type server struct {
	o   *obs.Obs
	cfg config
	db  *obs.DB
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

func main() {
	o := obs.New("order", version, commit)
	var cfg config
	if err := obs.LoadConfig(&cfg); err != nil {
		o.Log.Error("cannot load config", "error", err.Error())
		os.Exit(1)
	}
	dsn := fmt.Sprintf("postgres://%s:%s@%s:%d/%s?sslmode=disable&connect_timeout=3",
		cfg.DB.User, os.Getenv("POSTGRES_PASSWORD"), cfg.DB.Host, cfg.DB.Port, cfg.DB.Name)
	db, err := o.NewDB(context.Background(), dsn, cfg.DB.MaxConns)
	if err != nil {
		o.Log.Error("invalid database config", "error", err.Error())
		os.Exit(1)
	}
	s := &server{o: o, cfg: cfg, db: db}

	mux := o.NewMux()
	mux.HandleFunc("POST /orders", s.createOrder)
	mux.HandleFunc("GET /orders/{id}", s.getOrder)
	o.Serve(cfg.ListenAddr, mux)
}

func (s *server) queryCtx(r *http.Request) (context.Context, context.CancelFunc) {
	return context.WithTimeout(r.Context(), time.Duration(s.cfg.DB.QueryTimeoutMS)*time.Millisecond)
}

func (s *server) createOrder(w http.ResponseWriter, r *http.Request) {
	var req orderRequest
	if err := obs.ReadJSON(r, &req); err != nil || req.CustomerID <= 0 || req.ProductID <= 0 || req.Quantity <= 0 {
		obs.WriteJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid order"})
		return
	}
	ctx, cancel := s.queryCtx(r)
	defer cancel()

	// Fraud check: uses the (customer_id, created_at) index on orders.
	var recent int
	err := s.db.QueryRow(ctx, "recent_orders",
		`SELECT count(*) FROM orders WHERE customer_id = $1 AND created_at > now() - interval '1 minute'`,
		[]any{req.CustomerID}, &recent)
	if err != nil {
		s.dbError(w, r, "recent_orders", err)
		return
	}
	if recent >= s.cfg.MaxOrdersPerMinute {
		obs.WriteJSON(w, http.StatusTooManyRequests, map[string]string{"error": "too many orders"})
		return
	}

	o := order{CustomerID: req.CustomerID, ProductID: req.ProductID, Quantity: req.Quantity, Amount: req.Amount, Status: "confirmed"}
	err = s.db.QueryRow(ctx, "insert_order",
		`INSERT INTO orders (customer_id, product_id, quantity, amount, charge_id, status)
		 VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, created_at`,
		[]any{o.CustomerID, o.ProductID, o.Quantity, o.Amount, req.ChargeID, o.Status}, &o.ID, &o.CreatedAt)
	if err != nil {
		s.dbError(w, r, "insert_order", err)
		return
	}
	obs.WriteJSON(w, http.StatusCreated, o)
}

func (s *server) getOrder(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		obs.WriteJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	ctx, cancel := s.queryCtx(r)
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

// dbError logs the failure with upstream=postgres and returns 503.
func (s *server) dbError(w http.ResponseWriter, r *http.Request, query string, err error) {
	s.o.Log.ErrorContext(r.Context(), "database query failed", "upstream", "postgres", "query", query,
		"error", err.Error(), "trace_id", obs.TraceID(r.Context()))
	obs.WriteJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "database unavailable"})
}
