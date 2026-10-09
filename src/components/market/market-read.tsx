import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { ArrowDownRight, ArrowUpRight, Compass, Minus, ScanSearch } from "lucide-react";

import { Hint } from "@/components/hint";
import { EmptyState, SectionTitle, SignedBar, StatusChip } from "@/components/plain";
import { Button } from "@/components/ui/button";
import { runPythonAnalysis } from "@/lib/analysis.functions";
import type { PythonAnalysis } from "@/lib/analysis.contract";
import { humanizeReason, humanizeToken, sourceLabel } from "@/lib/labels";
import { WINDOW_LABELS, baseAsset } from "@/lib/market/symbols";
import { cn } from "@/lib/utils";

const explanations: Record<PythonAnalysis["baseline"]["status"], string> = {
  eligible: "Your alert condition is met at this moment. Only the scheduled monitor can save the alert.",
  cooldown: "The move is big enough, but this direction is on cooldown after a recent alert.",
  below_threshold: "The move since the saved starting price is smaller than your alert threshold.",
  already_processed: "The monitor has already checked this candle (or a newer one).",
  disabled: "Monitoring is paused, so this move cannot trigger an alert.",
  baseline_required: "No starting price saved yet. The next successful monitor check sets one.",
  baseline_reset_required:
    "The data source or threshold changed, so the monitor must save a new starting price before comparing.",
  unavailable: "Fresh completed market data is unavailable, so the alert rule was not checked.",
  invalid_state: "The saved starting price has inconsistent timestamps, so the alert rule was not checked.",
};
const percent = (value: string | null, digits = 2) => {
  if (value === null) return "—";
  const n = Number(value);
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(digits)}%`;
};
const at = (value: number | null) => (value === null ? "—" : new Date(value).toLocaleString());
const duration = (ms: number) => (ms < 1_000 ? `${ms} ms` : `${(ms / 1_000).toFixed(1)} s`);
const bytes = (value: number) =>
  value < 1_024 ? `${value} B` : `${(value / 1_024).toFixed(1)} KB`;
const frame = (value: string) => (value === "15" ? "15m" : `${Number(value) / 60}h`);
const factorLabels: Record<string, string> = {
  trend: "Trend",
  momentum: "Momentum",
  patterns: "Candle patterns",
  volume: "Volume",
};

type Technical = NonNullable<PythonAnalysis["technical"][keyof PythonAnalysis["technical"]]>;

function lean(row: Technical): { word: string; tone: string; Icon: typeof Minus } {
  if (row.classification === "bullish")
    return { word: "Bullish lean", tone: "text-bull", Icon: ArrowUpRight };
  if (row.classification === "bearish")
    return { word: "Bearish lean", tone: "text-bear", Icon: ArrowDownRight };
  if (row.classification === "neutral")
    return { word: "No clear lean", tone: "text-muted-foreground", Icon: Minus };
  return { word: humanizeReason(row.reason ?? row.status).short, tone: "text-warn", Icon: Minus };
}

/** "15m: bearish lean, score −40 of ±100, volatility 0.40%" */
function verdict(timeframe: string, row: Technical): string {
  const { word } = lean(row);
  const parts = [`${frame(timeframe)}: ${word.toLowerCase()}`];
  if (row.score !== null) parts.push(`score ${row.score > 0 ? "+" : row.score < 0 ? "−" : ""}${Math.abs(row.score)} of ±100`);
  if (row.atr_pct !== null) parts.push(`volatility ${row.atr_pct.toFixed(2)}%`);
  return parts.join(", ");
}

export function MarketRead({ symbols }: { symbols: string[] }) {
  const [selected, setSelected] = useState("");
  const symbol = symbols.includes(selected) ? selected : (symbols[0] ?? "");
  const call = useServerFn(runPythonAnalysis);
  const analysis = useMutation({
    mutationFn: async (pair: string) => {
      const reply = await call({ data: { symbol: pair } });
      if (!reply.ok) {
        throw Object.assign(new Error(reply.error), { category: reply.category });
      }
      return { result: reply.analysis, metrics: reply.metrics };
    },
  });
  const result =
    !analysis.isPending && !analysis.isError && analysis.data?.result.symbol === symbol
      ? analysis.data.result
      : undefined;
  const metrics = result ? analysis.data?.metrics : undefined;
  const technical = result ? Object.entries(result.technical) : [];
  return (
    <section className="panel mt-6 p-4 sm:p-5" aria-label="Market read" aria-busy={analysis.isPending}>
      <SectionTitle
        title="Market read"
        hint="A read-only snapshot from completed candles. It never saves alerts or changes your saved starting prices."
        right={
          <>
            <label className="sr-only" htmlFor="analysis-pair">
              Pair to analyse
            </label>
            <select
              id="analysis-pair"
              className="h-9 rounded-md border border-input bg-background px-2 text-sm"
              value={symbol}
              disabled={analysis.isPending || !symbols.length}
              onChange={(e) => {
                setSelected(e.target.value);
                analysis.reset();
              }}
            >
              {!symbols.length && <option value="">No watched pairs</option>}
              {symbols.map((pair) => (
                <option key={pair} value={pair}>
                  {baseAsset(pair)}/USDT
                </option>
              ))}
            </select>
            <Button disabled={!symbol || analysis.isPending} onClick={() => analysis.mutate(symbol)}>
              <ScanSearch className="size-4" aria-hidden />
              {analysis.isPending ? "Analysing…" : `Analyse ${symbol ? baseAsset(symbol) : "pair"}`}
            </Button>
          </>
        }
      />
      {analysis.isPending && (
        <p role="status" className="py-4 text-sm text-muted-foreground">
          Reading completed candles and checking your alert rule…
        </p>
      )}
      {analysis.isError && (
        <div role="alert" className="space-y-1 py-2 text-sm">
          <StatusChip level="problem">
            {humanizeReason(
              String((analysis.error as Error & { category?: string }).category ?? "unknown"),
            ).short}
          </StatusChip>
          <details className="text-xs text-muted-foreground">
            <summary>Technical details</summary>
            <p className="mt-1 break-words">{analysis.error.message}</p>
          </details>
        </div>
      )}
      {!result && !analysis.isPending && !analysis.isError && (
        <EmptyState
          icon={Compass}
          title={symbol ? `Press “Analyse ${baseAsset(symbol)}” for a read` : "Add a pair to get a market read"}
          body="You get a one-line verdict per timeframe (15m, 1h, 4h), the recent moves against your alert threshold and whether an alert would fire."
          className="py-6"
        />
      )}
      {result && (
        <div className="space-y-5" aria-live="polite">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="font-display text-lg font-semibold">{baseAsset(result.symbol)}/USDT</span>
            <span className="num text-lg">
              {result.price === null ? "Price unavailable" : `${Number(result.price).toLocaleString()} USDT`}
            </span>
            <span className="text-xs text-muted-foreground">
              as of {at(result.as_of_ms)}
              {result.source ? ` · ${sourceLabel(result.source)}` : ""}
            </span>
            {result.status !== "ok" ? (
              <StatusChip level={result.status === "partial" ? "wait" : "problem"}>
                {result.failure_category
                  ? humanizeReason(result.failure_category).short
                  : humanizeReason(result.status).short}
              </StatusChip>
            ) : null}
          </div>

          <div>
            <h3 className="text-sm font-medium">
              Indicator verdict{" "}
              <span className="font-normal text-muted-foreground">
                (<Hint term="score">score −100 bearish … +100 bullish</Hint>; a ranking, not a win probability)
              </span>
            </h3>
            <div className="mt-2 grid gap-3 sm:grid-cols-3">
              {technical.map(([timeframe, row]) => {
                const { word, tone, Icon } = lean(row);
                return (
                  <div key={timeframe} className="rounded-lg border border-border/70 bg-background/30 p-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="num text-xs text-muted-foreground">{frame(timeframe)}</span>
                      <SignedBar value={row.score} label={`Score ${row.score ?? "unavailable"}`} />
                    </div>
                    <p className={cn("mt-1 flex items-center gap-1 font-semibold", tone)}>
                      <Icon className="size-4" aria-hidden />
                      {word}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">{verdict(timeframe, row)}</p>
                  </div>
                );
              })}
              {!technical.length && (
                <p className="text-sm text-muted-foreground">No indicator verdict available for this snapshot.</p>
              )}
            </div>
          </div>

          <div>
            <h3 className="text-sm font-medium">
              Recent moves{" "}
              <span className="font-normal text-muted-foreground">vs your {result.threshold_pct}% alert threshold</span>
            </h3>
            <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-5">
              {Object.entries(result.rolling).map(([window, row]) => {
                const value = row.change_pct === null ? null : Number(row.change_pct);
                return (
                  <div key={window} className="rounded-md border border-border/70 p-2 text-xs">
                    <p className="text-muted-foreground">{WINDOW_LABELS[Number(window)]}</p>
                    <p
                      className={cn(
                        "num text-sm font-semibold",
                        value === null ? "" : value > 0 ? "text-bull" : value < 0 ? "text-bear" : "",
                      )}
                    >
                      {percent(row.change_pct)}
                    </p>
                    <p className="text-muted-foreground">
                      {row.status === "ok"
                        ? row.threshold_met
                          ? "Threshold reached"
                          : "Below threshold"
                        : humanizeReason(row.reason ?? row.status).short}
                    </p>
                  </div>
                );
              })}
              {!Object.keys(result.rolling).length && (
                <p className="col-span-full text-sm text-muted-foreground">Recent moves are unavailable.</p>
              )}
            </div>
          </div>

          <div className="rounded-lg border border-border/70 bg-background/30 p-3 text-sm">
            <h3 className="font-medium">Would an alert fire?</h3>
            <p className="mt-1 text-muted-foreground">{explanations[result.baseline.status]}</p>
            {result.baseline.baseline_price !== null && (
              <p className="num mt-1 text-xs">
                From {Number(result.baseline.baseline_price).toLocaleString()} USDT ({at(result.baseline.baseline_at_ms)}):{" "}
                {percent(result.baseline.change_pct)}
              </p>
            )}
          </div>

          <details className="rounded-md border border-border/70 p-3 text-xs text-muted-foreground">
            <summary className="font-medium text-foreground">Technical details</summary>
            <div className="mt-3 space-y-3 break-words">
              <p>
                Calculated by the Python analysis service · request {metrics ? duration(metrics.duration_ms) : "—"} ·
                data age{" "}
                {result.observed_at_ms === null
                  ? "unavailable"
                  : duration(Math.max(0, result.as_of_ms - result.observed_at_ms))}
              </p>
              {technical.map(([timeframe, row]) => (
                <div key={timeframe}>
                  <p className="font-medium text-foreground">
                    {frame(timeframe)} · candle closed {at(row.candle_close_time_ms)}
                  </p>
                  {row.factor_breakdown && (
                    <dl className="mt-1 grid max-w-sm grid-cols-[1fr_auto_auto] gap-x-3">
                      {Object.entries(row.factor_breakdown).map(([name, factor]) => (
                        <div key={name} className="contents">
                          <dt>{factorLabels[name] ?? name}</dt>
                          <dd>{humanizeToken(factor.classification)}</dd>
                          <dd className="num text-right">
                            {factor.contribution === null
                              ? "—"
                              : factor.contribution > 0
                                ? `+${factor.contribution}`
                                : factor.contribution}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  )}
                  <p className="mt-1">
                    Source event {at(row.source_event_time_ms)} · {row.ta_version} · {row.strategy_version} ·
                    reasons: {row.reason ?? (row.reasons.length ? row.reasons.join(", ") : "none")}
                    {row.factor_breakdown
                      ? ` · factor reasons: ${Object.entries(row.factor_breakdown)
                          .map(([name, factor]) => `${name}=${factor.reason}`)
                          .join(", ")}`
                      : ""}
                  </p>
                  <p>
                    {row.provenance.candle_count} candles · {row.provenance.warmup_candle_count} warm-up ·{" "}
                    {row.provenance.missing_open_times_ms.length} missing open times
                  </p>
                </div>
              ))}
              {Object.entries(result.rolling).map(([window, row]) =>
                row.status === "ok" ? (
                  <p key={window}>
                    {WINDOW_LABELS[Number(window)]}: {at(row.start_close_ms)} → {at(row.end_close_ms)}
                  </p>
                ) : null,
              )}
              <p>
                Status <code>{result.status}</code>
                {result.failure_category ? (
                  <>
                    {" "}
                    · failure <code>{result.failure_category}</code>
                  </>
                ) : null}{" "}
                · baseline <code>{result.baseline.status}</code> · cooldown{" "}
                {result.baseline.cooldown_evaluated ? "evaluated" : "not evaluated"}
                {result.baseline.cooldown_until_ms !== null
                  ? ` (ends ${at(result.baseline.cooldown_until_ms)})`
                  : ""}{" "}
                · eligibility{" "}
                {result.baseline.eligibility_evaluated
                  ? result.baseline.alert_eligible
                    ? "eligible"
                    : "not eligible"
                  : "not evaluated"}
              </p>
              <p>
                Requested instrument <code>{result.instrument.id}</code> · source contract{" "}
                <code>{result.source_instrument?.instrument_id ?? "unavailable"}</code>
              </p>
              <p>
                Endpoint <code>{result.endpoint ?? "unavailable"}</code> · price type{" "}
                <code>{result.price_type}</code> · retrieved {at(result.retrieved_at_ms)}
              </p>
              {metrics && (
                <p>
                  Payload {bytes(metrics.request_bytes)} sent / {bytes(metrics.response_bytes)} received
                </p>
              )}
              {!!result.attempts.length && (
                <p>
                  Provider attempts:{" "}
                  {result.attempts
                    .map((attempt) => `${sourceLabel(attempt.source)}: ${humanizeReason(attempt.reason).short}`)
                    .join("; ")}
                </p>
              )}
              <p>
                The monitor may update settings or state after this snapshot; it does not reserve an alert.
              </p>
            </div>
          </details>
        </div>
      )}
    </section>
  );
}
