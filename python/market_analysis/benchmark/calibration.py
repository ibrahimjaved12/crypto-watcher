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

PASS for a symbol x horizon x half-life: sd(z) in [0.9, 1.1]; for the 240 m row at
the labels' half-life also every (k, rr) expiry share within 10 % (relative) of the
continuous-monitoring theory (scan.py tests every minute's high/low, so the
continuous value is the reference; the discrete one is a sensitivity column).

Floats are only statistics: the report renders them as fixed 6-decimal strings,
so it stays canonical JSON (``canonical.canonical_bytes``) and hashable.
"""
from __future__ import annotations

from math import exp, isfinite, log, pi, sin, sqrt

import numpy as np

from .. import data_lake
from .bars import COMPROMISED_FLAGS, MISSING, BarSeries
from .canonical import content_hash, exact_to_str
from .hidden_guard import require_months
from .label_store import Geometry, load_geometry_columns
from .labels import LabelParams
from .market_data import load_symbol_bars
from .segments import SEGMENTS, segment_bounds_ms, segment_months
from .volatility import BLOCK_MINUTES, BLOCKS_PER_DAY, VAR_SCALE, build_variance, horizon_sigma

SCHEMA = "calibration-v1"
AUDIT_SEGMENTS = ("development", "validation")
HORIZONS = (15, 60, 240)
HALF_LIVES = (1, 3, 7)
VR_QS = (3, 12, 48)
QUANTILES = (1, 5, 25, 50, 75, 95, 99)
Z_THRESHOLDS = (1, 2, 3)
SD_BAND = (0.9, 1.1)
EXPIRY_TOLERANCE = 0.10          # relative
BARRIER_HORIZON = 240
BARRIER_K = (1, 2)
BGK_BETA = 0.5826                # -zeta(1/2) / sqrt(2 pi), Broadie-Glasserman-Kou
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
        return {"n": 0, "mean": nan, "sd": nan, "mean_abs": nan,
                "quantiles": {str(p): nan for p in QUANTILES}, "share_abs_gt": {str(t): nan for t in Z_THRESHOLDS}}
    magnitude = np.abs(z)
    return {"n": n, "mean": float(z.mean()), "sd": float(z.std(ddof=1)) if n > 1 else nan,
            "mean_abs": float(magnitude.mean()),
            "quantiles": {str(p): float(q) for p, q in zip(QUANTILES, np.quantile(z, [p / 100 for p in QUANTILES]))},
            "share_abs_gt": {str(t): float((magnitude > t).mean()) for t in Z_THRESHOLDS}}


def z_stats_by_hour(z, hours) -> list[dict]:
    """24 rows of z_stats by UTC hour 0..23."""
    z, hours = np.asarray(z, dtype=float), np.asarray(hours)
    return [{"hour": hour, **z_stats(z[hours == hour])} for hour in range(24)]


def sd_pass(stats: dict) -> bool:
    return isfinite(stats["sd"]) and SD_BAND[0] <= stats["sd"] <= SD_BAND[1]


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


def horizon_z(series: BarSeries, variance, entries, horizon: int, vr_expanding=None) -> dict:
    """z (and VR-corrected z) of the given entries for one horizon and one variance series."""
    variance = np.frombuffer(variance, dtype=np.int64)
    opens = np.frombuffer(series.open, dtype=np.int64)
    bad = _bad_minutes(series)
    entries = np.asarray(entries, dtype=np.int64)
    block = entries // BLOCK_MINUTES - 1  # the 5-minute block ending at minute e - 1
    var = np.where(block >= 0, variance[np.maximum(block, 0)], MISSING)
    no_sigma = (var == MISSING) | (var <= 0)
    bad_entry = ~no_sigma & bad[entries]
    bad_exit = ~no_sigma & ~bad_entry & bad[entries + horizon]
    use = ~(no_sigma | bad_entry | bad_exit)
    e, b = entries[use], block[use]
    sigma = np.array([horizon_sigma(int(v), horizon) for v in var[use]], dtype=float) / VAR_SCALE
    r = np.log(opens[e + horizon].astype(float) / opens[e].astype(float))
    z = r / sigma
    hours = ((series.start_ms + e * _MINUTE_MS) // _HOUR_MS) % 24
    result = {"skipped": {"no_sigma": int(no_sigma.sum()), "entry_compromised": int(bad_entry.sum()),
                          "exit_compromised": int(bad_exit.sum())},
              "z": z_stats(z), "z_by_hour": z_stats_by_hour(z, hours)}
    result["pass"] = sd_pass(result["z"])
    if vr_expanding is not None:
        ratio = vr_expanding[b]
        has = np.isfinite(ratio) & (ratio > 0)
        corrected = z[has] / np.sqrt(ratio[has])
        result["vr_corrected"] = {"q": horizon // BLOCK_MINUTES, "no_vr_estimate": int((~has).sum()),
                                  "z": z_stats(corrected), "z_by_hour": z_stats_by_hour(corrected, hours[has])}
        result["vr_corrected"]["pass"] = sd_pass(result["vr_corrected"]["z"])
    return result


def audit_series(series: BarSeries, first_ms: int, end_ms: int, *, horizons=HORIZONS, half_lives=HALF_LIVES,
                 params: LabelParams | None = None, progress=None) -> dict:
    """z audit and variance ratios of one symbol's bars on [first_ms, end_ms) (raw floats)."""
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
    out = {"variance_ratio": vr, "horizons": {}}
    for half_life in half_lives:
        variance = build_variance(series, half_life).variance
        for horizon in horizons:
            entries = entry_indices(series, first_ms, end_ms, horizon, params)
            row = horizon_z(series, variance, entries, horizon, expanding[horizon])
            row["entries"] = int(entries.size)
            out["horizons"].setdefault(str(horizon), {})[str(half_life)] = row
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
    """BGK continuity correction in sigma_h units: 0.5826 * sqrt(step / h)."""
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


