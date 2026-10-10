"""Dense stop-aware label table for the #182 benchmark (schemas ``labels-v1`` and ``labels-v2``).

For every decision time on each horizon's grid, both sides and every stop
multiple ``k``, the trade is labelled by ``scan.label_trade`` against every
reward:risk target in ``rr_grid``.

Decision times: ``signal_ms % (step_minutes * 60_000) == 0`` for the horizon; the
signal is the end of minute ``d = (signal_ms - start_ms) // 60_000 - 1`` (rows with
``d < 0`` are skipped) and a row belongs to the month containing ``signal_ms``.
Row order: ``signal_ms`` ascending, then horizon ascending, side +1 then -1, ``k``
ascending.

Per row (point in time: everything but ``p0`` uses data up to the end of minute
``d``): ``var`` is the EWMA variance of the 5-minute block ending at minute ``d``
for the horizon's half-life; ``sigma = horizon_sigma(var, horizon)``;
``d_ticks = ceil(k * sigma * p0 / (10**20 * tick))``; ``stop = p0 - side * d_ticks *
tick``; ``target_j = p0 + side * ceil(rr_j * d_ticks) * tick`` (rounded away from
entry). ``p0`` is the entry minute's open and ``tick`` the entry month's inferred
tick (``bars.infer_tick`` per monthly series before concatenation).

Non-trade statuses: V volatility unavailable (warm-up or no 5-minute block yet),
C entry minute compromised, G stop narrower than ``min_stop_ticks``, N no
admissible leverage (also a long stop at or below zero, i.e. a stop wider than
the price), I incomplete window (the window runs past the data), P entry open off
the entry month's tick grid (one of the rare off-tick prints ``bars.infer_tick``
tolerates under tick-v2: no stop/target grid can be built from it, and snapping it
would invent a fill price). Non-trade rows
leave every column after ``status`` empty.

CSV ``labels__SYMBOL__YYYY-MM.csv.gz`` (ASCII, deterministic gzip): header
``signal_ms,horizon_min,side,k,status,p0,sigma,d_ticks,leverage,wallet_ur,c0..`` with
one ``c`` column per ``rr_grid`` entry and ``k`` in canonical exact text. A cell
token is ``O:exit:net:cost:fund`` (amounts empty for 'X'); an ambiguous cell is
``pessimistic|optimistic``.

Sigma models (``LabelParams.sigma_model``, #220): ``ewma`` (default, schema ``labels-v1``, lb1
releases, unchanged) uses ``build_variance`` + ``horizon_sigma``. ``ewma-seasonal`` (schema
``labels-v2``, lb2 releases) uses the point-in-time intraday factors
(``volatility.seasonal_factors``): the variance level comes from
``build_variance_deseasonalised`` and the horizon sigma from ``horizon_sigma_seasonal`` with the
entry day's factors; a decision without factors for its entry day is V. The v2 header names the
model in its sigma column (``sigma_ewma_seasonal``); every other column is identical.
"""
from __future__ import annotations

from bisect import bisect_right
import csv
from dataclasses import dataclass
from fractions import Fraction
import re
import time
from typing import Iterator

from .. import data_lake
from .bars import MISSING, BarSeries, _text
from .canonical import content_hash, exact_from_str, exact_to_str
from .costs import COST_MODEL_V1, CostModel
from .funding import FundingSeries
from .scan import NON_TRADE_STATUSES, OUTCOMES, Cell, CellPair, label_trade, next_compromised
from .robust_sigma import CANDIDATE_MODELS, FTCAL_MODEL, ROBUST_MODELS, RobustSigma
from .volatility import (HCAL_CLIP, HCAL_MIN_DAYS, BLOCK_MINUTES, BLOCK_MS, DAY_MS, LAMBDA_NUM, SEASONAL_WINDOW_DAYS, VAR_SCALE,
                         build_variance, build_variance_deseasonalised, horizon_sigma, horizon_sigma_seasonal,
                         seasonal_factors)

