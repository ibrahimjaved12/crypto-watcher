import { ArrowLeftRight, Coins, Droplet, Moon, type LucideIcon } from "lucide-react";

import type { AssetClass } from "@/lib/market/instruments";
import {
  FX_TILE_CHANGE_WINDOW,
  changeTone,
  formatChange,
  formatPrice,
  formatSpread,
  splitPrice,
  type FxQuote,
  type FxQuoteState,
} from "@/lib/market/fx-quotes";
import { cn } from "@/lib/utils";

/**
 * One forex instrument on the Market page (#398). Shape and colour carry the state: a solid green dot with a
 * halo is live, a hollow amber ring (and an amber left edge) is stale, a moon is a closed market, a small grey
 * dot is no data. Phones get a compact card (symbol, state and the change on top, the bid below, ask/spread
 * and the sparkline wrapping under it); from `sm` the full stacked layout with the class chip and name.
 */
const CLASS_STYLE: Partial<Record<AssetClass, { icon: LucideIcon; chip: string }>> = {
  metal: { icon: Coins, chip: "bg-warn/15 text-warn ring-warn/30" },
  energy: { icon: Droplet, chip: "bg-chart-2/15 text-chart-2 ring-chart-2/30" },
  fx: { icon: ArrowLeftRight, chip: "bg-primary/15 text-primary ring-primary/30" },
};
const TONE_TEXT = { bull: "text-bull", bear: "text-bear", muted: "text-muted-foreground" } as const;
const TONE_CAPSULE = {
  bull: "bg-bull/12 text-bull ring-bull/25",
  bear: "bg-bear/12 text-bear ring-bear/25",
  muted: "bg-muted text-muted-foreground ring-border",
} as const;
const STATE_WORD: Record<FxQuoteState, string> = { live: "live", stale: "delayed", closed: "closed", none: "no data yet" };

export type FxQuoteTileProps = {
  symbol: string;
  /** Human name for wider screens (Gold, WTI crude, EUR / USD). */
  name?: string | undefined;
  assetClass: AssetClass;
  quote: FxQuote | null;
  state: FxQuoteState;
};

function ariaLabel({ symbol, quote, state }: FxQuoteTileProps): string {
  if (!quote || state === "none") return `${symbol}, no data yet`;
  const change = quote.changes[FX_TILE_CHANGE_WINDOW] ?? null;
  const move = formatChange(change);
  const trend = move === null ? "" : `, 1 hour ${change! >= 0 ? "up" : "down"} ${move}`;
  return `${symbol} bid ${formatPrice(quote.bid, quote.digits)} ask ${formatPrice(quote.ask, quote.digits)} spread ${
    formatSpread(quote.spreadPoints, quote.digits)}${trend}, ${STATE_WORD[state]}`;
}

export function FxQuoteTile(props: FxQuoteTileProps) {
  const { symbol, name, assetClass, quote, state } = props;
  const style = CLASS_STYLE[assetClass] ?? CLASS_STYLE.fx!;
  const Icon = style.icon;
  const hasQuote = quote !== null && state !== "none";
  const change = hasQuote ? (quote.changes[FX_TILE_CHANGE_WINDOW] ?? null) : null;
  const tone = changeTone(change);
  return (
    <article
      aria-label={ariaLabel(props)}
      className={cn(
        "panel flex min-w-0 flex-col overflow-hidden rounded-2xl border border-border p-3 sm:p-4",
        state === "stale" && "border-l-2 border-l-warn",
      )}
    >
      <div aria-hidden className="flex min-w-0 flex-1 flex-col">
        {/* Header. Phone: symbol + state, change at the top right. sm+: symbol and name, class chip with the
            state as a corner badge. */}
        <div className="flex min-w-0 items-start gap-2">
          <div className="min-w-0 flex-1">
            <p className="flex min-w-0 items-center gap-1.5">
              <span className="truncate font-display text-sm font-semibold sm:text-lg">{symbol}</span>
              <span className="sm:hidden">
                <StateMark state={state} />
              </span>
            </p>
            {name ? <p className="hidden truncate text-xs text-muted-foreground sm:block">{name}</p> : null}
          </div>
          {hasQuote ? <ChangeCapsule change={change} className="sm:hidden" /> : null}
          <span className={cn("relative hidden size-9 shrink-0 place-items-center rounded-xl ring-1 sm:grid", style.chip)}>
            <Icon className="size-4" />
            <span className="absolute -top-1 -right-1 grid size-4 place-items-center rounded-full bg-card ring-2 ring-card">
              <StateMark state={state} />
            </span>
          </span>
        </div>

        {hasQuote ? (
          <>
            <Price value={quote.bid} digits={quote.digits} className="mt-2 text-xl sm:mt-3 sm:text-2xl" />
            <div className="mt-1.5 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1.5 text-[11px] sm:text-xs">
              <span className="inline-flex min-w-0 items-baseline gap-1">
                <span className="text-muted-foreground">Ask</span>
                <span className="num text-foreground/85">{formatPrice(quote.ask, quote.digits)}</span>
              </span>
              <span className="num inline-flex shrink-0 items-center rounded-full bg-muted px-1.5 py-px text-[10px] text-muted-foreground ring-1 ring-border sm:text-[11px]">
                {formatSpread(quote.spreadPoints, quote.digits)}
              </span>
              <Sparkline
                points={quote.chart}
                tone={tone}
                dim={state === "closed"}
                className="mt-0.5 h-7 basis-full sm:mt-2 sm:h-10"
              />
            </div>
            <div className="mt-auto hidden items-center gap-2 pt-3 sm:flex">
              <ChangeCapsule change={change} />
              <span className="num text-[11px] text-muted-foreground">1h</span>
              {state === "closed" ? <span className="ml-auto text-[11px] text-muted-foreground">Closed</span> : null}
            </div>
          </>
        ) : (
          <>
            <svg viewBox="0 0 100 20" preserveAspectRatio="none" className="mt-3 h-5 w-full text-muted-foreground/50">
              <line x1="0" y1="10" x2="100" y2="10" stroke="currentColor" strokeWidth="1.5" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
            </svg>
            <p className="mt-2 text-xs text-muted-foreground">No data yet</p>
          </>
        )}
      </div>
    </article>
  );
}

