"""Sigma calibration audit for the #182 label engine (#220 slice A). Read-only.

Nothing here writes labels or changes the label engine: the audit recomputes the
label engine's own point-in-time sigma (``volatility.build_variance`` +
``horizon_sigma``, integers as in the engine) for the label-step entries of a
development or validation segment and asks whether it is correctly scaled.

Entries (exactly the label store's grid for the horizon): signal times
``signal_ms % (step * 60_000) == 0`` inside the segment whose worst-case window
``signal_ms + time_limit_multiple * h`` minutes still ends inside it
(``segments.eligible``) and fits in the loaded bars. Entry minute ``e`` is the
minute opening at ``signal_ms`` (the signal is the end of minute ``e - 1``); sigma
comes from the 5-minute block ending at minute ``e - 1``. An entry is used only
when that variance is published (non-zero), and both ``open[e]`` and ``open[e + h]``
are present in minutes without a ``COMPROMISED_FLAGS`` bit; skipped entries are
counted by reason.

Per symbol, horizon ``h`` in {15, 60, 240} minutes and EWMA half-life in {1, 3, 7} days:

- ``z = ln(open[e + h] / open[e]) / sigma_h`` (``sigma_h = horizon_sigma / 10**20``):
  n, mean, sd, mean |z|, quantiles 1/5/25/50/75/95/99 % and the shares with
  |z| > 1, 2, 3; the same per UTC hour of the signal time (24 rows).
- Variance ratio ``VR(q) = Var(r_q) / (q Var(r_1))`` of 5-minute block log returns
  (a return exists only between two valid consecutive block closes, the
  volatility.py rule; ``r_q`` are overlapping sums of q consecutive returns that
  all exist), q in {3, 12, 48}, on the blocks whose close lies in the segment.
- VR-corrected sigma ``sigma_h * sqrt(VR(h / 5))`` with VR estimated on an
  expanding window from the first loaded block up to the signal's block (point in
  time), published after ``VR_MIN_RETURNS`` q-sums; same z statistics.

Barrier theory (``BARRIER_HORIZON`` = 240 m, from the lb1 labels of the same
entries; the labels' own half-life for that horizon): shares of outcomes T, S, E,
L, X per k in {1, 2} and rr in the label grid (non-trade statuses and purged rows
counted separately) next to the driftless Brownian values of SPEC-1 section 4,
continuous and with the Broadie-Glasserman-Kou widening ``0.5826 sqrt(step / h)``,
plus the realized/predicted sigma ratio that reproduces the observed expiry share.

Monitoring step (P16, #220): ``scan.label_trade`` checks every 1-minute bar's high and low, i.e.
quasi-continuous monitoring, so the discrete-monitoring (BGK) widening uses the 1-minute
MONITORING step, ``0.5826 sqrt(1 / h)`` (0.0376 sigma_h at 240 m), not the label step. The old
widening for the 15-minute label step (``0.5826 sqrt(15 / 240)`` = 0.1457) and every verdict built
on it are kept in the JSON under their old names for traceability (``expiry_widened``,
``implied_sigma_ratio_widened``, ``expiry_within_tolerance``, ``pass_v1``, ``barrier_ok_v1``).

Barrier criterion (P16, pre-stated, applied identically to every sigma model, no tuning):
(a) target-first share T/(T+S) within TARGET_FIRST_TOLERANCE (0.02) of b/(a+b) for every
(k, rr, side) with k in {1, 2}; (b) for k = 1, the realized/predicted sigma ratio implied by the
pooled expiry share against the monitoring-step theory within IMPLIED_RATIO_BAND [0.85, 1.15] for
every rr; (c) for k = 2 the implied ratio is reported and marked DESCRIPTIVE: far barriers are hit
more often than Brownian motion predicts (heavy tails, volatility clustering), which no scalar sigma
can fix, so it is documented, not failed. ``barrier_ok`` = (a) and (b) on 240 m rows (None for other
horizons or when no labels were read). ``pass`` = (``sd_ok`` or ``robust_ok``) and, where set,
``barrier_ok``.

Sigma models (#220 P8): every (horizon, half-life) row exists per sigma model, ``ewma`` (rows under
``horizons``, the lb1 engine) and ``ewma-seasonal`` (rows under ``horizons_seasonal``: variance from
``build_variance_deseasonalised`` and horizon sigma from ``horizon_sigma_seasonal`` with the entry
day's factors, the lb2 engine; an entry whose day has no factors counts as ``no_sigma``). The 240 m
barrier check of a model reads labels built with that model (lb1 / lb2) and is NA when they are
absent.

Robust statistics in every z block: ``robust_sd = 1.4826 * MAD(z)`` and ``mean_abs_ratio =
mean|z| / 0.797885`` (both 1 for a standard normal). ``robust_ok``: both in ROBUST_BAND overall
and in ROBUST_HOUR_BAND in every one of the 24 UTC hours (an empty hour fails). The pre-P16 rule
(``sd_ok`` and ``robust_ok`` and the label-step barrier check) is kept as ``pass_v1``.

Floats are only statistics: the report renders them as fixed 6-decimal strings,
so it stays canonical JSON (``canonical.canonical_bytes``) and hashable.
"""
from __future__ import annotations

from dataclasses import replace
from math import exp, isfinite, log, pi, sin, sqrt

import numpy as np

from .. import data_lake
from .bars import COMPROMISED_FLAGS, MISSING, BarSeries
from .canonical import content_hash, exact_to_str
from .hidden_guard import require_months
from .label_store import Geometry, load_geometry_columns
from .labels import LabelParams
from .market_data import load_symbol_bars
from .robust_sigma import ROBUST_MODELS, RobustSigma
from .segments import SEGMENTS, segment_bounds_ms, segment_months
from .volatility import (BLOCK_MINUTES, BLOCK_MS, BLOCKS_PER_DAY, DAY_MS, VAR_SCALE, build_variance,
                         build_variance_deseasonalised, horizon_sigma, horizon_sigma_seasonal, seasonal_factors)

