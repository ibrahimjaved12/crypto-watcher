import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
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
          {String((analysis.error as Error & { category?: string }).category ?? "unknown")}.
        </p>
      )}
      {result && (
        <div className="space-y-4" aria-live="polite">
          <p className="num text-xs text-muted-foreground">
            {result.symbol} · {result.source ?? "No source available"} · {result.status} · analyzed{" "}
            {at(result.as_of_ms)}
            {result.price !== null &&
              ` · ${result.price} USDT · candle closed ${at(result.observed_at_ms)}`}
          </p>
          <p className="num text-xs text-muted-foreground">
            Requested {result.instrument.id} · source contract{" "}
            {result.source_instrument?.instrument_id ?? "unavailable"} ·{" "}
            {result.endpoint ?? "no endpoint"} · {result.price_type} price · retrieved{" "}
            {at(result.retrieved_at_ms)}
            {result.observed_at_ms !== null &&
              ` · source age ${Math.max(0, result.as_of_ms - result.observed_at_ms)} ms`}
          </p>
          {metrics && (
            <p className="num text-xs text-muted-foreground">
              Request {metrics.duration_ms} ms · payload {metrics.request_bytes} B sent /{" "}
              {metrics.response_bytes} B received
            </p>
          )}
          {result.failure_category && (
            <p className="text-xs text-muted-foreground">
              Failure category: {result.failure_category.replaceAll("_", " ")}.
            </p>
          )}
          <div>
            <h3 className="text-sm font-medium">Completed-candle technical analysis</h3>
            <div className="mt-2 grid gap-3 md:grid-cols-3">
              {Object.entries(result.technical).map(([timeframe, row]) => (
                <div key={timeframe} className="rounded border border-border p-2 text-xs">
                  <p className="font-medium">
                    {Number(timeframe) === 15 ? "15m" : `${Number(timeframe) / 60}h`} · {row.status}
                  </p>
                  <p>
                    {row.classification} · score {row.score ?? "—"}
                  </p>
                  <p className="text-muted-foreground">
                    {row.reason?.replaceAll("_", " ") ?? row.reasons.join(", ")} · candle closed{" "}
                    {at(row.candle_close_time_ms)} · source event {at(row.source_event_time_ms)}
                  </p>
                  {row.factor_breakdown && (
                    <p className="text-muted-foreground">
                      {Object.entries(row.factor_breakdown)
                        .map(
                          ([name, factor]) =>
                            `${name}: ${factor.classification} (${factor.contribution ?? "—"}; ${factor.reason})`,
                        )
                        .join(" · ")}
                    </p>
                  )}
                  <p className="text-muted-foreground">
                    {row.ta_version} · {row.strategy_version}
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
            <div className="mt-2 flex flex-wrap gap-3">
              {Object.entries(result.rolling).map(([window, row]) => (
                <div key={window} className="rounded border border-border p-2 text-xs">
                  <p>
                    {WINDOW_LABELS[Number(window)]} · {percent(row.change_pct)}
                  </p>
                  <p className="text-muted-foreground">
                    {row.status === "ok"
                      ? row.threshold_met
                        ? "Threshold reached"
                        : "Below threshold"
                      : row.reason?.replaceAll("_", " ")}
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
          <div className="space-y-1 text-sm">
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
          {!!result.attempts.length && (
            <p className="text-xs text-muted-foreground">
              Provider attempts:{" "}
              {result.attempts
                .map((a) => `${a.source}: ${a.reason.replaceAll("_", " ")}`)
                .join("; ")}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
