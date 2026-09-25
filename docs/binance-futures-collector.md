# Binance USD-M futures collector

The backend process owns one public Binance USD-M WebSocket connection for the union of all
watched contracts. It starts with the TanStack server process, runs without an open dashboard,
and is enabled only when both `BINANCE_COLLECTOR_ENABLED=true` and the operational database are
enabled. A renewable operational-database lease prevents two application instances from acting
as authoritative collectors.

## Inputs and candle semantics

Each subscribed contract uses five native streams:

- `aggTrade` for a bounded live trade/movement window;
- native `kline_1m`, `kline_15m`, `kline_1h`, and `kline_4h` streams.

No mark-price stream is subscribed because there is no live consumer in this ticket; the existing
futures snapshot REST call remains the on-demand mark-price/funding source. No order-book or raw
trade history is retained for simulated execution. Add either only with its consuming feature.

Binance-native klines are canonical. Exchange open and close timestamps identify a candle.
Developing (`x=false`) updates stay only in bounded process memory. Final (`x=true`) candles are
normalized, inserted idempotently into the operational database, emitted once after a successful
new insert, and then trigger the existing Python completed-candle TA path. The direct 15m/1h/4h
streams remain authoritative; they are not assembled from 1m data. Spot or non-USD-M events are
rejected.

The endpoint and operating limits were checked against Binance documentation on 2026-09-25:

- [USD-M market streams](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)
- [live subscribe/unsubscribe](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Live-Subscribing-Unsubscribing-to-streams)
- [USD-M REST klines](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)

The implementation uses `wss://fstream.binance.com/market/stream`, rotates before Binance's
24-hour connection lifetime, reconnects with bounded exponential backoff and jitter, and keeps
the supported 20-contract maximum at 100 streams—well below the documented 1,024-stream limit.
The WebSocket implementation answers protocol ping frames automatically.

## Bootstrap, recovery, and bounds

For each contract/timeframe, startup loads recent completed `/fapi/v1/klines` history and excludes
the still-developing REST candle. A skipped final interval changes health to `RECOVERING`, fetches
only the bounded missing range, verifies exact chronological continuity, deduplicates REST/WS
overlap in the database, and processes the incoming live final last. An unprovable or over-1,000
candle gap becomes `UNAVAILABLE`.

Memory contains only the latest trade, one developing candle per contract/timeframe, the latest
completed checkpoint, connection health, a five-minute/2,000-item aggregate-trade buffer per
contract, a 35-minute movement bucket ring, and a 256-item completed-candle work queue. Queue
overflow marks the collector stale, closes the socket, and relies on REST recovery; it never creates
an unbounded queue. Developing updates, aggregate trades, and movement buckets are not persisted.

## Movement bucket input

Accepted `aggTrade` events also feed a separate, memory-only movement bucket store. It uses Binance's
trade timestamp (`T`) on a fixed five-second epoch grid and keeps 420 compact buckets (35 minutes) per
subscribed contract. Each bucket retains its endpoint price, base and quote volume, trade count, last
real trade/event timestamps, carry-forward flag, and futures provenance. This store is separate from
the collector's five-minute/2,000-trade safety buffer.

A price is carried through an empty bucket only while its last real trade is at most 15 seconds old;
later buckets have no endpoint and report stale. Per-window readiness remains `WARMING` until the
continuous live history spans two adjacent 1m, 5m, or 15m windows. A new process starts empty and does
not reconstruct this path from REST candles. The deterministic component can be advanced by an
aligned boundary for live evaluation or replay, and it performs no database writes.

Recovered/bootstrap candles carry their origin and do not directly trigger live analysis or a
new movement alert. A subsequent genuinely live completed candle invokes the existing idempotent
Python TA orchestration, which may catch up due conclusions. Movement baseline/cooldown/alert
state remains wholly in its Lovable transaction.

## Ownership and operation

With the collector enabled, `collector_recent_candles`, `collector_health`, and `collector_leases`
in the operational database are the only completed-candle/checkpoint working-state path. The old
request-driven per-user operational candle/checkpoint writes and scheduled TA trigger are disabled;
Lovable remains authoritative for watchlists, settings, movement state/alerts, and permanent TA
conclusions. No candle is dual-written to Lovable.

Apply `operational-db/supabase/migrations/20260925120000_binance_collector.sql` only to the external
operational database. Set this server-only flag (never a `VITE_*` variable):

```text
BINANCE_COLLECTOR_ENABLED=true
```

`npm run dev:local:all` enables it for that local process. `npm run dev:local` disables it and uses
the legacy request-driven path. In production, enable it only on a long-lived TanStack server
runtime; the operational lease elects one collector across instances. Disable the flag on the
whole fleet to roll back without simultaneous writers. Health (`LIVE`, `RECOVERING`, `STALE`, or
`UNAVAILABLE`) is returned only through the authenticated operational-state server function and is
filtered to the caller's watchlist.

## Monitor overlap and freshness

Manual and scheduled monitor batches use the same renewable, per-user lease in Lovable. The lease
stores a run owner ID and expires after three minutes unless renewed every minute. Normal completion
releases it; a crashed process is recoverable after expiry. A competing request returns `skipped`
without doing market, movement, or TA work. Existing database idempotency remains the final defense:
`process_cumulative_observation` still owns the atomic baseline/cooldown/alert transition, while TA
uses the unique `(user_id, symbol, timeframe, candle_at, version)` identity and pending-only outcome
updates. Collector mode still disables the request-driven TA trigger; disabling collector mode keeps
that path available.

Apply `supabase/migrations/20260926090000_monitor_run_leases.sql` through the normal Lovable/main
database migration chain. It does not belong in the external operational migration chain.

Settings reports the collector's exchange source-event time and latest completed candle separately,
plus movement progress, successful TA evaluation/candle times by timeframe, and monitor results.
Freshness labels are derived from those persisted domain timestamps; browser refresh time is never
shown as market freshness. The synchronization outbox remains dormant infrastructure with no current
durable result domain, so no synchronization timestamp is presented.

Paper trading is still out of scope. Before issue #39 enables simulated positions, that domain must
add its own durable single-owner/fencing mechanism; the monitor-run and collector leases do not grant
paper-position ownership.
