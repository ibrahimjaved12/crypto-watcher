import { useId, useState } from "react";
import { ArrowDownRight, ArrowUpRight, ChevronDown, Info, Loader2, Minus } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import {
  formatAge,
  isWarmingReason,
  pairMovementChip,
  taPillText,
  type FreshnessGrade,
  type PairMovementView,
  type TaFrameView,
} from "@/lib/market/assessment";
import { slowUnusualWord, type slowPairRows } from "@/lib/market/slow-movement-view";
import { DivergingBar, Meter, NotReadyChip } from "./gauges";

export type SlowPairRow = ReturnType<typeof slowPairRows>[number];

const toneClass = { up: "text-bull", down: "text-bear", flat: "text-muted-foreground", unknown: "text-muted-foreground" } as const;
const leanClass = (lean: TaFrameView["lean"]) =>
  lean === "bullish" ? "text-bull" : lean === "bearish" ? "text-bear" : "text-foreground";
const LeanIcon = ({ lean, className }: { lean: TaFrameView["lean"]; className?: string }) => {
  const Icon = lean === "bullish" ? ArrowUpRight : lean === "bearish" ? ArrowDownRight : Minus;
  return <Icon className={cn("size-3.5 shrink-0", className)} aria-hidden />;
};
const readableFrame = (frame: TaFrameView) => frame.status === "ok" || frame.status === "stale";

export type TaFrameState = { frame: TaFrameView; grade: FreshnessGrade };

/**
 * Per-pair "now" summary under a quote card (#30), in two clearly separate groups:
 * "Moving now" (the live engine's 15-minute reading: one chip) and "Trend · by candle size" (technical analysis on
 * completed candles: a segmented row of 15m / 1h / 4h). Only a reading carries a direction symbol; warm-up is a
 * dashed hourglass chip and no reading is a dashed slash chip. "Details" (closed by default) opens the gauges.
 */
