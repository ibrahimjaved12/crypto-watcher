import { useQuery } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import {
  Bell,
  BellRing,
  CandlestickChart,
  FlaskConical,
  Moon,
  Plug,
  Server,
  Timer,
  Wallet,
  type LucideIcon,
} from "lucide-react";

import { FxQuoteTile, FxQuoteTileSkeleton } from "@/components/market/fx-quote-tile";
import { EmptyState, PageHeader } from "@/components/plain";
import { automaticQueryOptions } from "@/lib/activity-controls";
import { getForexQuotes } from "@/lib/forex-market.functions";
import { formatCountdown } from "@/lib/market/countdown";
import { fxFeedState, fxQuoteState, type FxFeedState, type FxQuote } from "@/lib/market/fx-quotes";
import { FOREX_DEFAULTS, type Instrument } from "@/lib/market/instruments";
import { FX_SESSIONS, fxNextOpenMs, fxSessionTags, fxWeekWindow, isFxOpen } from "@/lib/market/trading-calendar";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

/**
 * Forex screens (#374). The trading calendar, the session clock and, on the Market page, the published quotes
 * (#398: bid, ask, spread, 1h change and a 24 h sparkline from `fx_market_quotes`) are real; there are no
 * charts, signals or accounts yet, and nothing here pretends otherwise.
 */
type ForexPage = "market" | "paper" | "alerts" | "lab" | "control";
/** Same eyebrow and title as each crypto page: the teal theme alone says which market is shown. */
const PAGES: Record<ForexPage, { eyebrow: string; title: string; subtitle: string }> = {
  market: { eyebrow: "Live market", title: "Market", subtitle: "Prices, spreads and trading sessions for your forex pairs." },
  lab: { eyebrow: "Forward test", title: "Strategy Lab", subtitle: "Strategies are tested here with fake money and realistic fees." },
  paper: {
    eyebrow: "Simulated · fake money",
    title: "Paper account",
    subtitle: "The simulated forex wallet: what it holds, what it did, and what it was not allowed to do.",
  },
  alerts: { eyebrow: "Mission log", title: "Alerts", subtitle: "Every forex alert your rules raise will be logged here." },
  control: {
    eyebrow: "Settings & health",
    title: "Control room",
    subtitle: "Choose when you get alerts and what is collected, and check that everything is running.",
  },
};
const EMPTY: Partial<Record<ForexPage, { icon: LucideIcon; title: string; body: string }>> = {
  paper: { icon: Wallet, title: "No forex trades yet", body: "Your separate USD paper account appears with the first forex trade." },
  alerts: { icon: BellRing, title: "No forex alerts yet", body: "Alerts from your forex rules will be logged here." },
  lab: {
    icon: FlaskConical,
    title: "No forex strategies running yet",
    body: "Forex strategies are tested here once the data feed is connected.",
  },
};
const PIPELINE: readonly { icon: LucideIcon; name: string; state: string }[] = [
  { icon: Plug, name: "Data feed", state: "Not connected" },
  { icon: Server, name: "Collector", state: "Not started" },
  { icon: Timer, name: "Hourly run", state: "Not started" },
  { icon: Bell, name: "Alerts", state: "Off" },
];
const SESSION_LABELS: Record<string, string> = {
  sydney: "Sydney",
  tokyo: "Tokyo",
  london: "London",
  new_york: "New York",
};
/** UNVERIFIED placeholder: the Market page polls the published quotes every 15 s (the publisher writes once a
 * minute, so a new bar shows within about 15 s of its publication). */
