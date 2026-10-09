"""Screening mode (#220 slice B): continuous forward-return screens and dose-response. Descriptive only.

A screen uses EVERY decision time of a signal's state series instead of only the
triggered trades. It answers "does a stronger signal do better?" and "is there
anything after a stop-free time exit?". It never counts as a PASS (SPEC-1 section
3), writes no trades, labels or ledger entries, and the report states how many
series x horizons were examined (the multiplicity).

First series ``of-cum240-z``: the of-v1 cum240 robust z (``order_flow.cum240_z_series``
with the ``of_cum240_4h`` parameters, hourly decisions, history from FIRST_MONTH).

Per observation (decision at ``signal_ms`` = end of minute ``d``; everything below is
point in time at the decision minute):

- entry at the open of minute ``e = d + 1``, exit at the open of minute ``e + h``,
  h in ``HORIZONS``; ``r = ln(open[e + h] / open[e])`` in bp. A window with a missing or
  compromised minute is excluded (``excluded_compromised``), one whose exit lies at or
  past the segment end is excluded (``excluded_past_end``);
- controls: ``past240 = ln(close[d] / close[d - 240])`` in bp, ``sigma240`` =
  ``horizon_sigma(var, 240) / 10**20`` of the 3-day EWMA at the last completed 5-minute
  block, and the UTC hour of the decision; an observation without them is skipped
  in the regressions (counted).

Regression per horizon, pooled over symbols, plain OLS:
``r = alpha_symbol + gamma_hour + beta z + delta past240 + eta sigma240`` (with controls)
and ``r = alpha_symbol + beta z`` (without), covariance clustered by UTC day for
h <= 240 and by blocks of ``ceil(h / 1440) + 1`` days for longer (overlapping) windows.
IC: Spearman correlation of z with the symbol-demeaned r.

Bucket table (state version: every hourly decision with |z| >= 2, not only crossings):
|z| in [2, 2.5), [2.5, 3), [3, 3.5), [3.5, inf) x side = sign(z). Gross ``side * r``,
drift-adjusted ``side * (r - rbar_symbol)`` (``rbar_symbol`` = mean r of that symbol and
horizon over all decision times of the segment), round-trip cost (taker fee and
market slippage floor of ``costs.COST_MODEL_V1`` on entry and exit, no maker, no stop),
funding ``side * sum(rate) * 10**4`` over ``funding.events_between(open_time(e),
open_time(e + h))`` with the mark approximated by the price, net = gross - cost -
funding. Standard errors by a cluster bootstrap (B = 2000, clusters resampled, draws
from ``rng.u64_words``, seed ``BOOTSTRAP_SEED``). Dose-response (one pre-registered
hypothesis per horizon): weighted least-squares slope of the drift-adjusted bucket
mean on the bucket index 0..3 (both signs pooled, weights = N) and the Spearman
correlation of bucket index and bucket mean, with bootstrap intervals; no PASS/FAIL.

Positioning family (#188, #224; ``POSITIONING_SERIES``): hourly decisions from the Binance metrics
archive (``metrics_lake.load_symbol_metrics_range``), a row usable only from ``create_time + 5 min``
(``metrics_lake.usable_from_ms``). At decision t the value uses the latest period usable at t; a
missing period (or one of the periods a value needs) drops the decision (z None), never filled.
``oi-chg-4h`` = ln(sum_open_interest[t] / sum_open_interest[t - 4 h]) (contract units, so the price
move does not enter); ``toptrader-ls`` = sum_toptrader_long_short_ratio (position-weighted level);
``global-ls`` = count_long_short_ratio (account-weighted level); ``taker-ls-1h`` = mean of
sum_taker_long_short_vol_ratio over the last 12 periods. Positive = more long positioning / flow
(for OI: more open positions). Robust z (``order_flow.robust_z``) against the same-phase history:
the values at the previous hourly decisions of the trailing 30 days, at least 500 of them (the
literal same hour of day would give at most 30 values, below that minimum). Horizons 60 and
240 minutes; the screen reports the sign of beta (follow or fade) and never assumes it. For
``oi-chg-4h`` a quadrant table adds OI up/down x 4 h price change up/down (|past240| below
0.25 sigma240 is "flat" and excluded) with forward returns, drift adjustment, costs and the same
cluster bootstrap. Multiplicity of the family: 4 series x 2 horizons + the quadrant table.

Floats are fine here (descriptive statistics); the report renders them as strings.
"""
from __future__ import annotations

