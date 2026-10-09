# Strategy catalogue v1 (2026-10-09)

Source: a second research round (papers, practitioner posts, public repos) plus owner input. **Evidence grades are as reported by that research and have not been independently verified by us**: A = peer-reviewed or rigorous out-of-sample; B = credible practitioner/backtest evidence with costs; C = community or unverified; D = contradicted. No verifiable Reddit thread with live crypto-futures results was found; community items are grade C. Edge sizes are estimates used only to prioritise tests.

Columns: **Role** (CTX context, ZONE, TRIG trigger, FILT filter, EXIT, SIZE) · **Data** (K klines/taker, A aggTrades, F funding, P mark/index/premium, M metrics archive, X not available) · **Test** (RN ready now, NM needs metrics, ND needs new data, NT not testable) · **Ticket** · **Pri** (P1 first batches, P2 later, P3 low, P4 skip) · **Status** is updated by the composition harness: untested / rejected / role-limited / accepted.

## 1. Classic technical analysis

| ID | Strategy | Role | Gr | Data | Test | Ticket | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1.1 | Donchian/Turtle breakout, N in {20,55,120} on 4h, trailing exit | TRIG | B | K | RN | #222 | P1 |
| 1.2 | Donchian ensemble, daily lookbacks 10..250, vol-sized | TRIG+SIZE | B | K | RN | #222 | P1 |
| 1.3 | Vol-scaled time-series momentum (7/14/28d) | TRIG+SIZE | A/B | K | RN | #222 | P1 |
| 1.4 | First-half-hour to last-half-hour intraday momentum | TRIG | B | K | RN | #187 | P4 (about 2 bp, below cost) |
| 1.5 | MA ribbon / price-to-MA score | CTX | B | K | RN | #222, #223 | P1 |
| 1.6 | Supertrend(10,3) | TRIG/EXIT | C | K | RN | #187 | P2 |
| 1.7 | Ichimoku 9/26/52 | TRIG | C | K | RN | #187 | P3 |
| 1.8 | Parabolic SAR (as trail) | EXIT | C | K | RN | #221 | P3 |
| 1.9 | ADX/DMI trend filter | FILT | C | K | RN | #187 | P2 |
| 1.10 | Stochastic/StochRSI pullback inside a daily trend | TRIG | C | K | RN | #187 | P2 |
| 1.11-1.12 | CCI, Williams %R | TRIG | C | K | RN | #187 | P3 |
| 1.13 | N consecutive candles fade | TRIG | C | K | RN | #187 | P3 |
| 1.14 | Heikin-Ashi runs (fill at real prices) | TRIG | C | K | RN | #187 | P3 |
| 1.15 | Candlestick patterns (engulfing, pin bar, inside bar, doji, stars, three soldiers), only at levels | TRIG | C/D | K | RN | #185 | P3 |
| 1.16 | Kernel-regression chart patterns (Lo-Mamaysky-Wang) | TRIG | B (distributional) | K | RN | #187 | P3 |
| 1.17 | Fibonacci 0.618 pullback vs random ratios | ZONE | C | K | RN | #185 | P3 |
| 1.18 | Pivot points (classic, Camarilla, Woodie) | ZONE | C | K | RN | #185 | P2 |
| 1.19 | VWAP / anchored-VWAP z-score reversion | ZONE/TRIG | C | K | RN | #187 | P2 |
| 1.20 | Volume profile: POC, value area, HVN/LVN | ZONE | C | K/A | RN | #185 | P3 |
| 1.21 | Opening-range breakout (00:00 UTC, 13:30 UTC, funding hours) | TRIG | C | K | RN | #187 | P2 |
| 1.22 | Prior-day/week high-low sweep reversal | TRIG | C | K | RN | #185 | P1 |
| 1.23 | Wyckoff spring/upthrust | TRIG | C | K | RN | #185 | P2 |
| 1.24 | SMC/ICT: FVG, order block, BOS/CHoCH (strict definitions, placebo windows) | TRIG/ZONE | C | K | RN | #187 | P2 |
| 1.25 | Elliott wave | - | D/NT | - | NT | - | skip |
| 1.26 | Bollinger/TTM squeeze breakout | TRIG | C | K | RN | #187 | P2 |
| 1.27 | NR4/NR7 compression breakout | TRIG | C | K | RN | #187 | P2 |
| 1.28 | RSI/MACD divergence (fractal-5 pivots, respecting confirmation lag) | TRIG | C | K | RN | #187 | P3 |

