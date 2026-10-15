#!/usr/bin/env python3
"""Defect statistics of a doji pilot's annotations (docs/pattern-study.md, "Doji pilot").

    python scripts/research/pattern_pilot_defects.py --annotations annotations.csv --key pilot_key.csv

Joins the filled annotations.csv (detector_ok y|n|unsure, defect_type, visual_strength, notes) to pilot_key.csv
and prints the defect rate (n / (y + n); 'unsure' counted apart) by class, timeframe, class x timeframe and
stratum with n and Wilson 95% intervals, the defect types, and the defect rate split by visual_strength. It
only reports: it sets no pass threshold and writes nothing. Run it on the private files, locally or in a private
context; its output describes the detector's audit and is not meant for public logs.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from market_analysis.patterns import pattern_pilot as pp  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    args = parser.parse_args(argv)
    key = pp.read_key(args.key.read_text(encoding="utf-8"))
    rows = pp.read_annotations(args.annotations.read_text(encoding="utf-8"), key)
    print("\n".join(pp.defect_markdown(pp.defect_tables(key, rows))))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (pp.PilotError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
