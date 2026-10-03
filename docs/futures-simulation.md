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
acceptance and full order revalidation; quantities never round upward. Price-bearing
orders use LOT_SIZE and price filters; MARKET orders use only MARKET_LOT_SIZE and
mark-based MIN_NOTIONAL. Non-applicable filters do not constrain validation or
require evidence. Both quantity filters use minQty as their fixed lattice origin.
MARKET intents contain no submitted price; price-bearing intents require one.
Dynamic mark evidence is supplied separately from order intent: MARKET MIN_NOTIONAL
uses mark price; price-bearing orders use their submitted price. Price-bearing
PERCENT_PRICE checks only the BUY upper bound or SELL lower bound against mark.
PRICE_FILTER components are independently disabled by zero; enabled ticks use
minPrice as grid origin.
Leverage is an integer in 1–125, further limited by the supplied effective bracket.
Brackets are contiguous and maintenance-continuous: positive tiers own their cap
(`floor < notional <= cap`), so exact boundaries belong to the preceding tier.
Zero belongs to the first tier for zero-notional helpers; values above the final
supplied cap are unavailable, never extrapolated. Execution identity is versioned
v4. Missing settlement marks are unavailable and requested price protection is
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

## Implemented execution evidence boundary (#37 Part 2)

`execution_evidence.py`, `binance_execution_evidence.py`, and
`binance_execution_snapshots.py` add immutable, canonically hashed Python evidence
for later event resolution. The top-level snapshot references component identities,
records provenance and limitations, and explicitly represents missing components.
Its identity binds schema, evidence, policy and Part 1 algorithm versions. It can
be partial; it contains no wallet, position, fill or ledger state. TanStack retains
orchestration, validation and persistence boundaries; collector ingestion and the
existing V1/research calculations remain separate.

- **Exact factual event evidence:** local aggTrades reuse the existing verified
  archive/checksum/parser semantics, preserving aggregate and first/last trade IDs,
  exchange timestamp, price, quantity, maker flag, derived BUY/SELL aggressor and
  relative package path/SHA. Identical duplicates collapse; conflicting IDs fail.
  IDs do not expand an aggregate into individual trades. AggTrades do not prove
  passive fill allocation or queue position. Timestamp/ID sorting applies only
  within the tape and does not prove chronology across streams.
  `load_execution_trade_tape()` returns an immutable `ExecutionTradeTapeEvidence`
  manifest containing ordered package identities, parser/tape/ordering/duplicate
  policies, unique and duplicate counts, first/last event keys and an incremental
  normalized-row digest. It stores no rows, absolute paths or iteration state.
  Verification extends the existing temporary SQLite duplicate index with an
  on-disk canonical-order index; memory scales with package metadata and a bounded
  page cache, rather than trade count. Tape identity hashes only manifest metadata,
  including raw package SHAs and the stream digest; the top-level snapshot references
  that small identity. `iter_execution_trades(root, manifest)` verifies packages and
  recomputed manifest identity before yielding immutable `ExecutionTrade` rows in
  `(timestamp_ms, aggregate_trade_id)` order. Its temporary index is removed on
  exhaustion or explicit iterator close; use `contextlib.closing` when stopping early.
- **Bounded-resolution evidence:** the existing mark-price loader supplies
  `ONE_MINUTE_OHLC` risk envelopes and completion availability policy. Package SHA
  and existing mark-evidence SHA remain bound. Exact intraminute mark and crossing
  timestamp are unavailable; candles do not prove ordering relative to aggTrades,
  liquidation-time mark, or an exact funding settlement mark.
- **Current/fixed assumptions:** frozen exchangeInfo, leverage-bracket and fee
  JSON bytes normalize into Part 1 contracts, binding raw SHA and actual normalized
  parameters. Current snapshots are `CURRENT_RULE_ASSUMPTION`; user-configured
  simulation fees are `FIXED_SIMULATION_ASSUMPTION`. Caller-supplied reduce-only
  exemptions carry separate policy provenance because exchangeInfo normally omits
  that field. Bracket callers explicitly declare `BASE_TIERS` or
  `EFFECTIVE_TIERS`: base tiers scale floor, cap and cumulative maintenance offset
  by supplied `notionalCoef`; already-effective tiers are preserved. Account
  specificity, coefficient, declaration and effective table identity are retained.
