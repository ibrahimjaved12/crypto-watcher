// Fixed public runner URL for `npm run up` (#308). Hostnames here are placeholders, not real machines.
import assert from "node:assert/strict";
import { test } from "node:test";

import { parseRunnerPublicUrl } from "../scripts/lib/runner-url.mjs";

test("unset or empty returns null (missing)", () => {
  assert.equal(parseRunnerPublicUrl(undefined), null);
  assert.equal(parseRunnerPublicUrl(null), null);
  assert.equal(parseRunnerPublicUrl(""), null);
});

test("a Funnel address is normalized to its origin", () => {
  assert.equal(parseRunnerPublicUrl("https://a.b.ts.net"), "https://a.b.ts.net");
  assert.equal(parseRunnerPublicUrl("https://a.b.ts.net/"), "https://a.b.ts.net");
  assert.equal(parseRunnerPublicUrl("https://My-Laptop.tail-1234.ts.net"), "https://my-laptop.tail-1234.ts.net");
});

test("anything else is rejected without echoing the value", () => {
  for (const value of [
    "http://a.b.ts.net",
    "https://a.b.example.com",
    "https://ts.net",
    "https://b.ts.net.example.com",
    "https://a.b.ts.net:8443",
    "https://a.b.ts.net:443",
    "https://a.b.ts.net/path",
    "https://a.b.ts.net/?x=1",
    "https://a.b.ts.net?x=1",
    "https://a.b.ts.net#x",
    "https://a.b.ts.net/#x",
    "https://user:secret@a.b.ts.net",
    "https://user@a.b.ts.net",
    " https://a.b.ts.net",
    "a.b.ts.net",
    "not a url",
    42,
  ]) {
    assert.throws(() => parseRunnerPublicUrl(value), (error) => {
      assert.equal(error.message,
        "RUNNER_PUBLIC_URL must be an https://<machine>.<tailnet>.ts.net address with no path or port");
      if (typeof value === "string") assert.ok(!error.message.includes(value.trim()), value);
      return true;
    }, String(value));
  }
});

test("the launcher requires the fixed URL and has no tunnel or secret-writing path", async () => {
  const { readFile } = await import("node:fs/promises");
  const up = await readFile(new URL("../scripts/up.mjs", import.meta.url), "utf8");
  assert.match(up, /parseRunnerPublicUrl\(process\.env\.RUNNER_PUBLIC_URL \?\? env\?\.RUNNER_PUBLIC_URL\)/);
  assert.match(up, /command\("tailscale", \["version"\]\)/);
  assert.ok(up.includes(
    "RUNNER_PUBLIC_URL is missing. Set it to your Tailscale Funnel address (see docs/worker-host.md).",
  ));
  assert.ok(up.includes('"Usage: npm run up"'));
  for (const stale of ["cloudflared", "trycloudflare", "update-worker-secret", "wrangler"]) {
    assert.ok(!up.toLowerCase().includes(stale), stale);
  }
});
