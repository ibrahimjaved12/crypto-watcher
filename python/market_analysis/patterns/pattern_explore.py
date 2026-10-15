"""Free, descriptive doji exploration over candle-table-v1 (docs/pattern-study.md, "Doji exploration").

DISCOVERY ONLY. No stops are fixed, no outcome is required, nothing is a PASS, a trade or a label. The
module describes what follows a doji (event) compared with same-size non-doji candles (matched
controls) and hour-matched random rows (baseline), direction-free first, then under a few directional
entry rules that are defined without any stop. The only distances anywhere are the excursion-map grid.

Hygiene: the development segment only (validation needs a plan id; the hidden stretch is refused by
the guard); every slice x metric family evaluated is counted (experiment log, BH over all tests); paths
never cross the segment boundary; ambiguous barrier candles are counted as AMBIGUOUS, never resolved
optimistically.

Everything runs PER TIMEFRAME (pooling across timeframes let 15m dominate the doji study).

Units: one-candle sigma of the event row (``sigma_tf``; multi-candle quantities are in one-candle sigma
units, not divided by sqrt(h)), ATR (``atr_prev`` of the event row, price distance / ATR) and bp
(1e4 x log return). Entry reference: the open of candle i+1 unless a rule says otherwise.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
from dataclasses import dataclass
from functools import lru_cache
from math import erf, sqrt
from pathlib import Path
from typing import Callable
import re

import numpy as np

from ..benchmark.calibration import _render, check_segment
from ..benchmark.canonical import content_hash
from ..benchmark.screen import _interval, cluster_weights
from . import study_cells as sc

SCHEMA = "pattern-explore-v1"
EXPLORE_VERSION = "doji-explore-v1"
QUESTION_ID = "doji-explore"
REPORT_ROOT = "reports/pattern-explore"
TIERS = {"intraday": (15, 60, 240), "daily_weekly": (1440, 10080)}
STUDY_SEGMENTS = {"development": {"intraday": "development", "daily_weekly": "development-ext"},
                  "validation": {"intraday": "validation", "daily_weekly": "validation"}}
PATH_N = {15: 24, 60: 24, 240: 24, 1440: 20, 10080: 13}
HORIZONS = {15: (1, 2, 3, 4, 6, 8, 12, 24), 60: (1, 2, 3, 4, 6, 8, 12, 24), 240: (1, 2, 3, 4, 6, 8, 12, 24),
            1440: (1, 2, 3, 4, 6, 8, 12, 20), 10080: (1, 2, 3, 4, 6, 8, 13)}
LEAD_HORIZONS = {15: (4, 12), 60: (4, 12), 240: (4, 12), 1440: (3, 12), 10080: (2, 8)}
BARRIERS_ATR = (0.5, 1.0, 1.5, 2.0, 3.0)
RESOLUTION_ATR = 1.0          # the slice engine's "resolution" family: +/-1 ATR reached within h
BREAK_K = (1, 2, 3, 5)
BREAK_K_PRIMARY = 3
PATH_EXTRA = max(BREAK_K)     # paths are built to N + 5 so the break rule has N candles after the break
GRID_STOPS = (1.0, 1.5, 2.0, 3.0)
GRID_TARGETS = (1.0, 2.0, 3.0, 4.0)
COST_BP = 11.0
QUANTILES = (10, 25, 50, 75, 90)
BOOTSTRAP_B = 1000
BOOTSTRAP_SEED = 20261016
BASELINE_SEED = 20261017
BASELINE_MULTIPLE = 2
REVIEW_SEED = 20261018
LEAD_MIN_N = 100
LEAD_MIN_T = 2.0
LEAD_MIN_SYMBOLS = 4
LEADS_PER_FAMILY = 30
RANGE_BINS = (0.75, 1.0, 1.5, 2.5)
DAY_MS = 86_400_000
PLAN_ID = re.compile(r"[0-9a-f]{64}\Z")

EVENTS = {  # name -> (body_ratio max, range_atr min); "doji" is doji-geo-v2, the others descriptive variants
    "doji": (0.10, 0.5), "doji_b05": (0.05, 0.5), "doji_r10": (0.10, 1.0)}
CONTROL_BODY_MIN, CONTROL_RANGE_MIN = 0.10, 0.5
WICK_LONG, WICK_SHORT = 0.6, 0.1  # geometry's dragonfly / gravestone wick rule, used on controls without the body rule

REQUIRED_NUMERIC = ("tf", "open_ms", "decision_ms", "open", "high", "low", "close", "body_ratio", "range_atr",
                    "atr_prev", "sigma_tf", "upper_ratio", "lower_ratio", "atr_pct_prev", "rel_volume_prior20")
OPTIONAL_NUMERIC = (
    "rel_volume_slot", "adx_prev", "extension_prev_atr", "trend_dir", "trend_agree", "swing_trend_dir",
    "htf_trend_dir", "any_touch", "touch_round", "touch_prev_day", "touch_prev_week", "touch_prev_month",
    "touch_swing_tf", "touch_swing4h", "touch_swing1d", "touch_swing1w", "touch_pool", "touch_day_open",
    "touch_week_open", "touch_month_open", "touch_sig", "sig_nearest_level_dist_atr", "doi_z", "fund_pct90",
    "prem_z", "prev_doji_b10", "confirm_dir", "climax_age6", "climax_vol_max6", "flush_prev_dir",
    "sigma_seas_factor")
REGIME_COLUMNS = ("rg_mkt_t1", "rg_mkt_rv_state", "rg_mkt_dd_state", "rg_mkt_fr_state", "rg_sym_t1",
                  "rg_sym_dd_state")
REQUIRED_TEXT = ("symbol", "segment", "status")
OPTIONAL_TEXT = ("doji_subtype", *REGIME_COLUMNS)
TOUCH_SETS = ("any_touch", "touch_round", "touch_prev_day", "touch_prev_week", "touch_prev_month", "touch_swing_tf",
              "touch_swing4h", "touch_swing1d", "touch_swing1w", "touch_pool", "touch_day_open", "touch_week_open",
              "touch_month_open", "touch_sig")
TREND_DEFINITIONS = ("trend_dir", "trend_agree", "swing_trend_dir", "htf_trend_dir")
FAMILIES = ("expansion", "resolution", "drift", "break_follow", "subtype_dir")


class ExploreError(ValueError):
    """Invalid exploration input (segment, tier, table)."""


def check_explore_segment(segment: str, plan_id: str = "") -> str:
    """development, or validation with a plan id (sha256 hex); the hidden stretch is refused by check_segment."""
    check_segment(segment)
    if segment != "development" and not PLAN_ID.match(plan_id or ""):
        raise ExploreError("exploration runs on development; validation needs the plan id of a committed plan")
    return segment


def check_tier(tier: str) -> tuple:
    if tier not in TIERS:
        raise ExploreError(f"tier must be one of {', '.join(TIERS)}")
    return TIERS[tier]


# ---------------------------------------------------------------- table input


def read_frame(paths, segment_name: str) -> tuple[dict, list]:
    """(frame, absent optional columns). Every row of the files is kept (paths need neighbours); ``in_segment``
    marks the rows of the requested segment. Files are one symbol x timeframe each, sorted by open time."""
    numeric = {name: [] for name in (*REQUIRED_NUMERIC, *OPTIONAL_NUMERIC)}
    text = {name: [] for name in (*REQUIRED_TEXT, *OPTIONAL_TEXT)}
    absent = set()
    for path in paths:
        with io.TextIOWrapper(gzip.open(path, "rb"), encoding="ascii", newline="") as stream:
            reader = csv.reader(stream)
            header = next(reader)
            missing = [name for name in (*REQUIRED_NUMERIC, *REQUIRED_TEXT) if name not in header]
            if missing:
                raise ExploreError(f"{Path(path).name}: table lacks columns {', '.join(missing)}")
            where = {name: header.index(name) for name in (*numeric, *text) if name in header}
            absent |= {name for name in (*numeric, *text) if name not in where}
            for row in reader:
                for name in numeric:
                    k = where.get(name)
                    value = row[k] if k is not None else ""
                    numeric[name].append(float(value) if value != "" else np.nan)
                for name in text:
                    k = where.get(name)
                    text[name].append(row[k] if k is not None else "")
    frame = {name: np.asarray(values, dtype=float) for name, values in numeric.items()}
    frame.update({name: np.asarray(values, dtype=object) for name, values in text.items()})
    frame["in_segment"] = frame["segment"] == segment_name
    return frame, sorted(absent)


def subset(frame: dict, mask) -> dict:
    return {name: values[mask] for name, values in frame.items()}


def population(f: dict) -> np.ndarray:
    """Segment rows of non-invalid candles with a usable ATR and sigma."""
    with np.errstate(invalid="ignore"):
        return f["in_segment"] & (f["status"] != "invalid") & (f["atr_prev"] > 0) & (f["sigma_tf"] > 0)


def event_mask(f: dict, name: str = "doji") -> np.ndarray:
    body, size = EVENTS[name]
    with np.errstate(invalid="ignore"):
        return population(f) & (f["body_ratio"] <= body) & (f["range_atr"] >= size)


def control_mask(f: dict) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return population(f) & (f["body_ratio"] > CONTROL_BODY_MIN) & (f["range_atr"] >= CONTROL_RANGE_MIN)


def hours(f: dict) -> np.ndarray:
    return (np.floor_divide(f["open_ms"], 3_600_000) % 24).astype(np.int64)


def days(f: dict) -> np.ndarray:
    return np.floor_divide(f["open_ms"], DAY_MS).astype(np.int64)


def baseline_mask(f: dict, events, seed: int = BASELINE_SEED, multiple: int = BASELINE_MULTIPLE) -> np.ndarray:
    """Population rows sampled without replacement per (symbol, UTC hour of the open) with ``multiple`` x the
    events of that cell (all rows when fewer), so the baseline's hour mix matches the events'. Seeded."""
    rng = np.random.default_rng(seed)
    pop = population(f)
    hour, symbol = hours(f), f["symbol"]
    out = np.zeros(len(pop), bool)
    cells = sorted({(s, int(h)) for s, h in zip(symbol[events], hour[events])})
    for s, h in cells:
        wanted = multiple * int((events & (symbol == s) & (hour == h)).sum())
        candidates = np.flatnonzero(pop & (symbol == s) & (hour == h))
        if candidates.size <= wanted:
            out[candidates] = True
        else:
            out[rng.choice(candidates, wanted, replace=False)] = True
    return out


# ---------------------------------------------------------------- paths


@dataclass
class Paths:
    """Forward candles i+1..i+L of selected rows (NaN beyond ``length``)."""
    rows: np.ndarray     # frame indices
    open: np.ndarray     # (n, L)
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    length: np.ndarray   # consecutive usable candles after row i (0..L)

    @property
    def entry(self) -> np.ndarray:
        return self.open[:, 0]


def build_paths(f: dict, rows, steps: int) -> Paths:
    """A step k is usable iff row i+k exists, belongs to the same symbol and timeframe, opens exactly k candles
    later, is not invalid and lies in the same segment as row i; the path stops at the first unusable step."""
    rows = np.asarray(rows, np.int64)
    n, total = rows.size, len(f["tf"])
    out = {name: np.full((n, steps), np.nan) for name in ("open", "high", "low", "close")}
    alive = np.ones(n, bool)
    length = np.zeros(n, np.int64)
    step_ms = f["tf"][rows] * 60_000
    for k in range(1, steps + 1):
        j = rows + k
        inside = j < total
        jj = np.where(inside, j, 0)
        ok = (alive & inside & (f["symbol"][jj] == f["symbol"][rows]) & (f["tf"][jj] == f["tf"][rows])
              & (f["open_ms"][jj] - f["open_ms"][rows] == k * step_ms) & (f["status"][jj] != "invalid")
              & (f["segment"][jj] == f["segment"][rows]))
        for name in out:
            out[name][ok, k - 1] = f[name][jj[ok]]
        length += ok
        alive = ok
    return Paths(rows, out["open"], out["high"], out["low"], out["close"], length)


def direction_free(p: Paths, f: dict, h: int) -> dict:
    """Per row (NaN where the path is shorter than h): up/down excursion and range (sigma, ATR), drift (sigma, bp)."""
    ok = p.length >= h
    rows = p.rows
    sigma, atr = f["sigma_tf"][rows], f["atr_prev"][rows]
    entry = p.entry
    with np.errstate(invalid="ignore", divide="ignore"):
        high = np.fmax.reduce(p.high[:, :h], axis=1)  # NaN only when the whole window is missing (no warning)
        low = np.fmin.reduce(p.low[:, :h], axis=1)
        close = p.close[:, h - 1]
        out = {"up_sigma": np.log(high / entry) / sigma, "down_sigma": np.log(entry / low) / sigma,
               "up_atr": (high - entry) / atr, "down_atr": (entry - low) / atr,
               "range_sigma": np.log(high / low) / sigma, "range_atr": (high - low) / atr,
               "drift_sigma": np.log(close / entry) / sigma, "drift_bp": 1e4 * np.log(close / entry)}
    return {name: np.where(ok, value, np.nan) for name, value in out.items()}


def first_passage(p: Paths, f: dict, k: float) -> tuple[np.ndarray, np.ndarray]:
    """(first up index, first down index) of the +/-k ATR barriers around the entry (L when never, within the path)."""
    atr = f["atr_prev"][p.rows]
    L = p.high.shape[1]
    up_hit = p.high >= (p.entry + k * atr)[:, None]
    down_hit = p.low <= (p.entry - k * atr)[:, None]
    first_up = np.where(up_hit.any(axis=1), up_hit.argmax(axis=1), L)
    first_down = np.where(down_hit.any(axis=1), down_hit.argmax(axis=1), L)
    return first_up, first_down


def barrier_outcomes(p: Paths, f: dict, k: float, h: int) -> dict:
    """Per row: up_first / down_first / ambiguous (both inside the same candle) / neither within h; NaN-coded where
    the path is shorter than h and no barrier was reached before it ended (unknown)."""
    first_up, first_down = first_passage(p, f, k)
    first = np.minimum(first_up, first_down)
    reached = first < np.minimum(h, p.length)  # first == the path width means "never reached"
    known = reached | (p.length >= h)
    code = np.full(len(first), np.nan)  # 1 up first, -1 down first, 0 ambiguous, 2 neither
    code[known & ~reached] = 2
    code[reached & (first_up < first_down)] = 1
    code[reached & (first_down < first_up)] = -1
    code[reached & (first_up == first_down)] = 0
    time = np.where(reached, first + 1, np.nan)
    return {"code": code, "time": time}


# ---------------------------------------------------------------- directional rules (no stop anywhere)


def break_rule(p: Paths, f: dict, K: int) -> dict:
    """First close beyond the event candle's high (long) or low (short) among candles i+1..i+K.

    Returns per row: direction (+1/-1, 0 no break within K, NaN when the path ended before a break was decided),
    the break offset b (candle i+1+b) and the entry = that close."""
    rows = p.rows
    high, low = f["high"][rows], f["low"][rows]
    n = len(rows)
    direction = np.full(n, np.nan)
    offset = np.full(n, -1, np.int64)
    open_ = np.ones(n, bool)
    for b in range(K):
        usable = open_ & (p.length > b)
        close = p.close[:, b]
        up, down = usable & (close > high), usable & (close < low)
        direction[up], direction[down] = 1.0, -1.0
        offset[up | down] = b
        open_ &= ~(up | down)
        stopped = open_ & (p.length <= b)  # path ended before the decision
        open_ &= ~stopped
    direction[open_] = 0.0  # every K candle seen, no break
    entry = np.where(offset >= 0, p.close[np.arange(n), np.maximum(offset, 0)], np.nan)
    return {"direction": direction, "offset": offset, "entry": entry}


def follow_through(p: Paths, f: dict, direction, offset, entry, h: int) -> dict:
    """direction * log return from the entry (close of candle i+1+offset; offset -1 = open of i+1) to the close h
    candles later, with the side's MFE / MAE over those h candles, in sigma, ATR and bp. NaN when unavailable."""
    n = len(p.rows)
    sigma, atr = f["sigma_tf"][p.rows], f["atr_prev"][p.rows]
    start = offset + 1  # first candle after the entry
    end = start + h     # exclusive
    d = np.asarray(direction, float)
    ok = (d != 0) & np.isfinite(d) & (p.length >= end) & np.isfinite(entry)
    L = p.high.shape[1]
    ok &= end <= L
    idx = np.arange(L)
    window = (idx[None, :] >= start[:, None]) & (idx[None, :] < end[:, None])
    with np.errstate(invalid="ignore", divide="ignore"):
        high = np.nanmax(np.where(window, p.high, -np.inf), axis=1)
        low = np.nanmin(np.where(window, p.low, np.inf), axis=1)
        close = p.close[np.arange(n), np.clip(end - 1, 0, L - 1)]
        r = d * np.log(close / entry)
        mfe = np.where(d > 0, np.log(high / entry), np.log(entry / low))
        mae = np.where(d > 0, np.log(entry / low), np.log(high / entry))
        price_move = d * (close - entry)
        out = {"ret_sigma": r / sigma, "ret_bp": 1e4 * r, "ret_atr": price_move / atr,
               "mfe_sigma": mfe / sigma, "mae_sigma": mae / sigma,
               "mfe_atr": np.where(d > 0, high - entry, entry - low) / atr,
               "mae_atr": np.where(d > 0, entry - low, high - entry) / atr}
    return {name: np.where(ok, value, np.nan) for name, value in out.items()}


