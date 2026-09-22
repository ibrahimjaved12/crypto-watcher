# Temporary activity controls

The three automatic entry points default to disabled. Dashboard market prices and
TA history are manual-only, and authenticated scheduled-monitor requests return a
skipped result. Set their flags to an exact `true` (case insensitive) to enable them.

TA generation and outcome evaluation default to enabled so **Run check now** retains
its existing TA behavior. Set either of those flags to `false` to pause that work.
Missing, empty, or invalid values use the documented default.

| Environment variable                   | Scope          | Default  | Behavior                                                                                           |
| -------------------------------------- | -------------- | -------- | -------------------------------------------------------------------------------------------------- |
| `VITE_TA_HISTORY_AUTO_REFRESH_ENABLED` | Browser build  | Disabled | `true` enables initial loading and 60-second TA-history refresh; otherwise history is manual-only. |
| `VITE_MARKET_AUTO_REFRESH_ENABLED`     | Browser build  | Disabled | `true` enables initial loading and 60-second market refresh; otherwise prices are manual-only.     |
| `SCHEDULED_MONITOR_ENABLED`            | Server runtime | Disabled | `true` lets authenticated cron requests run; otherwise they return skipped before database access. |
| `TA_GENERATION_ENABLED`                | Server runtime | Enabled  | `false` skips new TA calculations and snapshot upserts during manual or scheduled checks.          |
| `TA_OUTCOME_EVALUATION_ENABLED`        | Server runtime | Enabled  | `false` skips pending-outcome queries and updates during manual or scheduled checks.               |

TA outcomes can run with generation paused and will still fetch candles. Disable both
TA switches to skip all TA I/O. Price monitoring, including its atomic baseline/alert
writes and run records, remains available through manual checks. Authentication,
watchlist/settings actions, initial watchlist/run reads and manual Python analysis
are not disabled. These are workload controls, not a blanket database shutdown.

They are deployment-level overrides. The separate user-facing
[independent activity controls](activity-domains.md) gate market collection,
movement alerts, and completed-candle TA for scheduled and manual monitoring runs.
An operation runs only when both its applicable deployment control and user control
permit it; neither layer silently changes the other layer's saved value.

## Configure

There are two environment boundaries because the dashboard code runs in the browser
and monitoring/TA processing runs on the server:

| Environment                       | Configuration location               | Applies to                                                          |
| --------------------------------- | ------------------------------------ | ------------------------------------------------------------------- |
| Local development                 | Ignored `.env.local`                 | All five controls; restart `npm run dev` after changes.             |
| Lovable preview/published browser | Committed `.env.production`                     | The two public `VITE_*` controls; publish/rebuild after changes.    |
| Lovable TanStack server           | Project server configuration/secrets | The three non-`VITE_*` runtime controls read through `process.env`. |
| Lovable scheduler                 | More → Cloud → Jobs                  | Whether the five-minute job invokes the endpoint at all.            |

The repository's committed `.env` explicitly sets both browser controls to `false`,
so local, preview, and published builds start in manual-only mode. `.env.local` is
gitignored and overrides `.env` locally. One local file can therefore control all
five values.

For a local diagnostic session with every listed workload paused, add these settings
to `.env.local` and restart the development server:

```dotenv
VITE_TA_HISTORY_AUTO_REFRESH_ENABLED=false
VITE_MARKET_AUTO_REFRESH_ENABLED=false
SCHEDULED_MONITOR_ENABLED=false
TA_GENERATION_ENABLED=false
TA_OUTCOME_EVALUATION_ENABLED=false
VITE_ACTIVITY_DIAGNOSTICS=true
ACTIVITY_DIAGNOSTICS=true
```

Browser variables are embedded by Vite at build time and are visible in the client
bundle, so they are configuration rather than secrets. Lovable requires `VITE_*`
values in the committed `.env.production`, not its Secrets manager. Server variables must reach
the TanStack server's request-time environment (`process.env`); configure them in the
Lovable environment serving the app. Local `.env.local` changes do not change the
deployed app. Keep the actual Lovable job disabled as well when the goal is to avoid
endpoint invocations, because `SCHEDULED_MONITOR_ENABLED=false` only makes each
authenticated invocation exit early.

References: [Lovable Secrets and `VITE_*` variables](https://docs.lovable.dev/features/secrets),
[Lovable Jobs](https://docs.lovable.dev/features/jobs), and the project's
[TanStack server runtime notes](python-api.md#immediate-failure-before-render-receives-a-request).

To enable the three automatic entry points, set:

```dotenv
VITE_TA_HISTORY_AUTO_REFRESH_ENABLED=true
VITE_MARKET_AUTO_REFRESH_ENABLED=true
SCHEDULED_MONITOR_ENABLED=true
```

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
   npm ci
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

3. Leave the three automatic flags unset (or set them explicitly to `false`), add
   the diagnostic flags shown in [Configure](#configure), and restart `npm run dev`.
   Sign in using an existing test account and open DevTools Network (Fetch/XHR) and
   Console.

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
   normal valid authentication while `SCHEDULED_MONITOR_ENABLED` is unset or false.
   Confirm the response has `ok: true`, `status: "skipped"`, `users: 0`, and an empty
   `results` array, with no new monitor run. An unauthenticated request must still be
   rejected. This flag does not disable the external cron itself.

9. Set the three automatic flags to `true`, restart/rebuild, and confirm the dashboard
   queries load automatically and resume their one-minute refresh behavior. If a safe
   scheduled environment is available, confirm one authenticated invocation now runs.
   Disable diagnostic flags after testing.

Use request counts to verify behavior, not console messages alone. A 30-minute interval
still permits initial/focus/reconnect fetches and is not an isolation test. Avoid commenting
out requests: that can hide the behavior being tested or produce fake successes.
