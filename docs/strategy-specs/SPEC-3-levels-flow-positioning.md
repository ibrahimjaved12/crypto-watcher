# SPEC-3: Levels, order flow, leverage/positioning and cross-asset specifications

*Part of the strategy specification library (#219). 2026-10-09. Draft by Claude (AI). Defaults are to be pre-registered, not tuned. Signals on completed bars, entry at the next 1-minute open, both directions always, costs and funding included. Evidence grades: A rigorous OOS, B credible, C hearsay, D contradicted. Edges are estimates.*

## 1. Levels (the owner's style; ticket #185)

### 1.1 Why this comes first

The owner trades levels and 4h-close confirmations ("a 4h close at 74,200 implies the next 4h reaches 78,400", "price heading to resistance 80k or support 70k"). Indicator crossings, flow and funding were the standard first tests, but they are not how the owner trades. Level reaction is therefore the first product-relevant study.

Mechanism (Osler, FRBNY Economic Policy Review 2000 and Staff Report 125 / Journal of Finance 2003, FX): take-profit orders cluster AT round numbers (trends reverse there); stop-loss orders cluster JUST BEYOND them (trends accelerate once a level is crossed). Published levels interrupted intraday trends more often than arbitrary levels (60.8% vs 56.2% in the 1996-98 study; significant in 9 of 16 cases; lasted at least 5 business days; weaker in high volatility; the advantage is small). In crypto, price clustering at round numbers is robust (Urquhart 2017 and later), barrier behavior is weaker and time-varying ("strong clustering and weak barriers"). Fibonacci levels, generic pivots and volume-profile levels have no rigorous placebo-controlled crypto evidence (grade D/untested): include them only as extra level sets in the same placebo framework.

### 1.2 Level definitions (all point in time)

- **Fractal swing high/low:** bar `i` is a swing high if `high_i > high_j` for all `j` in `i-n..i-1` and `i+1..i+n`; it becomes known only at the close of bar `i+n` (never use it earlier). `n = 3` (research H6) and `n = 5` (ours); on 4h bars over the last 90 days.
- **Clustering:** merge swing levels within `0.25 * ATR_14(4h)`; level = mean price of the cluster; strength = number of touches (a touch = a bar whose range overlaps the level +/- 0.1*ATR with a reversal); a level is valid only after the confirming bar closes.
- **Round numbers:** BTC multiples of 1,000 (5,000 and 10,000 strong); ETH 100; SOL 10; BNB 50; XRP 0.10; DOGE 0.01. Fix the grids in advance; never choose them after seeing reactions.
- **Previous period extremes:** previous UTC day/week/month high and low and weekly/monthly opens, usable only after that period has closed.
- **Session VWAP / anchored VWAP bands** and pivot points (SPEC-2 section 3) as additional sets.

### 1.3 Event and outcome definitions

- **Approach condition:** price has been at least `x * sigma_4h` away from the level during the last 24h (x = 1), then comes within `eps = 0.1 * ATR_1h` of it. The event is the first touch after the approach (avoids counting chop around a level as many events).
- **Bounce:** price reverses by at least `y * sigma_1h` (y = 1) before penetrating by `z * sigma_1h` (z = 0.25). **Penetration continuation:** conditional on crossing by `z`, forward return over `h, 2h, 4h` and over 1-5 days.
- **Descriptive first slice (no trade labels, can start now):** bounce frequency and continuation return for real vs placebo levels, by symbol, side, volatility tercile; counts before returns.
- **Placebo:** SPEC-1 section 5.2 (20 matched artificial levels per real level; time-shuffled swing levels). Report real minus placebo with block-bootstrap intervals.

### 1.4 Trade setups (second slice; need exits from #221 and hold classes from #220)

- **L1 Fade the first touch (H4):** limit order at `level - eps` (buy at support) or `level + eps` (sell at resistance), maker model of SPEC-1 section 7 (fill only on trade-through, cancel after T minutes). Stop `0.5 * sigma_1h` beyond the level (where stop-loss orders cluster, so a loss is small and fast); target `1.5-2 * sigma_1h` or the next level; time limit 8h. Evidence B in FX, C+ in crypto; est. gross 0.05-0.10 R, maker cost about 0.03-0.04 R.
- **L2 Clean break continuation (H5):** a 1h (or 4h) close beyond a round number or prior-week extreme by at least `0.5 * sigma_1h`, with cumulative taker imbalance over the break in the break direction; stop back inside the level by `0.5 * sigma`; trailing stop (X2/X3); time limit 24-72h. Overlaps with trend breakouts: test its incremental value over H1.
- **L3 Break and retest:** 4h close beyond the level by `>= 0.25 * ATR_4h`, retest within 12 bars where a bar closes back on the breakout side; stop `0.5 * ATR_4h` back inside; target a measured move; cap 7 days. No rigorous evidence (grade C/D), but it reduces trade count and the entry is cheap to test.
- **L4 Failed breakout / spring:** intrabar break `>= 0.2 * ATR_4h` of a valid level then a 4h close back inside; fade toward the far side of the range; stop at the extreme.
- **L5 Sweep reversal (Turtle Soup):** 1h high exceeds the previous-day high by `>= 0.1 * ATR_d` then a 1h close below it -> short (mirror long); stop at the sweep extreme + `0.1*ATR`; target the previous-day midpoint or 2R; cap 48h. Stop-hunt variant adds a taker-sell spike (z > 2) in the sweep bar and a reclaim within 3 bars.
- **L6 Owner's style (H6):** a 4h close above the highest confirmed fractal-3 swing high of the last 5 days (or below the lowest swing low); target = next round number or next unbroken swing level in the trade direction; stop = low of the breakout bar or `1.5 * sigma_4h`; expiry 5 days; run the time-only variant as well. Fix the target-level definition in advance.
- **Close vs intrabar trigger** is an A/B variant (same levels, trigger on a 1-minute cross vs on the 4h close).
- **Range trading:** efficiency ratio ER_20 < 0.2 and Donchian-50 width < 3*ATR: buy the bottom 20% of the range, sell the top 20%, stop outside the range.

### 1.5 The owner's example as a base-rate calculation

BTC at 74,200 on a 4h close and a target of 78,400 is +5.7%. With a BTC 4h sigma of 1.0-1.5% (2024-2025, est.) that is about 3.8-5.7 sigma_4h. For a driftless price the probability that the maximum over `n` 4h bars reaches `a` sigma_4h is `2 * (1 - Phi(a / sqrt(n)))`. For `a = 4`: n = 6 (1 day) about 10%; n = 18 (3 days) about 35%; n = 30 (5 days) about 47%. With a stop `b = 1.5` sigma below the entry, the probability the target comes first (no time limit) is `b / (a + b)` = 27%, which equals the break-even win rate for a 2.67 : 1 payoff. So a 16h expiry guarantees mostly expiries, and "touch" hit rates alone are meaningless (ranging coins revisit prices): every level study must compare with the Brownian base rate AND placebo levels, and judge on stop-aware net R.

### 1.6 Measured-move audit (descriptive first)

After a 4h close above resistance `L` (confirmed level), target `T = L + m * (L - last swing low)` with `m` in {0.618, 1.0}, or `L + k * ATR_4h`. Measure `P(max high over the next n bars >= T)` for `n` in {1, 6, 18} versus the Brownian base rate (above) and versus placebo levels.

## 2. Order flow and microstructure (ticket #188 phase 1)

Taker side from klines: `taker_buy_volume`; `taker_sell_volume = volume - taker_buy_volume`; imbalance of a window `TI_w = sum(buy - sell) / sum(volume)`.

### 2.1 Cumulative taker imbalance (existing of_cum240_4h; H2)

```
TI_240,t = sum over the last 240 minutes of (buy - sell) / sum of volume
z_t = (TI_240,t - mean_30d) / sd_30d                (trailing 30-day distribution of the same statistic, as implemented in order_flow.py; the exact estimator is defined by that module)
signal: z crosses +/-c  (crossing_signals; long on +c, short on -c), c = 2 (existing)
```

First result (development segment; numbers stay in the private repo): a small real gross effect, beating matched random entries, but smaller than the cost; most trades ended on the time limit (largely barrier geometry, SPEC-1 section 4).

**of-v2 design (answers the owner's points and the research):**

1. **Signal-strength ladder:** c in {2.0, 2.5, 3.0, 3.5}. A real effect should grow with strength; test the monotonic dose-response slope as one hypothesis (SPEC-1 section 3) and the rungs as K = 4 variants. Expected trade counts shrink with c: record them before running (power).
2. **Targets:** rr index 1, 2, 3 (1.5x, 2x, 3x risk) at k = 2 (the first run used only 1.5x). Add k = 3 and 4 (wider stops lower cost in R) which needs a new label revision (lb2) since lb1 holds k in {1, 2}.
3. **Exits:** time-only exits at 8h, 12h, 16h with a catastrophe stop at 3R (#220/#221) next to the barrier results; a trailing exit variant (X2) for survivors.
4. **Maker entry** (SPEC-1 section 7) and **vol filter** (top two terciles of the HAR-forecast volatility, pre-registered).
5. **Pooled** across the six symbols and both sides as one hypothesis.
6. **Control for momentum:** screening regression with the past 4h return as a control (is it just momentum?).
7. K accounting: ladder 4 x rr 3 = 12 for slice A (existing labels, no new code beyond the family parameters); more only after slice B.

### 2.2 Quarter-hour opening imbalance (H3; grade B-)

Source: Kim & Hansen (UNC), arXiv 2607.09426 (July 2026, not peer reviewed; independent replication by Kimbrough): taker imbalance in the first seconds of each quarter hour (:00, :15, :30, :45) on six Binance USD-M perps predicts cumulative returns over 4-12h (significant at the 95% level for four of the six contracts at every horizon; OOS R2 about 3.4%, AUC about 0.60); trading the forecast at every quarter-hour earns about 0.5 bp gross per trade (about one tenth of a taker fee): a real pattern, no stand-alone trade. Their data are 2021-01 to 2024-10, so 2025-2026 is genuinely out of sample.

```
OI_q = (buy_volume - sell_volume) / total_volume of the first 1-minute bar at each :00/:15/:30/:45   (aggTrades can narrow to the first 10 seconds)
QI_t = sum of OI_q over the last 16 boundaries (4h)  ; compare with the same sum over non-boundary minutes
predictive regression (screening): r_{t,t+h} = a + b*QI_t + c*r_{t-4h,t} + e,  h in {4, 8, 12}h, pooled with symbol FE, HAC errors
```

Use: as a FILTER/feature added to H2's z-score; stand-alone is expected to be sub-cost. Our top-of-hour result (smaller gross effect than the cumulative signal) warns that 1-minute aggregation dilutes a seconds-scale effect; aggTrades allow the 10-second version.

### 2.3 Other flow signals (grade C unless noted)

- **CVD:** `CVD_t = sum (buy - sell)`; bearish divergence = price makes a higher high at a confirmed pivot while CVD makes a lower high over the same two pivots (bullish mirror); stop above the high, TP 1.5R, cap 24h.
- **Volume climax/exhaustion:** `vol_1h / median(vol at the same hour of day, trailing 20 days) > 4` AND `|r_1h| > 3 * sigma_1h` -> fade, hold 4-24h; stop at the extreme + `0.25*ATR`; TP 1.5R or time. Deseasonalize volume by hour (periodicity is strong).
- **Absorption / effort vs result:** at a valid level: volume z > 2 and range < `0.5*ATR` -> fade the approach; stop beyond the level; TP 2R. Aggressor imbalance after a touch: over the next 15 min imbalance beyond -0.3 against the approach while price holds the level within `0.1*ATR` -> reversal.
- **Stop-hunt:** sweep below a fractal-5 swing low by <= `0.3*ATR` with taker-sell z > 2 in that bar and a reclaim (close above the swing) within 3 bars -> long; stop at the sweep low; TP at the opposite swing; cap 48h.
- **VPIN (flow toxicity, grade B in equities):** bucket volume `V_b = daily volume / 50`; classify each bucket's buy/sell volume from real taker volume; `VPIN = sum over the last 50 buckets of |V_buy - V_sell| / (50 * V_b)`; percentile above 0.9 -> FILTER (widen stops or skip reversion entries).
- **Amihud illiquidity shock:** `|r| / dollar volume` per bar; z above 3 -> filter. **Kyle lambda:** slope of r on signed volume in a rolling window; sizing filter.
- **Large prints (aggTrades):** trade size above the trailing 30-day 99.9th percentile; follow sign for 4-12h (note aggTrades merge fills, so prints are not orders). **OBV, Chaikin money flow:** redundant with CVD, low priority.
- **Funding-settlement windows:** return in `[t-30m, t]` and `[t, t+60m]` around 00/08/16 UTC multiplied by the sign of the next funding; cheap test, est. below 10 bp.
- **Premium dislocation (H-fb):** premium-index z (1h) < -3 with `r_1h < -2*sigma` -> long, 12h exit (mirror). Pre-existing funding contrarian signals showed no information versus matched random entries; funding is clamped at its floor most of the time (BitMEX BTC funding printed exactly 0.01% for 78% of a quarter), so it is a poor continuous feature.
- **Funding as meta-filter only (H11):** block new longs in the top funding decile (90-day), new shorts in the bottom decile. Funding premium/basis arbitrage needs a spot hedge and is out of scope.

## 3. Leverage and positioning (ticket #188 phase 2; needs the metrics archive, #224)

Metrics columns (5-minute, verify on a file): `sum_open_interest, sum_open_interest_value, count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio, sum_taker_long_short_vol_ratio`. Archive lags about 1.5-2 days (research only); REST history is 30 days (start logging). Join with a conservative lag (SPEC-5).

- **Deleveraging washout reversal (H8, grade C):** `dOI_1h / OI <= -3%` (or z < -3 on the trailing 30-day distribution of 1h OI changes) AND `r_1h <= -3 * sigma_1h` (also test -2) AND premium index < 0 (forced longs closing) AND volume z > 3 -> long; mirror for short squeezes (price up, OI down, premium high -> short). Entry at next open or limit at `-0.3*ATR`; exit time-only 4h/12h with catastrophe stop 2*sigma_4h beyond the extreme (or stop 1.5*ATR, TP 2R, 24h). Few events (3-10 per symbol-year, est.): pool symbols, cluster by event day, wide intervals. Danger: exogenous shocks (the 10 Oct 2025 cascade liquidated about 19 billion dollars in 24h, mid- and small caps fell 60-80% against BTC -11%).
- **OI x price quadrants (4h):** dP > 0 & dOI > 0 = new longs (follow); dP > 0 & dOI < 0 = short covering (fade); dP < 0 & dOI > 0 = new shorts (follow); dP < 0 & dOI < 0 = long liquidation (fade); 4 cells x 2 horizons = K of 8; 12-24h time exit.
- **OI / volume leverage ratio:** `L = OI_value / 24h quote volume`; percentile above 0.9 = fragile positioning -> FILTER (reduce size, avoid breakout longs).
- **Long/short ratios:** global account ratio z (30-day) above 2 -> short bias (contrarian), below -2 -> long bias; top-trader vs crowd divergence `z(sum_toptrader_long_short_ratio) - z(count_long_short_ratio) > 2` -> long; 24-72h. Grade C/D (anecdotal).
- **Model-based liquidation clusters (exploratory, grade C; Coinglass/Hyblock methods are proprietary):**

```
for each time bin with dOI_value > 0 at price P: split dOI_value across leverage buckets w_L (assumed a priori and frozen: 5x 30%, 10x 30%, 25x 25%, 50x 10%, 100x 5%)
side: long if taker buy flow dominated the bin, short otherwise (heuristic)
liquidation price: long  P*(1 - 1/L + MMR),  short  P*(1 + 1/L - MMR)     (approximation; exact formula in SPEC-4)
density(p) = sum of bucket notional within +/- 0.1% of price level p; remove notional pro rata when OI falls; decay with age
signal: price within 0.5% of the densest cluster inside +/- 3 sigma_4h -> expect a move toward it; exit at the cluster
```

  The leverage mix is a free parameter set in advance, not fitted. Use first as a descriptive feature.
- **Real liquidation feed:** `!forceOrder@arr` websocket is free but throttled ("only the latest liquidation per symbol per 1000 ms"); no free history; record forward (#224); paid history only with owner approval. Pre-cascade taker-flow variance compression (arXiv 2607.27070: across 7 BTC cascades only the compression of taker order-flow variance was a regularity; placebo-tested): rolling 6h variance of the 5-minute taker imbalance below its 10th percentile -> RISK FILTER (cut leverage); n = 7 events only.
- **OKX and other venues:** OKX public liquidation orders and open-interest/long-short statistics may give a second source (audit in #228).

## 4. Cross-asset (ticket #225)

- **Breadth flush (the owner's "7 of 10 coins down 2% in 15 minutes"):** with six coins, event when at least 5 of 6 have a 15-minute return below `-2 * sigma_15m` (point in time), mirror for upside; also the raw threshold version (fraction of coins down >= 2% in 15 minutes) to match the original idea. Report event counts first; test fade (long 4-24h) and follow (short); use as CONTEXT/FILTER for other triggers as well.
- **BTC lead-lag (grade B for existence; sub-second to seconds on liquid perps, concentrated in small caps):** BTC 5-minute return > `2 sigma` while an alt's 5-minute return < `0.5 sigma` -> trade the alt in BTC's direction, hold 15-60 minutes; expected below cost; high R2 at 500 ms did not translate into PnL. Low priority.
- **Cross-sectional momentum (weekly):** rank 7d and 28d returns across the six coins; long the top 2, short the bottom 2, volatility-weighted; four legs of cost plus funding. Six coins is a tiny cross-section. Cross-sectional 1-day reversal: weak to absent net of costs (P3).
- **Pairs:** ETH/BTC, SOL/ETH, BNB/BTC: `spread_t = ln(P_A) - beta_t * ln(P_B)` with `beta_t` from a rolling 60-day OLS (point in time); z-score of the spread over 60 days; enter at |z| > 2, exit at 0 or time limit; two legs of cost.
- **Alt-season proxy:** share of the five alts beating BTC over 90 days (official indices use the top 50 coins; 75% = altseason).
- **Macro regime filters** (SPX/NDX above the 200-day SMA, DXY, gold): daily filters from free sources (FRED, Stooq); correlation grade B, no direction edge claimed.


---
_Generated by [Claude Code](https://claude.ai/code)_
