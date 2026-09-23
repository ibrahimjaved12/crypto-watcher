import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { runPythonAnalysis } from "@/lib/analysis.functions";
import type { PythonAnalysis } from "@/lib/analysis.contract";
import { WINDOW_LABELS } from "@/lib/market/symbols";

const explanations: Record<PythonAnalysis["baseline"]["status"], string> = {
  eligible: "Eligible at this snapshot. Only the monitor can save an alert.",
  cooldown: "Threshold reached, but this direction is on cooldown.",
  below_threshold: "The net move from the baseline is below your threshold.",
  already_processed: "The monitor has already processed this candle (or a newer one).",
  disabled: "Monitoring is paused; this observation is not eligible for an alert.",
  baseline_required: "No saved baseline yet. The next successful monitor check can establish one.",
  baseline_reset_required:
    "The source or threshold changed. The monitor must establish a new baseline before comparing.",
  unavailable: "Fresh completed market data is unavailable; eligibility was not evaluated.",
  invalid_state: "The saved baseline has inconsistent timestamps; eligibility was not evaluated.",
};
const percent = (value: string | null) => (value === null ? "—" : `${Number(value).toFixed(4)}%`);
const at = (value: number | null) => (value === null ? "—" : new Date(value).toLocaleString());
const duration = (ms: number) => (ms < 1_000 ? `${ms} ms` : `${(ms / 1_000).toFixed(1)}s`);
const bytes = (value: number) =>
  value < 1_024 ? `${value} B` : `${(value / 1_024).toFixed(1)} KB`;
