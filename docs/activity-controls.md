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

## Steps to test

1. Use the repository's pinned Node 22 runtime, install dependencies if needed, and
   run the focused tests:

   ```sh
   nvm use
   npm install
   node --test tests/activity-controls.test.mjs
   ```

   These tests use a real QueryObserver with stubbed requests to check mount, timer,
   focus, reconnect, invalidation, filter changes, and manual refresh. Server tests
   stub all exchange/database operations and assert zero I/O when disabled. No local
   authentication bypass or live database is used.

2. Run the existing regression checks:

   ```sh
   npm test --prefix tests
   npx tsc --noEmit
   npm run build
   ```

3. Add the false flags and diagnostics shown in [Configure](#configure) to
   `.env.local`, then restart `npm run dev`. Sign in using an existing test account
   and open DevTools Network (Fetch/XHR) and Console.

4. Open the dashboard. Confirm that the page says automatic price refresh is paused,
   the TA panel says automatic TA refresh is paused, and the console contains fixed
   `automatic-paused` diagnostic events for `market` and `ta-history`. There should
   be no market snapshot or `ta_signals` request from those two queries on mount.
   Auth, watchlist, settings, and run-history requests are still expected.

5. Wait for more than 60 seconds, switch away and back, toggle offline/online, change
   TA timeframe/symbol/page, and add or remove a watchlist symbol if appropriate for
   the test account. Confirm neither disabled query runs automatically.

6. Press the market Refresh button and the TA Refresh button. Confirm each performs
   exactly one request and logs `request-started`. A failed manual request should not
   retry while the corresponding automatic flag is false.

7. Press **Run check now** with both TA server flags false. Confirm movement checking
   still completes, while server diagnostics show `ta-generation` and `ta-outcomes`
   as skipped. The focused automated test separately proves that this combination
   makes no TA provider or database calls.

8. If testing the scheduled endpoint in a safe environment, invoke it once with its
   normal valid authentication while `SCHEDULED_MONITOR_ENABLED=false`. Confirm the
   response has `ok: true`, `status: "skipped"`, `users: 0`, and an empty `results`
   array, with no new monitor run. An unauthenticated request must still be rejected.
   This flag does not disable the external cron itself.

9. Remove the false flags (or set them to `true`), restart/rebuild, and confirm the
   dashboard queries load automatically and resume their one-minute refresh behavior.
   Disable diagnostic flags after testing.

Use request counts to verify behavior, not console messages alone. A 30-minute interval
still permits initial/focus/reconnect fetches and is not an isolation test. Avoid commenting
out requests: that can hide the behavior being tested or produce fake successes.
