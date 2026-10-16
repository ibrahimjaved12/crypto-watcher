import { useId, useState, type ReactNode } from "react";
import { CheckCircle2, ChevronDown, CircleDashed, Clock, Hourglass, Info, Loader2, RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
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
  type WindowSummary,
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
import { BreadthBar, Meter, NotReadyChip } from "./gauges";

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
    <section className="panel mt-3 min-w-0 break-words p-4 sm:mt-4 sm:p-5" aria-label="Market now">
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
            variant="outline"
            size="sm"
            className="size-11 shrink-0 p-0 sm:h-8 sm:w-auto sm:px-3"
            onClick={checkNow.run}
            disabled={checkNow.disabled || checkNow.pending}
            title={checkNow.title}
            aria-label={checkNow.pending ? "Checking" : "Check now"}
          >
            {checkNow.pending ? (
              <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden />
            ) : (
              <RefreshCw aria-hidden />
            )}
            <span className="hidden sm:inline">{checkNow.pending ? "Checking…" : "Check now"}</span>
          </Button>
        ) : null}
      </div>

      {status === "loading" ? (
        <StripSkeleton />
      ) : headline ? (
        <div className="mt-3 sm:mt-4">
          <p className="flex flex-wrap items-baseline gap-x-2">
            <span
              className={cn(
                "min-w-0 font-display text-xl font-semibold transition-colors duration-150 motion-reduce:transition-none sm:text-2xl",
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
        <ul className="mt-3 grid grid-cols-3 gap-2 sm:mt-4 sm:gap-3" aria-label="Moves by window">
          {view.windows.map((w) => (
            <WindowTile key={w.minutes} w={w} fresh={fresh} />
          ))}
        </ul>
      ) : null}

      {status !== "loading" ? (
        <OutlierLine view={view} />
      ) : null}

      {slow ? <LongerMoves read={slow.read} status={slow.status} now={now} /> : null}

      <Diagnostics
        view={view}
        status={status}
        freshness={freshness}
        now={now}
        showSlowNote={!!slow}
        extra={[...(slow ? slowDiagnostics(slow.read) : []), ...diagnostics]}
      />
    </section>
  );
}

const shortCount = (w: WindowSummary) => {
  const falling = (w.medianMovePct ?? 0) < 0;
  return `${(falling ? w.falling : w.rising) ?? "?"}/${w.denominator} ${falling ? "down" : "up"}`;
};

/**
 * One window tile. Ready = solid tile with the median move, a breadth bar and the count; warm-up = dashed outline
 * with an hourglass; no reading = dashed outline with a slash. The full sentence is the tile's label and tooltip.
 */
/** Phone caption for a Longer moves tile, same shape as the V1 tiles: "6/9 down". */
const slowShortCount = (h: { medianReturnPct: number | null; symbols: Array<{ status: string; returnPct: number | null }> }) => {
  const ok = h.symbols.filter((row) => row.status === "ok" && row.returnPct !== null);
  if (!ok.length) return "—";
  const falling = (h.medianReturnPct ?? 0) < 0;
  return `${ok.filter((row) => (falling ? row.returnPct! < 0 : row.returnPct! > 0)).length}/${ok.length} ${falling ? "down" : "up"}`;
};

