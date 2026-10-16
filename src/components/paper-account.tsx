import { Link } from "@tanstack/react-router";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import {
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  ChevronDown,
  Download,
  FlaskConical,
  Inbox,
  RefreshCw,
  ShieldAlert,
  Wallet,
} from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { Hint } from "@/components/hint";
import { EmptyState, SectionTitle, StatusChip } from "@/components/plain";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { exportForwardLogCsv } from "@/lib/forward.functions";
import { freshnessLabel, strategyLabel, type Level } from "@/lib/labels";
import {
  getPaperAccount,
  getPaperLedger,
  getPaperRejected,
  getPaperTrades,
} from "@/lib/paper.functions";
import {
  PAPER_TERMS,
  accountView,
  closedTradeViews,
  dateTimeText,
  equityView,
  formatDuration,
  formatPercent,
  formatUsdt,
  ledgerViews,
  listState,
  openPositionViews,
  rejectedViews,
  resultsView,
  runFacts,
  type AccountView,
  type ClosedTradeView,
  type EquityView,
  type LedgerRow,
  type LedgerView,
  type ListState,
  type OutcomeKey,
  type PositionView,
  type RejectedView,
  type ResultsView,
  type Signed,
  type Tone,
} from "@/lib/paper/paper-account";
import type { Page, PaperAccountData } from "@/lib/paper/paper-data";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

/**
 * Paper account page body (#36 Part 3). Layout breakpoints (Tailwind): under 640 px ("mobile") the account cards are
 * a 2-column grid, every list is a stack of cards with an expand control, controls are at least 44 px high and the tab
 * bar sticks under the header; from 640 px lists are tables and the cards form a 4-column strip; from 1024 px the strip
 * is one row of 8. Charts and loading skeletons share one reserved height, so nothing jumps while data arrives.
 */

const TONE_CLASS: Record<Tone, string> = {
  gain: "text-bull",
  loss: "text-bear",
  flat: "text-muted-foreground",
};
const OUTCOME_LEVEL: Record<OutcomeKey, Level> = {
  target: "ok",
  stopped: "problem",
  liquidated: "problem",
  time_limit: "wait",
  ambiguous: "wait",
  unresolved: "wait",
};
const touch = "min-h-11 sm:min-h-9";
const strategyName = (id: string | null) => (id ? strategyLabel(id).name : "—");

function SignedText({ value, className = "" }: { value: Signed | null; className?: string }) {
  if (!value) return <span className="num text-muted-foreground">—</span>;
  return (
    <span className={cn("num whitespace-nowrap", TONE_CLASS[value.tone], className)}>
      {value.arrow ? <span aria-hidden>{value.arrow} </span> : null}
      {value.text}
    </span>
  );
}

export function SimulatedBanner() {
  return (
    <div
      role="note"
      className="flex items-start gap-2 rounded-lg border border-warn/40 bg-warn/10 p-3 text-sm"
    >
      <ShieldAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
      <p>
        <strong className="font-semibold">
          <Hint text={PAPER_TERMS.simulated}>Simulated account: fake money.</Hint>
        </strong>{" "}
        <span className="text-muted-foreground">
          Every balance, position and result on this page is simulated. Real orders are always
          placed by you.
        </span>
      </p>
    </div>
  );
}

function SideChip({ side }: { side: 1 | -1 }) {
  const Icon = side === 1 ? ArrowUpRight : ArrowDownRight;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-0.5 rounded-full border px-1.5 text-[11px] font-medium",
        side === 1 ? "border-bull/40 text-bull" : "border-bear/40 text-bear",
      )}
    >
      <Icon className="size-3" aria-hidden />
      {side === 1 ? "Long" : "Short"}
    </span>
  );
}

function ListStatus({
  state,
  error,
  onRetry,
  empty,
  children,
}: {
  state: ListState;
  error: unknown;
  onRetry: () => void;
  empty: { title: string; body: ReactNode };
  children: ReactNode;
}) {
  if (state === "loading")
    return (
      <div className="min-h-40 space-y-2" role="status" aria-live="polite">
        <span className="sr-only">Loading…</span>
        {[0, 1, 2].map((row) => (
          <div key={row} className="h-12 animate-pulse rounded-md bg-muted/50" aria-hidden />
        ))}
      </div>
    );
  if (state === "error")
    return (
      <div role="alert" className="min-h-40 space-y-3 py-4 text-sm">
        <p className="text-destructive">
          Could not load this list: {error instanceof Error ? error.message : "unknown error"}
        </p>
        <Button variant="outline" className={touch} onClick={onRetry}>
          <RefreshCw className="size-4" aria-hidden /> Try again
        </Button>
      </div>
    );
  if (state === "empty")
    return (
      <EmptyState icon={Inbox} title={empty.title} body={empty.body} className="min-h-40 py-6" />
    );
  return <>{children}</>;
}

