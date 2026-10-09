# Analysis records, conditional setups, and evaluation

This document defines how Crypto Watch records market analysis, proposes
conditional futures opportunities, scores them, and evaluates their effectiveness.
The separate [futures paper-trading simulator](futures-simulation.md) consumes these
records to test virtual-account performance.

## Current saved records

The current implementation has several histories, but they do not yet form the
intended conditional-trade evaluation system.

| Current record                                | What is saved now                                                                                                                              | Current purpose and limit                                                                                                                |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Movement alerts (`alerts`)                    | Futures instrument ID, observed change, saved-baseline comparison, rule, threshold, trade price, endpoint/source, timestamps, and test status  | Searchable alert history with CSV export. It records threshold movements, not conditional trade setups or strategy outcomes.             |
| Monitor runs (`monitor_runs`)                 | Run time, status, symbols checked, alerts created, source, and error text                                                                      | Operational evidence that a monitoring pass ran or failed. It is not a market assessment or profitability record.                        |
| TA snapshots (`ta_signals`)                   | Canonical/source futures identities, completed trade-price candle, indicators, persisted score/factors/reasons, versions, and source/evaluation/detection times | Immutable descriptive 15m/1h/4h technical evidence from the shared Python calculator. It is not a conditional trade setup. |
| TA forward outcomes (`ta_signals`)            | Pending/measured/unavailable status, future close, evaluation time, and return percentage                                                      | A fixed-horizon forward-price observation. It is not event-ordered TP/SL execution or profit after costs.                                |
| Dashboard TA interpretation                   | The saved `interpretation-v1` classification, score, factors, reasons, and indicator explanations                                              | The browser renders the persisted conclusion and does not recalculate its score. There is no current score-band effectiveness report.     |
| Analysis conclusions (`analysis_conclusions`) | Immutable futures identity, conclusion, score/factors, versions, timestamps, freshness, and input reference                                    | Append-only activity logging for authorized server writers. Manual Python analysis remains read-only.                                    |
| Monitor baselines (`monitor_baselines`)       | Futures instrument ID, saved trade-price comparison/time, endpoint/source, threshold, last observation, and directional cooldown times         | Mutable operational state for cumulative movement detection, not an audit history or prediction log.                                     |

Application/process diagnostic logs may also be emitted, but they are not durable
product evidence. There is currently no stored developing-setup lifecycle, general
outcome/effectiveness log, or simulated-wallet ledger. Scheduled completed-candle
conclusions remain in `ta_signals` because that table also owns their fixed-horizon
outcome lifecycle. The broader append-only `analysis_conclusions` contract remains
available for other analysis activity.

## Intended record model and histories

Keep these records linked by stable IDs and versions, but store their different
meanings separately:

| Intended record   | Required purpose                                                                                                                                                                         |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Assessment/signal | What was observed or concluded at the time, including score, factors, direction, evidence, prices, timestamps, freshness, and calculation versions                                       |
| Conditional setup | The future conditions proposed from that assessment: premise, activation, continued validity, invalidation, entry, management, expiry, and evaluation rules                              |
| Outcome           | What happened under the exact saved setup: pending, activated, entered, completed, stopped, liquidated, expired, invalidated, never triggered, rejected, ambiguous, or insufficient data |
| Simulation ledger | The separate simulator's orders, fills, fees, funding, margin, P&L, and account changes for a particular wallet/run                                                                      |

Later outcomes, recalculated scores, revised algorithms, and simulated execution
must not overwrite the original assessment or setup. A setup can be evaluated even
when one simulated wallet lacked capital to trade it.

These records support three user-facing histories:

1. **Activity and opportunity history:** movement alerts, assessments, saved factor
   snapshots, developing setups, and condition/state changes.
2. **Outcome and effectiveness history:** whether comparable setups activated and
   how they completed under their saved evaluation contracts, filterable by date,
   contract, direction, strategy/version, timeframe, score, and status.
3. **Simulation account history:** orders, fills, positions, costs, rejected
   opportunities, balance, margin, and equity for one virtual-wallet run. Its
   accounting rules belong to the [simulator specification](futures-simulation.md).

The effectiveness history measures analysis and strategy quality independently of
one wallet's capital. The simulation account history measures whether an automated
selection and execution policy could grow or deplete a constrained account.