/** Loading placeholder with the tile's footprint, so the grid does not shift when quotes arrive. */
export function FxQuoteTileSkeleton() {
  return (
    <div aria-hidden className="panel flex min-w-0 flex-col gap-2.5 rounded-2xl border border-border p-3 sm:gap-3 sm:p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="h-4 w-16 rounded bg-muted motion-safe:animate-pulse sm:h-5" />
        <span className="hidden size-9 rounded-xl bg-muted motion-safe:animate-pulse sm:block" />
      </div>
      <span className="h-6 w-24 rounded bg-muted motion-safe:animate-pulse sm:h-7" />
      <span className="h-7 w-full rounded bg-muted/60 motion-safe:animate-pulse sm:h-10" />
    </div>
  );
}

function StateMark({ state }: { state: FxQuoteState }) {
  if (state === "live") {
    return (
      <span className="relative flex size-2 shrink-0" title="Live">
        <span className="absolute inline-flex size-full rounded-full bg-bull opacity-60 motion-safe:animate-ping" />
        <span className="relative inline-flex size-2 rounded-full bg-bull" />
      </span>
    );
  }
  if (state === "stale") return <span className="block size-2 shrink-0 rounded-full border-2 border-warn" title="Delayed" />;
  if (state === "closed") return <Moon className="size-3 shrink-0 text-muted-foreground" />;
  return <span className="block size-1.5 shrink-0 rounded-full bg-muted-foreground/50" title="No data yet" />;
}

/** The bid, with the last digit group slightly dimmer so the big figure reads first. */
function Price({ value, digits, className }: { value: number; digits: number; className?: string }) {
  const [head, tail] = splitPrice(formatPrice(value, digits), digits);
  return (
    <p className={cn("num font-display leading-none font-semibold tracking-tight", className)}>
      {head}
      <span className="text-foreground/55">{tail}</span>
    </p>
  );
}

function ChangeCapsule({ change, className }: { change: number | null; className?: string }) {
  const tone = changeTone(change);
  const text = formatChange(change);
  return (
    <span
      className={cn(
        "num inline-flex shrink-0 items-center gap-0.5 rounded-full px-1.5 py-px text-[10px] font-medium ring-1 sm:px-2 sm:py-0.5 sm:text-xs",
        TONE_CAPSULE[tone],
        className,
      )}
    >
      {text === null ? "–" : `${tone === "bear" ? "▼" : tone === "bull" ? "▲" : ""} ${text}`.trim()}
    </span>
  );
}

/** 24 h of 15-minute bid closes: line plus a 10 % area, no axes. Time-scaled, so omitted buckets show as gaps
 * in spacing rather than being squeezed together. Fewer than two points: the dashed placeholder line. */
function Sparkline({
  points,
  tone,
  dim,
  className,
}: {
  points: readonly { time: number; close: number }[];
  tone: "bull" | "bear" | "muted";
  dim: boolean;
  className?: string;
}) {
  if (points.length < 2) {
    return (
      <svg viewBox="0 0 100 20" preserveAspectRatio="none" className={cn("w-full text-muted-foreground/50", className)}>
        <line x1="0" y1="10" x2="100" y2="10" stroke="currentColor" strokeWidth="1.5" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
      </svg>
    );
  }
  const t0 = points[0]!.time;
  const span = Math.max(1, points.at(-1)!.time - t0);
  const closes = points.map((p) => p.close);
  const lo = Math.min(...closes);
  const hi = Math.max(...closes);
  const range = hi - lo || 1;
  // 2 units of padding top and bottom so the 1.5 px line is never clipped.
  const xy = points.map((p) => [((p.time - t0) / span) * 100, 30 - ((p.close - lo) / range) * 28] as const);
  const line = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  const area = `${line} L100,32 L0,32 Z`;
  return (
    <svg
      viewBox="0 0 100 32"
      preserveAspectRatio="none"
      className={cn("w-full", TONE_TEXT[tone], dim && "opacity-60 grayscale", className)}
    >
      <path d={area} fill="currentColor" className="opacity-10" />
      <path
        d={line}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
