import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");
const source = await read("../src/lib/runner/status-core.ts");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { resolveRunnerStatus, unavailableRunnerStatus, RUNNER_HEALTH_FRESH_MS } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

const now = Date.parse("2026-10-13T12:00:00.000Z");
const timestamp = (ageMs = 0) => new Date(now - ageMs).toISOString();
const enabled = { gatewayEnabled: true, jobsEnabled: true, gatewayReachable: true };
const children = {
  "fastapi-analysis": true,
  "fastapi-forward": true,
  collector: true,
  "forward-hourly": true,
  "forward-trend": true,
  "runner-gateway": true,
  "runner-jobs": true,
};
function host(patch = {}) {
  return {
    service: "worker-host", status: "online", services: { ...children },
    updated_at: timestamp(), last_seen_at: timestamp(), ...patch,
  };
}
function jobs(patch = {}) {
  return {
    service: "runner-jobs", status: "online", services: {},
    updated_at: timestamp(), last_seen_at: timestamp(), ...patch,
  };
}
const resolve = (records, config = enabled, time = now) => resolveRunnerStatus(records, time, config);

test("fresh healthy workers enable services and retain the persisted machine timestamp", () => {
  const result = resolve([host(), jobs({ last_seen_at: timestamp(1_000) })]);
  assert.equal(result.configured, true);
  assert.equal(result.jobsEnabled, true);
  assert.equal(result.state, "online");
  assert.equal(result.online, true);
  assert.equal(result.lastOnlineAt, now);
  assert.deepEqual(result.services, { analysis: true, alerts: true, forward: true, jobs: true });
});

test("an alive jobs child does not enable queued work without a fresh healthy jobs heartbeat", () => {
  for (const row of [
    undefined,
    jobs({ updated_at: timestamp(RUNNER_HEALTH_FRESH_MS + 1) }),
    jobs({ status: "degraded" }),
    jobs({ status: "offline" }),
  ]) {
    const result = resolve([host(), ...(row ? [row] : [])]);
    assert.equal(result.state, "degraded");
    assert.equal(result.online, true);
    assert.equal(result.services.jobs, false);
    assert.equal(result.services.analysis, true);
    assert.equal(result.services.forward, true);
  }
});

test("a jobs heartbeat cannot override the supervisor's failed jobs process", () => {
  const result = resolve([host({ services: { ...children, "runner-jobs": false } }), jobs()]);
  assert.equal(result.state, "degraded");
  assert.equal(result.online, true);
  assert.equal(result.services.jobs, false);
});

test("Python failures degrade only the capabilities that require that service", () => {
  const analysisDown = resolve([
    host({ status: "degraded", services: { ...children, "fastapi-analysis": false } }), jobs(),
  ]);
  assert.equal(analysisDown.online, true);
  assert.equal(analysisDown.state, "degraded");
  assert.deepEqual(analysisDown.services, { analysis: false, alerts: false, forward: false, jobs: true });

  const forwardDown = resolve([
    host({ status: "degraded", services: { ...children, "fastapi-forward": false } }), jobs(),
  ]);
  assert.equal(forwardDown.state, "degraded");
  assert.deepEqual(forwardDown.services, { analysis: true, alerts: true, forward: false, jobs: true });
});

test("a collector failure disables work that requires live operational data", () => {
  const result = resolve([host({ services: { ...children, collector: false } }), jobs()]);
  assert.equal(result.online, true);
  assert.equal(result.state, "degraded");
  assert.deepEqual(result.services, { analysis: false, alerts: false, forward: false, jobs: true });
});

test("a tunnel outage does not turn a live machine offline or disable background forward jobs", () => {
  const result = resolve([host(), jobs()], { ...enabled, gatewayReachable: false });
  assert.equal(result.gatewayReachable, false);
  assert.equal(result.online, true);
  assert.equal(result.state, "degraded");
  assert.deepEqual(result.services, { analysis: false, alerts: false, forward: true, jobs: true });
});

test("missing health and lookup failures remain unknown even when the gateway is reachable", () => {
  const missing = resolve([]);
  assert.equal(missing.state, "unknown");
  assert.equal(missing.online, null);
  assert.equal(missing.gatewayReachable, true);
  assert.equal(missing.lastOnlineAt, null);
  assert.deepEqual(unavailableRunnerStatus(true), {
    configured: true, jobsEnabled: false, state: "unknown", online: null,
    lastOnlineAt: null, gatewayReachable: null,
    services: { analysis: false, alerts: false, forward: false, jobs: false },
  });
});

