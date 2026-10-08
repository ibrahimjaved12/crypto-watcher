"""Exact label selection, entry-day daily P&L and descriptive statistics.

Daily values and net_at use micro-R (10**6 = 1 R). Reports use R. Fractions
remain exact until decimal rendering: at most 12 decimal places, half-even,
with trailing zeros removed. Only square-root statistics use floats (repr).
Undefined ratios/empty statistics are None. X outcomes count as trades but
are excluded from monetary statistics and daily sums, never filled as returns.
"""
from __future__ import annotations

from array import array
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from math import sqrt
from numbers import Integral

from .label_store import Geometry, GeometryColumns
from .scan import OUTCOMES, UR
from .segments import segment_bounds_ms

DAY_MS = 86_400_000


@dataclass(frozen=True)
class Trade:
    geometry: Geometry
    signal_ms: int
    outcome: str
    exit_offset: int
    net_ur: int
    cost_ur: int
    fund_ur: int
    wallet_ur: int
    amb: int
    opt_net_ur: int
    opt_cost_ur: int
    opt_fund_ur: int

    @property
    def symbol(self) -> str:
        return self.geometry.symbol

    @property
    def exit_ms(self) -> int:
        """Exit minute's UTC start (the resolution of bar-mode labels)."""
        return self.signal_ms + self.exit_offset * 60_000


@dataclass
class Selection:
    trades: list[Trade] = field(default_factory=list)
    signals: int = 0
    non_trades: Counter = field(default_factory=Counter)
    not_in_grid: int = 0
    purged: int = 0

    def __iter__(self):
        return iter(self.trades)

    def __len__(self):
        return len(self.trades)


def _index(times, signal_ms):
    index = bisect_left(times, signal_ms)
    return index if index < len(times) and times[index] == signal_ms else None


def select(columns, signals) -> Selection:
    """Match (symbol, signal_ms, side, horizon_min) exactly, with no interpolation.

    Select one k/rr_index per (symbol, side, horizon) per call. Passing several
    alternatives for the same signal is rejected rather than multiplying trades
    or picking an arbitrary variant. Pass a subset of a loaded store for each
    strategy variant. Selection retains the signal accounting for metrics().
    """
    values = [columns] if isinstance(columns, GeometryColumns) else columns.values()
    lookup = {}
    for column in values:
        geometry = column.geometry
        key = (geometry.symbol, geometry.side, geometry.horizon_min)
        if key in lookup:
            raise ValueError("select needs one geometry per symbol/side/horizon")
        lookup[key] = column
    result = Selection()
    for symbol, signal_ms, side, horizon_min in signals:
        if type(signal_ms) is not int or type(side) is not int or side not in (-1, 1) or type(horizon_min) is not int:
            raise ValueError("signal time, side and horizon must be valid integers")
        key = (symbol, side, horizon_min)
        if key not in lookup:
            raise ValueError("signal geometry was not loaded")
        column = lookup[key]
        result.signals += 1
        if _index(column.purged_signal_ms, signal_ms) is not None:
            result.purged += 1
            continue
        index = _index(column.non_trades["signal_ms"], signal_ms)
        if index is not None:
            result.non_trades[chr(column.non_trades["reason"][index])] += 1
            continue
        index = _index(column["signal_ms"], signal_ms)
        if index is None:
            result.not_in_grid += 1
            continue
        row = {name: data[index] for name, data in column.data.items()}
        row["outcome"] = chr(row["outcome"])
        result.trades.append(Trade(column.geometry, **row))
    return result


def net_at(row: Trade, m=1, policy: str = "pess") -> Fraction:
    """Micro-R at a cost multiplier; funding is already included in net.

    Fractional multipliers retain sub-micro-R precision. The wallet floor applies
    to either ambiguity policy. X has no measured P&L and is an explicit error.
    """
    if type(m) not in (int, Fraction) or m < 0:
        raise ValueError("cost multiplier must be a non-negative int or Fraction")
    if policy not in ("pess", "opt"):
        raise ValueError("policy must be pess or opt")
    if row.outcome == "X":
        raise ValueError("X outcome has no measured net return")
    net, cost = (row.net_ur, row.cost_ur) if policy == "pess" else (row.opt_net_ur, row.opt_cost_ur)
    return max(Fraction(-row.wallet_ur), net + (1 - Fraction(m)) * cost)


