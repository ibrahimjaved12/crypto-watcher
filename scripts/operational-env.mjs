import { execFileSync } from "node:child_process";
import { appendFileSync, chmodSync, existsSync, readFileSync } from "node:fs";

const path = ".env.local";
const names = [
  "OPERATIONAL_DB_ENABLED",
  "OPERATIONAL_SUPABASE_URL",
  "OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY",
  "OPERATIONAL_CANDLE_RETENTION_DAYS",
  "OPERATIONAL_MONITOR_RUN_RETENTION_DAYS",
  "OPERATIONAL_OUTBOX_MAX_ATTEMPTS",
];

try {
  if (!existsSync(path))
    throw new Error(".env.local does not exist; run npm run env:local instead");
  const existing = readFileSync(path, "utf8");
  if (names.some((name) => new RegExp(`^${name}=`, "m").test(existing))) {
    throw new Error("operational variables already exist; refusing to overwrite them");
  }
  const status = JSON.parse(
    execFileSync("supabase", ["status", "--workdir", "operational-db", "-o", "json"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    }),
  );
  const url = new URL(status.API_URL);
  if (!["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) throw new Error("not local");
  if (!status.SERVICE_ROLE_KEY) throw new Error("missing service-role key");
  const values = {
    OPERATIONAL_DB_ENABLED: "true",
    OPERATIONAL_SUPABASE_URL: url.origin,
    OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY: status.SERVICE_ROLE_KEY,
    OPERATIONAL_CANDLE_RETENTION_DAYS: "8",
    OPERATIONAL_MONITOR_RUN_RETENTION_DAYS: "30",
    OPERATIONAL_OUTBOX_MAX_ATTEMPTS: "10",
  };
  appendFileSync(
    path,
    `${existing.endsWith("\n") ? "" : "\n"}${Object.entries(values)
      .map(([name, value]) => `${name}=${JSON.stringify(value)}`)
      .join("\n")}\n`,
    { mode: 0o600 },
  );
  chmodSync(path, 0o600);
  console.log("Added the separate local operational database to .env.local.");
} catch (error) {
  console.error(
    `[local-operational-env] ${error instanceof Error ? error.message : String(error)}`,
  );
  process.exitCode = 1;
}