## 2. Volume, order flow, microstructure

| ID | Strategy | Role | Gr | Data | Test | Ticket | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2.1 | Cumulative taker imbalance re-specified: 8-12h time-only exit, maker entry | TRIG/FILT | B | K | RN | #188 | P1 |
| 2.2 | Quarter-hour opening imbalance (effect about 0.5 bp gross, "real pattern, no trade") | FILT (feature) | A-/B | A | RN | #188 | P2 |
| 2.3 | CVD trend and divergence | TRIG/FILT | C | K | RN | #188 | P2 |
| 2.4 | Volume climax / exhaustion fade | TRIG | C | K | RN | #188 | P1 |
| 2.5 | OBV, Chaikin money flow | FILT | C | K | RN | #188 | P4 |
| 2.6 | VPIN flow toxicity | FILT | B | K/A | RN | #188 | P2 |
| 2.7 | Kyle lambda / Amihud shocks | FILT/SIZE | B | K | RN | #188 | P3 |
| 2.8 | Large prints from aggTrades (aggTrades merge fills; prints are not orders) | FILT | C | A | RN | #188 | P2 |
| 2.9 | Average trade size | FILT | C | K | RN | #188 | P4 |
| 2.10 | Absorption / effort-vs-result at levels | TRIG | C | K | RN | #185, #188 | P2 |
| 2.11 | Aggressor imbalance after a level touch | FILT | C | K/A | RN | #185, #188 | P2 |
| 2.12 | Stop-hunt: sweep plus taker spike plus reclaim | TRIG | C | K | RN | #185 | P1 |

## 3. Liquidation, leverage, positioning

| ID | Strategy | Role | Gr | Data | Test | Ticket | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 3.1 | OI-flush proxy ("buy the blood"): OI drop + big move + volume spike | TRIG | C | M,K | NM | #188/#224 | P1 after data |
| 3.2 | OI x price quadrants | CTX/FILT | C | M,K | NM | #188 | P2 |
| 3.3 | OI / volume leverage ratio (fragility filter) | CTX | C | M,K | NM | #188 | P2 |
| 3.4 | Funding-extreme contrarian | TRIG | D (first results: no information) | F | RN | - | skip |
| 3.5 | Spot-perp basis carry (non-directional) | - | A (existence) | spot | ND | #224 | P3 |
| 3.6 | Premium dislocation / basis flip reversal | TRIG | C | P,K | RN | #188 | P2 |
| 3.7 | Global long/short account ratio contrarian | FILT | C | M | NM | #188 | P2 |
| 3.8 | Top-trader vs crowd divergence | FILT | C | M | NM | #188 | P2 |
| 3.9 | Taker ratio from the archive | FILT | C | M | NM | #188 | P4 (duplicates kline taker data) |
| 3.10 | Model-based liquidation clusters ("heatmap magnet"), assumed leverage mix fixed a priori | ZONE | C | M,K | NM | #188 | P3 |
| 3.11 | Funding-settlement windows (00/08/16 UTC) | TRIG | C | F,K | RN | #188 | P2 |
| 3.12 | Mark-vs-last gap reversion | TRIG | C | P | RN | - | P4 (below cost) |
| 3.13 | Cross-exchange funding differentials | - | C | X | ND | - | P4 |
| 3.14 | Pre-cascade taker-flow variance compression (n = 7 events) | CTX | B (tiny sample) | K | RN | #188 | P2 |
| 3.15 | Real liquidation feed signals | TRIG | C | X | ND | #224 recorder | later |

