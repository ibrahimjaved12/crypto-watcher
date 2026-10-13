# Binance USD-M futures collector

The collector is an independently runnable persistent backend worker. It owns one public Binance
USD-M WebSocket connection for the application-assigned shared subscription universe, runs without
an open dashboard, and is enabled only when both `BINANCE_COLLECTOR_ENABLED=true` and the
operational database are enabled. The application owns user watchlists; it derives the union of
watched contracts required by accounts with monitoring and market-data collection enabled
and assigns that set to the operational database as collector input. The worker reads it
there rather than querying main-database user tables. The six benchmark symbols are always
included. Reconciliation is application-owned and independent of scheduled monitoring:
the machine-side `collector-universe-sync` child calls the existing `syncCollectorUniverse`
periodically. The authenticated `/api/public/hooks/sync-collector-subscriptions` hook remains
available for explicit reconciliation, and the server's best-effort first-request pass is a
safety net where collector mode and the operational store are available. The hosted Worker
has collector mode disabled and cannot reach that store. A renewable operational-database
lease prevents two worker instances from acting as authoritative collectors. The TanStack
application server does not start the collector: importing or starting the app never opens a market
stream. Production hosting for the worker remains an open decision in
[#19](https://github.com/ibrahimjaved12/crypto-watcher/issues/19).

## Collection pause and re-enable

The current-state lifecycle is:

- Collection enabled and a symbol required by an eligible watcher: the sync child assigns
  the symbol, the worker reconciles and performs REST bootstrap/recovery, and health
  becomes `LIVE` when current evidence is established.
- The final relevant watcher of a non-benchmark pair is removed or disables collection: the sync
  child removes the symbol from the shared universe on the next successful sync pass. The
  assignment RPC immediately sets every
  persisted health row outside that universe to `UNAVAILABLE`, with reason
  `collection disabled/unsubscribed`, null lag, and zero queue depth. The worker
  unsubscribes during reconciliation. An empty assignment invalidates all health rows.
- Collection re-enabled: the symbol re-enters the assigned universe, and its existing
  health remains `UNAVAILABLE` until normal worker bootstrap/recovery establishes
  current evidence. Assignment itself never marks it `LIVE`.

Assignment and health invalidation share one transaction. Health persistence checks
the authoritative assignment under a database lock held through the write, so queued
or in-flight intents cannot restore `LIVE`, `RECOVERING`, or `STALE` for an unsubscribed
symbol. These intents safely persist `UNAVAILABLE` with the collection-disabled reason.
Subscribed symbols retain the normal worker health behavior.

Removal preserves factual last-event time, last completed open time, and reconnect
count. It never deletes `collector_recent_candles`: historical completed observations
remain true and continue under the existing retention policy. Other eligible watchers
keep the symbol assigned even if one account pauses collection.

## Inputs and candle semantics

Each subscribed contract uses five native streams:

- `aggTrade` for a bounded live trade/movement window;
- native `kline_1m`, `kline_15m`, `kline_1h`, and `kline_4h` streams.

No mark-price stream is subscribed because there is no live consumer in this ticket; the existing
futures snapshot REST call remains the on-demand mark-price/funding source. No order-book or raw
trade history is retained for simulated execution. Add either only with its consuming feature.

Binance-native klines are canonical. Exchange open and close timestamps identify a candle.
Developing (`x=false`) updates stay only in bounded process memory. Final (`x=true`) candles are
normalized and inserted idempotently into the operational database. Each persisted completed candle
keeps the exchange kline close time, the actual exchange event time when one exists, and the
collector receive time as distinct fields, together with its exact endpoint and transport, so a
WebSocket live candle is never recorded with REST provenance and vice versa. Candle completion is
always the deterministic boundary `open + timeframe`; the exchange event time is provenance, not the
definition of completion, and REST bootstrap/recovery records no exchange event at all. The
worker owns no TA: the application's completed-candle orchestration determines due work, reads
canonical completed candles back from the operational store (`readCollectorCompletedCandles`) rather than
fetching a second live exchange series, calls the shared Python service, validates the response, and
writes conclusions to the main database. The direct 15m/1h/4h streams remain
authoritative; they are not assembled from 1m data. Spot or non-USD-M events are rejected.

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
the still-developing REST candle. REST bootstrap/recovery has no exchange event, so it records no
source event rather than inventing one; the kline's own close time and the collector receive time
are stored separately, and completion remains `open + timeframe`. A skipped final interval changes
health to `RECOVERING`, fetches
only the bounded missing range, verifies exact chronological continuity, deduplicates REST/WS
overlap in the database, and processes the incoming live final last. An unprovable or over-1,000
candle gap becomes `UNAVAILABLE`.

Memory contains only the latest trade, one developing candle per contract/timeframe, the latest
completed checkpoint, connection health, a five-minute/2,000-item aggregate-trade buffer per
contract, and a 256-item completed-candle work queue. Python owns the separate 420-record movement
ring. Queue overflow marks the collector stale, closes the socket, and relies on REST recovery; it never creates
an unbounded queue. Developing updates, aggregate trades, and movement buckets are not persisted.

The snapshot buffer retains logical aggTrade items subject to **both** five-minute age and at most
2,000 items per contract. Active symbols may hit the count limit earlier, so it guarantees no
five-minute recovery history. It is bounded collector transport/resource state, not canonical
movement analytical history; that path has its own validated ordered observation transport and
Python bucket state. The exact sizing has no demonstrated consumer-derived invariant and remains
unchanged pending separate evidence.

The collector's **30s socket-silence** rule handles connection staleness. It is separate from the
application's **60s movement-state age** diagnostic and Python's **15s endpoint/carry freshness**;
see [V1 ownership and constants](./market-movement-engine.md#v1-constants-and-ownership).

## Movement bucket input

Accepted `aggTrade` observations travel through validated ordered transport to the canonical,
memory-only Python movement bucket store. It uses Binance's trade timestamp (`T`) on a fixed
five-second epoch grid and keeps 420 compact boundary records per
subscribed contract. Each bucket retains its endpoint price, base and quote volume, trade count, last
real trade/event timestamps, carry-forward flag, and futures provenance. This store is separate from
the collector's five-minute/2,000-item snapshot. The largest adjacent 15m windows require 361 inclusive
boundary records; 420 supplies 59 extra records (4m55s boundary-span headroom), not an exact
mathematical requirement. The current capacity and constructor/spec minimum remain 420.

A price is carried through an empty bucket only while its last real trade is at most 15 seconds old;
later buckets have no endpoint and report stale. Per-window readiness remains `WARMING` until the
continuous live history spans two adjacent 1m, 5m, or 15m windows. A new process starts empty and does
not reconstruct this path from REST candles. The deterministic component can be advanced by an
aligned boundary for live evaluation or replay, and it performs no database writes.

Verified historical aggTrade archives can support research replay, subject to per-partition checks;
they do not restore the original WebSocket receive stream or reconnect history. See
[archive research](./historical-replay.md#archive-research-and-live-history).

Recovered/bootstrap candles carry their origin and never bypass the application's due-work check;
the collector performs no TA. Movement baseline/cooldown/alert state remains wholly in its main-database
transaction.

## Ownership and operation

### Pre-release operational database reset

The operational database contains disposable pre-release working state. Recreate/reset
it from the single current baseline,
`operational-db/supabase/migrations/20261004000000_operational_schema.sql`. This file
directly defines the final operational store, collector, subscription-aware health,
retention, and movement persistence/read semantics. Historical operational migration
evolution is intentionally not preserved yet; no production-data migration path is
required. Once production data must survive upgrades, freeze the baseline and make
future changes through forward migrations.

After recreation, WebSocket `source_event_at` is the actual Binance event time (`E`),
REST `source_event_at` is `NULL`, and `received_at` is the actual collector receive
or REST retrieval time. Candle completion is determined independently as
`open_time + timeframe`; event and receive timestamps are provenance, not a
completion boundary. The collector then rebuilds the required working history through
its normal REST bootstrap/backfill path.

With the collector enabled, `collector_recent_candles`, `collector_health`, and `collector_leases`
in the operational database are the only shared completed-candle/checkpoint working-state path, and
`collector_subscriptions` holds the application-assigned subscription universe. The old
request-driven per-user operational candle/checkpoint writes are disabled. The application reads
canonical completed candles back through `get_collector_completed_candles` for its own TA input; the read
transports each candle's recorded endpoint, transport, close time, source event time and receive
time rather than reconstructing them, and missing or
stale operational history fails the affected TA frame visibly instead of falling back to a live
exchange series. The main database remains
authoritative for watchlists, settings, movement state/alerts, and permanent TA conclusions, and it
is written only by TanStack: the worker holds no main-database credential and never reads watchlists or
settings or writes TA. No candle is dual-written to the main database.

Apply all unapplied SQL migrations in filename order to the application database. Recreate the
disposable pre-release operational database from its single current baseline. Its final candle writer
keeps each canonical series' newest 260 completed candles even when that spans more than the day window,
protecting TA history/catch-up availability independently of age-based retention. This storage floor
is distinct from Python's 200-candle minimum and the application's 260-candle TA read horizon.
Changing a TA input prefix can affect recursive indicator initialization; see
[TA history policies](./technical-analysis.md#history-policies). After the reset, the REST bootstrap
fetches 300 klines per frame and persists the completed ones (excluding the developing candle),
rebuilding 299 candles for the 15m, 1h, and 4h TA frames. Set this server-only flag (never a
`VITE_*` variable) on both the
collector worker and the application server:

```text
BINANCE_COLLECTOR_ENABLED=true
```

The worker reads it to start the collector; the application reads it to assign the shared
subscription universe, to know that the collector owns completed-candle/checkpoint state, and to
source completed-candle TA input from the operational store instead of the exchange REST provider.
Locally, `npm run dev:local:all` starts the
collector worker as a separate process and enables the flag for both processes, while
`npm run dev:local` disables it and uses the legacy request-driven path. `npm run collector:worker`
runs the worker on its own. `npm run collector:worker:build` produces the self-contained
`dist/collector-worker/collector-worker.mjs` artifact, which `npm run collector:worker:start` runs
with plain `node`; that production path needs no devDependencies. In production, run the collector
as a persistent worker process and select its host from the evidence in #19; the operational lease
elects one collector across instances. Disable the flag on the whole fleet and stop the worker to
roll back without simultaneous writers. Health (`LIVE`, `RECOVERING`, `STALE`, or `UNAVAILABLE`) is
returned only through the authenticated operational-state server function and is filtered to the
caller's watchlist.

The worker has its own server-only runtime configuration boundary. When enabled it validates the
operational database variables and `MOVEMENT_FINALIZATION_GRACE_MS` from the host's runtime
`process.env`, and fails visibly before starting if any are missing or invalid. It needs no main
main-database credential and never reads browser `VITE_*` values or build-time public configuration, so
the artifact built once is host-independent. Only the TanStack application additionally proves its
runtime matches the browser bundle it serves. See [environment setup](./environments.md).

## Monitor overlap and freshness

Manual and scheduled monitor batches use the same renewable, per-user lease in the main database. The lease
stores a run owner ID and expires after three minutes unless renewed every minute. Normal completion
releases it; a crashed process is recoverable after expiry. A competing request returns `skipped`
without doing market, movement, or TA work. Existing database idempotency remains the final defense:
`process_cumulative_observation` still owns the atomic baseline/cooldown/alert transition, while TA
uses the unique `(user_id, symbol, timeframe, candle_at, version)` identity and pending-only outcome
updates. The application's completed-candle TA orchestration runs in both modes: the collector never
owns TA, so enabling it does not disable the application path. In collector mode the orchestration
sources its candle history from the operational store; when the collector is disabled it uses the
exchange REST provider, and the two modes are never mixed for one frame.

Apply `supabase/migrations/20260926090000_monitor_run_leases.sql` through the normal main
database migration chain. It does not belong in the external operational baseline.

Settings reports the collector's exchange source-event time and latest completed candle separately,
plus movement progress, successful TA evaluation/candle times by timeframe, and monitor results.
Freshness labels are derived from those persisted domain timestamps; browser refresh time is never
shown as market freshness. The synchronization outbox remains dormant infrastructure with no current
durable result domain, so no synchronization timestamp is presented.

Paper trading is still out of scope. Before issue #39 enables simulated positions, that domain must
add its own durable single-owner/fencing mechanism; the monitor-run and collector leases do not grant
paper-position ownership.


### Candle conflicts (#239 P15)

A REST recovery candle can differ from the stream-finalised candle already stored for the same
open time. The stored candle is kept (immutable), the offered candle is appended to
`collector_candle_conflicts` with which side came from REST, and the rest of the batch is written:
a conflict never fails a batch and never makes a symbol UNAVAILABLE. Health rows report
`conflict_count_24h` / `last_conflict_at` only. `reconcile_conflicts(24)` appends REST revisions
within a stated tolerance to `collector_candle_revisions` for research parity without touching the
stored row. Details: [operational database](./operational-database.md#candle-conflicts-and-revisions-239-p15).

### Shared completed-candle contract (#91)

`completed-candle-v1` exposes collector-recorded native Binance USD-M perpetual
trade 1m candles as immutable market facts, separately from observation provenance.
A candle covers `[open_time_ms, open_time_ms + 60_000)`; the stored Binance
inclusive close label remains `open_time_ms + 59_999`. Python derives
`end_time_exclusive_ms = close_time_ms + 1`. Admission attests completion;
provenance timestamps do not define it.

WebSocket provenance preserves the actual Binance `E` and local receive time.
REST provenance carries the actual retrieval time and has no source-event field.
The generic operational read returns full identity, OHLC, base/quote volumes and
recorded provenance for native 1m/15m/1h/4h, newest-N bounded and chronologically
ordered. Gaps remain explicit; no candles are filled. The pre-production baseline
replaces the TA-specific RPC with `get_collector_completed_candles`; recreate the
disposable operational database from that baseline after review.

TA still consumes only 15m/1h/4h. #71 keeps its compact close/base/quote-volume
projection from the same authoritative `collector_recent_candles` table. #70's
five-second aggTrade buckets remain distinct. This adds no source, WebSocket,
scheduler, strategy, or per-message Python call.

## Market quote publisher (#323)

<!-- TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days -->
The Cloudflare Worker cannot reach Binance (measured: HTTP 403 on every Binance endpoint,
including the spot data host and the WebSocket upgrade; OKX answered 429), so the dashboard's price
cards no longer fetch from any exchange except the temporary degraded-mode fallback. Ingestion
and serving are separate during normal operation:

- **Machine.** `src/worker/market-quote-publisher.ts` runs as its own child of the worker host (`npm run up`
  → `worker:host`), beside the collector and not inside it, because the collector artifact must hold no
  main-database credential and the publisher writes to the main hosted project with the service-role
  key. A few seconds after each completed 1m candle it reads the collected symbols' completed 1m and 15m candles from the operational store
  (`readCollectorCompletedCandles`), builds each quote with `quote-calc.ts` and sends one batched
  call, `publish_market_quotes`, to hosted `public.market_quotes` with the same service-role client
  the forward jobs use. It makes no exchange call. A symbol whose newest candle did not advance is
  not sent, and the database never moves a row backwards, so restarts and retries are idempotent.
  A failed hosted write is logged and retried on the next candle; the collector is unaffected.
  Heartbeat: `[market-quote-publisher] heartbeat symbols=… published=… behind=… unavailable=…
  newest_published_at=…` once a minute. `MARKET_QUOTE_PUBLISHER_ENABLED=false` turns it off; the worker host also skips it when
  `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` is missing.
- **Worker.** `getMarketSnapshot` reads `market_quotes` with the publishable key (one query, RLS
  `SELECT` for `anon`/`authenticated`) and maps rows to `SymbolQuote`. A quote is stale after 10
  minutes (from `last_candle_at` or `published_at`). Old prices are not displayed as current prices;
  if fresh data cannot be fetched, the card shows unavailable.
  <!-- TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days -->
  Old or missing pairs temporarily fetch Kraken futures candles, then OKX for pairs without fresh
  Kraken candles. Fresh published pairs stay unchanged. Failed pairs retain published metadata in
  the API and show unavailable in the UI. Unexpected fallback errors retain the published snapshot.
  Cards name the source and quote currency without repeated stale or contract notices; the header
  carries runner availability and last-online time. No Worker request goes to Binance. See the
  [switch and removal condition](environments.md#temporary-degraded-market-fallback-323).
- **Check.** `npm run mvp:check -- --mode production` lists the age of each benchmark symbol's
  newest `published_at` and warns past 10 minutes.

## Machine-side collector universe sync (#329)

`src/worker/collector-universe-sync.ts`, launched by `scripts/collector-universe-sync.mjs`, is a
separate, non-essential worker-host child beside the collector and quote publisher. It calls
`syncCollectorUniverse(supabaseAdmin, getOperationalStore())`; the shared reconciliation reads
hosted `watchlist_items` and `monitor_settings`, includes pairs watched by at least one eligible
account plus the six frozen benchmarks, and calls `assignCollectorSubscriptions` only when the
sorted universe changes. No hook scheduler or dashboard action is needed. It uses the existing
hosted service-role client and local operational credentials, all supplied at runtime.

The host starts it only with `BINANCE_COLLECTOR_ENABLED=true`, `OPERATIONAL_DB_ENABLED=true`, and
all four hosted/operational URL and service-role variable names configured. Set
`COLLECTOR_UNIVERSE_SYNC_ENABLED=false` to disable just this child. The default
`COLLECTOR_UNIVERSE_SYNC_INTERVAL_MS` is **60000** (60 seconds); positive integers below **10000**
are clamped to 10 seconds. Missing, empty, invalid, zero, negative, fractional, or values beyond
Node's 2,147,483,647ms timer range use 60 seconds. These are server-only variables, never `VITE_*`.
For a standalone machine run: `npm run collector-universe-sync -- --mode production`.

The first pass is scheduled immediately; each subsequent pass starts one interval after the prior
pass completes, so slow reads extend the cadence and passes do not overlap. Every completed pass
logs one short line:
`[collector-universe-sync] heartbeat status=unchanged|assigned|skipped symbols=… newest_assignment_at=…`.
The assignment time is the most recent successful assignment **by this process** (`never` until it
assigns); the count is the last successfully observed universe (`unknown` until then). A failed pass
logs `status=skipped`, a bounded, redacted error and `retry=next_interval`, retaining those last-known
fields. Failed hosted reads cannot empty the universe, and operational DB startup failures are
retried without ending the child or stopping its siblings.

The pending timer remains referenced, keeping the standalone child alive (#326). All dependencies
used by later ticks are imported statically before Vite's `runnerImport` closes its module runner
(#327); there are no delayed module imports. `stop()` clears the timer and prevents rescheduling,
including after an in-flight pass. Startup/process crashes use the host's existing restart backoff
and non-essential crash-loop breaker: only this child stops after repeated crashes while the
collector, publisher and other children continue. One more child must be monitored, with two small
hosted reads per successful interval and operational assignment writes only on changes.

In production, `npm run mvp:check -- --mode production` adds a read-only **collector universe** line.
It uses the same hosted derivation as `syncCollectorUniverse`; missing or extra local subscriptions
produce `WARN` (stale universe), equality produces `PASS`, and read failures produce `WARN`. The
check never repairs or writes subscriptions.

### Running collector response: no restart required

Code paths: `CollectorRuntime.startTimers` / `reconcileSubscriptions` in
`src/lib/market/collector.server.ts`, and `BinanceFuturesCollector.reconcile` / `bootstrap` in
`src/lib/market/collector.ts`. The running collector re-reads the operational assignment every
**30 seconds**. Reconciliation removes dropped symbols from live memory, adds new ones and
bootstraps each new symbol's completed **1m, 15m, 1h and 4h** series sequentially from REST
(limit 300 per frame; the developing candle is excluded). After reconciliation it sends the
WebSocket stream differences as `UNSUBSCRIBE` / `SUBSCRIBE` and schedules movement history backfill.
It needs **no process restart or manual subscription command**. Removal preserves stored candles;
assignment immediately invalidates the removed symbols' health. Benchmarks never leave the universe,
and a pair with another eligible watcher remains assigned.

### How long until a newly watched symbol has a row

`addSymbol` in `src/lib/db.ts` inserts the hosted watchlist row; it does not call the hook. With the
machine running, collection enabled for the account, healthy hosted/local stores and Binance, and
the default sync interval:

1. **Hosted watchlist to assignment:** up to roughly **60 seconds**, plus the previous/current
   sync's hosted and operational read/write time (the interval starts after completion).
2. **Assignment to collector discovery:** up to **30 seconds**, plus any active collector work.
3. **Bootstrap:** REST and local persistence time for the new symbol's four native frames. The
   existing provider has an 8-second timeout per HTTP fetch; metadata resolution can add a fetch.
   Bootstrap stores completed 1m candles immediately and does not wait for 24 hours of live data.
   Quote calculation needs only a usable newest 1m close to publish a price; longer change windows
   remain unavailable until their history is present. Several new symbols bootstrap sequentially
   and can take longer; outages/recovery have no fixed completion bound.
4. **First publication:** the quote publisher reads subscriptions on every pass and publishes on
   its next pass, at the next minute boundary plus **3 seconds** (allow roughly **63 seconds**).
   Startup has a **5-second** delay. Behind candles get up to four additional **5-second** retries.
   Add local read and hosted upsert time; failed hosted writes retry on the next candle.

The scheduled waits total roughly **153 seconds**, plus read/write/bootstrap time, so a single
newly added supported pair usually has a hosted quote **within a few minutes** under healthy
conditions. This is a code-derived expectation, not a measured deadline or an outage guarantee.
Increasing the sync interval increases that latency. Dashboard cards see it on their next market
fetch: automatic market refresh polls every **60 seconds** when enabled, with unavailable pairs
also retried as one batch using jittered backoff. With automatic activity paused, use the dashboard's
market refresh control. Refreshing the page does not reconcile the
universe and is not required for machine collection/publication.

### Verify on the machine

- Start the operational stack and the production worker host through the existing `npm run up`
  flow. With account monitoring and collection enabled, add a supported non-benchmark pair (for
  example ADAUSDT) on the site. Without calling the hook, running a sync command or restarting
  the collector, watch for the sync child's `status=assigned`, the collector's changed symbol
  set/subscription acknowledgement and the publisher's next heartbeat. The pair should have a
  price within a few minutes under the conditions above; refresh market cards if auto-refresh is
  paused. Run `npm run mvp:check -- --mode production` to confirm the universe matches.
- Remove that pair's **last eligible watcher**. On the next successful sync it leaves the local
  assignment; within the collector's next 30-second poll it is removed from streams. Other
  eligible watchers and the six benchmarks deliberately retain their pairs. Historical candles
  and existing hosted quote rows are not deleted.
- Temporarily make hosted reads unavailable to the sync child, without altering credentials or
  the operational DB. Observe `status=skipped … retry=next_interval` repeatedly with the same
  child pid; the collector and quote publisher stay running. Restore access and confirm a later
  pass succeeds. If hosted access is blocked for the whole machine, the publisher also reports
  its existing hosted-write failures but remains independent.
