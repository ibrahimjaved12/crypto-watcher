import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const source = await readFile(
  new URL("../src/lib/monitor/observation.ts", import.meta.url),
  "utf8",
);
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext },
});
const { completedObservation } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);
const now = 1704067320000;
const closed = { time: now - 60000, close: 100, complete: true };

test("forming spikes are excluded and the close boundary is exact", () => {
  assert.equal(
    completedObservation([closed, { time: now, close: 150, complete: true }], now).price,
    100,
  );
  assert.throws(() => completedObservation([closed], now - 1), /No fresh/);
  assert.throws(() => completedObservation([{ ...closed, complete: false }], now), /No fresh/);
});

test("out-of-order input is sorted by time; invalid or stale input is rejected", () => {
  assert.equal(
    completedObservation([closed, { ...closed, time: now - 120000, close: 90 }], now).price,
    100,
  );
  assert.throws(() => completedObservation([closed, closed], now), /duplicate/);
  for (const close of [NaN, Infinity, 0, -1]) {
    assert.throws(() => completedObservation([{ ...closed, close }], now), /Invalid/);
  }
  assert.throws(() => completedObservation([{ ...closed, time: now + 60000 }], now), /Invalid/);
  assert.throws(() => completedObservation([{ ...closed, time: now - 1 }], now), /Invalid/);
  assert.equal(completedObservation([closed], now + 600000).price, 100);
  assert.throws(() => completedObservation([closed], now + 600001), /No fresh/);
});
