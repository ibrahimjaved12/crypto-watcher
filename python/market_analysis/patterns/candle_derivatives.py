"""candle-table-v1 groups derivatives (deriv-v1) and flush (flush-v1); see docs/pattern-study.md.

Every value is as known at the row's decision time (the candle's ``end_ms``); a missing input gives
an empty value and is never filled from an older row.

- Funding (ALL rows use the dk1 funding history from 2020, so the 90-day percentile has no hole at
  the start of the intraday table; the rd funding is only compared with it, see ``funding_parity``):
  a settlement counts once its time floored to the minute is at or before the decision.
- Premium/basis (rows whose minutes are covered by the 1m bars, 2024+): premium-index close at the
  candle's last minute, z of the candle's mean premium against the trailing 30 days of same-tf
  candle means (mean/std, >= 100 values, earlier candles only), and mark close vs price-index close.
- Open interest and positioning (only months published as mx releases): a metrics row stamped T is
  usable from T + 5 min (``metrics_lake.usable_from_ms``); the value at time t is the latest grid
  period usable at t, NaN periods stay empty. ``doi_z`` is the screen's robust z (median / 1.4826
  MAD, ``order_flow.robust_z``) of ``doi_pct`` against the same-tf earlier candles inside a trailing
  window. ``ls_global_z`` is the screen's global-ls z at the latest hourly decision at or before
  the row's decision.
- Flush proxy (flush-v1): candle log return beyond 3 sigma_tf with ``doi_z <= -3``.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from math import isfinite, log, sqrt

from .. import data_lake
from ..benchmark.bars import MISSING
from ..benchmark.order_flow import TrailingHistory, robust_z
from ..benchmark.screen import POSITIONING_HISTORY_DAYS, POSITIONING_MIN_HISTORY, positioning_value
from ..daily_lake import DAY_MS

SCALE = 10 ** data_lake.SCALE_DIGITS
MINUTE = data_lake.MINUTE_MS
HOUR = 60 * MINUTE
FUND_CLAMP = Fraction(1, 10_000)
FUND_PCT_DAYS = 90
FUND_CUM_SETTLEMENTS = 3
PREM_Z_DAYS, PREM_Z_MIN = 30, 100
DOI_WINDOW = {15: (30, 500), 60: (30, 300), 240: (30, 100), 1440: (365, 90), 10080: (730, 52)}  # days, min n
FLUSH_SIGMA, FLUSH_DOI_Z = 3, -3
POSITIONING_COLUMNS = {"ls_global": "count_long_short_ratio", "ls_top_acct": "count_toptrader_long_short_ratio",
                       "ls_top_pos": "sum_toptrader_long_short_ratio", "taker_ls": "sum_taker_long_short_vol_ratio"}
OI_COLUMN = "sum_open_interest_value"


@dataclass(frozen=True)
class DerivInputs:
    """Per-symbol inputs; any of them may be None (its columns are then empty)."""

    funding: object = None      # benchmark.funding.FundingSeries (dk1 history, for every timeframe)
    metrics: object = None      # metrics_lake.MetricsSeries (load_symbol_metrics_months)
    basis: object = None        # benchmark.premium.BasisSeries (1m premium / index closes)
    mark_close: object = None   # array('q') of BarSeries.mark_close, aligned with ``basis``
    ls_global_z: dict | None = None  # hour start ms -> z (hourly_ls_global_z)


def metrics_value(metrics, column: str, time_ms: int):
    """The metrics value usable at ``time_ms`` (latest period with usable_from <= time_ms), else None."""
    if metrics is None:
        return None
    index = metrics.index_at(time_ms)
    if index is None:
        return None
    value = float(metrics.columns[column][index])
    return value if isfinite(value) else None


def funding_parity(dk1, rd) -> dict:
    """dk1-vs-rd funding on their overlap [max(start), min(end)), settlements keyed by time floored to the
    minute: compared (in both), equal (same exact rate), differing, rd_only and dk1_only. Pure integers."""
    if dk1 is None or rd is None:
        return {"overlap_start_ms": None, "overlap_end_ms": None, "compared": 0, "equal": 0, "differing": 0,
                "rd_only": 0, "dk1_only": 0}
    start, end = max(dk1.start_ms, rd.start_ms), min(dk1.end_ms, rd.end_ms)

    def within(series):
        return {t // MINUTE * MINUTE: rate for t, rate in zip(series.calc_time_ms, series.rate) if start <= t < end}

    a, b = within(dk1), within(rd)
    both = a.keys() & b.keys()
    equal = sum(1 for t in both if a[t] == b[t])
    return {"overlap_start_ms": start, "overlap_end_ms": end, "compared": len(both), "equal": equal,
            "differing": len(both) - equal, "rd_only": len(b.keys() - a.keys()), "dk1_only": len(a.keys() - b.keys())}


def hourly_ls_global_z(metrics) -> dict:
    """The screen's global-ls z (positioning_z_series rule) at every hourly decision of the metrics grid."""
    if metrics is None:
        return {}
    history, out = TrailingHistory(POSITIONING_HISTORY_DAYS * DAY_MS), {}
    first = -(-metrics.start_ms // HOUR) * HOUR
    last = metrics.start_ms + (metrics.periods - 1) * 300_000
    for hour in range(first, last + 1, HOUR):
        value = positioning_value(metrics, "global-ls", hour)
        if value is None:
            continue
        z = robust_z(history.at(hour), value, POSITIONING_MIN_HISTORY)
        history.add(hour, value)
        if z is not None:
            out[hour] = float(z)
    return out


# ---------------------------------------------------------------- preparation


def _premium_per_candle(candles, basis, mark_close) -> list:
    """[(prem_last, prem_mean, basis_bp) or None] per candle covered by the minute series."""
    out = [None] * len(candles)
    if basis is None:
        return out
    step = candles.minutes
    for i in range(len(candles)):
        a = (candles.open_ms(i) - basis.start_ms) // MINUTE
        b = a + step
        if a < 0 or b > basis.minutes:
            continue
        last = b - 1
        premium = basis.premium_close
        prem_last = None if premium[last] == MISSING else premium[last] / SCALE
        window = premium[a:b]
        mean = None if MISSING in window else sum(window) / (step * SCALE)
        index, mark = basis.index_close[last], mark_close[last] if mark_close is not None else MISSING
        basis_bp = (mark - index) / index * 1e4 if MISSING not in (index, mark) and index > 0 else None
        out[i] = (prem_last, mean, basis_bp)
    return out


def _trailing_mean_z(times: list, values: list, span_ms: int, minimum: int) -> list:
    """z of values[i] against the values of earlier entries with time in [t_i - span, t_i) (mean / std)."""
    out = [None] * len(values)
    window, total, squares = deque(), 0.0, 0.0
    for i, (t, value) in enumerate(zip(times, values)):
        while window and window[0][0] < t - span_ms:
            _, old = window.popleft()
            total -= old
            squares -= old * old
        if value is None:
            continue
        n = len(window)
        if n >= minimum:
            mean = total / n
            variance = max(squares / n - mean * mean, 0.0)
            if variance > 0:
                out[i] = (value - mean) / sqrt(variance)
        window.append((t, value))
        total += value
        squares += value * value
    return out


def _turnover_24h(ctx, i: int):
    """Sum of close * base volume over the 24 h ending at the decision (intraday: the row's candles;
    daily/weekly: the dk1 day ending at the decision). None if any needed candle lacks it."""
    if ctx.minutes in (15, 60, 240):
        source, count, last = ctx.candles, 1440 // ctx.minutes, i
    else:
        source = getattr(ctx, "history_days", None)
        if source is None:
            return None
        count, last = 1, (ctx.candles.end_ms[i] - DAY_MS - source.start_ms) // DAY_MS
    first = last - count + 1
    if first < 0 or last >= len(source):
        return None
    total = 0
    for j in range(first, last + 1):
        if not source.valid[j] or source.volume[j] == MISSING:
            return None
        total += source.close[j] * source.volume[j]
    return total / (SCALE * SCALE)


def prepare(ctx) -> None:
    """Per-candle premium, OI change and turnover inputs (called by TableContext.__post_init__)."""
    d = ctx.derivs or DerivInputs()
    c = ctx.candles
    ctx.prem = _premium_per_candle(c, d.basis, d.mark_close)
    means = [None if row is None else row[1] for row in ctx.prem]
    ctx.prem_z = _trailing_mean_z(c.end_ms, means, PREM_Z_DAYS * DAY_MS, PREM_Z_MIN)
    ctx.oi_end = [metrics_value(d.metrics, OI_COLUMN, c.end_ms[i]) for i in range(len(c))]
    ctx.oi_start = [metrics_value(d.metrics, OI_COLUMN, c.open_ms(i)) for i in range(len(c))]
    ctx.doi = [end / start - 1 if end is not None and start is not None and start > 0 else None
               for end, start in zip(ctx.oi_end, ctx.oi_start)]
    days, minimum = DOI_WINDOW[ctx.minutes]
    history, ctx.doi_z = TrailingHistory(days * DAY_MS), [None] * len(c)
    for i, value in enumerate(ctx.doi):
        if value is None:
            continue
        z = robust_z(history.at(c.end_ms[i]), value, minimum)
        history.add(c.end_ms[i], value)
        ctx.doi_z[i] = None if z is None else float(z)


# ---------------------------------------------------------------- groups


def _funding(funding, decision: int) -> dict:
    out = dict.fromkeys(("fund_last", "fund_pct90", "fund_clamped", "fund_cum_24h"))
    if funding is None or not funding.start_ms <= decision < funding.end_ms:
        return out
    times, rates = funding.calc_time_ms, funding.rate
    k = bisect_left(times, decision + MINUTE)  # settled: time floored to the minute <= decision
    if k < 1:
        return out
    last = rates[k - 1]
    out.update(fund_last=float(last), fund_clamped=last == FUND_CLAMP)
    lower = decision - FUND_PCT_DAYS * DAY_MS
    if lower >= funding.start_ms:
        window = rates[bisect_left(times, lower):k]
        out["fund_pct90"] = sum(1 for rate in window if rate <= last) / len(window)
    if k >= FUND_CUM_SETTLEMENTS:
        out["fund_cum_24h"] = float(sum(rates[k - FUND_CUM_SETTLEMENTS:k]))
    return out


def derivatives(ctx, i: int) -> dict:
    d = ctx.derivs or DerivInputs()
    decision = ctx.candles.end_ms[i]
    out = _funding(d.funding, decision)
    prem = ctx.prem[i]
    out.update(prem_last=prem[0] if prem else None, prem_z=ctx.prem_z[i], basis_bp=prem[2] if prem else None)
    end, start = ctx.oi_end[i], ctx.oi_start[i]
    turnover = _turnover_24h(ctx, i)
    out.update(oi_value_end=end, oi_value_start=start, doi_pct=ctx.doi[i], doi_z=ctx.doi_z[i],
               oi_vol_ratio=end / turnover if end is not None and turnover else None)
    for name, column in POSITIONING_COLUMNS.items():
        out[name] = metrics_value(d.metrics, column, decision)
    hour = decision // HOUR * HOUR
    out["ls_global_z"] = (d.ls_global_z or {}).get(hour) if d.metrics is not None else None
    return out


def flush(ctx, i: int) -> dict:
    c = ctx.candles
    sigma, doi_z = ctx.sigma[i], ctx.doi_z[i]
    if not sigma or doi_z is None:
        return dict.fromkeys(("flush_down", "flush_up", "flush_dir"))
    ret = log(c.close[i] / c.open[i])
    collapsed = doi_z <= FLUSH_DOI_Z
    down = collapsed and ret <= -FLUSH_SIGMA * sigma
    up = collapsed and ret >= FLUSH_SIGMA * sigma
    return {"flush_down": down, "flush_up": up, "flush_dir": -1 if down else 1 if up else 0}


DERIVATIVES_COLUMNS = (
    ("fund_last", "last funding rate settled at or before the decision (settlement time floored to the minute)"),
    ("fund_pct90", "fraction of the rates settled in the previous 90 days (incl. fund_last) that are <= fund_last"),
    ("fund_clamped", "fund_last == 0.0001 (the clamp value)"),
    ("fund_cum_24h", "sum of the last 3 settled rates"),
    ("prem_last", "premium-index close at the candle's last minute (1m bars, 2024+)"),
    ("prem_z", "(candle mean premium - mean) / std of earlier same-tf candle means in 30 days (>= 100)"),
    ("basis_bp", "1e4 * (mark close - index close) / index close at the candle's last minute"),
    ("oi_value_end", "open interest value (USDT) usable at the decision (mx months only)"),
    ("oi_value_start", "open interest value usable at the candle's open"),
    ("doi_pct", "oi_value_end / oi_value_start - 1"),
    ("doi_z", "robust z of doi_pct vs earlier same-tf candles (15/60/240: 30 d >= 500/300/100; "
              "1440: 365 d >= 90; 10080: 730 d >= 52)"),
    ("oi_vol_ratio", "oi_value_end / sum of close * base volume over the 24 h ending at the decision"),
    ("ls_global", "global account long/short ratio usable at the decision"),
    ("ls_top_acct", "top-trader account long/short ratio usable at the decision"),
    ("ls_top_pos", "top-trader position long/short ratio usable at the decision"),
    ("taker_ls", "taker buy/sell volume ratio of the 5-minute period usable at the decision"),
    ("ls_global_z", "the screen's global-ls z at the latest hourly decision at or before the decision"),
)
FLUSH_COLUMNS = (
    ("flush_down", "ln(close/open) <= -3 sigma_tf and doi_z <= -3 (long liquidation proxy)"),
    ("flush_up", "ln(close/open) >= +3 sigma_tf and doi_z <= -3 (short squeeze proxy)"),
    ("flush_dir", "-1 flush_down, +1 flush_up, 0 neither"),
)
