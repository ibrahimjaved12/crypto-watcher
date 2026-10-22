import { useQuery } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import {
  AlertTriangle,
  ArrowLeftRight,
  Ban,
  CircleQuestionMark,
  CircleSlash,
  FileX,
  Gauge,
  Hourglass,
  Inbox,
  Layers,
  ListX,
  Lock,
  LogIn,
  LogOut,
  Moon,
  OctagonX,
  PiggyBank,
  RefreshCw,
  Scale,
  ShieldAlert,
  Target,
  Timer,
  TrendingUp,
  Wallet,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useMemo, useState } from "react";

import { Hint } from "@/components/hint";
import { ExportButton, SimulatedBanner } from "@/components/paper-account";
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
} from "@/components/paper/paper-kit";
import { usePagedLines, type PageLoader } from "@/components/paper/use-paged-lines";
import { PageHeader } from "@/components/plain";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { strategyLabel } from "@/lib/labels";
import { useMarket } from "@/lib/market/market-context";
import { getPaperAccount, getPaperLedger, getPaperRejected, getPaperTrades } from "@/lib/paper.functions";
import {
  FX_CLOSE_REASONS,
  fxAccountView,
  fxClosedTradeViews,
  fxConfigFacts,
  fxLedgerViews,
  fxPageState,
  fxPositionViews,
  fxRejectedViews,
  fxRunFacts,
  reasonTone,
  type FxClosedTradeView,
  type FxConfigFacts,
  type FxIconKey,
  type FxPositionView,
  type FxWalletStateRaw,
} from "@/lib/paper/fx-paper";
import {
  PAPER_TERMS,
  equityView,
  formatPercent,
  formatUsdt,
  listState,
  resultsView,
  signedE8,
  type WalletStateRaw,
} from "@/lib/paper/paper-account";
import type { FxPaperAccountData } from "@/lib/paper/paper-data";
import { balanceSeries, paperKeys } from "@/lib/paper/paper-view";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

/**
 * Forex Paper account (#454): the forex paper account only, never summed with crypto. Same design kit and layout
 * as the crypto page (`paper-kit.tsx`), with the forex wallet's own facts: lots, bid/ask marks, stop-out, swap,
 * commission, R multiples and the forex rejection reasons. Honest states: no access, no account yet, no run yet
 * ("Waiting for forex data"), and a stale run shown with its time; zeros are never shown as results.
 */

/** Every icon key of `fx-paper.ts`; the Record type makes a missing one a compile error. */
export const FX_ICONS: Record<FxIconKey, LucideIcon> = {
  lot: Scale,
  exposure: Layers,
  positions: ListX,
  margin: Wallet,
  contract: FileX,
  blocked: Ban,
  leverage: Gauge,
  unknown: CircleQuestionMark,
  target: Target,
  stop: OctagonX,
  time: Timer,
  "stop-out": Zap,
  unclear: CircleQuestionMark,
  early: CircleSlash,
  "open-fee": LogIn,
  "close-fee": LogOut,
  result: ArrowLeftRight,
  swap: Moon,
  rejected: Ban,
};

const FX_TERMS = {
  equity:
    "Balance + unrealized P&L. Longs are valued at the published bid, shorts at the ask, converted to USD at the rate of the entry (exact for USD-quoted symbols).",
  margin: "Margin set aside for open positions (notional ÷ leverage, in USD). Free margin = balance − margin used.",
  openRisk:
    "What the open positions lose together if every stop is hit from its entry fill, before costs. Sized at 1 % of the balance per trade.",
  costs: "Commission per lot on every open and close, plus overnight swap (paid or received).",
  r: "Net result ÷ the 1 % risk budget at entry. A full stop is about −1 R plus costs; lots are rounded down, so it can be a little less.",
} as const;

const strategyName = (id: string | null) => (id ? strategyLabel(id).name : "—");
const horizonText = (min: number | null) => (min === null ? "" : min >= 60 ? `${min / 60}h` : `${min}m`);
const strategyLine = (id: string | null, horizon: number | null) =>
  [strategyName(id), horizonText(horizon)].filter(Boolean).join(" · ");

