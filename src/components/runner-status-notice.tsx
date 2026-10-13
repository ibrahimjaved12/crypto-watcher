import { useRunnerStatus } from "@/lib/runner/use-runner-status";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { formatAgo } from "@/lib/labels";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

const RUNNER_SERVICES = [
  { key: "analysis", name: "Analysis" },
  { key: "alerts", name: "Alerts" },
  { key: "forward", name: "Forward runs" },
  { key: "jobs", name: "On-demand jobs" },
] as const;

export function RunnerStatusBadge() {
  const { data, available, checkFailed, isPending } = useRunnerStatus();
  const now = useNow();
  if (data?.configured === false) return null;
  if (isPending) return <span className="text-xs text-muted-foreground">Connecting…</span>;
  const online = available && data?.reason !== "never-reported";
  const label = checkFailed ? "Status unknown" : online ? "Online" : "Offline";

  return (
    <div role="status" className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs">
      <Popover>
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label={`Runner ${label.toLowerCase()}; show service history`}
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              checkFailed
                ? "border-border bg-muted text-muted-foreground"
                : online
                  ? "border-bull/40 bg-bull/10 text-bull"
                  : "border-bear/40 bg-bear/10 text-bear",
            )}
          >
            <span className="size-1.5 shrink-0 rounded-full bg-current" aria-hidden />
            <span className="sr-only">Runner </span>
            {label}
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          collisionPadding={16}
          className="w-80 max-w-[calc(100vw-2rem)] p-3"
        >
          <p className="mb-3 text-sm font-medium">Runner services</p>
          <ul className="space-y-3 text-xs">
            {RUNNER_SERVICES.filter(({ key }) => key !== "jobs" || data?.jobsEnabled).map(
              ({ key, name }) => {
                const working = !checkFailed && available && data?.services[key] === true;
                const lastWorking = checkFailed ? null : data?.servicesLastOnlineAt?.[key];
                return (
                  <li key={key} className="flex items-start justify-between gap-3">
                    <span className="flex shrink-0 items-center gap-2">
                      <span
                        aria-hidden
                        className={cn(
                          "size-1.5 rounded-full",
                          working ? "bg-bull" : lastWorking != null ? "bg-bear" : "bg-muted-foreground",
                        )}
                      />
                      {name}
                    </span>
                    <span
                      className="min-w-0 text-right text-muted-foreground"
                      title={lastWorking != null ? new Date(lastWorking).toLocaleString() : undefined}
                    >
                      {working
                        ? "Working now"
                        : lastWorking != null
                          ? `Last working ${formatAgo(lastWorking, now)}`
                          : "—"}
                    </span>
                  </li>
                );
              },
            )}
          </ul>
        </PopoverContent>
      </Popover>
      {checkFailed ? (
        <span className="text-xs text-muted-foreground">Couldn't check the runner</span>
      ) : data?.reason === "never-reported" ? (
        <span className="text-xs text-muted-foreground">No heartbeat received yet</span>
      ) : !available && data?.lastOnlineAt != null ? (
        <time
          className="text-xs text-muted-foreground"
          dateTime={new Date(data.lastOnlineAt).toISOString()}
          title={new Date(data.lastOnlineAt).toLocaleString()}
        >
          Last online {formatAgo(data.lastOnlineAt, now)}
        </time>
      ) : null}
    </div>
  );
}

export function RunnerStatusNotice() {
  const { data, available, checkFailed, isPending } = useRunnerStatus();
  const now = useNow();
  if (isPending || data?.configured === false || (available && data?.state === "online"))
    return null;
  const lastOnline =
    data?.lastOnlineAt != null
      ? ` (last online ${formatAgo(data.lastOnlineAt, now)})`
      : data?.reason === "never-reported"
        ? " (no heartbeat received yet)"
        : "";
  let message = `Runner temporarily offline${lastOnline}: Alerts, analysis and forward runs are temporarily down.`;
  if (checkFailed) {
    message = "Couldn't check the runner right now. Controls stay disabled until the check succeeds.";
  } else if (available && data?.state === "degraded") {
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
      className={cn(
        "mb-6 rounded-lg border px-4 py-3 text-sm leading-relaxed",
        checkFailed ? "border-border bg-muted text-muted-foreground" : "border-bear/30 bg-bear/5",
      )}
    >
      {message}
    </div>
  );
}
