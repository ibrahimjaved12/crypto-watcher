import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

test("alert check success handler uses error and warning toasts for unsuccessful checks", async () => {
  const source = await readFile(new URL("../src/routes/_authenticated/dashboard.tsx", import.meta.url), "utf8");
  const handler = source.split("const check = useMutation({")[1]?.split("onError:")[0];
  assert.ok(handler, "check success handler exists");
  assert.match(handler, /toast\.error\(/);
  assert.match(handler, /toast\.warning\(/);
  assert.match(handler, /result\.status === "failed"/);
  assert.match(handler, /result\.status === "partial"/);
});
