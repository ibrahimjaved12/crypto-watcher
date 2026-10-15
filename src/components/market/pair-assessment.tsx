import { useEffect, useState } from "react";
import { ArrowDownRight, ArrowUpRight, ChevronRight, Minus } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Hint } from "@/components/hint";
import { cn } from "@/lib/utils";
import {
  PAIR_COLLAPSED_KEY,
  readStoredFlag,
  summarizePair,
  writeStoredFlag,
  type PairMovementView,
  type TaFrameView,
} from "@/lib/market/assessment";

const toneClass = { up: "text-bull", down: "text-bear", flat: "text-muted-foreground", unknown: "text-muted-foreground" } as const;

/**
 * Per-pair "now" block under a quote card (#30): unusualness of the move, volume, outlier, and TA by timeframe.
 * Collapsible to a one-line summary (default expanded); the choice is remembered per viewer and per pair in
 * localStorage, and the block works when storage is unavailable.
 */
export function PairAssessment({
  symbol,
  movement,
  ta,
  now,
}: {
  symbol: string;
  movement: PairMovementView;
  ta: TaFrameView[];
  now: number;
}) {
  const storageKey = `${PAIR_COLLAPSED_KEY}:${symbol}`;
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    setCollapsed(readStoredFlag(() => window.localStorage, storageKey, false));
  }, [storageKey]);
  const panelId = `pair-assessment-${symbol}`;
  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    writeStoredFlag(() => window.localStorage, storageKey, next);
  };
  return (
    <div className="border-t border-border pt-2 text-xs" aria-label="Current assessment">
      <button
        type="button"
        className="flex w-full items-start gap-1.5 rounded-sm text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-expanded={!collapsed}
        aria-controls={panelId}
        onClick={toggle}
      >
        <ChevronRight className={cn("mt-0.5 size-3.5 shrink-0 transition-transform", !collapsed && "rotate-90")} aria-hidden />
        <span className="min-w-0">
          <span className="font-medium">Current assessment</span>
          {collapsed ? <span className="block text-muted-foreground">{summarizePair(movement, ta, now)}</span> : null}
        </span>
      </button>
      {collapsed ? null : (
        <div id={panelId} className="mt-2">
          {movement.kind === "ok" && movement.notLive ? (
            <p className="mb-1 text-warn" role="status">
              {movement.notLive}
            </p>
          ) : null}
          <AssessmentBody movement={movement} ta={ta} />
        </div>
      )}
    </div>
  );
}

function AssessmentBody({ movement, ta }: { movement: PairMovementView; ta: TaFrameView[] }) {
  return (
    <div>
      <p className="mb-1 font-medium">
        <Hint text="How unusual each recent move is for this pair, compared with its own normal moves over the past 7 days (a robust z-score). 0 is normal; the engine calls |z| ≥ 1 a material move.">
          How unusual is the move?
        </Hint>
      </p>
      {movement.kind === "unavailable" ? (
        <p className="text-muted-foreground">{movement.reason}</p>
      ) : movement.kind === "excluded" ? (
        <p className="text-muted-foreground">Left out of the market engine: {movement.reasons.join(", ").toLowerCase()}.</p>
      ) : (
        <>
          <ul className="grid grid-cols-3 gap-1 text-center">
            {movement.windows.map((w) => (
              <li key={w.minutes} className="rounded-md bg-secondary/60 px-1 py-1.5" title={w.reason ?? undefined}>
                <div className="text-[10px] uppercase text-muted-foreground">{w.minutes}m</div>
                <div className={cn("num font-semibold", toneClass[w.tone])}>{w.zText}</div>
                <div className="text-[10px] text-muted-foreground">{w.label}</div>
              </li>
            ))}
          </ul>
          <p className="mt-1 text-muted-foreground">
            <Hint text="Trading volume of the latest 5 minutes compared with the usual volume for the same window over recent history.">
              Volume
            </Hint>
            : {movement.rvol ? movement.rvol.text : `unavailable (${movement.rvolReason ?? "no data"})`}
          </p>
          {movement.outlier ? (
            <Badge variant="outline" className="mt-1 border-warn/60 text-warn" title={movement.outlier.text}>
              {movement.outlier.direction === "rising" ? "Isolated riser" : "Isolated faller"} · {movement.outlier.window}m
            </Badge>
          ) : null}
          {movement.partialReasons.length ? (
            <p className="mt-1 text-muted-foreground">Some windows unavailable: {movement.partialReasons.join(", ")}.</p>
          ) : null}
        </>
      )}

      <p className="mb-1 mt-3 font-medium">
        <Hint text="Saved indicator snapshots of completed candles only. A description of the chart, not a forecast.">
          Technical analysis
        </Hint>
      </p>
      <ul className="grid gap-1">
        {ta.map((f) => {
          const Icon = f.lean === "bullish" ? ArrowUpRight : f.lean === "bearish" ? ArrowDownRight : Minus;
          return (
            <li key={f.timeframe} className="rounded-md border border-border p-2">
              <div className="flex flex-wrap items-center gap-x-2">
                <span className="font-medium">{f.label}</span>
                {f.status === "ok" || f.status === "stale" ? (
                  <span className={cn("inline-flex items-center gap-1", f.lean === "bullish" ? "text-bull" : f.lean === "bearish" ? "text-bear" : "text-muted-foreground")}>
                    <Icon className="size-3.5" aria-hidden />
                    {f.leanText}
                  </span>
                ) : (
                  <span className="text-muted-foreground">
                    {f.status === "error" ? "Could not load" : f.status === "not_loaded" ? "Not loaded" : "No snapshot"}
                  </span>
                )}
                {f.status === "stale" ? <Badge variant="outline" className="border-warn/60 text-warn">Stale</Badge> : null}
              </div>
              {f.status === "ok" || f.status === "stale" ? (
                <p className="num text-muted-foreground">
                  {f.score === null ? "score —" : `score ${f.score > 0 ? "+" : ""}${f.score}`}
                  {" · "}RSI {f.rsi === null ? "—" : f.rsi.toFixed(1)}
                  {f.trend ? ` · ${f.trend} trend` : ""}
                  {f.atrPct === null ? "" : ` · volatility ${f.atrPct.toFixed(2)}%`}
                  {f.patterns.length ? ` · ${f.patterns.join(", ")}` : ""}
                </p>
              ) : null}
              <p className={cn(f.status === "stale" ? "text-warn" : "text-muted-foreground")}>{f.note}</p>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
