# Environment setup

Ordinary `npm run dev` has no hosted defaults. It fails before serving the app if
configuration is missing, targets disagree, or local mode points outside loopback.

## Local development

Prerequisites on Ubuntu (the current workspace runs Ubuntu 22.04):

1. **Node 22.12+ within Node 22, and npm.** Check `node --version` and
   `npm --version`. The workspace already has compatible Node 22.23.2.
2. **Docker Engine running and accessible to your user.** Follow the official
   [Ubuntu Docker installation](https://docs.docker.com/engine/install/ubuntu/)
   using its apt repository instructions, then the
   [non-root setup](https://docs.docker.com/engine/install/linux-postinstall/).
   Confirm `docker info` and `docker run --rm hello-world` work without sudo.
   Docker group membership grants root-level access. If this is WSL, use Docker
   Desktop's WSL integration or install Engine inside your Linux environment.
3. **Supabase CLI.** It is pinned as a project development dependency and installed
   by `npm ci`. Run it through `npx`; a global `supabase` command is not installed.
   [Official CLI setup](https://supabase.com/docs/guides/local-development/cli/getting-started).

Run from the repository root after Docker is ready:

```sh
npm ci
npx supabase --version
python3 -m venv python/.venv
python/.venv/bin/python -m pip install -r python/requirements.txt
npm run dev:local
```

`npm run dev:local` starts the main local Supabase project and disables the operational store.
`npm run dev:local:all` additionally starts and enables the separate operational project. Both
commands create `.env.local` when it is missing, start FastAPI when
`PYTHON_ANALYSIS_ENABLED=true`, and then start the application with a clean Vite dependency
optimization pass. `supabase start` downloads the local database, auth, and other services
in Docker; you do not install Postgres separately. The first run needs internet
access for container images. This repository already has `supabase/config.toml`, so
skip `supabase init`. No Supabase account, login, hosted link, or the hosted Supabase project is
required. npm adds the project-installed Supabase CLI to `PATH` for this command.

Ctrl+C stops the application and any FastAPI process started by the launcher.
Started Supabase stacks stay running so later starts are fast and local data remains available.
Run `npm run dev:local:stop` when you want to stop both projects. The launcher
reuses an already healthy FastAPI process instead of starting a duplicate.

Open the app URL printed by Vite. Local email/password provisioning and account
isolation are tracked in [issue #50](https://github.com/ibrahimjaved12/crypto-watcher/issues/50);
Google sign-in requires separate OAuth provisioning.

**Environment values:** Nothing needs manual filling for the basic app:
`npm run env:local` writes the main profiles, loopback URLs, anon keys,
server service-role key, and disabled automatic activity defaults. It also
generates a Python token but leaves Python analysis disabled. Leave cron secrets
unset and scheduled monitoring disabled. Do not copy hosted keys into this file.
The root `.env.example` is a reference, not a file you must copy first.

On later sessions, use `npm run dev:local` without operational storage or
`npm run dev:local:all` with it; keep the existing `.env.local`.
Use `npm run dev` only when you intentionally want the application without managing
Supabase or FastAPI.

The generator reads the main local Supabase status, writes a mode-0600, gitignored
`.env.local`, and refuses to overwrite an existing file. It does not link, push,
seed, or access a hosted project. For an existing local file, run
`npm run env:local:operational` once after starting the operational stack; never paste
status secret output into commits or logs. The committed
migrations replay cleanly with `npx supabase db reset --local`; deterministic users
and seed data remain part of [issue #48](https://github.com/ibrahimjaved12/crypto-watcher/issues/48).

Vite loads `.env`, `.env.local`, then mode-specific files; shell variables have
highest priority. Restart after changes. Local URLs must be HTTP(S) origins using
`localhost`, `127.0.0.1`, or `[::1]`. Browser and server must use the same origin,
profile, and public key (even localhost/127.0.0.1 aliases must match).

Optional Python analysis: follow [Python setup](../python/README.md), give FastAPI
the same `PYTHON_ANALYSIS_TOKEN` generated in `.env.local`, and set
`PYTHON_ANALYSIS_ENABLED=true` for TanStack. `npm run dev:local` passes that token to
FastAPI. FastAPI needs only its token, not Supabase credentials.

## Hosted production and shared staging

`npm run build` selects Vite's production mode and the committed `.env.production`
public configuration for the owner's Supabase project. See
[Supabase project setup](supabase-setup.md) for the project reference, migration
status, pending security verification, and Google OAuth setup. This file explicitly selects
`production` and opts into hosted Supabase. No service secrets belong in that file.
Configure the deployed TanStack runtime with `APP_PROFILE=production`,
`ALLOW_HOSTED_SUPABASE=true`, the matching `SUPABASE_URL` and
`SUPABASE_PUBLISHABLE_KEY`, and its server secrets. Runtime checks compare these
against the browser configuration embedded at build time before handling requests.

For an intentional hosted development session use `npm run dev -- --mode production`.
This accesses real hosted data. For shared staging use a gitignored
`.env.staging.local` with **both** profiles set to `production`, both hosted opt-ins
set to `true`, and both URL/key pairs set to the staging project; run
`npm run dev -- --mode staging` or `npm run build -- --mode staging`. There is no
implicit staging fallback. Deploy matching server runtime variables separately.
A hosted URL must use HTTPS. Activity flags do not authorize a hosted target.

## Variables

`MACHINE_ONLY_ENABLED` is server-only and defaults off unless exactly `true`.
Local Vite development (`.env.development`), the local launcher and `worker:host`
enable it; Cloudflare commits `false`. It gates the two on-demand forward actions
before machine imports. The UI reads the capability through an authenticated,
read-only server function; never create a `VITE_MACHINE_ONLY_ENABLED` variable.

All variables are listed in the root [.env.example](../.env.example). Empty required
values fail validation. Browser values are public and embedded at build time; server
values are runtime configuration (loaded into the dev process by Vite locally).

| Variable                                          | Owner / scope                       | Safe default and requirement                                                                            |
| ------------------------------------------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `APP_PROFILE`                                     | TanStack + build validation         | Required: `local` for development, `production` for hosted use                                          |
| `VITE_APP_PROFILE`                                | Browser + build                     | Required; must match server                                                                             |
| `ALLOW_HOSTED_SUPABASE`                           | TanStack + build                    | `false`; exact `true` required for hosted use                                                           |
| `VITE_ALLOW_HOSTED_SUPABASE`                      | Browser + build                     | `false`; exact `true` required for hosted use                                                           |
| `SUPABASE_URL`                                    | TanStack                            | Required; generated loopback API URL                                                                    |
| `VITE_SUPABASE_URL`                               | Browser                             | Required; identical to server URL                                                                       |
| `SUPABASE_PUBLISHABLE_KEY`                        | TanStack                            | Required; local anon key from status                                                                    |
| `VITE_SUPABASE_PUBLISHABLE_KEY`                   | Browser                             | Required; identical public/anon key; secret/service-role keys rejected                                  |
| `SUPABASE_SERVICE_ROLE_KEY`                       | TanStack secret                     | Empty; required for admin monitoring operations; local value from status                                |
| `SUPABASE_PROJECT_ID`, `VITE_SUPABASE_PROJECT_ID` | Server / browser metadata           | Optional; not used to choose targets                                                                    |
| `OPERATIONAL_DB_ENABLED`                          | TanStack + collector worker         | `false` for rollback; exact `true` transfers checkpoint/run ownership and enables recent-candle storage |
| `MARKET_QUOTE_PUBLISHER_ENABLED`                  | Collector worker                    | Optional; `false` stops the market quote publisher (#323). It also needs `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` on the machine |
| `OPERATIONAL_SUPABASE_URL`                        | TanStack + collector worker         | Separate local/hosted operational Supabase origin; required when enabled                                |
| `OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY`           | TanStack + collector worker secret  | Operational project service-role key; required when enabled; never `VITE_*`                             |
| `OPERATIONAL_CANDLE_RETENTION_DAYS`               | TanStack + collector worker         | `8`; allowed range 1–30; collector needs at least 8 for #71 warm-up                                    |
| `OPERATIONAL_MONITOR_RUN_RETENTION_DAYS`          | TanStack + collector worker         | `30`; allowed range 1–90                                                                                |
| `OPERATIONAL_OUTBOX_MAX_ATTEMPTS`                 | TanStack + collector worker         | `10`; allowed range 1–100 before dead-letter                                                            |
| `BINANCE_COLLECTOR_ENABLED`                       | Collector worker + app server       | Server-only cutover; requires operational DB and the separate collector worker process                  |
| `PYTHON_ANALYSIS_ENABLED`                         | TanStack + collector worker         | `false`; explicit `true` enables manual/scheduled TA and canonical Python movement buckets; movement service must be one replica/one Uvicorn worker |
| `PYTHON_ANALYSIS_URL`                             | TanStack + collector worker         | `http://127.0.0.1:8000`; required when enabled                                                          |
| `PYTHON_ANALYSIS_TOKEN`                           | TanStack + collector worker + FastAPI secret | Empty/disabled; matching 32–256 URL-safe characters required when enabled                     |
| `MONITOR_CRON_TOKEN`                              | Scheduler caller + TanStack secret  | Empty; required for the public monitor and collector-universe hooks                                     |
| `CRON_SECRET`                             | scheduled job + TanStack secret | Empty; used by cron authentication                                                              |
| `CRON_SECRET_PREVIOUS`                    | TanStack secret                     | Empty; optional rotation overlap                                                                        |
| `SCHEDULED_MONITOR_ENABLED`                       | TanStack                            | `false`                                                                                                 |
| `VITE_MARKET_AUTO_REFRESH_ENABLED`                | Browser                             | `false`                                                                                                 |
| `VITE_TA_HISTORY_AUTO_REFRESH_ENABLED`            | Browser                             | `false`                                                                                                 |
| `TA_GENERATION_ENABLED`                           | TanStack                            | `true`; preserves manual checks                                                                         |
| `TA_OUTCOME_EVALUATION_ENABLED`                   | TanStack                            | `true`; preserves manual checks                                                                         |
| `ACTIVITY_DIAGNOSTICS`                            | TanStack                            | `false`                                                                                                 |
| `VITE_ACTIVITY_DIAGNOSTICS`                       | Browser                             | `false`                                                                                                 |
| `VITE_GOOGLE_CLIENT_ID`                           | Browser                             | Optional, empty. Google OAuth client ID (public, not a secret) for Google Identity Services sign-in; when empty the sign-in page shows email only. Must end in `.apps.googleusercontent.com`. |

Never prefix tokens, passwords, private keys, or secrets with `VITE_`. Startup rejects
secret variable names and copied server-secret values in public configuration, as
well as Supabase secret/service-role keys used as publishable keys.

The collector worker is a headless ingestion process with its own server-only runtime
configuration boundary. It validates only the operational database variables and
`MOVEMENT_FINALIZATION_GRACE_MS` from the host's runtime `process.env`; it needs no main
main-database credential and never reads browser `VITE_*` values or build-time public
configuration, so one built artifact stays host-independent. Only the TanStack
application additionally proves its runtime matches the browser bundle it serves and owns
the main-database credentials. See
[the collector design](./binance-futures-collector.md).

The scheduled monitor hook and the collector-universe reconciliation hook authenticate with
`MONITOR_CRON_TOKEN` or the platform cron secret. Reconciling the collector's shared
subscription universe does not depend on `SCHEDULED_MONITOR_ENABLED`; disabling scheduled
monitoring never leaves the collector empty or stale.

## Actions and services

| Action                                                                   | Services used                                                                                                      |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| Sign in, initial reads, watchlist/settings changes, default row creation | Selected Supabase auth/database: local by default; shared staging or the hosted project only with explicit hosted profile     |
| Refresh market prices <!-- TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days --> | Hosted `market_quotes`, except the temporary degraded-mode fallback to Kraken/OKX                                                                 |
| Run check now / enabled scheduled checks                                 | Main-database permanent-state operations, operational working-state operations when enabled, and public exchange candles |
| Refresh TA history                                                       | Selected Supabase                                                                                                  |
| Manual Python analysis                                                   | Selected Supabase reads → configured FastAPI → public exchange APIs; local FastAPI by default                      |
| `npm run dev:local`                                                      | Main local Supabase, optional local FastAPI, and the application; operational ownership forced off                 |
| `npm run dev:local:all`                                                  | Main and operational local Supabase, shared Binance collector, optional local FastAPI, and the application         |
| `supabase start`, `npm run env:local`                                    | Main local Supabase; startup may download container images                                                         |
| Production build                                                         | Compiles explicit hosted configuration; does not itself run monitoring or seed data                                |

[Manual activity controls](activity-controls.md) and [per-user controls](activity-domains.md)
remain in effect. Automatic market/history refresh and scheduled monitoring remain
off; authentication and manual database actions remain available. No scheduler is
created by this change.

Operational ownership, isolated migrations, retention, synchronization, and rollback are
documented in [Operational PostgreSQL](operational-database.md).

<!-- TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days -->
## Temporary degraded market fallback (#323)

`MARKET_DEGRADED_FALLBACK_ENABLED` is a Worker runtime variable, default `"true"`, committed
in the `wrangler.jsonc` vars block. Set it to `"false"` there and deploy to restore published-only
behavior. Old prices remain unavailable in the UI. Dashboard changes are overwritten on deploy:
`keep_vars` does not protect config-defined variables. It contains no credential.

For each `getMarketSnapshot` call, keep fresh published pairs unchanged and fetch new Kraken
futures candles for old or missing pairs. Retry only pairs without fresh Kraken data on OKX,
including pairs whose Kraken request succeeded but returned old candles. Never Binance: its
Worker egress is blocked. Each card keeps the source beside its pair name, with USD for Kraken
and USDT for Binance/OKX. Remove the repeated contract explanation, stale badges and degraded
price banner. Failed pairs retain published metadata in the API, but the UI displays prices and
charts only when fresh; otherwise the price is unavailable. Unexpected fallback errors still
return the published snapshot. Existing provider failure diagnostics remain available in Details.

Runner availability is shown once beside the logo: green Online or red Offline, with relative
last-online time from the persisted heartbeat and the exact time in a tooltip. Elapsed labels tick
every 30 seconds, including between health polls. The badge opens a service list with current
readiness and persisted last-working times. No history shows "No heartbeat received yet";
failed health reads show neutral "Status unknown" and keep controls unavailable. Alert checks are disabled,
Market read is omitted, and Strategy Lab service cards show only their titles while offline.
Online service cards keep run details and actions without repeating coloured service badges.

Fresh page loads use one hosted query. Kraken normally needs two candle requests per distinct
pair (at most 20 for the 10-pair watchlist). The degraded path caps exchange requests at 40 total
across both venues, leaving room for the hosted read; pairs without fresh data show unavailable.
OKX may return HTTP 429 from Worker egress. Exchange latency/availability affects page loads
only in degraded mode. Degraded quotes are not cached, so each page load or automatic refresh
repeats the exchange requests. Existing automatic-refresh controls still apply.

Unavailable cards retry together in one request for just their down symbols when automatic
market refresh is enabled. Retries use 2/4/8/15/30/60-second base delays with 20% jitter, counting
failed requests too. They stop when all pairs recover and do not poll in hidden tabs; visibility
and reconnect events join an immediate retry without duplicating an active batch. Disabling
`VITE_MARKET_AUTO_REFRESH_ENABLED` disables these retries too. Only fresh matching pairs merge
into the watchlist cache; failed retry results never erase existing quotes or change the last
full-refresh time. Cards keep quote errors and last-price-seen time, with a retry indicator while
automatic recovery is enabled.

Remove all blocks marked `TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days`
after 30 days of continuous machine operation with no publication gap over 10 minutes,
checked with `mvp:check`. Record the qualifying date in ticket #323, then delete this fallback
and its runtime variable.

Deployed verification: stop the machine's publisher for more than 10 minutes and refresh the
market page. Expect fresh Kraken prices, with OKX for pairs Kraken cannot refresh. Stop the runner:
expect the red Offline header and title-only service cards; alert checks cannot be clicked.
Restart the publisher and refresh after a new publication: fresh Binance quotes return.
With the switch off, old published prices show unavailable.
