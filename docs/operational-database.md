# Operational PostgreSQL

Lovable PostgreSQL remains the permanent application database. A second Supabase/PostgreSQL
instance owns bounded, frequently updated working state when `OPERATIONAL_DB_ENABLED=true`.
TanStack remains the only privileged writer to Lovable. The collector worker is a headless
ingestion process that is privileged only on the operational database and holds no Lovable
credentials; Python remains calculation-only.

## Ownership

| State domain                                             | Authoritative owner when enabled | Writer                                                                       |
| -------------------------------------------------------- | -------------------------------- | ---------------------------------------------------------------------------- |
| Shared Binance completed candles (1m, 15m, 1h, 4h)       | Operational DB                   | Leased collector worker                                                      |
| Completed-candle TA input (collector mode)               | Operational DB                   | Collector worker writes; TanStack reads, then writes `ta_signals` to Lovable |
| Collector checkpoint/freshness/health                    | Operational DB                   | Leased collector worker                                                      |
| Collector subscription universe (derived input)          | Operational DB                   | TanStack operational repository assigns; collector worker reads              |
| Legacy per-user candles/checkpoints (collector disabled) | Operational DB                   | Request-driven TanStack monitor                                              |
| Monitor-run diagnostics                                  | Operational DB                   | TanStack operational repository                                              |
| Outbox/retry/dead-letter state                           | Operational DB                   | TanStack operational repository; currently dormant                           |
| Auth, users, watchlists, settings, notes                 | Lovable                          | Existing Lovable paths (TanStack only)                                       |
| Baseline, directional cooldown and alert insertion       | Lovable                          | `process_cumulative_observation` transaction                                 |
| TA conclusions/outcomes and other permanent user history | Lovable                          | Existing TanStack paths                                                      |

No domain is dual-written. With the flag enabled, checkpoint and monitor-run writes do not
touch their legacy Lovable tables. Operational-store failure is visible and never falls back to
Lovable. Completed candles have no Lovable copy. The application derives the collector's shared
subscription universe from Lovable watchlists and assigns it as operational input; that set is
derived collector state, never a second watchlist authority. Reconciliation of that set is
application-owned and independent of `SCHEDULED_MONITOR_ENABLED`: the dedicated
`/api/public/hooks/sync-collector-subscriptions` hook is the initial and ongoing mechanism the
deployment schedules, and the server's best-effort first-request pass is only a safety net for a
missed run. The scheduled monitor route never reads Lovable while disabled.

While `BINANCE_COLLECTOR_ENABLED=true`, the application's completed-candle TA reads canonical
completed candles from this operational store through `readCollectorTACandles` instead of fetching
a second live exchange candle series. The read carries the collector's recorded per-candle
provenance — exact provider/instrument identity, endpoint, transport, candle open/close time,
the exchange event time when one exists (absent for REST), receive time, and OHLCV — and never
reconstructs an endpoint, retrieval time, or exchange event time.
A single series can mix WebSocket live candles with REST bootstrap/recovery candles, so the
endpoint and transport are per candle: `/fapi/v1/klines` is recorded only for actual REST rows.
Missing or stale operational history fails the affected TA frame visibly; it is never silently
substituted with another calculator or live source. TanStack still determines due work, builds the
versioned Python request from that persisted evidence, validates responses, and is the sole
privileged `ta_signals` writer in Lovable.

## Configuration and baseline schema

Server-only variables:

```text
OPERATIONAL_DB_ENABLED=true
OPERATIONAL_SUPABASE_URL=https://<operational-project>.supabase.co
OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY=<server secret>
OPERATIONAL_CANDLE_RETENTION_DAYS=8
OPERATIONAL_MONITOR_RUN_RETENTION_DAYS=30
OPERATIONAL_OUTBOX_MAX_ATTEMPTS=10
BINANCE_COLLECTOR_ENABLED=true
```

Never create `VITE_*` forms. Startup validation rejects them. The collector worker needs only the
operational variables above plus `BINANCE_COLLECTOR_ENABLED` and `MOVEMENT_FINALIZATION_GRACE_MS`;
it never requires the main Lovable `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`, because it holds no
Lovable credentials. Only the TanStack application is configured for the main database. Apply only
`operational-db/supabase/migrations/20261004000000_operational_schema.sql` to the operational
project; never add this file to or apply it through the root `supabase/migrations/`, which
remains the Lovable chain. The nested
`supabase/` directory is required by the CLI when `operational-db` is its workdir.

