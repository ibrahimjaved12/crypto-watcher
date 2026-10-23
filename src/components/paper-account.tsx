import { Link } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import {
  AlertTriangle,
  Ban,
  CircleQuestionMark,
  CircleSlash,
  Coins,
  Download,
  Gauge,
  Inbox,
  OctagonX,
  PiggyBank,
  RefreshCw,
  ShieldAlert,
  Target,
  Timer,
  TrendingUp,
  Wallet,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { Hint } from "@/components/hint";
import {
  AccountGrid,
  CardList,
  DataTable,
  FreshnessPill,
  HeroDesktop,
  HeroMobile,
  HeroSkeleton,
  ItemCard,
  ListShell,
  LoadMore,
  MetricRail,
  MetricRailSkeleton,
  PaperTabBar,
  ReasonChip,
  RefreshButton,
  ResultsPanel,
  SideTag,
  SignedCapsule,
  SignedText,
  StateScreen,
  SymbolPill,
  Th,
  outcomeSlices,
  td,
  tdNum,
  type Metric,
  type PillState,
} from "@/components/paper/paper-kit";
import { usePagedLines, type PageLoader } from "@/components/paper/use-paged-lines";
import { PageHeader } from "@/components/plain";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { exportForwardLogCsv } from "@/lib/forward.functions";
import { strategyLabel } from "@/lib/labels";
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
  e8ToUsdt,
  equityView,
  formatPercent,
  formatUsdt,
  ledgerViews,
  listState,
  openPositionViews,
  rejectedViews,
  resultsView,
  runFacts,
  signedE8,
  signedUsdt,
  type AccountView,
  type ClosedTradeView,
  type OutcomeKey,
  type PositionView,
  type Tone,
} from "@/lib/paper/paper-account";
import { useMarket } from "@/lib/market/market-context";
import type { PaperAccountData } from "@/lib/paper/paper-data";
import { balanceSeries, paperKeys } from "@/lib/paper/paper-view";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

/**
 * Crypto Paper account page body (#36 Part 3; redesigned on the shared paper kit in #454). Phone, tablet and
 * desktop layouts are described in `paper/paper-kit.tsx`; the numbers and their rules are unchanged
 * (`lib/paper/paper-account.ts`).
 */

const OUTCOME_STYLE: Record<OutcomeKey, { icon: LucideIcon; tone: Tone }> = {
  target: { icon: Target, tone: "gain" },
  stopped: { icon: OctagonX, tone: "loss" },
  liquidated: { icon: Zap, tone: "loss" },
  time_limit: { icon: Timer, tone: "flat" },
  ambiguous: { icon: CircleQuestionMark, tone: "flat" },
  unresolved: { icon: CircleSlash, tone: "flat" },
};
const FRESH_PILL: Record<AccountView["freshness"], PillState> = {
  fresh: "fresh",
  delayed: "delayed",
  stale: "stale",
  none: "waiting",
};
const strategyName = (id: string | null) => (id ? strategyLabel(id).name : "—");
const horizonText = (min: number | null) => (min === null ? "" : min >= 60 ? `${min / 60}h` : `${min}m`);

export function SimulatedBanner() {
  return (
    <p
      role="note"
      className="flex items-center gap-2 rounded-full bg-warn/8 px-3 py-1.5 text-xs text-muted-foreground ring-1 ring-warn/25"
    >
      <ShieldAlert className="size-3.5 shrink-0 text-warn" aria-hidden />
      <span>
        <strong className="font-medium text-foreground/90">
          <Hint text={PAPER_TERMS.simulated}>Fake money.</Hint>
        </strong>{" "}
        Real orders are always placed by you.
      </span>
    </p>
  );
}

/** `plain` drops the tooltip trigger, for chips inside a tappable card (no button inside a button). */
function OutcomeChip({ trade, plain = false }: { trade: ClosedTradeView; plain?: boolean }) {
  const style = OUTCOME_STYLE[trade.outcome];
  return (
    <ReasonChip icon={style.icon} label={trade.outcomeLabel} tone={style.tone} hint={plain ? undefined : trade.outcomeHelp} />
  );
}

// ---------------------------------------------------------------- open positions

const markSourceText = (p: PositionView) =>
  p.markSource === "wallet-mark" ? "wallet mark" : p.markSource === "published-price" ? "published price" : "no price";
const strategyLine = (p: PositionView) =>
  [strategyName(p.strategyId), horizonText(p.horizonMin)].filter(Boolean).join(" · ");

