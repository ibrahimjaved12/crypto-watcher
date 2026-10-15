import { Hint } from "@/components/hint";
import { StatusChip } from "@/components/plain";
import type { Level } from "@/lib/labels";
import { cn } from "@/lib/utils";
import { formatAge, type FreshnessItem, type MarketStripView, type StatusTone } from "@/lib/market/assessment";

const TONE_LEVEL: Record<StatusTone, Level> = { live: "ok", warming: "wait", stale: "problem", offline: "problem", unavailable: "wait" };
const time = (ms: number | null) => (ms === null ? "—" : new Date(ms).toLocaleTimeString());

/** Market-wide line (#30 "Current assessment"): what the whole watched market is doing now, and how fresh that is. */
export function MarketStrip({
  view,
  freshness,
  now,
}: {
  view: MarketStripView;
  freshness: FreshnessItem[];
  now: number;
}) {
  return (
    <section className="panel mt-4 p-4" aria-label="Market-wide assessment">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold">
          <Hint text="What your watched pairs are doing together, from the market engine that reads every trade. It describes what is happening; it is not a forecast or a signal.">
            Market now
          </Hint>
        </h2>
        <StatusChip level={TONE_LEVEL[view.tone]}>{view.statusLabel}</StatusChip>
        {view.direction ? (
          <span className="text-sm font-medium">
            {view.direction.label}
            {view.pace ? <span className="font-normal text-muted-foreground"> · {view.pace}</span> : null}
          </span>
        ) : null}
        <span className="text-xs text-muted-foreground">{view.explanation}</span>
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">Source: {view.source}.</p>

      {view.windows.length > 0 ? (
        <>
          {view.notLiveNote ? (
            <p className="mt-2 text-xs text-warn" role="status">
              {view.notLiveNote}
            </p>
          ) : null}
          <div className={cn("mt-3 grid gap-3 md:grid-cols-3", !view.current && "opacity-70")}>
            {view.windows.map((w) => (
              <div key={w.minutes} className="rounded-md border border-border p-3 text-xs">
                <p className="font-medium">
                  {w.minutes} min
                  <span className="ml-2 font-normal text-muted-foreground">
                    <Hint text={w.materialNote}>what is “material”?</Hint>
                  </span>
                </p>
                <p className="mt-1 text-sm">{w.sentence}</p>
                {w.available ? (
                  <p className="num mt-1 text-muted-foreground">
                    <Hint text="The middle pair's move in this window, as a share of price. Half the pairs moved more, half less.">
                      Median move
                    </Hint>{" "}
                    {w.medianMovePct === null ? "—" : `${w.medianMovePct > 0 ? "+" : ""}${w.medianMovePct.toFixed(2)}%`}
                    {" · "}
                    <Hint text="How spread out the pairs' moves are (median absolute deviation of normalized moves). Low means pairs move alike; high means they disagree.">
                      Dispersion
                    </Hint>{" "}
                    {w.dispersion === null ? "—" : w.dispersion.toFixed(2)}
                  </p>
                ) : null}
                {w.outliers.length ? (
                  <p className="mt-1">
                    <Hint text="A pair moving far more than the rest of the market and more than usual for itself, while the others do not follow.">
                      Isolated outliers
                    </Hint>
                    :{" "}
                    {w.outliers
                      .map((o) => `${o.symbol.replace(/USDT$/, "")} ${o.direction} ${o.movePct > 0 ? "+" : ""}${o.movePct.toFixed(2)}%`)
                      .join(", ")}
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        </>
      ) : null}

      {view.universe ? (
        <details className="mt-3 text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            Universe: {view.universe.included.length} included of {view.universe.configured.length} watched
            {view.universe.excluded.length ? `, ${view.universe.excluded.length} excluded` : ""}
          </summary>
          <p className="mt-1">
            Included: {view.universe.included.map((s) => s.replace(/USDT$/, "")).join(", ") || "none"}
          </p>
          {view.universe.excluded.length ? (
            <ul className="mt-1 list-disc pl-5">
              {view.universe.excluded.map((e) => (
                <li key={e.symbol}>
                  {e.symbol.replace(/USDT$/, "")}: {e.reasons.join(", ").toLowerCase()}
                </li>
              ))}
            </ul>
          ) : null}
          <p className="num mt-1 text-muted-foreground">
            Evaluation boundary {time(view.times.evaluationBoundary)} · latest trade data received{" "}
            {time(view.times.lastReceivedAt)} · exchange event time {time(view.times.lastSourceEvent)} · state saved{" "}
            {time(view.times.engineUpdatedAt)}
          </p>
        </details>
      ) : null}

      <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs" aria-label="Data freshness by source">
        {freshness.map((item) => (
          <li key={item.id} className="flex items-baseline gap-1.5">
            <StatusChip level={item.status === "fresh" ? "ok" : item.status === "stale" ? "problem" : "wait"}>
              {item.label}: {item.status === "fresh" ? "fresh" : item.status === "stale" ? "stale" : "unavailable"}
            </StatusChip>
            <span className="text-muted-foreground">
              {item.detail}
              {item.asOf !== null ? ` (as of ${formatAge(now - item.asOf)} ago)` : ""}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
