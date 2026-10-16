/**
 * Paper account view models (#36 Part 3, pure). The page reads the signed-in user's wallet rows through RLS
 * (`paper-data.ts`); everything here is deterministic mapping and formatting: no clock (callers pass `nowMs`),
 * no I/O, no trading calculation. Money is stored as integers in 10^-8 USDT (`*_e8`) and prices/quantities as
 * exact text (`"12345"` or `"num/den"`); conversion to JavaScript numbers is for display only.
 *
 * Every amount that is a gain or a loss is a `Signed` value (text with its sign, an arrow and a tone), so a colour
 * is never the only signal.
 */
import { classifyRunAge, THRESHOLDS, type Level as CheckLevel } from "../forward/mvp-check";
import { outcomeLabel, walletRejectionLabel } from "../labels";

export const E8 = 1e8;
/** A mark older than this is shown as stale (the hourly job writes at most hourly; 3 h = a missed run or two). */
export const MARK_STALE_MS = 3 * 3_600_000;
/** A published quote older than this is shown as stale (the same 10 minutes `npm run mvp:check` uses). */
export const QUOTE_STALE_MS = 10 * 60_000;

// ---------------------------------------------------------------- formatting helpers

export type Tone = "gain" | "loss" | "flat";
export type Signed = { text: string; tone: Tone; arrow: "▲" | "▼" | "" };

/** Exact text (`"5"`, `"1/3"`, `"-7/2"`) or a number as a JavaScript number; null for anything else. */
export function parseExact(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string") return null;
  const [numerator = "", denominator = "1"] = value.split("/");
  const top = Number(numerator);
  const bottom = Number(denominator);
  if (numerator === "" || !Number.isFinite(top) || !Number.isFinite(bottom) || bottom === 0)
    return null;
  return top / bottom;
}

export const e8ToUsdt = (value: unknown): number | null => {
  const exact = parseExact(value);
  return exact === null ? null : exact / E8;
};

const fixed = (value: number, digits: number) =>
  value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });

/** USDT with sensible decimals: 2 from one unit up, 4 below one (fees are often a few hundredths). "—" for missing. */
export function formatUsdt(usdt: number | null | undefined): string {
  if (usdt === null || usdt === undefined || !Number.isFinite(usdt)) return "—";
  const digits = Math.abs(usdt) >= 1 || usdt === 0 ? 2 : 4;
  return fixed(usdt, digits);
}

export const formatE8 = (e8: unknown): string => formatUsdt(e8ToUsdt(e8));

/** A gain or loss: always with a sign and an arrow ("▲ +1.20", "▼ −0.05", "0.00"); never colour alone. */
export function signedUsdt(usdt: number | null | undefined): Signed | null {
  if (usdt === null || usdt === undefined || !Number.isFinite(usdt)) return null;
  const rounded = Number(usdt.toFixed(Math.abs(usdt) >= 1 ? 2 : 4));
  if (rounded === 0) return { text: formatUsdt(0), tone: "flat", arrow: "" };
  const text = `${rounded > 0 ? "+" : "−"}${formatUsdt(Math.abs(rounded))}`;
  return rounded > 0 ? { text, tone: "gain", arrow: "▲" } : { text, tone: "loss", arrow: "▼" };
}

export const signedE8 = (e8: unknown): Signed | null => signedUsdt(e8ToUsdt(e8));

/** A price (already divided by 1e8) with decimals that follow its size, so DOGE and BTC both read well. */
export function formatPrice(price: number | null | undefined): string {
  if (price === null || price === undefined || !Number.isFinite(price)) return "—";
  const abs = Math.abs(price);
  const digits = abs >= 1000 ? 2 : abs >= 10 ? 3 : abs >= 1 ? 4 : 6;
  return fixed(price, digits);
}

export const formatPriceE8 = (e8: unknown): string => {
  const usdt = e8ToUsdt(e8);
  return formatPrice(usdt);
};

/** A base-asset quantity (exact text) with up to 6 decimals, trailing zeros trimmed. */
export function formatQuantity(value: unknown): string {
  const quantity = parseExact(value);
  if (quantity === null) return "—";
  const text = quantity.toLocaleString("en-US", { maximumFractionDigits: 6 });
  return text === "-0" ? "0" : text;
}

