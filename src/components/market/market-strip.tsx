import { useId, useState, type ReactNode } from "react";
import { ChevronDown, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { StatusChip } from "@/components/plain";
import { cn } from "@/lib/utils";
import {
  formatAge,
  shortClock,
  stripHeadline,
  stripHeadlineDetail,
  unavailableShort,
  type FreshnessItem,
  type MarketStripView,
  type StripStatusKind,
} from "@/lib/market/assessment";
import {
  SLOW_ATTENTION_MS,
  SLOW_HORIZON_LABEL,
  slowBreadthLine,
  slowCoordinatedSentence,
  slowUnusualWord,
  type SlowMovementRead,
  type SlowStatus,
} from "@/lib/market/slow-movement-view";

const time = (ms: number | null) => (ms === null ? "—" : new Date(ms).toLocaleTimeString());
const signedPct = (value: number) => `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(2)}%`;
const directionClass = (value: number | null) =>
  value === null || value === 0 ? "text-foreground" : value > 0 ? "text-bull" : "text-bear";

/**
 * Market-wide block (#30 "Current assessment"): one calm status line, the headline and three window tiles.
 * Everything technical (evaluation times, per-source freshness, universe, dispersion, what "material" means)
 * lives in the closed Diagnostics disclosure at the bottom.
 */
export function MarketStrip({
  view,
  status,
  current,
  freshness,
  now,
  taNotice,
  diagnostics = [],
  checkNow,
  slow,
}: {
  view: MarketStripView;
  status: StripStatusKind;
  /** The reading may be shown as current (false keeps an already-late reading muted with its time, even while retried). */
  current: boolean;
  freshness: FreshnessItem[];
  now: number;
  /** "Technical analysis is catching up (last updated 14 min ago)." after a self-heal did not clear it. */
  taNotice: string | null;
  /** Extra rows for Diagnostics (price source, last refresh, last check). */
  diagnostics?: Array<[string, string]>;
  /** The existing manual check; null when this user cannot run one. */
  checkNow: { run: () => void; pending: boolean; disabled: boolean; title?: string | undefined } | null;
  /** "Longer moves" (slow-movement-v1): the published reading and its freshness on the page's clock. */
  slow?: { read: SlowMovementRead | undefined; status: SlowStatus } | undefined;
}) {
  const reading = view.times.evaluationBoundary;
  const fresh = current;
  const headline = stripHeadline(view);
  const detail = stripHeadlineDetail(view);

  return (
    <section className="panel mt-4 min-w-0 p-4 sm:p-5" aria-label="Market now">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1" role="status" aria-live="polite">
          <StatusLine status={status} view={view} now={now} reading={reading} />
          {taNotice ? (
            <p className="mt-2 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              <StatusChip level="wait">Catching up</StatusChip>
              {taNotice}
            </p>
          ) : null}
        </div>
        {checkNow ? (
          <Button
            variant="ghost"
            size="sm"
            className="h-11 shrink-0 sm:h-8"
            onClick={checkNow.run}
            disabled={checkNow.disabled || checkNow.pending}
            title={checkNow.title}
          >
            {checkNow.pending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden /> : null}
            {checkNow.pending ? "Checking…" : "Check now"}
          </Button>
        ) : null}
      </div>

      {status === "loading" ? (
        <StripSkeleton />
      ) : headline ? (
        <div className="mt-4">
          <p className="flex flex-wrap items-baseline gap-x-2">
            <span
              className={cn(
                "font-display text-xl font-semibold transition-colors duration-150 motion-reduce:transition-none sm:text-2xl",
                !fresh
                  ? "text-muted-foreground"
                  : headline.tone === "up"
                    ? "text-bull"
                    : headline.tone === "down"
                      ? "text-bear"
                      : "text-foreground",
              )}
            >
              {headline.text}
            </span>
            {!fresh && reading !== null ? (
              <span className="num text-sm text-muted-foreground">as of {shortClock(reading)}</span>
            ) : null}
          </p>
          {detail ? <p className="mt-1 text-sm text-muted-foreground">{detail}</p> : null}
        </div>
      ) : null}

      {status !== "loading" && view.windows.length > 0 ? (
        <ul
          className="mt-4 flex snap-x snap-mandatory gap-3 overflow-x-auto pb-1 sm:grid sm:grid-cols-3 sm:overflow-visible sm:pb-0"
          aria-label="Moves by window"
        >
          {view.windows.map((w) => (
            <li
              key={w.minutes}
              className={cn(
                "w-[70%] shrink-0 snap-start rounded-lg bg-secondary/40 p-4 sm:w-auto",
                !fresh && "opacity-70",
              )}
              aria-label={w.sentence}
              title={w.sentence}
            >
              <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">{w.minutes} min</p>
              <p
                className={cn(
                  "mt-1 font-display text-2xl font-semibold tabular-nums",
                  fresh ? directionClass(w.medianMovePct) : "text-muted-foreground",
                )}
              >
                {w.medianMovePct === null ? "—" : signedPct(w.medianMovePct)}
              </p>
              <p className="mt-1 text-sm text-muted-foreground">{w.short}</p>
            </li>
          ))}
        </ul>
      ) : null}

      {status !== "loading" ? (
        <OutlierLine view={view} />
      ) : null}

      {slow ? <LongerMoves read={slow.read} status={slow.status} now={now} /> : null}

      <Diagnostics view={view} freshness={freshness} now={now} extra={[...(slow ? slowDiagnostics(slow.read) : []), ...diagnostics]} />
    </section>
  );
}

function StatusLine({
  status,
  view,
  now,
  reading,
}: {
  status: StripStatusKind;
  view: MarketStripView;
  now: number;
  reading: number | null;
}) {
  const at = reading === null ? null : shortClock(reading);
  const dot = (className: string) => <span className={cn("inline-block size-2 shrink-0 rounded-full", className)} aria-hidden />;
  const line = (icon: ReactNode, text: ReactNode) => (
    <p className="flex items-center gap-2 text-sm text-muted-foreground">
      {icon}
      <span>{text}</span>
    </p>
  );
  switch (status) {
    case "live":
      return line(dot("bg-bull"), <>Live · updated <span className="num">{formatAge(Math.max(0, now - (reading ?? now)))}</span> ago</>);
    case "updating":
    case "loading":
      return line(<Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden />, "Updating…");
    case "warming":
      return line(dot("bg-muted-foreground"), "Warming up · collecting live history");
    case "unavailable":
      return line(dot("bg-muted-foreground"), unavailableShort(view));
    case "offline":
      return (
        <p className="flex flex-wrap items-center gap-2 text-sm">
          <StatusChip level="wait">Not reporting</StatusChip>
          <span>Your machine isn't reporting.{at ? ` Showing the last reading from ${at}.` : ""}</span>
        </p>
      );
    case "late":
      return (
        <p className="flex flex-wrap items-center gap-2 text-sm">
          <StatusChip level="wait">Delayed</StatusChip>
          <span>The market reading is behind.{at ? ` Showing the reading from ${at}.` : ""}</span>
        </p>
      );
    case "failed":
      return (
        <p className="flex flex-wrap items-center gap-2 text-sm">
          <StatusChip level="problem">Refresh failed</StatusChip>
          <span>{view.windows.length && at ? `Couldn't refresh. Showing the reading from ${at}.` : unavailableShort(view)}</span>
        </p>
      );
  }
}

function OutlierLine({ view }: { view: MarketStripView }) {
  const seen = new Set<string>();
  const outliers = view.windows.flatMap((w) =>
    w.outliers.flatMap((o) => {
      if (seen.has(o.symbol)) return [];
      seen.add(o.symbol);
      return [`${o.symbol.replace(/USDT$/, "")} ${signedPct(o.movePct)} in ${w.minutes} min`];
    }),
  );
  if (!outliers.length) return null;
  return (
    <p
      className="mt-3 text-sm text-muted-foreground"
      title="A pair moving far more than the rest of the market and more than usual for itself, while the others do not follow."
    >
      Moving alone: <span className="num">{outliers.join(", ")}</span>
    </p>
  );
}

function StripSkeleton() {
  return (
    <div className="mt-4 animate-pulse motion-reduce:animate-none" aria-hidden>
      <div className="h-7 w-56 rounded-md bg-secondary/60" />
      <div className="mt-2 h-4 w-72 max-w-full rounded-md bg-secondary/40" />
      <div className="mt-4 grid grid-cols-3 gap-3">
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-24 rounded-lg bg-secondary/40" />
        ))}
      </div>
    </div>
  );
}

