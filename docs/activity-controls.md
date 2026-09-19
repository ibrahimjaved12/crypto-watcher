# Temporary activity controls

All five switches default to enabled when unset. Set an exact `false` (case insensitive)
to pause work; remove it or set `true` to restore it. These controls reduce known
application activity. They do not measure or guarantee lower Lovable billing.

| Environment variable                   | Scope          | Effect of false                                                                                           |
| -------------------------------------- | -------------- | --------------------------------------------------------------------------------------------------------- |
| `VITE_TA_HISTORY_AUTO_REFRESH_ENABLED` | Browser build  | TA history becomes manual-only, including initial load, filters/pages, focus, reconnect and invalidation. |
| `VITE_MARKET_AUTO_REFRESH_ENABLED`     | Browser build  | Market prices become manual-only, including initial load and watchlist changes.                           |
| `SCHEDULED_MONITOR_ENABLED`            | Server runtime | Authenticated cron requests return skipped before database access; manual checks still work.              |
| `TA_GENERATION_ENABLED`                | Server runtime | No new TA calculations or snapshot upserts during manual or scheduled checks.                             |
| `TA_OUTCOME_EVALUATION_ENABLED`        | Server runtime | No pending-outcome queries or updates during those checks.                                                |

TA outcomes can run with generation paused and will still fetch candles. Disable both
TA switches to skip all TA I/O. Price monitoring, including its atomic baseline/alert
writes and run records, remains available through manual checks. Authentication,
watchlist/settings actions, initial watchlist/run reads and manual Python analysis
are not disabled. These are workload controls, not a blanket database shutdown.

## Configure

For a local diagnostic session, add these non-secret settings to `.env.local` and
restart the development server:

```dotenv
VITE_TA_HISTORY_AUTO_REFRESH_ENABLED=false
VITE_MARKET_AUTO_REFRESH_ENABLED=false
SCHEDULED_MONITOR_ENABLED=false
TA_GENERATION_ENABLED=false
TA_OUTCOME_EVALUATION_ENABLED=false
VITE_ACTIVITY_DIAGNOSTICS=true
ACTIVITY_DIAGNOSTICS=true
```

Browser variables are embedded by Vite at build time: changing them requires a
rebuild for hosting. Server variables must reach the TanStack server's runtime
environment (`process.env`); merely saving a secret to a separate Edge Function
environment is insufficient. Local configuration does not change the deployed app.
No live settings or schedules are changed by adding this code. Keep the actual
Lovable cron job disabled as well to avoid endpoint invocations.

Diagnostics are off by default. They print only a fixed operation name and decision
under `[activity-controls]`, without identities, tokens, URLs, rows or payloads.
Browser diagnostics log a paused decision on mount and actual query starts, including
manual refreshes; there is no idle logging timer. Server diagnostics log skipped
work when an authenticated caller actually invokes it. React development Strict Mode
may repeat mount logs. Disable diagnostics after testing to avoid unnecessary logs.

## Verify without live database traffic

Run `node --test tests/activity-controls.test.mjs`. Tests use a real QueryObserver
with stubbed requests to check mount, timer, focus, reconnect, invalidation, filter
changes and manual refresh. Server tests stub all exchange/database operations and
assert zero I/O when disabled. No local authentication bypass is needed.

## Optional browser verification

With an existing authenticated session, open DevTools Network (Fetch/XHR) and Console.
Load the dashboard once. With the browser flags false, expect paused log markers and
no `ta_signals` request or market snapshot call. Existing auth, watchlist and run reads
remain expected. Switch tabs, reconnect, change TA filters/pages and wait over a minute:
the two disabled queries should remain quiet. A manual Refresh performs the selected
query once (and logs its start); it is a real request and may use hosted resources.
The disabled queries also disable retries, so a failed manual refresh does not retry.
Restore flags and rebuild/restart to verify automatic fetching resumes.

Use request counts to verify behavior, not console messages alone. A 30-minute interval
still permits initial/focus/reconnect fetches and is not an isolation test. Avoid commenting
out requests: that can hide the behavior being tested or produce fake successes.