function PositionsList({ positions }: { positions: PositionView[] }) {
  return (
    <>
      <DataTable
        label="Open positions"
        head={
          <>
            <Th>Position</Th>
            <Th right>Quantity</Th>
            <Th right>Entry → mark</Th>
            <Th right>
              <Hint text={PAPER_TERMS.unrealized}>Unrealized</Hint>
            </Th>
            <Th right>Margin · lev.</Th>
            <Th right>
              <Hint text={PAPER_TERMS.liquidationDistance}>Liquidation</Hint>
            </Th>
            <Th right>Stop / target</Th>
          </>
        }
      >
        {positions.map((p) => (
          <tr key={p.id}>
            <td className={td}>
              <span className="flex items-center gap-2">
                <SymbolPill symbol={p.coin} />
                <SideTag side={p.side} />
              </span>
              <span className="mt-1 block text-[11px] text-muted-foreground">{strategyLine(p)}</span>
            </td>
            <td className={tdNum}>
              {p.quantity}
              <span className="block text-[11px] text-muted-foreground">{p.notional} USDT</span>
            </td>
            <td className={tdNum}>
              {p.entry} → {p.mark}
              <span className="block text-[11px] text-muted-foreground">
                <Hint text={PAPER_TERMS.markPrice}>{markSourceText(p)}</Hint>
                {p.markStale ? (
                  <span className="ml-1 inline-flex items-center gap-0.5 text-warn">
                    <AlertTriangle className="size-3" aria-hidden /> stale
                  </span>
                ) : null}
              </span>
            </td>
            <td className={cn(tdNum, "font-medium")}>
              <SignedText value={p.unrealized} />
            </td>
            <td className={tdNum}>
              {p.margin}
              <span className="block text-[11px] text-muted-foreground">
                {p.leverage}
                {p.atCap ? " · at cap" : ""}
              </span>
            </td>
            <td className={tdNum}>
              {p.liquidation}
              <span className="block text-[11px] text-bear/90">▼ {p.liquidationDistanceText} away</span>
            </td>
            <td className={tdNum}>
              <span className="text-bear/90">{p.stop}</span>
              <span className="block text-[11px] text-bull/90">{p.target}</span>
            </td>
          </tr>
        ))}
      </DataTable>
      <CardList label="Open positions">
        {positions.map((p) => (
          <ItemCard
            key={p.id}
            label={`${p.coin} ${p.sideLabel}`}
            lead={
              <>
                <SymbolPill symbol={p.coin} />
                <SideTag side={p.side} />
              </>
            }
            result={<SignedText value={p.unrealized} />}
            meta={
              <span className="num truncate">
                {p.quantity} · {p.entry} → {p.mark}
              </span>
            }
            subResult={<span className="num text-bear/90">▼ {p.liquidationDistanceText} to liq.</span>}
            details={[
              ["Strategy", strategyLine(p)],
              ["Mark source", `${markSourceText(p)}${p.markStale ? " (stale)" : ""}`],
              ["Margin (USDT)", p.margin],
              ["Leverage", p.atCap ? `${p.leverage} (at the cap)` : p.leverage],
              ["Liquidation", p.liquidation],
              ["Notional (USDT)", p.notional],
              ["Stop", p.stop],
              ["Target", p.target],
              ["Opened", p.openedText],
            ]}
          />
        ))}
      </CardList>
    </>
  );
}

// ---------------------------------------------------------------- closed trades, rejected, ledger

type ListProps = { accountId: string | null; boundary: number | null };