- **Unavailable historical evidence:** current rule/bracket snapshots are not
  automatically historical truth. Historical snapshot classification requires an
  explicitly supplied factual effective/applicability interval and observation
  timestamp; the adapter never invents those dates. Unknown inputs can remain
  `UNAVAILABLE`, and absent top-level components retain explicit reasons.

The existing settled-funding archive adapter preserves rate, interval and package
provenance. Exact funding cashflow requires the position quantity at settlement
(supplied later) and an **exact settlement mark**. Without that mark, Part 1 returns
`UNAVAILABLE_FUNDING_MARK`; the rate event remains valid. Separately supplied marks
must match symbol, funding timestamp and the complete funding-event identity.
Current/nearest marks, trades, candle open/close, interpolation and forward fill
are never substituted. A local frozen official funding-history JSON record can
supply its factual mark only after exact symbol/time/rate/event matching; supplied
`rateType` and ancillary facts are preserved and identity-bound. An archive event
without `rateType` can be enriched from a uniquely matching official record using
its known symbol, timestamp, exact rate, optional supplied interval and every
already-known ancillary fact. Missing `rateType` never defaults to `Regular`;
zero matching records fail explicitly, and multiple matching records fail as
ambiguous without using `markPrice` to choose. The exact mark binds the enriched
event identity. Funding collections require every contained event to bind the
collection's upstream evidence SHA. Archive availability
surrogates remain distinct from genuinely observed/effective provenance timestamps.
An exact mark's availability uses its supplied observation time, or explicitly
remains unknown; joining it never backdates observation to the rate archive's
availability surrogate.

These adapters perform no hidden acquisition or authenticated API calls. Missing
historical applicability, exact settlement marks and account/tier-specific fees or
brackets require supplied frozen evidence. Part 3 below uses those inputs for fill,
conditional, causal and financial proposals; wallet settlement remains #36 work.

## Implemented execution resolution (#37 Part 3)

`futures_execution_resolution{,_contracts}.py` supplies immutable admission,
order/conditional lifecycle views, position input views, candidates and identified
financial proposals. Part 3 has its own `binance-usdm-execution-resolution-v4`
identity; Part 1 math v4 and Part 2 scientific identities remain unchanged. It
performs no network calls, reads no clocks, and commits no state. Position
quantity, entry and isolated collateral are caller-supplied #36 input facts;
funding/risk require an explicitly confirmed position applicability interval.

Admission freezes a **VALID** Part 1 result and its exact rules, context and
evidence identities. Invalid or unavailable intents remain unadmitted, without
adjustment. Exact mark context must be supplied explicitly; a one-minute mark
candle is never turned into an admission mark. Historical snapshots must cover
the actual execution boundary. Dynamic admission filters are not rerun for later
partial fills. Conditional child intents are validated at activation with the
rules/context then supplied, rather than at parent creation.

- **MARKET:** the first causally eligible contract aggTrade anchors a full-fill
  assumption. Separate explicit fixed-bps spread and slippage transforms apply
  sequentially in the adverse direction; the final actual simulated quantity and
  price determine the normal TAKER fee. Zero bps is an explicit valid policy.
- **Passive LIMIT:** `PASSIVE_TRADE_THROUGH_FULL_PRINT_CAP_V1` requires an opposite
  aggressor and strict trade-through: BUY below the limit, SELL above it. Equality
  does not prove queue priority. Quantity is capped by both supplied remaining
  quantity and observed aggregate volume; fill price is the submitted limit, with
  a MAKER fee and no spread/slippage charge or favorable price improvement.
  Later calls require #36's updated remainder and committed fill cursor.
