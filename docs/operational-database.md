# Operational PostgreSQL

Lovable PostgreSQL remains the permanent application database. A second Supabase/PostgreSQL
instance owns bounded, frequently updated working state when `OPERATIONAL_DB_ENABLED=true`.
TanStack is the only privileged writer to both databases; Python remains calculation-only.

## Ownership

| State domain                                             | Authoritative owner when enabled | Writer                                             |
| -------------------------------------------------------- | -------------------------------- | -------------------------------------------------- |
| Shared Binance completed candles (1m, 15m, 1h, 4h)       | Operational DB                   | Leased TanStack WebSocket collector                |
| Collector checkpoint/freshness/health                    | Operational DB                   | Leased TanStack WebSocket collector                |
| Legacy per-user candles/checkpoints (collector disabled) | Operational DB                   | Request-driven TanStack monitor                    |
| Monitor-run diagnostics                                  | Operational DB                   | TanStack operational repository                    |
| Outbox/retry/dead-letter state                           | Operational DB                   | TanStack operational repository; currently dormant |
| Auth, users, watchlists, settings, notes                 | Lovable                          | Existing Lovable paths                             |
| Baseline, directional cooldown and alert insertion       | Lovable                          | `process_cumulative_observation` transaction       |
| TA conclusions/outcomes and other permanent user history | Lovable                          | Existing TanStack paths                            |

No domain is dual-written. With the flag enabled, checkpoint and monitor-run writes do not
touch their legacy Lovable tables. Operational-store failure is visible and never falls back to
Lovable. Completed candles have no Lovable copy.

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

Never create `VITE_*` forms. Startup validation rejects them. Apply only
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

Local development uses two isolated Supabase stacks:

```sh
npx supabase start
npx supabase start --workdir operational-db
npm run env:local                 # new .env.local
npm run env:local:operational     # existing .env.local only
npm run dev
```

`npm run dev:local` starts only the original main local stack and explicitly disables operational
ownership. `npm run dev:local:all` starts both stacks and enables the operational store.
`npm run dev:local:stop` stops both. The main API is at port 54321 and the operational API at 55321.
Local operational Supabase Auth runs only to issue its API/service-role credentials; application
users and authentication remain exclusively in the main local database or Lovable.

## Reads, retention and synchronization

The dashboard's recent-run read is an authenticated TanStack server function. It reuses the
verified Lovable user identity, adds an explicit `user_id` predicate to operational queries,
and never returns the operational service-role credential.

Shared and legacy completed candles default to seven-day retention (allowed range 1–30 days);
monitor runs default to 30 days (allowed range 1–90 days), and inactive checkpoints expire after
30 days. Writes perform database-wide bounded cleanup.
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

Deploy the operational migration and credentials first, then switch all long-lived TanStack
instances to `OPERATIONAL_DB_ENABLED=true` and `BINANCE_COLLECTOR_ENABLED=true` together. Roll the
collector back by switching `BINANCE_COLLECTOR_ENABLED=false` across the whole fleet; the legacy
request-driven operational candle/checkpoint path resumes. Switching `OPERATIONAL_DB_ENABLED=false`
as well returns all legacy operational ownership to Lovable. Operational data is retained. Never
run a mixed fleet with different flag values, because that would create two writers for those
domains. Do not enable fallback-on-error.
