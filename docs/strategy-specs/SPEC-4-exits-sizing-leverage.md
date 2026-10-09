# SPEC-4: Exits, trade management, sizing, leverage, margin and fees

*Part of the strategy specification library (#219). 2026-10-09. Draft by Claude (AI). Owner decisions: moving SL/TP is important; isolated margin only; **leverage is not a fixed number (7x was only an example): the program derives it from the stop, risk budget, liquidation buffer, volatility, venue limits and the edge estimate.** Binance numbers from secondary sources must be verified against the exchange before the simulator relies on them.*

## 1. Principle for exits

If prices were a martingale no exit rule would change expected P&L (optional stopping); exits reshape the payoff distribution and add value only where the serial dependence after the entry matches the exit style: trailing exits suit positive autocorrelation (trend), fixed targets with short limits suit negative autocorrelation (reversion). Kaminski & Lo (stop-loss rules): under a random walk simple stops lower expected return; under momentum they can add value; short-horizon, high-frequency stops tend to have negative "stopping premiums". So tight stops on 15m-1h mean-reversion entries are structurally negative, and fixed targets cap the right tail that trend signals need.

Therefore every entry is evaluated under these exit families side by side: **(i)** time-only exit at h, 2h, 4h (and the longer holds of the hold class) **(ii)** catastrophe stop only (k = 3-4) plus a time exit **(iii)** trailing stop (Donchian midline or 2-3 ATR chandelier) with no fixed target **(iv)** the existing fixed barriers (k in {1, 2}, rr in {1, 1.5, 2, 3}). If a signal works under (i) but not under (iv), the barriers are the problem.

Judging an exit: (a) same entries with a time-only exit; (b) `net R(real entries) - net R(random entries)` under the same exit; (c) parameter plateau; (d) MAE/MFE diagnostics. An exit never gets credit for standalone P&L (random entries with a trailing stop are a classic backtest illusion when intrabar fills are optimistic).

## 2. Exit engine rules (ticket #221)

The engine simulates signal rows only, over 1-minute bars from the entry open. Long shown; mirror for short.

```
X0 time-only:           exit at entry + H at market; optional catastrophe stop 3R
X1 fixed barriers:      stop = k*sigma_h, target = rr*stop, limit = T*h   (existing)
X2 ATR chandelier:      stop_t = max(stop_{t-1}, HH_since_entry - m*ATR_22),  m in {2.5, 3, 4}; ATR on the signal timeframe; updated only on completed bars, applied from the next bar
X3 Donchian trail:      stop = lowest low of the last M completed bars (M = N/2 of the entry channel); research version for H1: exit when close < channel midline (ratcheting)
X4 percent trail:       stop = HH_since_entry * (1 - p)  (control only; volatility-blind)
X5 swing trail:         stop = last confirmed fractal-3 low on the signal timeframe
X6 break-even:          after a COMPLETED bar with favorable excursion >= 1R: stop = entry + cost_buffer (0.15% covers fees); usually lowers expectancy on trend entries (stops winners out on retests); test with MAE/MFE
X7 partial ladder:      1/3 at 1R, 1/3 at 2R, remainder on X2; smooths equity, cannot add mean edge
X8 time tightening:     if MFE < 0.5R at H/2, move stop to -0.5R or exit at H
X9 signal-reversal:     exit when the entry signal flips
initial stop:           k*sigma_h or 2*ATR, declared per family
```

No-look-ahead and fill rules:

1. Trailing levels use data to bar t-1 and apply in bar t. Never ratchet with bar t's high and then test bar t's low.
2. Stop and target both inside one 1-minute bar: order from aggTrades inside the core window; otherwise stop first.
3. Stop fill (long) = `min(stop, bar open) - 2 bp`; mirror for short. Market exits use the existing slippage floors. In cascades add the stress gap g of section 4.
4. Liquidation is checked on **mark-price** 1-minute extremes before last-price stops (mark and last diverge in cascades).
5. Break-even moves only after the bar producing +1R has closed.
6. Funding is charged at every settlement crossed (00/08/16 UTC) from the isolated margin and moves the liquidation price.

MAE/MFE protocol (Sweeney): record maximum adverse/favorable excursion in R for every trade under X0; set the initial stop near the 90th percentile of winners' MAE; choose trailing multiples on development data from a coarse grid {2.5, 3, 4}*ATR where median giveback from MFE is smallest; report the plateau; scale trail distance by ATR or the HAR sigma, never by a fixed percentage.

Reports per exit: MAE/MFE distributions, giveback, win rate, payoff ratio, tail share, net R at 1x and 2x cost, liquidations, paired bootstrap against X0, placebo comparison.

## 3. Position sizing (risk first)

```
loss budget per trade        R_budget = f * Equity           (f = fraction of equity risked at the stop)
stop distance (fraction)     s = stop_bp / 10000
cost fraction per round trip c = (fees + slippage in fractions of notional)
notional                     N = R_budget / (s + c)           (so the full loss at the stop, including costs, equals the budget)
margin required (isolated)   M = N / L
```

Leverage L does not change the loss at the stop; it sets the margin posted and the liquidation distance. Funding accrues on notional regardless of L.

Choosing f: Kelly for R-multiples. Maximizing `E[ln(1 + f*R)] ~ f*E[R] - f^2*E[R^2]/2` gives `f* = E[R] / E[R^2]`. Use the **lower confidence bound** of E[R] from validation, not the point estimate; then take a fraction `phi_K` of Kelly (0.25 while unproven, up to 0.5 once confirmed forward) and cap f at 1-2%. Illustration: net edge E[R] = 0.03-0.05 with E[R^2] about 0.5 gives full Kelly of 6-10% of equity; a quarter of it is 1.5-2.5%. A fixed 7x notional on a 2.4% stop risks about 17% of equity per trade, roughly 3-10x fractional Kelly even if the edge were confirmed.

Volatility targeting for portfolios of continuous positions: `w = sigma_target / sigma_hat`, capped (SPEC-2 section 6).

Portfolio constraints: maximum simultaneous positions; total open risk cap (default 5% of equity); same-direction positions across the six coins are highly correlated and count as one cluster for the risk cap; maximum exposure per symbol; daily loss stop (paper wallet only).

## 4. Leverage selector (the program decides)

Inputs per trade: equity, risk fraction `f`, stop fraction `s`, symbol, notional bracket (maintenance margin rate `MMR` and amount), venue max leverage, margin budget per position `M_max` (default equity divided by the maximum concurrent positions), liquidation-buffer factor `phi` (default 0.5), stress gap `g` (default 1% for BTC/ETH/BNB/XRP, 2% for DOGE/SOL, representing fast moves past the stop), fee and funding assumptions.

Isolated liquidation distance (fraction of entry price), from the exchange formula with `WB = N/L` and `cum = 0` in tier 1:

```
long : d_liq(L) = (1/L - MMR) / (1 - MMR)        short: d_liq(L) = (1/L - MMR) / (1 + MMR)
```

Safety rule: `s + g <= phi * d_liq(L)`  (the stop plus the gap must sit inside half the liquidation distance). Solving for the largest allowed leverage (long):

```
L_max = 1 / ( MMR + (1 - MMR) * (s + g) / phi )
```

Selection:

```
1. N = R_budget / (s + c)
2. L_need = ceil( N / M_max )                 (smallest leverage that fits the margin budget)
3. L_cap  = min( L_max, venue/bracket max )   (liquidation safety and venue limits)
4. if L_need > L_cap : reduce N (smaller position) until L_need <= L_cap, or skip the trade; never raise leverage past L_cap
5. L = max(1, L_need) (lowest leverage that works)  -> maximum liquidation buffer at zero cost
6. recompute MMR for the notional bracket and iterate once if the bracket changed
```

So leverage is an OUTPUT. A tight-stop scalp on a 1% stop may allow up to about 40x by the safety rule but the margin budget usually needs only 1-3x; a 5% stop caps leverage near 10x. Higher leverage is only a margin-efficiency choice when several positions must share limited equity. Examples (BTC, MMR 0.40%, before fees): 3x liquidates about 33% away, 5x about 19.7%, 7x about 13.9% (long) / 13.8% (short), 10x about 9.6%, 20x about 4.6%. Worked sizing: equity 10,000, f = 1%, stop 2.4%, cost 0.11% -> N = 3,984 notional; margin 569 at 7x or 1,328 at 3x; loss at the stop about 100 (plus about 4.4 in fees).

Later experiment variants (explicit, versioned): score-dependent sizing (a high setup score must never automatically raise leverage), Kelly fraction ladder, volatility-targeted sizing.

## 5. Isolated margin facts for the simulator (ticket #36)

- **Formula (Binance FAQ, one-way mode, isolated):** `LP = (WB + cum - s*Q*EP) / (Q*MMR - s*Q)`, `s` = +1 long / -1 short, `WB` = isolated wallet balance (= Q*EP/L at entry), `cum` = maintenance amount of the bracket. Tier 1: long `LP = EP*(1 - 1/L)/(1 - MMR)`, short `LP = EP*(1 + 1/L)/(1 + MMR)`.
- **Maintenance margin** comes from the notional bracket, not from the leverage chosen: `MM = N*MMR - cum`. BTCUSDT tier 1 (owner-supplied): MMR 0.40% up to 300,000 USDT, max leverage 150x. Other symbols' brackets: still to be supplied or snapshotted; snapshot `GET /fapi/v1/leverageBracket` per symbol per day and version it (Binance may change MMR without announcement). New accounts may be capped at 20x for the first 30 days (reported; verify).
- **Liquidation fee (owner-supplied):** 1.25% of notional for BTC/ETH/BNB/XRP, 1.5% for DOGE/SOL. Charged on liquidation unless the position goes bankrupt; at 7x, 1.5% of notional is 10.5% of posted margin. Versioned input, stress-tested at higher values (research cites up to 2.5% unverified).
- **Isolated vs cross:** isolated risks the posted margin plus the clearance fee and the liquidation price stays fixed unless margin is added or funding is debited; cross exposes the whole wallet and moves LP with other PnL. Cross is out of scope.
- **Simulator rules:** per-position margin `N/L`; funding debited from the position margin; liquidation test on mark price before last-price stops; liquidation outcome L recorded separately (current label data: L share <= 0.007%, so cost model v2 is low priority).

## 6. Fees and venues

- Binance USD-M base tier 0.02% maker / 0.05% taker (owner screenshots); 10% discount when paying fees in BNB (0.018% / 0.045%); VIP tiers irrelevant for a personal account.
- USDC-margined perpetuals reportedly charge 0.00% maker / 0.04% taker (Bitsgap, secondary): if confirmed on Binance's own fee page, a maker-entry/taker-exit round trip on BTCUSDC would be about 4 bp plus slippage (about 40% less cost in R); USDC perps have separate order books and funding: test the signal on USDT data, execute on USDC. Verify before modeling.
- OKX (usable from Pakistan) fee tiers, contract specification, margin rules and funding schedule: to be audited in #228; the simulator takes the venue as an input.
- Stops are always taker. The 2 bp stop slippage floor is reasonable for BTC/ETH in normal conditions and too low for DOGE/SOL in cascades (October 2025 event).

## 7. Moving stops in the product (tickets #31, #32)

A setup stores its exit rule (type and parameters, X0-X9), not only fixed numbers; outcome lifecycle events include stop/target modifications (trailing updates, break-even moves, partial exits) as ordered events using the same rules as this engine; a modification takes effect from the next bar.


---
_Generated by [Claude Code](https://claude.ai/code)_