SCHEMA = "calibration-v2"
AUDIT_SEGMENTS = ("development", "validation")
HORIZONS = (15, 60, 240)
HALF_LIVES = (1, 3, 7)
VR_QS = (3, 12, 48)
QUANTILES = (1, 5, 25, 50, 75, 95, 99)
Z_THRESHOLDS = (1, 2, 3)
SD_BAND = (0.9, 1.1)
ROBUST_BAND = (0.9, 1.1)
ROBUST_HOUR_BAND = (0.8, 1.2)
MAD_TO_SD = 1.4826
MEAN_ABS_NORMAL = 0.797885       # E|Z| = sqrt(2 / pi)
SIGMA_MODELS = ("ewma", "ewma-seasonal", "ewma-robust", "ewma-robust-hcal")
MODEL_KEYS = {"ewma": ("horizons", "barriers"), "ewma-seasonal": ("horizons_seasonal", "barriers_seasonal"),
              "ewma-robust": ("horizons_robust", "barriers_robust"),
              "ewma-robust-hcal": ("horizons_robust_hcal", "barriers_robust_hcal")}
# Barrier expiry cell tolerance: |observed - theory| <= max(REL * theory, ABS). The 10 % relative band
# alone is about 0.1-0.3 percentage points on low-expiry cells (k 1, rr 1), below sampling noise and
# the discrete-monitoring effect; the 2 percentage point floor keeps a calibrated sigma from failing there.
EXPIRY_TOLERANCE_REL = 0.10
EXPIRY_TOLERANCE_ABS = 0.02
BARRIER_HORIZON = 240
BARRIER_K = (1, 2)
BGK_BETA = 0.5826                # -zeta(1/2) / sqrt(2 pi), Broadie-Glasserman-Kou
MONITORING_STEP_MIN = 1          # scan.label_trade checks every 1-minute bar's high/low
TARGET_FIRST_TOLERANCE = 0.02    # criterion (a): |T/(T+S) - b/(a+b)| <= 0.02 per (k, rr, side)
IMPLIED_RATIO_BAND = (0.85, 1.15)  # criterion (b): k = 1 implied sigma ratio vs monitoring-step theory
JUDGED_K = (1,)                  # k = 2 implied ratios are descriptive (criterion c)
VR_MIN_RETURNS = 7 * BLOCKS_PER_DAY
IMPLIED_RATIO_RANGE = (0.05, 20.0)
OUTCOME_CODES = ("T", "S", "E", "L", "X")
_MINUTE_MS = data_lake.MINUTE_MS
_HOUR_MS = 3_600_000


def check_segment(segment: str) -> list[str]:
    """The segment's months, after the hidden guard (no token: hidden is refused)."""
    if segment not in SEGMENTS:
        raise ValueError(f"segment must be one of {AUDIT_SEGMENTS}")
    months = segment_months(segment)
    require_months(months, None, None)  # raises HiddenStretchLocked for the hidden stretch
    if segment not in AUDIT_SEGMENTS:
        raise ValueError(f"segment must be one of {AUDIT_SEGMENTS}")
    return months


def _subset(values, allowed, name: str) -> tuple:
    values = tuple(values)
    if not values or len(set(values)) != len(values) or any(type(v) is not int or v not in allowed for v in values):
        raise ValueError(f"{name} must be distinct values from {allowed}")
    return tuple(sorted(values))


# ---------------------------------------------------------------- statistics


def z_stats(z) -> dict:
    """n, mean, sd (ddof 1), mean |z|, quantiles and |z| exceedance shares (floats; NaN when undefined)."""
    z = np.asarray(z, dtype=float)
    n = int(z.size)
    nan = float("nan")
    if n == 0:
        return {"n": 0, "mean": nan, "sd": nan, "mean_abs": nan, "robust_sd": nan, "mean_abs_ratio": nan,
                "quantiles": {str(p): nan for p in QUANTILES}, "share_abs_gt": {str(t): nan for t in Z_THRESHOLDS}}
    magnitude = np.abs(z)
    return {"n": n, "mean": float(z.mean()), "sd": float(z.std(ddof=1)) if n > 1 else nan,
            "mean_abs": float(magnitude.mean()),
            "robust_sd": MAD_TO_SD * float(np.median(np.abs(z - np.median(z)))),
            "mean_abs_ratio": float(magnitude.mean()) / MEAN_ABS_NORMAL,
            "quantiles": {str(p): float(q) for p, q in zip(QUANTILES, np.quantile(z, [p / 100 for p in QUANTILES]))},
            "share_abs_gt": {str(t): float((magnitude > t).mean()) for t in Z_THRESHOLDS}}


def z_stats_by_hour(z, hours) -> list[dict]:
    """24 rows of z_stats by UTC hour 0..23."""
    z, hours = np.asarray(z, dtype=float), np.asarray(hours)
    return [{"hour": hour, **z_stats(z[hours == hour])} for hour in range(24)]


def expiry_within_tolerance(observed: float, theory: float) -> bool:
    """|observed - theory| <= max(EXPIRY_TOLERANCE_REL * theory, EXPIRY_TOLERANCE_ABS); False when undefined."""
    return isfinite(observed) and abs(observed - theory) <= max(EXPIRY_TOLERANCE_REL * theory,
                                                                 EXPIRY_TOLERANCE_ABS)


def sd_ok(stats: dict) -> bool:
    return isfinite(stats["sd"]) and SD_BAND[0] <= stats["sd"] <= SD_BAND[1]