def subtype_direction(f: dict, rows, events) -> np.ndarray:
    """Events: dragonfly +1, gravestone -1, other subtypes 0. Controls: the same wick rule without the body rule
    (lower_ratio >= 0.6 and upper_ratio <= 0.1: +1; the mirror: -1; else 0)."""
    rows = np.asarray(rows)
    subtype = f["doji_subtype"][rows]
    upper, lower = f["upper_ratio"][rows], f["lower_ratio"][rows]
    with np.errstate(invalid="ignore"):
        wick = np.where((lower >= WICK_LONG) & (upper <= WICK_SHORT), 1.0,
                        np.where((upper >= WICK_LONG) & (lower <= WICK_SHORT), -1.0, 0.0))
    named = np.where(subtype == "dragonfly", 1.0, np.where(subtype == "gravestone", -1.0, 0.0))
    return np.where(np.asarray(events)[rows], named, wick)


# ---------------------------------------------------------------- statistics


@lru_cache(maxsize=16)
def _weights(groups: int, B: int, seed: int) -> np.ndarray:
    """screen.cluster_weights, cached: the same day count and B reuse one draw matrix (read-only)."""
    out = cluster_weights(groups, B, seed)
    out.setflags(write=False)
    return out


def _p_two_sided(t: float) -> float:
    return float("nan") if not np.isfinite(t) else 1 - erf(abs(t) / sqrt(2))


def weighted_quantiles(values, weights, qs=QUANTILES) -> list:
    order = np.argsort(values)
    v, w = values[order], weights[order]
    cum = np.cumsum(w)
    if not v.size or cum[-1] <= 0:
        return [float("nan")] * len(qs)
    return [float(v[min(np.searchsorted(cum, q / 100 * cum[-1]), v.size - 1)]) for q in qs]


