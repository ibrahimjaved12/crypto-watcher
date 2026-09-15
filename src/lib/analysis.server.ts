import type { SupabaseClient } from "@supabase/supabase-js";
import type { Database } from "@/integrations/supabase/types";
import { analysisInput, analysisResponse, type AnalysisReply } from "./analysis.contract";

type Env = Record<string, string | undefined>;
type Client = Pick<SupabaseClient<Database>, "from">;

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
  try {
    const response = await send(config.url, {
      method: "POST",
      redirect: "error",
      cache: "no-store",
      signal: controller.signal,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${config.token}` },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const error =
        response.status === 504
          ? "Python analysis timed out. Please retry."
          : response.status === 422
            ? "Your saved monitoring state was rejected by Python. Check your settings and try again."
            : [401, 403].includes(response.status)
              ? "Python service authentication failed. Its server configuration needs checking."
              : "The Python analysis service is unavailable. Please retry later.";
      return { ok: false, error };
    }
    const raw = await response.text();
    if (raw.length > 128_000) throw new Error("oversized response");
    const parsed = analysisResponse.safeParse(JSON.parse(raw));
    if (!parsed.success || parsed.data.symbol !== symbol) {
      return { ok: false, error: "The Python service returned an invalid analysis response." };
    }
    return { ok: true, analysis: parsed.data };
  } catch {
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
