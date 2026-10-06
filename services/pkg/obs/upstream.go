package obs

// Instrumented HTTP client for service-to-service calls.
// Every call: propagates X-Trace-Id, records upstream_* metrics, logs failures
// (with `error` and `upstream` fields) and slow responses.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"time"
)

// SlowUpstreamThreshold: calls slower than this are logged as WARN.
const SlowUpstreamThreshold = time.Second

// Upstream is a client for one dependency (e.g. "payment").
type Upstream struct {
	obs     *Obs
	Target  string // logical name used in metrics/logs
	BaseURL string
	client  *http.Client
}

// UpstreamError is returned for non-2xx responses so callers can map status codes.
type UpstreamError struct {
	Target string
	Status int // 0 when the request failed before getting a response
	Err    error
}

func (e *UpstreamError) Error() string {
	if e.Status == 0 {
		return fmt.Sprintf("%s unreachable: %v", e.Target, e.Err)
	}
	return fmt.Sprintf("%s returned %d", e.Target, e.Status)
}

// Timeout reports whether the call failed because the deadline was exceeded.
func (e *UpstreamError) Timeout() bool {
	return errors.Is(e.Err, context.DeadlineExceeded) || errors.Is(e.Err, os.ErrDeadlineExceeded)
}

// NewUpstream creates a client; an empty baseURL means "not configured".
func (o *Obs) NewUpstream(target, baseURL string, timeout time.Duration) *Upstream {
	return &Upstream{obs: o, Target: target, BaseURL: baseURL, client: &http.Client{Timeout: timeout}}
}

// Configured is false when the service has no URL for this dependency.
func (u *Upstream) Configured() bool { return u.BaseURL != "" }

// Do sends a JSON request and decodes a 2xx JSON response into out (if non-nil).
func (u *Upstream) Do(ctx context.Context, method, path string, body, out any) error {
	var reader *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		reader = bytes.NewReader(b)
	} else {
		reader = bytes.NewReader(nil)
	}
	req, err := http.NewRequestWithContext(ctx, method, u.BaseURL+path, reader)
	if err != nil {
		return &UpstreamError{Target: u.Target, Err: err}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(TraceHeader, TraceID(ctx))

	start := time.Now()
	resp, err := u.client.Do(req)
	latency := time.Since(start)
	UpstreamDuration.WithLabelValues(u.obs.Service, u.Target).Observe(latency.Seconds())

	if err != nil {
		UpstreamRequests.WithLabelValues(u.obs.Service, u.Target, "error").Inc()
		u.obs.Log.ErrorContext(ctx, "upstream request failed", "upstream", u.Target, "route", path,
			"error", err.Error(), "latency_ms", latency.Milliseconds(), "trace_id", TraceID(ctx))
		return &UpstreamError{Target: u.Target, Err: err}
	}
	defer resp.Body.Close()
	UpstreamRequests.WithLabelValues(u.obs.Service, u.Target, strconv.Itoa(resp.StatusCode)).Inc()

	if latency > SlowUpstreamThreshold {
		u.obs.Log.WarnContext(ctx, "slow upstream response", "upstream", u.Target, "route", path,
			"status", resp.StatusCode, "latency_ms", latency.Milliseconds(), "trace_id", TraceID(ctx))
	}
	if resp.StatusCode >= 300 {
		if resp.StatusCode >= 500 {
			u.obs.Log.ErrorContext(ctx, "upstream request failed", "upstream", u.Target, "route", path,
				"status", resp.StatusCode, "error", fmt.Sprintf("status %d", resp.StatusCode),
				"latency_ms", latency.Milliseconds(), "trace_id", TraceID(ctx))
		}
		return &UpstreamError{Target: u.Target, Status: resp.StatusCode}
	}
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}
