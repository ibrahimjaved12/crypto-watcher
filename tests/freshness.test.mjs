import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const source = await readFile(new URL("../src/lib/freshness.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { timestampFreshness } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

test("freshness is derived from persisted domain time, not page refresh time", () => {
  const now = Date.parse("2026-09-26T12:00:00Z");
  const options = { available: true, now, freshForMs: 10 * 60_000, delayedForMs: 30 * 60_000 };

  assert.equal(timestampFreshness("2026-09-26T11:55:00Z", options), "FRESH");
  assert.equal(timestampFreshness("2026-09-26T11:40:00Z", options), "DELAYED");
  assert.equal(timestampFreshness("2026-09-26T11:00:00Z", options), "STALE");
  assert.equal(timestampFreshness(null, options), "UNAVAILABLE");
  assert.equal(
    timestampFreshness("2026-09-26T11:55:00Z", { ...options, available: false }),
    "UNAVAILABLE",
  );
});
