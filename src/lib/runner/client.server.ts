type Env = Record<string, string | undefined>;

export class RunnerConfigurationError extends Error {
  readonly category = "configuration";

  constructor(
    readonly variable: "RUNNER_GATEWAY_ENABLED" | "RUNNER_GATEWAY_URL" | "RUNNER_GATEWAY_SECRET",
  ) {
    super(`Runner gateway is misconfigured (${variable}).`);
    this.name = "RunnerConfigurationError";
  }
}

export function runnerConfig(env: Env = process.env) {
  if (env["RUNNER_GATEWAY_ENABLED"] !== "true") return null;

  let origin: URL;
  try {
    origin = new URL(env["RUNNER_GATEWAY_URL"] ?? "");
  } catch {
    throw new RunnerConfigurationError("RUNNER_GATEWAY_URL");
  }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(origin.hostname);
  if (
    (origin.protocol !== "https:" && !(origin.protocol === "http:" && local)) ||
    origin.username ||
    origin.password ||
    origin.search ||
    origin.hash ||
    origin.pathname !== "/"
  ) {
    throw new RunnerConfigurationError("RUNNER_GATEWAY_URL");
  }

  const secret = env["RUNNER_GATEWAY_SECRET"] ?? "";
  if (!/^[A-Za-z0-9_-]{32,256}$/.test(secret)) {
    throw new RunnerConfigurationError("RUNNER_GATEWAY_SECRET");
  }
  return { url: origin.origin, secret };
}

type RunnerFailureCategory =
  | "configuration"
  | "runner_offline"
  | "timeout"
  | "unauthorized"
  | "rate_limited"
  | "busy"
  | "invalid_response"
  | "service";

type RunnerFailure = { ok: false; category: RunnerFailureCategory; error: string };
type RunnerReply<T> = { ok: true; data: T } | RunnerFailure;

/** A response status is the fetch API's evidence that the gateway was reached. */
export function classifyRunnerFailure(status: number | null, aborted: boolean): RunnerFailure {
  if (status === null && aborted) {
    return {
      ok: false,
      category: "timeout",
      error: "The runner did not respond in time. Please try again shortly.",
    };
  }
  if (status === null || [502, 503, 504, 522, 523, 524, 530].includes(status)) {
    return {
      ok: false,
      category: "runner_offline",
      error: "Runner temporarily offline. Please try again when it is back online.",
    };
  }
  if (aborted) {
    return { ok: false, category: "timeout", error: "The runner took too long. Try again." };
  }
  if (status === 401) {
    return {
      ok: false,
      category: "unauthorized",
      error: "Runner authorization failed. Sign in and try again.",
    };
  }
  if (status === 429) {
    return {
      ok: false,
      category: "rate_limited",
      error: "Too many runner requests. Try again shortly.",
    };
  }
  if (status === 409) {
    return { ok: false, category: "busy", error: "A check is already running." };
  }
  return {
    ok: false,
    category: "service",
    error: "The runner could not complete the request. Try again.",
  };
}

const invalidResponse: RunnerFailure = {
  ok: false,
  category: "invalid_response",
  error: "The runner returned an invalid response. Try again.",
};

export async function callRunner<T = unknown>(
  path: string,
  accessToken: string,
  body: unknown,
  { timeoutMs = 25_000, method = "POST" }: { timeoutMs?: number; method?: "POST" | "GET" } = {},
): Promise<RunnerReply<T>> {
  let config;
  try {
    config = runnerConfig();
    if (config === null) throw new RunnerConfigurationError("RUNNER_GATEWAY_ENABLED");
  } catch (error) {
    if (error instanceof RunnerConfigurationError) {
      return { ok: false, category: error.category, error: error.message };
    }
    throw error;
  }

  const signal = AbortSignal.timeout(timeoutMs);
  let status: number | null = null;
  try {
    const response = await fetch(new URL(path, config.url), {
      method,
      redirect: "manual",
      signal,
      headers: {
        "Content-Type": "application/json",
        "x-runner-secret": config.secret,
        Authorization: `Bearer ${accessToken}`,
      },
      ...(method === "POST" ? { body: JSON.stringify(body) } : {}),
    });
    status = response.status;
    const contentType = response.headers.get("Content-Type")?.split(";")[0]?.trim().toLowerCase();
    if (!response.ok || contentType !== "application/json") {
      // Never expose proxy HTML, redirect bodies, or upstream exception text.
      try {
        await response.body?.cancel();
      } catch {
        // Already canceled by the runtime.
      }
      if (response.ok) return invalidResponse;
      const failure = classifyRunnerFailure(status, false);
      if (status === 429) {
        const retryAfter = response.headers.get("Retry-After");
        if (retryAfter && /^\d+$/.test(retryAfter)) {
          const seconds = Number(retryAfter);
          if (Number.isSafeInteger(seconds)) {
            failure.error = `Too many runner requests. Try again in ${seconds} seconds.`;
          }
        }
      }
      return failure;
    }
    return { ok: true, data: (await response.json()) as T };
  } catch (error) {
    const aborted = signal.aborted || (error instanceof Error && error.name === "AbortError");
    if (aborted) return classifyRunnerFailure(status, true);
    if (error instanceof SyntaxError) return invalidResponse;
    return classifyRunnerFailure(null, false);
  }
}
