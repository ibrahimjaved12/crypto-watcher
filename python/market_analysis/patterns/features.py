"""Point-in-time features using the existing floating-point technical helpers.

Benchmark prices/quantities remain scaled integers in storage. Only this adapter
converts them to the float convention of technical.py. ATR/EMA series are reused
once per contiguous valid run, including prefix-identical linear-time ADX.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import data_lake
from ..benchmark.bars import MISSING
from ..benchmark.candles import CandleSeries
from ..technical import TechnicalCandle, _adx_series, _ema, _finite, _true_ranges, _wema


@dataclass(frozen=True)
class CandleFeatures:
    index: int
    high: float | None = None
    low: float | None = None
    range: float | None = None
    body: float | None = None
    upper: float | None = None
    lower: float | None = None
    body_ratio: float | None = None
    upper_ratio: float | None = None
    lower_ratio: float | None = None
    atr_prev: float | None = None
    range_atr: float | None = None
    rel_volume: float | None = None
    adx_prev: float | None = None
    trend_dir: int | None = None
    prior20_high: float | None = None
    prior20_low: float | None = None
    missing_inputs: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        # A zero/nonpositive ATR is present but fails the pattern's gate. Zero
        # reference volume instead makes the relative-volume input unavailable.
        return (not self.missing_inputs
                and all(_finite(value) for value in (
                    self.high, self.low, self.atr_prev, self.rel_volume,
                    self.adx_prev, self.prior20_high, self.prior20_low))
                and self.trend_dir in (-1, 0, 1))


@dataclass(frozen=True)
class _FeatureRun:
    first: int
    candles: tuple[TechnicalCandle, ...]
    atr: tuple[float | None, ...]
    ema_fast: tuple[float | None, ...]
    ema_slow: tuple[float | None, ...]
    adx: tuple[float | None, ...]
    # Additive (candle-table-v1): the +DI/-DI of the same _adx_series pass.
    plus_di: tuple[float | None, ...] = ()
    minus_di: tuple[float | None, ...] = ()


class CandleFeatureHistory:
    """Reusable causal indicator series; reading i uses values through i only.

    Every gap resets the helper initialization. Batch preparation may include
    later candles, but helper outputs at j depend only on the prefix through j.
    Setting end_index limits even preparation to the requested completed prefix.
    """

    def __init__(self, candles: CandleSeries, *, atr_period: int, adx_period: int,
                 ema_fast_period: int, ema_slow_period: int, lookback: int,
                 end_index: int | None = None):
        self._series = candles
        self._adx_period = adx_period
        self._lookback = lookback
        count = len(candles) if end_index is None else end_index + 1
        if not 0 <= count <= len(candles):
            raise IndexError("completed candle index outside series")
        self._runs: list[_FeatureRun | None] = [None] * count
        scale = 10 ** data_lake.SCALE_DIGITS
        first = 0
        while first < count:
            if not candles.valid[first]:
                first += 1
                continue
            end = first + 1
            while end < count and candles.valid[end]:
                end += 1
            run_candles = tuple(TechnicalCandle(
                open_ms=candles.open_ms(j), open=candles.open[j] / scale,
                high=candles.high[j] / scale, low=candles.low[j] / scale,
                close=candles.close[j] / scale,
                volume=candles.volume[j] / scale if candles.volume[j] != MISSING else float("nan"),
            ) for j in range(first, end))
            closes = [candle.close for candle in run_candles]
            adx_rows = _adx_series(run_candles, adx_period)
            run = _FeatureRun(first, run_candles,
                              tuple(_wema(_true_ranges(run_candles), atr_period)),
                              tuple(_ema(closes, ema_fast_period)),
                              tuple(_ema(closes, ema_slow_period)),
                              tuple(value[0] for value in adx_rows),
                              tuple(value[1] for value in adx_rows),
                              tuple(value[2] for value in adx_rows))
            for j in range(first, end):
                self._runs[j] = run
            first = end

    def at(self, index: int, *, include_adx: bool = True) -> CandleFeatures:
        if not 0 <= index < len(self._runs):
            raise IndexError("completed candle index outside series")
        run = self._runs[index]
        if run is None:
            return CandleFeatures(index, missing_inputs=("invalid_candle",))
        local = index - run.first
        series = self._series
        scale = 10 ** data_lake.SCALE_DIGITS
        # Geometry ratios use the exact stored differences before conversion,
        # avoiding cancellation from subtracting separately converted prices.
        span = series.high[index] - series.low[index]
        body = abs(series.close[index] - series.open[index])
        upper = series.high[index] - max(series.open[index], series.close[index])
        lower = min(series.open[index], series.close[index]) - series.low[index]
        atr = run.atr[local - 2] if local >= 2 else None
        fast = run.ema_fast[local - 1] if local else None
        slow = run.ema_slow[local - 1] if local else None
        trend = ((fast > slow) - (fast < slow)) if _finite(fast) and _finite(slow) else None
        prior = run.candles[local - self._lookback:local] if local >= self._lookback else ()
        mean_volume = (sum(candle.volume for candle in prior) / self._lookback) if prior else None
        volume = run.candles[local].volume
        rel_volume = volume / mean_volume if (_finite(volume) and _finite(mean_volume)
                                               and mean_volume > 0) else None
        # The ADX helper needs 2 * period candles to publish its first value.
        adx = run.adx[local - 1] if (
            include_adx and local >= 2 * self._adx_period) else None
        missing = tuple(name for name, value in (
            ("atr_prev", atr), ("rel_volume", rel_volume), ("adx_prev", adx),
            ("ema_prev", trend), ("prior20", len(prior) if prior else None),
        ) if not _finite(value))
        return CandleFeatures(
            index=index, high=series.high[index] / scale, low=series.low[index] / scale,
            range=span / scale, body=body / scale, upper=upper / scale, lower=lower / scale,
            body_ratio=body / span if span > 0 else None,
            upper_ratio=upper / span if span > 0 else None,
            lower_ratio=lower / span if span > 0 else None,
            atr_prev=atr, range_atr=(span / scale) / atr if _finite(atr) and atr > 0 else None,
            rel_volume=rel_volume, adx_prev=adx, trend_dir=trend,
            prior20_high=max(candle.high for candle in prior) if prior else None,
            prior20_low=min(candle.low for candle in prior) if prior else None,
            missing_inputs=missing,
        )


    def indicators(self, index: int) -> dict:
        """ATR/EMA/ADX/+DI/-DI computed through candle ``index`` INCLUSIVE (None if invalid or unready).

        ``at(i)`` reads these values at i - 1; candle-table ``_prev`` columns call ``indicators(i - 1)``.
        """
        if not 0 <= index < len(self._runs):
            raise IndexError("completed candle index outside series")
        run = self._runs[index]
        names = ("atr", "ema_fast", "ema_slow", "adx", "plus_di", "minus_di")
        if run is None:
            return dict.fromkeys(names)
        local = index - run.first

        def value(series, position):
            return series[position] if 0 <= position < len(series) and _finite(series[position]) else None

        return {"atr": value(run.atr, local - 1), "ema_fast": value(run.ema_fast, local),
                "ema_slow": value(run.ema_slow, local),
                "adx": value(run.adx, local) if local + 1 >= 2 * self._adx_period else None,
                "plus_di": value(run.plus_di, local), "minus_di": value(run.minus_di, local)}

    def wilder_atr(self, period: int) -> list:
        """Wilder ATR(period) through each candle (None if invalid or unready), reset at every gap."""
        out = [None] * len(self._runs)
        seen = set()
        for run in self._runs:
            if run is None or run.first in seen:
                continue
            seen.add(run.first)
            for local, atr in enumerate(_wema(_true_ranges(run.candles), period), start=1):
                out[run.first + local] = atr if _finite(atr) else None
        return out


def candle_features(candles: CandleSeries, index: int, *, atr_period: int,
                    adx_period: int, ema_fast_period: int, ema_slow_period: int,
                    lookback: int) -> CandleFeatures:
    """Single completed-candle reading, preparing no candle later than index."""
    return CandleFeatureHistory(
        candles, atr_period=atr_period, adx_period=adx_period,
        ema_fast_period=ema_fast_period, ema_slow_period=ema_slow_period,
        lookback=lookback, end_index=index,
    ).at(index)
