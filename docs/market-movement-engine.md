# Market movement engine

Integration of the market-wide movement engine (issues #70–#73) into the shared Binance USDⓈ-M
collector (issue #74). It is collector-driven: it runs with every dashboard tab closed and never
depends on a browser timer or a per-user engine.

## Data flow

```text
#26 aggTrade (one shared WebSocket)
  -> bounded collector transport and explicit evaluation boundary
  -> shared Python #70 buckets/state
  -> shared Python #71 metrics
  -> shared Python #72 classification
  -> shared Python #73 episode lifecycle
  -> TypeScript validation and persistence
  -> bounded `market_state_current` (operational DB)
```

Accepted, validated `aggTrade` observations remain in a bounded collector transport buffer and are
sent with each explicit evaluation boundary to the authenticated Python movement endpoint. The
shared Python `market_analysis.movement` package owns the canonical deterministic #70 bucket/state,
#71 metrics, #72 classification, and #73 lifecycle calculations for live operation and replay.
For lifecycle calculation, TypeScript sends the explicit previous lifecycle state and inputs;
Python returns the versioned canonical results. TypeScript orders boundary requests, validates those
results, and owns operational persistence. The FastAPI endpoint adapts requests to the shared Python
package; it does not implement the formulas. No TypeScript analytical fallback is active. TypeScript
also owns WebSocket ingestion, boundary scheduling, and transport. No additional WebSocket, raw tick
persistence, or per-five-second append-only stream is created. #26 completed-candle bootstrap/recovery
is unchanged.

Each finalized bucket retains its provider/instrument/price type and the last real trade's exchange
event/trade times plus its local receive time (`lastRealReceivedAt`); carry-forward buckets preserve
those timestamps. The bounded current evidence exposes the latest real `lastReceivedAt` alongside
`lastSourceEventTime`/`lastTradeTime`, so receive-time provenance survives without raw-tick
persistence.

The engine runtime (`movement-engine.server.ts`) starts only inside the authoritative collector
process (the operational lease owner) and evaluates each newly finalized five-second boundary
exactly once for the shared universe.

## V1 constants and ownership

The collector owns ingestion, REST recovery, transport ordering and connection health. Python owns
canonical movement, normalization, classification, lifecycle mathematics and replay semantics.
TanStack owns auth, orchestration, validation, privileged permanent-database writes and authenticated
reads. The operational database holds only explicitly assigned bounded working state; verified
archive files and manifests supply historical research. These responsibilities remain separate.

| Current value | Owner / classification and purpose | Effect on scientific/config meaning | Evidence required before changing |
| --- | --- | --- | --- |
| 5-second analytical cadence | Python / **scientific** boundary grid | Defines analytical time resolution | New scientific/config identity and research/revalidation |
| 1m / 5m / 15m adjacent windows | Python / **scientific** return and adjacent-window comparisons | Defines compared intervals and calculation semantics | New scientific/config identity and research/revalidation |
| 7-day lookback; 3-day minimum usable coverage; median/MAD estimator | Python / **scientific** normalization reference and eligibility | Changes normalized movement, breadth and classification | New scientific/config identity and research/revalidation |
| 15s real-trade endpoint/carry freshness | Python / **scientific** endpoint eligibility | Determines which exact endpoints are usable | New scientific/config identity and research/revalidation |
| 8-day completed-candle retention | Application configuration / **resource/retention** supporting canonical inputs | Stored surplus is neutral with identical inputs; insufficient retention can make evidence unavailable | Demonstrate complete raw-history coverage for each consumer |
| 5m **and** at most 2,000 logical aggTrade items per contract | Collector / **resource/retention**, bounded snapshot | Not canonical movement history or a guaranteed recovery interval | Concrete consumer-derived sizing evidence; no such invariant is currently demonstrated |
| 420 boundary records (`DEFAULT_HISTORY_BUCKETS`) | Python / **resource/retention** derived from window minimum | Enough exact history is mandatory; surplus is neutral with identical inputs | Window coverage and safety-margin evidence; preserve current constructor/spec minimum |
| 2,000ms finalization grace | TanStack live orchestration + Python explicit-boundary finalization / **operational safety** | Does not change return math; can change admitted live evidence and is in replay/config identity | Explicit versioned policy plus measured latency/late-event evidence; none currently justifies another value |
| 60s movement evaluation/current-state age | Application / **operational safety**, consumer-status diagnostic | Does not define endpoint freshness or market classification | Consumer cadence, finalization-lag and status-jitter evidence |
| 30s socket silence | Collector / **operational safety**, connection stale handling | Does not define canonical endpoint freshness | Connection-health and recovery evidence |
| Newest 260 completed candles per canonical series | Operational DB / **resource/retention**, TA availability margin | Stored surplus is neutral with identical inputs; changing the TA input prefix can change outputs | TA history/catch-up coverage and input-prefix impact evidence |
| 15-minute directional alert cooldown (user configurable) | Individual cumulative monitoring / **product/user notification policy** | Not market-wide V1, episode reversal or predictive evidence; suppression does not create a market state | User notification/product evidence |
| One Python worker / one replica for live movement | Python deployment / **operational safety**, authoritative state owner | Not mathematical algorithm identity | Designed ownership, partitioning, fencing, ordering and recovery semantics before scaling |
| 672-point 15-minute grid (7 days); 576 reference windows; 288 minimum | "Longer moves" / **slow, provisional, descriptor** (`slow-config-v1`) | Separate identity; no V1 effect | New slow config version; see [slow-movement.md](slow-movement.md) |
| z threshold 2; coordinated share 0.70; minimum 5 included pairs | "Longer moves" / **slow, provisional, descriptor** (`slow-config-v1`) | Separate identity; no V1 effect; creates no alerts | Research (#373) and a new slow config version |

Each horizon compares the current window with the immediately preceding equal-length window.
The largest calculation needs `2 × 15 minutes / 5 seconds + 1 = 361` records, including the boundary.
The current 420 capacity adds **59 records**, approximately **4m55s** of boundary-span headroom.
That margin is specified capacity rather than proven mathematical necessity.
See [collector bounds](./binance-futures-collector.md), [candle retention](./operational-database.md#reads-retention-and-synchronization),
[TA input history](./technical-analysis.md#history-policies) and [Python deployment](./python-api.md#movement-service-deployment-invariant).

## Live finalization / lateness watermark

TanStack live movement orchestration computes the grace-based finalizable boundary. The collector
transports/advances the explicit ordered boundary and input; Python permanently finalizes that
boundary and owns canonical bucket mathematics:

```text
MOVEMENT_FINALIZATION_GRACE_MS = 2000   # V1 default, server-only
finalizable_boundary = floor((wall_clock_now_ms - grace_ms) / 5000) * 5000
```

- Wall clock plus grace decides _when_ the explicit trade-time boundary is safe to finalize;
  Binance trade/transaction time `T` determines five-second bucket membership.
- At exactly a boundary `B` the result is `B - 5000`, so the current wall-clock boundary is never
  finalized immediately.
- A newer accepted trade still advances earlier buckets naturally through #70's trade-time logic.
- The periodic evaluator uses the boundary only to close quiet/no-trade symbols and trigger one
  synchronized evaluation per boundary.
- A trade whose trade/transaction time `T` belongs to an already-finalized bucket is rejected, never rewritten,
  and counted as a late-after-finalization event in diagnostics.

The grace is explicit, server-side and versioned. The effective config version deterministically
identifies the grace in force (`movement-finalization-config-v1:grace-<ms>`), so tuning
`MOVEMENT_FINALIZATION_GRACE_MS` (integer, 1–60000) is a new config version rather than a silent
change under an unchanged version. It must never be a `VITE_*` variable. The effective
`finalizationConfigVersion` and `finalizationGraceMs` are persisted in the bounded current evidence
and exposed through the authenticated current-state and compact diagnostics contracts.

The 2s default governs evidence admission. Binance trade/transaction time `T` defines five-second
bucket membership. Event time `E` and local receive time are preserved as provenance/diagnostic
timestamps; they do not determine bucket membership. Wall clock plus finalization grace determines
when the explicit trade-time boundary is safe to finalize. Late observations never rewrite finalized
history and remain diagnostic evidence. Changing grace requires the versioned decision and
measurements listed above.

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
to 8 days (`OPERATIONAL_CANDLE_RETENTION_DAYS`). The raw-history requirement is approximately
**7 days + 16 minutes**: the scientific lookback, longest 15-minute return window, and the prior 1m
candle needed at the return boundary. The worker therefore requires an 8-day whole-day operational
setting for default V1; 8 days is not itself a scientific market parameter. The three-day minimum
is an availability gate, not permission to shorten the normal seven-day reference distribution.
Median/MAD, aligned sampling, gaps and degenerate/unavailable behavior retain their current semantics.

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
This measures movement evaluation/current-state snapshot age. It is distinct from the collector's
30s socket-silence rule and Python's 15s real-trade endpoint/carry freshness.

Reads go through the operational store's explicit field mapping; the service-role credential never
reaches the browser. This integration does not build #31 setups/scoring or any prediction.

## Consumer: the dashboard's "Current assessment" (#30)

The Market page reads `getMarketMovementCurrentState` (now returning `readStatus`: `ok`, `store_disabled`
when the site's deployment has no operational store, or `read_failed`) and maps it with the pure functions in
`src/lib/market/assessment.ts`. No engine logic, threshold or prediction was added; every cut-off is either the
engine's own constant or a named freshness window.

| Shown | Fed by |
| --- | --- |
| One status line (Live · updated N ago / Updating… / Warming up / machine not reporting / delayed / refresh failed / unavailable); a reading that is not fresh is dimmed with "as of 5:43 PM" | `status` (`deriveMovementEngineStatus` on the machine's clock), `timestamps.evaluationBoundaryTime`, `publishedAt` / `timestamps.engineUpdatedAt` |
| Primary 5m direction and pace | `primary.directionState`, `primary.evidence.pace` |
| 1m / 5m / 15m: "4 of 6 pairs fell materially in 15 min", rising/falling counts | `context[w].breadth` (`rising`, `falling`, `materialRising`, `materialFalling`, `denominator`) |
| What "material" means, with its value | `DEFAULT_MARKET_MOVEMENT_CONFIG.materialZ` / `flatZ`, only when the evidence's `movementConfigVersion` equals that config's version; for any other saved version the page says it does not know the value and shows no number |
| Median move, dispersion, isolated outliers | `medianRawReturn` (as a percent), `dispersionMadNormalizedMovement`, `isolatedOutliers` |
| Universe, exclusions with reasons, boundary / latest receive / exchange event / state-saved times | `universe`, `timestamps` |
| Per pair: normalized move for 1m / 5m / 15m with a label, volume, outlier badge | `context[w].movementSnapshot.symbols[]` (`normalizedZ`, `materialRising/Falling`, `rvol`, `outlierCandidate`, `exclusionReasons`); label bands are the engine's `flatZ` and `materialZ` |
| Per pair: TA lean, score, RSI, trend, volatility, patterns, snapshot age for 15m / 1h / 4h | the latest `ta_signals` row per pair and timeframe (RLS client query, three queries for all pairs) |
| Freshness (in Diagnostics, and behind the grades): market stream, movement evaluation, completed candles, TA snapshots | `sourceTimeEvidence[].lastReceivedAtMs` measured back from the evaluation boundary (60 s), machine-side lateness = published or saved time minus the evaluation boundary (60 s), quote `lastCandleAt`/`stale` against now (10 min, `QUOTE_STALE_MS`), TA candle close against now (one timeframe plus 10 minutes, `TA_RUN_ALLOWANCE_MS`); each limit has a 3 s display grace (`FRESH_DISPLAY_GRACE_MS`) |

Fewer than 5 watched contracts shows UNAVAILABLE with the reason; per-pair rows still render. The movement state
is polled every 30 s (`MOVEMENT_POLL_INTERVAL_MS`, the engine's and the publisher's own cadence) behind the market
refresh flag; prices and TA keep the shared 60 s.

**Freshness rules (stale is a fault report, never the normal state).**

- *Late engine* is judged on the machine's clock only: published time (copy) or saved time (direct read) minus the
  evaluation boundary, against the engine's 60 s. The poll and publish delays never count. The published read maps
  the engine status at `published_at` for the same reason.
- *Machine not reporting*: the browser has heard nothing newer than `PUBLISHED_COPY_OFFLINE_AFTER_MS` = publish
  interval (30 s) + poll (30 s) + 30 s margin = **90 s** (`publishedAt`, or `engineUpdatedAt` on a direct read).
- *Grades* (`gradeFreshness`): an item within its limit (+3 s grace) is **fresh**; past it, it is **catching up**
  while a refresh is in flight, until a refresh after the crossing has landed, for 15 s after that refresh, while a
  self-heal check runs or while a resumed tab settles; it is **stale** only when a refresh after the crossing still
  left it past its limit, when the refresh failed, or when the machine is offline.
- *Pre-emptive refetch*: the page arms one refetch for the moment the soonest datum crosses its limit.
- *Resume*: a tab hidden for more than 5 s refetches the movement, price and TA queries on return and holds its clock
  (`useNow(…, { hold })`) until they land (at most 8 s), so throttled background timers never flash a false stale.
- *Self-heal*: TA or movement stale for 20 s in a visible tab with monitoring on runs one silent monitor check (error
  toast only), at most every 3 minutes, never while a manual check is pending or the machine is offline.

Not shown, by design:
support/resistance levels, news and scheduled events, setups, scores, entries or any forecast (#31 and later).

**Latest TA per pair.** The page asks the hosted SQL function `latest_ta_signals(timeframe, symbols)` once per
timeframe (`DISTINCT ON (symbol)`, newest `candle_at`, at most 10 symbols, `SECURITY INVOKER` so RLS scopes it to
the caller), so a pair that fell behind the others still returns its own newest snapshot.

**Pair block.** Always visible: the 15m movement chip and three TA pills (15m, 1h, 4h; muted when not fresh). A
"Details" button (closed by default on every card) opens the z-scores, volume, score, RSI, trend, volatility,
patterns and snapshot age. A TA lag shows "Updating…", and only after a self-heal check did not clear it "Technical
analysis last updated N ago"; never a red badge.

### Published copy for the deployed site (`market_state_published`)

The Cloudflare Worker cannot read the machine's operational database, so the machine publishes the engine's
persisted current state and the Worker reads that copy.

- **What leaves the machine:** the same payload `getMarketMovementCurrentState` maps from (direction, pace,
  versions and the bounded current evidence: window breadth and per-pair movement metrics, universe, timestamps),
  without the lifecycle internals. Market data only, no account data. One hosted row per universe, overwritten in
  place (`published_at`, the engine's own `evaluation_boundary_ms`), at most 512 KiB, no history.
- **How:** the worker-host child `movement-state-publisher` (`src/worker/movement-state-publisher.ts`) POSTs to the
  site hook `POST /api/public/hooks/market-state-publish`, authenticated with the shared machine secret
  (`RUNNER_GATEWAY_SECRET`) like the alert-chart upload hook; the site is the privileged writer through the
  service-role functions `publish_market_state` (an older evaluation never replaces a newer one) and
  `touch_market_state`. The machine holds no hosted service key for this and no new secret. Structure, cadence
  and failure handling follow the quote publisher (#323), which instead writes with the service key.
- **Cadence:** at most once per 30 s (the engine's write cadence). A changed evaluation publishes the payload; an
  unchanged one sends only a tiny heartbeat (the payload is skipped), so a stalled engine reads as STALE and a dead
  machine reads as offline. Best effort: a failure is logged and retried in full on the next pass and never blocks
  the engine; a vanished hosted row is republished.
- **Read:** `getMarketMovementCurrentState` reads the operational store when it is enabled (unchanged mapping).
  When it is disabled it reads the published row with the caller's own session (RLS: signed-in users read, only the
  service role writes). `readStatus` is `ok` (operational), `published` (copy, with `publishedAt`),
  `published_none` (no copy yet), `read_failed`, or `store_disabled`.
- **States:** the engine's own STALE rule applies to the evaluation time as of `published_at` (machine-side
  lateness). **Machine not reporting** is shown when `published_at` is older than `PUBLISHED_COPY_OFFLINE_AFTER_MS`
  = publish interval (30 s) + poll (30 s) + 30 s margin = **90 s**: derived from cadences, not a market threshold.
  The status line then reads "Your machine isn't reporting. Showing the last reading from 5:43 PM." with a "Check
  now" button; the source and per-item details are in the strip's Diagnostics. An old copy is never shown as live.
- **Settings (machine):** `MOVEMENT_STATE_HOSTED_URL` (site origin; falls back to `ALERT_CHART_HOSTED_URL`) with
  `RUNNER_GATEWAY_SECRET`; `MOVEMENT_STATE_PUBLISH_ENABLED=false` turns it off. Apply the hosted migration
  `20261016170000_market_state_published.sql` and restart the host.

### Longer moves (1 h / 4 h / 12 h)

A separate, versioned multi-hour descriptor (`slow-movement-v1`) is shown below the V1 tiles. It leaves V1 untouched,
creates no alerts and is not a predictor. Algorithm, constants, transport and UI states: [slow-movement.md](slow-movement.md).

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

### Operational state and research history

Live operational state retains only what live processing, recovery and bounded current consumers
require. Historical research uses verified archive/file partitions with checksums and manifests
outside transactional PostgreSQL; see [historical replay](./historical-replay.md#archive-research-and-live-history).
The live five-second ring remains bounded process/runtime analytical state. Current architecture
does not justify long-term PostgreSQL persistence of every raw aggTrade or every five-second bucket.

A future persistence proposal must name the consumer, required resolution, retention horizon,
restore/recovery behavior, expected volume, authoritative writer, and why archive/replay
reconstruction is insufficient. Possible future usefulness alone is not enough.

## Known follow-ups

These are separate correctness/design work, not changes to the current constants:

- Normalization validation accepts different valid scientific parameters under a reused nonempty
  version label; effective-parameter/config identity needs hardening.
- The one-worker invariant is documented but not explicitly pinned at every Uvicorn startup;
  deployment settings such as `WEB_CONCURRENCY` may change the worker count.
- The collector's exact 5m/2,000-item snapshot sizing lacks a demonstrated consumer-derived invariant.

## Operational baseline

Apply the current baseline only to a fresh or reset external operational database:

```text
operational-db/supabase/migrations/20261004000000_operational_schema.sql
```