def barrier_summary(columns: dict, params: LabelParams) -> dict:
    """Observed outcome shares per (k, rr) next to Brownian theory; non-trades and purged rows separately."""
    horizons = params.time_limit_multiple
    widening = discrete_widening(params.step(BARRIER_HORIZON), BARRIER_HORIZON)
    rows = []
    for k in [k for k in params.k_grid if k in BARRIER_K]:
        for rr_index, rr in enumerate(params.rr_grid):
            parts = {side: [c for g, c in columns.items() if g.k == k and g.rr_index == rr_index and g.side == side]
                     for side in (1, -1)}
            b, a = float(k), float(k * rr)
            theory = {"target_first_no_limit": target_first_probability(a, b),
                      "expiry_continuous": expiry_probability(a, b, horizons),
                      "expiry_discrete": expiry_probability(a + widening, b + widening, horizons)}
            sides = {}
            for name, group in (("long", parts[1]), ("short", parts[-1]), ("both", parts[1] + parts[-1])):
                observed = _outcome_counts(group)
                counts = observed["counts"]
                shares = _shares(counts)
                resolved = counts["T"] + counts["S"] + counts["L"]
                non_trades = {}
                for column in group:
                    for reason in column.non_trades["reason"]:
                        non_trades[chr(reason)] = non_trades.get(chr(reason), 0) + 1
                sides[name] = {"trades": sum(counts.values()), **observed, "shares": shares,
                               "target_first_resolved": counts["T"] / resolved if resolved else float("nan"),
                               "non_trades": dict(sorted(non_trades.items())),
                               "purged": sum(len(column.purged_signal_ms) for column in group),
                               "implied_sigma_ratio_continuous": implied_sigma_ratio(shares["E"], a, b, horizons),
                               "implied_sigma_ratio_discrete": implied_sigma_ratio(shares["E"], a, b, horizons,
                                                                                   widening)}
            expiry = sides["both"]["shares"]["E"]
            within = (isfinite(expiry) and abs(expiry - theory["expiry_continuous"])
                      <= EXPIRY_TOLERANCE * theory["expiry_continuous"])
            rows.append({"k": exact_to_str(k), "rr": exact_to_str(rr), "theory": theory, "sides": sides,
                         "expiry_within_tolerance": bool(within)})
    return {"horizon_min": BARRIER_HORIZON, "half_life_days": params.half_life(BARRIER_HORIZON),
            "time_limit_multiple": horizons, "monitoring_step_min": params.step(BARRIER_HORIZON),
            "discrete_widening": widening, "geometries": rows,
            "pass": bool(rows) and all(row["expiry_within_tolerance"] for row in rows)}


# ---------------------------------------------------------------- per symbol / report


def audit_symbol(bars_dir, label_dir, symbol: str, segment: str, *, horizons=HORIZONS, half_lives=HALF_LIVES,
                 params: LabelParams | None = None, progress=None) -> dict:
    """One symbol: guarded bar load from FIRST_MONTH (the labels' variance history), z audit, barriers.

    The hidden guard runs before any file is opened. ``label_dir=None`` skips the barrier part.
    """
    months = check_segment(segment)
    params = params or LabelParams()
    first_ms, end_ms = segment_bounds_ms(segment)
    bars = load_symbol_bars(bars_dir, symbol, data_lake.FIRST_MONTH, months[-1])
    result = audit_series(bars, first_ms, end_ms, horizons=horizons, half_lives=half_lives, params=params,
                          progress=progress)
    del bars
    if label_dir is not None and BARRIER_HORIZON in tuple(horizons):
        columns = load_geometry_columns(label_dir, symbol, months, barrier_geometries(symbol, params), params,
                                        segment=segment)
        result["barriers"] = barrier_summary(columns, params)
        label_half_life = str(params.half_life(BARRIER_HORIZON))
        row = result["horizons"][str(BARRIER_HORIZON)].get(label_half_life)
        if row is not None:
            row["pass"] = row["pass"] and result["barriers"]["pass"]
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
              "expiry_tolerance": str(EXPIRY_TOLERANCE), "vr_min_q_sums": VR_MIN_RETURNS,
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
    """Public log: symbol, horizon, half-life, n and PASS/FAIL only (n = entries with a z value)."""
    check_report(report)
    lines = [f"calibration audit segment {report['segment']}"]
    for symbol, result in report["symbols"].items():
        for horizon, rows in result["horizons"].items():
            for half_life, row in rows.items():
                verdict = "PASS" if row["pass"] else "FAIL"
                lines.append(f"{symbol} h={int(horizon)} hl={int(half_life)} n={int(row['z']['n'])} {verdict}")
    lines.append(f"report hash {report['report_hash']}")
    return lines


