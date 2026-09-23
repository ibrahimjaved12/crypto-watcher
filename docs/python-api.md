# Python analysis service and Lovable integration

This feature lets an authenticated dashboard user select a watched pair and run
Python analysis. It is disabled until the app's server configuration is supplied.
No Python service or hosted app was deployed during implementation.

## Request and ownership path

1. Dashboard calls the TanStack `runPythonAnalysis` server function with `{symbol}`.
2. Existing `requireSupabaseAuth` verifies the session. The existing global auth
   attacher sends that session to TanStack; existing CSRF protection still applies.
3. The function uses **that user's Supabase client and RLS**, plus explicit user ID
   filters, to SELECT the watched pair, settings and baseline. It does not use the
   admin client, seed settings/watchlists, insert logs, or call the monitor RPC.
4. TanStack calls `POST /v1/analysis` using a separate service bearer token. Only the
   selected symbol, rule settings and baseline/cooldown timestamps are sent. No user
   ID, session JWT, database URL or database credentials are forwarded to Python.
5. Python obtains public candles and returns rolling results and a baseline preview.
   TanStack validates the response schema/symbol before returning it to the browser.

The existing monitor remains the sole writer of alerts and baselines. Python calls
the existing `cumulative.observe` function but **discards its proposed state**.
Running analysis repeatedly cannot initialize or reset a baseline or save an alert.
No new database migrations, scheduler, login mechanism or Google OAuth changes
are part of this integration.

## Configuration

| Where                            | Variable                  | Value                                                                                                |
| -------------------------------- | ------------------------- | ---------------------------------------------------------------------------------------------------- |
| Python service secret            | `PYTHON_ANALYSIS_TOKEN`   | Random URL-safe token, 32–256 characters (`A-Z`, `a-z`, digits, `_`, `-`)                            |
| Lovable app server secret        | `PYTHON_ANALYSIS_TOKEN`   | The same value as the Python service                                                                 |
| Lovable app server configuration | `PYTHON_ANALYSIS_URL`     | Python's HTTPS origin, e.g. `https://analysis.example.com` (no path, credentials, query or fragment) |
| Lovable app server configuration | `PYTHON_ANALYSIS_ENABLED` | Exactly `true` to enable; unset or any other value disables requests                                 |

These must be runtime **server** environment variables, never `VITE_*` values or
browser configuration. Neither token nor service URL is returned to the browser.
Use your hosting secret manager for production; do not commit `.env` files. Token
rotation requires updating both deployments. HTTP is allowed only for localhost,
127.0.0.1 or ::1 development. Redirects are rejected so credentials are not forwarded
to another origin. A configured service URL must point at the final HTTPS origin.

## Exact local commands

Run from the repository root. Python 3.10+ is supported (tested with 3.10.12).
On Debian/Ubuntu the interpreter needs the `python3-venv` package for `venv` and pip.

```sh
cd python
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export PYTHON_ANALYSIS_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/python -m uvicorn market_analysis.api:app --host 127.0.0.1 --port 8000 --no-access-log --limit-concurrency 32
```

Stop the foreground server with **Ctrl+C**. The generated token stays in that
shell's environment; it is not printed. For a configured local TanStack server,
set its `PYTHON_ANALYSIS_TOKEN` to the same value, set `PYTHON_ANALYSIS_URL` to
`http://127.0.0.1:8000`, and enable `PYTHON_ANALYSIS_ENABLED=true`. Authenticated
browser validation is intended for Lovable's hosted app; no local login bypass exists.

In another terminal:

```sh
curl --fail http://127.0.0.1:8000/health
cd python
.venv/bin/python -m unittest discover -s tests -v
```

`requirements.txt` pins every resolved service dependency. `requirements.in` is the
direct-dependency input for intentional upgrades; ordinary installs use the lock.
Existing CLI calculations still run without service dependencies:

```sh
cd python
python3 -m market_analysis --symbol BTCUSDT --threshold 2 --window 15
```

## Docker

From the repository root, with Docker running and the token set in the shell:

```sh
docker build -t crypto-watch-analysis:local python
docker run --rm --name crypto-watch-analysis -p 127.0.0.1:8000:8000 --env PYTHON_ANALYSIS_TOKEN crypto-watch-analysis:local
```

Stop with Ctrl+C, or from a second terminal:

```sh
docker stop crypto-watch-analysis
```

The image uses Python 3.12 slim, runs as a non-root user on port 8000, includes a
`/health` healthcheck and excludes local secrets/tests/venvs from its build context.
Dependencies are pinned; the base image's minor-version tag receives updates.
For reproducible production image bytes, pin the approved base-image digest in
your release process and deploy the built image by digest. The Docker daemon was
unavailable here, so the image build/start must still be verified before deployment.

