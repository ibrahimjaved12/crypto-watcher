"""One-shot public analysis. Decimal values are JSON strings, timestamps ms."""
import argparse
import json
import time

from .core import WINDOWS, analyze, positive
from .providers import PROVIDERS, SUPPORTED_SYMBOLS, load


def run(symbol, threshold, window, loader=load, now=lambda: time.time_ns() // 1_000_000):
    attempts = []
    for provider in PROVIDERS:
        try:
            series = loader(provider, symbol)
            as_of = now()
            windows = analyze(series, as_of, threshold)
            if any(value["status"] != "ok" for value in windows.values()):
                attempts.append({"source": provider, "error": "incomplete_or_stale_analysis",
                                 "windows": windows})
                continue
            return {"schema_version": 1, "mode": "analysis_only", "ok": True,
                    "symbol": symbol, "quote_asset": "USDT", "source": provider,
                    "as_of_ms": as_of, "candle_policy": "completed_contiguous",
                    "threshold_pct": str(threshold), "selected_window_minutes": window,
                    "threshold_met": windows[str(window)]["threshold_met"],
                    "cooldown_checked": False, "windows": windows, "attempts": attempts}
        except (ValueError, TypeError, KeyError, IndexError, OSError) as exc:
            attempts.append({"source": provider, "error": str(exc)})
    return {"schema_version": 1, "mode": "analysis_only", "ok": False,
            "symbol": symbol, "as_of_ms": now(), "attempts": attempts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", type=str.upper, choices=sorted(SUPPORTED_SYMBOLS), default="BTCUSDT")
    parser.add_argument("--threshold", default="2")
    parser.add_argument("--window", type=int, choices=WINDOWS, default=15)
    args = parser.parse_args()
    try:
        threshold = positive(args.threshold)
    except ValueError as exc:
        parser.error(str(exc))
    result = run(args.symbol, threshold, args.window)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
