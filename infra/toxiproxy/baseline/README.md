# Baseline toxics (always on)

These toxics are part of the *normal* environment, not faults. They model the
database running on a separate server: every round trip from a service to
Postgres takes ~20 ms (±5 ms), as on a real network.

Why: with a local container the round trip is ~0.3 ms, so even a 2-connection
pool never runs out at our traffic levels, and the pool-exhaustion scenario
(s06) could not happen. With 20 ms per round trip, an order holds a pooled
connection for ~80 ms (BEGIN, fraud check, INSERT, COMMIT). The default pool
(10) handles that easily; a pool of 2 does not.

One file per toxic, named `<proxy>.json` (body of POST /proxies/<proxy>/toxics).
Applied by `scripts/toxiproxy_baseline.sh` (`make up`) and by `chaos reset`.
