import { pythonServiceConfig } from "../python-service.server";
import {
  technicalAnalysisBatchResponse,
  type TechnicalAnalysisRequest,
  type TechnicalAnalysisResult,
} from "./python-contract";

const TIMEOUT_MS = 6_000;
const MAX_ATTEMPTS = 2;
const MAX_RESPONSE_BYTES = 256_000;

/** One bounded retry is allowed for network, timeout, rate-limit and 5xx failures. */
export async function calculateTechnicalBatch(
  requests: TechnicalAnalysisRequest[],
  env: Record<string, string | undefined> = process.env,
  send: typeof fetch = fetch,
): Promise<TechnicalAnalysisResult[]> {
  if (requests.length < 1 || requests.length > 8) throw new Error("Invalid Python TA batch size");
  let config: ReturnType<typeof pythonServiceConfig>;
  try {
    config = pythonServiceConfig("/v1/technical-analysis/batch", env);
  } catch {
    throw new Error("Python TA service is not configured");
  }
  const body = JSON.stringify({ schema_version: 1, requests });
  let lastFailure = "unavailable";
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const response = await send(config.url, {
        method: "POST",
        redirect: "manual",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
          Authorization: `Bearer ${config.token}`,
        },
        body,
      });
      if (!response.ok) {
        try {
          await response.body?.cancel();
        } catch {
          // Cleanup must not replace the service failure.
        }
        const retryable = response.status === 429 || response.status >= 500;
        if (attempt < MAX_ATTEMPTS && retryable) {
          lastFailure = `HTTP ${response.status}`;
          continue;
        }
        if (response.status >= 300 && response.status < 400) {
          throw new Error("Python TA redirect rejected");
        }
        if ([401, 403].includes(response.status))
          throw new Error("Python TA authentication failed");
        if (response.status === 422) throw new Error("Python TA request was rejected");
        throw new Error(`Python TA service failed (${response.status})`);
      }
      const raw = await response.text();
      if (new TextEncoder().encode(raw).byteLength > MAX_RESPONSE_BYTES) {
        throw new Error("Python TA response is oversized");
      }
      let decoded: unknown;
      try {
        decoded = JSON.parse(raw);
      } catch {
        throw new Error("Python TA returned an invalid response");
      }
      const parsed = technicalAnalysisBatchResponse.safeParse(decoded);
      if (!parsed.success || parsed.data.results.length !== requests.length) {
        throw new Error("Python TA returned an invalid response");
      }
      return parsed.data.results;
    } catch (error) {
      lastFailure = controller.signal.aborted
        ? "timed out"
        : error instanceof Error
          ? error.message
          : String(error);
      const permanent =
        /authentication|rejected|redirect|invalid response|oversized|failed \(4\d\d\)/.test(
          lastFailure,
        );
      if (attempt === MAX_ATTEMPTS || permanent) break;
    } finally {
      clearTimeout(timer);
    }
  }
  throw new Error(`Python TA ${lastFailure}`);
}
