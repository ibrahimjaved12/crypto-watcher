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

Accepted `aggTrade` observations feed the existing `FuturesMovementBuckets` inside
`BinanceFuturesCollector`. No additional WebSocket, no raw tick persistence, and no per-five-second
append-only stream is created. #26 completed-candle bootstrap/recovery is unchanged.

The engine runtime (`movement-engine.server.ts`) starts only inside the authoritative collector
process (the operational lease owner) and evaluates each newly finalized five-second boundary
exactly once for the shared universe.

## Live finalization / lateness watermark

`advanceTo(boundary)` permanently finalizes exchange-time buckets, so the live path delays
finalization by an explicit grace:

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

The grace is explicit, server-side and versioned (`movement-finalization-config-v1`). Set
`MOVEMENT_FINALIZATION_GRACE_MS` (integer, 0–60000) to tune it; it must never be a `VITE_*`
variable.

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

#71 requires caller-supplied historical normalization data. The engine derives it from canonical
completed one-minute candles in the operational database (`collector_recent_candles`), read through
`get_collector_movement_candles` and refreshed periodically:

- Window returns use the same horizon as the live window (`log(close[t]/close[t-window])`).
- `usableCoverageMs` is the trailing contiguous run; a gap discards earlier, non-comparable candles.
- `previousNotionalVolumes` are the last `rvolComparisonWindows` completed-window notionals
  (base volume × close as the quote-notional proxy).
- Five-second history is never fabricated from candles. A restart warms the live bucket path per
  #70/#73; it does not backfill buckets.

If retained candle coverage is below #71's three-day minimum, the affected symbols are excluded with
`INSUFFICIENT_NORMALIZATION_HISTORY` rather than weakening the contract. Candle retention defaults
to 7 days (`OPERATIONAL_CANDLE_RETENTION_DAYS`).

## Current-state exposure

Authenticated server-side reads:

- `getMarketMovementCurrentState` (`src/lib/market-movement.functions.ts`) returns the structured
  contract for later #30/#31 consumers: primary 5m direction/pace, 1m/15m context, pace/acceleration,
  directional/material breadth, median movement, dispersion, RVOL/participation, isolated outliers,
  exact configured/included/excluded universe, timestamps, versions, most recent transition, and the
  distinct LIVE / WARMING / STALE / UNAVAILABLE status.
- `getOperationalState` includes a compact `movementEngine` diagnostics block rendered on the
  settings page (status, configured/eligible counts, primary 5m state, last evaluation boundary,
  versions, most recent transition, late-after-finalization count).

Reads go through the operational store's explicit field mapping; the service-role credential never
reaches the browser. This integration does not build #30's dashboard, #31 setups/scoring, or any
prediction.

## Persistence

Reuses #73's atomic `persist_market_episode_lifecycle_step` (append events + upsert current state in
one transaction). Current state is bounded to one row per `(universe_id, primary_window_minutes)`,
written on transitions or the default 30-second cadence, and protected by the existing monotonic
`evaluation_boundary_time` guard. No five-second append-only stream is created.

## Migration

Apply only to the external operational database:

```text
operational-db/supabase/migrations/20260925200000_movement_normalization_history.sql
```
