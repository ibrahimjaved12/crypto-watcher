import { validateGatewayEnv } from "./runner/gateway-core";
import { validatePublicSecrets, validateServerSupabase } from "./environment";
import { operationalDbConfig } from "./operational/config.server";
import { pythonServiceConfig } from "./python-service.server";

// Pure logic of the worker-host supervisor (#270, scripts/worker-host.mjs): preflight validation,
// restart backoff, crash-loop accounting and the status file. No Node imports and no clock reads:
// callers pass the time and the file operations, so tests run without processes, network or timers.
// Messages never contain configuration values, only variable names.

type Env = Record<string, string | undefined>;

const LOOPBACK_HOSTS = ["localhost", "127.0.0.1", "[::1]"];

export const STATUS_FILE_NAME = ".worker-host.status.json";
/** The status file is rewritten at least this often while the supervisor lives. */
export const STATUS_WRITE_INTERVAL_MS = 15_000;
/** `mvp:check` calls the file fresh when it was updated within this long. */
export const STATUS_FRESH_MS = 120_000;
/** `mvp:check` fails when a child has been down this long. */
export const DOWN_FAIL_MS = 3_600_000;
export const ESSENTIAL_CHILD = "collector";
export const FORWARD_CHILDREN = ["forward-hourly", "forward-trend"] as const;
/** Every child down for this long makes the supervisor exit. */
export const ALL_DOWN_GRACE_MS = 60_000;
export const SHUTDOWN_HARD_DEADLINE_MS = 15_000;

// ── Preflight ──────────────────────────────────────────────────────────────────────────────────

function parse(raw: string | undefined): URL | null {
  try {
    return new URL(String(raw));
  } catch {
    return null;
  }
}

/** An `http://<loopback>:<port>` origin: the only shape the host can start a FastAPI service on. */
function loopbackHttpOrigin(raw: string | undefined): URL | null {
  const url = parse(raw);
  if (
    !url ||
    url.protocol !== "http:" ||
    !LOOPBACK_HOSTS.includes(url.hostname) ||
    !url.port ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  )
    return null;
  return url;
}

function attempt(problems: string[], run: () => void) {
  try {
    run();
  } catch (error) {
    problems.push(error instanceof Error ? error.message : "invalid configuration");
  }
}

/** Names the host's own file must define: Vite would otherwise fill a missing one from `.env.local`. */
export const REQUIRED_ENV_FILE_NAMES = [
  "APP_PROFILE",
  "ALLOW_HOSTED_SUPABASE",
  "SUPABASE_URL",
  "SUPABASE_SERVICE_ROLE_KEY",
  "OPERATIONAL_DB_ENABLED",
  "OPERATIONAL_SUPABASE_URL",
  "OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY",
  "BINANCE_COLLECTOR_ENABLED",
  "PYTHON_ANALYSIS_ENABLED",
  "PYTHON_ANALYSIS_URL",
  "PYTHON_FORWARD_URL",
  "PYTHON_ANALYSIS_TOKEN",
] as const;

/** Names assigned in dotenv text (`NAME=…`, optional `export`); comments and values are ignored. */
export function envFileNames(text: string): Set<string> {
  const names = new Set<string>();
  for (const line of text.split(/\r?\n/)) {
    const match = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=/.exec(line);
    if (match) names.add(match[1]!);
  }
  return names;
}

export function missingEnvFileNames(text: string): string[] {
  const present = envFileNames(text);
  return REQUIRED_ENV_FILE_NAMES.filter((name) => !present.has(name));
}

/**
 * Everything that must hold before the worker host starts anything. Returns the list of problems
 * (empty = go). `envFileText` is the content of `.env.production.local` (null when it does not
 * exist); `env` is the merged production-mode environment the children will see. The existing validators are reused: their
 * messages carry variable names only, never values.
 */
