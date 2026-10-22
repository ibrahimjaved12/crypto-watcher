export const RUNNER_HEALTH_FRESH_MS = 60_000;

export type RunnerHealthRecord = {
  service: string;
  status: string;
  services: unknown;
  services_seen?: unknown;
  updated_at: string;
  last_seen_at: string | null;
};

export type GatewayReason = "ok" | "timeout" | "redirect" | "http_error" | "network" | "config";
/** Result of the Worker-to-Funnel `/health` check. */
export type GatewayCheck = { reachable: boolean; reason: GatewayReason; status?: number; ms: number };

export type RunnerStatus = {
  configured: boolean;
  jobsEnabled: boolean;
  state: "online" | "degraded" | "offline" | "unknown" | "disabled";
  online: boolean | null;
  lastOnlineAt: number | null;
  servicesLastOnlineAt: {
    analysis: number | null;
    alerts: number | null;
    forward: number | null;
    jobs: number | null;
  };
  reason: "ok" | "never-reported" | "check-failed";
  gatewayReachable: boolean | null;
  /** Why the gateway check failed (or "ok"); null when it was not run. */
  gatewayReason: string | null;
  services: { analysis: boolean; alerts: boolean; forward: boolean; jobs: boolean };
};

export function unavailableRunnerStatus(configured: boolean, jobsEnabled = false): RunnerStatus {
  return {
    configured,
    jobsEnabled,
    state: configured ? "unknown" : "disabled",
    online: null,
    lastOnlineAt: null,
    servicesLastOnlineAt: { analysis: null, alerts: null, forward: null, jobs: null },
    reason: configured ? "check-failed" : "ok",
    gatewayReachable: null,
    gatewayReason: null,
    services: { analysis: false, alerts: false, forward: false, jobs: false },
  };
}

/** Machine liveness comes from hosted heartbeats; a tunnel failure only blocks gateway actions. */
export function resolveRunnerStatus(
  records: readonly RunnerHealthRecord[],
  now: number,
  {
    gatewayEnabled,
    jobsEnabled,
    gatewayReachable,
    gatewayReason = null,
  }: {
    gatewayEnabled: boolean;
    jobsEnabled: boolean;
    gatewayReachable: boolean | null;
    gatewayReason?: string | null;
  },
): RunnerStatus {
  const configured = gatewayEnabled || jobsEnabled;
  const result = unavailableRunnerStatus(configured, jobsEnabled);
  if (!configured) return result;
  result.gatewayReachable = gatewayReachable;
  result.gatewayReason = gatewayReason;
  const valid = (row: RunnerHealthRecord) =>
    ["online", "degraded", "offline"].includes(row.status) &&
    Number.isFinite(Date.parse(row.updated_at)) &&
    Date.parse(row.updated_at) <= now + 30_000;
  const host = records.find((row) => row.service === "worker-host" && valid(row));
  const jobs = records.find((row) => row.service === "runner-jobs" && valid(row));
  const fresh = (row: RunnerHealthRecord | undefined) => {
    if (!row || !["online", "degraded", "offline"].includes(row.status)) return false;
    const age = now - Date.parse(row.updated_at);
    return Number.isFinite(age) && age >= -30_000 && age <= RUNNER_HEALTH_FRESH_MS;
  };
  const seen = [host, jobs]
    .map((row) => (row?.last_seen_at ? Date.parse(row.last_seen_at) : NaN))
    .filter((time) => Number.isFinite(time) && time <= now + 30_000);
  result.lastOnlineAt = seen.length ? Math.max(...seen) : null;
  result.reason = (!host && !jobs) || result.lastOnlineAt === null ? "never-reported" : "ok";
  const serviceSeen = (row: RunnerHealthRecord | undefined, name: string): number | null => {
    const seen = row?.services_seen;
    if (!seen || typeof seen !== "object" || Array.isArray(seen)) return null;
    const value = (seen as Record<string, unknown>)[name];
    const time = typeof value === "string" ? Date.parse(value) : NaN;
    return Number.isFinite(time) && time <= now + 30_000 ? time : null;
  };
  const together = (first: number | null, second: number | null) =>
    first !== null && second !== null ? Math.min(first, second) : null;
  const collectorSeen = serviceSeen(host, "collector");
  const analysisSeen = together(serviceSeen(host, "fastapi-analysis"), collectorSeen);
  result.servicesLastOnlineAt = {
    analysis: analysisSeen,
    alerts: analysisSeen,
    forward: together(serviceSeen(host, "fastapi-forward"), collectorSeen),
    jobs: serviceSeen(jobs, "runner-jobs"),
  };
  // A jobs heartbeat can still establish liveness if the supervisor has failed.
  const hostAlive = fresh(host) && host?.status !== "offline";
  // A final supervisor report follows its children exiting. Ignore older, unexpired jobs reports.
  const hostStoppedAfterJobs =
    fresh(host) &&
    host?.status === "offline" &&
    (!jobs || Date.parse(host.updated_at) >= Date.parse(jobs.updated_at));
  const jobsAlive = !hostStoppedAfterJobs && fresh(jobs) && jobs?.status !== "offline";
  if (!hostAlive && !jobsAlive) {
    if (host || jobs) {
      // Reachability proves the machine is present, but never establishes worker readiness.
      result.state = gatewayReachable === true ? "degraded" : "offline";
      result.online = gatewayReachable === true;
    }
    return result;
  }
  result.online = true;
  const children =
    hostAlive &&
    host?.services &&
    typeof host.services === "object" &&
    !Array.isArray(host.services)
      ? (host.services as Record<string, unknown>)
      : {};
  const running = (name: string) => children[name] === true;
  const pythonReady = running("fastapi-analysis") && running("fastapi-forward");
  const collectorReady = running("collector");
  result.services = {
    analysis:
      running("fastapi-analysis") &&
      collectorReady &&
      (!gatewayEnabled || gatewayReachable === true),
    alerts:
      running("fastapi-analysis") &&
      collectorReady &&
      (!gatewayEnabled || gatewayReachable === true),
    forward: pythonReady && collectorReady,
    jobs: jobsAlive && jobs?.status === "online" && running("runner-jobs"),
  };
  const required = [
    "fastapi-analysis",
    "fastapi-forward",
    "collector",
    "forward-hourly",
    "forward-trend",
  ];
  const healthy =
    hostAlive &&
    host?.status === "online" &&
    required.every(running) &&
    Object.values(children).every((value) => value === true) &&
    (!jobsEnabled || result.services.jobs) &&
    (!gatewayEnabled || gatewayReachable === true);
  result.state = healthy ? "online" : "degraded";
  return result;
}
