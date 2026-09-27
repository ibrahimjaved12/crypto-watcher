# Market-state experiment registry

This registry preregisters the deferred Issue #75 candidates. Each candidate is
evaluated independently against the unchanged canonical #28 V1 event stream.
The current experiments implement **EXP-75-01 EWMA** and **EXP-75-02 CUSUM**;
EXP-75-03 through EXP-75-12 remain unimplemented.

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

- **Status:** `NOT_IMPLEMENTED`
- **Hypothesis:** A preregistered causal state-space estimate of market level and
  trend may produce a more stable current-state estimate than raw adjacent-window
  evidence, only if robust across reasonable fixed covariance assumptions.
- **Input/data prerequisite:** Exact state vector, observation model, process
  covariance, measurement covariance, and initial state/covariance.
- **Causal/live suitability:** Causal in principle; no implementation here.
- **What changes relative to V1:** A future versioned state estimate could replace
  one explicitly selected aggregate.
- **What remains unchanged:** Per-symbol evidence, classifier thresholds, and
  lifecycle behavior.
- **Evaluation measurements:** Disagreement, latency, transition stability, and
  covariance sensitivity.
- **Promotion constraint:** All model assumptions must be fixed and versioned
  before evaluation.

## EXP-75-04 — Change-point detection

- **Status:** `NOT_IMPLEMENTED`
- **Hypothesis:** Independently detected structural change points may provide
  useful historical labels for assessing whether V1 transitions occur near real
  distributional changes.
- **Input/data prerequisite:** A defined aggregate series and a fixed detector.
  Online causal methods and offline PELT-style historical segmentation must be
  evaluated separately.
- **Causal/live suitability:** Online methods may be causal; offline results are
  historical labels only and can never become live features.
- **What changes relative to V1:** Future change-point labels or a separately
  versioned causal transform.
- **What remains unchanged:** Canonical V1 calculations and lifecycle rules.
- **Evaluation measurements:** Transition proximity, onset timing, and stability
  across untouched periods.
- **Promotion constraint:** Offline labels cannot justify live promotion.

## EXP-75-05 — Regression-slope acceleration

- **Status:** `NOT_IMPLEMENTED`
- **Hypothesis:** A preregistered local regression-slope change may be less
  boundary-sensitive than V1 adjacent-window acceleration while retaining useful
  detection latency.
- **Input/data prerequisite:** Fixed regression window, weighting, and robustness
  method.
- **Causal/live suitability:** Potentially causal after the window and estimator
  are fixed.
- **What changes relative to V1:** Future candidate acceleration evidence only.
- **What remains unchanged:** V1 movement metrics, breadth, classifier thresholds,
  and lifecycle rules.
- **Evaluation measurements:** Pace disagreement, onset lead/lag, and episode
  stability.
- **Promotion constraint:** The estimator must be versioned before independent
  comparison.

## EXP-75-06 — ATR / realized-volatility normalization

- **Status:** `NOT_IMPLEMENTED`
- **Hypothesis:** Alternative per-symbol volatility scaling may improve
  cross-contract comparability in changing volatility regimes, only if it does not
  mask genuine outliers or reduce regime stability compared with V1 median/MAD
  normalization.
- **Input/data prerequisite:** Separate fixed ATR and realized-volatility data
  requirements and point-in-time history.
- **Causal/live suitability:** Potentially causal; ATR and realized volatility are
  separate future candidate configurations.
- **What changes relative to V1:** Future per-symbol normalization only.
- **What remains unchanged:** Raw returns, breadth, lifecycle, and V1 thresholds.
- **Evaluation measurements:** Outlier retention, state disagreement, and regime
  stability.
- **Promotion constraint:** ATR and realized volatility must not be combined into
  one unidentifiable candidate.

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