## API contract and semantics

- `GET /health`: unauthenticated, 200 with `{"status":"ok"}` when the token has a
  valid shape; 503 when unconfigured. It is process/configuration readiness, not a
  live exchange connectivity test. No credentials or environment details are exposed.
- `POST /v1/analysis`: requires `Authorization: Bearer <service token>`. Version 1
  request models reject unknown fields, unsupported symbols, invalid decimal
  prices/settings and inconsistent baseline timestamps. Credentials are compared
  using a constant-time comparison. Validation errors do not echo the request body.
- No database client, persistence, scheduler or browser CORS access is installed in
  Python. Do not expose the service token to frontend callers. `/docs` and OpenAPI
  routes are disabled in this minimal deployed service.

Example body (state is normally supplied by TanStack, not entered by the user):

```json
{
  "schema_version": 1,
  "symbol": "BTCUSDT",
  "instrument_id": "binance-usdm:BTCUSDT",
  "settings": {
    "threshold_pct": "2",
    "cooldown_minutes": 15,
    "monitoring_enabled": true
  },
  "baseline": null
}
```

When present, `baseline` contains `price`, `at_ms`, `source`, `threshold`,
`last_observed_ms`, `last_up_alert_ms`, `last_down_alert_ms`. Prices/thresholds are
decimal strings and timestamps are UTC epoch milliseconds. The instrument ID must
match the symbol.

Results have `mode: "read_only"`, the canonical instrument identity, source,
endpoint, price type, retrieval/analysis/observation times and:

- **Rolling:** existing completed, contiguous 5m/15m/1h/4h/24h calculations. Each
  window reports its own start/end close time and validity. These windows can end
  at different times. A rolling threshold match is not cumulative alert eligibility.
- **Baseline:** the existing net-movement rule and directional cooldown preview.
  `eligibility_evaluated` and nullable `alert_eligible` explicitly distinguish an
  ineligible observation from one that could not be evaluated. `cooldown_evaluated`
  is true only if the rule reached the directional cooldown check, with the relevant
  expiry when an earlier alert exists. No cooldown check is needed below threshold.

Missing baseline → `baseline_required`; changed source/threshold →
`baseline_reset_required`. Neither causes a write. The monitor must establish the
new baseline on its normal check. Already-processed candles remain ineligible, even
if the current percentage is above threshold. Paused monitoring is ineligible.
Fresh qualifying moves preserve the existing inclusive boundary and exact cooldown
expiry rules for increases and decreases.

This is advisory against the loaded state. Settings and baselines may change during
the request. An eligible
preview neither reserves nor guarantees a future alert.

Python tries public Binance USDⓈ-M, OKX USDT swap, then Kraken perpetual-futures
trade candles. Fresh valid minute data can return **partial** rolling
history while still evaluating the baseline. Missing, stale, inactive, or unsupported
data is reported without invented values.

Database reads have a 5-second abort timer. Exchange HTTP operations have 5-second
timeouts; Python imposes an 18-second total analysis deadline, and TanStack aborts
the service request after 25 seconds. Thus a full app request may take up to roughly
30 seconds including state reads. Timed-out Python work is cancelled. No application
retry loops or new scheduler are added. Provider failure returns an unavailable
result with safe reason codes; timeout returns 504, invalid request 422, bad token
401, and unexpected service failure 502. Raw upstream errors and credentials are
never forwarded or logged by the integration. Responses are not cached.

## Hosted deployment and browser verification (not performed yet)

### Immediate failure before Render receives a request

