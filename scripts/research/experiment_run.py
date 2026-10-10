#!/usr/bin/env python3
"""Register, plan and run #182 benchmark questions against the private research-data repo.

Subcommands: ``register`` (questions/<id>.json), ``plan`` (plans/<id>__<id16>.plan.json,
after development and validation reports exist) and ``run`` (one segment). Everything
private (questions, plans, hidden openings, the experiment ledger, reports) lives in a
shallow clone of RESEARCH_DATA_REPOSITORY; labels and bars come from its releases with
sha256 verification. The token reaches git only through an http extraheader passed in
the environment (never a URL or an argument) and the release API through
ResearchDataRepo. Public output: the lines of experiment_run.public_summary and plan
ids only; library errors print their type name, never their text. Hypotheses and
finalists live in private drafts/ files of the research-data repo (committed by the
owner, never by this script), never in dispatch inputs; nothing from them is printed.

The wall clock is read ONCE at process start (now_utc) and passed down.

Progress (``Progress``): one public line per phase (checkout, downloads, spec build,
label load, evaluate/bootstrap/placebo per variant, joint bootstrap and step-down,
report write, push) with elapsed seconds from a monotonic clock, integer counts and
peak memory, plus a heartbeat at least every 60 s inside long stages, and a timing
table in the job summary. Phase names and count keys come from fixed allowlists and
counts must be integers, so no outcome, strategy name or symbol can reach these lines.
The monotonic clock never reaches records or reports.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from data_lake_build import GitHubError, ResearchDataRepo  # noqa: E402
from label_build import download, download_inputs, label_tag, published_inputs  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402
from market_analysis import metrics_lake as mx  # noqa: E402
from market_analysis.benchmark import experiment_run as er, positioning  # noqa: E402
from market_analysis.benchmark.experiment_log import ExperimentLog  # noqa: E402
from market_analysis.benchmark.hidden_guard import HiddenGate, write_plan  # noqa: E402
from market_analysis.benchmark.report import canonical_json, markdown  # noqa: E402
from market_analysis.benchmark.runner import NO_PROGRESS  # noqa: E402
from market_analysis.benchmark.segments import segment_months  # noqa: E402

NOW_UTC = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # the only wall-clock read
OPENS = "hidden-opens.jsonl"
PUSH_ATTEMPTS = 3


class GitError(RuntimeError):
    """A git step failed; the message names the step only (never output or URLs)."""


class PublicError(RuntimeError):
    """An error whose text is safe for the public log (tags, question ids, usage)."""


class Checkout:
    """Shallow clone of the private repo; credentials only via GIT_CONFIG_* environment."""

    def __init__(self, repository: str, token: str, path: Path):
        if not token:
            raise PublicError("configure the RESEARCH_DATA_TOKEN secret")
        self.path = path
        header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::add-mask::{header}", flush=True)
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {header}"}
        self._git("clone", ["clone", "--depth", "1", "--quiet", f"https://github.com/{repository}.git", str(path)],
                  cwd=None)
        self.branch = self._git("branch", ["symbolic-ref", "--short", "HEAD"]).strip()

    def _git(self, step: str, args: list[str], cwd: Path | None | str = "repo") -> str:
        command = ["git", "-c", "user.name=experiment-run", "-c",
                   "user.email=experiment-run@users.noreply.github.com", *args]
        result = subprocess.run(command, cwd=self.path if cwd == "repo" else cwd, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode:
            raise GitError(f"git {step} failed")
        return result.stdout

    def commit_and_push(self, paths: list[str], message: str) -> None:
        """One commit, pushed; on rejection fetch the tip and replay the commit (up to 3 attempts)."""
        self._git("add", ["add", "--", *paths])
        self._git("commit", ["commit", "--quiet", "-m", message])
        for attempt in range(PUSH_ATTEMPTS):
            try:
                self._git("push", ["push", "--quiet", "origin", f"HEAD:{self.branch}"])
                return
            except GitError:
                if attempt == PUSH_ATTEMPTS - 1:
                    raise GitError("git push rejected after retries") from None
            self._git("fetch", ["fetch", "--quiet", "--depth", "1", "origin", self.branch])
            # Exactly one local commit: replay it on the new tip (questions never share files).
            self._git("rebase", ["rebase", "--quiet", "--onto", "FETCH_HEAD", "HEAD~1"])


def _quiet(function, *args, **kwargs):
    """Library/download output can name symbols, months and sizes: keep it out of the public log."""
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        return function(*args, **kwargs)


PHASES = frozenset({"checkout", "label download", "bars download", "metrics download", "snapshot", "hidden opening", "spec build",
                    "label load", "evaluate", "bootstrap ci", "placebo", "spa bootstrap", "step-down", "report write",
                    "push"})
COUNT_KEYS = frozenset({"symbol_index", "symbols", "months", "files", "rows", "variant", "of", "total"})
HEARTBEAT_SECONDS = 60.0


def peak_memory_mb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024  # Linux reports KiB


class Progress:
    """Public progress lines: allowlisted phase names, integer counts, seconds and peak memory only.

    The output stream is captured at construction, so lines still reach the public
    log while ``_quiet`` sends library output to /dev/null.
    """

    def __init__(self, stream=None, *, clock=time.monotonic, memory=peak_memory_mb,
                 interval: float = HEARTBEAT_SECONDS):
        self.stream = stream if stream is not None else sys.stdout
        self.clock, self.memory, self.interval = clock, memory, interval
        self.started = clock()
        self.timings: dict[str, float] = {}
        self._open: tuple[str, float] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _check(name: str, counts: dict) -> None:
        if name not in PHASES:
            raise ValueError(f"unknown progress phase {name!r}")
        for key, value in counts.items():
            if key not in COUNT_KEYS or type(value) is not int:
                raise ValueError(f"progress counts are allowlisted integers only, not {key!r}")

    def _write(self, kind: str, name: str, counts: dict) -> None:
        now = self.clock()
        fields = [f"{kind}={name.replace(' ', '_')}", f"t={now - self.started:.0f}s",
                  *(f"{key}={value}" for key, value in counts.items()), f"maxrss_mb={self.memory()}"]
        with self._lock:
            self.stream.write("progress " + " ".join(fields) + "\n")
            self.stream.flush()

    def _close(self, now: float) -> None:
        if self._open is not None:
            name, since = self._open
            self.timings[name] = self.timings.get(name, 0.0) + now - since
        self._open = None

    def phase(self, name: str, **counts) -> None:
        self._check(name, counts)
        now = self.clock()
        self._close(now)
        self._open = (name, now)
        self._write("phase", name, counts)

    @contextmanager
    def stage(self, name: str, total: int | None = None):
        """A long stage: a phase line now, then a heartbeat every ``interval`` seconds until it ends."""
        self.phase(name, **({} if total is None else {"total": total}))
        step = {"value": 0}
        stop = threading.Event()

        def beat():
            while not stop.wait(self.interval):
                counts = {"step": step["value"]} if total is None else {"step": step["value"], "of": total}
                self._write("heartbeat", name, counts)

        thread = threading.Thread(target=beat, name=f"heartbeat {name}", daemon=True)
        thread.start()
        try:
            yield lambda value: step.__setitem__("value", int(value))
        finally:
            stop.set()
            thread.join()

    def finish(self) -> list[str]:
        """Close the last phase; the timing table (markdown) for the job summary."""
        now = self.clock()
        self._close(now)
        lines = ["### Experiment run timings", "", "| phase | seconds |", "| --- | ---: |"]
        lines += [f"| {name} | {seconds:.0f} |" for name, seconds in self.timings.items()]
        lines += [f"| total | {now - self.started:.0f} |", ""]
        return lines


def emit(lines: list[str], summary: Path | None) -> None:
    print("\n".join(lines), flush=True)
    if summary:
        with summary.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(["```", *lines, "```", ""]))


# ---------------------------------------------------------------- downloads


def download_labels(repo: ResearchDataRepo, question: dict, segment: str, label_dir: Path,
                    progress=NO_PROGRESS) -> None:
    """The question's label manifest (lb1/lb2/lb3/lb3h by sigma model) + the segment's monthly label files per
    symbol, sha256-verified against the manifest."""
    with progress.stage("label download", total=len(lake.SYMBOLS)) as set_symbol:
        for symbol_index, symbol in enumerate(lake.SYMBOLS, 1):
            set_symbol(symbol_index)
            _download_symbol_labels(repo, question, segment, label_dir, symbol)
            progress.phase("label download", symbol_index=symbol_index, symbols=len(lake.SYMBOLS),
                           months=len(segment_months(segment)), files=len(segment_months(segment)) + 1)


def _download_symbol_labels(repo: ResearchDataRepo, question: dict, segment: str, label_dir: Path,
                            symbol: str) -> None:
    # The question's sigma model selects the release family: lb1 / lb2 / lb3 / lb3h (P16).
    tag = label_tag(symbol, er.LABEL_FIRST_MONTH, er.LABEL_LAST_MONTH, question["label_revision"],
                    er.question_sigma_model(question))
    release = repo.published_release(tag)
    if release is None or release.get("tag_name") != tag or release.get("draft") is not False:
        raise PublicError(f"missing published release: {tag}")
    assets = repo.assets(release["id"])
    manifest_name = er.label_manifest_name(symbol)
    if manifest_name not in assets:
        raise PublicError(f"{tag}: missing {manifest_name}")
    manifest_path = label_dir / manifest_name
    download(repo, assets[manifest_name], manifest_path, limit=16 << 20)
    outputs = {item["month"]: item for item in json.loads(manifest_path.read_bytes())["outputs"]}
    for month in segment_months(segment):
        item = outputs.get(month)
        if item is None or item["name"] not in assets:
            raise PublicError(f"{tag}: missing label file for {month}")
        download(repo, assets[item["name"]], label_dir / item["name"], item["sha256"])


_MANIFEST_LIMIT = 16 << 20


def download_metrics_month(repo, symbol: str, month: str, revision: int, dest: Path) -> str:
    """Download one ``mx-SYMBOL-MONTH-rN`` csv.gz into ``dest``, sha256-verified (asset digest, else the
    release manifest's sha256, as ``daily_download``); returns its sha256."""
    tag = mx.release_tag(symbol, month, revision)
    release = repo.published_release(tag)
    if release is None or release.get("tag_name") != tag or release.get("draft") is not False:
        raise PublicError(f"missing published release: {tag}")
    assets = repo.assets(release["id"])
    name = mx.csv_asset_name(symbol, month)
    if name not in assets:
        raise PublicError(f"{tag}: missing asset {name}")
    expected = None
    if not assets[name].get("digest"):
        manifest_name = mx.manifest_asset_name(symbol, month)
        if manifest_name not in assets:
            raise PublicError(f"{tag}: missing {manifest_name} for checksum fallback")
        manifest_path = Path(dest) / manifest_name
        download(repo, assets[manifest_name], manifest_path, limit=_MANIFEST_LIMIT)
        manifest = json.loads(manifest_path.read_bytes())
        if (manifest.get("release_tag") != tag or manifest.get("symbol") != symbol
                or manifest.get("month") != month):
            raise PublicError(f"{tag}: manifest identity mismatch")
        matches = [item["sha256"] for item in manifest.get("assets", []) if item.get("name") == name]
        if len(matches) != 1:
            raise PublicError(f"{tag}: no unique sha256 for {name}")
        expected = matches[0]
    sha, _ = download(repo, assets[name], Path(dest) / name, expected)
    return sha


def download_metrics(repo: ResearchDataRepo, question: dict, segment: str, bars_dir: Path,
                     progress=NO_PROGRESS) -> None:
    """mx metrics csv.gz of FIRST_MONTH..segment's last month for a ``metrics`` family (positioning-v1);
    sha256-verified as in the screen. Only months of the segment's window are fetched, so a development or
    validation run never downloads hidden months."""
    if er.family_of(question).data != "metrics":
        return
    months = lake.months_between(lake.FIRST_MONTH, segment_months(segment)[-1])
    with progress.stage("metrics download", total=len(lake.SYMBOLS)) as set_symbol:
        for symbol_index, symbol in enumerate(lake.SYMBOLS, 1):
            set_symbol(symbol_index)
            for month in months:
                download_metrics_month(repo, symbol, month, positioning.METRICS_REVISION, bars_dir)
            progress.phase("metrics download", symbol_index=symbol_index, symbols=len(lake.SYMBOLS),
                           months=len(months))


def download_bars(repo: ResearchDataRepo, question: dict, segment: str, bars_dir: Path,
                  label_dir: Path | None, progress=NO_PROGRESS) -> None:
    """rd bars (and funding) of FIRST_MONTH..segment's last month; checks they are the labels' inputs.

    ``label_dir=None`` (count mode) skips that check, so no label file is read.
    """
    months = lake.months_between(lake.FIRST_MONTH, segment_months(segment)[-1])
    with progress.stage("bars download", total=len(lake.SYMBOLS)) as set_symbol:
        for symbol_index, symbol in enumerate(lake.SYMBOLS, 1):
            set_symbol(symbol_index)
            _download_symbol_bars(repo, question, bars_dir, label_dir, symbol, months)
            progress.phase("bars download", symbol_index=symbol_index, symbols=len(lake.SYMBOLS),
                           months=len(months))


def _download_symbol_bars(repo: ResearchDataRepo, question: dict, bars_dir: Path, label_dir: Path | None,
                          symbol: str, months: list[str]) -> None:
    try:
        releases = published_inputs(repo, symbol, months, question["data_revision"])
    except GitHubError as error:
        if str(error).startswith("missing published data releases: "):
            raise PublicError(str(error)) from None
        raise
    download_inputs(repo, symbol, releases, bars_dir)
    if label_dir is None:
        return
    label_rd = set(json.loads((label_dir / er.label_manifest_name(symbol)).read_bytes())
                   .get("rd_tags", []))
    if not {tag for tag, _ in releases.values()} <= label_rd:
        raise PublicError(f"{symbol}: data revision differs from the rd inputs of its label release")


# ---------------------------------------------------------------- subcommands


def register(args, checkout: Checkout) -> list[str]:
    if args.family not in er.FAMILIES:
        raise PublicError(f"family must be one of {sorted(er.FAMILIES)}")
    family = er.FAMILIES[args.family]
    strategies = (list(family.strategies_for(args.horizon)) if args.strategies == "all"
                  else args.strategies.split(","))
    try:
        hypothesis = er.read_hypothesis(checkout.path / "drafts", args.question_id)
        question = er.make_question(args.question_id, hypothesis, args.horizon, strategies, seed=args.seed,
                                    label_revision=args.label_revision, data_revision=args.data_revision,
                                    created_utc=NOW_UTC, B_stats=args.b_stats, B_placebo=args.b_placebo,
                                    family=args.family, sigma_model=args.sigma_model)
    except er.QuestionError as error:
        raise PublicError(str(error)) from None
    path = checkout.path / "questions" / f"{args.question_id}.json"
    payload = er.question_bytes(question)
    if path.exists():
        existing = er.load_question(path)
        if {**existing, "created_utc": NOW_UTC} != question:
            raise PublicError(f"question {args.question_id} is already registered with different content")
        return [f"question {args.question_id} already registered ({er.question_hash(existing)})"]
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(payload)
    checkout.commit_and_push([str(path.relative_to(checkout.path))],
                             f"Register question {args.question_id} ({er.question_hash(question)[:16]})")
    return [f"registered question {args.question_id} ({er.question_hash(question)})"]


def plan(args, checkout: Checkout) -> list[str]:
    question = er.load_question(checkout.path / "questions" / f"{args.question_id}.json")
    try:
        snapshot = er.snapshot_from_reports(checkout.path / "reports", args.question_id)
        record = er.build_plan(question, er.read_finalists(checkout.path / "drafts", args.question_id,
                                                           question["family"]),
                               NOW_UTC, snapshot)
    except er.QuestionError as error:
        raise PublicError(str(error)) from None
    plans = checkout.path / "plans"
    plans.mkdir(exist_ok=True)
    path = write_plan(plans, record)
    checkout.commit_and_push([str(path.relative_to(checkout.path))],
                             f"Plan hidden run of {args.question_id} ({record.plan_id[:16]})")
    return [f"plan {record.plan_id} for question {args.question_id}, {record.variants} variants"]


def run(args, checkout: Checkout, repo: ResearchDataRepo, workdir: Path, progress=NO_PROGRESS) -> list[str]:
    root = checkout.path
    question_path = root / "questions" / f"{args.question_id}.json"
    if not question_path.is_file():
        raise PublicError(f"question {args.question_id} is not registered")
    question = er.load_question(question_path)
    if args.segment == "hidden":
        er.check_hidden_request(question, args.plan_id, args.confirm_hidden)
    label_dir, bars_dir = workdir / "labels", workdir / "bars"
    label_dir.mkdir(parents=True)
    bars_dir.mkdir(parents=True)
    _quiet(download_labels, repo, question, args.segment, label_dir, progress)
    _quiet(download_bars, repo, question, args.segment, bars_dir, label_dir, progress)
    _quiet(download_metrics, repo, question, args.segment, bars_dir, progress)
    progress.phase("snapshot", symbols=len(lake.SYMBOLS), months=len(segment_months(args.segment)))
    snapshot = _quiet(er.data_snapshot, label_dir, segment_months(args.segment), er.question_params(question))
    token = gate = None
    if args.segment == "hidden":
        progress.phase("hidden opening")
        gate = HiddenGate(root / "plans", root / OPENS)
        # The opening is committed and pushed before any hidden label or bar is read;
        # if the push fails, the run stops here with nothing read.
        token = er.open_hidden_stretch(
            question, args.plan_id, args.confirm_hidden, gate, now_utc=NOW_UTC,
            persist=lambda: checkout.commit_and_push(
                [OPENS], f"Open hidden stretch for {args.question_id} (plan {args.plan_id[:16]})"))
    ledger = root / "experiments" / f"{args.question_id}.jsonl"
    ledger.parent.mkdir(exist_ok=True)
    report = _quiet(er.run_question, question, args.segment, bars_dir, label_dir, ExperimentLog(ledger),
                    token=token, gate=gate, now_utc=NOW_UTC, code_commit=os.environ.get("GITHUB_SHA", "local"),
                    data_snapshot_id=snapshot, progress=progress)
    progress.phase("report write")
    json_name, md_name = er.report_paths(args.question_id, args.segment, report)
    (root / "reports").mkdir(exist_ok=True)
    (root / json_name).write_bytes(canonical_json(report))
    (root / md_name).write_text(markdown(report), encoding="utf-8")
    progress.phase("push", files=3)
    checkout.commit_and_push([str(ledger.relative_to(root)), json_name, md_name],
                             f"Run {args.question_id} on {args.segment}: report {report['report_hash']}")
    return er.public_summary(report)


def count(args, checkout: Checkout, repo: ResearchDataRepo, workdir: Path, progress=NO_PROGRESS) -> list[str]:
    """Outcome-blind signal counts of a registered question; development/validation only.

    Downloads only the rd bars/funding inputs: no labels, no ledger, no plan or
    opening log is read or written. The per-strategy table is committed privately
    as counts/<id>.json (one entry per segment); the public lines carry totals only.
    """
    if args.segment not in er.COUNT_SEGMENTS:
        raise PublicError("signal counts are for development and validation only")
    question_path = checkout.path / "questions" / f"{args.question_id}.json"
    if not question_path.is_file():
        raise PublicError(f"question {args.question_id} is not registered")
    question = er.load_question(question_path)
    bars_dir = workdir / "bars"
    bars_dir.mkdir(parents=True)
    _quiet(download_bars, repo, question, args.segment, bars_dir, None, progress)
    _quiet(download_metrics, repo, question, args.segment, bars_dir, progress)
    counts = _quiet(er.signal_counts, question, args.segment, bars_dir, progress=progress)
    progress.phase("report write")
    path = checkout.path / "counts" / f"{args.question_id}.json"
    path.parent.mkdir(exist_ok=True)
    existing = path.read_bytes() if path.is_file() else None
    path.write_bytes(er.counts_file_bytes(existing, counts, code_commit=os.environ.get("GITHUB_SHA", "local"),
                                          now_utc=NOW_UTC))
    progress.phase("push", files=1)
    checkout.commit_and_push([str(path.relative_to(checkout.path))],
                             f"Signal counts for {args.question_id} on {args.segment}")
    return er.count_public_lines(counts)


# ---------------------------------------------------------------- CLI


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=Path("experiment-run-work"))
    parser.add_argument("--summary", type=Path, help="append the public output (markdown)")
    commands = parser.add_subparsers(dest="command", required=True)
    reg = commands.add_parser("register")
    reg.add_argument("--question-id", required=True)
    reg.add_argument("--family", default="ta-baselines")
    reg.add_argument("--horizon", type=int, required=True)
    reg.add_argument("--strategies", default="all")
    reg.add_argument("--label-revision", type=int, required=True)
    reg.add_argument("--data-revision", type=int, required=True)
    reg.add_argument("--seed", type=int, required=True)
    reg.add_argument("--b-stats", type=int, default=er.MIN_B_STATS)
    reg.add_argument("--b-placebo", type=int, default=er.MIN_B_PLACEBO)
    reg.add_argument("--sigma-model", choices=sorted(er.SIGMA_MODELS), default=None,
                     help="label release family: ewma (lb1, question-v1), ewma-seasonal (lb2), ewma-robust (lb3), "
                          "ewma-robust-hcal (lb3h); default: the family's fixed model, else ewma")
    pln = commands.add_parser("plan")
    pln.add_argument("--question-id", required=True)
    rn = commands.add_parser("run")
    rn.add_argument("--question-id", required=True)
    rn.add_argument("--segment", choices=er.SEGMENTS, required=True)
    rn.add_argument("--plan-id", default="")
    rn.add_argument("--confirm-hidden", default="")
    cnt = commands.add_parser("count")
    cnt.add_argument("--question-id", required=True)
    cnt.add_argument("--segment", choices=er.COUNT_SEGMENTS, required=True)
    args = parser.parse_args(argv)
    if args.command == "run" and args.segment == "hidden" and (
            not args.plan_id or args.confirm_hidden != args.question_id):
        parser.error("a hidden run needs --plan-id and --confirm-hidden equal to the question id")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    # Never remove a pre-existing caller directory; own only this newly created one.
    args.workdir.mkdir(parents=True, exist_ok=False)
    progress = Progress(sys.stdout)  # captured before any _quiet redirect
    try:
        repository, token = os.environ.get("RESEARCH_DATA_REPOSITORY", ""), os.environ.get("RESEARCH_DATA_TOKEN", "")
        try:  # validates owner/name, a separate repository, the token, and that it is private
            repo = ResearchDataRepo(repository, token)
        except ValueError as error:
            raise PublicError(str(error)) from None
        progress.phase("checkout")
        checkout = Checkout(repository, token, args.workdir / "research-data")
        if args.command == "register":
            lines = register(args, checkout)
        elif args.command == "plan":
            lines = plan(args, checkout)
        elif args.command == "count":
            lines = count(args, checkout, repo, args.workdir / "data", progress)
        else:
            lines = run(args, checkout, repo, args.workdir / "data", progress)
        emit(lines, args.summary)
    finally:
        shutil.rmtree(args.workdir, ignore_errors=True)
        timings = progress.finish()  # also on failure: shows where a run stopped
        if args.summary:
            with args.summary.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(timings))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (PublicError, GitError) as error:
        print(f"::error title=experiment run failed::{error}", flush=True)
        sys.exit(1)
    except Exception as error:  # library errors can contain private values: type name only
        print(f"::error title=experiment run failed::{type(error).__name__}", flush=True)
        sys.exit(1)
