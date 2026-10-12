import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
async function moduleUrl(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, target] of Object.entries(imports))
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(target));
  return stub(outputText);
}
const gateUrl = await moduleUrl("../src/lib/machine-only.server.ts");
const { machineOnlyEnabled } = await import(gateUrl);
const functionsUrl = await moduleUrl("../src/lib/forward.functions.ts", {
  "@tanstack/react-start": stub(`
    export const createServerFn = () => {
      let middleware = [];
      let validate;
      const chain = {
        middleware(value) {
          middleware = value;
          return chain;
        },
        validator(value) {
          validate = value;
          return chain;
        },
        handler(fn) {
          return async (options = {}) => {
            for (const item of middleware) await item();
            return fn({ context: { userId: 'user', supabase: globalThis.__machineSupabase },
              data: validate ? validate(options.data) : undefined });
          };
        },
      };
      return chain;
    };
  `),
  "@/integrations/supabase/auth-middleware": stub(`export const requireSupabaseAuth=()=>{
    globalThis.__machineAuthCalls++;if(!globalThis.__machineAuthenticated)throw new Error('Unauthorized');
  }`),
  zod: import.meta.resolve("zod"),
  "./machine-only.server": gateUrl,
  "./forward/forward-dashboard": stub(`export const exportForwardLog=()=>{},loadForwardDashboard=()=>{},loadForwardLogPage=()=>{},loadForwardReport=()=>{};`),
  "./forward/forward-filters": stub(`export const forwardFiltersSchema={};`),
  "./forward/forward-trend-dashboard": stub(`export const loadTrendDashboard=()=>{};`),
  "./forward/forward-deps.server": stub(`globalThis.__machineImports++;
    export const FORWARD_STRATEGY_IDS=[];
    export const forwardDeps=async()=>{globalThis.__machineCalls++;return {}};
    export const forwardTrendDeps=forwardDeps;`),
  "./forward/forward-run.server": stub(`export const runForward=async()=>({status:'ok'});`),
  "./forward/forward-trend-run.server": stub(`export const runForwardTrend=async()=>({status:'ok'});`),
});
const { runForwardNow, runForwardTrendNow, getMachineCapabilities, getRunnerJob } = await import(functionsUrl);
const runInput = { data: { idempotencyKey: "11111111-1111-4111-8111-111111111111" } };

test("forward routes fail closed before machine imports and retain user authentication", async () => {
  const previous = process.env.MACHINE_ONLY_ENABLED;
  const previousJobs = process.env.RUNNER_JOBS_ENABLED;
  globalThis.__machineImports = 0;
  globalThis.__machineCalls = 0;
  globalThis.__machineAuthCalls = 0;
  globalThis.__machineAuthenticated = true;
  try {
    process.env.RUNNER_JOBS_ENABLED = "false";
    for (const flag of [undefined, "false", "TRUE", "1"]) {
      if (flag === undefined) delete process.env.MACHINE_ONLY_ENABLED;
      else process.env.MACHINE_ONLY_ENABLED = flag;
      assert.equal(machineOnlyEnabled(), false);
      assert.deepEqual(await getMachineCapabilities(), { machineOnlyEnabled: false, runnerJobsEnabled: false });
      for (const route of [runForwardNow, runForwardTrendNow]) {
        let response;
        try { await route(runInput); } catch (error) { response = error; }
        assert.ok(response instanceof Response);
        assert.equal(response.status, 403);
        assert.deepEqual(await response.json(), { error: "machine_only" });
      }
    }
    assert.equal(globalThis.__machineImports, 0);
    assert.equal(globalThis.__machineCalls, 0);
    assert.equal(globalThis.__machineAuthCalls, 12);
    process.env.MACHINE_ONLY_ENABLED = "true";
    globalThis.__machineAuthenticated = false;
    for (const route of [runForwardNow, runForwardTrendNow, getMachineCapabilities])
      await assert.rejects(route(runInput), /Unauthorized/);
    assert.equal(globalThis.__machineImports, 0);
    globalThis.__machineAuthenticated = true;
    process.env.RUNNER_JOBS_ENABLED = "true";
    assert.deepEqual(await getMachineCapabilities(), { machineOnlyEnabled: true, runnerJobsEnabled: true });
    for (const route of [runForwardNow, runForwardTrendNow])
      assert.deepEqual(await route(runInput), { status: "ok" });
    assert.equal(globalThis.__machineCalls, 2);
  } finally {
    if (previous === undefined) delete process.env.MACHINE_ONLY_ENABLED;
    else process.env.MACHINE_ONLY_ENABLED = previous;
    if (previousJobs === undefined) delete process.env.RUNNER_JOBS_ENABLED;
    else process.env.RUNNER_JOBS_ENABLED = previousJobs;
    for (const name of ["__machineImports", "__machineCalls", "__machineAuthCalls", "__machineAuthenticated"])
      delete globalThis[name];
  }
});

