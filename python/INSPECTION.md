# Existing monitoring inspection

This report describes the original milestone at `c1bcb3c`. The subsequent
[cumulative monitor](../docs/cumulative-monitoring.md) changes the alert path,
state persistence and comparison UI. Deployment remains separate from local code.

Inspected local `c1bcb3c` before implementation. Read root `AGENTS.md`; no history
rewriting or app changes. The requested branch already existed. User confirmed
branch readiness after remote fetch was declined. This is a source inspection,
not proof of deployed behavior or a live database audit.

## Feature evidence

| Report | Actual code and limits |
| --- | --- |
| Authentication | `src/routes/auth.tsx`: email/password sign-in, signup and Google OAuth; authenticated route checks `getUser`. Provider settings, signup confirmation and redirects are deployment-dependent. |
| Account-specific persistence | Initial Supabase migration creates watchlist_items, monitor_settings, alerts, notes and monitor_runs. All enable RLS with auth.uid() ownership. `src/lib/db.ts` implements browser data access. Applied migrations and active policies were not queried. |
| Watchlists/settings/notes | Default BTC/ETH/DOGE seed, add/remove, threshold/window/cooldown/enabled controls, create/list/delete notes. Ten-pair cap is a client count check, not a DB constraint. Empty watchlists reseed on fetch. Settings constraints are mostly HTML validation, not database validation. |
| Live prices/fallback | `market/providers.server.ts` collects candles on the TanStack server. `market/quotes.server.ts` computes price/changes there. Browser dashboard requests a market snapshot every 60 seconds while open. No streaming feed or stored candle history. |
| Timeframes/chart | 5m, 15m, 1h, 4h, 24h percentage rows and Recharts chart exist. Calculations use row offsets and unfinished candles; see SPEC.md. Chart has no time XAxis, so tooltip label formatting may receive a row index rather than the candle timestamp. |
| Monitoring | `monitor/engine.server.ts` evaluates thresholds and inserts alerts. `monitor.functions.ts` authenticates a manual caller and checks only that user's watchlist. Disabling monitoring also skips manual checks. |
| Scheduling | GET/POST `/api/public/hooks/monitor-prices` authenticates a shared bearer token and checks all watchers sequentially. Latest migration only enables pg_cron and pg_net; no cron.schedule registration or target URL exists in the repository. Five-minute frequency and preview targeting are reported deployment configuration, not verified facts. |
| Alerts/history/export | Saved alert fields, test alerts, deletion, client search and CSV exist in db.ts and alerts.tsx. Search/export cover only the latest 500 fetched alerts. |
| Runs | Monitor stores success/partial/failed results and UI fetches latest 25. Skipped runs are not saved. Run insert errors are ignored; manual uncaught exceptions need not produce a log. Dashboard labels latest run as scheduled even though manual and scheduled runs have no discriminator; it does not track a distinct last successful run. |
| Notifications | No email/WhatsApp alert delivery implementation found. Auth confirmation email is separate. |

Collection and calculations run in the existing TypeScript/TanStack backend.
Alert evaluation runs in that same backend. A scheduler must live outside the
request process and call the hook; neither a UI sentence nor installed extensions
proves it is registered, enabled or succeeding with the browser closed.

Additional rule concerns: recent-alert lookup errors are ignored, cooldown is a
non-atomic read then insert, and concurrent manual/scheduled checks can duplicate
alerts. Settings query errors can silently fall back to defaults. Invalid DB
settings or nonfinite prices lack robust runtime validation. Per-run data_source
records only the first source although different symbols may use different ones.
These issues are documented, not modified by this milestone.

## Database write path and Python integration

Browser → Supabase publishable client with user session → RLS-protected own rows
for watchlist/settings/notes and alerts (including test alerts). The migration
allows users to insert/update their own non-test alerts as well; this is not a
trusted analysis-ingestion contract. monitor_runs permits authenticated reads only.

Manual authenticated server function or shared-secret cron hook →
`runMonitorForUser(supabaseAdmin, ...)` → alerts INSERT → `recordRun` →
monitor_runs INSERT. `client.server.ts` reads SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY server-side and bypasses RLS. User middleware instead
uses SUPABASE_PUBLISHABLE_KEY plus a verified bearer JWT.

There is **no supported application endpoint to ingest Python results**. The
existing authenticated cron route only executes the TypeScript monitor and ignores
analysis payloads. Calling it from Python would rerun monitoring, not integrate
Python calculations. A standard Supabase HTTP data API with publishable key + user
JWT is a potential user-scoped transport if externally reachable; code alone does
not verify Lovable Cloud permits/provisions the required external access. A
service-role key would confer broad access, not a scoped worker integration. Do
not distribute it to this analysis command.

Smallest next step: introduce a versioned, authenticated **read-only comparison**
path in staging that lets the TanStack server request Python analysis and display
results without alert writes. Preserve the existing auth/database ownership and
keep the app as the single alert writer. Agree on the SPEC.md differences before
switching that writer to consume Python outputs. A later write-capable integration
needs validation of symbols, timestamps, schema/rule version and user scope plus
atomic cooldown/idempotency enforcement; threshold_met alone is insufficient.

## Deployment prerequisites before replacing the monitor

- Verify actual deployed revision, applied RLS/migrations, scheduler registration,
  current preview URL, bearer secret, job execution history and HTTP outcomes.
- Provision a Python runtime/service in a region with reachable public exchange
  endpoints; configure dependency/runtime version, synchronized UTC clock, request
  timeouts, provider rate limits, capacity and monitoring. No Python hosting support
  in Lovable Cloud is established by this repository.
- Configure server-to-Python authenticated transport and a private service URL;
  validate schema and timeout/error behavior. Keep Supabase admin credentials solely
  in the existing trusted writer. Existing server needs SUPABASE_URL,
  SUPABASE_SERVICE_ROLE_KEY and SUPABASE_PUBLISHABLE_KEY; the cron hook accepts
  MONITOR_CRON_TOKEN or LOVABLE_CRON_SECRET (with optional previous platform secret).
- Compare both engines without writes in staging, verify per-user isolation, then
  resolve atomic deduplication and failure logging before production alert changes.
- Use exactly one active scheduled writer. At cutover pause the old job, drain any
  in-flight run, set the intended stable production URL and five-minute schedule,
  then enable the replacement with rollback and run-log verification. Do not leave
  preview and production schedules both writing to the same database.

None of these deployment changes were performed. No live database, auth flow,
monitor endpoint or scheduler was invoked by the Python milestone.