- **Conditional activation:** Part 1 owns all STOP/TAKE_PROFIT inequalities.
  CONTRACT_PRICE triggers bind exact trade keys; the trigger print cannot also
  fill the new child, even at the same millisecond. Mark high/low can establish
  `TRIGGERED_WITHIN_INTERVAL` only when open is false and an extreme reaches the
  trigger. If the condition already holds at open and creation predates the
  minute, causal bounds extend from creation through the mark open; the proposal
  records `TRIGGERED_BY_MARK_OPEN`. If creation overlaps the minute, a satisfied
  mark close strictly after the latest possible creation time is a factual
  trigger witness. The trigger's occurrence remains bounded from the earliest
  possible creation time through the candle close; it is not assigned an exact
  time. A high/low hit without that post-creation close witness, including
  creation exactly at the close, remains ambiguous. Child execution is possible
  only after the conservative close bound, and a cross-stream trade exactly at
  that bound remains unresolved. Trigger timing remains a factual proposal
  when child admission is rejected or unavailable; the exact Part 1 admission
  result is retained and no executable child view is produced. A child can
  execute only after its conservative activation bound.
  Cross-stream equal-time submission/trigger
  evidence remains unresolved, rather than assumed to precede a trade.
- **Causal resolution:** precedence requires disjoint time bounds or an explicit
  same-stream aggTrade key. There is no liquidation/stop/target/funding priority.
  `EXPLICIT_AMBIGUITY_V1` binds incomparable competing candidates and returns no
  executable proposals. Equal-time funding and position-changing fills, or a mark
  stop/target and liquidation crossing in the same envelope, remain ambiguous.
  Identified resource links use stable caller-supplied position/order IDs;
  related candidates must share the same position ID. Two exact funding charges
  against the same confirmed quantity can commute without inventing chronology.
- **Funding and liquidation:** exact event-based funding delegates to Part 1;
  missing marks stay `UNAVAILABLE_FUNDING_MARK`, and leverage never multiplies
  funding. Liquidation thresholds and bracket identities remain risk calculations.
  OHLC crossing gives a causal interval, never an exact liquidation timestamp.
  A threshold already breached at mark open uses bounds from the position's
  effective start through that open, with `BREACHED_BY_MARK_OPEN` basis. When a
  position starts during the minute, a breached mark close can witness risk if
  the position started strictly before the close and remained effective through
  it. Its bounds run from the position start through mark close, with
  `BREACHED_BY_MARK_CLOSE_AFTER_POSITION_START` basis; the crossing time remains
  unknown. A high/low-only breach, a position starting exactly at close, or a
  position ending before close cannot use the close witness and remains
  unavailable. Positions wholly outside the minute produce no risk candidate.
  Only an unambiguously selected risk allows
  `FIXED_BPS_FROM_LIQUIDATION_THRESHOLD_V1`: adverse SELL for LONG, BUY for SHORT,
  with a separate exact closeout charge. The closeout ID binds the selected risk
  candidate's own proof, so unrelated co-selected candidates do not change that
  economic proposal. This is a simulation approximation, not a factual Binance
  liquidation fill, and it is distinct from normal trading fees.

The production fill path consumes `ExecutionTradeTapeEvidence` through
`iter_execution_trades()` and closes the iterator at the first candidate. It never
materializes the tape. The causal resolver accepts a bounded current candidate
window (maximum 128 unique candidates), not a historical population. Callers must
supply all relevant competing streams for that window; missing streams cannot
prove that no competing event exists. Candidate payloads are tentative: only a
`SELECTED` frontier exposes executable proposals. After selection, #36 validates
account constraints, commits proposal IDs exactly once, and supplies the next
immutable state view/window; it must recompute later proposals after a state change.
Wallet balance, margin reservation, position/order persistence and the ledger
remain #36 responsibilities. These fill policies do not reconstruct Binance
order books, bid/ask history, hidden queue depth or matching-engine queue state.

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
