// Package obs is the shared observability kit used by every service.
//
// It exists so that all services emit the SAME log fields (SPEC 5.2) and the
// SAME metric names (SPEC 5.3). The agents can then query every service the
// same way, e.g. `{service="order"} | json | level="ERROR"` in Loki or
// `http_requests_total{service="order"}` in Prometheus.
//
// This file: logger, request middleware, /healthz, /metrics, build_info,
// trace-id propagation, JSON helpers, config loading and graceful serving.
// upstream.go: instrumented HTTP client for service-to-service calls.
// db.go: instrumented Postgres pool.
package obs

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"
	"gopkg.in/yaml.v3"
)

// TraceHeader carries the trace id from gateway to every downstream call.
const TraceHeader = "X-Trace-Id"

// Obs bundles the service name and its logger; one per process.
type Obs struct {
	Service string
	Log     *slog.Logger
	checks  map[string]func(context.Context) error
}

// New creates the logger and sets the build_info gauge.
func New(service, version, commit string) *Obs {
	h := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		// The spec uses "ts" instead of slog's default "time" key.
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if a.Key == slog.TimeKey && len(groups) == 0 {
				a.Key = "ts"
			}
			return a
		},
	})
	BuildInfo.WithLabelValues(service, version, commit).Set(1)
	o := &Obs{Service: service, Log: slog.New(h).With("service", service), checks: map[string]func(context.Context) error{}}
	o.Log.Info("starting", "version", version, "commit", commit)
	return o
}

// AddHealthCheck registers a dependency check reported by /healthz.
func (o *Obs) AddHealthCheck(name string, fn func(context.Context) error) {
	o.checks[name] = fn
}

// NewMux returns a ServeMux with /healthz and /metrics already registered.
func (o *Obs) NewMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.Handle("GET /metrics", promhttp.Handler())
	mux.HandleFunc("GET /healthz", o.healthz)
	return mux
}

func (o *Obs) healthz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), time.Second)
	defer cancel()
	status, code := "ok", http.StatusOK
	results := map[string]string{}
	for name, check := range o.checks {
		if err := check(ctx); err != nil {
			results[name] = err.Error()
			status, code = "degraded", http.StatusServiceUnavailable
		} else {
			results[name] = "ok"
		}
	}
	WriteJSON(w, code, map[string]any{"status": status, "service": o.Service, "checks": results})
}

// Middleware logs one line per request and records the HTTP metrics.
func (o *Obs) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		traceID := r.Header.Get(TraceHeader)
		if traceID == "" {
			traceID = newTraceID()
		}
		r = r.WithContext(context.WithValue(r.Context(), traceKey{}, traceID))
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		rec.Header().Set(TraceHeader, traceID)

		next.ServeHTTP(rec, r)

		// ServeMux sets r.Pattern (e.g. "POST /charge") while routing.
		route := "unmatched"
		if _, path, ok := strings.Cut(r.Pattern, " "); ok {
			route = path
		}
		if route == "/metrics" || route == "/healthz" {
			return // scraped every few seconds; not business traffic
		}
		latency := time.Since(start)
		status := strconv.Itoa(rec.status)
		HTTPRequests.WithLabelValues(o.Service, route, r.Method, status).Inc()
		HTTPDuration.WithLabelValues(o.Service, route).Observe(latency.Seconds())

		level := slog.LevelInfo
		if rec.status >= 500 {
			level = slog.LevelError
		}
		o.Log.Log(r.Context(), level, "http request",
			"route", route, "method", r.Method, "status", rec.status,
			"latency_ms", latency.Milliseconds(), "trace_id", traceID)
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

type traceKey struct{}

// TraceID returns the trace id of the current request ("" if none).
func TraceID(ctx context.Context) string {
	id, _ := ctx.Value(traceKey{}).(string)
	return id
}

func newTraceID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// WriteJSON writes v as a JSON response with the given status code.
func WriteJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// ReadJSON decodes the request body into v.
func ReadJSON(r *http.Request, v any) error {
	defer r.Body.Close()
	return json.NewDecoder(r.Body).Decode(v)
}

// LoadConfig reads the YAML file at $CONFIG_PATH (default ./config.yaml) into out.
// Config lives in the target repo so misconfiguration scenarios are real git diffs.
func LoadConfig(out any) error {
	path := os.Getenv("CONFIG_PATH")
	if path == "" {
		path = "config.yaml"
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return yaml.Unmarshal(b, out)
}

// Serve runs the HTTP server with the middleware and shuts down cleanly on SIGTERM.
func (o *Obs) Serve(addr string, mux *http.ServeMux) {
	srv := &http.Server{Addr: addr, Handler: o.Middleware(mux), ReadHeaderTimeout: 5 * time.Second}
	go func() {
		o.Log.Info("listening", "addr", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			o.Log.Error("server failed", "error", err.Error())
			os.Exit(1)
		}
	}()
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	o.Log.Info("stopped")
}