from math import ceil

import numpy as np

from .. import data_lake
from . import order_flow
from .bars import COMPROMISED_FLAGS, MISSING, BarSeries
from .calibration import _render, check_segment
from .canonical import content_hash
from .costs import COST_MODEL_V1
from .funding import FundingSeries
from ..metrics_lake import load_symbol_metrics_range
from .market_data import load_symbol_bars, load_symbol_funding
from .rng import u64_words
from .segments import segment_bounds_ms
from .volatility import BLOCK_MINUTES, VAR_SCALE, build_variance, horizon_sigma

SCHEMA = "screen-v1"
POSITIONING_SERIES = ("oi-chg-4h", "toptrader-ls", "global-ls", "taker-ls-1h")
SERIES = ("of-cum240-z", *POSITIONING_SERIES)
POSITIONING_HORIZONS = (60, 240)
POSITIONING_HISTORY_DAYS = 30
POSITIONING_MIN_HISTORY = 500
QUADRANT_DEAD_ZONE_SIGMA = 0.25
QUADRANT_HORIZONS = (60, 240)
QUADRANTS = ("oi_up_price_up", "oi_up_price_down", "oi_down_price_up", "oi_down_price_down")
HORIZONS = (60, 120, 240, 480, 960, 1440)
BUCKET_EDGES = (2.0, 2.5, 3.0, 3.5)
BUCKET_NAMES = ("[2,2.5)", "[2.5,3)", "[3,3.5)", "[3.5,inf)")
SIGMA_HALF_LIFE_DAYS = 3
PAST_WINDOW_MIN = 240
BOOTSTRAP_B = 2000
BOOTSTRAP_SEED = 20261009
ROUND_TRIP_COST_BP = float(2 * (COST_MODEL_V1.taker_rate * 10_000 + COST_MODEL_V1.market_slip_floor_bps))
_MINUTE = data_lake.MINUTE_MS
_HOUR = 60 * _MINUTE
_DAY = 1440 * _MINUTE


def _of_cum240_z(bars: BarSeries) -> list:
    _, parameters, _ = order_flow.STRATEGIES["of_cum240_4h"]
    return order_flow.cum240_z_series(bars, window_min=parameters["window_min"],
                                      decision_step_min=parameters["decision_step_min"],
                                      history_days=parameters["history_days"],
                                      min_history=parameters["min_history"])


SERIES_BUILDERS = {"of-cum240-z": _of_cum240_z}
_PERIODS_4H = 48
_PERIODS_1H = 12


def series_horizons(series: str) -> tuple:
    return POSITIONING_HORIZONS if series in POSITIONING_SERIES else HORIZONS


def positioning_value(metrics, series: str, decision_ms: int):
    """The raw positioning value at a decision (float) or None when a needed period is missing."""
    i = metrics.index_at(decision_ms)
    if i is None:
        return None
    c = metrics.columns
    if series == "oi-chg-4h":
        if i < _PERIODS_4H:
            return None
        now, before = c["sum_open_interest"][i], c["sum_open_interest"][i - _PERIODS_4H]
        return None if np.isnan(now) or np.isnan(before) or now <= 0 or before <= 0 else float(np.log(now / before))
    if series == "toptrader-ls":
        value = c["sum_toptrader_long_short_ratio"][i]
    elif series == "global-ls":
        value = c["count_long_short_ratio"][i]
    elif series == "taker-ls-1h":
        if i < _PERIODS_1H - 1:
            return None
        window = c["sum_taker_long_short_vol_ratio"][i - _PERIODS_1H + 1:i + 1]
        return None if np.isnan(window).any() else float(window.mean())
    else:
        raise ValueError(f"unknown positioning series {series!r}")
    return None if np.isnan(value) else float(value)


def positioning_z_series(bars: BarSeries, metrics, series: str) -> list:
    """[(signal_ms, z or None, raw or None)] at every hourly decision of the bars (as cum240_z_series)."""
    history, out = order_flow.TrailingHistory(POSITIONING_HISTORY_DAYS * _DAY), []
    first = -bars.start_ms % _HOUR // _MINUTE
    for e in range(first, bars.minutes + 1, 60):
        signal_ms = bars.start_ms + e * _MINUTE
        value = positioning_value(metrics, series, signal_ms)
        z = None
        if value is not None:
            z = order_flow.robust_z(history.at(signal_ms), value, POSITIONING_MIN_HISTORY)
            history.add(signal_ms, value)
        out.append((signal_ms, None if z is None else float(z), value))
    return out


