"""Run one tier of the Python test suite: ``python tests/run_shard.py <core|study> [--changed-file FILE] [--jobs N]``.

``tests/shards.json`` lists the ``study`` tier (the #123/EXP-75 research cluster) by module name. The
implicit ``core`` tier is every other discovered module, so a new test file always runs somewhere.
A listed name that matches no module is an error.

``--changed-file`` (study only) restricts the tier to the modules whose imports reach a changed path
(``select_tests.py``); no file means every module of the tier. Each module runs as
``python -m unittest <module>`` in its own subprocess (same discovery names as
``python -m unittest discover -s tests``), longest first, ``--jobs`` at a time, and its output is
printed when it completes. A per-module seconds table and a ``run_shard:`` summary line close the run.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

TESTS = Path(__file__).resolve().parent
PYTHON_DIR = TESTS.parent
CORE, STUDY = "core", "study"
# Seconds measured on a 2-core sandbox; only the order matters (longest first). Unknown modules weigh 1.
WEIGHTS = {
    "test_historical_market_state_study_part_c": 53,
    "test_historical_market_state_study": 36,
    "test_market_state_hmm_regimes_experiment": 18,
    "test_historical_market_state_study_evaluation": 13,
    "test_forward_trend_track": 9.6,
    "test_benchmark_bars": 7.1,
    "test_benchmark_runner": 7.1,
    "test_benchmark_trend": 3.2,
    "test_forward_engine": 3.2,
    "test_data_lake": 2.9,
    "test_benchmark_spa": 2.6,
    "test_benchmark_volatility": 2.3,
    "test_benchmark_stepm": 1.9,
}


def load_shards(path: Path = TESTS / "shards.json") -> dict[str, list[str]]:
    shards = json.loads(path.read_text(encoding="utf-8"))
    if type(shards) is not dict or CORE in shards:
        raise SystemExit(f"{path.name}: expected an object of tier lists without an explicit {CORE!r} tier")
    for name, modules in shards.items():
        if type(modules) is not list or not all(type(module) is str for module in modules):
            raise SystemExit(f"{path.name}: tier {name!r} must be a list of module names")
    return shards


def all_modules(directory: Path = TESTS) -> set[str]:
    return {path.stem for path in directory.glob("test*.py")}


def modules_for(tier: str, shards: dict[str, list[str]], modules: set[str]) -> set[str]:
    listed = [module for names in shards.values() for module in names]
    unknown = sorted(set(listed) - modules)
    if unknown:
        raise SystemExit(f"shards.json names no such test module: {', '.join(unknown)}")
    duplicated = sorted({module for module in listed if listed.count(module) > 1})
    if duplicated:
        raise SystemExit(f"shards.json lists modules more than once: {', '.join(duplicated)}")
    if tier == CORE:
        return modules - set(listed)
    if tier not in shards:
        raise SystemExit(f"unknown tier {tier!r}; expected one of {sorted([*shards, CORE])}")
    return set(shards[tier])


def run_module(module: str) -> tuple[str, int, float, int, str]:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(TESTS), str(PYTHON_DIR)])}
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-m", "unittest", module], cwd=PYTHON_DIR, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    ran = re.search(r"^Ran (\d+) tests? in", result.stdout, re.MULTILINE)
    return module, result.returncode, time.monotonic() - started, int(ran.group(1)) if ran else 0, result.stdout


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tier")
    parser.add_argument("--changed-file", help="file with one changed repo-relative path per line (study tier)")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv[1:])
    shards = load_shards()
    selected = sorted(modules_for(args.tier, shards, all_modules()))
    if args.changed_file and args.tier != CORE:
        from select_tests import select  # sibling module: tests/ is sys.path[0] when run as a script

        changed = Path(args.changed_file).read_text(encoding="utf-8").splitlines()
        chosen = set(select(changed, selected, PYTHON_DIR))
        selected = [module for module in selected if module in chosen]
        if not selected:
            print("run_shard: skipped (no study code touched)")
            return 0
    if not selected:
        raise SystemExit(f"tier {args.tier!r} selected no tests")
    selected.sort(key=lambda module: (-WEIGHTS.get(module, 1), module))
    print(f"run_shard: {args.tier}: {len(selected)} module(s), {args.jobs} job(s)", flush=True)
    started = time.monotonic()
    rows: list[tuple[str, int, float, int]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for module, code, seconds, tests, output in pool.map(run_module, selected):
            print(f"\n=== {module} ({tests} tests, {seconds:.1f}s) {'ok' if code == 0 else 'FAILED'} ===", flush=True)
            if code != 0:
                print(output, flush=True)
            rows.append((module, code, seconds, tests))
    print("\nseconds  tests  module")
    for module, code, seconds, tests in sorted(rows, key=lambda row: -row[2]):
        print(f"{seconds:7.1f}  {tests:5d}  {module}{'  FAILED' if code else ''}")
    failed = [module for module, code, _, _ in rows if code]
    total = sum(tests for _, _, _, tests in rows)
    print(f"run_shard: {args.tier} modules={len(rows)} tests={total} failed={len(failed)} "
          f"seconds={time.monotonic() - started:.0f}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
