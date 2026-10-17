package obs

// Instrumented Postgres access (pgxpool).
// Pool wait time and query time are measured separately, so the agents can tell
// "queries are slow" (db_query_duration_seconds) from "the pool is exhausted"
// (db_pool_acquire_duration_seconds, db_pool_acquired_conns == db_pool_max_conns).

import (
	"context"
	"fmt"
	"os"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

// DBConfig is the `db:` block of a service's config.yaml.
// The password is not in the file: it comes from env POSTGRES_PASSWORD.
type DBConfig struct {
	Host           string `yaml:"host"`
	Port           int    `yaml:"port"`
	Name           string `yaml:"name"`
	User           string `yaml:"user"`
	MaxConns       int32  `yaml:"max_conns"`
	QueryTimeoutMS int    `yaml:"query_timeout_ms"`
}

// QueryTimeout bounds one request's database work (pool wait included).
func (c DBConfig) QueryTimeout() time.Duration {
	return time.Duration(c.QueryTimeoutMS) * time.Millisecond
}

// DB wraps a pgx pool with metrics.
type DB struct {
	obs  *Obs
	Pool *pgxpool.Pool
}

// NewDB creates the pool (connections are opened lazily, so the service starts
// even if Postgres is down) and exports pool gauges.
func (o *Obs) NewDB(ctx context.Context, c DBConfig) (*DB, error) {
	dsn := fmt.Sprintf("postgres://%s:%s@%s:%d/%s?sslmode=disable&connect_timeout=3",
		c.User, os.Getenv("POSTGRES_PASSWORD"), c.Host, c.Port, c.Name)
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	if c.MaxConns > 0 {
		cfg.MaxConns = c.MaxConns
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

// querier is what pgx connections and transactions have in common.
type querier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

// timed runs one query and records its duration under the label `name`.
func (d *DB) timed(name string, run func() error) error {
	start := time.Now()
	err := run()
	DBQueryDuration.WithLabelValues(d.obs.Service, name).Observe(time.Since(start).Seconds())
	return err
}

// QueryRow runs a single-row query and scans into dest.
func (d *DB) QueryRow(ctx context.Context, name, sql string, args []any, dest ...any) error {
	conn, err := d.acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	return d.timed(name, func() error { return conn.QueryRow(ctx, sql, args...).Scan(dest...) })
}

// Exec runs a statement without result rows (e.g. a migration).
func (d *DB) Exec(ctx context.Context, name, sql string, args ...any) (pgconn.CommandTag, error) {
	conn, err := d.acquire(ctx)
	if err != nil {
		return pgconn.CommandTag{}, err
	}
	defer conn.Release()
	var tag pgconn.CommandTag
	err = d.timed(name, func() error {
		var execErr error
		tag, execErr = conn.Exec(ctx, sql, args...)
		return execErr
	})
	return tag, err
}

// Query runs a multi-row query; each(scan) is called once per row.
func (d *DB) Query(ctx context.Context, name, sql string, args []any, each func(scan func(dest ...any) error) error) error {
	conn, err := d.acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	return d.timed(name, func() error { return queryAll(ctx, conn, sql, args, each) })
}

func queryAll(ctx context.Context, q querier, sql string, args []any, each func(scan func(dest ...any) error) error) error {
	rows, err := q.Query(ctx, sql, args...)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		if err := each(rows.Scan); err != nil {
			return err
		}
	}
	return rows.Err()
}

// Tx is a transaction; it holds one pooled connection until it ends.
type Tx struct {
	d  *DB
	tx pgx.Tx
}

// QueryRow runs a single-row query inside the transaction.
func (t *Tx) QueryRow(ctx context.Context, name, sql string, args []any, dest ...any) error {
	return t.d.timed(name, func() error { return t.tx.QueryRow(ctx, sql, args...).Scan(dest...) })
}

// InTx runs fn in a transaction: commit if fn returns nil, otherwise roll back.
func (d *DB) InTx(ctx context.Context, fn func(tx *Tx) error) error {
	conn, err := d.acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	pgtx, err := conn.Begin(ctx)
	if err != nil {
		return err
	}
	if err := fn(&Tx{d: d, tx: pgtx}); err != nil {
		_ = pgtx.Rollback(context.WithoutCancel(ctx))
		return err
	}
	return pgtx.Commit(ctx)
}

// ErrNoRows is re-exported so services don't need to import pgx directly.
var ErrNoRows = pgx.ErrNoRows
