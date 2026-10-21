import {
  ArrowDownRight,
  ArrowUpRight,
  CalendarRange,
  Check,
  ChevronDown,
  Inbox,
  RefreshCw,
  type LucideIcon,
} from "lucide-react";
import { useId, useMemo, useState, type ReactNode } from "react";

import { Hint } from "@/components/hint";
import { StatusChip } from "@/components/plain";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  dateTimeText,
  formatPercent,
  formatUsdt,
  type EquityPoint,
  type ListState,
  type ResultsView,
  type Signed,
  type Tone,
} from "@/lib/paper/paper-account";
import { EQUITY_RANGES, rangeChange, rangePoints, type EquityRange } from "@/lib/paper/paper-view";
import { cn } from "@/lib/utils";

/**
 * Paper account design kit (#454), shared by the crypto and forex accounts so both read as one app; the market
 * accent comes from `--primary` (violet for crypto, teal for forex). Colour carries meaning and is never alone:
 * green/red only for profit and loss (with ▲/▼), amber for stale, muted for rejected.
 *
 * Breakpoints. Phone (< 640 px) has its own components: a hero card with the curve full-bleed, a horizontally
 * snapping metric row, a segmented control pinned under the header and two-line cards. Tablet (640-1023 px) uses
 * the desktop hero with a 4-up tile grid and two-column cards. Desktop (>= 1024 px) is a two-column grid (wide
 * equity card, stacked tiles) with tables whose headers stick under the tab bar.
 */

// Header 48 px + 2 px market line; the tab bar is a fixed 60 px (phone) / 56 px (desktop), so table headers stick
// right under it.
export const STICKY_TABS = "top-[50px]";
export const STICKY_THEAD = "lg:top-[106px]";

export const TONE_TEXT: Record<Tone, string> = {
  gain: "text-bull",
  loss: "text-bear",
  flat: "text-muted-foreground",
};
const TONE_CAPSULE: Record<Tone, string> = {
  gain: "bg-bull/12 text-bull ring-bull/25",
  loss: "bg-bear/12 text-bear ring-bear/25",
  flat: "bg-muted text-muted-foreground ring-border",
};

export function SignedText({ value, className = "" }: { value: Signed | null; className?: string }) {
  if (!value) return <span className="num text-muted-foreground">—</span>;
  return (
    <span className={cn("num whitespace-nowrap", TONE_TEXT[value.tone], className)}>
      {value.arrow ? <span aria-hidden>{value.arrow} </span> : null}
      {value.text}
    </span>
  );
}

/** A signed value in a capsule (hero P&L, R multiples). */
export function SignedCapsule({
  value,
  label,
  suffix,
  className,
}: {
  value: Signed | null;
  label?: string | undefined;
  suffix?: string | undefined;
  className?: string | undefined;
}) {
  const tone = value?.tone ?? "flat";
  return (
    <span
      className={cn(
        "num inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1",
        TONE_CAPSULE[tone],
        className,
      )}
    >
      {label ? <span className="font-sans font-normal opacity-80">{label}</span> : null}
      {value ? (
        <span>
          {value.arrow ? <span aria-hidden>{value.arrow} </span> : null}
          {value.text}
          {suffix ? <span className="opacity-75"> {suffix}</span> : null}
        </span>
      ) : (
        "—"
      )}
    </span>
  );
}

export function SymbolPill({ symbol, className }: { symbol: string; className?: string | undefined }) {
  return (
    <span
      className={cn(
        "num inline-flex h-6 items-center rounded-md bg-muted/80 px-2 text-xs font-semibold tracking-tight text-foreground ring-1 ring-border/70",
        className,
      )}
    >
      {symbol || "—"}
    </span>
  );
}

export function SideTag({ side }: { side: 1 | -1 | null }) {
  if (side === null) return null;
  const Icon = side === 1 ? ArrowUpRight : ArrowDownRight;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-0.5 text-[11px] font-semibold tracking-wide uppercase",
        side === 1 ? "text-bull" : "text-bear",
      )}
    >
      <Icon className="size-3.5" aria-hidden />
      {side === 1 ? "Long" : "Short"}
    </span>
  );
}

