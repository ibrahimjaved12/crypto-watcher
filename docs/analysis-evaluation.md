# Analysis records, conditional setups, and evaluation

This document defines how Crypto Watch records market analysis, proposes
conditional futures opportunities, scores them, and evaluates their effectiveness.
The separate [futures paper-trading simulator](futures-simulation.md) consumes these
records to test virtual-account performance.

## Current saved records

The current implementation has several histories, but they do not yet form the
intended conditional-trade evaluation system.

| Current record                          | What is saved now                                                                                                                 | Current purpose and limit                                                                                                                |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Movement alerts (`alerts`)              | Futures instrument ID, observed change, saved-baseline comparison, rule, threshold, trade price, endpoint/source, timestamps, and test status | Searchable alert history with CSV export. It records threshold movements, not conditional trade setups or strategy outcomes.             |
| Monitor runs (`monitor_runs`)           | Run time, status, symbols checked, alerts created, source, and error text                                                         | Operational evidence that a monitoring pass ran or failed. It is not a market assessment or profitability record.                        |
| TA snapshots (`ta_signals`)             | Futures instrument ID, completed trade-price candle, timeframe, indicator JSON, detected patterns, endpoint/source, TA version, and timestamps | Saved descriptive 15m/1h/4h technical evidence. It does not currently contain the intended immutable conditional setup contract.         |
| TA forward outcomes (`ta_signals`)      | Pending/measured/unavailable status, future close, evaluation time, and return percentage                                         | A fixed-horizon forward-price observation. It is not event-ordered TP/SL execution or profit after costs.                                |
| Browser TA interpretation               | A current `interpretation-v1` score and factor explanation calculated from each saved snapshot                                    | The score is displayed but not persisted as an immutable historically issued score. There is no current score-band effectiveness report. |
| Monitor baselines (`monitor_baselines`) | Futures instrument ID, saved trade-price comparison/time, endpoint/source, threshold, last observation, and directional cooldown times | Mutable operational state for cumulative movement detection, not an audit history or prediction log.                                     |

Application/process diagnostic logs may also be emitted, but they are not durable
product evidence. There is currently no stored developing-setup lifecycle, general
outcome/effectiveness log, or simulated-wallet ledger.

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
technical, movement, news, fundamental/event, or mixed assessment. Its entry,
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

## Concrete conditional-setup example

These illustrative rules are requirements examples, not trading advice:

| Stage               | Explicit rule                                                                                                                              |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Premise/evidence    | A named, versioned four-hour support-bounce assessment is developing from its saved technical, movement, volume, and other required inputs |
| Eligibility         | BTCUSDT perpetual, long direction, setup score at least 80 under a named score version                                                     |
| Developing setup    | Last price enters the predefined support zone around 74,120 before setup expiry                                                            |
| Confirmation        | A completed one-minute candle closes above 74,200 while the originating premise and required evidence remain valid                         |
| Activation          | Record the confirmation event and make the setup eligible for its configured entry rule                                                    |
| Entry               | Use the next executable market event under the configured order/fill model                                                                 |
| Initial protection  | Stop at 73,950 using the configured trigger price type                                                                                     |
| Partial exit        | Close 50% of filled quantity at 75,000, subject to the fill model                                                                          |
| Extension branch    | If a completed candle closes above 75,500 before the branch deadline, target 76,000 for the remainder                                      |
| Fallback branch     | If extension confirmation does not arrive by its deadline, manage or close the remainder under the predefined executable fallback rule     |
| Expiry/invalidation | Cancel an untriggered setup at expiry or as soon as its premise or stated invalidation rule fails                                          |

“Otherwise exit at 75,000” requires a defined time/event and executable-price rule.
Rules must be structured and versioned before evaluation. Short strategies require
equivalent direction-correct conditions.

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

Each strategy family defines success using the assessment and trade path it was
designed for. Evaluation checks whether required evidence remained valid, the full
activation condition occurred before expiry, an executable entry existed, and the
configured management rules completed. Partial exits contribute their actual
quantities, fills, and costs when execution data is available.

Preserve pending, expired, invalidated, never-triggered, rejected/unexecutable,
liquidated, ambiguous, and insufficient-data states instead of forcing every setup
into win/loss. Movement alerts may retain fixed-horizon observations for research,
but these remain separate from conditional trade outcomes. Aggregate success rates
only across compatible strategy and evaluation versions, always showing the
denominator and excluded statuses.

## Scores and effectiveness reports

The general trade/setup score combines the factors declared by its scoring version.
Save the total score and each factor's raw value, normalization, weight,
contribution, reason, and disqualifiers with the original assessment. A score such
as 90/100 is a ranking until separately calibrated and validated as a probability.

Reports support date range, contract, direction, strategy/version, timeframe,
outcome status, and score range. Default comparison bands are 60–69, 70–79, 80–89,
and 90–100. For each band, show:

- signal/setup, activation, and completed-trade counts;
- setup success rate under the named outcome contract;
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
