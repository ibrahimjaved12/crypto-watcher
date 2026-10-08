"""Build the #182 label table for one symbol: ``python -m market_analysis.benchmark.label_cli``.

Reads the data-lake ``bars1m`` and ``funding`` files of every month from
``--first-month`` to ``--last-month`` in ``--bars-dir`` (all must exist), builds
one bar/funding series over the whole window (volatility needs the history),
and writes ``labels__SYMBOL__YYYY-MM.csv.gz`` per month plus
``labels__SYMBOL.manifest.json``. Logs and the optional markdown summary carry
aggregate counts only (the repository is public): never tick values. The
private manifest records per month the inferred tick, the number of off-tick
prints, and the tick rule (``tick-v2``).
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from pathlib import Path
import sys

from .. import data_lake
from .bars import TICK_RULE, BarSeries, infer_tick, off_tick_count, read_bars_csv
from .funding import FundingSeries, read_funding_csv
from .labels import SCHEMA, LabelParams, build_labels, write_label_csv

_BLOCK = 1 << 20


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def label_asset_name(symbol: str, month: str) -> str:
    return f"labels__{symbol}__{month}.csv.gz"


def manifest_name(symbol: str) -> str:
    return f"labels__{symbol}.manifest.json"


def tick_summary(manifest: dict) -> dict:
    """Public aggregates: months with any off-tick print and months whose tick differs from the previous month."""
    ticks = [manifest["ticks"][month] for month in sorted(manifest["ticks"])]
    off = manifest.get("off_tick_prints", {})
    return {"months_with_off_tick_prints": sum(1 for count in off.values() if count),
            "tick_changes": sum(1 for previous, current in zip(ticks, ticks[1:]) if current != previous)}


def run(symbol: str, bars_dir: Path, first_month: str, last_month: str, out_dir: Path,
        params: LabelParams | None = None) -> dict:
    params = params or LabelParams()
    data_lake.validate_symbol(symbol)
    months = data_lake.months_between(first_month, last_month)
    paths = {month: (bars_dir / data_lake.bars_asset_name(symbol, month),
                     bars_dir / data_lake.funding_asset_name(symbol, month)) for month in months}
    missing = [str(path) for pair in paths.values() for path in pair if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing input files: {missing}")
    bar_parts, funding_parts, ticks, off_ticks, inputs = [], [], {}, {}, {}
    for month in months:
        bars_path, funding_path = paths[month]
        with bars_path.open("rb") as stream:
            part = read_bars_csv(stream, symbol, month)
        ticks[month] = infer_tick(part)  # point-in-time tick of this month, before concatenation
        off_ticks[month] = off_tick_count(part, ticks[month])
        bar_parts.append(part)
        with funding_path.open("rb") as stream:
            funding_parts.append(read_funding_csv(stream, month))
        inputs[month] = {"bars": {"name": bars_path.name, "sha256": file_sha256(bars_path)},
                         "funding": {"name": funding_path.name, "sha256": file_sha256(funding_path)}}
        print(f"loaded {symbol} {month}: {part.minutes} minutes", flush=True)
    bars = BarSeries.concat(bar_parts)
    funding = FundingSeries.concat(funding_parts)
    del bar_parts, funding_parts

    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for month, rows in build_labels(symbol, bars, funding, params, ticks):
        path = out_dir / label_asset_name(symbol, month)
        with path.open("wb") as stream:
            count = write_label_csv(stream, rows, params)
        statuses = Counter(row.status for row in rows)
        outcomes = [Counter() for _ in params.rr_grid]
        ambiguous = [0] * len(params.rr_grid)
        for row in rows:
            for index, pair in enumerate(row.cells):
                outcomes[index][pair.pess.outcome] += 1
                ambiguous[index] += pair.opt is not None
        outputs.append({
            "name": path.name, "month": month, "rows": count, "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
            "status_counts": {status: statuses[status] for status in sorted(statuses)},
            "outcome_counts_pessimistic": [{outcome: counter[outcome] for outcome in sorted(counter)}
                                           for counter in outcomes],
            "ambiguous_counts": ambiguous,
        })
        print(f"wrote {path.name}: {count} rows, statuses {dict(sorted(statuses.items()))}", flush=True)
    manifest = {
        "schema": SCHEMA,
        "symbol": symbol,
        "first_month": first_month,
        "last_month": last_month,
        "params": params.to_record(),
        "params_identity": params.identity(),
        "cost_model_identity": params.cost_model.identity(),
        "inputs": inputs,
        "ticks": ticks,
        "tick_rule": TICK_RULE,
        "off_tick_prints": off_ticks,
        "outputs": outputs,
    }
    summary = tick_summary(manifest)
    print(f"tick check: {summary['months_with_off_tick_prints']} months with off-tick prints, "
          f"{summary['tick_changes']} month-to-month tick changes", flush=True)
    (out_dir / manifest_name(symbol)).write_text(data_lake.canonical_json(manifest), encoding="ascii")
    return manifest


def summary_lines(manifest: dict) -> list[str]:
    lines = [f"### Labels {manifest['symbol']} {manifest['first_month']}..{manifest['last_month']}", "",
             f"params identity `{manifest['params_identity']}`", "",
             "| file | rows | bytes | sha256 | statuses |", "| --- | ---: | ---: | --- | --- |"]
    for output in manifest["outputs"]:
        statuses = ", ".join(f"{status} {count}" for status, count in output["status_counts"].items())
        lines.append(f"| `{output['name']}` | {output['rows']} | {output['bytes']} | `{output['sha256']}` | {statuses} |")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the #182 stop-aware label table for one symbol")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--first-month", required=True)
    parser.add_argument("--last-month", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--summary", type=Path, help="append a markdown summary (aggregate counts only)")
    args = parser.parse_args(argv)
    try:
        manifest = run(args.symbol, args.bars_dir, args.first_month, args.last_month, args.out_dir)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(summary_lines(manifest)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
