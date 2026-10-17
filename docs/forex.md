# Forex (second market, #374)

## Goal

Add forex next to crypto in the same repo and product. The backend always computes both
markets; the dashboard Crypto/Forex toggle is UI only. Strategies, indicators and charts are
shared where the data allows, and every reuse goes through a compatibility check that fails
closed.

## Decisions

- Markets: `crypto`, `forex`.
- Instrument kinds: crypto `perpetual`; forex `spot_cfd` (MT5-style CFD) and `future`
  (dated/rolling, PMEX-style).
- Default forex instruments: XAUUSD, EURUSD, USDJPY, USOIL (WTI). Optional extras: DXY (US
  Dollar Index), UKOIL (Brent). Users can add any symbol later.
- Primary feeds: Exness MT5 demo (Standard) for CFDs, PMEX MT5 demo for futures. Both run
  behind a separate gateway service; the app never imports MT5 directly.
- Fallback chain, in order:
  1. another broker's MT5 demo (IC Markets `ICMarketsSC-Demo`, XM `XMGlobal-Demo`);
  2. Kraken FX spot / Capital.com demo API;
  3. Binance XAUUSDT/XAGUSDT, as a flagged gold proxy only;
  4. slow REST cross-checks (Twelve Data, Yahoo, gold-api.com).
- Backtesting history: Dukascopy.
- A fallback never silently changes a signal: provenance is recorded and a source change is
  surfaced.
- Entitlements by role/plan per market. Separate paper accounts per market, running
  simultaneously.
- Demo accounts expire (Exness MT5 is reported at 21/90/180 days depending on the source):
  credentials must be replaceable, a login failure must alert, and spare accounts are kept.
  Real-trading automation is a possible final stage and would need a real account; the gateway
  keeps credentials swappable. Islamic accounts are out of scope.

## Instrument model

Code: `python/market_analysis/instruments.py` and `src/lib/market/instruments.ts` (same names,
same rules, same reason strings).

- `Instrument`: `instrument_id`, `market`, `kind`, `asset_class` (`crypto`, `fx`, `metal`,
  `energy`, `index`), `base`, `quote`, `display`.
- Identity is venue-independent:
  - crypto keeps its existing id `binance-usdm:SYMBOL`, so nothing downstream changes;
  - forex CFD: `fx:SYMBOL` (`fx:EURUSD`, `fx:XAUUSD`, `fx:USOIL`);
  - forex future: `fut:SYMBOL`.
- `VenueSymbol` maps a venue's native name to an instrument (`exness-mt5-demo`,
  `pmex-mt5-demo`, `binance-usdm`, ...). Digits, tick size, contract size and expiry are filled
  from the broker's symbol info on the demo account, never guessed.
- Market/kind rules: crypto allows only `perpetual`; forex allows `spot_cfd` or `future`.

## Strategy compatibility

### Declared strategies

Code: `python/market_analysis/forward/strategy_capabilities.py`.

| Strategy | Markets | Requires | Why |
|---|---|---|---|
| `ema_cross_20_50`, `ema_cross_50_200`, `rsi_14_reversion`, `macd_12_26_9`, `bollinger_20_2_reversion`, `keltner_breakout_20_14_2` | crypto, forex | OHLC | read only close/high/low and the candle count |
| `doji_continuation_v1`, `doji_reversal_v1` | crypto | OHLC, real volume | paused, calibrated on crypto, marked unvalidated; relative volume gates every reading |

- Placebos inherit the matched strategy's capabilities (random emission, same data needs).
- Fail closed: an unknown strategy id has no capabilities and is refused (`no_capabilities_declared`).
- Adding a strategy to `ta_strategies.STRATEGIES` requires adding its capabilities; the module fails at
  import otherwise.

### General rules

- Data fields: `ohlc`, `volume_real`, `volume_tick`, `mark`, `funding`, `taker_flow`,
  `open_interest`, `spread`.
- Provided per instrument:
  - crypto perpetual: ohlc, volume_real, mark, funding, taker_flow, open_interest;
  - forex `spot_cfd`: ohlc, volume_tick, spread;
  - forex `future`: ohlc, volume_tick, spread, volume_real.
- A strategy declares `StrategyCapabilities`: supported markets, required fields, "any of"
  groups (e.g. `{volume_real, volume_tick}` for a strategy fine with any volume), and whether it
  is session-aware.
- `check_compatibility` fails closed. Reasons, in fixed order: `no_capabilities_declared`;
  `market_not_supported:<market>`; `missing_field:<field>` (sorted);
  `missing_any_of:<a>|<b>` (members sorted).
- Volume: forex CFDs have tick volume only (no central tape). Strategies that need real traded
  volume are incompatible with forex CFDs.

## Costs

Code: `python/market_analysis/fx/costs.py` (exact `int`/`Fraction` maths; the Binance-perpetual
`CostModel` in `benchmark/costs.py` is unchanged).

