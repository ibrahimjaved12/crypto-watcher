import { ChevronDown, CircleHelp, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { useRunnerStatus } from "@/lib/runner/use-runner-status";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { formatAgo, formatClock } from "@/lib/labels";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

const RUNNER_SERVICES = [
  { key: "analysis", name: "Analysis" },
  { key: "alerts", name: "Alerts" },
  { key: "forward", name: "Forward runs" },
  { key: "jobs", name: "On-demand jobs" },
] as const;

type Tone = "muted" | "warn" | "bull" | "bear";
const TONE_TEXT: Record<Tone, string> = {
  muted: "text-muted-foreground",
  warn: "text-warn",
  bull: "text-bull",
  bear: "text-bear",
};
const TONE_PILL: Record<Tone, string> = {
  muted: "md:border-border md:bg-muted",
  warn: "md:border-warn/40 md:bg-warn/10",
  bull: "md:border-bull/40 md:bg-bull/10",
  bear: "md:border-bear/40 md:bg-bear/10",
};

/** One tone rule for the header dot and the notice: failed check muted, degraded amber, online green, else red. */
function runnerTone(checkFailed: boolean, online: boolean, degraded: boolean): Tone {
  if (checkFailed) return "muted";
  if (online && degraded) return "warn";
  return online ? "bull" : "bear";
}

export function RunnerStatusBadge() {
  const { data, available, checkFailed, isPending } = useRunnerStatus();
  const now = useNow();
  if (data?.configured === false) return null;
  if (isPending) {
    return (
      <span role="status" className="grid size-10 place-items-center" aria-label="Runner connecting">
        <span className="size-2 rounded-full bg-muted-foreground motion-safe:animate-pulse" aria-hidden />
        <span className="sr-only">Connecting…</span>
      </span>
    );
  }
  const online = available && data?.reason !== "never-reported";
  const degraded = online && data?.state === "degraded";
  const tone = runnerTone(checkFailed, online, degraded);
  // Only Status unknown / Online / Offline; a degraded runner is "Online" in amber (details in the notice and popover).
  const label = checkFailed ? "Status unknown" : online ? "Online" : "Offline";
  const heartbeat = checkFailed
    ? "Couldn't check the runner"
    : data?.reason === "never-reported"
      ? "No heartbeat received yet"
      : data?.lastOnlineAt != null
        ? `Last heartbeat ${formatClock(data.lastOnlineAt, now)}`
        : null;

  return (
    <div role="status" className="flex shrink-0 items-center">
      <Popover>
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label={`Runner ${label.toLowerCase()}; show service history`}
            className={cn(
              "grid size-10 place-items-center rounded-full font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:inline-flex md:size-auto md:items-center md:gap-1.5 md:border md:px-2 md:py-0.5 md:text-xs",
              TONE_TEXT[tone],
              TONE_PILL[tone],
            )}
          >
            {/* Phone: an 8 px dot in a 40 px hit area (online pings). From md: the original 6 px dot pill. */}
            <span className="relative grid size-2 place-items-center md:hidden" aria-hidden>
              {tone === "bull" ? (
                <span className="absolute inline-flex size-full rounded-full bg-current opacity-60 motion-safe:animate-ping" />
              ) : null}
              <span className="relative size-2 rounded-full bg-current" />
            </span>
            <span className="hidden size-1.5 shrink-0 rounded-full bg-current md:inline-block" aria-hidden />
            <span className="sr-only">Runner </span>
            <span className="sr-only md:not-sr-only">{label}</span>
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          collisionPadding={16}
          className="w-80 max-w-[calc(100vw-2rem)] p-3"
        >
          <div className="mb-3 space-y-1">
            <p className="text-sm font-medium">Runner services</p>
            {heartbeat ? <p className="text-xs text-muted-foreground">{heartbeat}</p> : null}
          </div>
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
                          ? `Last working ${formatAgo(lastWorking, now)} · ${formatClock(lastWorking, now)}`
                          : "—"}
                    </span>
                  </li>
                );
              },
            )}
          </ul>
        </PopoverContent>
      </Popover>
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
      ? ` (last online ${formatAgo(data.lastOnlineAt, now)} · ${formatClock(data.lastOnlineAt, now)})`
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

  const tone = checkFailed ? "muted" : available && data?.state === "degraded" ? "warn" : "bear";
  return <NoticeBox tone={tone} message={message} />;
}

function NoticeBox({ tone, message }: { tone: "muted" | "warn" | "bear"; message: string }) {
  const [open, setOpen] = useState(false);
  const Icon = tone === "muted" ? CircleHelp : TriangleAlert;
  return (
    <div
      role="status"
      className={cn(
        "mb-4 flex items-start gap-2.5 rounded-lg border px-4 py-2.5 text-sm leading-relaxed sm:mb-6",
        tone === "muted"
          ? "border-border bg-muted text-muted-foreground"
          : tone === "warn"
            ? "border-warn/30 bg-warn/5"
            : "border-bear/30 bg-bear/5",
      )}
    >
      <Icon className={cn("mt-0.5 size-4 shrink-0", TONE_TEXT[tone])} aria-hidden />
      <p id="runner-notice-text" className={cn("min-w-0 flex-1", !open && "max-sm:line-clamp-1")}>
        {message}
      </p>
      <button
        type="button"
        aria-expanded={open}
        aria-controls="runner-notice-text"
        aria-label={open ? "Show less" : "Show the full message"}
        onClick={() => setOpen((value) => !value)}
        className="-my-2 -mr-2 grid size-10 shrink-0 place-items-center rounded-full text-muted-foreground hover:text-foreground sm:hidden"
      >
        <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} aria-hidden />
      </button>
    </div>
  );
}