The operational database is disposable pre-release working state. Recreate/reset it from
this single current baseline; historical operational migration evolution is intentionally
not preserved yet, and no production-data migration path is required. Once production data
must survive upgrades, freeze the baseline and use forward migrations for future changes.

For a hosted operational project, use its direct PostgreSQL connection string (not a Lovable
connection) and run:

```sh
npx supabase db push --workdir operational-db \
  --db-url 'postgresql://postgres:<password>@db.<operational-project>.supabase.co:5432/postgres'
```

Alternatively, apply the SQL file through that operational project's SQL editor. Neither method
requires or authorizes a Lovable login or Lovable schema change.

Local development uses two isolated Supabase stacks and runs the application and the collector
worker as separate processes:

```sh
npx supabase start
npx supabase start --workdir operational-db
npm run env:local                 # new .env.local
npm run env:local:operational     # existing .env.local only
npm run dev:local:all             # app + collector worker
# or run them independently:
npm run dev                       # app only, no collector
npm run collector:worker          # collector worker only
```

`npm run dev:local` starts only the original main local stack, runs the app alone, and explicitly
disables operational ownership. `npm run dev:local:all` starts both stacks and runs the app plus a
separate collector worker process. `npm run dev:local:stop` stops both. The main API is at port
54321 and the operational API at 55321.
Local operational Supabase Auth runs only to issue its API/service-role credentials; application
users and authentication remain exclusively in the main local database or Lovable. The collector
worker process inherits `.env.local` but reads only the operational and collector variables; the
main-database values in that file are for the application.

## Reads, retention and synchronization

The dashboard's recent-run read is an authenticated TanStack server function. It reuses the
verified Lovable user identity, adds an explicit `user_id` predicate to operational queries,
and never returns the operational service-role credential.

The active application adapter defaults shared and legacy completed-candle retention to **8 days**
(`OPERATIONAL_CANDLE_RETENTION_DAYS`, allowed range 1–30 days) and supplies the retention argument to
SQL. Existing SQL functions retain a legacy **7-day fallback only when that argument is omitted**;
that fallback is not the active application default and remains unchanged in the baseline.
Default V1 needs approximately **7 days + 16 minutes** of raw candles: seven scientific lookback days,
the longest 15-minute return window and its prior 1m boundary candle. The collector worker requires
the 8-day whole-day setting for these inputs. Retention is a resource bound: increasing surplus with
identical canonical inputs is science-neutral; reducing required coverage can make history explicitly
unavailable and is not equivalent.