test("expired heartbeats are offline and keep their last-seen timestamp across new lookups", () => {
  const age = RUNNER_HEALTH_FRESH_MS + 1;
  const records = [
    host({ updated_at: timestamp(age), last_seen_at: timestamp(age) }),
    jobs({ updated_at: timestamp(age + 5_000), last_seen_at: timestamp(age + 5_000) }),
  ];
  const config = { ...enabled, gatewayReachable: false };
  const result = resolve(records, config);
  assert.equal(result.state, "offline");
  assert.equal(result.online, false);
  assert.equal(result.lastOnlineAt, now - age);
  assert.deepEqual(result.services, { analysis: false, alerts: false, forward: false, jobs: false });
  assert.equal(resolve(structuredClone(records), config, now + 3_600_000).lastOnlineAt, result.lastOnlineAt);
});

test("a reachable gateway with expired workers is degraded and cannot enable worker controls", () => {
  const age = RUNNER_HEALTH_FRESH_MS + 1;
  const result = resolve([
    host({ updated_at: timestamp(age), last_seen_at: timestamp(age) }),
    jobs({ updated_at: timestamp(age), last_seen_at: timestamp(age) }),
  ]);
  assert.equal(result.gatewayReachable, true);
  assert.equal(result.state, "degraded");
  assert.equal(result.online, true);
  assert.equal(result.lastOnlineAt, now - age);
  assert.deepEqual(result.services, { analysis: false, alerts: false, forward: false, jobs: false });
});

test("jobs-only machines become offline when workers expire and no gateway is configured", () => {
  const age = RUNNER_HEALTH_FRESH_MS + 1;
  const result = resolve([
    host({ updated_at: timestamp(age) }), jobs({ updated_at: timestamp(age) }),
  ], { gatewayEnabled: false, jobsEnabled: true, gatewayReachable: null });
  assert.equal(result.state, "offline");
  assert.equal(result.online, false);
});

test("explicit stopped reports become offline without discarding the previous last-seen value", () => {
  const previous = timestamp(5_000);
  const result = resolve([
    host({ status: "offline", last_seen_at: previous }),
    jobs({ status: "offline", last_seen_at: timestamp(10_000) }),
  ], { ...enabled, gatewayReachable: false });
  assert.equal(result.state, "offline");
  assert.equal(result.online, false);
  assert.equal(result.lastOnlineAt, Date.parse(previous));
});

test("an independent jobs heartbeat proves machine liveness when the supervisor is stale", () => {
  const result = resolve([host({ updated_at: timestamp(RUNNER_HEALTH_FRESH_MS + 1) }), jobs()]);
  assert.equal(result.online, true);
  assert.equal(result.state, "degraded");
  assert.equal(result.lastOnlineAt, now);
  assert.deepEqual(result.services, { analysis: false, alerts: false, forward: false, jobs: false });
});

test("a newer stopped supervisor report supersedes an older jobs heartbeat", () => {
  const result = resolve([
    host({ status: "offline", last_seen_at: timestamp(10_000) }),
    jobs({ updated_at: timestamp(5_000), last_seen_at: timestamp(5_000) }),
  ], { ...enabled, gatewayReachable: false });
  assert.equal(result.state, "offline");
  assert.equal(result.online, false);
  assert.equal(result.lastOnlineAt, now - 5_000);
});

test("jobs observed after a stopped supervisor still establish machine liveness", () => {
  const result = resolve([
    host({ status: "offline", updated_at: timestamp(5_000), last_seen_at: timestamp(10_000) }),
    jobs(),
  ]);
  assert.equal(result.online, true);
  assert.equal(result.state, "degraded");
  assert.equal(result.lastOnlineAt, now);
  assert.deepEqual(result.services, { analysis: false, alerts: false, forward: false, jobs: false });
});

test("heartbeat expiry uses the freshness boundary", () => {
  const gatewayOnly = { gatewayEnabled: true, jobsEnabled: false, gatewayReachable: false };
  assert.equal(resolve([host({ updated_at: timestamp(RUNNER_HEALTH_FRESH_MS) })], gatewayOnly).online, true);
  assert.equal(resolve([host({ updated_at: timestamp(RUNNER_HEALTH_FRESH_MS + 1) })], gatewayOnly).online, false);
});

