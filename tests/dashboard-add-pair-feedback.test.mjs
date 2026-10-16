import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

test("adding a pair confirms success and keeps previous market quotes", async () => {
  const source = await readFile(new URL("../src/routes/_authenticated/dashboard.tsx", import.meta.url), "utf8");
  assert.match(source, /placeholderData: keepPreviousData/);
  const add = source.split("const add = useMutation({")[1]?.split("const remove =")[0];
  assert.ok(add, "add mutation exists");
  assert.match(add, /toast\.success\(/);
  assert.match(add, /baseAsset\(symbol\)/);
  assert.match(source, /<QuoteCard\b[\s\S]*?onRetry=\{\(\) => void market\.refetch\(\)\}/);
  assert.match(source, /market\.isFetching \|\| \(!market\.data && !market\.isError\) \? \(\s*<QuoteCardSkeleton/);
  const unavailable = source.split('Price unavailable{" "}')[1]?.split("</p>")[0];
  assert.ok(unavailable, "missing quotes show the unavailable-price fallback");
  assert.match(unavailable, /<button\b[\s\S]*?onClick=\{\(\) => void market\.refetch\(\)\}[\s\S]*?>\s*Retry\s*<\/button>/);
});
