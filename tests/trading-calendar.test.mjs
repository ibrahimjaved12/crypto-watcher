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

const cal = await import(await moduleUrl("../src/lib/market/trading-calendar.ts"));

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const utc = (text) => Date.parse(`${text}:00Z`);

const WEEKS = [
  [utc("2026-10-16T21:00"), utc("2026-10-18T21:00"), 48],
  [utc("2026-10-30T21:00"), utc("2026-11-01T22:00"), 49],
  [utc("2026-03-06T22:00"), utc("2026-03-08T21:00"), 47],
];

test("FX week vectors and open/closed boundaries match Python", () => {
  for (const [close, open, gap] of WEEKS) {
    assert.equal(cal.fxWeekWindow(close - 1)[1], close);
    assert.equal(cal.fxWeekWindow(close)[0], open);
    assert.equal(open - close, gap * HOUR);
    assert.equal(cal.isFxOpen(close - 1), true);
    assert.equal(cal.isFxOpen(close), false);
    assert.equal(cal.isFxOpen(open - 1), false);
    assert.equal(cal.isFxOpen(open), true);
  }
});

test("next open from the weekend", () => {
  assert.equal(cal.fxNextOpenMs(utc("2026-10-17T12:00")), utc("2026-10-18T21:00"));
  assert.equal(cal.fxNextOpenMs(utc("2026-11-01T21:30")), utc("2026-11-01T22:00"));
});

test("sessions and the DST-dependent London/New York overlap", () => {
  assert.deepEqual(cal.fxSessionTags(utc("2026-10-14T12:00")), ["london", "new_york"]);
  assert.ok(cal.fxSessionTags(utc("2026-10-14T20:00")).includes("sydney"));
  assert.deepEqual(cal.fxSessionTags(utc("2026-10-17T12:00")), []);
  for (const [day, expected] of [["2026-10-14", 240], ["2026-10-26", 300], ["2026-11-03", 240]]) {
    const start = utc(`${day}T00:00`);
    let overlap = 0;
    for (let minute = 0; minute < 1440; minute++) {
      const tags = cal.fxSessionTags(start + minute * MINUTE);
      if (tags.includes("london") && tags.includes("new_york")) overlap++;
    }
    assert.equal(overlap, expected, day);
  }
});

test("crypto is always open; forex follows the FX week", () => {
  for (const ms of [utc("2026-10-17T12:00"), utc("2026-10-14T12:00"), 0]) assert.equal(cal.isMarketOpen("crypto", ms), true);
  assert.equal(cal.isMarketOpen("forex", utc("2026-10-17T12:00")), false);
  assert.equal(cal.isMarketOpen("forex", utc("2026-10-14T12:00")), true);
  assert.throws(() => cal.isFxOpen(1.5));
});