## 4. Volatility

| ID | Strategy | Role | Gr | Data | Test | Ticket | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 4.1 | Volatility targeting | SIZE | A (equities)/B | K | RN | #184, #222 | P1 |
| 4.2 | HAR-RV forecast as sigma engine | SIZE/EXIT | A | K | RN | #184 | P1 |
| 4.3 | Parkinson, Garman-Klass, Rogers-Satchell, Yang-Zhang as stop-distance engines | EXIT | A | K | RN | #184, #220 | P1 |
| 4.4 | Realized-vol regime filter | CTX | B | K | RN | #223 | P1 |
| 4.5 | Compression then expansion | TRIG | C | K | RN | #187 | P2 |
| 4.6 | Jump detection and post-jump drift (two-sided test) | TRIG/FILT | B/C | K | RN | #188 | P2 |
| 4.7 | DVOL minus realized vol | CTX | C | ND (free) | ND | #224 | P3 |
| 4.8 | HMM/Markov switching (filtered probabilities only, never smoothed) | CTX | C | K | RN | #186 | P3 |
| 4.9 | Efficiency ratio, Hurst, variance ratio | CTX | C | K | RN | #223 | P2 |

## 5. Levels (the owner's style)

| ID | Strategy | Role | Gr | Data | Test | Ticket | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 5.1 | Pivot-cluster levels, first retest | ZONE+TRIG | B (FX methodology)/C | K | RN | #185 | P1 |
| 5.2 | Round numbers: fade first touch vs continuation after a close beyond | ZONE+TRIG | B (FX) | K | RN | #185 | P1 |
| 5.3 | Prior day/week/month highs-lows, weekly/monthly opens | ZONE | C | K | RN | #185 | P1 |
| 5.4 | Break and retest with 4h-close confirmation | TRIG | C | K | RN | #185 | P1 |
| 5.5 | Failed breakout / spring | TRIG | C | K | RN | #185 | P1 |
| 5.6 | 4h close confirmation vs intrabar trigger (A/B) | method | - | K | RN | #185 | P1 |
| 5.7 | Owner's measured-move rule ("4h close at X implies next 4h reaches X+Y") | TRIG | C | K | RN | #185 | P1 |
| 5.8 | Range trading between support and resistance | TRIG | C | K | RN | #185 | P2 |
| 5.10 | Placebo-level protocol (mandatory for all level tests) | method | - | K | RN | #185 | P1 |

## 6. Seasonality, cycle, calendar

| ID | Effect | Role | Gr | Data | Test | Ticket | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 6.1 | Month of year ("Uptober", weak September); sources disagree; about 13 observations | CTX | C | K | descriptive | #223 | P3 |
| 6.2-6.3 | Q4 strength, January effect | CTX | C | K | descriptive | #223 | P3 |
| 6.4 | Halving-cycle phase (n = 4) | CTX | C | dates | descriptive | #223 | P3 |
| 6.5 | Long 21:00-23:00 UTC with maker entry/exit | TRIG | B | K | RN | #223 | P1 (cheap) |
| 6.6 | Hour-of-day filter (weak 01-05 UTC, peak 15-16 UTC) | FILT | B | K | RN | #223 | P2 |
| 6.7 | Day-of-week / weekend (single pre-registered test) | CTX | C | K | RN | #223 | P3 |
| 6.8 | Turn of month | CTX | C | K | RN | #223 | P3 |
| 6.9 | CME gap fill vs random weekend timestamps | TRIG | C | K+calendar | RN | deferred | P3 |
| 6.10 | FOMC/CPI windows (volatility blackout filter only) | FILT | C | calendar | ND (free) | deferred | P3 |
| 6.11 | Options expiry / max pain | - | C | X | ND | - | P4 |

## 7. Cross-asset

