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
"""
from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from data_lake_build import GitHubError, ResearchDataRepo  # noqa: E402
from label_build import download, download_inputs, label_tag, published_inputs  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402
from market_analysis.benchmark import experiment_run as er  # noqa: E402
from market_analysis.benchmark.experiment_log import ExperimentLog  # noqa: E402
from market_analysis.benchmark.hidden_guard import HiddenGate, write_plan  # noqa: E402
from market_analysis.benchmark.report import canonical_json, markdown  # noqa: E402
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


def emit(lines: list[str], summary: Path | None) -> None:
    print("\n".join(lines), flush=True)
    if summary:
        with summary.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(["```", *lines, "```", ""]))


# ---------------------------------------------------------------- downloads


def download_labels(repo: ResearchDataRepo, question: dict, segment: str, label_dir: Path) -> None:
    """lb1 manifest + the segment's monthly label files per symbol, sha256-verified against the manifest."""
    for symbol in lake.SYMBOLS:
        tag = label_tag(symbol, er.LABEL_FIRST_MONTH, er.LABEL_LAST_MONTH, question["label_revision"])
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


def download_bars(repo: ResearchDataRepo, question: dict, segment: str, bars_dir: Path, label_dir: Path) -> None:
    """rd bars (and funding) of FIRST_MONTH..segment's last month; checks they are the labels' inputs."""
    months = lake.months_between(lake.FIRST_MONTH, segment_months(segment)[-1])
    for symbol in lake.SYMBOLS:
        try:
            releases = published_inputs(repo, symbol, months, question["data_revision"])
        except GitHubError as error:
            if str(error).startswith("missing published data releases: "):
                raise PublicError(str(error)) from None
            raise
        download_inputs(repo, symbol, releases, bars_dir)
        label_rd = set(json.loads((label_dir / er.label_manifest_name(symbol)).read_bytes())
                       .get("rd_tags", []))
        if not {tag for tag, _ in releases.values()} <= label_rd:
            raise PublicError(f"{symbol}: data revision differs from the rd inputs of its label release")


# ---------------------------------------------------------------- subcommands


def register(args, checkout: Checkout) -> list[str]:
    if args.family not in er.FAMILIES:
        raise PublicError(f"family must be one of {sorted(er.FAMILIES)}")
    family = er.FAMILIES[args.family]
    strategies = list(family.strategies) if args.strategies == "all" else args.strategies.split(",")
    try:
        hypothesis = er.read_hypothesis(checkout.path / "drafts", args.question_id)
        question = er.make_question(args.question_id, hypothesis, args.horizon, strategies, seed=args.seed,
                                    label_revision=args.label_revision, data_revision=args.data_revision,
                                    created_utc=NOW_UTC, B_stats=args.b_stats, B_placebo=args.b_placebo,
                                    family=args.family)
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
        record = er.build_plan(question, er.read_finalists(checkout.path / "drafts", args.question_id),
                               NOW_UTC, snapshot)
    except er.QuestionError as error:
        raise PublicError(str(error)) from None
    plans = checkout.path / "plans"
    plans.mkdir(exist_ok=True)
    path = write_plan(plans, record)
    checkout.commit_and_push([str(path.relative_to(checkout.path))],
                             f"Plan hidden run of {args.question_id} ({record.plan_id[:16]})")
    return [f"plan {record.plan_id} for question {args.question_id}, {record.variants} variants"]


def run(args, checkout: Checkout, repo: ResearchDataRepo, workdir: Path) -> list[str]:
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
    _quiet(download_labels, repo, question, args.segment, label_dir)
    _quiet(download_bars, repo, question, args.segment, bars_dir, label_dir)
    snapshot = _quiet(er.data_snapshot, label_dir, segment_months(args.segment))
    token = gate = None
    if args.segment == "hidden":
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
                    data_snapshot_id=snapshot)
    json_name, md_name = er.report_paths(args.question_id, args.segment, report)
    (root / "reports").mkdir(exist_ok=True)
    (root / json_name).write_bytes(canonical_json(report))
    (root / md_name).write_text(markdown(report), encoding="utf-8")
    checkout.commit_and_push([str(ledger.relative_to(root)), json_name, md_name],
                             f"Run {args.question_id} on {args.segment}: report {report['report_hash']}")
    return er.public_summary(report)


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
    pln = commands.add_parser("plan")
    pln.add_argument("--question-id", required=True)
    rn = commands.add_parser("run")
    rn.add_argument("--question-id", required=True)
    rn.add_argument("--segment", choices=er.SEGMENTS, required=True)
    rn.add_argument("--plan-id", default="")
    rn.add_argument("--confirm-hidden", default="")
    args = parser.parse_args(argv)
    if args.command == "run" and args.segment == "hidden" and (
            not args.plan_id or args.confirm_hidden != args.question_id):
        parser.error("a hidden run needs --plan-id and --confirm-hidden equal to the question id")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    # Never remove a pre-existing caller directory; own only this newly created one.
    args.workdir.mkdir(parents=True, exist_ok=False)
    try:
        repository, token = os.environ.get("RESEARCH_DATA_REPOSITORY", ""), os.environ.get("RESEARCH_DATA_TOKEN", "")
        try:  # validates owner/name, a separate repository, the token, and that it is private
            repo = ResearchDataRepo(repository, token)
        except ValueError as error:
            raise PublicError(str(error)) from None
        checkout = Checkout(repository, token, args.workdir / "research-data")
        if args.command == "register":
            lines = register(args, checkout)
        elif args.command == "plan":
            lines = plan(args, checkout)
        else:
            lines = run(args, checkout, repo, args.workdir / "data")
        emit(lines, args.summary)
    finally:
        shutil.rmtree(args.workdir, ignore_errors=True)
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
