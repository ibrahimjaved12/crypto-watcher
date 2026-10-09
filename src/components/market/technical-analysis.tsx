import { Fragment, useEffect, useState } from "react";
import { automaticQueryOptions, logActivity } from "@/lib/activity-controls";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { Hint } from "@/components/hint";
import { humanizeReason, GLOSSARY } from "@/lib/labels";
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
  const [showAll, setShowAll] = useState(false);
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
    typeof v === "number" && Number.isFinite(v)
      ? v.toLocaleString(undefined, { maximumFractionDigits: 4 })
      : "—";
  const signed = (v: number | null) => (v === null ? "—" : `${v > 0 ? "+" : ""}${number(v)}`);
  const headers = [
    ["Pair / candle", "The pair and opening time of the completed candle, in your local time."],
    [
      "Trend lean",
      "A description of the indicators, not a recommendation to trade. Expand for the reasoning.",
    ],
    ["Indicator score", GLOSSARY["indicator score"]!.plain],
    ["Volatility (ATR %)", GLOSSARY["ATR"]!.plain],
    [
      "What happened next",
      "The price return after the signal's timeframe elapsed. Not yet known means the outcome is still pending.",
    ],
  ];
  return (
    <section className="panel mt-6 min-w-0 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h2 className="text-lg font-semibold">Indicator history</h2>
          <p className="text-xs text-muted-foreground">
            Saved market reads and what happened after each candle.
          </p>
        </div>
        <Button
          variant="ghost"
          className="ml-auto"
          onClick={() => setShowAll(!showAll)}
          aria-expanded={showAll}
          aria-controls="indicator-history-rows"
        >
          {showAll ? "Show 10" : "Show all"}
        </Button>
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
                setExpanded(null);
              }}
            >
              {label}
            </Button>
          ))}
        </div>
        <input
          aria-label="Filter indicator history by pair"
          placeholder="Pair, e.g. BTCUSDT"
          value={symbol}
          onChange={(e) => {
            setSymbol(e.target.value);
            setPage(0);
            setExpanded(null);
          }}
          className="h-9 w-44 rounded-md border border-input bg-background px-3 text-sm"
        />
        <Button
          variant="ghost"
          size="icon"
          title="Refresh indicator history"
          aria-label="Refresh indicator history"
          disabled={history.isFetching}
          onClick={() => history.refetch()}
        >
          <RefreshCw className="size-4" />
        </Button>
      </div>
      {!automatic.enabled && (
        <p className="mt-2 text-xs text-muted-foreground">
          Automatic history refresh is paused. Press Refresh after changing filters or pages.
        </p>
      )}
      {history.isPending && !history.isFetching && !automatic.enabled ? (
        <p className="py-4 text-sm text-muted-foreground">
          Press Refresh to load saved market reads.
        </p>
      ) : history.isPending ? (
        <p className="py-4 text-sm">Loading indicator history…</p>
      ) : history.error ? (
        <div role="alert" className="py-4 text-sm text-bear">
          {humanizeReason(history.error.message).short}
          <details className="text-xs text-muted-foreground">
            <summary>Details</summary>
            {history.error.message}
          </details>
        </div>
      ) : !history.data?.length ? (
        <p className="py-6 text-sm text-muted-foreground">
          No saved market reads yet. They appear after monitoring collects enough completed candles
          for this pair and timeframe.
        </p>
      ) : (
        <div
          className="mt-3 overflow-x-auto"
          tabIndex={0}
          role="region"
          aria-label="Indicator history table"
        >
          <table className="w-full min-w-[620px] text-left text-xs">
            <thead className="bg-secondary/40">
              <tr className="border-b border-border">
                {headers.map(([heading, help]) => (
                  <th
                    scope="col"
                    className="p-3 font-medium text-muted-foreground"
                    key={heading}
                    title={help}
                  >
                    {heading === "Indicator score" ? (
                      <Hint term="indicator score" />
                    ) : heading === "Volatility (ATR %)" ? (
                      <Hint term="ATR" />
                    ) : (
                      heading
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody id="indicator-history-rows">
              {history.data.slice(0, showAll ? 25 : 10).map((row) => {
                const values = (row.indicators ?? {}) as Record<string, unknown>;
                const factors = (row.factor_breakdown ?? {}) as Record<string, unknown>;
                const context = explainTA(row.price, row.indicators, row.patterns);
                const momentum = context
                  .find((item) => item.label.startsWith("MACD"))
                  ?.value.split(" · ")[0];
                const strength = context
                  .find((item) => item.label.startsWith("ADX"))
                  ?.value.split(" · ")[0];
                const tone =
                  row.classification === "bullish"
                    ? "text-bull"
                    : row.classification === "bearish"
                      ? "text-bear"
                      : "text-muted-foreground";
                return (
                  <Fragment key={row.id}>
                    <tr className="border-b border-border/50 align-top">
                      <td className="p-3">
                        <span className="font-medium">{row.symbol}</span> ·{" "}
                        {row.timeframe === 15 ? "15m" : `${row.timeframe / 60}h`}
                        <div className="mt-1 text-muted-foreground">
                          <time dateTime={row.candle_at}>
                            {new Date(row.candle_at).toLocaleString()}
                          </time>
                        </div>
                      </td>
                      <td className="max-w-56 p-3">
                        <div className={`font-medium ${tone}`}>
                          {humanizeReason(row.classification).short}
                        </div>
                        <p className="mt-1 text-muted-foreground">
                          {momentum} momentum · {strength?.toLowerCase()}
                        </p>
                        <button
                          className="mt-1 text-primary"
                          aria-expanded={expanded === row.id}
                          aria-controls={`indicators-${row.id}`}
                          onClick={() => setExpanded(expanded === row.id ? null : row.id)}
                        >
                          {expanded === row.id ? "Hide details" : "Show details"}
                        </button>
                      </td>
                      <td className="p-3">
                        <span className={`num ${tone}`}>
                          {signed(row.score)} <span className="text-muted-foreground">/ ±100</span>
                        </span>
                        <div
                          className="relative mt-2 h-1.5 w-24 rounded-full bg-secondary"
                          aria-hidden
                        >
                          <span className="absolute left-1/2 h-1.5 w-px bg-muted-foreground" />
                          {row.score !== null && (
                            <span
                              className={`absolute h-1.5 rounded-full ${row.score < 0 ? "bg-bear" : "bg-bull"}`}
                              style={{
                                left: `${row.score < 0 ? 50 - Math.abs(row.score) / 2 : 50}%`,
                                width: `${Math.abs(row.score) / 2}%`,
                              }}
                            />
                          )}
                        </div>
                      </td>
                      <td className="num p-3">
                        {row.atr_pct === null ? "—" : `${row.atr_pct.toFixed(2)}%`}
                      </td>
                      <td className="num p-3">
                        {row.return_pct === null
                          ? humanizeReason(row.outcome_status).short
                          : `${signed(row.return_pct)}%`}
                      </td>
                    </tr>
                    {expanded === row.id && (
                      <tr
                        id={`indicators-${row.id}`}
                        className="border-b border-border bg-secondary/20"
                      >
                        <td colSpan={5} className="p-4">
                          <dl className="grid gap-4 sm:grid-cols-2">
                            <div>
                              <dt>
                                <Hint term="EMA">Moving averages · 20 / 50 / 200</Hint>
                              </dt>
                              <dd className="num">
                                {number(values["ema20"])} / {number(values["ema50"])} /{" "}
                                {number(values["ema200"])} USDT
                              </dd>
                            </div>
                            <div>
                              <dt>
                                <Hint term="RSI" />
                              </dt>
                              <dd className="num">{number(values["rsi14"])}</dd>
                            </div>
                            <div>
                              <dt>
                                <Hint term="ATR">Typical movement · 14 candles</Hint>
                              </dt>
                              <dd className="num">
                                {number(values["atr14"])} USDT ·{" "}
                                {row.atr_pct === null ? "—" : `${row.atr_pct.toFixed(2)}%`}
                              </dd>
                            </div>
                            <div>
                              <dt title="Compared with average volume of the preceding 20 candles.">
                                Volume change
                              </dt>
                              <dd className="num">
                                {typeof values["volume_change_pct"] === "number"
                                  ? `${signed(values["volume_change_pct"])}%`
                                  : "—"}
                              </dd>
                            </div>
                            <div>
                              <dt title="Patterns detected in the completed candle; these do not guarantee a price move.">
                                Detected patterns
                              </dt>
                              <dd>
                                {row.patterns
                                  .map((pattern) => humanizeReason(pattern).short)
                                  .join(", ") || "None"}
                              </dd>
                            </div>
                            <div>
                              <dt>Candle closed (local time)</dt>
                              <dd>
                                {new Date(
                                  Date.parse(row.candle_at) + row.timeframe * 60_000,
                                ).toLocaleString()}
                              </dd>
                            </div>
                          </dl>
                          <details className="mt-3">
                            <summary>Indicator explanations</summary>
                            <dl className="grid gap-4 sm:grid-cols-2">
                              {context.map((item) => (
                                <div key={item.label}>
                                  <dt className="font-medium">
                                    {item.label}: {item.value}
                                  </dt>
                                  <dd className="mt-1 text-muted-foreground">{item.explanation}</dd>
                                </div>
                              ))}
                            </dl>
                          </details>
                          <details>
                            <summary>Score contributions</summary>
                            <dl className="space-y-2">
                              {Object.entries(factors).map(([name, raw]) => {
                                const value =
                                  raw && typeof raw === "object" && !Array.isArray(raw)
                                    ? (raw as Record<string, unknown>)
                                    : {};
                                return (
                                  <div key={name}>
                                    <dt>
                                      {humanizeReason(name).short}:{" "}
                                      <span className="num">
                                        {signed(
                                          typeof value["contribution"] === "number"
                                            ? value["contribution"]
                                            : null,
                                        )}
                                      </span>
                                    </dt>
                                    <dd className="text-muted-foreground">
                                      {
                                        humanizeReason(
                                          typeof value["reason"] === "string"
                                            ? value["reason"]
                                            : null,
                                        ).short
                                      }
                                    </dd>
                                  </div>
                                );
                              })}
                            </dl>
                            <p className="mt-2 text-muted-foreground">
                              Only trend, momentum, patterns and volume contribute points. Other
                              indicators provide context; agreement is not independent confirmation.
                            </p>
                          </details>
                          <details className="break-words text-muted-foreground">
                            <summary>Technical details</summary>
                            <p>
                              Exchange: {row.source} · source symbol: {row.source_native_symbol} ·{" "}
                              {row.version} · {row.strategy_version}
                            </p>
                            <p>
                              Reasons: {row.reasons.join(", ") || "none"} · outcome:{" "}
                              {row.outcome_status}
                            </p>
                          </details>
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
      <div className="mt-3 flex flex-wrap items-center justify-end gap-2 text-xs text-muted-foreground">
        {!!history.data?.length && (
          <span>
            {Math.min(history.data.length, showAll ? 25 : 10)} of{" "}
            {Math.min(history.data.length, 25)} on this page
          </span>
        )}
        <Button
          variant="ghost"
          size="icon"
          title="Previous page"
          aria-label="Previous indicator page"
          disabled={page === 0 || history.isFetching}
          onClick={() => {
            setPage(page - 1);
            setExpanded(null);
          }}
        >
          <ChevronLeft className="size-4" />
        </Button>
        <span>Page {page + 1}</span>
        <Button
          variant="ghost"
          size="icon"
          title="Next page"
          aria-label="Next indicator page"
          disabled={!history.data || history.data.length <= 25 || history.isFetching}
          onClick={() => {
            setPage(page + 1);
            setExpanded(null);
          }}
        >
          <ChevronRight className="size-4" />
        </Button>
      </div>
    </section>
  );
}
