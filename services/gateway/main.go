// Gateway: the public API of the shop.
//
// GET  /api/products       → inventory GET /products
// POST /api/orders         → inventory POST /reserve → payment POST /charge (with retries) → order POST /orders
// GET  /api/orders/{id}    → order GET /orders/{id}
// Every downstream call goes through Toxiproxy (URLs in config.yaml).
package main

import (
	"errors"
	"net/http"
	"os"
	"time"

	"shop/pkg/obs"
)

var (
	version = "dev"
	commit  = "unknown"
)

type config struct {
	ListenAddr        string `yaml:"listen_addr"`
	InventoryURL      string `yaml:"inventory_url"`
	PaymentURL        string `yaml:"payment_url"`
	OrderURL          string `yaml:"order_url"`
	PaymentRetries    int    `yaml:"payment_retries"` // retries after the first attempt
	UpstreamTimeoutMS int    `yaml:"upstream_timeout_ms"`
}

type server struct {
	o         *obs.Obs
	cfg       config
	inventory *obs.Upstream
	payment   *obs.Upstream
	order     *obs.Upstream
}

type createOrderRequest struct {
	CustomerID int `json:"customer_id"`
	ProductID  int `json:"product_id"`
	Quantity   int `json:"quantity"`
}

func main() {
	o := obs.New("gateway", version, commit)
	var cfg config
	if err := obs.LoadConfig(&cfg); err != nil {
		o.Log.Error("cannot load config", "error", err.Error())
		os.Exit(1)
	}
	timeout := time.Duration(cfg.UpstreamTimeoutMS) * time.Millisecond
	s := &server{
		o: o, cfg: cfg,
		inventory: o.NewUpstream("inventory", cfg.InventoryURL, timeout),
		payment:   o.NewUpstream("payment", cfg.PaymentURL, timeout),
		order:     o.NewUpstream("order", cfg.OrderURL, timeout),
	}

	mux := o.NewMux()
	mux.HandleFunc("GET /api/products", s.listProducts)
	mux.HandleFunc("POST /api/orders", s.createOrder)
	mux.HandleFunc("GET /api/orders/{id}", s.getOrder)
	o.Serve(cfg.ListenAddr, mux)
}

func (s *server) listProducts(w http.ResponseWriter, r *http.Request) {
	var products any
	if err := s.inventory.Do(r.Context(), "GET", "/products", nil, &products); err != nil {
		s.upstreamFailed(w, err)
		return
	}
	obs.WriteJSON(w, http.StatusOK, products)
}

func (s *server) createOrder(w http.ResponseWriter, r *http.Request) {
	var req createOrderRequest
	if err := obs.ReadJSON(r, &req); err != nil || req.CustomerID <= 0 || req.ProductID <= 0 || req.Quantity <= 0 {
		obs.WriteJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid order request"})
		return
	}
	ctx := r.Context()

	// 1. Reserve stock (returns the unit price).
	var reserved struct {
		UnitPrice float64 `json:"unit_price"`
	}
	reserveBody := map[string]int{"product_id": req.ProductID, "quantity": req.Quantity}
	if err := s.inventory.Do(ctx, "POST", "/reserve", reserveBody, &reserved); err != nil {
		s.upstreamFailed(w, err)
		return
	}
	amount := reserved.UnitPrice * float64(req.Quantity)

	// 2. Charge the customer, retrying on errors/timeouts.
	var charge struct {
		ChargeID string `json:"charge_id"`
	}
	chargeBody := map[string]any{"customer_id": req.CustomerID, "amount": amount}
	if err := s.chargeWithRetries(r, chargeBody, &charge); err != nil {
		s.upstreamFailed(w, err)
		return
	}

	// 3. Persist the order.
	var created map[string]any
	orderBody := map[string]any{
		"customer_id": req.CustomerID, "product_id": req.ProductID, "quantity": req.Quantity,
		"amount": amount, "charge_id": charge.ChargeID,
	}
	if err := s.order.Do(ctx, "POST", "/orders", orderBody, &created); err != nil {
		s.upstreamFailed(w, err)
		return
	}
	obs.WriteJSON(w, http.StatusCreated, created)
}

// chargeWithRetries calls payment up to 1+payment_retries times. Client errors (4xx) are not retried.
func (s *server) chargeWithRetries(r *http.Request, body map[string]any, out any) error {
	var err error
	for attempt := 0; attempt <= s.cfg.PaymentRetries; attempt++ {
		if err = s.payment.Do(r.Context(), "POST", "/charge", body, out); err == nil {
			return nil
		}
		var ue *obs.UpstreamError
		if errors.As(err, &ue) && ue.Status >= 400 && ue.Status < 500 {
			return err
		}
		if attempt < s.cfg.PaymentRetries {
			s.o.Log.WarnContext(r.Context(), "retrying payment", "upstream", "payment", "attempt", attempt+1,
				"error", err.Error(), "trace_id", obs.TraceID(r.Context()))
			time.Sleep(50 * time.Millisecond)
		}
	}
	return err
}

func (s *server) getOrder(w http.ResponseWriter, r *http.Request) {
	var o any
	err := s.order.Do(r.Context(), "GET", "/orders/"+r.PathValue("id"), nil, &o)
	var ue *obs.UpstreamError
	if errors.As(err, &ue) && ue.Status == http.StatusNotFound {
		obs.WriteJSON(w, http.StatusNotFound, map[string]string{"error": "order not found"})
		return
	}
	if err != nil {
		s.upstreamFailed(w, err)
		return
	}
	obs.WriteJSON(w, http.StatusOK, o)
}

// upstreamFailed maps a downstream failure to 504 (timeout) or 502 (anything else).
func (s *server) upstreamFailed(w http.ResponseWriter, err error) {
	code, target := http.StatusBadGateway, ""
	var ue *obs.UpstreamError
	if errors.As(err, &ue) {
		target = ue.Target
		if ue.Timeout() {
			code = http.StatusGatewayTimeout
		}
	}
	obs.WriteJSON(w, code, map[string]string{"error": err.Error(), "upstream": target})
}