function TradesList({ accountId, boundary }: ListProps) {
  const load = useServerFn(getPaperTrades) as unknown as PageLoader;
  const { query, lines, strategies } = usePagedLines("crypto", "trades", load, accountId, boundary);
  const trades = useMemo(() => closedTradeViews(lines, strategies), [lines, strategies]);
  return (
    <ListShell
      state={listState({ isPending: query.isPending, error: query.error, count: trades.length })}
      error={query.error}
      onRetry={() => void query.refetch()}
      empty={{ icon: Inbox, title: "No closed trades yet" }}
    >
      <DataTable
        label="Closed trades"
        head={
          <>
            <Th>Closed</Th>
            <Th>Trade</Th>
            <Th>Result</Th>
            <Th right>Gross</Th>
            <Th right>
              <Hint text={PAPER_TERMS.fees}>Fees</Hint>
            </Th>
            <Th right>
              <Hint text={PAPER_TERMS.funding}>Funding</Hint>
            </Th>
            <Th right>Net</Th>
            <Th right>Held</Th>
          </>
        }
      >
        {trades.map((t) => (
          <tr key={t.id}>
            <td className={cn(td, "num text-xs text-muted-foreground")}>{t.closedText}</td>
            <td className={td}>
              <SymbolPill symbol={t.coin} />
              <span className="mt-1 block text-[11px] text-muted-foreground">{strategyName(t.strategyId)}</span>
            </td>
            <td className={td}>
              <OutcomeChip trade={t} />
            </td>
            <td className={tdNum}>
              <SignedText value={t.gross} />
            </td>
            <td className={tdNum}>
              <SignedText value={t.fees} />
            </td>
            <td className={tdNum}>
              <SignedText value={t.funding} />
            </td>
            <td className={cn(tdNum, "font-semibold")}>
              <SignedText value={t.net} />
            </td>
            <td className={cn(tdNum, "text-muted-foreground")}>{t.holdingText}</td>
          </tr>
        ))}
      </DataTable>
      <CardList label="Closed trades">
        {trades.map((t) => (
          <ItemCard
            key={t.id}
            label={`${t.coin} trade`}
            lead={
              <>
                <SymbolPill symbol={t.coin} />
                <OutcomeChip trade={t} plain />
              </>
            }
            result={<SignedText value={t.net} />}
            meta={
              <span className="num truncate">
                {t.closedText.split(",")[0]} · {t.holdingText}
              </span>
            }
            details={[
              ["Closed", t.closedText],
              ["Strategy", strategyName(t.strategyId)],
              ["Gross", <SignedText key="g" value={t.gross} />],
              ["Fees", <SignedText key="f" value={t.fees} />],
              ["Funding", <SignedText key="u" value={t.funding} />],
              ["Ledger lines", String(t.lines)],
            ]}
          />
        ))}
      </CardList>
      <LoadMore query={query} shown={trades.length} />
    </ListShell>
  );
}

function RejectedList({ accountId, boundary }: ListProps) {
  const load = useServerFn(getPaperRejected) as unknown as PageLoader;
  const { query, lines, strategies } = usePagedLines("crypto", "rejected", load, accountId, boundary);
  const items = useMemo(() => rejectedViews(lines, strategies), [lines, strategies]);
  const kind = (k: string) => (k === "order" ? "Order" : k === "fill" ? "Fill" : "Setup");
  return (
    <ListShell
      state={listState({ isPending: query.isPending, error: query.error, count: items.length })}
      error={query.error}
      onRetry={() => void query.refetch()}
      empty={{ icon: Ban, title: "Nothing was turned down" }}
    >
      <DataTable
        label="Rejected"
        head={
          <>
            <Th>When</Th>
            <Th>Coin</Th>
            <Th>Strategy</Th>
            <Th>What</Th>
            <Th>Reason</Th>
          </>
        }
      >
        {items.map((item) => (
          <tr key={item.seq} className="text-muted-foreground">
            <td className={cn(td, "num text-xs")}>{item.timeText}</td>
            <td className={td}>
              <SymbolPill symbol={item.coin} className="opacity-80" />
            </td>
            <td className={cn(td, "text-xs")}>{strategyName(item.strategyId)}</td>
            <td className={cn(td, "text-xs")}>{kind(item.kind)}</td>
            <td className={cn(td, "text-xs text-foreground/80")}>
              <span className="inline-flex items-center gap-1.5">
                <Ban className="size-3.5 text-muted-foreground" aria-hidden />
                {item.reasonLabel}
              </span>
            </td>
          </tr>
        ))}
      </DataTable>
      <CardList label="Rejected">
        {items.map((item) => (
          <ItemCard
            key={item.seq}
            muted
            label={`${item.coin} rejected`}
            lead={
              <>
                <SymbolPill symbol={item.coin} className="opacity-80" />
                <span className="truncate text-sm text-foreground/80">{item.reasonLabel}</span>
              </>
            }
            meta={
              <span className="truncate">
                {kind(item.kind)} · {strategyName(item.strategyId)}
              </span>
            }
            subResult={<span className="num">{item.timeText.split(",")[0]}</span>}
          />
        ))}
      </CardList>
      <LoadMore query={query} shown={items.length} />
    </ListShell>
  );
}

