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

| Variable | Value supplied by the owner |
| --- | --- |
| `NODE_VERSION` | `22.23.2` |
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

Under the Worker's **Settings > Variables and Secrets**, separately set
`APP_PROFILE=production`, `ALLOW_HOSTED_SUPABASE=true`, `SUPABASE_URL`, and
`SUPABASE_PUBLISHABLE_KEY` to match the build. `keep_vars` preserves dashboard
runtime variables across Wrangler deployments.

Add secrets using the dashboard's **Secret** type, or run these locally after
building and authenticating Wrangler; enter values at the prompts:

```sh
npx wrangler secret put SUPABASE_SERVICE_ROLE_KEY
npx wrangler secret put CRON_SECRET
```

`SUPABASE_SERVICE_ROLE_KEY` authorizes server-side database operations.
`CRON_SECRET` already is the cron authentication name; callers send it as
`Authorization: Bearer <secret>`. Optional `CRON_SECRET_PREVIOUS` allows rotation
overlap. The separate existing `MONITOR_CRON_TOKEN` is an optional alternative
credential for the monitor/collector-subscription hooks; `CRON_SECRET` works for
both without setting it. No scheduler is created by this deployment.

Start with `SCHEDULED_MONITOR_ENABLED=false`, `BINANCE_COLLECTOR_ENABLED=false`,
`OPERATIONAL_DB_ENABLED=false`, and `PYTHON_ANALYSIS_ENABLED=false`. If enabling
remote services later, set a reachable `OPERATIONAL_SUPABASE_URL` and secret
`OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY`, or `PYTHON_ANALYSIS_URL` and secret
`PYTHON_ANALYSIS_TOKEN`, respectively. Loopback URLs cannot reach the owner's
machine from a Worker. See [environment setup](environments.md) for activity gates.

Keep all actual secret values in Cloudflare, never in `wrangler.jsonc`, docs,
commits, browser variables, or build logs. `.dev.vars` and local environment
overrides are gitignored.

## After the first deployment

1. Copy the HTTPS Workers URL shown by Cloudflare.
2. In Google Cloud Console, edit the Web application OAuth client matching
   `VITE_GOOGLE_CLIENT_ID`. Add that origin (scheme and hostname, no path) to
   **Authorized JavaScript origins**.
3. In Supabase **Authentication > URL Configuration > Redirect URLs**, add
   `<url>/auth/callback`, replacing `<url>` with the Workers origin. Use the
   production origin as Site URL where appropriate. Repeat for a custom domain.
4. Once runtime values and secrets are set, check sign-in and the callback on the
   deployed site. See [Supabase setup](supabase-setup.md) for provider configuration.

## Known limitations deferred to #278

The forward-run server functions (`runForwardNow`, `runForwardTrendNow`) reach
`forward-sigma-cache.server.ts`, which uses host filesystem paths, persistent
`.forward-state` files, PID locks and `process.kill`. Workers' virtual filesystem
does not provide those host semantics. Python analysis requires a reachable
external service; local FastAPI/operational database endpoints will not work.
The `worker:host` supervisor spawns child processes and the collector and forward
CLI jobs expect a long-lived machine; none is started by this web deployment.
These routes/jobs are not made Workers-compatible here.

Free-plan upload size, CPU and subrequest limits still apply. A successful build
does not prove expensive monitor/analysis routes fit the free runtime budget;
review deployment size and runtime behavior when handling #278.

References: [Nitro Cloudflare preset](https://nitro.build/deploy/providers/cloudflare),
[Workers Builds configuration](https://developers.cloudflare.com/workers/ci-cd/builds/configuration/),
[Worker secrets](https://developers.cloudflare.com/workers/configuration/secrets/),
[Workers filesystem](https://developers.cloudflare.com/workers/runtime-apis/nodejs/fs/),
[Workers limits](https://developers.cloudflare.com/workers/platform/limits/).
