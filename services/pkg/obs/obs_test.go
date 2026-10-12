package obs

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

// The middleware must label metrics with the route pattern (not the raw path)
// and propagate the caller's trace id.
func TestMiddlewareRouteLabelAndTraceID(t *testing.T) {
	o := New("test", "v0", "c0")
	mux := o.NewMux()
	mux.HandleFunc("GET /items/{id}", func(w http.ResponseWriter, r *http.Request) {
		if TraceID(r.Context()) != "trace-123" {
			t.Errorf("trace id not in context")
		}
		w.WriteHeader(http.StatusTeapot)
	})
	req := httptest.NewRequest("GET", "/items/42", nil)
	req.Header.Set(TraceHeader, "trace-123")
	rec := httptest.NewRecorder()

	o.Middleware(mux).ServeHTTP(rec, req)

	if got := rec.Header().Get(TraceHeader); got != "trace-123" {
		t.Errorf("response trace header = %q", got)
	}
	if n := testutil.ToFloat64(HTTPRequests.WithLabelValues("test", "/items/{id}", "GET", "418")); n != 1 {
		t.Errorf("http_requests_total for route pattern = %v, want 1", n)
	}
}

func TestUpstreamErrorTimeout(t *testing.T) {
	if !(&UpstreamError{Target: "x", Err: context.DeadlineExceeded}).Timeout() {
		t.Error("deadline exceeded should be reported as timeout")
	}
	if (&UpstreamError{Target: "x", Status: 500}).Timeout() {
		t.Error("HTTP 500 is not a timeout")
	}
}

// Chaos flags start and stop their fault; stopping releases it.
func TestChaosFlagsToggle(t *testing.T) {
	c := newChaos()
	c.set("leak_memory", true)
	if !c.state()["leak_memory"] {
		t.Fatal("leak_memory should be on")
	}
	c.set("leak_memory", false)
	if c.state()["leak_memory"] {
		t.Fatal("leak_memory should be off")
	}
	if len(c.state()) != 3 {
		t.Fatalf("expected 3 flags, got %v", c.state())
	}
}
