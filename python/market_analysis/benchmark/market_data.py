"""Guarded loading of data-lake 1-minute bars for indicator history (#182 slice F).

Indicators need history before the evaluated segment (EMA200 on 4h candles warms
up over 402 candles, about 67 days). Callers pass ``data_lake.FIRST_MONTH`` as
``first_month`` so every segment's indicators see the same contiguous history,
and the last month of the evaluated segment as ``last_month``. Every requested
month is authorized with ``hidden_guard.require_months`` before any file is
opened: indicator history for a development/validation run therefore never
touches hidden months, and a hidden run needs a verified token. Funding settlements
(``funding__SYMBOL__MONTH.csv.gz``, downloaded with the bars) are loaded the same way.

Memory: 33 months of one symbol are about 1.4M minutes x 14 int64 columns
(roughly 160 MB); load one symbol at a time and keep only its candles.
"""
from __future__ import annotations

from pathlib import Path

from .. import data_lake
from .bars import BarSeries, read_bars_csv
from .candles import TIMEFRAMES, CandleSeries, build_candles
from .funding import FundingSeries, read_funding_csv
from .hidden_guard import require_months


def load_symbol_bars(bars_dir, symbol: str, first_month: str, last_month: str, *, token=None,
                     gate=None) -> BarSeries:
    """Contiguous bars for ``first_month..last_month`` (inclusive) of one symbol."""
    data_lake.validate_symbol(symbol)
    months = data_lake.months_between(first_month, last_month)
    require_months(months, token, gate)  # all months, before opening any file
    parts = []
    for month in months:
        with (Path(bars_dir) / data_lake.bars_asset_name(symbol, month)).open("rb") as stream:
            parts.append(read_bars_csv(stream, symbol, month))
    return BarSeries.concat(parts)


def load_symbol_candles(bars_dir, symbol: str, first_month: str, last_month: str, timeframes=TIMEFRAMES, *,
                        token=None, gate=None) -> dict[int, CandleSeries]:
    """{minutes: CandleSeries} built from one guarded bar load; the bars are then released."""
    series = load_symbol_bars(bars_dir, symbol, first_month, last_month, token=token, gate=gate)
    return {minutes: build_candles(series, minutes) for minutes in timeframes}


def load_symbol_funding(bars_dir, symbol: str, first_month: str, last_month: str, *, token=None,
                        gate=None) -> FundingSeries:
    """Contiguous funding settlements for ``first_month..last_month`` (inclusive) of one symbol."""
    data_lake.validate_symbol(symbol)
    months = data_lake.months_between(first_month, last_month)
    require_months(months, token, gate)  # all months, before opening any file
    parts = []
    for month in months:
        with (Path(bars_dir) / data_lake.funding_asset_name(symbol, month)).open("rb") as stream:
            parts.append(read_funding_csv(stream, month))
    return FundingSeries.concat(parts)
