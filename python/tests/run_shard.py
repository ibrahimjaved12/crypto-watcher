"""Run one CI shard of the Python test suite: ``python tests/run_shard.py <shard>``.

``tests/shards.json`` maps shard names to test module names. The implicit shard
``rest`` runs every discovered module that no other shard lists, so a new test
file always runs somewhere. A listed name that matches no module is an error.
Discovery is the same as ``python -m unittest discover -s tests``.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

TESTS = Path(__file__).resolve().parent
REST = "rest"


def load_shards(path: Path = TESTS / "shards.json") -> dict[str, list[str]]:
    shards = json.loads(path.read_text(encoding="utf-8"))
    if type(shards) is not dict or REST in shards:
        raise SystemExit(f"{path.name}: expected an object of shard lists without an explicit {REST!r} shard")
    for name, modules in shards.items():
        if type(modules) is not list or not all(type(module) is str for module in modules):
            raise SystemExit(f"{path.name}: shard {name!r} must be a list of module names")
    return shards


def all_modules(directory: Path = TESTS) -> set[str]:
    return {path.stem for path in directory.glob("test*.py")}


def modules_for(shard: str, shards: dict[str, list[str]], modules: set[str]) -> set[str]:
    listed = [module for names in shards.values() for module in names]
    unknown = sorted(set(listed) - modules)
    if unknown:
        raise SystemExit(f"shards.json names no such test module: {', '.join(unknown)}")
    duplicated = sorted({module for module in listed if listed.count(module) > 1})
    if duplicated:
        raise SystemExit(f"shards.json lists modules more than once: {', '.join(duplicated)}")
    if shard == REST:
        return modules - set(listed)
    if shard not in shards:
        raise SystemExit(f"unknown shard {shard!r}; expected one of {sorted([*shards, REST])}")
    return set(shards[shard])


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _module_of(test) -> str:
    # A module that fails to import becomes a _FailedTest named after the module: keep it in its shard.
    if type(test).__name__ == "_FailedTest":
        return test._testMethodName
    return type(test).__module__


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: python tests/run_shard.py <shard>")
    selected = modules_for(argv[1], load_shards(), all_modules())
    sys.path.insert(0, str(TESTS.parent))  # market_analysis, as with ``python -m unittest`` from python/
    suite = unittest.TestSuite(test for test in _flatten(unittest.defaultTestLoader.discover(str(TESTS)))
                               if _module_of(test) in selected)
    if not suite.countTestCases():
        raise SystemExit(f"shard {argv[1]!r} selected no tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
