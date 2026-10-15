#!/usr/bin/env python3
"""Defect statistics of a doji pilot's annotations (docs/pattern-study.md, "Doji pilot").

    python scripts/research/pattern_pilot_defects.py --annotations annotations.csv --key pilot_key.csv
    python scripts/research/pattern_pilot_defects.py --key pilot_key.csv \\
        --annotations a.csv --rater alice --annotations b.csv --rater bob

Any annotations path is accepted; --rater labels the file at the same position (default "reviewer" for a single
file). Several raters are reported one after another in one run, each tabulated from its own file only.

Joins the filled annotations.csv (detector_ok = the reviewer's visual call: y "I would call it a doji", n "not a
doji", unsure; defect_type, visual_strength, notes) to pilot_key.csv and prints, per class, class x timeframe and
stratum, with n and Wilson 95% intervals: for events the false-positive rate n / (y + n), for near-misses and
controls the missed-doji rate y / (y + n); 'unsure' counted apart. Also the defect types of the events judged n
and how the reviewer rates the visual strength of events by the detector's strength bucket. It only reports: it
sets no pass threshold and writes nothing. Run it on the private files, locally or in a private
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
    parser.add_argument("--annotations", type=Path, action="append", required=True,
                        help="annotations CSV of one rater; repeat for several raters")
    parser.add_argument("--rater", action="append", default=None,
                        help="rater label of the --annotations file at the same position (default: reviewer, "
                             "only valid with a single file)")
    parser.add_argument("--key", type=Path, required=True)
    args = parser.parse_args(argv)
    raters = args.rater if args.rater is not None else ["reviewer"]
    if len(raters) != len(args.annotations):
        raise pp.PilotError(f"{len(args.annotations)} --annotations files need as many --rater labels "
                            f"(got {len(raters)})")
    if len(set(raters)) != len(raters):
        raise pp.PilotError(f"duplicate --rater labels: {raters}")
    key = pp.read_key(args.key.read_text(encoding="utf-8"))
    # each file is parsed and tabulated on its own, so raters never share rows
    for rater, path in zip(raters, args.annotations):
        rows = pp.read_annotations(path.read_text(encoding="utf-8"), key)
        print(f"## Rater: {rater} ({path.name})\n")
        print("\n".join(pp.defect_markdown(pp.defect_tables(key, rows))))
        print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (pp.PilotError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
