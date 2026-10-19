# Worker host (#270)

`npm run worker:host` runs the forward-test workers on the owner's machine in **production mode**:
hosted main Supabase, local operational Supabase, the jobs use outbound connections
(discussion #167, hosting plan v4). It exists because `npm run dev:local:all` starts the _local_ main
stack and points the jobs at it.

The host explicitly sets `MACHINE_ONLY_ENABLED=true` for its child environment.
Cloudflare keeps this server-only capability off. The committed Worker config has
`RUNNER_GATEWAY_ENABLED=true` and `RUNNER_JOBS_ENABLED=true`. The machine must run
`npm run worker:host` with both flags set to `true` in `.env.production.local`.
For stale hosted results and restart/catch-up steps, see the [forward outage runbook](forward-outage-runbook.md).

It supervises five core children, plus the quote publisher, collector universe sync, optional
gateway and jobs runner, the movement-state publisher, alert charts and the monitor scheduler, each with a log prefix and its own restart policy:

| Child             | Command (same as the npm script / `dev-local.mjs`)                                                                 |
| ----------------- | ------------------------------------------------------------------------------------------------------------------ |
| `fastapi-analysis` | `python/.venv` `-m uvicorn market_analysis.api:app --host H --port P --no-access-log --limit-concurrency 32 --workers 1` on the `PYTHON_ANALYSIS_URL` port |
| `fastapi-forward`  | the same command on the `PYTHON_FORWARD_URL` port                                                                  |
| `collector`        | `node scripts/collector-worker.mjs --mode production` (`npm run collector:worker`)                                  |
| `forward-hourly`   | `node scripts/forward-run.mjs --mode production` (`npm run forward:run`)                                            |
| `forward-trend`    | `node scripts/forward-run.mjs --trend --mode production` (`npm run forward:trend`)                                  |
| `quote-publisher` | `node scripts/market-quote-publisher.mjs --mode production` (`npm run market-quote-publisher`; hosted credentials required; non-essential) |
| `collector-universe-sync` | `node scripts/collector-universe-sync.mjs --mode production` (`npm run collector-universe-sync`; collector/operational DB enabled and hosted/operational credentials required; non-essential) |
| `runner-gateway` | `node scripts/runner-gateway.mjs --mode production` (only when `RUNNER_GATEWAY_ENABLED=true`; non-essential) |
| `runner-jobs` | `node scripts/runner-jobs.mjs --mode production` (`npm run runner:jobs`; only when `RUNNER_JOBS_ENABLED=true`; non-essential) |
| `movement-state-publisher` | `node scripts/movement-state-publisher.mjs --mode production` (`npm run movement-state-publisher`; only when the operational DB is enabled and `MOVEMENT_STATE_HOSTED_URL` or `ALERT_CHART_HOSTED_URL` is set, and `MOVEMENT_STATE_PUBLISH_ENABLED` is not `false`; non-essential; publishes the engine's persisted state to the hosted copy through the site hook every 30 s at most, using `RUNNER_GATEWAY_SECRET`; see [market movement engine](market-movement-engine.md#published-copy-for-the-deployed-site-market_state_published)). It also computes and publishes "Longer moves" (`slow-movement-v1`) once per 15-minute boundary, 20 s after it (up to 3 attempts 30 s apart, Python `/v1/movement/slow`); `SLOW_MOVEMENT_ENABLED=false` turns that part off; see [slow movement](slow-movement.md) |
| `alert-charts` | `node scripts/alert-charts.mjs --mode production` (`npm run alert-charts`; only when `ALERT_CHART_DIR` is set and `ALERT_CHARTS_ENABLED` is not `false`; non-essential; needs `python/requirements-machine.txt`; pending jobs wait up to 7 days, unlike the 15-minute `runner_jobs` rule, because the window is stored; see [alert charts](alert-charts.md)) |
| `monitor-scheduler` | `node scripts/monitor-scheduler.mjs --mode production` (`npm run monitor-scheduler`; only when the hosted credentials are set, `OPERATIONAL_DB_ENABLED=true` and `MONITOR_SCHEDULER_ENABLED` is not `false`; non-essential; starts after both Python `/health` endpoints answer because TA calls Python; runs the scheduled monitoring pass (movement alerts and completed-candle TA for every watching user) at each interval boundary plus 8 s, default every 60 s (`MONITOR_SCHEDULER_INTERVAL_SECONDS`, 30–3600; intervals over 60 s round up to whole minutes); passes never overlap; logs only a failed pass or a pass that saved alerts or TA; finishes the current pass on SIGTERM) |
| `fx-calendar` | `node scripts/fx-calendar-fetcher.mjs --mode production` (`npm run fx:calendar`; only when `FX_CALENDAR_ENABLED=true` and `OPERATIONAL_DB_ENABLED=true`; non-essential; starts after both Python `/health` endpoints answer because it parses through `/v1/fx/calendar/parse`; keeps the forex economic calendar fresh, see [Economic calendar](#economic-calendar-fx-calendar)) |
| `fx-quote-publisher` | `node scripts/fx-quote-publisher.mjs --mode production` (`npm run fx:quote`; only when `FX_QUOTE_PUBLISHER_ENABLED=true`, `OPERATIONAL_DB_ENABLED=true` and the hosted credentials are set; non-essential; default off until the forex collector runs; every minute after a 3 s settle it publishes one batch of forex quotes from the operational bars and symbol specs to the hosted `fx_market_quotes`, see [forex](forex.md#hosted-forex-quotes)) |
| `forex-hourly` | `node scripts/forex-hourly.mjs --mode production` (`npm run forex:hourly`; only when `FX_HOURLY_ENABLED=true` (default off; keep it off until the MT5 forex collector writes bars) and `OPERATIONAL_DB_ENABLED=true`; a forex child; starts after both Python `/health` endpoints answer; once per completed hour, 3 minutes after it, evaluates one forex market stage through `/v1/fx/forward/stage` and commits it with `commit_fx_stage`; refuses the stage (`skipped_stale`, nothing committed) on a missing symbol spec, stale collector health or calendar coverage short of the boundary plus 16 hours; then the per-user pass (#446): every forex-entitled account consumes the committed stages after its cursor through `/v1/fx/forward/wallet_step` and `commit_fx_user_run`, one failing account never stops the others, and the run exits non-zero when any failed (runs only while `FX_HOURLY_ENABLED=true`, still off); see [forex](forex.md#hourly-market-stage-post-v1fxforwardstage-397)) |

The jobs are launched with `node` directly rather than through `npm`, so the supervisor owns the real
process (no wrapper to signal through). `--workers 1` is explicit because uvicorn otherwise takes its
worker count from `WEB_CONCURRENCY`, and movement sessions are process-local: the analysis service must
stay one process. The FastAPI children do not receive the Supabase or operational credentials, only
their token. The forward jobs, `runner-jobs`, `monitor-scheduler` and `fx-calendar` start only after **both** `/health` endpoints answer.

### Economic calendar (`fx-calendar`)

Default off until forex runs (`FX_CALENDAR_ENABLED=true` turns it on). It downloads the Forex Factory
weekly feeds, has the analysis service parse them strictly (`POST /v1/fx/calendar/parse`) and appends
the observations and one fetch-log row per attempt to the operational database (#401). Each cycle
fetches this week, waits 30 s, then fetches next week, and repeats every 2 h plus up to 10 min of
jitter; the first pass runs 5 s after start-up. A 404 for next week means "not published yet" (no
error, logged once). A 429 honours `Retry-After` (at most 6 h), otherwise waits 30 min doubling to 6 h;
any other failure waits 5 min doubling to 1 h and resets after a success. Error rows carry a short
fixed reason, never the URL, body or headers. All schedule values are UNVERIFIED placeholders.
The collector starts immediately so the 1m history does not gap while Python warms up. History is
never seeded by the host.

The host does not start Docker or Supabase. The local operational stack must already be running.

The hourly job keeps one atomic, gitignored sigma file per symbol in `.forward-state/` at the
repository root. Keep that directory across worker restarts. Restore order is: validate the local
file, then restore and validate that symbol's hosted checkpoint only if the file is missing/invalid,
then rebuild from local operational minutes only if hosted is also missing. Advanced local state is
saved after a successful new market-stage commit. Hosted checkpoints are written at most once per
24 hours per symbol, with failed attempts retried the next hour; checksum conflicts trigger a hosted
restore on the next run. The 120-day TA window still goes to local Python. See
[P28b/P29](forward-harness.md#p28b-persistence-and-hourly-wiring) for cache and measurement details.

### Scheduled checks (`monitor-scheduler`)

The machine owns scheduled monitoring: the Worker cron hook `/api/public/hooks/monitor-prices` stays off
(`SCHEDULED_MONITOR_ENABLED=false`, and the Worker has no operational database). Without this child, movement
alerts and TA snapshots only update when someone clicks "Check for alerts now". Both paths call the same
`runScheduledMonitorPass` (`src/lib/monitor/scheduled-pass.server.ts`), and the per-user monitor lease keeps a
pass and a manual click from double-running (the loser is `skipped`, which logs nothing). TA catch-up stays at
`TA_CATCH_UP_LIMIT` (8 candles per frame per run), so a long backlog after a restart clears within minutes.

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `MONITOR_SCHEDULER_ENABLED` | on | `false` turns the child off; any value other than `true`/`false`/empty fails preflight. |
| `MONITOR_SCHEDULER_INTERVAL_SECONDS` | `60` | Seconds between passes, whole number 30–3600; validated by preflight. |

A thrown pass error is logged (single line, at most 300 characters) and retried after 30 s.

## Setup

Variable **names** only; values live in the gitignored `.env.production.local` (mode 0600), never in the
repository and never in `VITE_*`.

With the local operational Supabase stack running, use `npm run env:production` to create
`.env.production.local` with mode 0600. The command reads only the local operational stack's status,
generates the Python token and sets the worker flags and separate Python endpoints. It refuses to
overwrite an existing file. Fill in `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` yourself: they start
as `REPLACE_` placeholders, and hosted values are never fetched. Both runner flags
are generated as `true`; also define `SUPABASE_PUBLISHABLE_KEY` and replace the
`RUNNER_GATEWAY_SECRET` placeholder before starting the host. The worker host refuses any variable
whose value still starts with `REPLACE_`, reporting only its name, except
`RUNNER_GATEWAY_SECRET` while the gateway is disabled.

Vite loads `.env`, `.env.local`, `.env.production`, then `.env.production.local` (last wins; shell
variables beat all). The committed `.env.production` already selects the production profile and the
hosted main project, but it sets `OPERATIONAL_DB_ENABLED=false` and `BINANCE_COLLECTOR_ENABLED=false`, so
the host's file must override them. Values in `.env.local` still fill any name the production file
leaves unset. That would silently mix in the local stack's keys, so the host **refuses to start unless
`.env.production.local` itself defines every name marked below** (checked in the file, not in the merged
environment; an empty value counts as defined and is then caught by the value checks).

- Main project: `APP_PROFILE=production`\*, `ALLOW_HOSTED_SUPABASE=true`\*, `SUPABASE_URL`\*,
  `SUPABASE_SERVICE_ROLE_KEY`\*
- Operational project (local stack): `OPERATIONAL_DB_ENABLED=true`\*, `OPERATIONAL_SUPABASE_URL`\*,
  `OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY`\*, `OPERATIONAL_CANDLE_RETENTION_DAYS` (default 8 is enough)
- `BINANCE_COLLECTOR_ENABLED=true`\*
- Python: `PYTHON_ANALYSIS_ENABLED=true`\*, `PYTHON_ANALYSIS_URL`\* and `PYTHON_FORWARD_URL`\* (two different
  loopback HTTP origins with explicit ports, for example ports 8000 and 8001), `PYTHON_ANALYSIS_TOKEN`\*
  (32–256 URL-safe characters)
- Optional: `FORWARD_USER_ID` (narrow the jobs to one account), `MOVEMENT_FINALIZATION_GRACE_MS`
- Universe sync: `COLLECTOR_UNIVERSE_SYNC_ENABLED=false` disables only that child;
  `COLLECTOR_UNIVERSE_SYNC_INTERVAL_MS` defaults to 60000, minimum 10000. See
  [collector response and latency](binance-futures-collector.md#machine-side-collector-universe-sync-329).
- Alert charts: `ALERT_CHART_DIR` (absolute) starts `alert-charts`; `ALERT_CHART_HOSTED_URL` (the site origin) turns on the
  hosted copy of movement-alert charts for the Alerts page (uses the existing `RUNNER_GATEWAY_SECRET`; see
  [alert charts](alert-charts.md#hosted-copy-and-the-alerts-page-298)); install matplotlib once with
  `python/.venv/bin/python -m pip install -r python/requirements-machine.txt`. See [alert charts](alert-charts.md).
- Durable jobs: `RUNNER_JOBS_ENABLED=true` enables `runner-jobs`; it uses the existing
  `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. No new secrets or publishable key are required on the
  host. `FORWARD_USER_ID` narrows the scheduled jobs only; durable jobs use the user on each queued row.

Prerequisites: Node 22.12+, `npm ci`, and the Python environment
(`python3 -m venv python/.venv && python/.venv/bin/python -m pip install -r python/requirements.txt`).

### First-time order

1. **Hosted main migrations first.** Apply the repository's `supabase/migrations` to the hosted main
   project (P29's migration must be there before the runner). Start the local operational stack and apply
   its migrations as `dev-local.mjs` does:
   `supabase start --workdir operational-db` then `supabase migration up --local --workdir operational-db`.
2. **Create the host environment:** `npm run env:production`. Replace the `SUPABASE_URL` and
   `SUPABASE_SERVICE_ROLE_KEY` placeholders in `.env.production.local` with the hosted main project's
   values, define `SUPABASE_PUBLISHABLE_KEY`, and replace the `RUNNER_GATEWAY_SECRET`
   placeholder before proceeding. Keep both runner flags set to `true`.
3. **Restart Python.** Make sure no old FastAPI is running; the host refuses to start while anything
   already answers on either Python port.
4. **Seed import** (sigma state, see [forward harness](forward-harness.md)). The loader reads only the
   process environment, so pass the files explicitly:
   `node --env-file=.env.production --env-file=.env.production.local scripts/seed-sigma-state.mjs <seed.json>`
5. **Backfill the 1m history** once, in production mode (the plain npm script would load the
   development environment): `npm run forward:backfill -- --mode production`. Re-running resumes.
6. **Start the host:** `npm run worker:host`.
7. **Check:** `npm run mvp:check -- --mode production` (its `worker host` line, below).

### Forex crash domain (#396)

`fx-calendar`, `fx-quote-publisher` and `forex-hourly` are forex children (`FOREX_CHILDREN` in
`src/lib/worker-host.ts`; `forex-collector` is reserved for #394). They never keep the host alive and
never stop it: the "every child down for 60 s" exit counts only the other children, so a dead crypto
stack still exits while forex runs, and a dead forex stack never stops crypto. The `worker host` line of
`mvp:check` ignores them too; they get their own `forex children` line, shown only when the status file
lists a forex child: FAIL on a forex crash loop or a forex child down for an hour, WARN when one is not
running or the status is stale (the worker host line already fails for that), PASS otherwise.

## One-command laptop startup (#308, #431)

After completing the host setup above, run `npm run up` (it takes no arguments). The gateway is
reached at a fixed Tailscale Funnel address, `https://<machine>.<tailnet>.ts.net`. The launcher never
installs, starts or configures Tailscale and never writes the Worker secret.

Requirements:

- Docker Engine running and accessible to your user. The launcher checks it and prints manual
  service, context and group-permission hints if needed; it never starts Docker, changes context,
  calls sudo, or launches Docker Desktop.
- `tailscale` on PATH and signed in (`tailscale up`), with MagicDNS and HTTPS certificates enabled
  in the Tailscale admin console.
- Funnel started by hand once: `tailscale funnel --bg 8787` (the gateway port only).
- `RUNNER_PUBLIC_URL=https://<machine>.<tailnet>.ts.net` in `.env.production.local` (a shell value
  overrides the file). It is required: when it is missing, preflight fails with one line. It must
  be exactly that origin (`https`, a `.ts.net` host, no port, path, query or credentials); an
  invalid value fails preflight without printing it.
- The Cloudflare secret `RUNNER_GATEWAY_URL` set once to the same value:
  `npx wrangler secret put RUNNER_GATEWAY_URL` (paste the value when asked).
- The installed npm/Python dependencies, and both `RUNNER_GATEWAY_ENABLED=true` and
  `RUNNER_JOBS_ENABLED=true` in `.env.production.local`. The gateway must use port 8787.

What the launcher does:

1. `up` reuses `npm run dev:local:all -- --operational-only`. This mode checks the operational
   stack's status and exits successfully without changing anything if it is already running.
   Otherwise it starts only `operational-db/supabase`; it does not start the main/app database,
   the dev server, or other development services. It does not apply migrations or edit
   environment files. Docker failures exit non-zero.
2. It streams `npm run worker:host` output. A host exit stops the launcher.
3. After a 5-second grace it checks `<url>/health` every 3 seconds (no redirects, 5-second
   timeout). On the first 200 it prints `Runner gateway is healthy: <url>` once. If there is no
   success within 90 seconds it logs once that the public URL is not answering (check
   `tailscale status` and `tailscale funnel status`), kills nothing and does not exit. From then on
   it re-checks every 30 seconds and logs only when the state changes.
4. Ctrl+C/SIGTERM stops the worker host and allows up to 75 seconds for its job drain before
   forcing remaining processes to stop; the operational Docker stack stays running.

Limits: Funnel is a Tailscale beta with bandwidth limits; the laptop must be awake and online for
the URL to answer; expose only the gateway port (8787), never an MT5 or any other port.

The planned fallback is a Cloudflare named tunnel on an owned domain (see the optional Cloudflare
Tunnel steps under the runner gateway below); it is not part of the launcher.

## Preflight

The host refuses to start (exit 1, one line per problem, no secret values) when:

- `.env.production.local` is missing, or does not itself define every required name (marked \* above);
- any environment value still starts with `REPLACE_` (except the disabled gateway secret);
- `APP_PROFILE` is not `production`, `SUPABASE_URL` is not a hosted HTTPS origin, or
  `ALLOW_HOSTED_SUPABASE` is not exactly `true`;
- the operational database is disabled, its URL is neither loopback nor HTTPS, or it is the same project
  as main;
- `PYTHON_FORWARD_URL` is unset or equals (or shares a port with) `PYTHON_ANALYSIS_URL`, or either is not
  a loopback HTTP origin with a port;
- `PYTHON_ANALYSIS_ENABLED` is not `true` or the Python token is missing or malformed;
- `BINANCE_COLLECTOR_ENABLED` is not `true`;
- the service-role key (or any secret) appears in a `VITE_*` variable;
- the collector's own environment contract fails, the Python environment is missing, something already
  serves either Python port, or **any process recorded in the previous status file is still alive** (see
  below).

The checks reuse `validateServerSupabase`, `validatePublicSecrets`, `operationalDbConfig`,
`pythonServiceConfig` and `validateCollectorWorkerEnvironment`; the pure part is
`src/lib/worker-host.ts`.

## Restart policy and status

Per child: exponential backoff starting at 2 s, doubling to a 5 minute cap; a run of 10 minutes or more
resets it. Any exit while the host is running counts as a crash (the children are meant to run forever).

- **Crash-loop breaker (all children except the collector):** 5 crashes within 10 minutes stop restarting
  that child.
- **The collector never enters the permanent stop.** A gap in the 1m history makes the forward job skip,
  so after the fifth crash in ten minutes it keeps retrying at the 5 minute cap (and goes back to short
  backoff after a 10 minute stable run). `mvp:check` fails once any child has been down for an hour.

**The host exits non-zero** (so systemd/pm2 restart it from a clean slate) when both forward jobs are in a
crash loop, when the collector is (a guard; its policy never gets there), or when every child has been
not running for 60 s. Other children in a crash loop (for example one FastAPI service) do not end the
host; `mvp:check` shows them as FAIL, and forward jobs that were waiting for that service stay waiting.
A FastAPI service in a crash loop therefore needs a look; the host does not exit for it alone.

`.worker-host.status.json` (gitignored, written atomically every 15 s and on every change) holds each
child's state (`waiting`, `running`, `backoff`, `crash-loop`, `stopping`, `stopped`), pid (whenever the
child process is alive), restart count, last restart time, last exit and `downSince`. On a clean stop the
file is removed; after a non-zero exit it is kept.

The shared web badge reads persisted `runner_health` records from hosted Supabase, independently
of the gateway tunnel. Apply `20261013130000_runner_health.sql` and
`20261013140000_runner_services_seen.sql` to the hosted main database before
restarting the updated host and deploying the web app. Every 15 seconds the host reports its child
states and probes both Python `/health` endpoints. The jobs runner reports successful queue claims
(at most every 15 seconds) and lease renewals; failures report degraded readiness. A heartbeat older
than 60 seconds expires. The header shows **Online** while the machine is alive, with unavailable
services named in the banner. Expired machine heartbeats with an unreachable gateway show
**Offline**. A reachable gateway without fresh worker heartbeats cannot enable worker controls.
Missing machine history shows **Offline · No heartbeat received yet**; failed health reads show
a neutral **Status unknown** badge and leave controls disabled.

Gateway reachability gates manual analysis separately. A tunnel outage leaves healthy queued forward
run controls available. Strategy Lab refreshes both published dashboards every minute even while
the runner is unavailable. `Last online` uses the persisted last machine heartbeat, including degraded
operation, displayed as ticking elapsed time with the exact local timestamp in a tooltip.
Clicking the keyboard-accessible badge lists service readiness and persisted last-working times.
`services_seen` records the database clock only for services reported as JSON `true`; false and
missing keys preserve history. Analysis and Alerts require history for both analysis and collector;
Forward runs require forward and collector. Their earlier dependency timestamp is displayed.
On-demand jobs use the jobs runner's own service timestamp. Old records without service history
remain unknown until a successful report. Machine health and service history are global for all users.
Only the service role can publish health; signed-in users can read status booleans and
timestamps, with no host names, process IDs or credentials.

`npm run mvp:check` gets one new line, `worker host`: **PASS** fresh (updated within 2 minutes) and all
children running; **WARN** file missing (host not used, e.g. the `dev:local:all` flow) or fresh but a
child is waiting, backing off or stopping; **FAIL** stale, unreadable, any child in a crash loop, or any
child down for an hour or more. Existing lines are unchanged.

## Orphans

Children run as their own process groups, so a host that dies without cleanup (SIGKILL, power loss)
leaves them running. A closed terminal sends SIGHUP, which the host handles like SIGTERM. On every start
the host reads the previous status file, whatever its age, and **refuses to start while the recorded
supervisor or any recorded child pid is still alive**, naming the pid and the command to stop it. A file
last written before the machine booted is ignored (pids are reused). If a pid now belongs to something
unrelated, delete `.worker-host.status.json`. Orphans whose status file was deleted are not found;
FastAPI ones are still caught by the port check.

## Stopping

Ctrl+C, SIGINT, SIGTERM or SIGHUP: when `runner-jobs` is alive, the host sends it SIGTERM first and gives
it up to **60 s** to finish active work, keeping Python and the collector alive during that drain.
It then sends SIGTERM to the remaining children, SIGKILL to any still alive after 10 s, and waits up to
15 s for this final phase. Total shutdown is at most **75 s** with the jobs child, or **15 s** without it.
Configure any service manager's stop timeout to allow that window.
A child that survives SIGKILL is named in the log
and its pid stays in the status file; the exit code is then 1, and the next start refuses until it is
gone. A second signal kills at once. Otherwise it exits 0 (1 if a crash-loop was seen).

- **Linux/macOS:** each child leads its own process group, and signals go to the group. This is the
  handled and intended path.
- **Windows:** Node has no POSIX signals there. `SIGINT`/`SIGBREAK`/`SIGTERM`/`SIGHUP` are handled by the
  host; the "SIGTERM" step is a forced terminate of the direct child and the 10 s step is
  `taskkill /pid N /T /F` for its tree. There is no graceful shutdown on this path. The code path exists
  but has **not been run on Windows**.

Under systemd (control-group kill) orphans do not survive a host crash.

## Durable on-demand jobs (#294)

The Strategy Lab's signal and trend buttons enqueue rows in `public.runner_jobs` in hosted main
Supabase when the Worker has `RUNNER_JOBS_ENABLED=true` and `MACHINE_ONLY_ENABLED=false`. The Worker
uses the signed-in user's own Supabase client, so RLS limits inserts and polling to their own rows.
Local app setups with `MACHINE_ONLY_ENABLED=true` retain the direct run behavior. The hourly and daily
scheduled jobs keep their existing schedules and execution path.

This queue is independent of `RUNNER_GATEWAY_ENABLED`: jobs use outbound Supabase connections and
**do not use the gateway or tunnel**. Enable `RUNNER_JOBS_ENABLED=true` separately in the host's
gitignored `.env.production.local`. The jobs child is non-essential and follows the usual crash-loop
policy; its failure leaves other children running.

The host claims the oldest queued job every 2 seconds with its existing hosted service-role client,
runs one job at a time for the row's user, and writes the existing forward summary back to that row.
Each claim has a 120-second lease, renewed every 30 seconds while work runs. A claim error backs off
for 10 seconds. A running job whose lease has elapsed becomes `failed` with
`Runner restarted before finishing` on the next claim sweep; it is not requeued. An unfinished job
after the shutdown drain is left for this lease-expiry handling.

Queued jobs older than **15 minutes** become `expired` on the next claim sweep and are never replayed.
If the host is offline, a stale row can still display as queued until it reconnects and sweeps it.
The browser polls every 2 seconds, shows a waiting message after 30 seconds queued, and stops polling
on success, failure, expiry or page unmount. A page reload does not cancel durable work; clicking again
joins the active job of that kind for that user. Each click supplies a UUID retry key, and a duplicate
key returns the original row even if it has already completed.

Current rollout state: both #294 migrations are applied to **hosted main**, and
both runner flags are `true` in committed `wrangler.jsonc`. Run `npm run worker:host`
with `RUNNER_GATEWAY_ENABLED=true` and `RUNNER_JOBS_ENABLED=true` in the host's
`.env.production.local`, and confirm both `runner-gateway` and `runner-jobs` are running.
The runner jobs migration creates the queue, user read/insert policies and three service-role-only RPCs; no
operational database migration, new credential, gateway configuration or tunnel change is needed.

## Keep it running

Start on boot (examples, adjust paths; none were run):

- **systemd user unit** (`~/.config/systemd/user/worker-host.service`): `WorkingDirectory=` the
  repository, `ExecStart=` the absolute `node` path with `scripts/worker-host.mjs`, `Restart=on-failure`,
  `RestartSec=30`, `TimeoutStopSec=90`, `KillMode=control-group`; then
  `loginctl enable-linger $USER` and `systemctl --user enable --now worker-host`. The host's own
  `Restart` handling is per child; systemd only covers the supervisor.
- **pm2:** `pm2 start scripts/worker-host.mjs --name worker-host --kill-timeout 90000`, then
  `pm2 save` and `pm2 startup`. Install `pm2-logrotate`.
- **Windows Task Scheduler:** a task at logon/startup running `node scripts\worker-host.mjs` in the
  repository directory, "run whether user is logged on or not", restart on failure.

**Keep the machine awake.** Sleep, hibernation or lid-close stops the collector and leaves a gap in the
1m history; any gap makes the forward job record `skipped_stale`. Disable sleep on AC power.
**Keep the clock right.** Enable NTP (`timedatectl set-ntp true`, or Windows time sync); the forward job,
the 00:05 UTC trend run and the status freshness check all depend on the system clock.

**Logs** go to stdout/stderr with an ISO timestamp and `[child]` prefix. Under systemd they are in the
journal (`journalctl --user -u worker-host`; cap it with `SystemMaxUse=` / `MaxRetentionSec=`). With pm2
use `pm2-logrotate`. If you redirect to a file, rotate it with `logrotate` (`copytruncate`) or a
Scheduled-Task-side size check; the host does not rotate logs itself.

## Not covered

- Backups of the hosted main app database (the `forward_*` and `paper_*` tables are permanent records)
  and a restore test.
- Alerting: nothing notifies anyone when `worker host` goes FAIL or `paper_runs` rows stay
  `skipped_stale`; `mvp:check` has to be run.
- Funding events, mark prices, maintenance-margin tiers and Python host sizing (see
  [production-todo](production-todo.md)).


## Reaching the gateway

The Worker gateway is enabled for Analyse and Check. Set these server-only
variables in `.env.production.local` and run `npm run worker:host`:

- `RUNNER_GATEWAY_ENABLED=true`.
- `RUNNER_JOBS_ENABLED=true` for durable Run forward and Run trend jobs.
- `RUNNER_GATEWAY_PORT=8787` (integer 1024–65535).
- `RUNNER_GATEWAY_SECRET`: a random secret of 32–256 URL-safe characters (`A-Z`, `a-z`, `0-9`, `_`, `-`).

When the gateway is enabled, `RUNNER_GATEWAY_ENABLED`, `RUNNER_GATEWAY_PORT`,
`RUNNER_GATEWAY_SECRET` and `SUPABASE_PUBLISHABLE_KEY` must be defined in that file itself.
The publishable key must belong to the hosted main project. The gateway also requires the existing
`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Replace the generated secret placeholder before enabling.
Never commit credentials or expose them via `VITE_*` variables.

The listener binds only to `127.0.0.1`. `GET /health` is public; `POST /v1/analysis` with JSON
`{"symbol":"BTCUSDT"}` and `POST /v1/monitor/check` require both `x-runner-secret` and
`Authorization: Bearer <user JWT>`. Send `Content-Type: application/json` for bodies; check accepts
an empty request. Bodies are capped at 4 KB. Analysis allows 12 requests/minute per user and checks
allow 6, with one concurrent check per user. This is a server-to-server interface.

To reach it through an optional Cloudflare Tunnel:

1. Run `cloudflared tunnel login`.
2. Run `cloudflared tunnel create crypto-watcher-runner`.
3. Add a DNS route with `cloudflared tunnel route dns crypto-watcher-runner <YOUR_HOSTNAME>`.
4. Create a cloudflared configuration using your tunnel ID, credentials path and hostname:

   ```yaml
   tunnel: <TUNNEL_ID>
   credentials-file: <PATH_TO_TUNNEL_CREDENTIALS_JSON>
   ingress:
     - hostname: <YOUR_HOSTNAME>
       service: http://127.0.0.1:8787
     - service: http_status:404
   ```

5. Run `cloudflared tunnel run crypto-watcher-runner` with that configuration.

A VM with nginx can later replace the tunnel with the same URL contract.

Keep the host running: with the flags on and the host off, buttons show runner offline.
The Cloudflare `RUNNER_GATEWAY_URL` runtime secret holds the fixed public gateway URL, set once
(see [One-command laptop startup](#one-command-laptop-startup-308-431)). Never commit the URL or secret.
