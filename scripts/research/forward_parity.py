#!/usr/bin/env python3
"""Forward vs label-engine parity on one real development month (workflow forward-parity.yml). Read-only.

Downloads the private rd bars and funding (FIRST_MONTH .. the month AFTER the audited one: its first 960
minutes are the longest time limit, 4 x 240 min, of the month's last decisions), builds the reference with
the label engine in-process on the FULL history (``labels.row_factory``: the same sigma, tick and row
function as ``build_labels``; the expanding hcal calibration is the point), runs ONE ``forward.evaluate``
call exactly as the live request is shaped (bars starting 120 days before the month, from_ms = month
start), joins every forward setup to its label row and writes the full report to reports/parity/ in the
private research-data repository. Nothing is published to the public repo; the public log carries counts
and PASS/FAIL per symbol only. Hidden-stretch months are refused by the loader (the hidden guard) before
any file is opened.

One call is equivalent to hourly runs because ``evaluate`` is stateless over bars: the decision window
(from_ms, to_ms], the open setups and the wallet are inputs, nothing is read from a store. With
``--slice-check`` a 3-day slice is also evaluated as 72 hourly calls chained through processed_to_ms and
compared with one call (signals, setups and resolutions).
"""
from __future__ import annotations

import argparse
import json
from math import isfinite
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import experiment_run as xr  # noqa: E402  (scripts/research: Checkout, Progress, downloads)
from data_lake_build import ResearchDataRepo  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402
from market_analysis.benchmark import labels as lab  # noqa: E402
from market_analysis.benchmark import experiment_run as er  # noqa: E402
from market_analysis.benchmark.bars import MISSING, infer_tick  # noqa: E402
from market_analysis.benchmark.label_cli import label_asset_name  # noqa: E402
from market_analysis.benchmark.canonical import canonical_bytes, content_hash  # noqa: E402
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked  # noqa: E402
from market_analysis.benchmark.market_data import load_symbol_bars, load_symbol_funding  # noqa: E402
from market_analysis.forward import parity  # noqa: E402
from label_build import label_tag  # noqa: E402
from market_analysis.forward.bars_adapter import bars_from_collector_rows  # noqa: E402
from market_analysis.forward.evaluate import _funding, evaluate  # noqa: E402
from market_analysis.forward.setups import FORWARD_PARAMS  # noqa: E402
from market_analysis.forward.signals import FORWARD_STRATEGIES  # noqa: E402

NOW_UTC = xr.NOW_UTC
PublicError, GitError, Checkout, emit, _quiet = xr.PublicError, xr.GitError, xr.Checkout, xr.emit, xr._quiet
PHASES = frozenset({"checkout", "bars download", "reference", "forward", "compare", "slice check", "report write", "push"})
COUNT_KEYS = xr.COUNT_KEYS
MINUTE = 60_000
HOUR = 3_600_000
DAY = 86_400_000
REQUEST_DAYS = 120                      # the live request window (FORWARD_HISTORY_DAYS)
TAIL_MINUTES = 4 * 240                  # the longest time limit
SLICE_HOURS = 72


class ParityProgress(xr.Progress):
    @staticmethod
    def _check(name: str, counts: dict) -> None:
        if name not in PHASES:
            raise ValueError(f"unknown progress phase {name!r}")
        for key, value in counts.items():
            if key not in COUNT_KEYS or type(value) is not int:
                raise ValueError(f"progress counts are allowlisted integers only, not {key!r}")

    def finish(self) -> list[str]:
        lines = super().finish()
        lines[0] = "### Forward parity timings"
        return lines


def next_month(month: str) -> str:
    year, number = int(month[:4]), int(month[5:7])
    return f"{year + (number == 12):04d}-{number % 12 + 1:02d}"


def ta_strategy_ids() -> list[str]:
    return sorted(key for key, strategy in FORWARD_STRATEGIES.items() if strategy.kind == "ta")