/** A card with the key fields always visible and the rest behind a 44 px expand control (mobile lists). */
function ExpandCard({
  head,
  summary,
  details,
  label,
}: {
  head: ReactNode;
  summary: [string, ReactNode][];
  details: [string, ReactNode][];
  label: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <li className="panel p-3">
      <div className="flex flex-wrap items-center gap-2">{head}</div>
      <dl className="mt-2 grid grid-cols-3 gap-2 text-xs">
        {summary.map(([name, value]) => (
          <div key={name} className="min-w-0">
            <dt className="text-muted-foreground">{name}</dt>
            <dd className="num mt-0.5 truncate">{value}</dd>
          </div>
        ))}
      </dl>
      <button
        type="button"
        aria-expanded={open}
        aria-label={`${open ? "Hide" : "Show"} details: ${label}`}
        onClick={() => setOpen((value) => !value)}
        className="mt-1 flex min-h-11 w-full items-center justify-between rounded-md px-1 text-xs text-muted-foreground"
      >
        {open ? "Hide details" : "More details"}
        <ChevronDown
          className={cn("size-4 transition-transform", open && "rotate-180")}
          aria-hidden
        />
      </button>
      {open ? (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-2 border-t border-border/50 pt-2 text-xs">
          {details.map(([name, value]) => (
            <div key={name} className="min-w-0">
              <dt className="text-muted-foreground">{name}</dt>
              <dd className="num mt-0.5 break-words">{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </li>
  );
}

const th = "py-2 pr-3 font-medium";
const thRight = "py-2 pr-3 text-right font-medium";

// ---------------------------------------------------------------- header strip and status

function AccountStrip({ view }: { view: AccountView }) {
  return (
    <section
      aria-label="Account"
      className="grid grid-cols-2 gap-2 sm:grid-cols-4 sm:gap-3 lg:grid-cols-8"
    >
      {view.cards.map((card) => (
        <div key={card.key} className="panel min-w-0 p-3">
          <p className="text-[11px] leading-tight text-muted-foreground">
            <Hint text={card.hint}>{card.label}</Hint>
          </p>
          <p
            className={cn(
              "num mt-1 truncate text-base font-semibold sm:text-lg",
              card.tone ? TONE_CLASS[card.tone] : "",
            )}
          >
            {card.tone === "gain" ? (
              <span aria-hidden>▲ </span>
            ) : card.tone === "loss" ? (
              <span aria-hidden>▼ </span>
            ) : null}
            {card.value}
          </p>
          <p className="mt-0.5 truncate text-[11px] text-muted-foreground">{card.sub}</p>
        </div>
      ))}
    </section>
  );
}

const FRESH_LEVEL = { fresh: "ok", delayed: "wait", stale: "problem", none: "wait" } as const;

function StatusLine({
  view,
  now,
  fetching,
  onRefresh,
}: {
  view: AccountView;
  now: number;
  fetching: boolean;
  onRefresh: () => void;
}) {
  const label =
    view.freshness === "none"
      ? { short: "No data yet" }
      : freshnessLabel(view.freshness.toUpperCase());
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <StatusChip level={FRESH_LEVEL[view.freshness]}>{label.short}</StatusChip>
      {view.asOfMs !== null ? (
        <span className="num">
          Wallet as of {dateTimeText(view.asOfMs)} ({formatDuration(now - view.asOfMs)} ago)
        </span>
      ) : null}
      {view.status === "stale" ? <Hint text={PAPER_TERMS.stale}>Why is this old?</Hint> : null}
      <Button
        variant="outline"
        className={cn(touch, "ml-auto")}
        onClick={onRefresh}
        disabled={fetching}
      >
        <RefreshCw className={cn("size-4", fetching && "animate-spin")} aria-hidden /> Refresh
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------- equity and drawdown

const W = 480;
function linePath(values: number[], height: number, low: number, high: number): string {
  const span = high - low || 1;
  return values
    .map(
      (value, index) =>
        `${index ? "L" : "M"}${(index / Math.max(1, values.length - 1)) * W},${height - ((value - low) / span) * height}`,
    )
    .join(" ");
}

function EquityPanel({ view, state }: { view: EquityView; state: "loading" | "ready" }) {
  const reserved = "h-48";
  const values = view.points.map((point) => point.value);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const path = view.status === "ready" ? linePath(values, 160, low, high) : "";
  const depth = view.drawdown.map((point) => point.value);
  const worst = Math.min(0, ...depth);
  return (
    <section className="panel p-4 sm:p-5" aria-label="Equity and drawdown">
      <SectionTitle
        title="Equity and drawdown"
        hint="Balance history in USDT, with equity now (balance plus unrealized P&L) as the last point."
      />
      <div className={cn(reserved, "w-full")}>
        {state === "loading" ? (
          <div className="size-full animate-pulse rounded-md bg-muted/50" role="status">
            <span className="sr-only">Loading chart…</span>
          </div>
        ) : view.status === "empty" ? (
          <EmptyState
            icon={Wallet}
            title="No balance history yet"
            body="The line starts after the first ledger entry."
            className="h-full justify-center py-4"
          />
        ) : (
          <svg
            viewBox={`0 0 ${W} 160`}
            preserveAspectRatio="none"
            className="size-full text-primary"
            role="img"
            aria-label={`Balance history; now ${formatUsdt(view.last)} USDT`}
          >
            <defs>
              <linearGradient id="paper-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="currentColor" stopOpacity="0.3" />
                <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
              </linearGradient>
            </defs>
            <path d={`${path} L${W},160 L0,160 Z`} fill="url(#paper-fill)" />
            <path
              d={path}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              vectorEffect="non-scaling-stroke"
            />
          </svg>
        )}
      </div>
      <div className="mt-3 h-16 w-full" aria-hidden={view.status !== "ready"}>
        {view.status === "ready" ? (
          <svg
            viewBox={`0 0 ${W} 48`}
            preserveAspectRatio="none"
            className="size-full text-bear"
            role="img"
            aria-label={
              view.maxDrawdown
                ? `Drawdown from peak; worst ${formatPercent(view.maxDrawdown.fraction)}`
                : "No drawdown yet"
            }
          >
            <line
              x1="0"
              y1="1"
              x2={W}
              y2="1"
              stroke="currentColor"
              strokeOpacity="0.35"
              strokeWidth="1"
              vectorEffect="non-scaling-stroke"
            />
            {worst < 0 ? (
              <path
                d={`${linePath(depth, 47, worst, 0)} L${W},1 L0,1 Z`}
                fill="currentColor"
                fillOpacity="0.2"
              />
            ) : null}
          </svg>
        ) : null}
      </div>
      {view.status === "ready" ? (
        <p className="num mt-2 text-xs text-muted-foreground">
          {dateTimeText(view.points[0]?.ms)} →{" "}
          {dateTimeText(view.points[view.points.length - 1]?.ms)} · since start{" "}
          <SignedText value={view.change} />
          {view.changeFraction !== null ? ` (${formatPercent(view.changeFraction)})` : ""} ·{" "}
          <Hint text={PAPER_TERMS.maxDrawdown}>Max drawdown</Hint>{" "}
          {view.maxDrawdown
            ? `▼ −${formatUsdt(view.maxDrawdown.usdt)} USDT (−${formatPercent(view.maxDrawdown.fraction)})`
            : "none yet"}
        </p>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------- open positions

function MarkCell({ position }: { position: PositionView }) {
  const source =
    position.markSource === "wallet-mark"
      ? "wallet mark"
      : position.markSource === "published-price"
        ? "published price"
        : "no price";
  return (
    <span className="block">
      <span className="num">{position.mark}</span>
      <span className="block text-[11px] text-muted-foreground">
        <Hint text={PAPER_TERMS.markPrice}>{source}</Hint>
        {position.markStale ? (
          <span className="ml-1 inline-flex items-center gap-0.5 text-warn">
            <AlertTriangle className="size-3" aria-hidden /> stale
          </span>
        ) : null}
      </span>
    </span>
  );
}

const strategyLine = (p: PositionView) =>
  `${strategyName(p.strategyId)}${p.horizonMin !== null ? ` · ${p.horizonMin >= 60 ? `${p.horizonMin / 60}h` : `${p.horizonMin}m`} horizon` : ""}`;

function PositionsTab({ positions }: { positions: PositionView[] }) {
  return (
    <>
      <div className="hidden sm:block overflow-x-auto" tabIndex={0} aria-label="Open positions">
        <table className="w-full min-w-[980px] text-left text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr className="border-b border-border">
              <th scope="col" className={th}>
                Position
              </th>
              <th scope="col" className={thRight}>
                Quantity
              </th>
              <th scope="col" className={thRight}>
                Entry
              </th>
              <th scope="col" className={thRight}>
                Mark
              </th>
              <th scope="col" className={thRight}>
                <Hint text={PAPER_TERMS.unrealized}>Unrealized</Hint>
              </th>
              <th scope="col" className={thRight}>
                Margin
              </th>
              <th scope="col" className={thRight}>
                Leverage
              </th>
              <th scope="col" className={thRight}>
                <Hint text={PAPER_TERMS.liquidationDistance}>Liquidation</Hint>
              </th>
              <th scope="col" className={thRight}>
                Stop
              </th>
              <th scope="col" className={thRight}>
                Target
              </th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.id} className="border-b border-border/50 align-top">
                <td className="py-2 pr-3">
                  <span className="num font-medium">{p.coin}</span> <SideChip side={p.side} />
                  <span className="block text-[11px] text-muted-foreground">{strategyLine(p)}</span>
                  <span className="block text-[11px] text-muted-foreground">{p.openedText}</span>
                </td>
                <td className="num py-2 pr-3 text-right">{p.quantity}</td>
                <td className="num py-2 pr-3 text-right">{p.entry}</td>
                <td className="py-2 pr-3 text-right">
                  <MarkCell position={p} />
                </td>
                <td className="py-2 pr-3 text-right">
                  <SignedText value={p.unrealized} />
                </td>
                <td className="num py-2 pr-3 text-right">{p.margin}</td>
                <td className="num py-2 pr-3 text-right">
                  {p.leverage}
                  {p.atCap ? (
                    <span className="block text-[11px] text-muted-foreground">at the cap</span>
                  ) : null}
                </td>
                <td className="num py-2 pr-3 text-right">
                  {p.liquidation}
                  <span className="block text-[11px] text-muted-foreground">
                    ▼ {p.liquidationDistanceText} away
                  </span>
                </td>
                <td className="num py-2 pr-3 text-right">{p.stop}</td>
                <td className="num py-2 text-right">{p.target}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="space-y-2 sm:hidden" aria-label="Open positions">
        {positions.map((p) => (
          <ExpandCard
            key={p.id}
            label={`${p.coin} ${p.sideLabel}`}
            head={
              <>
                <span className="num font-medium">{p.coin}</span>
                <SideChip side={p.side} />
                <span className="ml-auto">
                  <SignedText value={p.unrealized} />
                </span>
              </>
            }
            summary={[
              ["Quantity", p.quantity],
              ["Entry → mark", `${p.entry} → ${p.mark}`],
              ["To liquidation", `▼ ${p.liquidationDistanceText}`],
            ]}
            details={[
              [
                "Mark source",
                p.markSource === "wallet-mark"
                  ? "wallet mark"
                  : p.markSource === "published-price"
                    ? "published price"
                    : "no price",
              ],
              ["Stale", p.markStale ? "yes" : "no"],
              ["Strategy", strategyLine(p)],
              ["Margin (USDT)", p.margin],
              ["Leverage", p.atCap ? `${p.leverage} (at the cap)` : p.leverage],
              ["Liquidation price", p.liquidation],
              ["Stop", p.stop],
              ["Target", p.target],
              ["Notional (USDT)", p.notional],
              ["Opened", p.openedText],
            ]}
          />
        ))}
      </ul>
    </>
  );
}

// ---------------------------------------------------------------- paged lists

type Loader = (args: { data: { accountId: string; cursor: string | null } }) => Promise<unknown>;

/** Keyset-paginated list; its key carries the wallet's last update, so a new hour refetches only the first page. */
function usePagedLines(
  kind: string,
  load: Loader,
  accountId: string | null,
  boundary: number | null,
) {
  const query = useInfiniteQuery({
    queryKey: ["paper", kind, accountId, boundary],
    enabled: accountId !== null,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) =>
      (await load({ data: { accountId: accountId ?? "", cursor: pageParam } })) as unknown as Page,
    getNextPageParam: (last) => last.nextCursor,
  });
  const pages = useMemo(() => query.data?.pages ?? [], [query.data]);
  const lines = useMemo(() => {
    const bySeq = new Map<number, LedgerRow>();
    for (const page of pages) for (const line of page.lines) bySeq.set(line.seq, line);
    return [...bySeq.values()];
  }, [pages]);
  const strategies = useMemo(() => {
    const map = new Map<string, string>();
    for (const page of pages)
      for (const [setup, strategy] of Object.entries(page.strategies)) map.set(setup, strategy);
    return map;
  }, [pages]);
  return { query, lines, strategies };
}

function LoadMore({
  query,
  shown,
}: {
  query: { hasNextPage: boolean; isFetchingNextPage: boolean; fetchNextPage: () => unknown };
  shown: number;
}) {
  return (
    <div className="mt-3 flex items-center gap-3">
      {query.hasNextPage ? (
        <Button
          variant="outline"
          className={touch}
          onClick={() => void query.fetchNextPage()}
          disabled={query.isFetchingNextPage}
        >
          {query.isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      ) : (
        <span className="text-xs text-muted-foreground">All {shown} shown.</span>
      )}
      <span className="text-xs text-muted-foreground">Newest first</span>
    </div>
  );
}

function TradesTab({ accountId, boundary }: { accountId: string | null; boundary: number | null }) {
  const load = useServerFn(getPaperTrades);
  const { query, lines, strategies } = usePagedLines("trades", load, accountId, boundary);
  const trades = useMemo(() => closedTradeViews(lines, strategies), [lines, strategies]);
  const state = listState({ isPending: query.isPending, error: query.error, count: trades.length });
  return (
    <ListStatus
      state={state}
      error={query.error}
      onRetry={() => void query.refetch()}
      empty={{
        title: "No closed trades yet",
        body: "A trade appears here when a position reaches its target, stop or time limit, or is liquidated.",
      }}
    >
      <div className="hidden sm:block overflow-x-auto" tabIndex={0} aria-label="Closed trades">
        <table className="w-full min-w-[860px] text-left text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr className="border-b border-border">
              <th scope="col" className={th}>
                Closed
              </th>
              <th scope="col" className={th}>
                Trade
              </th>
              <th scope="col" className={th}>
                Result
              </th>
              <th scope="col" className={thRight}>
                Gross
              </th>
              <th scope="col" className={thRight}>
                <Hint text={PAPER_TERMS.fees}>Fees</Hint>
              </th>
              <th scope="col" className={thRight}>
                <Hint text={PAPER_TERMS.funding}>Funding</Hint>
              </th>
              <th scope="col" className={thRight}>
                Net
              </th>
              <th scope="col" className={thRight}>
                Held
              </th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => (
              <TradeRow key={t.id} trade={t} />
            ))}
          </tbody>
        </table>
      </div>
      <ul className="space-y-2 sm:hidden" aria-label="Closed trades">
        {trades.map((t) => (
          <ExpandCard
            key={t.id}
            label={`${t.coin} trade`}
            head={
              <>
                <span className="num font-medium">{t.coin}</span>
                <OutcomeChip trade={t} />
                <span className="ml-auto">
                  <SignedText value={t.net} />
                </span>
              </>
            }
            summary={[
              ["Closed", t.closedText.split(",")[0] ?? "—"],
              ["Held", t.holdingText],
              ["Net (USDT)", <SignedText key="n" value={t.net} />],
            ]}
            details={[
              ["Closed at", t.closedText],
              ["Strategy", strategyName(t.strategyId)],
              ["Gross", <SignedText key="g" value={t.gross} />],
              ["Fees", <SignedText key="f" value={t.fees} />],
              ["Funding", <SignedText key="u" value={t.funding} />],
              ["Ledger lines", String(t.lines)],
            ]}
          />
        ))}
      </ul>
      <LoadMore query={query} shown={trades.length} />
    </ListStatus>
  );
}

function OutcomeChip({ trade }: { trade: ClosedTradeView }) {
  return (
    <Hint text={trade.outcomeHelp}>
      <StatusChip level={OUTCOME_LEVEL[trade.outcome]}>{trade.outcomeLabel}</StatusChip>
    </Hint>
  );
}

function TradeRow({ trade: t }: { trade: ClosedTradeView }) {
  return (
    <tr className="border-b border-border/50 align-top">
      <td className="num py-2 pr-3 text-xs">{t.closedText}</td>
      <td className="py-2 pr-3">
        <span className="num font-medium">{t.coin}</span>
        <span className="block text-[11px] text-muted-foreground">
          {strategyName(t.strategyId)}
        </span>
      </td>
      <td className="py-2 pr-3">
        <OutcomeChip trade={t} />
      </td>
      <td className="py-2 pr-3 text-right">
        <SignedText value={t.gross} />
      </td>
      <td className="py-2 pr-3 text-right">
        <SignedText value={t.fees} />
      </td>
      <td className="py-2 pr-3 text-right">
        <SignedText value={t.funding} />
      </td>
      <td className="py-2 pr-3 text-right font-medium">
        <SignedText value={t.net} />
      </td>
      <td className="num py-2 text-right">{t.holdingText}</td>
    </tr>
  );
}

function RejectedTab({
  accountId,
  boundary,
}: {
  accountId: string | null;
  boundary: number | null;
}) {
  const load = useServerFn(getPaperRejected);
  const { query, lines, strategies } = usePagedLines("rejected", load, accountId, boundary);
  const items = useMemo(() => rejectedViews(lines, strategies), [lines, strategies]);
  const state = listState({ isPending: query.isPending, error: query.error, count: items.length });
  const kind = (item: RejectedView) =>
    item.kind === "order" ? "Order" : item.kind === "fill" ? "Fill" : "Setup";
  return (
    <ListStatus
      state={state}
      error={query.error}
      onRetry={() => void query.refetch()}
      empty={{
        title: "Nothing was turned down",
        body: "When the wallet cannot afford or allow a setup, order or fill, the reason is recorded here.",
      }}
    >
      <div className="hidden sm:block overflow-x-auto" tabIndex={0} aria-label="Rejected">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr className="border-b border-border">
              <th scope="col" className={th}>
                When
              </th>
              <th scope="col" className={th}>
                Coin
              </th>
              <th scope="col" className={th}>
                Strategy
              </th>
              <th scope="col" className={th}>
                What
              </th>
              <th scope="col" className={th}>
                Why it was turned down
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.seq} className="border-b border-border/50 align-top">
                <td className="num py-2 pr-3 text-xs">{item.timeText}</td>
                <td className="num py-2 pr-3">{item.coin || "—"}</td>
                <td className="py-2 pr-3 text-xs">{strategyName(item.strategyId)}</td>
                <td className="py-2 pr-3 text-xs">{kind(item)}</td>
                <td className="py-2 text-xs">{item.reasonLabel}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="space-y-2 sm:hidden" aria-label="Rejected">
        {items.map((item) => (
          <li key={item.seq} className="panel space-y-1 p-3 text-sm">
            <p className="flex flex-wrap items-center gap-2">
              <span className="num font-medium">{item.coin || "—"}</span>
              <span className="rounded-full border border-border px-1.5 text-[11px]">
                {kind(item)}
              </span>
              <span className="num ml-auto text-xs text-muted-foreground">{item.timeText}</span>
            </p>
            <p>{item.reasonLabel}</p>
            <p className="text-xs text-muted-foreground">{strategyName(item.strategyId)}</p>
          </li>
        ))}
      </ul>
      <LoadMore query={query} shown={items.length} />
    </ListStatus>
  );
}

function LedgerTab({ accountId, boundary }: { accountId: string | null; boundary: number | null }) {
  const load = useServerFn(getPaperLedger);
  const exportCsv = useServerFn(exportForwardLogCsv);
  const { query, lines } = usePagedLines("ledger", load, accountId, boundary);
  const rows: LedgerView[] = useMemo(
    () => ledgerViews([...lines].sort((a, b) => b.seq - a.seq)),
    [lines],
  );
  const state = listState({ isPending: query.isPending, error: query.error, count: rows.length });
  const download = useMutation({
    mutationFn: () =>
      exportCsv({
        data: {
          kind: "ledger",
          filters: { families: [], horizons: [], sides: [], symbols: [], rr: [] },
        },
      }),
    onSuccess: (result) => {
      const url = URL.createObjectURL(new Blob([result.csv], { type: "text/csv" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `paper-ledger-${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      URL.revokeObjectURL(url);
      toast(`Exported ${result.rows} rows${result.truncated ? " (first 50,000 only)" : ""}`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Export failed"),
  });
  return (
    <>
      <div className="mb-2 flex justify-end">
        <Button
          variant="outline"
          className={touch}
          onClick={() => download.mutate()}
          disabled={download.isPending}
        >
          <Download className="size-4" aria-hidden /> Export CSV
        </Button>
      </div>
      <ListStatus
        state={state}
        error={query.error}
        onRetry={() => void query.refetch()}
        empty={{
          title: "The ledger is empty",
          body: "Every fee, result, funding payment, liquidation, rejection and order step is written here, append-only.",
        }}
      >
        <div className="hidden sm:block overflow-x-auto" tabIndex={0} aria-label="Ledger">
          <table className="w-full min-w-[820px] text-left text-sm">
            <thead className="text-xs text-muted-foreground">
              <tr className="border-b border-border">
                <th scope="col" className={thRight}>
                  #
                </th>
                <th scope="col" className={th}>
                  When
                </th>
                <th scope="col" className={th}>
                  Entry
                </th>
                <th scope="col" className={th}>
                  Coin
                </th>
                <th scope="col" className={thRight}>
                  Amount
                </th>
                <th scope="col" className={thRight}>
                  Balance
                </th>
                <th scope="col" className={th}>
                  Detail
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.seq} className="border-b border-border/50 align-top">
                  <td className="num py-1.5 pr-3 text-right text-xs text-muted-foreground">
                    {row.seq}
                  </td>
                  <td className="num py-1.5 pr-3 text-xs">{row.timeText}</td>
                  <td className="py-1.5 pr-3">{row.typeLabel}</td>
                  <td className="num py-1.5 pr-3">{row.coin || "—"}</td>
                  <td className="py-1.5 pr-3 text-right">
                    <SignedText value={row.amount} />
                  </td>
                  <td className="num py-1.5 pr-3 text-right">{row.balance}</td>
                  <td className="py-1.5 text-xs text-muted-foreground">{row.detail || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <ul className="space-y-2 sm:hidden" aria-label="Ledger">
          {rows.map((row) => (
            <ExpandCard
              key={row.seq}
              label={`ledger line ${row.seq}`}
              head={
                <>
                  <span className="font-medium">{row.typeLabel}</span>
                  <span className="num text-xs text-muted-foreground">{row.coin}</span>
                  <span className="ml-auto">
                    <SignedText value={row.amount} />
                  </span>
                </>
              }
              summary={[
                ["#", String(row.seq)],
                ["Balance", row.balance],
                ["When", row.timeText.split(",")[0] ?? "—"],
              ]}
              details={[
                ["Time", row.timeText],
                ...(row.detail ? ([["Detail", row.detail]] as [string, string][]) : []),
                ...row.fields,
              ]}
            />
          ))}
        </ul>
        <LoadMore query={query} shown={rows.length} />
      </ListStatus>
    </>
  );
}

// ---------------------------------------------------------------- results

function Stat({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="min-w-0 rounded-lg border border-border/60 p-3">
      <dt className="text-xs text-muted-foreground">
        {hint ? <Hint text={hint}>{label}</Hint> : label}
      </dt>
      <dd className="num mt-1 truncate text-base font-semibold">{children}</dd>
    </div>
  );
}

function ResultsBlock({
  results,
  error,
  facts,
}: {
  results: ResultsView | null;
  error: string | null;
  facts: [string, string][];
}) {
  return (
    <section className="panel p-4 sm:p-5" aria-label="Results of this run">
      <SectionTitle
        title="Results of this run"
        hint={
          <>
            <Hint text={PAPER_TERMS.filter}>Filtering never recreates the wallet</Hint>: these
            totals cover every ledger line of the run.
          </>
        }
      />
      {error ? (
        <p role="alert" className="text-sm text-destructive">
          Totals could not be loaded: {error}
        </p>
      ) : !results ? (
        <div className="min-h-40 animate-pulse rounded-md bg-muted/50" role="status">
          <span className="sr-only">Loading results…</span>
        </div>
      ) : results.completed === 0 && results.rejected === 0 ? (
        <EmptyState
          icon={FlaskConical}
          title="No results yet"
          body="Totals appear after the first trade closes or is turned down."
          className="min-h-40 py-6"
        />
      ) : (
        <>
          <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="Completed">{results.completed}</Stat>
            <Stat label="Rejected">{results.rejected}</Stat>
            <Stat label="Target hit">{results.target}</Stat>
            <Stat label="Stopped">{results.stopped}</Stat>
            <Stat label="Liquidated">{results.liquidated}</Stat>
            <Stat label="Unclear" hint={PAPER_TERMS.ambiguous}>
              {results.ambiguous}
            </Stat>
            <Stat label="Time limit">{results.timeLimit}</Stat>
            <Stat label="Closed early">{results.unresolved}</Stat>
            <Stat label="Expectancy" hint={PAPER_TERMS.expectancy}>
              <SignedText value={results.expectancy} />
            </Stat>
            <Stat label="Profit factor" hint={PAPER_TERMS.profitFactor}>
              {results.profitFactor}
            </Stat>
            <Stat label="Max drawdown" hint={PAPER_TERMS.maxDrawdown}>
              {results.maxDrawdown}
            </Stat>
            <Stat label="Margin utilization" hint={PAPER_TERMS.marginUtilization}>
              {results.marginUtilization}
            </Stat>
            <Stat label="Fees" hint={PAPER_TERMS.fees}>
              <SignedText value={results.fees} />
            </Stat>
            <Stat label="Funding" hint={PAPER_TERMS.funding}>
              <SignedText value={results.funding} />
            </Stat>
            <Stat label="Slippage" hint={PAPER_TERMS.slippage}>
              {results.slippage}
            </Stat>
            <Stat label="Liquidation losses" hint={PAPER_TERMS.liquidation}>
              <SignedText value={results.liquidation} />
            </Stat>
          </dl>
          <p className="mt-2 text-xs text-muted-foreground">
            Few trades swing a lot: expectancy and profit factor mean little before dozens of closed
            trades.
          </p>
        </>
      )}
      {facts.length ? (
        <details className="mt-3 text-xs text-muted-foreground">
          <summary className="flex min-h-11 cursor-pointer items-center text-foreground/80 sm:min-h-0">
            Run configuration (frozen for this run)
          </summary>
          <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
            {facts.map(([name, value]) => (
              <div key={name}>
                <dt>{name}</dt>
                <dd className="num text-foreground">{value}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------- the page body

export function PaperAccountView() {
  const now = useNow(60_000);
  const load = useServerFn(getPaperAccount);
  // Same cadence as the Strategy Lab dashboard: one read a minute, nothing faster.
  const data = useQuery({
    queryKey: ["paper-account"],
    queryFn: async () => (await load()) as unknown as PaperAccountData,
    refetchInterval: 60_000,
  });
  const loaded = data.data;
  const model = useMemo(() => {
    const run = loaded?.run ?? null;
    const positions = openPositionViews(
      run?.wallet_state ?? null,
      loaded?.quotes ?? [],
      now,
      loaded?.positionMeta ?? {},
    );
    const account = accountView(run, loaded?.summary ?? null, positions, now);
    const equity = equityView(loaded?.equity ?? [], account.equityUsdt, now);
    return {
      run,
      positions,
      account,
      equity,
      results: resultsView(loaded?.summary ?? null, run?.wallet_state ?? null, equity),
    };
  }, [loaded, now]);
  const boundary = model.run?.boundary_ms ?? null;
  const accountId = loaded?.accountId ?? null;
  const [tab, setTab] = useState("positions");

  if (data.error && !loaded)
    return (
      <div role="alert" className="panel min-h-40 space-y-3 p-4 text-sm sm:p-5">
        <p className="text-destructive">
          The paper account could not be loaded: {data.error.message}
        </p>
        <Button variant="outline" className={touch} onClick={() => void data.refetch()}>
          <RefreshCw className="size-4" aria-hidden /> Try again
        </Button>
      </div>
    );
  const pending = data.isPending;
  if (!pending && model.account.status === "empty")
    return (
      <EmptyState
        icon={Wallet}
        title="No paper wallet yet"
        body={
          <>
            The wallet starts with the first hourly run of the{" "}
            <Link
              to="/forward"
              search={{ families: [], horizons: [], sides: [], symbols: [], rr: [] }}
              className="underline"
            >
              Strategy Lab
            </Link>
            . Until then there is nothing to show.
          </>
        }
      />
    );
  return (
    <div className="space-y-6">
      {pending ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8" role="status">
          <span className="sr-only">Loading the account…</span>
          {Array.from({ length: 8 }, (_, index) => (
            <div key={index} className="h-20 animate-pulse rounded-lg bg-muted/50" aria-hidden />
          ))}
        </div>
      ) : (
        <>
          <StatusLine
            view={model.account}
            now={now}
            fetching={data.isFetching}
            onRefresh={() => void data.refetch()}
          />
          <AccountStrip view={model.account} />
          {model.account.note ? (
            <p className="text-xs text-muted-foreground">{model.account.note}</p>
          ) : null}
        </>
      )}
      <EquityPanel view={model.equity} state={pending ? "loading" : "ready"} />
      <Tabs value={tab} onValueChange={setTab}>
        <div className="sticky top-28 z-20 -mx-4 border-b border-border/60 bg-background/95 px-4 py-2 backdrop-blur sm:top-14">
          <TabsList className="h-auto w-full justify-start gap-1 bg-muted/60 p-1">
            {(
              [
                ["positions", "Open", "Open positions", model.positions.length],
                ["trades", "Closed", "Closed trades", loaded?.summary?.trades ?? null],
                [
                  "rejected",
                  "Rejected",
                  "Rejected",
                  loaded?.summary ? loaded.summary.rejected + loaded.summary.order_rejected : null,
                ],
                ["ledger", "Ledger", "Ledger", loaded?.summary?.lines ?? null],
              ] as const
            ).map(([value, short, long, count]) => (
              <TabsTrigger
                key={value}
                value={value}
                className="min-h-11 flex-1 gap-1 px-2 sm:min-h-9 sm:flex-none sm:px-3"
              >
                <span className="sm:hidden">{short}</span>
                <span className="hidden sm:inline">{long}</span>
                {count !== null ? (
                  <span className="num text-xs text-muted-foreground">{count}</span>
                ) : null}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        <TabsContent value="positions" className="mt-3">
          <ListStatus
            state={listState({
              isPending: pending,
              error: data.error,
              count: model.positions.length,
            })}
            error={data.error}
            onRetry={() => void data.refetch()}
            empty={{
              title: "The wallet is flat",
              body: "A position appears here while a paper trade is open.",
            }}
          >
            <PositionsTab positions={model.positions} />
          </ListStatus>
        </TabsContent>
        <TabsContent value="trades" className="mt-3">
          <TradesTab accountId={accountId} boundary={boundary} />
        </TabsContent>
        <TabsContent value="rejected" className="mt-3">
          <RejectedTab accountId={accountId} boundary={boundary} />
        </TabsContent>
        <TabsContent value="ledger" className="mt-3">
          <LedgerTab accountId={accountId} boundary={boundary} />
        </TabsContent>
      </Tabs>
      <ResultsBlock
        results={model.results}
        error={loaded?.summaryError ?? null}
        facts={runFacts(model.run?.wallet_config ?? null)}
      />
    </div>
  );
}