test("hosted jobs use the user client, reuse the retry key and join an active job", async () => {
  const saved = { machine: process.env.MACHINE_ONLY_ENABLED, jobs: process.env.RUNNER_JOBS_ENABLED };
  const unique = { data: null, error: { code: "23505" } };
  const calls = [];
  let responses = [];
  globalThis.__machineAuthenticated = true;
  globalThis.__machineAuthCalls = 0;
  const rpcCalls = [];
  globalThis.__machineSupabase = {
    rpc(name) { rpcCalls.push(name); return Promise.resolve({ data: 0, error: null }); },
    from(table) {
      const call = { table, filters: [] };
      calls.push(call);
      const query = {
        insert(row) { call.insert = row; return query; },
        select(columns) { call.select = columns; return query; },
        eq(name, value) { call.filters.push([name, value]); return query; },
        in(name, values) { call.filters.push([name, values]); return query; },
        single() { assert.ok(responses.length); return Promise.resolve(responses.shift()); },
        maybeSingle() { return query.single(); },
      };
      return query;
    },
  };
  try {
    process.env.MACHINE_ONLY_ENABLED = "false";
    process.env.RUNNER_JOBS_ENABLED = "true";
    assert.deepEqual(await getMachineCapabilities(), { machineOnlyEnabled: false, runnerJobsEnabled: true });
    for (const [route, kind] of [[runForwardNow, "forward_signals"], [runForwardTrendNow, "forward_trend"]]) {
      responses = [{ data: { id: "new-job", status: "queued" }, error: null }];
      assert.deepEqual(await route(runInput), { jobId: "new-job", status: "queued", alreadyActive: false });
      assert.deepEqual(calls.at(-1).insert, { user_id: "user", kind, idempotency_key: runInput.data.idempotencyKey });
      assert.equal(calls.at(-1).table, "runner_jobs");
      assert.equal(rpcCalls.at(-1), "expire_my_stale_runner_jobs");

      responses = [unique, { data: { id: "original-job", status: "succeeded" }, error: null }];
      assert.deepEqual(await route(runInput), { jobId: "original-job", status: "succeeded", alreadyActive: false });
      assert.deepEqual(calls.at(-1).filters, [["user_id", "user"], ["kind", kind], ["idempotency_key", runInput.data.idempotencyKey]]);

      responses = [unique, { data: null, error: null }, { data: { id: "active-job", status: "running" }, error: null }];
      assert.deepEqual(await route(runInput), { jobId: "active-job", status: "running", alreadyActive: true });
      assert.deepEqual(calls.at(-1).filters, [["user_id", "user"], ["kind", kind], ["status", ["queued", "running"]]]);
      await assert.rejects(route({ data: { idempotencyKey: "invalid" } }));
      await assert.rejects(route());
      assert.equal(responses.length, 0);
    }

    responses = [{ data: { id: "job", kind: "forward_signals", status: "queued", created_at: "created",
      started_at: null, finished_at: null, result: null, error: null }, error: null }];
    assert.deepEqual(await getRunnerJob({ data: { jobId: runInput.data.idempotencyKey } }), {
      id: "job", kind: "forward_signals", status: "queued", createdAt: "created",
      startedAt: null, finishedAt: null, result: null, error: null,
    });
    assert.equal(calls.at(-1).table, "runner_jobs");
    assert.deepEqual(calls.at(-1).filters, [["id", runInput.data.idempotencyKey]]);
    responses = [{ data: null, error: { message: "private database details" } }];
    await assert.rejects(getRunnerJob({ data: { jobId: runInput.data.idempotencyKey } }), /Runner job could not be found/);
    globalThis.__machineAuthenticated = false;
    await assert.rejects(runForwardNow(runInput), /Unauthorized/);
    await assert.rejects(getRunnerJob({ data: { jobId: runInput.data.idempotencyKey } }), /Unauthorized/);
  } finally {
    for (const [name, value] of [["MACHINE_ONLY_ENABLED", saved.machine], ["RUNNER_JOBS_ENABLED", saved.jobs]]) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    for (const name of ["__machineSupabase", "__machineAuthenticated", "__machineAuthCalls"]) delete globalThis[name];
  }
});

const { forwardDataStale, FORWARD_STALE_AFTER_MS } = await import(
  await moduleUrl("../src/lib/forward/forward-freshness.ts"));
test("hosted data becomes stale strictly after two hours, with missing data handled separately", () => {
  const now = 10 * FORWARD_STALE_AFTER_MS;
  assert.equal(forwardDataStale(now - FORWARD_STALE_AFTER_MS, now), false);
  assert.equal(forwardDataStale(now - FORWARD_STALE_AFTER_MS - 1, now), true);
  assert.equal(forwardDataStale(now + 1, now), false);
  for (const value of [null, undefined, NaN]) assert.equal(forwardDataStale(value, now), false);
});

const rulesUrl = await moduleUrl("../src/lib/forward/mvp-check.ts");
const { classifyHostedDatabaseSize } = await import(rulesUrl);
test("whole hosted database size warns at 350 MB and fails at 450 MB", () => {
  for (const [bytes, level] of [[0, "PASS"], [349999999, "PASS"], [350000000, "WARN"],
    [449999999, "WARN"], [450000000, "FAIL"]])
    assert.equal(classifyHostedDatabaseSize(bytes).level, level);
});

const sizeUrl = await moduleUrl("../src/worker/hosted-db-size.ts", {
  "../lib/forward/mvp-check": rulesUrl,
  "node:child_process": stub(`export const execFile=()=>{throw new Error('must not connect')};`),
});
const { hostedDatabaseSizeCheck } = await import(sizeUrl);
test("size check skips without a locally available service and does not expose connection errors", async () => {
  const saved = { ...process.env };
  try {
    delete process.env.PGSERVICE;
    assert.match((await hostedDatabaseSizeCheck()).detail, /^SKIP: no local PGSERVICE/);
    process.env.PGSERVICE = "test-service";
    process.env.APP_PROFILE = "local";
    assert.match((await hostedDatabaseSizeCheck()).detail, /^SKIP: requires the hosted/);
    process.env.APP_PROFILE = "production";
    process.env.ALLOW_HOSTED_SUPABASE = "true";
    const check = await hostedDatabaseSizeCheck();
    assert.equal(check.level, "WARN");
    assert.match(check.detail, /^SKIP:/);
    assert.doesNotMatch(check.detail, /must not connect|test-service/);
  } finally {
    for (const key of ["PGSERVICE", "APP_PROFILE", "ALLOW_HOSTED_SUPABASE"]) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
  }
});
