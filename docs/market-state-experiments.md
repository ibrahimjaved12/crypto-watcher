# Market-state experiment registry

This registry preregisters the deferred Issue #75 candidates. Each candidate is
evaluated independently against the unchanged canonical #28 V1 event stream.
The current experiments implement **EXP-75-01 EWMA**, **EXP-75-02 CUSUM**,
**EXP-75-03 Kalman/state-space**, **EXP-75-04A offline PELT**,
**EXP-75-04B online Bayesian change-point detection**, and
**EXP-75-05 regression-slope acceleration**, and **EXP-75-06A realized-volatility
normalization**. EXP-75-06B and EXP-75-07 through EXP-75-12 remain unimplemented.

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

- **Status:** `NOT_IMPLEMENTED`
- **Input/data prerequisite:** ATR requires causal replay inputs containing
  actual OHLC/range evidence, including high, low, and previous-close semantics.
  `MarketStateExperimentPoint` contains canonical #71 evaluations, not the raw
  OHLC sequence required for ATR. High/low must not be inferred from close
  prices or rolling returns.

## EXP-75-07 — PCA/common factor

- **Status:** `NOT_IMPLEMENTED`
- **Hypothesis:** With a sufficiently large aligned universe, first-factor
  strength may distinguish genuinely coordinated moves from coincidental breadth
  more effectively than breadth and dispersion alone.
- **Input/data prerequisite:** Aligned completed >=1m returns, sufficient history,
  an explicit covariance estimator, and a minimum universe size.
- **Causal/live suitability:** Potentially causal only with point-in-time aligned
  inputs and a fixed estimator.
- **What changes relative to V1:** Future common-factor supporting evidence.
- **What remains unchanged:** Canonical V1 metrics and #72/#73 decisions.
- **Evaluation measurements:** Coordination discrimination, disagreement, and
  universe-size sensitivity.
- **Promotion constraint:** Covariance and universe requirements must be fixed
  before evaluation.

## EXP-75-08 — Correlation/clustering

- **Status:** `NOT_IMPLEMENTED`
- **Hypothesis:** Stable correlation clusters derived from aligned completed >=1m
  returns may identify subgroup-driven moves that simple market-wide breadth treats
  as broader coordination.
- **Input/data prerequisite:** Explicit aligned returns, estimator, window, and
  clustering stability rule.
- **Causal/live suitability:** Potentially causal with completed aligned inputs.
  Naive asynchronous tick Pearson correlation is prohibited.
- **What changes relative to V1:** Future subgroup coordination evidence.
- **What remains unchanged:** Canonical #71, #72, and #73 semantics.
- **Evaluation measurements:** Cluster stability, subgroup coverage, and state
  disagreement.
- **Promotion constraint:** Asynchronous tick correlation cannot be promoted.

## EXP-75-09 — HMM/learned regimes

- **Status:** `NOT_IMPLEMENTED`
- **Hypothesis:** A chronologically trained latent-regime model may provide
  repeatable segmentation beyond deterministic V1 only if state definitions and
  performance remain stable on untouched periods.
- **Input/data prerequisite:** Explicit model, state definitions, chronological
  training, and #43 leakage/splitting discipline.
- **Causal/live suitability:** Requires a separate causal training/inference
  decision; no implementation here.
- **What changes relative to V1:** Future learned regime labels only.
- **What remains unchanged:** Canonical V1 event stream remains the comparator.
- **Evaluation measurements:** Stability, untouched-period behavior, and leakage
  checks.
- **Promotion constraint:** No model is promoted from in-sample results.

## EXP-75-10 — Mark-price context

- **Status:** `NOT_IMPLEMENTED`
- **Hypothesis:** Explicit mark-price/trade-price divergence may identify stressed
  derivatives conditions not visible in trade-price movement state alone.
- **Input/data prerequisite:** Point-in-time mark price and canonical trade-price
  series.
- **Causal/live suitability:** Supporting context only; mark price must never be
  silently substituted for the canonical trade-price series.
- **What changes relative to V1:** Future supporting mark-price evidence.
- **What remains unchanged:** Canonical trade-price movement and lifecycle rules.
- **Evaluation measurements:** Divergence incidence and event-context usefulness.
- **Promotion constraint:** Any use must retain both explicit price types.

## EXP-75-11 — OI/funding/liquidations

- **Status:** `NOT_IMPLEMENTED`
- **Hypothesis:** Point-in-time leverage, funding, and liquidation context may
  distinguish superficially similar price/breadth events, but should initially
  remain supporting evidence rather than a direct broad-state trigger.
- **Input/data prerequisite:** Replayable point-in-time data for each family.
- **Causal/live suitability:** Supporting context only until each data family is
  independently versioned.
- **What changes relative to V1:** Future supporting evidence, one data family per
  experiment/version.
- **What remains unchanged:** V1 broad-state triggers and lifecycle rules.
- **Evaluation measurements:** Context separation and data-quality coverage.
- **Promotion constraint:** OI, funding, and liquidations remain separate future
  experiments.

## EXP-75-12 — Taker imbalance

- **Status:** `NOT_IMPLEMENTED`
- **Hypothesis:** Aggressor-side notional imbalance may add stable participation
  information beyond price breadth and RVOL when calculated from correctly
  timestamped Binance aggressor-side data.
- **Input/data prerequisite:** Replayable buyer-maker/aggressor-side field.
- **Causal/live suitability:** Potentially causal after the input is retained and
  timestamped; collector changes are outside this experiment.
- **What changes relative to V1:** Future supporting participation evidence.
- **What remains unchanged:** Canonical movement, classification, and lifecycle
  semantics.
- **Evaluation measurements:** Coverage, directional agreement, and stability.
- **Promotion constraint:** Do not modify the collector in this PR.
