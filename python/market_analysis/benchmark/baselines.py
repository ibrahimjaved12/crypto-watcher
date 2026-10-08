"""Geometry-matched baselines and counter-RNG placebo comparisons for #182.

Placebo draws use word % cell_size. The modulo bias is at most cell_size / 2**64
per draw and negligible at label-pool sizes. Matching uses UTC hour-of-week
(Monday 00:00 = 0), and exact d_ticks*tick/p0 volatility. Pool tercile boundaries
are nearest-rank 1/3 and 2/3 within (symbol, horizon, k), including both sides;
ties stay together in the lower tercile. X outcomes have no return and are not
part of the placebo population. No numpy.random or wall clock is used.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from itertools import chain
import json
from pathlib import Path

from .evaluate import Trade, decimal_text, nearest_rank, net_at, select
from .hidden_guard import require_months
from .labels import read_label_csv
from .rng import u64_words
from .scan import UR
from .segments import eligible, worst_case_window_end_ms


def random_walk_hit_rate(rr) -> Fraction:
    if type(rr) not in (int, Fraction) or rr <= 0:
        raise ValueError("rr must be a positive int or Fraction")
    return 1 / (1 + Fraction(rr))


def random_walk_comparison(trades, rr) -> dict:
    targets = stops = 0
    for row in trades:
        targets += row.outcome == "T"
        stops += row.outcome == "S"
    return {"expected_target_share": decimal_text(random_walk_hit_rate(rr)),
            "observed_target_share": decimal_text(Fraction(targets, targets + stops)) if targets + stops else None,
            "target_count": targets, "stop_count": stops}


def always_side(columns, side: int):
    """Every eligible grid signal on a side, including non-trade reason counts.

    Returns C1 Selection for use with its metrics and daily_series functions.
    columns must represent a single (k, rr_index, horizon) variant.
    """
    if type(side) is not int or side not in (-1, 1):
        raise ValueError("side must be +1 or -1")
    chosen = {key: value for key, value in columns.items() if value.geometry.side == side}
    signals = []
    for column in chosen.values():
        g = column.geometry
        for ms in chain(column["signal_ms"], column.non_trades["signal_ms"]):
            signals.append((g.symbol, ms, side, g.horizon_min))
    return select(chosen, sorted(signals))


def trade_key(row: Trade) -> tuple:
    g = row.geometry
    return g.symbol, g.horizon_min, g.side, g.k, row.signal_ms


@dataclass(frozen=True)
class PoolRow:
    trade: Trade
    volatility: Fraction

    def __post_init__(self):
        if type(self.volatility) not in (int, Fraction) or self.volatility <= 0:
            raise ValueError("volatility must be a positive exact ratio")


def load_volatility(label_dir, symbol, months, geometries, params, segment, *, token=None, gate=None) -> dict:
    """Read metadata omitted by C1; require the actual label manifest tick sizes.

    No proxy or inferred-from-outcomes volatility is allowed. The same hidden
    authorization and worst-case segment purge apply before metadata is exposed.
    C1 validates file ordering/month ownership when loading the parallel columns.
    """
    months = list(months)
    require_months(months, token, gate)
    root = Path(label_dir)
    manifest = json.loads((root / f"labels__{symbol}.manifest.json").read_bytes())
    if (manifest["params_identity"] != params.identity() or manifest["symbol"] != symbol
            or manifest["schema"] != params.schema):
        raise ValueError("label manifest does not match the requested parameters/symbol/schema")
    requested = {(g.horizon_min, g.side, g.k) for g in geometries}
    result = {}
    for month in sorted(months):
        tick = manifest["ticks"][month]
        if type(tick) is not int or tick <= 0:
            raise ValueError("manifest tick must be a positive integer")
        with (root / f"labels__{symbol}__{month}.csv.gz").open("rb") as stream:
            for row in read_label_csv(stream, params):
                if row.status != "T" or (row.horizon_min, row.side, row.k) not in requested:
                    continue
                if eligible(segment, row.signal_ms, worst_case_window_end_ms(
                        row.signal_ms, row.horizon_min, params.time_limit_multiple)):
                    result[(symbol, row.horizon_min, row.side, row.k, row.signal_ms)] = Fraction(row.d_ticks * tick, row.p0)
    return result


def _group(row: PoolRow) -> tuple:
    g = row.trade.geometry
    return g.symbol, g.horizon_min, g.k


def tercile_boundaries(pool) -> dict:
    groups = defaultdict(list)
    for row in pool:
        if row.trade.outcome != "X":
            groups[_group(row)].append(row.volatility)
    return {key: (nearest_rank(values, Fraction(1, 3)), nearest_rank(values, Fraction(2, 3)))
            for key, values in groups.items()}


def volatility_tercile(row: PoolRow, boundaries: dict) -> int:
    low, high = boundaries[_group(row)]
    return 1 if row.volatility <= low else 2 if row.volatility <= high else 3


def _cell(row: PoolRow) -> tuple:
    g = row.trade.geometry
    utc = datetime.fromtimestamp(row.trade.signal_ms // 1000, timezone.utc)
    # rr_index is also fixed: never mix reward targets while matching a variant.
    return g.symbol, g.horizon_min, g.side, g.k, g.rr_index, utc.weekday() * 24 + utc.hour


def matched_placebo(strategy_trades, pool, B: int = 200, *, seed: int, stream_prefix: str) -> dict:
    """PoolRow inputs; return mean-net-R replicates, 5/50/95 quantiles and p.

    fallback_count counts strategy trades needing the reduced cell (per replicate,
    not multiplied by B). If even that cell is empty, fail rather than silently
    change symbol/side/hour matching or the observed-trade denominator.
    """
    if type(B) is not int or B < 1:
        raise ValueError("B must be positive")
    u64_words(seed, stream_prefix, 0, 0)  # validate even for an empty strategy
    pool = sorted((row for row in pool if row.trade.outcome != "X"),
                  key=lambda row: (row.trade.geometry, row.trade.signal_ms))
    strategy = sorted((row for row in strategy_trades if row.trade.outcome != "X"),
                      key=lambda row: (row.trade.geometry, row.trade.signal_ms))
    boundaries = tercile_boundaries(pool)
    cells, fallback_cells = defaultdict(list), defaultdict(list)
    for row in pool:
        cell = _cell(row)
        fallback_cells[cell].append(row.trade)
        cells[cell + (volatility_tercile(row, boundaries),)].append(row.trade)
    candidates, fallback_count = [], 0
    for row in strategy:
        cell = _cell(row)
        group = _group(row)
        candidates_here = cells.get(cell + (volatility_tercile(row, boundaries),)) if group in boundaries else None
        if not candidates_here:
            fallback_count += 1
            candidates_here = fallback_cells.get(cell)
        if not candidates_here:
            raise ValueError("no placebo pool row for the required symbol/horizon/side/k/hour")
        candidates.append(candidates_here)
    if not strategy:
        return {"B": B, "n_trades": 0, "fallback_count": 0, "observed_mean_net_r": None,
                "means_net_r": [], "quantiles": {"5": None, "50": None, "95": None}, "p_placebo": None,
                "cost_grid": {str(m): {"observed_mean_net_r": None,
                                       "quantiles": {"5": None, "50": None, "95": None}, "p_placebo": None}
                              for m in range(4)}}
    observed = sum(net_at(row.trade) for row in strategy) / len(strategy) / UR
    by_cost = {m: [] for m in range(4)}
    for b in range(B):
        words = u64_words(seed, f"{stream_prefix}/rep/{b}", 0, len(strategy))
        drawn = [rows[int(word) % len(rows)] for rows, word in zip(candidates, words)]
        for m in range(4):
            by_cost[m].append(sum(net_at(row, m) for row in drawn) / len(strategy) / UR)
    means = by_cost[1]
    cost_grid = {}
    for m, values in by_cost.items():
        observed_m = sum(net_at(row.trade, m) for row in strategy) / len(strategy) / UR
        cost_grid[str(m)] = {
            "observed_mean_net_r": decimal_text(observed_m),
            "quantiles": {str(p): decimal_text(nearest_rank(values, Fraction(p, 100))) for p in (5, 50, 95)},
            "p_placebo": decimal_text(Fraction(1 + sum(value >= observed_m for value in values), B + 1))}
    return {"B": B, "n_trades": len(strategy), "fallback_count": fallback_count,
            "observed_mean_net_r": decimal_text(observed), "means_net_r": [decimal_text(value) for value in means],
            "quantiles": {str(p): decimal_text(nearest_rank(means, Fraction(p, 100))) for p in (5, 50, 95)},
            "p_placebo": decimal_text(Fraction(1 + sum(value >= observed for value in means), B + 1)),
            "cost_grid": cost_grid}
