"""Multi-day trend, Mode B portfolio evaluation (#222; SPEC-2 section 1, frozen v1 choices).

Daily-bar trend signals sized to a volatility target and evaluated as a daily portfolio
return stream (Mode B). Mode A (trade level) and the 4h variants are not part of v1.

Timing (point in time). The decision of close ``d`` uses closes through ``d`` only; the
position of day ``t = d + 1`` is established at the day-t open and held to the day-t close.
So day t's information set is closes up to day t-1. The Donchian channel of decision ``d``
is ``max/min(close[d-L..d-1])`` compared with ``close[d]`` (the SPEC-2 formula moved one
day: a channel that contained ``close[d]`` itself could never be broken).

A component or signal is undefined (weight 0, flat) while any close in its lookback
window is MISSING or the window is not fully available. An ensemble component restarts
from state 0 after an undefined day. Data gaps never feed back into the weight path:
a day whose own open/close is MISSING (or without funding coverage) is simply not
evaluated for that symbol, so ``w_t`` never depends on day t or later.

Day net of one symbol, as a fraction of equity:
``w_t * (close_t / open_t - 1) - m * 11 bp * |w_t - w_{t-1}| - w_t * sum(funding rates)``
over settlements with ``open_t < calc_time <= open_t + 1 day`` (a settlement exactly at the
next 00:00 is paid by the position held into it; long pays positive rates). The portfolio
stream is the equal-weight mean over the symbols defined that day (weight defined, or a
non-zero weight being closed, and the day evaluable); a day with none is 0.

Evaluation reuses the #182 harness: integer parts per million of equity per day,
``evaluate.bootstrap_ci`` (its ``mean_daily_r`` is the mean daily return fraction),
``power.min_detectable_edge_per_day`` (mu_min, recorded before any verdict),
``spa.spa_test``, ``stepm.stepm`` / ``stepm_p_values`` and ``best_trial.best_trial_dsr``
across the K = 9 fixed variants, with ``runner.required_t`` of the question-wide count.
Floats are used for signals, weights and the Newey-West regression only.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from fractions import Fraction
from math import isfinite, sqrt

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .. import data_lake
from ..daily_lake import DAY_MS, DailySeries, load_symbol_daily, load_symbol_funding_range
from .bars import MISSING
from .best_trial import best_trial_dsr
from .calibration import check_segment
from .canonical import content_hash
from .evaluate import bootstrap_ci, decimal_text, drawdown
from .experiment_log import TrialRecord, TrialStatus
from .funding import FundingSeries
from .power import min_detectable_edge_per_day
from .rng import u64_words
from .runner import NO_PROGRESS, required_t
from .segments import segment_bounds_ms
from .spa import spa_test
from .stepm import stepm, stepm_p_values

SCHEMA = "trend-report-v1"
QUESTION_ID = "trend-v1"
FAMILY_ID = "trend"
STRATEGY_VERSION = "trend-v1"
WARMUP_FIRST_MONTH = "2020-01"
LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
SUB3_LOOKBACKS = (20, 60, 150)
SIGMA_WINDOW = 90
EWMA_HALF_LIFE = 60
EWMA_MIN_RETURNS = 2 * EWMA_HALF_LIFE  # warm-up of the EWMA estimator (no fixed window to fill)
ANNUALIZATION = 365
CAP = 2.0
BAND = 0.10
COST_BP_PER_UNIT_TURNOVER = 11
COST_MULTIPLIERS = (1, 2)
MA_LENGTHS = (20, 50, 100, 200)
M_LONG_MIN, M_SHORT_MAX = 3, 1
RV_WINDOW = 30
RV_PCT_WINDOW = 365
RV_BLOCK_PCT = 70
NW_LAG = 10
ALPHA_T_MIN = 2.0
PLACEBO_ALPHA = 0.05
B_STATS = 2000
N_SHIFTS = 1000
MIN_SHIFT = 30
SEED = 20261009
PPM = 1_000_000
_COST = COST_BP_PER_UNIT_TURNOVER / 10_000
_NAN = float("nan")


# ---------------------------------------------------------------- variants


@dataclass(frozen=True)
class TrendVariant:
    name: str
    family: str                      # "ens" (Donchian ensemble) | "tsmom"
    sigma_target: str                # annualized, decimal text
    lookbacks: tuple = LOOKBACKS     # ens only
    long_only: bool = False
    filter: str | None = None        # None | "mfilter" | "rvfilter"
    tsmom_lookback: int | None = None

    def __post_init__(self):
        if self.family not in ("ens", "tsmom") or self.filter not in (None, "mfilter", "rvfilter"):
            raise ValueError(f"invalid trend variant {self.name!r}")
        if (self.family == "tsmom") != (self.tsmom_lookback is not None):
            raise ValueError("tsmom_lookback is required for tsmom variants only")

    @property
    def target(self) -> float:
        return float(self.sigma_target)

    @property
    def sizing(self) -> str:
        return "std90" if self.family == "ens" else "ewma60"

    def config(self, symbols) -> dict:
        """Every parameter (canonical JSON types only): the ledger identity of the variant."""
        signal = ({"lookbacks": list(self.lookbacks), "channel": "close-based Donchian, trailing midline exit"}
                  if self.family == "ens" else {"lookback": self.tsmom_lookback, "signal": "sign of L-day log return"})
        sizing = ({"estimator": "sample std of daily log returns", "window": SIGMA_WINDOW}
                  if self.family == "ens" else
                  {"estimator": "zero-mean EWMA std of daily log returns", "half_life": EWMA_HALF_LIFE,
                   "min_returns": EWMA_MIN_RETURNS})
        return {"family": self.family, "signal": signal, "long_only": self.long_only, "filter": self.filter,
                "filter_params": ({"ma_lengths": list(MA_LENGTHS), "long_min": M_LONG_MIN,
                                   "short_max": M_SHORT_MAX} if self.filter == "mfilter" else
                                  {"rv_window": RV_WINDOW, "pct_window": RV_PCT_WINDOW, "block_pct": RV_BLOCK_PCT}
                                  if self.filter == "rvfilter" else None),
                "sigma_target": self.sigma_target, "sizing": sizing, "annualization": ANNUALIZATION,
                "cap": "2", "band": "0.10", "cost_bp_per_unit_turnover": COST_BP_PER_UNIT_TURNOVER,
                "cost_multipliers": list(COST_MULTIPLIERS), "funding": "w * sum(rate), open < calc <= next open",
                "execution": "position at the day-t open from closes through t-1, held to the day-t close",
                "catastrophe_stop": None, "universe": sorted(symbols), "portfolio": "equal weight, defined symbols"}


VARIANTS = (
    TrendVariant("ens_ls_25", "ens", "0.25"),
    TrendVariant("ens_lo_25", "ens", "0.25", long_only=True),
    TrendVariant("ens_ls_15", "ens", "0.15"),
    TrendVariant("ens_ls_25_sub3", "ens", "0.25", lookbacks=SUB3_LOOKBACKS),
    TrendVariant("tsmom_7", "tsmom", "0.25", lookbacks=(), tsmom_lookback=7),
    TrendVariant("tsmom_14", "tsmom", "0.25", lookbacks=(), tsmom_lookback=14),
    TrendVariant("tsmom_28", "tsmom", "0.25", lookbacks=(), tsmom_lookback=28),
    TrendVariant("ens_ls_25_mfilter", "ens", "0.25", filter="mfilter"),
    TrendVariant("ens_ls_25_rvfilter", "ens", "0.25", filter="rvfilter"),
)
K = len(VARIANTS)


# ---------------------------------------------------------------- primitives (index d = value at close d)


def prices(column) -> np.ndarray:
    """Scaled integer prices as floats (ratios only, so the 10^8 scale is irrelevant); MISSING -> NaN."""
    values = np.asarray(column, dtype=np.int64)
    return np.where(values == MISSING, _NAN, values.astype(np.float64))


def shift1(values) -> np.ndarray:
    """Value of the previous close: the information available at the next day's open."""
    values = np.asarray(values, dtype=np.float64)
    out = np.full(values.shape, _NAN)
    out[1:] = values[:-1]
    return out


