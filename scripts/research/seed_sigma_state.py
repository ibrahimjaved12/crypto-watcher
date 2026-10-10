#!/usr/bin/env python3
"""Export a checksummed sigma seed from already-downloaded, guarded lake months (no server)."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from market_analysis.benchmark.canonical import canonical_bytes
from market_analysis.benchmark.market_data import load_symbol_bars
from market_analysis.forward.sigma_state import seed_state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--first-month", required=True)
    parser.add_argument("--last-month", required=True)
    parser.add_argument("--cutoff-ms", type=int, required=True, help="exclusive UTC minute boundary")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Loader authorizes all months before opening files; hidden months remain locked.
    bars = load_symbol_bars(args.bars_dir, args.symbol, args.first_month, args.last_month)
    state = seed_state(bars, args.cutoff_ms)
    with args.output.open("xb") as stream:
        stream.write(canonical_bytes(state.to_record()))
    print(f"sigma seed {state.symbol}: as_of_ms={state.as_of_ms}")


if __name__ == "__main__":
    main()
