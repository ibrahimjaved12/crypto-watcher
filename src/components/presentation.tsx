import type { ReactNode } from "react";
import { AlertCircle, CheckCircle2, CircleDashed, Telescope } from "lucide-react";
import { cn } from "@/lib/utils";
import { issueSummary, type StatusTone } from "@/lib/presentation/labels";

export function PageHeader({
  title,
  description,
  children,
}: {
  title: string;
  description: ReactNode;
  children?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-5">
      <div className="max-w-2xl">
        <p className="eyebrow mb-2">Crypto Watch / Workspace</p>
        <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">{title}</h1>
        <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{description}</p>
      </div>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </header>
  );
}

export function StatusBadge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: StatusTone;
}) {
  const Icon = tone === "good" ? CheckCircle2 : tone === "neutral" ? CircleDashed : AlertCircle;
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
        {
          good: "border-bull/25 bg-bull/10 text-bull",
          warning: "border-warn/25 bg-warn/10 text-warn",
          danger: "border-bear/30 bg-bear/10 text-bear",
          neutral: "border-border bg-secondary/60 text-muted-foreground",
        }[tone],
      )}
    >
      <Icon className="size-3.5 shrink-0" aria-hidden />
      {children}
    </span>
  );
}

export function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-border bg-background/30 px-6 py-9 text-center">
      <Telescope className="mx-auto mb-3 size-6 text-primary/70" aria-hidden />
      <h3 className="text-sm font-medium">{title}</h3>
      <div className="mx-auto mt-2 max-w-lg text-sm leading-relaxed text-muted-foreground">
        {children}
      </div>
    </div>
  );
}

export function TechnicalDetails({
  children,
  title = "Technical details",
  className,
}: {
  children: ReactNode;
  title?: string;
  className?: string;
}) {
  return (
    <details
      className={cn(
        "technical-details rounded-lg border border-border/70 px-3 py-2 text-xs",
        className,
      )}
    >
      <summary className="cursor-pointer py-1 font-medium text-muted-foreground">{title}</summary>
      <div className="mt-3 min-w-0 space-y-3 break-words text-muted-foreground">{children}</div>
    </details>
  );
}

export function ErrorNotice({ error, title }: { error: unknown; title?: string }) {
  const raw =
    error && typeof error === "object" && "message" in error
      ? String(error.message)
      : String(error);
  return (
    <div className="space-y-3 rounded-lg border border-bear/30 bg-bear/5 p-4">
      <p role="alert" className="text-sm text-bear">
        {title ? `${title} ` : ""}
        {issueSummary(raw)}
      </p>
      <TechnicalDetails>
        <p className="font-mono whitespace-pre-wrap">{raw}</p>
        {error && typeof error === "object" && Object.keys(error).length > 0 ? (
          <pre className="whitespace-pre-wrap">{JSON.stringify(error, null, 2)}</pre>
        ) : null}
      </TechnicalDetails>
    </div>
  );
}