def log_returns(close) -> np.ndarray:
    out = np.full(len(close), _NAN)
    out[1:] = np.log(close[1:] / close[:-1])
    return out


def _rolling(values, window: int, reduce) -> np.ndarray:
    """out[d] = reduce(values[d-window+1..d]); NaN when the window is incomplete or holds NaN."""
    values = np.asarray(values, dtype=np.float64)
    out = np.full(len(values), _NAN)
    if len(values) >= window:
        out[window - 1:] = reduce(sliding_window_view(values, window))
    return out


def rolling_mean(values, window: int) -> np.ndarray:
    return _rolling(values, window, lambda w: w.mean(axis=1))


def rolling_std(values, window: int) -> np.ndarray:
    """Sample standard deviation (ddof 1)."""
    return _rolling(values, window, lambda w: w.std(axis=1, ddof=1))


def prior_extremes(close, lookback: int) -> tuple[np.ndarray, np.ndarray]:
    """(max, min) of close[d-L..d-1] at index d (the channel decision d compares close[d] against)."""
    upper, lower = np.full(len(close), _NAN), np.full(len(close), _NAN)
    if len(close) > lookback:
        windows = sliding_window_view(close, lookback)[:len(close) - lookback]
        upper[lookback:] = windows.max(axis=1)  # NaN propagates: a gap leaves the channel undefined
        lower[lookback:] = windows.min(axis=1)
    return upper, lower


def percentile_rank(values, window: int) -> np.ndarray:
    """100 * #(x in values[d-window+1..d] with x <= values[d]) / window; NaN unless the window is complete."""
    values = np.asarray(values, dtype=np.float64)
    out = np.full(len(values), _NAN)
    if len(values) >= window:
        windows = sliding_window_view(values, window)
        ranks = (windows <= windows[:, -1:]).sum(axis=1) * 100.0 / window
        out[window - 1:] = np.where(np.isnan(windows).any(axis=1), _NAN, ranks)
    return out


