import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");

test("runner jobs restrict user access to their own queued inserts and reads", async () => {
  const sql = await read("../supabase/migrations/20261012180000_runner_jobs.sql");
  assert.match(sql, /ALTER TABLE public\.runner_jobs ENABLE ROW LEVEL SECURITY/i);
  assert.match(sql, /FOR SELECT TO authenticated\s+USING \(user_id = auth\.uid\(\)\)/i);
  assert.match(sql, /FOR INSERT TO authenticated\s+WITH CHECK \(\s*user_id = auth\.uid\(\) AND status = 'queued' AND result IS NULL AND worker_id IS NULL\s+AND started_at IS NULL AND finished_at IS NULL/i);
  assert.doesNotMatch(sql, /CREATE POLICY[^;]+FOR (UPDATE|DELETE|ALL)/i);
  assert.match(sql, /REVOKE ALL ON public\.runner_jobs FROM PUBLIC, anon, authenticated/i);
  assert.match(sql, /GRANT SELECT, INSERT ON public\.runner_jobs TO authenticated/i);
  assert.match(sql, /UNIQUE \(user_id, kind, idempotency_key\)/i);
  assert.match(sql, /CREATE UNIQUE INDEX runner_jobs_active_job ON public\.runner_jobs \(user_id, kind\)\s+WHERE status IN \('queued', 'running'\)/i);
});

test("only service role can claim, extend and finish jobs; old jobs are never replayed", async () => {
  const sql = await read("../supabase/migrations/20261012180000_runner_jobs.sql");
  assert.equal(sql.match(/SECURITY DEFINER SET search_path = public/gi)?.length, 3);
  for (const name of ["claim_runner_job", "extend_runner_job_lease", "finish_runner_job"]) {
    assert.match(sql, new RegExp(`REVOKE ALL ON FUNCTION public\\.${name}\\([^;]+FROM PUBLIC, anon, authenticated`, "i"));
    assert.match(sql, new RegExp(`GRANT EXECUTE ON FUNCTION public\\.${name}\\([^;]+TO service_role`, "i"));
  }
  assert.equal(sql.match(/GRANT EXECUTE/gi)?.length, 3);
  assert.match(sql, /status = 'expired', finished_at = now\(\)\s+WHERE status = 'queued' AND created_at < now\(\) - interval '15 minutes'/i);
  assert.match(sql, /status = 'failed', error = 'Runner restarted before finishing', finished_at = now\(\)\s+WHERE status = 'running' AND lease_expires_at < now\(\)/i);
  assert.match(sql, /ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED/i);
  assert.equal(sql.match(/WHERE id = p_id AND worker_id = p_worker_id AND status = 'running'/gi)?.length, 2);
  assert.equal(sql.match(/RETURN FOUND/gi)?.length, 2);
});

test("Worker enqueues and polls with the user client behind the separate flag", async () => {
  const source = await read("../src/lib/forward.functions.ts");
  assert.match(source, /z\.object\(\{ idempotencyKey: z\.string\(\)\.uuid\(\) \}\)/);
  assert.match(source, /export const getRunnerJob = createServerFn\(\{ method: "GET" \}\)\s+\.middleware\(\[requireSupabaseAuth\]\)/);
  assert.match(source, /context\.supabase\.from\("runner_jobs"\)/);
  assert.match(source, /runnerJobsEnabled: process\.env\["RUNNER_JOBS_ENABLED"\] === "true"/);
  assert.equal(source.match(/!machineOnlyEnabled\(\) && process\.env\["RUNNER_JOBS_ENABLED"\] === "true"/g)?.length, 2);
  assert.equal(source.match(/enqueueRunnerJob\(context\.supabase, context\.userId/g)?.length, 2);
  assert.doesNotMatch(source, /supabaseAdmin|callRunner/);
});

test("host starts the non-essential runner jobs child only behind its flag", async () => {
  const host = await read("../scripts/worker-host.mjs");
  const preflight = await read("../src/lib/worker-host.ts");
  const launcher = await read("../scripts/runner-jobs.mjs");
  const config = await read("../wrangler.jsonc");
  assert.match(host, /if \(env\.RUNNER_JOBS_ENABLED === "true"\)\s+specs\.push\(nodeJob\("runner-jobs", "scripts\/runner-jobs\.mjs"\)\)/);
  assert.match(host, /startAfterPython\(records\.get\("runner-jobs"\), urls, pythonNames\)/);
  assert.match(host, /const RUNNER_JOBS_DRAIN_MS = 60_000/);
  assert.match(preflight, /ESSENTIAL_CHILD = "collector"/);
  assert.match(preflight, /env\["RUNNER_JOBS_ENABLED"\] === "true"/);
  assert.match(launcher, /runnerImport\("\/src\/worker\/runner-jobs\.ts"/);
  assert.match(launcher, /alias: \{ "@": resolve\(root, "src"\) \}/);
  assert.match(config, /"RUNNER_JOBS_ENABLED": "false"/);
});

test("runner uses leased sequential execution and safe failure messages without gateway calls", async () => {
  const source = await read("../src/worker/runner-jobs.ts");
  assert.match(source, /workerId = `\$\{hostname\(\)\}-\$\{process\.pid\}`/);
  for (const rpc of ["claim_runner_job", "extend_runner_job_lease", "finish_runner_job"])
    assert.ok(source.includes(`supabaseAdmin.rpc("${rpc}"`));
  for (const value of ["LEASE_SECONDS = 120", "POLL_MS = 2_000", "RENEW_MS = 30_000", "CLAIM_BACKOFF_MS = 10_000", "SHUTDOWN_MS = 60_000"])
    assert.ok(source.includes(value));
  assert.match(source, /runForward\(await forwardDeps\(\), \{\s+userId: job\.user_id, trigger: "on_demand", strategyIds: FORWARD_STRATEGY_IDS/);
  assert.match(source, /runForwardTrend\(await forwardTrendDeps\(\), \{\s+userId: job\.user_id, trigger: "on_demand"/);
  assert.match(source, /await execute\(job\)/);
  assert.match(source, /Object\.values\(process\.env\)/);
  assert.match(source, /failure = safeError\(error\);\s+console\.error\(failure\)/);
  assert.match(source, /\.slice\(0, 500\)/);
  assert.doesNotMatch(source, /\.stack|callRunner|RUNNER_GATEWAY|createServer/);
});

test("Strategy Lab polls to terminal status and explains queued, running and expired work", async () => {
  const source = await read("../src/routes/_authenticated/forward.tsx");
  assert.match(source, /machineOnlyEnabled \|\| runnerJobsEnabled/);
  assert.match(source, /crypto\.randomUUID\(\)/);
  assert.match(source, /new Set\(\["succeeded", "failed", "expired"\]\)/);
  assert.match(source, /refetchInterval: [\s\S]*?\? false : 2_000/);
  assert.match(source, /refetchIntervalInBackground: true/);
  assert.match(source, /"Running…" : "Queued…"/);
  assert.match(source, /Date\.parse\(queuedCreatedAt\) \+ 30_000/);
  assert.match(source, /Waiting for the runner\. It will run when your computer is back online \(up to 15 minutes\)\./);
  assert.match(source, /The runner did not pick this up in time\. Check that it is online and try again\./);
  assert.match(source, /toast\.error\(result\.error \|\|/);
  assert.match(source, /runToast\(toastLabel, result\.result\)/);
});
