import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const source = await readFile(new URL("../src/lib/operational/config.server.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { collectorCandleSettleSeconds, operationalDbConfig } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("collector settle seconds defaults to 15 and validates operational integer bounds", () => {
  assert.equal(collectorCandleSettleSeconds({}), 15);
  assert.equal(collectorCandleSettleSeconds({ COLLECTOR_CANDLE_SETTLE_SECONDS: "" }), 15);
  for (const seconds of [5, 15, 60]) {
    assert.equal(collectorCandleSettleSeconds({ COLLECTOR_CANDLE_SETTLE_SECONDS: String(seconds) }), seconds);
  }
  for (const value of ["4", "61", "15.5", "invalid", "Infinity"]) {
    assert.throws(() => operationalDbConfig({ COLLECTOR_CANDLE_SETTLE_SECONDS: value }), /5–60/);
  }
});
