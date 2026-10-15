"""Pre-registered doji event study on candle-table-v1 (docs/pattern-study.md, "Doji event study").

DESCRIPTIVE ONLY: a screen in the style of ``benchmark/screen.py``. Nothing here is a PASS, a trade,
a label or a ledger entry; the report states how many hypotheses were examined. Cells live in
``study_cells.py`` and are frozen.

Primary (K = 9 cells, Holm over their clustered t): pooled over the symbols and the intraday
timeframes 15/60/240,
    y = alpha_{symbol,tf} + gamma_hour + beta * D + delta' X + e,
D = event (doji-geo-v2) within the cell's context, sample = context rows with range_atr >= 0.5,
X = [ln range_atr, ln rel_volume_prior20, atr_pct_prev, s * ret3_prev_sigma (signed cells),
|ret3_prev_sigma|], covariance clustered by the UTC day of the candle open (screen.ols_clustered).
Signed cells: y = s * ret_1_sigma; unsigned cells (A, C5): y = ln_range1_sigma. A stratified
difference in means on the same sample (day-cluster bootstrap CI) is the robustness estimator.
"""
from __future__ import annotations

import csv
from functools import lru_cache
import gzip
import io
import json
from math import sqrt
from pathlib import Path
from statistics import NormalDist

import numpy as np

from ..benchmark.calibration import _render, check_segment
from ..benchmark.canonical import content_hash
from ..benchmark import levels as lv
from ..benchmark.screen import _bootstrap_mean, _interval, cluster_weights, ols_clustered, spearman_points
from . import study_cells as sc

SCHEMA = "doji-study-v1"
REPORT_ROOT = "reports/pattern-study"
INTRADAY_TFS = (15, 60, 240)
DAILY_TFS = (1440, 10080)
ROUND_TRIP_COST_BP = 11.0
MDE_FACTOR = 2.8                # z(0.975) + z(0.80), rounded as pre-registered
MDE_ALPHA, MDE_POWER = 0.05, 0.80
MIN_MEMBERS_FLAG = 100
MIN_MEMBERS_DAILY = 30
BOOTSTRAP_B = 2000
BOOTSTRAP_SEED = 20261015
RANGE_BINS = (0.75, 1.0, 1.5, 2.5)  # range_atr strata [0.5, 0.75, 1, 1.5, 2.5, inf)
DOSE_VARIABLES = ("body_ratio", "range_atr", "rel_volume_prior20", "nearest_level_dist_atr")
DOSE_BUCKETS = 5
DAY_MS = 86_400_000
TEXT_COLUMNS = ("symbol", "segment", "status", "doji_subtype")
NUMERIC_COLUMNS = (
    "tf", "open_ms", "decision_ms", "fwd_ok_1", "fwd_ok_2", "fwd_ok_4", "high", "low", "range", "body_ratio",
    "range_atr", "adx_prev", "trend_dir", "extension_prev_atr", "any_touch", "nearest_level_dist_atr",
    "sweep_any_up", "sweep_any_dn", "sweep_both", "sweep_dir", "touch_approach_side", "squeeze_on_prev",
    "flush_dir", "rel_volume_slot", "rel_volume_prior20", "ret3_prev_sigma", "atr_pct_prev", "utc_hour",
    "touch_round", "touch_prev_day", "touch_prev_week", "touch_prev_month", "touch_swing_tf", "touch_swing4h",
    "touch_swing1d", "touch_swing1w", "sigma_tf", "ret_1_sigma", "ret_1_bp", "ln_range1_sigma", "ret_2_sigma", "ret_4_sigma",
    "mfe_long_2_sigma", "mae_long_2_sigma", "mfe_long_4_sigma", "mae_long_4_sigma")
TOUCH_COLUMNS = (("any", "any_touch"), *((name, f"touch_{name}") for name in lv.PATTERN_LEVEL_SETS))
STUDY_SEGMENTS = {"development": {"intraday": "development", "daily": "development-ext"},
                  "validation": {"intraday": "validation", "daily": "validation"}}


def mde_multiplier(alpha: float, power: float = MDE_POWER) -> float:
    """z(1 - alpha/2) + z(power): the minimum detectable effect in standard errors."""
    normal = NormalDist()
    return normal.inv_cdf(1 - alpha / 2) + normal.inv_cdf(power)


def holm_mde_multiplier(k: int = sc.K_PRIMARY, alpha: float = MDE_ALPHA, power: float = MDE_POWER) -> float:
    """The MDE multiplier at the Holm-adjusted per-test alpha alpha / K (about 3.6 for K = 9)."""
    return mde_multiplier(alpha / k, power)


@lru_cache(maxsize=8)
def _weights(groups: int, B: int, seed: int = BOOTSTRAP_SEED):
    return cluster_weights(groups, B, seed)


def day_mean(values, days, B: int = BOOTSTRAP_B) -> dict:
    """Mean of the finite values with a UTC-day-clustered bootstrap SE and 95% interval."""
    values, days = np.asarray(values, float), np.asarray(days)
    ok = np.isfinite(values)
    values, days = values[ok], days[ok]
    if not values.size:
        return {"n": 0, "mean": float("nan"), "se": float("nan"), "ci95": [float("nan")] * 2}
    _, cluster = np.unique(days, return_inverse=True)
    out = _bootstrap_mean(values, cluster, _weights(int(cluster.max()) + 1, B))
    return {"n": int(values.size), "mean": out["mean"], "se": out["se"], "ci95": out["ci95"]}


# ---------------------------------------------------------------- table input


def read_table(paths) -> dict:
    """Frame (dict of numpy arrays) of the needed columns of candle-table csv.gz files, in file order."""
    numeric = {name: [] for name in NUMERIC_COLUMNS}
    text = {name: [] for name in TEXT_COLUMNS}
    for path in paths:
        with io.TextIOWrapper(gzip.open(path, "rb"), encoding="ascii", newline="") as stream:
            reader = csv.reader(stream)
            header = next(reader)
            where = {name: header.index(name) for name in (*NUMERIC_COLUMNS, *TEXT_COLUMNS) if name in header}
            missing = [name for name in (*NUMERIC_COLUMNS, *TEXT_COLUMNS) if name not in where]
            if missing:
                raise ValueError(f"{Path(path).name}: table lacks columns {', '.join(missing)}")
            for row in reader:
                for name in NUMERIC_COLUMNS:
                    value = row[where[name]]
                    numeric[name].append(float(value) if value != "" else np.nan)
                for name in TEXT_COLUMNS:
                    text[name].append(row[where[name]])
    frame = {name: np.asarray(values, dtype=float) for name, values in numeric.items()}
    frame.update({name: np.asarray(values, dtype=object) for name, values in text.items()})
    return add_prior20(frame)


