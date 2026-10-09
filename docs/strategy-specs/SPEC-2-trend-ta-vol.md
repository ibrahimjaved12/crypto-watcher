# SPEC-2: Trend, classic TA, volatility and regime/calendar specifications

*Part of the strategy specification library (#219). 2026-10-09. Draft by Claude (AI). Parameters are defaults to be pre-registered, not tuned. Bars are completed bars (UTC); signals at bar close; entry at the next 1-minute open; both directions always. Evidence grades: A rigorous OOS, B credible practitioner, C hearsay, D contradicted. Expected edges are estimates.*

Conventions: `H,L,C,O,V` = bar high/low/close/open/volume; `ATR_n` = Wilder ATR (RMA of true range, `TR = max(H-L, |H-C_prev|, |L-C_prev|)`); `sigma_h` = point-in-time horizon volatility (SPEC-1 section 4); `ann = sqrt(365)` for daily crypto returns.

## 1. Multi-day trend (hypothesis H1; ticket #222)

### 1.1 Donchian ensemble, close-based (research version, grade B)

Source: Zarattini, Pagani & Barbon (SSRN 5209907, 2025), long-only spot, reported net Sharpe above 1.5 on BTC/ETH (BTC about 30% CAGR, Sharpe about 1.56, max drawdown about 19%, 2015-2025, net of 10 bp; secondary summaries; we do not replicate, we specify our own version).

```
lookbacks L in {5, 10, 20, 30, 60, 90, 150, 250, 360} days  (daily bars)  [also test 4h bars with the same L in bars*6 = days]
upper_L,t = max(close_{t-L..t-1}),  lower_L,t = min(close_{t-L..t-1}),  mid_L,t = (upper+lower)/2
component state s_L,t:  +1 if close_t > upper_L,t  (enter long)
                        stays +1 until close_t < mid_L,t  (exit: trailing midline, ratcheting with the channel)
                        -1 if close_t < lower_L,t  (short mirror); exits when close_t > mid_L,t
                        else 0
signal S_t = (1/9) * sum_L s_L,t              in [-1, +1]
weight w_t = clip( S_t * sigma_target / sigma_hat_t , -cap, +cap )
sigma_target = 0.25 annualized (research default; also test 0.15), sigma_hat_t = std(daily returns over 90 days) * sqrt(365), cap = 2.0x notional
rebalance daily at 00:00 UTC; no-trade band: only change position if |w_t - w_{t-1}| >= 0.10 * |w_{t-1}| or sign changes
catastrophe stop: 4 * sigma_daily from the average entry
```

Variants (K <= 12 together with section 1.2): ensemble long/short; ensemble long-only; lookbacks subset {20, 60, 150} only as a robustness check (not selected on results). Evaluate as a portfolio return stream (Mode B) and as trades (Mode A), see #222. Costs: 11 bp per unit turnover + funding on held notional.

Expected gross (est.): 0.2-0.5 R per trade against a cost of about 0.02 R. Risks: few independent trends in 2024-2026 (a handful per asset); results dominated by one or two moves; shorts on perps may lose where the source is long-only; funding drag on longs.

### 1.2 Time-series momentum (TSMOM), vol-scaled (grade A in futures, B in crypto)

```
s_t = sign( ln P_t - ln P_{t-L} ),   L in {7, 14, 28} days
w_t = s_t * sigma_target / sigma_hat_t    (EWMA daily vol, half-life 60 days), cap 2x, no-trade band 10%
```

Sources: Moskowitz-Ooi-Pedersen (JFE 2012) for the construction; Liu & Tsyvinski (RFS 2021) and the crypto momentum literature for 1-8 week effects. One post-ETF note reports Sharpe 0.82 -> 1.22 not significantly different.

### 1.3 Donchian breakout on 4h bars (trade level)

```
long when close_t > max(high_{t-N..t-1}),  N in {20, 55, 120} bars (3.3, 9, 20 days)
exit: X3 Donchian trail (lowest low of last N/2 bars) or X2 chandelier 3*ATR_22 (SPEC-4)
initial stop: 2*ATR_20; time cap 60 days; re-entry on next breakout; no pyramiding in v1
```

### 1.4 Filters for trend entries

- Price-to-MA score `M_t = sum_{k in {20,50,100,200}} 1[close_t > SMA_k]` (daily): longs need M >= 3, shorts need M <= 1.
- Realized-vol percentile below 70 (365-day window): block entries in the stressed tail.
- Season/regime label from #223 as context.
- Funding filter (H11): no new longs when current funding is in the top decile of its trailing 90 days, no new shorts in the bottom decile (carry is a cost).

## 2. Other trend and momentum TA (ticket #187)

Each is a TRIGGER or FILTER; edge estimates are guesses and most are expected near zero after costs; they are in the catalogue to be tested cheaply and logged.

- **Supertrend(10, 3):** `basic_upper = (H+L)/2 + 3*ATR_10`, `basic_lower = (H+L)/2 - 3*ATR_10`; `final_upper = basic_upper if basic_upper < final_upper_prev or C_prev > final_upper_prev else final_upper_prev` (mirror for lower); trend flips long when `C > final_upper`, short when `C < final_lower`. The active band is the trailing stop. Test 4h.
- **ADX/DMI (Wilder):** `+DM = H - H_prev` if > `L_prev - L` and > 0, else 0 (mirror -DM); smoothed with RMA(14); `+DI = 100*RMA(+DM)/ATR`, `DX = 100*|+DI - -DI|/(+DI + -DI)`, `ADX = RMA(DX, 14)`. Filter: ADX > 25 and `+DI > -DI` for longs.
- **Ichimoku (9, 26, 52):** `tenkan = (HH9+LL9)/2`, `kijun = (HH26+LL26)/2`, `senkouA = (tenkan+kijun)/2` shifted forward 26 (when used for decisions at t, use the value computed from bars up to t-26), `senkouB = (HH52+LL52)/2` shifted forward 26, `chikou = close shifted back 26` (look-ahead trap: never use chikou for entries). Long: close above the cloud and tenkan > kijun. Low priority.
- **Parabolic SAR:** `SAR_{t+1} = SAR_t + AF*(EP - SAR_t)`, AF starts 0.02, +0.02 on each new extreme, max 0.20; flips when price crosses SAR. Use as a trailing exit control; as an entry it whipsaws.
- **Heikin-Ashi:** `HA_close = (O+H+L+C)/4`, `HA_open = (HA_open_prev + HA_close_prev)/2`; signals read from HA candles but fills use real prices only.
- **Stochastic / StochRSI pullback in trend (C):** `%K = 100*(C - LL14)/(HH14 - LL14)`, smoothed (3,3); long when daily close > SMA200 and 1h %K crosses up through 20; stop 1.5*ATR; target 2R or trail; hold 1-3 days.
- **CCI(20):** `(TP - SMA_TP)/(0.015 * mean deviation)`, TP = (H+L+C)/3. **Williams %R:** `-100*(HH14 - C)/(HH14 - LL14)`. Low priority; correlated with Stochastic.
- **N consecutive candles fade (C):** N >= 5 consecutive 1h closes in the same direction, fade; TP 1R, stop 1R, 12h cap. Influencer-reported (unverifiable); low priority.
- **Intraday momentum (grade B):** return of the first 30 minutes of the UTC day predicts the last 30 minutes (Shen, Urquhart & Wang 2022, OOS R2 about 1.1-1.6%, about 2 bp per trade): below cost, P4.

## 3. Compression, breakouts and mean reversion

- **Bollinger squeeze / TTM:** `BB(20, 2)` inside Keltner `KC(20, 1.5*ATR_20)` for >= 6 consecutive bars; entry on the first close outside the BB; stop at the opposite band; chandelier trail. 4h.
- **NR4/NR7 (Crabel 1990):** bar range is the minimum of the last 7 (or 4) bars; place a buy-stop above its high and a sell-stop below its low for the next bar (OCO); stop-order fills use the stop slippage floor (2 bp) and gap rule.
- **Volatility compression -> expansion (H9, grade C):** `HAR sigma forecast (1d) in bottom 20% of its trailing 90-day range` AND `24h range in bottom 20%`; enter in the direction of the first 4h close outside the 24h range; exit trailing at the opposite side of the range; limit 3 days. **Control:** random-direction entries at the same times (separates timing from direction).
- **Opening-range breakout:** anchors (pre-registered) 00:00 UTC, 13:30 UTC, funding times 00/08/16 UTC; range = first 30 minutes after the anchor; stop-order entries at range +/- 1 tick; stop at the opposite side; exit at the next anchor.
- **VWAP / anchored-VWAP z-reversion:** `VWAP_24h` from the UTC day start (or anchored at the last confirmed swing); `z = (P - VWAP) / sd(P - VWAP over 24h)`; entry at |z| > 2.5, TP at VWAP, stop at |z| = 4, 12h cap.
- **Pivot points** (levels, see SPEC-3): classic `P = (H+L+C)/3`, `R1 = 2P - L`, `S1 = 2P - H`, `R2 = P + (H-L)`, `S2 = P - (H-L)`; Camarilla `R1 = C + 1.1*(H-L)/12`, `R2 = C + 1.1*(H-L)/6`, `R3 = C + 1.1*(H-L)/4`, `R4 = C + 1.1*(H-L)/2` (S mirrored), Woodie `P = (H+L+2C)/4`; computed from the previous completed UTC day.
- **RSI/MACD divergence:** swing pivots by fractal-n (SPEC-3 section 1); bullish regular divergence = price makes a lower low while RSI(14) makes a higher low between the same two confirmed pivots; signal only after the second pivot is confirmed (n bars later); hidden divergence mirrored. Low priority.
- **Candlestick patterns** (only as triggers at levels): bullish engulfing `C > O_prev, O < C_prev, body > body_prev`; pin bar `lower wick >= 2*body and close in upper third`; inside bar `H < H_prev and L > L_prev`; morning star = long down bar, small body gap/overlap, long up bar closing above the midpoint of the first. Compare with random bars at the same levels. Grade C/D.
- **Smart-money concepts (strict definitions, grade C, placebo required):** fair value gap (bull) `L_t > H_{t-2}` (the gap zone `[H_{t-2}, L_t]`); order block = the last down candle before a displacement bar (range > 2*ATR_14) that breaks a fractal-3 swing high (BOS); CHoCH = first BOS against the prior structure; entry limit at the 50% of the FVG/OB; stop beyond the zone; target the next swing. Placebo: random 3-bar windows, report fill rate and R versus base rate.
- **Fibonacci / Elliott:** Fibonacci entries are tested only against random retracement ratios `U(0.5, 0.7)` (placebo); Elliott wave is not falsifiable as stated and is out.

## 4. Seasonality and calendar (ticket #223; descriptive unless stated)

- Month of year (Oct vs others), Q4, January effect; halving-cycle phase (months since 2012-11-28, 2016-07-09, 2020-05-11, 2024-04-20); with about 13 observations and 4 cycles these cannot pass t >= 3.3 and are never signals by themselves.
- Reported October statistics differ by source and sample (BTC average October return about 14-26%, September mixed; October 2025 was negative). Treat as folklore with a small tilt at most.
- **21:00-23:00 UTC long (H7a, grade B in-sample):** Padysak & Vojtko (Quantpedia) report about 33-40% annualized on Gemini spot 2015-2022; about 8-9 bp gross per daily trade (est.). Test with maker entry and exit; at taker costs it fails.
- **Monday / Asia-open window (H7b, grade C+):** apply a 1h close beyond the 24h Donchian channel only between Sunday 23:00 UTC and Monday 23:00 UTC; time-only exit 24h; must appear in 2024-2025 before the hidden set is touched.
- Hour-of-day filter (activity peaks about 15-16 UTC, weak 01-05 UTC); funding-settlement windows (00/08/16 UTC bursts); day-of-week/weekend; turn of month.
- CME gap-fill folklore: compare the gap between Friday close and Sunday reopen being revisited with random weekend timestamps (base rate of revisiting a nearby price is high). Needs a documented CME calendar; deferred.
- Event windows (FOMC, CPI): volatility blackout filter only (macro signals mostly predict volatility, not direction); news stays out.

## 5. Objective regime variable (ticket #223)

Daily 00:00 UTC, from BTC closes (point in time, frozen thresholds): T1 `close > SMA200`; T2 `SMA50 > SMA200`; DD = drawdown from the running ATH (> -20% bull-like, -20% to -50% neutral, < -50% bear-like); Mayer multiple `close / SMA200` (above 2.4 overheated, below 0.8 capitulation); RV percentile (30-day realized vol over a 365-day window; < 30 calm, > 70 stressed); FR (30-day mean funding > 0.01% per 8h crowded long, < 0 crowded short); months since halving bins {0-6, 6-18, 18-30, 30-48}. Season = Bull (T1 and T2 true and DD > -20%), Bear (both false), else Transition; optional hysteresis (3 consecutive daily closes). Reading on 2026-10-09 per the research: Transition (price above the 200-day, about 35% below the October 2025 high, about 30 months after the halving). Switching rules: trend longs only when T1 = +1 (Bull or Transition), trend shorts when T1 = -1, reversion setups when efficiency ratio is low or RV percentile < 70.

## 6. Volatility estimators and filters (ticket #184)

All point in time; `r` = log returns. Range estimators on n bars of the target horizon (not scaled from 5-minute data):

```
Parkinson:       sigma^2 = (1 / (4 ln 2)) * mean( ln(H/L)^2 )
Garman-Klass:    sigma^2 = mean( 0.5*ln(H/L)^2 - (2 ln 2 - 1) * ln(C/O)^2 )
Rogers-Satchell: sigma^2 = mean( ln(H/C)*ln(H/O) + ln(L/C)*ln(L/O) )          (drift-robust)
Yang-Zhang:      sigma^2 = sigma_o^2 + k*sigma_c^2 + (1-k)*sigma_RS^2,   k = 0.34 / (1.34 + (n+1)/(n-1))
                 sigma_o^2 = var of ln(O_t / C_{t-1}),  sigma_c^2 = var of ln(C_t / O_t)
```

For 24/7 crypto the overnight gap term is small; keep Yang-Zhang as a candidate and compare by QLIKE.

HAR-RV (Corsi 2009), crypto calendar:

```
RV_t = sum of squared 1-minute (or 5-minute) log returns over day t
RV_{t+1} = b0 + bd*RV_t + bw*mean(RV_{t-6..t}) + bm*mean(RV_{t-29..t}) + e        (d=1, w=7, m=30 days)
horizon variance forecast for h < 1 day: scale by the intraday seasonal profile
seasonal profile s(hour-of-week) = trailing expanding-window mean of squared returns by slot / overall mean; deseasonalize returns by 1/sqrt(s)
optional jump term: RV split into continuous and jump parts via bipower variation BV = (pi/2) * sum |r_i||r_{i-1}|, jump J = max(RV - BV, 0)
fit: expanding window, refit weekly, forecasts only use data to t; evaluate QLIKE and MSE vs EWMA and ATR
```

Other volatility tools: variance ratio `VR(q) = Var(r_q) / (q*Var(r_1))` (< 1 mean reversion at that scale, > 1 trend); Kaufman efficiency ratio `ER_N = |P_t - P_{t-N}| / sum |dP|` (N = 20 bars; > 0.3 trending mode, < 0.15 ranging); Hurst exponent by DFA or R/S on 1h returns (use as a descriptor, noisy); Lee-Mykland jump test `L_i = |r_i| / sqrt(BV_local)` with a threshold about 4; post-jump drift tested two-sided (continuation vs reversal) at 15m-4h. Vol targeting: `w = sigma_target / sigma_hat` with a cap (Moreira & Muir 2017; Harvey et al. 2018): primarily risk control, alpha second (critics show real-time implementation can fail).

Large-move reversal (H10, grade C): a 15m-1h return beyond +/-4*sigma_h when the daily TSMOM state is neutral or opposite -> fade; time-only exit 1-4h; catastrophe stop 2*sigma beyond the extreme; run it as the other side of the same event study as the continuation signal (SPEC-3 H5) and report which dominates at each horizon.

## 7. Candlestick/ML notes

Meta-labeling (Lopez de Prado): a secondary classifier decides take/skip and size of a primary signal; it improves precision only if the primary has edge and costs degrees of freedom. Only after a primary shows positive gross edge (#43). Feature list (if reached): returns at 1/5/15/60/240 minute lags, realized-vol percentiles, volume z-scores by hour, taker imbalance windows, funding and premium z, OI change (when available), distance to nearest level in sigma, regime label. Purged k-fold with embargo >= label horizon; features lagged one bar. Deep learning on raw minute bars is excluded (low prior given the effect sizes).


---
_Generated by [Claude Code](https://claude.ai/code)_
