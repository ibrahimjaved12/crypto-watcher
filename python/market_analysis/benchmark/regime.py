"""Objective regime/season table and pre-registered calendar tests (#223; SPEC-2 sections 4-5).

DESCRIPTIVE ONLY: nothing here is a strategy, counts as a PASS or enters the experiment ledger.

Regime table ``regime-v1``: one row per UTC day t and symbol, computed at 00:00 UTC of day t
from closes through day t-1 (point in time, no smoothing, frozen thresholds). Market labels
come from BTCUSDT daily closes and BTCUSDT funding; per-symbol labels from each symbol's own
closes with the same rules. Undefined values (warm-up, gaps) are empty, never filled.

Calendar hypotheses (exactly four, development + validation days 2024-01..2025-12 only):
H1 October vs other months, H2 months 6-18 after a halving vs others, H3 long 21:00-24:00 UTC
vs the other 3-hour windows (gross, with taker and maker round trips printed), H4 weekend vs
weekday. Inference: calendar-month block bootstrap. Never a signal on its own.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from fractions import Fraction
from math import ceil, comb, isfinite, log, sqrt
from statistics import NormalDist

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .. import data_lake
from ..daily_lake import DAY_MS, DailySeries, load_symbol_daily, load_symbol_funding_range
from .bars import MISSING, BarSeries
from .canonical import content_hash
from .costs import COST_MODEL_V1
from .evaluate import decimal_text, nearest_rank
from .funding import FundingSeries
from .rng import u64_words
from .segments import segment_bounds_ms
from .trend import log_returns, percentile_rank, prices, rolling_mean, rolling_std, shift1

SCHEMA = "regime-v1"
FIRST_MONTH = "2020-01"
LAST_MONTH = "2025-12"  # the hidden stretch is never read; the guard refuses later months without a token
MARKET_SYMBOL = "BTCUSDT"
SMA_SHORT, SMA_LONG = 50, 200
DD_BULL, DD_BEAR = -0.20, -0.50
MM_OVERHEATED, MM_CAPITULATION = 2.4, 0.8
RV_WINDOW, RV_PCT_WINDOW = 30, 365
RV_CALM, RV_STRESSED = 30.0, 70.0
FR_WINDOW_DAYS = 30
FR_CROWDED_LONG = Fraction(1, 10_000)  # per 8h
FR_CROWDED_SHORT = Fraction(0)
HALVINGS = (date(2012, 11, 28), date(2016, 7, 9), date(2020, 5, 11), date(2024, 4, 20))
HALVING_BINS = ((0, 6, "0-6"), (6, 18, "6-18"), (18, 30, "18-30"), (30, 48, "30-48"), (48, None, "48+"))
HYSTERESIS_DAYS = 3
ER_WINDOW = 20
VR_Q, VR_WINDOW = 5, 180
ANNUALIZATION = 365
MARKET_COLUMNS = ("mkt_t1", "mkt_t2", "mkt_dd", "mkt_dd_state", "mkt_mm", "mkt_mm_flag", "mkt_rv30", "mkt_rv_pct",
                  "mkt_rv_state", "mkt_fr30", "mkt_fr_state", "halving_months", "halving_bin", "mkt_season",
                  "mkt_season_h")
SYMBOL_COLUMNS = ("sym_t1", "sym_t2", "sym_dd", "sym_dd_state", "sym_season", "sym_er20", "sym_vr5", "sym_hurst")
COLUMNS = ("date", "symbol", *MARKET_COLUMNS, *SYMBOL_COLUMNS)
MARKET_STATE_LABELS = ("mkt_t1", "mkt_t2", "mkt_dd_state", "mkt_mm_flag", "mkt_rv_state", "mkt_fr_state",
                       "halving_bin", "mkt_season", "mkt_season_h")
SYMBOL_STATE_LABELS = ("sym_t1", "sym_t2", "sym_dd_state", "sym_season")
# Calendar tests
CALENDAR_FIRST_MS = segment_bounds_ms("development")[0]
CALENDAR_END_MS = segment_bounds_ms("validation")[1]
CALENDAR_B = 2000
CALENDAR_SEED = 20261009
REQUIRED_T = 3.3
WINDOW_HOURS = 3
H3_START_HOUR = 21
TAKER_ROUND_TRIP = 2 * (COST_MODEL_V1.taker_rate + COST_MODEL_V1.market_slip_floor_bps / 10_000)  # 12 bp
MAKER_ROUND_TRIP = 2 * COST_MODEL_V1.maker_rate  # 4 bp
_NAN = float("nan")
_HOUR_MS = 3_600_000


# ---------------------------------------------------------------- label primitives (index d = at close d)


def _sign_label(condition, defined) -> np.ndarray:
    return np.where(defined, np.where(condition, 1.0, -1.0), _NAN)


def close_features(close) -> dict:
    """Values at each close d from closes through d (callers shift by one day for day t)."""
    sma_short, sma_long = rolling_mean(close, SMA_SHORT), rolling_mean(close, SMA_LONG)
    defined_long = ~np.isnan(sma_long) & ~np.isnan(close)
    running_max = np.fmax.accumulate(close)  # NaN gaps are skipped; leading NaN stays NaN
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = close / running_max - 1.0
        mm = close / sma_long
    rv30 = rolling_std(log_returns(close), RV_WINDOW) * sqrt(ANNUALIZATION)
    return {"t1": _sign_label(close > sma_long, defined_long),
            "t2": _sign_label(sma_short > sma_long, ~np.isnan(sma_short) & ~np.isnan(sma_long)),
            "dd": np.where(np.isnan(close), _NAN, dd), "mm": np.where(defined_long, mm, _NAN),
            "rv30": rv30, "rv_pct": percentile_rank(rv30, RV_PCT_WINDOW)}


def efficiency_ratio(close, window: int = ER_WINDOW) -> np.ndarray:
    """|close[d] - close[d-n]| / sum of |daily changes| over the same n days (NaN when flat or incomplete)."""
    change = np.full(len(close), _NAN)
    change[1:] = np.abs(np.diff(close))
    path = rolling_mean(change, window) * window
    net = np.full(len(close), _NAN)
    net[window:] = np.abs(close[window:] - close[:-window])
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(path > 0, net / path, _NAN)


def variance_ratio(close, q: int = VR_Q, window: int = VR_WINDOW) -> np.ndarray:
    """Var(r_q) / (q Var(r_1)) over the trailing ``window`` daily log returns (overlapping q-day sums, ddof 1)."""
    returns = log_returns(close)
    out = np.full(len(close), _NAN)
    if len(returns) < window:
        return out
    windows = sliding_window_view(returns, window)
    sums = np.cumsum(windows, axis=1)
    multi = sums[:, q - 1:] - np.concatenate([np.zeros((len(windows), 1)), sums[:, :-q]], axis=1)
    one = windows.var(axis=1, ddof=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = multi.var(axis=1, ddof=1) / (q * one)
    out[window - 1:] = np.where(np.isnan(windows).any(axis=1) | ~(one > 0), _NAN, ratio)
    return out


def hurst_from_vr(vr) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(np.asarray(vr) > 0, 0.5 + np.log(vr) / (2 * log(VR_Q)), _NAN)


def dd_state(value: float) -> str:
    if value != value:
        return ""
    return "bull" if value > DD_BULL else "bear" if value < DD_BEAR else "neutral"


def mm_flag(value: float) -> str:
    if value != value:
        return ""
    return "overheated" if value > MM_OVERHEATED else "capitulation" if value < MM_CAPITULATION else "normal"


def rv_state(value: float) -> str:
    if value != value:
        return ""
    return "calm" if value < RV_CALM else "stressed" if value > RV_STRESSED else "normal"


def fr_state(value) -> str:
    if value is None:
        return ""
    return "crowded_long" if value > FR_CROWDED_LONG else "crowded_short" if value < FR_CROWDED_SHORT else "neutral"


def season(t1: float, t2: float, dd: float) -> str:
    """Bull: t1 = t2 = +1 and dd > -20%; Bear: t1 = t2 = -1; else Transition; empty if any input undefined."""
    if t1 != t1 or t2 != t2 or dd != dd:
        return ""
    if t1 > 0 and t2 > 0 and dd > DD_BULL:
        return "Bull"
    if t1 < 0 and t2 < 0:
        return "Bear"
    return "Transition"


def hysteresis(labels, days: int = HYSTERESIS_DAYS) -> list[str]:
    """The held label changes only after a new candidate label has held ``days`` consecutive days.

    The first defined day starts the held label; an empty label empties the output and restarts it.
    """
    out, held, candidate, run = [], "", "", 0
    for label in labels:
        if not label:
            held, candidate, run = "", "", 0
            out.append("")
            continue
        if not held:
            held = label
        elif label == held:
            candidate, run = "", 0
        else:
            run = run + 1 if label == candidate else 1
            candidate = label
            if run >= days:
                held, candidate, run = label, "", 0
        out.append(held)
    return out


def halving_months(day: date) -> int | None:
    """Whole months since the latest halving on or before ``day`` (None before the first)."""
    latest = None
    for halving in HALVINGS:
        if halving <= day:
            latest = halving
    if latest is None:
        return None
    return (day.year - latest.year) * 12 + day.month - latest.month - (1 if day.day < latest.day else 0)


def halving_bin(months: int | None) -> str:
    if months is None:
        return ""
    for low, high, label in HALVING_BINS:
        if months >= low and (high is None or months < high):
            return label
    return ""


def funding_means(funding: FundingSeries, day_ms) -> list:
    """Mean BTCUSDT funding rate per 8h (rate * 8 / interval_hours) over settlements in [t - 30 days, t).

    Exact Fractions; None when the window is not inside the covered range or holds no settlement.
    """
    normalized = [rate * 8 / hours for rate, hours in zip(funding.rate, funding.interval_hours)]
    prefix = [Fraction(0)]
    for value in normalized:
        prefix.append(prefix[-1] + value)
    out = []
    for ms in day_ms:
        start = ms - FR_WINDOW_DAYS * DAY_MS
        if start < funding.start_ms or ms > funding.end_ms:
            out.append(None)
            continue
        low, high = funding.index_at_or_after(start), funding.index_at_or_after(ms)
        out.append((prefix[high] - prefix[low]) / (high - low) if high > low else None)
    return out


# ---------------------------------------------------------------- table


def _day_text(ms: int) -> str:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _number(value: float, places: int = 6) -> str:
    return f"{value:.{places}f}" if value == value and isfinite(value) else ""


def _sign_text(value: float) -> str:
    return "" if value != value else ("+1" if value > 0 else "-1")


def _on_grid(values, series: DailySeries, first_ms: int, days: int) -> np.ndarray:
    """Close-indexed values shifted to day t (closes through t-1) and placed on the table's day grid."""
    shifted = shift1(values)
    index = (first_ms - series.start_ms) // DAY_MS + np.arange(days)
    inside = (index >= 0) & (index < series.days)
    return np.where(inside, shifted[np.clip(index, 0, max(series.days - 1, 0))] if series.days else _NAN, _NAN)


