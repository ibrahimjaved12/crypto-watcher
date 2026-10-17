import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

// Guards the crypto Paper account page, Strategy Lab and mvp:check against forex runs (#374): each "latest run"
// query on paper_runs must filter by market, or a user's newer forex run would be read as the crypto wallet.
const files = ["src/lib/paper/paper-data.ts", "src/lib/forward/forward-dashboard.ts", "src/worker/mvp-check.ts"];

test("latest-run readers of paper_runs filter by market", async () => {
  for (const file of files) {
    const text = await readFile(new URL(`../${file}`, import.meta.url), "utf8");
    const chains = [...text.matchAll(/from\("paper_runs"\)[\s\S]*?\.limit\(1\)/g)].map((m) => m[0]);
    assert.ok(chains.length > 0, file);
    for (const chain of chains)
      assert.ok(/market|paper_account/.test(chain), `${file}: ${chain.slice(0, 80)}`);
  }
});
