import type { ReactNode } from "react";
import { CircleSlash, Hourglass } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * The dashboard's one visual language (#30): every bar, meter and "not ready" chip on the Market page comes from
 * here, so a state looks the same everywhere. Plain divs, existing tokens only, no chart library. Every gauge is a
 * `role="meter"` with its value in words.
 */

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
const pct = (value: number, min: number, max: number) => ((clamp(value, min, max) - min) / (max - min)) * 100;
const motion = "transition-[left,width] duration-150 motion-reduce:transition-none";

/**
 * A centred track for signed values: the centre tick is zero, the marker's position is direction and strength, and
 * the fill runs from the centre to the marker (bull right, bear left). Inside `flatBand` nothing is filled and the
 * marker is muted. `value === null` draws a dashed empty track.
 */
export function DivergingBar({
  value,
  min,
  max,
  flatBand = 0,
  label,
  valueText,
  muted = false,
  className,
}: {
  value: number | null;
  min: number;
  max: number;
  flatBand?: number;
  label: string;
  valueText: string;
  muted?: boolean;
  className?: string;
}) {
  if (value === null) {
    return <div className={cn("h-1.5 w-full rounded-full border border-dashed border-muted-foreground/40", className)} role="meter"
      aria-label={label} aria-valuemin={min} aria-valuemax={max} aria-valuetext={valueText} />;
  }
  const centre = pct(0, min, max);
  const at = pct(value, min, max);
  const flat = Math.abs(value) < flatBand;
  const tone = muted || flat ? "bg-muted-foreground" : value > 0 ? "bg-bull" : "bg-bear";
  return (
    <div
      className={cn("relative h-1.5 w-full rounded-full bg-secondary/60", className)}
      role="meter"
      aria-label={label}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={clamp(value, min, max)}
      aria-valuetext={valueText}
    >
      <span className="absolute top-1/2 h-3 w-px -translate-y-1/2 bg-muted-foreground/60" style={{ left: `${centre}%` }} aria-hidden />
      {flat ? null : (
        <span
          className={cn("absolute inset-y-0 rounded-full", tone, motion, muted && "opacity-60")}
          style={{ left: `${Math.min(centre, at)}%`, width: `${Math.abs(at - centre)}%` }}
          aria-hidden
        />
      )}
      <span
        className={cn("absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-background", tone, motion)}
        style={{ left: `${at}%` }}
        aria-hidden
      />
    </div>
  );
}

/**
 * A zero-to-max track: fill length is size. Optional reference ticks (e.g. "1×" usual volume, the age limit) and
 * shaded zones (e.g. RSI below 30 / above 70). `value === null` draws a dashed empty track.
 */
export function Meter({
  value,
  max,
  label,
  valueText,
  fillClass = "bg-primary",
  ticks = [],
  zones = [],
  className,
}: {
  value: number | null;
  max: number;
  label: string;
  valueText: string;
  fillClass?: string;
  ticks?: Array<{ at: number; label?: string }>;
  zones?: Array<{ from: number; to: number; className: string }>;
  className?: string;
}) {
  if (value === null) {
    return <div className={cn("h-1.5 w-full rounded-full border border-dashed border-muted-foreground/40", className)} role="meter"
      aria-label={label} aria-valuemin={0} aria-valuemax={max} aria-valuetext={valueText} />;
  }
  return (
    <div className={cn("w-full", className)}>
      <div
        className="relative h-1.5 w-full overflow-visible rounded-full bg-secondary/60"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={clamp(value, 0, max)}
        aria-valuetext={valueText}
      >
        {zones.map((zone) => (
          <span key={`${zone.from}-${zone.to}`} className={cn("absolute inset-y-0", zone.className)}
            style={{ left: `${pct(zone.from, 0, max)}%`, width: `${pct(zone.to, 0, max) - pct(zone.from, 0, max)}%` }} aria-hidden />
        ))}
        <span className={cn("absolute inset-y-0 left-0 rounded-full", fillClass, motion)} style={{ width: `${pct(value, 0, max)}%` }} aria-hidden />
        {ticks.map((tick) => (
          <span key={tick.at} className="absolute top-1/2 h-3 w-px -translate-y-1/2 bg-muted-foreground/70" style={{ left: `${pct(tick.at, 0, max)}%` }} aria-hidden />
        ))}
      </div>
      {ticks.some((tick) => tick.label) ? (
        <div className="relative mt-0.5 h-4 text-xs text-muted-foreground" aria-hidden>
          {ticks.filter((tick) => tick.label).map((tick) => (
            <span key={tick.at} className="num absolute -translate-x-1/2" style={{ left: `${pct(tick.at, 0, max)}%` }}>{tick.label}</span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** One bar split by pair counts: falling (bear), flat (muted), rising (bull), widths in proportion. */
export function BreadthBar({
  falling,
  rising,
  total,
  label,
  className,
}: {
  falling: number;
  rising: number;
  total: number;
  label: string;
  className?: string;
}) {
  if (total <= 0) return null;
  const flat = Math.max(0, total - falling - rising);
  const seg = (count: number) => `${(count / total) * 100}%`;
  return (
    <div className={cn("flex h-1.5 w-full overflow-hidden rounded-full bg-secondary/60", className)} role="img" aria-label={label}>
      <span className={cn("h-full bg-bear", motion)} style={{ width: seg(falling) }} />
      <span className={cn("h-full bg-muted-foreground/40", motion)} style={{ width: seg(flat) }} />
      <span className={cn("h-full bg-bull", motion)} style={{ width: seg(rising) }} />
    </div>
  );
}

/**
 * The "not ready" chip, identical everywhere: dashed outline, muted, an hourglass for warm-up or a slash for no
 * reading. Never a direction symbol and never a colour.
 */
export function NotReadyChip({
  kind,
  title,
  children,
  className,
}: {
  kind: "warming" | "none";
  title?: string | undefined;
  children?: ReactNode;
  className?: string;
}) {
  const Icon = kind === "warming" ? Hourglass : CircleSlash;
  return (
    <span
      className={cn(
        "inline-flex w-fit items-center gap-1 rounded-full border border-dashed border-muted-foreground/50 px-2 py-0.5 text-xs font-medium text-muted-foreground",
        className,
      )}
      title={title}
    >
      <Icon className="size-3.5 shrink-0" aria-hidden />
      {children ?? (kind === "warming" ? "Warming up" : "No reading")}
      {title ? <span className="sr-only">: {title}</span> : null}
    </span>
  );
}