const QUOTE_REFRESH_MS = 15_000;
const COMING: readonly { icon: LucideIcon; title: string }[] = [
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

/** The default instruments first, then any other published symbol, each with its quote and state. */
function tileRows(quotes: readonly FxQuote[], now: number, open: boolean) {
  const bySymbol = new Map(quotes.map((quote) => [quote.symbol, quote]));
  const symbolOf = (instrument: Instrument) => instrument.instrumentId.split(":")[1] ?? instrument.display;
  const defaults = FOREX_DEFAULTS.map((instrument) => {
    const symbol = symbolOf(instrument);
    const quote = bySymbol.get(symbol) ?? null;
    return {
      symbol,
      name: instrument.display !== symbol ? instrument.display : `${instrument.base} / ${instrument.quote}`,
      assetClass: instrument.assetClass,
      quote,
      state: fxQuoteState(quote, now, open),
    };
  });
  const known = new Set(defaults.map((row) => row.symbol));
  const extras = quotes
    .filter((quote) => !known.has(quote.symbol))
    .sort((a, b) => a.symbol.localeCompare(b.symbol))
    .map((quote) => ({ symbol: quote.symbol, name: undefined, assetClass: "fx" as const, quote,
      state: fxQuoteState(quote, now, open) }));
  return [...defaults, ...extras];
}

export function ForexLanding({ page }: { page: ForexPage }) {
  const now = useNow(30_000);
  const meta = PAGES[page];
  const empty = EMPTY[page];
  const loadQuotes = useServerFn(getForexQuotes);
  const automatic = automaticQueryOptions(import.meta.env["VITE_MARKET_AUTO_REFRESH_ENABLED"]);
  // Read once whenever the Market page is shown; poll every 15 s only where automatic refresh is enabled.
  const quotes = useQuery({
    queryKey: ["forex-quotes"],
    queryFn: () => loadQuotes(),
    enabled: page === "market",
    refetchInterval: page === "market" && automatic.enabled ? QUOTE_REFRESH_MS : false,
    ...(automatic.enabled ? {} : { retry: false as const }),
  });
  const open = isFxOpen(now);
  const tiles = tileRows(quotes.data?.quotes ?? [], now, open);
  const feed: FeedChipState = quotes.isError
    ? "unavailable"
    : quotes.isPending
      ? "loading"
      : fxFeedState(tiles.map((tile) => tile.state), open);
  return (
    <div className="space-y-6 overflow-x-clip">
      <PageHeader eyebrow={meta.eyebrow} title={meta.title} subtitle={meta.subtitle} />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <MarketStateCard now={now} feed={page === "market" ? feed : null} />
        {page === "market" ? <SessionStrip now={now} /> : null}
        {page === "control" ? <PipelineCard /> : null}
        {empty ? <EmptyState icon={empty.icon} title={empty.title} body={empty.body} className="panel rounded-2xl" /> : null}
      </div>
      {page === "market" ? (
        <>
          <div className="grid grid-cols-2 gap-2.5 sm:gap-3 lg:grid-cols-4" aria-busy={quotes.isPending}>
            {quotes.isPending
              ? FOREX_DEFAULTS.map((instrument) => <FxQuoteTileSkeleton key={instrument.instrumentId} />)
              : tiles.map((tile) => <FxQuoteTile key={tile.symbol} {...tile} />)}
          </div>
          <div className="grid grid-cols-2 gap-2 sm:gap-3">
            {COMING.map(({ icon: Icon, title }) => (
              <div
                key={title}
                className="panel flex min-w-0 flex-col items-center gap-2 rounded-2xl border border-border p-2.5 sm:flex-row sm:gap-3 sm:p-3"
              >
                <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/12 text-primary ring-1 ring-primary/25">
                  <Icon className="size-4" aria-hidden />
                </span>
                <span className="text-center text-[11px] leading-tight font-medium sm:text-left sm:text-sm">{title}</span>
              </div>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}

type FeedChipState = FxFeedState | "loading" | "unavailable";
const FEED_CHIP: Record<Exclude<FeedChipState, "loading">, { text: string; tone: string; dot: string }> = {
  live: { text: "Live", tone: "bg-bull/15 text-bull", dot: "bg-bull" },
  delayed: { text: "Delayed", tone: "bg-warn/15 text-warn", dot: "border-2 border-warn" },
  none: { text: "No data", tone: "bg-muted text-muted-foreground", dot: "bg-current" },
  closed: { text: "Closed", tone: "bg-muted text-muted-foreground", dot: "" },
  unavailable: { text: "Unavailable", tone: "bg-muted text-muted-foreground", dot: "bg-current" },
};

/** The data-feed chip: same shape as NotYetChip, tone from the quotes; a skeleton of the same size while loading. */
function FeedChip({ state, className }: { state: FeedChipState; className?: string }) {
  if (state === "loading") {
    return <span aria-hidden className={cn("inline-block h-5 w-16 shrink-0 rounded-full bg-muted motion-safe:animate-pulse", className)} />;
  }
  const chip = FEED_CHIP[state];
  return (
    <span className={cn("inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs", chip.tone, className)}>
      {state === "closed" ? <Moon className="size-3" aria-hidden /> : <span className={cn("size-1.5 rounded-full", chip.dot)} aria-hidden />}
      {chip.text}
    </span>
  );
}

function MarketStateCard({ now, feed }: { now: number; feed: FeedChipState | null }) {
  const open = isFxOpen(now);
  const next = open ? fxWeekWindow(now)[1] : fxNextOpenMs(now);
  return (
    <section className="panel flex flex-col gap-3 rounded-2xl border border-border p-5" aria-label="Forex market state">
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
      <div>
        <p className="num font-display text-2xl font-semibold">
          {open ? "Closes in" : "Opens in"} {formatCountdown(next - now)}
        </p>
        <p className="mt-0.5 text-sm text-muted-foreground">
          <span className="num">{whenFormat.format(next)}</span>
        </p>
      </div>
      {feed ? (
        <div className="mt-auto flex items-center gap-2 border-t border-border/60 pt-3 text-sm">
          <Plug className="size-4 text-muted-foreground" aria-hidden />
          <span>Data feed</span>
          <FeedChip state={feed} className="ml-auto" />
        </div>
      ) : null}
    </section>
  );
}

function NotYetChip({ text, className }: { text: string; className?: string }) {
  return (
    <span className={cn("inline-flex shrink-0 items-center gap-1.5 rounded-full bg-muted px-2.5 py-0.5 text-xs text-muted-foreground", className)}>
      <span className="size-1.5 rounded-full bg-current" aria-hidden />
      {text}
    </span>
  );
}

/** Control room: the forex pipeline, all muted, because none of it runs yet. */
function PipelineCard() {
  return (
    <section className="panel rounded-2xl border border-border p-5" aria-label="Forex pipeline">
      <ol>
        {PIPELINE.map(({ icon: Icon, name, state }, i) => (
          <li key={name} className="relative flex items-center gap-3 pb-5 last:pb-0">
            {i < PIPELINE.length - 1 ? (
              <span aria-hidden className="absolute top-8 bottom-0 left-4 border-l border-dashed border-border" />
            ) : null}
            <span className="relative grid size-8 shrink-0 place-items-center rounded-full bg-muted text-muted-foreground ring-1 ring-border">
              <Icon className="size-4" aria-hidden />
            </span>
            <span className="min-w-0 flex-1 text-sm font-medium">{name}</span>
            <NotYetChip text={state} />
          </li>
        ))}
      </ol>
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
              <div className="relative h-3.5 overflow-hidden rounded-full bg-background/60 ring-1 ring-border/60 sm:h-3">
                {localSegments(timeZone, start, end, now).map(([from, to]) => (
                  <span
                    key={from}
                    className={cn("absolute inset-y-0 rounded-full", on ? "bg-primary" : "bg-foreground/10")}
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