function CloseChip({ trade }: { trade: FxClosedTradeView }) {
  const reason = FX_CLOSE_REASONS[trade.reason];
  return <ReasonChip icon={FX_ICONS[reason.icon]} label={reason.label} tone={reasonTone(trade.reason)} hint={reason.help} />;
}

// ---------------------------------------------------------------- open positions

function PositionsList({ positions }: { positions: FxPositionView[] }) {
  const markWord = (p: FxPositionView) => (p.side === 1 ? "bid" : "ask");
  return (
    <>
      <DataTable
        label="Open positions"
        head={
          <>
            <Th>Position</Th>
            <Th right>Lots</Th>
            <Th right>Entry → mark</Th>
            <Th right>
              <Hint text={FX_TERMS.equity}>Unrealized</Hint>
            </Th>
            <Th right>Stop / target</Th>
            <Th right>Stop-out</Th>
            <Th right>Margin · lev.</Th>
            <Th right>
              <Hint text={FX_TERMS.openRisk}>Risk</Hint>
            </Th>
          </>
        }
      >
        {positions.map((p) => (
          <tr key={p.id}>
            <td className={td}>
              <span className="flex items-center gap-2">
                <SymbolPill symbol={p.symbol} />
                <SideTag side={p.side} />
              </span>
              <span className="mt-1 block text-[11px] text-muted-foreground">
                {strategyLine(p.strategyId, p.horizonMin)} · {p.heldText}
              </span>
            </td>
            <td className={tdNum}>{p.lotsText}</td>
            <td className={tdNum}>
              {p.entry} → {p.mark}
              <span className="block text-[11px] text-muted-foreground">
                {p.hasMark ? markWord(p) : "no price"}
                {p.markStale ? <span className="ml-1 text-warn">· delayed</span> : null}
              </span>
            </td>
            <td className={cn(tdNum, "font-medium")}>
              <SignedText value={p.unrealized} />
            </td>
            <td className={tdNum}>
              <span className="text-bear/90">{p.stop}</span>
              <span className="block text-[11px] text-bull/90">{p.target}</span>
            </td>
            <td className={cn(tdNum, "text-muted-foreground")}>{p.stopOut}</td>
            <td className={tdNum}>
              {p.margin}
              <span className="block text-[11px] text-muted-foreground">{p.leverage}</span>
            </td>
            <td className={tdNum}>{p.openRisk}</td>
          </tr>
        ))}
      </DataTable>
      <CardList label="Open positions">
        {positions.map((p) => (
          <ItemCard
            key={p.id}
            label={`${p.symbol} ${p.sideLabel}`}
            lead={
              <>
                <SymbolPill symbol={p.symbol} />
                <SideTag side={p.side} />
              </>
            }
            result={<SignedText value={p.unrealized} />}
            meta={
              <span className="num truncate">
                {p.lotsText} lots · {p.entry} → {p.mark}
              </span>
            }
            subResult={<span className="num">risk {p.openRisk}</span>}
            details={[
              ["Strategy", strategyLine(p.strategyId, p.horizonMin)],
              ["Mark", p.hasMark ? `${markWord(p)}${p.markStale ? " (delayed)" : ""}` : "no price"],
              ["Stop", p.stop],
              ["Target", p.target],
              ["Stop-out", p.stopOut],
              ["Margin (USD)", p.margin],
              ["Leverage", p.leverage],
              ["Opened", p.openedText],
            ]}
          />
        ))}
      </CardList>
    </>
  );
}

// ---------------------------------------------------------------- closed, rejected, ledger

type ListProps = {
  accountId: string | null;
  boundary: number | null;
  config: Pick<FxConfigFacts, "riskFraction" | "contractSizes">;
  digits: Record<string, number>;
};

