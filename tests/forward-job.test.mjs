import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

async function load(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, target] of Object.entries(imports))
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(target));
  return `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
}
const runStub = `export const FORWARD_HISTORY_DAYS = 120, FORWARD_SYMBOLS = ["BTCUSDT", "ETHUSDT"], HOUR_MS = 3600000;
  export const missingMinutes = (rows, since, boundary) => Math.max(0, Math.floor((boundary - since) / 60000) - rows.length);
  export async function repairSymbolHistory(deps, symbol, since, boundary, backfill) {
    if (!backfill) throw new Error("backfill-only must repair");
    const series = await deps.readMinutes(symbol, since, boundary);
    return { series, error: symbol === "ETHUSDT" ? "ETHUSDT: HTTP 429" : null };
  }
  export async function runForward() { throw new Error("replaced by the injected run"); }`;
const job = await import(await load("../src/lib/forward/forward-job.server.ts", {
  "./forward-run.server": `data:text/javascript;base64,${Buffer.from(runStub).toString("base64")}`,
}));

test("the hourly job runs every account; one failing account does not stop the others", async () => {
  const seen = [];
  const lines = [];
  const errors = [];
  const result = await job.runHourlyForAccounts({
    listUsers: async () => ["u1", "u2", "u3"],
    deps: async () => ({}),
    strategyIds: ["x"],
    log: (line) => lines.push(line),
    error: (line) => errors.push(line),
    run: async (_deps, input) => {
      seen.push(input.userId);
      if (input.userId === "u2") throw new Error("boom");
      return { runKey: "hour:1", status: "ok", counts: { signals: 1 } };
    },
  });
  assert.deepEqual(seen, ["u1", "u2", "u3"]);
  assert.deepEqual(result, { users: 3, failed: 1 });
  assert.equal(lines.length, 2);
  assert.match(errors[0], /u2: boom/);
});

test("no accounts is a quiet no-op and never builds the dependencies", async () => {
  let built = false;
  const result = await job.runHourlyForAccounts({
    listUsers: async () => [], deps: async () => { built = true; return {}; }, strategyIds: [],
    log: () => {}, error: () => {},
  });
  assert.deepEqual(result, { users: 0, failed: 0 });
  assert.equal(built, false);
});

test("backfill-only reports days stored and minutes still missing per symbol, including failures", async () => {
  const now = 1_760_000_000_000;
  const boundary = Math.floor(now / 3_600_000) * 3_600_000;
  const lines = [];
  const reports = await job.backfillOnly({
    now: () => now,
    readMinutes: async () => [{ open_time_ms: boundary - 10 * 86_400_000 }, { open_time_ms: boundary - 60_000 }],
    backfillMinutes: async () => 0,
  }, (line) => lines.push(line), ["BTCUSDT", "ETHUSDT"]);
  assert.deepEqual(reports.map((r) => [r.symbol, r.days, r.error]), [["BTCUSDT", 10, null], ["ETHUSDT", 10, "ETHUSDT: HTTP 429"]]);
  assert.ok(reports[0].missing > 0);
  assert.match(lines.at(-1), /ETHUSDT: 10 days stored.*FAILED: ETHUSDT: HTTP 429/);
});