def _cell(value) -> str:
    return "n/a" if value is None else str(value)


def _z_row(label: str, stats: dict) -> str:
    q = stats["quantiles"]
    s = stats["share_abs_gt"]
    return " | ".join([f"| {label}", str(stats["n"]), _cell(stats["sd"]), _cell(stats["mean"]),
                       _cell(stats["mean_abs"]), *(_cell(q[str(p)]) for p in QUANTILES),
                       *(_cell(s[str(t)]) for t in Z_THRESHOLDS)]) + " |"


_Z_HEADER = ("| row | n | sd | mean | mean abs | " + " | ".join(f"q{p}" for p in QUANTILES) + " | "
             + " | ".join(f"abs>{t}" for t in Z_THRESHOLDS) + " |")
_Z_RULE = "|" + " --- |" * (5 + len(QUANTILES) + len(Z_THRESHOLDS))


def markdown(report: dict) -> str:
    check_report(report)
    lines = [f"# Sigma calibration audit ({report['segment']})", "",
             f"Report hash: `{report['report_hash']}`. Code commit `{report['code_commit']}`, "
             f"created {report['created_utc']}.", "",
             f"PASS: sd(z) in [{report['sd_band'][0]}, {report['sd_band'][1]}]; for the {BARRIER_HORIZON} m row at "
             f"the labels' half-life also every expiry share within {report['expiry_tolerance']} (relative) of the "
             "continuous Brownian theory.", ""]
    for symbol, result in report["symbols"].items():
        lines += [f"## {symbol}", "", "### Variance ratio (segment, 5-minute log returns)", "",
                  "| q | VR | returns | q-sums |", "| --- | --- | --- | --- |"]
        for q, row in result["variance_ratio"].items():
            lines.append(f"| {q} | {_cell(row['vr'])} | {row['n_returns']} | {row['n_q_sums']} |")
        lines.append("")
        for horizon, rows in result["horizons"].items():
            for half_life, row in rows.items():
                lines += [f"### h = {horizon} m, half-life {half_life} d: {'PASS' if row['pass'] else 'FAIL'}", "",
                          f"Entries {row['entries']}; skipped: " + ", ".join(
                              f"{name} {count}" for name, count in row["skipped"].items()) + ".", "",
                          _Z_HEADER, _Z_RULE, _z_row("EWMA", row["z"])]
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
        barriers = result.get("barriers")
        if barriers is not None:
            lines += [f"### Barriers at {barriers['horizon_min']} m (labels' half-life {barriers['half_life_days']} d, "
                      f"limit {barriers['time_limit_multiple']} x h, BGK widening {barriers['discrete_widening']}): "
                      f"{'PASS' if barriers['pass'] else 'FAIL'}", "",
                      "| k | rr | side | trades | T | S | E | L | X | amb | T/(T+S+L) | theory b/(a+b) | "
                      "theory E cont. | theory E disc. | implied ratio cont. | implied ratio disc. | non-trades | "
                      "purged |", "|" + " --- |" * 18]
            for row in barriers["geometries"]:
                theory = row["theory"]
                for side, item in row["sides"].items():
                    shares = item["shares"]
                    lines.append(" | ".join([
                        f"| {row['k']}", row["rr"], side, str(item["trades"]),
                        *(_cell(shares[code]) for code in OUTCOME_CODES), str(item["ambiguous"]),
                        _cell(item["target_first_resolved"]), _cell(theory["target_first_no_limit"]),
                        _cell(theory["expiry_continuous"]), _cell(theory["expiry_discrete"]),
                        _cell(item["implied_sigma_ratio_continuous"]), _cell(item["implied_sigma_ratio_discrete"]),
                        ", ".join(f"{code} {count}" for code, count in item["non_trades"].items()) or "none",
                        str(item["purged"])]) + " |")
            lines.append("")
    return "\n".join(lines) + "\n"