test("invalid and implausibly future timestamps cannot establish an offline machine", () => {
  const gatewayOnly = { gatewayEnabled: true, jobsEnabled: false, gatewayReachable: true };
  for (const record of [
    host({ updated_at: timestamp(-30_001) }),
    host({ updated_at: "not-a-timestamp" }),
    host({ status: "unexpected" }),
  ]) {
    const result = resolve([record], gatewayOnly);
    assert.equal(result.state, "unknown");
    assert.equal(result.online, null);
  }
});

test("jobs-only configuration works without gateway configuration or tunnel reachability", () => {
  const services = { ...children };
  delete services["runner-gateway"];
  const result = resolve([host({ services }), jobs()], {
    gatewayEnabled: false, jobsEnabled: true, gatewayReachable: null,
  });
  assert.equal(result.configured, true);
  assert.equal(result.state, "online");
  assert.equal(result.online, true);
  assert.equal(result.gatewayReachable, null);
  assert.equal(result.services.forward, true);
  assert.equal(result.services.jobs, true);
});

test("gateway-only configuration does not require a disabled jobs runner", () => {
  const services = { ...children };
  delete services["runner-jobs"];
  const result = resolve([host({ services })], {
    gatewayEnabled: true, jobsEnabled: false, gatewayReachable: true,
  });
  assert.equal(result.state, "online");
  assert.equal(result.services.analysis, true);
  assert.equal(result.services.jobs, false);
  assert.equal(result.jobsEnabled, false);
});

test("disabled runners do not reuse old health records", () => {
  const result = resolve([host(), jobs()], {
    gatewayEnabled: false, jobsEnabled: false, gatewayReachable: true,
  });
  assert.deepEqual(result, unavailableRunnerStatus(false));
  assert.equal(result.state, "disabled");
  assert.equal(result.online, null);
});

test("runner health is read-only for authenticated users and only service role can report", async () => {
  const sql = await read("../supabase/migrations/20261013130000_runner_health.sql");
  assert.match(sql, /ALTER TABLE public\.runner_health ENABLE ROW LEVEL SECURITY/i);
  assert.match(sql, /REVOKE ALL ON public\.runner_health FROM PUBLIC, anon, authenticated/i);
  assert.match(sql, /GRANT SELECT ON public\.runner_health TO authenticated/i);
  assert.match(sql, /CREATE POLICY runner_health_select[\s\S]+FOR SELECT TO authenticated\s+USING \(true\)/i);
  assert.doesNotMatch(sql, /GRANT (?:ALL|INSERT|UPDATE|DELETE)[^;]+TO authenticated/i);
  assert.doesNotMatch(sql, /CREATE POLICY[^;]+FOR (INSERT|UPDATE|DELETE|ALL)/i);
  assert.match(sql, /SECURITY DEFINER SET search_path = public/i);
  assert.match(sql, /REVOKE ALL ON FUNCTION public\.report_runner_health\(TEXT, TEXT, JSONB\)\s+FROM PUBLIC, anon, authenticated/i);
  assert.match(sql, /GRANT EXECUTE ON FUNCTION public\.report_runner_health\(TEXT, TEXT, JSONB\) TO service_role/i);
});

test("health reporting uses the database clock and preserves last seen after an offline report", async () => {
  const sql = await read("../supabase/migrations/20261013130000_runner_health.sql");
  assert.match(sql, /observed_at TIMESTAMPTZ := clock_timestamp\(\)/i);
  assert.match(sql, /CREATE FUNCTION public\.report_runner_health\(\s*p_service TEXT, p_status TEXT, p_services JSONB/i);
  assert.doesNotMatch(sql, /p_(?:updated_at|last_seen_at|observed_at)/i);
  assert.match(sql, /CASE WHEN p_status <> 'offline' THEN observed_at ELSE NULL END/i);
  assert.match(sql, /last_seen_at = CASE WHEN EXCLUDED\.status <> 'offline'\s+THEN EXCLUDED\.last_seen_at ELSE runner_health\.last_seen_at END/i);
});

test("the badge uses persisted last-seen data for relative time and its absolute tooltip", async () => {
  const hook = await read("../src/lib/runner/use-runner-status.ts");
  const badge = await read("../src/components/runner-status-notice.tsx");
  assert.doesNotMatch(hook, /Date\.now\(|getQueryData|lastOnlineAt:/);
  assert.match(badge, /new Date\(data\.lastOnlineAt\)\.toLocaleString\(\)/);
  assert.match(badge, /relativeTime\(data\.lastOnlineAt\)/);
});