export function formatPercent(fraction: number | null | undefined, digits = 2): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) return "—";
  return `${(fraction * 100).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
}

/** "45 s", "12 min", "2 h 05 min", "3 d 4 h": holding times and ages. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return "—";
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ${String(minutes % 60).padStart(2, "0")} min`;
  return `${Math.floor(hours / 24)} d ${hours % 24} h`;
}

export const coinOf = (symbol: unknown): string => String(symbol ?? "").replace(/USDT$/, "");
export const dateTimeText = (ms: unknown): string =>
  typeof ms === "number" && Number.isFinite(ms)
    ? new Date(ms).toLocaleString("en-US", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "UTC",
      }) + " UTC"
    : "—";

// ---------------------------------------------------------------- terms (Hints)

export const PAPER_TERMS = {
  simulated:
    "Everything on this page is simulated with fake money. No order is ever sent to an exchange and no trading key is stored.",
  balance:
    "Wallet balance: the starting balance plus realized results, minus fees and funding. Open positions are not in it.",
  equity:
    "Equity = balance + unrealized P&L at the latest price. It is what the account would be worth if every position closed at that price.",
  available:
    "Available margin = balance − margin used by open positions − margin reserved for working limit orders. New trades can only use this.",
  marginUsed:
    "Initial margin set aside for open positions (notional ÷ leverage). It stays yours; it is collateral, not a cost.",
  unrealized:
    "Profit or loss of open positions at the latest price. It changes until the position is closed.",
  realized: "Profit or loss of closed trades and partial exits, before fees and funding.",
  fees: "Trading fees paid on every fill (taker on market orders, maker on resting limit orders).",
  funding:
    "Funding paid or received at each settlement while a position is open. Positive = received.",
  markPrice:
    "The price used to value open positions: the wallet's own mark when it has one, otherwise the latest published trade price.",
  liquidationDistance:
    "How far the price is from the liquidation price, as a share of the current price. Smaller means closer to losing the whole margin.",
  expectancy:
    "Average net result per closed trade (after fees and funding). With few trades it is mostly noise.",
  profitFactor:
    "Total winning results divided by total losing results of closed trades. Above 1 means winners outweighed losers so far.",
  maxDrawdown:
    "Largest fall of the balance from a peak to a later low, on the hourly (daily after 60 days) points. An intra-bucket dip is not seen.",
  slippage:
    "Cost of filling slightly worse than the reference price. It is already inside fill prices, so it is not an extra ledger line.",
  liquidation:
    "Money lost when a position was liquidated: the loss up to the isolated margin plus the liquidation fee.",
  marginUtilization: "Margin used as a share of the balance right now.",
  ambiguous:
    "Target and stop were both reachable inside the same candle. The result is counted the worse way, never the better one.",
  filter:
    "Filtering a report never recreates the wallet. Changing a selection rule needs a new run, because it changes which trades could be afforded.",
  stale: "The last wallet update is old. Positions and totals below are from then.",
} as const;

// ---------------------------------------------------------------- inputs (as stored)

export type JsonValue =
  string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export type PositionRaw = {
  symbol: string;
  side: number;
  entry_ms: number;
  stop: number;
  target: number;
  qty: string;
  entry_fill: string;
  leverage: number;
  leverage_cap?: number;
  liquidation_price: string;
  margin_e8: number;
  notional_e8: string;
  tier_label?: string;
};
/** Strategy and holding horizon of a setup, from the shared setup facts. */
export type SetupMeta = { strategy_id: string; horizon_min: number | null };
export type WalletStateRaw = {
  version?: string;
  balance_e8: number;
  used_margin_e8: number;
  reserved_e8?: number;
  positions?: Record<string, PositionRaw>;
  marks?: Record<string, { ms: number; price: number }>;
  totals?: Partial<
    Record<"realized_e8" | "fees_e8" | "funding_e8" | "liquidation_e8" | "slippage_e8", number>
  >;
};
export type Quote = { symbol: string; price: number; published_at: string };
export type RunInfo = {
  boundary_ms: number;
  created_at?: string | undefined;
  wallet_config: Record<string, unknown> | null;
  wallet_state: WalletStateRaw | null;
};
/** The one-row `paper_account_summary()` result (numbers: SQL bigint/numeric text converted by the loader). */
export type SummaryRaw = {
  trades: number;
  target: number;
  stopped: number;
  expired: number;
  liquidated: number;
  ambiguous: number;
  unresolved: number;
  wins: number;
  losses: number;
  sum_net_e8: number;
  gross_wins_e8: number;
  gross_losses_e8: number;
  rejected: number;
  order_rejected: number;
  open_fees_e8: number;
  close_fees_e8: number;
  funding_e8: number;
  liquidation_e8: number;
  pnl_e8: number;
  lines: number;
  first_ms: number | null;
  last_ms: number | null;
};
export type LedgerRow = {
  seq: number;
  ms: number;
  type: string;
  setup_id: string | null;
  symbol: string | null;
  amount_e8: number;
  balance_e8: number;
  payload: Record<string, unknown> | null;
};

