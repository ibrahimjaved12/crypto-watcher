# SPEC-1: Evaluation methods, statistics and cost arithmetic

*Part of the strategy specification library (#219). 2026-10-09. Draft by Claude (AI) from two research rounds and the owner discussion. Numbers marked (est.) are estimates; sources are listed in SPEC-0. `CLAUDE.md` is the source of truth.*

## 1. Cost in R

Definitions (model v1, existing): taker fee 5 bp per side (1/2000), maker 2 bp per side (1/5000), slippage floors 1 bp market and 2 bp stop. Round trip with a market entry and a stop exit is about 8-13 bp; the harness uses about 11 bp as the working figure.

```
cost_R = (fee_in + fee_out + slip_in + slip_out + funding_bp) / stop_bp
stop_bp = k * sigma_h (in bp of price)
funding_bp = sum over settlements crossed of (rate_i * side_sign), long pays when rate > 0
```

Examples (cost 11 bp): stop 220 bp (BTC 4h, k = 2) -> 0.05 R; stop 100 bp (1h) -> 0.11 R; 15m with k = 1 -> about 0.4 R; daily horizon with k = 2 (stop about 5%) -> about 0.02 R. Longer holds with wider stops lower cost in R; funding adds about 1 bp per 8h at the typical 0.01% rate (paid by longs), so a 5-day long pays about 15 bp, which is larger than the round trip. Funding must therefore be in every multi-day result.

Maker entry + taker exit: fee_in 2 bp instead of 5 bp, but only the filled share of signals trades; adverse selection must be measured (section 7).

## 2. Statistical power

Trades needed for a one-sided t-statistic t* given mean net edge mu and per-trade standard deviation sigma_R (both in R):

```
N = (t* * sigma_R / mu)^2          minimum detectable edge: mu_min = t* * sigma_R / sqrt(N_eff)
```

With t* = 3.28 (K = 48) and sigma_R = 0.8 R (est.): mu = 0.10 R needs about 700 trades; 0.05 R about 2,800; 0.03 R about 7,700. With sigma_R = 1.2 R the counts are 2.25x larger. A variant with a few dozen trades can only detect edges above about 0.35 R (sigma_R 0.8): it cannot pass even if it is good.

Effective sample size: trades on six correlated perps at the same time are not independent. Use clusters by UTC day (or by signal-hour) in the bootstrap and report `N_eff = N / (1 + (n_bar - 1) * rho_bar)` (n_bar = average simultaneous trades, rho_bar = average pairwise correlation of trade R) as a diagnostic. Overlapping holds (hold longer than signal spacing) are also dependent: use block length at least the holding time.

Requirements: every question prints, before running, expected trades per variant, an estimate of sigma_R from a random-entry run, and mu_min. Variants whose mu_min exceeds a plausible edge (0.15 R at 4h, est.) are pooled across symbols/sides or redesigned (more trades) before running.

## 3. Screening mode on continuous forward returns

Barrier PASS tests spend statistical power on triggered trades only. A cheap first look uses every bar:

```
r_{t,t+h} = log(open_{t+1+h} / open_{t+1})            (entry-open to exit-open, point in time)
r_{t,t+h} = alpha_sym + beta * s_t + gamma * controls_t + eps     (pooled, symbol fixed effects)
s_t = standardized signal (z-score vs trailing 30 days, same window)
```

- Standard errors: Newey-West with lag at least h / bar-spacing, clustered by day.
- Report beta in bp per 1 sigma of signal, the information coefficient (Spearman rank correlation of s_t with r_{t,t+h}), and the implied gross per trade `beta * E|s| over triggered bars` in bp against the 11 bp hurdle.
- Controls: past return over the same window (so a flow signal that is just momentum is detected), volatility forecast, hour-of-day dummies.
- Screens are logged in the experiment ledger as type "screen". They never count as a PASS; a family proceeds to barrier/exit tests only if the screen beta has the expected sign on development AND validation.
- Dose-response: for a threshold ladder (section 6), fit the slope of net R (or forward return) on the signal strength bucket; a real effect grows with strength. This is one pre-registered hypothesis (monotonic trend) in addition to the per-rung results.

## 4. Barrier theory and calibration

For a driftless Brownian price with horizon variance 1 (price in units of sigma_h), stop at -b, target at +a (b = k, a = k * rr):

```
P(target first, no time limit) = b / (a + b)
P(still open at time T) = (4/pi) * sum_{n=1,3,5,...} (1/n) * sin(n*pi*b/w) * exp(-(n*pi)^2 * T / (2*w^2)),  w = a + b
T = time limit in horizon units (4 for the current 4 x horizon limit)
```

Discrete monitoring (labels are checked every 5-15 minutes) widens barriers effectively by `0.5826 * sigma_step` (Broadie-Glasserman-Kou), where `sigma_step = sigma_h * sqrt(step / h)`.

Expected expiry share with correctly scaled sigma (T = 4):

| k | rr = 1 | rr = 1.5 | rr = 2 | rr = 3 |
| --- | --- | --- | --- | --- |
| 1 | 1% | 5% | 12% | 26% |
| 2 | 37% | 55% | 64% | 68% |

(with 15-minute monitoring on a 240m horizon the 2-row becomes 44%, 60%, 68%, 72%).

Reading: **an expiry share of about 70% at k = 2, rr = 1.5 is not by itself evidence of badly mis-scaled volatility**. The implied ratio of realized to predicted sigma that reproduces about 70% is 0.84-0.89, i.e. sigma overstated by 10-16% (under the Brownian assumption). The earlier research claim that a correct sigma gives only 25-35% expiries was wrong for k = 2. Wider targets (rr 2, 3) and wider stops raise expiry by construction, so the time limit must grow with them (hold classes, #220).

Calibration audit (read-only, #220), per symbol, horizon (15m, 60m, 240m), half-life (1, 3, 7 days), development segment:

```
z = r_h / sigma_h                       (sigma_h = point-in-time value used by the label engine)
report: sd(z), mean|z|, quantiles 1/5/25/50/75/95/99%, share |z| > 1, 2, 3
variance ratio VR(q) = Var(r_q) / (q * Var(r_1)) on 5-minute returns, q = 3, 12, 48
touch rates of +/- k sigma barriers within h and within T*h, versus section 4 formula
by UTC hour of day (deseasonalization check)
pass: sd(z) in [0.9, 1.1] and touch rates within 10% (relative) of theory
```

Candidate sigma engines (all point in time): (a) current EWMA of 5-minute variance scaled by sqrt(h/5); (b) EWMA scaled by sqrt(VR(q)) with VR estimated on an expanding window; (c) h-bar range estimators (SPEC-2 section 6); (d) HAR-RV with hour-of-week deseasonalization (SPEC-2 section 6). Choose by calibration, then by out-of-sample forecast loss (QLIKE).

## 5. Placebo designs (one per role)

1. **Random entries, identical barriers/exits** (existing): same symbols, same number of entries per month, random direction or both directions. Strategy must beat the matched distribution (p <= 0.05) AND be positive net.
2. **Placebo levels (Osler style)** for level strategies: for each real level draw 20 artificial levels with (a) the same distance to the current price at the approach time, (b) the same approach condition, (c) offset from real levels (for round numbers uniformly within +/- 0.3-0.7 of the grid spacing). Also time-shuffled swing levels (levels from another week rescaled by price ratio). Report real minus placebo with block-bootstrap intervals, by symbol and volatility tercile.
3. **Random thinning** for filters: keep a random subset of trigger signals with the same retention fraction p (matched by symbol and month); repeat 1,000 times; the filtered result must lie above the 95th percentile of the thinned distribution. A filter that only removes trades is not edge.
4. **Regime placebo**: block-permute or circularly shift regime labels by a random offset of at least 30 days, preserving state durations; the real label must beat the shifted ones.
5. **Time-shift placebo for signals**: shift the signal series by a random offset (days) and re-evaluate with the same exits; isolates timing information from exit/geometry effects.
6. **Random-direction control** for volatility-timing strategies (compression breakouts): same entry times, random sign.
7. **Exit placebo**: exits are judged on (real entries - random entries) under the same exit, not on standalone P&L.

## 6. Multiple testing, ledger and splits

- K = variants per question; every rung of a threshold ladder, every exit, every filter and every playbook is a variant. Cumulative K feeds the deflated Sharpe.
- Gates (existing): stationary bootstrap, SPA, Romano-Wolf StepM, deflated Sharpe, matched placebo, positive at 2x cost, t >= 3.28 for K = 48 (3.0 floor for small K), FRAGILE if ambiguity > 10% or X share > 5%.
- Pre-registration: before a batch runs, commit the hypotheses, parameter values (at most 3 per parameter), K cap and expected mu_min to the private repo. The experiment log is append-only.
- Splits: development 2024-01..2025-06, validation 2025-07..2025-12, hidden 2026-01..2026-09 (once per question, with plan id), forward live from 2026-10. Extra history (2020-2023) is a fallback for multi-day families only (owner decision 2026-10-09).
- Threshold ladders: rungs are K variants for the PASS gate; the monotonic dose-response test is a single extra hypothesis.
- Replication venue (#228): PASS/FRAGILE on Binance data is re-run on a second venue's data; not a new variant, belongs to the same hidden opening.

## 7. Maker-entry model

```
post limit at best price (or at the level) at signal-bar close
fill only if the trade price goes THROUGH the limit by >= 1 tick (touch is not a fill), using aggTrades
cancel after T minutes (T in {1, 5, 15}); unfilled signals earn 0 and cost 0
```

Report: fill rate; mean forward return of filled vs unfilled signals (adverse selection = filled minus unfilled); net R per signal and per fill; comparison with taker entry. Practitioner simulations report 8-25% fills for near-side limits with adverse selection, so maker entry is only rational where the move is much larger than the wait (4h+ holds), and the expected benefit is a lower cost (about 3 bp per side) at the price of missed trades.

## 8. Resampling and inference details

- Stationary bootstrap (existing) with expected block length >= max(holding time, 1 day); cluster by UTC day when trades from several symbols coincide.
- Bootstrap by calendar month for regime and season comparisons.
- Report: n trades, n non-trades by status, mean net R at 1x/2x cost, median, win rate, payoff ratio, tail share (top-decile winners' share of total R), max drawdown in R, longest losing streak, ambiguity (X) and liquidation (L) shares.
- Report sample sizes next to every result; no result without N.

## 9. Pass-bar sanity

Sanity checks before trusting any PASS: (a) the result is positive in both validation halves; (b) neighbors in parameter space keep at least half the effect (plateau); (c) it survives removing the best month; (d) long and short legs are not both required to be positive but each is reported; (e) it is not explained by exposure to the market (compare with buy-and-hold at the same exposure and with the always-long/short random baselines, which are asymmetric in a trending sample).


---
_Generated by [Claude Code](https://claude.ai/code)_