def ewma_std(returns, half_life: int = EWMA_HALF_LIFE, min_returns: int = EWMA_MIN_RETURNS) -> np.ndarray:
    """Zero-mean EWMA std (weights 0.5^(age/half_life), normalized) over the run since the last gap."""
    decay = 0.5 ** (1.0 / half_life)
    out = np.full(len(returns), _NAN)
    numerator = denominator = 0.0
    run = 0
    for d, value in enumerate(returns):
        if value != value:  # NaN: a gap restarts the estimator
            numerator = denominator = 0.0
            run = 0
            continue
        numerator = decay * numerator + value * value
        denominator = decay * denominator + 1.0
        run += 1
        if run >= min_returns:
            out[d] = sqrt(numerator / denominator)
    return out


def channel_states(close, lookback: int) -> np.ndarray:
    """Component state at each close in {-1, 0, +1}; NaN while undefined (then restarts from 0).

    Order: close > upper -> +1; else close < lower -> -1; else long and close < mid -> 0;
    else short and close > mid -> 0; else keep.
    """
    upper, lower = prior_extremes(close, lookback)
    out = np.full(len(close), _NAN)
    state = 0
    for d in range(len(close)):
        u, low, x = upper[d], lower[d], close[d]
        if u != u or low != low or x != x:
            state = 0
            continue
        mid = (u + low) / 2
        if x > u:
            state = 1
        elif x < low:
            state = -1
        elif state == 1 and x < mid:
            state = 0
        elif state == -1 and x > mid:
            state = 0
        out[d] = state
    return out


def ensemble_signal(close, lookbacks=LOOKBACKS) -> np.ndarray:
    """Mean of the component states at each close; defined only when every component is."""
    return np.vstack([channel_states(close, lookback) for lookback in lookbacks]).mean(axis=0)


def tsmom_signal(close, lookback: int) -> np.ndarray:
    """sign(ln close[d] - ln close[d-L]) at each close."""
    out = np.full(len(close), _NAN)
    if len(close) > lookback:
        out[lookback:] = np.sign(np.log(close[lookback:]) - np.log(close[:-lookback]))
    return out


def ma_score(close) -> np.ndarray:
    """M = #(k in 20, 50, 100, 200 with close[d] > SMA_k[d]); NaN until every SMA is defined."""
    stacked = np.vstack([rolling_mean(close, k) for k in MA_LENGTHS])
    score = (close[None, :] > stacked).sum(axis=0).astype(np.float64)
    return np.where(np.isnan(stacked).any(axis=0) | np.isnan(close), _NAN, score)


def realized_vol_percentile(close) -> np.ndarray:
    """Percentile of the 30-day realized vol within the trailing 365 days (including the day), at each close."""
    return percentile_rank(rolling_std(log_returns(close), RV_WINDOW), RV_PCT_WINDOW)


# ---------------------------------------------------------------- weights


@dataclass(frozen=True)
class SignalInputs:
    """Position-day inputs of one variant on one symbol (index t = position day of the series)."""

    signal: np.ndarray     # S_t, NaN when undefined
    sigma: np.ndarray      # annualized sigma_hat_t, NaN when undefined
    block: np.ndarray | None  # rvfilter: new positions blocked on day t


def signal_inputs(variant: TrendVariant, close) -> SignalInputs:
    returns = log_returns(close)
    if variant.family == "ens":
        signal = shift1(ensemble_signal(close, variant.lookbacks))
        sigma = shift1(rolling_std(returns, SIGMA_WINDOW)) * sqrt(ANNUALIZATION)
    else:
        signal = shift1(tsmom_signal(close, variant.tsmom_lookback))
        sigma = shift1(ewma_std(returns)) * sqrt(ANNUALIZATION)
    if variant.long_only:
        signal = np.maximum(signal, 0.0)  # NaN stays NaN
    block = None
    if variant.filter == "mfilter":
        score = shift1(ma_score(close))
        signal = np.where((signal > 0) & (score < M_LONG_MIN), 0.0, signal)
        signal = np.where((signal < 0) & (score > M_SHORT_MAX), 0.0, signal)
        signal = np.where(np.isnan(score), _NAN, signal)
    elif variant.filter == "rvfilter":
        percentile = shift1(realized_vol_percentile(close))
        signal = np.where(np.isnan(percentile), _NAN, signal)
        block = percentile >= RV_BLOCK_PCT
    return SignalInputs(signal, sigma, block)