// ---------------------------------------------------------------- freshness

export type Freshness = "fresh" | "delayed" | "stale" | "none";
const LEVEL_TO_FRESHNESS: Record<CheckLevel, Freshness> = {
  PASS: "fresh",
  WARN: "delayed",
  FAIL: "stale",
};

/** Age of the last wallet update with the same 2 h / 6 h thresholds as `npm run mvp:check`. */
export function walletFreshness(boundaryMs: number | null | undefined, nowMs: number): Freshness {
  if (boundaryMs === null || boundaryMs === undefined) return "none";
  return LEVEL_TO_FRESHNESS[classifyRunAge(Math.max(0, nowMs - boundaryMs))];
}
export const STALE_AFTER_MS = THRESHOLDS.runWarnWithinMs;

// ---------------------------------------------------------------- open positions

export type MarkSource = "wallet-mark" | "published-price" | "none";
export type PositionView = {
  id: string;
  symbol: string;
  coin: string;
  side: 1 | -1;
  sideLabel: "Long" | "Short";
  quantity: string;
  entry: string;
  mark: string;
  markSource: MarkSource;
  markStale: boolean;
  unrealized: Signed | null;
  unrealizedUsdt: number | null;
  margin: string;
  notional: string;
  leverage: string;
  /** True when the program's leverage choice was held down by the configured cap. */
  atCap: boolean;
  strategyId: string | null;
  horizonMin: number | null;
  liquidation: string;
  /** Distance of the price from the liquidation price as a share of the price; null when no price is known. */
  liquidationDistance: number | null;
  liquidationDistanceText: string;
  stop: string;
  target: string;
  openedMs: number;
  openedText: string;
  tier: string;
};

/** One card per open position. The mark is the wallet's own, else the latest published price, else unknown. */
export function openPositionViews(
  state: WalletStateRaw | null,
  quotes: readonly Quote[],
  nowMs: number,
  meta: Readonly<Record<string, SetupMeta>> = {},
): PositionView[] {
  if (!state) return [];
  const bySymbol = new Map(quotes.map((quote) => [quote.symbol, quote]));
  const views: PositionView[] = [];
  for (const [id, position] of Object.entries(state.positions ?? {})) {
    const side: 1 | -1 = position.side === -1 ? -1 : 1;
    const qty = parseExact(position.qty);
    const entry = e8ToUsdt(position.entry_fill);
    const walletMark = state.marks?.[position.symbol];
    const quote = bySymbol.get(position.symbol);
    let price: number | null = null;
    let source: MarkSource = "none";
    let stale = false;
    if (walletMark) {
      price = walletMark.price / E8;
      source = "wallet-mark";
      stale = nowMs - walletMark.ms > MARK_STALE_MS;
    } else if (quote) {
      price = quote.price;
      source = "published-price";
      stale = nowMs - Date.parse(quote.published_at) > QUOTE_STALE_MS;
    }
    const liquidation = e8ToUsdt(position.liquidation_price);
    const reference = price ?? entry;
    const distance =
      reference !== null && reference > 0 && liquidation !== null
        ? (side * (reference - liquidation)) / reference
        : null;
    const unrealizedUsdt =
      price !== null && qty !== null && entry !== null ? side * qty * (price - entry) : null;
    views.push({
      id,
      symbol: position.symbol,
      coin: coinOf(position.symbol),
      side,
      sideLabel: side === 1 ? "Long" : "Short",
      quantity: formatQuantity(position.qty),
      entry: formatPrice(entry),
      mark: price === null ? "—" : formatPrice(price),
      markSource: source,
      markStale: stale,
      unrealized: signedUsdt(unrealizedUsdt),
      unrealizedUsdt,
      margin: formatE8(position.margin_e8),
      notional: formatE8(position.notional_e8),
      leverage: `${position.leverage}×`,
      atCap: position.leverage_cap !== undefined && position.leverage === position.leverage_cap,
      strategyId: meta[id]?.strategy_id ?? null,
      horizonMin: meta[id]?.horizon_min ?? null,
      liquidation: formatPrice(liquidation),
      liquidationDistance: distance,
      liquidationDistanceText: distance === null ? "—" : formatPercent(distance, 1),
      stop: formatPriceE8(position.stop),
      target: formatPriceE8(position.target),
      openedMs: position.entry_ms,
      openedText: dateTimeText(position.entry_ms),
      tier: position.tier_label ?? "flat-tier",
    });
  }
  return views.sort((a, b) => b.openedMs - a.openedMs || (a.id < b.id ? -1 : 1));
}

