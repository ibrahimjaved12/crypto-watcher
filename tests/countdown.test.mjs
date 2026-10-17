import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

async function moduleUrl(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, target] of Object.entries(imports))
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(target));
  return `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
}

const { formatCountdown } = await import(await moduleUrl("../src/lib/market/countdown.ts"));

test("formatCountdown truncates to d/h, h/min or min and never goes negative", () => {
  assert.equal(formatCountdown(30 * 60_000), "30 min");
  assert.equal(formatCountdown(3 * 3_600_000 + 12 * 60_000), "3 h 12 min");
  assert.equal(formatCountdown(30 * 3_600_000 + 5 * 60_000), "1 d 6 h");
  assert.equal(formatCountdown(59_999), "< 1 min");
  assert.equal(formatCountdown(0), "< 1 min");
  assert.equal(formatCountdown(-5 * 60_000), "< 1 min");
  assert.equal(formatCountdown(24 * 3_600_000), "1 d 0 h");
});
