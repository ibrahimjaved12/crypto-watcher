# Forward worker outage runbook

The public Worker serves hosted results. It has `MACHINE_ONLY_ENABLED=false` and
cannot run `runForwardNow` or `runForwardTrendNow`; do recovery on the owner's
machine. The Strategy Lab and Market dashboard warn when the latest hosted
`paper_runs.boundary_ms` is more than two hours behind. This is the processed
boundary, not a fresh insertion timestamp on a retry. Missing runs and failed
reads have separate messages. A banner indicates stale data, not proof of a host
outage: an input gap, failed Python request or database outage can also stop progress.

## Identify an outage

1. On the host, inspect the supervisor logs and `.worker-host.status.json`.
   The status file is written every 15 seconds. An update older than two minutes,
   `crash-loop`, or a stopped child needs investigation. A missing file can mean a
   clean stop; it does not prove the host is healthy.
2. Run `npm run mvp:check -- --mode production` locally. Check the `worker host`,
   collector health, recent candle gaps, latest run, Python and ledger lines.
   This command reads data and health endpoints; it does not repair anything.
3. Check power/sleep, connectivity, disk capacity, clock synchronization, the local
   operational Supabase stack and both Python services. Preserve logs before a
   restart. Never paste environment files or credentials into an issue.

## Restart the host

Use the existing supervisor if installed (`systemctl --user restart worker-host`
or `pm2 restart worker-host`, using your configured service name). Inspect its
logs with `journalctl --user -u worker-host -f` or `pm2 logs worker-host`.

For a foreground host, stop the old instance with Ctrl+C and wait for its children
to exit, then from the repository root run:

```sh
npm run worker:host
```

Keep a single supervisor instance. It refuses occupied Python ports and owns its
children's shutdown/restart lifecycle. Do not delete PID locks or cache files to
force a second copy to start. `worker:host` sets `MACHINE_ONLY_ENABLED=true` for
its children; its process supervision and sigma-cache behavior are unchanged.
Existing setup is in [worker-host.md](worker-host.md): the gitignored
`.env.production.local` must target hosted main Supabase and local operational
Supabase, with both Python endpoints. Fix preflight errors before retrying.

## Catch-up

The hourly forward runner resumes from committed hosted market stages and each
account's cursor. Each invocation processes at most **7 chunks of 24 hours**
(the last chunk may be shorter), reloading the committed stage between chunks.
Wallet replay follows committed market stages in order; don't reset cursors or
rewrite ledger rows. Subsequent scheduled runs resume remaining work.

Look for `[forward-catchup] chunks=… remaining_hours=…`. Remaining hours should
decrease and eventually reach zero. Candle gaps can produce `skipped_stale`
instead of progress; resolve collector/input problems before expecting replay.
An outage beyond the available 120-day history can emit `[forward-range] clamped`;
that is not evidence of complete historical recovery. Investigate the missing range.
Sigma state uses the existing local cache and hosted restore/checkpoint behavior;
do not delete it as a routine restart step.

## Stop and investigate

Stop the supervisor through its service manager or Ctrl+C if repeated restarts
produce crash loops, checksum conflicts, non-decreasing catch-up lag, ledger
invariant failures, unexpected history clamping, or sustained large hosted
traffic. Inspect the failing stage and inputs before restarting. Investigate a
database-size warning at 350 MB; at 450 MB the size check fails, and ingestion
should stay stopped until the owner resolves capacity. Do not delete permanent
forward or paper records as an automatic remedy.

## Confirm recovery

The hourly runner emits `[forward-hosted] reads=… read_bytes=… writes=… write_bytes=…`
after an attempt. These count attempted hosted requests, including failed attempts;
one line or a nonzero write count alone does not prove recovery. Correlate successive
lines with successful run summaries, decreasing catch-up lag, fresh hosted run
boundaries and a passing ledger check. When caught up, traffic should settle to
the normal hourly workload and `remaining_hours=0`; the stale banner clears after
the next hosted refresh (roughly a minute). Both Python services and all supervisor
children should be healthy. Check the daily trend job separately: its schedule is daily.

## Read-only hosted size check

`mvp:check` adds `hosted database size`. It uses `psql` and an **existing local
libpq service** selected by `PGSERVICE`, which must point to the same hosted main
database as the production profile. Connection settings/credentials remain in the
owner's local service/password configuration. The Supabase HTTP service-role key
does not provide an arbitrary SQL connection. No new database RPC or table is created.
If `PGSERVICE` or `psql` is unavailable, the line says `SKIP` with a reason at WARN
level. Connection/query failures also skip without printing sensitive details.

The query runs in a read-only transaction with connection and statement timeouts:

```sql
SELECT pg_database_size(current_database())::bigint;
```

It measures the whole hosted database, including Supabase system schemas, matching
the scope of Supabase's 500 MB database size limit. Thresholds use decimal MB
(1 MB = 1,000,000 bytes): WARN at 350 MB and FAIL at 450 MB.