# ---------------------------------------------------------------- observations


def _prices(column) -> np.ndarray:
    raw = np.frombuffer(column, dtype=np.int64)
    return np.where(raw == MISSING, np.nan, raw.astype(float))


def usable_minutes(bars: BarSeries) -> np.ndarray:
    flags = np.frombuffer(bars.flags, dtype=np.uint16)
    usable = (flags & COMPROMISED_FLAGS) == 0
    for column in (bars.open, bars.high, bars.low, bars.close):
        usable &= np.frombuffer(column, dtype=np.int64) != MISSING
    return usable


def cluster_ids(day: np.ndarray, horizon: int) -> np.ndarray:
    """UTC day for h <= 240, else blocks of ceil(h / 1440) + 1 days (overlapping windows)."""
    return day if horizon <= 240 else day // (ceil(horizon / 1440) + 1)


def forward_returns(bars: BarSeries, entries: np.ndarray, horizon: int, end_index: int) -> tuple:
    """(r in bp with NaN where excluded, excluded_compromised mask, excluded_past_end mask)."""
    opens = _prices(bars.open)
    prefix = np.concatenate(([0], np.cumsum(~usable_minutes(bars))))
    entries = np.asarray(entries, dtype=np.int64)
    exits = entries + horizon
    past_end = exits >= min(end_index, bars.minutes)
    safe = np.where(past_end, entries, exits)
    compromised = ~past_end & ((prefix[safe + 1] - prefix[entries]) > 0)
    ok = ~past_end & ~compromised
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(ok, np.log(opens[safe] / opens[entries]) * 1e4, np.nan)
    return r, compromised, past_end


