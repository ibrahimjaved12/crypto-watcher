import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Clock, type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import type { Level } from "@/lib/labels";

/** Shared building blocks so every page has a title, a one-line purpose and readable states. */

export function PageHeader({
  eyebrow,
  title,
  subtitle,
  actions,
}: {
  eyebrow?: string | undefined;
  title: string;
  subtitle: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="min-w-0">
        {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
        <h1 className="mt-1 text-2xl font-semibold sm:text-3xl">{title}</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{subtitle}</p>
      </div>
      {actions ? <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function SectionTitle({ title, hint, right }: { title: string; hint?: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-lg font-semibold">{title}</h2>
        {right ? <div className="ml-auto flex flex-wrap items-center gap-2">{right}</div> : null}
      </div>
      {hint ? <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p> : null}
      <div className="orbit-rule mt-2" />
    </div>
  );
}

const LEVEL_STYLE: Record<Level, { icon: LucideIcon; text: string; chip: string; dot: string; word: string }> = {
  ok: {
    icon: CheckCircle2,
    text: "text-bull",
    chip: "border-bull/40 bg-bull/10 text-bull",
    dot: "bg-bull",
    word: "Working",
  },
  wait: {
    icon: Clock,
    text: "text-warn",
    chip: "border-warn/40 bg-warn/10 text-warn",
    dot: "bg-warn",
    word: "Waiting",
  },
  problem: {
    icon: AlertTriangle,
    text: "text-bear",
    chip: "border-bear/40 bg-bear/10 text-bear",
    dot: "bg-bear",
    word: "Needs attention",
  },
};

export function levelStyle(level: Level) {
  return LEVEL_STYLE[level];
}

/** Coloured chip with an icon and a word, so colour is never the only signal. */
export function StatusChip({ level, children, className }: { level: Level; children: ReactNode; className?: string }) {
  const style = LEVEL_STYLE[level];
  const Icon = style.icon;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        style.chip,
        className,
      )}
    >
      <Icon className="size-3.5" aria-hidden />
      {children}
    </span>
  );
}

/**
 * One status per subsystem: level, a plain one-liner, what to do, and the raw machine string only
 * inside a collapsed "Technical details".
 */
export function StatusCard({
  level,
  title,
  detail,
  action,
  help,
  raw,
  children,
}: {
  level: Level;
  title: string;
  detail: ReactNode;
  action?: ReactNode;
  help?: ReactNode;
  raw?: ReactNode;
  children?: ReactNode;
}) {
  const style = LEVEL_STYLE[level];
  const Icon = style.icon;
  return (
    <div className="panel flex gap-3 p-4">
      <Icon className={cn("mt-0.5 size-5 shrink-0", style.text)} aria-hidden />
      <div className="min-w-0 flex-1 space-y-1 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{title}</span>
          <StatusChip level={level}>{style.word}</StatusChip>
        </div>
        <p className="text-muted-foreground">{detail}</p>
        {action ? <p className="text-xs">{action}</p> : null}
        {children}
        {help || raw ? (
          <details className="pt-1 text-xs text-muted-foreground">
            <summary className="text-foreground/80">Technical details</summary>
            <div className="mt-2 space-y-2 break-words">
              {help ? <div>{help}</div> : null}
              {raw ? <code className="block rounded bg-muted/60 p-2 font-mono text-[11px]">{raw}</code> : null}
            </div>
          </details>
        ) : null}
      </div>
    </div>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  body,
  className,
}: {
  icon: LucideIcon;
  title: string;
  body: ReactNode;
  className?: string | undefined;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-2 rounded-lg border border-dashed border-border px-6 py-10 text-center",
        className,
      )}
    >
      <span className="grid size-12 place-items-center rounded-full bg-gradient-to-br from-primary/25 to-aurora/15 ring-1 ring-primary/30">
        <Icon className="size-5 text-primary" aria-hidden />
      </span>
      <p className="font-medium">{title}</p>
      <p className="max-w-md text-sm text-muted-foreground">{body}</p>
    </div>
  );
}

/** Signed horizontal bar for a −max…+max value (e.g. indicator score), with the number. */
export function SignedBar({
  value,
  max = 100,
  label,
  format = (v) => (v > 0 ? `+${v}` : String(v)),
}: {
  value: number | null;
  max?: number;
  label?: string | undefined;
  format?: (value: number) => string;
}) {
  if (value === null || !Number.isFinite(value))
    return <span className="text-muted-foreground">—</span>;
  const share = Math.min(1, Math.abs(value) / max) * 50;
  const positive = value > 0;
  return (
    <span className="inline-flex items-center gap-2" aria-label={label}>
      <span className="relative h-1.5 w-16 overflow-hidden rounded-full bg-muted" aria-hidden>
        <span className="absolute inset-y-0 left-1/2 w-px bg-muted-foreground/50" />
        <span
          className={cn("absolute inset-y-0 rounded-full", positive ? "bg-bull" : "bg-bear")}
          style={positive ? { left: "50%", width: `${share}%` } : { right: "50%", width: `${share}%` }}
        />
      </span>
      <span className={cn("num", positive ? "text-bull" : value < 0 ? "text-bear" : "text-muted-foreground")}>
        {format(value)}
      </span>
    </span>
  );
}

/** Exact time in a tooltip-like title, relative text visible. */
export function TimeAgo({ at, text }: { at: number | string | null | undefined; text: string }) {
  if (at === null || at === undefined) return <span className="text-muted-foreground">—</span>;
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return <span className="text-muted-foreground">—</span>;
  return (
    <time dateTime={date.toISOString()} title={date.toLocaleString()} className="num">
      {text}
    </time>
  );
}
