import { Fragment, useEffect, useState } from "react";
import { automaticQueryOptions, logActivity } from "@/lib/activity-controls";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorNotice, StatusBadge, TechnicalDetails } from "@/components/presentation";
import {
  humanizeCode,
  outcomeLabel,
  pairLabel,
  sourceLabel,
  statusTone,
} from "@/lib/presentation/labels";
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
  const [expanded, setExpanded] = useState<string | null>(null);
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
    <section className="panel mt-8 min-w-0 p-5 sm:p-6">
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
          aria-label="Filter analysis by pair"
          placeholder="Pair, e.g. BTCUSDT"
          value={symbol}
          onChange={(e) => {
            setSymbol(e.target.value);
            setPage(0);
          }}
          className="h-9 w-48 rounded-md border border-input bg-background px-3 text-sm"
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
        Saved snapshots from completed 15m, 1h and 4h candles. Scores describe rule-based
        directional bias, not win probability. Expand a record for indicators and context.
      </p>
      {!automatic.enabled && (
        <p className="mt-2 text-xs text-muted-foreground">
          Automatic history refresh is paused. Press Refresh after changing filters or pages.
        </p>
      )}
      {history.isPending && !history.isFetching && !automatic.enabled ? (
        <p className="py-4 text-sm">Press Refresh to load analysis.</p>
      ) : history.isPending ? (
        <p className="py-4 text-sm">Loading analysis...</p>
      ) : history.error ? (
        <ErrorNotice title="Saved analysis unavailable." error={history.error} />
      ) : !history.data?.length ? (
        <div className="mt-4">
          <EmptyState title="No saved snapshots in this view">
            Try another pair or timeframe. Monitoring saves new snapshots when completed-candle
            analysis is enabled and enough history is available.
          </EmptyState>
        </div>
      ) : (
        <div
          className="mt-4 overflow-x-auto"
          tabIndex={0}
          role="region"
          aria-label="Saved analysis history"
        >
          <table className="data-table min-w-[960px]">
            <caption className="sr-only">
              Completed-candle market snapshots. All candle times use your local timezone.
            </caption>
            <thead>
              <tr>
                {[
                  "Pair / timeframe",
                  "Candle time",
                  "Market reading",
                  "Rule score",
                  "RSI 14",
                  "Volatility / ATR %",
                  "Notable signals",
                  "Outcome",
                  "Details",
                ].map((h) => (
                  <th scope="col" key={h}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {history.data.slice(0, 25).map((row) => {
                const values = (row.indicators ?? {}) as Record<string, unknown>;
                const factors = (row.factor_breakdown ?? {}) as Record<string, unknown>;
                const isOpen = expanded === row.id;
                return (
                  <Fragment key={row.id}>
                    <tr>
                      <td>
                        <span className="font-semibold">{pairLabel(row.symbol)}</span>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {row.timeframe === 15 ? "15m" : `${row.timeframe / 60}h`}
                        </p>
                      </td>
                      <td className="whitespace-nowrap text-xs">
                        {new Date(row.candle_at).toLocaleDateString()}
                        <p className="num mt-1 text-muted-foreground">
                          {new Date(row.candle_at).toLocaleTimeString()}
                        </p>
                      </td>
                      <td>
                        <StatusBadge tone={statusTone(row.classification)}>
                          {humanizeCode(row.classification)}
                        </StatusBadge>
                      </td>
                      <td className="num">
                        {row.score === null ? "—" : `${row.score > 0 ? "+" : ""}${row.score}`}
                        <p className="mt-1 text-xs text-muted-foreground">/ ±100</p>
                      </td>
                      <td className="num">{number(values["rsi14"])}</td>
                      <td className="num">
                        {row.atr_pct === null ? "—" : `${row.atr_pct.toFixed(2)}%`}
                      </td>
                      <td className="max-w-48 text-xs">
                        {row.patterns.map(humanizeCode).join(", ") || "None detected"}
                      </td>
                      <td>
                        {row.return_pct === null ? (
                          outcomeLabel(row.outcome_status)
                        ) : (
                          <>
                            <span className="num">
                              {row.return_pct > 0 ? "+" : ""}
                              {number(row.return_pct)}%
                            </span>
                            <p className="mt-1 text-xs text-muted-foreground">
                              {outcomeLabel(row.outcome_status)}
                            </p>
                          </>
                        )}
                      </td>
                      <td>
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-expanded={isOpen}
                          aria-controls={`snapshot-${row.id}`}
                          aria-label={`${isOpen ? "Hide" : "View"} details for ${pairLabel(row.symbol)} at ${new Date(row.candle_at).toLocaleString()}`}
                          onClick={() => setExpanded(isOpen ? null : row.id)}
                        >
                          {isOpen ? "Hide" : "View"}
                        </Button>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr id={`snapshot-${row.id}`}>
                        <td colSpan={9} className="bg-background/40">
                          <div className="grid gap-6 lg:grid-cols-[1fr_2fr]">
                            <div className="space-y-4">
                              <div>
                                <h3 className="font-semibold">Snapshot context</h3>
                                <p className="mt-2 text-sm text-muted-foreground">
                                  {sourceLabel(row.source)} · Close price{" "}
                                  <span className="num">{number(row.price)} USDT</span>
                                </p>
                                <p className="mt-1 text-xs text-muted-foreground">
                                  Candle closed{" "}
                                  {new Date(
                                    Date.parse(row.candle_at) + row.timeframe * 60_000,
                                  ).toLocaleString()}
                                </p>
                              </div>
                              <div>
                                <h3 className="font-semibold">Factor contributions</h3>
                                <dl className="mt-2 space-y-2">
                                  {Object.entries(factors).map(([name, raw]) => {
                                    const factor =
                                      raw && typeof raw === "object" && !Array.isArray(raw)
                                        ? (raw as Record<string, unknown>)
                                        : {};
                                    return (
                                      <div
                                        key={name}
                                        className="flex justify-between gap-3 text-sm"
                                      >
                                        <dt>
                                          {humanizeCode(name)}
                                          <span className="ml-2 text-xs text-muted-foreground">
                                            {humanizeCode(
                                              typeof factor["classification"] === "string"
                                                ? factor["classification"]
                                                : null,
                                            )}
                                          </span>
                                        </dt>
                                        <dd className="num">{number(factor["contribution"])}</dd>
                                      </div>
                                    );
                                  })}
                                </dl>
                                <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
                                  EMA20/50 trend, RSI, patterns and volume contribute to the rule
                                  score. MACD, EMA200, bands, ADX and range add context, not points.
                                  Indicator agreement is not independent confirmation.
                                </p>
                              </div>
                              <TechnicalDetails>
                                <p>
                                  Source: <code>{row.source}</code> · Native symbol:{" "}
                                  <code>{row.source_native_symbol}</code>
                                </p>
                                <p>
                                  Versions:{" "}
                                  <code>
                                    {row.version} · {row.strategy_version}
                                  </code>
                                </p>
                                <p>
                                  Classification: <code>{row.classification}</code> · Outcome:{" "}
                                  <code>{row.outcome_status}</code>
                                </p>
                                <p>
                                  Reasons: <code>{row.reasons.join(", ") || "none"}</code>
                                </p>
                                <pre className="whitespace-pre-wrap text-xs">
                                  {JSON.stringify(row.factor_breakdown, null, 2)}
                                </pre>
                              </TechnicalDetails>
                            </div>
                            <div>
                              <h3 className="mb-3 font-semibold">Indicators & explanations</h3>
                              <dl className="grid gap-4 md:grid-cols-2">
                                {explainTA(row.price, row.indicators, row.patterns).map((item) => (
                                  <div
                                    key={item.label}
                                    className="rounded-lg border border-border/70 p-3"
                                  >
                                    <dt className="font-medium">
                                      {humanizeCode(item.label)}
                                      <p className="num mt-1 text-xs text-primary">{item.value}</p>
                                    </dt>
                                    <dd className="mt-2 text-xs leading-relaxed text-muted-foreground">
                                      {item.explanation}
                                    </dd>
                                  </div>
                                ))}
                              </dl>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
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
          aria-label="Previous analysis page"
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
          aria-label="Next analysis page"
          disabled={!history.data || history.data.length <= 25 || history.isFetching}
          onClick={() => setPage(page + 1)}
        >
          <ChevronRight className="size-4" />
        </Button>
      </div>
    </section>
  );
}