SCHEMA = "labels-v1"
SCHEMA_V2 = "labels-v2"
SCHEMA_V3 = "labels-v3"
SIGMA_MODELS = {"ewma": SCHEMA, "ewma-seasonal": SCHEMA_V2, "ewma-robust": SCHEMA_V3,
                "ewma-robust-hcal": SCHEMA_V3}
# Candidate sigma models (audit only): refused by LabelParams unless ``allow_candidate=True`` and by the forward harness.
CANDIDATE_SIGMA_MODELS = {FTCAL_MODEL: SCHEMA_V3}
LABEL_NON_TRADE_STATUSES = (*NON_TRADE_STATUSES, "P")
FIXED_COLUMNS = ("signal_ms", "horizon_min", "side", "k", "status", "p0", "sigma", "d_ticks", "leverage",
                 "wallet_ur")
_INTEGER = re.compile(r"-?[0-9]+\Z")
_MINUTE_MS = 60_000
_BLOCK_MS = BLOCK_MINUTES * _MINUTE_MS


def _pairs(value, name: str) -> tuple:
    items = sorted(value.items()) if isinstance(value, dict) else sorted(tuple(pair) for pair in value)
    if any(type(key) is not int or type(item) is not int or item <= 0 for key, item in items):
        raise ValueError(f"{name} must map int horizons to positive ints")
    return tuple(items)


def _fractions(value, name: str) -> tuple:
    items = tuple(Fraction(item) if type(item) is int else item for item in value)
    if not items or any(type(item) is not Fraction or item <= 0 for item in items):
        raise ValueError(f"{name} must be positive Fractions")
    if list(items) != sorted(set(items)):
        raise ValueError(f"{name} must be strictly ascending")
    return items


