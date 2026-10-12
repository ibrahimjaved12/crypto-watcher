import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const source = await readFile(new URL("../src/lib/runner/gateway-core.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { createRateLimiter, secretMatches, validateGatewayEnv } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

test("sliding windows isolate users and give rounded Retry-After without extending on rejection", () => {
  let now = 0;
  const limit = createRateLimiter({ windowMs: 60_000, max: 2 }, () => now);
  assert.deepEqual(limit("a"), { ok: true, retryAfterSeconds: 0 });
  now = 1000;
  assert.equal(limit("a").ok, true);
  now = 1501;
  assert.deepEqual(limit("a"), { ok: false, retryAfterSeconds: 59 });
  assert.equal(limit("b").ok, true);
  now = 59_999;
  assert.deepEqual(limit("a"), { ok: false, retryAfterSeconds: 1 });
  now = 60_000;
  assert.equal(limit("a").ok, true);
  assert.equal(limit("a").ok, false);
  now = 61_000;
  assert.equal(limit("a").ok, true);
  now = 200_000;
  assert.equal(limit("a").ok, true);
});

test("secret comparison handles equal values, differences and length mismatch", () => {
  assert.equal(secretMatches("abcd", "abcd"), true);
  assert.equal(secretMatches("abce", "abcd"), false);
  assert.equal(secretMatches("abcdx", "abcd"), false);
  assert.equal(secretMatches("", "abcd"), false);
});

test("environment validation uses defaults and reports names without values", () => {
  const good = { RUNNER_GATEWAY_SECRET: "x".repeat(32), SUPABASE_URL: "https://example.invalid",
    SUPABASE_PUBLISHABLE_KEY: "test-publishable", SUPABASE_SERVICE_ROLE_KEY: "test-service" };
  assert.deepEqual(validateGatewayEnv(good), []);
  for (const port of ["1024", "8787", "65535"])
    assert.deepEqual(validateGatewayEnv({ ...good, RUNNER_GATEWAY_PORT: port }), []);
  for (const port of ["", "1023", "65536", "8787.5", "1e4", "private-value"])
    assert.deepEqual(validateGatewayEnv({ ...good, RUNNER_GATEWAY_PORT: port }),
      ["RUNNER_GATEWAY_PORT must be an integer from 1024 to 65535."]);
  for (const secret of ["sensitive value!", "x".repeat(31), "x".repeat(257)])
    assert.deepEqual(validateGatewayEnv({ ...good, RUNNER_GATEWAY_SECRET: secret }),
      ["RUNNER_GATEWAY_SECRET must be 32–256 URL-safe characters."]);
  assert.deepEqual(validateGatewayEnv({}), [
    "RUNNER_GATEWAY_SECRET must be 32–256 URL-safe characters.", "SUPABASE_URL is required.",
    "SUPABASE_PUBLISHABLE_KEY is required.", "SUPABASE_SERVICE_ROLE_KEY is required.",
  ]);
});

test("gateway stays loopback-only and host launch is opt-in", async () => {
  const gateway = await readFile(new URL("../src/worker/runner-gateway.ts", import.meta.url), "utf8");
  const launcher = await readFile(new URL("../scripts/runner-gateway.mjs", import.meta.url), "utf8");
  const host = await readFile(new URL("../scripts/worker-host.mjs", import.meta.url), "utf8");
  assert.match(gateway, /server\.listen\(port, "127\.0\.0\.1"/);
  assert.ok(!gateway.includes("Access-Control"));
  assert.match(gateway, /error: result\.error \? result\.error\.slice\(0, 1000\) : result\.error/);
  assert.doesNotMatch(launcher, /safeLog|console\[[^\]]+\]\s*=/);
  assert.match(host, /env\.RUNNER_GATEWAY_ENABLED === "true"/);
  assert.match(host, /nodeJob\("runner-gateway", "scripts\/runner-gateway\.mjs"\)/);
});
