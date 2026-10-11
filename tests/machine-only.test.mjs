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
      const chain = {
        middleware(value) {
          middleware = value;
          return chain;
        },
        validator() {
          return chain;
        },
        handler(fn) {
          return async () => {
            for (const item of middleware) await item();
            return fn({ context: { userId: 'user' } });
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
const { runForwardNow, runForwardTrendNow, getMachineCapabilities } = await import(functionsUrl);

test("forward routes fail closed before machine imports and retain user authentication", async () => {
  const previous = process.env.MACHINE_ONLY_ENABLED;
  globalThis.__machineImports = 0;
  globalThis.__machineCalls = 0;
  globalThis.__machineAuthCalls = 0;
  globalThis.__machineAuthenticated = true;
  try {
    for (const flag of [undefined, "false", "TRUE", "1"]) {
      if (flag === undefined) delete process.env.MACHINE_ONLY_ENABLED;
      else process.env.MACHINE_ONLY_ENABLED = flag;
      assert.equal(machineOnlyEnabled(), false);
      assert.deepEqual(await getMachineCapabilities(), { machineOnlyEnabled: false });
      for (const route of [runForwardNow, runForwardTrendNow]) {
        let response;
        try { await route(); } catch (error) { response = error; }
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
      await assert.rejects(route(), /Unauthorized/);
    assert.equal(globalThis.__machineImports, 0);
    globalThis.__machineAuthenticated = true;
    assert.deepEqual(await getMachineCapabilities(), { machineOnlyEnabled: true });
    for (const route of [runForwardNow, runForwardTrendNow])
      assert.deepEqual(await route(), { status: "ok" });
    assert.equal(globalThis.__machineCalls, 2);
  } finally {
    if (previous === undefined) delete process.env.MACHINE_ONLY_ENABLED;
    else process.env.MACHINE_ONLY_ENABLED = previous;
    for (const name of ["__machineImports", "__machineCalls", "__machineAuthCalls", "__machineAuthenticated"])
      delete globalThis[name];
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
test("public-schema size warns at 350 MB and fails at 450 MB", () => {
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