// ---------------------------------------------------------------- account cards

export type AccountCard = {
  key: string;
  label: string;
  hint: string;
  value: string;
  tone: Tone | null;
  sub?: string;
};
export type AccountView = {
  status: "empty" | "ready" | "stale";
  freshness: Freshness;
  asOfMs: number | null;
  cards: AccountCard[];
  equityUsdt: number | null;
  balanceUsdt: number | null;
  unmarked: number;
  note: string | null;
};

const toneOf = (signed: Signed | null): Tone | null => (signed ? signed.tone : null);

/**
 * The header strip. Realized P&L, fees and funding come from the SQL summary (every ledger line); when it could
 * not be read they fall back to the wallet-v3 totals, else "—". Equity adds the unrealized P&L of the positions that
 * have a price; positions without one are counted in `unmarked` and the note says so.
 */
export function accountView(
  run: RunInfo | null,
  summary: SummaryRaw | null,
  positions: readonly PositionView[],
  nowMs: number,
): AccountView {
  const state = run?.wallet_state ?? null;
  if (!run || !state) {
    return {
      status: "empty",
      freshness: "none",
      asOfMs: null,
      cards: [],
      equityUsdt: null,
      balanceUsdt: null,
      unmarked: 0,
      note: null,
    };
  }
  const freshness = walletFreshness(run.boundary_ms, nowMs);
  const balance = e8ToUsdt(state.balance_e8);
  const used = e8ToUsdt(state.used_margin_e8) ?? 0;
  const reserved = e8ToUsdt(state.reserved_e8 ?? 0) ?? 0;
  const marked = positions.filter((position) => position.unrealizedUsdt !== null);
  const unmarked = positions.length - marked.length;
  const unrealized = marked.reduce((sum, position) => sum + (position.unrealizedUsdt ?? 0), 0);
  const equity = balance === null ? null : balance + unrealized;
  const totals = state.totals ?? {};
  const realizedE8 = summary ? summary.pnl_e8 : (totals.realized_e8 ?? null);
  const feesE8 = summary ? summary.open_fees_e8 + summary.close_fees_e8 : (totals.fees_e8 ?? null);
  const fundingE8 = summary ? summary.funding_e8 : (totals.funding_e8 ?? null);
  const utilization = balance !== null && balance > 0 ? used / balance : null;
  const realized = signedE8(realizedE8);
  const fees = signedE8(feesE8);
  const funding = signedE8(fundingE8);
  const open =
    positions.length === 0 ? signedUsdt(0) : marked.length ? signedUsdt(unrealized) : null;
  const cards: AccountCard[] = [
    {
      key: "balance",
      label: "Balance",
      hint: PAPER_TERMS.balance,
      value: formatUsdt(balance),
      tone: null,
      sub: "USDT",
    },
    {
      key: "equity",
      label: "Equity",
      hint: PAPER_TERMS.equity,
      value: formatUsdt(equity),
      tone: null,
      sub: unmarked ? `${unmarked} position${unmarked === 1 ? "" : "s"} without a price` : "USDT",
    },
    {
      key: "available",
      label: "Available margin",
      hint: PAPER_TERMS.available,
      value: formatUsdt(balance === null ? null : balance - used - reserved),
      tone: null,
      sub: "USDT",
    },
    {
      key: "margin",
      label: "Margin used",
      hint: PAPER_TERMS.marginUsed,
      value: formatUsdt(used),
      tone: null,
      sub: utilization === null ? "USDT" : `${formatPercent(utilization, 1)} of balance`,
    },
    {
      key: "unrealized",
      label: "Unrealized P&L",
      hint: PAPER_TERMS.unrealized,
      value: open ? open.text : "—",
      tone: toneOf(open),
      sub: "open positions",
    },
    {
      key: "realized",
      label: "Realized P&L",
      hint: PAPER_TERMS.realized,
      value: realized ? realized.text : "—",
      tone: toneOf(realized),
      sub: "before costs",
    },
    {
      key: "fees",
      label: "Fees",
      hint: PAPER_TERMS.fees,
      value: fees ? fees.text : "—",
      tone: toneOf(fees),
      sub: "paid",
    },
    {
      key: "funding",
      label: "Funding",
      hint: PAPER_TERMS.funding,
      value: funding ? funding.text : "—",
      tone: toneOf(funding),
      sub: "paid / received",
    },
  ];
  return {
    status: freshness === "stale" ? "stale" : "ready",
    freshness,
    asOfMs: run.boundary_ms,
    cards,
    equityUsdt: equity,
    balanceUsdt: balance,
    unmarked,
    note: unmarked
      ? "Open positions without a price are valued at their entry, so equity and unrealized P&L leave them out."
      : null,
  };
}

