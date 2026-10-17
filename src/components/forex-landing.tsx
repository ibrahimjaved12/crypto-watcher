import {
  ArrowDownUp,
  ArrowLeftRight,
  CandlestickChart,
  Coins,
  Droplet,
  Moon,
  Wallet,
  type LucideIcon,
} from "lucide-react";

import { PageHeader } from "@/components/plain";
import { FOREX_DEFAULTS, type AssetClass, type Instrument } from "@/lib/market/instruments";
import { FX_SESSIONS, fxNextOpenMs, fxSessionTags, fxWeekWindow, isFxOpen } from "@/lib/market/trading-calendar";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

/**
 * Forex screens before the data feed exists (#374). Only the trading calendar and session clock are real; there
 * are no prices, charts, signals or accounts, and nothing here pretends otherwise.
 */
const TITLES = { market: "Market", paper: "Paper account", alerts: "Alerts", lab: "Strategy Lab" } as const;
const SESSION_LABELS: Record<string, string> = {
  sydney: "Sydney",
  tokyo: "Tokyo",
  london: "London",
  new_york: "New York",
};
const CLASS_STYLE: Partial<Record<AssetClass, { icon: LucideIcon; chip: string }>> = {
  metal: { icon: Coins, chip: "bg-warn/15 text-warn ring-warn/30" },
  energy: { icon: Droplet, chip: "bg-chart-2/15 text-chart-2 ring-chart-2/30" },
  fx: { icon: ArrowLeftRight, chip: "bg-primary/15 text-primary ring-primary/30" },
};
const COMING: readonly { icon: LucideIcon; title: string }[] = [
  { icon: ArrowDownUp, title: "Bid, ask, spread" },
  { icon: CandlestickChart, title: "Session-aware charts" },
  { icon: Wallet, title: "Separate USD account" },
];

const whenFormat = new Intl.DateTimeFormat(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const clockFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

/** Hours the wall clock of `timeZone` is ahead of UTC at `ms` (fractional for half-hour zones). */
function zoneOffsetHours(timeZone: string | undefined, ms: number): number {
  const parts: Record<string, string> = {};
  for (const part of new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
  }).formatToParts(ms))
    parts[part.type] = part.value;
  const wall = Date.UTC(Number(parts["year"]), Number(parts["month"]) - 1, Number(parts["day"]), Number(parts["hour"]), Number(parts["minute"]));
  return (wall - Math.floor(ms / 60_000) * 60_000) / 3_600_000;
}

/** A session's conventional hours on the viewer's 24 h clock as [start, end) pieces, split at midnight. */
export function localSegments(timeZone: string, start: number, end: number, ms: number): [number, number][] {
  const shift = zoneOffsetHours(undefined, ms) - zoneOffsetHours(timeZone, ms);
  const from = (((start + shift) % 24) + 24) % 24;
  const to = from + (end - start);
  return to <= 24 ? [[from, to]] : [[from, 24], [0, to - 24]];
}

function localHour(ms: number): number {
  return (((new Date(ms).getUTCHours() + new Date(ms).getUTCMinutes() / 60 + zoneOffsetHours(undefined, ms)) % 24) + 24) % 24;
}

