import { useRunnerStatus } from "@/lib/runner/use-runner-status";
import { relativeTime } from "@/lib/labels";
import { cn } from "@/lib/utils";

export function RunnerStatusBadge() {
  const { data } = useRunnerStatus();
  if (!data?.configured || data.online === null) return null;

  return (
    <div role="status" className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
      <span
        className={cn(
          "rounded-full border px-2 py-0.5 font-medium",
          data.online
            ? "border-bull/40 bg-bull/10 text-bull"
            : "border-bear/40 bg-bear/10 text-bear",
        )}
      >
        <span className="sr-only">Runner </span>
        {data.online ? "Online" : "Offline"}
      </span>
      {!data.online && data.lastOnlineAt !== null && (
        <span
          className="whitespace-nowrap text-muted-foreground"
          title={new Date(data.lastOnlineAt).toLocaleString()}
        >
          Last online {relativeTime(data.lastOnlineAt)}
        </span>
      )}
    </div>
  );
}

export function RunnerStatusNotice() {
  const { data } = useRunnerStatus();
  if (!data?.configured || data.online !== false) return null;

  return (
    <div role="status" className="mb-6 rounded-lg border border-warn/40 bg-warn/10 p-3 text-sm">
      Runner temporarily offline: Alerts, analysis and forward runs are temporarily down.
    </div>
  );
}
