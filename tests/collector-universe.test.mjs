import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;

// The module only reads the documented collection-control defaults.
const engineStub = stub(`
  export const DEFAULT_SETTINGS = {
    monitoring_enabled: true,
    market_data_collection_enabled: true,
  };
`);

let loadCount = 0;

async function load(imports = {}) {
  const source = await readFile(
    new URL("../src/lib/market/collector-subscriptions.server.ts", import.meta.url),
    "utf8",
  );
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  const replacements = { "../monitor/engine.server": engineStub, ...imports };
  for (const [specifier, target] of Object.entries(replacements)) {
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(target));
  }
  const url = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
  return import(`${url}#${loadCount++}`);
}

function admin({ watchers = [], settings = [], watcherError = null, settingsError = null } = {}) {
  return {
    from(table) {
      if (table === "watchlist_items") {
        return { select: () => Promise.resolve({ data: watchers, error: watcherError }) };
      }
      assert.equal(table, "monitor_settings");
      return {
        select() {
          return this;
        },
        in() {
          return Promise.resolve({ data: settings, error: settingsError });
        },
      };
    },
  };
}

function fakeStore(initial = [], enabled = true) {
  const state = { assigned: [], current: [...initial], enabled };
  return {
    get assigned() {
      return state.assigned;
    },
    get enabled() {
      return state.enabled;
    },
    async readCollectorSubscriptions() {
      return [...state.current];
    },
    async assignCollectorSubscriptions(symbols) {
      state.assigned.push([...symbols]);
      state.current = [...symbols];
    },
  };
}

const enabled = { monitoring_enabled: true, market_data_collection_enabled: true };

test("the collector universe collapses duplicates and honours collection controls", async () => {
  const { collectorUniverse } = await load();
  const watchers = [
    { user_id: "a", symbol: "btcusdt" },
    { user_id: "b", symbol: "BTCUSDT" },
    { user_id: "c", symbol: "ETHUSDT" },
    { user_id: "d", symbol: "SOLUSDT" },
    { user_id: "e", symbol: "DOGEUSDT" },
  ];
  const settings = new Map([
    ["a", enabled],
    ["b", enabled],
    // Collection paused: the symbol is not required by this account.
    ["c", { monitoring_enabled: true, market_data_collection_enabled: false }],
    // Master switch paused: no market-data work for this account.
    ["d", { monitoring_enabled: false, market_data_collection_enabled: true }],
    // "e" has no settings row and keeps the documented on-by-default behaviour.
  ]);
  assert.deepEqual(collectorUniverse(watchers, settings), ["BTCUSDT", "DOGEUSDT"]);
});

test("a symbol leaves the universe only when its last relevant subscriber does", async () => {
  const { collectorUniverse } = await load();
  assert.deepEqual(
    collectorUniverse(
      [
        { user_id: "a", symbol: "BTCUSDT" },
        { user_id: "b", symbol: "BTCUSDT" },
      ],
      new Map([
        ["a", enabled],
        ["b", enabled],
      ]),
    ),
    ["BTCUSDT"],
  );
  // One subscriber remains, so the symbol stays.
  assert.deepEqual(
    collectorUniverse([{ user_id: "a", symbol: "BTCUSDT" }], new Map([["a", enabled]])),
    ["BTCUSDT"],
  );
  // The last subscriber disables collection, so the symbol is removed.
  assert.deepEqual(
    collectorUniverse(
      [{ user_id: "a", symbol: "BTCUSDT" }],
      new Map([["a", { monitoring_enabled: true, market_data_collection_enabled: false }]]),
    ),
    [],
  );
  // An empty universe is a valid assignment.
  assert.deepEqual(collectorUniverse([], new Map()), []);
});

