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
  `blackout(instrument, ts)`; calendar observations in UTC (`fx_calendar_observations`, see News
  blackout below); calendar history is recorded from day one.

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

### News blackout

Code: `python/market_analysis/news_blackout.py` (level 1: scheduled timing only; pure, no clock, no I/O).

- Modes: `off` (no effect), `tag` (keep the entry and mark it), `suppress` (default: drop new entries
  inside the window and log them as "would have fired", each with the events that decided it).
- Window: `[event − before, event + after]`, inclusive. Defaults HIGH ±30 min, MEDIUM ±15 min, LOW 0,
  and only HIGH events count (`min_impact`). All are **UNVERIFIED placeholders**.
- Point in time: an event counts only if it was known at the decision time (`first_seen_ms <= ts`),
  so a backtest never uses a schedule published later.
- Mapping: FX pairs react to base and quote currency events; metals, energy and other indices to the
  quote currency; gold also to `gold`-tagged events and oil to `oil`-tagged events (any currency);
  DXY to its basket currencies (USD, EUR, JPY, GBP, CAD, SEK, CHF; weights not modelled). Crypto is
  never blacked out.
- Storage (#401 part 1): the local operational database, `20261019130000_fx_calendar.sql`.
  `fx_calendar_observations` is append-only and kept forever: one row per distinct
  `(source, source_event_key, event_ts, impact, title)` with `first_seen_at`, the time this store
  first saw it. A rescheduled, re-rated or re-titled event is a **new observation** (new row, new
  `first_seen_at`), never an update, so the schedule can be replayed as it looked at any time.
  `get_fx_calendar_events(from_ms, to_ms, as_of_ms)` (at most 120 days) returns exactly the
  `CalendarEvent` shape, point in time by `first_seen_at <= as_of_ms`; `event_id` is
  `source:key:ts_ms`, so a rescheduled event is its own event with its own `first_seen_ms`, and the
  new time blacks out only from when it was known. A re-rating at the same time gets the suffix
  `:o<observation_id>` (event ids must be unique). The superseded time is not retracted: it stays
  blacked out (conservative). `python/market_analysis/fx/calendar_rows.py` maps the rows
  (`calendar_events_from_rows`, strict: a malformed row raises).
- Fail closed: if the feed silently stopped, the blackout would quietly switch itself off. Every
  fetch is logged in `fx_calendar_fetches` (90 days; an ok fetch records `covered_to`, the end of the
  range its response covers), and `calendar_coverage(status, now, needed_to)` refuses, in this order,
  with `calendar_never_fetched` (no ok fetch), `calendar_stale` (latest ok older than 6 h,
  **UNVERIFIED placeholder**) or `calendar_horizon_short` (its `covered_to` is before the needed
  horizon). A later error does not hide a fresh ok fetch.
- Feed parser (#401 part 2a): `python/market_analysis/fx/calendar_feed.py` (`parse_ff_week`, pure).
  The Forex Factory weekly feed is a JSON list of `title`, `country`, `date` (ISO 8601 with the New
  York offset), `impact` (`Low`, `Medium`, `High`, `Holiday`), `forecast`, `previous`, with no event
  id. Holidays and `All` rows with low impact are skipped and counted; a medium/high `All` row
  raises (a market-wide event is never dropped silently, it needs an owner decision). The key is
  `currency|title|week_sunday|n` (New York Sunday-week, `n` = ordinal by time among same currency
  and title), so a time change within the week is a new observation of the same event, while an
  event moved across a Sunday boundary becomes a new event. Every event must lie in one week, and an
  empty result is a failed fetch. `covered_to` is the end of the feed's own week (next Sunday 00:00
  New York), so the horizon check needs next week's feed too; the live fetcher (#401 part 2b)
  fetches both.
- Freshness end to end (#401 part 2b): the worker-host child `fx-calendar`
  (`src/lib/fx/calendar-fetcher.server.ts`; off unless `FX_CALENDAR_ENABLED=true`) fetches this
  week's and then next week's feed every 2 h, parses them through the analysis service and writes
  observations plus a fetch-log row; `calendar_coverage` then fails closed on the latest ok fetch
  (never fetched, older than 6 h, or `covered_to` short of the needed horizon). Next week is always
  fetched last, because `get_fx_calendar_status` reports the `covered_to` of the LATEST ok fetch,
  not the maximum. The schedule, backoffs, feed addresses and user agent are UNVERIFIED
  placeholders. **The feed's terms of use have not been checked: the owner must read them before
  relying on it.**
- **Not done yet:** interpreting surprises or reactions.

### Hosted forex quotes

The Worker cannot read the local operational database, so forex quotes reach it the same way crypto
quotes do (#323): the worker-host child `fx-quote-publisher` (#433;
`src/lib/fx/fx-quote-publisher.server.ts`, off unless `FX_QUOTE_PUBLISHER_ENABLED=true`) reads the
subscribed instruments, their completed 1m BID bars and the broker's symbol specs every minute and
upserts one row per symbol into the hosted `fx_market_quotes` (`publish_fx_market_quotes`,
forward-only by `last_bar_at`). Only users entitled to forex (`has_market('forex')`) can read the
table; anon gets nothing. A row has the bid (the newest bar's close), the ask = bid +
`spread_points` x the broker's point (computed on integer points), the spread, percent changes over
5/15/60/240/1440 minutes (null when the reference bar is more than 60 minutes older than its target,
e.g. across the weekend) and 24 h of 15-minute bid closes. A symbol without a spec is skipped and
logged once.

The forex Market page (#398) reads them with the user's own client (`getForexQuotes`; RLS decides) and
shows one tile per instrument: live (bar at most 3 minutes old while the market is open), delayed (older;
amber edge), closed (the last price, desaturated) or no data. The 3-minute limit and the 15 s refresh are
UNVERIFIED placeholders.

### Bars and candles

Contract specs (digits, point, tick, contract size, lots, currencies, swaps) live in the operational
table `fx_symbol_spec_observations` (#432; decision 3 of #397 settled: a table, written once per
change by the gateway); `python/market_analysis/fx/specs_rows.py` builds `VenueSymbol` and
`FxContract` from it.

Code: `python/market_analysis/fx/bars.py`, `python/market_analysis/fx/candles.py` (separate from the
crypto `BarSeries`/`CandleSeries`, which assume every minute exists).

- Gateway rows must be UTC (`time_basis = "utc"`; broker server time is converted by the gateway) and
  carry BID OHLC, `spread_points` (× the broker's tick size) and tick volume.
- Each minute is classified with the trading calendar: row + open = present; no row + closed =
  expected (closed); no row + open = **missing** (real data loss); row + closed = kept and counted as
  an anomaly.
- **A closed market is no candle, not a gap.** Weekend, holiday and break buckets are omitted, so the
  next candle follows in trading time and indicators never reset at a weekend (they reset on every
  invalid candle, and an EMA200 needs 402 consecutive valid candles). Only a minute missing while open
  makes its candle invalid.
- Rows printed while the calendar says closed are **quarantined**: kept in the bars and counted, but
  never part of a candle's OHLC or volume, and a bucket with no open minute is never a candle.
- `spread_points` are MT5 points (`10^-digits`), which can differ from the tick size (the minimum
  price change); the spread uses the point.
- Candles that straddle a close or reopen are kept as `partial`; a partial candle ends at its last open
  minute, so a Friday signal is dated at the close, never inside the weekend. Buckets at the series
  edge that the series covers only partly are not built.
- `volume` is the **sum of tick volume** (price updates), not traded volume; `taker_buy_volume` is
  unavailable.
- ASSUMPTION to verify on the Exness demo: buckets are UTC-epoch multiples (240-minute candles open at
  00:00, 04:00, ... UTC), which matches a broker whose MT5 server time is GMT+0. Compare a downloaded
  H4 bar with the 240-minute bucket before comparing with the broker's charts.

### Signals

Code: `python/market_analysis/fx/signals.py` (`generate_fx_signals`; the crypto `forward/signals.py` is
unchanged). The forward baseline TA strategies run on trading-time candles; only strategies whose
declared capabilities fit the instrument run, and the signal identity is the crypto one
(`sha256(strategy_id|version|symbol|signal_ms|side)` with the plain symbol, e.g. `EURUSD`).

A signal is a valid candle close, dated at that candle's real end. Entry checks, in order:
1. the market is open at the signal time (else `market_closed_at_entry`);
2. the entry minute has a row (else `entry_minute_missing`);
3. the whole time-limit window (4 × horizon) lies in ONE continuous open run: no weekend, no daily
   break, so no trade is truncated (else `window_exceeds_session`).

A signal failing a check is still built (its id exists) and kept in `suppressed` with its reason and
session tags: the "would have fired" log; nothing is dropped silently. Session tags are recorded for
every signal. Placebo controls for forex come later (`placebo_not_supported_for_fx`).

### Setups and resolution

Code: `python/market_analysis/fx/setups.py` (geometry `fx-atr-v1`) and `python/market_analysis/fx/resolve.py`.
`forward/setups.py` and `forward/outcomes.py` are unchanged; the separate geometry version keeps
reports from ever mixing forex ATR setups with crypto sigma setups.

- Geometry: stop distance = `k` × Wilder ATR(14) of the signal's own timeframe at the signal candle
  (point-in-time), rounded **up** to whole ticks; target = `rr` × that distance in whole ticks;
  default grid k = 2, rr ∈ {3/2, 2}. Both are measured from `p0` (long `bid_open + spread`, short
  `bid_open`). A stop narrower than 5 spreads (UNVERIFIED placeholder) is refused (`G`).
- Triggers on bid candles + the minute's spread: long stop `bid_low <= stop`, long target
  `bid_high >= target`, short stop `bid_high + spread >= stop`, short target `bid_low + spread <= target`.
  A gap through the stop fills from the worse open; stop and target in one minute is `ambiguous`
  (pessimistic result reported, target result kept as `optimistic`); a minute missing while open is `X`.
- Costs in R (1 R = |entry fill − stop|): the spread is already inside `p0`, so it shows up
  symmetrically as a tighter stop and a harder target; slippage and round-trip commission are
  `cost_ur`; swap at each rollover (×3 on the triple day) is `fund_ur`.
- Time limit: 4 × horizon in open minutes, and the whole window must lie in one open session, so no
  trade is truncated by a weekend or break; a closed minute inside a window is an error.
- Time-limit exit `E` is stamped with the OPEN time of the last window minute but fills at that
  minute's CLOSE; rollover swap is counted for rollovers in `(entry_ms, exit_ms]`, so a rollover at
  exactly the end of the window is not charged. This is a deliberate v1 convention, to be revisited
  with real broker swap statements.
- Outcome `X` (a minute missing while open) has no exit fill, so the wallet cannot close it yet; the
  policy is an open owner decision tracked in the forex runner ticket.
- **Open research item:** port the crypto robust seasonal sigma to FX (session seasonality, weekend
  handling) and compare it with the ATR geometry.

### Paper wallet

Code: `python/market_analysis/fx/wallet.py` (`fx-wallet-v1`; the crypto wallets are unchanged). Same
`step(state, events, config) -> (state, ledger)` shape and ledger line fields as the crypto wallet,
so the existing `paper_ledger` table can store it later.

- Size in base units from the broker's contract size (lots = units / contract size), floored to the
  lot step; risk per trade = `risk_fraction` × balance over the setup's risk distance, converted to
  the account currency. Below the minimum lot → `rejected` (`below_min_lot`).
- Margin = notional / leverage in the **account** currency (the quote currency is converted with the
  event's `quote_to_account`); leverage = the highest whose stop-out stays two stop distances away,
  capped at the account's maximum.
- Commission per lot and side (`open_fee`, `close_fee`); the spread is already inside the fills.
- Swap at each rollover is a `funding` line with `kind: "swap"` (×3 on the triple day).
- Stop-out is **isolated per position** at the stored stop-out price, never losing more than that
  position's margin. The real account is cross-margin, so this is conservative.
- Not done yet: mark-to-market equity, portfolio risk caps beyond total exposure, and wiring into
  any runner or the database.

### Database

Migration: `supabase/migrations/20261018100000_forex_access_and_market_tables.sql`; test
`tests/forex-market-db.test.mjs`. The backend always computes and stores both markets; who may see
forex is an access rule.

- **Shared per-market facts.** `fx_market_stages`, `fx_market_signals`, `fx_market_suppressed`,
  `fx_market_setups`, `fx_market_outcomes`, `fx_market_events` are append-only, shared by all
  entitled users, and independent of the crypto `forward_market_*` tables. `commit_fx_stage` has its
  own advisory lock (`forward-market-fx`) and its own stage cursor, so a forex stage and a crypto
  stage for the same hour never collide or block each other. Forex symbols may be 3-16 characters
  (DXY); setups carry `geometry_version` and the ATR fields (`p0`, `d_ticks`, spread) in `payload`.
- **Per-user accounts per market.** `forward_wallet_accounts` is keyed by `(user_id, market)`; both
  accounts of one user run at the same time. The crypto account id is unchanged; the forex account
  id is `<user uuid>:forex`. `ensure_fx_account` and `commit_fx_user_run` use the lock key
  `forward-wallet:<uuid>:forex`, so one user's two markets never block each other. Forex runs are
  `paper_runs` rows with `market='forex'`, `fx_stage_run_id` set, `stage_run_id` NULL and run key
  `fx-hour:<ms>`. `paper_account_summary(p_market)` and `paper_equity_series(..., p_market)` default
  to `crypto` and never add the two markets together.
- **Access labels (#411).** Nobody has access by default. Each user has at most one `user_access`
  row with one label: `crypto`, `forex` or `all` (both), plus an optional `expires_at`; no row or an
  expired row means no markets. `plans`/`plan_markets` map the label to markets. There are no roles.
  Users can read their own row and never write it. `my_markets()` and `has_market(market)` are for
  the app; the fx tables' RLS uses `has_market('forex')`. **Limit:** crypto data is gated in the app
  only (the gate screen), not by database policy; forex is gated by both. Commands (service-role env
  from the shell or `.env.production.local`):
  - grant or change: `npm run access:grant -- <email> --label crypto|forex|all [--expires 2027-01-01]`
    (omitting `--expires` clears it);
  - revoke: `npm run access:grant -- <email> --revoke`;
  - list: `npm run access:grant -- --list` (email, label, expiry; users without a row are not listed).
- **Not done yet:** forex report RPCs (outcome/paired/non-trade reports), runner wiring, the
  operational candle tables, and a calendar table.

### Dashboard toggle

Code: `src/lib/market/market-select.ts` (pure rules), `src/lib/market/market-context.tsx`
(`MarketProvider`, `useMarket()`), `src/components/market-toggle.tsx`, `src/components/forex-landing.tsx`.

- **UI-only.** The backend always computes and stores both markets; the switch only chooses which
  screens are shown. The Forex identity is a teal primary and nebula (`:root[data-market="forex"]`,
  set on `<html>` and on the shell) on the same surfaces, plus a 2 px market line under the header
  and a small "Forex" chip beside the logo.
- **Entitlement and gate.** The provider reads `my_markets()` (react-query key `my-markets`, 5 min).
  While it loads, the signed-in app shows only a pulsing orb (no page content or queries mount). With
  no access the gate shows "Access pending" (the two markets as locked chips, "Check again" and
  "Sign out"); if the check fails it shows "Can't check access" with "Try again". A user with one
  label starts on that market; a stored choice that is not openable snaps to the first open market.
- **Locked state.** A market the label does not include stays visible in the toggle but dimmed, with
  a lock and a dashed outline. Activating it opens a short note ("Forex isn't switched on for you",
  "Ask the owner to add it.") and never switches. No price, purchase button or link.
- **Stable header (#413, #415).** One row on every width with a fixed height, and nothing in it
  depends on the market: the logo (always the brand purple, `text-brand`; wordmark from 1024 px), a
  fixed-width text-only toggle (120 px phone, 144 px desktop), the desktop nav (labels for every item
  from 1280 px, only the active item below that), the runner status (an 8 px dot on a phone; from
  768 px the Online/Offline pill, amber when degraded) and sign-out. On a phone the navigation is a
  bottom tab bar. Forex pages use the same eyebrows and titles as the crypto pages and say nothing
  else about the mode; the teal theme carries it, and the market card's "Data feed · Not connected"
  chip is the only place that says the feed is missing.
- **Storage.** The choice is kept in `localStorage` under `cw.market` (wrapped in try/catch) and
  shared across tabs through the `storage` event.
- **What Forex shows today.** Market, Strategy Lab, Paper account and Alerts show a Forex landing
  screen. Its real parts are the open/closed state with the next open or close time, and the four
  sessions in the viewer's time zone, both from the trading calendar mirror. It also shows tiles for
  the default instruments with "No data yet" and no numbers, and a footnote that nothing here is a
  price or a signal. Notes and Control room are market-neutral. Every forex price, chart and account
  screen arrives with its data feed and runner.

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