def add_prior20(frame: dict, n: int = 20) -> dict:
    """prior20_high / prior20_low: max high / min low of candles i-20..i-1 (the sweep group's prior20
    reference), from consecutive rows of the same symbol and timeframe; NaN unless all 20 are valid."""
    count = len(frame["tf"])
    high, low = np.full(count, np.nan), np.full(count, np.nan)
    keys = np.array([f"{s}|{t}" for s, t in zip(frame["symbol"], frame["tf"])], dtype=object)
    for key in dict.fromkeys(keys):
        rows = np.flatnonzero(keys == key)
        if rows.size <= n:
            continue
        step = frame["tf"][rows[0]] * 60_000
        opens, highs, lows = frame["open_ms"][rows], frame["high"][rows], frame["low"][rows]
        windows_h = np.lib.stride_tricks.sliding_window_view(highs, n)[:-1]
        windows_l = np.lib.stride_tricks.sliding_window_view(lows, n)[:-1]
        contiguous = opens[n:] - opens[:-n] == n * step
        ok = contiguous & ~np.isnan(windows_h).any(axis=1) & ~np.isnan(windows_l).any(axis=1)
        high[rows[n:]] = np.where(ok, windows_h.max(axis=1), np.nan)
        low[rows[n:]] = np.where(ok, windows_l.min(axis=1), np.nan)
    frame["prior20_high"], frame["prior20_low"] = high, low
    return frame


def subset(frame: dict, mask) -> dict:
    return {name: values[mask] for name, values in frame.items()}


# ---------------------------------------------------------------- samples


