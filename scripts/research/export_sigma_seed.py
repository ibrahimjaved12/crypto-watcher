#!/usr/bin/env python3
"""Manual private lake seed export: guarded history, durable openings, counts/checksums only."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import experiment_run as xr
from data_lake_build import ResearchDataRepo
from market_analysis import data_lake as lake
from market_analysis.benchmark.canonical import canonical_bytes, content_hash
from market_analysis.benchmark.hidden_guard import HiddenGate, HiddenStretchLocked, read_plan, require_months
from market_analysis.benchmark.market_data import load_symbol_bars
from market_analysis.forward.sigma_state import SIGMA_VERSION, seed_state


class ExportError(ValueError):
    pass


def selection(symbols: str, cutoff: str):
    selected = [symbol.strip() for symbol in symbols.split(",")]
    if not selected or len(set(selected)) != len(selected) or any(symbol not in lake.SYMBOLS for symbol in selected):
        raise ExportError("invalid symbols")
    if not re.fullmatch(r"\d{4}-\d{2}-01T00:00:00Z", cutoff):
        raise ExportError("cutoff must be a UTC month boundary")
    end = datetime.strptime(cutoff, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    last = (end - timedelta(days=1)).strftime("%Y-%m")
    if last < lake.FIRST_MONTH or cutoff > xr.NOW_UTC[:7] + "-01T00:00:00Z":
        raise ExportError("cutoff outside published lake range")
    return sorted(selected), lake.months_between(lake.FIRST_MONTH, last), int(end.timestamp()) * 1000


def authorize(checkout, months, symbols, cutoff, plan_id, *, now_utc):
    """Persist the opening before any hidden release is downloaded or any bar is read.

    Plan files must already be committed in the private clone; this exporter neither
    creates plans nor forces a second opening for an already-opened question.
    """
    try:
        require_months(months, None, None)
    except HiddenStretchLocked:
        pass
    else:
        return None, None
    if not re.fullmatch(r"[0-9a-f]{64}", plan_id):
        raise ExportError("hidden history needs a registered plan")
    matches = list((checkout.path / "plans").glob(f"*__{plan_id[:16]}.plan.json"))
    if len(matches) != 1:
        raise ExportError("no unique registered plan")
    relative = str(matches[0].relative_to(checkout.path))
    checkout._git("verify registered plan", ["ls-files", "--error-unmatch", "--", relative])
    plan = read_plan(matches[0])
    intended = {"strategy_id": "forward-sigma-seed", "strategy_version": SIGMA_VERSION,
                "config": {"symbols": symbols, "cutoff": cutoff,
                           "first_month": lake.FIRST_MONTH, "data_revision": 1}}
    if plan.plan_id != plan_id or plan.variants != 1 or plan.strategies != [intended]:
        raise ExportError("plan does not authorize this seed export")
    gate = HiddenGate(checkout.path / "plans", checkout.path / xr.OPENS)
    token = gate.open_hidden(plan.question_id, plan_id, now_utc=now_utc)
    checkout.commit_and_push([xr.OPENS], f"Record sigma seed opening {plan_id}")
    require_months(months, token, gate)
    return token, gate


def export(args, checkout, repo):
    symbols, months, cutoff_ms = selection(args.symbols, args.cutoff)
    token, gate = authorize(checkout, months, symbols, args.cutoff, args.plan_id, now_utc=xr.NOW_UTC)
    paths, entries = [], []
    for symbol in symbols:
        directory = args.workdir / "bars" / symbol
        directory.mkdir(parents=True)
        try:
            # Same verified releases and guarded bar loader as forward_parity.py.
            require_months(months, token, gate)
            xr._download_symbol_bars(repo, {"data_revision": 1}, directory, None, symbol, months)
            bars = load_symbol_bars(directory, symbol, months[0], months[-1], token=token, gate=gate)
            state = seed_state(bars, cutoff_ms, source="lake_seed")
            record = state.to_record()
            raw = canonical_bytes(record)
            path = f"seeds/sigma/{symbol}/{args.cutoff[:10]}__{record['checksum']}.json"
            destination = checkout.path / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.read_bytes() != raw:
                    raise ExportError("seed artifact collision")
            else:
                destination.write_bytes(raw)
                paths.append(path)
            entries.append({"symbol": symbol, "path": path, "checksum": record["checksum"],
                            "payload_bytes": len(canonical_bytes(record["payload"])), "rows": bars.minutes})
            del bars, state, record, raw
        finally:
            shutil.rmtree(directory, ignore_errors=True)
    manifest = {"schema": "sigma-seed-export-v1", "sigma_version": SIGMA_VERSION,
                "source": "lake_seed", "cutoff": args.cutoff, "first_month": months[0],
                "last_month": months[-1], "data_revision": 1, "plan_id": args.plan_id if token else None,
                "opened_utc": token.opened_utc if token else None,
                "code_commit": os.environ.get("GITHUB_SHA", "local"), "seeds": entries}
    digest = content_hash(manifest)
    manifest_path = f"seeds/sigma/manifests/{digest}.json"
    destination = checkout.path / manifest_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_bytes(canonical_bytes({**manifest, "checksum": digest}))
        paths.append(manifest_path)
    if paths:
        checkout.commit_and_push(paths, f"Export {len(entries)} sigma seeds {digest}")
    return entries, digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--plan-id", default="")
    parser.add_argument("--workdir", type=Path, default=Path("sigma-seed-export-work"))
    args = parser.parse_args(argv)
    selection(args.symbols, args.cutoff)
    if args.plan_id and not re.fullmatch(r"[0-9a-f]{64}", args.plan_id):
        raise ExportError("invalid plan id")
    args.workdir.mkdir(parents=True, exist_ok=False)
    try:
        repository = os.environ.get("RESEARCH_DATA_REPOSITORY", "")
        token = os.environ.get("RESEARCH_DATA_TOKEN", "")
        repo = ResearchDataRepo(repository, token)
        checkout = xr._quiet(xr.Checkout, repository, token, args.workdir / "research-data")
        entries, digest = xr._quiet(export, args, checkout, repo)
        print(f"seeds={len(entries)} checksum={digest}", flush=True)
        for index, entry in enumerate(entries, 1):
            print(f"seed={index} rows={entry['rows']} payload_bytes={entry['payload_bytes']} checksum={entry['checksum']}", flush=True)
    finally:
        shutil.rmtree(args.workdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("::error::Sigma seed export failed; private details suppressed", flush=True)
        sys.exit(1)
