import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";

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
        .eq("timeframe", frame)
        .order("candle_at", { ascending: false })
        .order("id", { ascending: false })
        .range(page * 25, page * 25 + 25);
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
                  "EMA 20 / 50",
                  "RSI 14",
                  "ATR 14",
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
                const values = row.indicators as Record<string, unknown>;
                return (
                  <tr key={row.id} className="border-b border-border/50 align-top">
                    <td className="p-2">
                      {row.symbol}
                      <div className="text-muted-foreground">{row.source}</div>
                    </td>
                    <td className="p-2 whitespace-nowrap">
                      {new Date(row.candle_at).toLocaleString()}
                    </td>
                    <td className="p-2">
                      {number(values["ema20"])} / {number(values["ema50"])}
                    </td>
                    <td className="p-2">{number(values["rsi14"])}</td>
                    <td className="p-2">{number(values["atr14"])}</td>
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