export function preflight(input: { env: Env; envFileText: string | null }): string[] {
  const { env, envFileText } = input;
  if (envFileText === null) {
    return [
      ".env.production.local is missing. Run npm run env:production, then configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (see docs/worker-host.md).",
    ];
  }
  const problems: string[] = [];
  for (const [name, value] of Object.entries(env)) {
    if (name === "RUNNER_GATEWAY_SECRET" && env["RUNNER_GATEWAY_ENABLED"] !== "true") continue;
    if (value?.startsWith("REPLACE_"))
      problems.push(`${name} must be configured; placeholder values are not allowed.`);
  }
  const missing = missingEnvFileNames(envFileText);
  if (env["RUNNER_JOBS_ENABLED"] === "true") {
    // Same hosted main credentials as the scheduled forward jobs; no gateway key is needed.
    for (const name of ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"])
      if (!env[name]?.trim()) problems.push(`${name} is required for runner jobs.`);
  }
  if (env["RUNNER_GATEWAY_ENABLED"] === "true") {
    problems.push(...validateGatewayEnv(env));
    const present = envFileNames(envFileText);
    for (const name of ["RUNNER_GATEWAY_ENABLED", "RUNNER_GATEWAY_PORT", "RUNNER_GATEWAY_SECRET", "SUPABASE_PUBLISHABLE_KEY"])
      if (!present.has(name)) missing.push(name);
  }
  if (missing.length)
    problems.push(
      `.env.production.local must define these names itself (a name missing there would silently take its value from .env.local, the local stack): ${missing.join(", ")}.`,
    );

  if (env["APP_PROFILE"] !== "production") problems.push("APP_PROFILE must be production.");
  const main = parse(env["SUPABASE_URL"]);
  if (!main || main.protocol !== "https:" || LOOPBACK_HOSTS.includes(main.hostname))
    problems.push("SUPABASE_URL must be the hosted main project: an HTTPS, non-loopback origin.");
  if (env["ALLOW_HOSTED_SUPABASE"] !== "true") problems.push("ALLOW_HOSTED_SUPABASE must be exactly true.");
  attempt(problems, () => void validateServerSupabase(env));
  attempt(problems, () => validatePublicSecrets(env));

  attempt(problems, () => {
    const operational = operationalDbConfig(env);
    if (!operational.enabled) throw new Error("OPERATIONAL_DB_ENABLED must be true.");
    const url = new URL(operational.url);
    if (!LOOPBACK_HOSTS.includes(url.hostname) && url.protocol !== "https:")
      throw new Error("OPERATIONAL_SUPABASE_URL must be loopback or HTTPS.");
  });
  if (env["BINANCE_COLLECTOR_ENABLED"] !== "true")
    problems.push("BINANCE_COLLECTOR_ENABLED must be true (the collector child would otherwise exit at once).");

  if (env["PYTHON_ANALYSIS_ENABLED"] !== "true") problems.push("PYTHON_ANALYSIS_ENABLED must be true.");
  // Token rule from the application's own validator, evaluated as if enabled so a missing token is
  // reported even when the enabled flag is also wrong.
  try {
    pythonServiceConfig("/v1/movement/boundary", { ...env, PYTHON_ANALYSIS_ENABLED: "true" });
  } catch (error) {
    if (error instanceof Error && error.message === "token")
      problems.push("PYTHON_ANALYSIS_TOKEN is missing or is not 32–256 URL-safe characters.");
  }
  const analysis = loopbackHttpOrigin(env["PYTHON_ANALYSIS_URL"]);
  if (!analysis)
    problems.push(
      "PYTHON_ANALYSIS_URL must be a loopback HTTP origin with an explicit port; the host starts FastAPI itself.",
    );
  const forwardRaw = env["PYTHON_FORWARD_URL"];
  if (!forwardRaw) {
    problems.push(
      "PYTHON_FORWARD_URL is not set, so forward requests would use PYTHON_ANALYSIS_URL. Set it to a separate loopback port.",
    );
  } else {
    const forward = loopbackHttpOrigin(forwardRaw);
    const sameOrigin = parse(forwardRaw)?.origin === parse(env["PYTHON_ANALYSIS_URL"])?.origin;
    if (sameOrigin)
      problems.push("PYTHON_FORWARD_URL must differ from PYTHON_ANALYSIS_URL (a separate service on its own port).");
    else if (!forward)
      problems.push("PYTHON_FORWARD_URL must be a loopback HTTP origin with an explicit port.");
    else if (analysis && forward.port === analysis.port)
      problems.push("PYTHON_FORWARD_URL needs a different port from PYTHON_ANALYSIS_URL.");
  }
  problems.push(...monitorSchedulerProblems(env));
  return problems;
}

// ── Monitor scheduler (#30) ────────────────────────────────────────────────────────────────────

export const MONITOR_SCHEDULER_DEFAULT_INTERVAL_SECONDS = 60;
export const MONITOR_SCHEDULER_MIN_INTERVAL_SECONDS = 30;
export const MONITOR_SCHEDULER_MAX_INTERVAL_SECONDS = 3600;
/** A pass starts this long after the boundary, so the 1m candle that just closed has settled. */
export const MONITOR_SCHEDULER_SETTLE_MS = 8_000;
/** Wait after a failed pass before the next attempt. */
export const MONITOR_SCHEDULER_ERROR_BACKOFF_MS = 30_000;

function monitorSchedulerIntervalSeconds(env: Env): number | null {
  const raw = env["MONITOR_SCHEDULER_INTERVAL_SECONDS"];
  const value = raw === undefined || raw.trim() === "" ? MONITOR_SCHEDULER_DEFAULT_INTERVAL_SECONDS : Number(raw);
  return Number.isSafeInteger(value) &&
    value >= MONITOR_SCHEDULER_MIN_INTERVAL_SECONDS &&
    value <= MONITOR_SCHEDULER_MAX_INTERVAL_SECONDS
    ? value
    : null;
}

function monitorSchedulerProblems(env: Env): string[] {
  const problems: string[] = [];
  const enabled = env["MONITOR_SCHEDULER_ENABLED"];
  if (enabled !== undefined && enabled !== "" && enabled !== "true" && enabled !== "false")
    problems.push("MONITOR_SCHEDULER_ENABLED must be true or false.");
  if (monitorSchedulerIntervalSeconds(env) === null)
    problems.push(
      `MONITOR_SCHEDULER_INTERVAL_SECONDS must be a whole number from ${MONITOR_SCHEDULER_MIN_INTERVAL_SECONDS} to ${MONITOR_SCHEDULER_MAX_INTERVAL_SECONDS}.`,
    );
  return problems;
}

/**
 * Whether the host starts `monitor-scheduler`: hosted main credentials, the operational database, and
 * MONITOR_SCHEDULER_ENABLED not "false". Scheduled checks belong to the machine; the Worker cron stays off.
 */
export function monitorSchedulerConfigured(env: Env): boolean {
  return (
    env["MONITOR_SCHEDULER_ENABLED"] !== "false" &&
    env["OPERATIONAL_DB_ENABLED"] === "true" &&
    !!env["SUPABASE_URL"]?.trim() &&
    !!env["SUPABASE_SERVICE_ROLE_KEY"]?.trim()
  );
}

/** The pass interval in milliseconds; throws with the variable name only when it is invalid. */
export function monitorSchedulerIntervalMs(env: Env): number {
  const seconds = monitorSchedulerIntervalSeconds(env);
  if (seconds === null) throw new Error(monitorSchedulerProblems(env).join(" "));
  return seconds * 1000;
}

/**
 * When the next pass starts. Passes sit on a grid of `step + settle` instants, where the step is the
 * interval for sub-minute intervals and one minute otherwise (longer intervals round up to whole
 * minutes, so every pass still follows a fresh 1m close). `previousTargetMs` is the grid instant the
 * last pass was scheduled for (null before the first pass); a pass that overran is followed by the next
 * grid instant, never by an immediate catch-up burst.
 */
export function nextMonitorPassAt(nowMs: number, previousTargetMs: number | null, intervalMs: number): number {
  const step = intervalMs >= 60_000 ? 60_000 : intervalMs;
  const earliest = previousTargetMs === null ? nowMs : Math.max(nowMs, previousTargetMs + intervalMs);
  const k = Math.ceil((earliest - MONITOR_SCHEDULER_SETTLE_MS) / step);
  return k * step + MONITOR_SCHEDULER_SETTLE_MS;
}

export type PythonEndpoint = { origin: string; host: string; port: string };

/** The two FastAPI listeners to start; call only after `preflight` returned no problems. */
export function pythonEndpoints(env: Env): { analysis: PythonEndpoint; forward: PythonEndpoint } {
  const endpoint = (raw: string | undefined): PythonEndpoint => {
    const url = loopbackHttpOrigin(raw);
    if (!url) throw new Error("Python URL is not a loopback HTTP origin with a port.");
    return { origin: url.origin, host: url.hostname === "[::1]" ? "::1" : url.hostname, port: url.port };
  };
  return { analysis: endpoint(env["PYTHON_ANALYSIS_URL"]), forward: endpoint(env["PYTHON_FORWARD_URL"]) };
}

/**
 * Same command as scripts/dev-local.mjs plus an explicit `--workers 1`: uvicorn would otherwise take
 * its worker count from WEB_CONCURRENCY, and movement sessions are process-local, so the movement
 * service must stay one process.
 */
export function uvicornArgs(endpoint: Pick<PythonEndpoint, "host" | "port">): string[] {
  return [
    "-m",
    "uvicorn",
    "market_analysis.api:app",
    "--host",
    endpoint.host,
    "--port",
    endpoint.port,
    "--workers",
    "1",
    "--no-access-log",
    "--limit-concurrency",
    "32",
  ];
}

/** FastAPI needs only its token; keep database credentials out of the Python processes. */
export function pythonChildEnv(env: Env): Env {
  const out: Env = {};
  for (const [name, value] of Object.entries(env)) {
    if (/SUPABASE|OPERATIONAL|SERVICE_ROLE|^VITE_/.test(name) || name === "WEB_CONCURRENCY") continue;
    out[name] = value;
  }
  return out;
}

// ── Restart policy ─────────────────────────────────────────────────────────────────────────────

export const RESTART_POLICY = {
  initialMs: 2_000,
  capMs: 300_000,
  /** A run at least this long resets the backoff. */
  stableMs: 600_000,
  /** `loopCrashes` crashes within `loopWindowMs` stop restarting that child. */
  loopWindowMs: 600_000,
  loopCrashes: 5,
} as const;

export type RestartPolicy = {
  readonly [K in keyof typeof RESTART_POLICY]: number;
};

export type RestartState = { backoffMs: number; crashes: number[] };

export const initialRestartState = (policy: RestartPolicy = RESTART_POLICY): RestartState => ({
  backoffMs: policy.initialMs,
  crashes: [],
});

export type RestartDecision =
  | { kind: "restart"; delayMs: number }
  | { kind: "crash-loop"; crashesInWindow: number };

/**
 * A child exited unexpectedly at `nowMs` after running since `startedAtMs`. Returns the new state
 * and whether to relaunch (after `delayMs`) or give up. The delay doubles per crash from
 * `initialMs` up to `capMs`; a run of `stableMs` or more resets it. A crash counts toward the
 * breaker while it is less than `loopWindowMs` old. With `neverGiveUp` the breaker is replaced by a
 * restart at the cap.
 */
export function nextRestart(
  state: RestartState,
  event: { nowMs: number; startedAtMs: number },
  policy: RestartPolicy = RESTART_POLICY,
  options: { neverGiveUp?: boolean } = {},
): { state: RestartState; decision: RestartDecision } {
  const stable = event.nowMs - event.startedAtMs >= policy.stableMs;
  const base = stable ? policy.initialMs : state.backoffMs;
  const crashes = [...state.crashes.filter((at) => event.nowMs - at < policy.loopWindowMs), event.nowMs];
  if (crashes.length >= policy.loopCrashes && options.neverGiveUp) {
    // Essential child (the collector): never a permanent stop, keep retrying at the cap.
    return { state: { backoffMs: policy.capMs, crashes }, decision: { kind: "restart", delayMs: policy.capMs } };
  }
  if (crashes.length >= policy.loopCrashes) {
    return { state: { backoffMs: base, crashes }, decision: { kind: "crash-loop", crashesInWindow: crashes.length } };
  }
  return {
    state: { backoffMs: Math.min(base * 2, policy.capMs), crashes },
    decision: { kind: "restart", delayMs: base },
  };
}

export function describeExit(code: number | null, signal: string | null): string {
  return signal ? `signal ${signal}` : `exit code ${code ?? "unknown"}`;
}

// ── Status file ────────────────────────────────────────────────────────────────────────────────

export type ChildRunState = "waiting" | "running" | "backoff" | "crash-loop" | "stopping" | "stopped";

export type ChildStatus = {
  name: string;
  state: ChildRunState;
  pid: number | null;
  restartCount: number;
  lastRestartAt: string | null;
  lastExit: string | null;
  nextRestartAt: string | null;
  /** When the child last stopped unexpectedly and has not since run for `stableMs`; else null. */
  downSince?: string | null;
};

export type WorkerHostStatus = {
  schema: 1;
  supervisorPid: number;
  startedAt: string;
  updatedAt: string;
  shuttingDown: boolean;
  children: ChildStatus[];
};

export function buildStatus(input: {
  supervisorPid: number;
  startedAtMs: number;
  nowMs: number;
  shuttingDown: boolean;
  children: readonly ChildStatus[];
}): WorkerHostStatus {
  return {
    schema: 1,
    supervisorPid: input.supervisorPid,
    startedAt: new Date(input.startedAtMs).toISOString(),
    updatedAt: new Date(input.nowMs).toISOString(),
    shuttingDown: input.shuttingDown,
    children: input.children.map((child) => ({ ...child })),
  };
}

export type StatusFs = {
  writeFile(path: string, data: string): void;
  rename(from: string, to: string): void;
  remove(path: string): void;
};

/** Write to a sibling temporary file, then rename over the target, so readers never see a partial file. */
export function writeStatusAtomic(fs: StatusFs, path: string, status: WorkerHostStatus, pid: number): void {
  const temporary = `${path}.${pid}.tmp`;
  try {
    fs.writeFile(temporary, `${JSON.stringify(status, null, 2)}\n`);
    fs.rename(temporary, path);
  } catch (error) {
    try {
      fs.remove(temporary);
    } catch {
      // nothing left to clean
    }
    throw error;
  }
}

export function parseStatus(raw: string): WorkerHostStatus | null {
  try {
    const value = JSON.parse(raw) as Partial<WorkerHostStatus> | null;
    if (
      !value ||
      value.schema !== 1 ||
      typeof value.updatedAt !== "string" ||
      !Number.isFinite(Date.parse(value.updatedAt)) ||
      typeof value.supervisorPid !== "number" ||
      !Array.isArray(value.children)
    )
      return null;
    return value as WorkerHostStatus;
  } catch {
    return null;
  }
}

export function isStatusFresh(status: WorkerHostStatus, nowMs: number): boolean {
  return Math.abs(nowMs - Date.parse(status.updatedAt)) <= STATUS_FRESH_MS;
}

/**
 * The `worker host` line of `mvp:check`. `raw` is the status file text, or null when it does not
 * exist. Missing = host not used (WARN); stale, unreadable or any child in a crash loop = FAIL;
 * fresh with all children running = PASS; fresh otherwise (a child waiting, backing off or
 * stopping) = WARN.
 */
export function classifyWorkerHost(
  raw: string | null,
  nowMs: number,
): { level: "PASS" | "WARN" | "FAIL"; detail: string } {
  if (raw === null)
    return { level: "WARN", detail: `no ${STATUS_FILE_NAME}; the worker host is not in use (npm run worker:host)` };
  const status = parseStatus(raw);
  if (!status || status.children.length === 0)
    return { level: "FAIL", detail: `${STATUS_FILE_NAME} is unreadable or lists no children` };
  const looping = status.children.filter((child) => child.state === "crash-loop").map((child) => child.name);
  const ageS = Math.round((nowMs - Date.parse(status.updatedAt)) / 1000);
  if (looping.length)
    return { level: "FAIL", detail: `crash loop, restarts stopped: ${looping.join(", ")}${isStatusFresh(status, nowMs) ? "" : ` (status also stale, ${ageS}s old)`}` };
  const downTooLong = status.children
    .filter((child) => child.downSince && nowMs - Date.parse(child.downSince) >= DOWN_FAIL_MS)
    .map((child) => child.name);
  if (downTooLong.length)
    return { level: "FAIL", detail: `down for more than an hour: ${downTooLong.join(", ")}` };
  if (!isStatusFresh(status, nowMs))
    return {
      level: "FAIL",
      detail:
        ageS < 0
          ? `status timestamp is ${-ageS}s in the future (check the clock/NTP)`
          : `status stale: last update ${ageS}s ago (host stopped or hung)`,
    };
  const notRunning = status.children.filter((child) => child.state !== "running");
  const restarts = status.children.reduce((sum, child) => sum + child.restartCount, 0);
  if (notRunning.length || status.shuttingDown)
    return {
      level: "WARN",
      detail: `not running: ${notRunning.map((child) => `${child.name} ${child.state}`).join(", ") || "host shutting down"}`,
    };
  return { level: "PASS", detail: `${status.children.length} children running, ${restarts} restart(s) since start, updated ${ageS}s ago` };
}

// ── Orphans and exit rules ─────────────────────────────────────────────────────────────────────

/**
 * Process ids a previous run left in the status file: the supervisor and every child that was
 * running. A file last written before the machine booted cannot refer to live processes of ours
 * (pids are reused), so it yields none. The caller checks which are alive.
 */
export function recordedPids(
  status: WorkerHostStatus | null,
  bootMs: number,
): { label: string; pid: number }[] {
  if (!status || Date.parse(status.updatedAt) < bootMs) return [];
  const out: { label: string; pid: number }[] = [];
  if (Number.isInteger(status.supervisorPid)) out.push({ label: "worker host", pid: status.supervisorPid });
  for (const child of status.children)
    if (typeof child.pid === "number" && Number.isInteger(child.pid)) out.push({ label: child.name, pid: child.pid });
  return out;
}

/**
 * When the supervisor must give up and exit non-zero (so systemd/pm2 can restart it cleanly): the
 * collector in a crash loop (the collector policy never reaches it; kept as a guard), both forward
 * jobs in a crash loop, or every child not running for `ALL_DOWN_GRACE_MS`. `allDownSinceMs` is the
 * caller-held tracker; pass back what this returns.
 */
export function exitDecision(
  children: readonly { name: string; state: ChildRunState }[],
  allDownSinceMs: number | null,
  nowMs: number,
): { reason: string | null; allDownSinceMs: number | null } {
  const state = (name: string) => children.find((child) => child.name === name)?.state;
  if (state(ESSENTIAL_CHILD) === "crash-loop")
    return { reason: `${ESSENTIAL_CHILD} is in a crash loop`, allDownSinceMs };
  if (FORWARD_CHILDREN.every((name) => state(name) === "crash-loop"))
    return { reason: "both forward jobs are in a crash loop", allDownSinceMs };
  if (children.length && children.every((child) => child.state !== "running")) {
    const since = allDownSinceMs ?? nowMs;
    if (nowMs - since >= ALL_DOWN_GRACE_MS)
      return { reason: `every child has been down for ${ALL_DOWN_GRACE_MS / 1000}s`, allDownSinceMs: since };
    return { reason: null, allDownSinceMs: since };
  }
  return { reason: null, allDownSinceMs: null };
}