// ---------------------------------------------------------------- closed trades

export type OutcomeKey =
  "target" | "stopped" | "liquidated" | "time_limit" | "ambiguous" | "unresolved";
const OUTCOME_FROM_CODE: Record<string, OutcomeKey> = {
  T: "target",
  S: "stopped",
  E: "time_limit",
  ambiguous: "ambiguous",
};
const OUTCOME_SHOWN: Record<OutcomeKey, { label: string; help: string; code: string }> = {
  target: { label: "Target hit", help: outcomeLabel("T").help, code: "T" },
  stopped: { label: "Stopped out", help: outcomeLabel("S").help, code: "S" },
  liquidated: { label: "Liquidated", help: outcomeLabel("L").help, code: "L" },
  time_limit: { label: "Time limit", help: outcomeLabel("E").help, code: "E" },
  ambiguous: { label: "Unclear", help: outcomeLabel("ambiguous").help, code: "ambiguous" },
  unresolved: {
    label: "Closed early",
    help: "Closed without a target, stop or time limit (for example data was missing, so it closed at its entry price).",
    code: "X",
  },
};
export const outcomeShown = (key: OutcomeKey) => OUTCOME_SHOWN[key];

export type ClosedTradeView = {
  id: string;
  symbol: string;
  coin: string;
  strategyId: string | null;
  openedMs: number;
  closedMs: number;
  closedText: string;
  holdingMs: number;
  holdingText: string;
  gross: Signed | null;
  fees: Signed | null;
  funding: Signed | null;
  net: Signed | null;
  netE8: number;
  outcome: OutcomeKey;
  outcomeLabel: string;
  outcomeHelp: string;
  lines: number;
};

const MONEY_KINDS = new Set(["open_fee", "pnl", "close_fee", "funding", "liquidation"]);
const str = (value: unknown): string | undefined => (typeof value === "string" ? value : undefined);
const isClosing = (line: LedgerRow) => {
  const event = line.payload?.["position_event"];
  return (
    line.type === "liquidation" ||
    (line.type === "pnl" && (event === undefined || event === "closed"))
  );
};

/**
 * Closed trades from complete per-setup ledger lines. A trade is closed by a liquidation line or by a result line
 * that is not a partial reduction. Gross = result lines (a liquidation line is the loss including its fee), fees =
 * opening and closing fees, net = every money line of the setup. Newest close first.
 */
