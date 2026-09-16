import type { SupabaseClient } from "@supabase/supabase-js";
import type { Database } from "@/integrations/supabase/types";
import { analysisInput, analysisResponse, type AnalysisReply } from "./analysis.contract";

type Env = Record<string, string | undefined>;
type Client = Pick<SupabaseClient<Database>, "from">;
type AnalysisStage =
  "fetch" | "response-status" | "response-text" | "size-guard" | "json-parse" | "schema-validation";

function serviceConfig(env: Env) {
  if (env["PYTHON_ANALYSIS_ENABLED"] !== "true") throw new Error("disabled");
  const token = env["PYTHON_ANALYSIS_TOKEN"] ?? "";
  if (!/^[A-Za-z0-9_-]{32,256}$/.test(token)) throw new Error("token");
  const url = new URL(env["PYTHON_ANALYSIS_URL"] ?? "");
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (
    (url.protocol !== "https:" && !(url.protocol === "http:" && local)) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== "/"
  ) {
    throw new Error("url");
  }
  return { url: new URL("/v1/analysis", url).toString(), token };
}

function milliseconds(value: string | null) {
  if (value === null) return null;
  const time = Date.parse(value);
  if (!Number.isSafeInteger(time)) throw new Error("Invalid saved baseline time");
  return time;
}

function safeField(value: unknown, field: "name" | "code" | "message" | "cause") {
  try {
    if (typeof value !== "object" || value === null) return undefined;
    return (value as Record<string, unknown>)[field];
  } catch {
    return undefined;
  }
}

function sanitizedField(
  value: unknown,
  field: "name" | "code" | "message",
  sensitiveValues: string[],
) {
  const candidate = safeField(value, field);
  if (typeof candidate === "number") return candidate;
  if (typeof candidate !== "string") return undefined;
  return sensitiveValues.reduce(
    (sanitized, sensitive) =>
      sensitive.length ? sanitized.replaceAll(sensitive, "[redacted]") : sanitized,
    candidate,
  );
}

function logAnalysisError(
  stage: AnalysisStage,
  error: unknown,
  aborted: boolean,
  httpStatus: number | undefined,
  sensitiveValues: string[],
  safeMessage?: string,
) {
  const cause = safeField(error, "cause");
  console.error({
    stage,
    errorName: sanitizedField(error, "name", sensitiveValues) ?? "UnknownError",
    errorMessage:
      safeMessage ??
      sanitizedField(error, "message", sensitiveValues) ??
      "No error message available",
    causeName: sanitizedField(cause, "name", sensitiveValues),
    causeCode: sanitizedField(cause, "code", sensitiveValues),
    causeMessage: sanitizedField(cause, "message", sensitiveValues),
    aborted,
    httpStatus,
  });
}

/** Receives the verified middleware's user-scoped client, never an admin client.
 * Only SELECTs: do not reuse fetchWatchlist/fetchSettings, which can seed rows.
 */