export function ForexLanding({ page }: { page: keyof typeof TITLES }) {
  const now = useNow(30_000);
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Forex · simulated"
        title={TITLES[page]}
        subtitle="The forex engine is built; its data feed is not connected yet."
      />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <MarketStateCard now={now} />
        <SessionStrip now={now} />
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {FOREX_DEFAULTS.map((instrument) => (
          <InstrumentTile key={instrument.instrumentId} instrument={instrument} />
        ))}
      </div>
      <div className="-mx-4 flex snap-x snap-mandatory gap-3 overflow-x-auto px-4 pb-1 sm:mx-0 sm:grid sm:grid-cols-3 sm:overflow-visible sm:px-0">
        {COMING.map(({ icon: Icon, title }) => (
          <div
            key={title}
            className="panel flex min-w-[11rem] shrink-0 snap-start items-center gap-3 rounded-2xl border border-border p-3 sm:min-w-0"
          >
            <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/12 text-primary ring-1 ring-primary/25">
              <Icon className="size-4" aria-hidden />
            </span>
            <span className="text-sm font-medium">{title}</span>
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        Simulated. Forex data is not connected yet; nothing here is a price or a signal.
      </p>
    </div>
  );
}

function MarketStateCard({ now }: { now: number }) {
  const open = isFxOpen(now);
  const next = open ? fxWeekWindow(now)[1] : fxNextOpenMs(now);
  return (
    <section className="panel flex flex-col justify-center gap-3 rounded-2xl border border-border p-5" aria-label="Forex market state">
      <span
        className={cn(
          "inline-flex w-fit items-center gap-2 rounded-full px-4 py-2 font-display text-lg font-semibold",
          open ? "bg-bull/15 text-bull" : "bg-muted text-muted-foreground",
        )}
      >
        {open ? (
          <span className="relative flex size-2.5" aria-hidden>
            <span className="absolute inline-flex size-full rounded-full bg-bull opacity-60 motion-safe:animate-ping" />
            <span className="relative inline-flex size-2.5 rounded-full bg-bull" />
          </span>
        ) : (
          <Moon className="size-4" aria-hidden />
        )}
        {open ? "Market open" : "Market closed"}
      </span>
      <p className="text-sm text-muted-foreground">
        {open ? "Closes" : "Next open"} <span className="num text-foreground">{whenFormat.format(next)}</span>
      </p>
    </section>
  );
}

function SessionStrip({ now }: { now: number }) {
  const active = new Set(fxSessionTags(now));
  const nowPct = (localHour(now) / 24) * 100;
  return (
    <section className="panel rounded-2xl border border-border p-5" aria-label="Trading sessions in your time zone">
      <div className="relative space-y-3 pt-5">
        {FX_SESSIONS.map(([name, timeZone, start, end]) => {
          const on = active.has(name);
          return (
            <div key={name} className="sm:grid sm:grid-cols-[88px_minmax(0,1fr)] sm:items-center sm:gap-3">
              <span className={cn("mb-1 block text-xs sm:mb-0 sm:text-sm", on ? "text-foreground" : "text-muted-foreground")}>
                {SESSION_LABELS[name] ?? name}
              </span>
              <div className="relative h-3.5 overflow-hidden rounded-full bg-background/50 ring-1 ring-border/60 sm:h-3">
                {localSegments(timeZone, start, end, now).map(([from, to]) => (
                  <span
                    key={from}
                    className={cn("absolute inset-y-0 rounded-full", on ? "bg-primary" : "bg-muted")}
                    style={{ left: `${(from / 24) * 100}%`, width: `${((to - from) / 24) * 100}%` }}
                  />
                ))}
              </div>
            </div>
          );
        })}
        <div className="pointer-events-none absolute inset-y-0 left-0 right-0 sm:left-[100px]" aria-hidden>
          <div className="absolute inset-y-0 w-px bg-foreground/70" style={{ left: `${nowPct}%` }}>
            <span className="num absolute -top-0.5 left-1/2 -translate-x-1/2 rounded bg-background/80 px-1 text-[10px] text-foreground">
              {clockFormat.format(now)}
            </span>
          </div>
        </div>
      </div>
      <p className="sr-only">Active now: {active.size ? [...active].map((n) => SESSION_LABELS[n] ?? n).join(", ") : "none"}</p>
    </section>
  );
}

function InstrumentTile({ instrument }: { instrument: Instrument }) {
  const symbol = instrument.instrumentId.split(":")[1] ?? instrument.display;
  const style = CLASS_STYLE[instrument.assetClass] ?? CLASS_STYLE.fx!;
  const Icon = style.icon;
  const name = instrument.display !== symbol ? instrument.display : `${instrument.base} / ${instrument.quote}`;
  return (
    <div className="panel flex flex-col gap-3 rounded-2xl border border-border p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-display text-lg font-semibold">{symbol}</p>
          <p className="truncate text-xs text-muted-foreground">{name}</p>
        </div>
        <span className={cn("grid size-9 shrink-0 place-items-center rounded-xl ring-1", style.chip)}>
          <Icon className="size-4" aria-hidden />
        </span>
      </div>
      <svg viewBox="0 0 100 20" preserveAspectRatio="none" className="h-5 w-full text-muted-foreground/50" aria-hidden>
        <line x1="0" y1="10" x2="100" y2="10" stroke="currentColor" strokeWidth="1.5" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
      </svg>
      <p className="text-xs text-muted-foreground">No data yet</p>
    </div>
  );
}
