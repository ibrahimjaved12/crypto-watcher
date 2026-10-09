export type OperationalDbConfig =
  | { enabled: false }
  | {
      enabled: true;
      url: string;
      serviceRoleKey: string;
      candleRetentionDays: number;
      minuteCandleRetentionDays: number;
      monitorRunRetentionDays: number;
      outboxMaxAttempts: number;
    };

function boundedInteger(value: string | undefined, fallback: number, min: number, max: number) {
  const parsed = value === undefined || value === "" ? fallback : Number(value);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`Operational database retention/retry value must be ${min}–${max}`);
  }
  return parsed;
}

export function operationalDbConfig(
  env: Record<string, string | undefined> = process.env,
): OperationalDbConfig {
  const rawEnabled = env["OPERATIONAL_DB_ENABLED"];
  if (rawEnabled !== undefined && rawEnabled !== "true" && rawEnabled !== "false") {
    throw new Error("OPERATIONAL_DB_ENABLED must be true or false");
  }
  if (rawEnabled !== "true") return { enabled: false };

  const rawUrl = env["OPERATIONAL_SUPABASE_URL"];
  const serviceRoleKey = env["OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY"];
  if (!rawUrl || !serviceRoleKey) {
    throw new Error(
      "OPERATIONAL_SUPABASE_URL and OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY are required when the operational database is enabled",
    );
  }
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new Error("OPERATIONAL_SUPABASE_URL must be a valid HTTP(S) origin");
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  ) {
    throw new Error("OPERATIONAL_SUPABASE_URL must be an HTTP(S) origin without credentials");
  }
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (env["APP_PROFILE"] === "local" && !loopback) {
    throw new Error("Local development requires a loopback operational database URL");
  }
  if (!loopback && url.protocol !== "https:") {
    throw new Error("Hosted operational Supabase requires HTTPS");
  }
  if (env["SUPABASE_URL"] && new URL(env["SUPABASE_URL"]).origin === url.origin) {
    throw new Error("The operational database must be separate from the main database");
  }

  return {
    enabled: true,
    url: url.origin,
    serviceRoleKey,
    candleRetentionDays: boundedInteger(env["OPERATIONAL_CANDLE_RETENTION_DAYS"], 8, 1, 30),
    // 1m collector candles feed the forward engine (#239): 28-day seasonal profile + EWMA
    // warm-up + 30-day signal history. Enforced by record_collector_candles (31..90).
    minuteCandleRetentionDays: boundedInteger(
      env["OPERATIONAL_MINUTE_CANDLE_RETENTION_DAYS"],
      62,
      31,
      90,
    ),
    monitorRunRetentionDays: boundedInteger(
      env["OPERATIONAL_MONITOR_RUN_RETENTION_DAYS"],
      30,
      1,
      90,
    ),
    outboxMaxAttempts: boundedInteger(env["OPERATIONAL_OUTBOX_MAX_ATTEMPTS"], 10, 1, 100),
  };
}
