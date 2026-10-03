# Futures paper-trading simulation

This document specifies the separate automated paper-trading tool. The simulator
receives versioned conditional opportunities from the
[analysis and evaluation system](analysis-evaluation.md), is assigned a virtual
USDT balance and run configuration, and executes those opportunities using fake
money. It never places real-money orders.

The initial scope is Binance USDⓈ-M, USDT-margined perpetual futures, long and
short. The simulator tests whether a selection, sizing, execution, and management
policy can grow or deplete a constrained virtual account after costs.

## Input and ownership boundary

The analysis system owns:

- assessments, evidence, factors, and scores;
- conditional setup creation and versioned strategy rules;
- activation, invalidation, expiry, and outcome definitions;
- general score-band and strategy-effectiveness reporting.

The simulator consumes immutable setup IDs and ordered transitions. It does not
invent a score, detach price levels from their originating pattern, or reinterpret
old setups using current strategy code.

A simulation run may select opportunities by strategy, contract, direction,
timeframe, minimum score, or other saved fields. For example, it may accept only
setups whose original score is at least 80. That threshold is a run policy, not the
definition or calibration of the underlying trade score.

## Automated simulation workflow

For each configured run, the tool:

1. Receives eligible developing and activated setups from the analysis engine.
2. Applies the run's selection, capital, risk, overlap, and exposure policies.
3. Creates simulated order intents only when the setup's complete activation and
   validity requirements are satisfied.
4. Generates fills or rejections from ordered market events and the selected fill
   model.
5. Manages long/short positions through stops, partial TP/SL, permitted stop
   adjustments, branching targets, fallback/time exits, funding, and liquidation.
6. Applies every fill and cost to the virtual wallet and append-only accounting
   ledger.
7. Continues until the run is paused, stopped, or reaches its configured end.

No AI is required for this process. The same deterministic strategy, execution,
and accounting functions should support historical and live paper modes.

## Run configuration

Each run freezes:

- initial USDT wallet balance;
- eligible strategy and scoring versions;
- contract universe, direction, timeframe, and score/selection policy;
- isolated-margin mode and permitted leverage range;
- risk-per-trade and position-sizing policy;
- maximum simultaneous positions, committed margin, and total exposure;
- market/limit order and partial-fill assumptions;
- fee, spread, slippage, funding, contract-rule, liquidation, and ambiguity models;
- start/end time, event source, data manifest, code version, and random seed where
  applicable.

A material configuration change creates a new run/version instead of rewriting the
history of an existing account.

## Wallet and position accounting

The initial model supports isolated-margin linear USDT perpetuals, configurable
leverage, long and short positions, market and limit intents, partial fills/exits,
and concurrent-position limits.

An illustrative position is:

| Item                                |                   Value |
| ----------------------------------- | ----------------------: |
| Starting wallet balance             |                100 USDT |
| Isolated margin allocated           |                 10 USDT |
| Leverage                            |                      8× |
| Position notional/exposure          |   approximately 80 USDT |
| Gross P&L after a 2% favorable move | +1.60 USDT before costs |
| Gross P&L after a 2% adverse move   | −1.60 USDT before costs |

The 10 USDT is committed collateral, not an immediate expense. Track wallet
balance, available balance/margin, initial and maintenance margin, notional
exposure, unrealized P&L, realized P&L, fees, funding, and total equity separately.

Initial position sizing is risk-first:

1. Choose the maximum wallet loss allowed if the initial stop executes.
2. Estimate stop distance plus fees, spread, slippage, and applicable funding.
3. Calculate position notional and quantity from that complete loss budget.
4. Choose leverage that supplies the required margin within contract and
   liquidation constraints.

For example, a 1 USDT maximum loss with a stop 2% from entry implies approximately
50 USDT notional before costs. At 5× leverage, that requires approximately 10 USDT
initial margin. Expected costs reduce the quantity where needed to keep the loss
estimate within the 1 USDT budget.

Leverage controls required margin for a chosen notional. A high score does not
automatically increase leverage. Fixed-risk and score-dependent sizing are separate
experiment variants.

## Accounting ledger

The simulator needs an append-only ledger because signal or outcome histories alone
cannot reconstruct account performance. Record:

- order submission, acceptance, rejection, cancellation, and expiry;
- partial and complete fills with quantity, side, price, and time;
- position opens, increases, reductions, and closes;
- margin commitments/releases and available-balance changes;
- realized/unrealized P&L transitions;
- maker/taker fees, funding payments, and liquidation/closeout charges;
- the originating setup, position, account, run, and source event for every entry.