export function closedTradeViews(
  lines: readonly LedgerRow[],
  strategyBySetup: ReadonlyMap<string, string> = new Map(),
): ClosedTradeView[] {
  const bySetup = new Map<string, LedgerRow[]>();
  for (const line of lines) {
    if (!line.setup_id || !MONEY_KINDS.has(line.type)) continue;
    const list = bySetup.get(line.setup_id) ?? [];
    list.push(line);
    bySetup.set(line.setup_id, list);
  }
  const trades: ClosedTradeView[] = [];
  for (const [setupId, setupLines] of bySetup) {
    const closing = setupLines.filter(isClosing).sort((a, b) => b.seq - a.seq)[0];
    if (!closing) continue;
    const sum = (kinds: string[]) =>
      setupLines
        .filter((line) => kinds.includes(line.type))
        .reduce((s, line) => s + line.amount_e8, 0);
    const outcome: OutcomeKey =
      closing.type === "liquidation"
        ? "liquidated"
        : (OUTCOME_FROM_CODE[str(closing.payload?.["outcome"]) ?? ""] ?? "unresolved");
    const openedMs = Math.min(
      ...setupLines
        .filter((line) => line.type === "open_fee")
        .map((line) => line.ms)
        .concat(closing.ms),
    );
    const net = sum([...MONEY_KINDS]);
    const shown = OUTCOME_SHOWN[outcome];
    trades.push({
      id: setupId,
      symbol: closing.symbol ?? "",
      coin: coinOf(closing.symbol),
      strategyId: strategyBySetup.get(setupId) ?? null,
      openedMs,
      closedMs: closing.ms,
      closedText: dateTimeText(closing.ms),
      holdingMs: Math.max(0, closing.ms - openedMs),
      holdingText: formatDuration(Math.max(0, closing.ms - openedMs)),
      gross: signedE8(sum(["pnl", "liquidation"])),
      fees: signedE8(sum(["open_fee", "close_fee"])),
      funding: signedE8(sum(["funding"])),
      net: signedE8(net),
      netE8: net,
      outcome,
      outcomeLabel: shown.label,
      outcomeHelp: shown.help,
      lines: setupLines.length,
    });
  }
  return trades.sort((a, b) => b.closedMs - a.closedMs || (a.id < b.id ? -1 : 1));
}

// ---------------------------------------------------------------- rejected

export type RejectedView = {
  seq: number;
  ms: number;
  timeText: string;
  symbol: string;
  coin: string;
  setupId: string;
  strategyId: string | null;
  kind: "setup" | "order" | "fill";
  reason: string;
  reasonLabel: string;
};

/** Recorded non-trades: a setup the wallet could not afford or allow, a rejected order, or a rejected fill. */
export function rejectedViews(
  lines: readonly LedgerRow[],
  strategyBySetup: ReadonlyMap<string, string> = new Map(),
): RejectedView[] {
  return lines
    .filter((line) => line.type === "rejected" || line.type === "order_rejected")
    .map((line): RejectedView => {
      const reason = str(line.payload?.["reason"]) ?? "";
      return {
        seq: line.seq,
        ms: line.ms,
        timeText: dateTimeText(line.ms),
        symbol: line.symbol ?? "",
        coin: coinOf(line.symbol),
        setupId: line.setup_id ?? "",
        strategyId: line.setup_id ? (strategyBySetup.get(line.setup_id) ?? null) : null,
        kind:
          line.type === "order_rejected"
            ? "order"
            : line.payload?.["fill_id"] !== undefined
              ? "fill"
              : "setup",
        reason,
        reasonLabel: walletRejectionLabel(reason),
      };
    })
    .sort((a, b) => b.seq - a.seq);
}

// ---------------------------------------------------------------- ledger

const KIND_LABEL: Record<string, string> = {
  open_fee: "Opening fee",
  pnl: "Trade result",
  close_fee: "Closing fee",
  funding: "Funding",
  liquidation: "Liquidation",
  rejected: "Rejected",
  order_submitted: "Order submitted",
  order_accepted: "Order accepted",
  order_rejected: "Order rejected",
  order_cancelled: "Order cancelled",
  order_expired: "Order expired",
  order_filled: "Order filled",
  order_partially_filled: "Order partly filled",
  order_ambiguous: "Order unclear",
  stop_moved: "Stop moved",
  target_moved: "Target moved",
};
export const ledgerKindLabel = (type: string): string =>
  KIND_LABEL[type] ?? type.replaceAll("_", " ");

