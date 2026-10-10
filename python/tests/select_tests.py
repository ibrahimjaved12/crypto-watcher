"""Select the study-tier test modules a change can affect (pure stdlib, no test is executed).

Builds the module import graph of ``market_analysis`` (and of the helper modules that live in
``python/tests``) with ``ast``, takes for every study test module the transitive closure of what it
imports, and selects the modules whose closure (plus the test file itself) intersects the changed
paths. Anything this analysis cannot see (requirements, the runner and selector, fixtures, the
workflow itself) selects every study module. Dynamic imports (``importlib``) are invisible; the
weekly full run and manual dispatch are the safety net.

``python tests/select_tests.py FILE`` prints the selected study modules for the changed paths in FILE.
"""
from __future__ import annotations

import ast
from pathlib import Path
import sys

PACKAGE = "market_analysis"
# Repo-relative paths (or prefixes ending in "/") whose change runs every study module.
RUN_ALL = (
    "python/requirements",
    "python/tests/run_shard.py",
    "python/tests/shards.json",
    "python/tests/select_tests.py",
    "python/tests/fixtures/",
    ".github/workflows/verify.yml",
)
# Directories the study tests read at run time: prefix -> words that must all appear in the test source.
DATA_DIRS = {"scripts/research/": ("scripts", "research"), "research/": ("research",)}


def _module_name(path: Path, root: Path, prefix: str) -> tuple[str, bool]:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join([p for p in (prefix, *parts) if p]), is_package


def imports_of(source: str, module: str, is_package: bool) -> set[str]:
    """Absolute dotted names imported by ``source`` (candidates; the caller keeps the real modules)."""
    found: set[str] = set()
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".") if package else []
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join([*base, *(node.module.split(".") if node.module else [])])
            else:
                target = node.module or ""
            if not target:
                continue
            found.add(target)
            found.update(f"{target}.{alias.name}" for alias in node.names)  # `from pkg import submodule`
    return found


def build_graph(python_dir: Path, package: str = PACKAGE) -> tuple[dict[str, set[str]], dict[str, Path]]:
    """module -> imported modules that exist in the package or in the tests directory; module -> file."""
    files: dict[str, Path] = {}
    packages: set[str] = set()
    for path in (python_dir / package).rglob("*.py"):
        name, is_package = _module_name(path, python_dir, "")
        files[name] = path
        if is_package:
            packages.add(name)
    for path in (python_dir / "tests").glob("*.py"):  # helper and test modules import each other by bare name
        files[path.stem] = path
    graph: dict[str, set[str]] = {}
    for name, path in files.items():
        is_package = name in packages
        deps: set[str] = set()
        for candidate in imports_of(path.read_text(encoding="utf-8"), name, is_package):
            if candidate in files and candidate != name:
                deps.add(candidate)
            parts = candidate.split(".")  # importing a.b.c also runs a/__init__ and a/b/__init__
            deps.update(".".join(parts[:i]) for i in range(1, len(parts)) if ".".join(parts[:i]) in packages)
        graph[name] = deps
    return graph, files


def closure(root: str, graph: dict[str, set[str]]) -> set[str]:
    seen, pending = {root}, [root]
    while pending:
        for dep in graph.get(pending.pop(), ()):
            if dep not in seen:
                seen.add(dep)
                pending.append(dep)
    return seen


def select(changed: list[str], study_modules: list[str], python_dir: Path, repo_root: Path | None = None,
           package: str = PACKAGE, run_all: tuple[str, ...] = RUN_ALL) -> list[str]:
    """Study modules affected by the repo-relative ``changed`` paths, in the given order."""
    repo_root = repo_root or python_dir.parent
    changed = [path.strip() for path in changed if path.strip()]
    if any(path.startswith(prefix) for path in changed for prefix in run_all):
        return list(study_modules)
    graph, files = build_graph(python_dir, package)
    changed_files = {(repo_root / path).resolve() for path in changed}
    touched = {name for name, path in files.items() if path.resolve() in changed_files}
    selected = []
    for module in study_modules:
        test_file = files.get(module)
        if test_file is None:
            selected.append(module)  # unknown to the graph: be safe
            continue
        if closure(module, graph) & touched:
            selected.append(module)
            continue
        source = test_file.read_text(encoding="utf-8")
        prefixes = [d for d, words in DATA_DIRS.items() if all(word in source for word in words)]
        if any(path.startswith(prefix) for path in changed for prefix in prefixes):
            selected.append(module)
    return selected


def main(argv: list[str]) -> int:
    import json

    if len(argv) != 2:
        raise SystemExit("usage: python tests/select_tests.py CHANGED_FILE")
    tests = Path(__file__).resolve().parent
    study = json.loads((tests / "shards.json").read_text(encoding="utf-8"))["study"]
    changed = Path(argv[1]).read_text(encoding="utf-8").splitlines()
    print("\n".join(select(changed, study, tests.parent)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