/** Icon + word chip for an outcome or reason: good (green), bad (red), neutral (muted). */
export function ReasonChip({
  icon: Icon,
  label,
  tone,
  hint,
}: {
  icon: LucideIcon;
  label: string;
  tone: Tone;
  hint?: string | undefined;
}) {
  const chip = (
    <span
      className={cn(
        "inline-flex h-6 items-center gap-1 rounded-full px-2 text-[11px] font-medium whitespace-nowrap ring-1",
        TONE_CAPSULE[tone],
      )}
    >
      <Icon className="size-3.5" aria-hidden />
      {label}
    </span>
  );
  if (!hint) return chip;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button type="button" className="cursor-help rounded-full">
          {chip}
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-64 border border-border bg-popover text-xs text-popover-foreground">
        {hint}
      </TooltipContent>
    </Tooltip>
  );
}

// ---------------------------------------------------------------- freshness

export type PillState = "fresh" | "delayed" | "stale" | "waiting";

/**
 * One short pill: a green dot when current, amber when delayed or stale (with the time of the last run), and the
 * Market page's quiet "not reporting" chip while nothing has arrived yet.
 */
export function FreshnessPill({
  state,
  atMs,
  waiting = "Waiting for data",
}: {
  state: PillState;
  atMs: number | null;
  waiting?: string;
}) {
  if (state === "waiting") return <StatusChip level="wait">{waiting}</StatusChip>;
  const time = atMs === null ? "" : dateTimeText(atMs);
  if (state === "fresh")
    return (
      <span className="num inline-flex h-7 items-center gap-2 rounded-full bg-bull/10 px-2.5 text-xs text-foreground/90 ring-1 ring-bull/25">
        <span className="relative flex size-2">
          <span className="absolute inline-flex size-full rounded-full bg-bull opacity-60 motion-safe:animate-ping" />
          <span className="relative inline-flex size-2 rounded-full bg-bull" />
        </span>
        <span className="font-sans">Updated</span> {time}
      </span>
    );
  return (
    <span
      className={cn(
        "num inline-flex h-7 items-center gap-2 rounded-full bg-warn/12 px-2.5 text-xs text-warn ring-1 ring-warn/35",
      )}
    >
      <span className="block size-2 rounded-full border-2 border-warn" aria-hidden />
      <span className="font-sans font-medium">{state === "stale" ? "Stale" : "Delayed"}</span>
      <span className="text-warn/85">· last run {time}</span>
    </span>
  );
}

export function RefreshButton({ fetching, onRefresh }: { fetching: boolean; onRefresh: () => void }) {
  return (
    <Button
      variant="outline"
      size="icon"
      className="size-11 rounded-full sm:size-9"
      onClick={onRefresh}
      disabled={fetching}
      aria-label="Refresh"
      title="Refresh"
    >
      <RefreshCw className={cn("size-4", fetching && "motion-safe:animate-spin")} aria-hidden />
    </Button>
  );
}

// ---------------------------------------------------------------- full-screen states