def describe(values, day, weights=None, B: int = 0, seed: int = BOOTSTRAP_SEED) -> dict:
    """n, mean, median, quantiles, share > 0, day-clustered SE (analytic) and, with B > 0, a day-cluster
    bootstrap 95% interval of the (weighted) mean."""
    values = np.asarray(values, float)
    weights = np.ones(values.size) if weights is None else np.asarray(weights, float)
    ok = np.isfinite(values) & (weights > 0)
    v, w, d = values[ok], weights[ok], np.asarray(day)[ok]
    if not v.size:
        return {"n": 0, "mean": float("nan"), "median": float("nan"), "quantiles": [float("nan")] * len(QUANTILES),
                "share_positive": float("nan"), "se": float("nan"), "ci95": [float("nan")] * 2, "days": 0}
    total = w.sum()
    mean = float((w * v).sum() / total)
    _, cluster = np.unique(d, return_inverse=True)
    groups = int(cluster.max()) + 1
    u = np.bincount(cluster, w * (v - mean), groups) / total
    se = float(sqrt(groups / (groups - 1) * (u ** 2).sum())) if groups > 1 else float("nan")
    out = {"n": int(v.size), "mean": mean, "median": weighted_quantiles(v, w, (50,))[0],
           "quantiles": weighted_quantiles(v, w), "share_positive": float(w[v > 0].sum() / total), "se": se,
           "days": groups, "ci95": [float("nan")] * 2}
    if B:
        draws = _weights(groups, B, seed)
        num, den = np.bincount(cluster, w * v, groups), np.bincount(cluster, w, groups)
        with np.errstate(invalid="ignore", divide="ignore"):
            out["ci95"] = _interval((draws @ num) / (draws @ den))
    return out


