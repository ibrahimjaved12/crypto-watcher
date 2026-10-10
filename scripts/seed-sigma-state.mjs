#!/usr/bin/env node
// Import an offline Python seed. No market evaluation, wallet updates, or local server.
import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";

export function canonical(value) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number" && Number.isSafeInteger(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort()
    .map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  throw new Error("Invalid seed JSON");
}

export function validateSeed(record) {
  const { checksum, ...body } = record;
  if (body.sigma_version !== "forward-sigma-v1" || !/^[A-Z0-9]{5,16}$/.test(body.symbol) ||
      !Number.isSafeInteger(body.as_of_ms) || body.as_of_ms < 0 ||
      body.payload?.symbol !== body.symbol || body.payload?.as_of_ms !== String(body.as_of_ms) ||
      createHash("sha256").update(canonical(body)).digest("hex") !== checksum)
    throw new Error("Sigma seed version, identity, or checksum mismatch");
  return record;
}

async function main() {
  const path = process.argv[2];
  if (!path) throw new Error("Usage: node scripts/seed-sigma-state.mjs SEED.json");
  const record = validateSeed(JSON.parse(await readFile(path, "utf8")));
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required");
  const response = await fetch(new URL("/rest/v1/rpc/seed_forward_sigma_state", url), {
    method: "POST", headers: { apikey: key, "Content-Type": "application/json",
      ...(!key.startsWith("sb_secret_") ? { Authorization: `Bearer ${key}` } : {}) },
    body: JSON.stringify({ p_state: record }), signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error(`Seed import failed (HTTP ${response.status}); existing checkpoints are never overwritten`);
  console.log(`Imported ${record.symbol} ${record.sigma_version} as_of_ms=${record.as_of_ms}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
}