function useLedgerExport(market: "crypto" | "forex") {
  const exportCsv = useServerFn(exportForwardLogCsv);
  return useMutation({
    mutationFn: () =>
      exportCsv({
        data: {
          kind: "ledger",
          market,
          filters: { families: [], horizons: [], sides: [], symbols: [], rr: [] },
        },
      }),
    onSuccess: (result) => {
      const url = URL.createObjectURL(new Blob([result.csv], { type: "text/csv" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `paper-ledger-${market}-${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      URL.revokeObjectURL(url);
      toast(`Exported ${result.rows} rows${result.truncated ? " (first 50,000 only)" : ""}`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Export failed"),
  });
}

/** The ledger CSV export: on the tab bar on desktop, above the list on phones. */
export function ExportButton({ market, compact = false }: { market: "crypto" | "forex"; compact?: boolean }) {
  const download = useLedgerExport(market);
  return (
    <Button
      variant="outline"
      className={cn("rounded-full", compact ? "h-9" : "min-h-11 w-full")}
      onClick={() => download.mutate()}
      disabled={download.isPending}
    >
      <Download className="size-4" aria-hidden /> Export CSV
    </Button>
  );
}

function LedgerList({ accountId, boundary }: ListProps) {
  const load = useServerFn(getPaperLedger) as unknown as PageLoader;
  const { query, lines } = usePagedLines("crypto", "ledger", load, accountId, boundary);
  const rows = useMemo(() => ledgerViews([...lines].sort((a, b) => b.seq - a.seq)), [lines]);
  return (
    <>
      <div className="pt-3 lg:hidden">
        <ExportButton market="crypto" />
      </div>
      <ListShell
        state={listState({ isPending: query.isPending, error: query.error, count: rows.length })}
        error={query.error}
        onRetry={() => void query.refetch()}
        empty={{ icon: Inbox, title: "The ledger is empty" }}
      >
        <DataTable
          label="Ledger"
          head={
            <>
              <Th right>#</Th>
              <Th>When</Th>
              <Th>Entry</Th>
              <Th>Coin</Th>
              <Th right>Amount</Th>
              <Th right>Balance</Th>
              <Th>Detail</Th>
            </>
          }
        >
          {rows.map((row) => (
            <tr key={row.seq}>
              <td className={cn(tdNum, "text-xs text-muted-foreground")}>{row.seq}</td>
              <td className={cn(td, "num text-xs text-muted-foreground")}>{row.timeText}</td>
              <td className={td}>{row.typeLabel}</td>
              <td className={td}>{row.coin ? <SymbolPill symbol={row.coin} /> : "—"}</td>
              <td className={cn(tdNum, "font-medium")}>
                <SignedText value={row.amount} />
              </td>
              <td className={tdNum}>{row.balance}</td>
              <td className={cn(td, "text-xs text-muted-foreground")}>{row.detail || "—"}</td>
            </tr>
          ))}
        </DataTable>
        <CardList label="Ledger">
          {rows.map((row) => (
            <ItemCard
              key={row.seq}
              label={`ledger line ${row.seq}`}
              lead={
                <>
                  {row.coin ? <SymbolPill symbol={row.coin} /> : null}
                  <span className="truncate text-sm font-medium">{row.typeLabel}</span>
                </>
              }
              result={row.amount ? <SignedText value={row.amount} /> : null}
              meta={<span className="num truncate">{row.timeText}</span>}
              subResult={<span className="num">bal. {row.balance}</span>}
              details={[["Line", String(row.seq)], ...(row.detail ? ([["Detail", row.detail]] as [string, string][]) : []), ...row.fields]}
            />
          ))}
        </CardList>
        <LoadMore query={query} shown={rows.length} />
      </ListShell>
    </>
  );
}

// ---------------------------------------------------------------- the page body

const cardValue = (view: AccountView, key: string) => view.cards.find((card) => card.key === key);

function cryptoMetrics(view: AccountView, loaded: PaperAccountData | undefined): Metric[] {
  const state = loaded?.run?.wallet_state ?? null;
  const summary = loaded?.summary ?? null;
  const used = state ? e8ToUsdt(state.used_margin_e8) : null;
  const utilization = view.balanceUsdt && view.balanceUsdt > 0 && used !== null ? used / view.balanceUsdt : null;
  const realized = cardValue(view, "realized");
  const unrealized = cardValue(view, "unrealized");
  const totals = state?.totals ?? {};
  const fees = summary ? summary.open_fees_e8 + summary.close_fees_e8 : (totals.fees_e8 ?? null);
  const funding = summary ? summary.funding_e8 : (totals.funding_e8 ?? null);
  const costs = fees === null && funding === null ? null : signedE8((fees ?? 0) + (funding ?? 0));
  return [
    {
      key: "equity",
      icon: TrendingUp,
      label: "Equity",
      hint: PAPER_TERMS.equity,
      value: formatUsdt(view.equityUsdt),
      unit: "USDT",
      sub: view.unmarked
        ? `${view.unmarked} without a price`
        : unrealized
          ? `${unrealized.value} unrealized`
          : null,
    },
    {
      key: "margin",
      icon: Gauge,
      label: "Margin used",
      hint: PAPER_TERMS.marginUsed,
      value: formatUsdt(used),
      unit: "USDT",
      meter: utilization === null ? null : { fraction: utilization, warnAt: 0.5, label: "Margin used of balance" },
      sub: `${cardValue(view, "available")?.value ?? "—"} available`,
    },
    {
      key: "realized",
      icon: Coins,
      label: "Realized P&L",
      hint: PAPER_TERMS.realized,
      value: realized?.value ?? "—",
      tone: realized?.tone ?? null,
      sub: "before costs",
    },
    {
      key: "costs",
      icon: PiggyBank,
      label: "Costs",
      hint: `${PAPER_TERMS.fees}\n${PAPER_TERMS.funding}`,
      value: costs?.text ?? "—",
      tone: costs?.tone ?? null,
      sub: `fees ${cardValue(view, "fees")?.value ?? "—"} · funding ${cardValue(view, "funding")?.value ?? "—"}`,
    },
  ];
}

export function PaperAccountView() {
  const { market } = useMarket();
  const now = useNow(60_000);
  const load = useServerFn(getPaperAccount);
  // Same cadence as the Strategy Lab dashboard: one read a minute, nothing faster.
  const data = useQuery({
    queryKey: paperKeys.account("crypto"),
    queryFn: async () => (await load({ data: { market: "crypto" } })) as unknown as PaperAccountData,
    refetchInterval: 60_000,
  });
  const loaded = data.data;
  const model = useMemo(() => {
    const run = loaded?.run ?? null;
    const positions = openPositionViews(run?.wallet_state ?? null, loaded?.quotes ?? [], now, loaded?.positionMeta ?? {});
    const account = accountView(run, loaded?.summary ?? null, positions, now);
    const equity = equityView(loaded?.equity ?? [], account.equityUsdt, now);
    const initial = e8ToUsdt(run?.wallet_config?.["initial_balance_e8"]);
    return {
      run,
      positions,
      account,
      equity,
      curve: balanceSeries(loaded?.equity ?? [], now),
      open: signedUsdt(positions.reduce((sum, p) => sum + (p.unrealizedUsdt ?? 0), 0)),
      total: account.balanceUsdt !== null && initial !== null ? signedUsdt(account.balanceUsdt - initial) : null,
      totalFraction:
        account.balanceUsdt !== null && initial !== null && initial > 0 ? (account.balanceUsdt - initial) / initial : null,
      results: resultsView(loaded?.summary ?? null, run?.wallet_state ?? null, equity),
    };
  }, [loaded, now]);
  const boundary = model.run?.boundary_ms ?? null;
  const accountId = loaded?.accountId ?? null;
  const [tab, setTab] = useState("positions");
  const pending = data.isPending;

  const header = (
    <PageHeader
      eyebrow="Simulated · fake money"
      title="Paper account"
      subtitle="The simulated futures wallet."
      actions={
        pending || model.account.status === "empty" ? null : (
          <>
            <FreshnessPill state={FRESH_PILL[model.account.freshness]} atMs={model.account.asOfMs} />
            <RefreshButton fetching={data.isFetching} onRefresh={() => void data.refetch()} />
          </>
        )
      }
    />
  );

  if (data.error && !loaded)
    return (
      <div className="space-y-5">
        {header}
        <StateScreen icon={AlertTriangle} title="Could not load the account" tone="bear">
          <Button variant="outline" className="min-h-11 rounded-full" onClick={() => void data.refetch()}>
            <RefreshCw className="size-4" aria-hidden /> Try again
          </Button>
        </StateScreen>
      </div>
    );
  if (!pending && model.account.status === "empty")
    return (
      <div className="space-y-5">
        {header}
        <StateScreen icon={Wallet} title="No paper wallet yet">
          <Link
            to="/$market/forward"
            params={{ market }}
            search={{ families: [], horizons: [], sides: [], symbols: [], rr: [] }}
            className="text-sm text-primary underline-offset-4 hover:underline"
          >
            Open the Strategy Lab
          </Link>
        </StateScreen>
      </div>
    );

  const hero = {
    label: "Balance",
    value: formatUsdt(model.account.balanceUsdt),
    unit: "USDT",
    capsules: (
      <>
        <SignedCapsule
          value={model.total}
          label="Total"
          suffix={model.totalFraction !== null ? `(${formatPercent(model.totalFraction)})` : undefined}
        />
        {model.account.unmarked === 0 && model.positions.length ? (
          <SignedCapsule value={model.open} label="Open" />
        ) : null}
      </>
    ),
    points: model.curve,
    nowMs: now,
    stale: model.account.status === "stale",
    footer: model.equity.maxDrawdown ? (
      <span className="num">
        <Hint text={PAPER_TERMS.maxDrawdown}>Max drawdown</Hint> ▼ −{formatUsdt(model.equity.maxDrawdown.usdt)} USDT (−
        {formatPercent(model.equity.maxDrawdown.fraction)})
      </span>
    ) : null,
  };
  return (
    <div className="space-y-5">
      {header}
      <SimulatedBanner />
      {pending ? (
        <AccountGrid hero={<HeroSkeleton />} rail={<MetricRailSkeleton />} />
      ) : (
        <>
          <AccountGrid
            hero={
              <>
                <HeroMobile {...hero} />
                <HeroDesktop {...hero} />
              </>
            }
            rail={<MetricRail label="Account" metrics={cryptoMetrics(model.account, loaded)} />}
          />
          {model.account.note ? <p className="text-xs text-muted-foreground">{model.account.note}</p> : null}
        </>
      )}
      <Tabs value={tab} onValueChange={setTab}>
        <PaperTabBar
          tabs={[
            { value: "positions", label: "Open", count: pending ? null : model.positions.length },
            { value: "trades", label: "Closed", count: loaded?.summary?.trades ?? null },
            {
              value: "rejected",
              label: "Rejected",
              count: loaded?.summary ? loaded.summary.rejected + loaded.summary.order_rejected : null,
            },
            { value: "ledger", label: "Ledger", count: loaded?.summary?.lines ?? null },
          ]}
          action={tab === "ledger" ? <ExportButton market="crypto" compact /> : null}
        />
        <TabsContent value="positions" className="mt-0">
          <ListShell
            state={listState({ isPending: pending, error: data.error, count: model.positions.length })}
            error={data.error}
            onRetry={() => void data.refetch()}
            empty={{ icon: Wallet, title: "No open positions" }}
          >
            <PositionsList positions={model.positions} />
          </ListShell>
        </TabsContent>
        <TabsContent value="trades" className="mt-0">
          <TradesList accountId={accountId} boundary={boundary} />
        </TabsContent>
        <TabsContent value="rejected" className="mt-0">
          <RejectedList accountId={accountId} boundary={boundary} />
        </TabsContent>
        <TabsContent value="ledger" className="mt-0">
          <LedgerList accountId={accountId} boundary={boundary} />
        </TabsContent>
      </Tabs>
      <ResultsPanel
        title="Results of this run"
        completed={model.results?.completed ?? 0}
        rejected={model.results?.rejected ?? 0}
        loading={!model.results && !loaded?.summaryError}
        error={loaded?.summaryError ?? null}
        slices={model.results ? outcomeSlices(model.results, "Liquidated", PAPER_TERMS.ambiguous) : []}
        quality={
          model.results
            ? [
                { label: "Expectancy", hint: PAPER_TERMS.expectancy, value: <SignedText value={model.results.expectancy} /> },
                { label: "Profit factor", hint: PAPER_TERMS.profitFactor, value: model.results.profitFactor },
                { label: "Max drawdown", hint: PAPER_TERMS.maxDrawdown, value: model.results.maxDrawdown },
                { label: "Margin use", hint: PAPER_TERMS.marginUtilization, value: model.results.marginUtilization },
              ]
            : []
        }
        costs={
          model.results
            ? [
                { label: "Fees", hint: PAPER_TERMS.fees, value: <SignedText value={model.results.fees} /> },
                { label: "Funding", hint: PAPER_TERMS.funding, value: <SignedText value={model.results.funding} /> },
                { label: "Slippage", hint: PAPER_TERMS.slippage, value: model.results.slippage },
                { label: "Liquidations", hint: PAPER_TERMS.liquidation, value: <SignedText value={model.results.liquidation} /> },
              ]
            : []
        }
        note="Few trades swing a lot: expectancy and profit factor mean little before dozens of closed trades. Filtering never recreates the wallet."
        facts={runFacts(model.run?.wallet_config ?? null)}
      />
    </div>
  );
}
