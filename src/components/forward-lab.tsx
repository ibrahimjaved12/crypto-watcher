import { useMutation, useQuery } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { Download, FlaskConical } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Hint } from "@/components/hint";
import { EmptyState, SectionTitle } from "@/components/plain";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { exportForwardLogCsv, getForwardLog, getForwardReport } from "@/lib/forward.functions";
import type { LogKind, LogRow } from "@/lib/forward/forward-dashboard";
import {
  HORIZON_VALUES,
  RR_VALUES,
  SIDE_VALUES,
  SYMBOL_VALUES,
  type ForwardFilters,
} from "@/lib/forward/forward-filters";
import { bonferroniCritical, type ReportRow } from "@/lib/forward/forward-report";
import { outcomeLabel, relativeTime, strategyLabel, TA_FAMILY_IDS } from "@/lib/labels";
import { cn } from "@/lib/utils";

const rr = (value: number | null, digits = 2) =>
  value === null ? "—" : `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(digits)} R`;
const frame = (minutes: number) => (minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`);
const rrText = (value: string) => (value === "3/2" ? "1.5" : value);
const coin = (symbol: unknown) => String(symbol ?? "").replace(/USDT$/, "");
const at = (ms: unknown) =>
  typeof ms === "number" ? new Date(ms).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";

function Chips<T extends string | number>({
  label,
  values,
  selected,
  show,
  onChange,
}: {
  label: string;
  values: readonly T[];
  selected: readonly T[];
  show: (value: T) => string;
  onChange: (next: T[]) => void;
}) {
  return (
    <fieldset className="min-w-0">
      <legend className="mb-1 text-xs text-muted-foreground">{label}</legend>
      <div className="flex flex-wrap gap-1">
        {values.map((value) => {
          const on = selected.includes(value);
          return (
            <button
              key={String(value)}
              type="button"
              aria-pressed={on}
              onClick={() => onChange(on ? selected.filter((item) => item !== value) : [...selected, value])}
              className={cn(
                "min-h-8 rounded-full border px-2.5 text-xs",
                on ? "border-primary bg-primary/15 text-foreground" : "border-border text-muted-foreground",
              )}
            >
              {show(value)}
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}

/** Filters live in the URL search params, so a refresh or a shared link keeps the view. */
export function FiltersBar({
  filters,
  onChange,
}: {
  filters: ForwardFilters;
  onChange: (patch: Partial<ForwardFilters>) => void;
}) {
  return (
    <section className="panel space-y-3 p-4 sm:p-5" aria-label="Filters">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs text-muted-foreground">
          From (UTC date)
          <input
            type="date"
            className="mt-1 block h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground"
            value={filters.from ?? ""}
            onChange={(event) => onChange({ from: event.target.value || undefined })}
          />
        </label>
        <label className="text-xs text-muted-foreground">
          To (UTC date, included)
          <input
            type="date"
            className="mt-1 block h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground"
            value={filters.to ?? ""}
            onChange={(event) => onChange({ to: event.target.value || undefined })}
          />
        </label>
        <p className="text-xs text-muted-foreground">Dates select by when a trade finished (non-trades: when they would have entered).</p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Chips
          label="Strategy"
          values={TA_FAMILY_IDS}
          selected={filters.families}
          show={(id) => strategyLabel(id).name}
          onChange={(families) => onChange({ families })}
        />
        <Chips
          label="Timeframe"
          values={HORIZON_VALUES}
          selected={filters.horizons}
          show={frame}
          onChange={(horizons) => onChange({ horizons })}
        />
        <Chips
          label="Direction"
          values={SIDE_VALUES}
          selected={filters.sides}
          show={(side) => (side === 1 ? "Long" : "Short")}
          onChange={(sides) => onChange({ sides })}
        />
        <Chips
          label="Coin"
          values={SYMBOL_VALUES}
          selected={filters.symbols}
          show={coin}
          onChange={(symbols) => onChange({ symbols })}
        />
        <Chips
          label="Reward : risk"
          values={RR_VALUES}
          selected={filters.rr}
          show={rrText}
          onChange={(next) => onChange({ rr: next })}
        />
      </div>
    </section>
  );
}

function NonTradesCell({ row }: { row: ReportRow }) {
  const t = row.nonTrades;
  const skipped = t.vetoed + t.cost + t.position + t.gap + t.notEntered;
  const detail =
    `No volatility estimate yet: ${t.vetoed}. Bad entry candle: ${t.cost}. Entry price off the tick grid: ${t.position}. ` +
    `Stop too tight: ${t.gap}. Not tradeable: ${t.notEntered}.`;
  return (
    <span className="num">
      <Hint text={detail}>{skipped} skipped</Hint>
      {t.pending ? <span className="block text-[11px] text-muted-foreground">{t.pending} still open</span> : null}
    </span>
  );
}

export function ResultsPanel({ filters }: { filters: ForwardFilters }) {
  const load = useServerFn(getForwardReport);
  const report = useQuery({
    queryKey: ["forward-report", filters],
    queryFn: () => load({ data: filters }),
  });
  const rows = report.data ?? [];
  const k = rows.length;
  return (
    <section className="panel p-4 sm:p-5">
      <SectionTitle
        title="Results by strategy"
        hint="Finished paper trades only, after fees and funding, each next to a random-timing baseline with the same exits."
      />
      {report.isPending ? (
        <p className="py-6 text-sm text-muted-foreground">Loading results…</p>
      ) : report.error ? (
        <p role="alert" className="py-6 text-sm text-destructive">
          Results could not be loaded: {report.error.message}
        </p>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={FlaskConical}
          title="No finished trades for these filters"
          body="Results appear after a signal fires and its time limit passes (hours). Widen the dates or clear a filter."
        />
      ) : (
        <div className="-mx-1 overflow-x-auto px-1" tabIndex={0} aria-label="Results by strategy">
          <table className="w-full min-w-[980px] text-left text-sm">
            <thead className="text-xs text-muted-foreground">
              <tr className="border-b border-border">
                <th className="py-2 pr-3 font-medium">Strategy</th>
                <th className="py-2 pr-3 text-right font-medium">Trades</th>
                <th className="py-2 pr-3 text-right font-medium">Wins</th>
                <th className="py-2 pr-3 text-right font-medium">
                  <Hint text="Target and stop were both touched inside the same candle. Counted with the worse result, never the better one.">
                    Unclear
                  </Hint>
                </th>
                <th className="py-2 pr-3 text-right font-medium">
                  <Hint term="R">Mean per trade ± SE</Hint>
                </th>
                <th className="py-2 pr-3 text-right font-medium">
                  <Hint term="placebo">Random baseline</Hint>
                </th>
                <th className="py-2 pr-3 text-right font-medium">Difference</th>
                <th className="py-2 pr-3 font-medium">
                  <Hint
                    text={`Unadjusted for the ${k} strategy rows tried here: a positive row is a hypothesis for the next sample, not an edge. With ${k} rows a single row needs z above ${bonferroniCritical(k).toFixed(2)} to survive a Bonferroni correction.`}
                  >
                    Verdict (unadjusted for {k} rows)
                  </Hint>
                </th>
                <th className="py-2 font-medium">Non-trades</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const label = strategyLabel(row.strategyId);
                return (
                  <tr key={row.key} className="border-b border-border/50 align-top">
                    <td className="py-2 pr-3">
                      <Hint text={`${label.blurb} (id ${row.strategyId}${row.version ? `, ${row.version}` : ""})`}>
                        <span className="font-medium">{label.name}</span>
                      </Hint>
                      <div className="text-[11px] text-muted-foreground">
                        {frame(row.horizonMin)} · reward:risk {rrText(row.rr)}
                      </div>
                    </td>
                    <td className="num py-2 pr-3 text-right">{row.n}</td>
                    <td className="num py-2 pr-3 text-right">
                      {row.wins}
                      {row.winRate !== null ? (
                        <span className="text-muted-foreground"> ({Math.round(row.winRate * 100)}%)</span>
                      ) : null}
                    </td>
                    <td className="num py-2 pr-3 text-right">{row.ambiguous}</td>
                    <td className="num py-2 pr-3 text-right">
                      {rr(row.mean.mean)}
                      {row.mean.se !== null ? (
                        <span className="text-muted-foreground"> ± {row.mean.se.toFixed(2)}</span>
                      ) : null}
                    </td>
                    <td className="num py-2 pr-3 text-right text-muted-foreground">
                      {row.placebo ? (
                        <>
                          {rr(row.placebo.mean)}
                          {row.placebo.se !== null ? ` ± ${row.placebo.se.toFixed(2)}` : ""}
                          <div className="text-[11px]">{row.placebo.n} trades</div>
                        </>
                      ) : (
                        "none yet"
                      )}
                    </td>
                    <td className="num py-2 pr-3 text-right">
                      {rr(row.diffR)}
                      {row.z !== null ? (
                        <div className="text-[11px] text-muted-foreground">z = {row.z.toFixed(1)}</div>
                      ) : null}
                    </td>
                    <td className="py-2 pr-3 text-xs">{row.verdict.text}</td>
                    <td className="py-2 text-xs">
                      <NonTradesCell row={row} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="mt-2 text-xs text-muted-foreground">
            Small samples swing a lot. Fewer than 30 trades is never judged; the standard error (SE) shows how
            much the average could move by chance.
          </p>
        </div>
      )}
    </section>
  );
}

const LOG_HEADERS: Record<LogKind, { key: string; label: string }[]> = {
  signals: [
    { key: "signal_ms", label: "When" },
    { key: "symbol", label: "Coin" },
    { key: "side", label: "Direction" },
    { key: "strategy_id", label: "Strategy" },
    { key: "horizon_min", label: "Timeframe" },
  ],
  outcomes: [
    { key: "exit_ms", label: "Closed" },
    { key: "symbol", label: "Coin" },
    { key: "side", label: "Direction" },
    { key: "strategy_id", label: "Strategy" },
    { key: "status", label: "Result" },
    { key: "net_ur", label: "Net (R)" },
    { key: "cost_ur", label: "Fees (R)" },
    { key: "fund_ur", label: "Funding (R)" },
  ],
  ledger: [
    { key: "seq", label: "#" },
    { key: "ms", label: "When" },
    { key: "type", label: "Type" },
    { key: "symbol", label: "Coin" },
    { key: "amount_e8", label: "Amount (USDT)" },
    { key: "balance_e8", label: "Balance (USDT)" },
  ],
};

function cell(kind: LogKind, key: string, row: LogRow) {
  const value = row[key];
  if (key === "signal_ms" || key === "exit_ms" || key === "ms") return at(value);
  if (key === "symbol") return coin(value);
  if (key === "side") return value === 1 ? "Long" : "Short";
  if (key === "strategy_id") return strategyLabel(String(value)).name;
  if (key === "horizon_min") return frame(Number(value));
  if (key === "status") return outcomeLabel(String(value)).short;
  if (key === "net_ur" || key === "cost_ur" || key === "fund_ur")
    return value === null ? "—" : (Number(value) / 1e6).toFixed(3);
  if (key === "amount_e8" || key === "balance_e8") return (Number(value) / 1e8).toFixed(2);
  return String(value ?? "—");
}

function LogTable({ kind, filters }: { kind: LogKind; filters: ForwardFilters }) {
  const load = useServerFn(getForwardLog);
  const exportCsv = useServerFn(exportForwardLogCsv);
  const [pages, setPages] = useState<{ rows: LogRow[]; nextCursor: string | null }[]>([]);
  const first = useQuery({
    queryKey: ["forward-log", kind, filters],
    queryFn: () => load({ data: { kind, cursor: null, filters } }),
  });
  // The parent keys this component by the filters, so appended pages reset when they change.
  const more = useMutation({
    mutationFn: (cursor: string) => load({ data: { kind, cursor, filters } }),
    onSuccess: (page) => setPages((current) => [...current, page]),
    onError: (error) => toast.error(error instanceof Error ? error.message : "Could not load more"),
  });
  const download = useMutation({
    mutationFn: () => exportCsv({ data: { kind, filters } }),
    onSuccess: (result) => {
      const url = URL.createObjectURL(new Blob([result.csv], { type: "text/csv" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `forward-${kind}-${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      URL.revokeObjectURL(url);
      toast(`Exported ${result.rows} rows${result.truncated ? " (first 50,000 only; narrow the dates)" : ""}`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Export failed"),
  });
  const all = [...(first.data?.rows ?? []), ...pages.flatMap((page) => page.rows)];
  const next = pages.length ? pages.at(-1)!.nextCursor : (first.data?.nextCursor ?? null);
  const headers = LOG_HEADERS[kind];
  return (
    <div>
      <div className="mb-2 flex justify-end">
        <Button variant="outline" size="sm" onClick={() => download.mutate()} disabled={download.isPending}>
          <Download className="size-4" aria-hidden />
          Export CSV
        </Button>
      </div>
      {first.isPending ? (
        <p className="py-6 text-sm text-muted-foreground">Loading…</p>
      ) : first.error ? (
        <p role="alert" className="py-6 text-sm text-destructive">
          Could not load the log: {first.error.message}
        </p>
      ) : all.length === 0 ? (
        <EmptyState
          icon={FlaskConical}
          title="Nothing logged for these filters"
          body={
            kind === "signals"
              ? "A signal is logged when a strategy’s condition is met on a completed candle."
              : kind === "outcomes"
                ? "A trade appears here when it reaches its target, stop or time limit."
                : "Ledger lines appear when the paper wallet pays a fee, funding or closes a trade."
          }
          className="py-6"
        />
      ) : (
        <div className="-mx-1 overflow-x-auto px-1" tabIndex={0} aria-label={`${kind} log`}>
          <table className="w-full min-w-[640px] text-left text-sm">
            <thead className="text-xs text-muted-foreground">
              <tr className="border-b border-border">
                {headers.map((header) => (
                  <th key={header.key} className="py-2 pr-3 font-medium">
                    {header.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {all.map((row, index) => (
                <tr key={`${index}-${String(row["signal_id"] ?? row["setup_id"] ?? row["seq"])}`} className="border-b border-border/50">
                  {headers.map((header) => (
                    <td key={header.key} className="num py-1.5 pr-3">
                      {cell(kind, header.key, row)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3 flex items-center gap-3">
            {next ? (
              <Button variant="outline" size="sm" onClick={() => more.mutate(next)} disabled={more.isPending}>
                Load more
              </Button>
            ) : (
              <span className="text-xs text-muted-foreground">All {all.length} rows shown.</span>
            )}
            <span className="text-xs text-muted-foreground">
              Newest first{all[0] ? ` · latest ${relativeTime(Number(all[0]["signal_ms"] ?? all[0]["exit_ms"] ?? all[0]["ms"]))}` : ""}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

export function LogsPanel({ filters }: { filters: ForwardFilters }) {
  return (
    <section className="panel p-4 sm:p-5">
      <SectionTitle title="Logs" hint="Every signal, finished trade and wallet line, newest first. Filters above apply." />
      <Tabs defaultValue="signals">
        <TabsList>
          <TabsTrigger value="signals">Signals</TabsTrigger>
          <TabsTrigger value="outcomes">Outcomes</TabsTrigger>
          <TabsTrigger value="ledger">Ledger</TabsTrigger>
        </TabsList>
        {(["signals", "outcomes", "ledger"] as const).map((kind) => (
          <TabsContent key={kind} value={kind} className="mt-3">
            <LogTable key={JSON.stringify(filters)} kind={kind} filters={filters} />
          </TabsContent>
        ))}
      </Tabs>
    </section>
  );
}
