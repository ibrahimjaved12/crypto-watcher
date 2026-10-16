import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from "recharts";
import type { ReactNode } from "react";
import { Loader2, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { formatAgo, formatClock, sourceLabel } from "@/lib/labels";
import { useNow } from "@/lib/use-now";
import { isUnavailable } from "@/lib/market/self-heal";
import { CHANGE_WINDOWS, WINDOW_LABELS, baseAsset } from "@/lib/market/symbols";
import type { SymbolQuote } from "@/lib/market/quote-calc";

function formatPrice(value: number): string {
  const digits = value >= 1000 ? 2 : value >= 1 ? 4 : 6;
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function changeClass(value: number | null | undefined): string {
  if (value == null) return "text-muted-foreground";
  if (value > 0) return "text-bull";
  if (value < 0) return "text-bear";
  return "text-muted-foreground";
}

export function QuoteCard({
  quote,
  onRemove,
  onRetry,
  retrying = false,
  assessment,
  longerMoves,
  now: nowProp,
}: {
  quote: SymbolQuote;
  onRemove?: () => void;
  /** Refetch prices; shown as a "Retry" link when the price is unavailable. */
  onRetry?: () => void;
  retrying?: boolean;
  /** The per-pair "Current assessment" block (#30), shown under the price, changes and chart. */
  assessment?: ReactNode;
  /** One muted "1h −0.4% · 4h −1.9% · 12h −3.2%" line under the timeframe row ("Longer moves"). */
  longerMoves?: ReactNode;
  /** The page's clock (held while a resumed tab settles); falls back to a local one. */
  now?: number;
}) {
  const localNow = useNow();
  const now = nowProp ?? localNow;
  const unavailable = isUnavailable(quote);

  return (
    <article className="panel flex min-w-0 flex-col gap-4 p-4 transition-colors hover:border-primary/40 sm:p-5">
      <header className="flex items-start gap-3">
        <div className="min-w-0">
          <h3 className="font-display text-base font-semibold">
            {baseAsset(quote.symbol)}
            {/* TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days */}
            <span className="text-muted-foreground">
              /{quote.source === "kraken-futures" ? "USD" : "USDT"}
            </span>
          </h3>
          {unavailable ? null : (
            <p className="mt-1 font-display text-2xl font-semibold tabular-nums">${formatPrice(quote.price!)}</p>
          )}
        </div>
        <div className="ml-auto flex items-center gap-1 text-muted-foreground">
          {quote.source ? (
            <Badge variant="outline" className="text-xs font-normal text-muted-foreground" title={quote.source}>
              {sourceLabel(quote.source)}
            </Badge>
          ) : null}
          {onRemove ? (
            <Button
              variant="ghost"
              size="icon"
              className="size-11 text-muted-foreground sm:size-9"
              onClick={onRemove}
              aria-label={`Remove ${quote.symbol}`}
            >
              <Trash2 className="size-4" aria-hidden />
            </Button>
          ) : null}
        </div>
      </header>
      {unavailable ? (
        <div aria-live="polite" className="text-sm text-muted-foreground" title={quote.error ?? undefined}>
          <p className="flex flex-wrap items-center gap-x-2">
            {retrying ? (
              <>
                <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden />
                Price unavailable · retrying…
              </>
            ) : (
              <>
                Price unavailable
                {onRetry ? (
                  <button
                    type="button"
                    className="inline-flex min-h-11 items-center text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:min-h-0"
                    onClick={onRetry}
                  >
                    Retry
                  </button>
                ) : null}
              </>
            )}
          </p>
          {quote.lastCandleAt !== null && Number.isFinite(quote.lastCandleAt) ? (
            <p className="mt-1 text-xs">Last price seen {formatAgo(quote.lastCandleAt, now)}</p>
          ) : null}
        </div>
      ) : (
        <>
          <dl className="grid grid-cols-3 gap-x-2 gap-y-3 sm:grid-cols-5">
            {CHANGE_WINDOWS.map((w) => {
              const value = quote.changes[w];
              return (
                <div key={w} className="min-w-0">
                  <dt className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                    {WINDOW_LABELS[w]}
                  </dt>
                  <dd className={`num text-[13px] font-semibold ${changeClass(value)}`}>
                    {value == null
                      ? "—"
                      : `${value > 0 ? "▲" : value < 0 ? "▼" : ""}${Math.abs(value).toFixed(2)}%`}
                  </dd>
                </div>
              );
            })}
          </dl>
          {longerMoves}

          <div className="h-24">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={quote.chart} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id={`g-${quote.symbol}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--color-primary)" stopOpacity={0.45} />
                    <stop offset="100%" stopColor="var(--color-primary)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <YAxis hide domain={["dataMin", "dataMax"]} />
                <Tooltip
                  contentStyle={{
                    background: "var(--color-popover)",
                    border: "1px solid var(--color-border)",
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                  labelFormatter={(_label, payload) => {
                    const t = payload?.[0]?.payload?.time;
                    return Number.isFinite(t) ? new Date(t).toLocaleString() : "";
                  }}
                  formatter={(v: number) => [`$${formatPrice(v)}`, "Close"]}
                />
                <Area
                  type="monotone"
                  dataKey="close"
                  stroke="var(--color-primary)"
                  strokeWidth={1.5}
                  fill={`url(#g-${quote.symbol})`}
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
          <p className="text-xs text-muted-foreground">
            Last 24 hours · updated{" "}
            <span className="num">{formatClock(Date.parse(quote.fetchedAt), now)}</span>
          </p>
        </>
      )}
      {assessment}
    </article>
  );
}

/** Loading placeholder in the shape of a pair card (soft pulse, still under reduced motion). */
export function QuoteCardSkeleton({ symbol }: { symbol: string }) {
  return (
    <div className="panel flex flex-col gap-4 p-4 sm:p-5" aria-busy="true" aria-label={`Loading ${symbol}`}>
      <div className="animate-pulse motion-reduce:animate-none">
        <div className="h-5 w-24 rounded-md bg-secondary/60" />
        <div className="mt-2 h-7 w-36 rounded-md bg-secondary/60" />
        <div className="mt-4 grid grid-cols-3 gap-2 sm:grid-cols-5">
          {[0, 1, 2, 3, 4].map((i) => (
            <div key={i} className="h-9 rounded-md bg-secondary/40" />
          ))}
        </div>
        <div className="mt-4 h-24 rounded-md bg-secondary/40" />
        <div className="mt-4 h-6 w-40 rounded-full bg-secondary/40" />
      </div>
    </div>
  );
}
