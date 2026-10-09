import { humanizeCode, sourceLabel, pairLabel, outcomeLabel } from "@/lib/presentation/labels";
import { TechnicalDetails } from "@/components/presentation";
import { useEffect, useState } from "react";
import { automaticQueryOptions, logActivity } from "@/lib/activity-controls";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { explainTA } from "@/lib/ta/interpretation";

export function TechnicalAnalysis() {
  const automatic = automaticQueryOptions(import.meta.env["VITE_TA_HISTORY_AUTO_REFRESH_ENABLED"]);
  useEffect(() => {
    if (!automatic.enabled)
      logActivity(import.meta.env["VITE_ACTIVITY_DIAGNOSTICS"], "ta-history", "automatic-paused");
  }, [automatic.enabled]);
  const [frame, setFrame] = useState(15);
  const [symbol, setSymbol] = useState("");
  const [page, setPage] = useState(0);
  const history = useQuery({
    queryKey: ["ta", frame, symbol, page],
    queryFn: async () => {
      logActivity(import.meta.env["VITE_ACTIVITY_DIAGNOSTICS"], "ta-history", "request-started");
      let query = supabase
        .from("ta_signals")
        .select(
          "id, symbol, timeframe, candle_at, source, source_native_symbol, version, strategy_version, price, classification, score, atr_pct, factor_breakdown, reasons, indicators, patterns, outcome_status, return_pct",
        )
        .order("candle_at", { ascending: false })
        .order("id", { ascending: false })
        .range(page * 25, page * 25 + 25);
      if (frame !== 0) query = query.eq("timeframe", frame);
      if (symbol.trim()) query = query.eq("symbol", symbol.trim().toUpperCase());
      const { data, error } = await query;
      if (error) throw error;
      return data;
    },
    ...automatic,
  });
  const number = (v: unknown) =>
    typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 5 }) : "--";
  return (
    <section className="mt-6 min-w-0 border-t border-border pt-5">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-semibold">Saved analysis history</h2>
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
        agreement is not independent confirmation. Scores express rule-based bias, not win
        probability. Expand a record for indicators and technical details.
      </p>
      {!automatic.enabled && (
        <p className="mt-2 text-xs text-muted-foreground">
          Automatic history refresh paused. Press Refresh after changing filters or pages.
        </p>
      )}
      {history.isPending && !history.isFetching && !automatic.enabled ? (
        <p className="py-4 text-sm">Press Refresh to load analysis.</p>
      ) : history.isPending ? (
        <p role="status" className="py-4 text-sm">
          Loading analysis…
        </p>
      ) : history.error ? (
        <div role="alert" className="py-4 text-sm text-destructive">
          Analysis history unavailable.<TechnicalDetails>{history.error.message}</TechnicalDetails>
        </div>
      ) : !history.data?.length ? (
        <p className="py-4 text-sm text-muted-foreground">
          No saved snapshots yet. A monitoring check can record completed-candle analysis when
          enough history is available.
        </p>
      ) : (
        <div
          className="mt-4 max-h-[34rem] overflow-auto"
          role="region"
          tabIndex={0}
          aria-label="Technical analysis history"
        >
          <table className="data-table">
            <thead className="sticky top-0 bg-background">
              <tr className="border-b border-border">
                {[
                  "Pair / timeframe",
                  "Candle (local time)",
                  "Interpretation",
                  "Rule score",
                  "RSI 14",
                  "ATR volatility",
                  "Signals",
                  "Outcome",
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
                const factors = (row.factor_breakdown ?? {}) as Record<string, unknown>;
                const factor = (name: string) => {
                  const value = factors[name];
                  return value && typeof value === "object" && !Array.isArray(value)
                    ? (value as Record<string, unknown>)
                    : {};
                };
                const label = humanizeCode;
                const signed = (v: number | null) =>
                  v === null ? "Unavailable" : v > 0 ? `+${v}` : String(v);
                return (
                  <tr key={row.id} className="border-b border-border/50 align-top">
                    <td className="p-2">
                      {pairLabel(row.symbol)}
                      <div className="text-muted-foreground">{sourceLabel(row.source)}</div>
                      <div className="text-muted-foreground">
                        {row.timeframe === 15 ? "15m" : `${row.timeframe / 60}h`}
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
                          row.classification === "bullish"
                            ? "text-emerald-400"
                            : row.classification === "bearish"
                              ? "text-rose-400"
                              : "text-muted-foreground"
                        }
                      >
                        {label(factor("trend")["classification"])} trend
                      </div>
                      <div>{label(factor("momentum")["classification"])} momentum</div>
                      <div className="mt-1 text-muted-foreground">
                        {label(factor("patterns")["classification"])}
                      </div>
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
                      <TechnicalDetails>
                        <p>
                          Source: {row.source} · {row.source_native_symbol}
                        </p>
                        <p>
                          Versions: {row.version} · {row.strategy_version}
                        </p>
                        <p>
                          EMA 20 / 50 / 200: {number(values["ema20"])} / {number(values["ema50"])} /{" "}
                          {number(values["ema200"])}
                        </p>
                        <p>
                          ATR 14: {number(values["atr14"])} · Volume change:{" "}
                          {values["volume_change_pct"] == null
                            ? "—"
                            : `${number(values["volume_change_pct"])}%`}
                        </p>
                        <p>
                          Classification: {row.classification} · outcome: {row.outcome_status}
                        </p>
                        <p>Reasons: {row.reasons.join(", ") || "none"}</p>
                        <pre className="whitespace-pre-wrap">
                          {JSON.stringify(row.factor_breakdown, null, 2)}
                        </pre>
                      </TechnicalDetails>
                    </td>
                    <td className="p-2 min-w-44">
                      <details>
                        <summary className="cursor-pointer rounded-sm focus-visible:outline focus-visible:outline-2">
                          {signed(row.score)} <span className="text-muted-foreground">/ ±100</span>
                        </summary>
                        <dl className="mt-2 space-y-1">
                          {Object.entries(factors).map(([name, raw]) => {
                            const value =
                              raw && typeof raw === "object" && !Array.isArray(raw)
                                ? (raw as Record<string, unknown>)
                                : {};
                            const points =
                              typeof value["contribution"] === "number"
                                ? value["contribution"]
                                : null;
                            return (
                              <div key={name} className="flex justify-between gap-3">
                                <dt className="capitalize">{humanizeCode(name)}</dt>
                                <dd>{signed(points)}</dd>
                              </div>
                            );
                          })}
                        </dl>
                        <p className="mt-2 text-muted-foreground">
                          Rule-based bias, not a probability. Uses EMA20/50 trend, RSI, patterns and
                          volume only. MACD, EMA200, bands, ADX and range provide separate context
                          and add no points.
                        </p>
                      </details>
                    </td>
                    <td className="p-2">{number(values["rsi14"])}</td>
                    <td className="p-2">
                      <div className="text-muted-foreground">
                        {row.atr_pct === null ? "--" : `${row.atr_pct.toFixed(2)}%`}
                      </div>
                    </td>
                    <td className="p-2 max-w-52 break-words">
                      {row.patterns.map(humanizeCode).join(", ") || "None"}
                    </td>
                    <td className="p-2">
                      {row.return_pct === null
                        ? outcomeLabel(row.outcome_status)
                        : `${number(row.return_pct)}%`}
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
