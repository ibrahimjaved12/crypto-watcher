import assert from "node:assert/strict";
import { test } from "node:test";

import { parseArgs } from "../scripts/grant-access.mjs";

test("grant-access parses labels, revoke, list and expiry; rejects old flags and missing labels", () => {
  assert.deepEqual(parseArgs(["Me@Example.com", "--label", "all"]), { email: "me@example.com", revoke: false, label: "all" });
  assert.deepEqual(parseArgs(["me@example.com", "--revoke"]), { email: "me@example.com", revoke: true });
  assert.deepEqual(parseArgs(["--list"]), { list: true });
  assert.equal(parseArgs(["me@example.com", "--label", "forex", "--expires", "2027-01-01"]).expires,
    "2027-01-01T00:00:00.000Z");
  assert.throws(() => parseArgs(["me@example.com", "--label", "pro"]), /Invalid --label/);
  assert.throws(() => parseArgs(["me@example.com", "--role", "owner"]), /Unknown flag --role/);
  assert.throws(() => parseArgs(["me@example.com", "--plan", "crypto"]), /Unknown flag --plan/);
  assert.throws(() => parseArgs(["me@example.com"]), /Usage/);
  assert.throws(() => parseArgs(["me@example.com", "--expires", "2027-01-01"]), /Usage/);
  assert.throws(() => parseArgs([]), /Usage/);
});
