# SPEC-0: Master plan, hypothesis index, order of work and decision rules

*Strategy specification library index (#219). 2026-10-09. Draft by Claude (AI) from the owner discussion and three research rounds (round 1 diagnosis and ranked test plan; round 2 strategy catalogue and break-even diagnosis; the owner's own earlier ideas). `CLAUDE.md` is the source of truth. SPEC-1 methods and statistics; SPEC-2 trend, TA, volatility, regime and calendar; SPEC-3 levels, order flow, positioning and cross-asset; SPEC-4 exits, sizing, leverage, margin and fees; SPEC-5 data. Private results stay in the private research-data repo.*

## 1. Where we are and what we learned

- Benchmark rounds on the development segment (hidden segment never opened): classic TA baselines are not profitable after costs at 1h and are underpowered at 4h; a cumulative taker-imbalance signal has a small real gross effect that costs consume; funding/premium contrarian signals carry no information; random entries lose roughly the round-trip cost.
- Why (to be tested, not assumed): (1) cost per R: about 11 bp round trip is 0.05-0.2 R before any signal; (2) documented crypto edges at 15m-4h are a few bp, trend effects are multi-day; (3) fixed targets and a short time limit truncate the right tail; (4) statistical power: a 4h variant needs hundreds to thousands of independent trades; (5) the volatility scaling may be slightly too high (see the correction below); (6) funding is a degenerate feature (clamped at its floor most of the time).
- **Correction to the round-2 research.** It said a correctly scaled sigma should produce only 25-35% time-limit expiries at k = 2, rr = 1.5 (limit 4 x horizon). The exact Brownian calculation (SPEC-1 section 4) gives **55%** (60% with 15-minute monitoring), so an observed expiry of about 70% implies realized sigma about 0.85-0.9 of predicted, not 0.6-0.7. The calibration audit (#220) will decide; most of the high expiry is barrier geometry, and wider targets raise it further (68-72% at rr = 3), so longer time limits are required for wide targets.
- The owner's style (levels, 4h closes, multi-day targets) has not been tested yet. The tests so far (indicator crossings, flow, funding) are standard first tests, not how the owner trades.

## 2. Owner decisions and directions (2026-10-09)

1. Horizon is conditional, not fixed (scalp 15m-1h, intraday 1h-24h, swing 1-20+ days).
2. Algorithms have roles (context, zone, trigger, filter, exit, sizing) and are combined into written, versioned playbooks (#226).
3. Breakeven is not good enough: test widely, including hearsay, by trial and error under the multiple-testing controls.
4. Moving SL/TP is important. Isolated margin only.
5. **Leverage is not fixed (7x was only an example): the program determines leverage** from stop distance, risk budget, liquidation buffer, venue limits and edge estimate (SPEC-4 section 4).
6. Seasons/halving are context variables coded objectively; news stays out for now.
7. Levels come first: the owner's style is level reaction, so the level-reaction study (#185) is moved ahead of squeezing more out of order flow (which continues in parallel as a cheap slice).
8. Test larger targets (2x, 3x risk) and a stronger-signal ladder; download more datasets for other kinds of testing.
9. Do not rely on Binance alone: OKX (usable from Pakistan) as a second data source, replication venue and possible execution venue (#228). 2020-2023 history is a fallback only.

## 3. Hypothesis index (research ranking merged with owner items)

| ID | Hypothesis | Spec | Ticket | Data now? |
| --- | --- | --- | --- | --- |
| H1 | Multi-day Donchian ensemble trend, vol-targeted, trailing midline exit | SPEC-2 s1.1 | #222 | yes |
| H1b | TSMOM 7/14/28d; Donchian 4h trade-level | SPEC-2 s1.2-1.3 | #222 | yes |
| H2 | Cumulative taker-imbalance continuation (of-v2: ladder, 1.5x/2x/3x targets, time-only exits, maker, vol filter) | SPEC-3 s2.1 | #188 | yes |
| H3 | Quarter-hour opening imbalance (replication; feature/filter) | SPEC-3 s2.2 | #188 | yes |
| H4 | Round-number first-touch fade (placebo levels) | SPEC-3 s1.4 L1 | #185 | yes |
| H5 | Clean break of round number / prior-week extreme continuation | SPEC-3 s1.4 L2 | #185 | yes |
| H6 | Owner's 4h-close breakout with multi-day target at next level | SPEC-3 s1.4 L6 | #185 | yes |
| H7 | 21-23 UTC long (maker); Monday/Asia window filter | SPEC-2 s4 | #223 | yes |
| H8 | Deleveraging washout reversal (OI flush) | SPEC-3 s3 | #188 | needs metrics |
| H9 | Volatility compression breakout vs random direction | SPEC-2 s3 | #187 | yes |
| H10 | Large-move reversal vs continuation | SPEC-2 s6, SPEC-3 | #188 | yes |
| H11 | Funding-carry filter | SPEC-2 s1.4 | #223/#226 | yes |
| C1-C4 | Breadth flush (owner's idea), BTC lead-lag, XS momentum, pairs | SPEC-3 s4 | #225 | yes |
| E1 | Exit families X0-X9 on survivors | SPEC-4 s1-2 | #221 | yes |
| S1 | Sizing, Kelly fraction, leverage selector, vol targeting | SPEC-4 s3-5 | #36, #226 | yes |
| T1 | Remaining TA catalogue (Supertrend, ADX, squeeze, NR7, ORB, VWAP z, pivots, SMC with placebo, divergence, candles at levels) | SPEC-2 s2-3 | #187 | yes |
| V1 | Volatility engines (range estimators, HAR) and calibration | SPEC-2 s6, SPEC-1 s4 | #184, #220 | yes |
| R1 | Objective season/regime label and role test | SPEC-2 s5 | #223 | yes |

Dropped or skipped (reasons in the catalogue): textbook crossovers/oscillators as primaries at 15m-1h; anything at 15m with taker execution; BTC-to-alt lead-lag as a main idea on liquid perps; order-book-imbalance strategies (no depth data, seconds-scale); funding/basis arbitrage (needs spot hedge); Fibonacci/generic pivots as primaries; retail long/short ratio as stand-alone; deep learning on raw 1-minute bars; full-Kelly or fixed 7x sizing; martingale/grid/DCA bots; Elliott wave.

## 4. Order of work (waves; limited by review capacity of about 5 PRs per day)

**Wave 0 (documents, now):** commit SPEC-0..5 to `docs/strategy-specs/` (docs PR, #229); `CLAUDE.md` horizon text (D1); this plan in the discussion; OKX availability audit (#228 phase 1, research by the architect, no code).

**Wave 1 (parallel, no dependencies between them):**
1. **#220 Harness v2**: calibration audit (read-only first), hold classes, time-only exit mode, maker entry, power report, pooled hypotheses, funding costs, screening mode. Unlocks everything.
2. **#185 slice L0**: level-reaction event study, descriptive (bounce/penetration frequencies of real vs placebo levels; counts first; no trade labels needed). The owner's style.
3. **#188 slice of-v2a**: signal-strength ladder c in {2, 2.5, 3, 3.5} x rr {1.5, 2, 3} at k = 2 on existing lb1 labels (K = 12) plus the dose-response and past-return-control screens. Cheap, answers the owner's questions about bigger targets and stronger signals.
4. **#224 phase 1**: Binance metrics archive download and coverage report; start live logging of REST-only series and the liquidation recorder (D3).
5. **#223**: regime/season/calendar labeler (data-only, independent of #220).

**Wave 2 (after #220 results):**
6. **#184**: implement the winning sigma engine (range estimators/HAR/VR-corrected) as the label engine's sigma (new label revision).
7. **#221 Exit engine** (X0-X9) with the first demonstration on of-v2 and on level setups.
8. Label revision lb2 (k = 3, 4; hold-class time limits) if the calibration audit or of-v2a supports wider stops.

**Wave 3 (after #220 and #221):**
9. **#222 Trend family** (H1, TSMOM, Donchian 4h) with exits X2/X3; Mode A and Mode B evaluation.
10. **#185 slice L1**: level setups L1-L6 with exits and maker model; H6 owner's style.
11. **#188 slice of-v2b**: time-only 8/12/16h, maker entry, vol filter, k = 3, 4; H3 feature.

**Wave 4 (after metrics and results of waves 1-3):**
12. **#188 phase 2** OI family (H8 and quadrants); #187 TA v2 catalogue batch; #186 regime-conditioned tests using #223.

**Wave 5:** #225 cross-asset; #226 composition harness with the survivors; #43 meta-labeling on the best primary; #228 phases 2-4 (OKX lake, venue-aware simulation, replication).

**Product track (independent):** setup/outcome/ledger/dashboard work (#31, #32, #36, #30, #33) continues; setups store exit rules; MVP may ship with all setups "unvalidated".

## 5. Decision rules (what we do with results)

- **Continue a family** if the screen has the expected sign on development AND validation, net R at 1x cost is positive on development with a sensible interval, a parameter plateau exists, and `mu_min` (SPEC-1 section 2) is below a plausible edge.
- **Promote to a playbook component** only after its role test (#219).
- **Stop adding entry families** if, after waves 1-3, no family shows a positive validation result: then (a) the MVP ships with all setups "unvalidated" (the acceptance criteria allow this), (b) consider paid data only with owner approval, (c) revisit hold classes and execution cost reduction (maker, USDC perps, VIP tiers) before more signals.
- **Hidden segment:** opened once per question with a committed plan; shortlist of at most 3; replication on the second venue in the same opening.
- Real-money execution stays manual and outside the MVP; any live use starts with small size and fills reconciled against live prints.

## 6. Honest expectations

Documented crypto edges at 15m-4h are a few bp and the cost is about 11 bp; the best-evidenced edge is multi-day trend following with volatility sizing and trailing exits (long-only spot source, net Sharpe above 1.5 reported by secondary summaries, not verified by us). Level effects are real but small in FX and untested here. Flow effects are real but tiny. Expected edges in the specs are estimates to order tests, not forecasts. A small positive net edge of 0.03-0.1 R per trade with correct sizing can still compound; the aim is to find any that is robust, then size it by fractional Kelly.

## 7. Sources used (as reported by the research; not independently verified unless stated)

Zarattini, Pagani & Barbon (SSRN 5209907); Moskowitz, Ooi & Pedersen (JFE 2012); Liu & Tsyvinski (RFS 2021); Kim & Hansen (arXiv 2607.09426) and the Kimbrough replication; Shen, Urquhart & Wang (2022, intraday momentum); Osler (FRBNY EPR 2000, "Support for Resistance"; Staff Report 125 / JF 2003); Urquhart (2017) and later round-number papers; Kaminski & Lo (stop-loss rules); Corsi (HAR-RV 2009); Moreira & Muir (JF 2017); Harvey et al. (2018); He, Manela, Ross & von Wachter (arXiv 2212.06888); Palazzi et al. (SSRN 6725492); Garcia Seuma (arXiv 2607.27070, 2608.03616); arXiv 2306.17178 (execution study); arXiv 2602.00776 (maker backtests); Hudson & Urquhart (Annals of OR 2021); Hutchinson et al. (RIBAF 2022); Deprez & Frömmel (IREF 2024); Lopez de Prado (Advances in Financial Machine Learning, 2018); Hudson & Thames (meta-labeling); Padysak & Vojtko (Quantpedia); Concretum ("Monday Asia Open"); CoinQuant Donchian backtests; Binance FAQ (liquidation price), Binance developer docs (metrics, long/short ratios, force orders), data.binance.vision; okx.com/historical-data. Community items (Reddit/forum/Substack) are grade C or D: the research found no verifiable Reddit thread with live results.


---
_Generated by [Claude Code](https://claude.ai/code)_
