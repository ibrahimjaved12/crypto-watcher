import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from "recharts";
import { AlertTriangle, Loader2, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { formatAgo, sourceLabel } from "@/lib/labels";
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
  retrying = false,
}: {
  quote: SymbolQuote;
  onRemove?: () => void;
  retrying?: boolean;
}) {
  const now = useNow();
  const unavailable = isUnavailable(quote);

  return (
    <article className="panel flex flex-col gap-4 p-5 transition-colors hover:border-primary/40">
      <header className="flex items-start gap-3">
        <div>
          <h3 className="font-display text-lg font-semibold">
            {baseAsset(quote.symbol)}
            {/* TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days */}
            <span className="text-muted-foreground">
              /{quote.source === "kraken-futures" ? "USD" : "USDT"}
            </span>
          </h3>
          <p className="num mt-1 text-2xl font-semibold">
            {unavailable ? (
              <span className="text-base text-muted-foreground">Price unavailable</span>
            ) : (
              `$${formatPrice(quote.price!)}`
            )}
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          {quote.source ? (
            <Badge variant="secondary" className="text-[10px]" title={quote.source}>
              {sourceLabel(quote.source)}
            </Badge>
          ) : null}
          {onRemove ? (
            <Button
              variant="ghost"
              size="icon"
              onClick={onRemove}
              aria-label={`Remove ${quote.symbol}`}
            >
              <Trash2 className="size-4" aria-hidden />
            </Button>
          ) : null}
        </div>
      </header>
      {unavailable ? (
        <p
          aria-live="polite"
          className="flex items-start gap-2 rounded-md border border-border bg-muted/40 p-3 text-xs text-muted-foreground"
        >
          {retrying ? (
            <Loader2
              className="mt-0.5 size-4 shrink-0 animate-spin motion-reduce:animate-none"
              aria-hidden
            />
          ) : (
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          )}
          <span>
            {quote.error ??
              (retrying
                ? "Price feed is catching up — retrying automatically"
                : "No market data available.")}
            {quote.lastCandleAt !== null && Number.isFinite(quote.lastCandleAt) && (
              <span className="mt-1 block">
                Last price seen {formatAgo(quote.lastCandleAt, now)}
              </span>
            )}
          </span>
        </p>
      ) : (
        <>
          <div className="grid grid-cols-5 gap-1 text-center">
            {CHANGE_WINDOWS.map((w) => {
              const value = quote.changes[w];
              return (
                <div key={w} className="rounded-md bg-secondary/60 px-1 py-2">
                  <div className="text-[10px] uppercase text-muted-foreground">
                    {WINDOW_LABELS[w]}
                  </div>
                  <div className={`num text-xs font-semibold ${changeClass(value)}`}>
                    {value == null
                      ? "—"
                      : `${value > 0 ? "▲" : value < 0 ? "▼" : ""}${Math.abs(value).toFixed(2)}%`}
                  </div>
                </div>
              );
            })}
          </div>

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
          <p className="num text-[11px] text-muted-foreground">
            Last 24 hours (15-minute candles) · updated{" "}
            {new Date(quote.fetchedAt).toLocaleTimeString()}
          </p>
        </>
      )}
    </article>
  );
}
