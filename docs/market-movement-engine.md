# Market movement engine

Integration of the market-wide movement engine (issues #70–#73) into the shared Binance USDⓈ-M
collector (issue #74). It is collector-driven: it runs with every dashboard tab closed and never
depends on a browser timer or a per-user engine.

## Data flow

```text
#26 aggTrade (one shared WebSocket)
  -> #70 synchronized five-second buckets (memory only)
  -> #71 pure metrics
  -> #72 structured classification
  -> #73 episode lifecycle
  -> bounded `market_state_current` (operational DB)
```

Accepted, validated `aggTrade` observations remain in a bounded collector transport buffer and are
sent with each explicit finalization boundary to the authenticated Python movement endpoint. The
Python service invokes the shared pure `market_analysis.movement` engine and retains its buckets
for canonical Python #71 evaluation and Python #72 classification. TypeScript transports both
results to the existing #73 lifecycle, which remains a temporary TypeScript consumer pending
its own correction ticket.
TypeScript owns WebSocket ingestion, boundary scheduling, transport, and persistence. No additional
WebSocket, raw tick persistence, or per-five-second append-only stream
is created. #26 completed-candle bootstrap/recovery is unchanged.

Each finalized bucket retains its provider/instrument/price type and the last real trade's exchange
event/trade times plus its local receive time (`lastRealReceivedAt`); carry-forward buckets preserve
those timestamps. The bounded current evidence exposes the latest real `lastReceivedAt` alongside
`lastSourceEventTime`/`lastTradeTime`, so receive-time provenance survives without raw-tick
persistence.

The engine runtime (`movement-engine.server.ts`) starts only inside the authoritative collector
process (the operational lease owner) and evaluates each newly finalized five-second boundary
exactly once for the shared universe.

## Live finalization / lateness watermark

The existing collector runtime delays each explicit Python boundary request by an explicit grace:

```text
MOVEMENT_FINALIZATION_GRACE_MS = 2000   # V1 default, server-only
finalizable_boundary = floor((wall_clock_now_ms - grace_ms) / 5000) * 5000
```

- Wall clock only decides _when_ an exchange-time bucket is safe; bucket identity stays
  exchange/event-time based.
- At exactly a boundary `B` the result is `B - 5000`, so the current wall-clock boundary is never
  finalized immediately.
- A newer accepted trade still advances earlier buckets naturally through #70's trade-time logic.
- The periodic evaluator uses the boundary only to close quiet/no-trade symbols and trigger one
  synchronized evaluation per boundary.
- A trade whose exchange time belongs to an already-finalized bucket is rejected, never rewritten,
  and counted as a late-after-finalization event in diagnostics.

The grace is explicit, server-side and versioned. The effective config version deterministically
identifies the grace in force (`movement-finalization-config-v1:grace-<ms>`), so tuning
`MOVEMENT_FINALIZATION_GRACE_MS` (integer, 0–60000) is a new config version rather than a silent
change under an unchanged version. It must never be a `VITE_*` variable. The effective
`finalizationConfigVersion` and `finalizationGraceMs` are persisted in the bounded current evidence
and exposed through the authenticated current-state and compact diagnostics contracts.

## Universe semantics

The shared universe is the union of all watched Binance USDⓈ-M perpetual contracts
(`watchlist_items`), identified by `binance-usdm-public-market`. Its version is a deterministic
hash of the sorted membership (`market-universe-v1:<hash>`).

- Membership changes produce a new universe version. #73 terminates the active episode with
  `universe_changed` instead of silently redefining its denominator.
- With fewer than five configured contracts, market-wide state is `UNAVAILABLE`; per-contract
  metrics may still exist.
- There is no per-account market engine. All consumers read the same shared, market-wide state.

## Historical normalization input

#71 receives raw canonical completed one-minute candles from the operational database
(`collector_recent_candles`) through `get_collector_movement_candles`. Python derives the
normalization input for each explicit evaluation boundary:

- Window returns use the same horizon as the live window (`log(close[t]/close[t-window])`).
- Sampling is exchange-time aligned, not index/phase dependent: 1m stays canonical 1m, while 5m and
  15m endpoints (and their return/notional windows) align to fixed 5-minute / 15-minute epoch
  boundaries. Adding or removing irrelevant leading candles cannot shift the sampled population.
- Observations use a 1m candle's _completed_ boundary (`openTime + 60s`), so the close used for a
  boundary is the candle that completes immediately before it — e.g. the 12:05 boundary uses the
  12:04-open candle's close, never the 12:05-open candle. The prior endpoint is exactly `w` minutes
  earlier and the notional window covers the same aligned interval.
- `usableCoverageMs` is the trailing contiguous run; a gap discards earlier, non-comparable candles.
- `previousNotionalVolumes` are the last `rvolComparisonWindows` exact Binance quote-volume
  sums over those completed aligned intervals.
- Five-second history is never fabricated from candles. A restart warms the live bucket path per
  #70/#73; it does not backfill buckets.

If retained candle coverage is below #71's three-day minimum, the affected symbols are excluded with
`INSUFFICIENT_NORMALIZATION_HISTORY` rather than weakening the contract. Candle retention defaults
to 8 days (`OPERATIONAL_CANDLE_RETENTION_DAYS`) to retain the 7-day lookback and 15-minute
historical return warm-up.

## Current-state exposure

Authenticated server-side reads:

- `getMarketMovementCurrentState` (`src/lib/market-movement.functions.ts`) returns the structured
  contract for later #30/#31 consumers: primary 5m direction/pace, 1m/15m context, pace/acceleration,
  directional/material breadth, median movement, dispersion, RVOL/participation, isolated outliers,
  exact configured/included/excluded universe, timestamps (including the latest real receive time),
  versions, the effective finalization config version/grace, most recent transition, and the
  distinct LIVE / WARMING / STALE / UNAVAILABLE status.
- `getOperationalState` includes a compact `movementEngine` diagnostics block rendered on the
  settings page (status, configured/eligible counts, primary 5m state, last evaluation boundary,
  versions, finalization config/grace, most recent transition, late-after-finalization count).

The `STALE` threshold is an explicit, version-independent constant (`MOVEMENT_ENGINE_STALE_AFTER_MS`,
60s) chosen to clear the 30-second persistence cadence plus finalization lag, so a healthy engine
does not flicker to `STALE` from normal persistence/timer jitter. The write frequency is unchanged.

Reads go through the operational store's explicit field mapping; the service-role credential never
reaches the browser. This integration does not build #30's dashboard, #31 setups/scoring, or any
prediction.

## Persistence

Reuses #73's atomic `persist_market_episode_lifecycle_step` (append events + upsert current state in
one transaction). Current state is bounded to one row per `(universe_id, primary_window_minutes)`,
written on transitions or the default 30-second cadence, and protected by the existing monotonic
`evaluation_boundary_time` guard. No five-second append-only stream is created.

Durable persistence is mandatory. A persistence-required batch (current snapshot plus zero or more
transitions) is held pending until the atomic write succeeds; later boundaries are not evaluated
while a batch is pending, and the exact deterministic events/snapshot are retried (event IDs are
unchanged and the DB append stays idempotent). Lifecycle persistence cadence is acknowledged only
after the write succeeds, so a transient database failure cannot lose a STARTED / STRENGTHENED /
WEAKENED / REVERSED / ENDED transition.

Startup restore fails closed. Evaluation never begins with a null lifecycle state: it waits until an
operational read establishes either that no persisted state exists, or that persisted state has been
restored via #73's restart-interruption semantics. Transient restore and normalization-history read
failures use bounded exponential backoff rather than retrying on every one-second tick.

Lease handoff is treated as a change of ownership. `stop()` (which the collector calls whenever it
loses or gives up the authoritative lease) discards all in-memory lifecycle ownership — engine
state, last-evaluated boundary, pending persistence and cached history. The next `start()`
reloads `market_state_current` with #73 restart/interruption semantics before evaluating, so a
reacquiring instance never resumes from a previous tenure's counters and never writes a stale
tenure's pending batch over a newer owner's durable state.

Raw candle history is re-registered when the completed-minute cutoff, universe, movement session,
or collector backfill changes. Python derives aligned historical inputs separately for each
requested evaluation boundary.

## Migration

Apply only to the external operational database:

```text
operational-db/supabase/migrations/20260925200000_movement_normalization_history.sql
```
