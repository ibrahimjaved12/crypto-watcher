# Futures paper-trading simulation

This document specifies the separate automated paper-trading tool. The simulator
receives versioned conditional opportunities from the
[analysis and evaluation system](analysis-evaluation.md), is assigned a virtual
USDT balance and run configuration, and executes those opportunities using fake
money. It never places real-money orders; real-money execution stays manual for
now (not permanently excluded, and any change is an owner decision).

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

## Paper wallet implementation (#36)

Code: `python/market_analysis/forward/wallet.py` (**wallet-v2**, frozen: its ledger is stored and the
forward-parity runs depend on it) and `wallet_v3.py` (**wallet-v3**, this section). Both are pure: no clock, no
I/O, amounts are integers in 10^-8 USDT rounded half-even once per ledger line, quantities and prices exact
fractions. Nothing here can place an order: there is no credential, no signed endpoint and no real-order adapter.

### Versions and run configuration

The wallet version is part of the run configuration (`wallet_version` in the wallet service request, stored in
`paper_runs.wallet_config`). A state of one version is refused by the other (`wallet-v3` state under a v2
configuration, or the reverse), so a material change is a new run, never a conversion of history. Every v3 option
defaults to the wallet-v2 behaviour: with all options at their defaults `wallet_v3.step` reproduces
`wallet.step` ledger lines bit for bit (`python/tests/test_forward_wallet_v3.py`, "golden" tests, including the
three-configuration portfolio composition). The v3-only request fields are refused under a v2 configuration.

### Account quantities (v3)

`account_summary(state, config, as_of_ms, max_mark_age_ms)` reports, separately (e8 integers):
wallet balance; used (initial) margin; reserved margin (working orders, Part 2); available balance
(`balance - used - reserved`); maintenance margin; notional exposure; realized P&L; fees; funding; liquidation
losses; slippage (informational: it is inside fill prices, not a ledger line); unrealized P&L; equity
(`balance + unrealized`). Per position: signed quantity, weighted entry, mark, unrealized P&L, margin,
maintenance margin, leverage, liquidation price and distance, stop and target.

- **Mark price carried by the event stream.** A `mark` event `{type, ms, symbol, mark}` sets the symbol's mark
  (an older mark never replaces a newer one; replay is a no-op). A position with no mark has no unrealized P&L
  (None, not zero); its notional and liquidation distance use the entry. Staleness is explicit
  (`mark_stale`), never assumed. The collector stores no mark price (see the forward harness assumptions), so a
  mark is the trade-price proxy until the hourly stage supplies one; the hourly stage does **not** emit `mark`
  events yet.
- **Sizing basis.** Risk fraction and the exposure cap use the realized balance (default) or, with
  `sizing_basis: "equity"`, the equity above. The margin check always uses the realized, available balance.