def _robust_within(stats: dict, band) -> bool:
    return all(isfinite(stats[name]) and band[0] <= stats[name] <= band[1] for name in ("robust_sd", "mean_abs_ratio"))


def robust_ok(z: dict, by_hour) -> bool:
    """Robust sd and mean|z|/0.7979 in ROBUST_BAND overall and in ROBUST_HOUR_BAND in every UTC hour."""
    return _robust_within(z, ROBUST_BAND) and len(by_hour) == 24 and all(
        _robust_within(hour, ROBUST_HOUR_BAND) for hour in by_hour)


# ---------------------------------------------------------------- variance ratio


def block_log_returns(series: BarSeries) -> np.ndarray:
    """Per 5-minute block: ln(c_j / c_{j-1}) when both block closes are valid, else NaN (volatility.py rule)."""
    blocks = series.minutes // BLOCK_MINUTES
    close = np.frombuffer(series.close, dtype=np.int64)
    flags = np.frombuffer(series.flags, dtype=np.uint16)
    minute = BLOCK_MINUTES * np.arange(blocks) + BLOCK_MINUTES - 1
    price = close[minute]
    valid = (price != MISSING) & ((flags[minute] & COMPROMISED_FLAGS) == 0)
    level = np.where(valid, np.log(np.where(valid, price, 1).astype(float)), np.nan)
    returns = np.full(blocks, np.nan)
    returns[1:] = level[1:] - level[:-1]
    return returns


def q_sums(returns, q: int) -> np.ndarray:
    """r_q[j] = r[j-q+1] + ... + r[j] when all q returns exist, else NaN (overlapping)."""
    returns = np.asarray(returns, dtype=float)
    if type(q) is not int or q < 1:
        raise ValueError("q must be a positive int")
    finite = np.isfinite(returns)
    total = np.concatenate(([0.0], np.cumsum(np.where(finite, returns, 0.0))))
    count = np.concatenate(([0], np.cumsum(finite)))
    out = np.full(returns.size, np.nan)
    if returns.size >= q:
        window = total[q:] - total[:-q]
        full = (count[q:] - count[:-q]) == q
        out[q - 1:] = np.where(full, window, np.nan)
    return out


def variance_ratio(returns, q: int) -> dict:
    """Mean-adjusted VR(q) = Var(r_q) / (q Var(r_1)) over all existing returns and q-sums."""
    returns = np.asarray(returns, dtype=float)
    one = returns[np.isfinite(returns)]
    sums = q_sums(returns, q)
    sums = sums[np.isfinite(sums)]
    result = {"q": q, "n_returns": int(one.size), "n_q_sums": int(sums.size), "vr": float("nan")}
    if one.size < 2 or sums.size < 2:
        return result
    mu = one.mean()
    var_one = float(((one - mu) ** 2).mean())
    if var_one > 0:
        result["vr"] = float(((sums - q * mu) ** 2).mean()) / (q * var_one)
    return result


def expanding_variance_ratio(returns, q: int, min_q_sums: int = VR_MIN_RETURNS) -> np.ndarray:
    """VR(q) at every block j from returns and q-sums ending at or before j only (point in time).

    NaN until ``min_q_sums`` q-sums exist. Same mean-adjusted estimator as ``variance_ratio``.
    """
    returns = np.asarray(returns, dtype=float)
    f1 = np.isfinite(returns)
    x = np.where(f1, returns, 0.0)
    n1, s1, sq1 = np.cumsum(f1), np.cumsum(x), np.cumsum(x * x)
    sums = q_sums(returns, q)
    fq = np.isfinite(sums)
    y = np.where(fq, sums, 0.0)
    nq, sq, sqq = np.cumsum(fq), np.cumsum(y), np.cumsum(y * y)
    with np.errstate(divide="ignore", invalid="ignore"):
        mu = s1 / n1
        var_one = sq1 / n1 - mu * mu
        var_q = sqq / nq - 2 * q * mu * sq / nq + (q * mu) ** 2
        vr = var_q / (q * var_one)
    return np.where((nq >= min_q_sums) & (n1 >= 2) & (var_one > 0), vr, np.nan)


# ---------------------------------------------------------------- z audit


def entry_indices(series: BarSeries, first_ms: int, end_ms: int, horizon: int, params: LabelParams) -> np.ndarray:
    """Entry minutes e of the label grid inside [first_ms, end_ms): window eligible and inside the bars."""
    step_ms = params.step(horizon) * _MINUTE_MS
    window = params.window(horizon)
    low = max(first_ms, series.start_ms + _MINUTE_MS)  # d = e - 1 >= 0
    low += -low % step_ms
    signals = np.arange(low, end_ms, step_ms, dtype=np.int64)
    signals = signals[signals + window * _MINUTE_MS < end_ms]  # segments.eligible: window end < segment end
    entries = (signals - series.start_ms) // _MINUTE_MS
    return entries[entries + window - 1 < series.minutes]


def _bad_minutes(series: BarSeries) -> np.ndarray:
    opens = np.frombuffer(series.open, dtype=np.int64)
    flags = np.frombuffer(series.flags, dtype=np.uint16)
    return (opens == MISSING) | ((flags & COMPROMISED_FLAGS) != 0)


