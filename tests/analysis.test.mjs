import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

async function moduleUrl(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext },
  });
  for (const [specifier, target] of Object.entries(imports)) {
    outputText = outputText.replaceAll(`"${specifier}"`, JSON.stringify(target));
  }
  return `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
}
const symbols = await moduleUrl("../src/lib/market/symbols.ts");
const contract = await moduleUrl("../src/lib/analysis.contract.ts", {
  zod: import.meta.resolve("zod"),
  "./market/symbols": symbols,
});
const { analysisInput } = await import(contract);
const { analyzeForUser } = await import(
  await moduleUrl("../src/lib/analysis.server.ts", {
    "./analysis.contract": contract,
  })
);
const env = {
  PYTHON_ANALYSIS_ENABLED: "true",
  PYTHON_ANALYSIS_URL: "https://analysis.example.test",
  PYTHON_ANALYSIS_TOKEN: "test-service-" + "x".repeat(32),
};

function db(rows = {}, failTable = "") {
  const queries = [];
  return {
    queries,
    // Deliberately offers SELECT only; inserts/RPCs would fail this test.
    from(table) {
      const entry = { table, filters: [] };
      queries.push(entry);
      const query = {
        select() {
          return query;
        },
        eq(key, value) {
          entry.filters.push([key, value]);
          return query;
        },
        abortSignal() {
          return query;
        },
        async maybeSingle() {
          return {
            data: rows[table] ?? null,
            error: table === failTable ? { message: "private-db-detail" } : null,
          };
        },
      };
      return query;
    },
  };
}
function result() {
  return {
    schema_version: 1,
    mode: "read_only",
    symbol: "BTCUSDT",
    status: "unavailable",
    source: null,
    as_of_ms: 1704153600000,
    price: null,
    observed_at_ms: null,
    threshold_pct: "2",
    rolling: {},
    attempts: [],
    baseline: {
      status: "unavailable",
      change_pct: null,
      direction: null,
      baseline_price: null,
      baseline_at_ms: null,
      cooldown_evaluated: false,
      cooldown_until_ms: null,
      eligibility_evaluated: false,
      alert_eligible: null,
    },
  };
}

test("browser input accepts only a symbol, never user identity or baseline overrides", () => {
  assert.equal(analysisInput.safeParse({ symbol: "BTCUSDT" }).success, true);
  assert.equal(analysisInput.safeParse({ symbol: "POLUSDT" }).success, true);
  assert.equal(analysisInput.safeParse({ symbol: "MATICUSDT" }).success, false);
  assert.equal(analysisInput.safeParse({ symbol: "BTCUSDT", user_id: "other" }).success, false);
  assert.equal(analysisInput.safeParse({ symbol: "BTCUSDT", baseline: null }).success, false);
  assert.equal(analysisInput.safeParse({ symbol: "INVALID" }).success, false);
});

test("missing session context, disabled config and unwatched pairs never call Python", async () => {
  const store = db();
  const send = () => {
    throw new Error("must not fetch");
  };
  assert.equal((await analyzeForUser(store, "", "BTCUSDT", env, send)).ok, false);
  assert.equal((await analyzeForUser(store, "user", "BTCUSDT", {}, send)).ok, false);
  assert.equal(
    (await analyzeForUser(store, "user", "BTCUSDT", env, send)).error,
    "This pair is not in your watchlist.",
  );
});

test("authorized reads are scoped to the caller and forward only relevant saved state", async () => {
  const store = db({
    watchlist_items: { symbol: "BTCUSDT" },
    monitor_settings: {
      threshold_pct: 3,
      cooldown_minutes: 20,
      monitoring_enabled: true,
    },
    monitor_baselines: {
      baseline_price: 100,
      baseline_at: "2024-01-01T00:00:00Z",
      data_source: "OKX",
      threshold_pct: 3,
      last_observed_at: "2024-01-01T00:05:00Z",
      last_up_alert_at: "2024-01-01T00:04:00Z",
      last_down_alert_at: null,
    },
  });
  const reply = await analyzeForUser(
    store,
    "verified-user",
    "BTCUSDT",
    env,
    async (url, options) => {
      assert.equal(url, "https://analysis.example.test/v1/analysis");
      assert.equal(options.redirect, "error");
      assert.equal(options.headers.Authorization, `Bearer ${env.PYTHON_ANALYSIS_TOKEN}`);
      const body = JSON.parse(options.body);
      assert.equal(body.settings.threshold_pct, "3");
      assert.equal(body.baseline.source, "OKX");
      assert.equal(body.baseline.last_up_alert_ms, Date.parse("2024-01-01T00:04:00Z"));
      assert.equal(body.baseline.last_down_alert_ms, null);
      assert.equal(body.user_id, undefined);
      assert.equal(options.body.includes("verified-user"), false);
      return Response.json(result());
    },
  );
  assert.equal(reply.ok, true);
  for (const query of store.queries)
    assert.deepEqual(query.filters[0], ["user_id", "verified-user"]);
  assert.equal(JSON.stringify(reply).includes(env.PYTHON_ANALYSIS_TOKEN), false);
});

test("missing saved baseline/settings stay absent and use documented defaults without writes", async () => {
  const reply = await analyzeForUser(
    db({ watchlist_items: {} }),
    "user",
    "BTCUSDT",
    env,
    async (_, options) => {
      const body = JSON.parse(options.body);
      assert.equal(body.baseline, null);
      assert.deepEqual(body.settings, {
        threshold_pct: "2",
        cooldown_minutes: 15,
        monitoring_enabled: true,
      });
      return Response.json(result());
    },
  );
  assert.equal(reply.ok, true);
});

test("database failure cannot become an empty baseline or leak raw details", async () => {
  const store = db({ watchlist_items: {} }, "monitor_baselines");
  const reply = await analyzeForUser(store, "user", "BTCUSDT", env, () => {
    throw new Error("must not fetch");
  });
  assert.equal(reply.ok, false);
  assert.equal(reply.error.includes("private-db-detail"), false);
});

test("HTTP authentication errors, unavailable services, malformed and mismatched results are handled", async () => {
  for (const status of [401, 403, 422, 503, 504]) {
    const reply = await analyzeForUser(
      db({ watchlist_items: {} }),
      "user",
      "BTCUSDT",
      env,
      async () => new Response("private-service-detail", { status }),
    );
    assert.equal(reply.ok, false);
    assert.equal(reply.error.includes("private-service-detail"), false);
  }
  for (const response of [
    new Response("<html>error</html>"),
    Response.json({}),
    Response.json({ ...result(), symbol: "ETHUSDT" }),
  ]) {
    assert.equal(
      (
        await analyzeForUser(
          db({ watchlist_items: {} }),
          "user",
          "BTCUSDT",
          env,
          async () => response,
        )
      ).ok,
      false,
    );
  }
  assert.equal(
    (
      await analyzeForUser(db({ watchlist_items: {} }), "user", "BTCUSDT", env, async () => {
        throw new Error("private-network-detail");
      })
    ).ok,
    false,
  );
});

test("insecure remote URL or URL credentials are rejected before state reads", async () => {
  for (const url of [
    "http://remote.test",
    "https://user:password@example.test",
    "https://example.test/?token=secret",
  ]) {
    const store = db();
    assert.equal(
      (await analyzeForUser(store, "user", "BTCUSDT", { ...env, PYTHON_ANALYSIS_URL: url })).ok,
      false,
    );
    assert.equal(store.queries.length, 0);
  }
});

test("service deadline aborts the request and returns a safe timeout message", async () => {
  const original = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...args) => original(fn, ms === 25_000 ? 1 : ms, ...args);
  try {
    const reply = await analyzeForUser(
      db({ watchlist_items: {} }),
      "user",
      "BTCUSDT",
      env,
      async (_, options) =>
        new Promise((resolve, reject) => {
          options.signal.addEventListener("abort", () =>
            reject(new Error("private-timeout-details")),
          );
        }),
    );
    assert.equal(reply.ok, false);
    assert.match(reply.error, /timed out/);
    assert.equal(reply.error.includes("private-timeout-details"), false);
  } finally {
    globalThis.setTimeout = original;
  }
});
