# Operational PostgreSQL

The owner's hosted main Supabase remains the permanent application database for accounts and
published results. Under architecture v5 (#273), the operational database is the local Supabase
stack on the owner's machine, alongside the collector, FastAPI and forward jobs. It owns bounded,
frequently updated working state when `OPERATIONAL_DB_ENABLED=true`.
TanStack remains the only privileged writer to the main database. The collector worker is a headless
ingestion process that is privileged only on the operational database and holds no main-database
credentials; Python remains calculation-only.

## Ownership

| State domain                                             | Authoritative owner when enabled | Writer                                                                       |
| -------------------------------------------------------- | -------------------------------- | ---------------------------------------------------------------------------- |
| Shared Binance completed candles (1m, 15m, 1h, 4h)       | Operational DB                   | Leased collector worker                                                      |
| Completed-candle TA input (collector mode)               | Operational DB                   | Collector worker writes; TanStack reads, then writes `ta_signals` to the main database |
| Collector checkpoint/freshness/health                    | Operational DB                   | Leased collector worker                                                      |
| Collector subscription universe (derived input)          | Operational DB                   | TanStack operational repository assigns; collector worker reads              |
| Legacy per-user candles/checkpoints (collector disabled) | Operational DB                   | Request-driven TanStack monitor                                              |
| Monitor-run diagnostics                                  | Operational DB                   | TanStack operational repository                                              |
| Outbox/retry/dead-letter state                           | Operational DB                   | TanStack operational repository; currently dormant                           |
| Auth, users, watchlists, settings, notes                 | Main database                              | Existing main-database paths (TanStack only)                                       |
| Baseline, directional cooldown and alert insertion       | Main database                              | `process_cumulative_observation` transaction                                 |
| TA conclusions/outcomes and other permanent user history | Main database                              | Existing TanStack paths                                                      |

No domain is dual-written. With the flag enabled, checkpoint and monitor-run writes do not
touch their legacy main-database tables. Operational-store failure is visible and never falls back to
the main database. Completed candles have no main-database copy. The application derives the collector's shared
subscription universe from main-database watchlists and assigns it as operational input; that set is
derived collector state, never a second watchlist authority. Reconciliation of that set is
application-owned and independent of `SCHEDULED_MONITOR_ENABLED`: the dedicated
`/api/public/hooks/sync-collector-subscriptions` hook is the initial and ongoing mechanism the
deployment schedules, and the server's best-effort first-request pass is only a safety net for a
missed run. The scheduled monitor route never reads the main database while disabled.

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
privileged `ta_signals` writer in the main database.

## Configuration and baseline schema

Server-only variables:

```text
OPERATIONAL_DB_ENABLED=true
OPERATIONAL_SUPABASE_URL=http://127.0.0.1:55321
OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY=<server secret>
OPERATIONAL_CANDLE_RETENTION_DAYS=8
OPERATIONAL_MONITOR_RUN_RETENTION_DAYS=30
OPERATIONAL_OUTBOX_MAX_ATTEMPTS=10
BINANCE_COLLECTOR_ENABLED=true
```

Never create `VITE_*` forms. Startup validation rejects them. The collector worker needs only the
operational variables above plus `BINANCE_COLLECTOR_ENABLED` and `MOVEMENT_FINALIZATION_GRACE_MS`;
it never requires the main `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`, because it holds no
main-database credential. Only the TanStack application is configured for the main database. Apply only
`operational-db/supabase/migrations/20261004000000_operational_schema.sql` to the operational
project; never add this file to or apply it through the root `supabase/migrations/`, which
remains the main-database chain. The nested
`supabase/` directory is required by the CLI when `operational-db` is its workdir.

The operational database is disposable pre-release working state. Recreate/reset it from
this single current baseline; historical operational migration evolution is intentionally
not preserved yet, and no production-data migration path is required. Once production data
must survive upgrades, freeze the baseline and use forward migrations for future changes.

With the machine's local operational stack running, apply its migrations:

```sh
npx supabase migration up --local --workdir operational-db
```

The operational stack is local in production as well as development. This command does not
require or authorize a hosted main-database login or schema change. See [worker host](worker-host.md)
for the production setup.

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
users and authentication remain exclusively in the main local database or the hosted project. The collector
worker process inherits `.env.local` but reads only the operational and collector variables; the
main-database values in that file are for the application.

## Reads, retention and synchronization

The dashboard's recent-run read is an authenticated TanStack server function. It reuses the
verified main-database user identity, adds an explicit `user_id` predicate to operational queries,
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

## REST candle settling (#68)

`COLLECTOR_CANDLE_SETTLE_SECONDS` is a server-only integer setting: default **15 seconds**,
allowed **5–60**, validated at collector startup with the operational environment values.
Binance can still add trades to a just-closed REST minute. Ten confirmed provisional candles
were retrieved 0.7–4.7 seconds after close, with 0.5–5% volume differences and tiny price differences.

The shared collector REST normalizer admits a 1m candle only when its retrieval timestamp is
at least `close_time + settle seconds`. This covers bootstrap, reconnect/gap recovery, and
movement-history backfill. Early responses are discarded, never held and written later;
recovery/backfill retries fetch fresh values. Other intervals retain their closed-candle rule.
WebSocket ingestion is unchanged: it requires Binance's explicit `k.x = true` final event,
and the confirmed problem concerns REST's clock-based completion check.

Deploy the new operational migration `20261012110000_collector_rest_settle_revisions.sql`
without resetting the database, then restart the collector with the setting (or its default).
The migration only replaces the reconciliation function; it does not rewrite historical rows.
The ten existing conflicts remain known provisional candles. Stored candles and conflict rows
stay unchanged, and forward signals are not recomputed. On the existing hourly reconciliation
schedule, conflicts older than 24 hours with a later 1m REST offer append that copy to
`collector_candle_revisions`, including REST-to-REST differences and differences beyond the
old 2% volume tolerance. These revisions carry `tolerance.policy = later-rest-evidence`:
they preserve evidence, not approval to replace the original. Revision insertion is idempotent.

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
- **Research parity for other intervals.** REST is what the Binance archive matches. `reconcile_conflicts(older_than_hours
  DEFAULT 24, p_price_tolerance DEFAULT 0.002, p_volume_tolerance DEFAULT 0.02)` appends, for every
  conflict older than the window whose offered side is REST (stored side is the stream) and whose
  only differing fields are high/low/close/volume/quote_volume within the stated tolerance (prices:
  relative difference at most 0.2 %; volumes: absolute difference at most 2 % of the larger value),
  one row to the append-only `collector_candle_revisions` (`revision_id`, `conflict_id`, identity,
  `fields` = the REST values and their hash, `source = 'rest'`, `supersedes` = the stored candle's
  hash, `tolerance`). It never modifies `collector_recent_candles`; readers keep the original candle.
  Outside the 1m REST evidence policy above, conflicts outside the tolerance or without a REST
  offer stay for manual review.
- **Candle versions.** `collector_candle_hash(...)` ("candle-v1": md5 over identity, open time and
  OHLCV text) identifies a candle's values. `get_collector_forward_minutes` returns it with every
  1m row; the forward job stores, with every `forward_signals` / `forward_setups` row (application
  database), `candle_version` = SHA-256 over the hashes of the 1m candles forming the decision
  candle, so any decision can say which version it used and a later revision can be traced to it.
  Hashes are never sent to Python and the forward engine's input is unchanged.

`get_collector_candle_conflicts(p_hours, p_limit)` lists recent conflicts with their revision id.
Apply pending operational migrations to an existing database; do not reset it to deploy the
settle fix, because the existing conflict evidence must be preserved.

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
purged. A future domain must define its main-database destination before activating delivery.

## Alert setup charts (#299)

`20261016090000_alert_charts.sql` adds `alert_charts`: one row per alert or pattern event with the
point-in-time candle window copied at event time (the candle tables keep only ~8 days), and the
render job state for the machine-side child `alert-charts`. Columns: event kind and id, owner (movement
alerts only), symbol, event timeframe and candle, rule version, the compact window (`candle_window`
JSONB and its byte size), `status` (`pending`, `rendering`, `rendered`, `failed`, `expired`),
attempts, error, lease, the PNG path, sha256 and size, and created/queued/started/rendered/finished
times. The PNG itself is on the machine's disk under `ALERT_CHART_DIR`, never in the database.

Writes go only through service-role functions: `enqueue_alert_chart` (idempotent on kind, symbol,
timeframe, event candle, rule version and scope; refuses a window with a candle after the event
candle; refuses a new row at the row/byte cap, never deleting), `claim_alert_chart` (runner-job rules:
stale pending rows expire, elapsed leases fail), `finish_alert_chart`, `requeue_alert_chart` (command
line re-run by event id) and `get_alert_chart_diagnostics` (the `alert charts` line of `mvp:check`).
Users and anon have no access. Apply it with `npx supabase migration up --local --workdir
operational-db`; it is additive and needs no reset. See [alert charts](alert-charts.md).

`20261016150000_alert_chart_upload.sql` adds the machine-side state of the hosted copy of movement-alert
charts (#298): `hosted_status` (`not_applicable`, `pending`, `uploaded`, `refused`), `upload_attempts`,
`upload_next_at`, `upload_error`, `uploaded_at`, and the functions `queue_alert_chart_upload`,
`claim_alert_chart_upload` and `finish_alert_chart_upload` (transient failure: 1 minute doubling to 1 hour,
retried indefinitely; the local PNG is never touched). See [alert charts](alert-charts.md#hosted-copy-and-the-alerts-page-298).

`20261016120000_alert_chart_followups.sql` adds `alert_chart_capture_failures` (a failed or abandoned
capture, idempotent on kind, symbol, timeframe and event candle; reported by `mvp:check`), the side
table `alert_chart_event_ids` (every signal/alert id that produced or repeated a chart, so the
market-wide pattern chart is found by any account's id), `record_alert_chart_capture_failure`, a
`requeue_alert_chart` that sweeps expired leases first and reports rows still rendering, and capture
failure counts in `get_alert_chart_diagnostics`. The pending expiry is an application setting passed to
`claim_alert_chart` (default 7 days).

## Forex bars (#395, part of #374)

`20261019120000_fx_bars.sql` adds the forex collector's tables next to the crypto collector ones
(no crypto object is changed). The forex collector (#394) polls the MT5 gateway and writes
**completed** 1-minute BID bars only (never the forming minute); the forex runner (#397) reads them.

- `fx_recent_bars`: one row per `(venue, instrument_id, open_time)`, `instrument_id` like
  `fx:EURUSD` or `fut:...`, UTC minute open time (the close is always `open_time + 1 minute`), BID
  OHLC, `spread_points` (ask - bid in MT5 points, `SYMBOL_POINT = 10**-digits`), `tick_volume`,
  `transport` (`poll`, `history`, `stream`) and `received_at`. Forex has no real volume, no quote
  volume and no source event time. Stored bars are immutable.
- `fx_bar_conflicts`: append-only evidence (UPDATE/DELETE fail with SQLSTATE `55000`). An offer for
  a stored minute with other values is never written to `fx_recent_bars`, never fails its batch and
  is recorded once per distinct offer (`fxbar-v1` hash, `fx_bar_hash`). Nothing reconciles forex
  bars yet.
- `fx_collector_health`: one row per `(venue, instrument_id)`. Status is `LIVE`, `RECOVERING`,
  `STALE`, `UNAVAILABLE`, `MARKET_CLOSED` or `LOGIN_FAILED`. A closed market is `MARKET_CLOSED`,
  never `STALE`; `LOGIN_FAILED` is how an expired demo account or a wrong password shows. Conflicts
  only feed `conflict_count_24h`/`last_conflict_at`.
- `fx_subscriptions` (`fx-shared`, an array of `{venue, instrument_id, native_symbol}`; instruments
  no longer assigned get `UNAVAILABLE` health) and `fx_collector_leases` (`fx-mt5-gateway`, same
  lease semantics as the crypto collector).

The row contract is `python/market_analysis/fx/bars.py` (`fx_bars_from_rows`, `_check_row`).
`get_fx_bars(venue, instrument_id, from_ms, limit, to_ms)` returns the newest `limit` bars (1-20000,
default 1000) from `from_ms` (inclusive) up to `to_ms` (EXCLUSIVE, optional; `20261020100000_fx_bars_paging.sql`,
#440), oldest first, so a reader pages backwards by passing the oldest `open_time_ms` of the previous page as
`to_ms`; each bar is exactly in the gateway row shape that
`fx_bars_from_rows` consumes (`time_basis: "utc"`, `open_time_ms`, prices as JSON numbers,
`spread_points`, `tick_volume`, `transport`, `received_at_ms`). `get_fx_bar_coverage()` reports the
count and the oldest/newest open time per series; it does not decide what is missing, which needs
the trading calendar (a missing minute while the market is closed is expected).

Retention is `OPERATIONAL_FX_BAR_RETENTION_DAYS`, **140 days** by default (allowed 31-200, enforced
by `record_fx_bars` and `operationalDbConfig`): a bar whose minute ended before the cutoff is deleted
unless it is among the newest 2000 bars of its series, so a long closure or outage never empties
the runner's warm-up. All tables and functions are service-role only. Apply it with
`npx supabase migration up --local --workdir operational-db`; it is additive and needs no reset.

## Forex economic calendar (#401)

`20261019130000_fx_calendar.sql` adds `fx_calendar_observations` (append-only, kept forever, point
in time by `first_seen_at`; a reschedule or re-rating is a new row) and the fetch log
`fx_calendar_fetches` (90 days), with `record_fx_calendar`, `record_fx_calendar_fetch`,
`get_fx_calendar_events` and `get_fx_calendar_status`. Service-role only. The rules and the
fail-closed coverage check are in [forex](forex.md#news-blackout).

## Forex symbol specs (#432)

`20261019140000_fx_symbol_specs.sql` adds `fx_symbol_spec_observations`: the broker's contract
details per `(venue, instrument_id)` (digits, point, tick size and value, contract size, lot min/max/
step, base/profit/margin currency, account currency, trade mode, swap mode, long/short swaps and the
triple-swap day). It is append-only (UPDATE/DELETE fail with `55000`) and versioned: the database
computes `spec_hash` (sha256 of every economic field, not `native_symbol`) and stores a spec only
when that hash is new for the instrument. `point` must equal `10^-digits`. **`tick_value` is in the
account currency** (MT5 reports it that way), so `account_currency` is stored with it.
`record_fx_symbol_specs(p_rows)` (1 to 500 items, every column, JSON numbers, no other key, one
generic error) returns the number of new observations; `get_fx_symbol_specs(p_as_of_ms)` returns
the latest observation per instrument at or before that time (null = now). Service-role only.
Limit: a spec that changes back to an earlier value (A, B, A) is not stored again, so the latest
read stays at B.

## Cutover and rollback

Deploy the operational baseline and credentials first, then start the collector worker and switch
the application fleet to `OPERATIONAL_DB_ENABLED=true` and `BINANCE_COLLECTOR_ENABLED=true`
together. Roll the collector back by stopping the worker and switching
`BINANCE_COLLECTOR_ENABLED=false` across the whole fleet; the legacy request-driven operational
candle/checkpoint path resumes, and completed-candle TA returns to reading the exchange REST
provider rather than the operational store. Switching `OPERATIONAL_DB_ENABLED=false` as well
returns all legacy operational ownership to the main database. Operational data is retained. Never run a
mixed fleet with different flag values, because that would create two writers for those domains,
and never read collector candles for TA while the worker is stopped. Do not enable
fallback-on-error.