export type LedgerView = {
  seq: number;
  ms: number;
  timeText: string;
  type: string;
  typeLabel: string;
  coin: string;
  setupId: string;
  amount: Signed | null;
  balance: string;
  detail: string;
  fields: [string, string][];
};

/** One row per ledger line; lines that move no money (rejections, order steps) show no amount, not a zero. */
export function ledgerViews(lines: readonly LedgerRow[]): LedgerView[] {
  return lines.map((line): LedgerView => {
    const payload = line.payload ?? {};
    const reason = str(payload["reason"]);
    const outcome = str(payload["outcome"]);
    const detail = reason
      ? walletRejectionLabel(reason)
      : outcome
        ? outcomeLabel(outcome).short
        : "";
    const fields: [string, string][] = [];
    const add = (label: string, value: unknown) => {
      if (value !== undefined && value !== null && value !== "")
        fields.push([label, String(value)]);
    };
    add("Setup", line.setup_id);
    add("Order", payload["order_id"]);
    add("Fill", payload["fill_id"]);
    add("Quantity", payload["qty"] === undefined ? undefined : formatQuantity(payload["qty"]));
    add("Liquidity", payload["liquidity"]);
    const assumptions = payload["assumptions"];
    add("Assumption", Array.isArray(assumptions) ? assumptions.join(", ") : undefined);
    add("Note", payload["ambiguity"]);
    return {
      seq: line.seq,
      ms: line.ms,
      timeText: dateTimeText(line.ms),
      type: line.type,
      typeLabel: ledgerKindLabel(line.type),
      coin: coinOf(line.symbol),
      setupId: line.setup_id ?? "",
      amount: MONEY_KINDS.has(line.type) ? signedE8(line.amount_e8) : null,
      balance: formatE8(line.balance_e8),
      detail,
      fields,
    };
  });
}

// ---------------------------------------------------------------- equity and drawdown

export type EquityPoint = { ms: number; value: number };
export type EquityView = {
  status: "empty" | "ready";
  points: EquityPoint[];
  drawdown: EquityPoint[];
  last: number | null;
  change: Signed | null;
  changeFraction: number | null;
  maxDrawdown: { usdt: number; fraction: number } | null;
  currentEquity: number | null;
};

/**
 * Balance history (the realized balance after the last ledger line of each hour, daily beyond 60 days) with the
 * account's equity now as a final point, when it differs. Drawdown is measured from the running peak.
 */
export function equityView(
  series: readonly { ms: number; balance_e8: number | string }[],
  currentEquity: number | null,
  nowMs: number,
): EquityView {
  const points: EquityPoint[] = series
    .map((point) => ({ ms: Number(point.ms), value: Number(point.balance_e8) / E8 }))
    .filter((point) => Number.isFinite(point.ms) && Number.isFinite(point.value))
    .sort((a, b) => a.ms - b.ms);
  const lastPoint = points[points.length - 1];
  if (
    currentEquity !== null &&
    Number.isFinite(currentEquity) &&
    lastPoint &&
    lastPoint.value !== currentEquity
  ) {
    points.push({ ms: Math.max(nowMs, lastPoint.ms + 1), value: currentEquity });
  }
  if (points.length < 2) {
    return {
      status: "empty",
      points,
      drawdown: [],
      last: lastPoint ? lastPoint.value : null,
      change: null,
      changeFraction: null,
      maxDrawdown: null,
      currentEquity,
    };
  }
  let peak = points[0]!.value;
  let worst = { usdt: 0, fraction: 0 };
  const drawdown = points.map((point) => {
    peak = Math.max(peak, point.value);
    const usdt = peak - point.value;
    const fraction = peak > 0 ? usdt / peak : 0;
    if (usdt > worst.usdt) worst = { usdt, fraction };
    return { ms: point.ms, value: -fraction };
  });
  const first = points[0]!.value;
  const last = points[points.length - 1]!.value;
  return {
    status: "ready",
    points,
    drawdown,
    last,
    change: signedUsdt(last - first),
    changeFraction: first > 0 ? (last - first) / first : null,
    maxDrawdown: worst.usdt > 0 ? worst : null,
    currentEquity,
  };
}

