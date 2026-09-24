import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { randomBytes } from "node:crypto";

// Never print status output: it contains the service-role key.
try {
  const status = JSON.parse(
    execFileSync("supabase", ["status", "-o", "json"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    }),
  );
  const url = new URL(status.API_URL);
  if (!["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) throw new Error("not local");
  if (!status.ANON_KEY || !status.SERVICE_ROLE_KEY) throw new Error("missing credentials");
  const values = {
    APP_PROFILE: "local",
    VITE_APP_PROFILE: "local",
    ALLOW_HOSTED_SUPABASE: "false",
    VITE_ALLOW_HOSTED_SUPABASE: "false",
    SUPABASE_URL: url.origin,
    VITE_SUPABASE_URL: url.origin,
    SUPABASE_PUBLISHABLE_KEY: status.ANON_KEY,
    VITE_SUPABASE_PUBLISHABLE_KEY: status.ANON_KEY,
    SUPABASE_SERVICE_ROLE_KEY: status.SERVICE_ROLE_KEY,
    PYTHON_ANALYSIS_ENABLED: "false",
    PYTHON_ANALYSIS_URL: "http://127.0.0.1:8000",
    PYTHON_ANALYSIS_TOKEN: randomBytes(32).toString("base64url"),
    SCHEDULED_MONITOR_ENABLED: "false",
    BINANCE_COLLECTOR_ENABLED: "false",
    VITE_MARKET_AUTO_REFRESH_ENABLED: "false",
    VITE_TA_HISTORY_AUTO_REFRESH_ENABLED: "false",
  };
  writeFileSync(
    ".env.local",
    Object.entries(values)
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
      .join("\n") + "\n",
    { flag: "wx", mode: 0o600 },
  );
  console.log("Created .env.local from local Supabase status. Run npm run dev:local.");
} catch {
  console.error(
    "Could not create .env.local. Start local Supabase, ensure the CLI is on PATH, and ensure .env.local does not already exist. Existing files are never overwritten.",
  );
  process.exitCode = 1;
}
