import { useEffect, useState, type ReactNode } from "react";
import { automaticQueryOptions, logActivity } from "@/lib/activity-controls";
import { useQueries, useQuery } from "@tanstack/react-query";
import {
  ArrowDownRight,
  ArrowUpRight,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  History,
  Minus,
  RefreshCw,
} from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { Hint } from "@/components/hint";
import { EmptyState, SectionTitle, SignedBar } from "@/components/plain";
import { humanizeReason, humanizeToken, sourceLabel } from "@/lib/labels";
import { explainTA } from "@/lib/ta/interpretation";
import { TA_FRAMES } from "@/lib/ta/schedule";
import { cn } from "@/lib/utils";
import { fetchWatchlist } from "@/lib/db";

export function TechnicalAnalysis() {
  const automatic = automaticQueryOptions(import.meta.env["VITE_TA_HISTORY_AUTO_REFRESH_ENABLED"]);
  useEffect(() => {
    if (!automatic.enabled)
      logActivity(import.meta.env["VITE_ACTIVITY_DIAGNOSTICS"], "ta-history", "automatic-paused");
  }, [automatic.enabled]);
  const [frame, setFrame] = useState(15);
  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: fetchWatchlist });
  const defaultSymbol = watchlist.data?.[0]?.symbol ?? "BTCUSDT";
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [symbolTouched, setSymbolTouched] = useState(false);
  const symbolReady = symbolTouched || watchlist.isSuccess || watchlist.isError;
  useEffect(() => {
    if (!symbolTouched) setSymbol(defaultSymbol);
  }, [defaultSymbol, symbolTouched]);
  const [page, setPage] = useState(0);
  const history = useQuery({
    queryKey: ["ta", frame, symbol, page],
    queryFn: () => loadHistory(frame, symbol, page * 25, 26),
    ...automatic,
    enabled: symbolReady && automatic.enabled,
  });
  const selectedSymbol = symbol.trim().toUpperCase();
  const latestByFrame = useQueries({
    queries: TA_FRAMES.map((timeframe) => ({
      queryKey: ["ta", timeframe, selectedSymbol, "latest"],
      queryFn: () => loadHistory(timeframe, selectedSymbol, 0, 1),
      ...automatic,
      enabled: symbolReady && automatic.enabled && selectedSymbol.length > 0,
    })),
  });
  const refreshing = history.isFetching || latestByFrame.some((query) => query.isFetching);
  const [showAll, setShowAll] = useState(false);
  const rows = (history.data ?? []).slice(0, 25);
  const visible = showAll ? rows : rows.slice(0, 10);
  return (
    <section className="panel mt-6 min-w-0 p-4 sm:p-5">
      <SectionTitle
        title="Indicator history"
        hint="Saved indicator snapshots for every completed 15m, 1h and 4h candle, newest first, with what price did next."
        right={
          <>
            <div className="flex rounded-md bg-muted/60 p-0.5" role="group" aria-label="Timeframe">
              {[
                [0, "All"],
                [15, "15m"],
                [60, "1h"],
                [240, "4h"],
              ].map(([value, label]) => (
                <Button
                  key={value}
                  variant={frame === value ? "secondary" : "ghost"}
                  className="h-9 px-3"
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
              aria-label="Filter by pair"
              placeholder="Pair, e.g. BTCUSDT"
              value={symbol}
              onChange={(e) => {
                setSymbolTouched(true);
                setSymbol(e.target.value);
                setPage(0);
              }}
              className="h-9 w-40 rounded-md border border-input bg-background px-3 text-sm"
            />
            <Button
              variant="ghost"
              size="icon"
              title="Refresh indicator history"
              aria-label="Refresh indicator history"
              disabled={refreshing}
              onClick={() => {
                void history.refetch();
                if (selectedSymbol) {
                  for (const query of latestByFrame) void query.refetch();
                }
              }}
            >
              <RefreshCw className={cn("size-4", refreshing && "animate-spin")} />
            </Button>
          </>
        }
      />
      {!automatic.enabled && (
        <p className="mb-2 text-xs text-muted-foreground">
          Automatic refresh is paused. Press Refresh after changing filters or pages.
        </p>
      )}
      <div className="mb-4" role="group" aria-label="Latest by timeframe">
        <p className="mb-2 text-xs font-medium">
          Latest by timeframe{selectedSymbol ? ` · ${selectedSymbol}` : " · enter a pair above"}
        </p>
        <div className="grid gap-2 sm:grid-cols-3">
          {TA_FRAMES.map((timeframe, index) => {
            const query = latestByFrame[index]!;
            const row = query.data?.[0];
            return (
              <button
                key={timeframe}
                type="button"
                aria-pressed={frame === timeframe}
                className={cn(
                  "min-w-0 rounded-md border p-3 text-left text-xs hover:bg-accent/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  frame === timeframe ? "border-primary bg-accent/30" : "border-border",
                )}
                onClick={() => {
                  setFrame(timeframe);
                  setPage(0);
                }}
              >
                <span className="font-medium">{timeframeLabel(timeframe)}</span>
                {!selectedSymbol ? (
                  <span className="ml-2 text-muted-foreground">Select a pair</span>
                ) : row ? (
                  <>
                    <span className="num mt-1 block text-muted-foreground">
                      {candleClosedAt(row).toLocaleString(undefined, { dateStyle: "short", timeStyle: "medium" })}
                    </span>
                    <span className="num my-1 block">Candle close: {number(row.price)}</span>
                    <HistorySignal row={row} />
                    <HistoryScore row={row} />
                  </>
                ) : query.isError ? (
                  <span className="ml-2 text-destructive">Could not load</span>
                ) : query.isPending ? (
                  <span className="ml-2 text-muted-foreground">
                    {!symbolReady || query.isFetching ? "Loading…" : "Press Refresh to load"}
                  </span>
                ) : (
                  <span className="ml-2 text-muted-foreground">no data yet</span>
                )}
              </button>
            );
          })}
        </div>
      </div>
      {symbolReady && history.isPending && !history.isFetching && !automatic.enabled ? (
        <p className="py-4 text-sm">Press Refresh to load the indicator history.</p>
      ) : !symbolReady || history.isPending ? (
        <p className="py-4 text-sm text-muted-foreground">Loading indicator history…</p>
      ) : history.error ? (
        <p role="alert" className="py-4 text-sm text-destructive">
          Indicator history could not be loaded: {history.error.message}
        </p>
      ) : !rows.length ? (
        <EmptyState
          icon={History}
          title="No indicator snapshots yet"
          body="A snapshot is saved after each completed 15m, 1h and 4h candle once monitoring and technical analysis are switched on in the Control room."
          className="py-6"
        />
      ) : (
        <>
          <div className="-mx-1 overflow-x-auto px-1" tabIndex={0} aria-label="Indicator history">
            <table className="w-full min-w-[780px] text-left text-xs">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border">
                  {HEADERS.map(([h, help]) => (
                    <th className="p-2 font-medium" key={h}>
                      {help ? <Hint text={help}>{h}</Hint> : h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visible.map((row) => (
                  <HistoryRow key={row.id} row={row} />
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {rows.length > 10 ? (
              <Button variant="ghost" className="h-9" onClick={() => setShowAll(!showAll)}>
                {showAll ? "Show fewer" : `Show all ${rows.length} on this page`}
              </Button>
            ) : null}
            <span className="ml-auto">Page {page + 1}</span>
            <Button
              variant="ghost"
              size="icon"
              title="Previous page"
              aria-label="Previous page of indicator history"
              disabled={page === 0 || history.isFetching}
              onClick={() => setPage(page - 1)}
            >
              <ChevronLeft className="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              title="Next page"
              aria-label="Next page of indicator history"
              disabled={!history.data || history.data.length <= 25 || history.isFetching}
              onClick={() => setPage(page + 1)}
            >
              <ChevronRight className="size-4" />
            </Button>
          </div>
        </>
      )}
    </section>
  );
}

async function loadHistory(frame: number, symbol: string, offset: number, limit: number) {
  logActivity(import.meta.env["VITE_ACTIVITY_DIAGNOSTICS"], "ta-history", "request-started");
  let query = supabase
    .from("ta_signals")
    .select(
      "id, symbol, timeframe, candle_at, source, source_native_symbol, version, strategy_version, price, classification, score, atr_pct, factor_breakdown, reasons, indicators, patterns, outcome_status, return_pct",
    )
    .order("candle_at", { ascending: false })
    .order("id", { ascending: false })
    .range(offset, offset + limit - 1);
  if (frame !== 0) query = query.eq("timeframe", frame);
  if (symbol.trim()) query = query.eq("symbol", symbol.trim().toUpperCase());
  const { data, error } = await query;
  if (error) throw error;
  return data;
}

const HEADERS: [string, string | null][] = [
  ["Pair", null],
  ["Candle closed", "Local time when the analysed candle finished. Only completed candles are used."],
  ["Candle close", "Close of the analysed candle, not the live price."],
  [
    "Trend lean",
    "Direction suggested by the 20/50-candle averages and momentum. A description of the chart, not a forecast.",
  ],
  [
    "Indicator score",
    "−100 (bearish) … +100 (bullish), from trend, RSI, candle patterns and volume. A ranking of how the indicators look, not a win probability.",
  ],
  ["Volatility (ATR %)", "Average candle range as a share of price. Higher means bigger swings, in either direction."],
  ["RSI 14", "Momentum from 0 to 100. Below 30 oversold, above 70 overbought. Not a reversal signal by itself."],
  [
    "What happened next",
    "Price change over the following period, filled in once that period has finished. “Not yet known” until then.",
  ],
  ["", null],
];

type HistoryRowData = {
  id: string;
  symbol: string;
  timeframe: number;
  candle_at: string;
  source: string;
  source_native_symbol: string;
  version: string;
  strategy_version: string;
  price: number;
  classification: string;
  score: number | null;
  atr_pct: number | null;
  factor_breakdown: unknown;
  reasons: unknown;
  indicators: unknown;
  patterns: string[];
  outcome_status: string;
  return_pct: number | null;
};

const number = (v: unknown) =>
  typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 5 }) : "—";
const signed = (v: number | null) => (v === null ? "—" : v > 0 ? `+${v}` : String(v));
const label = (value: unknown) =>
  typeof value === "string" ? humanizeToken(value) : "Unavailable";

const timeframeLabel = (timeframe: number) => timeframe === 15 ? "15m" : `${timeframe / 60}h`;
const candleClosedAt = (row: HistoryRowData) =>
  new Date(Date.parse(row.candle_at) + row.timeframe * 60_000);

function HistorySignal({ row }: { row: HistoryRowData }) {
  const factors = (row.factor_breakdown ?? {}) as Record<string, unknown>;
  const factor = (name: string) => {
    const value = factors[name];
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  };
  const tone =
    row.classification === "bullish"
      ? "text-bull"
      : row.classification === "bearish"
        ? "text-bear"
        : "text-muted-foreground";
  const LeanIcon =
    row.classification === "bullish" ? ArrowUpRight : row.classification === "bearish" ? ArrowDownRight : Minus;
  const pattern = row.patterns[0] ? humanizeToken(row.patterns[0]) : null;
  return (
    <>
      <span className={cn("inline-flex items-center gap-1 font-medium", tone)}>
        <LeanIcon className="size-3.5" aria-hidden />
        {label(factor("trend")["classification"])} trend
      </span>
      <span className="block text-muted-foreground">
        {label(factor("momentum")["classification"])} momentum{pattern ? ` · ${pattern}` : ""}
      </span>
    </>
  );
}

function HistoryScore({ row }: { row: HistoryRowData }) {
  return <SignedBar value={row.score} label={`Indicator score ${signed(row.score)} of ±100`} />;
}

function HistoryRow({ row }: { row: HistoryRowData }) {
  const [open, setOpen] = useState(false);
  const values = (row.indicators ?? {}) as Record<string, unknown>;
  const factors = (row.factor_breakdown ?? {}) as Record<string, unknown>;
  const timeframe = timeframeLabel(row.timeframe);
  const closed = candleClosedAt(row);
  return (
    <>
      <tr className="border-b border-border/50 align-top hover:bg-accent/30">
        <td className="p-2">
          <span className="font-medium">{row.symbol.replace(/USDT$/, "")}</span>
          <span className="text-muted-foreground"> · {timeframe}</span>
        </td>
        <td className="num whitespace-nowrap p-2" title={`Opened ${new Date(row.candle_at).toLocaleString()}`}>
          {closed.toLocaleString(undefined, { dateStyle: "short", timeStyle: "medium" })}
        </td>
        <td className="num whitespace-nowrap p-2">{number(row.price)}</td>
        <td className="p-2">
          <HistorySignal row={row} />
        </td>
        <td className="p-2">
          <HistoryScore row={row} />
        </td>
        <td className="num p-2">{row.atr_pct === null ? "—" : `${row.atr_pct.toFixed(2)}%`}</td>
        <td className="num p-2">{number(values["rsi14"])}</td>
        <td className="num p-2">
          {row.return_pct === null ? (
            <span className="text-muted-foreground">
              {row.outcome_status === "pending" ? "not yet known" : humanizeReason(row.outcome_status).short.toLowerCase()}
            </span>
          ) : (
            <span className={row.return_pct > 0 ? "text-bull" : row.return_pct < 0 ? "text-bear" : ""}>
              {row.return_pct > 0 ? "▲ +" : row.return_pct < 0 ? "▼ " : ""}
              {number(row.return_pct)}%
            </span>
          )}
        </td>
        <td className="p-2 text-right">
          <Button
            variant="ghost"
            size="icon"
            aria-expanded={open}
            aria-label={open ? "Hide details" : "Show details"}
            title={open ? "Hide details" : "Show details"}
            onClick={() => setOpen(!open)}
          >
            <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
          </Button>
        </td>
      </tr>
      {open ? (
        <tr className="border-b border-border/50 bg-background/30">
          <td colSpan={9} className="p-3">
            <div className="grid gap-4 md:grid-cols-[1fr_2fr]">
              <dl className="space-y-1">
                <Detail name="EMA 20 / 50 / 200">
                  {number(values["ema20"])} / {number(values["ema50"])} / {number(values["ema200"])}
                </Detail>
                <Detail name="ATR 14 (price units)">{number(values["atr14"])}</Detail>
                <Detail name="Volume vs previous 20">
                  {values["volume_change_pct"] === null || values["volume_change_pct"] === undefined
                    ? "—"
                    : `${number(values["volume_change_pct"])}%`}
                </Detail>
                <Detail name="Candle patterns">
                  {row.patterns.map((p) => humanizeToken(p)).join(", ") || "None"}
                </Detail>
                <Detail name="Score parts">
                  {Object.entries(factors)
                    .map(([name, raw]) => {
                      const value =
                        raw && typeof raw === "object" && !Array.isArray(raw)
                          ? (raw as Record<string, unknown>)
                          : {};
                      const points = typeof value["contribution"] === "number" ? value["contribution"] : null;
                      return `${humanizeToken(name)} ${signed(points)}`;
                    })
                    .join(" · ") || "—"}
                </Detail>
                <p className="pt-1 text-muted-foreground">
                  The score uses the 20/50 averages, RSI, patterns and volume only. MACD, EMA 200, bands, ADX and
                  range are context and add no points.
                </p>
                <p className="text-muted-foreground">
                  {sourceLabel(row.source)} · {row.source_native_symbol} · {row.version} · {row.strategy_version}
                </p>
              </dl>
              <details>
                <summary className="font-medium">Indicator explanations</summary>
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
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

function Detail({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap justify-between gap-x-3">
      <dt className="text-muted-foreground">{name}</dt>
      <dd className="num text-right">{children}</dd>
    </div>
  );
}
