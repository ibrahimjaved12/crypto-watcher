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
import gzip
import io
import json
from math import sqrt
from pathlib import Path
from statistics import NormalDist

import numpy as np

from ..benchmark.calibration import _render, check_segment
from ..benchmark.canonical import content_hash
from ..benchmark.screen import _interval, cluster_weights, ols_clustered, spearman_points
from . import study_cells as sc

SCHEMA = "doji-study-v1"
REPORT_ROOT = "reports/pattern-study"
INTRADAY_TFS = (15, 60, 240)
DAILY_TFS = (1440, 10080)
ROUND_TRIP_COST_BP = 11.0
MDE_FACTOR = 2.8
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
    "sigma_tf", "ret_1_sigma", "ret_1_bp", "ln_range1_sigma", "ret_2_sigma", "ret_4_sigma",
    "mfe_long_2_sigma", "mae_long_2_sigma", "mfe_long_4_sigma", "mae_long_4_sigma")
STUDY_SEGMENTS = {"development": {"intraday": "development", "daily": "development-ext"},
                  "validation": {"intraday": "validation", "daily": "validation"}}


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
            "hour": frame["utc_hour"][keep], "day": (frame["open_ms"][keep] // DAY_MS).astype(np.int64),
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


def strata_codes(sample: dict, *, hour_effects: bool) -> np.ndarray:
    """(symbol, tf, 6-hour bucket, atr_pct tercile, range_atr bin, rel_volume tercile) as one integer code."""
    _, group = np.unique(sample["group"], return_inverse=True)
    hour6 = (sample["hour"] // 6).astype(np.int64) if hour_effects else np.zeros(len(group), np.int64)
    atr = np.digitize(sample["atr_pct"], (1 / 3, 2 / 3))
    size = np.digitize(sample["range_atr"], RANGE_BINS)
    edges = np.quantile(sample["rel_volume"], (1 / 3, 2 / 3)) if len(group) else (0, 0)
    volume = np.digitize(sample["rel_volume"], edges)
    stacked = np.column_stack([group, hour6, atr, size, volume])
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


def stratified(sample: dict, *, hour_effects: bool, B: int = BOOTSTRAP_B, seed: int = BOOTSTRAP_SEED) -> dict:
    """Member-weighted mean over strata of (mean y members - mean y controls); strata need both."""
    code = strata_codes(sample, hour_effects=hour_effects)
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


def cost_lines(beta: float, se: float, mean_sigma_bp: float) -> dict:
    """bp conversion, minimum detectable effect and the 11 bp round-trip hurdle."""
    hurdle_sigma = ROUND_TRIP_COST_BP / mean_sigma_bp if mean_sigma_bp > 0 else float("nan")
    effect_bp = beta * mean_sigma_bp
    return {"mean_sigma_bp": mean_sigma_bp, "beta_bp": effect_bp, "se_bp": se * mean_sigma_bp,
            "ci95_bp": [(beta - 1.96 * se) * mean_sigma_bp, (beta + 1.96 * se) * mean_sigma_bp],
            "mde_sigma": MDE_FACTOR * se, "mde_bp": MDE_FACTOR * se * mean_sigma_bp,
            "hurdle_bp": ROUND_TRIP_COST_BP, "hurdle_sigma": hurdle_sigma,
            "effect_minus_hurdle_bp": effect_bp - ROUND_TRIP_COST_BP}


def summarize_cell(sample: dict, *, hour_effects: bool, B: int = BOOTSTRAP_B) -> dict:
    D = sample["D"].astype(bool)
    members, controls = int(D.sum()), int((~D).sum())
    out = {"cell": sample["cell"].id, "signed": sample["signed"], "description": sample["cell"].description,
           "members": members, "controls": controls, "days": int(np.unique(sample["day"]).size),
           "excluded_missing_controls": sample["excluded_missing_controls"],
           "excluded_missing_outcome": sample["excluded_missing_outcome"],
           "low_members_flag": members < MIN_MEMBERS_FLAG,
           "mean_y_members": float(sample["y"][D].mean()) if members else float("nan"),
           "mean_y_controls": float(sample["y"][~D].mean()) if controls else float("nan")}
    fit = regression(sample, hour_effects=hour_effects)
    out["regression"] = fit
    out["stratified"] = stratified(sample, hour_effects=hour_effects, B=B)
    out["hit_rates"] = hit_rates(sample)
    if fit is not None:
        out["mde_sigma"] = MDE_FACTOR * fit["se"]
        if sample["signed"] and members:
            out["cost"] = cost_lines(fit["beta"], fit["se"], float(sample["sigma_tf"][D].mean()) * 1e4)
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


def dose_response(sample: dict, variable: str, B: int = BOOTSTRAP_B, seed: int = BOOTSTRAP_SEED) -> dict:
    """Within members: quintile means of y on ``variable`` and Spearman(quintile, mean), day-bootstrap CI."""
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
        weights = cluster_weights(groups, B, seed)
        drawn = (weights @ sums.T) / (weights @ counts.T)
    index = np.arange(DOSE_BUCKETS)
    rhos = np.array([spearman_points(index, row) for row in drawn]) if B else np.zeros(0)
    return {"variable": variable, "n": int(x.size), "edges": [float(e) for e in edges],
            "bucket_means": [float(v) for v in means], "bucket_n": [int(v) for v in counts.sum(axis=1)],
            "spearman": spearman_points(index, means), "spearman_ci95": _interval(rhos)}


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
           "sensitivity_range_atr_1_0": {}, "subtypes": {}, "dose_response_A": [], "daily_weekly": {}}
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


def build_report(segment: str, primary_results: list, descriptive_results: dict, *, table_report: str,
                 table_counts: dict, reference: dict | None = None, code_commit: str, created_utc: str) -> dict:
    check_segment(segment)
    report = {"schema": SCHEMA, "segment": segment, "cells_version": sc.CELLS_VERSION,
              "event_version": sc.EVENT_VERSION, "table_report": table_report,
              "hypotheses_examined": {"primary": sc.K_PRIMARY, "correction": "Holm over the primary tests",
                                      "descriptive": "labelled descriptive tables, not tested"},
              "table_counts": table_counts,
              "parameters": _render({"round_trip_cost_bp": ROUND_TRIP_COST_BP, "mde_factor": MDE_FACTOR,
                                     "bootstrap_B": BOOTSTRAP_B, "bootstrap_seed": BOOTSTRAP_SEED,
                                     "range_atr_bins": [0.5, *RANGE_BINS], "min_members_flag": MIN_MEMBERS_FLAG,
                                     "min_members_daily": MIN_MEMBERS_DAILY}),
              "primary": _render(primary_results),
              "replication": _render(replication(primary_results, reference)),
              "descriptive": _render(descriptive_results),
              "notes": ["Descriptive screen: never a PASS, no trades, no labels, no ledger entry.",
                        "A significant beta says doji candles differ from same-size non-doji candles in this "
                        "context; it is not a cost-aware trading result. The bp lines compare it with the 11 bp "
                        "round trip.",
                        "Unsigned cells (A, C5) test the next-candle log range; they have no direction, hit rate or "
                        "cost line."],
              "code_commit": code_commit, "created_utc": created_utc}
    report["report_hash"] = content_hash({k: v for k, v in report.items() if k not in HASH_EXCLUDED})
    return report


def check_report(report: dict) -> None:
    if report.get("schema") != SCHEMA:
        raise ValueError("not a doji-study-v1 report")
    if report.get("report_hash") != content_hash({k: v for k, v in report.items() if k not in HASH_EXCLUDED}):
        raise ValueError("report_hash mismatch")


def report_dir(report: dict) -> str:
    check_report(report)
    return f"{REPORT_ROOT}/doji-{report['segment']}__{report['report_hash'][:16]}"


def public_lines(report: dict) -> list:
    """Counts, the number of hypotheses examined and the private path; never an effect size."""
    check_report(report)
    lines = [f"doji event study {report['cells_version']} segment {report['segment']}: "
             f"{report['hypotheses_examined']['primary']} primary hypotheses (Holm) + labelled descriptive tables"]
    for row in report["primary"]:
        lines.append(f"cell {row['cell']} members={int(row['members'])} controls={int(row['controls'])} "
                     f"days={int(row['days'])} excluded_missing_controls={int(row['excluded_missing_controls'])} "
                     f"excluded_missing_outcome={int(row['excluded_missing_outcome'])}")
    lines += [f"private path {report_dir(report)}/", f"report hash {report['report_hash']}"]
    return lines


CSV_FIELDS = ("cell", "signed", "members", "controls", "days", "mean_y_members", "mean_y_controls", "beta", "se",
              "t", "p", "p_holm", "beta_bp", "mde_sigma", "mde_bp", "hurdle_sigma", "effect_minus_hurdle_bp",
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
    return build_report(segment, primary_results, descriptive_results, table_report=table_report,
                        table_counts=counts, reference=reference, code_commit=code_commit, created_utc=created_utc)


__all__ = ["SCHEMA", "read_table", "add_prior20", "cell_sample", "regression", "stratified", "hit_rates", "holm",
           "wilson", "cost_lines", "primary", "descriptive", "build_report", "check_report", "report_dir",
           "public_lines", "cells_csv", "markdown", "run_study"]
