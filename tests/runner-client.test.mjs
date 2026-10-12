import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const source = await readFile(new URL("../src/lib/runner/client.server.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { runnerConfig, callRunner, classifyRunnerFailure, RunnerConfigurationError } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

const env = {
  RUNNER_GATEWAY_ENABLED: "true",
  RUNNER_GATEWAY_URL: "https://runner.example.test",
  RUNNER_GATEWAY_SECRET: "x".repeat(32),
};
const offline = {
  ok: false,
  category: "runner_offline",
  error: "The runner (your computer) is offline. Start npm run worker:host and try again.",
};
const preResponseTimeout = {
  ok: false,
  category: "timeout",
  error: "The runner did not respond in time. If your computer is on, wait a moment and try again; if it is off, start npm run worker:host.",
};

async function withRunnerFetch(send, action) {
  const originalFetch = globalThis.fetch;
  const originalEnv = Object.fromEntries(Object.keys(env).map((name) => [name, process.env[name]]));
  Object.assign(process.env, env);
  globalThis.fetch = send;
  try {
    return await action();
  } finally {
    globalThis.fetch = originalFetch;
    for (const [name, value] of Object.entries(originalEnv)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
}

test("runner config is opt-in and accepts HTTPS or loopback HTTP origins", () => {
  for (const enabled of [undefined, "false", "TRUE", "1"]) {
    assert.equal(runnerConfig({ RUNNER_GATEWAY_ENABLED: enabled }), null);
  }
  assert.deepEqual(runnerConfig(env), { url: env.RUNNER_GATEWAY_URL, secret: env.RUNNER_GATEWAY_SECRET });
  for (const url of ["https://runner.example.test:8443/", "http://localhost:8787", "http://127.0.0.1:8787", "http://[::1]:8787"]) {
    assert.equal(runnerConfig({ ...env, RUNNER_GATEWAY_URL: url }).url, new URL(url).origin);
  }
});

test("invalid runner config reports a friendly message with the variable and typed category", () => {
  for (const url of [undefined, "", "private-url", "http://runner.example.test", "ftp://localhost", "https://private-user:private-password@runner.example.test", "https://runner.example.test/path", "https://runner.example.test?private-query", "https://runner.example.test#private-fragment"]) {
    assert.throws(() => runnerConfig({ ...env, RUNNER_GATEWAY_URL: url }), (error) => {
      assert.ok(error instanceof RunnerConfigurationError);
      assert.equal(error.category, "configuration");
      assert.equal(error.variable, "RUNNER_GATEWAY_URL");
      assert.equal(error.message, "Runner gateway is misconfigured (RUNNER_GATEWAY_URL).");
      return true;
    });
  }
  for (const secret of [undefined, "", "private secret!", "x".repeat(31), "x".repeat(257)]) {
    assert.throws(() => runnerConfig({ ...env, RUNNER_GATEWAY_SECRET: secret }), (error) => {
      assert.ok(error instanceof RunnerConfigurationError);
      assert.equal(error.category, "configuration");
      assert.equal(error.variable, "RUNNER_GATEWAY_SECRET");
      assert.equal(error.message, "Runner gateway is misconfigured (RUNNER_GATEWAY_SECRET).");
      return true;
    });
  }
});

test("tunnel failures stay offline even when aborted, while pre-response aborts time out", () => {
  assert.deepEqual(classifyRunnerFailure(null, false), offline);
  assert.deepEqual(classifyRunnerFailure(null, true), preResponseTimeout);
  for (const status of [502, 503, 504, 522, 523, 524, 530]) {
    assert.deepEqual(classifyRunnerFailure(status, false), offline);
    assert.deepEqual(classifyRunnerFailure(status, true), offline);
  }
  for (const status of [200, 401, 409, 429, 500]) {
    assert.deepEqual(classifyRunnerFailure(status, true), {
      ok: false, category: "timeout", error: "The runner took too long. Try again.",
    });
  }
  assert.equal(classifyRunnerFailure(401, false).category, "unauthorized");
  assert.equal(classifyRunnerFailure(429, false).category, "rate_limited");
  assert.deepEqual(classifyRunnerFailure(409, false), {
    ok: false, category: "busy", error: "A check is already running.",
  });
});

test("callRunner returns friendly configuration errors without fetching", async () => {
  for (const [variable, value] of [
    ["RUNNER_GATEWAY_ENABLED", "false"],
    ["RUNNER_GATEWAY_URL", "private-url"],
    ["RUNNER_GATEWAY_SECRET", "private secret!"],
  ]) {
    await withRunnerFetch(async () => { assert.fail("Invalid config must not reach fetch"); }, async () => {
      process.env[variable] = value;
      assert.deepEqual(await callRunner("/v1/analysis", "user-token", {}), {
        ok: false, category: "configuration", error: `Runner gateway is misconfigured (${variable}).`,
      });
    });
  }
});

test("callRunner forwards the user token and secret only to a manual-redirect JSON request", async () => {
  await withRunnerFetch(async (url, init) => {
    assert.equal(String(url), `${env.RUNNER_GATEWAY_URL}/v1/analysis`);
    assert.equal(init.method, "POST");
    assert.equal(init.redirect, "manual");
    assert.ok(init.signal instanceof AbortSignal);
    assert.equal(init.headers["x-runner-secret"], env.RUNNER_GATEWAY_SECRET);
    assert.equal(init.headers.Authorization, "Bearer user-token");
    assert.equal(init.headers["Content-Type"], "application/json");
    assert.deepEqual(JSON.parse(init.body), { symbol: "BTCUSDT" });
    return Response.json({ result: "safe" });
  }, async () => {
    assert.deepEqual(await callRunner("/v1/analysis", "user-token", { symbol: "BTCUSDT" }), {
      ok: true, data: { result: "safe" },
    });
  });
});

test("callRunner maps HTTP 530 and network errors to the offline message", async () => {
  for (const send of [
    async () => new Response("private-tunnel-html", { status: 530 }),
    async () => { throw new TypeError("private-network-detail"); },
  ]) {
    await withRunnerFetch(send, async () => {
      assert.deepEqual(await callRunner("/v1/monitor/check", "user-token", {}), offline);
    });
  }
});

test("callRunner maps a pre-response AbortError to the no-response timeout message", async () => {
  await withRunnerFetch(async () => {
    throw Object.assign(new Error("private-abort-detail"), { name: "AbortError" });
  }, async () => {
    assert.deepEqual(await callRunner("/v1/monitor/check", "user-token", {}), preResponseTimeout);
  });
});

test("aborting response parsing after reaching the gateway gives the timeout retry message", async () => {
  await withRunnerFetch(async () => {
    const response = Response.json({});
    response.json = async () => { throw Object.assign(new Error("private-abort-detail"), { name: "AbortError" }); };
    return response;
  }, async () => {
    assert.deepEqual(await callRunner("/v1/analysis", "user-token", {}), {
      ok: false, category: "timeout", error: "The runner took too long. Try again.",
    });
  });
});

test("rate limiting includes Retry-After seconds without forwarding response bodies", async () => {
  await withRunnerFetch(async () => new Response("private-error-body", {
    status: 429, headers: { "Retry-After": "17" },
  }), async () => {
    assert.deepEqual(await callRunner("/v1/monitor/check", "user-token", {}), {
      ok: false, category: "rate_limited", error: "Too many runner requests. Try again in 17 seconds.",
    });
  });
});

test("redirects, non-JSON replies and malformed JSON never expose their bodies", async () => {
  for (const response of [
    new Response("private-redirect-body", { status: 302, headers: { Location: "https://other.example.test" } }),
    new Response("private-html-body", { headers: { "Content-Type": "text/html" } }),
    new Response("private-invalid-json", { headers: { "Content-Type": "application/json" } }),
  ]) {
    await withRunnerFetch(async () => response, async () => {
      const reply = await callRunner("/v1/analysis", "user-token", {});
      assert.equal(reply.ok, false);
      assert.ok(!reply.error.includes("private"));
      assert.ok(["service", "invalid_response"].includes(reply.category));
    });
  }
});

test("authenticated middleware passes the extracted token to server function context", async () => {
  const middleware = await readFile(new URL("../src/integrations/supabase/auth-middleware.ts", import.meta.url), "utf8");
  const next = middleware.slice(middleware.indexOf("return next({"));
  assert.match(next, /context:\s*\{[\s\S]*accessToken:\s*token/);
});

test("analysis and monitor branch on runnerConfig before loading their local implementation", async () => {
  const analysis = await readFile(new URL("../src/lib/analysis.functions.ts", import.meta.url), "utf8");
  const monitor = await readFile(new URL("../src/lib/monitor.functions.ts", import.meta.url), "utf8");
  assert.match(analysis, /config = runnerConfig\(\)/);
  assert.match(analysis, /if \(config !== null\)/);
  assert.match(analysis, /callRunner\("\/v1\/analysis", context\.accessToken/);
  assert.ok(analysis.indexOf("if (config !== null)") < analysis.indexOf('await import("./analysis.server")'));
  assert.match(analysis, /analysis:\s*analysisResponse/);
  assert.match(analysis, /analyzeForUser\(context\.supabase, context\.userId, data\.symbol\)/);
  assert.match(monitor, /if \(runnerConfig\(\) !== null\)/);
  assert.match(monitor, /"\/v1\/monitor\/check",\s*context\.accessToken/);
  assert.ok(monitor.indexOf("if (runnerConfig() !== null)") < monitor.indexOf('await import("@/integrations/supabase/client.server")'));
  assert.match(monitor, /if \(!reply\.ok\) throw new Error\(reply\.error\)/);
  assert.match(monitor, /runManualMonitorCheck\(context\.supabase, supabaseAdmin, context\.userId\)/);
});
