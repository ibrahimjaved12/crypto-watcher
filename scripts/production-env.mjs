import { execFileSync } from "node:child_process";
import { existsSync, writeFileSync } from "node:fs";
import { randomBytes } from "node:crypto";

const path = ".env.production.local";
const existsMessage = `${path} already exists. Existing files are never overwritten.`;

if (existsSync(path)) {
  console.error(existsMessage);
  process.exitCode = 1;
} else {
  // Never print status output or errors: they can contain the service-role key.
  try {
    const status = JSON.parse(
      execFileSync("supabase", ["status", "--workdir", "operational-db", "-o", "json"], {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
      }),
    );
    const url = new URL(status.API_URL);
    if (
      !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname) ||
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.pathname !== "/" ||
      url.search ||
      url.hash
    )
      throw new Error("not a loopback HTTP(S) origin");
    if (typeof status.SERVICE_ROLE_KEY !== "string" || !status.SERVICE_ROLE_KEY)
      throw new Error("missing service-role key");
    const values = {
      APP_PROFILE: "production",
      MACHINE_ONLY_ENABLED: "true",
      ALLOW_HOSTED_SUPABASE: "true",
      SUPABASE_URL: "REPLACE_HOSTED_SUPABASE_URL",
      SUPABASE_SERVICE_ROLE_KEY: "REPLACE_HOSTED_SUPABASE_SERVICE_ROLE_KEY",
      OPERATIONAL_DB_ENABLED: "true",
      OPERATIONAL_SUPABASE_URL: url.origin,
      OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY: status.SERVICE_ROLE_KEY,
      RUNNER_GATEWAY_ENABLED: "false",
      RUNNER_JOBS_ENABLED: "false",
      RUNNER_GATEWAY_PORT: "8787",
      RUNNER_GATEWAY_SECRET: "REPLACE_WITH_RANDOM_32_PLUS_CHARS",
      BINANCE_COLLECTOR_ENABLED: "true",
      PYTHON_ANALYSIS_ENABLED: "true",
      PYTHON_ANALYSIS_URL: "http://127.0.0.1:8000",
      PYTHON_FORWARD_URL: "http://127.0.0.1:8001",
      PYTHON_ANALYSIS_TOKEN: randomBytes(32).toString("hex"),
    };
    writeFileSync(
      path,
      Object.entries(values)
        .map(([name, value]) => `${name}=${JSON.stringify(value)}`)
        .join("\n") + "\n",
      { flag: "wx", mode: 0o600 },
    );
    console.log(
      "Created .env.production.local from local operational Supabase status. Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY before running npm run worker:host.",
    );
  } catch (error) {
    console.error(
      error?.code === "EEXIST"
        ? existsMessage
        : "Could not create .env.production.local. Start the local operational Supabase stack and ensure the CLI is on PATH. Existing files are never overwritten.",
    );
    process.exitCode = 1;
  }
}