function TradesList({ accountId, boundary, config, digits }: ListProps) {
  const load = useServerFn(getPaperTrades) as unknown as PageLoader;
  const { query, lines, facts } = usePagedLines("forex", "trades", load, accountId, boundary);
  const trades = useMemo(() => fxClosedTradeViews(lines, facts, config, digits), [lines, facts, config, digits]);
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
            <Th right>Lots</Th>
            <Th right>Entry → exit</Th>
            <Th>Reason</Th>
            <Th right>
              <Hint text={FX_TERMS.r}>Net R</Hint>
            </Th>
            <Th right>Net USD</Th>
            <Th right>Held</Th>
          </>
        }
      >
        {trades.map((t) => (
          <tr key={t.id}>
            <td className={cn(td, "num text-xs text-muted-foreground")}>{t.closedText}</td>
            <td className={td}>
              <span className="flex items-center gap-2">
                <SymbolPill symbol={t.symbol} />
                <SideTag side={t.side} />
              </span>
              <span className="mt-1 block text-[11px] text-muted-foreground">{strategyLine(t.strategyId, t.horizonMin)}</span>
            </td>
            <td className={tdNum}>{t.lotsText}</td>
            <td className={tdNum}>
              {t.entry} → {t.exit}
            </td>
            <td className={td}>
              <CloseChip trade={t} />
            </td>
            <td className={tdNum}>
              <SignedText value={t.netR} />
            </td>
            <td className={cn(tdNum, "font-semibold")}>
              <SignedText value={t.net} />
              <span className="block text-[11px] font-normal text-muted-foreground">
                costs {t.costs?.text ?? "—"} · swap {t.swap?.text ?? "—"}
              </span>
            </td>
            <td className={cn(tdNum, "text-muted-foreground")}>{t.heldText}</td>
          </tr>
        ))}
      </DataTable>
      <CardList label="Closed trades">
        {trades.map((t) => {
          const reason = FX_CLOSE_REASONS[t.reason];
          const Icon = FX_ICONS[reason.icon];
          return (
            <ItemCard
              key={t.id}
              label={`${t.symbol} trade`}
              lead={
                <>
                  <SymbolPill symbol={t.symbol} />
                  <SideTag side={t.side} />
                </>
              }
              result={<SignedText value={t.net} />}
              meta={
                <>
                  <Icon className="size-3.5 shrink-0" aria-hidden />
                  <span className="num truncate">
                    {reason.label} · {t.lotsText} lots · {t.heldText}
                  </span>
                </>
              }
              subResult={<SignedText value={t.netR} />}
              details={[
                ["Closed", t.closedText],
                ["Strategy", strategyLine(t.strategyId, t.horizonMin)],
                ["Entry → exit", `${t.entry} → ${t.exit}`],
                ["Gross", <SignedText key="g" value={t.gross} />],
                ["Commission", <SignedText key="c" value={t.costs} />],
                ["Swap", <SignedText key="s" value={t.swap} />],
              ]}
            />
          );
        })}
      </CardList>
      <LoadMore query={query} shown={trades.length} />
    </ListShell>
  );
}

function RejectedList({ accountId, boundary }: Pick<ListProps, "accountId" | "boundary">) {
  const load = useServerFn(getPaperRejected) as unknown as PageLoader;
  const { query, lines, facts } = usePagedLines("forex", "rejected", load, accountId, boundary);
  const items = useMemo(() => fxRejectedViews(lines, facts), [lines, facts]);
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
            <Th>Setup</Th>
            <Th>Strategy</Th>
            <Th>Reason</Th>
          </>
        }
      >
        {items.map((item) => {
          const Icon = FX_ICONS[item.icon];
          return (
            <tr key={item.seq} className="text-muted-foreground">
              <td className={cn(td, "num text-xs")}>{item.timeText}</td>
              <td className={td}>
                <span className="flex items-center gap-2 opacity-85">
                  <SymbolPill symbol={item.symbol} />
                  <SideTag side={item.side} />
                </span>
              </td>
              <td className={cn(td, "text-xs")}>{strategyName(item.strategyId)}</td>
              <td className={cn(td, "text-xs text-foreground/80")}>
                <Hint text={item.help}>
                  <span className="inline-flex items-center gap-1.5">
                    <Icon className="size-3.5 text-muted-foreground" aria-hidden />
                    {item.label}
                  </span>
                </Hint>
              </td>
            </tr>
          );
        })}
      </DataTable>
      <CardList label="Rejected">
        {items.map((item) => {
          const Icon = FX_ICONS[item.icon];
          return (
            <ItemCard
              key={item.seq}
              muted
              label={`${item.symbol} rejected`}
              lead={
                <>
                  <SymbolPill symbol={item.symbol} className="opacity-85" />
                  <span className="flex min-w-0 items-center gap-1.5 text-sm text-foreground/80">
                    <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                    <span className="truncate">{item.label}</span>
                  </span>
                </>
              }
              meta={<span className="truncate">{strategyName(item.strategyId)}</span>}
              subResult={<span className="num">{item.timeText.split(",")[0]}</span>}
              details={[
                ["Why", item.help],
                ["When", item.timeText],
              ]}
            />
          );
        })}
      </CardList>
      <LoadMore query={query} shown={items.length} />
    </ListShell>
  );
}

