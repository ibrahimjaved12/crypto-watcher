"""Offline chronological TA replay; it never exposes future candles to the calculator."""
import argparse
from dataclasses import replace
import json
from pathlib import Path

from .technical import (
    FuturesInstrument,
    TechnicalCandle,
    TechnicalConfig,
    TechnicalInput,
    calculate_technical_analysis,
)


def replay_technical_analysis(request, evaluation_times_ms=None):
    duration = request.timeframe_minutes * 60_000
    all_candles = sorted(
        request.warmup_candles + request.candles,
        key=lambda candle: candle.open_ms,
    )
    points = evaluation_times_ms or tuple(
        candle.open_ms + duration for candle in all_candles if candle.complete
    )
    results = []
    for evaluation_time_ms in points:
        visible = tuple(
            candle for candle in all_candles
            if candle.complete and candle.open_ms + duration <= evaluation_time_ms
        )
        step = replace(
            request,
            candles=visible,
            warmup_candles=(),
            source_event_time_ms=evaluation_time_ms,
            evaluation_time_ms=evaluation_time_ms,
            detection_time_ms=evaluation_time_ms,
        )
        results.append(calculate_technical_analysis(step))
    return results


def input_from_dict(value):
    instrument = FuturesInstrument(**value["instrument"])
    candles = tuple(TechnicalCandle(**candle) for candle in value["candles"])
    warmup = tuple(TechnicalCandle(**candle) for candle in value.get("warmup_candles", []))
    config = TechnicalConfig(**value.get("config", {}))
    return TechnicalInput(
        instrument=instrument,
        timeframe_minutes=value["timeframe_minutes"],
        candles=candles,
        warmup_candles=warmup,
        missing_open_times_ms=tuple(value.get("missing_open_times_ms", [])),
        source=value["source"],
        source_event_time_ms=value["source_event_time_ms"],
        evaluation_time_ms=value["evaluation_time_ms"],
        detection_time_ms=value["detection_time_ms"],
        price_type=value.get("price_type", "trade"),
        config=config,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path, help="versioned JSON TechnicalInput fixture")
    args = parser.parse_args()
    request = input_from_dict(json.loads(args.fixture.read_text()))
    print(json.dumps(replay_technical_analysis(request), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
