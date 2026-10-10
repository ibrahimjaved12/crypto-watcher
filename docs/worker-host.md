# Worker host (#270)

`npm run worker:host` runs the forward-test workers on the owner's machine in **production mode**:
hosted main Supabase, local operational Supabase, everything the machine does is outbound
(discussion #167, hosting plan v4). It exists because `npm run dev:local:all` starts the _local_ main
stack and points the jobs at it.

It supervises five children, each with a log prefix and its own restart policy:

| Child             | Command (same as the npm script / `dev-local.mjs`)                                                                 |
| ----------------- | ------------------------------------------------------------------------------------------------------------------ |
| `fastapi-analysis` | `python/.venv` `-m uvicorn market_analysis.api:app --host H --port P --no-access-log --limit-concurrency 32 --workers 1` on the `PYTHON_ANALYSIS_URL` port |
| `fastapi-forward`  | the same command on the `PYTHON_FORWARD_URL` port                                                                  |
| `collector`        | `node scripts/collector-worker.mjs --mode production` (`npm run collector:worker`)                                  |
| `forward-hourly`   | `node scripts/forward-run.mjs --mode production` (`npm run forward:run`)                                            |
| `forward-trend`    | `node scripts/forward-run.mjs --trend --mode production` (`npm run forward:trend`)                                  |

The jobs are launched with `node` directly rather than through `npm`, so the supervisor owns the real
process (no wrapper to signal through). `--workers 1` is explicit because uvicorn otherwise takes its
worker count from `WEB_CONCURRENCY`, and movement sessions are process-local: the analysis service must
stay one process. The FastAPI children do not receive the Supabase or operational credentials, only
their token. The forward jobs start only after **both** `/health` endpoints answer. The collector starts
immediately so the 1m history does not gap while Python warms up. History is never seeded by the host.

The host does not start Docker or Supabase. The local operational stack must already be running.

## Setup

Variable **names** only; values live in the gitignored `.env.production.local` (mode 0600) or the
host's secret store, never in the repository and never in `VITE_*`.

Vite loads `.env`, `.env.local`, `.env.production`, then `.env.production.local` (last wins; shell
variables beat all). The committed `.env.production` already selects the production profile and the
hosted main project, but it sets `OPERATIONAL_DB_ENABLED=false` and `BINANCE_COLLECTOR_ENABLED=false`, so
the host's file must override them. Values in `.env.local` still fill any name the production file
leaves unset, so set every name below in `.env.production.local` explicitly.

- Main project: `APP_PROFILE=production`, `ALLOW_HOSTED_SUPABASE=true`, `SUPABASE_URL`,
  `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SERVICE_ROLE_KEY`
- Operational project (local stack): `OPERATIONAL_DB_ENABLED=true`, `OPERATIONAL_SUPABASE_URL`,
  `OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY`, `OPERATIONAL_CANDLE_RETENTION_DAYS` (default 8 is enough)
- `BINANCE_COLLECTOR_ENABLED=true`
- Python: `PYTHON_ANALYSIS_ENABLED=true`, `PYTHON_ANALYSIS_URL` and `PYTHON_FORWARD_URL` (two different
  loopback HTTP origins with explicit ports, for example ports 8000 and 8001), `PYTHON_ANALYSIS_TOKEN`
  (32–256 URL-safe characters)
- Optional: `FORWARD_USER_ID` (narrow the jobs to one account), `MOVEMENT_FINALIZATION_GRACE_MS`

Prerequisites: Node 22.12+, `npm ci`, and the Python environment
(`python3 -m venv python/.venv && python/.venv/bin/python -m pip install -r python/requirements.txt`).

### First-time order

1. **Hosted main migrations first.** Apply the repository's `supabase/migrations` to the hosted main
   project (P29's migration must be there before the runner). Start the local operational stack and apply
   its migrations as `dev-local.mjs` does:
   `supabase start --workdir operational-db` then `supabase migration up --local --workdir operational-db`.
2. **Restart Python.** Make sure no old FastAPI is running; the host refuses to start while anything
   already answers on either Python port.
3. **Seed import** (sigma state, see [forward harness](forward-harness.md)). The loader reads only the
   process environment, so pass the files explicitly:
   `node --env-file=.env.production --env-file=.env.production.local scripts/seed-sigma-state.mjs <seed.json>`
4. **Backfill the 1m history** once, in production mode (the plain npm script would load the
   development environment): `npm run forward:backfill -- --mode production`. Re-running resumes.
5. **Start the host:** `npm run worker:host`.
6. **Check:** `npm run mvp:check -- --mode production` (its `worker host` line, below).

## Preflight

The host refuses to start (exit 1, one line per problem, no secret values) when:

- `.env.production.local` is missing;
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
  serves either Python port, or another worker host is running.

The checks reuse `validateServerSupabase`, `validatePublicSecrets`, `operationalDbConfig`,
`pythonServiceConfig` and `validateCollectorWorkerEnvironment`; the pure part is
`src/lib/worker-host.ts`.

## Restart policy and status

Per child: exponential backoff starting at 2 s, doubling to a 5 minute cap; a run of 10 minutes or more
resets it. **Crash-loop breaker:** 5 crashes within 10 minutes stop restarting that child; the others
keep running and the host exits non-zero at shutdown. Any exit while the host is running counts as a
crash (the children are meant to run forever). After the breaker opens, fix the cause and restart the
host.

`.worker-host.status.json` (gitignored, written atomically every 15 s and on every change) holds each
child's state (`waiting`, `running`, `backoff`, `crash-loop`, `stopping`, `stopped`), pid, restart count,
last restart time and last exit. On a clean stop the file is removed; after a non-zero exit it is kept.

`npm run mvp:check` gets one new line, `worker host`: **PASS** fresh (updated within 2 minutes) and all
children running; **WARN** file missing (host not used, e.g. the `dev:local:all` flow) or fresh but a
child is waiting, backing off or stopping; **FAIL** stale, unreadable, or any child in a crash loop.
Existing lines are unchanged.

## Stopping

Ctrl+C, SIGINT or SIGTERM: the host sends SIGTERM to every child, then SIGKILL to any still alive after
10 s; a second signal kills at once. It exits 0, or 1 if a crash-loop was seen.

- **Linux/macOS:** each child leads its own process group, and signals go to the group. This is the
  handled and intended path.
- **Windows:** Node has no POSIX signals there. `SIGINT`/`SIGBREAK`/`SIGTERM` are handled by the host;
  the "SIGTERM" step is a forced terminate of the direct child and the 10 s step is
  `taskkill /pid N /T /F` for its tree. There is no graceful shutdown on this path. The code path exists
  but has **not been run on Windows**.

If the host itself is SIGKILLed, children are orphaned and keep the Python ports, so the next start
refuses until they are stopped. Under systemd (control-group kill) this does not happen.

## Keep it running

Start on boot (examples, adjust paths; none were run):

- **systemd user unit** (`~/.config/systemd/user/worker-host.service`): `WorkingDirectory=` the
  repository, `ExecStart=` the absolute `node` path with `scripts/worker-host.mjs`, `Restart=on-failure`,
  `RestartSec=30`, `TimeoutStopSec=20`, `KillMode=control-group`; then
  `loginctl enable-linger $USER` and `systemctl --user enable --now worker-host`. The host's own
  `Restart` handling is per child; systemd only covers the supervisor.
- **pm2:** `pm2 start scripts/worker-host.mjs --name worker-host --kill-timeout 15000`, then
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
