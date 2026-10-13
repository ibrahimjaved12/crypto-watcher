import { useRunnerStatus } from "@/lib/runner/use-runner-status";
import { relativeTime } from "@/lib/labels";
import { cn } from "@/lib/utils";

export function RunnerStatusBadge() {
  const { data, available, isPending } = useRunnerStatus();
  if (data?.configured === false) return null;
  if (isPending) return <span className="text-xs text-muted-foreground">Connecting…</span>;

  return (
    <div role="status" className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs">
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-medium",
          available ? "border-bull/40 bg-bull/10 text-bull" : "border-bear/40 bg-bear/10 text-bear",
        )}
      >
        <span className="size-1.5 shrink-0 rounded-full bg-current" aria-hidden />
        <span className="sr-only">Runner </span>
        {available ? "Online" : "Offline"}
      </span>
      {!available && data?.lastOnlineAt != null && (
        <time
          className="whitespace-nowrap text-muted-foreground"
          dateTime={new Date(data.lastOnlineAt).toISOString()}
          title={new Date(data.lastOnlineAt).toLocaleString()}
        >
          Last online {relativeTime(data.lastOnlineAt).replace(/ (min|h|d)\b/g, "$1")}
        </time>
      )}
    </div>
  );
}

export function RunnerStatusNotice() {
  const { data, available, isPending } = useRunnerStatus();
  if (isPending || data?.configured === false || (available && data?.state === "online"))
    return null;
  let message =
    "Runner temporarily offline: Alerts, analysis and forward runs are temporarily down.";
  if (available && data?.state === "degraded") {
    const unavailable = [
      !data.services.analysis ? "Analysis" : null,
      !data.services.alerts ? "Alerts" : null,
      !data.services.forward ? "Forward runs" : null,
      data.jobsEnabled && !data.services.jobs ? "On-demand forward runs" : null,
    ].filter(Boolean);
    message = unavailable.length
      ? `Runner online: ${unavailable.join(", ")} temporarily unavailable.`
      : "Runner online: One or more background services are temporarily unavailable.";
    if (data.gatewayReachable === false && data.services.forward) {
      message += " The runner connection is unavailable; background runs can continue.";
    }
  }

  return (
    <div
      role="status"
      className="mb-6 rounded-lg border border-bear/30 bg-bear/5 px-4 py-3 text-sm leading-relaxed"
    >
      {message}
    </div>
  );
}