def size(signal, sigma, target: float) -> np.ndarray:
    """clip(S * sigma_target / sigma_hat, -2, 2); NaN when either input is undefined or sigma_hat <= 0."""
    signal, sigma = np.asarray(signal, dtype=np.float64), np.asarray(sigma, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = np.clip(signal * target / sigma, -CAP, CAP)
    return np.where(sigma > 0, raw, _NAN)


def apply_band(target, block=None, w0=0.0) -> np.ndarray:
    """Weight path under the no-trade band; works on (T,) or (T, m) targets (m independent paths).

    Undefined target -> 0 (flat). Otherwise the previous weight is kept unless
    |w - w_prev| >= 0.10 |w_prev|, the sign changes, or w_prev == 0 and w != 0. With
    ``block`` (rvfilter) a new position (w_prev == 0, w != 0) is refused on blocked days.
    """
    target = np.asarray(target, dtype=np.float64)
    out = np.empty(target.shape)
    previous = np.zeros(target.shape[1:]) + w0
    for t in range(len(target)):
        wanted = np.where(np.isnan(target[t]), 0.0, target[t])
        move = ((np.sign(wanted) != np.sign(previous)) | ((previous == 0) & (wanted != 0))
                | (np.abs(wanted - previous) >= BAND * np.abs(previous)))
        weight = np.where(move, wanted, previous)
        if block is not None:
            weight = np.where(block[t] & (previous == 0) & (weight != 0), 0.0, weight)
        out[t] = weight
        previous = weight
    return out


# ---------------------------------------------------------------- symbol data and streams


@dataclass(frozen=True)
class SymbolData:
    """One symbol over the whole loaded history, plus the segment's per-day return and funding."""

    symbol: str
    close: np.ndarray          # full series, NaN on gap days
    index: np.ndarray          # (T,) series index of each segment day (-1 when outside the series)
    ret: np.ndarray            # (T,) close/open - 1 of the segment day, NaN when not evaluable
    funding: np.ndarray        # (T,) sum of funding rates paid by a position held that day, NaN when unknown

    @property
    def evaluable(self) -> np.ndarray:
        return ~np.isnan(self.ret) & ~np.isnan(self.funding)


def day_funding(funding: FundingSeries, open_ms: int) -> float:
    """Sum of rates with open < calc_time <= open + 1 day (clipped to the covered range); NaN outside it."""
    if not funding.start_ms <= open_ms < funding.end_ms:
        return _NAN
    events = funding.events_between(open_ms, min(open_ms + DAY_MS, funding.end_ms - 1))
    return float(sum((rate for _, rate, _ in events), Fraction(0)))


def prepare_symbol(series: DailySeries, funding: FundingSeries, first_ms: int, end_ms: int) -> SymbolData:
    close, opens = prices(series.close), prices(series.open)
    days = (end_ms - first_ms) // DAY_MS
    index = (first_ms - series.start_ms) // DAY_MS + np.arange(days)
    inside = (index >= 0) & (index < series.days)
    index = np.where(inside, index, -1)
    ret = np.full(days, _NAN)
    ret[inside] = close[index[inside]] / opens[index[inside]] - 1.0
    paid = np.array([day_funding(funding, first_ms + j * DAY_MS) if inside[j] else _NAN for j in range(days)])
    return SymbolData(series.symbol, close, index, ret, paid)


def _segment(values, data: SymbolData, fill=_NAN) -> np.ndarray:
    values = np.asarray(values)
    return np.where(data.index >= 0, values[np.maximum(data.index, 0)], fill)


@dataclass
class Leg:
    weight: np.ndarray    # (T,) or (T, m)
    previous: np.ndarray  # weight of the day before
    active: np.ndarray    # bool, same shape


def real_path(variant: TrendVariant, data: SymbolData, *, constant_signal: bool = False) -> tuple[Leg, SignalInputs]:
    """The variant's weight path over the whole history, sliced to the segment (``constant_signal``:
    the volatility-targeted buy-and-hold control, S = 1, same sizing, band and costs, no filter)."""
    inputs = signal_inputs(variant, data.close)
    if constant_signal:
        inputs = SignalInputs(np.where(np.isnan(inputs.sigma), _NAN, 1.0), inputs.sigma, None)
    target = size(inputs.signal, inputs.sigma, variant.target)
    weights = apply_band(target, inputs.block)
    previous_full = np.concatenate([[0.0], weights[:-1]])
    weight = _segment(weights, data, 0.0)
    previous = _segment(previous_full, data, 0.0)
    defined = _segment(~np.isnan(target), data, False).astype(bool)
    return Leg(weight, previous, data.evaluable & (defined | (previous != 0))), inputs


def net(leg: Leg, data: SymbolData, multiplier: int = 1, part: str = "both") -> np.ndarray:
    """Day net per symbol (fraction of equity); ``part`` long/short keeps the positive/negative part of w."""
    weight, previous = leg.weight, leg.previous
    if part == "long":
        weight, previous = np.maximum(weight, 0.0), np.maximum(previous, 0.0)
    elif part == "short":
        weight, previous = np.minimum(weight, 0.0), np.minimum(previous, 0.0)
    ret, paid = data.ret, data.funding
    if weight.ndim == 2:
        ret, paid = ret[:, None], paid[:, None]
    with np.errstate(invalid="ignore"):
        value = weight * ret - multiplier * _COST * np.abs(weight - previous) - weight * paid
    return np.where(leg.active, value, 0.0)


def portfolio(values, actives) -> np.ndarray:
    """Equal-weight mean over the active symbols of each day (0 on a day without one)."""
    total = sum(np.where(active, value, 0.0) for value, active in zip(values, actives))
    count = sum(active.astype(np.int64) for active in actives)
    return np.where(count > 0, total / np.maximum(count, 1), 0.0)


def to_ppm(values) -> list[int]:
    """Daily fractions of equity -> integer parts per million (round half to even)."""
    return [int(value) for value in np.rint(np.asarray(values, dtype=np.float64) * PPM).astype(np.int64)]


# ---------------------------------------------------------------- statistics and controls


def stream_stats(daily: list[int]) -> dict:
    T = len(daily)
    values = np.asarray(daily, dtype=np.float64) / PPM
    mean = Fraction(sum(daily), T * PPM) if T else None
    sd = float(values.std(ddof=1)) if T > 1 else None
    sharpe = float(values.mean()) / sd * sqrt(ANNUALIZATION) if sd else None
    worst, underwater = drawdown(daily)
    return {"T_days": T, "mean_daily": decimal_text(mean), "sum": decimal_text(Fraction(sum(daily), PPM)),
            "sd_daily": sd, "sharpe_annualized": sharpe, "max_drawdown": decimal_text(Fraction(worst, PPM)),
            "longest_underwater_days": underwater}


def newey_west_alpha(y, x, lag: int = NW_LAG) -> dict:
    """OLS y = a + b x + e with Newey-West (Bartlett, ``lag``) standard errors; None when x is constant."""
    y, x = np.asarray(y, dtype=np.float64), np.asarray(x, dtype=np.float64)
    T = len(y)
    empty = {"alpha_daily": None, "beta": None, "se_alpha": None, "t_alpha": None, "lag": lag, "T_days": T}
    if T <= lag + 2 or not np.ptp(x) > 0:
        return empty
    X = np.column_stack([np.ones(T), x])
    inverse = np.linalg.inv(X.T @ X)
    beta = inverse @ (X.T @ y)
    scores = X * (y - X @ beta)[:, None]
    omega = scores.T @ scores
    for l in range(1, lag + 1):
        gamma = scores[l:].T @ scores[:-l]
        omega += (1.0 - l / (lag + 1)) * (gamma + gamma.T)
    covariance = inverse @ omega @ inverse
    se = sqrt(covariance[0, 0]) if covariance[0, 0] > 0 else None
    return {"alpha_daily": float(beta[0]), "beta": float(beta[1]), "se_alpha": se,
            "t_alpha": float(beta[0]) / se if se else None, "lag": lag, "T_days": T}


def shift_offsets(T: int, n_shifts: int = N_SHIFTS, seed: int = SEED) -> np.ndarray:
    """Circular shifts k in [30, T - 30] from rng.u64_words (empty when T < 60)."""
    span = T - 2 * MIN_SHIFT + 1
    if span <= 0:
        return np.zeros(0, dtype=np.int64)
    words = u64_words(seed, "trend-placebo-shift", 0, n_shifts)
    return (MIN_SHIFT + (words % np.uint64(span))).astype(np.int64)


def placebo_sums(variant: TrendVariant, symbols: dict, legs: dict, plans: dict, offsets) -> np.ndarray:
    """Total 1x portfolio ppm of each circular shift of the whole signal series (weights recomputed)."""
    if not len(offsets):
        return np.zeros(0, dtype=np.int64)
    values, actives = [], []
    for symbol, data in symbols.items():
        T = len(data.index)
        signal = _segment(plans[symbol].signal, data)
        sigma = _segment(plans[symbol].sigma, data)
        rolled = signal[(np.arange(T)[:, None] - offsets[None, :]) % T]
        target = size(rolled, sigma[:, None], variant.target)
        block = None if plans[symbol].block is None else _segment(plans[symbol].block, data, False).astype(bool)
        start = legs[symbol].previous[0]
        weight = apply_band(target, None if block is None else block[:, None], start)
        previous = np.vstack([np.full((1, len(offsets)), start), weight[:-1]])
        active = data.evaluable[:, None] & (~np.isnan(target) | (previous != 0))
        leg = Leg(weight, previous, active)
        values.append(net(leg, data))
        actives.append(active)
    return np.rint(portfolio(values, actives) * PPM).astype(np.int64).sum(axis=0)


def _month_labels(first_ms: int, T: int) -> list[str]:
    return [datetime.fromtimestamp((first_ms + j * DAY_MS) // 1000, tz=timezone.utc).strftime("%Y-%m")
            for j in range(T)]


def without_best_month(daily: list[int], months: list[str]) -> dict:
    totals = {}
    for value, month in zip(daily, months):
        totals[month] = totals.get(month, 0) + value
    best = max(sorted(totals), key=lambda month: totals[month]) if totals else None  # earliest on ties
    rest = [value for value, month in zip(daily, months) if month != best]
    mean = Fraction(sum(rest), len(rest) * PPM) if rest else None
    return {"best_month": best, "best_month_sum": decimal_text(Fraction(totals[best], PPM)) if best else None,
            "T_days": len(rest), "mean_daily": decimal_text(mean)}


def verdict(*, stepm_rejected: bool, ci_lower, t_statistic, required_t, mean_1x, mean_2x, mean_ex_best,
            p_placebo, alpha_t) -> str:
    """Trend Mode B verdict (documented in docs/trend-mode-b.md).

    Core conditions: StepM rejection, bootstrap CI lower bound > 0, bootstrap t >= required_t,
    placebo p <= 0.05 and alpha-vs-buy-and-hold t >= 2. PASS needs the core conditions, a
    positive 1x mean, a positive mean at 2x cost and a positive mean without the best calendar
    month. FRAGILE: positive 1x mean and the core conditions hold, so only 2x cost or the removal
    of the best month fails. Otherwise FAIL. Means are exact (Fraction or decimal text).
    """
    def positive(value) -> bool:
        return value is not None and Fraction(value) > 0

    core = (bool(stepm_rejected) and positive(ci_lower)
            and t_statistic is not None and float(t_statistic) >= float(required_t)
            and p_placebo is not None and float(p_placebo) <= PLACEBO_ALPHA
            and alpha_t is not None and float(alpha_t) >= ALPHA_T_MIN)
    if not (core and positive(mean_1x)):
        return "FAIL"
    return "PASS" if positive(mean_2x) and positive(mean_ex_best) else "FRAGILE"


# ---------------------------------------------------------------- loading and evaluation


def load_inputs(daily_dir, segment: str, symbols=data_lake.SYMBOLS) -> dict:
    """Guarded loader: the hidden guard runs before any file is opened; dk1 2020-01..segment end only."""
    months = check_segment(segment)
    first_ms, end_ms = segment_bounds_ms(segment)
    out = {}
    for symbol in symbols:
        series = load_symbol_daily(daily_dir, symbol, WARMUP_FIRST_MONTH, months[-1])
        funding = load_symbol_funding_range(daily_dir, symbol, WARMUP_FIRST_MONTH, months[-1])
        out[symbol] = prepare_symbol(series, funding, first_ms, end_ms)
    return out


def _render(value):
    """Floats -> repr (non-finite -> None), numpy scalars unwrapped, recursively (canonical JSON types)."""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return repr(value) if isfinite(value) else None
    if isinstance(value, dict):
        return {key: _render(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_render(item) for item in value]
    return value


def evaluate_variant(variant: TrendVariant, symbols: dict, months: list[str], offsets, *, stream: str, B: int,
                     seed: int, controls: dict) -> tuple[dict, list[int]]:
    legs, plans = {}, {}
    for symbol, data in symbols.items():
        legs[symbol], plans[symbol] = real_path(variant, data)
    actives = [legs[symbol].active for symbol in symbols]

    def stream_of(multiplier=1, part="both") -> list[int]:
        return to_ppm(portfolio([net(legs[s], symbols[s], multiplier, part) for s in symbols], actives))

    daily, daily_2x = stream_of(), stream_of(2)
    T = len(daily)
    sd = float(np.std(np.asarray(daily, dtype=np.float64) / PPM, ddof=1)) if T > 1 else 0.0
    mu_min = min_detectable_edge_per_day(T, sd) if T else None  # recorded before any verdict
    ci = bootstrap_ci(daily, B=B, seed=seed, stream_prefix=stream + "/ci")
    key = (variant.sizing, variant.sigma_target)
    if key not in controls:
        bh_legs = {symbol: real_path(variant, data, constant_signal=True)[0] for symbol, data in symbols.items()}
        bh_actives = [bh_legs[symbol].active for symbol in symbols]
        controls[key] = to_ppm(portfolio([net(bh_legs[s], symbols[s]) for s in symbols], bh_actives))
    buy_hold = controls[key]
    alpha = newey_west_alpha(np.asarray(daily) / PPM, np.asarray(buy_hold) / PPM)
    sums = placebo_sums(variant, symbols, legs, plans, offsets)
    p_placebo = float(np.count_nonzero(sums >= sum(daily)) / len(sums)) if len(sums) else None
    turnover = portfolio([np.abs(legs[s].weight - legs[s].previous) for s in symbols], actives)
    paid = portfolio([np.where(legs[s].active, legs[s].weight * np.nan_to_num(symbols[s].funding), 0.0)
                      for s in symbols], actives)
    per_symbol = {}
    for symbol, data in symbols.items():
        mask = legs[symbol].active
        per_symbol[symbol] = {"days": int(mask.sum()),
                              **stream_stats(to_ppm(net(legs[symbol], data)[mask]))}
    entry = {
        "variant": variant.name, "config": variant.config(symbols), "T_days": T,
        "symbols_defined": sum(1 for symbol in symbols if legs[symbol].active.any()),
        "symbol_days": int(sum(active.sum() for active in actives)),
        "power": {"mu_min_daily": mu_min, "sigma_daily": sd},
        "net_1x": stream_stats(daily), "net_2x": stream_stats(daily_2x), "bootstrap_ci": ci,
        "turnover_per_year": float(turnover.mean()) * ANNUALIZATION if T else None,
        "funding_drag": {"mean_daily": float(paid.mean()) if T else None,
                         "annualized": float(paid.mean()) * ANNUALIZATION if T else None},
        "controls": {"buy_and_hold": {"sizing": variant.sizing, "sigma_target": variant.sigma_target,
                                      **stream_stats(buy_hold)},
                     "alpha_vs_buy_and_hold": alpha,
                     "placebo": {"shifts": len(sums), "p": p_placebo, "min_shift": MIN_SHIFT,
                                 "stream": "trend-placebo-shift"},
                     "long_leg": stream_stats(stream_of(part="long")),
                     "short_leg": stream_stats(stream_of(part="short")),
                     "without_best_month": without_best_month(daily, months)},
        "per_symbol": per_symbol}
    return entry, daily


def evaluate_trend(symbols: dict, segment: str, log, *, code_commit: str, data_snapshot_id: str, now_utc: str,
                   B: int = B_STATS, n_shifts: int = N_SHIFTS, seed: int = SEED, extra: dict | None = None,
                   progress=NO_PROGRESS) -> dict:
    """Evaluate the K = 9 variants on one segment, append one ledger record per variant, return the report."""
    check_segment(segment)  # hidden refused even when inputs were built elsewhere
    if not symbols:
        raise ValueError("need at least one symbol")
    first_ms, end_ms = segment_bounds_ms(segment)
    T = (end_ms - first_ms) // DAY_MS
    if any(len(data.index) != T for data in symbols.values()):
        raise ValueError("symbol data was prepared for another segment")
    months = _month_labels(first_ms, T)
    offsets = shift_offsets(T, n_shifts, seed)
    templates = []
    for variant in VARIANTS:
        templates.append(TrialRecord(
            question_id=QUESTION_ID, family_id=FAMILY_ID, strategy_id=variant.name,
            strategy_version=STRATEGY_VERSION, config=variant.config(symbols), split_id=segment,
            data_snapshot_id=data_snapshot_id, code_commit=code_commit, status=TrialStatus.OK, counts_toward_n=True,
            count_reason="variant evaluated", result_hash=None, result_summary={}, created_utc=now_utc))
    ids = [record.trial_id for record in templates]
    n_trials = max(K, len(set(log.counted_trial_ids(question_id=QUESTION_ID)) | set(ids)))
    threshold = required_t(n_trials)
    entries, columns, controls = [], [], {}
    with progress.stage("evaluate", total=K) as set_variant:
        for index, (variant, trial_id) in enumerate(zip(VARIANTS, ids), 1):
            set_variant(index)
            progress.phase("evaluate", variant=index, of=K)
            entry, daily = evaluate_variant(variant, symbols, months, offsets, stream=f"trend/{trial_id}", B=B,
                                            seed=seed, controls=controls)
            entry["trial_id"] = trial_id
            entries.append(entry)
            columns.append(daily)
    matrix = np.column_stack([np.asarray(daily, dtype=np.int64) for daily in columns])
    joint = "trend/" + content_hash([QUESTION_ID, segment, ids]) + "/joint"
    with progress.stage("spa bootstrap", total=1):
        spa = spa_test(matrix, B=B, seed=seed, stream_prefix=joint)
    with progress.stage("step-down", total=2):
        step = stepm(matrix, B=B, seed=seed, stream_prefix=joint)
        adjusted = stepm_p_values(matrix, B=B, seed=seed, stream_prefix=joint)
    nonconstant = sum(len(set(daily)) > 1 for daily in columns)
    if nonconstant >= 2:
        dsr = best_trial_dsr(matrix, n_trials=n_trials).to_record()
    else:
        dsr = {"best_index": None, "n_trials": n_trials, "dsr_raw": None, "dsr_effective": None,
               "reason": "need at least 2 non-constant trials"}
    records = []
    for index, (entry, template) in enumerate(zip(entries, templates)):
        controls_ = entry["controls"]
        entry["required_t"] = threshold
        entry["stepm_rejected"] = index in step.rejected
        entry["stepm_p_value"] = decimal_text(adjusted[index])
        entry["verdict"] = verdict(
            stepm_rejected=entry["stepm_rejected"], ci_lower=entry["bootstrap_ci"]["lower"],
            t_statistic=entry["bootstrap_ci"]["t_statistic"], required_t=threshold,
            mean_1x=entry["net_1x"]["mean_daily"], mean_2x=entry["net_2x"]["mean_daily"],
            mean_ex_best=controls_["without_best_month"]["mean_daily"], p_placebo=controls_["placebo"]["p"],
            alpha_t=controls_["alpha_vs_buy_and_hold"]["t_alpha"])
        rendered = _render(entry)
        entries[index] = rendered
        summary = {"T_days": rendered["T_days"], "mean_daily": rendered["net_1x"]["mean_daily"] or "undefined",
                   "t_stat": rendered["bootstrap_ci"]["t_statistic"] or "undefined",
                   "required_t": repr(threshold), "p_placebo": rendered["controls"]["placebo"]["p"] or "undefined",
                   "alpha_t": rendered["controls"]["alpha_vs_buy_and_hold"]["t_alpha"] or "undefined",
                   "B_stats": B, "n_shifts": len(offsets), "verdict": rendered["verdict"]}
        records.append(replace(template, result_summary=summary, result_hash=content_hash(rendered)))
    log.append_many(records)
    for index, entry in enumerate(entries):
        is_best = dsr["best_index"] == index
        entry["dsr"] = {"scope": "best trial only", "is_best": is_best,
                        "raw": dsr["dsr_raw"] if is_best else None,
                        "effective": dsr["dsr_effective"] if is_best else None}
    return build_report(segment, entries, spa=spa.to_record(),
                        stepm={**step.to_record(), "adjusted_p_values": [decimal_text(v) for v in adjusted]},
                        dsr=_render(dsr), n_trials=n_trials, required_t=threshold, B=B, n_shifts=len(offsets),
                        seed=seed, code_commit=code_commit, data_snapshot_id=data_snapshot_id, created_utc=now_utc,
                        extra=extra)


# ---------------------------------------------------------------- report


def build_report(segment: str, variants: list, *, spa: dict, stepm: dict, dsr: dict, n_trials: int, required_t,
                 B: int, n_shifts: int, seed: int, code_commit: str, data_snapshot_id: str, created_utc: str,
                 extra: dict | None = None) -> dict:
    report = {"schema": SCHEMA, "question_id": QUESTION_ID, "segment": segment, "K": K, "n_trials": n_trials,
              "required_t": _render(float(required_t)), "B_stats": B, "n_shifts": n_shifts, "seed": seed,
              "cost_bp_per_unit_turnover": COST_BP_PER_UNIT_TURNOVER, "variants": variants, "spa": spa,
              "stepm": stepm, "best_trial_dsr": dsr, "code_commit": code_commit,
              "data_snapshot_id": data_snapshot_id, "created_utc": created_utc, "inputs": _render(extra or {}),
              "notes": ["Mode B only: daily portfolio return stream; Mode A (trade level) and 4h variants are not "
                        "in v1, and there is no catastrophe stop (daily bars cannot place it honestly).",
                        "mu_min (minimum detectable mean daily return at alpha 0.05, power 0.8) is recorded "
                        "before the verdict and should be read first.",
                        "Daily values are integer parts per million of equity; mean_daily is a return fraction."]}
    report["report_hash"] = content_hash(report)
    return report


def check_report(report: dict) -> None:
    body = {key: value for key, value in report.items() if key != "report_hash"}
    if report.get("report_hash") != content_hash(body):
        raise ValueError("report_hash mismatch")


def report_paths(report: dict) -> tuple[str, str]:
    stem = f"reports/trend/{report['segment']}__{report['report_hash'][:16]}"
    return stem + ".json", stem + ".md"


def public_lines(report: dict) -> list[str]:
    """Public log: variant name, segment, T days, defined symbols and the report hash. No outcomes."""
    check_report(report)
    lines = [f"trend mode B segment {report['segment']} K={int(report['K'])}"]
    for entry in report["variants"]:
        lines.append(f"{entry['variant']} segment={report['segment']} T={int(entry['T_days'])} "
                     f"symbols={int(entry['symbols_defined'])}")
    lines.append(f"report hash {report['report_hash']}")
    return lines


def _cell(value) -> str:
    return "n/a" if value is None else str(value)


def markdown(report: dict) -> str:
    check_report(report)
    lines = [f"# Trend Mode B ({report['segment']})", "",
             f"Report hash `{report['report_hash']}`, code `{report['code_commit']}`, created "
             f"{report['created_utc']}. K = {report['K']}, question-wide trials {report['n_trials']}, "
             f"required t {report['required_t']}, B = {report['B_stats']}, placebo shifts {report['n_shifts']}.", ""]
    lines += [f"- {note}" for note in report["notes"]] + [""]
    lines += ["## Power first", "", "| variant | T | symbols | sigma daily | mu_min daily |", "| --- | --- | --- | --- | --- |"]
    for entry in report["variants"]:
        lines.append(f"| {entry['variant']} | {entry['T_days']} | {entry['symbols_defined']} | "
                     f"{_cell(entry['power']['sigma_daily'])} | {_cell(entry['power']['mu_min_daily'])} |")
    lines += ["", "## Results", "",
              "| variant | mean/day 1x | mean/day 2x | 95% CI | t | Sharpe ann. | max DD | turnover/yr | funding/yr | "
              "placebo p | alpha t | ex-best-month mean | StepM p | verdict |", "|" + " --- |" * 14]
    for entry in report["variants"]:
        c = entry["controls"]
        lines.append(
            f"| {entry['variant']} | {_cell(entry['net_1x']['mean_daily'])} | {_cell(entry['net_2x']['mean_daily'])} "
            f"| {_cell(entry['bootstrap_ci']['lower'])} .. {_cell(entry['bootstrap_ci']['upper'])} "
            f"| {_cell(entry['bootstrap_ci']['t_statistic'])} | {_cell(entry['net_1x']['sharpe_annualized'])} "
            f"| {_cell(entry['net_1x']['max_drawdown'])} | {_cell(entry['turnover_per_year'])} "
            f"| {_cell(entry['funding_drag']['annualized'])} | {_cell(c['placebo']['p'])} "
            f"| {_cell(c['alpha_vs_buy_and_hold']['t_alpha'])} | {_cell(c['without_best_month']['mean_daily'])} "
            f"({_cell(c['without_best_month']['best_month'])} removed) | {_cell(entry['stepm_p_value'])} "
            f"| **{entry['verdict']}** |")
    lines += ["", "## Controls", "", "| variant | buy-and-hold mean/day | B&H Sharpe | long leg mean/day | "
              "short leg mean/day |", "| --- | --- | --- | --- | --- |"]
    for entry in report["variants"]:
        c = entry["controls"]
        lines.append(f"| {entry['variant']} | {_cell(c['buy_and_hold']['mean_daily'])} | "
                     f"{_cell(c['buy_and_hold']['sharpe_annualized'])} | {_cell(c['long_leg']['mean_daily'])} | "
                     f"{_cell(c['short_leg']['mean_daily'])} |")
    lines += ["", f"SPA p (consistent) {report['spa']['p_consistent']}; best-trial DSR "
              f"{_cell(report['best_trial_dsr'].get('dsr_raw'))} (raw), "
              f"{_cell(report['best_trial_dsr'].get('dsr_effective'))} (effective).",
              "", "Per-symbol streams (diagnostics) are in the JSON report.", ""]
    return "\n".join(lines) + "\n"