def cell_sample(frame: dict, cell, *, intraday: bool, body_max: float = sc.BODY_MAX,
                range_atr_min: float = sc.RANGE_ATR_MIN) -> dict:
    """Arrays of one cell: context rows of the population in the size class, with complete controls."""
    context, direction = cell.rule(frame, intraday)
    base = sc.population(frame) & context & sc.sized(frame, range_atr_min)
    members = sc.event(frame, body_max, range_atr_min)
    base &= members | sc.control(frame, range_atr_min)
    s = direction if cell.signed else np.ones(len(direction))
    y = s * frame["ret_1_sigma"] if cell.signed else frame["ln_range1_sigma"]
    with np.errstate(divide="ignore", invalid="ignore"):
        controls = [np.log(frame["range_atr"]), np.log(frame["rel_volume_prior20"]), frame["atr_pct_prev"]]
    if cell.signed:
        controls.append(s * frame["ret3_prev_sigma"])
    controls.append(np.abs(frame["ret3_prev_sigma"]))
    X = np.column_stack(controls)
    complete = np.isfinite(X).all(axis=1)
    has_y = np.isfinite(y)
    keep = base & complete & has_y
    return {"cell": cell, "signed": cell.signed, "intraday": intraday,
            "excluded_missing_controls": int((base & ~complete).sum()),
            "excluded_missing_outcome": int((base & complete & ~has_y).sum()),
            "D": members[keep].astype(float), "y": y[keep], "X": X[keep], "s": s[keep],
            "group": np.array([f"{a}|{int(b)}" for a, b in zip(frame["symbol"][keep], frame["tf"][keep])],
                              dtype=object),
            "symbol": frame["symbol"][keep], "tf": frame["tf"][keep],
            "hour": frame["utc_hour"][keep], "session": sc.session_labels(frame["decision_ms"][keep], intraday),
            "day": (frame["open_ms"][keep] // DAY_MS).astype(np.int64),
            "atr_pct": frame["atr_pct_prev"][keep], "range_atr": frame["range_atr"][keep],
            "rel_volume": frame["rel_volume_prior20"][keep], "sigma_tf": frame["sigma_tf"][keep],
            "ret_bp": frame["ret_1_bp"][keep], "subtype": frame["doji_subtype"][keep],
            "rows": {name: frame[name][keep] for name in ("ret_2_sigma", "ret_4_sigma", "mfe_long_2_sigma",
                                                          "mae_long_2_sigma", "mfe_long_4_sigma",
                                                          "mae_long_4_sigma", "fwd_ok_2", "fwd_ok_4",
                                                          *DOSE_VARIABLES)}}


def _dummies(labels) -> np.ndarray:
    _, index = np.unique(labels, return_inverse=True)
    out = np.zeros((len(labels), int(index.max()) + 1 if len(labels) else 0))
    out[np.arange(len(labels)), index] = 1.0
    return out


def regression(sample: dict, *, hour_effects: bool) -> dict | None:
    """beta on D with group fixed effects (+ hour dummies) and controls; None when not estimable."""
    D, y = sample["D"], sample["y"]
    n_members, n_controls = int(D.sum()), int((1 - D).sum())
    if n_members < 1 or n_controls < 1:
        return None
    parts = [_dummies(sample["group"])]
    if hour_effects:
        hours = _dummies(sample["hour"].astype(np.int64))
        parts.append(hours[:, 1:])  # drop the first hour: the group effects carry the level
    parts += [D[:, None], sample["X"]]
    design = np.column_stack(parts)
    position = sum(part.shape[1] for part in parts[:-2])
    if len(y) <= design.shape[1]:
        return None
    _, clusters = np.unique(sample["day"], return_inverse=True)
    fit = ols_clustered(y, design, clusters)
    beta, se = float(fit["beta"][position]), float(fit["se"][position])
    if not np.isfinite(se) or se <= 0:
        return None
    t = beta / se
    return {"beta": beta, "se": se, "t": t, "p": 2 * (1 - NormalDist().cdf(abs(t))), "n": int(fit["n"]),
            "clusters": int(fit["clusters"]), "ci95": [beta - 1.96 * se, beta + 1.96 * se]}


def strata_codes(sample: dict, *, hour_effects: bool, with_session: bool = False) -> np.ndarray:
    """(symbol, tf, 6-hour bucket, atr_pct tercile, range_atr bin, rel_volume tercile[, session]) as one code."""
    _, group = np.unique(sample["group"], return_inverse=True)
    hour6 = (sample["hour"] // 6).astype(np.int64) if hour_effects else np.zeros(len(group), np.int64)
    atr = np.digitize(sample["atr_pct"], (1 / 3, 2 / 3))
    size = np.digitize(sample["range_atr"], RANGE_BINS)
    edges = np.quantile(sample["rel_volume"], (1 / 3, 2 / 3)) if len(group) else (0, 0)
    volume = np.digitize(sample["rel_volume"], edges)
    columns = [group, hour6, atr, size, volume]
    if with_session:
        columns.append(np.unique(sample["session"], return_inverse=True)[1])
    stacked = np.column_stack(columns)
    _, code = np.unique(stacked, axis=0, return_inverse=True) if len(group) else (None, np.zeros(0, np.int64))
    return np.asarray(code).reshape(-1)


def _stratified_value(m_n, m_s, c_n, c_s) -> float:
    ok = (m_n >= 1) & (c_n >= 1)
    if not ok.any():
        return float("nan")
    with np.errstate(divide="ignore", invalid="ignore"):
        diff = np.where(ok, m_s / np.where(ok, m_n, 1) - c_s / np.where(ok, c_n, 1), 0.0)
    weight = np.where(ok, m_n, 0.0)
    return float((weight * diff).sum() / weight.sum())


def stratified(sample: dict, *, hour_effects: bool, B: int = BOOTSTRAP_B, seed: int = BOOTSTRAP_SEED,
               with_session: bool = False) -> dict:
    """Member-weighted mean over strata of (mean y members - mean y controls); strata need both.

    ``with_session`` adds the decision-time session as a further stratum (a robustness view, never the
    primary result and never in the Holm family)."""
    code = strata_codes(sample, hour_effects=hour_effects, with_session=with_session)
    D, y = sample["D"].astype(bool), sample["y"]
    if not len(code):
        return {"estimate": float("nan"), "ci95": [float("nan")] * 2, "strata_used": 0}
    strata = int(code.max()) + 1
    _, day = np.unique(sample["day"], return_inverse=True)
    days = int(day.max()) + 1
    arrays = []
    for mask, values in ((D, np.ones(len(y))), (D, y), (~D, np.ones(len(y))), (~D, y)):
        grid = np.zeros((days, strata))
        np.add.at(grid, (day[mask], code[mask]), values[mask])
        arrays.append(grid)
    totals = [grid.sum(axis=0) for grid in arrays]
    estimate = _stratified_value(*totals)
    used = int(((totals[0] >= 1) & (totals[2] >= 1)).sum())
    weights = cluster_weights(days, B, seed)
    drawn = [weights @ grid for grid in arrays]  # (B, strata)
    draws = np.array([_stratified_value(*(d[b] for d in drawn)) for b in range(B)]) if B else np.zeros(0)
    return {"estimate": estimate, "ci95": _interval(draws), "strata_used": used}


def wilson(k: int, n: int, z: float = 1.96) -> list:
    if n <= 0:
        return [float("nan"), float("nan")]
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [centre - half, centre + half]


def hit_rates(sample: dict) -> dict | None:
    """Signed cells: hit = s * ret_1_bp > 0; ties (ret_1_bp == 0) are excluded and counted."""
    if not sample["signed"]:
        return None
    D = sample["D"].astype(bool)
    signed_bp = sample["s"] * sample["ret_bp"]
    tie = sample["ret_bp"] == 0
    out = {}
    for label, mask in (("members", D), ("controls", ~D)):
        decided = mask & ~tie
        k, n = int((decided & (signed_bp > 0)).sum()), int(decided.sum())
        out[label] = {"hits": k, "n": n, "ties": int((mask & tie).sum()), "rate": k / n if n else float("nan"),
                      "wilson95": wilson(k, n)}
    out["difference"] = out["members"]["rate"] - out["controls"]["rate"]
    return out


def holm(pvalues: list) -> list:
    """Holm step-down adjusted p-values; None (not estimable) counts as p = 1 and stays None."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: (1.0 if pvalues[i] is None else pvalues[i], i))
    adjusted, running = [None] * m, 0.0
    for rank, i in enumerate(order):
        p = 1.0 if pvalues[i] is None else pvalues[i]
        running = max(running, min(1.0, (m - rank) * p))
        adjusted[i] = None if pvalues[i] is None else running
    return adjusted


def cost_lines(beta: float, se: float, mean_sigma_bp: float, members: dict | None = None,
               controls: dict | None = None) -> dict:
    """bp conversion, the 11 bp hurdle, and the minimum detectable effect at 2.8 SE and at the Holm alpha.

    ``members`` / ``controls`` are ``day_mean`` results of the signed return in bp (gross, one candle).
    """
    hurdle_sigma = ROUND_TRIP_COST_BP / mean_sigma_bp if mean_sigma_bp > 0 else float("nan")
    effect_bp = beta * mean_sigma_bp
    holm_factor = holm_mde_multiplier()
    out = {"mean_sigma_bp": mean_sigma_bp, "beta_bp": effect_bp, "se_bp": se * mean_sigma_bp,
           "ci95_bp": [(beta - 1.96 * se) * mean_sigma_bp, (beta + 1.96 * se) * mean_sigma_bp],
           "mde_sigma": MDE_FACTOR * se, "mde_bp": MDE_FACTOR * se * mean_sigma_bp,
           "mde_multiplier_holm": holm_factor, "mde_sigma_holm": holm_factor * se,
           "mde_bp_holm": holm_factor * se * mean_sigma_bp,
           "hurdle_bp": ROUND_TRIP_COST_BP, "hurdle_sigma": hurdle_sigma,
           "effect_minus_hurdle_bp": effect_bp - ROUND_TRIP_COST_BP}
    empty = {"n": 0, "mean": float("nan"), "se": float("nan"), "ci95": [float("nan")] * 2}
    out["members_mean_bp"] = members if members is not None else empty
    out["controls_mean_bp"] = controls if controls is not None else empty
    out["members_minus_hurdle_bp"] = out["members_mean_bp"]["mean"] - ROUND_TRIP_COST_BP
    return out


def summarize_cell(sample: dict, *, hour_effects: bool, B: int = BOOTSTRAP_B) -> dict:
    D = sample["D"].astype(bool)
    members, controls = int(D.sum()), int((~D).sum())
    out = {"cell": sample["cell"].id, "signed": sample["signed"], "description": sample["cell"].description,
           "members": members, "controls": controls, "days": int(np.unique(sample["day"]).size),
           "excluded_missing_controls": sample["excluded_missing_controls"],
           "excluded_missing_outcome": sample["excluded_missing_outcome"],
           "low_members_flag": members < MIN_MEMBERS_FLAG, "mde_sigma": None, "mde_sigma_holm": None,
           "mean_y_members": float(sample["y"][D].mean()) if members else float("nan"),
           "mean_y_controls": float(sample["y"][~D].mean()) if controls else float("nan")}
    fit = regression(sample, hour_effects=hour_effects)
    out["regression"] = fit
    out["stratified"] = stratified(sample, hour_effects=hour_effects, B=B)
    if sample["intraday"]:  # robustness beside the primary result; not part of the Holm family
        out["stratified_session"] = stratified(sample, hour_effects=hour_effects, B=B, with_session=True)
    out["hit_rates"] = hit_rates(sample)
    if fit is not None:
        out["mde_sigma"] = MDE_FACTOR * fit["se"]
        out["mde_sigma_holm"] = holm_mde_multiplier() * fit["se"]
        if sample["signed"] and members:
            signed_bp = sample["s"] * sample["ret_bp"]
            out["cost"] = cost_lines(fit["beta"], fit["se"], float(sample["sigma_tf"][D].mean()) * 1e4,
                                     day_mean(signed_bp[D], sample["day"][D], B),
                                     day_mean(signed_bp[~D], sample["day"][~D], B))
    return out


# ---------------------------------------------------------------- descriptive tables


def _mean_split(sample: dict, mask=None) -> dict:
    D = sample["D"].astype(bool)
    mask = np.ones(len(D), bool) if mask is None else mask
    m, c = sample["y"][mask & D], sample["y"][mask & ~D]
    return {"members": int(m.size), "controls": int(c.size),
            "mean_members": float(m.mean()) if m.size else float("nan"),
            "mean_controls": float(c.mean()) if c.size else float("nan"),
            "difference": float(m.mean() - c.mean()) if m.size and c.size else float("nan")}


def by_split(sample: dict, key: str) -> dict:
    label = (lambda v: str(int(v))) if key == "tf" else str  # noqa: E731
    return {label(value): _mean_split(sample, sample[key] == value) for value in sorted(set(sample[key].tolist()))}


def horizons(sample: dict) -> dict | None:
    """m = 2 and 4 for signed cells: s * ret_m_sigma and the side's MFE / MAE (members vs controls)."""
    if not sample["signed"]:
        return None
    rows, s, D = sample["rows"], sample["s"], sample["D"].astype(bool)
    out = {}
    for m in (2, 4):
        ok = rows[f"fwd_ok_{m}"] == 1
        long = s > 0
        values = {"ret": s * rows[f"ret_{m}_sigma"],
                  "mfe": np.where(long, rows[f"mfe_long_{m}_sigma"], rows[f"mae_long_{m}_sigma"]),
                  "mae": np.where(long, rows[f"mae_long_{m}_sigma"], rows[f"mfe_long_{m}_sigma"])}
        out[str(m)] = {name: {"members": float(np.nanmean(v[ok & D])) if (ok & D).any() else float("nan"),
                              "controls": float(np.nanmean(v[ok & ~D])) if (ok & ~D).any() else float("nan")}
                       for name, v in values.items()}
        out[str(m)]["n_members"], out[str(m)]["n_controls"] = int((ok & D).sum()), int((ok & ~D).sum())
    return out


def subtypes(sample: dict) -> dict:
    D = sample["D"].astype(bool)
    out = {"controls": {"n": int((~D).sum()),
                        "mean_y": float(sample["y"][~D].mean()) if (~D).any() else float("nan")}}
    for name in sorted({str(v) for v in sample["subtype"][D]}):
        mask = D & (sample["subtype"] == name)
        out[name or "none"] = {"n": int(mask.sum()), "mean_y": float(sample["y"][mask].mean())}
    return out


def row_spearman(x, y, day, B: int = BOOTSTRAP_B, seed: int = BOOTSTRAP_SEED) -> dict:
    """Row-level Spearman rho of (x, y) with a day-cluster bootstrap 95% interval (whole UTC days resampled)."""
    x, y, day = np.asarray(x, float), np.asarray(y, float), np.asarray(day)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y, day = x[ok], y[ok], day[ok]
    if x.size < 3:
        return {"n": int(x.size), "rho": float("nan"), "ci95": [float("nan")] * 2}
    rho = spearman_points(x, y)
    if not B:
        return {"n": int(x.size), "rho": rho, "ci95": [float("nan")] * 2}
    _, cluster = np.unique(day, return_inverse=True)
    groups = int(cluster.max()) + 1
    order = np.argsort(cluster, kind="stable")
    starts = np.searchsorted(cluster[order], np.arange(groups + 1))
    members = [order[starts[g]:starts[g + 1]] for g in range(groups)]
    rhos = []
    for row in cluster_weights(groups, B, seed).astype(np.int64):
        take = np.concatenate([np.repeat(members[g], row[g]) for g in np.flatnonzero(row)])
        rhos.append(spearman_points(x[take], y[take]))
    return {"n": int(x.size), "rho": rho, "ci95": _interval(rhos)}


def dose_response(sample: dict, variable: str, B: int = BOOTSTRAP_B, seed: int = BOOTSTRAP_SEED) -> dict:
    """Within members: quintile means of y on ``variable`` (descriptive) and the ROW-level Spearman of (variable,
    y) with a day-cluster bootstrap CI. A Spearman over 5 bucket means had only 5 points and is not reported."""
    D = sample["D"].astype(bool)
    x, y = sample["rows"][variable][D], sample["y"][D]
    ok = np.isfinite(x)
    x, y, day = x[ok], y[ok], sample["day"][D][ok]
    if x.size < DOSE_BUCKETS:
        return {"variable": variable, "n": int(x.size), "not_tested": True}
    edges = np.quantile(x, np.linspace(0, 1, DOSE_BUCKETS + 1)[1:-1])
    bucket = np.digitize(x, edges)
    _, cluster = np.unique(day, return_inverse=True)
    groups = int(cluster.max()) + 1
    sums, counts = np.zeros((DOSE_BUCKETS, groups)), np.zeros((DOSE_BUCKETS, groups))
    np.add.at(sums, (bucket, cluster), y)
    np.add.at(counts, (bucket, cluster), 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        means = sums.sum(axis=1) / counts.sum(axis=1)
    rank = row_spearman(x, y, day, B, seed)
    return {"variable": variable, "n": int(x.size), "edges": [float(e) for e in edges],
            "bucket_means": [float(v) for v in means], "bucket_n": [int(v) for v in counts.sum(axis=1)],
            "spearman_row": rank["rho"], "spearman_row_ci95": rank["ci95"],
            "spearman_basis": "row level (variable, y) within members; day-cluster bootstrap"}


# ---------------------------------------------------------------- population, base rates, level diagnostics


def tf_frames(frame: dict, tfs) -> dict:
    return {tf: subset(frame, frame["tf"] == tf) for tf in tfs if (frame["tf"] == tf).any()}


def population_counts(frame: dict) -> dict:
    """Counts only: segment rows, invalid candles, rows without a valid one-candle window, the population."""
    invalid = frame["status"] == "invalid"
    window = ~invalid & (frame["fwd_ok_1"] != 1)
    return {"segment_rows": int(len(invalid)), "invalid": int(invalid.sum()),
            "no_forward_window": int(window.sum()), "population": int(sc.population(frame).sum())}


def population_report(intraday_frames: dict, daily_frames: dict) -> dict:
    """Population counts per timeframe (intraday and daily/weekly) plus the intraday total."""
    by_tf = {str(tf): population_counts(f) for tf, f in sorted({**intraday_frames, **daily_frames}.items())}
    total = {key: sum(row[key] for tf, row in by_tf.items() if int(tf) in INTRADAY_TFS)
             for key in ("segment_rows", "invalid", "no_forward_window", "population")}
    return {"intraday_total": total, "by_tf": by_tf}


def context_base_rates(intraday_frames: dict) -> dict:
    """Per cell and intraday timeframe: context rows, events and controls (counts and shares of the population)."""
    out = {}
    for cell in sc.CELLS:
        rows = {}
        for tf, f in sorted(intraday_frames.items()):
            pop = sc.population(f)
            context, _ = cell.rule(f, True)
            in_context = pop & context
            events = in_context & sc.event(f)
            controls = in_context & sc.control(f)
            n = int(pop.sum())
            rows[str(tf)] = {"population": n, "context": int(in_context.sum()), "events": int(events.sum()),
                             "controls": int(controls.sum()),
                             "context_share": int(in_context.sum()) / n if n else float("nan"),
                             "event_share": int(events.sum()) / n if n else float("nan"),
                             "control_share": int(controls.sum()) / n if n else float("nan")}
        out[cell.id] = rows
    return out


def level_diagnostics(frames: dict, label: str) -> dict:
    """Touch base rates per level set (and any_touch) for the frames of one timeframe family.

    Evaluated rows are the non-invalid rows whose touch flag is not empty. A set with a touch share above
    TOUCH_SHARE_MAX or below TOUCH_SHARE_MIN is labelled degenerate / "not interpretable": it stays in the
    output and is never counted as a null result. For round levels the median round step (price units) over
    the median candle range is reported per symbol and as the median over symbols.
    """
    out = {}
    for tf, f in sorted(frames.items()):
        rows = {}
        for name, column in TOUCH_COLUMNS:
            flag = f[column]
            evaluated = (f["status"] != "invalid") & np.isfinite(flag)
            n = int(evaluated.sum())
            share = float(flag[evaluated].mean()) if n else float("nan")
            entry = {"evaluated": n, "touch_share": share}
            if n and sc.degenerate_touch(share):
                entry["label"] = "degenerate: not interpretable"
            elif not n:
                entry["label"] = "not applicable (no evaluated rows)"
            if name == "round" and n:
                ratios = {}
                for symbol in sorted(set(f["symbol"][evaluated].tolist())):
                    mask = evaluated & (f["symbol"] == symbol) & (f["range"] > 0)
                    if mask.any():
                        ratios[symbol] = lv.ROUND_SPACING[symbol] / lv.SCALE / float(np.median(f["range"][mask]))
                entry["round_step_over_range"] = {"median": float(np.median(list(ratios.values()))) if ratios else None,
                                                  "by_symbol": ratios}
            rows[name] = entry
        out[str(tf)] = rows
    return {"family": label, "thresholds": {"degenerate_above": sc.TOUCH_SHARE_MAX,
                                            "degenerate_below": sc.TOUCH_SHARE_MIN},
            "note": "cells C1 and C4 use any_touch; a degenerate any_touch makes them not interpretable at that "
                    "timeframe", "by_tf": out}


def diagnostics(intraday_frames: dict, daily_frames: dict) -> dict:
    return {"population": population_report(intraday_frames, daily_frames),
            "base_rates": {"context": context_base_rates(intraday_frames)},
            "level_diagnostics": {"intraday": level_diagnostics(intraday_frames, "intraday"),
                                  "daily_weekly": level_diagnostics(daily_frames, "daily_weekly")}}


# ---------------------------------------------------------------- sessions and sigma seasonality (descriptive)


def _masked(sample: dict, mask) -> dict:
    return {key: (value[mask] if isinstance(value, np.ndarray) else value) for key, value in sample.items()
            if key != "rows"}


def session_table(frames: dict, B: int = BOOTSTRAP_B) -> dict:
    """DESCRIPTIVE: per cell x timeframe x session. Sessions come from the decision time (hour of end_ms, UTC).

    A session with fewer events than MIN_EVENTS_DESCRIPTIVE is "too few" (counts shown, no estimates).
    Signed cells report the signed return in bp; unsigned cells (A, C5) the next-candle ln range in sigma.
    """
    out = {}
    for cell in sc.CELLS:
        by_tf = {}
        for tf, f in sorted(frames.items()):
            sample = cell_sample(f, cell, intraday=True)
            rows = {}
            for session in sc.SESSION_NAMES:
                part = _masked(sample, sample["session"] == session)
                D = part["D"].astype(bool)
                row = {"events": int(D.sum()), "controls": int((~D).sum())}
                if row["events"] < sc.MIN_EVENTS_DESCRIPTIVE:
                    row["status"] = "too few"
                else:
                    value = part["s"] * part["ret_bp"] if cell.signed else part["y"]
                    members, controls = day_mean(value[D], part["day"][D], B), day_mean(value[~D], part["day"][~D], B)
                    row.update(status="ok", unit="bp (signed)" if cell.signed else "ln range, sigma",
                               members=members, controls_mean=controls,
                               difference=members["mean"] - controls["mean"], hit_rates=hit_rates(part))
                rows[session] = row
            by_tf[str(tf)] = rows
        out[cell.id] = by_tf
    return {"label": "DESCRIPTIVE / exploratory: not part of the K = 9 tests; a pattern in one session only is "
                     "session-specific", "min_events": sc.MIN_EVENTS_DESCRIPTIVE, "cells": out}


def sigma_seasonality(frames: dict) -> dict:
    """DESCRIPTIVE: control rows of cell A only (population, range_atr >= 0.5, body_ratio > 0.10), per intraday
    timeframe and UTC hour of the decision time: mean |ret_1_sigma| (the next candle's |return| / sigma_tf) and the
    count; ratio = max / min hourly mean. About flat when sigma is well scaled. Sigma is not changed."""
    out = {}
    for tf, f in sorted(frames.items()):
        rows = sc.population(f) & sc.control(f) & np.isfinite(f["ret_1_sigma"]) & np.isfinite(f["decision_ms"])
        hour = (np.floor_divide(f["decision_ms"][rows], 3_600_000) % 24).astype(int)
        value = np.abs(f["ret_1_sigma"][rows])
        hours = {str(h): {"mean": float(value[hour == h].mean()), "count": int((hour == h).sum())}
                 for h in range(24) if (hour == h).any()}
        means = [row["mean"] for row in hours.values()]
        out[str(tf)] = {"by_hour": hours, "controls": int(rows.sum()),
                        "max_over_min": (max(means) / min(means)) if means and min(means) > 0 else None}
    return {"label": "DESCRIPTIVE: control rows only; hour = UTC hour of the decision time (the next candle's open)",
            "sigma_model": "ewma-3d-5m-v1 (unchanged)", "by_tf": out}


# ---------------------------------------------------------------- study


def primary(frame: dict, B: int = BOOTSTRAP_B) -> list:
    """The K = 9 pre-registered tests (intraday rows), with Holm-adjusted p-values."""
    results = [summarize_cell(cell_sample(frame, cell, intraday=True), hour_effects=True, B=B) for cell in sc.CELLS]
    adjusted = holm([None if r["regression"] is None else r["regression"]["p"] for r in results])
    for result, p in zip(results, adjusted):
        result["p_holm"] = p
    return results


def descriptive(frame: dict, daily_frames: dict, B: int = BOOTSTRAP_B) -> dict:
    out = {"label": "DESCRIPTIVE: not part of the K = 9 primary tests; no multiplicity correction",
           "by_timeframe": {}, "by_symbol": {}, "horizons": {}, "sensitivity_body_0_05": {},
           "sensitivity_range_atr_1_0": {}, "subtypes": {}, "dose_response_A": [], "daily_weekly": {},
           "sessions": session_table(tf_frames(frame, INTRADAY_TFS), B),
           "sigma_seasonality": sigma_seasonality(tf_frames(frame, INTRADAY_TFS))}
    for cell in sc.CELLS:
        sample = cell_sample(frame, cell, intraday=True)
        out["by_timeframe"][cell.id] = by_split(sample, "tf")
        out["by_symbol"][cell.id] = by_split(sample, "symbol")
        out["horizons"][cell.id] = horizons(sample)
        out["subtypes"][cell.id] = subtypes(sample)
        for key, options in (("sensitivity_body_0_05", {"body_max": sc.BODY_MAX_STRICT}),
                             ("sensitivity_range_atr_1_0", {"range_atr_min": sc.RANGE_ATR_LARGE})):
            variant = cell_sample(frame, cell, intraday=True, **options)
            out[key][cell.id] = {"members": int(variant["D"].sum()),
                                 "regression": regression(variant, hour_effects=True)}
        if cell.id == "A":
            out["dose_response_A"] = [dose_response(sample, v, B) for v in DOSE_VARIABLES]
    for tf, daily in sorted(daily_frames.items()):
        family = {}
        for cell in sc.CELLS:
            sample = cell_sample(daily, cell, intraday=False)
            members = int(sample["D"].sum())
            if members < MIN_MEMBERS_DAILY:
                family[cell.id] = {"members": members, "not_tested": True}
                continue
            family[cell.id] = {"members": members, "controls": int((1 - sample["D"]).sum()),
                               **_mean_split(sample), "regression": regression(sample, hour_effects=False)}
        out["daily_weekly"][str(tf)] = family
    return out


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def replication(results: list, reference: dict | None) -> dict | None:
    """Same-sign replication of each primary beta against a development reference report."""
    if reference is None:
        return None
    check_report(reference)
    if reference.get("segment") != "development":
        raise ValueError("the reference report must be a development report")
    ref = {row["cell"]: row for row in reference["primary"]}
    out = {}
    for row in results:
        mine = row["regression"]["beta"] if row["regression"] else float("nan")
        theirs = _float((ref.get(row["cell"], {}).get("regression") or {}).get("beta"))
        out[row["cell"]] = None if not (np.isfinite(mine) and np.isfinite(theirs)) else bool(np.sign(mine) == np.sign(theirs))
    return {"reference_hash": reference["report_hash"], "same_sign": out}


HASH_EXCLUDED = ("report_hash", "code_commit", "created_utc")


REQUIRED_KEYS = ("schema", "segment", "cells_version", "event_version", "table_report", "hypotheses_examined",
                 "table_counts", "parameters", "population", "base_rates", "level_diagnostics", "primary",
                 "replication", "descriptive", "notes", "report_hash")
REQUIRED_DESCRIPTIVE = ("sessions", "sigma_seasonality", "daily_weekly")
REQUIRED_POPULATION = ("segment_rows", "invalid", "no_forward_window", "population")
REQUIRED_PRIMARY = ("cell", "members", "controls", "days", "excluded_missing_controls", "excluded_missing_outcome",
                    "mde_sigma", "mde_sigma_holm", "p_holm", "stratified")
REQUIRED_COST = ("beta_bp", "mde_bp", "mde_bp_holm", "mde_multiplier_holm", "hurdle_bp", "effect_minus_hurdle_bp",
                 "members_mean_bp", "controls_mean_bp", "members_minus_hurdle_bp")


def build_report(segment: str, primary_results: list, descriptive_results: dict, diagnostic_results: dict, *,
                 table_report: str, table_counts: dict, reference: dict | None = None, code_commit: str,
                 created_utc: str) -> dict:
    check_segment(segment)
    report = {"schema": SCHEMA, "segment": segment, "cells_version": sc.CELLS_VERSION,
              "event_version": sc.EVENT_VERSION, "table_report": table_report,
              "hypotheses_examined": {"primary": sc.K_PRIMARY, "correction": "Holm over the primary tests",
                                      "descriptive": "labelled descriptive tables, not tested"},
              "table_counts": table_counts,
              "parameters": _render({"round_trip_cost_bp": ROUND_TRIP_COST_BP, "mde_factor": MDE_FACTOR,
                                     "bootstrap_B": BOOTSTRAP_B, "bootstrap_seed": BOOTSTRAP_SEED,
                                     "range_atr_bins": [0.5, *RANGE_BINS], "min_members_flag": MIN_MEMBERS_FLAG,
                                     "min_members_daily": MIN_MEMBERS_DAILY,
                                     "min_events_descriptive": sc.MIN_EVENTS_DESCRIPTIVE,
                                     "mde_multiplier_holm": holm_mde_multiplier(), "mde_alpha": MDE_ALPHA,
                                     "mde_power": MDE_POWER, "ret3_shock_threshold": sc.RET3_SHOCK_THRESHOLD,
                                     "touch_share_degenerate": [sc.TOUCH_SHARE_MIN, sc.TOUCH_SHARE_MAX],
                                     "sessions": {name: [low, high] for low, high, name in sc.SESSIONS}}),
              "population": _render(diagnostic_results["population"]),
              "base_rates": _render(diagnostic_results["base_rates"]),
              "level_diagnostics": _render(diagnostic_results["level_diagnostics"]),
              "primary": _render(primary_results),
              "replication": _render(replication(primary_results, reference)),
              "descriptive": _render(descriptive_results),
              "notes": ["Descriptive screen: never a PASS, no trades, no labels, no ledger entry.",
                        "A significant beta says doji candles differ from same-size non-doji candles in this "
                        "context; it is not a cost-aware trading result. The bp lines compare the effect and the "
                        "members' gross mean with the 11 bp round trip.",
                        "Sessions, the sigma-seasonality diagnostic and the level diagnostics are descriptive; they "
                        "add no hypothesis (K stays 9). A degenerate level set is not interpretable, not a null.",
                        "Unsigned cells (A, C5) test the next-candle log range; they have no direction, hit rate or "
                        "cost line."],
              "code_commit": code_commit, "created_utc": created_utc}
    report["report_hash"] = content_hash({k: v for k, v in report.items() if k not in HASH_EXCLUDED})
    return report


def check_report(report: dict) -> None:
    """Hash and structure: every field the readers rely on (population counts, base rates, level diagnostics,
    the sessions / sigma-seasonality tables, MDE at the Holm alpha, members' mean and the hurdle lines)."""
    if report.get("schema") != SCHEMA:
        raise ValueError("not a doji-study-v1 report")
    if report.get("report_hash") != content_hash({k: v for k, v in report.items() if k not in HASH_EXCLUDED}):
        raise ValueError("report_hash mismatch")
    missing = [key for key in REQUIRED_KEYS if key not in report]
    missing += [f"descriptive.{key}" for key in REQUIRED_DESCRIPTIVE if key not in report.get("descriptive", {})]
    missing += [f"population.{key}" for key in REQUIRED_POPULATION
                if key not in report.get("population", {}).get("intraday_total", {})]
    for row in report.get("primary", []):
        missing += [f"primary.{row.get('cell')}.{key}" for key in REQUIRED_PRIMARY if key not in row]
        if row.get("signed") and "cost" in row:
            missing += [f"primary.{row.get('cell')}.cost.{key}" for key in REQUIRED_COST if key not in row["cost"]]
    if missing:
        raise ValueError("report lacks required fields: " + ", ".join(missing))


def report_dir(report: dict) -> str:
    check_report(report)
    return f"{REPORT_ROOT}/doji-{report['segment']}__{report['report_hash'][:16]}"


def public_lines(report: dict) -> list:
    """Counts, the number of hypotheses examined and the private path; never an effect size."""
    check_report(report)
    lines = [f"doji event study {report['cells_version']} segment {report['segment']}: "
             f"{report['hypotheses_examined']['primary']} primary hypotheses (Holm) + labelled descriptive tables"]
    total = report["population"]["intraday_total"]
    lines.append(f"population intraday segment_rows={int(total['segment_rows'])} invalid={int(total['invalid'])} "
                 f"no_forward_window={int(total['no_forward_window'])} population={int(total['population'])}")
    for tf, row in report["population"]["by_tf"].items():
        lines.append(f"population tf={int(tf)} segment_rows={int(row['segment_rows'])} invalid={int(row['invalid'])} "
                     f"no_forward_window={int(row['no_forward_window'])} population={int(row['population'])}")
    for row in report["primary"]:
        lines.append(f"cell {row['cell']} members={int(row['members'])} controls={int(row['controls'])} "
                     f"days={int(row['days'])} excluded_missing_controls={int(row['excluded_missing_controls'])} "
                     f"excluded_missing_outcome={int(row['excluded_missing_outcome'])}")
    lines += [f"private path {report_dir(report)}/", f"report hash {report['report_hash']}"]
    return lines


CSV_FIELDS = ("cell", "signed", "members", "controls", "days", "mean_y_members", "mean_y_controls", "beta", "se",
              "t", "p", "p_holm", "beta_bp", "mde_sigma", "mde_bp", "mde_sigma_holm", "mde_bp_holm", "hurdle_sigma",
              "effect_minus_hurdle_bp", "members_mean_bp", "controls_mean_bp", "members_minus_hurdle_bp",
              "stratified", "hit_rate_difference", "low_members_flag")


def cells_csv(report: dict) -> str:
    check_report(report)
    lines = [",".join(CSV_FIELDS)]
    for row in report["primary"]:
        fit, cost = row.get("regression") or {}, row.get("cost") or {}
        values = {"cell": row["cell"], "signed": row["signed"], "members": row["members"],
                  "controls": row["controls"], "days": row["days"], "mean_y_members": row["mean_y_members"],
                  "mean_y_controls": row["mean_y_controls"], "beta": fit.get("beta"), "se": fit.get("se"),
                  "t": fit.get("t"), "p": fit.get("p"), "p_holm": row.get("p_holm"),
                  "beta_bp": cost.get("beta_bp"), "mde_sigma": row.get("mde_sigma"), "mde_bp": cost.get("mde_bp"),
                  "hurdle_sigma": cost.get("hurdle_sigma"),
                  "effect_minus_hurdle_bp": cost.get("effect_minus_hurdle_bp"),
                  "mde_sigma_holm": row.get("mde_sigma_holm"), "mde_bp_holm": cost.get("mde_bp_holm"),
                  "members_mean_bp": (cost.get("members_mean_bp") or {}).get("mean"),
                  "controls_mean_bp": (cost.get("controls_mean_bp") or {}).get("mean"),
                  "members_minus_hurdle_bp": cost.get("members_minus_hurdle_bp"),
                  "stratified": (row.get("stratified") or {}).get("estimate"),
                  "hit_rate_difference": (row.get("hit_rates") or {}).get("difference"),
                  "low_members_flag": row["low_members_flag"]}
        lines.append(",".join("" if values[f] is None else str(values[f]) for f in CSV_FIELDS))
    return "\n".join(lines) + "\n"


def _c(value) -> str:
    return "n/a" if value is None else str(value)


def markdown(report: dict) -> str:
    check_report(report)
    lines = [f"# Doji event study ({report['segment']})", "",
             f"Report hash `{report['report_hash']}`; cells {report['cells_version']}, event "
             f"{report['event_version']}; table {report['table_report']}. Hypotheses examined: "
             f"{report['hypotheses_examined']['primary']} primary (Holm); everything under Descriptive is "
             "labelled and untested.", ""]
    lines += [f"- {note}" for note in report["notes"]] + ["", "## Primary (intraday 15/60/240, pooled)", "",
              "| cell | members | controls | days | beta (sigma) | se | 95% CI | p Holm | beta bp | MDE bp | "
              "effect - hurdle bp | stratified (95% CI) | hit diff | flag |", "|" + " --- |" * 14]
    replication_rows = (report.get("replication") or {}).get("same_sign", {})
    for row in report["primary"]:
        fit, cost = row.get("regression") or {}, row.get("cost") or {}
        strat = row.get("stratified") or {}
        flag = "members < 100" if row["low_members_flag"] else ""
        lines.append(f"| {row['cell']} | {row['members']} | {row['controls']} | {row['days']} | {_c(fit.get('beta'))} | "
                     f"{_c(fit.get('se'))} | {_c((fit.get('ci95') or [None])[0])} .. "
                     f"{_c((fit.get('ci95') or [None, None])[1])} | {_c(row.get('p_holm'))} | "
                     f"{_c(cost.get('beta_bp'))} | {_c(cost.get('mde_bp'))} | "
                     f"{_c(cost.get('effect_minus_hurdle_bp'))} | {_c(strat.get('estimate'))} "
                     f"({_c((strat.get('ci95') or [None])[0])} .. {_c((strat.get('ci95') or [None, None])[1])}) | "
                     f"{_c((row.get('hit_rates') or {}).get('difference'))} | {flag} |")
    lines += ["", "## Cost lines (signed cells; gross one-candle bp vs the 11 bp round trip)", "",
              "| cell | members mean bp (95% CI) | controls mean bp (95% CI) | members - hurdle bp | beta - hurdle bp | "
              f"MDE bp (2.8 SE) | MDE bp (Holm, x{_c(report['parameters']['mde_multiplier_holm'])}) |",
              "|" + " --- |" * 7]
    for row in report["primary"]:
        cost = row.get("cost")
        if not cost:
            continue
        members, controls = cost["members_mean_bp"], cost["controls_mean_bp"]
        lines.append(f"| {row['cell']} | {_c(members['mean'])} ({_c(members['ci95'][0])} .. {_c(members['ci95'][1])}) | "
                     f"{_c(controls['mean'])} ({_c(controls['ci95'][0])} .. {_c(controls['ci95'][1])}) | "
                     f"{_c(cost['members_minus_hurdle_bp'])} | {_c(cost['effect_minus_hurdle_bp'])} | "
                     f"{_c(cost['mde_bp'])} | {_c(cost['mde_bp_holm'])} |")
    total = report["population"]["intraday_total"]
    lines += ["", "## Population (intraday)", "",
              f"Segment rows {total['segment_rows']}; invalid {total['invalid']}; no valid forward window "
              f"{total['no_forward_window']}; population {total['population']}. Missing controls per cell are in "
              "the primary table's exclusion fields (cells.csv / summary.json).", "",
              "## Level diagnostics (touch shares; degenerate sets are not interpretable)", ""]
    for family, block in report["level_diagnostics"].items():
        for tf, sets in block["by_tf"].items():
            flagged = [f"{name} ({entry['touch_share']})" for name, entry in sets.items()
                       if "degenerate" in entry.get("label", "")]
            if flagged:
                lines.append(f"- {family} {tf}: degenerate: " + ", ".join(flagged))
    if replication_rows:
        lines += ["", "Same-sign replication vs the development reference "
                      f"`{report['replication']['reference_hash'][:16]}`: "
                  + ", ".join(f"{cell} {_c(value)}" for cell, value in replication_rows.items())]
    lines += ["", "## Cells", ""] + [f"- {row['cell']}: {row['description']}" for row in report["primary"]]
    lines += ["", "## Descriptive (labelled; not tested)", "",
              "Per-timeframe, per-symbol, m = 2/4, sensitivity, subtype, dose-response and daily/weekly tables are in "
              "summary.json under `descriptive`."]
    for tf, family in report["descriptive"].get("daily_weekly", {}).items():
        lines += ["", f"### {tf}-minute family (descriptive)", "", "| cell | members | beta | se |", "|" + " --- |" * 4]
        for cell, row in family.items():
            fit = row.get("regression") or {}
            status = "not tested (members < 30)" if row.get("not_tested") else _c(fit.get("beta"))
            lines.append(f"| {cell} | {row['members']} | {status} | {_c(fit.get('se'))} |")
    return "\n".join(lines) + "\n"


def table_paths(report_dir_path: Path, manifest: dict) -> dict:
    """{tf: [csv.gz paths]} of a candle-table report directory."""
    out = {}
    for item in manifest["files"]:
        tf = int(item["name"].split("__")[2])
        out.setdefault(tf, []).append(Path(report_dir_path) / item["name"])
    return out


def run_study(table_dir: Path, segment: str, *, table_report: str, reference: dict | None = None,
              code_commit: str, created_utc: str, B: int = BOOTSTRAP_B) -> dict:
    """Load a candle-table report directory and build the report (the hidden segment is refused first)."""
    from .candle_table import check_manifest

    check_segment(segment)
    manifest = json.loads((Path(table_dir) / "manifest.json").read_text(encoding="utf-8"))
    check_manifest(manifest)
    paths = table_paths(table_dir, manifest)
    names = STUDY_SEGMENTS[segment]
    intraday = read_table([p for tf in INTRADAY_TFS for p in paths.get(tf, [])])
    intraday = subset(intraday, intraday["segment"] == names["intraday"])
    daily_frames = {}
    for tf in DAILY_TFS:
        if tf in paths:
            frame = read_table(paths[tf])
            daily_frames[tf] = subset(frame, frame["segment"] == names["daily"])
    counts = {"intraday_rows": int(len(intraday["tf"])),
              **{f"rows_{tf}": int(len(frame["tf"])) for tf, frame in daily_frames.items()}}
    primary_results = primary(intraday, B)
    descriptive_results = descriptive(intraday, daily_frames, B)
    diagnostic_results = diagnostics(tf_frames(intraday, INTRADAY_TFS), daily_frames)
    return build_report(segment, primary_results, descriptive_results, diagnostic_results, table_report=table_report,
                        table_counts=counts, reference=reference, code_commit=code_commit, created_utc=created_utc)


__all__ = ["SCHEMA", "read_table", "add_prior20", "cell_sample", "regression", "stratified", "hit_rates", "holm",
           "wilson", "cost_lines", "primary", "descriptive", "build_report", "check_report", "report_dir",
           "public_lines", "cells_csv", "markdown", "run_study"]
