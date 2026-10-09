# Production to-do (forward harness MVP)

> **Status: not done, local MVP only.** Everything below is still needed before the forward test
> (#239), the collector and the dashboard run anywhere but a developer machine.

| Item | Status | Notes |
| --- | --- | --- |
| External operational database account and credentials | not done, local MVP only | `OPERATIONAL_SUPABASE_URL` / `OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY` point at a local Supabase today. The hosted project must be separate from the main DB and use HTTPS (see `docs/operational-database.md`). |
| Operational schema on the hosted DB | not done, local MVP only | Recreate it from the single baseline `operational-db/supabase/migrations/20261004000000_operational_schema.sql`, which now has the 62-day 1m retention, `get_collector_forward_minutes` and the append-only `forward_daily_bars` feed. |
| Collector worker host and supervision (#90) | not done, local MVP only | A long-running host for `collector:worker:start` with restart-on-failure, logs and health alerting. The collector must run continuously so the 1m history has no gaps; any gap makes the forward job skip as stale. |
| 1m history backfill | not done, local MVP only | The collector's REST bootstrap fetches 300 klines per frame, so about 60 days of 1m history only builds up over time. Until then strategies report `insufficient_history` and setups are V. A bounded REST backfill job is needed for a faster start. |
| Schedule hook for the forward job | not done, local MVP only | Locally, `npm run forward:run` loops every completed hour (+2 min). Production needs a scheduled, authenticated trigger (like the `/api/public/hooks/*` routes) or a supervised worker, plus overlap protection (the run key `hour:<ms>` already makes repeats no-ops). |
| Schedule hook for the daily trend track job (#239 P14) | not done, local MVP only | Locally, `npm run forward:trend` runs at 00:05 UTC every day (`--once` for one run). Production needs a scheduled, authenticated daily trigger shortly after 00:05 UTC (same options as the hourly hook). Repeats are no-ops: run key `day:<last completed day>` and ledger/weight rows unique per (track, day); `partial` runs (funding or a symbol's klines unavailable) are retried by the next trigger. |
| Daily kline feed (`forward_daily_bars`) | not done, local MVP only | Operational table and RPCs are in the baseline schema. The first run backfills from 2025-08-07 (420 days before the 2026-10-01 track start) with one public `/fapi/v1/klines?interval=1d` request per symbol; the hosted operational DB needs the table before the first run. |
| Account selection for the forward job | not done, local MVP only | The job runs for one account (`FORWARD_USER_ID`). A multi-user product needs per-account enablement (for example the existing `paper_trading_enabled` setting) and fair scheduling. |
| Funding events and mark prices | not done, local MVP only | The forward request accepts funding events, but the job does not fetch them yet, so funding is 0. Mark prices are a proxy for trade prices (`mark-proxy`). |
| Maintenance-margin tier tables | not done, local MVP only | The wallet uses a flat tier (`flat-tier` on every ledger line). |
| Secrets | not done, local MVP only | `PYTHON_ANALYSIS_TOKEN`, the service-role keys and `FORWARD_USER_ID` belong in the host's secret store, never in the repo or in `VITE_*` variables. |
| Python service hosting | not done, local MVP only | `/v1/forward/evaluate` receives about 90,000 rows per symbol per hour. It needs a host with enough memory and a request size limit above about 60 MB, or the job must send compact or incremental history. |
| Backups | not done, local MVP only | The application DB (`forward_*`, `paper_*` are permanent, append-only records) needs scheduled backups and a restore test. The operational DB is disposable working state. |
| Monitoring of skipped runs | not done, local MVP only | `paper_runs.status = skipped_stale` rows should alert when they persist. |
