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
        <p className="eyebrow">Crypto Watch / Personal intelligence</p>
        <h1 className="mt-2 text-3xl font-semibold">{title}</h1>
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
    <div className="rounded-xl border border-dashed border-border p-6 text-sm">
      <p className="font-medium text-foreground">{title}</p>
      <p className="mt-1 max-w-xl text-muted-foreground">{children}</p>
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
