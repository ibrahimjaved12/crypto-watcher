import type { ReactNode } from "react";

export function PageHeader({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div className="min-w-0">
        <p className="eyebrow">Crypto Watch</p>
        <h1 className="mt-2 text-2xl font-semibold sm:text-3xl">{title}</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">{description}</p>
      </div>
      {children && <div className="flex flex-wrap items-center gap-2 lg:ml-auto">{children}</div>}
    </div>
  );
}
export function TechnicalDetails({
  children,
  title = "Technical details",
}: {
  children: ReactNode;
  title?: string;
}) {
  return (
    <details className="technical-details">
      <summary>{title}</summary>
      <div className="mt-3 space-y-3 break-words text-xs text-muted-foreground">{children}</div>
    </details>
  );
}
export function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-4 rounded-xl border border-dashed border-border p-6 text-sm">
      <svg viewBox="0 0 48 48" className="size-12 shrink-0 text-primary/70" aria-hidden>
        <circle cx="24" cy="24" r="7" fill="currentColor" opacity="0.85" />
        <ellipse cx="24" cy="24" rx="20" ry="7" fill="none" stroke="currentColor" strokeWidth="1.2" opacity="0.5" transform="rotate(-20 24 24)" />
        <circle cx="8" cy="10" r="1" fill="currentColor" />
        <circle cx="40" cy="38" r="1.2" fill="currentColor" />
        <circle cx="41" cy="9" r="0.8" fill="currentColor" />
      </svg>
      <div>
      <p className="font-medium text-foreground">{title}</p>
      <p className="mt-1 max-w-xl text-muted-foreground">{children}</p>
      </div>
    </div>
  );
}
export function QueryNotice({ pending, error }: { pending: boolean; error: unknown }) {
  if (error)
    return (
      <div
        role="alert"
        className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm"
      >
        Unable to load this view. Try refreshing.
        <TechnicalDetails>
          {error instanceof Error
            ? error.message
            : typeof error === "string"
              ? error
              : JSON.stringify(error)}
        </TechnicalDetails>
      </div>
    );
  return pending ? (
    <p role="status" className="py-4 text-sm text-muted-foreground">
      Loading your data…
    </p>
  ) : null;
}

const LEVEL_WORD = { ok: "Healthy", wait: "Waiting", problem: "Needs attention" } as const;

/** One subsystem's health: coloured level, a plain sentence, what to do, raw value under Details. */
export function StatusCard({
  level,
  title,
  detail,
  action,
  raw,
  children,
}: {
  level: "ok" | "wait" | "problem";
  title: string;
  detail: string;
  action?: string | undefined;
  raw?: string | null | undefined;
  children?: ReactNode;
}) {
  return (
    <div className="panel p-4 text-sm" data-level={level}>
      <div className="flex items-center gap-2">
        <span className="status-dot" data-level={level} aria-hidden />
        <span className="font-medium">{title}</span>
        <span className="chip ml-auto" data-tone={level === "ok" ? "bull" : level === "wait" ? "warn" : "bear"}>
          {LEVEL_WORD[level]}
        </span>
      </div>
      <p className="mt-2 text-foreground/90">{detail}</p>
      {action && <p className="mt-1 text-xs text-muted-foreground">What to do: {action}</p>}
      {(raw || children) && (
        <TechnicalDetails>
          {raw && <p className="font-mono">{raw}</p>}
          {children}
        </TechnicalDetails>
      )}
    </div>
  );
}

/** Centre-zero bar for a signed value in [-max, max] plus the number (colour is never the only cue). */
export function SignedBar({ value, max, label }: { value: number | null; max: number; label?: string }) {
  if (value === null || !Number.isFinite(value)) return <span className="text-muted-foreground">—</span>;
  const share = Math.min(1, Math.abs(value) / max) * 50;
  return (
    <span className="inline-flex items-center gap-2">
      <span className="signed-bar" aria-hidden>
        <span
          style={value >= 0 ? { left: "50%", width: `${share}%` } : { right: "50%", width: `${share}%` }}
          className={value > 0 ? "bg-bull" : value < 0 ? "bg-bear" : ""}
        />
      </span>
      <span className="num">{label ?? (value > 0 ? `+${value}` : String(value))}</span>
    </span>
  );
}

/** "12 min ago" with the exact local time in the title tooltip. */
export function RelativeTime({ ms, now = Date.now() }: { ms: number | string | null | undefined; now?: number }) {
  const value = typeof ms === "string" ? Date.parse(ms) : ms;
  if (value === null || value === undefined || !Number.isFinite(value)) return <span>—</span>;
  const seconds = Math.round((now - value) / 1000);
  const abs = Math.abs(seconds);
  const text =
    abs < 45 ? "just now"
    : abs < 3600 ? `${Math.round(abs / 60)} min`
    : abs < 86_400 ? `${Math.round(abs / 3600)} h`
    : `${Math.round(abs / 86_400)} d`;
  return (
    <time dateTime={new Date(value).toISOString()} title={new Date(value).toLocaleString()}>
      {text === "just now" ? text : seconds >= 0 ? `${text} ago` : `in ${text}`}
    </time>
  );
}