def horizon_z(series: BarSeries, variance, entries, horizon: int, vr_expanding=None, factors=None,
              sigma_fn=None) -> dict:
    """z (and VR-corrected z) of the given entries for one horizon and one variance series.

    ``factors`` (a ``volatility.SeasonalSeries``) selects the seasonal horizon sigma with the entry day's
    factors; an entry without factors for its day counts as ``no_sigma``. ``sigma_fn(entry_ms) -> int | None``
    (the robust models) gives the horizon sigma directly; None counts as ``no_sigma``.
    """
    variance = np.frombuffer(variance, dtype=np.int64)
    opens = np.frombuffer(series.open, dtype=np.int64)
    bad = _bad_minutes(series)
    entries = np.asarray(entries, dtype=np.int64)
    block = entries // BLOCK_MINUTES - 1  # the 5-minute block ending at minute e - 1
    var = np.where(block >= 0, variance[np.maximum(block, 0)], MISSING)
    no_sigma = (var == MISSING) | (var <= 0)
    entry_ms = series.start_ms + entries * _MINUTE_MS
    if factors is not None:
        no_sigma |= np.array([factors.day_factors(int(ms) // DAY_MS) is None for ms in entry_ms], dtype=bool)
    direct = None
    if sigma_fn is not None:
        direct = [None if missing else sigma_fn(int(ms)) for missing, ms in zip(no_sigma, entry_ms)]
        no_sigma |= np.array([value is None for value in direct], dtype=bool)
    bad_entry = ~no_sigma & bad[entries]
    bad_exit = ~no_sigma & ~bad_entry & bad[entries + horizon]
    use = ~(no_sigma | bad_entry | bad_exit)
    e, b = entries[use], block[use]
    if direct is not None:
        sigma = np.array([value for value, ok in zip(direct, use) if ok], dtype=float) / VAR_SCALE
    elif factors is None:
        sigma = np.array([horizon_sigma(int(v), horizon) for v in var[use]], dtype=float) / VAR_SCALE
    else:
        sigma = np.array([horizon_sigma_seasonal(int(v), factors.day_factors(int(ms) // DAY_MS),
                                                 int(ms % DAY_MS) // BLOCK_MS, horizon)
                          for v, ms in zip(var[use], entry_ms[use])], dtype=float) / VAR_SCALE
    r = np.log(opens[e + horizon].astype(float) / opens[e].astype(float))
    z = r / sigma
    hours = ((series.start_ms + e * _MINUTE_MS) // _HOUR_MS) % 24
    result = {"skipped": {"no_sigma": int(no_sigma.sum()), "entry_compromised": int(bad_entry.sum()),
                          "exit_compromised": int(bad_exit.sum())},
              "z": z_stats(z), "z_by_hour": z_stats_by_hour(z, hours)}
    result["sd_ok"] = sd_ok(result["z"])
    result["robust_ok"] = robust_ok(result["z"], result["z_by_hour"])
    result["barrier_ok"] = None  # set by set_barrier_verdict on 240 m rows
    result["barrier_ok_v1"] = None
    result["pass"] = result["sd_ok"] or result["robust_ok"]
    result["pass_v1"] = result["sd_ok"] and result["robust_ok"]
    if vr_expanding is not None:
        ratio = vr_expanding[b]
        has = np.isfinite(ratio) & (ratio > 0)
        corrected = z[has] / np.sqrt(ratio[has])
        result["vr_corrected"] = {"q": horizon // BLOCK_MINUTES, "no_vr_estimate": int((~has).sum()),
                                  "z": z_stats(corrected), "z_by_hour": z_stats_by_hour(corrected, hours[has])}
        result["vr_corrected"]["sd_ok"] = sd_ok(result["vr_corrected"]["z"])
    return result


def _models(values) -> tuple:
    values = tuple(values)
    if not values or len(set(values)) != len(values) or any(v not in SIGMA_MODELS for v in values):
        raise ValueError(f"sigma_models must be distinct values from {SIGMA_MODELS}")
    return tuple(model for model in SIGMA_MODELS if model in values)


def audit_series(series: BarSeries, first_ms: int, end_ms: int, *, horizons=HORIZONS, half_lives=HALF_LIVES,
                 params: LabelParams | None = None, progress=None, sigma_models=("ewma",)) -> dict:
    """z audit and variance ratios of one symbol's bars on [first_ms, end_ms) (raw floats), per sigma model."""
    params = params or LabelParams()
    horizons = _subset(horizons, HORIZONS, "horizons")
    half_lives = _subset(half_lives, HALF_LIVES, "half_lives")
    if any(h not in params.horizons for h in horizons):
        raise ValueError("every audited horizon needs a label step")
    returns = block_log_returns(series)
    block_close_ms = series.start_ms + (BLOCK_MINUTES * np.arange(returns.size) + BLOCK_MINUTES) * _MINUTE_MS
    inside = (block_close_ms - _MINUTE_MS >= first_ms) & (block_close_ms <= end_ms)
    segment_returns = np.where(inside, returns, np.nan)
    first_inside = np.argmax(inside) if inside.any() else None
    if first_inside is not None:
        segment_returns[first_inside] = np.nan  # its return starts before the segment
    vr = {str(q): variance_ratio(segment_returns, q) for q in VR_QS}
    expanding = {h: expanding_variance_ratio(returns, h // BLOCK_MINUTES) for h in horizons}
    out = {"variance_ratio": vr}
    for model in _models(sigma_models):
        key = MODEL_KEYS[model][0]
        out[key] = {}
        factors = seasonal_factors(series) if model == "ewma-seasonal" else None
        robust = RobustSigma(series, hcal=model == "ewma-robust-hcal") if model in ROBUST_MODELS else None
        for half_life in half_lives:
            if robust is not None:
                variance = robust.levels(half_life)
            else:
                variance = (build_variance(series, half_life) if factors is None
                            else build_variance_deseasonalised(series, half_life, factors)).variance
            for horizon in horizons:
                entries = entry_indices(series, first_ms, end_ms, horizon, params)
                sigma_fn = None
                if robust is not None:
                    def sigma_fn(entry_ms, robust=robust, horizon=horizon, half_life=half_life):
                        return robust.sigma(horizon, half_life, params.step(horizon), entry_ms)
                row = horizon_z(series, variance, entries, horizon, expanding[horizon], factors, sigma_fn)
                row["entries"] = int(entries.size)
                row["sigma_model"] = model
                out[key].setdefault(str(horizon), {})[str(half_life)] = row
                if progress is not None:
                    progress()
    return out


# ---------------------------------------------------------------- barrier theory


def target_first_probability(a: float, b: float) -> float:
    """Driftless Brownian motion from 0, target +a, stop -b, no time limit."""
    return b / (a + b)


def expiry_probability(a: float, b: float, horizons: float, *, tolerance: float = 1e-16) -> float:
    """P(neither barrier hit by time T) for unit-variance Brownian motion (SPEC-1 section 4 series)."""
    if a <= 0 or b <= 0 or horizons <= 0:
        raise ValueError("barriers and time must be positive")
    width = a + b
    total, n = 0.0, 1
    while True:
        decay = exp(-((n * pi) ** 2) * horizons / (2 * width * width))
        total += sin(n * pi * b / width) * decay / n
        if decay < tolerance:
            break
        n += 2
    return min(1.0, max(0.0, 4 / pi * total))


def discrete_widening(step_minutes: int, horizon_minutes: int) -> float:
    """BGK continuity correction in sigma_h units: 0.5826 * sqrt(step / h).

    ``step_minutes`` must be the MONITORING step of the barrier scan (MONITORING_STEP_MIN = 1 for
    ``scan.label_trade``); the label step is only used for the traced pre-P16 fields.
    """
    return BGK_BETA * sqrt(step_minutes / horizon_minutes)


def implied_sigma_ratio(observed_expiry: float, a: float, b: float, horizons: float, widening: float = 0.0,
                        *, iterations: int = 60):
    """Realized/predicted sigma ratio c with P_expiry(a/c + w, b/c + w, T) = observed (bisection on log c).

    None when the observed share is outside what the ratio range can produce.
    """
    low, high = IMPLIED_RATIO_RANGE

    def share(ratio):
        return expiry_probability(a / ratio + widening, b / ratio + widening, horizons)

    if not isfinite(observed_expiry) or not share(high) < observed_expiry < share(low):
        return None
    lo, hi = log(low), log(high)
    for _ in range(iterations):
        mid = (lo + hi) / 2
        if share(exp(mid)) > observed_expiry:
            lo = mid  # still too many expiries: realized sigma is larger
        else:
            hi = mid
    return exp((lo + hi) / 2)


def _outcome_counts(columns) -> dict:
    counts = {code: 0 for code in OUTCOME_CODES}
    ambiguous = 0
    for column in columns:
        for value in column["outcome"]:
            counts[chr(value)] += 1
        ambiguous += sum(column["amb"])
    return {"counts": counts, "ambiguous": ambiguous}


def _shares(counts: dict) -> dict:
    n = sum(counts.values())
    nan = float("nan")
    return {code: (counts[code] / n if n else nan) for code in OUTCOME_CODES}


def barrier_geometries(symbol: str, params: LabelParams) -> list[Geometry]:
    return [Geometry(symbol, BARRIER_HORIZON, side, k, rr_index)
            for side in (1, -1) for k in params.k_grid if k in BARRIER_K
            for rr_index in range(len(params.rr_grid))]


def _target_first_ok(value: float, theory: float) -> bool:
    return isfinite(value) and abs(value - theory) <= TARGET_FIRST_TOLERANCE


def _ratio_in_band(value) -> bool:
    return value is not None and isfinite(value) and IMPLIED_RATIO_BAND[0] <= value <= IMPLIED_RATIO_BAND[1]


def _median_summary(values) -> dict:
    values = sorted(value for value in values if value is not None and isfinite(value))
    n = len(values)
    median = (values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2) if n else float("nan")
    return {"median": median, "min": values[0] if n else float("nan"), "max": values[-1] if n else float("nan"),
            "cells": n}


def barrier_summary(columns: dict, params: LabelParams) -> dict:
    """Observed outcome shares per (k, rr) next to Brownian theory and the P16 criteria (a)-(c).

    Non-trades and purged rows are counted separately. Theory: continuous, widened by the 1-minute
    monitoring step (judged), and widened by the label step (pre-P16, traced only).
    """
    horizons = params.time_limit_multiple
    label_step = params.step(BARRIER_HORIZON)
    widening = discrete_widening(MONITORING_STEP_MIN, BARRIER_HORIZON)
    widening_label = discrete_widening(label_step, BARRIER_HORIZON)
    rows = []
    for k in [k for k in params.k_grid if k in BARRIER_K]:
        for rr_index, rr in enumerate(params.rr_grid):
            parts = {side: [c for g, c in columns.items() if g.k == k and g.rr_index == rr_index and g.side == side]
                     for side in (1, -1)}
            b, a = float(k), float(k * rr)
            theory = {"target_first_no_limit": target_first_probability(a, b),
                      "expiry_continuous": expiry_probability(a, b, horizons),
                      "expiry_monitoring": expiry_probability(a + widening, b + widening, horizons),
                      "expiry_widened": expiry_probability(a + widening_label, b + widening_label, horizons)}
            sides = {}
            for name, group in (("long", parts[1]), ("short", parts[-1]), ("both", parts[1] + parts[-1])):
                observed = _outcome_counts(group)
                counts = observed["counts"]
                shares = _shares(counts)
                resolved = counts["T"] + counts["S"] + counts["L"]
                decided = counts["T"] + counts["S"]
                target_first = counts["T"] / decided if decided else float("nan")
                non_trades = {}
                for column in group:
                    for reason in column.non_trades["reason"]:
                        non_trades[chr(reason)] = non_trades.get(chr(reason), 0) + 1
                sides[name] = {"trades": sum(counts.values()), **observed, "shares": shares,
                               "target_first_resolved": counts["T"] / resolved if resolved else float("nan"),
                               "target_first_ts": target_first,
                               "target_first_ok": _target_first_ok(target_first, theory["target_first_no_limit"]),
                               "non_trades": dict(sorted(non_trades.items())),
                               "purged": sum(len(column.purged_signal_ms) for column in group),
                               "implied_sigma_ratio_continuous": implied_sigma_ratio(shares["E"], a, b, horizons),
                               "implied_sigma_ratio_monitoring": implied_sigma_ratio(shares["E"], a, b, horizons,
                                                                                      widening),
                               "implied_sigma_ratio_widened": implied_sigma_ratio(shares["E"], a, b, horizons,
                                                                                   widening_label)}
            expiry = sides["both"]["shares"]["E"]
            judged = int(k) in JUDGED_K and k.denominator == 1
            ratio = sides["both"]["implied_sigma_ratio_monitoring"]
            rows.append({"k": exact_to_str(k), "rr": exact_to_str(rr), "theory": theory, "sides": sides,
                         # (a) every side, (b) pooled sides at k = 1, (c) k = 2 descriptive.
                         "target_first_ok": all(sides[side]["target_first_ok"] for side in ("long", "short")),
                         "implied_ratio_role": "judged" if judged else "descriptive",
                         "implied_ratio_ok": _ratio_in_band(ratio) if judged else None,
                         # pre-P16 check, traced only: pooled expiry vs the label-step widened theory.
                         "expiry_within_tolerance": bool(expiry_within_tolerance(expiry, theory["expiry_widened"]))})
    criterion_a = bool(rows) and all(row["target_first_ok"] for row in rows)
    judged_rows = [row for row in rows if row["implied_ratio_role"] == "judged"]
    criterion_b = bool(judged_rows) and all(row["implied_ratio_ok"] for row in judged_rows)
    return {"horizon_min": BARRIER_HORIZON, "half_life_days": params.half_life(BARRIER_HORIZON),
            "sigma_model": params.sigma_model,
            "implied_sigma_ratio_monitoring": _median_summary(
                row["sides"]["both"]["implied_sigma_ratio_monitoring"] for row in rows),
            "implied_sigma_ratio_widened": _median_summary(
                row["sides"]["both"]["implied_sigma_ratio_widened"] for row in rows),
            "time_limit_multiple": horizons, "monitoring_step_min": MONITORING_STEP_MIN,
            "label_step_min": label_step,
            "discrete_widening_monitoring_sigma_h": widening,
            "discrete_widening_sigma_h": widening_label,
            "criteria": {"target_first_tolerance": TARGET_FIRST_TOLERANCE,
                         "implied_ratio_band": list(IMPLIED_RATIO_BAND), "judged_k": list(JUDGED_K),
                         "target_first_ok": criterion_a, "implied_ratio_ok": criterion_b},
            "geometries": rows,
            "pass": criterion_a and criterion_b,
            "pass_v1": bool(rows) and all(row["expiry_within_tolerance"] for row in rows)}


# ---------------------------------------------------------------- per symbol / report


def set_barrier_verdict(result: dict, barrier_ok: bool, model: str = "ewma", barrier_ok_v1: bool | None = None) -> None:
    """Attach the barrier verdict to every 240 m row of one sigma model.

    pass = (sd_ok or robust_ok) and barrier_ok (P16); the pre-P16 verdict stays as pass_v1 =
    sd_ok and robust_ok and barrier_ok_v1 when the old barrier verdict is given.
    """
    for row in result.get(MODEL_KEYS[model][0], {}).get(str(BARRIER_HORIZON), {}).values():
        row["barrier_ok"] = bool(barrier_ok)
        row["pass"] = (bool(row["sd_ok"]) or bool(row["robust_ok"])) and row["barrier_ok"]
        if barrier_ok_v1 is not None:
            row["barrier_ok_v1"] = bool(barrier_ok_v1)
            row["pass_v1"] = bool(row["sd_ok"]) and bool(row["robust_ok"]) and row["barrier_ok_v1"]


def audit_symbol(bars_dir, label_dir, symbol: str, segment: str, *, horizons=HORIZONS, half_lives=HALF_LIVES,
                 params: LabelParams | None = None, progress=None, sigma_models=("ewma",),
                 seasonal_label_dir=None, label_dirs: dict | None = None) -> dict:
    """One symbol: guarded bar load from FIRST_MONTH (the labels' variance history), z audit, barriers.

    The hidden guard runs before any file is opened. ``label_dir`` holds lb1 labels (ewma rows) and
    ``seasonal_label_dir`` lb2 labels (ewma-seasonal rows); ``label_dirs`` maps any model to its label
    directory (lb3 / lb3h for the robust models). None skips that model's barrier part (NA).
    """
    months = check_segment(segment)
    params = params or LabelParams()
    first_ms, end_ms = segment_bounds_ms(segment)
    bars = load_symbol_bars(bars_dir, symbol, data_lake.FIRST_MONTH, months[-1])
    models = _models(sigma_models)
    result = audit_series(bars, first_ms, end_ms, horizons=horizons, half_lives=half_lives, params=params,
                          progress=progress, sigma_models=models)
    del bars
    directories = {"ewma": label_dir, "ewma-seasonal": seasonal_label_dir, **(label_dirs or {})}
    for model, directory in directories.items():
        if model in models and directory is not None and BARRIER_HORIZON in tuple(horizons):
            model_params = params if params.sigma_model == model else replace(params, schema=None, sigma_model=model)
            columns = load_geometry_columns(directory, symbol, months, barrier_geometries(symbol, model_params),
                                            model_params, segment=segment)
            key = MODEL_KEYS[model][1]
            result[key] = barrier_summary(columns, model_params)
            set_barrier_verdict(result, result[key]["pass"], model, result[key]["pass_v1"])
    return result


def _render(value):
    """Floats -> fixed 6-decimal strings (NaN/inf -> None), recursively; everything else unchanged."""
    if isinstance(value, float):
        return f"{value:.6f}" if isfinite(value) else None
    if isinstance(value, dict):
        return {key: _render(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_render(item) for item in value]
    if isinstance(value, np.generic):
        return _render(value.item())
    return value


def build_report(segment: str, symbols: dict, *, horizons, half_lives, params: LabelParams, code_commit: str,
                 created_utc: str, data_snapshot_id: str | None) -> dict:
    report = {"schema": SCHEMA, "segment": segment, "horizons": list(horizons), "half_lives": list(half_lives),
              "vr_qs": list(VR_QS), "sd_band": [str(SD_BAND[0]), str(SD_BAND[1])],
              "robust_band": [str(ROBUST_BAND[0]), str(ROBUST_BAND[1])],
              "robust_hour_band": [str(ROBUST_HOUR_BAND[0]), str(ROBUST_HOUR_BAND[1])],
              "expiry_tolerance_rel": str(EXPIRY_TOLERANCE_REL), "expiry_tolerance_abs": str(EXPIRY_TOLERANCE_ABS),
              "expiry_reference": "continuous-monitoring Brownian theory widened by the 1-minute monitoring step "
                                  "(scan.label_trade checks every 1-minute high/low; BGK 0.5826 sqrt(1/h)); the "
                                  "label-step widening is kept only in the traced pre-P16 fields",
              "monitoring_step_min": MONITORING_STEP_MIN,
              "barrier_criteria": {"a": f"T/(T+S) within {TARGET_FIRST_TOLERANCE} of b/(a+b) for every (k, rr, side)",
                                   "b": f"k = 1: implied sigma ratio (pooled sides, monitoring-step theory) in "
                                        f"[{IMPLIED_RATIO_BAND[0]}, {IMPLIED_RATIO_BAND[1]}] for every rr",
                                   "c": "k = 2: implied ratio reported, DESCRIPTIVE (not judged)",
                                   "pass": "(sd_ok or robust_ok) and, on 240 m rows, (a) and (b)"},
              "vr_min_q_sums": VR_MIN_RETURNS,
              "label_params_identity": params.identity(), "data_snapshot_id": data_snapshot_id,
              "code_commit": code_commit, "created_utc": created_utc, "symbols": _render(symbols)}
    report["report_hash"] = content_hash(report)
    return report


def check_report(report: dict) -> None:
    body = {key: value for key, value in report.items() if key != "report_hash"}
    if report.get("report_hash") != content_hash(body):
        raise ValueError("report_hash mismatch")


def report_paths(report: dict) -> tuple[str, str]:
    stem = f"reports/calibration/{report['segment']}__{report['report_hash'][:16]}"
    return stem + ".json", stem + ".md"


def public_lines(report: dict) -> list[str]:
    """Public log: symbol, horizon, half-life, n and verdicts only (n = entries with a z value).

    ``SYMBOL model=M h=H hl=D n=N sd=PASS|FAIL robust=PASS|FAIL barrier=PASS|FAIL|NA PASS|FAIL`` (the
    last token is the overall verdict).
    """
    check_report(report)
    lines = [f"calibration audit segment {report['segment']}"]
    for symbol, result in report["symbols"].items():
        for model in SIGMA_MODELS:
            for horizon, rows in result.get(MODEL_KEYS[model][0], {}).items():
                for half_life, row in rows.items():
                    lines.append(f"{symbol} model={model} h={int(horizon)} hl={int(half_life)} n={int(row['z']['n'])} "
                                 f"sd={_verdict(row['sd_ok'])} robust={_verdict(row['robust_ok'])} "
                                 f"barrier={_verdict(row['barrier_ok'])} {_verdict(row['pass'])}")
    lines.append(f"report hash {report['report_hash']}")
    return lines


def _verdict(value) -> str:
    return "NA" if value is None else ("PASS" if value else "FAIL")


def _cell(value) -> str:
    return "n/a" if value is None else str(value)


def _z_row(label: str, stats: dict) -> str:
    q = stats["quantiles"]
    s = stats["share_abs_gt"]
    return " | ".join([f"| {label}", str(stats["n"]), _cell(stats["sd"]), _cell(stats.get("robust_sd")),
                       _cell(stats.get("mean_abs_ratio")), _cell(stats["mean"]),
                       _cell(stats["mean_abs"]), *(_cell(q[str(p)]) for p in QUANTILES),
                       *(_cell(s[str(t)]) for t in Z_THRESHOLDS)]) + " |"


_Z_HEADER = ("| row | n | sd | robust sd | mean abs / 0.7979 | mean | mean abs | " + " | ".join(f"q{p}" for p in QUANTILES) + " | "
             + " | ".join(f"abs>{t}" for t in Z_THRESHOLDS) + " |")
_Z_RULE = "|" + " --- |" * (7 + len(QUANTILES) + len(Z_THRESHOLDS))


def markdown(report: dict) -> str:
    check_report(report)
    lines = [f"# Sigma calibration audit ({report['segment']})", "",
             f"Report hash: `{report['report_hash']}`. Code commit `{report['code_commit']}`, "
             f"created {report['created_utc']}.", "",
             f"PASS = (sd verdict, sd(z) in [{report['sd_band'][0]}, {report['sd_band'][1]}], or robust verdict) and, "
             f"on {BARRIER_HORIZON} m rows, barrier verdict: (a) T/(T+S) within {TARGET_FIRST_TOLERANCE} of b/(a+b) "
             f"for every (k, rr, side); (b) at k = 1 the implied sigma ratio against the theory widened by the "
             f"{MONITORING_STEP_MIN}-minute MONITORING step (the scan checks every 1-minute high/low) in "
             f"[{IMPLIED_RATIO_BAND[0]}, {IMPLIED_RATIO_BAND[1]}]; (c) k = 2 ratios are descriptive. The pre-P16 "
             "label-step widening and its verdicts stay in the JSON (pass_v1). Robust verdict: robust sd = 1.4826 MAD(z) "
             "and mean|z| / 0.7979 "
             f"in [{report['robust_band'][0]}, {report['robust_band'][1]}] overall and in "
             f"[{report['robust_hour_band'][0]}, {report['robust_hour_band'][1]}] in every UTC hour.", ""]
    for symbol, result in report["symbols"].items():
        lines += [f"## {symbol}", "", "### Variance ratio (segment, 5-minute log returns)", "",
                  "| q | VR | returns | q-sums |", "| --- | --- | --- | --- |"]
        for q, row in result["variance_ratio"].items():
            lines.append(f"| {q} | {_cell(row['vr'])} | {row['n_returns']} | {row['n_q_sums']} |")
        lines.append("")
        for model in SIGMA_MODELS:
            rows_key, barrier_key = MODEL_KEYS[model]
            for horizon, rows in result.get(rows_key, {}).items():
                for half_life, row in rows.items():
                    lines += [f"### {model}, h = {horizon} m, half-life {half_life} d: {_verdict(row['pass'])} "
                              f"(sd {_verdict(row['sd_ok'])}, robust {_verdict(row['robust_ok'])}, "
                              f"barrier {_verdict(row['barrier_ok'])})", "",
                              f"Entries {row['entries']}; skipped: " + ", ".join(
                                  f"{name} {count}" for name, count in row["skipped"].items()) + ".", "",
                              _Z_HEADER, _Z_RULE, _z_row(model, row["z"])]
                    corrected = row.get("vr_corrected")
                    if corrected is not None:
                        lines.append(_z_row(f"VR-corrected (q={corrected['q']})", corrected["z"]))
                    lines += ["", f"By UTC hour (EWMA; VR-corrected: no estimate for "
                                  f"{corrected['no_vr_estimate'] if corrected else 'n/a'} entries):", "",
                              _Z_HEADER, _Z_RULE]
                    lines += [_z_row(f"{hour['hour']:02d}h", hour) for hour in row["z_by_hour"]]
                    if corrected is not None:
                        lines += ["", "By UTC hour (VR-corrected):", "", _Z_HEADER, _Z_RULE]
                        lines += [_z_row(f"{hour['hour']:02d}h", hour) for hour in corrected["z_by_hour"]]
                    lines.append("")
            barriers = result.get(barrier_key)
            if barriers is not None:
                criteria = barriers.get("criteria", {})
                lines += [f"### Barriers ({model}) at {barriers['horizon_min']} m (labels' half-life {barriers['half_life_days']} d, "
                          f"limit {barriers['time_limit_multiple']} x h, monitoring step "
                          f"{barriers.get('monitoring_step_min')} min, widening "
                          f"{barriers.get('discrete_widening_monitoring_sigma_h')} sigma_h; label-step widening "
                          f"{barriers['discrete_widening_sigma_h']} traced only): "
                          f"{'PASS' if barriers['pass'] else 'FAIL'} (a {_verdict(criteria.get('target_first_ok'))}, "
                          f"b {_verdict(criteria.get('implied_ratio_ok'))}; pre-P16 "
                          f"{_verdict(barriers.get('pass_v1'))})", "",
                          "| k | rr | side | trades | T | S | E | L | X | amb | T/(T+S) | theory b/(a+b) | (a) | "
                          "theory E cont. | theory E monitoring | implied ratio cont. | implied ratio monitoring | role | "
                          "theory E label-step | implied ratio label-step | non-trades | purged |", "|" + " --- |" * 22]
                for row in barriers["geometries"]:
                    theory = row["theory"]
                    for side, item in row["sides"].items():
                        shares = item["shares"]
                        role = row.get("implied_ratio_role", "")
                        if side == "both" and row.get("implied_ratio_ok") is not None:
                            role += f" {_verdict(row['implied_ratio_ok'])}"
                        lines.append(" | ".join([
                            f"| {row['k']}", row["rr"], side, str(item["trades"]),
                            *(_cell(shares[code]) for code in OUTCOME_CODES), str(item["ambiguous"]),
                            _cell(item.get("target_first_ts")), _cell(theory["target_first_no_limit"]),
                            _verdict(item.get("target_first_ok")) if side != "both" else "",
                            _cell(theory["expiry_continuous"]), _cell(theory.get("expiry_monitoring")),
                            _cell(item["implied_sigma_ratio_continuous"]),
                            _cell(item.get("implied_sigma_ratio_monitoring")), role,
                            _cell(theory["expiry_widened"]), _cell(item["implied_sigma_ratio_widened"]),
                            ", ".join(f"{code} {count}" for code, count in item["non_trades"].items()) or "none",
                            str(item["purged"])]) + " |")
                ratio = barriers.get("implied_sigma_ratio_monitoring")
                if ratio is not None:
                    lines += [f"Implied sigma ratio ({barriers.get('sigma_model', model)}, monitoring-step theory, both "
                              f"sides): median {_cell(ratio['median'])}, min {_cell(ratio['min'])}, max "
                              f"{_cell(ratio['max'])} over {ratio['cells']} (k, rr) cells (1 = the barrier geometry "
                              "matches the realized expiry share; k = 2 cells are descriptive).", ""]
                lines.append("")
    return "\n".join(lines) + "\n"
