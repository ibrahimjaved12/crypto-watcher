import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from "recharts";
import { AlertTriangle, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ErrorNotice, StatusBadge } from "@/components/presentation";
import { sourceLabel } from "@/lib/presentation/labels";
import { CHANGE_WINDOWS, WINDOW_LABELS, baseAsset } from "@/lib/market/symbols";
import type { SymbolQuote } from "@/lib/market/quotes.server";

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

export function QuoteCard({ quote, onRemove }: { quote: SymbolQuote; onRemove?: () => void }) {
  const unavailable = !quote.ok || quote.price == null;

  return (
    <article className="panel flex min-w-0 flex-col gap-4 border-t-primary/30 p-5">
      <header className="flex flex-wrap items-start gap-3">
        <div>
          <h3 className="font-display text-lg font-semibold">
            {baseAsset(quote.symbol)}
            <span className="text-muted-foreground">/USDT</span>
          </h3>
          <p className="num mt-1 text-3xl font-semibold">
            {unavailable ? (
              <span className="font-sans text-base text-muted-foreground">Price unavailable</span>
            ) : (
              `$${formatPrice(quote.price!)}`
            )}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
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
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>{sourceLabel(quote.source)}</span>
        <StatusBadge tone={unavailable ? "danger" : quote.stale ? "warning" : "good"}>
          {unavailable ? "Unavailable" : quote.stale ? "Delayed" : "Fresh"}
        </StatusBadge>
      </div>
      {unavailable ? (
        <ErrorNotice error={quote.error ?? "Market data unavailable"} />
      ) : (
        <>
          {quote.stale ? (
            <p className="flex items-center gap-2 rounded-md border border-warn/40 bg-warn/10 p-2 text-xs text-warn">
              <AlertTriangle className="size-4" aria-hidden />
              Delayed data — last candle{" "}
              {quote.lastCandleAt ? new Date(quote.lastCandleAt).toLocaleString() : "unavailable"}
            </p>
          ) : null}

          <div className="grid grid-cols-5 gap-1 text-center">
            {CHANGE_WINDOWS.map((w) => {
              const value = quote.changes[w];
              return (
                <div key={w} className="rounded-md bg-secondary/60 px-1 py-2">
                  <div className="text-[10px] uppercase text-muted-foreground">
                    {WINDOW_LABELS[w]}
                  </div>
                  <div className={`num text-xs font-semibold ${changeClass(value)}`}>
                    {value == null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)}%`}
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
                  labelFormatter={(t) => new Date(Number(t)).toLocaleString()}
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
            24h of 15m candles · updated {new Date(quote.fetchedAt).toLocaleTimeString()}
          </p>
        </>
      )}
    </article>
  );
}
