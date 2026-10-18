# Deploy the web app to Cloudflare Workers

Refs #277. This deploys the TanStack web app using the existing Nitro
`cloudflare-module` preset. Machine-only jobs remain on the owner's host; their
separation from web routes is tracked in #278.

## Connect GitHub with Workers Builds

In Cloudflare **Workers & Pages**, create a Worker using **Import a repository**
(or connect Git under an existing Worker's **Settings > Build**). Authorize GitHub
access to this repository and select these settings:

| Setting | Value |
| --- | --- |
| Worker name | `crypto-watcher` (must match `wrangler.jsonc`) |
| Production branch | `main` |
| Root directory | `/` (repository root) |
| Build command | `npm ci && npm run build` |
| Deploy command | `npm run deploy` |

Use the Workers Free plan. No paid binding or account ID is required in the
repository. Workers Builds manages its deployment API token in Cloudflare; do not
put that token in Git. Set the variables below before starting/retrying the build.

## Build variables

Set these in **Settings > Build > Variables and secrets**, not just the runtime
settings. Node must satisfy the repository's `>=22.12.0 <23` requirement.
Cloudflare has two separate lists: build variables (including `VITE_*` values
compiled into the browser bundle) and Worker runtime variables under **Settings >
Variables and Secrets**. Setting a build variable does not configure the running
Worker; the runtime values are supplied separately as described below.

| Variable | Value supplied by the owner |
| --- | --- |
| `NODE_VERSION` | `22` |
| `APP_PROFILE`, `VITE_APP_PROFILE` | `production` for both |
| `ALLOW_HOSTED_SUPABASE`, `VITE_ALLOW_HOSTED_SUPABASE` | `true` for both |
| `SUPABASE_URL`, `VITE_SUPABASE_URL` | The same hosted Supabase HTTPS origin |
| `SUPABASE_PUBLISHABLE_KEY`, `VITE_SUPABASE_PUBLISHABLE_KEY` | The same public publishable key (or legacy `anon` key) |
| `VITE_GOOGLE_CLIENT_ID` | Google Web application OAuth client ID ending in `.apps.googleusercontent.com`; empty disables Google sign-in |

These names match [.env.example](../.env.example). Build validation requires both
server and browser profiles, URLs and public keys to agree. Explicit dashboard
values override the committed production public defaults. Every `VITE_*` value is
public and compiled into the browser bundle: never use a service-role key or any
secret there. Rebuild after changing browser values. Runtime secrets are not
needed to compile the app.

## Build output and Wrangler configuration

`npm run build` runs `vite build`. The pinned Nitro preset emits:

| Artifact | Path |
| --- | --- |
| Output directory | `.output/` |
| Worker entry | `.output/server/index.mjs` |
| Static assets | `.output/public/` |
| Generated Wrangler config | `.output/server/wrangler.json` |
| Wrangler config redirect | `.wrangler/deploy/config.json` |

The generated config uses `main: "index.mjs"` and assets `directory: "../public"`
relative to `.output/server`, with binding `ASSETS`, `nodejs_compat`, `no_bundle`
and ES module rules. The committed root `wrangler.jsonc` expresses the same paths
relative to the repository root. Nitro reads it, preserves the Worker name/date
and other settings, and rewrites the output-relative paths. Its warnings that
`main` and `assets` are overridden are expected. `wrangler deploy` follows the
generated redirect, so always build before deploying and do not commit generated
output. `nodejs_compat` is needed by emitted `node:process` imports and server
crypto usage; it does not make host processes or persistent local disk available.

## Runtime configuration and secrets

The non-secret runtime variables are committed in the `vars` block of
[`wrangler.jsonc`](../wrangler.jsonc), except the publishable key:

| Runtime variable | Committed value |
| --- | --- |
| `ALLOW_HOSTED_SUPABASE` | `true` |
| `APP_PROFILE` | `production` |
| `BINANCE_COLLECTOR_ENABLED` | `false` |
| `MACHINE_ONLY_ENABLED` | `false` |
| `OPERATIONAL_DB_ENABLED` | `false` |
| `PYTHON_ANALYSIS_ENABLED` | `false` |
| `RUNNER_GATEWAY_ENABLED` | `true` |
| `RUNNER_JOBS_ENABLED` | `true` |
| `SCHEDULED_MONITOR_ENABLED` | `false` |
| `SUPABASE_URL` | `https://xshzjhocpcenuumdrhdh.supabase.co` |

`SCHEDULED_MONITOR_ENABLED` stays `false` and the Worker cron for `/api/public/hooks/monitor-prices`
stays off: the Worker has no operational database. Scheduled checks (movement alerts and completed-candle
TA) run on the owner's machine in the worker-host child `monitor-scheduler`, about once a minute; see
[worker host](worker-host.md#scheduled-checks-monitor-scheduler).

Set `SUPABASE_PUBLISHABLE_KEY` in the Worker's **Settings > Variables and Secrets**
to match the public key used by the build. Keys and secrets stay in the dashboard;
none belongs in the committed `vars` block. `keep_vars: true` preserves dashboard
runtime variables that are not defined in the config; it does not protect config-defined vars. Change the runner flags in
`wrangler.jsonc`, not the dashboard. Build profiles, hosted opt-ins,
URLs and public keys must match these runtime values.

If runtime variables are missing, the app fails validation; a missing
`APP_PROFILE` produces `[Environment] APP_PROFILE must be local or production`.
Setting `VITE_APP_PROFILE` or `APP_PROFILE` only in the build-variable list does
not resolve a missing runtime profile. Rebuild and redeploy with the committed
runtime configuration, and set the runtime publishable key in its separate
dashboard list.

`MACHINE_ONLY_ENABLED=false` keeps direct machine execution disabled on the
Worker. With `RUNNER_JOBS_ENABLED=true`, authenticated `runForwardNow` and
`runForwardTrendNow` calls enqueue durable jobs in hosted Supabase for the host
instead. Do not enable `MACHINE_ONLY_ENABLED` on Cloudflare; local development
and `worker:host` explicitly enable it on the owner's machine.

The first deployment runs with only the public build and runtime values. Leave
`SUPABASE_SERVICE_ROLE_KEY` and `CRON_SECRET` unset for that deployment. Add them
only if another deployed route needs them. The #278 gate prevents direct machine
execution on the Worker even when those secrets are present; durable jobs use the signed-in user’s Supabase client.

The committed runtime flags start scheduled monitoring, the collector, the
operational database and Python analysis disabled. If enabling
remote services later, set a reachable `OPERATIONAL_SUPABASE_URL` and secret
`OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY`, or `PYTHON_ANALYSIS_URL` and secret
`PYTHON_ANALYSIS_TOKEN`, respectively. Loopback URLs cannot reach the owner's
machine from a Worker. See [environment setup](environments.md) for activity gates.

### Later: add secrets for enabled routes

With the #278 machine-only gate in place, add only the secrets required by the
routes deployed to Workers. Use the dashboard's **Secret** type, or run the
applicable command locally after building and authenticating Wrangler; enter
values at the prompts:

```sh
npx wrangler secret put SUPABASE_SERVICE_ROLE_KEY
npx wrangler secret put CRON_SECRET
```

`SUPABASE_SERVICE_ROLE_KEY` authorizes server-side database operations.
`CRON_SECRET` is the cron authentication name; callers send it as
`Authorization: Bearer <secret>`. Optional `CRON_SECRET_PREVIOUS` allows rotation
overlap. The separate existing `MONITOR_CRON_TOKEN` is an optional alternative
credential for the monitor/collector-subscription hooks; `CRON_SECRET` works for
both without setting it. No scheduler is created by this deployment.

Keep all actual secret values in Cloudflare, never in `wrangler.jsonc`, docs,
commits, browser variables, or build logs. `.dev.vars` and local environment
overrides are gitignored.

## Runner gateway

Gateway and jobs modules, including their transitive dependencies, must use static
imports: Vite's `runnerImport` closes its module runner after loading the entry
module, so later dynamic imports can fail with "Vite module runner has been closed".
Keep the Worker-side dynamic imports in `src/lib/monitor.functions.ts`; those
preserve the Cloudflare server-function boundary.

Refs #294. Both runner flags are on in committed `wrangler.jsonc`. Both hosted
Supabase migrations are applied, and `RUNNER_GATEWAY_URL` / `RUNNER_GATEWAY_SECRET`
are configured as Cloudflare runtime secrets. The machine must run
`npm run worker:host` with `RUNNER_GATEWAY_ENABLED=true` and
`RUNNER_JOBS_ENABLED=true` in `.env.production.local`.

To maintain the gateway and durable jobs setup:

1. On the machine, set `RUNNER_GATEWAY_ENABLED=true`, `RUNNER_JOBS_ENABLED=true` and a valid
   `RUNNER_GATEWAY_SECRET` in `.env.production.local`, then start
   `npm run worker:host` and expose the gateway through an HTTPS tunnel origin.
   See [the host gateway setup](worker-host.md#reaching-the-gateway).
2. In the Worker's **Settings > Variables and Secrets**, add
   `RUNNER_GATEWAY_URL` and `RUNNER_GATEWAY_SECRET` using the encrypted **Secret**
   type. The URL must be an HTTPS origin with no credentials, path, query, or
   fragment. Do not put either value in `wrangler.jsonc` or a `VITE_*` variable.
3. Set the Worker secret `RUNNER_GATEWAY_SECRET` to exactly the same value as
   `RUNNER_GATEWAY_SECRET` in `.env.production.local` on the machine. It must be
   32–256 characters using only letters, digits, underscores, and hyphens.
4. Deploy with both runner flags already set to `"true"` in the committed
   `wrangler.jsonc` runtime vars. Leave `PYTHON_ANALYSIS_ENABLED=false` on the Worker;
   the machine's Python service configuration handles the actual analysis.

The Worker passes the signed-in user's token to the gateway along with the
shared secret. Analysis and manual alert checks wait directly for a result with
a 25-second timeout. If the gateway cannot be reached, the UI shows a runner
offline notice and the action returns a message asking you to start
`npm run worker:host` and retry. An interrupted response from a reached gateway
shows a timeout retry message. The status check uses `/health` with a two-second
timeout, sends no gateway secret, and polls every 30 seconds while configured.
Keep the HTTPS tunnel and the host supervisor running for the gateway to remain
reachable. Run forward and Run trend use `public.runner_jobs` in hosted Supabase; the
host jobs runner consumes them without using the tunnel. Quick-tunnel URLs change
on restart; update the Cloudflare runtime URL secret when that happens. A fixed URL avoids
this: a Tailscale Funnel (fixed URL) or later an owned-domain Cloudflare tunnel; see the
[stable URL setup](worker-host.md#stable-url-tailscale-funnel) (#308).

## After the first deployment

1. Copy the HTTPS Workers URL shown by Cloudflare.
2. In Google Cloud Console, edit the Web application OAuth client matching
   `VITE_GOOGLE_CLIENT_ID`. Add that origin (scheme and hostname, no path) to
   **Authorized JavaScript origins**.
3. In Supabase **Authentication > URL Configuration > Redirect URLs**, add
   `<url>/auth/callback`, replacing `<url>` with the Workers origin. Use the
   production origin as Site URL where appropriate. Repeat for a custom domain.
4. Once the public runtime values are set, check sign-in and the callback on the
   deployed site. See [Supabase setup](supabase-setup.md) for provider configuration.

## Machine-only work and stale data

When enabled on the owner's machine, the forward-run server functions reach
`forward-sigma-cache.server.ts`, which uses host filesystem paths, persistent
`.forward-state` files, PID locks and `process.kill`. Workers' virtual filesystem
does not provide those host semantics. Python analysis requires a reachable
external service; local FastAPI/operational database endpoints will not work.
The `worker:host` supervisor spawns child processes and the collector and forward
CLI jobs expect a long-lived machine; none is started by this web deployment.
The Worker enqueues the two on-demand routes as durable jobs; execution remains
on the host.
The Market dashboard and Strategy Lab show a stale-data banner when the newest
hosted forward run's processed boundary is more than two hours old, using the
existing authenticated hosted read. A recent retry timestamp does not hide old
processed data. Empty and failed reads show separate availability messages.
See the [forward outage runbook](forward-outage-runbook.md) for recovery.

Free-plan upload size, CPU and subrequest limits still apply. A successful build
does not prove expensive monitor/analysis routes fit the free runtime budget;
review deployment size and runtime behavior before enabling other expensive routes.

References: [Nitro Cloudflare preset](https://nitro.build/deploy/providers/cloudflare),
[Workers Builds configuration](https://developers.cloudflare.com/workers/ci-cd/builds/configuration/),
[Worker secrets](https://developers.cloudflare.com/workers/configuration/secrets/),
[Workers filesystem](https://developers.cloudflare.com/workers/runtime-apis/nodejs/fs/),
[Workers limits](https://developers.cloudflare.com/workers/platform/limits/).