export function PairAssessment({
  movement,
  movementCurrent,
  ta,
  healAttempted,
  now,
  slowRows = [],
}: {
  movement: PairMovementView;
  /** False when the market reading is not fresh (machine offline, late, failed): the chip is shown muted. */
  movementCurrent: boolean;
  ta: TaFrameState[];
  healAttempted: boolean;
  now: number;
  /** "Longer moves" per horizon for this pair: return and z (z is shown only here, in Details). */
  slowRows?: SlowPairRow[];
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const chip = pairMovementChip(movement);
  const lagging = ta.filter((t) => t.grade !== "fresh" && readableFrame(t.frame));
  const behind = lagging.filter((t) => t.grade === "stale");
  const oldest = behind.reduce<number | null>((min, t) => {
    const at = t.frame.savedAt ?? t.frame.candleClosedAt;
    return at === null ? min : min === null ? at : Math.min(min, at);
  }, null);
  const ChipIcon = chip.tone === "up" ? ArrowUpRight : chip.tone === "down" ? ArrowDownRight : Minus;

  return (
    <div className="flex flex-col gap-4 border-t border-border pt-4" aria-label="Current assessment">
      <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:justify-between">
        <h4 className="text-xs font-medium text-muted-foreground">Moving now · last 15 min</h4>
        {chip.kind === "reading" ? (
          <Badge
            variant="outline"
            className={cn("w-fit gap-1 px-2 py-0.5 text-xs font-medium", movementCurrent ? toneClass[chip.tone] : "text-muted-foreground")}
            title={chip.title}
          >
            <ChipIcon className="size-3.5" aria-hidden />
            {chip.text}
          </Badge>
        ) : (
          <NotReadyChip kind={chip.kind} title={chip.title} />
        )}
      </div>

      <div>
        <h4 className="text-xs font-medium text-muted-foreground">Trend · by candle size</h4>
        <ul className="mt-1.5 grid grid-cols-3 gap-1 rounded-xl bg-secondary/40 p-1" aria-label="Technical analysis by timeframe">
          {ta.map(({ frame, grade }) => {
            const readable = readableFrame(frame);
            const current = readable && grade === "fresh";
            const NotReady = frame.status === "not_loaded" ? "warming" : "none";
            return (
              <li
                key={frame.timeframe}
                className="flex min-w-0 flex-col items-center gap-0.5 rounded-lg bg-background/40 px-1 py-1.5 text-center"
                title={frame.note}
              >
                <span className="num text-xs text-muted-foreground">{frame.label}</span>
                {readable ? (
                  <span className={cn("inline-flex items-center gap-1 text-xs font-medium", current ? leanClass(frame.lean) : "text-muted-foreground")}>
                    <LeanIcon lean={frame.lean} />
                    {taPillText(frame)}
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                    <NotReadyChip kind={NotReady} className="border-0 px-0 py-0">{taPillText(frame)}</NotReadyChip>
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      </div>

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
        className="-mx-1 -my-2 inline-flex min-h-11 w-fit items-center gap-1 rounded-md px-1 text-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
          <Details movement={movement} ta={ta} now={now} slowRows={slowRows} />
        </div>
      </div>
    </div>
  );
}

const signedZ = (z: number) => `${z > 0 ? "+" : z < 0 ? "−" : ""}${Math.abs(z).toFixed(2)}`;

function Details({ movement, ta, now, slowRows }: { movement: PairMovementView; ta: TaFrameState[]; now: number; slowRows: SlowPairRow[] }) {
  return (
    <div className="flex flex-col gap-5 pb-1 text-sm">
      <section>
        <h4 className="text-xs font-medium text-muted-foreground">How unusual is the move</h4>
        {movement.kind !== "ok" ? (
          <div className="mt-2">
            <NotReadyChip
              kind={movement.kind === "unavailable" && isWarmingReason(movement.reason) ? "warming" : "none"}
              title={movement.kind === "unavailable" ? movement.reason : movement.reasons.join(", ").toLowerCase()}
            />
          </div>
        ) : (
          <>
            <ul className="mt-2 flex flex-col gap-3">
              {movement.windows.map((w) => (
                <li key={w.minutes} className="grid grid-cols-[2.5rem_1fr] items-center gap-x-3 gap-y-1 sm:grid-cols-[2.5rem_1fr_11rem]">
                  <span className="num text-xs text-muted-foreground">{`${w.minutes}m`.padStart(3, " ")}</span>
                  <DivergingBar
                    value={w.available ? w.z : null}
                    min={-3}
                    max={3}
                    flatBand={movement.flatZ}
                    label={`${w.minutes}-minute move versus this pair's usual`}
                    valueText={w.available && w.z !== null ? `${signedZ(w.z)}, ${w.label}` : isWarmingReason(w.reason) ? "warming up" : "not available"}
                  />
                  <span className="col-start-2 flex items-baseline gap-2 sm:col-start-3 sm:justify-end" title={w.reason ?? undefined}>
                    {w.available && w.z !== null ? (
                      <>
                        <span className={cn("num text-sm font-medium", toneClass[w.tone])}>{signedZ(w.z)}</span>
                        <span className="text-xs text-muted-foreground">{w.label}</span>
                      </>
                    ) : (
                      <span className="text-xs text-muted-foreground">{isWarmingReason(w.reason) ? "Warming up" : "Not available"}</span>
                    )}
                  </span>
                </li>
              ))}
              <li className="grid grid-cols-[2.5rem_1fr] items-start gap-x-3 gap-y-1 sm:grid-cols-[2.5rem_1fr_11rem]">
                <span className="text-xs text-muted-foreground">Vol</span>
                <Meter
                  value={movement.rvol ? movement.rvol.value : null}
                  max={3}
                  ticks={[{ at: 1, label: "usual" }]}
                  label="Volume of the latest 5 minutes versus usual"
                  valueText={movement.rvol ? `${movement.rvol.value.toFixed(2)} times usual` : `unavailable, ${movement.rvolReason ?? "no data"}`}
                />
                <span className="col-start-2 text-xs sm:col-start-3 sm:text-right">
                  {movement.rvol ? (
                    <span className="num">{movement.rvol.value.toFixed(2)}× usual</span>
                  ) : (
                    <span className="text-muted-foreground">{movement.rvolReason ?? "No data"}</span>
                  )}
                </span>
              </li>
            </ul>
            {movement.outlier || movement.partialReasons.length ? (
              <p className="mt-3 flex items-start gap-1.5 text-xs text-muted-foreground">
                <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                <span>
                  {[movement.outlier?.text, movement.partialReasons.length ? `Some windows unavailable: ${movement.partialReasons.join(", ")}` : null]
                    .filter(Boolean)
                    .join(". ")}
                </span>
              </p>
            ) : null}
          </>
        )}
      </section>

      {slowRows.length ? (
        <section>
          <h4 className="text-xs font-medium text-muted-foreground">Longer moves · versus its past week</h4>
          <ul className="mt-2 flex flex-col gap-3">
            {slowRows.map((row) => (
              <li key={row.horizonMin} className="grid grid-cols-[2.5rem_1fr] items-center gap-x-3 gap-y-1 sm:grid-cols-[2.5rem_1fr_11rem]">
                <span className="num text-xs text-muted-foreground">{row.label}</span>
                <DivergingBar
                  value={row.z}
                  min={-3}
                  max={3}
                  flatBand={2}
                  label={`${row.label} move versus this pair's past week`}
                  valueText={row.z === null ? (row.status === "collecting_history" ? "collecting history" : "not available") : `${signedZ(row.z)}, ${slowUnusualWord(row.z)?.toLowerCase()}`}
                />
                <span className="col-start-2 flex items-baseline gap-2 sm:col-start-3 sm:justify-end">
                  {row.z === null ? (
                    <span className="text-xs text-muted-foreground">{row.status === "collecting_history" ? "Warming up" : "Not available"}</span>
                  ) : (
                    <>
                      <span className="num text-sm font-medium">{signedZ(row.z)}</span>
                      <span className="text-xs text-muted-foreground">{slowUnusualWord(row.z)}</span>
                    </>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="@container">
        <h4 className="text-xs font-medium text-muted-foreground">Technical analysis (completed candles, not a forecast)</h4>
        <ul className="mt-2 grid gap-2 @2xl:grid-cols-3">
          {ta.map((state) => (
            <TaCard key={state.frame.timeframe} state={state} now={now} />
          ))}
        </ul>
      </section>
    </div>
  );
}

function TaCard({ state: { frame: f, grade }, now }: { state: TaFrameState; now: number }) {
  const readable = readableFrame(f);
  const fresh = grade === "fresh";
  const at = f.savedAt ?? f.candleClosedAt;
  return (
    <li className={cn("flex flex-col gap-3 rounded-lg bg-secondary/30 p-3", !fresh && "opacity-80")}>
      <div className="flex items-center gap-2">
        <span className="font-display text-sm font-semibold">{f.label}</span>
        {readable ? (
          <span className={cn("inline-flex items-center gap-1 text-xs font-medium", fresh ? leanClass(f.lean) : "text-muted-foreground")}>
            <LeanIcon lean={f.lean} />
            {taPillText(f)}
          </span>
        ) : (
          <NotReadyChip kind={f.status === "not_loaded" ? "warming" : "none"} title={f.note}>{taPillText(f)}</NotReadyChip>
        )}
        {at !== null ? (
          <span className="ml-auto inline-flex items-center gap-1.5 text-xs text-muted-foreground" title={f.note}>
            <span className={cn("size-1.5 rounded-full", fresh ? "bg-bull" : "bg-muted-foreground")} aria-hidden />
            <span className="num">{formatAge(Math.max(0, now - at))} ago</span>
            <span className="sr-only">{fresh ? "(current)" : "(behind)"}</span>
          </span>
        ) : null}
      </div>
      {readable ? (
        <>
          <div className="grid grid-cols-[3rem_1fr_3rem] items-center gap-2">
            <span className="text-xs text-muted-foreground">Score</span>
            <DivergingBar
              value={f.score}
              min={-100}
              max={100}
              label={`${f.label} technical score`}
              valueText={f.score === null ? "no score" : `${f.score > 0 ? "+" : ""}${f.score} of 100`}
              muted={!fresh}
            />
            <span className="num text-right text-xs">{f.score === null ? "—" : `${f.score > 0 ? "+" : ""}${f.score}`}</span>
          </div>
          <div className="grid grid-cols-[3rem_1fr_3rem] items-start gap-2">
            <span className="text-xs text-muted-foreground">RSI</span>
            <Meter
              value={f.rsi}
              max={100}
              fillClass="bg-muted-foreground/30"
              zones={[{ from: 0, to: 30, className: "bg-bear/15" }, { from: 70, to: 100, className: "bg-bull/15" }]}
              ticks={[{ at: 30, label: "30" }, { at: 70, label: "70" }]}
              label={`${f.label} RSI`}
              valueText={f.rsi === null ? "no RSI" : `RSI ${f.rsi.toFixed(1)}${f.rsi < 30 ? ", low" : f.rsi > 70 ? ", high" : ""}`}
            />
            <span className="num text-right text-xs">{f.rsi === null ? "—" : f.rsi.toFixed(1)}</span>
          </div>
          {f.trend || f.atrPct !== null || f.patterns.length ? (
            <div className="flex flex-wrap gap-1.5">
              {f.trend ? <Badge variant="outline" className="text-xs font-normal">{f.trend} trend</Badge> : null}
              {f.atrPct !== null ? <Badge variant="outline" className="num text-xs font-normal">Volatility {f.atrPct.toFixed(2)}%</Badge> : null}
              {f.patterns.map((pattern) => (
                <Badge key={pattern} variant="outline" className="text-xs font-normal">{pattern}</Badge>
              ))}
            </div>
          ) : null}
          {!fresh ? <p className="text-xs text-muted-foreground">{f.note}</p> : null}
        </>
      ) : null}
    </li>
  );
}