const humanize = (value: string) => value.replaceAll("_", " ");
const title = (value: string) => `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
const frame = (value: string) => (value === "15" ? "15m" : `${Number(value) / 60}h`);
const signed = (value: number | null) =>
  value === null ? "—" : value > 0 ? `+${value}` : String(value);
const sourceLabels: Record<string, string> = {
  "binance-usdm": "Binance USDⓈ-M",
  "kraken-futures": "Kraken Futures",
  "okx-usdt-swap": "OKX USDT Swap",
};
const factorLabels: Record<string, string> = {
  trend: "Trend",
  momentum: "Momentum",
  patterns: "Patterns",
  volume: "Volume",
};
const classificationTone: Record<string, string> = {
  bullish: "border-emerald-500/40 bg-emerald-500/10 text-emerald-400",
  bearish: "border-rose-500/40 bg-rose-500/10 text-rose-400",
  neutral: "border-border bg-muted text-muted-foreground",
  unavailable: "border-amber-500/40 bg-amber-500/10 text-amber-400",
};

export function PythonAnalysisPanel({ symbols }: { symbols: string[] }) {
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
  return (
    <section
      className="panel mt-5 space-y-3 p-4"
      aria-label="Python analysis"
      aria-busy={analysis.isPending}
    >
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="font-semibold">Python analysis</h2>
        <label className="sr-only" htmlFor="analysis-pair">
          Pair to analyze
        </label>
        <select
          id="analysis-pair"
          className="rounded-md border border-border bg-background p-2 text-sm"
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
              {pair}
            </option>
          ))}
        </select>
        <Button
          variant="secondary"
          disabled={!symbol || analysis.isPending}
          onClick={() => analysis.mutate(symbol)}
        >
          {analysis.isPending ? "Analyzing…" : "Run Python analysis"}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Read-only snapshot using completed candles. This action does not save alerts or reset
        baselines.
      </p>
      {analysis.isPending && (
        <p role="status" className="text-sm">
          Fetching candles and checking your saved baseline…
        </p>
      )}
      {analysis.isError && (
        <p role="alert" className="text-sm text-destructive">
          {analysis.error.message} Failure category:{" "}
          {humanize(
            String((analysis.error as Error & { category?: string }).category ?? "unknown"),
          )}
          .
        </p>
      )}
      {result && (
        <div className="space-y-4" aria-live="polite">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-muted/20 p-3">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-lg font-semibold">{result.symbol}</span>
                <Badge variant={result.status === "ok" ? "secondary" : "outline"}>
                  {title(result.status)}
                </Badge>
                {result.source && (
                  <Badge variant="outline">{sourceLabels[result.source] ?? result.source}</Badge>
                )}
              </div>
              <p className="mt-1 text-sm">
                {result.price === null ? "Price unavailable" : `${result.price} USDT`}
              </p>
              <p className="text-xs text-muted-foreground">Analyzed {at(result.as_of_ms)}</p>
            </div>
            <div className="text-right text-xs text-muted-foreground">
              <p>
                Source freshness:{" "}
                {result.observed_at_ms === null
                  ? "unavailable"
                  : duration(Math.max(0, result.as_of_ms - result.observed_at_ms))}
              </p>
              {metrics && <p>Request duration: {duration(metrics.duration_ms)}</p>}
            </div>
          </div>
          {result.failure_category && (
            <p className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-300">
              Failure category: {humanize(result.failure_category)}.
            </p>
          )}
          <div>
            <h3 className="text-sm font-medium">Completed-candle technical analysis</h3>
            <div className="mt-2 grid gap-3 md:grid-cols-3">
              {Object.entries(result.technical).map(([timeframe, row]) => (
                <div key={timeframe} className="rounded-md border border-border p-3 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-semibold">{frame(timeframe)}</span>
                    <Badge variant="outline" className={classificationTone[row.classification]}>
                      {title(row.classification)}
                    </Badge>
                  </div>
                  <p className="mt-2 text-sm font-medium">Rule score: {signed(row.score)}</p>
                  {row.factor_breakdown && (
                    <dl className="mt-2 divide-y divide-border/60">
                      {Object.entries(row.factor_breakdown).map(([name, factor]) => (
                        <div key={name} className="grid grid-cols-[1fr_auto_auto] gap-2 py-1">
                          <dt className="text-muted-foreground">{factorLabels[name] ?? name}</dt>
                          <dd>{title(humanize(factor.classification))}</dd>
                          <dd className="num w-6 text-right">{signed(factor.contribution)}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                  {row.reason && (
                    <p className="mt-2 text-amber-400">{title(humanize(row.reason))}</p>
                  )}
                  <p className="mt-2 text-muted-foreground">
                    Candle closed {at(row.candle_close_time_ms)}
                  </p>
                </div>
              ))}
              {!Object.keys(result.technical).length && (
                <p className="text-sm">Technical analysis unavailable.</p>
              )}
            </div>
            <p className="mt-2 text-xs text-muted-foreground">
              Rule-based score, not a calibrated win probability.
            </p>
          </div>
          <div>
            <h3 className="text-sm font-medium">
              Rolling windows — threshold {result.threshold_pct}%
            </h3>
            <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
              {Object.entries(result.rolling).map(([window, row]) => (
                <div key={window} className="rounded-md border border-border p-2 text-xs">
                  <p className="font-medium">
                    {WINDOW_LABELS[Number(window)]} · {percent(row.change_pct)}
                  </p>
                  <p className="text-muted-foreground">
                    {row.status === "ok"
                      ? row.threshold_met
                        ? "Threshold reached"
                        : "Below threshold"
                      : row.reason && title(humanize(row.reason))}
                  </p>
                  {row.status === "ok" && (
                    <p className="text-muted-foreground">
                      {at(row.start_close_ms)} → {at(row.end_close_ms)}
                    </p>
                  )}
                </div>
              ))}
              {!Object.keys(result.rolling).length && (
                <p className="text-sm">Rolling data unavailable.</p>
              )}
            </div>
          </div>
          <div className="space-y-1 rounded-md border border-border bg-muted/10 p-3 text-sm">
            <h3 className="font-medium">Saved-baseline comparison</h3>
            <p>{explanations[result.baseline.status]}</p>
            {result.baseline.baseline_price !== null && (
              <p className="num text-xs">
                Baseline: {result.baseline.baseline_price} USDT at{" "}
                {at(result.baseline.baseline_at_ms)} · net change{" "}
                {percent(result.baseline.change_pct)}
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              Directional cooldown:{" "}
              {result.baseline.cooldown_evaluated ? "evaluated" : "not evaluated"}.
              {result.baseline.cooldown_until_ms !== null &&
                ` Relevant cooldown ends ${at(result.baseline.cooldown_until_ms)}.`}{" "}
              Alert eligibility:{" "}
              {result.baseline.eligibility_evaluated
                ? result.baseline.alert_eligible
                  ? "eligible"
                  : "not eligible"
                : "not evaluated"}
              .
            </p>
            <p className="text-xs text-muted-foreground">
              The monitor may update settings or state after this snapshot; this is not a reserved
              alert.
            </p>
          </div>
          <details className="rounded-md border border-border p-3 text-xs text-muted-foreground">
            <summary className="cursor-pointer font-medium text-foreground">
              Technical details
            </summary>
            <div className="mt-3 space-y-2 break-words">
              <p>
                Requested instrument: <code>{result.instrument.id}</code>
              </p>
              <p>
                Source contract:{" "}
                <code>{result.source_instrument?.instrument_id ?? "unavailable"}</code>
              </p>
              <p>
                Endpoint: <code>{result.endpoint ?? "unavailable"}</code> · price type:{" "}
                <code>{result.price_type}</code> · retrieved {at(result.retrieved_at_ms)}
              </p>
              {metrics && (
                <p>
                  Payload: {bytes(metrics.request_bytes)} sent / {bytes(metrics.response_bytes)}{" "}
                  received
                </p>
              )}
              {Object.entries(result.technical).map(([timeframe, row]) => (
                <div key={timeframe}>
                  <p className="font-medium text-foreground">{frame(timeframe)}</p>
                  <p>
                    Source event {at(row.source_event_time_ms)} · {row.ta_version} ·{" "}
                    {row.strategy_version}
                  </p>
                  <p>
                    Reasons: {row.reason ?? (row.reasons.length ? row.reasons.join(", ") : "none")}
                  </p>
                  {row.factor_breakdown && (
                    <p>
                      Factor reasons:{" "}
                      {Object.entries(row.factor_breakdown)
                        .map(([name, factor]) => `${name}=${factor.reason}`)
                        .join(", ")}
                    </p>
                  )}
                </div>
              ))}
              {!!result.attempts.length && (
                <p>
                  Provider attempts:{" "}
                  {result.attempts
                    .map((attempt) => `${attempt.source}: ${humanize(attempt.reason)}`)
                    .join("; ")}
                </p>
              )}
            </div>
          </details>
        </div>
      )}
    </section>
  );
}