| ID | Strategy | Role | Gr | Data | Test | Ticket | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 7.1 | BTC lead-lag to alts | TRIG | B (existence) | K | RN | #225 | P2 |
| 7.2 | Cross-sectional momentum across the six coins | TRIG | B | K | RN | #225 | P2 |
| 7.3 | Cross-sectional 1-day reversal | TRIG | C | K | RN | #225 | P3 |
| 7.4 | Pairs ETH/BTC, SOL/ETH, BNB/BTC | TRIG | C | K | RN | #225 | P3 |
| 7.5 | Breadth flush (>= 5 of 6 coins below -2 sigma in 15 min) | TRIG/CTX | C | K | RN | #225 | P1 |
| 7.6 | Correlation breakdown | CTX | C | K | RN | #225 | P3 |
| 7.7 | Alt-season proxy (share of alts beating BTC over 90d) | CTX | C | K | RN | #223 | P3 |
| 7.8 | SPX/NDX/DXY/gold regime | CTX | B (correlation) | ND (free) | ND | deferred | P3 |

## 8. Sentiment and alternative data (later; news excluded)

Fear & Greed, Google Trends, stablecoin supply, spot-ETF flows, MVRV/on-chain: grade C or paid; free daily histories exist for Fear & Greed and ETF flow tables (#224 phase 4). Not before the families above.

## 9. Exits, management, sizing (#221)

| ID | Technique | Role | Gr | Pri |
| --- | --- | --- | --- | --- |
| 9.1 | Time-only exit (diagnostic for every entry) | EXIT | A | P1 |
| 9.2 | Percent trail (control) | EXIT | C | P3 |
| 9.3 | ATR chandelier, m in {2.5, 3, 4} | EXIT | B | P1 |
| 9.4 | Donchian trail | EXIT | B | P1 |
| 9.6 | Swing (fractal-3) trail | EXIT | C | P2 |
| 9.8 | Break-even after +1 R (expected to cut winners on trend entries; test with MAE/MFE) | EXIT | C | P1 |
| 9.9 | Partial ladder plus runner | EXIT | C | P1 |
| 9.10 | Time tightening | EXIT | C | P2 |
| 9.11 | Signal-reversal exit | EXIT | C | P2 |
| 9.12 | Pyramiding into winners | SIZE | B | P3 |
| 9.13 | Risk-% sizing, fractional Kelly (<= 0.25 x f*) | SIZE | A | P1 |
| 9.14 | Martingale / grid / DCA bots | - | D | never |

## 10. ML and meta-labeling (#43, later)

Triple-barrier plus meta-labeling needs a primary signal with positive gross edge first; purged k-fold with embargo; published deep-learning momentum gains vanish above 2-3 bp of costs; single unreplicated preprints are replication candidates only.

## Testing batches (pre-registered caps)

0. Harness v2 (#220): calibration audit, time-only exits, maker entry, power report. No K spent.
1. Trend (#222) K <= 12.
2. Levels and sweeps (#185) K <= 12.
3. Event and flow (#188 phase 1, #225 breadth) K <= 10.
4. Positioning (#188 phase 2, after #224) K <= 10.
5. Exit variants on survivors of 1-4 (#221) K <= 8 per survivor.
6. Composition and meta-labeling (#226, #43).

Filters/context to test as add-ons, not entries: 1.5, 1.9, 2.2, 2.6, 2.7, 3.3, 3.14, 4.4, 4.9, 6.x, 7.7, 7.8.

## Traps (contradicted or structurally flawed)

Martingale/grid/DCA bots (hide tail risk; the October 2025 liquidation event shows the tail); RSI 70/30 reversal on trending crypto; indicator stacking without ablation; funding-extreme contrarian as a stand-alone signal; exits as a source of edge from random entries (optimistic intrabar fills in public backtest tools fabricate this); unfalsifiable pattern systems (Elliott); smoothed regime probabilities and repainting indicators (look-ahead); seasonal bets from about 13 observations.


---
_Generated by [Claude Code](https://claude.ai/code)_
