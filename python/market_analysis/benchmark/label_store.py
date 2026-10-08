"""Streaming, geometry-specific label columns; all monetary columns are micro-R.

Outcome/reason codes are ASCII ordinals. X cells have zero placeholders in
amount columns, never observed zero returns: consumers must check the outcome.
Without an explicit segment, each row must fit wholly in its own chronological
segment. With one, only rows eligible for that segment survive. Purged signal
times are retained separately so selection cannot call them missing grid rows.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import NamedTuple

from ..data_lake import month_bounds_ms, validate_symbol
from . import hidden_guard, segments
from .labels import LabelParams, read_label_csv


class Geometry(NamedTuple):
    symbol: str
    horizon_min: int
    side: int
    k: Fraction
    rr_index: int


COLUMN_NAMES = ("signal_ms", "outcome", "exit_offset", "net_ur", "cost_ur", "fund_ur", "wallet_ur",
                "amb", "opt_net_ur", "opt_cost_ur", "opt_fund_ur")


@dataclass
class GeometryColumns:
    geometry: Geometry
    data: dict[str, array] = field(default_factory=lambda: {name: array("q") for name in COLUMN_NAMES})
    non_trades: dict[str, array] = field(default_factory=lambda: {name: array("q") for name in ("signal_ms", "reason")})
    purged_signal_ms: array = field(default_factory=lambda: array("q"))
    # Optional, parallel to data: d_ticks and p0 of every trade row (volatility inputs).
    metadata: dict[str, array] | None = None

    def __getitem__(self, name: str) -> array:
        return self.data[name]

    def __len__(self) -> int:
        return len(self.data["signal_ms"])


def load_geometry_columns(label_dir, symbol: str, months, geometries, params: LabelParams,
                          *, token=None, gate=None, segment: str | None = None,
                          metadata: bool = False) -> dict[Geometry, GeometryColumns]:
    """Guard all requested months before opening any file, then stream once.

    Input months may be unordered but must be distinct. Files must have the
    labels-v1 ordering, unique row keys, and signal times inside their month.
    Eligibility always uses the configured worst-case window, including for X
    and non-trade rows; a short realized exit never rescues a purged signal.
    With metadata=True each column also carries d_ticks and p0 for its trade
    rows, so volatility needs no second pass over the files.
    """
    months = list(months)
    hidden_guard.require_months(months, token, gate)
    validate_symbol(symbol)
    if len(set(months)) != len(months):
        raise ValueError("months must be distinct")
    if segment is not None:
        segments.segment_bounds_ms(segment)
    columns, lookup = {}, {}
    for value in geometries:
        geometry = Geometry(*value)
        if (geometry.symbol != symbol or type(geometry.horizon_min) is not int
                or geometry.horizon_min not in params.horizons or type(geometry.side) is not int
                or geometry.side not in (-1, 1) or type(geometry.k) not in (int, Fraction)
                or geometry.k not in params.k_grid or type(geometry.rr_index) is not int
                or not 0 <= geometry.rr_index < len(params.rr_grid)):
            raise ValueError("geometry is outside the requested symbol/label parameters")
        if geometry in columns:
            raise ValueError("duplicate geometry")
        columns[geometry] = GeometryColumns(geometry)
        if metadata:
            columns[geometry].metadata = {"d_ticks": array("q"), "p0": array("q")}
        lookup.setdefault((geometry.horizon_min, geometry.side, geometry.k), []).append(columns[geometry])
    for month in sorted(months):
        first, end, _ = month_bounds_ms(month)
        path = Path(label_dir) / f"labels__{symbol}__{month}.csv.gz"
        previous = None
        with path.open("rb") as stream:
            for row in read_label_csv(stream, params):
                key = (row.signal_ms, row.horizon_min, -row.side, row.k)
                if not first <= row.signal_ms < end or (previous is not None and key <= previous):
                    raise ValueError(f"{path.name}: unordered, duplicate or out-of-month label row")
                previous = key
                requested = lookup.get((row.horizon_min, row.side, row.k), ())
                if not requested:
                    continue
                row_segment = segment if segment is not None else segments.segment_of(row.signal_ms)
                window_end = segments.worst_case_window_end_ms(row.signal_ms, row.horizon_min,
                                                               params.time_limit_multiple)
                keep = row_segment is not None and segments.eligible(row_segment, row.signal_ms, window_end)
                for column in requested:
                    if not keep:
                        column.purged_signal_ms.append(row.signal_ms)
                    elif row.status != "T":
                        column.non_trades["signal_ms"].append(row.signal_ms)
                        column.non_trades["reason"].append(ord(row.status))
                    else:
                        pair = row.cells[column.geometry.rr_index]
                        pess, opt = pair.pess, pair.opt or pair.pess
                        values = (row.signal_ms, ord(pess.outcome), pess.exit_offset,
                                  pess.net_ur or 0, pess.cost_ur or 0, pess.fund_ur or 0, row.wallet_ur,
                                  int(pair.opt is not None), opt.net_ur or 0, opt.cost_ur or 0, opt.fund_ur or 0)
                        for name, value in zip(COLUMN_NAMES, values):
                            column[name].append(value)
                        if column.metadata is not None:
                            column.metadata["d_ticks"].append(row.d_ticks)
                            column.metadata["p0"].append(row.p0)
    return columns
