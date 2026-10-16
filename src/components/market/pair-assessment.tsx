import { useId, useState } from "react";
import { ArrowDownRight, ArrowRight, ArrowUpRight, ChevronDown, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import {
  formatAge,
  pairMovementChip,
  taPillText,
  type FreshnessGrade,
  type PairMovementView,
  type TaFrameView,
} from "@/lib/market/assessment";

const toneClass = { up: "text-bull", down: "text-bear", flat: "text-muted-foreground", unknown: "text-muted-foreground" } as const;

export type TaFrameState = { frame: TaFrameView; grade: FreshnessGrade };

/**
 * Per-pair "now" summary under a quote card (#30): the movement chip and three TA pills are always visible;
 * "Details" opens the rest (z-scores, volume, score, RSI, trend, volatility, patterns, snapshot age). Closed by
 * default on every card. A lag never shows red: "Updating…" while catching up, and a muted "last updated" line
 * only after a self-heal check did not clear it.
 */
export function PairAssessment({
  movement,
  movementCurrent,
  ta,
  healAttempted,
  now,
}: {
  movement: PairMovementView;
  /** False when the market reading is not fresh (machine offline, late, failed): the chip is shown muted. */
  movementCurrent: boolean;
  ta: TaFrameState[];
  healAttempted: boolean;
  now: number;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const chip = pairMovementChip(movement);
  const ChipIcon = chip.tone === "up" ? ArrowUpRight : chip.tone === "down" ? ArrowDownRight : ArrowRight;
  const lagging = ta.filter((t) => t.grade !== "fresh" && (t.frame.status === "ok" || t.frame.status === "stale"));
  const behind = lagging.filter((t) => t.grade === "stale");
  const oldest = behind.reduce<number | null>((min, t) => {
    const at = t.frame.savedAt ?? t.frame.candleClosedAt;
    return at === null ? min : min === null ? at : Math.min(min, at);
  }, null);

  return (
    <div className="flex flex-col gap-3 border-t border-border pt-4" aria-label="Current assessment">
      <Badge
        variant="outline"
        className={cn(
          "w-fit gap-1 px-2 py-1 text-xs font-medium",
          movementCurrent ? toneClass[chip.tone] : "text-muted-foreground",
        )}
      >
        <ChipIcon className="size-3.5" aria-hidden />
        {chip.text}
      </Badge>

      <ul className="grid grid-cols-3 gap-2" aria-label="Technical analysis by timeframe">
        {ta.map(({ frame, grade }) => {
          const Icon = frame.lean === "bullish" ? ArrowUpRight : frame.lean === "bearish" ? ArrowDownRight : ArrowRight;
          const readable = frame.status === "ok" || frame.status === "stale";
          const current = readable && grade === "fresh";
          return (
            <li
              key={frame.timeframe}
              className={cn(
                "flex items-center justify-center gap-1 rounded-full bg-secondary/40 px-2 py-1 text-xs",
                current
                  ? frame.lean === "bullish"
                    ? "text-bull"
                    : frame.lean === "bearish"
                      ? "text-bear"
                      : "text-foreground"
                  : "text-muted-foreground",
              )}
              title={frame.note}
            >
              <span className="num">{frame.label}</span>
              {readable ? <Icon className="size-3.5 shrink-0" aria-hidden /> : null}
              <span className="truncate">{taPillText(frame)}</span>
            </li>
          );
        })}
      </ul>

      {lagging.length ? (
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground" role="status">
          {behind.length && healAttempted && oldest !== null ? (
            <>Technical analysis last updated {formatAge(Math.max(0, now - oldest))} ago</>
          ) : (
            <>
              <Loader2 className="size-3 animate-spin motion-reduce:animate-none" aria-hidden />
              Updating…
            </>
          )}
        </p>
      ) : null}

      <button
        type="button"
        className="-mx-1 inline-flex min-h-11 w-fit items-center gap-1 rounded-md px-1 text-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
      >
        Details
        <ChevronDown
          className={cn("size-4 transition-transform duration-150 motion-reduce:transition-none", open && "rotate-180")}
          aria-hidden
        />
      </button>
      <div
        id={panelId}
        className={cn(
          "grid transition-[grid-template-rows] duration-150 ease-out motion-reduce:transition-none",
          open ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
        )}
        inert={!open}
      >
        <div className={cn("min-h-0 overflow-hidden", !open && "invisible")}>
          <Details movement={movement} ta={ta} now={now} />
        </div>
      </div>
    </div>
  );
}

function Details({ movement, ta, now }: { movement: PairMovementView; ta: TaFrameState[]; now: number }) {
  return (
    <div className="flex flex-col gap-4 pb-1 text-sm">
      <section>
        <h4 className="text-xs font-medium text-muted-foreground">How unusual is the move</h4>
        {movement.kind === "unavailable" ? (
          <p className="mt-1 text-muted-foreground">{movement.reason}</p>
        ) : movement.kind === "excluded" ? (
          <p className="mt-1 text-muted-foreground">Left out of the market reading: {movement.reasons.join(", ").toLowerCase()}.</p>
        ) : (
          <>
            <dl className="mt-1 grid grid-cols-3 gap-2">
              {movement.windows.map((w) => (
                <div key={w.minutes} title={w.reason ?? undefined}>
                  <dt className="text-[11px] uppercase tracking-wider text-muted-foreground">{w.minutes}m</dt>
                  <dd className={cn("num", toneClass[w.tone])}>{w.zText}</dd>
                  <dd className="text-xs text-muted-foreground">{w.label}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-2 text-xs text-muted-foreground">
              Volume: {movement.rvol ? movement.rvol.text : `unavailable (${movement.rvolReason ?? "no data"})`}
            </p>
            {movement.outlier ? <p className="mt-1 text-xs text-muted-foreground">{movement.outlier.text}.</p> : null}
            {movement.partialReasons.length ? (
              <p className="mt-1 text-xs text-muted-foreground">Some windows unavailable: {movement.partialReasons.join(", ")}.</p>
            ) : null}
          </>
        )}
      </section>

      <section>
        <h4 className="text-xs font-medium text-muted-foreground">Technical analysis (completed candles, not a forecast)</h4>
        <ul className="mt-1 flex flex-col gap-2">
          {ta.map(({ frame: f, grade }) => {
            const readable = f.status === "ok" || f.status === "stale";
            const at = f.savedAt ?? f.candleClosedAt;
            return (
              <li key={f.timeframe} className={cn(grade !== "fresh" && "text-muted-foreground")}>
                <span className="num font-medium">{f.label}</span>{" "}
                {readable ? (
                  <span className="num text-xs">
                    {f.score === null ? "score —" : `score ${f.score > 0 ? "+" : ""}${f.score}`}
                    {" · "}RSI {f.rsi === null ? "—" : f.rsi.toFixed(1)}
                    {f.trend ? ` · ${f.trend} trend` : ""}
                    {f.atrPct === null ? "" : ` · volatility ${f.atrPct.toFixed(2)}%`}
                    {f.patterns.length ? ` · ${f.patterns.join(", ")}` : ""}
                    {grade !== "fresh" && at !== null ? ` · as of ${formatAge(Math.max(0, now - at))} ago` : ""}
                  </span>
                ) : (
                  <span className="text-xs text-muted-foreground">{f.note}</span>
                )}
                {readable ? <p className="text-xs text-muted-foreground">{f.note}</p> : null}
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}
