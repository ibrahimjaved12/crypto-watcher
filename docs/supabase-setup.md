# Supabase project setup

The owner's Supabase project holds accounts and published results. Cloudflare
Workers serves the web app; the owner's machine runs the collector, operational
Postgres, FastAPI, and forward jobs, as decided in #273. Lovable removal remains
separate work in #276.

## Project and public configuration

- Project reference: `xshzjhocpcenuumdrhdh`.
- Project URL: `https://xshzjhocpcenuumdrhdh.supabase.co`.
- Region: Mumbai (`ap-south-1`).
- Intended plan: Free, with standard Postgres. The Free project pauses after
  seven days without activity. The owner must confirm the organization is on
  Free with no payment method.

`supabase/config.toml` selects this project. Production Supabase URLs, publishable
keys, and project metadata belong only in `.env.production`. `.env` retains the
development defaults from `origin/main`; local credentials are generated in the
gitignored `.env.local`. Keep hosted values out of files loaded by development.
Service-role keys and passwords do not belong in committed environment files.
Keep `REPLACE_` placeholders for values awaiting owner setup; the production
preflight rejects them.

The private server file is `.env.production.local` at the repository root.
It is gitignored, untracked, mode `0600`, and holds all 12 required names listed in
[Worker host setup](worker-host.md#setup), including `SUPABASE_SERVICE_ROLE_KEY`,
the operational database credentials, worker flags, and Python endpoints/token.
Never commit its contents or give a secret a `VITE_` prefix. Its permissions were
verified as `0600` without opening it.

For local development and runtime variable requirements, see
[Environment setup](environments.md).

## Migration status

On October 11, 2026, `npx supabase link` linked this repository to the project.
`npx supabase db push --dry-run` listed all 21 local migration filenames in
exactly their filename order, with none of those versions already applied.
One `npx supabase db push` invocation then applied all 21 successfully, from
version `20260913170352` through `20261011150000`. The PR records the full list.

The September 14 migration was read in full and contains only extension creation
for `pg_cron` and `pg_net`; it has no Lovable scheduler calls or cron-auth values.
Both `20261011130000_forward_sigma_state.sql` and
`20261011150000_forward_shared_stages.sql` were read in full and left unchanged.
They are not idempotent: do not replay them manually. No existing migration was
edited, and no security repair migration has been applied.

## Security verification: pending

Live security verification has not been run. The owner will run
[p34_security_checks.sql](../supabase/verify/p34_security_checks.sql) in this
project's Supabase SQL Editor and return the results. Select and run each query
separately so that all four result sets are available.

| Check | Required result |
| --- | --- |
| a: RLS | Zero public tables without RLS. |
| b: Effective write access | Zero rows where `protected_results_table` is true. Other returned rows need justification in the PR or a fix. |
| c: Function execution | Every `commit*` function has `anon_exec = false`, `auth_exec = false`, and `service_role_exec = true`. All public SECURITY DEFINER functions are also listed for review. |
| d: Extensions | Two rows: `pg_cron` and `pg_net`. |

Check b considers public tables and the `anon` and `authenticated` roles for
`INSERT`, `UPDATE`, and `DELETE`. It returns a row only when
`has_table_privilege(role, table, command)` is true and either RLS is off or a
policy applies to that role and command. Policy matching includes policies for
all commands, PUBLIC, and roles whose privileges are inherited without `SET ROLE`.
It checks policy applicability, not the policy's row expressions.

Return the four row counts, any rows from a or b, the function privilege flags
from c, and the extension names from d. Function and extension row counts alone
cannot establish that those checks pass. Check c includes SECURITY INVOKER commit
functions as well as SECURITY DEFINER functions, and resolves effective privileges
including Postgres's default EXECUTE grant to PUBLIC.

If verification finds a failure, add a new migration with a fresh timestamp;
never edit an applied migration. Review its dry run before a single apply. Keep
#275 open until live security checks and its remaining acceptance items pass.

## Owner setup still pending

1. Create the Google OAuth client with this authorized redirect URI:
   `https://xshzjhocpcenuumdrhdh.supabase.co/auth/v1/callback`.
   Enable the Google provider in Supabase and enter the client credentials there.
2. Set the Auth Site URL and redirect allow-list after #277 establishes the web
   app's final URL.
3. Set up a weekly `npx supabase db dump` in #279. Keep dumps outside this public
   repository.
4. Confirm the organization is on Free with no payment method, and complete the
   SQL Editor verification above.