- MT5 candles are **bid** candles and carry a `spread`; `ask = bid + spread`. A long buys at the ask
  and sells at the bid; a short sells at the bid and buys at the ask.
- Triggers on bid candles: long stop `bid_low <= stop`, long target `bid_high >= target`, short stop
  `bid_high + spread >= stop`, short target `bid_low + spread <= target`.
- Entry reference `p0` is the executable price before slippage (long `bid_open + spread`, short
  `bid_open`); stop and target are measured from `p0`. A stop is exactly −1 R and a target +rr R
  before slippage, commission and swap; the spread appears **symmetrically** as a tighter stop and a
  harder target, never as a hidden P&L term.
- Slippage = `cost_multiplier × max(fraction × spread, fraction × bar range)`; a gap through a stop
  fills from the worse open. Commission is per lot and side (0 on a Standard account). Swap is
  charged at each rollover from the trading calendar (×3 on the triple day) instead of funding; a
  margin stop-out replaces liquidation.
- **Placeholders (`ASSUMPTIONS_FX_V1`):** every `FxCostModel` default (slippage fractions,
  commission 0, swap multiplier, leverage cap 100, stop-out level 50%, buffer 2) is UNVERIFIED.
  Commission, swap rates, leverage and stop-out level are read from the demo account before any
  result is trusted.

## Data policy

- Costs differ per kind: crypto perpetual (funding + taker/maker), FX CFD (spread +
  swap/rollover + optional commission), futures (commission + roll). The existing `CostModel` in
  `benchmark/costs.py` is Binance-perpetual specific and is not renamed; the market-agnostic
  abstraction gets a different name in a later PR.
- Trading week: Sunday 17:00 to Friday 17:00 America/New_York (21:00 or 22:00 UTC depending on
  US DST). Computed 2026 week lengths: normal 48 h; US fall-back week 49 h (close Fri 30 Oct
  21:00 UTC, open Sun 1 Nov 22:00 UTC); spring-forward week 47 h. EU DST ends 25 Oct 2026, US
  DST 1 Nov 2026.
- Forex candle anchors differ from crypto's UTC anchors; this belongs to the calendar PR.
- News blackout filter (v1 feature): modes `off` / `tag` / `suppress`. Default suppresses new
  entries inside the window and logs "would have fired" signals. Pure function
  `blackout(instrument, ts)`; `calendar_event` table in UTC; calendar history is recorded from
  day one.

### Trading calendar

`python/market_analysis/trading_calendar.py` (UI mirror: `src/lib/market/trading-calendar.ts`)
answers, as pure functions over UTC epoch ms: is the market open, until when, when it next opens,
how many open minutes a range has, when rollover is charged, and which conventional sessions are
active. Crypto uses `AlwaysOpenCalendar`; forex CFDs use `FxWeekCalendar` (Sunday 17:00 to Friday
17:00 New York); forex futures fail closed until their sessions are known.

- 2026 vectors: normal week closes Fri 16 Oct 21:00Z and reopens Sun 18 Oct 21:00Z (48 h); US
  fall-back closes Fri 30 Oct 21:00Z, reopens Sun 1 Nov 22:00Z (49 h); US spring-forward closes
  Fri 6 Mar 22:00Z, reopens Sun 8 Mar 21:00Z (47 h). Every week window is 120 h (7200 minutes).
- Rollover: 17:00 New York, Monday to Friday, never Sunday. Wednesday is charged three days by
  default (Mon 1, Tue 1, Wed 3, Thu 1, Fri 1 = 7). A position entered exactly at the rollover
  instant is not charged for it; one exiting exactly at it is.
- Sessions (Sydney, Tokyo, London, New York) use conventional local hours, not exchange-official
  ones. The London/New York overlap is 4 h, or 5 h in the week when only the EU has left DST.
- Daily breaks and holidays for metals and oil CFDs are **not guessed**: they come from the
  broker's symbol sessions and are passed in as `daily_breaks` / `closures`.

## Roadmap of PRs

1. Foundation: instrument vocabulary and compatibility check (done).
2. Trading calendar (pure, DST-tested) (this PR adds it).
3. Strategy capability declarations for the existing baseline strategies + cost-model protocol.
4. `market` column migrations (additive).
5. Bars adapter decoupling.
6. Gateway + forex collector.
7. Worker-host forex children.
8. Dashboard toggle + entitlements.
9. Blackout filter.

Items 3-9 need further codebase reading before their prompts are written.

## Open items

- DXY and Brent: confirm whether they join the defaults.
- USOIL roll behaviour on the Exness demo.
- Exness symbol suffix on the demo (Standard accounts are reported to use an `m` suffix,
  unverified).
- Calendar source: FMP vs ForexFactory feed.
- Free-user forex access.
- Host sizing.
- CLAUDE.md §3 (paper trading parked) and §8 (always-on host not needed) are in tension with
  this plan; owner decision pending.