- **Cost-aware sizing** (`cost_aware_sizing`, off by default). wallet-v2 sizes 1 R as the price distance from the
  slipped entry fill to the stop, so fees and the stop exit's slippage come on top. With the option on, the loss at the
  initial stop (price distance, the entry and exit fee, and the stop exit's slippage floor) equals the risk budget,
  so the quantity is a little smaller; a test closes a position at its stop and checks the loss equals the budget.
  Expected funding is not forecast by the wallet and stays outside the budget.
- **Liquidation reserve.** As in v2, the liquidation price reserves maintenance margin plus the closing taker fee.

### Fills: scale-in and partial exit

A `fill` event `{setup_id, fill_id, direction: increase|reduce, qty, price, liquidity?}` is quantity-exact:

- **increase**: weighted average entry `(Q0*E0 + q*P) / (Q0 + q)`, margin `q*P / leverage` added (rounded up like
  the open), taker fee on the fill, the same exposure and margin checks as an open (a rejection is a
  `rejected` line with the cap's reason and does not change the position). The liquidation price is recomputed
  from the new quantity, entry and isolated margin.
- **reduce**: realized P&L `side * q * (P - E)` on the reduced part only (the average entry never moves),
  one fee for the fill, margin released pro rata (`floor(margin * q / Q)`, the whole remainder on the last
  reduction), liquidation price recomputed. Reducing the whole quantity closes the position.
- A reduction larger than the open quantity, a quantity <= 0 or a fill for no open position is a `rejected`
  ledger line with a reason (`over_close`, `invalid_fill_quantity`, `no_open_position`) and moves no money.
- **Idempotent by `fill_id`**: any outcome, even a rejection, spends the id; replaying it adds nothing.
- Market-style fills (`liquidity` taker, the default) pay the taker fee and the slippage floor against the
  position; `maker` fills pay the maker fee and no slippage.

Every v3 ledger line can also link its run, position and source event (`ledger_links: true`): `run_id`
(configuration), `position_id` (one position per setup in one-way mode) and `source_event`
(`{stage_run_id, symbol, sequence}` of the market event, or `{type, ms}` for derived events such as a
path liquidation or a direct fill).

### Reconciliation

`reconcile(initial, ledger, state)` (`wallet_reconcile.py`) is pure and **reports; it never corrects**:
consecutive `seq`, running balance of every line, state balance and seq against the ledger, used margin equal
to the sum over open positions, no negative available margin, no negative (or empty) open quantity, reserved
margin equal to the working orders, no duplicated fill or order line, and the totals
(`initial + realized + fees + funding + liquidation = balance`). It works for v2 and v3 ledgers.

The wallet service endpoint returns `reconciliation` (this call's lines against the state it started from) for
v3, and `full_reconciliation` whenever the request carries `reconcile: {initial_balance_e8, ledger}` (send
`events: []` to check a stored state). `npm run mvp:check` runs the whole-ledger check for every account and
prints **counts only** (PASS, FAIL on any mismatch, WARN when the Python service is not configured or the ledger
could not be read).

### Contract inputs, tiers and liquidation (v3, #36 Part 2)

`python/market_analysis/forward/data/contract_inputs_v1.json` (loader: `contract_inputs.py`) holds, for the six
frozen symbols, the maintenance-margin tiers (notional bracket, rate, maintenance amount, max leverage), the
taker/maker fees, the liquidation fee rate and the tick/step/minimum-notional filters, each with its source. The
SHA-256 of its canonical JSON is recorded in the run configuration (`contract_inputs_hash`) and on every ledger
line; selecting it is `contract_inputs: true`. A stored configuration may carry `contract_inputs_version` and
`contract_inputs_hash` (the run record does); a different file is refused, since changing the inputs is a new
run. The signed Binance leverage-bracket endpoint is not used.

- **Owner-supplied (#36, used exactly):** liquidation fee 1.25% of notional for BTC, ETH, BNB, XRP and 1.5% for
  DOGE, SOL; BTCUSDT tier 1: 0.40% maintenance margin up to 300,000 USDT, max leverage 150x.
- **Placeholders (conservative, labelled `placeholder-tier` on the ledger line):** every other tier value: 1%
  maintenance margin, 20x, one bracket up to 10,000,000 USDT (BTCUSDT above 300,000 USDT likewise, its maintenance
  amount chosen so `notional * mmr - cum` is continuous at the edge). A notional above the last bracket is
  *unavailable* and the setup is rejected (`notional_above_tier_table`); nothing is extrapolated. Without
  contract inputs the old flat 1% tier applies and lines carry `flat-tier`. Owner-supplied tier lines carry
  `owner-tier`.
- **Not supplied:** the filters. `null` means "not enforced" (never an invented number); when set, the quantity is
  floored to the step (`below_step_size`), a notional under the minimum is rejected (`below_min_notional`) and a limit
  price must sit on the tick grid (`price_off_tick`).
- **Bracket of the position's notional.** The quantity is sized first (risk / stop distance), then the bracket of
  that notional is looked up. Liquidation price (one-way, isolated):
  `LP = (WB + cum - s*Q*EP) / (Q*MMR - s*Q)` with `WB` the isolated margin, `cum` the bracket's maintenance
  amount, `s = +1` long and `-1` short; at the liquidation price equity equals `Q*LP*MMR - cum`. For
  tier 1 (`cum = 0`) a long's `LP = EP (1 - 1/L) / (1 - MMR)`. The leverage selector takes the largest integer
  leverage, at most the configured cap and the bracket's maximum, whose liquidation price is at least
  `liquidation_buffer` x the stop distance from the entry: `1/L >= MMR + (need*Q*(1 - s*MMR) - cum) / N`.
  A scale-in re-looks-up the bracket and is rejected if the position's leverage exceeds the new bracket's maximum.
- **Reproducing the ticket's example.** BTC at 73,000 with MMR 0.40% (before fees): 7x, 10x and 20x longs liquidate
  13.94%, 9.64% and 4.62% below entry (the ticket's "about 13.9 / 9.6 / 4.6"), asserted by
  `test_the_ticket_example_distances_are_reproduced`. The wallet's own liquidation price is slightly *closer*
  (about 0.045 percentage points at 10x), because, as in v2, its maintenance rate also reserves the closing taker
  fee (`MMR + taker`, 0.40% + 0.05%); the ticket's numbers exclude fees.
- **Liquidation charge** uses the symbol's liquidation fee (capped, as before, at the isolated margin). Liquidation
  is still checked on mark-price extremes before last-price stops (the adverse-path mechanism is unchanged); the
  mark is the trade-price proxy until the collector stores a mark.
- Not changed: the leverage rule stays "largest leverage inside the buffer". The later #36 idea of the *lowest*
  leverage that fits a margin budget, funding moving the liquidation price (funding is debited from the wallet
  balance, as in v2) and the fractional-Kelly risk fraction are not implemented.

### Portfolio limits (v3)

Run-configuration options, off by default (`max_open_risk_fraction`, `cluster_risk_fraction`; or
`portfolio_limits: true` for the defaults). Open risk of a position or working order is `qty * |entry - stop|`
(its loss at the initial stop, before costs).

- **Total open risk** at most 5% of the sizing base (owner default of #36; `DEFAULT_MAX_OPEN_RISK`):
  rejection `cap:total_open_risk`.
- **Cluster:** same-direction positions across the six coins count as one cluster; the open risk of one
  direction is capped (`cap:cluster_risk`). The default (3%) is a **placeholder**, not an owner decision.
- The existing caps (`cap:max_positions`, `cap:total_exposure`, `insufficient_margin`) keep their names and
  order; working limit orders count in every limit. Every rejection is a `rejected` ledger line with its reason.

### Orders (v3)

`order` events (`order_type` market or limit) write a lifecycle in the ledger: `order_submitted`, then
`order_accepted` or `order_rejected` (with the reason), then `order_filled` / `order_partially_filled`,
`order_cancelled` (`cancel` event), `order_expired` (`expires_ms`, applied by the next event after it), and
`order_ambiguous`. Money is only ever on the fee/P&L lines; lifecycle lines have amount 0. An order id is spent
for good: replays do nothing, and a second order for a setup that already has a position or a working order is
rejected (`setup_already_used`). A market order fills at once like an `open` (taker fee, slippage floor).

A limit order is passive only (a buy below, a sell above the decision price; otherwise `limit_not_passive`). It
reserves its margin and maker fee (`reserved_e8`, so concurrent setups cannot spend it twice) until it is filled,
cancelled or expired. **Fill rule (conservative; ambiguity is marked, never resolved in the order's favour):**

1. Evidence is a `tape` event: one *completed* minute of trades for a symbol, stamped at its close. The minute
   must have begun at or after the order's submission: no data from before the decision, and nothing after the
   event's own time is ever used (events are processed in time order and the wallet reads nothing else).
2. The traded price must **cross** the limit: `low < limit` for a buy, `high > limit` for a sell. Touching it does
   not fill the order (equality proves no queue position).
3. With `through_qty` (the quantity traded strictly through the limit, e.g. from aggTrades) the fill is
   `min(remaining, through_qty)`, possibly partial; the orders of one symbol share that quantity in order-id order.
   With bars only there is no quantity: the whole remainder fills only when the minute *closed* strictly beyond
   the limit. If the price crossed but closed back (or `through_qty` is 0) the order stays working and one
   `order_ambiguous` line records it.
4. Fills are at the limit price (no slippage, no price improvement) with the **maker** fee. Each minute is
   considered once per order, so replaying a tape is a no-op.
5. If the fill minute's range also reached the order's stop, the fill line carries
   `ambiguity: "stop_touched_in_fill_minute"`; the position's clock starts at the fill minute's open, so that
   minute's mark extremes are inside the liquidation scan (conservative).

The forward harness does not emit limit intents yet; `order`/`tape` events are for the wallet service and the
research runs. The ledger kinds `stop_moved` and `target_moved` are reserved for #221 (trailing and break-even
exits): they exist in the Python, SQL (`20261016180000_paper_ledger_order_kinds.sql`) and TypeScript lists
(checked equal by a test) but nothing emits them.

### Paper account page (#36 Part 3)

`/paper` ("Paper account", `src/components/paper-account.tsx`, view models in `src/lib/paper/paper-account.ts`) is the
money view of the simulated wallet. It is a top-level navigation item between **Strategy Lab** and **Alerts**: the
Strategy Lab answers "does a strategy work" (results by strategy, signals, setups, outcomes), this page answers "what
does the account hold and do"; a separate page keeps both readable on a phone. The Strategy Lab's former wallet
panels (balance curve, open positions, last-20 rejections) and its Ledger log tab moved here, so nothing is shown
twice; the Lab keeps a one-card link.

- **Safety boundary.** A "Simulated account: fake money" note sits at the top (and in the page eyebrow). Every
  figure is simulated; no credential or order endpoint is involved.
- **Header strip.** Balance, equity, available margin, margin used, unrealized P&L, realized P&L, fees, funding;
  each term has a hint. Gains and losses always carry a sign and an arrow, never colour alone; money is
  tabular-numeral monospace in USDT (2 decimals from 1 USDT, 4 below).
- **Equity and drawdown.** The balance history (one point per hour, per day beyond 60 days, from
  `paper_equity_series`) with equity now as the last point, and the drawdown from the running peak. The history is
  the realized balance: unrealized P&L is only known now, because marks are not stored.
- **Tabs.** Open positions (side, quantity, entry, mark, unrealized P&L, margin, leverage, liquidation price and
  distance, stop, target); Closed trades (gross, fees, funding, net, holding time, outcome: target, stopped,
  liquidated, time limit, unclear or closed early); Rejected (setups, orders and fills the wallet turned down, with
  the reason); Ledger (every line, with the order/fill ids, CSV export). Lists are keyset-paginated by ledger `seq`
  (newest first) and reload their first page when the wallet's last update changes.
- **Mark price.** The wallet's own mark when its state has one (wallet-v3 `mark` events), otherwise the latest
  published trade price (`market_quotes`), labelled "published price"; a mark older than 3 hours or a price older than
  10 minutes is flagged stale; a position with no price is valued at its entry and counted in a note.
- **Results of this run.** From `paper_account_summary()` (SQL over every ledger line, no row cap): completed,
  rejected, target, stopped, liquidated, unclear, time-limit and closed-early counts, expectancy, profit factor,
  max drawdown, fees, funding, slippage (wallet-v3 totals only; "not recorded" for v2), liquidation losses and margin
  utilization, each with a hint, plus the frozen run configuration. A hint states that filtering a report never
  recreates the wallet.
- **Breakpoints (Tailwind).** Below 640 px: the cards are a 2-column grid; every list is a stack of cards with the
  key fields and a 44 px expand control; buttons and tabs are at least 44 px high; the tab bar is sticky under the
  header; no horizontal page scroll. From 640 px: lists are tables (scrolling inside their own container if wider
  than the screen) and the cards form a 4-column strip. From 1024 px the strip is one row of 8. Charts and loading
  skeletons share one reserved height, so nothing jumps.
- **States.** Every list has loading (skeleton at the final height), empty and error (message and retry) states; the
  page shows a stale chip with the wallet's age (same 2 h / 6 h thresholds as `npm run mvp:check`).
- **Data.** The user's own RLS reads through server functions (`paper.functions.ts`), refreshed once a minute like the
  Strategy Lab; mapping is pure browser code, nothing heavy runs on the Worker.

## Outcome vocabulary and stop definition

For benchmark-equivalent setups ([analysis and evaluation](analysis-evaluation.md#setup-definition-the-benchmark-barrier-event)),
the simulator reports the same outcomes: T (target first), S (stop first), E
(time-limit exit at 4 x horizon), L (liquidation) and X (ambiguous intrabar order),
in R units gross and net of costs. The stop is k x horizon sigma, entry is the next
minute's open, and X is resolved pessimistically for headline numbers and always
reported as its own share. Success is a profitable trade after costs on the stop-aware
path. Wallet-level results (sizing, leverage, capital conflicts) are a portfolio layer
over strategies that passed the benchmark; they are not evidence of edge by themselves.
The 2% stop in the sizing example above is arithmetic illustration only.

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
- Keep real-money decisions and order placement manual for now (an owner decision,
  not a permanent exclusion).
