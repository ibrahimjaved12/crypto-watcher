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
npx supabase start
npx supabase db reset --local
npm run env:local
npm run dev
```

`supabase start` downloads and starts the local database, auth, and other services
in Docker; you do not install Postgres separately. The first run needs internet
access for container images. This repository already has `supabase/config.toml`,
so skip `supabase init`. No Supabase account, login, hosted link, or Lovable Cloud
is required. `npm run env:local` can find the project-installed CLI because npm
adds `node_modules/.bin` to PATH.

Open the app URL printed by Vite. Local email/password provisioning and account
isolation are tracked in [issue #50](https://github.com/ibrahimjaved12/crypto-watcher/issues/50);
Google sign-in requires separate OAuth provisioning.

**Environment values:** Nothing needs manual filling for the basic app:
`npm run env:local` writes both profiles, both loopback URLs, both anon keys,
the server service-role key, and disabled automatic activity defaults. It also
generates a Python token but leaves Python analysis disabled. Leave cron secrets
unset and scheduled monitoring disabled. Do not copy hosted keys into this file.
The root `.env.example` is a reference, not a file you must copy first.

On later sessions, run `npx supabase start` then `npm run dev`; keep the existing
`.env.local`. Stop the app with Ctrl+C and the database with `npx supabase stop`.

The generator reads `supabase status -o json`, writes a mode-0600, gitignored
`.env.local`, and refuses to overwrite an existing file. It does not link, push,
seed, or access a hosted project. For existing local files, update values using
`supabase status`; never paste its secret output into commits or logs. The committed
migrations replay cleanly with `npx supabase db reset --local`; deterministic users
and seed data remain part of [issue #48](https://github.com/ibrahimjaved12/crypto-watcher/issues/48).

Vite loads `.env`, `.env.local`, then mode-specific files; shell variables have
highest priority. Restart after changes. Local URLs must be HTTP(S) origins using
`localhost`, `127.0.0.1`, or `[::1]`. Browser and server must use the same origin,
profile, and public key (even localhost/127.0.0.1 aliases must match).

Optional Python analysis: follow [Python setup](../python/README.md), give FastAPI
the same `PYTHON_ANALYSIS_TOKEN` generated in `.env.local`, and set
`PYTHON_ANALYSIS_ENABLED=true` for TanStack. FastAPI does not automatically load the
root dotenv file. It needs only its token, not Supabase credentials.

## Hosted production and shared staging

`npm run build` selects Vite's production mode and the committed `.env.production`
public configuration for the existing Lovable project. This file explicitly selects
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

All variables are listed in the root [.env.example](../.env.example). Empty required
values fail validation. Browser values are public and embedded at build time; server
values are runtime configuration (loaded into the dev process by Vite locally).

| Variable | Owner / scope | Safe default and requirement |
| --- | --- | --- |
| `APP_PROFILE` | TanStack + build validation | Required: `local` for development, `production` for hosted use |
| `VITE_APP_PROFILE` | Browser + build | Required; must match server |
| `ALLOW_HOSTED_SUPABASE` | TanStack + build | `false`; exact `true` required for hosted use |
| `VITE_ALLOW_HOSTED_SUPABASE` | Browser + build | `false`; exact `true` required for hosted use |
| `SUPABASE_URL` | TanStack | Required; generated loopback API URL |
| `VITE_SUPABASE_URL` | Browser | Required; identical to server URL |
| `SUPABASE_PUBLISHABLE_KEY` | TanStack | Required; local anon key from status |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | Browser | Required; identical public/anon key; secret/service-role keys rejected |
| `SUPABASE_SERVICE_ROLE_KEY` | TanStack secret | Empty; required for admin monitoring operations; local value from status |
| `SUPABASE_PROJECT_ID`, `VITE_SUPABASE_PROJECT_ID` | Server / browser metadata | Optional; not used to choose targets |
| `PYTHON_ANALYSIS_ENABLED` | TanStack | `false`; explicit `true` enables manual analysis |
| `PYTHON_ANALYSIS_URL` | TanStack | `http://127.0.0.1:8000`; required when enabled |
| `PYTHON_ANALYSIS_TOKEN` | TanStack + FastAPI secret | Empty/disabled; matching 32–256 URL-safe characters required when enabled |
| `MONITOR_CRON_TOKEN` | Scheduler caller + TanStack secret | Empty; required for the public monitor hook |
| `LOVABLE_CRON_SECRET` | Lovable scheduler + TanStack secret | Empty; used by Lovable cron authentication |
| `LOVABLE_CRON_SECRET_PREVIOUS` | TanStack secret | Empty; optional rotation overlap |
| `SCHEDULED_MONITOR_ENABLED` | TanStack | `false` |
| `VITE_MARKET_AUTO_REFRESH_ENABLED` | Browser | `false` |
| `VITE_TA_HISTORY_AUTO_REFRESH_ENABLED` | Browser | `false` |
| `TA_GENERATION_ENABLED` | TanStack | `true`; preserves manual checks |
| `TA_OUTCOME_EVALUATION_ENABLED` | TanStack | `true`; preserves manual checks |
| `ACTIVITY_DIAGNOSTICS` | TanStack | `false` |
| `VITE_ACTIVITY_DIAGNOSTICS` | Browser | `false` |

Never prefix tokens, passwords, private keys, or secrets with `VITE_`. Startup rejects
secret variable names and copied server-secret values in public configuration, as
well as Supabase secret/service-role keys used as publishable keys.

## Actions and services

| Action | Services used |
| --- | --- |
| Sign in, initial reads, watchlist/settings changes, default row creation | Selected Supabase auth/database: local by default; shared staging or Lovable only with explicit hosted profile |
| Refresh market prices | Public exchange APIs; independent of Lovable Cloud |
| Run check now / enabled scheduled checks | Selected Supabase reads/writes and public exchange prices/candles |
| Refresh TA history | Selected Supabase |
| Manual Python analysis | Selected Supabase reads → configured FastAPI → public exchange APIs; local FastAPI by default |
| `supabase start`, `npm run env:local` | Local Docker/Supabase; startup may download container images |
| Production build | Compiles explicit hosted configuration; does not itself run monitoring or seed data |

[Manual activity controls](activity-controls.md) and [per-user controls](activity-domains.md)
remain in effect. Automatic market/history refresh and scheduled monitoring remain
off; authentication and manual database actions remain available. No scheduler is
created by this change.
