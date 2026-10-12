// Inventory service: product catalogue and stock reservation.
//
// GET /products is cache-aside: Redis first; on a miss or when Redis is
// unavailable it reads PostgreSQL, which is slower because it also counts
// each product's orders of the last 7 days.
// POST /reserve decrements stock in PostgreSQL and returns the unit price.
// Redis and Postgres are reached through Toxiproxy (inventory→redis, inventory→postgres).
package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"time"

	"github.com/redis/go-redis/v9"

	"shop/pkg/obs"
)

var (
	version = "dev"
	commit  = "unknown"
)

const cacheKey = "inventory:products:v1"

type config struct {
	ListenAddr string       `yaml:"listen_addr"`
	DB         obs.DBConfig `yaml:"db"`
	Redis      struct {
		Addr       string `yaml:"addr"`
		TTLSeconds int    `yaml:"ttl_s"` // how long the product list stays cached
	} `yaml:"redis"`
}

type server struct {
	o     *obs.Obs
	cfg   config
	db    *obs.DB
	cache *redis.Client
}

type product struct {
	ID       int     `json:"id"`
	Name     string  `json:"name"`
	Price    float64 `json:"price"`
	Stock    int     `json:"stock"`
	Orders7d int     `json:"orders_7d"`
}

func main() {
	o := obs.New("inventory", version, commit)
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
	cache := redis.NewClient(&redis.Options{Addr: cfg.Redis.Addr, DialTimeout: time.Second,
		ReadTimeout: 500 * time.Millisecond, WriteTimeout: 500 * time.Millisecond})
	o.AddHealthCheck("redis", func(ctx context.Context) error { return cache.Ping(ctx).Err() })
	s := &server{o: o, cfg: cfg, db: db, cache: cache}

	mux := o.NewMux()
	mux.HandleFunc("GET /products", s.listProducts)
	mux.HandleFunc("POST /reserve", s.reserve)
	o.Serve(cfg.ListenAddr, mux)
}

func (s *server) listProducts(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), s.cfg.DB.QueryTimeout())
	defer cancel()

	cached, err := s.cache.Get(ctx, cacheKey).Bytes()
	switch {
	case err == nil:
		obs.CacheRequests.WithLabelValues("inventory", "hit").Inc()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(cached)
		return
	case errors.Is(err, redis.Nil):
		obs.CacheRequests.WithLabelValues("inventory", "miss").Inc()
	default:
		obs.CacheRequests.WithLabelValues("inventory", "error").Inc()
		s.o.Log.WarnContext(ctx, "cache unavailable, reading from database", "upstream", "redis",
			"error", err.Error(), "trace_id", obs.TraceID(ctx))
	}

	products, dbErr := s.loadProducts(ctx)
	if dbErr != nil {
		s.dbError(w, r, "list_products", dbErr)
		return
	}
	body, _ := json.Marshal(products)
	if errors.Is(err, redis.Nil) { // refill the cache only after a clean miss
		ttl := time.Duration(s.cfg.Redis.TTLSeconds) * time.Second
		if setErr := s.cache.Set(ctx, cacheKey, body, ttl).Err(); setErr != nil {
			obs.CacheRequests.WithLabelValues("inventory", "error").Inc()
		}
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(body)
}

// loadProducts reads the catalogue with stock and 7-day popularity from PostgreSQL.
func (s *server) loadProducts(ctx context.Context) ([]product, error) {
	var products []product
	err := s.db.Query(ctx, "list_products",
		`SELECT p.id, p.name, p.price, s.quantity, coalesce(o.cnt, 0)
		 FROM products p
		 JOIN stock s ON s.product_id = p.id
		 LEFT JOIN (SELECT product_id, count(*) AS cnt FROM orders
		            WHERE created_at > now() - interval '7 days' GROUP BY product_id) o ON o.product_id = p.id
		 ORDER BY p.id`, nil,
		func(scan func(dest ...any) error) error {
			var p product
			if err := scan(&p.ID, &p.Name, &p.Price, &p.Stock, &p.Orders7d); err != nil {
				return err
			}
			products = append(products, p)
			return nil
		})
	return products, err
}

func (s *server) reserve(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ProductID int `json:"product_id"`
		Quantity  int `json:"quantity"`
	}
	if err := obs.ReadJSON(r, &req); err != nil || req.ProductID <= 0 || req.Quantity <= 0 {
		obs.WriteJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid reservation"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), s.cfg.DB.QueryTimeout())
	defer cancel()

	var price float64
	err := s.db.QueryRow(ctx, "reserve_stock",
		`UPDATE stock SET quantity = stock.quantity - $2 FROM products p
		 WHERE stock.product_id = $1 AND p.id = stock.product_id AND stock.quantity >= $2
		 RETURNING p.price`,
		[]any{req.ProductID, req.Quantity}, &price)
	if errors.Is(err, obs.ErrNoRows) {
		obs.WriteJSON(w, http.StatusConflict, map[string]string{"error": "product unavailable"})
		return
	}
	if err != nil {
		s.dbError(w, r, "reserve_stock", err)
		return
	}
	obs.WriteJSON(w, http.StatusOK, map[string]any{"product_id": req.ProductID, "quantity": req.Quantity, "unit_price": price})
}

// dbError logs the failure with upstream=postgres and returns 503.
func (s *server) dbError(w http.ResponseWriter, r *http.Request, query string, err error) {
	s.o.Log.ErrorContext(r.Context(), "database query failed", "upstream", "postgres", "query", query,
		"error", err.Error(), "trace_id", obs.TraceID(r.Context()))
	obs.WriteJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "database unavailable"})
}
