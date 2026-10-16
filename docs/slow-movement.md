# Longer moves (slow-movement-v1)

Refs #30, #373. A multi-hour market movement **descriptor** shown on the dashboard next to the V1 1m / 5m / 15m
movement engine. Motivation: a macro or news event can bleed or pump the market for several hours, which the
15-minute V1 windows cannot show.

**What it is not.** It describes the current market state. It is not a predictor, a trade rule or a
profitability claim, and it creates **no alerts or notifications** (alerting is #373 and needs research first).
Its thresholds are provisional and unvalidated; it counts as an unvalidated descriptor. No backtest or
validation claim is made.

## Identity

| Item | Value |
| --- | --- |
| Algorithm | `slow-movement-v1` (`python/market_analysis/slow_movement.py`) |
| Config | `slow-config-v1`, dataclass `SlowMovementConfig`; the result carries a 16-hex SHA-256 of the canonical config JSON (`configHash`), so any change is a new identity |
| Published payload | `slow-movement-published-v1` |
| Hosted row | `market_state_published` with `universe_id = binance-usdm-public-market:slow-v1` |

## Algorithm

- Evaluation boundary **B**: a UTC multiple of 15 minutes. Horizons **H** ∈ {60, 240, 720} minutes, h = H / 15
  grid steps (4, 16, 48).
- **Grid**: N = 672 points (7 days at 15 minutes). g_k = close of the completed 1m candle whose openTime is
  B − 15 min × k − 60 s (the price at B − 15 min × k), or null when that candle is missing. Only candles with
  openTime + 60 s ≤ B are used (no look-ahead) and nothing is filled. TypeScript sends only these 672 prices per
  symbol to Python, never raw minutes.
- **Window return** r_j = ln(g_j / g_(j+h)), defined only when both prices exist.
- **Current**: r0 = r_0. **Reference**: r_j for j = h … h + 575 (576 samples, 6 days); these windows end at or
  before B − H, so they are disjoint from the current window.
- **Eligibility**: r0 defined and at least 288 valid reference samples (3 days); otherwise the symbol is
  `collecting_history` for that horizon (not included, not filled). A missing endpoint candle therefore excludes
  the symbol for that horizon.
- **Scale**: m = median(reference), s = 1.4826 × MAD (MAD = median |r − m|). s ≤ 0 or not finite → `unavailable`.
- **z** = (r0 − m) / s.
- **Per horizon** over the `ok` symbols: `included`, `medianReturnPct` = (exp(median r0) − 1) × 100, `medianZ`,
  `up` = count(z ≥ 2), `down` = count(z ≤ −2), `neutral` = the rest, `upShare`, `downShare`.
- **Coordinated** (provisional): `rise` when included ≥ 5, upShare ≥ 0.70 and medianZ ≥ 2; `fall` when included
  ≥ 5, downShare ≥ 0.70 and medianZ ≤ −2; otherwise null.
- Floats are returned unrounded. Aggregates do not depend on symbol order.

**Known property: overlapping reference samples.** The reference windows step by 15 minutes but span H minutes,
so neighbouring samples share most of their minutes (for 12 h, 47 of 48 steps). They are not independent
observations. That is acceptable for a robust scale estimate (median / MAD) but means the 576 samples carry far
fewer than 576 independent pieces of information, and z is not a calibrated probability.

## Constants (slow, provisional, descriptor)

| Constant | Value | Meaning |
| --- | --- | --- |
| `grid_points` | 672 | 7 days at 15 minutes |
| `reference_samples` | 576 | 6 days of reference windows |
| `min_reference_samples` | 288 | 3 days; fewer → collecting history |
| `z_threshold` | 2.0 | up / down count, coordinated median-z gate |
| `coordinated_share` | 0.70 | share of included pairs beyond ±2 |
| `min_included` | 5 | fewer included pairs never coordinate |

The 2 / 0.70 / 5 thresholds are **provisional and unvalidated**. Changing any value means a new config version.

## Why V1 is unchanged

V1 (windows, classifier, lifecycle, constants, `market_state_current`) is untouched. "Longer moves" is a separate
versioned extension with its own identity, stateless in Python (`POST /v1/movement/slow`, no session store, no
movement lock), so it cannot change V1 evidence or replay.

## Transport

- **Machine**: the existing `movement-state-publisher` child (no new child). At most once per 15-minute boundary,
  20 s after it, it reads the included symbols of the current persisted V1 state (at most 20), reads 7 days + 2 min
  of completed 1m candles per symbol (`readMovementCandleHistory`, within the 8-day candle retention), builds the
  grids, calls Python, validates and publishes. Up to 3 attempts 30 s apart, then it waits for the next boundary;
  the final failure is logged once. Its loop and state are separate from the V1 publish, so a failure never
  affects V1. `SLOW_MOVEMENT_ENABLED=false` disables compute and publish.
- **Site**: the same hook `POST /api/public/hooks/market-state-publish` with a new op `publish_slow` (shared
  machine secret, same body caps, payload schema check, envelope boundary must equal the payload's). It writes
  through the existing `publish_market_state` RPC to the `:slow-v1` row. No new table and no migration. Heartbeats
  stay for the main row only.
- **Read**: `getSlowMovementState` reads the `:slow-v1` row with the caller's session (RLS read for signed-in
  users).

## UI states

| State | Rule (boundary age on the page clock) | Shown |
| --- | --- | --- |
| warming | no row yet | "Collecting history, needs about 3 days of data" (muted) |
| current | ≤ 15 + 5 min | tiles, nothing extra |
| catching up | ≤ 35 min, or the first load in flight | "Updating…" |
| late | > 35 min | "Longer moves last updated 40 min ago" (muted; amber chip only after 1 hour) |

Never red for a lag. Tiles: 1 hour / 4 hours / 12 hours with the median move, "6 of 9 pairs fell" (the included
pairs' own return signs in the direction of the median), and "Very unusual" (|median z| ≥ 3), "Unusual" (≥ 2) or
"Normal". A coordinated horizon adds one sentence, e.g. "Most pairs are falling together over 4 hours". Pair cards
show "1h −0.4% · 4h −1.9% · 12h −3.2%"; z and the unusual wording are only inside the card's Details. Diagnostics
carries "How longer moves work", the versions, config hash and per-horizon counts.