## Conditional trade prediction

A prediction is a **time-bounded, conditional trade plan** derived from a specific
technical, movement or mixed assessment (news/event inputs are parked until
news ingestion, #29, is unparked). Its entry,
take-profit, stop-loss, and invalidation levels are valid only within the evidence
and pattern conditions under which the strategy calculated them.

For example, if 55,000 is an entry level for a named chart-pattern setup, price
reaching 55,000 does not activate the trade unless that pattern and its required
confirmation occur while the setup remains valid. Each strategy version defines:

- the originating assessment, premise, and immutable evidence snapshot;
- the evidence that makes a setup eligible;
- the activation condition and validity period;
- which originating conditions must still hold at activation and fill;
- the executable-entry rule after activation;
- stop, target, partial take-profit/stop-loss, permitted stop adjustments,
  branching, fallback, and time-exit rules;
- invalidation, expiry, and liquidation behavior;
- the market-event and price types used by every trigger;
- the outcome contract fixed before later events are observed.

Current assessment, movement alert, developing setup, activation, and filled
position are different states. A movement alert can contribute evidence without
becoming a trade. Activation records that all required conditions occurred; a
position exists only after an executable fill.

## Setup definition: the benchmark barrier event

> The owner's intent in [`CLAUDE.md`](../CLAUDE.md) section 2 is the source of truth.
> This section follows the stop-aware benchmark ([#182](https://github.com/ibrahimjaved12/crypto-watcher/issues/182))
> and the reset tickets #31, #32, #38 and #43 ([#189](https://github.com/ibrahimjaved12/crypto-watcher/issues/189)).

A **setup** is the same stop-aware barrier event the benchmark tests, so that a
strategy's benchmark result describes the setups it produces:

| Element      | Rule                                                                                                                                        |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Scope        | BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, DOGEUSDT, XRPUSDT; horizons 15m, 1h, 4h; long and short always (1m/5m are inputs and diagnostics only) |
| Decision     | A registered strategy version decides at `signal_ms`, the end of minute d, from point-in-time inputs only                                   |
| Entry        | At the open of the next minute; intended price, fill price, spread and slippage are recorded                                                |
| Stop         | k x horizon volatility (sigma), same estimator as the benchmark labels; trigger price type recorded                                         |
| Target       | A reward-to-risk multiple of the risk distance (or a named level), fixed at creation                                                        |
| Time limit   | 4 x horizon after entry, then exit at the next executable price                                                                             |
| Costs        | Fees, funding, slippage and liquidation per [the simulator specification](futures-simulation.md), at 1x and 2x cost                         |
| Invalidation | An untriggered setup is cancelled at expiry, or as soon as its premise or a stated invalidation rule fails                                  |

Partial exits, stop adjustments and branching are allowed only when fixed at creation
and modelled by the same label code; they are not part of the benchmark-equivalent core.
Short setups use direction-correct mirrored rules. A price level is one part of a setup,
not a reusable order instruction: a level cannot activate a trade whose premise never
confirmed or has become invalid, and "otherwise exit at X" needs a defined time/event
and executable-price rule.

### Validation status

Every setup carries a status shown next to its score:

- `validated`: the strategy version passed the benchmark gate. The setup stores the
  benchmark question id and the report hash.
- `unvalidated`: a rule we run without proven edge. A setup with no benchmark link is
  unvalidated.

Only a strategy version registered in the experiment log can create a setup, and a
live setup must be reproducible by the benchmark labels from the same inputs.
Portfolio and paper-trading results are never presented as evidence of edge for an
unvalidated strategy.

## Evidence roles and strategy families

Every setup distinguishes:

- **Creation evidence:** the assessment and factors that caused the opportunity to
  be proposed.
- **Activation conditions:** the future pattern, price, volume, news/event, or
  other confirmation required before expiry.
- **Continued-validity conditions:** the originating conditions that must remain
  true through activation and entry.
- **Invalidation conditions:** evidence or events that cancel the setup even when a
  numeric entry level is later touched.
- **Post-entry management policy:** whether the premise is no longer re-evaluated
  after fill, or whether named evidence changes adjust a stop/target or exit.

Technical, movement-driven, news-driven, and mixed strategies can assign different
roles and weights to their inputs. A news-driven strategy may depend little on TA;
a chart-pattern strategy may require its pattern and confirmation to remain valid.
Known scheduled events may provide context, but their future content or effect is
not treated as known.

The state machine is deterministic and driven by versioned rules and ordered market
events. AI is not required to create or manage these setups.

## Outcome evaluation

Success is a **profitable trade after costs on the stop-aware path**. Price eventually
touching a target is not a success when the stop (or liquidation) came first, and any
metric that ignores the stop is misleading. Outcomes use the benchmark label rules, so
the app and the benchmark cannot disagree:

| Code | Meaning                                                      |
| ---- | ------------------------------------------------------------ |
| T    | Target reached first                                         |
| S    | Stop reached first                                           |
| E    | Time-limit exit (4 x horizon)                                |
| L    | Liquidation                                                  |
| X    | Ambiguous: intrabar order of stop and target cannot be known |

- Results are in **R units** (multiples of the risk at the stop), gross and net of
  costs, at 1x and 2x cost.
- Ambiguity is never resolved optimistically. X is resolved pessimistically for
  headline numbers and always reported as its own share. Prefer finer trade or
  aggregate-trade data when ordering matters, and record the data resolution.
- Non-trade statuses (volatility not ready, compromised data, incomplete window,
  off-tick entry price, never triggered, expired, invalidated, rejected/unexecutable,
  insufficient data) stay separate and are never counted as wins or losses.
- Hit rate is always shown next to the **barrier base rate**: ranging coins revisit
  prices, so a benchmark any naive strategy passes proves nothing. Always show sample
  sizes and non-trade counts, per direction and per horizon.
- Fixed-horizon returns, MFE and MAE stay auxiliary research numbers and never replace
  the trade-path outcome.
- Aggregate only across compatible strategy and evaluation versions, with the
  denominator and excluded statuses visible.

Partial exits contribute their actual quantities, fills and costs when execution data
exists. Late data appends a correction under a new outcome version; earlier results
are preserved.

## Scores and effectiveness reports

The general trade/setup score combines the factors declared by its scoring version.
Save the total score and each factor's raw value, normalization, weight,
contribution, reason, and disqualifiers with the original assessment. A score such
as 90/100 is a ranking, never a win probability, until separately calibrated and
validated out of sample.

Reports support date range, contract, direction, strategy/version, timeframe,
outcome status, and score range. Default comparison bands are 60–69, 70–79, 80–89,
and 90–100. For each band, show:

- signal/setup, activation, and completed-trade counts;
- setup success rate under the named outcome contract, next to the barrier base rate;
- average net P&L per completed trade when costed execution exists;
- average win and average loss;
- cumulative net return and maximum drawdown for coherent wallet runs;
- fees, funding, slippage, liquidations, ambiguity, and sample-size warnings where
  applicable.

This analysis asks whether higher-ranked opportunities perform better. For example,
if 90+ setups underperform 70–79 setups on unseen data, the scoring formula needs
review even though both bands may contain individual winners.

Filtering saved outcomes to score ≥80 is an analysis/report operation. Configuring
a simulator to accept only score ≥80 creates a new wallet run with different capital
availability, overlaps, rejections, and compounding. The two results must not be
presented as equivalent.

## Historical evaluation and research

Issue #17's fixed-fixture TA smoke replay remains a calculator check. Issue #35
Part 1 provides a separate [causal historical market replay core](historical-replay.md):
an explicit availability clock feeds canonical five-second movement buckets and
canonical market-movement evaluations, then exports chronological points for
the Issue #75 experiments. Part 2 adds a verified, local Binance USD-M daily
archive adapter that supplies Part 1's explicit input records. Part 3 runs the
fixed Issue #75 historical experiment suite over one replay and emits a compact,
reproducible research report. Part 4 acquires the required official Binance
daily archives into a verified local cache. This is comparative market-state evidence; it does not promote an algorithm or
measure portfolio P&L. Issue #123 (recommended to pause; its test days have no special status under the
CLAUDE.md splits) extends that work across multiple historical
periods and adds forward-information screening: it asks whether causally available
candidate evidence provides method-appropriate information about subsequent market
behavior beyond the unchanged V1 state. That screening is upstream of strategy
construction; it is not itself a trade, win-rate, or profitability claim.

A #75 candidate can therefore be useful without being a standalone directional
predictor. Normalization, change-detection, coordination, mark/trade, taker-flow,
open-interest, funding, and liquidation evidence may instead improve the state
representation consumed by later predictive rules. Promotion requires explicit
review of versioned untouched-period evidence; no candidate is adopted merely
because it was implemented or resembled V1 on a pilot.

Strategy and predictor evaluation uses the benchmark under the splits fixed in
CLAUDE.md (development 2024-01..2025-06, validation 2025-07..2025-12, hidden test
2026-01..2026-09 opened once per question with a committed plan, forward live from
2026-10); every variant tried counts in the private experiment log.

Technical-assessment composition, strategy/setup replay, funding/news/event adapters,
execution/outcome resolution, and portfolio simulation (#38) remain deferred.

Historical inputs initially include one-minute Binance USDⓈ-M futures candles,
finer trade or aggregate-trade data for ordering-sensitive periods, funding
history, required mark/index-price history, and versioned contract metadata.
One-minute candles can be aggregated into analysis timeframes deterministically.
Bulk market files remain outside transactional application tables; manifests,
checksums, experiment configurations, references, and compact results are stored.

Technical factors can be reconstructed only from the data and code version
available to the replay. Proprietary scores, news, sentiment, and fundamental/event
factors are replayable only when their timestamped inputs and corresponding
versioned calculations exist. Current information cannot be inserted into an old
evaluation.

Later AI-assisted review can inspect completed, reproducible experiments and propose
testable changes. For example: “High-score breakout setups underperformed during
versioned low-volume conditions; test this specific minimum-volume rule.” The change
becomes a new strategy version and is compared with the unchanged baseline on a
later untouched period. Research includes successful trades, failures, expired
setups, liquidations, and missed opportunities so it is not fitted only to losses.

### Sigma calibration audit

The barrier labels measure stops and targets in units of the label engine's
point-in-time sigma, so before more strategies are judged on those labels, the
audit checks that sigma is scaled correctly
([#220](https://github.com/ibrahimjaved12/crypto-watcher/issues/220) slice A, SPEC-1
sections 2 and 4). It is read-only: it does not change labels, the label engine or
any existing result. The **Calibration audit** workflow
(`.github/workflows/calibration-audit.yml`,
`python/market_analysis/benchmark/calibration.py`) runs on the development or
validation segment only; the hidden guard refuses the hidden segment. It covers the
six symbols, horizons 15, 60 and 240 minutes, and EWMA half-lives of 1, 3 and 7 days,
using the same label-step entries the label store uses:

- `z = ln(open[e+h] / open[e]) / sigma_h`, where `sigma_h` is `horizon_sigma` at
  signal time. The audit reports n, sd, mean |z|, quantiles (1 to 99%) and the
  shares with |z| > 1, 2, 3, overall and by UTC hour.
- The variance ratio `VR(q)` of valid, non-compromised 5-minute block returns,
  for q = 3, 12 and 48.
- A VR-corrected variant, `sigma_h * sqrt(VR(h/5))`, with VR estimated on an
  expanding window (point in time).
- At 240 m, the observed T/S/E/L/X shares from lb1 for k in {1, 2} and every rr.
  These are shown next to the driftless Brownian theory (no-limit target-first
  probability, and the expiry share at 4 x horizon, with continuous monitoring and
  with Broadie-Glasserman-Kou widening), plus the realized/predicted sigma ratio
  implied by the observed expiry share. Non-trade statuses are counted separately.

**Pass criteria** per symbol x horizon x half-life. The report shows two separate
verdicts, and PASS requires both:

- sd verdict: sd(z) is in [0.9, 1.1].
- Barrier-expiry verdict, only for the 240 m row at the labels' half-life
  (7 days): every (k, rr) expiry share satisfies
  `|observed - theory| <= max(10% x theory, 2 pp)`. Theory here is the
  BGK-widened (discrete-monitoring) value for the label step. Other rows report
  this verdict as n/a.

The full report (JSON + Markdown) is committed to `reports/calibration/` in the
private research-data repo. The public log shows only symbol, horizon, half-life,
n and PASS/FAIL.

## Strategy specifications

- [SPEC-0 master plan](strategy-specs/SPEC-0-master-plan.md)
- [Strategy catalogue](strategy-catalogue.md)
