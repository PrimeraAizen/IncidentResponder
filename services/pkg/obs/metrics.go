package obs

// Standard metric names shared by all services (SPEC 5.3).
// Registered once on the default Prometheus registry; Go runtime collectors
// (go_goroutines, go_memstats_heap_alloc_bytes, ...) are included by default.

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

// Latency buckets (seconds). Include 1.5/2/3 s so injected multi-second delays are visible.
var latencyBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 1.5, 2, 3, 5, 10}

var (
	HTTPRequests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "http_requests_total", Help: "HTTP requests served.",
	}, []string{"service", "route", "method", "status"})

	HTTPDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name: "http_request_duration_seconds", Help: "HTTP request latency.", Buckets: latencyBuckets,
	}, []string{"service", "route"})

	UpstreamRequests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "upstream_requests_total", Help: "Calls to other services; status is the HTTP code or 'error'.",
	}, []string{"service", "target", "status"})

	UpstreamDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name: "upstream_request_duration_seconds", Help: "Latency of calls to other services.", Buckets: latencyBuckets,
	}, []string{"service", "target"})

	DBQueryDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name: "db_query_duration_seconds", Help: "Database query latency (excluding pool wait).", Buckets: latencyBuckets,
	}, []string{"service", "query"})

	DBPoolAcquireDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name: "db_pool_acquire_duration_seconds", Help: "Time spent waiting for a pool connection.", Buckets: latencyBuckets,
	}, []string{"service"})

	CacheRequests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "cache_requests_total", Help: "Cache lookups by result (hit|miss|error).",
	}, []string{"service", "result"})

	EventsPublished = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "events_published_total", Help: "Events published by status (ok|error).",
	}, []string{"service", "status"})

	EventsConsumed = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "events_consumed_total", Help: "Events consumed.",
	}, []string{"service"})

	BuildInfo = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "build_info", Help: "Always 1; labels carry the deployed version and git commit.",
	}, []string{"service", "version", "commit"})
)