def daily_series(trades, segment: str) -> array:
    """Full segment's int64 micro-R totals, attributed to the ENTRY UTC day.

    A trade's entire net (including funding) belongs to its entry day, regardless
    of exit day. Days without measured trades are zero. X is counted by metrics
    but has no P&L to attribute. Out-of-segment entries are errors, not dropped.
    """
    first, end = segment_bounds_ms(segment)
    daily = array("q", [0]) * ((end - first) // DAY_MS)
    for row in trades:
        if not first <= row.signal_ms < end:
            raise ValueError("trade entry outside daily segment")
        if row.outcome != "X":
            index = (row.signal_ms - first) // DAY_MS
            daily[index] += int(net_at(row))  # m=1 is integral; array catches int64 overflow
    return daily


def decimal_text(value, places: int = 12) -> str | None:
    """Exact integer half-even rounding to a finite decimal report string."""
    if value is None:
        return None
    value = Fraction(value)
    scale = 10 ** places
    quotient, remainder = divmod(abs(value.numerator) * scale, value.denominator)
    if 2 * remainder > value.denominator or (2 * remainder == value.denominator and quotient % 2):
        quotient += 1
    whole, fractional = divmod(quotient, scale)
    text = str(whole)
    if fractional:
        text += "." + f"{fractional:0{places}d}".rstrip("0")
    return ("-" if value < 0 and quotient else "") + text


def _mean(values):
    return Fraction(sum(values), len(values)) if values else None


def nearest_rank(values, probability: Fraction):
    """Nearest-rank quantile: sorted_values[ceil(p*n)-1], 0 < p <= 1."""
    if type(probability) not in (int, Fraction) or not 0 < probability <= 1:
        raise ValueError("quantile probability must be in (0, 1]")
    if not values:
        return None
    probability = Fraction(probability)
    rank = -(-probability.numerator * len(values) // probability.denominator)
    return sorted(values)[rank - 1]


def daily_moments(daily):
    """Exact sample mean/variance in R and R**2; None when undefined."""
    values = []
    for value in daily:
        if not isinstance(value, Integral) or isinstance(value, bool):
            raise ValueError("daily values must be integer micro-R")
        values.append(Fraction(int(value), UR))
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1) if len(values) > 1 else None
    return values, mean, variance


def drawdown(daily) -> tuple[int, int]:
    """Maximum cumulative micro-R drawdown and consecutive days below a peak.

    Include the initial zero equity peak. Recovery to an equal peak ends a spell;
    an unrecovered final spell counts through the last observed day.
    """
    equity = peak = maximum = underwater = longest = 0
    for value in daily:
        equity += int(value)
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
        underwater = underwater + 1 if equity < peak else 0
        longest = max(longest, underwater)
    return maximum, longest


def metrics(trades, daily) -> dict:
    """All trade shares include X; return/win/payoff statistics exclude X.

    Win = positive net after costs/funding. Payoff compares mean positive net to
    mean absolute negative net; profit factor compares their sums. Breakevens
    enter win-rate/mean denominators. Sharpe and Sortino annualize by sqrt(365);
    downside deviation is sqrt(mean(min(daily_R, 0)**2)) over ALL segment days.
    """
    selection = trades if isinstance(trades, Selection) else None
    trades = list(trades)
    usable = [row for row in trades if row.outcome != "X"]
    nets = sorted(net_at(row) / UR for row in usable)
    mean = _mean(nets)
    optimistic = _mean([net_at(row, policy="opt") / UR for row in usable])
    positives, negatives = [net for net in nets if net > 0], [-net for net in nets if net < 0]
    outcomes = Counter(row.outcome for row in trades)
    values, daily_mean, variance = daily_moments(daily)
    T = len(values)
    std = sqrt(variance) if variance is not None else None
    downside = sqrt(sum(min(value, 0) ** 2 for value in values) / T) if T else None
    maximum, longest = drawdown(daily)
    median = (nets[(len(nets) - 1) // 2] + nets[len(nets) // 2]) / 2 if nets else None
    return {
        "signals": selection.signals if selection is not None else len(trades),
        "trades": len(trades), "usable_trades": len(usable),
        "non_trades": dict(sorted(selection.non_trades.items())) if selection is not None else {},
        "not_in_grid": selection.not_in_grid if selection is not None else 0,
        "purged": selection.purged if selection is not None else 0, "X_count": outcomes["X"],
        "outcome_counts": {name: outcomes[name] for name in OUTCOMES},
        "outcome_shares": {name: decimal_text(Fraction(outcomes[name], len(trades))) if trades else None for name in OUTCOMES},
        "ambiguity_share": decimal_text(Fraction(sum(row.amb for row in trades), len(trades))) if trades else None,
        "mean_net_r": decimal_text(mean), "median_net_r": decimal_text(median),
        "quantiles_net_r": {str(p): decimal_text(nearest_rank(nets, Fraction(p, 100))) for p in (1, 5, 25, 75, 95, 99)},
        "best_net_r": decimal_text(nets[-1]) if nets else None, "worst_net_r": decimal_text(nets[0]) if nets else None,
        "win_rate": decimal_text(Fraction(len(positives), len(nets))) if nets else None,
        "payoff_ratio": decimal_text(_mean(positives) / _mean(negatives)) if positives and negatives else None,
        "profit_factor": decimal_text(sum(positives) / sum(negatives)) if negatives else None,
        "cost_grid_mean_net_r": {str(m): decimal_text(_mean([net_at(row, m) / UR for row in usable])) for m in range(4)},
        "mean_funding_r": decimal_text(_mean([Fraction(row.fund_ur, UR) for row in usable])),
        "optimistic_mean_net_r": decimal_text(optimistic),
        "sign_flip": mean is not None and mean * optimistic < 0,
        "daily_mean_r": decimal_text(daily_mean), "daily_std_r": repr(std) if std is not None else None,
        "annualized_sharpe": repr(float(daily_mean) / std * sqrt(365)) if std else None,
        "sortino": repr(float(daily_mean) / downside * sqrt(365)) if downside else None,
        "max_drawdown_r": decimal_text(Fraction(maximum, UR)), "longest_underwater_days": longest,
        "T_days": T, "trades_per_day": decimal_text(Fraction(len(trades), T)) if T else None,
    }


def bootstrap_ci(daily, *, B: int, seed: int, stream_prefix: str) -> dict:
    """Stationary-bootstrap 95% nearest-rank percentile CI of mean daily R.

    Use the existing counter RNG via bootstrap_indices; exact replicate sums are
    divided by T only as Fractions. t_statistic = mean / std of the B replicate
    means (population std, exact from the integer sums; one sqrt at the end), so
    it respects the serial dependence the stationary bootstrap keeps. t_iid is
    the ordinary iid sample-standard-error t, kept for reference only.
    Replicates are batched to avoid retaining a B-by-T index matrix.
    """
    import numpy as np
    from .bootstrap import bootstrap_indices, bootstrap_mean_matrix, default_mean_block

    if type(B) is not int or B < 1:
        raise ValueError("B must be a positive integer")
    _, mean, variance = daily_moments(daily)
    d = np.asarray(daily, dtype=np.int64)
    if d.ndim != 1:
        raise ValueError("daily must be one-dimensional")
    d = d.reshape(-1, 1)
    T = len(d)
    block = default_mean_block(T)
    sums = []
    batch = max(1, (8 * 1024 * 1024) // (8 * T))
    for start in range(0, B, batch):
        indices = bootstrap_indices(T, block, seed, stream_prefix, range(start, min(B, start + batch)))
        sums.extend(int(value) for value in bootstrap_mean_matrix(d, indices)[:, 0])
    iid = float(mean) / sqrt(variance / T) if variance else None
    # Var_b(sum_b / (T * UR)) exactly: (B * sum(S^2) - (sum S)^2) / (B^2 * T^2 * UR^2).
    spread = B * sum(value * value for value in sums) - sum(sums) ** 2
    statistic = None
    if spread > 0:
        squared = mean * mean / Fraction(spread, (B * T * UR) ** 2)
        statistic = (1 if mean >= 0 else -1) * sqrt(squared)
    return {"lower": decimal_text(Fraction(nearest_rank(sums, Fraction(1, 40)), T * UR)),
            "upper": decimal_text(Fraction(nearest_rank(sums, Fraction(39, 40)), T * UR)),
            "mean_daily_r": decimal_text(mean), "t_statistic": repr(statistic) if statistic is not None else None,
            "t_iid": repr(iid) if iid is not None else None,
            "B": B, "T_days": T, "mean_block": block}
