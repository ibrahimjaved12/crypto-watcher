import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { classifyHostedDatabaseSize, type Check } from "../lib/forward/mvp-check";

// Count table storage, including indexes and TOAST, once per relation.
// Partition leaves are relkind 'r'; parent partitions have no storage of their own.
export const PUBLIC_SCHEMA_SIZE_SQL = `BEGIN READ ONLY;
SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0)::bigint
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'm');
COMMIT;`;

/** Use an existing local libpq service; never create credentials or a database RPC. */
export async function hostedDatabaseSizeCheck(): Promise<Check> {
  const name = "hosted public-schema size";
  const skipped = (detail: string): Check => ({ name, level: "WARN", detail: `SKIP: ${detail}` });
  if (!process.env["PGSERVICE"]) {
    return skipped("no local PGSERVICE connection configured for the hosted DB; service-role API keys cannot run SQL");
  }
  if (process.env["APP_PROFILE"] !== "production" || process.env["ALLOW_HOSTED_SUPABASE"] !== "true") {
    return skipped("requires the hosted production profile (--mode production)");
  }
  try {
    const { stdout } = await promisify(execFile)("psql", [
      "-X", "--no-password", "--quiet", "--tuples-only", "--no-align",
      "--set", "ON_ERROR_STOP=1", "--command", PUBLIC_SCHEMA_SIZE_SQL,
    ], {
      env: { ...process.env, PGCONNECT_TIMEOUT: "5", PGOPTIONS: "-c default_transaction_read_only=on -c statement_timeout=5000" },
      timeout: 10_000,
      maxBuffer: 64 * 1024,
    });
    const value = stdout.trim();
    if (!/^\d+$/.test(value)) return skipped("size query returned an invalid result");
    return classifyHostedDatabaseSize(Number(value));
  } catch {
    // psql errors can contain connection information: never echo stderr or the environment.
    return skipped("psql or the existing hosted service connection is unavailable, or the read-only query failed");
  }
}