function LedgerList({ accountId, boundary }: Pick<ListProps, "accountId" | "boundary">) {
  const load = useServerFn(getPaperLedger) as unknown as PageLoader;
  const { query, lines } = usePagedLines("forex", "ledger", load, accountId, boundary);
  const rows = useMemo(() => fxLedgerViews([...lines].sort((a, b) => b.seq - a.seq)), [lines]);
  return (
    <>
      <div className="pt-3 lg:hidden">
        <ExportButton market="forex" />
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
              <Th>Symbol</Th>
              <Th right>Amount</Th>
              <Th right>Balance</Th>
              <Th>Detail</Th>
            </>
          }
        >
          {rows.map((row) => {
            const Icon = FX_ICONS[row.icon];
            return (
              <tr key={row.seq} className={cn(row.type === "rejected" && "text-muted-foreground")}>
                <td className={cn(tdNum, "text-xs text-muted-foreground")}>{row.seq}</td>
                <td className={cn(td, "num text-xs text-muted-foreground")}>{row.timeText}</td>
                <td className={td}>
                  <span className="inline-flex items-center gap-1.5">
                    <Icon className="size-3.5 text-primary/80" aria-hidden />
                    {row.label}
                  </span>
                </td>
                <td className={td}>{row.symbol ? <SymbolPill symbol={row.symbol} /> : "—"}</td>
                <td className={cn(tdNum, "font-medium")}>
                  <SignedText value={row.amount} />
                </td>
                <td className={tdNum}>{row.balance}</td>
                <td className={cn(td, "text-xs text-muted-foreground")}>{row.detail || "—"}</td>
              </tr>
            );
          })}
        </DataTable>
        <CardList label="Ledger">
          {rows.map((row) => {
            const Icon = FX_ICONS[row.icon];
            return (
              <ItemCard
                key={row.seq}
                muted={row.type === "rejected"}
                label={`ledger line ${row.seq}`}
                lead={
                  <>
                    <Icon className="size-4 shrink-0 text-primary/80" aria-hidden />
                    <span className="truncate text-sm font-medium">{row.label}</span>
                    {row.symbol ? <SymbolPill symbol={row.symbol} /> : null}
                  </>
                }
                result={row.amount ? <SignedText value={row.amount} /> : null}
                meta={
                  <span className="num truncate">
                    {row.timeText}
                    {row.detail ? ` · ${row.detail}` : ""}
                  </span>
                }
                subResult={<span className="num">bal. {row.balance}</span>}
              />
            );
          })}
        </CardList>
        <LoadMore query={query} shown={rows.length} />
      </ListShell>
    </>
  );
}

// ---------------------------------------------------------------- the page body