def request(symbol, bars, funding, first_ms, end_ms, from_ms, to_ms) -> dict:
    return {"symbols": [{"symbol": symbol, "rows": parity.rows_from_bars(bars, first_ms, end_ms),
                         "funding": parity.funding_events(funding), "funding_available": True}],
            "strategy_ids": ta_strategy_ids(), "from_ms": from_ms, "to_ms": to_ms}


def run_forward(call: dict) -> dict:
    return evaluate(call["symbols"], strategy_ids=call["strategy_ids"], from_ms=call["from_ms"], to_ms=call["to_ms"])


def report_record(value):
    """Render float statistics as decimal strings (non-finite -> None) for canonical hashing.

    Verdicts are computed on the original numbers; only the persisted report is rendered, as in
    the trend/regime reports. Keep the strict canonical serializer's no-floats contract intact.
    """
    if type(value) is float:
        return repr(value) if isfinite(value) else None
    if isinstance(value, dict):
        return {key: report_record(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [report_record(item) for item in value]
    return value


def slice_check(symbol, bars, funding, month_start_ms: int) -> dict:
    """One call over a 3-day slice versus 72 hourly calls chained through processed_to_ms."""
    start = month_start_ms + 10 * DAY
    end = start + SLICE_HOURS * HOUR
    first, tail_end = start - REQUEST_DAYS * DAY, end + (TAIL_MINUTES + 1) * MINUTE
    single = run_forward(request(symbol, bars, funding, first, tail_end, start, end))
    ids = {"signals": set(), "setups": set()}
    finals = {}
    previous = start
    for hour in range(1, SLICE_HOURS + 1):
        to_ms = start + hour * HOUR
        chained = run_forward(request(symbol, bars, funding, first, tail_end, previous, to_ms))
        previous = chained["processed_to_ms"][symbol]
        ids["signals"].update(item["signal_id"] for item in chained["signals"])
        ids["setups"].update(item["setup_id"] for item in chained["setups"])
        finals.update({item["setup_id"]: (item["status"], item.get("exit_ms"), item.get("net_ur"))
                       for item in chained["resolutions"]})
    one = {item["setup_id"]: (item["status"], item.get("exit_ms"), item.get("net_ur")) for item in single["resolutions"]}
    same = (ids["signals"] == {item["signal_id"] for item in single["signals"]}
            and ids["setups"] == {item["setup_id"] for item in single["setups"]} and finals == one)
    return {"hours": SLICE_HOURS, "signals": len(ids["signals"]), "setups": len(ids["setups"]), "identical": bool(same)}


# The published lb3h releases are built with the default k / rr grids, so their params identity cannot equal
# FORWARD_PARAMS' (k = 2, rr in {3/2, 2}). "The revision with FORWARD_PARAMS' identity" is therefore the one
# whose SIGMA-relevant fields agree: sigma model, horizons, half-lives, steps, time limit, min stop, costs.
SIGMA_FIELDS = ("sigma_model", "horizons", "half_life_days", "step_minutes", "time_limit_multiple",
                "min_stop_ticks", "cost_model_identity", "robust")


def published_cross_check(repo, symbol: str, month: str, workdir: Path, reference, setups: list, revision: str) -> dict:
    """Informational: the same rows from a published lb3h release (sigma, d_ticks, status), if one matches."""
    wanted = {key: FORWARD_PARAMS.to_record()[key] for key in SIGMA_FIELDS}
    for number in (range(1, 21) if revision == "auto" else [int(revision)]):
        tag = label_tag(symbol, er.LABEL_FIRST_MONTH, er.LABEL_LAST_MONTH, number, "ewma-robust-hcal")
        release = repo.published_release(tag)
        if release is None or release.get("draft") is not False:
            continue
        assets = repo.assets(release["id"])
        manifest_name = er.label_manifest_name(symbol)
        month_file = label_asset_name(symbol, month)
        if manifest_name not in assets or month_file not in assets:
            continue
        directory = workdir / f"published-{number}"
        directory.mkdir(parents=True)
        xr.download(repo, assets[manifest_name], directory / manifest_name, limit=16 << 20)
        manifest = json.loads((directory / manifest_name).read_bytes())
        if any(manifest["params"].get(key) != value for key, value in wanted.items()):
            continue
        item = {entry["month"]: entry for entry in manifest["outputs"]}.get(month)
        if item is None:
            continue
        xr.download(repo, assets[month_file], directory / month_file, item["sha256"])
        published_params = lab.LabelParams(sigma_model="ewma-robust-hcal")
        with (directory / month_file).open("rb") as stream:
            published = {(row.signal_ms, row.horizon_min, row.side, row.k): row for row in lab.read_label_csv(stream, published_params)}
        compared = same_sigma = same_d = same_status = 0
        for key in {(s["signal_ms"], s["horizon_min"], s["side"], s["k"]) for s in setups}:
            row = published.get((key[0], key[1], key[2], lab.Fraction(key[3])))
            if row is None:
                continue
            ours = reference(key[0], key[1], key[2], lab.Fraction(key[3]))
            compared += 1
            same_status += ours.status == row.status
            same_sigma += ours.sigma == row.sigma
            same_d += ours.d_ticks == row.d_ticks
        return {"revision": number, "tag": tag, "compared": compared, "same_status": same_status,
                "same_sigma": same_sigma, "same_d_ticks": same_d}
    return {"revision": None, "note": "no published lb3h release matches the sigma-relevant FORWARD_PARAMS fields"}


def run(args, checkout: Checkout, repo: ResearchDataRepo, workdir: Path, progress) -> list[str]:
    symbol, month = args.symbol, args.month
    if symbol not in lake.SYMBOLS:
        raise PublicError(f"symbol must be one of {','.join(lake.SYMBOLS)}")
    months = lake.months_between(lake.FIRST_MONTH, next_month(month))
    month_start, month_end, _ = lake.month_bounds_ms(month)
    bars_dir = workdir / "bars"
    bars_dir.mkdir(parents=True)
    with progress.stage("bars download", total=len(months)) as set_month:
        _quiet(xr._download_symbol_bars, repo, {"data_revision": args.data_revision}, bars_dir, None, symbol, months)
        set_month(len(months))
    bars = _quiet(load_symbol_bars, bars_dir, symbol, lake.FIRST_MONTH, months[-1])
    funding = _quiet(load_symbol_funding, bars_dir, symbol, lake.FIRST_MONTH, months[-1])
    ticks = {m: infer_tick(_quiet(load_symbol_bars, bars_dir, symbol, m, m)) for m in months}  # per month, as label_cli
    progress.phase("reference", months=len(months))
    reference = _quiet(lab.row_factory, symbol, bars, funding, FORWARD_PARAMS, ticks)
    progress.phase("forward")
    first = month_start - REQUEST_DAYS * DAY
    end = month_end + (TAIL_MINUTES + 1) * MINUTE
    call = request(symbol, bars, funding, first, end, month_start, month_end)
    result = _quiet(run_forward, call)
    request_item = call["symbols"][0]
    request_bars = _quiet(bars_from_collector_rows, symbol, request_item["rows"])
    request_funding = _quiet(_funding, request_bars, request_item.get("funding"))
    progress.phase("compare", rows=len(result["setups"]))
    robust = reference.robust
    horizon_half = {h: FORWARD_PARAMS.half_life(h) for h in FORWARD_PARAMS.horizons}
    stats = _quiet(parity.compare, result["setups"], result["resolutions"], reference,
                   lambda h, ms: None if robust.level_at(horizon_half[h], ms) == MISSING else robust.level_at(horizon_half[h], ms),
                   lambda h, ms: robust.multiplier(h, horizon_half[h], FORWARD_PARAMS.step(h), ms),
                   reference.tick_at, FORWARD_PARAMS, bars=request_bars, funding=request_funding)
    report = {"schema": "forward-parity-v1", "symbol": symbol, "month": month, "reference_through": months[-1],
              "request_days": REQUEST_DAYS, "forward_params_identity": FORWARD_PARAMS.identity(),
              "pass_rule": parity.PASS_RULE, "signals": len(result["signals"]), "setups": len(result["setups"]),
              "stats": stats, "data_revision": args.data_revision,
              "code_commit": os.environ.get("GITHUB_SHA", "local"), "created_utc": NOW_UTC}
    report["published_cross_check"] = _quiet(published_cross_check, repo, symbol, month, workdir, reference,
                                             result["setups"], args.label_revision)
    if args.slice_check:
        progress.phase("slice check")
        report["slice_check"] = _quiet(slice_check, symbol, bars, funding, month_start)
    report = report_record(report)
    report["report_hash"] = content_hash(report)
    progress.phase("report write")
    path = f"reports/parity/{month}__{symbol}__{report['report_hash'][:16]}.json"
    (checkout.path / path).parent.mkdir(parents=True, exist_ok=True)
    (checkout.path / path).write_bytes(canonical_bytes(report))
    progress.phase("push", files=1)
    checkout.commit_and_push([path], f"Forward parity {symbol} {month}: report {report['report_hash']}")
    lines = [parity.public_line(symbol, month, stats)]
    if "slice_check" in report:
        lines.append(f"{symbol} slice check one call vs {SLICE_HOURS} hourly calls: "
                     f"{'PASS' if report['slice_check']['identical'] else 'FAIL'}")
    lines.append(f"report hash {report['report_hash']}")
    ok = stats["verdict"]["pass"] and report.get("slice_check", {"identical": True})["identical"]
    args.failed = not ok
    return lines


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=Path("forward-parity-work"))
    parser.add_argument("--summary", type=Path, help="append the public output (markdown)")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--month", default="2025-05", help="a DEVELOPMENT month YYYY-MM (hidden months are refused)")
    parser.add_argument("--data-revision", type=int, default=1)
    parser.add_argument("--label-revision", default="auto",
                        help="lb3h revision for the informational published-label cross-check; auto = the one whose "
                             "sigma-relevant params equal FORWARD_PARAMS' (none is not an error)")
    parser.add_argument("--slice-check", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    args.failed = False
    try:  # the hidden guard, before any network or file access (also covers the month after, read for the tail)
        from market_analysis.benchmark.hidden_guard import require_months
        require_months(lake.months_between(lake.FIRST_MONTH, next_month(args.month)), None, None)
    except HiddenStretchLocked:
        raise PublicError("a hidden-stretch month is locked: the parity check runs on development months "
                          "(the month after the audited one is read too)") from None
    except ValueError:
        raise PublicError("month must be YYYY-MM inside the data lake") from None
    args.workdir.mkdir(parents=True, exist_ok=False)
    progress = ParityProgress(sys.stdout)
    try:
        repository, token = os.environ.get("RESEARCH_DATA_REPOSITORY", ""), os.environ.get("RESEARCH_DATA_TOKEN", "")
        try:
            repo = ResearchDataRepo(repository, token)
        except ValueError as error:
            raise PublicError(str(error)) from None
        progress.phase("checkout")
        checkout = Checkout(repository, token, args.workdir / "research-data")
        emit(run(args, checkout, repo, args.workdir / "data", progress), args.summary)
    finally:
        shutil.rmtree(args.workdir, ignore_errors=True)
        timings = progress.finish()
        if args.summary:
            with args.summary.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(timings))
    return 1 if args.failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (PublicError, GitError) as error:
        print(f"::error title=forward parity failed::{error}", flush=True)
        sys.exit(1)
    except Exception as error:  # library errors can contain private values: type name only
        print(f"::error title=forward parity failed::{type(error).__name__}", flush=True)
        sys.exit(1)