/** An honest state with one short name: an icon in an orb, the name, and an optional pill or action. */
export function StateScreen({
  icon: Icon,
  title,
  tone = "primary",
  children,
}: {
  icon: LucideIcon;
  title: string;
  tone?: "primary" | "muted" | "warn" | "bear";
  children?: ReactNode;
}) {
  const ring = {
    primary: "text-primary ring-primary/30 bg-primary/10",
    muted: "text-muted-foreground ring-border bg-muted/50",
    warn: "text-warn ring-warn/30 bg-warn/10",
    bear: "text-bear ring-bear/30 bg-bear/10",
  }[tone];
  return (
    <section className="panel grid min-h-[45vh] place-items-center rounded-2xl px-4 py-12 text-center sm:min-h-80">
      <div className="flex flex-col items-center gap-4">
        <span className={cn("grid size-16 place-items-center rounded-full ring-1", ring)}>
          <Icon className="size-7" aria-hidden />
        </span>
        <h2 className="font-display text-xl font-semibold">{title}</h2>
        {children}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------- metric tiles

export type Metric = {
  key: string;
  icon: LucideIcon;
  label: string;
  hint?: string | undefined;
  value: string;
  unit?: string | undefined;
  tone?: Tone | null | undefined;
  sub?: ReactNode;
  /** 0..1 fill of a thin bar under the value (utilization, risk share); amber from `warnAt`. */
  meter?: { fraction: number; warnAt: number; label: string } | null | undefined;
};

function Meter({ meter }: { meter: NonNullable<Metric["meter"]> }) {
  const width = Math.max(0, Math.min(1, meter.fraction)) * 100;
  const warn = meter.fraction >= meter.warnAt;
  return (
    <div
      className="mt-2 h-1 w-full overflow-hidden rounded-full bg-muted"
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(width)}
      aria-label={meter.label}
    >
      <div className={cn("h-full rounded-full", warn ? "bg-warn" : "bg-primary")} style={{ width: `${width}%` }} />
    </div>
  );
}

export function MetricTile({ metric }: { metric: Metric }) {
  const Icon = metric.icon;
  return (
    <div className="panel min-w-0 rounded-xl p-3 sm:p-4">
      <p className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
        <Icon className="size-3.5 text-primary/80" aria-hidden />
        {metric.hint ? <Hint text={metric.hint}>{metric.label}</Hint> : metric.label}
      </p>
      <p
        className={cn(
          "num mt-1.5 truncate text-lg leading-tight font-semibold sm:text-xl",
          metric.tone ? TONE_TEXT[metric.tone] : "text-foreground",
        )}
      >
        {metric.tone === "gain" ? <span aria-hidden>▲ </span> : metric.tone === "loss" ? <span aria-hidden>▼ </span> : null}
        {metric.value}
        {metric.unit ? <span className="ml-1 text-xs font-normal text-muted-foreground">{metric.unit}</span> : null}
      </p>
      {metric.meter ? <Meter meter={metric.meter} /> : null}
      {metric.sub ? <p className="mt-1 truncate text-[11px] text-muted-foreground">{metric.sub}</p> : null}
    </div>
  );
}

/**
 * Phone: one row that scrolls sideways and snaps per tile (the page itself never scrolls sideways). Tablet: four
 * across. Desktop: a vertical stack beside the equity card.
 */
export function MetricRail({ metrics, label }: { metrics: Metric[]; label: string }) {
  return (
    <section aria-label={label}>
      <ul className="-mx-4 flex snap-x snap-mandatory scroll-px-4 gap-2 overflow-x-auto px-4 pb-1 [scrollbar-width:none] sm:hidden [&::-webkit-scrollbar]:hidden">
        {metrics.map((metric) => (
          <li key={metric.key} className="w-[42%] shrink-0 snap-start">
            <MetricTile metric={metric} />
          </li>
        ))}
      </ul>
      <ul className="hidden gap-3 sm:grid sm:grid-cols-4 lg:grid-cols-1">
        {metrics.map((metric) => (
          <li key={metric.key}>
            <MetricTile metric={metric} />
          </li>
        ))}
      </ul>
    </section>
  );
}

export function MetricRailSkeleton() {
  return (
    <div className="flex gap-2 overflow-hidden sm:grid sm:grid-cols-4 sm:gap-3 lg:grid-cols-1" aria-hidden>
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="h-[92px] w-[42%] shrink-0 rounded-xl bg-muted/50 motion-safe:animate-pulse sm:w-auto" />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- equity curve

const W = 600;

/**
 * The balance line in the market accent with a soft fill, a dashed reference at the first point of the range and
 * a dot at the last point (positioned in HTML so the stretched SVG never turns it into an oval).
 */
export function EquityCurve({
  points,
  className,
  label,
}: {
  points: readonly EquityPoint[];
  className?: string | undefined;
  label: string;
}) {
  const gradient = useId();
  if (points.length < 2)
    return (
      <div className={cn("grid place-items-center text-xs text-muted-foreground", className)}>
        <svg viewBox="0 0 100 20" preserveAspectRatio="none" className="h-5 w-full text-muted-foreground/40" aria-hidden>
          <line x1="0" y1="10" x2="100" y2="10" stroke="currentColor" strokeWidth="1.5" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
        </svg>
      </div>
    );
  const H = 200;
  const t0 = points[0]!.ms;
  const span = Math.max(1, points[points.length - 1]!.ms - t0);
  const values = points.map((p) => p.value);
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const pad = (hi - lo || Math.abs(hi) * 0.01 || 1) * 0.12;
  const low = lo - pad;
  const high = hi + pad;
  const y = (v: number) => H - ((v - low) / (high - low)) * H;
  const x = (ms: number) => ((ms - t0) / span) * W;
  // Step line: the balance holds its value until the next ledger line.
  let d = `M${x(points[0]!.ms).toFixed(1)},${y(points[0]!.value).toFixed(1)}`;
  for (let i = 1; i < points.length; i++) {
    const p = points[i]!;
    d += ` H${x(p.ms).toFixed(1)} V${y(p.value).toFixed(1)}`;
  }
  const base = y(points[0]!.value);
  const last = points[points.length - 1]!;
  return (
    <div className={cn("relative", className)}>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="size-full text-primary" role="img" aria-label={label}>
        <defs>
          <linearGradient id={gradient} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.28" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
        </defs>
        <line
          x1="0"
          x2={W}
          y1={base}
          y2={base}
          className="text-muted-foreground"
          stroke="currentColor"
          strokeOpacity="0.45"
          strokeDasharray="3 5"
          vectorEffect="non-scaling-stroke"
        />
        <path d={`${d} V${H} H0 Z`} fill={`url(#${gradient})`} />
        <path d={d} fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      </svg>
      <span
        aria-hidden
        className="absolute size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary ring-4 ring-primary/25"
        style={{ left: `${(x(last.ms) / W) * 100}%`, top: `${(y(last.value) / H) * 100}%` }}
      />
    </div>
  );
}

/** Drawdown from the running peak as a thin red "underwater" strip (desktop only). */
export function DrawdownStrip({ points }: { points: readonly EquityPoint[] }) {
  if (points.length < 2) return null;
  let peak = points[0]!.value;
  const depth = points.map((p) => {
    peak = Math.max(peak, p.value);
    return peak > 0 ? (peak - p.value) / peak : 0;
  });
  const worst = Math.max(...depth);
  if (worst <= 0) return null;
  const t0 = points[0]!.ms;
  const span = Math.max(1, points[points.length - 1]!.ms - t0);
  const path = points
    .map((p, i) => `${i ? "L" : "M"}${(((p.ms - t0) / span) * W).toFixed(1)},${((depth[i]! / worst) * 30).toFixed(1)}`)
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${W} 30`}
      preserveAspectRatio="none"
      className="h-6 w-full text-bear"
      role="img"
      aria-label={`Drawdown from peak; worst in this range ${formatPercent(-worst)}`}
    >
      <path d={`${path} L${W},0 L0,0 Z`} fill="currentColor" fillOpacity="0.22" />
    </svg>
  );
}

// ---------------------------------------------------------------- range controls

export function RangeChips({ value, onChange }: { value: EquityRange; onChange: (range: EquityRange) => void }) {
  return (
    <div role="radiogroup" aria-label="Range" className="inline-flex rounded-full bg-muted/70 p-0.5 ring-1 ring-border/70">
      {EQUITY_RANGES.map((range) => (
        <button
          key={range.key}
          type="button"
          role="radio"
          aria-checked={value === range.key}
          title={range.long}
          onClick={() => onChange(range.key)}
          className={cn(
            "num h-7 min-w-10 rounded-full px-2.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground",
            value === range.key && "bg-background text-foreground shadow-sm ring-1 ring-primary/40",
          )}
        >
          {range.label}
        </button>
      ))}
    </div>
  );
}

/** Phone: the range is an icon-sized menu (44 px target) instead of a chip row. */
export function RangeMenu({ value, onChange }: { value: EquityRange; onChange: (range: EquityRange) => void }) {
  const [open, setOpen] = useState(false);
  const current = EQUITY_RANGES.find((range) => range.key === value);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Range: ${current?.long ?? ""}`}
          className="relative grid size-11 place-items-center rounded-full bg-muted/70 text-foreground ring-1 ring-border/70"
        >
          <CalendarRange className="size-5" aria-hidden />
          <span className="num absolute -right-1 -bottom-1 rounded-full bg-primary px-1 text-[9px] leading-4 font-semibold text-primary-foreground">
            {current?.label}
          </span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-48 p-1">
        <ul role="listbox" aria-label="Range">
          {EQUITY_RANGES.map((range) => (
            <li key={range.key}>
              <button
                type="button"
                role="option"
                aria-selected={value === range.key}
                onClick={() => {
                  onChange(range.key);
                  setOpen(false);
                }}
                className="flex min-h-11 w-full items-center gap-2 rounded-md px-3 text-sm hover:bg-accent"
              >
                <span className="num w-8 text-xs text-muted-foreground">{range.label}</span>
                {range.long}
                {value === range.key ? <Check className="ml-auto size-4 text-primary" aria-hidden /> : null}
              </button>
            </li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}

// ---------------------------------------------------------------- hero card

export type HeroProps = {
  label: string;
  value: string;
  unit: string;
  /** P&L capsules beside the headline (total, today). */
  capsules: ReactNode;
  points: readonly EquityPoint[];
  nowMs: number;
  stale: boolean;
  footer?: ReactNode;
};

function useRange(points: readonly EquityPoint[], nowMs: number) {
  const [range, setRange] = useState<EquityRange>("all");
  const shown = useMemo(() => rangePoints(points, range, nowMs), [points, range, nowMs]);
  return { range, setRange, shown, change: rangeChange(shown) };
}

function RangeChange({ change, unit }: { change: ReturnType<typeof rangeChange>; unit: string }) {
  if (!change) return null;
  const tone: Tone = change.value > 0 ? "gain" : change.value < 0 ? "loss" : "flat";
  return (
    <span className={cn("num text-xs", TONE_TEXT[tone])}>
      {tone === "gain" ? "▲ +" : tone === "loss" ? "▼ −" : ""}
      {formatUsdt(Math.abs(change.value))} {unit}
      {change.fraction !== null ? ` (${formatPercent(Math.abs(change.fraction))})` : ""}
    </span>
  );
}

/** Desktop and tablet: a wide card, headline on the left, range chips on the right, the curve below. */
export function HeroDesktop(props: HeroProps) {
  const { range, setRange, shown, change } = useRange(props.points, props.nowMs);
  return (
    <section
      aria-label="Balance and equity curve"
      className={cn("panel hidden flex-col rounded-2xl p-5 sm:flex lg:p-6", props.stale && "ring-1 ring-warn/40")}
    >
      <div className="flex flex-wrap items-start gap-4">
        <div className="min-w-0">
          <p className="eyebrow">{props.label}</p>
          <p className="num mt-2 font-display text-4xl leading-none font-semibold tracking-tight lg:text-5xl">
            {props.value}
            <span className="ml-2 align-top text-sm font-normal text-muted-foreground">{props.unit}</span>
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">{props.capsules}</div>
        </div>
        <div className="ml-auto flex flex-col items-end gap-2">
          <RangeChips value={range} onChange={setRange} />
          <RangeChange change={change} unit={props.unit} />
        </div>
      </div>
      <EquityCurve points={shown} className="mt-5 h-52 lg:h-60" label={`Balance, ${props.value} ${props.unit} now`} />
      <DrawdownStrip points={shown} />
      {props.footer ? <div className="mt-2 text-xs text-muted-foreground">{props.footer}</div> : null}
    </section>
  );
}

/** Phone: headline first, the range as an icon menu, the curve edge to edge at the bottom of the card. */
export function HeroMobile(props: HeroProps) {
  const { range, setRange, shown, change } = useRange(props.points, props.nowMs);
  return (
    <section
      aria-label="Balance and equity curve"
      className={cn(
        "panel -mx-4 overflow-hidden rounded-none border-x-0 sm:hidden",
        props.stale && "border-y-warn/40",
      )}
    >
      <div className="flex items-start gap-3 px-4 pt-4">
        <div className="min-w-0 flex-1">
          <p className="eyebrow">{props.label}</p>
          <p className="num mt-1.5 font-display text-[2.5rem] leading-none font-semibold tracking-tight">
            {props.value}
            <span className="ml-1.5 align-top text-xs font-normal text-muted-foreground">{props.unit}</span>
          </p>
        </div>
        <RangeMenu value={range} onChange={setRange} />
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 px-4">
        {props.capsules}
        <RangeChange change={change} unit={props.unit} />
      </div>
      <EquityCurve points={shown} className="mt-3 h-40 w-full" label={`Balance, ${props.value} ${props.unit} now`} />
    </section>
  );
}

export function HeroSkeleton() {
  return (
    <div aria-hidden>
      <div className="-mx-4 h-[300px] bg-muted/40 motion-safe:animate-pulse sm:hidden" />
      <div className="hidden h-[380px] rounded-2xl bg-muted/40 motion-safe:animate-pulse sm:block" />
    </div>
  );
}

/** Desktop grid: the wide equity card and the tile stack side by side from 1024 px. */
export function AccountGrid({ hero, rail }: { hero: ReactNode; rail: ReactNode }) {
  return (
    <div className="grid gap-3 sm:gap-4 lg:grid-cols-[minmax(0,1fr)_17rem]">
      <div className="min-w-0">{hero}</div>
      <div className="min-w-0">{rail}</div>
    </div>
  );
}

// ---------------------------------------------------------------- tabs

export type TabSpec = { value: string; label: string; count: number | null };

/**
 * The list switcher, pinned under the header. Phone: a full-width segmented control (44 px segments, count under
 * the word). Desktop: compact tabs with a count pill, and an optional action on the right.
 */
export function PaperTabBar({ tabs, action }: { tabs: TabSpec[]; action?: ReactNode }) {
  return (
    <div
      className={cn(
        "sticky z-20 -mx-4 flex h-[60px] items-center gap-3 border-b border-border/60 bg-background/90 px-4 backdrop-blur-md lg:h-14",
        STICKY_TABS,
      )}
    >
      <TabsList className="grid h-11 w-full grid-cols-4 gap-1 rounded-xl bg-muted/70 p-1 lg:flex lg:h-10 lg:w-auto lg:justify-start">
        {tabs.map((tab) => (
          <TabsTrigger
            key={tab.value}
            value={tab.value}
            className="h-full gap-1.5 rounded-lg px-1 text-[13px] data-[state=active]:bg-background data-[state=active]:shadow-[inset_0_-2px_0_var(--color-primary)] lg:px-3.5"
          >
            <span>{tab.label}</span>
            {tab.count !== null ? (
              <span className="num rounded-full bg-muted px-1.5 text-[10px] leading-4 text-muted-foreground">
                {tab.count > 999 ? "999+" : tab.count}
              </span>
            ) : null}
          </TabsTrigger>
        ))}
      </TabsList>
      {action ? <div className="ml-auto hidden lg:block">{action}</div> : null}
    </div>
  );
}

// ---------------------------------------------------------------- lists

export function ListShell({
  state,
  error,
  onRetry,
  empty,
  children,
}: {
  state: ListState;
  error: unknown;
  onRetry: () => void;
  empty: { icon?: LucideIcon | undefined; title: string };
  children: ReactNode;
}) {
  if (state === "loading")
    return (
      <div className="min-h-40 space-y-2 pt-3" role="status" aria-live="polite">
        <span className="sr-only">Loading…</span>
        {[0, 1, 2].map((row) => (
          <div key={row} className="h-16 rounded-xl bg-muted/40 motion-safe:animate-pulse lg:h-11" aria-hidden />
        ))}
      </div>
    );
  if (state === "error")
    return (
      <div role="alert" className="flex min-h-40 flex-col items-center justify-center gap-3 py-6 text-sm">
        <p className="text-destructive">
          Could not load this list{error instanceof Error ? `: ${error.message}` : ""}
        </p>
        <Button variant="outline" className="min-h-11 lg:min-h-9" onClick={onRetry}>
          <RefreshCw className="size-4" aria-hidden /> Try again
        </Button>
      </div>
    );
  if (state === "empty") {
    const Icon = empty.icon ?? Inbox;
    return (
      <div className="flex min-h-40 flex-col items-center justify-center gap-3 py-8 text-muted-foreground">
        <span className="grid size-12 place-items-center rounded-full bg-muted/60 ring-1 ring-border">
          <Icon className="size-5" aria-hidden />
        </span>
        <p className="text-sm font-medium text-foreground/85">{empty.title}</p>
      </div>
    );
  }
  return <>{children}</>;
}

export function LoadMore({
  query,
  shown,
}: {
  query: { hasNextPage: boolean; isFetchingNextPage: boolean; fetchNextPage: () => unknown };
  shown: number;
}) {
  return (
    <div className="mt-3 flex items-center justify-center gap-3">
      {query.hasNextPage ? (
        <Button
          variant="outline"
          className="min-h-11 w-full rounded-full sm:w-auto lg:min-h-9"
          onClick={() => void query.fetchNextPage()}
          disabled={query.isFetchingNextPage}
        >
          {query.isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      ) : (
        <span className="num text-xs text-muted-foreground">All {shown} shown · newest first</span>
      )}
    </div>
  );
}

/**
 * Phone and tablet list item: two lines (who and what on top with the result on the right; secondary details below)
 * and, when there are details, the whole card is a 44 px+ button that opens them.
 */
export function ItemCard({
  lead,
  result,
  meta,
  subResult,
  details,
  label,
  muted = false,
}: {
  lead: ReactNode;
  result?: ReactNode;
  meta: ReactNode;
  subResult?: ReactNode;
  details?: [string, ReactNode][] | undefined;
  label: string;
  muted?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const body = (
    <>
      <span className="flex min-w-0 items-center gap-2">
        {lead}
        {result ? <span className="ml-auto shrink-0 text-right text-[15px] font-semibold">{result}</span> : null}
      </span>
      <span className="mt-1.5 flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
        <span className="flex min-w-0 items-center gap-1.5 truncate">{meta}</span>
        <span className="ml-auto flex shrink-0 items-center gap-1">
          {subResult}
          {details?.length ? (
            <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} aria-hidden />
          ) : null}
        </span>
      </span>
    </>
  );
  return (
    <li className={cn("panel overflow-hidden rounded-xl", muted && "bg-none bg-muted/25 shadow-none")}>
      {details?.length ? (
        <button
          type="button"
          aria-expanded={open}
          aria-label={`${open ? "Hide" : "Show"} details: ${label}`}
          onClick={() => setOpen((value) => !value)}
          className="block min-h-14 w-full px-3.5 py-3 text-left"
        >
          {body}
        </button>
      ) : (
        <div className="min-h-14 px-3.5 py-3">{body}</div>
      )}
      {open && details ? (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-2.5 border-t border-border/50 bg-background/30 px-3.5 py-3 text-xs">
          {details.map(([name, value]) => (
            <div key={name} className="min-w-0">
              <dt className="text-muted-foreground">{name}</dt>
              <dd className="num mt-0.5 break-words text-foreground/90">{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </li>
  );
}

export function CardList({ label, children }: { label: string; children: ReactNode }) {
  return (
    <ul className="grid gap-2 pt-3 sm:grid-cols-2 lg:hidden" aria-label={label}>
      {children}
    </ul>
  );
}

/** Desktop table: tabular numbers, right-aligned amounts, headers that stick under the tab bar. */
export function DataTable({ label, head, children }: { label: string; head: ReactNode; children: ReactNode }) {
  return (
    <table className="hidden w-full text-left text-sm lg:table" aria-label={label}>
      <thead className="text-[11px] tracking-wide text-muted-foreground uppercase">
        <tr>{head}</tr>
      </thead>
      <tbody className="[&>tr]:border-b [&>tr]:border-border/40 [&>tr:hover]:bg-muted/25">{children}</tbody>
    </table>
  );
}

export function Th({ children, right = false }: { children?: ReactNode; right?: boolean }) {
  return (
    <th
      scope="col"
      className={cn(
        "sticky z-10 border-b border-border bg-background/95 py-2.5 pr-4 font-medium backdrop-blur-md first:pl-1",
        STICKY_THEAD,
        right && "text-right",
      )}
    >
      {children}
    </th>
  );
}

export const td = "py-2.5 pr-4 align-middle first:pl-1";
export const tdNum = "num py-2.5 pr-4 text-right align-middle";

// ---------------------------------------------------------------- results

export type OutcomeSlice = { key: string; label: string; count: number; className: string; hint?: string | undefined };
export type StatItem = { label: string; hint?: string | undefined; value: ReactNode };

/** Outcome mix as one proportional bar with a legend, then quality and cost figures. */
export function ResultsPanel({
  title,
  completed,
  rejected,
  slices,
  quality,
  costs,
  error,
  loading,
  facts,
  note,
}: {
  title: string;
  completed: number;
  rejected: number;
  slices: OutcomeSlice[];
  quality: StatItem[];
  costs: StatItem[];
  error: string | null;
  loading: boolean;
  facts: [string, string][];
  note: string;
}) {
  const total = slices.reduce((sum, slice) => sum + slice.count, 0);
  return (
    <section className="panel rounded-2xl p-4 sm:p-5" aria-label={title}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-lg font-semibold">{title}</h2>
        {!loading && !error ? (
          <p className="num text-xs text-muted-foreground">
            {completed} closed · {rejected} rejected
          </p>
        ) : null}
      </div>
      <div className="orbit-rule mt-2 mb-4" />
      {error ? (
        <p role="alert" className="text-sm text-destructive">
          Totals could not be loaded: {error}
        </p>
      ) : loading ? (
        <div className="h-40 rounded-xl bg-muted/40 motion-safe:animate-pulse" role="status">
          <span className="sr-only">Loading results…</span>
        </div>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
          <div>
            {total > 0 ? (
              <div className="flex h-3 w-full overflow-hidden rounded-full bg-muted" role="img" aria-label="Outcome mix">
                {slices
                  .filter((slice) => slice.count > 0)
                  .map((slice) => (
                    <span key={slice.key} className={cn("h-full", slice.className)} style={{ width: `${(slice.count / total) * 100}%` }} />
                  ))}
              </div>
            ) : (
              <div className="h-3 w-full rounded-full border border-dashed border-border" aria-hidden />
            )}
            <ul className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
              {slices.map((slice) => (
                <li key={slice.key} className="flex items-center gap-2 text-xs">
                  <span className={cn("size-2.5 shrink-0 rounded-sm", slice.className)} aria-hidden />
                  <span className="truncate text-muted-foreground">
                    {slice.hint ? <Hint text={slice.hint}>{slice.label}</Hint> : slice.label}
                  </span>
                  <span className="num ml-auto font-medium">{slice.count}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="grid gap-4">
            <StatGrid items={quality} />
            <StatGrid items={costs} subtle />
          </div>
        </div>
      )}
      {!loading && !error ? <p className="mt-4 text-[11px] text-muted-foreground">{note}</p> : null}
      {facts.length ? (
        <details className="mt-3 text-xs text-muted-foreground">
          <summary className="flex min-h-11 items-center text-foreground/80 lg:min-h-0">Run configuration</summary>
          <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
            {facts.map(([name, value]) => (
              <div key={name} className="min-w-0">
                <dt>{name}</dt>
                <dd className="num break-words text-foreground">{value}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </section>
  );
}

function StatGrid({ items, subtle = false }: { items: StatItem[]; subtle?: boolean }) {
  return (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {items.map((item) => (
        <div key={item.label} className={cn("min-w-0 rounded-lg p-2.5 ring-1 ring-border/60", subtle ? "bg-transparent" : "bg-muted/30")}>
          <dt className="truncate text-[11px] text-muted-foreground">
            {item.hint ? <Hint text={item.hint}>{item.label}</Hint> : item.label}
          </dt>
          <dd className="num mt-1 truncate text-sm font-semibold">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The outcome slices both markets share; `liquidated` is named per market (Liquidated / Stop-out). */
export function outcomeSlices(results: ResultsView, liquidatedLabel: string, ambiguousHint: string): OutcomeSlice[] {
  return [
    { key: "target", label: "Target", count: results.target, className: "bg-bull" },
    { key: "stopped", label: "Stop", count: results.stopped, className: "bg-bear" },
    { key: "liquidated", label: liquidatedLabel, count: results.liquidated, className: "bg-bear/55" },
    { key: "time", label: "Time limit", count: results.timeLimit, className: "bg-muted-foreground/60" },
    { key: "ambiguous", label: "Unclear", count: results.ambiguous, className: "bg-warn/80", hint: ambiguousHint },
    { key: "early", label: "Closed early", count: results.unresolved, className: "bg-muted-foreground/30" },
  ];
}