def market_labels(btc: DailySeries, funding: FundingSeries, first_ms: int, end_ms: int) -> dict:
    days = (end_ms - first_ms) // DAY_MS
    day_ms = [first_ms + j * DAY_MS for j in range(days)]
    features = {name: _on_grid(values, btc, first_ms, days) for name, values in close_features(prices(btc.close)).items()}
    fr30 = funding_means(funding, day_ms)
    halving = [halving_months(datetime.fromtimestamp(ms // 1000, tz=timezone.utc).date()) for ms in day_ms]
    seasons = [season(features["t1"][j], features["t2"][j], features["dd"][j]) for j in range(days)]
    return {"mkt_t1": [_sign_text(v) for v in features["t1"]], "mkt_t2": [_sign_text(v) for v in features["t2"]],
            "mkt_dd": [_number(v) for v in features["dd"]], "mkt_dd_state": [dd_state(v) for v in features["dd"]],
            "mkt_mm": [_number(v) for v in features["mm"]], "mkt_mm_flag": [mm_flag(v) for v in features["mm"]],
            "mkt_rv30": [_number(v) for v in features["rv30"]],
            "mkt_rv_pct": [_number(v, 4) for v in features["rv_pct"]],
            "mkt_rv_state": [rv_state(v) for v in features["rv_pct"]],
            "mkt_fr30": ["" if v is None else decimal_text(v) for v in fr30],
            "mkt_fr_state": [fr_state(v) for v in fr30],
            "halving_months": ["" if v is None else str(v) for v in halving],
            "halving_bin": [halving_bin(v) for v in halving], "mkt_season": seasons,
            "mkt_season_h": hysteresis(seasons)}


def symbol_labels(series: DailySeries, first_ms: int, end_ms: int) -> dict:
    days = (end_ms - first_ms) // DAY_MS
    close = prices(series.close)
    features = {name: _on_grid(values, series, first_ms, days) for name, values in close_features(close).items()}
    vr5 = variance_ratio(close)
    er20, vr, hurst = (_on_grid(values, series, first_ms, days)
                       for values in (efficiency_ratio(close), vr5, hurst_from_vr(vr5)))
    return {"sym_t1": [_sign_text(v) for v in features["t1"]], "sym_t2": [_sign_text(v) for v in features["t2"]],
            "sym_dd": [_number(v) for v in features["dd"]], "sym_dd_state": [dd_state(v) for v in features["dd"]],
            "sym_season": [season(features["t1"][j], features["t2"][j], features["dd"][j]) for j in range(days)],
            "sym_er20": [_number(v) for v in er20], "sym_vr5": [_number(v) for v in vr],
            "sym_hurst": [_number(v) for v in hurst]}


def build_table(daily: dict, btc_funding: FundingSeries, first_ms: int, end_ms: int) -> tuple[list, dict]:
    """(rows in date then symbol order, label columns by name) for the symbols of ``daily``."""
    if MARKET_SYMBOL not in daily:
        raise ValueError("the market labels need BTCUSDT")
    days = (end_ms - first_ms) // DAY_MS
    market = market_labels(daily[MARKET_SYMBOL], btc_funding, first_ms, end_ms)
    per_symbol = {symbol: symbol_labels(daily[symbol], first_ms, end_ms)
                  for symbol in data_lake.SYMBOLS if symbol in daily}
    rows = []
    for j in range(days):
        stamp = _day_text(first_ms + j * DAY_MS)
        prefix = [market[name][j] for name in MARKET_COLUMNS]
        for symbol, labels in per_symbol.items():
            rows.append([stamp, symbol, *prefix, *(labels[name][j] for name in SYMBOL_COLUMNS)])
    return rows, {"market": market, "symbols": per_symbol}


def write_table(fileobj, rows) -> int:
    """Deterministic csv.gz (data_lake.write_csv_gz: mtime 0, empty name, fixed level)."""
    return data_lake.write_csv_gz(fileobj, [",".join(COLUMNS) + "\n", *(",".join(row) + "\n" for row in rows)])


def label_statistics(labels) -> dict:
    """Days per state, runs, mean duration (defined days / runs; edge runs included) and flips per year."""
    states, runs, flips, pairs, previous = {}, 0, 0, 0, ""
    for label in labels:
        if label:
            states[label] = states.get(label, 0) + 1
            if previous:
                pairs += 1
                flips += label != previous
            runs += label != previous
        previous = label
    defined = sum(states.values())
    return {"defined_days": defined, "states": dict(sorted(states.items())), "runs": runs, "flips": flips,
            "mean_duration_days": defined / runs if runs else None,
            "flip_rate_per_year": flips / pairs * ANNUALIZATION if pairs else None}


def diagnostics(columns: dict, first_ms: int, end_ms: int) -> dict:
    """Label statistics over all table days and over development + validation days (2024-01..2025-12)."""
    days = (end_ms - first_ms) // DAY_MS
    low = max(0, (CALENDAR_FIRST_MS - first_ms) // DAY_MS)
    high = min(days, (CALENDAR_END_MS - first_ms) // DAY_MS)
    out = {}
    for scope, (a, b) in (("all", (0, days)), ("development_validation", (low, high))):
        out[scope] = {"market": {name: label_statistics(columns["market"][name][a:b])
                                 for name in MARKET_STATE_LABELS},
                      "symbols": {symbol: {name: label_statistics(labels[name][a:b]) for name in SYMBOL_STATE_LABELS}
                                  for symbol, labels in columns["symbols"].items()}}
    return out


# ---------------------------------------------------------------- calendar inference


def month_block_bootstrap(months, values, in_group, *, B: int = CALENDAR_B, seed: int = CALENDAR_SEED,
                          stream: str) -> dict:
    """Mean(group) - mean(rest) with a calendar-month block bootstrap (whole months resampled, rng.u64_words).

    Replicates whose resample lacks either group are undefined and counted. Interval: nearest-rank
    2.5 % / 97.5 % of the defined replicates; t = difference / std of the defined replicates.
    """
    keys = sorted(set(months))
    position = {key: i for i, key in enumerate(keys)}
    M = len(keys)
    sums = np.zeros((4, M))
    for month, value, flag in zip(months, values, in_group):
        if value != value:
            continue
        row = 0 if flag else 2
        sums[row, position[month]] += value
        sums[row + 1, position[month]] += 1
    n_a, n_b = int(sums[1].sum()), int(sums[3].sum())
    difference = sums[0].sum() / n_a - sums[2].sum() / n_b if n_a and n_b else None
    replicates = []
    if M and difference is not None:
        draws = (u64_words(seed, stream, 0, B * M) % np.uint64(M)).astype(np.int64).reshape(B, M)
        weights = np.zeros((B, M))
        np.add.at(weights, (np.repeat(np.arange(B), M), draws.ravel()), 1.0)
        totals = [(weights * row[None, :]).sum(axis=1) for row in sums]
        defined = (totals[1] > 0) & (totals[3] > 0)
        with np.errstate(invalid="ignore", divide="ignore"):
            stats = totals[0] / totals[1] - totals[2] / totals[3]
        replicates = [float(value) for value in stats[defined]]
    se = float(np.std(replicates)) if len(replicates) > 1 else None
    t = difference / se if difference is not None and se else None
    group_months = len({month for month, value, flag in zip(months, values, in_group) if flag and value == value})
    return {"n_months": M, "n_group": n_a, "n_rest": n_b, "group_months": group_months,
            "difference": difference, "interval95": [nearest_rank(replicates, Fraction(1, 40)),
                                                     nearest_rank(replicates, Fraction(39, 40))],
            "bootstrap_se": se, "t": t, "B": B, "replicates_defined": len(replicates), "stream": stream}


def cannot_pass_line(n_months: int, group_months: int | None, difference, se, t) -> str:
    """The pre-registered caveat computed from the number of months (never a signal on its own)."""
    p = 2 * (1 - NormalDist().cdf(REQUIRED_T))
    needed = ceil(1 / p)
    if group_months is not None and 0 < group_months < n_months and comb(n_months, group_months) < needed:
        return (f"cannot pass t >= {REQUIRED_T} with this n: {n_months} months with {group_months} in the tested "
                f"group allow only C({n_months},{group_months}) = {comb(n_months, group_months)} month relabellings, "
                f"a two-sided p of {p:.5f} needs at least {needed}")
    if t is None or abs(t) < REQUIRED_T:
        need = "undefined" if se is None else f"{REQUIRED_T * se:.6f}"
        return (f"cannot pass t >= {REQUIRED_T} with this n: {n_months} months, |difference| would need >= {need} "
                f"({REQUIRED_T} bootstrap standard errors)")
    return f"t >= {REQUIRED_T} with {n_months} months, but descriptive only: never a signal on its own"


def _calendar_days(series: DailySeries) -> tuple[list[int], np.ndarray]:
    """(day open times, daily log returns close[d-1] -> close[d]) of the calendar window."""
    close = prices(series.close)
    returns = log_returns(close)
    days = (CALENDAR_END_MS - CALENDAR_FIRST_MS) // DAY_MS
    stamps = [CALENDAR_FIRST_MS + j * DAY_MS for j in range(days)]
    index = (CALENDAR_FIRST_MS - series.start_ms) // DAY_MS + np.arange(days)
    inside = (index >= 0) & (index < series.days)
    values = np.where(inside, returns[np.clip(index, 0, max(series.days - 1, 0))], _NAN)
    return stamps, values


def _month(ms: int) -> str:
    return _day_text(ms)[:7]


def _result(name: str, description: str, stats: dict, *, use_group_months: bool, extra: dict | None = None) -> dict:
    line = cannot_pass_line(stats["n_months"], stats["group_months"] if use_group_months else None,
                            stats["difference"], stats["bootstrap_se"], stats["t"])
    return {"hypothesis": name, "description": description, **stats, **(extra or {}), "caveat": line}


def h1_october(btc: DailySeries, B: int = CALENDAR_B, seed: int = CALENDAR_SEED) -> dict:
    """Monthly log return (last close of the month vs last close of the previous month), October vs others."""
    stamps, daily = _calendar_days(btc)
    months, values = [], []
    for month in sorted({_month(ms) for ms in stamps}):
        picked = [value for ms, value in zip(stamps, daily) if _month(ms) == month]
        months.append(month)
        values.append(float(np.sum(picked)) if picked and not np.isnan(picked).any() else _NAN)
    stats = month_block_bootstrap(months, values, [month.endswith("-10") for month in months], B=B, seed=seed,
                                  stream="regime-h1")
    return _result("H1", "October vs other months: mean monthly log return difference", stats,
                   use_group_months=True)


def h2_halving(btc: DailySeries, B: int = CALENDAR_B, seed: int = CALENDAR_SEED) -> dict:
    stamps, daily = _calendar_days(btc)
    group = []
    for ms in stamps:
        months = halving_months(datetime.fromtimestamp(ms // 1000, tz=timezone.utc).date())
        group.append(months is not None and 6 <= months < 18)
    stats = month_block_bootstrap([_month(ms) for ms in stamps], list(daily), group, B=B, seed=seed,
                                  stream="regime-h2")
    return _result("H2", "Months 6-18 after a halving vs others: mean daily log return difference", stats,
                   use_group_months=True)


def h4_weekend(btc: DailySeries, B: int = CALENDAR_B, seed: int = CALENDAR_SEED) -> dict:
    stamps, daily = _calendar_days(btc)
    weekend = [datetime.fromtimestamp(ms // 1000, tz=timezone.utc).weekday() >= 5 for ms in stamps]
    stats = month_block_bootstrap([_month(ms) for ms in stamps], list(daily), weekend, B=B, seed=seed,
                                  stream="regime-h4")
    return _result("H4", "Weekend (Saturday, Sunday UTC) vs weekday: mean daily log return difference", stats,
                   use_group_months=False)


def window_returns(bars: BarSeries) -> tuple[list[int], np.ndarray]:
    """(day open times, (days, 8) simple returns open[h] -> open[h + 3h] for h = 0, 3, ..., 21 UTC).

    A day is kept only when all eight windows are defined (the last loaded day's 21:00 window needs the
    next day's 00:00 open, which is outside the loaded range, so that day is dropped).
    """
    first = max(CALENDAR_FIRST_MS, -(-bars.start_ms // DAY_MS) * DAY_MS)
    end = min(CALENDAR_END_MS, bars.end_ms)
    starts, rows = [], []
    for day in range(first, end, DAY_MS):
        opens = []
        for hour in range(0, 25, WINDOW_HOURS):
            offset = (day + hour * _HOUR_MS - bars.start_ms) // data_lake.MINUTE_MS
            value = bars.open[offset] if 0 <= offset < bars.minutes else MISSING
            opens.append(_NAN if value == MISSING else float(value))
        opens = np.asarray(opens)
        returns = opens[1:] / opens[:-1] - 1.0
        if not np.isnan(returns).any():
            starts.append(day)
            rows.append(returns)
    return starts, np.asarray(rows).reshape(len(rows), 24 // WINDOW_HOURS)


def h3_late_window(bars: BarSeries, B: int = CALENDAR_B, seed: int = CALENDAR_SEED) -> dict:
    """Long 21:00 open -> 00:00 open vs the average of the other seven 3-hour windows (gross)."""
    starts, returns = window_returns(bars)
    late = H3_START_HOUR // WINDOW_HOURS
    months, values, group = [], [], []
    for day, row in zip(starts, returns):
        for window, value in enumerate(row):
            months.append(_month(day))
            values.append(float(value))
            group.append(window == late)
    stats = month_block_bootstrap(months, values, group, B=B, seed=seed, stream="regime-h3")
    gross = float(returns[:, late].mean()) if len(returns) else None
    extra = {"days": len(starts), "mean_gross_21_24": gross,
             "taker_round_trip": decimal_text(TAKER_ROUND_TRIP), "maker_round_trip": decimal_text(MAKER_ROUND_TRIP),
             "mean_net_taker": None if gross is None else gross - float(TAKER_ROUND_TRIP),
             "mean_net_maker": None if gross is None else gross - float(MAKER_ROUND_TRIP)}
    return _result("H3", "Long 21:00-24:00 UTC (21:00 open to 00:00 open) vs the other 3-hour windows, gross", stats,
                   use_group_months=False, extra=extra)


def calendar_tests(btc: DailySeries, bars: BarSeries, B: int = CALENDAR_B, seed: int = CALENDAR_SEED) -> list[dict]:
    return [h1_october(btc, B, seed), h2_halving(btc, B, seed), h3_late_window(bars, B, seed),
            h4_weekend(btc, B, seed)]


# ---------------------------------------------------------------- loading and manifest


def load_inputs(daily_dir, symbols=data_lake.SYMBOLS, *, last_month: str = LAST_MONTH, token=None,
                gate=None) -> tuple[dict, FundingSeries]:
    """dk1 closes of FIRST_MONTH..last_month and BTCUSDT funding; the hidden guard refuses hidden months."""
    daily = {symbol: load_symbol_daily(daily_dir, symbol, FIRST_MONTH, last_month, token=token, gate=gate)
             for symbol in symbols}
    funding = load_symbol_funding_range(daily_dir, MARKET_SYMBOL, FIRST_MONTH, last_month, token=token, gate=gate)
    return daily, funding


def table_bounds(last_month: str = LAST_MONTH) -> tuple[int, int]:
    return data_lake.month_bounds_ms(FIRST_MONTH)[0], data_lake.month_bounds_ms(last_month)[1]


def _render(value):
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return repr(value) if isfinite(value) else None
    if isinstance(value, dict):
        return {key: _render(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_render(item) for item in value]
    return value


def build_manifest(*, rows: int, table_sha256: str, first_day: str, last_day: str, diagnostics: dict,
                   calendar: list, code_commit: str, created_utc: str, inputs: dict | None = None) -> dict:
    manifest = {"schema": SCHEMA, "columns": list(COLUMNS), "rows": rows, "table_sha256": table_sha256,
                "first_day": first_day, "last_day": last_day,
                "thresholds": _render({"sma": [SMA_SHORT, SMA_LONG], "dd_bull": DD_BULL, "dd_bear": DD_BEAR,
                                       "mm_overheated": MM_OVERHEATED, "mm_capitulation": MM_CAPITULATION,
                                       "rv_window": RV_WINDOW, "rv_pct_window": RV_PCT_WINDOW, "rv_calm": RV_CALM,
                                       "rv_stressed": RV_STRESSED, "fr_window_days": FR_WINDOW_DAYS,
                                       "fr_crowded_long_per_8h": decimal_text(FR_CROWDED_LONG),
                                       "fr_crowded_short_per_8h": decimal_text(FR_CROWDED_SHORT),
                                       "halvings": [day.isoformat() for day in HALVINGS],
                                       "halving_bins": [label for _, _, label in HALVING_BINS],
                                       "hysteresis_days": HYSTERESIS_DAYS, "er_window": ER_WINDOW,
                                       "vr_q": VR_Q, "vr_window": VR_WINDOW}),
                "diagnostics": _render(diagnostics), "calendar": _render(calendar),
                "inputs": _render(inputs or {}), "code_commit": code_commit, "created_utc": created_utc,
                "notes": ["Descriptive only: nothing here is a PASS or enters the experiment ledger.",
                          "Labels of day t use closes through day t-1 (00:00 UTC); undefined values are empty.",
                          "Calendar tests use development + validation days (2024-01..2025-12) only and are never "
                          "a signal on their own."]}
    manifest["report_hash"] = content_hash(manifest)
    return manifest


def check_manifest(manifest: dict) -> None:
    body = {key: value for key, value in manifest.items() if key != "report_hash"}
    if manifest.get("report_hash") != content_hash(body):
        raise ValueError("report_hash mismatch")


def report_paths(manifest: dict) -> tuple[str, str, str]:
    stem = f"reports/regime/{SCHEMA}__{manifest['report_hash'][:16]}"
    return stem + ".csv.gz", stem + ".json", stem + ".md"


def public_lines(manifest: dict) -> list[str]:
    """Public log: counts only (rows written) and the report hash."""
    check_manifest(manifest)
    return [f"regime table {manifest['schema']} rows={int(manifest['rows'])}",
            f"calendar hypotheses={len(manifest['calendar'])}", f"report hash {manifest['report_hash']}"]


def _cell(value) -> str:
    return "n/a" if value is None else str(value)


def markdown(manifest: dict) -> str:
    check_manifest(manifest)
    lines = [f"# Regime labels {manifest['schema']}", "",
             f"Report hash `{manifest['report_hash']}`, table sha256 `{manifest['table_sha256']}`, "
             f"{manifest['rows']} rows, {manifest['first_day']}..{manifest['last_day']}. Code "
             f"`{manifest['code_commit']}`, created {manifest['created_utc']}.", ""]
    lines += [f"- {note}" for note in manifest["notes"]] + [""]
    lines += ["## Market label diagnostics (development + validation days)", "",
              "| label | defined days | states | mean duration (days) | flips per year |", "| --- | --- | --- | --- | --- |"]
    for name, stats in manifest["diagnostics"]["development_validation"]["market"].items():
        states = ", ".join(f"{key} {value}" for key, value in stats["states"].items())
        lines.append(f"| {name} | {stats['defined_days']} | {states} | {_cell(stats['mean_duration_days'])} | "
                     f"{_cell(stats['flip_rate_per_year'])} |")
    lines += ["", "## Calendar hypotheses (descriptive, never a signal on their own)", "",
              "| hypothesis | months | n group / rest | difference | 95% interval | t | caveat |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for item in manifest["calendar"]:
        lines.append(f"| {item['hypothesis']}: {item['description']} | {item['n_months']} | {item['n_group']} / "
                     f"{item['n_rest']} | {_cell(item['difference'])} | {_cell(item['interval95'][0])} .. "
                     f"{_cell(item['interval95'][1])} | {_cell(item['t'])} | {item['caveat']} |")
    for item in manifest["calendar"]:
        if item["hypothesis"] == "H3":
            lines += ["", f"H3 gross mean 21:00-24:00 {_cell(item['mean_gross_21_24'])}; net of the taker round trip "
                      f"({item['taker_round_trip']}) {_cell(item['mean_net_taker'])}; net of the maker round trip "
                      f"({item['maker_round_trip']}) {_cell(item['mean_net_maker'])}; days {item['days']}."]
    lines += ["", "Per-symbol diagnostics and the all-days scope are in the JSON manifest.", ""]
    return "\n".join(lines) + "\n"
