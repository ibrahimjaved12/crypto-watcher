import type { SupabaseClient } from "@supabase/supabase-js";
import type { Database } from "@/integrations/supabase/types";
import { analysisInput, analysisResponse, type AnalysisReply } from "./analysis.contract";
import { pythonServiceConfig } from "./python-service.server";

type Client = Pick<SupabaseClient<Database>, "from">;

type PythonAnalysisStage =
  | "fetch"
  | "response-status"
  | "response-text"
  | "size-guard"
  | "json-parse"
  | "schema-validation";

function pythonAnalysisDiagnostic(stage: PythonAnalysisStage, status?: number) {
  // Never pass exception details, request state, service configuration or bodies.
  try {
    console.error("[python-analysis]", status === undefined ? { stage } : { stage, status });
  } catch {
    // Diagnostics must not alter the safe failure returned to the caller.
  }
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
  env: Record<string, string | undefined> = process.env,
  send: typeof fetch = fetch,
): Promise<AnalysisReply> {
  if (!userId) return { ok: false, error: "Sign in to request analysis.", category: "auth" };
  if (!analysisInput.safeParse({ symbol }).success) {
    return {
      ok: false,
      error: "Choose a supported pair from your watchlist.",
      category: "invalid_contract",
    };
  }
  let config;
  try {
    config = pythonServiceConfig("/v1/analysis", env);
  } catch {
    return {
      ok: false,
      error: "Python analysis is not enabled or its service configuration is incomplete.",
      category: "configuration",
    };
  }

  const dbController = new AbortController();
  const dbTimer = setTimeout(() => dbController.abort(), 5_000);
  let payload;
  let expectedInstrumentId = "";
  try {
    const watched = await supabase
      .from("watchlist_items")
      .select("symbol,instrument_id")
      .eq("user_id", userId)
      .eq("symbol", symbol)
      .abortSignal(dbController.signal)
      .maybeSingle();
    if (watched.error) throw new Error("watchlist unavailable");
    if (!watched.data)
      return {
        ok: false,
        error: "This pair is not in your watchlist.",
        category: "not_watched",
      };
    expectedInstrumentId = watched.data.instrument_id;
    const [settings, baseline] = await Promise.all([
      supabase
        .from("monitor_settings")
        .select(
          "threshold_pct,cooldown_minutes,monitoring_enabled,market_data_collection_enabled,movement_alerts_enabled",
        )
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
      instrument_id: watched.data.instrument_id,
      settings: {
        threshold_pct: String(settings.data?.threshold_pct ?? 2),
        cooldown_minutes: settings.data?.cooldown_minutes ?? 15,
        // Schema v1 has one eligibility switch. Collapse the three relevant
        // controls so the read-only preview cannot claim alert eligibility while
        // collection or movement-alert generation is paused.
        monitoring_enabled:
          (settings.data?.monitoring_enabled ?? true) &&
          (settings.data?.market_data_collection_enabled ?? true) &&
          (settings.data?.movement_alerts_enabled ?? true),
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
      category: "database",
    };
  } finally {
    clearTimeout(dbTimer);
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 25_000);
  const requestBody = JSON.stringify(payload);
  const requestBytes = new TextEncoder().encode(requestBody).byteLength;
  const startedAt = Date.now();
  let failureStage: PythonAnalysisStage = "fetch";
  let status: number | undefined;
  try {
    const response = await send(config.url, {
      method: "POST",
      // Workers support manual redirects; never forward the service token to Location.
      redirect: "manual",
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
    failureStage = "response-status";
    const responseStatus = response.status;
    if (Number.isInteger(responseStatus) && responseStatus >= 0 && responseStatus <= 599) {
      status = responseStatus;
    }
    if (!response.ok) {
      // Release unused bodies without letting cleanup replace the original failure.
      try {
        await response.body?.cancel();
      } catch {
        // The runtime may already have canceled the response.
      }
      if (responseStatus >= 300 && responseStatus < 400) {
        throw new Error("Python analysis service redirect rejected");
      }
      const [error, category] =
        responseStatus === 504
          ? ["Python analysis timed out. Please retry.", "timeout"]
          : responseStatus === 422
            ? [
                "Your saved monitoring state was rejected by Python. Check your settings and try again.",
                "invalid_request",
              ]
            : [401, 403].includes(responseStatus)
              ? [
                  "Python service authentication failed. Its server configuration needs checking.",
                  "service_auth",
                ]
              : ["The Python analysis service is unavailable. Please retry later.", "service"];
      pythonAnalysisDiagnostic(failureStage, status);
      return { ok: false, error, category };
    }
    failureStage = "response-text";
    const raw = await response.text();
    failureStage = "size-guard";
    const responseBytes = new TextEncoder().encode(raw).byteLength;
    if (responseBytes > 128_000) throw new Error("oversized response");
    failureStage = "json-parse";
    const data = JSON.parse(raw);
    failureStage = "schema-validation";
    const parsed = analysisResponse.safeParse(data);
    if (
      !parsed.success ||
      parsed.data.symbol !== symbol ||
      parsed.data.instrument.id !== expectedInstrumentId ||
      parsed.data.instrument.native_symbol !== symbol
    ) {
      pythonAnalysisDiagnostic(failureStage, status);
      return {
        ok: false,
        error: "The Python service returned an invalid analysis response.",
        category: "invalid_response",
      };
    }
    return {
      ok: true,
      analysis: parsed.data,
      metrics: {
        duration_ms: Math.max(0, Date.now() - startedAt),
        request_bytes: requestBytes,
        response_bytes: responseBytes,
      },
    };
  } catch {
    pythonAnalysisDiagnostic(failureStage, status);
    return {
      ok: false,
      error: controller.signal.aborted
        ? "Python analysis timed out. Please retry."
        : "Could not reach the Python analysis service or read its response. Please retry later.",
      category: controller.signal.aborted ? "timeout" : "network_or_response",
    };
  } finally {
    clearTimeout(timer);
  }
}