export function FxPaperAccountView() {
  const now = useNow(60_000);
  const { ready, canOpen } = useMarket();
  const entitled = canOpen("forex");
  const load = useServerFn(getPaperAccount);
  const data = useQuery({
    queryKey: paperKeys.account("forex"),
    queryFn: async () => (await load({ data: { market: "forex" } })) as unknown as FxPaperAccountData,
    enabled: ready && entitled,
    refetchInterval: 60_000,
  });
  const loaded = data.data;
  const [tab, setTab] = useState("positions");
  const model = useMemo(() => {
    const run = loaded?.run ?? null;
    const state = (run?.wallet_state ?? null) as FxWalletStateRaw | null;
    const config = fxConfigFacts(run?.wallet_config ?? loaded?.accountConfig ?? null);
    const positions = fxPositionViews(state, loaded?.quotes ?? [], now, loaded?.positionMeta ?? {});
    const account = fxAccountView(state, positions, {
      initialBalanceE8: config.initialBalanceE8,
      dayStartBalanceE8: loaded?.dayStartBalanceE8 ?? null,
    });
    const equity = equityView(loaded?.equity ?? [], account.equityUsd, now);
    const summary = loaded?.summary ?? null;
    return {
      run,
      config,
      positions,
      account,
      equity,
      curve: balanceSeries(loaded?.equity ?? [], now),
      digits: Object.fromEntries((loaded?.quotes ?? []).map((quote) => [quote.symbol, quote.digits])),
      commission: summary ? signedE8(summary.open_fees_e8 + summary.close_fees_e8) : null,
      swap: summary ? signedE8(summary.funding_e8) : null,
      costs: summary ? signedE8(summary.open_fees_e8 + summary.close_fees_e8 + summary.funding_e8) : null,
      results: resultsView(summary, state as unknown as WalletStateRaw | null, equity),
    };
  }, [loaded, now]);
  const state = fxPageState({
    access: ready ? "ready" : "loading",
    entitled,
    loaded: loaded !== undefined,
    error: data.isError,
    accountId: loaded?.accountId ?? null,
    boundaryMs: model.run?.boundary_ms ?? null,
    nowMs: now,
  });

  const header = (
    <PageHeader
      eyebrow="Simulated · fake money"
      title="Paper account"
      subtitle="The simulated forex wallet, in USD."
      actions={
        state === "ready" || state === "stale" ? (
          <>
            <FreshnessPill state={state === "stale" ? "stale" : "fresh"} atMs={model.run?.boundary_ms ?? null} />
            <RefreshButton fetching={data.isFetching} onRefresh={() => void data.refetch()} />
          </>
        ) : state === "waiting" ? (
          <FreshnessPill state="waiting" atMs={null} waiting="Waiting for forex data" />
        ) : null
      }
    />
  );

  if (state === "no_access")
    return (
      <div className="space-y-5">
        {header}
        <StateScreen icon={Lock} title="No forex access" tone="muted" />
      </div>
    );
  if (state === "error")
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
  if (state === "no_account")
    return (
      <div className="space-y-5">
        {header}
        <StateScreen icon={Wallet} title="No forex account yet" tone="muted" />
      </div>
    );
  if (state === "waiting")
    return (
      <div className="space-y-5">
        {header}
        <StateScreen icon={Hourglass} title="No runs yet" tone="muted" />
      </div>
    );
  if (state === "loading")
    return (
      <div className="space-y-5">
        {header}
        <AccountGrid hero={<HeroSkeleton />} rail={<MetricRailSkeleton />} />
      </div>
    );

  const a = model.account;
  const metrics: Metric[] = [
    {
      key: "equity",
      icon: TrendingUp,
      label: "Equity",
      hint: FX_TERMS.equity,
      value: formatUsdt(a.equityUsd),
      unit: "USD",
      sub: a.unmarked ? `${a.unmarked} without a price` : a.unrealized ? `${a.unrealized.text} unrealized` : null,
    },
    {
      key: "margin",
      icon: Gauge,
      label: "Margin used",
      hint: FX_TERMS.margin,
      value: formatUsdt(a.usedMarginUsd),
      unit: "USD",
      meter: a.utilization === null ? null : { fraction: a.utilization, warnAt: 0.5, label: "Margin used of balance" },
      sub: `${formatUsdt(a.freeMarginUsd)} free`,
    },
    {
      key: "risk",
      icon: ShieldAlert,
      label: "Open risk",
      hint: FX_TERMS.openRisk,
      value: formatUsdt(a.openRiskUsd),
      unit: "USD",
      meter:
        a.openRiskFraction === null ? null : { fraction: a.openRiskFraction, warnAt: 0.05, label: "Open risk of balance" },
      sub: `${a.openPositions} open${a.openRiskFraction !== null ? ` · ${formatPercent(a.openRiskFraction, 1)} of balance` : ""}`,
    },
    {
      key: "costs",
      icon: PiggyBank,
      label: "Costs",
      hint: FX_TERMS.costs,
      value: model.costs?.text ?? "—",
      tone: model.costs?.tone ?? null,
      sub: `commission ${model.commission?.text ?? "—"} · swap ${model.swap?.text ?? "—"}`,
    },
  ];
  const hero = {
    label: "Balance",
    value: formatUsdt(a.balanceUsd),
    unit: "USD",
    capsules: (
      <>
        <SignedCapsule
          value={a.total}
          label="Total"
          suffix={a.totalFraction !== null ? `(${formatPercent(a.totalFraction)})` : undefined}
        />
        <SignedCapsule value={a.today} label="Today" />
      </>
    ),
    points: model.curve,
    nowMs: now,
    stale: state === "stale",
    footer: model.equity.maxDrawdown ? (
      <span className="num">
        <Hint text={PAPER_TERMS.maxDrawdown}>Max drawdown</Hint> ▼ −{formatUsdt(model.equity.maxDrawdown.usdt)} USD (−
        {formatPercent(model.equity.maxDrawdown.fraction)})
      </span>
    ) : null,
  };
  const summary = loaded?.summary ?? null;
  const accountId = loaded?.accountId ?? null;
  const boundary = model.run?.boundary_ms ?? null;
  return (
    <div className="space-y-5">
      {header}
      <SimulatedBanner />
      <AccountGrid
        hero={
          <>
            <HeroMobile {...hero} />
            <HeroDesktop {...hero} />
          </>
        }
        rail={<MetricRail label="Account" metrics={metrics} />}
      />
      <Tabs value={tab} onValueChange={setTab}>
        <PaperTabBar
          tabs={[
            { value: "positions", label: "Open", count: model.positions.length },
            { value: "trades", label: "Closed", count: summary?.trades ?? null },
            { value: "rejected", label: "Rejected", count: summary?.rejected ?? null },
            { value: "ledger", label: "Ledger", count: summary?.lines ?? null },
          ]}
          action={tab === "ledger" ? <ExportButton market="forex" compact /> : null}
        />
        <TabsContent value="positions" className="mt-0">
          <ListShell
            state={listState({ isPending: false, error: null, count: model.positions.length })}
            error={null}
            onRetry={() => void data.refetch()}
            empty={{ icon: Wallet, title: "No open positions" }}
          >
            <PositionsList positions={model.positions} />
          </ListShell>
        </TabsContent>
        <TabsContent value="trades" className="mt-0">
          <TradesList accountId={accountId} boundary={boundary} config={model.config} digits={model.digits} />
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
        slices={model.results ? outcomeSlices(model.results, "Stop-out", PAPER_TERMS.ambiguous) : []}
        quality={
          model.results
            ? [
                { label: "Expectancy", hint: PAPER_TERMS.expectancy, value: <SignedText value={model.results.expectancy} /> },
                { label: "Profit factor", hint: PAPER_TERMS.profitFactor, value: model.results.profitFactor },
                { label: "Max drawdown", hint: PAPER_TERMS.maxDrawdown, value: model.results.maxDrawdown.replace("USDT", "USD") },
                { label: "Margin use", hint: FX_TERMS.margin, value: model.results.marginUtilization },
              ]
            : []
        }
        costs={
          model.results
            ? [
                { label: "Commission", hint: FX_TERMS.costs, value: <SignedText value={model.results.fees} /> },
                { label: "Swap", hint: FX_TERMS.costs, value: <SignedText value={model.results.funding} /> },
                {
                  label: "Stop-outs",
                  hint: FX_CLOSE_REASONS.stop_out.help,
                  value: <SignedText value={model.results.liquidation} />,
                },
                { label: "Spread", hint: "Already inside every fill price, so it is not a separate line.", value: "in fills" },
              ]
            : []
        }
        note="Few trades swing a lot: expectancy and profit factor mean little before dozens of closed trades. Stop-out is isolated per position here, which is conservative (the real account is cross-margin)."
        facts={fxRunFacts(model.run?.wallet_config ?? null)}
      />
    </div>
  );
}