The ledger must reconstruct wallet state exactly, prevent capital from being spent
twice, reject reductions larger than the open quantity, and remain idempotent when
events are retried. This accounting ledger is part of the simulator; general market
activity and analysis-effectiveness histories are defined separately in
[analysis records](analysis-evaluation.md#intended-record-model-and-histories).

## Exchange and execution realism

The execution model versions and exposes:

- maker/taker fees and fee tier;
- spread and slippage assumptions;
- market/limit fill and partial-fill rules;
- funding rate, sign, position notional, and settlement timestamp;
- tick size, step size, minimum quantity/notional, and effective contract filters;
- leverage and maintenance-margin brackets;
- last/contract, mark, or index price used by each trigger;
- liquidation thresholds, ordering, and closeout assumptions;
- simultaneous-position, committed-margin, and exposure constraints.

Gross P&L comes from signed quantity and fill-price movement. Net P&L includes all
entry/exit fees, spread/slippage, funding cash flows, and liquidation charges.
Holding time matters because a position may cross funding timestamps. There is no
invented per-minute leverage charge: trading fees apply to fills and funding applies
at the exchange's relevant settlement events.

## Implemented execution-math foundation (#37 Part 1)

`python/market_analysis/futures_execution{,_contracts}.py` provides stateless,
versioned exact calculations for P&L and position increases/reductions, fill fees,
settlement funding amounts, filter-grid validation and explicit adjustment
suggestions, leverage/maintenance brackets, margin, piecewise isolated liquidation
risk thresholds, explicit STOP/TAKE_PROFIT references, and fixed-bps adverse prices.
Inputs use the existing `binance-usdm:<symbol>` identity for one-way isolated linear
USDT perpetuals. Snapshot identities bind actual parameters, per-filter provenance,
effective time and observed time; current-rule assumptions remain distinguishable
from historical evidence. The frozen [Issue #37 design comment](https://github.com/ibrahimjaved12/crypto-watcher/issues/37#issuecomment-5969740973)
is the detailed contract.

Submitted intents are validated without modification. Suggestions require explicit
acceptance and full order revalidation; quantities never round upward. Market
quantities satisfy both supplied LOT_SIZE and MARKET_LOT_SIZE filters, with minQty
as each fixed lattice origin. MARKET intents contain no submitted price;
price-bearing intents require one. Dynamic mark evidence is supplied separately
from order intent: MARKET MIN_NOTIONAL uses
mark price; price-bearing orders use their submitted price. PERCENT_PRICE checks
only the BUY upper bound or SELL lower bound against mark. PRICE_FILTER components
are independently disabled by zero; enabled ticks use minPrice as grid origin.
Leverage is an integer in 1–125, further limited by the supplied effective bracket.
Brackets are contiguous and maintenance-continuous: positive tiers own their cap
(`floor < notional <= cap`), so exact boundaries belong to the preceding tier.
Zero belongs to the first tier for zero-notional helpers; values above the final
supplied cap are unavailable, never extrapolated. Execution identity is versioned
v3. Missing settlement marks are unavailable and requested price protection is
unsupported.

Arithmetic is independent of the caller's Decimal context. Reduced integer
`ExactScalar` values make repeating quotients VALID and usable by subsequent P&L,
entry, margin and liquidation calculations. Finite results retain an exact Decimal
view; no rounding assumption is introduced. Liquidation candidates are checked
against their own brackets and exact equity/MM equality, including repeating roots.
Shared neutral canonical hashing preserves existing lifecycle identities unchanged.

This foundation does not provide wallet/ledger state (#36), account admission,
funding entitlement or postings, network/historical acquisition, fill/event ordering,
spread/order-book modeling, or liquidation execution/settlement. Initial margin is
only notional/leverage, not complete exchange order acceptance. Other margin and
position modes, non-USDT settlement, BNB fee state, trailing stops, authenticated
operations and real trading remain unsupported. Later #37 parts remain open.

## Event resolution and ambiguity

Analysis cadence and simulation event resolution are separate. A five-minute
analysis cycle cannot establish the order of events inside those five minutes. Use
one-minute candles for initial replay and finer trade/aggregate-trade events when
entry, stop, target, fill, or liquidation ordering matters.

If the data cannot establish event order, mark the execution/result ambiguous or
apply a named conservative policy. Never silently select the profitable ordering.

## Historical and live paper modes

Historical mode consumes validated, chronological market/funding events and the
point-in-time analyses or reconstructable inputs defined by the analysis contract.
Live mode consumes public futures streams. Both modes use the same setup-transition,
fill, cost, position, and accounting functions; adapters provide their clocks and
event sources.

Every historical experiment freezes the dataset manifest, setup/strategy versions,
wallet policy, execution/cost models, contract rules, ordering policy, and code
version. Compare simple entry/stop/target execution with conditional branches and
advanced management through declared experiment variants.

Live paper runs persist cursors and ledger events, rebuild state after restart,
deduplicate market events and fills, and block unsafe advancement across unresolved
data gaps. Pausing new entries must still manage open simulated positions unless a
separate, explicit control pauses all processing.

## Simulator results

For each coherent wallet run, report:

- starting and ending balance/equity;
- gross and net P&L and return;
- equity curve and maximum drawdown;
- completed, rejected, stopped, liquidated, and ambiguous trade counts;
- average win/loss, expectancy, profit factor, and holding time;
- fees, funding, spread/slippage, and liquidation totals;
- margin utilization, exposure, and capital-conflict counts;
- results by configured selection policy, strategy version, contract, direction,
  and period.

Filtering an existing account report does not recreate the wallet. Changing the
minimum score or any other selection rule requires a new run because it changes
capital availability, overlapping positions, rejected trades, and compounding.

## Safety boundary

- Use public market-data endpoints only.
- Store no exchange trading credential.
- Provide no hidden real-order adapter or production flag.
- Label all positions, notifications, balances, and P&L as simulated.
- Make real-money decisions and order placement remain manual.
