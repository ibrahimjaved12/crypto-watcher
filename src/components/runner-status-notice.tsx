import { useRunnerStatus } from "@/lib/runner/use-runner-status";
import { cn } from "@/lib/utils";

export function RunnerStatusBadge() {
  const { data } = useRunnerStatus();
  if (!data?.configured) return null;
  const label = {
    online: "Online",
    degraded: "Degraded",
    offline: "Offline",
    unknown: "Status unavailable",
    disabled: "",
  }[data.state];

  return (
    <div role="status" className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
      <span
        className={cn(
          "rounded-full border px-2 py-0.5 font-medium",
          data.state === "online"
            ? "border-bull/40 bg-bull/10 text-bull"
            : data.state === "offline"
              ? "border-bear/40 bg-bear/10 text-bear"
              : "border-warn/40 bg-warn/10 text-warn",
        )}
      >
        <span className="sr-only">Runner </span>
        {label}
      </span>
      {data.state === "offline" && data.lastOnlineAt !== null && (
        <span
          className="whitespace-nowrap text-muted-foreground"
          title={new Date(data.lastOnlineAt).toLocaleString()}
        >
          Last online {new Date(data.lastOnlineAt).toLocaleString()}
        </span>
      )}
    </div>
  );
}

export function RunnerStatusNotice() {
  const { data } = useRunnerStatus();
  if (!data?.configured || data.state === "online") return null;
  let message = "Runner health is unavailable. Service readiness could not be confirmed.";
  if (data.state === "offline") {
    message = "Runner temporarily offline: Alerts, analysis and forward runs are temporarily down.";
  } else if (data.state === "degraded") {
    const unavailable = [
      !data.services.analysis ? "Analysis" : null,
      !data.services.alerts ? "Alerts" : null,
      !data.services.forward ? "Forward runs" : null,
      data.jobsEnabled && !data.services.jobs ? "On-demand forward runs" : null,
    ].filter(Boolean);
    message = unavailable.length
      ? `Runner services degraded: ${unavailable.join(", ")} temporarily unavailable.`
      : "Runner services degraded: One or more background services are unavailable.";
    if (data.gatewayReachable === false && data.services.forward) {
      message += " The runner connection is unavailable; background runs can continue.";
    }
  }

  return (
    <div role="status" className="mb-6 rounded-lg border border-warn/40 bg-warn/10 p-3 text-sm">
      {message}
    </div>
  );
}