export async function analyzeForUser(
  supabase: Client,
  userId: string,
  symbol: string,
  env: Env = process.env,
  send: typeof fetch = fetch,
): Promise<AnalysisReply> {
  if (!userId) return { ok: false, error: "Sign in to request analysis." };
  if (!analysisInput.safeParse({ symbol }).success) {
    return { ok: false, error: "Choose a supported pair from your watchlist." };
  }
  let config;
  try {
    config = serviceConfig(env);
  } catch {
    return {
      ok: false,
      error: "Python analysis is not enabled or its service configuration is incomplete.",
    };
  }

  const dbController = new AbortController();
  const dbTimer = setTimeout(() => dbController.abort(), 5_000);
  let payload;
  try {
    const watched = await supabase
      .from("watchlist_items")
      .select("symbol")
      .eq("user_id", userId)
      .eq("symbol", symbol)
      .abortSignal(dbController.signal)
      .maybeSingle();
    if (watched.error) throw new Error("watchlist unavailable");
    if (!watched.data) return { ok: false, error: "This pair is not in your watchlist." };
    const [settings, baseline] = await Promise.all([
      supabase
        .from("monitor_settings")
        .select("threshold_pct,cooldown_minutes,monitoring_enabled")
        .eq("user_id", userId)
        .abortSignal(dbController.signal)
        .maybeSingle(),
      supabase
        .from("monitor_baselines")
        .select(
          "baseline_price,baseline_at,data_source,threshold_pct,last_observed_at,last_up_alert_at,last_down_alert_at",
        )
        .eq("user_id", userId)
        .eq("symbol", symbol)
        .abortSignal(dbController.signal)
        .maybeSingle(),
    ]);
    if (settings.error || baseline.error) throw new Error("state unavailable");
    const b = baseline.data;
    payload = {
      schema_version: 1,
      symbol,
      settings: {
        threshold_pct: String(settings.data?.threshold_pct ?? 2),
        cooldown_minutes: settings.data?.cooldown_minutes ?? 15,
        monitoring_enabled: settings.data?.monitoring_enabled ?? true,
      },
      baseline: b
        ? {
            price: String(b.baseline_price),
            at_ms: milliseconds(b.baseline_at),
            source: b.data_source,
            threshold: String(b.threshold_pct),
            last_observed_ms: milliseconds(b.last_observed_at),
            last_up_alert_ms: milliseconds(b.last_up_alert_at),
            last_down_alert_ms: milliseconds(b.last_down_alert_at),
          }
        : null,
    };
  } catch {
    return {
      ok: false,
      error:
        "Could not read your monitoring settings or baseline. Please retry; the baseline database setup may need checking.",
    };
  } finally {
    clearTimeout(dbTimer);
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 25_000);
  let stage: AnalysisStage = "fetch";
  let responseStatus: number | undefined;
  let requestBody = "";
  try {
    requestBody = JSON.stringify(payload);
    const response = await send(config.url, {
      method: "POST",
      redirect: "error",
      signal: controller.signal,
      // RequestInit.cache can throw before network I/O in Worker compatibility modes.
      // This authenticated POST and Python's no-store response must remain uncached.
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        Authorization: `Bearer ${config.token}`,
      },
      body: requestBody,
    });
    stage = "response-status";
    responseStatus = response.status;
    if (!response.ok) {
      const error =
        responseStatus === 504
          ? "Python analysis timed out. Please retry."
          : responseStatus === 422
            ? "Your saved monitoring state was rejected by Python. Check your settings and try again."
            : [401, 403].includes(responseStatus)
              ? "Python service authentication failed. Its server configuration needs checking."
              : "The Python analysis service is unavailable. Please retry later.";
      return { ok: false, error };
    }
    stage = "response-text";
    const raw = await response.text();
    stage = "size-guard";
    if (raw.length > 128_000) throw new Error("oversized response");
    stage = "json-parse";
    const json = JSON.parse(raw);
    stage = "schema-validation";
    const parsed = analysisResponse.safeParse(json);
    if (!parsed.success || parsed.data.symbol !== symbol) {
      logAnalysisError(
        stage,
        new Error("invalid analysis response"),
        controller.signal.aborted,
        responseStatus,
        [],
        "Python analysis response failed validation",
      );
      return { ok: false, error: "The Python service returned an invalid analysis response." };
    }
    return { ok: true, analysis: parsed.data };
  } catch (error) {
    const sensitiveValues = [
      ...Object.values(env).filter((value): value is string => typeof value === "string"),
      `Bearer ${config.token}`,
      config.token,
      userId,
      requestBody,
      payload.baseline === null ? "" : JSON.stringify(payload.baseline),
    ].sort((a, b) => b.length - a.length);
    const safeMessage =
      stage === "json-parse"
        ? "Python analysis response was not valid JSON"
        : stage === "schema-validation"
          ? "Python analysis response failed validation"
          : undefined;
    logAnalysisError(
      stage,
      error,
      controller.signal.aborted,
      responseStatus,
      sensitiveValues,
      safeMessage,
    );
    return {
      ok: false,
      error: controller.signal.aborted
        ? "Python analysis timed out. Please retry."
        : "Could not reach the Python analysis service or read its response. Please retry later.",
    };
  } finally {
    clearTimeout(timer);
  }
}