// ---------------------------------------------------------------- results block

export type ResultsView = {
  completed: number;
  rejected: number;
  stopped: number;
  liquidated: number;
  ambiguous: number;
  target: number;
  timeLimit: number;
  unresolved: number;
  expectancy: Signed | null;
  profitFactor: string;
  maxDrawdown: string;
  fees: Signed | null;
  funding: Signed | null;
  slippage: string;
  liquidation: Signed | null;
  marginUtilization: string;
};

/** The "Simulator results" of docs/futures-simulation.md for the one wallet run, from the SQL summary. */
export function resultsView(
  summary: SummaryRaw | null,
  state: WalletStateRaw | null,
  equity: EquityView | null,
): ResultsView | null {
  if (!summary) return null;
  const trades = summary.trades;
  const gross = summary.gross_wins_e8;
  const loss = Math.abs(summary.gross_losses_e8);
  const profitFactor =
    trades === 0
      ? "—"
      : loss === 0
        ? gross > 0
          ? "no losing trade"
          : "—"
        : (gross / loss).toFixed(2);
  const balance = state ? e8ToUsdt(state.balance_e8) : null;
  const used = state ? e8ToUsdt(state.used_margin_e8) : null;
  const slippage = state?.totals?.slippage_e8;
  return {
    completed: trades,
    rejected: summary.rejected + summary.order_rejected,
    stopped: summary.stopped,
    liquidated: summary.liquidated,
    ambiguous: summary.ambiguous,
    target: summary.target,
    timeLimit: summary.expired,
    unresolved: summary.unresolved,
    expectancy: trades === 0 ? null : signedE8(summary.sum_net_e8 / trades),
    profitFactor,
    maxDrawdown: equity?.maxDrawdown
      ? `−${formatUsdt(equity.maxDrawdown.usdt)} USDT (−${formatPercent(equity.maxDrawdown.fraction)})`
      : "—",
    fees: signedE8(summary.open_fees_e8 + summary.close_fees_e8),
    funding: signedE8(summary.funding_e8),
    slippage: slippage === undefined ? "not recorded (wallet-v2)" : `${formatE8(slippage)} USDT`,
    liquidation: signedE8(summary.liquidation_e8),
    marginUtilization:
      balance !== null && balance > 0 && used !== null ? formatPercent(used / balance, 1) : "—",
  };
}

// ---------------------------------------------------------------- run configuration

/** The configuration facts shown with the results: what this wallet run froze (a change would be a new run). */
export function runFacts(config: Record<string, unknown> | null): [string, string][] {
  if (!config) return [];
  const facts: [string, string][] = [];
  const add = (label: string, value: unknown) => {
    if (value !== undefined && value !== null && value !== "") facts.push([label, String(value)]);
  };
  add("Wallet version", config["wallet_version"] ?? config["version"]);
  const initial = e8ToUsdt(config["initial_balance_e8"]);
  add("Starting balance", initial === null ? undefined : `${formatUsdt(initial)} USDT`);
  const risk = parseExact(config["risk_fraction"]);
  add("Risk per trade", risk === null ? undefined : formatPercent(risk, 2));
  add(
    "Leverage cap",
    config["leverage_cap"] === undefined ? undefined : `${String(config["leverage_cap"])}×`,
  );
  add("Max positions", config["max_positions"]);
  add("Sizing from", config["sizing_basis"]);
  const total = parseExact(config["max_open_risk_fraction"]);
  add("Max total open risk", total === null ? undefined : formatPercent(total, 1));
  const cluster = parseExact(config["cluster_risk_fraction"]);
  add("Same-direction cluster risk", cluster === null ? undefined : formatPercent(cluster, 1));
  const hash = str(config["contract_inputs_hash"]);
  add(
    "Contract inputs",
    hash ? `${str(config["contract_inputs_version"]) ?? "inputs"} ${hash.slice(0, 8)}` : undefined,
  );
  return facts;
}

// ---------------------------------------------------------------- page state

export type ListState = "loading" | "error" | "empty" | "ready";
export function listState(query: { isPending: boolean; error: unknown; count: number }): ListState {
  if (query.error) return "error";
  if (query.isPending) return "loading";
  return query.count === 0 ? "empty" : "ready";
}