This is a TanStack Start project, not the older static Lovable stack. Lovable's
[TanStack runtime documentation](https://lovable.dev/blog/building-apps-using-tanstack-start)
describes server secrets as request-time Worker bindings. The installed Lovable
Vite configuration targets Nitro's Cloudflare module handler. The generated server
imports `node:process` and uses `nodejs_compat` with a recent compatibility date;
Cloudflare [populates process.env from bindings](https://developers.cloudflare.com/workers/runtime-apis/nodejs/process/)
in that configuration. `analyzeForUser` reads it at invocation time, not module load.
An Edge Function migration is therefore not required to access these secrets.

The reported “Could not reach the Python analysis service or read its response”
message is emitted only after config validation and user-scoped SELECTs succeed.
A misspelled/missing secret name would instead report incomplete configuration.
A correctly shaped but wrong token would ordinarily reach Render and return the
distinct service-authentication error. A valid-looking but wrong hostname can
still produce the reported network error; the live values were not inspected.

The proxy previously passed `cache: "no-store"` to fetch. Cloudflare documents
that this can throw before network I/O unless the runtime enables the relevant
[cache compatibility support](https://developers.cloudflare.com/changelog/post/2024-11-11-cache-no-store/).
The proxy now sends the standard `Cache-Control: no-store` request header instead;
the request remains an authenticated POST and Python already marks responses
`Cache-Control: no-store`. A focused test simulating that Worker rejection fails
before this change and passes after it. This identifies a compatibility defect
consistent with the symptoms, **not proof of the live exception**: hosted Worker
logs/compatibility settings and project secret values were unavailable locally.
The local production build currently generates compatibility date `2026-09-16`
with `nodejs_compat`, which supports the original cache option. If the published
runtime uses equivalent settings, this option is not the cause there; investigate
the configured hostname, DNS/TLS/egress, redirects, or unreadable upstream response.

For this repair, once the change is separately approved and published:

1. In the project/environment serving the published URL, check exact server-secret
   names: `PYTHON_ANALYSIS_ENABLED=true`, `PYTHON_ANALYSIS_URL` equal to Render's
   final HTTPS origin (no `/v1/analysis` path), and `PYTHON_ANALYSIS_TOKEN` equal to
   Render's token. Do not use `VITE_` names or paste token values into logs/reports.
2. Open the published app, sign in normally, open browser Network tools and Render
   logs, then select a watched pair and click **Run Python analysis** once. Expect
   the TanStack server-function POST in Network and `POST /v1/analysis` in Render
   logs. The browser should never call Render directly or contain the service token.
3. Confirm the server-function response contains `ok: true` and analysis with
   `mode: "read_only"` and the selected symbol. Compare its threshold and baseline
   with that user's saved settings/baseline using read-only database inspection.
4. Repeat once. Verify analysis creates no alerts/run records and changes no
   baselines; distinguish independent scheduled monitor activity by timestamps.
   Do not pause or modify the scheduler for this check.
5. If the same immediate failure persists, inspect the Worker exception and verify
   the configured hostname/DNS/egress. Do not infer a secrets-access problem from
   this generic error or copy raw headers/tokens into diagnostics. A configuration
   error points to missing/invalid bindings; a service-authentication error points
   to token mismatch; Render receiving no request still points to pre-service failure.

No deployment, live-secret changes, or hosted authenticated verification were
performed as part of this repair.

### Initial deployment checklist

1. Build and deploy the Python image separately, with HTTPS routing to container
   port 8000 and the token supplied as a secret. Use a region able to reach the
   three public exchange APIs. Configure ingress request limits/concurrency for the
   deployment and keep clocks synchronized. Disable request/header/body capture in
   external proxies/APM so they do not record tokens or private baseline data.
2. Check `/health`, reject an unauthenticated analysis request, then test an
   authenticated request using your secure HTTP tooling. Health alone does not
   prove market-data access. Do not test by calling the monitor's writing endpoint.
3. Set the three app server variables above in the Lovable environment serving the
   intended hosted app, and deploy/restart the app as required for runtime secrets.
   That app server must allow outbound HTTPS and about 30 seconds for this request.
   Platform request-duration/egress limits are not verified by local builds.
4. Ensure the existing baseline table and owner-read RLS are applied in the database
   used by that environment. No new migration is required. The repository migration
   chain replays cleanly with `npx supabase db reset --local`.
5. Sign in normally on Lovable, select a watched pair and use **Run Python analysis**.
   Check rolling versus baseline results, timestamps, loading/errors and cooldown
   labels. Repeat the request and confirm baseline/alert/run records are unchanged
   by analysis (the independently scheduled monitor may still write during testing).
6. Test another account, a pair outside its watchlist, signed-out calls, paused
   monitoring, missing baseline, mismatched service token and service downtime.
   Restore valid configuration and verify recovery. Check browser requests/bundles
   contain only TanStack calls and never the Python service token or user state payload
   destined for the service. The result itself intentionally displays the user's baseline.

Verification commands from the repo root:

```sh
npm ci --prefix tests --ignore-scripts
npm test --prefix tests
npx tsc --noEmit
npm run build
```

The aggregate Node suite currently has three unrelated diagnostics-test failures
tracked in [issue #57](https://github.com/ibrahimjaved12/crypto-watcher/issues/57).

API tests use fixed candles and mocked HTTP transport, not mock login. Bridge tests
exercise user-scoped SELECTs, rejection paths, payload minimization and response
validation. They do not replace the complete hosted session/CSRF/RLS browser test.

Local verification passed: 45 Python tests, 21 JavaScript/database/bridge tests,
TypeScript typecheck, targeted ESLint and the Bun production build. The generated
browser assets were checked for service-token/URL configuration identifiers and
contain neither. Python API tests required execution outside the sandbox because
its thread/event-loop restrictions stalled the test client. Docker image verification
is still blocked by the stopped Docker daemon. Hosted end-to-end verification remains
pending deployment and secret configuration.