**One-minute collector candles** have their own retention, **140 days** by default
(`OPERATIONAL_MINUTE_CANDLE_RETENTION_DAYS`, allowed range 31–200 days, #239 P16). The forward
engine's `ewma-robust-hcal` sigma needs 60 days of completed horizon windows after the 28-day
intraday slot-factor warm-up and the level warm-up: the forward job reads about 120 days of 1m
candles per symbol, and on a run whose stored history starts later it backfills 130 days once from
public `GET /fapi/v1/klines?interval=1m` (limit 1500 per request, paginated, `X-MBX-USED-WEIGHT-1M`
aware) through `record_collector_candles`. The bound is enforced in two places: by
`record_collector_candles` (`p_minute_retention_days`) in the operational baseline, and by
`operationalDbConfig`. At 1440 rows per symbol per day that is about 200,000 rows per symbol;
15m, 1h and 4h candles keep the general day window.

## Candle conflicts and revisions (#239 P15)

Owner policy (2026-10-10). A candle finalised from the WebSocket stream can differ from the REST
candle for the same open time (missed trades after a disconnect; seen live as BTCUSDT 15m volume
1006.95 from the stream vs 1007.427 from REST). Before this policy the immutability check raised for
the whole recovery batch and every symbol went UNAVAILABLE.

- **Stored candles are immutable.** `collector_recent_candles` rows are never overwritten; a
  conflicting candle is never inserted.
- **A conflict never fails the batch.** `record_collector_candles` writes every non-conflicting row
  and appends each conflicting offer to `collector_candle_conflicts` (provider, instrument, symbol,
  timeframe, open time, `stored` + `stored_transport` + `stored_hash`, `offered` +
  `offered_transport` + `offered_hash`, `differing_fields`, `rest_side` = which side came from REST:
  `stored`, `offered`, `both` or `neither`, `detected_at`). The unique key (identity + offered hash)
  makes replays idempotent. The table is append-only (UPDATE/DELETE rejected) and is evidence, never
  a data source.
- **Health is not degraded.** `collector_health` gains `conflict_count_24h` and `last_conflict_at`,
  refreshed after every candle batch and every health write. The status stays as measured (for
  example LIVE); only structural problems (a gap, a missing candle) degrade it. The Control room
  shows "conflicts: n" next to the feed chip.
- **Research parity.** REST is what the Binance archive matches. `reconcile_conflicts(older_than_hours
  DEFAULT 24, p_price_tolerance DEFAULT 0.002, p_volume_tolerance DEFAULT 0.02)` appends, for every
  conflict older than the window whose offered side is REST (stored side is the stream) and whose
  only differing fields are high/low/close/volume/quote_volume within the stated tolerance (prices:
  relative difference at most 0.2 %; volumes: absolute difference at most 2 % of the larger value),
  one row to the append-only `collector_candle_revisions` (`revision_id`, `conflict_id`, identity,
  `fields` = the REST values and their hash, `source = 'rest'`, `supersedes` = the stored candle's
  hash, `tolerance`). It never modifies `collector_recent_candles`; readers keep the original candle.
  Conflicts outside the tolerance or without a REST side stay for manual review.
- **Candle versions.** `collector_candle_hash(...)` ("candle-v1": md5 over identity, open time and
  OHLCV text) identifies a candle's values. `get_collector_forward_minutes` returns it with every
  1m row; the forward job stores, with every `forward_signals` / `forward_setups` row (application
  database), `candle_version` = SHA-256 over the hashes of the 1m candles forming the decision
  candle, so any decision can say which version it used and a later revision can be traced to it.
  Hashes are never sent to Python and the forward engine's input is unchanged.

`get_collector_candle_conflicts(p_hours, p_limit)` lists recent conflicts with their revision id.
After pulling this change, reset the local operational database (the baseline is edited in place):
`supabase db reset --local --workdir operational-db`.

Monitor runs default to 30 days (allowed range 1–90 days), and inactive checkpoints expire after
30 days. Writes perform database-wide bounded cleanup. Age-based retention and the protected
**newest-260 completed-candle floor per canonical collector series** are separate mechanisms:
the floor protects TA history/catch-up availability even beyond the day window. It is not Python's
200-candle TA minimum or the collector's 300-candle bootstrap request. The application TA read horizon
is also 260 where implemented; changing its actual input prefix can affect recursive indicators.
See [TA history policies](./technical-analysis.md#history-policies).

The authenticated read reports per-user storage counts; operators can call
`get_global_storage_diagnostics()` for total row counts, oldest candle time, outbox state counts
and the oldest undelivered event, and `get_collector_storage_diagnostics()` for shared candle and
health growth. Only completed candles are persisted—never developing updates or raw `aggTrade`
events. See [the collector design](./binance-futures-collector.md) for recovery, memory bounds, and
health.

Operational working state serves live processing, recovery and bounded current consumers.
Historical research uses verified archive files, checksums and manifests outside transactional
PostgreSQL. No current consumer justifies storing every raw aggTrade or five-second bucket long term;
the live ring stays bounded Python runtime state. See the
[persistence proposal requirements](./market-movement-engine.md#operational-state-and-research-history).

No current durable result is safe to migrate, so the transactional outbox is intentionally
unused. Its infrastructure atomically stages a stable result ID and stable event ID, claims
batches with leases, requires an idempotent destination upsert keyed by event ID, confirms the
source only after the batch succeeds, retries failed/expired leases, and retains dead letters.
Cleanup can delete only delivered outbox records; pending, failed and dead results cannot be
purged. A future domain must define its Lovable destination before activating delivery.

## Cutover and rollback

Deploy the operational baseline and credentials first, then start the collector worker and switch
the application fleet to `OPERATIONAL_DB_ENABLED=true` and `BINANCE_COLLECTOR_ENABLED=true`
together. Roll the collector back by stopping the worker and switching
`BINANCE_COLLECTOR_ENABLED=false` across the whole fleet; the legacy request-driven operational
candle/checkpoint path resumes, and completed-candle TA returns to reading the exchange REST
provider rather than the operational store. Switching `OPERATIONAL_DB_ENABLED=false` as well
returns all legacy operational ownership to Lovable. Operational data is retained. Never run a
mixed fleet with different flag values, because that would create two writers for those domains,
and never read collector candles for TA while the worker is stopped. Do not enable
fallback-on-error.
