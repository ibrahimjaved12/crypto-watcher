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
    "./python-service.server": await moduleUrl("../src/lib/python-service.server.ts"),
  })
);
const env = {
  PYTHON_ANALYSIS_ENABLED: "true",
  PYTHON_ANALYSIS_URL: "https://analysis.example.test",
  PYTHON_ANALYSIS_TOKEN: "test-service-" + "x".repeat(32),
};

async function captureDiagnostics(action) {
  const logs = [];
  const originalError = console.error;
  console.error = (...args) => logs.push(args);
  try {
    return { value: await action(), logs };
  } finally {
    console.error = originalError;
  }
}

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
          const value = rows[table] ?? null;
          return {
            data:
              table === "watchlist_items" && value
                ? { instrument_id: "binance-usdm:BTCUSDT", ...value }
                : value,
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
    source_instrument: null,
    instrument: {
      id: "binance-usdm:BTCUSDT",
      exchange: "binance",
      native_symbol: "BTCUSDT",
      market_type: "futures",
      contract_type: "perpetual",
      base_asset: "BTC",
      quote_asset: "USDT",
      margin_asset: "USDT",
      settlement_asset: "USDT",
      linear: true,
      contract_multiplier: 1,
    },
    price_type: "trade",
    endpoint: null,
    retrieved_at_ms: 1704153600000,
    as_of_ms: 1704153600000,
    price: null,
    observed_at_ms: null,
    threshold_pct: "2",
    rolling: {},
    technical: {},
    failure_category: "provider_unavailable",
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
    watchlist_items: { symbol: "BTCUSDT", instrument_id: "binance-usdm:BTCUSDT" },
    monitor_settings: {
      threshold_pct: 3,
      cooldown_minutes: 20,
      monitoring_enabled: true,
    },
    monitor_baselines: {
      baseline_price: 100,
      baseline_at: "2024-01-01T00:00:00Z",
      data_source: "binance-usdm",
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
      assert.equal(options.redirect, "manual");
      assert.equal(options.headers.Authorization, `Bearer ${env.PYTHON_ANALYSIS_TOKEN}`);
      const body = JSON.parse(options.body);
      assert.equal(body.settings.threshold_pct, "3");
      assert.equal(body.instrument_id, "binance-usdm:BTCUSDT");
      assert.equal(body.baseline.source, "binance-usdm");
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

test("read-only baseline eligibility respects independent collection and movement pauses", async () => {
  for (const paused of ["market_data_collection_enabled", "movement_alerts_enabled"]) {
    const reply = await analyzeForUser(
      db({
        watchlist_items: { symbol: "BTCUSDT" },
        monitor_settings: {
          threshold_pct: 2,
          cooldown_minutes: 15,
          monitoring_enabled: true,
          market_data_collection_enabled: true,
          movement_alerts_enabled: true,
          [paused]: false,
        },
      }),
      "user",
      "BTCUSDT",
      env,
      async (_, options) => {
        assert.equal(JSON.parse(options.body).settings.monitoring_enabled, false);
        return Response.json(result());
      },
    );
    assert.equal(reply.ok, true);
  }
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

test("proxy reaches the service on Workers runtimes that reject RequestInit.cache", async () => {
  let requests = 0;
  const reply = await analyzeForUser(
    db({ watchlist_items: {} }),
    "verified-user",
    "BTCUSDT",
    env,
    async (_, options) => {
      // Older/restricted Worker compatibility modes throw before any network I/O.
      if (!["follow", "manual"].includes(options.redirect)) {
        throw new TypeError("Invalid redirect value");
      }
      if ("cache" in options) {
        throw new TypeError(
          "The cache field on RequestInitializerDict is not implemented in fetch",
        );
      }
      requests++;
      assert.equal(options.method, "POST");
      assert.equal(options.headers["Cache-Control"], "no-store");
      assert.equal(options.headers.Authorization, `Bearer ${env.PYTHON_ANALYSIS_TOKEN}`);
      return Response.json(result());
    },
  );
  assert.equal(reply.ok, true);
  assert.equal(requests, 1);
});

test("redirects are never followed and unused error bodies are canceled", async () => {
  for (const status of [301, 302, 303, 307, 308, 401, 403, 422, 503, 504]) {
    for (const cleanupFails of [false, true]) {
      let calls = 0;
      let canceled = 0;
      const { value: reply } = await captureDiagnostics(() =>
        analyzeForUser(db({ watchlist_items: {} }), "user", "BTCUSDT", env, async (_, options) => {
          calls++;
          assert.equal(options.redirect, "manual");
          return new Response(
            new ReadableStream({
              cancel() {
                canceled++;
                if (cleanupFails) throw new Error("private cleanup failure");
              },
            }),
            { status, headers: { Location: "https://other.example.test" } },
          );
        }),
      );
      assert.equal(calls, 1);
      assert.equal(canceled, 1);
      const expected =
        status < 400
          ? "Could not reach the Python analysis service or read its response. Please retry later."
          : status === 504
            ? "Python analysis timed out. Please retry."
            : status === 422
              ? "Your saved monitoring state was rejected by Python. Check your settings and try again."
              : [401, 403].includes(status)
                ? "Python service authentication failed. Its server configuration needs checking."
                : "The Python analysis service is unavailable. Please retry later.";
      const category =
        status < 400
          ? "network_or_response"
          : status === 504
            ? "timeout"
            : status === 422
              ? "invalid_request"
              : [401, 403].includes(status)
                ? "service_auth"
                : "service";
      assert.deepEqual(reply, { ok: false, error: expected, category });
    }
  }
});

test("outbound failures return safe categories without logging private data", async () => {
  const privateResponse = "private-response-body";
  const cases = [
    {
      stage: "fetch",
      send: async (_, options) => {
        const cause = Object.assign(new Error(`socket ${env.PYTHON_ANALYSIS_TOKEN}`), {
          code: "ECONNRESET",
        });
        throw new TypeError(
          `fetch failed ${env.PYTHON_ANALYSIS_URL} ${options.headers.Authorization} private-user-id ${options.body}`,
          { cause },
        );
      },
      status: undefined,
    },
    {
      stage: "response-status",
      send: async () => ({
        status: 200,
        get ok() {
          throw new TypeError("could not read response status");
        },
      }),
      status: 200,
    },
    {
      stage: "response-text",
      send: async () => ({
        status: 200,
        ok: true,
        text: async () => {
          throw new TypeError("could not read response text");
        },
      }),
      status: 200,
    },
    {
      stage: "size-guard",
      send: async () => new Response(privateResponse.repeat(7_000)),
      status: 200,
    },
    {
      stage: "json-parse",
      send: async () => new Response(`<${privateResponse}>`),
      status: 200,
    },
    {
      stage: "schema-validation",
      send: async () => Response.json({ privateResponse }),
      status: 200,
    },
  ];

  for (const entry of cases) {
    const { value: reply, logs } = await captureDiagnostics(() =>
      analyzeForUser(db({ watchlist_items: {} }), "private-user-id", "BTCUSDT", env, entry.send),
    );
    assert.equal(reply.ok, false);
    assert.equal(
      reply.category,
      entry.stage === "schema-validation" ? "invalid_response" : "network_or_response",
    );
    assert.equal(logs.length, 0);
    const surfaced = JSON.stringify(reply);
    assert.equal(surfaced.includes(env.PYTHON_ANALYSIS_TOKEN), false);
    assert.equal(surfaced.includes(env.PYTHON_ANALYSIS_URL), false);
    assert.equal(surfaced.includes("private-user-id"), false);
    assert.equal(surfaced.includes(privateResponse), false);
  }
});

test("successful analysis returns transport measurements", async () => {
  const expected = result();
  const { value: reply, logs } = await captureDiagnostics(() =>
    analyzeForUser(db({ watchlist_items: {} }), "user", "BTCUSDT", env, async () =>
      Response.json(expected),
    ),
  );
  assert.equal(reply.ok, true);
  assert.deepEqual(reply.analysis, expected);
  assert.ok(reply.metrics.duration_ms >= 0);
  assert.ok(reply.metrics.request_bytes > 0);
  assert.ok(reply.metrics.response_bytes > 0);
  assert.deepEqual(logs, []);
});

test("misspelled secret names fail configuration before reads or outbound requests", async () => {
  for (const name of Object.keys(env)) {
    const config = { ...env, [name.toLowerCase()]: env[name] };
    delete config[name];
    const store = db();
    const reply = await analyzeForUser(store, "user", "BTCUSDT", config, () => {
      assert.fail("must not call Python with incomplete configuration");
    });
    assert.equal(
      reply.error,
      "Python analysis is not enabled or its service configuration is incomplete.",
    );
    assert.equal(store.queries.length, 0);
  }
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
    Response.json({
      ...result(),
      instrument: { ...result().instrument, id: "binance-usdm:ETHUSDT" },
    }),
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
