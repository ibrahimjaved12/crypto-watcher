import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const source = readFileSync(new URL("../src/lib/google-identity.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { createNonce } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

test("createNonce returns 32 random bytes as hex and its SHA-256 hex", async () => {
  const { raw, hashed } = await createNonce();
  assert.match(raw, /^[0-9a-f]{64}$/);
  assert.match(hashed, /^[0-9a-f]{64}$/);
  assert.equal(hashed, createHash("sha256").update(raw).digest("hex"));
  assert.notEqual(hashed, raw);
});

test("createNonce differs on every call", async () => {
  const [a, b] = await Promise.all([createNonce(), createNonce()]);
  assert.notEqual(a.raw, b.raw);
  assert.notEqual(a.hashed, b.hashed);
});
