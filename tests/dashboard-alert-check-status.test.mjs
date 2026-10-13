import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

test("alert check success handler describes the result and uses all three toast kinds", async () => {
  const source = await readFile(new URL("../src/routes/_authenticated/dashboard.tsx", import.meta.url), "utf8");
  const handler = source.split("const check = useMutation({")[1]?.split("onError:")[0];
  assert.ok(handler, "check success handler exists");
  assert.match(handler, /describeAlertCheck\(result\)/);
  assert.match(handler, /toast\.success\(/);
  assert.match(handler, /toast\.error\(/);
  assert.match(handler, /toast\.warning\(/);
});
