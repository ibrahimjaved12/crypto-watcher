# Market-state experiment registry

> **Provisional (2026-10-09).** The #123 state-study framing and the five-symbol pilot
> universe are superseded for predictor work by the stop-aware benchmark (#182) and the
> frozen six symbols (BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, DOGEUSDT, XRPUSDT). #123 is
> recommended to pause and its test days have no special status under the splits in
> CLAUDE.md. These methods are market-state descriptors that may still serve as
> features or filters; they are not trade predictors and make no profitability claim.

This registry preregisters the deferred Issue #75 candidates. Each candidate is
evaluated independently against the unchanged canonical #28 V1 event stream.
The current experiments implement **EXP-75-01 EWMA**, **EXP-75-02 CUSUM**,
**EXP-75-03 Kalman/state-space**, **EXP-75-04A offline PELT**,
**EXP-75-04B online Bayesian change-point detection**, and
**EXP-75-05 regression-slope acceleration**, **EXP-75-06A realized-volatility
normalization**, **EXP-75-06B ATR-SMA normalization**, **EXP-75-07
PCA/common-factor diagnostics**, **EXP-75-08
correlation, clustering, and network diagnostics**, and **EXP-75-09 Gaussian HMM
learned-regime diagnostics**. EXP-75-10 remains unimplemented in the fixed suite;
EXP-75-11 and EXP-75-12 are implemented as separate historical extensions.



## Research role and promotion boundary

These experiments are upstream evidence for the project's larger detection-and-prediction
pipeline. An individual candidate does **not** need to be a standalone directional
predictor to be useful. A method may instead improve normalization, state stability,
structural-change detection, market coordination, or flow/leverage context that later
helps distinguish what is likely to happen next when combined with V1/current-state
evidence.

Implementation in this registry does not imply adoption. A candidate may be promoted
into the canonical market-state or downstream predictive pipeline only after a
versioned multi-period study on untouched data demonstrates **method-appropriate
incremental value** beyond the unchanged V1 baseline. Issue #123 owns that comparative
robustness and forward-information evaluation.

The primary predictive-information question is therefore not universally "did this
candidate independently predict the next candle?" It is whether causally available
candidate evidence at time `t` changes the distribution of later returns, direction,
breadth, volatility, persistence, weakening, reversal, or another method-appropriate
outcome beyond what V1 already explains.

No study result automatically promotes a method. Any claim that an evidence
combination is an actionable trade setup additionally requires the versioned strategy,
event-order, execution, cost, and backtest evaluation owned by #31/#32/#38.

Issue #127's historical mark-versus-trade diagnostic is a separate descriptive
extension, not another fixed V1 experiment or a change to EXP-75-12. It pairs
verified #111 trade-price closes with separately verified Binance USD-M mark
price candles at exact minute-boundary replay points, with contiguous 1m
coverage required for 1m, 5m, and 15m windows. It reports source availability,
coverage, log returns, endpoint mark/trade bases, and their divergence alongside
same-time V1 context. Mark methodology may differ across history, so later #123
analysis should retain and review date/provenance strata. V1 remains a
comparator rather than ground truth; mark price is a reference rather than an
executable price. See the Issue #127 section in `historical-replay.md` for the
source-time surrogate, gap handling, identities, and CLI.

## EXP-75-01 — EWMA aggregate smoothing

- **Status:** `IMPLEMENTED_EXPERIMENT`
- **Hypothesis:** Smoothing only the primary 5m median normalized market movement
  may reduce short-lived V1 broad-state changes while introducing measurable
  onset delay. Any improvement must be evaluated against the unchanged V1 event
  stream on the same chronological data and universe.
- **Input/data prerequisite:** Consecutive canonical #71 evaluations with a
  finite 5m `median_normalized_movement`, plus point-in-time per-symbol source
  time evidence and an explicit development/validation/test partition.
- **Causal/live suitability:** Causal and replayable as a research transform;
  not wired into live behavior.
- **What changes relative to V1:** Only the 5m median normalized movement is
  replaced by a versioned EWMA using one of the preregistered 10s, 30s, or 60s
  half-lives.
- **What remains unchanged:** Per-symbol metrics, raw median return, breadth,
  material breadth, acceleration, RVOL, outliers, #72 thresholds, and #73
  hysteresis/lifecycle.
- **Evaluation measurements:** Direction-state disagreement; transition and
  episode counts; short-lived episode incidence; candidate onset lead/lag;
  same-direction episode overlap; and unavailable points.
- **Promotion constraint:** No automatic promotion or half-life selection. A
  later decision must consider development, validation, untouched test data,
  interpretability, data quality, delay, and stability.

## EXP-75-02 — CUSUM

- **Status:** `IMPLEMENTED_EXPERIMENT`
- **Hypothesis:** A causal two-sided CUSUM of the canonical #71 primary 5m median
  normalized market movement may identify persistent directional shifts before V1
  episode onsets. Any earlier detection is useful only if it does not create
  excessive unmatched detections, unstable short detection regions, or poor
  directional overlap with the unchanged V1 episode stream.
- **Input/data prerequisite:** The raw canonical #71 5m
  `median_normalized_movement` and point-in-time source evidence in an explicit
  chronological development/validation/test replay.
- **Causal/live suitability:** Causal and replayable as a research detector; it is
  not wired into live behavior. V1 episodes are the comparator, not ground truth.
- **What changes relative to V1:** A separate CUSUM detector uses the fixed
  `reference=0.0` parameter and one of the preregistered `(k, h)` pairs:
  `(0.10, 0.75)`, `(0.10, 1.50)`, or `(0.25, 1.50)`.
- **What remains unchanged:** The canonical #71 evaluation, #72 classifier, and
  #73 lifecycle branch. CUSUM never replaces or mutates a movement evaluation.
- **Evaluation measurements:** CUSUM detection regions and onsets, unmatched
  alarms, baseline onset lead/lag, same-direction overlap, baseline coverage,
  transition/episode counts, ambiguous/unavailable points, and short-lived closed
  regions.
- **Promotion constraint:** No parameter optimization or automatic promotion;
  unmatched alarms are not labelled statistical false positives.

## EXP-75-03 — Kalman/state-space

- **Status:** `IMPLEMENTED_EXPERIMENT`
- **Hypothesis:** A causal two-state local-linear-trend Kalman filter of the
  canonical #71 primary 5m median normalized market movement may reduce
  short-lived state changes while estimating a useful latent trend. Any benefit
  must remain observable across preregistered process-noise assumptions and must
  not come at excessive episode-onset delay relative to unchanged V1.
- **Input/data prerequisite:** Consecutive canonical #71 evaluations with the raw
  5m `median_normalized_movement`, point-in-time source evidence, and explicit
  development/validation/test partitions.
- **Causal/live suitability:** Causal and replayable as a research transform; it
  is not wired into live behavior. V1 is the comparator, not ground truth.
- **What changes relative to V1:** Only the candidate 5m median normalized
  movement is replaced by the filtered Kalman latent level. The level/trend state
  uses the fixed two-state transition and observation models with one of three
  preregistered process-noise variances.
- **What remains unchanged:** Canonical #71 metrics and all per-symbol evidence;
  breadth, acceleration, pace, raw return, RVOL, outliers, #72 rules, and #73
  lifecycle behavior.
- **Evaluation measurements:** Direction-state disagreement, transition and
  episode counts, short-lived episodes, candidate onset lead/lag, same-direction
  overlap, covariance sensitivity, and innovation/filter-gap/trend diagnostics.
- **Promotion constraint:** No automatic promotion or covariance selection. Each
  fixed configuration is an independent research run evaluated across development,
  validation, and untouched test data.

## EXP-75-04A — Offline PELT mean-shift segmentation

- **Status:** `IMPLEMENTED_EXPERIMENT`
- **Hypothesis:** Offline exact segmentation of the canonical #71 primary 5m
  median normalized market movement may identify structural mean-shift boundaries
  that provide useful independent historical context for evaluating V1 lifecycle
  transitions. Usefulness requires reasonably stable boundaries across
  preregistered penalties and meaningful temporal proximity to V1 events; these
  hindsight labels must never be treated as causal detections or live features.
- **Input/data prerequisite:** The raw canonical #71 5m
  `median_normalized_movement`, split into contiguous compatible scope blocks;
  unavailable values and scope changes end a block.
- **Causal/live suitability:** Offline hindsight segmentation only. Later
  observations may revise historical boundaries; PELT labels are never live
  features or causal detection times. V1 is the comparator, not ground truth.
- **What changes relative to V1:** Nothing in V1. PELT independently segments
  the scalar aggregate under a piecewise-constant mean SSE cost with a fixed
  six-point minimum segment length.
- **What remains unchanged:** Canonical #71 values, #72 classification, and #73
  lifecycle processing.
- **Evaluation measurements:** Segment and change-point counts, structural mean
  deltas, onset proximity within a fixed ±60-second matching window, unmatched
  historical labels, and V1 episode diagnostics across separate partition views.
- **Promotion constraint:** No automatic penalty selection or live promotion.
  PELT labels are model-dependent historical context, not ground truth.
- **Preregistered penalties:** `beta=1.0` (more sensitive), `beta=2.0`
  (middle), and `beta=4.0` (more conservative), each with six observations per
  segment. No winner is selected in code.

## EXP-75-04B — Online Bayesian change-point detection

- **Status:** `IMPLEMENTED_EXPERIMENT`
- **Hypothesis:** A causal posterior over recent structural-change timing may
  provide useful point-in-time evidence to compare with unchanged V1 episodes.
  V1 is the comparator, not ground truth; BOCPD evidence is not a trade signal,
  prediction, or replacement classifier.
- **Input/data prerequisite:** Only the raw canonical #71 5m
  `median_normalized_movement` from explicit chronological experiment points.
  The model does not consume another experiment's output or any future state.
- **Model:** A scalar Gaussian observation model with unknown segment mean and
  known observation variance. The prior is `mu ~ Normal(0, 4)` and each
  observation is `Normal(mu, 1)`; variance is fixed, not estimated. Conjugate
  predictive densities and the standard constant-hazard run-length recursion
  are calculated in log space.
- **Preregistered hazard configurations:** Expected run lengths are 30, 60, and
  120 points, with hazards `1/30`, `1/60`, and `1/120` respectively. At the
  fixed 5-second cadence, these correspond to prior run durations of 150, 300,
  and 600 seconds. No winner is selected in code.
- **Evidence state:** The exact, untruncated posterior retains run lengths
  `0..N`. The directionless `recent_change_probability` is
  `P(run_length_steps <= 2)`. The first six consecutive usable observations are
  `WARMING`; afterward `CHANGE` requires recent-run mass >= `0.50`, with
  `NONE` otherwise. Run length zero alone is only a hazard diagnostic.
- **Causal/live suitability:** Online and causal as a research transform, but
  exact posterior state grows with uninterrupted history. This is a research
  reference only and is not wired live; indefinite live retention would require
  a separately versioned and exactness-tested approximation.
- **What changes relative to V1:** Nothing. BOCPD is an independent, directionless
  evidence stream. Only V1 runs through canonical #71 → #72 → #73.
- **What remains unchanged:** Canonical calculations and lifecycle rules; BOCPD
  creates no candidate market evaluation, classifier branch, or lifecycle branch.
- **Evaluation measurements:** Change-boundary and detection-region incidence,
  short-lived closed regions, recent-change and expected-run-length diagnostics,
  hazard sensitivity, unmatched regions, and signed lead/lag to V1 `STARTED` and
  `REVERSED` onsets within a one-to-one ±60-second matching window. Unmatched
  regions are not labeled false positives.
- **Partition discipline:** Development, validation, and test summaries use
  causal cutoffs. Earlier evidence remains available to later partition state;
  later events cannot alter an earlier summary, and regions open at a cutoff are
  censored there.
- **Promotion constraint:** BOCPD is evaluated separately from offline PELT and
  other experiments. There is no automatic hazard selection or live promotion.

## EXP-75-05 — Regression-slope acceleration

- **Status:** `IMPLEMENTED_EXPERIMENT`
- **Hypothesis:** A causal uniformly weighted OLS slope of each symbol's
  canonical 5m velocity may provide less boundary-sensitive primary
  acceleration/pace evidence than V1's adjacent-window acceleration. Benefit
  requires improved pace/strength stability without excessive estimator lag or
  unavailability, while broad direction and directional episode boundaries
  remain unchanged.
- **Input/data prerequisite:** Consecutive five-second canonical #71 5m
  per-symbol velocity observations (log return per second), not raw
  intra-window price history. Preregistered windows contain 12, 36, or 60
  points, each with uniform-weight OLS against five-second timestamps.
- **Causal/live suitability:** Each candidate uses only observations through
  the current boundary; missing or excluded symbol velocity resets that
  symbol's bounded history.
- **What changes relative to V1:** Included symbols' 5m acceleration metric
  only. The candidate evaluation has a separate movement algorithm/config
  identity and uses the same canonical #72 classifier and #73 lifecycle.
- **What remains unchanged:** All 1m and 15m metric values, 5m direction
  inputs, market eligibility, breadth, classifier thresholds, lifecycle rules,
  and the baseline branch.
- **Evaluation measurements:** Regression warm-up and availability, primary 5m
  pace disagreement, acceleration sign and breadth differences, matched pace
  changes within a one-to-one ±300-second causal window, and downstream
  `STRENGTHENED`/`WEAKENED` counts. Directional episode boundaries are an
  invariance check, not a lead/lag outcome.
- **Promotion constraint:** The three fixed window lengths remain independent
  research candidates. No automatic selection or live promotion.

## EXP-75-06A — Realized-volatility normalization

- **Status:** `IMPLEMENTED_EXPERIMENT`
- **Hypothesis:** Scaling each symbol's canonical returns by causal realized
  volatility estimated from synchronized, non-overlapping completed 1m returns
  may produce more comparable normalized magnitudes across instruments than
  historical MAD scaling. Any benefit must appear as improved cross-asset
  normalization consistency and useful regime stability without excessive
  warming, outlier masking, or unstable V1 state changes.
- **Input/data prerequisite:** Canonical #71 1m `current_return` sampled only at
  boundaries divisible by 60,000 ms. Every other five-second rolling 1m return
  is ignored. The 30, 60, and 120-minute configs require that many consecutive
  prior synchronized one-minute samples per symbol.
- **Estimator:** `sigma_1m = sqrt(sum(prior_1m_return²) / N)` with no centering,
  annualization, Bessel correction, or exponential weighting. For a `w`-minute
  horizon, `sigma_w = sigma_1m * sqrt(w)` and candidate
  `normalized_z = (current_return - canonical_historical_median) / sigma_w`.
  The current return is appended to history only after the candidate at that
  boundary is calculated, so it cannot normalize itself. There is no MAD
  conversion factor `0.6745` in the candidate formula.
- **What changes relative to V1:** Only the normalization method and derived
  direction, material flags, normalized breadth/aggregates, and outlier status
  across 1m, 5m, and 15m. Canonical #71 pure helpers recompute the derived
  evidence; canonical #72 and #73 process both independent branches.
- **What remains unchanged:** Returns, velocities, acceleration, historical
  center and MAD diagnostic, notionals/RVOL, raw cross-sectional z, included
  universe and eligibility, and V1 classifier/lifecycle rules.
- **Evaluation measurements:** Availability, normalized magnitudes, primary
  cross-asset median-absolute-z dispersion, material breadth, paired outlier
  changes, broad direction, episodes, onset timing, and active overlap. These
  descriptive comparisons do not rank or promote a lookback.
- **Promotion constraint:** Three fixed lookbacks are independent candidates;
  no automatic selection or live integration.

## EXP-75-06B — ATR / range normalization

- **Status:** Implemented in the separate historical ATR extension suite. The
  original 28-run suite and its identities are unchanged.
- **Frozen hypothesis:** Actual completed Binance USD-M trade-price 1m ranges
  provide a different causal scale from 06A's close-to-close RMS. The three
  fixed lookbacks are 30, 60, and 120 valid true ranges. No lookback is tuned
  or promoted from the pilot.
- **True range:** For each candle with an eligible immediately preceding minute,
  TR = max(high-low, abs(high-previous_close), abs(low-previous_close)) in price
  units. A missing or not-yet-visible previous close makes that TR unavailable;
  a zero TR is a valid sample. Require the newest expected minute and N
  consecutive TRs, hence N+1 adjacent available candles. Do not bridge gaps
  or carry an older ATR across a missing newest minute.
- **Estimator:** ATR_SMA_N = mean(last N TRs),
  relative_ATR_N = ATR_SMA_N / latest_eligible_close, and
  scale_(N,w) = sqrt(pi/8) * relative_ATR_N * sqrt(w) for w=1,5,15.
  The candidate score is
  (V1_current_log_return_w - V1_historical_median_w) / scale_(N,w).
  The fixed sqrt(pi/8) is an ideal-diffusion range calibration assumption,
  not a fitted constant. The candidate has no V1 MAD multiplier. Its versioned
  config specifies SMA, divisor, calibration, horizon scaling, 50-digit Decimal
  division, and strict timing. This is neither Wilder smoothing nor a
  Parkinson estimator.
- **Causal timing:** At boundary t, every candle and its preceding close must
  have first_seen_at_ms < t. The candle whose minute completes exactly at t
  is excluded. The newest permitted minute end is
  floor((t-1 ms)/60,000 ms) * 60,000 ms. Pre-output archive candles may warm
  06B, and ATR history continues across research partitions. An older candle
  becoming visible later can change later snapshots only.
- **What changes:** The score, direction/material flags, outliers, breadth,
  aggregates, classification, and independent episode lifecycle. V1 returns,
  historical center, thresholds, source-time evidence, included universe,
  classifier settings, and lifecycle rules are reused. The OHLC evidence stays
  separate from replay and experiment-point identities.
- **Availability diagnostics:** Missing latest minute, latest not yet visible,
  missing adjacent minute, preceding close not yet visible, insufficient
  history, zero scale, numerical scale failure, and missing V1 numerator/center
  have separate reasons. Reports include raw and relative ATR, calibrated
  scales, 06A RMS, coverage and a V1/06A/06B common-ready intersection, score,
  breadth, outlier and state differences, and full-stream episode/onset
  comparisons. 06A starts cold at the output interval; 06B can use causal
  pre-output candles.
- **Limitation:** Archive first-seen time is an exchange completion-time
  surrogate, not historical network receipt. V1 agreement or one 12-hour
  pilot cannot establish prediction or trading profitability.

## EXP-75-07 — PCA/common factor

- **Status:** `IMPLEMENTED_EXPERIMENT`
- **Hypothesis:** A causal PCA of synchronized, standardized, non-overlapping 1m
  returns may provide a useful measure of common market coordination beyond V1
  directional breadth. Benefit requires stable enough leading-factor structure,
  interpretable explained-variance behavior, and measurable information that is
  not merely a restatement of breadth. PCA is evaluated as supporting diagnostic
  evidence only and must not alter V1 market-state classification or lifecycle
  behavior.
- **Input/data prerequisite:** Complete configured-universe vectors of canonical
  #71 1m `current_return` at boundaries divisible by 60,000 ms. The 60, 120,
  and 240-row configurations use only prior synchronized minute rows. An
  incomplete aligned row clears the entire matrix history after current
  evidence is calculated.
- **Estimator:** Prior-window population mean and standard deviation for each
  symbol, followed by a symmetric correlation matrix and a deterministic
  standard-library Jacobi eigensolver. Explained variance is the largest
  eigenvalue divided by the eigenvalue sum. Identified PC1 loadings use a
  deterministic sign orientation; score sign is not a market direction.
- **Causal/live suitability:** The current row cannot change its own model. PCA
  is calculated and projected only at synchronized minute boundaries; the V1
  #72/#73 branch still advances every five seconds.
- **What changes relative to V1:** Nothing in the canonical #71/#72/#73 event
  stream. EXP-75-07 produces diagnostic evidence only, with no candidate
  classifier or lifecycle branch.
- **Evaluation measurements:** Explained variance, eigengap, loading coherence
  and stability, current PC1 energy, associations with V1 directional breadth,
  and descriptive groups by V1 state and active episode. No coordination
  threshold, trading signal, window ranking, or automatic promotion is defined.
- **Promotion constraint:** Compare the fixed correlation model against V1 on
  development, validation, and untouched test data before considering any
  future versioned use in production.

## EXP-75-08 — Correlation/clustering

- **Status:** `IMPLEMENTED_EXPERIMENT`
- **Hypothesis:** A causal correlation structure estimated from synchronized,
  non-overlapping 1m returns may identify stable coordinated subgroups that are
  not fully described by V1 directional breadth or the single common-factor
  diagnostics of EXP-75-07. Useful evidence requires stable pairwise/network/cluster
  structure across chronological replay and must not depend on asynchronous tick
  correlation, future observations, or tuned clustering thresholds.
- **Input/data prerequisite:** At boundaries divisible by 60,000 ms, use only
  complete, included, finite canonical #71 1m `current_return` vectors in exact
  configured-universe order. The fixed 60, 120, and 240-row models use only prior
  contiguous synchronized rows. An incomplete current row is never appended;
  prior ready evidence may still be reported before the whole history is cleared.
- **Estimator:** Population Pearson correlations with prior means, population
  standard deviations, and covariance divided by the product of the standard
  deviations. An undirected positive-correlation network includes edges at
  `rho >= +0.70`. Deterministic agglomerative average-linkage clustering uses
  distance `1-rho` and cut `0.30`; equal-distance merges choose the
  lexicographically smallest cluster pair. Network connected components and
  clusters remain distinct outputs.
- **Causal/live suitability:** Strict-prior diagnostics are computed before the
  current aligned row is appended. Non-aligned five-second points are not
  scheduled. No asynchronous tick correlation estimator is used.
- **What changes relative to V1:** Diagnostic-only pairwise, network, cluster,
  stability, and material-mover concentration evidence. There is no candidate
  #72 classifier or #73 lifecycle branch; canonical V1 advances unchanged.
- **Evaluation measurements:** Pairwise correlation distribution, network edge
  density and components, cluster sizes and within-cluster correlation,
  consecutive compatible edge and co-cluster-pair Jaccard stability, current V1
  material-mover concentration in prior subgroups, descriptive associations with
  V1 breadth, and V1 state/episode grouping across chronological partitions.
  The fixed thresholds and lookbacks are not optimized or ranked. No automatic
  promotion or financial meaning is assigned to clusters.

## EXP-75-09 — HMM/learned regimes

- **Status:** `IMPLEMENTED_EXPERIMENT`
- **Hypothesis:** A deterministic three-state Gaussian hidden Markov model
  trained only on chronological development-period market-state features may
  produce stable out-of-sample latent-state segmentation that contains descriptive
  information beyond deterministic V1 direction/lifecycle states. Usefulness
  requires reproducible state definitions, acceptable posterior stability,
  sensible out-of-sample likelihood, and consistent validation/test associations
  without any future-derived training inputs.
- **Input/data prerequisite:** At minute-aligned boundaries, use exactly four
  point-in-time primary 5m features: median normalized movement, material breadth
  imbalance, dispersion MAD of normalized movement, and median RVOL over every
  included symbol. An unavailable feature breaks a development training block or
  resets validation/test filtering. No feature is imputed.
- **Estimator:** Unsupervised three-state, four-dimensional diagonal Gaussian
  HMM. Development-only population mean and standard deviation normalize the
  features. Deterministic initialization and 50 log-space Baum-Welch iterations
  fit the model; emissions use a fixed `1e-4` variance floor and updated
  probabilities use a fixed `1e-12` floor. Canonical states are ordered by the
  learned mean of feature 0 and named LOW/MID/HIGH_MOVEMENT. Training data and
  frozen model parameters have deterministic SHA-256 fingerprints.
- **Causal/live suitability:** Development receives no fitted regime output.
  Validation starts a forward filter from frozen initial probabilities; test may
  carry the preceding validation posterior across a complete minute boundary.
  Missing observations reset the filter. No Viterbi or future smoothing is used.
- **What changes relative to V1:** Research-only latent-state diagnostics.
  Canonical #71/#72/#73 remains unchanged, with no candidate #72/#73 branch.
- **Evaluation measurements:** Frozen-model predictive log likelihood, posterior
  confidence/entropy, HMM × V1 state contingency, per-state descriptive features,
  HMM and V1 switch counts, and validation/test occupancy total variation.
  V1 direction and lifecycle are comparison context, never training labels.
- **Relationship to #43:** EXP-75-09 reuses #43's chronological leakage
  discipline but is not the supervised trade-selection ML baseline owned by #43.
  No trading labels, P&L targets, live integration, or automatic promotion exist.

## EXP-75-10 — Mark-price context

- **Status:** `IMPLEMENTED_EXTENSION` via Issue #127 as a separate historical,
  descriptive mark-versus-trade diagnostic.
- **Suite boundary:** EXP-75-10 is not part of the immutable fixed 28-run
  `EXPERIMENT_SUITE_V1`; suite membership and identities remain unchanged.
- **Hypothesis:** Explicit mark-price/trade-price divergence may identify stressed
  derivatives conditions not visible in trade-price movement state alone.
- **Input/data:** The extension pairs verified #111 trade-price closes with
  separately verified Binance USD-M mark-price 1m candles at exact
  minute-boundary points. It reports source availability, coverage, returns,
  endpoint bases, and divergence; gaps and unavailable candles are not filled.
- **Causal/provenance limits:** Availability uses the exchange-close-time-plus-1ms
  archive surrogate, not an observed network-receipt timestamp. The movement
  replay's finalization grace is not applied. Mark methodology can vary across
  historical periods, so date and package-provenance strata must be retained.
- **Role relative to V1:** V1 remains trade-price based. Mark price is supporting
  reference evidence and is never silently substituted for canonical trade
  price. The extension is descriptive and does not add a live signal or
  predictive claim.
- **What remains unchanged:** The original fixed 28-run suite, its identities,
  and canonical trade-price movement and lifecycle rules.
- **Evaluation measurements:** Divergence incidence and event-context
  comparisons alongside same-time V1 context. Issue #123 owns broader
  multi-period robustness and incremental forward-information evaluation.
- **Promotion constraint:** Any later use must retain both explicit price types
  and the extension's source availability and provenance limitations.

## EXP-75-11 — OI/funding/liquidations

- **Status:** `IMPLEMENTED_EXTENSION` as three independent supporting diagnostics:
  `EXP-75-11-OI`, `EXP-75-11-FUNDING`, and `EXP-75-11-LIQUIDATION`.
- **Ownership:** Separate evidence modules and prepared-data library runners use
  the exact exported canonical replay points and ordered configured universe.
  Each has its own evidence SHA, algorithm/config versions, candidate output SHA,
  extension fingerprint and report SHA. No five-symbol restriction applies.
- **OI:** Verified daily Binance metrics; base-quantity log changes using exact
  5m/15m endpoints, conservative `create_time + 5m` availability, at least 20m
  prehistory, and no carry through a missing current nominal slot. Positive OI
  change does not identify new longs or shorts.
- **Funding:** Verified monthly settled events, preceding-calendar-month history,
  `calc_time + 1ms` completion surrogate, rate divided by the recorded positive
  interval hours. Missing expected next settlement invalidates the carried rate.
- **Liquidations:** Tardis provider-normalized snapshots, local receipt timestamps
  in microseconds, exact observed `price * amount` totals in 1m/5m/15m event-time
  windows, full archive-day coverage, and explicit unavailable/no-observation
  states. Opt-in acquisition supports unauthenticated first-of-month samples only.
- **Summaries:** Partial symbol coverage is allowed. Ready denominators and unique
  OI endpoint-pair / funding settlement counts distinguish repeated projections
  from independent source observations.
- **Scope:** Descriptive same-time V1 direction context only. Core dataset, V1
  movement/classification/lifecycle, existing fingerprints and fixed 28 experiments
  retain their existing definitions. No predictions, signals, thresholds, rankings,
  forward outcomes, live collection or automatic promotion.
- **Validation:** Generated archive fixtures cover the frozen schema/causality
  contracts. Real-source validation is a separate user-run step before #128 closes;
  incompatible real schemas fail explicitly. See [historical replay](historical-replay.md#exp-75-11-independent-derivatives-context).

## EXP-75-12 — Taker imbalance

- **Status:** `IMPLEMENTED_EXTENSION`
- **Scope:** A separate, versioned historical extension over the verified
  Binance USD-M public daily `aggTrades` archive. The fixed 28-run
  `EXPERIMENT_SUITE_V1`, its order, manifests, reports, fingerprints, point
  stream, and hashes are unchanged. Flow never enters V1 state decisions.
- **Calculation:** For each replay point and symbol, aggregate quote notional
  `price × quantity` into aggressive buys (`buyer_is_maker == false`) and sells
  (`true`) using the replay's right-closed `(t − w, t]` windows for exactly
  1m, 5m, and 15m. Report `B`, `S`, gross, signed net, and `(B − S) / (B + S)`;
  a covered empty window has `NO_OBSERVED_AGGTRADES` and null imbalance.
  Positive balanced activity is `ACTIVE` with zero imbalance. Exact Decimal
  accumulation and 50-digit Decimal division are used.
- **Replay finalization:** The opt-in sidecar uses the canonical 5-second bucket
  assignment and replay's inclusive `first_seen_at_ms <= boundary +
  finalization_grace_ms` rule. Rows assigned to finalized buckets are rejected
  permanently. Its prefix-indexed buckets cover the replay engine's actual
  warm-up start through output end. Evidence is built in the archive loader's
  validated aggTrade pass and records the dataset identity, content SHA,
  ordered symbols, range, grace, policies, and bucket digest.
- **Comparison:** The report checks `B + S` against V1
  `current_notional_volume` for each common symbol/point/window; any mismatch is
  an evidence-integrity error. It reports active-only median imbalance,
  buy/sell/balanced sign breadth, pooled notional imbalance, largest active
  symbol share, coverage, and descriptive same-point direction/breadth
  comparisons by development, validation, and test partition.
- **Source limitations:** Buy and sell counts are counts of Binance public
  `aggTrades` archive rows, not counts of individual fills, orders, or
  constituent trades; a row can aggregate fills sharing a price and taking
  side over a short interval. Describe the result as archived aggregate
  taker-side flow. The public archive schema does not include the API's `nq`
  field, so this evidence cannot separate RPI quantity from archived buy/sell
  quantities. An empty observed interval means no aggTrade rows were present in
  the verified archive input; it does not prove that the exchange had no trades
  or that a live collector was healthy. The archive event timestamp used for
  `first_seen_at_ms` is a replay surrogate, not an observed network-receipt
  timestamp. This is neither net long/short positioning nor a directional
  prediction; it tests no forward returns or trading value. Forward outcomes
  are deferred to #123.