test("the universe sync assigns changes and no-ops while unchanged", async () => {
  const { syncCollectorUniverse } = await load();
  const env = { BINANCE_COLLECTOR_ENABLED: "true" };
  const store = fakeStore([]);

  const assigned = await syncCollectorUniverse(
    admin({ watchers: [{ user_id: "a", symbol: "BTCUSDT" }] }),
    store,
    env,
  );
  assert.equal(assigned.status, "assigned");
  assert.deepEqual(assigned.symbols, ["BTCUSDT"]);
  assert.deepEqual(store.assigned, [["BTCUSDT"]]);

  const unchanged = await syncCollectorUniverse(
    admin({ watchers: [{ user_id: "a", symbol: "BTCUSDT" }] }),
    store,
    env,
  );
  assert.equal(unchanged.status, "unchanged");
  assert.equal(store.assigned.length, 1);

  const removed = await syncCollectorUniverse(admin({ watchers: [] }), store, env);
  assert.equal(removed.status, "assigned");
  assert.deepEqual(removed.symbols, []);
  assert.deepEqual(store.assigned, [["BTCUSDT"], []]);
});

test("the universe sync runs while scheduled monitoring is disabled", async () => {
  const { syncCollectorUniverse } = await load();
  const store = fakeStore([]);
  const result = await syncCollectorUniverse(
    admin({ watchers: [{ user_id: "a", symbol: "ETHUSDT" }] }),
    store,
    { BINANCE_COLLECTOR_ENABLED: "true", SCHEDULED_MONITOR_ENABLED: "false" },
  );
  assert.equal(result.status, "assigned");
  assert.deepEqual(result.symbols, ["ETHUSDT"]);
});

test("the universe sync skips without collector mode or an operational store", async () => {
  const { syncCollectorUniverse } = await load();
  let lovableReads = 0;
  const forbiddenAdmin = {
    from() {
      lovableReads += 1;
      throw new Error("Lovable must not be read");
    },
  };
  assert.deepEqual(
    await syncCollectorUniverse(forbiddenAdmin, fakeStore([]), {
      BINANCE_COLLECTOR_ENABLED: "false",
    }),
    { status: "skipped", reason: "Collector mode disabled" },
  );
  assert.deepEqual(
    await syncCollectorUniverse(forbiddenAdmin, fakeStore([], false), {
      BINANCE_COLLECTOR_ENABLED: "true",
    }),
    { status: "skipped", reason: "Operational store disabled" },
  );
  assert.equal(lovableReads, 0);
});

test("a failed watchlist read surfaces instead of silently emptying the universe", async () => {
  const { syncCollectorUniverse } = await load();
  const store = fakeStore(["BTCUSDT"]);
  await assert.rejects(
    syncCollectorUniverse(admin({ watcherError: { message: "db down" } }), store, {
      BINANCE_COLLECTOR_ENABLED: "true",
    }),
    /watchlist read failed: db down/,
  );
  assert.deepEqual(store.assigned, []);
});

test("startup bootstrap reconciles only in collector mode and only once per process", async () => {
  const bootstrapImports = {
    "@/integrations/supabase/client.server": stub(`
      export const supabaseAdmin = {
        from(table) {
          if (table === "watchlist_items") {
            return { select: () => Promise.resolve({ data: [], error: null }) };
          }
          return {
            select() { return this; },
            in() { return Promise.resolve({ data: [], error: null }); },
          };
        },
      };
    `),
    "../operational/repository.server": stub(`
      export function getOperationalStore() {
        return {
          enabled: true,
          // A stale assignment means the derived empty set is a real change.
          async readCollectorSubscriptions() { return ["BTCUSDT"]; },
          async assignCollectorSubscriptions() { globalThis.__universeBootstrap.syncs += 1; },
        };
      }
    `),
  };

  globalThis.__universeBootstrap = { syncs: 0 };
  try {
    const disabled = await load(bootstrapImports);
    disabled.bootstrapCollectorUniverse({ BINANCE_COLLECTOR_ENABLED: "false" });
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(globalThis.__universeBootstrap.syncs, 0);

    const module = await load(bootstrapImports);
    module.bootstrapCollectorUniverse({ BINANCE_COLLECTOR_ENABLED: "true" });
    module.bootstrapCollectorUniverse({ BINANCE_COLLECTOR_ENABLED: "true" });
    for (let attempt = 0; attempt < 50 && globalThis.__universeBootstrap.syncs === 0; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(globalThis.__universeBootstrap.syncs, 1);
  } finally {
    delete globalThis.__universeBootstrap;
  }
});
