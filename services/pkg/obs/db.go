package obs

// Instrumented Postgres access (pgxpool).
// Pool wait time and query time are measured separately, so the agents can tell
// "queries are slow" (db_query_duration_seconds) from "the pool is exhausted"
// (db_pool_acquire_duration_seconds, db_pool_acquired_conns == db_pool_max_conns).

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

// DB wraps a pgx pool with metrics.
type DB struct {
	obs  *Obs
	Pool *pgxpool.Pool
}

// NewDB creates the pool (connections are opened lazily, so the service starts
// even if Postgres is down) and exports pool gauges.
func (o *Obs) NewDB(ctx context.Context, dsn string, maxConns int32) (*DB, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	if maxConns > 0 {
		cfg.MaxConns = maxConns
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	labels := prometheus.Labels{"service": o.Service}
	promauto.NewGaugeFunc(prometheus.GaugeOpts{
		Name: "db_pool_acquired_conns", Help: "Connections currently in use.", ConstLabels: labels,
	}, func() float64 { return float64(pool.Stat().AcquiredConns()) })
	promauto.NewGaugeFunc(prometheus.GaugeOpts{
		Name: "db_pool_max_conns", Help: "Configured pool size.", ConstLabels: labels,
	}, func() float64 { return float64(pool.Stat().MaxConns()) })
	o.AddHealthCheck("postgres", func(ctx context.Context) error { return pool.Ping(ctx) })
	return &DB{obs: o, Pool: pool}, nil
}

// acquire takes a connection from the pool and records how long we waited.
func (d *DB) acquire(ctx context.Context) (*pgxpool.Conn, error) {
	start := time.Now()
	conn, err := d.Pool.Acquire(ctx)
	DBPoolAcquireDuration.WithLabelValues(d.obs.Service).Observe(time.Since(start).Seconds())
	if err != nil {
		return nil, fmt.Errorf("acquire connection: %w", err)
	}
	return conn, nil
}

// QueryRow runs a single-row query named `name` (used as the metric label) and scans into dest.
func (d *DB) QueryRow(ctx context.Context, name, sql string, args []any, dest ...any) error {
	conn, err := d.acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	start := time.Now()
	err = conn.QueryRow(ctx, sql, args...).Scan(dest...)
	DBQueryDuration.WithLabelValues(d.obs.Service, name).Observe(time.Since(start).Seconds())
	return err
}

// Exec runs a statement named `name`.
func (d *DB) Exec(ctx context.Context, name, sql string, args ...any) (pgconn.CommandTag, error) {
	conn, err := d.acquire(ctx)
	if err != nil {
		return pgconn.CommandTag{}, err
	}
	defer conn.Release()
	start := time.Now()
	tag, err := conn.Exec(ctx, sql, args...)
	DBQueryDuration.WithLabelValues(d.obs.Service, name).Observe(time.Since(start).Seconds())
	return tag, err
}

// ErrNoRows is re-exported so services don't need to import pgx directly.
var ErrNoRows = pgx.ErrNoRows