def symbol_observations(bars: BarSeries, funding: FundingSeries, segment: str, series: str = "of-cum240-z",
                        z_series=None) -> dict:
    """Every decision time of the series inside the segment, with z, controls, returns and funding per horizon."""
    first_ms, end_ms = segment_bounds_ms(segment)
    zs = SERIES_BUILDERS[series](bars) if z_series is None else z_series
    decisions = [item for item in zs if first_ms <= item[0] < end_ms]
    signal_ms = np.array([item[0] for item in decisions], dtype=np.int64)
    z = np.array([np.nan if item[1] is None else float(item[1]) for item in decisions], dtype=float)
    raw = np.array([np.nan if len(item) < 3 or item[2] is None else float(item[2]) for item in decisions],
                   dtype=float)
    e = (signal_ms - bars.start_ms) // _MINUTE
    d = e - 1
    end_index = (end_ms - bars.start_ms) // _MINUTE
    closes, usable = _prices(bars.close), usable_minutes(bars)
    past = np.full(len(e), np.nan)
    ok = (d - PAST_WINDOW_MIN >= 0) & (d < bars.minutes)
    idx, back = np.where(ok, d, 0), np.where(ok, d - PAST_WINDOW_MIN, 0)
    ok &= usable[idx] & usable[back]
    with np.errstate(invalid="ignore", divide="ignore"):
        past = np.where(ok, np.log(closes[idx] / closes[back]) * 1e4, np.nan)
    variance = build_variance(bars, SIGMA_HALF_LIFE_DAYS).variance
    blocks = (d + 1) // BLOCK_MINUTES - 1
    sigma = np.full(len(e), np.nan)
    for i, block in enumerate(blocks):
        if 0 <= block < len(variance) and variance[block] != MISSING and variance[block] > 0:
            sigma[i] = horizon_sigma(variance[block], 240) / VAR_SCALE
    out = {"symbol": bars.symbol, "signal_ms": signal_ms, "day": (signal_ms - first_ms) // _DAY,
           "hour": (signal_ms // _HOUR) % 24, "z": z, "raw": raw, "past240": past, "sigma240": sigma,
           "horizons": list(series_horizons(series)), "returns": {}, "funding_bp": {}, "excluded": {}}
    for h in series_horizons(series):
        r, compromised, past_end = forward_returns(bars, e, h, end_index)
        fund = np.full(len(e), np.nan)
        for i in np.flatnonzero(~np.isnan(r)):
            start = bars.open_time(int(e[i]))
            events = funding.events_between(start, start + h * _MINUTE)
            fund[i] = float(sum(rate for _, rate, _ in events)) * 1e4
        out["returns"][h] = r
        out["funding_bp"][h] = fund
        out["excluded"][h] = {"compromised": int(compromised.sum()), "past_end": int(past_end.sum())}
    return out


def run_symbol(bars_dir, symbol: str, segment: str, series: str = "of-cum240-z", metrics_dir=None) -> dict:
    """Guarded: the hidden guard runs before any file is opened; bars, funding (and metrics for the
    positioning series) FIRST_MONTH..segment end."""
    months = check_segment(segment)
    if series not in SERIES:
        raise ValueError(f"series must be one of {SERIES}")
    z_series = None
    if series in POSITIONING_SERIES:
        if metrics_dir is None:
            raise ValueError("positioning series need the metrics directory")
        metrics = load_symbol_metrics_range(metrics_dir, symbol, data_lake.FIRST_MONTH, months[-1])
    bars = load_symbol_bars(bars_dir, symbol, data_lake.FIRST_MONTH, months[-1])
    funding = load_symbol_funding(bars_dir, symbol, data_lake.FIRST_MONTH, months[-1])
    if series in POSITIONING_SERIES:
        z_series = positioning_z_series(bars, metrics, series)
    return symbol_observations(bars, funding, segment, series, z_series)


# ---------------------------------------------------------------- statistics


def ols_clustered(y: np.ndarray, X: np.ndarray, clusters: np.ndarray) -> dict:
    """OLS with cluster-robust (CR1) and naive standard errors."""
    n, k = X.shape
    xtx_inv = np.linalg.pinv(X.T @ X)
    beta = xtx_inv @ (X.T @ y)
    resid = y - X @ beta
    _, index = np.unique(clusters, return_inverse=True)
    groups = int(index.max()) + 1 if n else 0
    scores = np.zeros((groups, k))
    np.add.at(scores, index, X * resid[:, None])
    correction = (groups / max(groups - 1, 1)) * ((n - 1) / max(n - k, 1))
    cov = correction * xtx_inv @ (scores.T @ scores) @ xtx_inv
    naive = xtx_inv * (resid @ resid) / max(n - k, 1)
    return {"beta": beta, "se": np.sqrt(np.maximum(np.diag(cov), 0)), "se_naive": np.sqrt(np.maximum(np.diag(naive), 0)),
            "n": n, "clusters": groups}


def _ranks(values: np.ndarray) -> np.ndarray:
    """Average ranks (ties share the mean rank)."""
    unique, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
    ends = np.cumsum(counts)
    mean_rank = ends - (counts - 1) / 2.0
    return mean_rank[inverse]


def spearman(a, b) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    if a.size < 3:
        return float("nan")
    ra, rb = _ranks(a), _ranks(b)
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def bucket_index(abs_z) -> np.ndarray:
    """0..3 for |z| in [2, 2.5), [2.5, 3), [3, 3.5), [3.5, inf); -1 below 2 or undefined."""
    abs_z = np.asarray(abs_z, dtype=float)
    out = np.searchsorted(np.asarray(BUCKET_EDGES), abs_z, side="right") - 1
    return np.where(np.isnan(abs_z), -1, out)


def drift_adjusted(side, r, rbar) -> np.ndarray:
    return np.asarray(side) * (np.asarray(r) - np.asarray(rbar))


def cluster_weights(groups: int, B: int = BOOTSTRAP_B, seed: int = BOOTSTRAP_SEED) -> np.ndarray:
    """(B, groups) multiplicities of a cluster bootstrap; draws from rng.u64_words."""
    if groups <= 0:
        return np.zeros((B, 0))
    words = u64_words(seed, f"screen-bootstrap-{groups}", 0, B * groups)
    draws = (words % np.uint64(groups)).astype(np.int64).reshape(B, groups)
    flat = (draws + np.arange(B)[:, None] * groups).ravel()
    return np.bincount(flat, minlength=B * groups).reshape(B, groups).astype(float)


def weighted_slope(x, y, w) -> float:
    x, y, w = (np.asarray(v, float) for v in (x, y, w))
    keep = (w > 0) & ~np.isnan(y)
    if keep.sum() < 2:
        return float("nan")
    x, y, w = x[keep], y[keep], w[keep]
    xm, ym = np.average(x, weights=w), np.average(y, weights=w)
    denominator = np.sum(w * (x - xm) ** 2)
    return float(np.sum(w * (x - xm) * (y - ym)) / denominator) if denominator > 0 else float("nan")


def dose_response(bucket: np.ndarray, value: np.ndarray, clusters: np.ndarray, weights: np.ndarray) -> dict:
    """WLS slope of bucket means on the bucket index and Spearman(index, mean), with bootstrap intervals."""
    sums, counts = np.zeros((4, weights.shape[1])), np.zeros((4, weights.shape[1]))
    for b in range(4):
        mask = bucket == b
        np.add.at(sums[b], clusters[mask], value[mask])
        np.add.at(counts[b], clusters[mask], 1)
    total_n, total_sum = counts.sum(axis=1), sums.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        means = total_sum / total_n
    index = np.arange(4)
    slope, rho = weighted_slope(index, means, total_n), spearman_points(index, means)
    drawn_n, drawn_sum = weights @ counts.T, weights @ sums.T  # (B, 4)
    with np.errstate(invalid="ignore", divide="ignore"):
        drawn_means = drawn_sum / drawn_n
    slopes = np.array([weighted_slope(index, m, n) for m, n in zip(drawn_means, drawn_n)])
    rhos = np.array([spearman_points(index, m) for m in drawn_means])
    return {"bucket_means": [float(v) for v in means], "bucket_n": [int(v) for v in total_n],
            "slope_bp_per_bucket": slope, "slope_ci95": _interval(slopes), "spearman": rho,
            "spearman_ci95": _interval(rhos)}


def spearman_points(x, y) -> float:
    x, y = np.asarray(x, float), np.asarray(y, float)
    keep = ~np.isnan(y)
    if keep.sum() < 3:
        return float("nan")
    return spearman(x[keep], y[keep]) if np.unique(y[keep]).size > 1 else float("nan")


def _interval(draws) -> list:
    draws = np.asarray(draws, float)
    draws = draws[~np.isnan(draws)]
    if not draws.size:
        return [float("nan"), float("nan")]
    return [float(v) for v in np.percentile(draws, [2.5, 97.5])]


def _bootstrap_mean(values, clusters, weights) -> dict:
    groups = weights.shape[1]
    sums, counts = np.zeros(groups), np.zeros(groups)
    np.add.at(sums, clusters, values)
    np.add.at(counts, clusters, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        draws = (weights @ sums) / (weights @ counts)
    draws = draws[~np.isnan(draws)]
    mean = float(values.mean()) if values.size else float("nan")
    return {"mean": mean, "se": float(draws.std(ddof=1)) if draws.size > 1 else float("nan"),
            "ci95": _interval(draws)}


# ---------------------------------------------------------------- per-horizon screen


def _pooled(results: dict, horizon: int) -> dict:
    symbols = list(results)
    columns = {}
    for name in ("day", "hour", "z", "past240", "sigma240"):
        columns[name] = np.concatenate([results[s][name] for s in symbols])
    columns["symbol"] = np.concatenate([np.full(len(results[s]["z"]), i) for i, s in enumerate(symbols)])
    columns["r"] = np.concatenate([results[s]["returns"][horizon] for s in symbols])
    columns["funding_bp"] = np.concatenate([results[s]["funding_bp"][horizon] for s in symbols])
    return columns


def _design(columns, mask, symbols: int, controls: bool) -> np.ndarray:
    parts = [np.eye(symbols)[columns["symbol"][mask]]]
    if controls:
        parts.append(np.eye(24)[columns["hour"][mask]][:, 1:])
    parts.append(columns["z"][mask][:, None])
    if controls:
        parts += [columns["past240"][mask][:, None], columns["sigma240"][mask][:, None]]
    return np.hstack(parts)


def screen_horizon(results: dict, horizon: int, n_days: int, B: int = BOOTSTRAP_B) -> dict:
    columns = _pooled(results, horizon)
    symbols = len(results)
    clusters_all = cluster_ids(columns["day"], horizon)
    groups = int(cluster_ids(np.array([max(n_days - 1, 0)]), horizon)[0]) + 1
    weights = cluster_weights(groups, B)
    r, z = columns["r"], columns["z"]
    has_r = ~np.isnan(r)
    rbar = np.zeros(symbols)
    for i in range(symbols):
        mask = has_r & (columns["symbol"] == i)
        rbar[i] = r[mask].mean() if mask.any() else np.nan
    out = {"horizon_min": horizon, "clusters": "utc_day" if horizon <= 240 else
           f"blocks_of_{ceil(horizon / 1440) + 1}_days", "rbar_bp": {s: float(rbar[i]) for i, s in
                                                                    enumerate(results)}}
    for label, controls in (("with_controls", True), ("without_controls", False)):
        mask = has_r & ~np.isnan(z)
        if controls:
            mask &= ~np.isnan(columns["past240"]) & ~np.isnan(columns["sigma240"])
        X = _design(columns, mask, symbols, controls)
        if mask.sum() <= X.shape[1]:
            out[label] = {"n": int(mask.sum()), "beta_z": None}
            continue
        fit = ols_clustered(r[mask], X, clusters_all[mask])
        names = ["z", "past240", "sigma240"] if controls else ["z"]
        first = symbols + (23 if controls else 0)
        out[label] = {"n": fit["n"], "clusters": fit["clusters"],
                      "coefficients": {name: {"beta": float(fit["beta"][first + j]), "se": float(fit["se"][first + j]),
                                              "t": float(fit["beta"][first + j] / fit["se"][first + j])
                                              if fit["se"][first + j] > 0 else float("nan"),
                                              "se_naive": float(fit["se_naive"][first + j])}
                                       for j, name in enumerate(names)}}
    mask = has_r & ~np.isnan(z)
    demeaned = r[mask] - rbar[columns["symbol"][mask]]
    out["ic_spearman"] = spearman(z[mask], demeaned)
    out["ic_n"] = int(mask.sum())
    abs_z = np.abs(np.where(mask, z, np.nan))
    bucket = bucket_index(abs_z)
    side = np.sign(np.where(np.isnan(z), 0, z))
    gross = side * r
    adjusted = drift_adjusted(side, r, rbar[columns["symbol"]])
    funding_bp = side * columns["funding_bp"]
    net = gross - ROUND_TRIP_COST_BP - funding_bp
    rows = []
    for b, name in enumerate(BUCKET_NAMES):
        for sign_value in (1, -1):
            cell = mask & (bucket == b) & (side == sign_value)
            c = clusters_all[cell]
            rows.append({"bucket": name, "side": int(sign_value), "n": int(cell.sum()),
                         "n_clusters": int(np.unique(c).size),
                         "gross_bp": _bootstrap_mean(gross[cell], c, weights),
                         "drift_adjusted_bp": _bootstrap_mean(adjusted[cell], c, weights),
                         "cost_bp": ROUND_TRIP_COST_BP,
                         "funding_bp": float(funding_bp[cell].mean()) if cell.any() else float("nan"),
                         "net_bp": _bootstrap_mean(net[cell], c, weights)})
    out["buckets"] = rows
    pooled = mask & (bucket >= 0)
    out["dose_response"] = dose_response(bucket[pooled], adjusted[pooled], clusters_all[pooled], weights)
    return out


def quadrant_index(oi_change, past240_bp, sigma240) -> np.ndarray:
    """0..3 per QUADRANTS, -1 when excluded: OI change missing or zero, price "flat"
    (|past240| < 0.25 sigma240) or the controls missing."""
    oi = np.asarray(oi_change, dtype=float)
    price = np.asarray(past240_bp, dtype=float) / 1e4
    sigma = np.asarray(sigma240, dtype=float)
    with np.errstate(invalid="ignore"):
        valid = ~np.isnan(oi) & (oi != 0) & ~np.isnan(price) & ~np.isnan(sigma)
        valid &= np.abs(price) >= QUADRANT_DEAD_ZONE_SIGMA * sigma
        index = np.where(oi > 0, 0, 2) + np.where(price > 0, 0, 1)
    return np.where(valid, index, -1)


def quadrant_table(results: dict, horizon: int, n_days: int, B: int = BOOTSTRAP_B) -> dict:
    """Forward returns per OI x price quadrant (long side gross; drift-adjusted; net long and net short)."""
    columns = _pooled(results, horizon)
    raw = np.concatenate([results[s]["raw"] for s in results])
    symbols = len(results)
    clusters_all = cluster_ids(columns["day"], horizon)
    groups = int(cluster_ids(np.array([max(n_days - 1, 0)]), horizon)[0]) + 1
    weights = cluster_weights(groups, B)
    r = columns["r"]
    has_r = ~np.isnan(r)
    rbar = np.array([r[has_r & (columns["symbol"] == i)].mean() if (has_r & (columns["symbol"] == i)).any()
                     else np.nan for i in range(symbols)])
    quadrant = quadrant_index(raw, columns["past240"], columns["sigma240"])
    adjusted = r - rbar[columns["symbol"]]
    funding_bp = columns["funding_bp"]
    rows = []
    for index, name in enumerate(QUADRANTS):
        cell = has_r & (quadrant == index)
        c = clusters_all[cell]
        rows.append({"quadrant": name, "n": int(cell.sum()), "n_clusters": int(np.unique(c).size),
                     "gross_long_bp": _bootstrap_mean(r[cell], c, weights),
                     "drift_adjusted_bp": _bootstrap_mean(adjusted[cell], c, weights),
                     "cost_bp": ROUND_TRIP_COST_BP,
                     "net_long_bp": _bootstrap_mean(r[cell] - ROUND_TRIP_COST_BP - funding_bp[cell], c, weights),
                     "net_short_bp": _bootstrap_mean(-r[cell] - ROUND_TRIP_COST_BP + funding_bp[cell], c, weights)})
    return {"horizon_min": horizon, "dead_zone_sigma240": QUADRANT_DEAD_ZONE_SIGMA,
            "excluded": int((has_r & (quadrant < 0)).sum()), "quadrants": rows}


# ---------------------------------------------------------------- report


def build_report(segment: str, results: dict, *, series: str = "of-cum240-z", code_commit: str, created_utc: str,
                 B: int = BOOTSTRAP_B) -> dict:
    first_ms, end_ms = segment_bounds_ms(segment)
    n_days = (end_ms - first_ms) // _DAY
    symbols = {symbol: {"decisions": int(len(result["z"])), "with_z": int((~np.isnan(result["z"])).sum()),
                        "without_controls": int((np.isnan(result["past240"]) | np.isnan(result["sigma240"])).sum()),
                        "horizons": {str(h): {"n": int((~np.isnan(result["returns"][h])).sum()),
                                              "excluded_compromised": result["excluded"][h]["compromised"],
                                              "excluded_past_end": result["excluded"][h]["past_end"]}
                                     for h in series_horizons(series)}}
               for symbol, result in results.items()}
    horizons = [screen_horizon(results, h, n_days, B) for h in series_horizons(series)]
    positioning = series in POSITIONING_SERIES
    quadrants = ([quadrant_table(results, h, n_days, B) for h in QUADRANT_HORIZONS]
                 if series == "oi-chg-4h" else None)
    family = (len(POSITIONING_SERIES) * len(POSITIONING_HORIZONS)) if positioning else len(HORIZONS)
    report = {"schema": SCHEMA, "segment": segment, "series": [series], "horizons_min": list(series_horizons(series)),
              "series_x_horizons_examined": family,
              "multiplicity": (f"{len(POSITIONING_SERIES)} series x {len(POSITIONING_HORIZONS)} horizons + the "
                               "oi-chg-4h quadrant table (positioning family)" if positioning
                               else f"1 series x {len(HORIZONS)} horizons"),
              "quadrants": _render(quadrants), "symbols": _render(symbols),
              "parameters": _render({"round_trip_cost_bp": ROUND_TRIP_COST_BP, "cost_model": COST_MODEL_V1.version,
                                     "bootstrap_B": B, "bootstrap_seed": BOOTSTRAP_SEED,
                                     "sigma_half_life_days": SIGMA_HALF_LIFE_DAYS, "bucket_edges": list(BUCKET_EDGES)}),
              "notes": ["Screening is exploratory: it never counts as a PASS and writes no ledger entry.",
                        *(["Positioning series: metrics rows usable from create_time + 5 min; robust z against "
                           "the previous 30 days of hourly values (min 500); the sign of beta says follow (+) or "
                           "fade (-), it is not assumed."] if positioning else []),
                        "Funding bp uses the price as the mark (rate x 10^4).",
                        "Costs: taker fee and market slippage floor on entry and exit; no maker, no stop.",
                        "Without controls: symbol intercepts and z only; with controls: plus hour of day, past240 "
                        "and sigma240."],
              "results": _render(horizons), "code_commit": code_commit, "created_utc": created_utc}
    report["report_hash"] = content_hash(report)
    return report


def check_report(report: dict) -> None:
    body = {key: value for key, value in report.items() if key != "report_hash"}
    if report.get("report_hash") != content_hash(body):
        raise ValueError("report_hash mismatch")


def report_paths(report: dict) -> tuple[str, str]:
    stem = f"reports/screens/{report['series'][0]}__{report['segment']}__{report['report_hash'][:16]}"
    return stem + ".json", stem + ".md"


def public_lines(report: dict) -> list[str]:
    """Counts only: symbol, horizon, segment, N, excluded counts; the report hash. No coefficients or returns."""
    check_report(report)
    lines = [f"screen {report['series'][0]} segment {report['segment']}"]
    for symbol, result in report["symbols"].items():
        for horizon, counts in result["horizons"].items():
            lines.append(f"{symbol} h={int(horizon)} segment={report['segment']} n={int(counts['n'])} "
                         f"excluded_compromised={int(counts['excluded_compromised'])} "
                         f"excluded_past_end={int(counts['excluded_past_end'])}")
    for table in report.get("quadrants") or []:
        for row in table["quadrants"]:
            lines.append(f"quadrant {row['quadrant']} h={int(table['horizon_min'])} n={int(row['n'])}")
    lines.append(f"report hash {report['report_hash']}")
    return lines


def _cell(value) -> str:
    return "n/a" if value is None else str(value)


def markdown(report: dict) -> str:
    check_report(report)
    lines = [f"# Screen {report['series'][0]} ({report['segment']})", "",
             f"Report hash: `{report['report_hash']}`. Series x horizons examined: "
             f"{report['series_x_horizons_examined']} ({report.get('multiplicity', '')}; exploratory; never a "
             f"PASS). Round-trip cost "
             f"{report['parameters']['round_trip_cost_bp']} bp.", ""]
    lines += [f"- {note}" for note in report["notes"]] + [""]
    for result in report["results"]:
        lines += [f"## h = {result['horizon_min']} m (clusters: {result['clusters']})", ""]
        for label in ("with_controls", "without_controls"):
            fit = result[label]
            z = fit.get("coefficients", {}).get("z")
            if z is None:
                lines.append(f"- {label}: n {fit['n']}, not estimable")
            else:
                lines.append(f"- {label}: beta_z {z['beta']} bp/z (se {z['se']}, t {z['t']}, naive se "
                             f"{z['se_naive']}), n {fit['n']}, clusters {fit['clusters']}")
        lines += [f"- IC (Spearman, symbol-demeaned r): {_cell(result['ic_spearman'])} (n {result['ic_n']})", "",
                  "| bucket | side | N | clusters | gross bp | drift-adj bp | cost bp | funding bp | net bp (se) |",
                  "|" + " --- |" * 9]
        for row in result["buckets"]:
            lines.append(f"| {row['bucket']} | {row['side']} | {row['n']} | {row['n_clusters']} | "
                         f"{_cell(row['gross_bp']['mean'])} | {_cell(row['drift_adjusted_bp']['mean'])} | "
                         f"{row['cost_bp']} | {_cell(row['funding_bp'])} | {_cell(row['net_bp']['mean'])} "
                         f"({_cell(row['net_bp']['se'])}) |")
        dose = result["dose_response"]
        lines += ["", f"Dose-response: slope {_cell(dose['slope_bp_per_bucket'])} bp per bucket "
                      f"(95% {_cell(dose['slope_ci95'][0])} .. {_cell(dose['slope_ci95'][1])}), Spearman "
                      f"{_cell(dose['spearman'])} (95% {_cell(dose['spearman_ci95'][0])} .. "
                      f"{_cell(dose['spearman_ci95'][1])}).", ""]
    for table in report.get("quadrants") or []:
        lines += [f"## Quadrants (OI change x 4 h price change), h = {table['horizon_min']} m", "",
                  f"Flat price (|past240| < {table['dead_zone_sigma240']} sigma240) excluded: {table['excluded']}.",
                  "", "| quadrant | N | clusters | gross long bp | drift-adj bp | cost bp | net long bp (se) | "
                  "net short bp (se) |", "|" + " --- |" * 8]
        for row in table["quadrants"]:
            lines.append(f"| {row['quadrant']} | {row['n']} | {row['n_clusters']} | "
                         f"{_cell(row['gross_long_bp']['mean'])} | {_cell(row['drift_adjusted_bp']['mean'])} | "
                         f"{row['cost_bp']} | {_cell(row['net_long_bp']['mean'])} ({_cell(row['net_long_bp']['se'])}) | "
                         f"{_cell(row['net_short_bp']['mean'])} ({_cell(row['net_short_bp']['se'])}) |")
        lines.append("")
    return "\n".join(lines) + "\n"
