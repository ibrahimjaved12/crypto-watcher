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
`/api/public/hooks/sync-collector-subscriptions` hook and a once-per-process application startup
bootstrap keep it current, and the scheduled monitor route never reads Lovable while disabled.

While `BINANCE_COLLECTOR_ENABLED=true`, the application's completed-candle TA reads canonical
completed candles from this operational store through `readCollectorTACandles` instead of fetching
a second live exchange candle series. Missing or stale operational history fails the affected TA
frame visibly; it is never silently substituted with another calculator or live source. TanStack
still determines due work, calls the shared Python contract, validates responses, and is the sole
privileged `ta_signals` writer in Lovable.

## Configuration and migrations

Server-only variables:

```text
OPERATIONAL_DB_ENABLED=true
OPERATIONAL_SUPABASE_URL=https://<operational-project>.supabase.co
OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY=<server secret>
OPERATIONAL_CANDLE_RETENTION_DAYS=7
OPERATIONAL_MONITOR_RUN_RETENTION_DAYS=30
OPERATIONAL_OUTBOX_MAX_ATTEMPTS=10
BINANCE_COLLECTOR_ENABLED=true
```

Never create `VITE_*` forms. Startup validation rejects them. The collector worker needs only the
operational variables above plus `BINANCE_COLLECTOR_ENABLED` and `MOVEMENT_FINALIZATION_GRACE_MS`;
it never requires the main Lovable `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`, because it holds no
Lovable credentials. Only the TanStack application is configured for the main database. Apply only
`operational-db/supabase/migrations/` to the operational project; never add these files to or
apply them through the root `supabase/migrations/`, which remains the Lovable chain. The nested
`supabase/` directory is required by the CLI when `operational-db` is its workdir.

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

Shared and legacy completed candles default to seven-day retention (allowed range 1–30 days);
monitor runs default to 30 days (allowed range 1–90 days), and inactive checkpoints expire after
30 days. Writes perform database-wide bounded cleanup. Each canonical collector series always keeps
its newest 260 completed candles regardless of the day window, so the longest completed-candle TA
frame still has the minimum history and bounded catch-up it needs.
The authenticated read reports per-user storage counts; operators can call
`get_global_storage_diagnostics()` for total row counts, oldest candle time, outbox state counts
and the oldest undelivered event, and `get_collector_storage_diagnostics()` for shared candle and
health growth. Only completed candles are persisted—never developing updates or raw `aggTrade`
events. See [the collector design](./binance-futures-collector.md) for recovery, memory bounds, and
health.

No current durable result is safe to migrate, so the transactional outbox is intentionally
unused. Its infrastructure atomically stages a stable result ID and stable event ID, claims
batches with leases, requires an idempotent destination upsert keyed by event ID, confirms the
source only after the batch succeeds, retries failed/expired leases, and retains dead letters.
Cleanup can delete only delivered outbox records; pending, failed and dead results cannot be
purged. A future domain must define its Lovable destination before activating delivery.

## Cutover and rollback

Deploy the operational migration and credentials first, then start the collector worker and switch
the application fleet to `OPERATIONAL_DB_ENABLED=true` and `BINANCE_COLLECTOR_ENABLED=true`
together. Roll the collector back by stopping the worker and switching
`BINANCE_COLLECTOR_ENABLED=false` across the whole fleet; the legacy request-driven operational
candle/checkpoint path resumes, and completed-candle TA returns to reading the exchange REST
provider rather than the operational store. Switching `OPERATIONAL_DB_ENABLED=false` as well
returns all legacy operational ownership to Lovable. Operational data is retained. Never run a
mixed fleet with different flag values, because that would create two writers for those domains,
and never read collector candles for TA while the worker is stopped. Do not enable
fallback-on-error.