@dataclass(frozen=True)
class LabelParams:
    horizons: tuple = (15, 60, 240)
    half_life_days: tuple = ((15, 1), (60, 3), (240, 7))
    step_minutes: tuple = ((15, 5), (60, 15), (240, 15))
    k_grid: tuple = (Fraction(1), Fraction(2))
    rr_grid: tuple = (Fraction(1), Fraction(3, 2), Fraction(2), Fraction(3))
    time_limit_multiple: int = 4
    min_stop_ticks: int = 4
    cost_model: CostModel = COST_MODEL_V1
    schema: str | None = None  # derived from sigma_model when omitted
    sigma_model: str = "ewma"
    # Not part of the record or identity: only lets a CANDIDATE sigma model (calibration audit) be constructed.
    allow_candidate: bool = False

    def __post_init__(self):
        horizons = tuple(self.horizons)
        if (not horizons or list(horizons) != sorted(set(horizons))
                or any(type(h) is not int or h < BLOCK_MINUTES or h % BLOCK_MINUTES for h in horizons)):
            raise ValueError("horizons must be ascending positive multiples of 5 minutes")
        object.__setattr__(self, "horizons", horizons)
        half_lives = _pairs(self.half_life_days, "half_life_days")
        steps = _pairs(self.step_minutes, "step_minutes")
        if [h for h, _ in half_lives] != list(horizons) or [h for h, _ in steps] != list(horizons):
            raise ValueError("half_life_days and step_minutes need exactly one entry per horizon")
        if any(days not in LAMBDA_NUM for _, days in half_lives):
            raise ValueError(f"half lives must be in {sorted(LAMBDA_NUM)}")
        if any(step % BLOCK_MINUTES for _, step in steps):
            raise ValueError("decision steps must be multiples of 5 minutes (aligned to variance blocks)")
        object.__setattr__(self, "half_life_days", half_lives)
        object.__setattr__(self, "step_minutes", steps)
        object.__setattr__(self, "k_grid", _fractions(self.k_grid, "k_grid"))
        object.__setattr__(self, "rr_grid", _fractions(self.rr_grid, "rr_grid"))
        for name in ("time_limit_multiple", "min_stop_ticks"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive int")
        if not isinstance(self.cost_model, CostModel):
            raise ValueError("cost_model must be a CostModel")
        if self.cost_model.cost_multiplier != 1 or self.cost_model.funding_multiplier != 1:
            raise ValueError("labels are built at cost_multiplier = funding_multiplier = 1 (the grid is applied later)")
        if self.sigma_model in CANDIDATE_SIGMA_MODELS and not self.allow_candidate:
            raise ValueError(f"sigma_model {self.sigma_model!r} is a candidate for the calibration audit only; "
                             "labels, experiments and the forward harness do not accept it")
        known = {**SIGMA_MODELS, **(CANDIDATE_SIGMA_MODELS if self.allow_candidate else {})}
        if self.sigma_model not in known:
            raise ValueError(f"sigma_model must be one of {sorted(SIGMA_MODELS)}, got {self.sigma_model!r}")
        expected = known[self.sigma_model]
        if self.schema is None:
            object.__setattr__(self, "schema", expected)
        elif self.schema != expected:
            raise ValueError(f"unsupported label schema {self.schema!r} for sigma model {self.sigma_model!r}")

    @property
    def seasonal(self) -> bool:
        return self.sigma_model == "ewma-seasonal"

    def half_life(self, horizon: int) -> int:
        return dict(self.half_life_days)[horizon]

    def step(self, horizon: int) -> int:
        return dict(self.step_minutes)[horizon]

    def window(self, horizon: int) -> int:
        return self.time_limit_multiple * horizon

    @property
    def header(self) -> tuple:
        model = "sigma_" + self.sigma_model.replace("-", "_")
        fixed = tuple(model if name == "sigma" and self.sigma_model != "ewma" else name for name in FIXED_COLUMNS)
        return (*fixed, *(f"c{index}" for index in range(len(self.rr_grid))))

    def to_record(self) -> dict:
        record = {
            "schema": self.schema,
            "horizons": list(self.horizons),
            "half_life_days": {str(h): days for h, days in self.half_life_days},
            "step_minutes": {str(h): step for h, step in self.step_minutes},
            "k_grid": [exact_to_str(k) for k in self.k_grid],
            "rr_grid": [exact_to_str(rr) for rr in self.rr_grid],
            "time_limit_multiple": self.time_limit_multiple,
            "min_stop_ticks": self.min_stop_ticks,
            "cost_model": self.cost_model.to_record(),
            "cost_model_identity": self.cost_model.identity(),
        }
        if self.sigma_model != "ewma":  # lb1 records (and identities) stay exactly as before
            record["sigma_model"] = self.sigma_model
            record["seasonal_window_days"] = SEASONAL_WINDOW_DAYS
        if self.sigma_model in ROBUST_MODELS or self.sigma_model in CANDIDATE_SIGMA_MODELS:  # labels-v3 only
            record["robust"] = {"statistic": "EWMA of |r5| / 0.797885", "seasonal_on": "|r5|",
                                "aggregation": "sum of (s * g)^2 over the horizon's blocks"}
            if self.sigma_model == "ewma-robust-hcal":
                record["robust"]["hcal"] = {"statistic": "median |ln(open[e+h]/open[e])| / sigma / 0.674490",
                                            "grid": "label step", "min_days": HCAL_MIN_DAYS,
                                            "clip": [str(HCAL_CLIP[0]), str(HCAL_CLIP[1])]}
            if self.sigma_model == FTCAL_MODEL:
                record["robust"]["ftcal"] = {"statistic": "median over past windows of M_e / sigma, M_e = max over the "
                                                          "window's 1m bars of max(ln(high/open_e), -ln(low/open_e)), "
                                                          "divided by the median of sup|W| on [0, 1]",
                                             "grid": "label step", "min_days": HCAL_MIN_DAYS,
                                             "clip": [str(HCAL_CLIP[0]), str(HCAL_CLIP[1])]}
        return record

    def identity(self) -> str:
        return content_hash(self.to_record())


@dataclass(frozen=True)
class LabelRow:
    signal_ms: int
    horizon_min: int
    side: int
    k: Fraction
    status: str
    p0: int | None = None
    sigma: int | None = None
    d_ticks: int | None = None
    leverage: int | None = None
    wallet_ur: int | None = None
    cells: tuple = ()


def month_of(ms: int) -> str:
    moment = time.gmtime(ms // 1000)
    return f"{moment.tm_year:04d}-{moment.tm_mon:02d}"


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def build_labels(symbol: str, bars: BarSeries, funding: FundingSeries, params: LabelParams,
                 ticks: dict) -> Iterator[tuple[str, list[LabelRow]]]:
    """(month, rows) for every month the series touches, one month at a time."""
    if bars.symbol != symbol:
        raise ValueError(f"bars are {bars.symbol}, not {symbol}")
    months = data_lake.months_between(month_of(bars.start_ms), month_of(bars.end_ms - 1))
    missing = [month for month in months if month not in ticks]
    if missing:
        raise ValueError(f"no tick for months {missing}")
    month_starts = [data_lake.month_bounds_ms(month)[0] for month in months]
    half_lives = {params.half_life(h) for h in params.horizons}
    if params.seasonal:
        factors = seasonal_factors(bars, SEASONAL_WINDOW_DAYS)
        variances = {days: build_variance_deseasonalised(bars, days, factors).variance for days in half_lives}

        def sigma_of(var: int, horizon: int, signal_ms: int):
            day = factors.day_factors(signal_ms // DAY_MS)  # entry day's factors (known at entry)
            if day is None:
                return None
            return lambda: horizon_sigma_seasonal(var, day, (signal_ms % DAY_MS) // BLOCK_MS, horizon)
    elif params.sigma_model in ROBUST_MODELS or params.sigma_model in CANDIDATE_SIGMA_MODELS:
        if params.sigma_model in CANDIDATE_SIGMA_MODELS and not params.allow_candidate:
            raise ValueError(f"sigma_model {params.sigma_model!r} is a candidate for the calibration audit only")
        robust = RobustSigma(bars, hcal=params.sigma_model == "ewma-robust-hcal",
                             ftcal=params.sigma_model == FTCAL_MODEL)
        variances = {days: robust.levels(days) for days in half_lives}

        def sigma_of(var: int, horizon: int, signal_ms: int):
            value = robust.sigma(horizon, params.half_life(horizon), params.step(horizon), signal_ms)
            return None if value is None else (lambda: value)
    else:
        variances = {days: build_variance(bars, days).variance for days in half_lives}

        def sigma_of(var: int, horizon: int, signal_ms: int):
            return lambda: horizon_sigma(var, horizon)
    next_comp = next_compromised(bars)

    def tick_at(index: int) -> int:
        return ticks[months[bisect_right(month_starts, bars.open_time(index)) - 1]]

    for month in months:
        start, end, _ = data_lake.month_bounds_ms(month)
        first = max(start, bars.start_ms)
        first += -first % _BLOCK_MS
        rows = []
        for signal_ms in range(first, min(end, bars.end_ms + 1), _BLOCK_MS):
            d = (signal_ms - bars.start_ms) // _MINUTE_MS - 1
            if d < 0:
                continue
            for horizon in params.horizons:
                if signal_ms % (params.step(horizon) * _MINUTE_MS):
                    continue
                variance = variances[params.half_life(horizon)]
                for side in (1, -1):
                    for k in params.k_grid:
                        rows.append(_row(bars, funding, params, signal_ms, d, horizon, side, k, variance,
                                         next_comp, tick_at, sigma_of))
        yield month, rows


def row_factory(symbol: str, bars: BarSeries, funding: FundingSeries, params: LabelParams, ticks: dict):
    """``row(signal_ms, horizon, side, k) -> LabelRow`` for ONE decision, with the sigma, tick and row
    function ``build_labels`` uses (robust models only), without generating every row of every month.

    The forward parity workflow compares forward setups with these rows on full-history bars: the
    expanding hcal calibration is the point, but generating every label row of 17 months is not.
    ``test_benchmark_robust_sigma`` pins ``row_factory`` rows equal to ``build_labels`` rows.
    """
    if bars.symbol != symbol:
        raise ValueError(f"bars are {bars.symbol}, not {symbol}")
    if params.sigma_model not in ROBUST_MODELS:
        raise ValueError("row_factory supports the robust sigma models only")
    months = data_lake.months_between(month_of(bars.start_ms), month_of(bars.end_ms - 1))
    missing = [month for month in months if month not in ticks]
    if missing:
        raise ValueError(f"no tick for months {missing}")
    month_starts = [data_lake.month_bounds_ms(month)[0] for month in months]
    robust = RobustSigma(bars, hcal=params.sigma_model == "ewma-robust-hcal")
    variances = {days: robust.levels(days) for days in {params.half_life(h) for h in params.horizons}}
    next_comp = next_compromised(bars)

    def sigma_of(var: int, horizon: int, signal_ms: int):
        value = robust.sigma(horizon, params.half_life(horizon), params.step(horizon), signal_ms)
        return None if value is None else (lambda: value)

    def tick_at(index: int) -> int:
        return ticks[months[bisect_right(month_starts, bars.open_time(index)) - 1]]

    def row(signal_ms: int, horizon: int, side: int, k) -> LabelRow:
        d = (signal_ms - bars.start_ms) // _MINUTE_MS - 1
        if d < 0:
            raise ValueError("signal precedes the bars")
        return _row(bars, funding, params, signal_ms, d, horizon, side, k, variances[params.half_life(horizon)],
                    next_comp, tick_at, sigma_of)

    row.robust = robust
    row.tick_at = lambda entry_ms: tick_at((entry_ms - bars.start_ms) // _MINUTE_MS)
    return row


def _row(bars, funding, params, signal_ms, d, horizon, side, k, variance, next_comp, tick_at, sigma_of) -> LabelRow:
    base = (signal_ms, horizon, side, k)
    e = d + 1
    window = params.window(horizon)
    if e + window - 1 >= bars.minutes:
        return LabelRow(*base, "I")
    block = (d + 1) // BLOCK_MINUTES - 1  # the 5-minute block ending at minute d
    var = variance[block] if block >= 0 else MISSING
    sigma_fn = sigma_of(var, horizon, signal_ms) if var != MISSING else None
    if sigma_fn is None:
        return LabelRow(*base, "V")
    if next_comp[e] == e:
        return LabelRow(*base, "C")
    p0 = bars.open[e]
    tick = tick_at(e)
    if p0 % tick:
        return LabelRow(*base, "P")
    sigma = sigma_fn()
    d_ticks = _ceil_div(k.numerator * sigma * p0, k.denominator * VAR_SCALE * tick)
    if d_ticks < params.min_stop_ticks:
        return LabelRow(*base, "G")
    stop = p0 - side * d_ticks * tick
    if stop <= 0:
        return LabelRow(*base, "N")  # stop wider than the price: no admissible leverage
    targets = [p0 + side * _ceil_div(rr.numerator * d_ticks, rr.denominator) * tick for rr in params.rr_grid]
    trade = label_trade(bars, funding, params.cost_model, tick=tick, entry_index=e, side=side, stop_price=stop,
                        target_prices=targets, window_minutes=window, next_comp=next_comp)
    if trade.status != "T":
        return LabelRow(*base, trade.status)
    return LabelRow(*base, "T", p0, sigma, d_ticks, trade.leverage, trade.wallet_ur, trade.cells)


# ---------------------------------------------------------------- CSV


def _amount(value: int | None) -> str:
    return "" if value is None else str(value)


def _cell_token(cell: Cell) -> str:
    return f"{cell.outcome}:{cell.exit_offset}:{_amount(cell.net_ur)}:{_amount(cell.cost_ur)}:{_amount(cell.fund_ur)}"


def cell_text(pair: CellPair) -> str:
    return _cell_token(pair.pess) if pair.opt is None else f"{_cell_token(pair.pess)}|{_cell_token(pair.opt)}"


def _line(row: LabelRow, params: LabelParams) -> str:
    fields = [str(row.signal_ms), str(row.horizon_min), str(row.side), exact_to_str(row.k), row.status]
    if row.status == "T":
        fields += [str(row.p0), str(row.sigma), str(row.d_ticks), str(row.leverage), str(row.wallet_ur)]
        fields += [cell_text(pair) for pair in row.cells]
    else:
        fields += [""] * (len(params.header) - len(fields))
    return ",".join(fields) + "\n"


def write_label_csv(fileobj, rows, params: LabelParams) -> int:
    """Deterministic gzip CSV of label rows; returns the number of data rows."""
    def lines():
        yield ",".join(params.header) + "\n"
        for row in rows:
            if row.status == "T" and len(row.cells) != len(params.rr_grid):
                raise ValueError(f"row at {row.signal_ms} has {len(row.cells)} cells for {len(params.rr_grid)} targets")
            yield _line(row, params)

    return data_lake.write_csv_gz(fileobj, lines())


def _int(text: str, minimum: int | None = None) -> int:
    if not _INTEGER.match(text):
        raise ValueError(f"invalid integer {text!r}")
    value = int(text)
    if minimum is not None and value < minimum:
        raise ValueError(f"{value} is below {minimum}")
    return value


def _parse_cell(text: str) -> Cell:
    parts = text.split(":")
    if len(parts) != 5 or parts[0] not in OUTCOMES:
        raise ValueError(f"invalid cell token {text!r}")
    outcome, offset, net, cost, fund = parts
    amounts = (net, cost, fund)
    if outcome == "X":
        if any(amounts):
            raise ValueError(f"an X cell carries no amounts: {text!r}")
        return Cell("X", _int(offset, 0), None, None, None)
    return Cell(outcome, _int(offset, 0), _int(net), _int(cost), _int(fund))


def _parse_pair(text: str) -> CellPair:
    parts = text.split("|")
    if len(parts) == 1:
        return CellPair(_parse_cell(parts[0]))
    if len(parts) == 2:
        pess, opt = _parse_cell(parts[0]), _parse_cell(parts[1])
        if (pess.outcome, opt.outcome) != ("S", "T") or pess.exit_offset != opt.exit_offset:
            raise ValueError(f"an ambiguous cell is a same-minute stop|target pair, got {text!r}")
        return CellPair(pess, opt)
    raise ValueError(f"invalid cell {text!r}")


def read_label_csv(fileobj, params: LabelParams) -> Iterator[LabelRow]:
    """Strictly validated label rows; errors name the data line and column."""
    reader = csv.reader(_text(fileobj))
    header = next(reader, None)
    if header is None or tuple(header) != params.header:
        raise ValueError(f"labels header differs from {','.join(params.header)}")
    statuses = ("T", *LABEL_NON_TRADE_STATUSES)
    for line, values in enumerate(reader, start=1):
        if len(values) != len(params.header):
            raise ValueError(f"labels line {line}: expected {len(params.header)} columns, got {len(values)}")
        column = "signal_ms"
        try:
            signal_ms = _int(values[0], 0)
            column = "horizon_min"
            horizon = _int(values[1])
            if horizon not in params.horizons:
                raise ValueError(f"horizon {horizon} is not in {params.horizons}")
            column = "side"
            if values[2] not in ("1", "-1"):
                raise ValueError(f"side must be 1 or -1, got {values[2]!r}")
            side = int(values[2])
            column = "k"
            k = exact_from_str(values[3])
            if k not in params.k_grid:
                raise ValueError(f"k {values[3]} is not in the k grid")
            column = "status"
            status = values[4]
            if status not in statuses:
                raise ValueError(f"unknown status {status!r}")
            if status != "T":
                for column, value in zip(params.header[5:], values[5:]):
                    if value:
                        raise ValueError("non-trade rows leave this column empty")
                yield LabelRow(signal_ms, horizon, side, k, status)
                continue
            column = "p0"
            p0 = _int(values[5], 1)
            column = "sigma"
            sigma = _int(values[6], 0)
            column = "d_ticks"
            d_ticks = _int(values[7], 1)
            column = "leverage"
            leverage = _int(values[8], 1)
            column = "wallet_ur"
            wallet_ur = _int(values[9], 0)
            cells = []
            for column, value in zip(params.header[10:], values[10:]):
                cells.append(_parse_pair(value))
        except ValueError as error:
            raise ValueError(f"labels line {line} column {column}: {error}") from None
        yield LabelRow(signal_ms, horizon, side, k, "T", p0, sigma, d_ticks, leverage, wallet_ur, tuple(cells))
