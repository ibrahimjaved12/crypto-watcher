import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { interpretTA, explainTA } from "@/lib/ta/interpretation";

export function TechnicalAnalysis() {
  const [frame, setFrame] = useState(15);
  const [symbol, setSymbol] = useState("");
  const [page, setPage] = useState(0);
  const history = useQuery({
    queryKey: ["ta", frame, symbol, page],
    queryFn: async () => {
      let query = supabase
        .from("ta_signals")
        .select("*")
        .order("candle_at", { ascending: false })
        .order("id", { ascending: false })
        .range(page * 25, page * 25 + 25);
      if (frame !== 0) query = query.eq("timeframe", frame);
      if (symbol.trim()) query = query.eq("symbol", symbol.trim().toUpperCase());
      const { data, error } = await query;
      if (error) throw error;
      return data;
    },
    refetchInterval: 60_000,
  });
  const number = (v: unknown) =>
    typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 5 }) : "--";
  return (
    <section className="mt-6 min-w-0 border-t border-border pt-5">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-semibold">Technical analysis</h2>
        <div className="flex" role="group" aria-label="Timeframe">
          {[
            [0, "All"],
            [15, "15m"],
            [60, "1h"],
            [240, "4h"],
          ].map(([value, label]) => (
            <Button
              key={value}
              variant={frame === value ? "secondary" : "ghost"}
              aria-pressed={frame === value}
              onClick={() => {
                setFrame(Number(value));
                setPage(0);
              }}
            >
              {label}
            </Button>
          ))}
        </div>
        <input
          aria-label="Filter TA symbol"
          placeholder="Symbol"
          value={symbol}
          onChange={(e) => {
            setSymbol(e.target.value);
            setPage(0);
          }}
          className="h-9 w-36 rounded-md border border-input bg-background px-3 text-sm"
        />
        <Button
          variant="ghost"
          size="icon"
          title="Refresh technical analysis"
          aria-label="Refresh technical analysis"
          disabled={history.isFetching}
          onClick={() => history.refetch()}
        >
          <RefreshCw className="size-4" />
        </Button>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        Completed candles only · 15m, 1h and 4h snapshots. Indicators describe price and volume;
        agreement is not independent confirmation. Expand “Indicator explanations” for values and
        rules. New v2 values appear after the next monitor check; older records retain their
        original values.
      </p>
      {history.isPending ? (
        <p className="py-4 text-sm">Loading analysis...</p>
      ) : history.error ? (
        <p role="alert" className="py-4 text-sm text-destructive">
          TA history unavailable: {history.error.message}
        </p>
      ) : !history.data?.length ? (
        <p className="py-4 text-sm text-muted-foreground">No analysis recorded.</p>
      ) : (
        <div
          className="mt-3 max-h-96 overflow-auto"
          tabIndex={0}
          aria-label="Technical analysis history"
        >
          <table className="w-full min-w-[900px] text-left text-xs">
            <thead className="sticky top-0 bg-background">
              <tr className="border-b border-border">
                {[
                  "Pair / exchange",
                  "Candle (local time)",
                  "Interpretation",
                  "TA score",
                  "EMA 20 / 50 / 200",
                  "RSI 14",
                  "ATR 14 / %",
                  "Volume change",
                  "Signals",
                  "Forward return",
                ].map((h) => (
                  <th className="p-2 font-medium" key={h}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {history.data.slice(0, 25).map((row) => {
                const values = (row.indicators ?? {}) as Record<string, unknown>;
                const interpretation = interpretTA(row.price, row.indicators, row.patterns);
                const signed = (v: number | null) =>
                  v === null ? "Unavailable" : v > 0 ? `+${v}` : String(v);
                return (
                  <tr key={row.id} className="border-b border-border/50 align-top">
                    <td className="p-2">
                      {row.symbol}
                      <div className="text-muted-foreground">{row.source}</div>
                      <div className="text-muted-foreground">
                        {row.timeframe === 15 ? "15m" : `${row.timeframe / 60}h`} · {row.version}
                      </div>
                    </td>
                    <td className="p-2 whitespace-nowrap">
                      {new Date(row.candle_at).toLocaleString()}
                      <div className="text-muted-foreground">
                        Closed{" "}
                        {new Date(
                          Date.parse(row.candle_at) + row.timeframe * 60_000,
                        ).toLocaleString()}
                      </div>
                    </td>
                    <td className="p-2 min-w-52">
                      <div
                        className={
                          interpretation.trend === "Bullish"
                            ? "text-emerald-400"
                            : interpretation.trend === "Bearish"
                              ? "text-rose-400"
                              : "text-muted-foreground"
                        }
                      >
                        {interpretation.trend} trend
                      </div>
                      <div>{interpretation.momentum} momentum</div>
                      <div className="mt-1 text-muted-foreground">{interpretation.support}</div>
                      <details className="mt-2 max-w-md">
                        <summary className="cursor-pointer">Indicator explanations</summary>
                        <dl className="mt-2 space-y-3">
                          {explainTA(row.price, row.indicators, row.patterns).map((item) => (
                            <div key={item.label}>
                              <dt className="font-medium">
                                {item.label}: {item.value}
                              </dt>
                              <dd className="mt-1 text-muted-foreground">{item.explanation}</dd>
                            </div>
                          ))}
                        </dl>
                      </details>
                    </td>
                    <td className="p-2 min-w-44">
                      <details>
                        <summary className="cursor-pointer rounded-sm focus-visible:outline focus-visible:outline-2">
                          {signed(interpretation.score)}{" "}
                          <span className="text-muted-foreground">/ ±100</span>
                        </summary>
                        <dl className="mt-2 space-y-1">
                          {Object.entries(interpretation.contributions).map(([label, points]) => (
                            <div key={label} className="flex justify-between gap-3">
                              <dt className="capitalize">{label}</dt>
                              <dd>{signed(points)}</dd>
                            </div>
                          ))}
                        </dl>
                        <p className="mt-2 text-muted-foreground">
                          Rule-based bias, not a probability. Uses EMA20/50 trend, RSI, patterns and
                          volume only. MACD, EMA200, bands, ADX and range provide separate context
                          and add no points.
                        </p>
                        <span className="text-muted-foreground">{interpretation.version}</span>
                      </details>
                    </td>
                    <td className="p-2">
                      {number(values["ema20"])} / {number(values["ema50"])} /{" "}
                      {number(values["ema200"])}
                    </td>
                    <td className="p-2">{number(values["rsi14"])}</td>
                    <td className="p-2">
                      {number(values["atr14"])}
                      <div className="text-muted-foreground">
                        {interpretation.atrPct === null
                          ? "--"
                          : `${interpretation.atrPct.toFixed(2)}%`}
                      </div>
                    </td>
                    <td className="p-2">
                      {values["volume_change_pct"] === null
                        ? "--"
                        : `${number(values["volume_change_pct"])}%`}
                    </td>
                    <td className="p-2 max-w-52 break-words">
                      {row.patterns.map((p) => p.replaceAll("_", " ")).join(", ") || "None"}
                    </td>
                    <td className="p-2">
                      {row.return_pct === null ? row.outcome_status : `${number(row.return_pct)}%`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="mt-2 flex items-center justify-end gap-2 text-xs text-muted-foreground">
        <Button
          variant="ghost"
          size="icon"
          title="Previous page"
          aria-label="Previous TA page"
          disabled={page === 0 || history.isFetching}
          onClick={() => setPage(page - 1)}
        >
          <ChevronLeft className="size-4" />
        </Button>
        <span>Page {page + 1}</span>
        <Button
          variant="ghost"
          size="icon"
          title="Next page"
          aria-label="Next TA page"
          disabled={!history.data || history.data.length <= 25 || history.isFetching}
          onClick={() => setPage(page + 1)}
        >
          <ChevronRight className="size-4" />
        </Button>
      </div>
    </section>
  );
}