function WindowTile({ w, fresh }: { w: WindowSummary; fresh: boolean }) {
  if (!w.available) {
    return (
      <li
        className="flex min-w-0 flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-muted-foreground/40 bg-transparent p-3 text-center sm:items-start sm:p-4 sm:text-left"
        aria-label={`${w.minutes} min: ${w.warming ? "warming up" : w.sentence}`}
        title={w.warming ? "Needs a little more live data after a restart" : w.sentence}
      >
        <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
          <span className="sm:hidden">{w.minutes}m</span>
          <span className="hidden sm:inline">{w.minutes} min</span>
        </p>
        <NotReadyChip kind={w.warming ? "warming" : "none"} className="border-0 px-0" />
      </li>
    );
  }
  const total = w.denominator;
  return (
    <li
      className={cn(
        "flex min-w-0 flex-col items-center gap-1.5 rounded-lg bg-secondary/40 p-3 text-center sm:items-stretch sm:p-4 sm:text-left",
        !fresh && "opacity-70",
      )}
      aria-label={w.sentence}
      title={w.sentence}
    >
      <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
        <span className="sm:hidden">{w.minutes}m</span>
        <span className="hidden sm:inline">{w.minutes} min</span>
      </p>
      <p
        className={cn(
          "font-display text-lg font-semibold tabular-nums sm:text-2xl",
          fresh ? directionClass(w.medianMovePct) : "text-muted-foreground",
        )}
      >
        {w.medianMovePct === null ? "—" : signedPct(w.medianMovePct)}
      </p>
      <BreadthBar
        falling={w.falling ?? 0}
        rising={w.rising ?? 0}
        total={total}
        label={`${w.falling ?? 0} falling, ${w.rising ?? 0} rising of ${total} pairs`}
      />
      <p className="num text-xs text-muted-foreground sm:hidden">{shortCount(w)}</p>
      <p className="hidden text-sm text-muted-foreground sm:block">{w.short}</p>
    </li>
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
      return line(<Hourglass className="size-3.5" aria-hidden />, "Warming up · collecting live history");
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
            <>
              <Hourglass className="size-3.5" aria-hidden />
              Warming up · needs about 3 days of data
            </>
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
        <ul className="mt-3 grid grid-cols-3 gap-2 sm:gap-3" aria-label="Moves over 1, 4 and 12 hours">
          {result.horizons.map((h) => {
            const label = SLOW_HORIZON_LABEL[h.horizonMin];
            const word = slowUnusualWord(h.medianZ);
            const breadth = slowBreadthLine(h);
            return (
              <li
                key={h.horizonMin}
                className={cn("flex min-w-0 flex-col items-center gap-1 rounded-lg bg-secondary/40 p-3 text-center sm:items-stretch sm:p-4 sm:text-left", !fresh && "opacity-70")}
                title={breadth}
                aria-label={`${label?.long ?? h.horizonMin}: median move ${h.medianReturnPct === null ? "unavailable" : signedPct(h.medianReturnPct)}, ${breadth}${word ? `, ${word.toLowerCase()}` : ""}`}
              >
                <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                  <span className="sm:hidden">{label?.short ?? h.horizonMin}</span>
                  <span className="hidden sm:inline">{label?.tile ?? `${h.horizonMin} MIN`}</span>
                </p>
                <p className={cn("font-display text-lg font-semibold tabular-nums sm:text-2xl", fresh ? directionClass(h.medianReturnPct) : "text-muted-foreground")}>
                  {h.medianReturnPct === null ? "—" : signedPct(h.medianReturnPct)}
                </p>
                <p className="num text-xs text-muted-foreground sm:hidden">{slowShortCount(h)}</p>
                <p className="hidden text-sm text-muted-foreground sm:block">{breadth}</p>
                {word ? <p className="mt-auto text-xs font-medium text-muted-foreground">{word}</p> : null}
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
  const rows: Array<[string, string]> = [];
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

const HEALTH: Record<StripStatusKind, { level: "ok" | "wait" | "problem" | null; word: string }> = {
  live: { level: "ok", word: "Healthy" },
  updating: { level: null, word: "Updating" },
  loading: { level: null, word: "Loading" },
  warming: { level: null, word: "Warming up" },
  unavailable: { level: null, word: "No reading" },
  offline: { level: "wait", word: "Not reporting" },
  late: { level: "wait", word: "Delayed" },
  failed: { level: "problem", word: "Refresh failed" },
};

/**
 * The Diagnostics drawer, closed by default. Its first row answers "is anything wrong?" with one chip; then a
 * freshness checklist with age-versus-limit bars, the window table, the pair chips, and a nested "Technical details"
 * drawer with the raw rows.
 */
function Diagnostics({
  view,
  status,
  freshness,
  now,
  showSlowNote,
  extra,
}: {
  view: MarketStripView;
  status: StripStatusKind;
  freshness: FreshnessItem[];
  now: number;
  showSlowNote: boolean;
  extra: Array<[string, string]>;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const health = HEALTH[status];
  const boundary = view.times.evaluationBoundary;
  const universe = view.universe;
  const technical: Array<[string, string]> = [
    ["Status", `${view.statusLabel}: ${view.explanation}`],
    ["Source", view.source],
    ...(view.notLiveNote ? [["Note", view.notLiveNote] as [string, string]] : []),
    ["Evaluation boundary", time(view.times.evaluationBoundary)],
    ["Latest trade data received", time(view.times.lastReceivedAt)],
    ["Exchange event time", time(view.times.lastSourceEvent)],
    ["State saved", time(view.times.engineUpdatedAt)],
    ...freshness.map((item): [string, string] => [item.label, `${item.status}: ${item.detail}`]),
    ...view.windows.flatMap((w): Array<[string, string]> => [
      [`${w.minutes} min`, w.sentence],
      [`${w.minutes} min, material`, w.materialNote],
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
        <div id={id} className="mt-3 flex min-w-0 flex-col gap-5 text-sm">
          {/* 1. Health */}
          <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:gap-3">
            {health.level ? (
              <StatusChip level={health.level}>{health.word}</StatusChip>
            ) : (
              <NotReadyChip kind={status === "unavailable" ? "none" : "warming"}>{health.word}</NotReadyChip>
            )}
            <span className="text-muted-foreground">
              {boundary !== null ? <>Evaluated <span className="num">{formatAge(Math.max(0, now - boundary))}</span> ago</> : "Not evaluated yet"}
              {universe ? <> · <span className="num">{universe.included.length} of {universe.configured.length}</span> pairs</> : null}
            </span>
          </div>

          {/* 2. Freshness checklist */}
          <ul className="flex flex-col gap-3" aria-label="Data freshness">
            {freshness.map((item) => (
              <FreshnessRow key={item.id} item={item} now={now} />
            ))}
          </ul>

          {/* 3. Windows */}
          {view.windows.length ? (
            <div>
              <div className="hidden grid-cols-[3rem_1fr_1fr_1.5fr] gap-3 text-xs text-muted-foreground sm:grid">
                <span>Window</span>
                <span className="text-right">Median move</span>
                <span className="text-right">Usual spread</span>
                <span>Pairs</span>
              </div>
              <ul className="mt-1 grid gap-2">
                {view.windows.map((w) => (
                  <li
                    key={w.minutes}
                    className="grid grid-cols-2 items-center gap-x-3 gap-y-1 rounded-lg bg-secondary/30 p-3 sm:grid-cols-[3rem_1fr_1fr_1.5fr] sm:bg-transparent sm:p-0"
                    title={w.sentence}
                  >
                    <span className="num text-xs text-muted-foreground">{w.minutes}m</span>
                    {w.available ? (
                      <>
                        <span className={cn("num text-right", directionClass(w.medianMovePct))}>
                          {w.medianMovePct === null ? "—" : signedPct(w.medianMovePct)}
                        </span>
                        <span className="num text-xs text-muted-foreground sm:text-right sm:text-sm sm:text-foreground">
                          <span className="sm:hidden">Usual spread </span>
                          {w.dispersion === null ? "—" : w.dispersion.toFixed(2)}
                        </span>
                        <BreadthBar falling={w.falling ?? 0} rising={w.rising ?? 0} total={w.denominator}
                          label={`${w.falling ?? 0} falling, ${w.rising ?? 0} rising of ${w.denominator} pairs`} />
                      </>
                    ) : (
                      <span className="col-span-1 justify-self-end sm:col-span-3 sm:justify-self-start">
                        <NotReadyChip kind={w.warming ? "warming" : "none"} title={w.sentence} />
                      </span>
                    )}
                  </li>
                ))}
              </ul>
              <p className="mt-2 flex items-start gap-1.5 text-xs text-muted-foreground">
                <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                A move counts as material when it is clearly bigger than usual for that pair.
              </p>
              {showSlowNote ? (
                <p className="mt-1 flex items-start gap-1.5 text-xs text-muted-foreground">
                  <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                  Longer moves compare each pair's last 1, 4 and 12 hours with its own past week. They describe, not predict.
                </p>
              ) : null}
            </div>
          ) : null}

          {/* 4. Pairs */}
          {universe ? (
            <div>
              <p className="text-xs text-muted-foreground">
                <span className="num">{universe.included.length} of {universe.configured.length}</span> pairs included
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {universe.included.map((symbol) => (
                  <Badge key={symbol} variant="outline" className="num text-xs font-normal">{symbol.replace(/USDT$/, "")}</Badge>
                ))}
                {universe.excluded.map((e) => (
                  <NotReadyChip key={e.symbol} kind="none" title={e.reasons.join(", ").toLowerCase()}>
                    {e.symbol.replace(/USDT$/, "")}
                  </NotReadyChip>
                ))}
              </div>
              {universe.excluded.length && universe.excluded.length <= 3 ? (
                <ul className="mt-1.5 text-xs text-muted-foreground">
                  {universe.excluded.map((e) => (
                    <li key={e.symbol}>{e.symbol.replace(/USDT$/, "")}: {e.reasons.join(", ").toLowerCase()}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}

          {/* 5. Technical details */}
          <TechnicalDetails rows={technical} />
        </div>
      ) : null}
    </div>
  );
}

function FreshnessRow({ item, now }: { item: FreshnessItem; now: number }) {
  const [showDetail, setShowDetail] = useState(false);
  const age = item.ageMs !== null && Number.isFinite(item.ageMs) ? item.ageMs : item.asOf !== null ? Math.max(0, now - item.asOf) : null;
  const ratio = item.ageMs !== null && item.limitMs ? (Number.isFinite(item.ageMs) ? item.ageMs / item.limitMs : 1) : null;
  const Icon = item.status === "fresh" ? CheckCircle2 : item.status === "stale" ? Clock : CircleDashed;
  const iconClass = item.status === "fresh" ? "text-bull" : item.status === "stale" ? "text-warn" : "text-muted-foreground";
  const word = item.status === "fresh" ? "fresh" : item.status === "stale" ? "behind" : "not available";
  return (
    <li className="grid grid-cols-[1.25rem_1fr_auto] items-center gap-x-2 gap-y-1">
      <Icon className={cn("size-4", iconClass)} role="img" aria-label={word} />
      <span className="min-w-0 truncate">{item.label}</span>
      <span className="num text-right text-xs">{age === null ? "—" : formatAge(age)}</span>
      {ratio !== null && item.limitMs ? (
        <div className="col-start-2 col-end-4 flex items-center gap-2">
          <Meter
            value={Math.min(1, ratio)}
            max={1}
            fillClass={ratio < 0.75 ? "bg-bull" : "bg-warn"}
            label={`${item.label} age versus its limit`}
            valueText={`${age === null ? "unknown age" : formatAge(age)} of a ${formatAge(item.limitMs)} limit`}
          />
          <span className="num shrink-0 text-xs text-muted-foreground">limit {formatAge(item.limitMs)}</span>
        </div>
      ) : null}
      {item.status !== "fresh" ? (
        <p className="col-start-2 col-end-4 hidden text-xs text-muted-foreground sm:block">{item.detail}</p>
      ) : null}
      <div className="col-start-2 col-end-4 sm:hidden">
        <button
          type="button"
          className="inline-flex min-h-11 items-center gap-1 text-xs text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-expanded={showDetail}
          onClick={() => setShowDetail((v) => !v)}
        >
          Details
          <ChevronDown className={cn("size-3.5 transition-transform duration-150 motion-reduce:transition-none", showDetail && "rotate-180")} aria-hidden />
        </button>
        {showDetail ? <p className="text-xs text-muted-foreground">{item.detail}</p> : null}
      </div>
    </li>
  );
}

function TechnicalDetails({ rows }: { rows: Array<[string, string]> }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div>
      <button
        type="button"
        className="inline-flex min-h-11 items-center gap-1 rounded-md text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:min-h-8"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
      >
        Technical details
        <ChevronDown className={cn("size-3.5 transition-transform duration-150 motion-reduce:transition-none", open && "rotate-180")} aria-hidden />
      </button>
      {open ? (
        <dl id={id} className="mt-2 grid gap-x-6 gap-y-2 text-xs sm:grid-cols-[minmax(10rem,auto)_1fr]">
          {rows.map(([label, value], index) => (
            <div key={`${label}-${index}`} className="contents">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="num min-w-0 break-words">{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}