function LongerMoves({ read, status, now }: { read: SlowMovementRead | undefined; status: SlowStatus; now: number }) {
  const result = read?.result ?? null;
  const fresh = status === "current" || status === "catching_up";
  const age = read?.boundaryMs != null ? Math.max(0, now - read.boundaryMs) : null;
  const coordinated = result && fresh ? result.horizons.map((h) => ({ h, text: slowCoordinatedSentence(h) })).find((c) => c.text) : undefined;
  return (
    <section className="mt-6" aria-label="Longer moves">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="font-display text-base font-semibold">Longer moves</h3>
        <p className="flex items-center gap-1.5 text-sm text-muted-foreground" role="status">
          {status === "warming" ? (
            "Collecting history, needs about 3 days of data"
          ) : status === "unavailable" ? (
            "Not available right now"
          ) : status === "catching_up" ? (
            <>
              <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden />
              Updating…
            </>
          ) : status === "late" && age !== null ? (
            age > SLOW_ATTENTION_MS ? (
              <span className="flex flex-wrap items-center gap-2">
                <StatusChip level="wait">Behind</StatusChip>
                Longer moves last updated {formatAge(age)} ago
              </span>
            ) : (
              <>Longer moves last updated {formatAge(age)} ago</>
            )
          ) : null}
        </p>
      </div>
      {result ? (
        <ul
          className="mt-3 flex snap-x snap-mandatory gap-3 overflow-x-auto pb-1 sm:grid sm:grid-cols-3 sm:overflow-visible sm:pb-0"
          aria-label="Moves over 1, 4 and 12 hours"
        >
          {result.horizons.map((h) => {
            const label = SLOW_HORIZON_LABEL[h.horizonMin];
            const word = slowUnusualWord(h.medianZ);
            const breadth = slowBreadthLine(h);
            return (
              <li
                key={h.horizonMin}
                className={cn("flex w-[70%] shrink-0 snap-start flex-col rounded-lg bg-secondary/40 p-4 sm:w-auto", !fresh && "opacity-70")}
                aria-label={`${label?.long ?? h.horizonMin}: median move ${h.medianReturnPct === null ? "unavailable" : signedPct(h.medianReturnPct)}, ${breadth}${word ? `, ${word.toLowerCase()}` : ""}`}
              >
                <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">{label?.tile ?? `${h.horizonMin} MIN`}</p>
                <p className={cn("mt-1 font-display text-2xl font-semibold tabular-nums", fresh ? directionClass(h.medianReturnPct) : "text-muted-foreground")}>
                  {h.medianReturnPct === null ? "—" : signedPct(h.medianReturnPct)}
                </p>
                <p className="mt-1 text-sm text-muted-foreground">{breadth}</p>
                {word ? <p className="mt-auto pt-1 text-xs font-medium text-muted-foreground">{word}</p> : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      {coordinated?.text ? (
        <p className={cn("mt-3 font-display text-base font-semibold", coordinated.h.coordinated === "fall" ? "text-bear" : "text-bull")}>
          {coordinated.text}
        </p>
      ) : null}
    </section>
  );
}

function slowDiagnostics(read: SlowMovementRead | undefined): Array<[string, string]> {
  const rows: Array<[string, string]> = [
    [
      "How longer moves work",
      "Compares each pair's last 1, 4 and 12 hours with its own typical moves over the past week. It describes the market; it does not predict it.",
    ],
  ];
  const result = read?.result;
  if (result) {
    rows.push(
      ["Longer moves version", `${result.algorithmVersion} · ${result.configVersion} (${result.configHash})`],
      ["Longer moves boundary", new Date(result.boundaryMs).toLocaleString()],
      ...result.horizons.map((h): [string, string] => [
        `Longer moves ${SLOW_HORIZON_LABEL[h.horizonMin]?.short ?? h.horizonMin}`,
        `${h.included} included · median z ${h.medianZ === null ? "—" : h.medianZ.toFixed(2)} · ${h.up} up, ${h.down} down, ${h.neutral} neither (|z| ≥ ${result.config.zThreshold})`,
      ]),
    );
  }
  return rows;
}

function Diagnostics({
  view,
  freshness,
  now,
  extra,
}: {
  view: MarketStripView;
  freshness: FreshnessItem[];
  now: number;
  extra: Array<[string, string]>;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const rows: Array<[string, ReactNode]> = [
    ["Status", `${view.statusLabel}: ${view.explanation}`],
    ["Source", view.source],
    ...(view.notLiveNote ? [["Note", view.notLiveNote] as [string, ReactNode]] : []),
    ["Evaluation boundary", time(view.times.evaluationBoundary)],
    ["Latest trade data received", time(view.times.lastReceivedAt)],
    ["Exchange event time", time(view.times.lastSourceEvent)],
    ["State saved", time(view.times.engineUpdatedAt)],
    ...freshness.map((item): [string, ReactNode] => [
      item.label,
      `${item.status}: ${item.detail}${item.asOf !== null ? ` (as of ${formatAge(now - item.asOf)} ago)` : ""}`,
    ]),
    ...(view.universe
      ? ([
          ["Universe", `${view.universe.included.length} included of ${view.universe.configured.length} watched${view.universe.excluded.length ? `, ${view.universe.excluded.length} excluded` : ""}`],
          ["Included", view.universe.included.map((s) => s.replace(/USDT$/, "")).join(", ") || "none"],
          ...view.universe.excluded.map((e): [string, ReactNode] => [
            `Excluded ${e.symbol.replace(/USDT$/, "")}`,
            e.reasons.join(", ").toLowerCase(),
          ]),
        ] as Array<[string, ReactNode]>)
      : []),
    ...view.windows.flatMap((w): Array<[string, ReactNode]> => [
      [`${w.minutes} min`, w.sentence],
      [
        `${w.minutes} min median / dispersion`,
        `${w.medianMovePct === null ? "—" : signedPct(w.medianMovePct)} / ${w.dispersion === null ? "—" : w.dispersion.toFixed(2)}`,
      ],
      [`${w.minutes} min, what is “material”`, w.materialNote],
    ]),
    ...extra,
  ];
  return (
    <div className="mt-4">
      <button
        type="button"
        className="inline-flex min-h-11 items-center gap-1 rounded-md text-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:min-h-8"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
      >
        Diagnostics
        <ChevronDown
          className={cn("size-4 transition-transform duration-150 motion-reduce:transition-none", open && "rotate-180")}
          aria-hidden
        />
      </button>
      {open ? (
        <dl id={id} className="mt-2 grid gap-x-6 gap-y-2 text-xs sm:grid-cols-[minmax(10rem,auto)_1fr]">
          {rows.map(([label, value], index) => (
            <div key={`${label}-${index}`} className="contents">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="num break-words">{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}