def difference(a, a_day, b, b_day, b_weights=None, B: int = 0, seed: int = BOOTSTRAP_SEED) -> dict:
    """mean(a) - weighted mean(b), with a day-clustered SE over the union of days (shared days covary), t, p and,
    with B > 0, a joint day-cluster bootstrap 95% interval."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    wb = np.ones(b.size) if b_weights is None else np.asarray(b_weights, float)
    oka, okb = np.isfinite(a), np.isfinite(b) & (wb > 0)
    a, ad, b, bd, wb = a[oka], np.asarray(a_day)[oka], b[okb], np.asarray(b_day)[okb], wb[okb]
    nan = {"difference": float("nan"), "se": float("nan"), "t": float("nan"), "p": float("nan"),
           "ci95": [float("nan")] * 2, "n_a": int(a.size), "n_b": int(b.size)}
    if not a.size or not b.size:
        return nan
    ma, mb = a.mean(), (wb * b).sum() / wb.sum()
    _, cluster = np.unique(np.concatenate([ad, bd]), return_inverse=True)
    ca, cb = cluster[:a.size], cluster[a.size:]
    groups = int(cluster.max()) + 1
    u = np.bincount(ca, (a - ma) / a.size, groups) - np.bincount(cb, wb * (b - mb) / wb.sum(), groups)
    se = sqrt(groups / (groups - 1) * (u ** 2).sum()) if groups > 1 else float("nan")
    diff = float(ma - mb)
    t = diff / se if se and np.isfinite(se) and se > 0 else float("nan")
    out = {**nan, "difference": diff, "se": float(se), "t": float(t), "p": _p_two_sided(t)}
    if B:
        draws = _weights(groups, B, seed)
        sa, na = np.bincount(ca, a, groups), np.bincount(ca, None, groups)
        sb, nb = np.bincount(cb, wb * b, groups), np.bincount(cb, wb, groups)
        with np.errstate(invalid="ignore", divide="ignore"):
            out["ci95"] = _interval((draws @ sa) / (draws @ na) - (draws @ sb) / (draws @ nb))
    return out


def bh_qvalues(pvalues) -> list:
    """Benjamini-Hochberg q-values; NaN p (not estimable) counts as p = 1 in m and stays NaN."""
    p = np.array([1.0 if not np.isfinite(v) else v for v in pvalues], float)
    m = p.size
    if not m:
        return []
    order = np.argsort(p, kind="stable")
    ranked = p[order] * m / np.arange(1, m + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(q, 1.0)
    return [float("nan") if not np.isfinite(v) else float(q_) for v, q_ in zip(pvalues, out)]


# ---------------------------------------------------------------- matched controls


def strata_codes(f: dict, rows, *, intraday: bool, with_session: bool = False) -> np.ndarray:
    """(symbol, tf, 6-hour bucket, atr_pct tercile, range_atr bin, rel_volume tercile[, session]) per row; -1 when
    an input is missing. Volume terciles come from the rows given (events and controls together)."""
    rows = np.asarray(rows)
    atr_pct, size, volume = f["atr_pct_prev"][rows], f["range_atr"][rows], f["rel_volume_prior20"][rows]
    complete = np.isfinite(atr_pct) & np.isfinite(size) & np.isfinite(volume)
    if not rows.size:
        return np.zeros(0, np.int64)
    edges = np.quantile(volume[complete], (1 / 3, 2 / 3)) if complete.any() else (0.0, 0.0)
    columns = [np.unique(f["symbol"][rows], return_inverse=True)[1], f["tf"][rows].astype(np.int64),
               (hours(subset(f, rows)) // 6) if intraday else np.zeros(rows.size, np.int64),
               np.digitize(np.nan_to_num(atr_pct), (1 / 3, 2 / 3)), np.digitize(np.nan_to_num(size), RANGE_BINS),
               np.digitize(np.nan_to_num(volume), edges)]
    if with_session:
        columns.append(np.unique(sc.session_labels(f["decision_ms"][rows], intraday), return_inverse=True)[1])
    _, code = np.unique(np.column_stack(columns), axis=0, return_inverse=True)
    code = np.asarray(code).reshape(-1)
    return np.where(complete, code, -1)


def match_weights(code, is_event, mask=None) -> tuple[np.ndarray, np.ndarray]:
    """(event weights 0/1, control weights): controls in a stratum get events / controls of that stratum; events
    in strata without controls (and rows with code -1) get 0. ``mask`` restricts both sides first (a slice)."""
    code, is_event = np.asarray(code), np.asarray(is_event, bool)
    mask = np.ones(code.size, bool) if mask is None else np.asarray(mask, bool)
    usable = mask & (code >= 0)
    size = int(code.max()) + 1 if code.size and code.max() >= 0 else 0
    if not size:
        return np.zeros(code.size), np.zeros(code.size)
    n_e = np.bincount(code[usable & is_event], minlength=size).astype(float)
    n_c = np.bincount(code[usable & ~is_event], minlength=size).astype(float)
    both = (n_e > 0) & (n_c > 0)
    safe = np.where(code >= 0, code, 0)
    event_w = (usable & is_event & both[safe]).astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        control_w = np.where(usable & ~is_event & both[safe], n_e[safe] / np.where(n_c[safe] > 0, n_c[safe], 1), 0.0)
    return event_w, control_w


# ---------------------------------------------------------------- per-timeframe unit


@dataclass
class Unit:
    """One timeframe: the union of event, control and baseline rows with their paths and per-row metrics."""
    tf: int
    frame: dict
    rows: np.ndarray       # frame indices of the union
    event: np.ndarray      # bools over ``rows``
    control: np.ndarray
    baseline: np.ndarray
    day: np.ndarray
    paths: Paths
    code: np.ndarray       # strata code over rows (events/controls; baseline rows -1)
    event_w: np.ndarray
    control_w: np.ndarray
    half: np.ndarray       # 0 first half of the segment, 1 second


def build_unit(f: dict, tf: int, *, intraday: bool, event: str = "doji", with_session: bool = False) -> Unit:
    events, controls = event_mask(f, event), control_mask(f)
    base = baseline_mask(f, events)
    rows = np.flatnonzero(events | controls | base)
    paths = build_paths(f, rows, PATH_N[tf] + PATH_EXTRA)
    is_event, is_control, is_base = events[rows], controls[rows], base[rows]
    matched = is_event | is_control
    code = np.full(rows.size, -1, np.int64)
    code[matched] = strata_codes(f, rows[matched], intraday=intraday, with_session=with_session)
    event_w, control_w = match_weights(np.where(matched, code, -1), is_event)
    decision = f["decision_ms"][rows]
    pop_decision = f["decision_ms"][population(f)]
    middle = (pop_decision.min() + pop_decision.max()) / 2 if pop_decision.size else 0
    return Unit(tf, f, rows, is_event, is_control, is_base, days(subset(f, rows)), paths, code, event_w, control_w,
                (decision >= middle).astype(np.int64))


def family_values(unit: Unit, family: str, h: int) -> np.ndarray:
    """The per-row value of a slice-engine metric family at horizon h (NaN when unavailable)."""
    f, p = unit.frame, unit.paths
    if family == "expansion":
        return direction_free(p, f, h)["range_atr"]
    if family == "resolution":
        code = barrier_outcomes(p, f, RESOLUTION_ATR, h)["code"]
        return np.where(np.isfinite(code), (code != 2).astype(float), np.nan)
    if family == "drift":
        return direction_free(p, f, h)["drift_sigma"]
    if family == "break_follow":
        rule = break_rule(p, f, BREAK_K_PRIMARY)
        return follow_through(p, f, rule["direction"], rule["offset"], rule["entry"], h)["ret_sigma"]
    if family == "subtype_dir":
        d = subtype_direction(f, unit.rows, _expand(unit.event, unit.rows, len(f["tf"])))
        return follow_through(p, f, d, np.full(len(d), -1), p.entry, h)["ret_sigma"]
    raise ExploreError(f"unknown family {family!r}")


def _expand(flags, rows, size: int) -> np.ndarray:
    out = np.zeros(size, bool)
    out[rows] = flags
    return out


# ---------------------------------------------------------------- headline tables (per timeframe, all events)


def _groups(unit: Unit) -> dict:
    """{group: (mask over rows, weights)}: events (all), matched controls (stratum weights), baseline (unweighted)."""
    return {"event": (unit.event, np.ones(unit.rows.size)), "control_matched": (unit.control, unit.control_w),
            "baseline": (unit.baseline, np.ones(unit.rows.size))}


def _compare(values, unit: Unit, B: int, baseline_values=None) -> dict:
    """Events, matched controls and baseline; ``baseline_values`` replaces ``values`` for the baseline rows (a rule
    whose side is assigned differently there)."""
    out = {}
    base = values if baseline_values is None else baseline_values
    for name, (mask, weights) in _groups(unit).items():
        v = base if name == "baseline" else values
        out[name] = describe(v[mask], unit.day[mask], weights[mask], B)
    e = unit.event & (unit.event_w > 0)
    out["event_minus_control"] = difference(values[e], unit.day[e], values[unit.control], unit.day[unit.control],
                                            unit.control_w[unit.control], B)
    out["event_minus_baseline"] = difference(values[unit.event], unit.day[unit.event], base[unit.baseline],
                                             unit.day[unit.baseline], None, B)
    return out


def path_counts(unit: Unit) -> dict:
    """Rows per group and how many paths are too short for each horizon (excluded there, never filled)."""
    out = {}
    for name, (mask, _) in _groups(unit).items():
        lengths = unit.paths.length[mask]
        out[name] = {"rows": int(mask.sum()),
                     "incomplete_by_h": {str(h): int((lengths < h).sum()) for h in HORIZONS[unit.tf]}}
    out["events_unmatched"] = int((unit.event & (unit.event_w == 0)).sum())
    return out


def direction_free_table(unit: Unit, B: int) -> list:
    rows = []
    for h in HORIZONS[unit.tf]:
        metrics = direction_free(unit.paths, unit.frame, h)
        for name, values in metrics.items():
            rows.append({"tf": unit.tf, "h": h, "metric": name, **_compare(values, unit, B)})
        for k in BARRIERS_ATR:
            outcome = barrier_outcomes(unit.paths, unit.frame, k, h)
            code = outcome["code"]
            known = np.isfinite(code)
            for label, value in (("up_first", 1), ("down_first", -1), ("ambiguous", 0), ("neither", 2)):
                share = np.where(known, (code == value).astype(float), np.nan)
                rows.append({"tf": unit.tf, "h": h, "metric": f"barrier_{k:g}atr_{label}", **_compare(share, unit, B)})
            resolved = np.where(known, (code != 2).astype(float), np.nan)
            rows.append({"tf": unit.tf, "h": h, "metric": f"resolution_{k:g}atr", **_compare(resolved, unit, B)})
            rows.append({"tf": unit.tf, "h": h, "metric": f"time_to_first_{k:g}atr",
                         **_compare(outcome["time"], unit, 0)})
    return rows


def _seasonal_h1(unit: Unit, B: int) -> dict | None:
    """h = 1 drift in seasonal sigma units (sigma_tf x sigma_seas_factor) where the table has the factor."""
    factor = unit.frame["sigma_seas_factor"][unit.rows]
    if not np.isfinite(factor).any():
        return None
    drift = direction_free(unit.paths, unit.frame, 1)["drift_sigma"] / factor
    return {"tf": unit.tf, "h": 1, "metric": "drift_sigma_seas", **_compare(drift, unit, B)}


def _rule_rows(unit: Unit, rule: str, direction, offset, entry, B: int, extra: dict | None = None,
               mask=None, baseline_direction=None) -> list:
    out = []
    mask = np.ones(unit.rows.size, bool) if mask is None else mask
    for h in HORIZONS[unit.tf]:
        values = follow_through(unit.paths, unit.frame, direction, offset, entry, h)
        base = (None if baseline_direction is None else
                follow_through(unit.paths, unit.frame, baseline_direction, offset, entry, h))
        for name in ("ret_sigma", "ret_bp", "ret_atr", "mfe_sigma", "mae_sigma", "mfe_atr", "mae_atr"):
            v = np.where(mask, values[name], np.nan)
            b = None if base is None else np.where(mask, base[name], np.nan)
            out.append({"tf": unit.tf, "rule": rule, "h": h, "metric": name, **(extra or {}),
                        **_compare(v, unit, B, b)})
        fade = np.where(mask, -values["ret_sigma"], np.nan)  # the mirror entry: simply the negative
        base_fade = None if base is None else np.where(mask, -base["ret_sigma"], np.nan)
        out.append({"tf": unit.tf, "rule": rule, "h": h, "metric": "fade_ret_sigma", **(extra or {}),
                    **_compare(fade, unit, B, base_fade)})
    return out


def directional_tables(unit: Unit, B: int) -> dict:
    f, p = unit.frame, unit.paths
    out = {"break": [], "break_counts": {}, "subtype": [], "break_trend": []}
    for K in BREAK_K:
        rule = break_rule(p, f, K)
        d = rule["direction"]
        out["break_counts"][str(K)] = {
            group: {"long": int((mask & (d == 1)).sum()), "short": int((mask & (d == -1)).sum()),
                    "no_break": int((mask & (d == 0)).sum()), "undecided": int((mask & ~np.isfinite(d)).sum())}
            for group, (mask, _) in _groups(unit).items()}
        out["break"] += _rule_rows(unit, "break", d, rule["offset"], rule["entry"], B, {"K": K})
        if K == BREAK_K_PRIMARY:
            for definition in TREND_DEFINITIONS:
                trend = _trend_sign(f, unit.rows, definition)
                if trend is None:
                    continue
                for label, keep in (("with", d * trend > 0), ("counter", d * trend < 0),
                                    ("no_trend", (trend == 0) | ~np.isfinite(trend))):
                    out["break_trend"] += _rule_rows(unit, "break", d, rule["offset"], rule["entry"], 0,
                                                     {"K": K, "trend": definition, "alignment": label}, keep)
    rng = np.random.default_rng(BASELINE_SEED + unit.tf)
    d = subtype_direction(f, unit.rows, _expand(unit.event, unit.rows, len(f["tf"])))
    long_share = float((d[unit.event] > 0).sum() / max((d[unit.event] != 0).sum(), 1))
    random_dir = np.where(rng.random(unit.rows.size) < long_share, 1.0, -1.0)
    out["subtype_counts"] = {group: {"long": int((mask & (side == 1)).sum()), "short": int((mask & (side == -1)).sum()),
                                     "none": int((mask & (side == 0)).sum())}
                             for group, (mask, _) in _groups(unit).items()
                             for side in ((random_dir if group == "baseline" else d),)}
    out["subtype_baseline_rule"] = (f"every baseline row gets a seeded random side with the events' long share "
                                    f"{long_share:.4f}; controls use the dragonfly/gravestone wick rule without the "
                                    "body rule")
    out["subtype"] = _rule_rows(unit, "subtype", d, np.full(unit.rows.size, -1), p.entry, B,
                                baseline_direction=random_dir)
    return out


def _trend_sign(f: dict, rows, definition: str):
    """Trend direction of a definition (NaN when unavailable); trend_agree gives trend_dir where it agrees, else 0."""
    if definition == "trend_agree":
        agree, direction = f["trend_agree"][rows], f["trend_dir"][rows]
        if not np.isfinite(agree).any():
            return None
        return np.where(np.isfinite(agree), np.where(agree == 1, direction, 0.0), np.nan)
    values = f[definition][rows]
    return values if np.isfinite(values).any() else None


# ---------------------------------------------------------------- excursion map (a map, not a recommendation)


def excursion_map(p: Paths, f: dict, direction, offset, entry, h_max: int, cost_bp: float = COST_BP) -> list:
    """Per (stop, target) in ATR multiples: first passage over candle highs/lows after the entry, within h_max
    candles (else a time exit at the close). Both barriers inside one candle = AMBIGUOUS (counted apart, never
    resolved optimistically). A candle opening beyond the stop exits at that open (gap). Returns gross / net bp
    with ambiguous excluded and with ambiguous counted as stops, and the stop-only time exit at each h."""
    n = len(p.rows)
    atr = f["atr_prev"][p.rows]
    d = np.asarray(direction, float)
    L = p.high.shape[1]
    start = offset + 1
    usable = (d != 0) & np.isfinite(d) & np.isfinite(entry) & (p.length >= start + h_max) & (start + h_max <= L)
    rows = np.flatnonzero(usable)
    out = []
    for stop in GRID_STOPS:
        for target in (*GRID_TARGETS, None):
            result = {"stop_atr": stop, "target_atr": target, "n": int(rows.size)}
            gross = np.full(rows.size, np.nan)
            ambiguous = np.zeros(rows.size, bool)
            stop_price = entry[rows] - d[rows] * stop * atr[rows]
            target_price = None if target is None else entry[rows] + d[rows] * target * atr[rows]
            done = np.zeros(rows.size, bool)
            outcome = np.zeros(rows.size, np.int64)  # 1 target, -1 stop, 0 time exit, 2 ambiguous
            for k in range(h_max):
                col = start[rows] + k
                hi, lo, op = p.high[rows, col], p.low[rows, col], p.open[rows, col]
                long = d[rows] > 0
                gap = ~done & np.where(long, op <= stop_price, op >= stop_price)
                stop_hit = ~done & ~gap & np.where(long, lo <= stop_price, hi >= stop_price)
                if target_price is None:
                    target_hit = np.zeros(rows.size, bool)
                else:
                    target_hit = ~done & ~gap & np.where(long, hi >= target_price, lo <= target_price)
                both = stop_hit & target_hit
                exit_price = np.where(gap, op, np.where(stop_hit & ~both, stop_price,
                                                        target_price if target_price is not None else np.nan))
                closing = gap | stop_hit | target_hit
                ambiguous |= both
                outcome[gap | (stop_hit & ~both)] = -1
                outcome[target_hit & ~both] = 1
                outcome[both] = 2
                with np.errstate(invalid="ignore", divide="ignore"):
                    value = 1e4 * d[rows] * np.log(exit_price / entry[rows])
                gross = np.where(closing & ~both, value, gross)
                done |= closing
            last = start[rows] + h_max - 1
            with np.errstate(invalid="ignore", divide="ignore"):
                time_exit = 1e4 * d[rows] * np.log(p.close[rows, last] / entry[rows])
            gross = np.where(~done, time_exit, gross)
            with np.errstate(invalid="ignore", divide="ignore"):
                stop_value = 1e4 * d[rows] * np.log(stop_price / entry[rows])
            pessimistic = np.where(ambiguous, stop_value, gross)
            clear = ~ambiguous
            result.update(
                target_share=float((outcome == 1).mean()) if rows.size else float("nan"),
                stop_share=float((outcome == -1).mean()) if rows.size else float("nan"),
                time_exit_share=float((outcome == 0).mean()) if rows.size else float("nan"),
                ambiguous_share=float(ambiguous.mean()) if rows.size else float("nan"),
                mean_gross_bp_excl_ambiguous=float(gross[clear].mean()) if clear.any() else float("nan"),
                mean_gross_bp_ambiguous_as_stop=float(pessimistic.mean()) if rows.size else float("nan"),
                mean_net_bp_excl_ambiguous=float(gross[clear].mean() - cost_bp) if clear.any() else float("nan"),
                mean_net_bp_ambiguous_as_stop=float(pessimistic.mean() - cost_bp) if rows.size else float("nan"),
                h_max=h_max, cost_bp=cost_bp)
            out.append(result)
    for stop in GRID_STOPS:  # stop only, time exit at each h (no target)
        for h in range(1, h_max + 1):
            sub = excursion_stop_time(p, f, d, offset, entry, stop, h, cost_bp)
            out.append(sub)
    return out


def excursion_stop_time(p: Paths, f: dict, d, offset, entry, stop: float, h: int, cost_bp: float) -> dict:
    """Stop at ``stop`` ATR (gap: exit at the open), otherwise exit at the close of the h-th candle after the entry."""
    atr = f["atr_prev"][p.rows]
    L = p.high.shape[1]
    start = offset + 1
    usable = (d != 0) & np.isfinite(d) & np.isfinite(entry) & (p.length >= start + h) & (start + h <= L)
    rows = np.flatnonzero(usable)
    stop_price = entry[rows] - d[rows] * stop * atr[rows]
    result = np.full(rows.size, np.nan)
    done = np.zeros(rows.size, bool)
    long = d[rows] > 0
    for k in range(h):
        col = start[rows] + k
        op, hi, lo = p.open[rows, col], p.high[rows, col], p.low[rows, col]
        gap = ~done & np.where(long, op <= stop_price, op >= stop_price)
        hit = ~done & ~gap & np.where(long, lo <= stop_price, hi >= stop_price)
        with np.errstate(invalid="ignore", divide="ignore"):
            result = np.where(gap, 1e4 * d[rows] * np.log(op / entry[rows]), result)
            result = np.where(hit, 1e4 * d[rows] * np.log(stop_price / entry[rows]), result)
        done |= gap | hit
    with np.errstate(invalid="ignore", divide="ignore"):
        close = 1e4 * d[rows] * np.log(p.close[rows, start[rows] + h - 1] / entry[rows])
    result = np.where(done, result, close)
    mean = float(result.mean()) if rows.size else float("nan")
    return {"stop_atr": stop, "target_atr": None, "time_exit_h": h, "n": int(rows.size),
            "stop_share": float(done.mean()) if rows.size else float("nan"), "mean_gross_bp": mean,
            "mean_net_bp": mean - cost_bp, "cost_bp": cost_bp}


def excursion_tables(unit: Unit, cost_bp: float = COST_BP) -> list:
    """Grid for rule (a) (K = 3) and rule (b), per group (events, matched-control rows, baseline rows)."""
    f, p = unit.frame, unit.paths
    out = []
    rule = break_rule(p, f, BREAK_K_PRIMARY)
    d_sub = subtype_direction(f, unit.rows, _expand(unit.event, unit.rows, len(f["tf"])))
    h_max = PATH_N[unit.tf]
    for name, (d, offset, entry) in (("break_K3", (rule["direction"], rule["offset"], rule["entry"])),
                                     ("subtype", (d_sub, np.full(unit.rows.size, -1), p.entry))):
        for group, (mask, _) in _groups(unit).items():
            if group == "baseline" and name == "subtype":
                continue  # baseline rows have no subtype side
            dm = np.where(mask, d, 0.0)
            for cell in excursion_map(p, f, dm, offset, entry, h_max, cost_bp):
                out.append({"tf": unit.tf, "rule": name, "group": group, **cell})
    return out


# ---------------------------------------------------------------- slice engine


@dataclass(frozen=True)
class Slice:
    id: str
    columns: tuple                      # every column the slice reads; absent -> the slice is skipped
    labels: Callable                    # labels(f, rows) -> object array of bucket labels ("" = outside)
    bucket_controls: bool = True        # False for event-defining slices (subtype, body): all matched controls


def _bucket(edges, names):
    def labels(values):
        out = np.array([""] * len(values), dtype=object)
        ok = np.isfinite(values)
        index = np.digitize(values[ok], edges)
        out[ok] = np.asarray(names, dtype=object)[index]
        return out
    return labels


def _column_slice(column: str, edges=None, names=None, *, fmt=None):
    if edges is None:
        def labels(f, rows):
            values = f[column][rows]
            if values.dtype == object:
                return np.where(values == "", "", values).astype(object)
            out = np.array([""] * len(values), dtype=object)
            ok = np.isfinite(values)
            out[ok] = [fmt(v) if fmt else f"{v:g}" for v in values[ok]]
            return out
        return labels
    bucket = _bucket(edges, names)
    return lambda f, rows: bucket(f[column][rows])


def _quantile_slice(column: str, q: int = 5):
    def labels(f, rows):
        values = f[column][rows]
        ok = np.isfinite(values)
        out = np.array([""] * len(values), dtype=object)
        if ok.sum() < q:
            return out
        edges = np.quantile(values[ok], np.linspace(0, 1, q + 1)[1:-1])
        out[ok] = [f"q{int(i) + 1}" for i in np.digitize(values[ok], edges)]
        return out
    return labels


def _combo(a: Callable, b: Callable):
    def labels(f, rows):
        x, y = a(f, rows), b(f, rows)
        return np.array([f"{u}&{v}" if u and v else "" for u, v in zip(x, y)], dtype=object)
    return labels


def _volume_column(f) -> str:
    return "rel_volume_slot" if np.isfinite(f["rel_volume_slot"]).any() else "rel_volume_prior20"


def _volume_slice(f, rows):
    return _quantile_slice(_volume_column(f))(f, rows)


def _session_slice(f, rows):
    return sc.session_labels(f["decision_ms"][rows], bool((f["tf"][rows] < 1440).all())).astype(object)


def _bool_slice(column):
    return _column_slice(column, fmt=lambda v: "1" if v == 1 else "0")


def _prev_confirm(f, rows):
    prev, confirm = f["prev_doji_b10"][rows], f["confirm_dir"][rows]
    ok = np.isfinite(prev) & np.isfinite(confirm)
    out = np.array([""] * len(rows), dtype=object)
    out[ok] = [f"prev_doji={int(a)}&confirm={int(b):+d}" for a, b in zip(prev[ok], confirm[ok])]
    return out


def default_slices() -> tuple:
    adx = _column_slice("adx_prev", (20, 30, 40), ("<20", "20-30", "30-40", ">=40"))
    extension = _column_slice("extension_prev_atr", (-2, -1, 0, 1, 2),
                              ("<=-2", "-2..-1", "-1..0", "0..1", "1..2", ">=2"))
    z_names = ("<=-2", "-2..-1", "-1..1", "1..2", ">=2")
    slices = [
        Slice("all", ("tf",), lambda f, rows: np.array(["all"] * len(rows), dtype=object)),
        Slice("symbol", ("symbol",), _column_slice("symbol")),
        Slice("session", ("decision_ms",), _session_slice),
        Slice("rel_volume_q5", ("rel_volume_prior20",), _volume_slice),
        Slice("range_atr", ("range_atr",), _column_slice("range_atr", RANGE_BINS,
                                                         ("0.5-0.75", "0.75-1", "1-1.5", "1.5-2.5", ">=2.5"))),
        Slice("body_ratio", ("body_ratio",), _column_slice("body_ratio", (0.02, 0.05),
                                                           ("<=0.02", "0.02-0.05", "0.05-0.10")), False),
        Slice("subtype", ("doji_subtype",), _column_slice("doji_subtype"), False),
        Slice("adx", ("adx_prev",), adx),
        Slice("extension", ("extension_prev_atr",), extension),
        *(Slice(f"trend:{name}", (name,), _bool_slice(name) if name == "trend_agree" else _column_slice(name))
          for name in TREND_DEFINITIONS),
        *(Slice(f"touch:{name}", (name,), _bool_slice(name)) for name in TOUCH_SETS),
        Slice("sig_level_dist", ("sig_nearest_level_dist_atr",),
              _column_slice("sig_nearest_level_dist_atr", (0.25, 0.5, 1, 2),
                            ("<0.25", "0.25-0.5", "0.5-1", "1-2", ">=2"))),
        *(Slice(f"regime:{name}", (name,), _column_slice(name)) for name in REGIME_COLUMNS),
        Slice("doi_z", ("doi_z",), _column_slice("doi_z", (-2, -1, 1, 2), z_names)),
        Slice("fund_pct90", ("fund_pct90",), _column_slice("fund_pct90", (0.2, 0.4, 0.6, 0.8),
                                                           ("<0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", ">=0.8"))),
        Slice("prem_z", ("prem_z",), _column_slice("prem_z", (-2, -1, 1, 2), z_names)),
        Slice("prev_doji_confirm", ("prev_doji_b10", "confirm_dir"), _prev_confirm),
        Slice("climax_age6", ("climax_age6",), _column_slice("climax_age6", fmt=lambda v: f"{int(v)}")),
        Slice("climax_vol_max6", ("climax_vol_max6",),
              _column_slice("climax_vol_max6", (1.5, 2.5, 4), ("<1.5", "1.5-2.5", "2.5-4", ">=4"))),
        Slice("flush_prev_dir", ("flush_prev_dir",), _column_slice("flush_prev_dir", fmt=lambda v: f"{int(v):+d}")),
        Slice("volume_x_touch_sig", ("rel_volume_prior20", "touch_sig"),
              _combo(_volume_slice, _bool_slice("touch_sig"))),
        Slice("volume_x_trend_agree", ("rel_volume_prior20", "trend_agree"),
              _combo(_volume_slice, _bool_slice("trend_agree"))),
        Slice("subtype_x_touch_sig", ("doji_subtype", "touch_sig"),
              _combo(_column_slice("doji_subtype"), _bool_slice("touch_sig")), False),
    ]
    return tuple(slices)


SLICES = default_slices()


def _slice_available(f: dict, item: Slice, rows) -> bool:
    for column in item.columns:
        values = f.get(column)
        if values is None:
            return False
        part = values[rows]
        if part.dtype == object:
            if not (part != "").any():
                return False
        elif not np.isfinite(part).any():
            return False
    return True


def _diff_sign(values, unit: Unit, e_mask, c_mask, weights) -> float:
    a, b = values[e_mask], values[c_mask]
    wa, wb = np.isfinite(a), np.isfinite(b) & (weights[c_mask] > 0)
    if not wa.any() or not wb.any():
        return float("nan")
    return float(np.sign(a[wa].mean() - (weights[c_mask][wb] * b[wb]).sum() / weights[c_mask][wb].sum()))


def is_lead(t: float, n_events: int, difference_sign: float, half_signs, symbol_signs) -> tuple[bool, bool, int]:
    """(lead, halves agree, symbols agreeing): |t| >= 2, n >= 100, the same sign in both halves of the segment and
    in at least 4 symbols."""
    agree_halves = bool(np.isfinite(difference_sign) and difference_sign != 0
                        and all(x == difference_sign for x in half_signs))
    agree_symbols = int(sum(1 for x in symbol_signs if np.isfinite(difference_sign) and x == difference_sign))
    lead = bool(np.isfinite(t) and abs(t) >= LEAD_MIN_T and n_events >= LEAD_MIN_N and agree_halves
                and agree_symbols >= LEAD_MIN_SYMBOLS)
    return lead, agree_halves, agree_symbols


def run_slices(unit: Unit, slices=SLICES) -> tuple[list, list]:
    """(results, skipped): one row per slice bucket x family x lead horizon (events vs matched controls within
    the bucket; baseline mean beside it), with the half and per-symbol sign checks of the lead rule."""
    f = unit.frame
    values = {(family, h): family_values(unit, family, h) for family in FAMILIES for h in LEAD_HORIZONS[unit.tf]}
    symbols = sorted(set(f["symbol"][unit.rows[unit.event]].tolist()))
    matched = unit.event | unit.control
    out, skipped = [], []
    for item in slices:
        if not _slice_available(f, item, unit.rows):
            skipped.append({"tf": unit.tf, "slice": item.id, "reason": "column absent or empty"})
            continue
        labels = item.labels(f, unit.rows)
        for bucket in sorted({str(v) for v in labels[unit.event] if v}):
            in_bucket = labels == bucket
            mask = in_bucket | (unit.control & ~np.asarray(item.bucket_controls))
            event_w, control_w = match_weights(np.where(matched, unit.code, -1), unit.event, mask & matched)
            e = unit.event & in_bucket & (event_w > 0)
            c = unit.control & (control_w > 0)
            base = unit.baseline & (in_bucket if item.bucket_controls else True)
            for (family, h), v in values.items():
                diff = difference(v[e], unit.day[e], v[c], unit.day[c], control_w[c])
                halves = [_diff_sign(v, unit, e & (unit.half == k), c & (unit.half == k), control_w)
                          for k in (0, 1)]
                per_symbol = {s: _diff_sign(v, unit, e & (f["symbol"][unit.rows] == s),
                                            c & (f["symbol"][unit.rows] == s), control_w) for s in symbols}
                sign = float(np.sign(diff["difference"])) if np.isfinite(diff["difference"]) else float("nan")
                event_values = v[unit.event & in_bucket]
                n_events = int(np.isfinite(event_values).sum())
                lead, agree_halves, agree_symbols = is_lead(diff["t"], n_events, sign, halves, per_symbol.values())
                base_values = v[base]
                out.append({"tf": unit.tf, "slice": item.id, "bucket": bucket, "family": family, "h": h,
                            "n_events": n_events, "n_events_matched": diff["n_a"], "n_controls": diff["n_b"],
                            "mean_events": float(np.nanmean(event_values)) if n_events else float("nan"),
                            "difference": diff["difference"], "se": diff["se"], "t": diff["t"], "p": diff["p"],
                            "baseline_mean": float(np.nanmean(base_values)) if np.isfinite(base_values).any()
                            else float("nan"),
                            "half_signs": halves, "agree_halves": agree_halves, "symbol_signs": per_symbol,
                            "agree_symbols": agree_symbols, "lead": lead})
    return out, skipped


def add_qvalues(results: list) -> int:
    """BH q-values over every test of the run (all timeframes, slices, families, horizons); returns the count."""
    q = bh_qvalues([row["p"] for row in results])
    for row, value in zip(results, q):
        row["q_bh"] = value
    return len(results)


def top_leads(results: list, per_family: int = LEADS_PER_FAMILY) -> list:
    out = []
    for family in FAMILIES:
        leads = [row for row in results if row["family"] == family and row["lead"]]
        leads.sort(key=lambda row: (-abs(row["t"]), row["tf"], row["slice"], row["bucket"], row["h"]))
        out += leads[:per_family]
    return out


# ---------------------------------------------------------------- review packs (charts inspected by eye)


def review_sample(unit: Unit, *, n: int = 10, seed: int = REVIEW_SEED, slice_item: Slice | None = None,
                  bucket: str | None = None) -> dict:
    """Seeded RANDOM samples: events split into hits / misses of rule (a) (K = 3, return at the first lead
    horizon > 0 is a hit), balanced (min of n/2, hits, misses each), plus random events regardless of outcome and
    random controls. A pack with no hits is withheld (never a misses-only pack). Rows are frame indices."""
    rng = np.random.default_rng(seed + unit.tf)
    h = LEAD_HORIZONS[unit.tf][0]
    values = family_values(unit, "break_follow", h)
    scope = np.ones(unit.rows.size, bool)
    label = "all events"
    if slice_item is not None:
        scope = slice_item.labels(unit.frame, unit.rows) == bucket
        label = f"slice {slice_item.id}={bucket}"
    events = unit.event & scope
    hits = np.flatnonzero(events & (values > 0))
    misses = np.flatnonzero(events & (values <= 0))
    each = min(n // 2, hits.size, misses.size)

    def pick(pool, k):
        return sorted(rng.choice(pool, k, replace=False).tolist()) if k and pool.size else []

    packs = {"random_events": {"rule": f"{label}: {min(n, int(events.sum()))} uniformly random events (seed "
                                       f"{seed + unit.tf}), any outcome", "rows": pick(np.flatnonzero(events),
                                                                                     min(n, int(events.sum())))},
             "random_controls": {"rule": f"{'same slice: ' if slice_item else ''}{n} uniformly random controls "
                                         f"(same-size non-doji)",
                                 "rows": pick(np.flatnonzero(unit.control & (scope if slice_item and
                                                                             slice_item.bucket_controls else True)),
                                              min(n, int(unit.control.sum())))}}
    if each:
        packs["balanced_hits_misses"] = {
            "rule": f"{label}: {each} random hits and {each} random misses of rule (a) K=3 at h={h} (hit = return "
                    "> 0); balanced by construction", "rows": pick(hits, each) + pick(misses, each),
            "hits": each, "misses": each}
    else:
        packs["balanced_hits_misses"] = {"rule": "withheld: no hits or no misses in scope (never a one-sided pack)",
                                         "rows": [], "hits": int(hits.size), "misses": int(misses.size)}
    return {"tf": unit.tf, "scope": label, "packs": packs}


def candle_svg(f: dict, row: int, before: int, after: int, title: str) -> str:
    """Inline SVG candlesticks of rows row-before .. row+after of the same symbol/tf (gaps end the window)."""
    lo_i, hi_i = row, row
    while lo_i > 0 and row - lo_i < before and f["symbol"][lo_i - 1] == f["symbol"][row] \
            and f["tf"][lo_i - 1] == f["tf"][row]:
        lo_i -= 1
    while hi_i + 1 < len(f["tf"]) and hi_i - row < after and f["symbol"][hi_i + 1] == f["symbol"][row] \
            and f["tf"][hi_i + 1] == f["tf"][row]:
        hi_i += 1
    span = range(lo_i, hi_i + 1)
    highs = [f["high"][j] for j in span if np.isfinite(f["high"][j])]
    lows = [f["low"][j] for j in span if np.isfinite(f["low"][j])]
    width, height, pad = 640, 220, 18
    top, bottom = (max(highs), min(lows)) if highs else (1.0, 0.0)
    scale = (height - 2 * pad) / (top - bottom if top > bottom else 1.0)
    step = (width - 2 * pad) / max(len(span), 1)

    def y(price):
        return height - pad - (price - bottom) * scale

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height + 20}" '
             f'viewBox="0 0 {width} {height + 20}" font-family="sans-serif" font-size="11">',
             f'<text x="4" y="12">{_escape(title)}</text>']
    for k, j in enumerate(span):
        x = pad + k * step + step / 2
        if j == row:
            parts.append(f'<rect x="{x - step / 2:.1f}" y="14" width="{step:.1f}" height="{height - 14}" '
                         'fill="#f2d16b" opacity="0.35"/>')
        o, h, l, c = (f[name][j] for name in ("open", "high", "low", "close"))
        if not all(np.isfinite(v) for v in (o, h, l, c)) or f["status"][j] == "invalid":
            continue
        colour = "#2a9d4b" if c >= o else "#c8423b"
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y(h) + 14:.1f}" y2="{y(l) + 14:.1f}" stroke="{colour}"/>')
        body_top, body_bottom = y(max(o, c)) + 14, y(min(o, c)) + 14
        parts.append(f'<rect x="{x - step * 0.35:.1f}" y="{body_top:.1f}" width="{step * 0.7:.1f}" '
                     f'height="{max(body_bottom - body_top, 1):.1f}" fill="{colour}"/>')
    parts.append("</svg>")
    return "".join(parts)


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def review_html(unit: Unit, sample: dict) -> str:
    f = unit.frame
    after = PATH_N[unit.tf]
    lines = ["<!doctype html><meta charset='utf-8'><title>Doji review pack</title>",
             f"<h1>Doji review pack, {unit.tf}-minute, {_escape(sample['scope'])}</h1>",
             "<p>Exploration only. Every chart states its sampling rule. Highlighted candle = the event/control row; "
             f"the window shows up to 20 candles before and {after} after.</p>"]
    for name, pack in sample["packs"].items():
        lines.append(f"<h2>{_escape(name)}</h2><p>Sampling rule: {_escape(pack['rule'])}</p>")
        for row in pack["rows"]:
            index = int(unit.rows[row])
            stamp = int(f["open_ms"][index])
            title = f"{f['symbol'][index]} {unit.tf}m open_ms {stamp} [{name}: {pack['rule'][:60]}]"
            lines.append(candle_svg(f, index, 20, after, title))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- run and report


def run_explore(frames: dict, *, tier: str, segment: str, table_report: str, table_hash: str, code_commit: str,
                created_utc: str, plan_id: str = "", B: int = BOOTSTRAP_B, cost_bp: float = COST_BP,
                absent: list | None = None, review: tuple | None = None) -> tuple[dict, dict]:
    """(report, review html per name). ``frames``: {tf: frame} of the tier (all rows; ``in_segment`` marks the
    segment). ``review``: optional (slice id, bucket) for an extra slice pack."""
    check_explore_segment(segment, plan_id)
    timeframes = check_tier(tier)
    intraday = tier == "intraday"
    report = {"schema": SCHEMA, "version": EXPLORE_VERSION, "question_id": QUESTION_ID, "tier": tier,
              "segment": segment, "plan_id": plan_id or None, "table_report": table_report, "table_hash": table_hash,
              "absent_columns": sorted(absent or []), "by_tf": {}, "slices": [], "skipped_slices": []}
    reviews = {}
    for tf in timeframes:
        f = frames.get(tf)
        if f is None or not population(f).any():
            report["by_tf"][str(tf)] = {"status": "no rows"}
            continue
        unit = build_unit(f, tf, intraday=intraday)
        block = {"counts": path_counts(unit), "direction_free": direction_free_table(unit, B),
                 "directional": directional_tables(unit, B), "excursion_map": excursion_tables(unit, cost_bp),
                 "variants": {}}
        seasonal = _seasonal_h1(unit, B) if intraday else None
        if seasonal is not None:
            block["direction_free"].append(seasonal)
        for name in EVENTS:
            if name == "doji":
                continue
            variant = build_unit(f, tf, intraday=intraday, event=name)
            block["variants"][name] = {"counts": path_counts(variant),
                                       "direction_free": [row for row in direction_free_table(variant, 0)
                                                          if row["metric"] in ("range_atr", "drift_sigma",
                                                                               "resolution_1atr")]}
        if intraday:
            session_unit = build_unit(f, tf, intraday=True, with_session=True)
            block["session_matched"] = [row for row in direction_free_table(session_unit, 0)
                                        if row["metric"] in ("range_atr", "resolution_1atr")]
        results, skipped = run_slices(unit)
        report["slices"] += results
        report["skipped_slices"] += skipped
        sample = review_sample(unit)
        reviews[f"review_{tf}_all.html"] = review_html(unit, sample)
        block["review"] = {name: {k: v for k, v in pack.items() if k != "rows"} | {"count": len(pack["rows"])}
                           for name, pack in sample["packs"].items()}
        if review:
            item = next((s for s in SLICES if s.id == review[0]), None)
            if item is not None and _slice_available(f, item, unit.rows):
                extra = review_sample(unit, slice_item=item, bucket=review[1])
                reviews[f"review_{tf}_{_safe(review[0])}_{_safe(review[1])}.html"] = review_html(unit, extra)
        report["by_tf"][str(tf)] = block
    report["tests_evaluated"] = add_qvalues(report["slices"])
    report["leads"] = top_leads(report["slices"])
    report["parameters"] = {
        "events": {k: {"body_ratio_max": b, "range_atr_min": r} for k, (b, r) in EVENTS.items()},
        "controls": {"body_ratio_gt": CONTROL_BODY_MIN, "range_atr_min": CONTROL_RANGE_MIN},
        "path_n": {str(k): v for k, v in PATH_N.items()}, "horizons": {str(k): list(v) for k, v in HORIZONS.items()},
        "lead_horizons": {str(k): list(v) for k, v in LEAD_HORIZONS.items()}, "barriers_atr": list(BARRIERS_ATR),
        "break_k": list(BREAK_K), "break_k_primary": BREAK_K_PRIMARY, "grid_stops_atr": list(GRID_STOPS),
        "grid_targets_atr": list(GRID_TARGETS), "cost_bp": cost_bp, "bootstrap_B": B,
        "bootstrap_seed": BOOTSTRAP_SEED, "baseline_seed": BASELINE_SEED, "baseline_multiple": BASELINE_MULTIPLE,
        "review_seed": REVIEW_SEED, "lead_rule": {"min_abs_t": LEAD_MIN_T, "min_events": LEAD_MIN_N,
                                                  "halves_agree": True, "min_symbols_agree": LEAD_MIN_SYMBOLS},
        "families": list(FAMILIES), "slices": [s.id for s in SLICES]}
    report["notes"] = [
        "Exploration: descriptive, no stops in discovery, no required outcome; nothing here is a trade or a PASS.",
        "Every timeframe is analysed on its own; pooled numbers are never the headline.",
        "Leads are |t| >= 2 with n >= 100 whose sign agrees in both halves of the segment and in >= 4 symbols; "
        "BH q-values are over every test of the run (tests_evaluated). A lead is a candidate for a validation plan, "
        "not a result.",
        "Excursion map: a map for choosing stops LATER from data. Ambiguous candles are reported apart and in a "
        "pessimistic column (counted as stops); never resolved optimistically.",
        "Units: one-candle sigma of the event row (multi-candle values not divided by sqrt(h)), ATR (atr_prev), bp."]
    report["code_commit"], report["created_utc"] = code_commit, created_utc
    report = _render(report)
    report["report_hash"] = content_hash({k: v for k, v in report.items() if k not in HASH_EXCLUDED})
    return report, reviews


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text)).strip("-") or "x"


HASH_EXCLUDED = ("report_hash", "code_commit", "created_utc")
REQUIRED_KEYS = ("schema", "version", "question_id", "tier", "segment", "table_report", "table_hash", "by_tf",
                 "slices", "skipped_slices", "tests_evaluated", "leads", "parameters", "notes", "report_hash")


def check_report(report: dict) -> None:
    if report.get("schema") != SCHEMA:
        raise ExploreError("not a pattern-explore-v1 report")
    missing = [key for key in REQUIRED_KEYS if key not in report]
    if missing:
        raise ExploreError("report lacks required fields: " + ", ".join(missing))
    if report["report_hash"] != content_hash({k: v for k, v in report.items() if k not in HASH_EXCLUDED}):
        raise ExploreError("report_hash mismatch")


def report_dir(report: dict) -> str:
    check_report(report)
    return f"{REPORT_ROOT}/doji-{report['segment']}__{report['report_hash'][:16]}"


def public_lines(report: dict) -> list:
    """Counts, the number of tests and the private path; never an effect size or a lead."""
    check_report(report)
    lines = [f"doji exploration {report['version']} tier {report['tier']} segment {report['segment']}: "
             f"{report['tests_evaluated']} slice tests (BH over all)"]
    for tf, block in report["by_tf"].items():
        if "counts" not in block:
            lines.append(f"tf={int(tf)} no rows")
            continue
        counts = block["counts"]
        lines.append(f"tf={int(tf)} events={int(counts['event']['rows'])} "
                     f"controls={int(counts['control_matched']['rows'])} "
                     f"baseline={int(counts['baseline']['rows'])} events_unmatched={int(counts['events_unmatched'])}")
    lines += [f"private path {report_dir(report)}/", f"report hash {report['report_hash']}"]
    return lines


def _csv(rows: list, fields: list) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow(["" if row.get(k) is None else (json.dumps(row[k], sort_keys=True)
                                                         if isinstance(row.get(k), (dict, list)) else row[k])
                         for k in fields])
    return out.getvalue()


def _flatten_compare(row: dict, keys: tuple) -> dict:
    out = {k: row.get(k) for k in keys}
    for group in ("event", "control_matched", "baseline"):
        stats = row.get(group) or {}
        out.update({f"{group}_n": stats.get("n"), f"{group}_mean": stats.get("mean"),
                    f"{group}_median": stats.get("median"), f"{group}_share_positive": stats.get("share_positive"),
                    f"{group}_se": stats.get("se"), f"{group}_ci95": stats.get("ci95"),
                    f"{group}_quantiles": stats.get("quantiles")})
    for name in ("event_minus_control", "event_minus_baseline"):
        d = row.get(name) or {}
        out.update({f"{name}": d.get("difference"), f"{name}_se": d.get("se"), f"{name}_t": d.get("t"),
                    f"{name}_ci95": d.get("ci95")})
    return out


_COMPARE_FIELDS = [f"{g}_{s}" for g in ("event", "control_matched", "baseline")
                   for s in ("n", "mean", "median", "share_positive", "se", "ci95", "quantiles")] + \
    [f"{n}{s}" for n in ("event_minus_control", "event_minus_baseline") for s in ("", "_se", "_t", "_ci95")]


def csv_files(report: dict) -> dict:
    """One CSV per metric family: direction_free, directional (break, break_trend, subtype), excursion_map, slices,
    leads."""
    check_report(report)
    direction_free, directional, excursion = [], [], []
    for block in report["by_tf"].values():
        direction_free += [_flatten_compare(r, ("tf", "h", "metric")) for r in block.get("direction_free", [])]
        d = block.get("directional", {})
        for key in ("break", "break_trend", "subtype"):
            directional += [_flatten_compare({**r, "table": key}, ("table", "tf", "rule", "K", "trend", "alignment",
                                                                   "h", "metric")) for r in d.get(key, [])]
        excursion += block.get("excursion_map", [])
    slice_fields = ["tf", "slice", "bucket", "family", "h", "n_events", "n_events_matched", "n_controls",
                    "mean_events", "difference", "se", "t", "p", "q_bh", "baseline_mean", "half_signs",
                    "agree_halves", "symbol_signs", "agree_symbols", "lead"]
    excursion_fields = ["tf", "rule", "group", "stop_atr", "target_atr", "time_exit_h", "h_max", "n", "target_share",
                        "stop_share", "time_exit_share", "ambiguous_share", "mean_gross_bp_excl_ambiguous",
                        "mean_gross_bp_ambiguous_as_stop", "mean_net_bp_excl_ambiguous",
                        "mean_net_bp_ambiguous_as_stop", "mean_gross_bp", "mean_net_bp", "cost_bp"]
    return {"direction_free.csv": _csv(direction_free, ["tf", "h", "metric", *_COMPARE_FIELDS]),
            "directional.csv": _csv(directional, ["table", "tf", "rule", "K", "trend", "alignment", "h", "metric",
                                                  *_COMPARE_FIELDS]),
            "excursion_map.csv": _csv(excursion, excursion_fields),
            "slices.csv": _csv(report["slices"], slice_fields),
            "leads.csv": _csv(report["leads"], slice_fields)}


def _f(value) -> str:
    try:
        return f"{float(value):.4g}"
    except (TypeError, ValueError):
        return "n/a"


def markdown(report: dict, review_files=()) -> str:
    check_report(report)
    lines = [f"# Doji exploration ({report['tier']}, {report['segment']})", "",
             f"Report `{report['report_hash']}`; {report['version']}; table {report['table_report']}. "
             f"Slice tests evaluated: {report['tests_evaluated']} (BH over all).", ""]
    lines += [f"- {note}" for note in report["notes"]]
    if report["absent_columns"]:
        lines += ["", "Columns absent from this table (their slices are skipped): "
                  + ", ".join(report["absent_columns"])]
    for tf, block in report["by_tf"].items():
        lines += ["", f"## {tf}-minute"]
        if "counts" not in block:
            lines.append("No rows.")
            continue
        c = block["counts"]
        lines.append(f"Events {c['event']['rows']}, controls {c['control_matched']['rows']}, baseline "
                     f"{c['baseline']['rows']}, events without a matched stratum {c['events_unmatched']}.")
        lines += ["", "| h | metric | events mean | matched controls | baseline | event - control (95% CI) |",
                  "|" + " --- |" * 6]
        for row in block["direction_free"]:
            if row["metric"] not in ("range_atr", "drift_sigma", "resolution_1atr", "up_atr", "down_atr"):
                continue
            d = row["event_minus_control"]
            lines.append(f"| {row['h']} | {row['metric']} | {_f(row['event']['mean'])} | "
                         f"{_f(row['control_matched']['mean'])} | {_f(row['baseline']['mean'])} | "
                         f"{_f(d['difference'])} "
                         f"({_f(d['ci95'][0])} .. {_f(d['ci95'][1])}) |")
        lines += ["", "Break rule (a), K = 3, return in sigma (fade = the negative):", "",
                  "| h | events mean | median | share > 0 | controls | baseline | event - control (95% CI) |",
                  "|" + " --- |" * 7]
        for row in block["directional"]["break"]:
            if row.get("K") != BREAK_K_PRIMARY or row["metric"] != "ret_sigma":
                continue
            d = row["event_minus_control"]
            lines.append(f"| {row['h']} | {_f(row['event']['mean'])} | {_f(row['event']['median'])} | "
                         f"{_f(row['event']['share_positive'])} | {_f(row['control_matched']['mean'])} | "
                         f"{_f(row['baseline']['mean'])} | {_f(d['difference'])} ({_f(d['ci95'][0])} .. "
                         f"{_f(d['ci95'][1])}) |")
        lines += ["", "Review packs: " + ", ".join(f"{name} ({pack['count']}: {pack['rule']})"
                                                     for name, pack in block["review"].items())]
    lines += ["", "## Leads (top per family; candidates for a plan, not results)", "",
              "| tf | slice | bucket | family | h | n | difference | t | q (BH) |", "|" + " --- |" * 9]
    for row in report["leads"]:
        lines.append(f"| {row['tf']} | {row['slice']} | {row['bucket']} | {row['family']} | {row['h']} | "
                     f"{row['n_events']} | {_f(row['difference'])} | {_f(row['t'])} | {_f(row['q_bh'])} |")
    if review_files:
        lines += ["", "Review pack files: " + ", ".join(f"`review/{name}`" for name in review_files)]
    lines += ["", "Full tables: direction_free.csv, directional.csv, excursion_map.csv, slices.csv, leads.csv, "
              "summary.json."]
    return "\n".join(lines) + "\n"


def table_paths(table_dir: Path, manifest: dict, timeframes) -> dict:
    out = {}
    for item in manifest["files"]:
        tf = int(item["name"].split("__")[2])
        if tf in timeframes:
            out.setdefault(tf, []).append(Path(table_dir) / item["name"])
    return out


def load_tier(table_dir: Path, tier: str, segment: str) -> tuple[dict, list, dict]:
    """({tf: frame}, absent columns, manifest) of a candle-table report directory."""
    from .candle_table import check_manifest

    manifest = json.loads((Path(table_dir) / "manifest.json").read_text(encoding="utf-8"))
    check_manifest(manifest)
    timeframes = check_tier(tier)
    paths = table_paths(table_dir, manifest, timeframes)
    frames, absent = {}, set()
    for tf in timeframes:
        if tf in paths:
            frames[tf], missing = read_frame(paths[tf], STUDY_SEGMENTS[segment][tier])
            absent |= set(missing)
    return frames, sorted(absent), manifest


def experiment_records(report: dict, *, run_id: str, data_snapshot_id: str) -> list:
    """One TrialRecord per timeframe x slice bucket x metric family evaluated (horizons folded in the config)."""
    from ..benchmark.experiment_log import TrialRecord, TrialStatus

    check_report(report)
    groups = {}
    for row in report["slices"]:
        key = (row["tf"], row["slice"], row["bucket"], row["family"])
        groups.setdefault(key, []).append(row)
    records = []
    for (tf, slice_id, bucket, family), rows in sorted(groups.items()):
        config = {"tf": tf, "slice": slice_id, "bucket": bucket, "family": family,
                  "horizons": sorted(r["h"] for r in rows), "event": "doji-geo-v2", "tier": report["tier"]}
        records.append(TrialRecord(
            question_id=QUESTION_ID, family_id="doji-explore", strategy_id=family, strategy_version=EXPLORE_VERSION,
            config=config, split_id=report["segment"], data_snapshot_id=data_snapshot_id,
            code_commit=report["code_commit"], status=TrialStatus.OK, counts_toward_n=True,
            count_reason="exploration slice x metric family", result_hash=report["report_hash"],
            result_summary={"n": int(max(r["n_events"] for r in rows)), "run_id": run_id,
                            "tests": len(rows), "slice_id": f"{tf}|{slice_id}|{bucket}",
                            "private_report": report_dir(report) + "/report.md"},
            created_utc=report["created_utc"]))
    return records


__all__ = ["SCHEMA", "QUESTION_ID", "check_explore_segment", "read_frame", "build_paths", "direction_free",
           "barrier_outcomes", "break_rule", "follow_through", "subtype_direction", "excursion_map", "describe",
           "difference", "bh_qvalues", "strata_codes", "match_weights", "baseline_mask", "SLICES",
           "run_slices", "top_leads", "review_sample", "run_explore", "check_report", "report_dir", "public_lines",
           "csv_files", "markdown", "load_tier", "experiment_records"]
