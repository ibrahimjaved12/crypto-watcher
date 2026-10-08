"""One benchmark question end to end, without network or git (#182 slice G).

A question (schema ``question-v1``) is ONE horizon of ONE signal family: its
strategies x the family's fixed k values x rr indices, all six symbols, both sides.
``runner.run_experiment`` turns every strategy x every (horizon, k, rr_index) present
in the geometries into a variant, so the geometries never mix horizons.

Families (``FAMILIES``) fix the strategy version, the allowed strategies and
horizons, the geometry, the data a strategy needs and how specs are built:
``ta-baselines`` (ta-v1 on closed candles, k in {1, 2} x rr_index 0..3, K = strategies
x 8) and ``order-flow`` (of-v1 on minute bars + funding, 240 minutes, k 2 x rr_index 1).
Inputs are loaded one symbol at a time and released once that symbol's signals are
built.

Indicator history always starts at ``data_lake.FIRST_MONTH`` and ends with the last
month of the evaluated segment, so a development/validation run never touches
hidden months. A hidden run needs a verified OpenToken for this question; its
specs are the plan's finalists only. The scripts layer (scripts/research/
experiment_run.py) does downloads, git and the hidden-opening commit.
Hypotheses and finalists live in private drafts/ files of the research-data repo,
never in (public) workflow dispatch inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
from typing import Callable

from .. import data_lake
from . import order_flow, ta_strategies
from .canonical import canonical_bytes, content_hash, exact_from_str
from .evaluate import decimal_text
from .hidden_guard import HiddenGate, HiddenGuardError, HiddenStretchLocked, Plan
from .label_store import Geometry
from .labels import LabelParams
from .market_data import load_symbol_bars, load_symbol_candles, load_symbol_funding
from .runner import StrategySpec, run_experiment
from .segments import segment_bounds_ms, segment_months

SCHEMA = "question-v1"
MIN_B_STATS, MIN_B_PLACEBO = 2000, 200
SEGMENTS = ("development", "validation", "hidden")
LABEL_FIRST_MONTH = data_lake.FIRST_MONTH
LABEL_LAST_MONTH = segment_months("hidden")[-1]  # lb1 releases cover every segment
QUESTION_FIELDS = ("schema", "question_id", "hypothesis", "family", "strategy_version", "strategies", "horizon_min",
                   "k_values", "rr_indices", "seed", "B_stats", "B_placebo", "label_revision", "data_revision",
                   "created_utc")
_QUESTION_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}\Z")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


class QuestionError(ValueError):
    """An invalid question-v1 record."""


@dataclass(frozen=True)
class Family:
    """A signal family: what a question of this family may contain and how its specs are built.

    ``load(bars_dir, symbol, last_month, horizon, token, gate)`` returns one symbol's
    inputs (guarded loads from ``data_lake.FIRST_MONTH``); ``signals(inputs, symbol,
    name, horizon, first_ms, end_ms, params)`` returns that symbol's signal rows;
    ``config(name, horizon)`` is the StrategySpec config.
    """

    family_id: str
    strategy_version: str
    strategies: tuple
    horizons: tuple
    k_values: tuple
    rr_indices: tuple
    data: str
    load: Callable
    signals: Callable
    config: Callable


def _load_candles(bars_dir, symbol, last_month, horizon, token, gate):
    return load_symbol_candles(bars_dir, symbol, data_lake.FIRST_MONTH, last_month, (horizon,), token=token,
                               gate=gate)


def _ta_signals(candles, symbol, name, horizon, first_ms, end_ms, params):
    return ta_strategies.make_specs({symbol: candles}, name, horizon, first_ms=first_ms, end_ms=end_ms).signals


def _load_bars_funding(bars_dir, symbol, last_month, horizon, token, gate):
    bars = load_symbol_bars(bars_dir, symbol, data_lake.FIRST_MONTH, last_month, token=token, gate=gate)
    return bars, load_symbol_funding(bars_dir, symbol, data_lake.FIRST_MONTH, last_month, token=token, gate=gate)


def _order_flow_signals(inputs, symbol, name, horizon, first_ms, end_ms, params):
    bars, funding = inputs
    return order_flow.symbol_signals(name, bars, funding, horizon, first_ms=first_ms, end_ms=end_ms,
                                     label_step_min=params.step(horizon))


FAMILIES = {family.family_id: family for family in (
    Family("ta-baselines", ta_strategies.VERSION, tuple(ta_strategies.STRATEGIES), ta_strategies.TIMEFRAMES,
           ("1", "2"), (0, 1, 2, 3), "candles", _load_candles, _ta_signals, ta_strategies.strategy_config),
    Family("order-flow", order_flow.VERSION, tuple(order_flow.STRATEGIES), (order_flow.HORIZON,),
           tuple(order_flow.K_VALUES), tuple(order_flow.RR_INDICES), "bars+funding", _load_bars_funding,
           _order_flow_signals, order_flow.strategy_config),
)}


def family_of(question: dict) -> Family:
    family = FAMILIES.get(question.get("family")) if type(question.get("family")) is str else None
    if family is None:
        raise QuestionError(f"family must be one of {sorted(FAMILIES)}")
    return family


MAX_HYPOTHESIS = 2000


def _draft_text(path) -> str:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        text = ""
    if not text:
        raise QuestionError(f"write drafts/{path.name} in the research-data repo first")
    return text


def read_hypothesis(drafts_dir, question_id: str) -> str:
    """drafts/<id>.hypothesis.txt: UTF-8, stripped, non-empty, at most 2000 characters."""
    text = _draft_text(Path(drafts_dir) / f"{question_id}.hypothesis.txt")
    if len(text) > MAX_HYPOTHESIS:
        raise QuestionError(f"hypothesis draft exceeds {MAX_HYPOTHESIS} characters")
    return text


def read_finalists(drafts_dir, question_id: str, family_id: str) -> list[str]:
    """drafts/<id>.finalists.txt: one strategy name per line; blank lines and # comments ignored.

    Every name must be a strategy of the question's own family (``build_plan`` then
    also requires it to be one of the question's strategies).
    """
    family = family_of({"family": family_id})
    names = []
    for line in _draft_text(Path(drafts_dir) / f"{question_id}.finalists.txt").splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            names.append(name)
    if not names:
        raise QuestionError(f"write drafts/{question_id}.finalists.txt in the research-data repo first")
    if len(set(names)) != len(names):
        raise QuestionError("finalists draft lists a strategy more than once")
    if any(name not in family.strategies for name in names):
        raise QuestionError(f"finalists draft names a strategy outside the {family.family_id} family")
    return names


def _int(value, name: str, minimum: int, maximum: int | None = None) -> None:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise QuestionError(f"{name} must be an integer >= {minimum}" + (f" and <= {maximum}" if maximum else ""))


def validate_question(question: dict) -> dict:
    """Strict question-v1 check; returns the question unchanged."""
    if type(question) is not dict:
        raise QuestionError("question must be an object")
    unknown = sorted(set(question) - set(QUESTION_FIELDS))
    missing = sorted(set(QUESTION_FIELDS) - set(question))
    if unknown or missing:
        raise QuestionError(f"question fields: unknown {unknown}, missing {missing}")
    family = family_of(question)
    if question["schema"] != SCHEMA or question["strategy_version"] != family.strategy_version:
        raise QuestionError(f"question must be {SCHEMA} / {family.family_id} / {family.strategy_version}")
    if type(question["question_id"]) is not str or not _QUESTION_ID.fullmatch(question["question_id"]):
        raise QuestionError("question_id must be lowercase [a-z0-9._-], at most 100 characters")
    if type(question["hypothesis"]) is not str or not question["hypothesis"].strip():
        raise QuestionError("hypothesis must be non-empty text")
    strategies = question["strategies"]
    if (type(strategies) is not list or not strategies or len(set(strategies)) != len(strategies)
            or any(name not in family.strategies for name in strategies)):
        raise QuestionError(f"strategies must be distinct names from {sorted(family.strategies)}")
    if question["horizon_min"] not in family.horizons or type(question["horizon_min"]) is not int:
        raise QuestionError(f"horizon_min must be one of {family.horizons}")
    if question["k_values"] != list(family.k_values) or question["rr_indices"] != list(family.rr_indices):
        raise QuestionError(f"k_values must be {list(family.k_values)} and rr_indices {list(family.rr_indices)}")
    _int(question["seed"], "seed", 0, 2 ** 64 - 1)
    _int(question["B_stats"], "B_stats", MIN_B_STATS)
    _int(question["B_placebo"], "B_placebo", MIN_B_PLACEBO)
    _int(question["label_revision"], "label_revision", 1, 999)
    _int(question["data_revision"], "data_revision", 1, 999)
    if type(question["created_utc"]) is not str or not _UTC.fullmatch(question["created_utc"]):
        raise QuestionError("created_utc must be YYYY-MM-DDTHH:MM:SSZ")
    return question


def make_question(question_id: str, hypothesis: str, horizon_min: int, strategies, *, seed: int,
                  label_revision: int, data_revision: int, created_utc: str, B_stats: int = MIN_B_STATS,
                  B_placebo: int = MIN_B_PLACEBO, family: str = "ta-baselines") -> dict:
    entry = family_of({"family": family})
    question = {"schema": SCHEMA, "question_id": question_id, "hypothesis": hypothesis, "family": family,
                "strategy_version": entry.strategy_version, "strategies": list(strategies), "horizon_min": horizon_min,
                "k_values": list(entry.k_values), "rr_indices": list(entry.rr_indices), "seed": seed,
                "B_stats": B_stats,
                "B_placebo": B_placebo, "label_revision": label_revision, "data_revision": data_revision,
                "created_utc": created_utc}
    return validate_question(question)


def question_bytes(question: dict) -> bytes:
    return canonical_bytes(validate_question(question))


def load_question(path) -> dict:
    raw = Path(path).read_bytes()
    question = validate_question(json.loads(raw))
    if canonical_bytes(question) != raw:
        raise QuestionError(f"{Path(path).name} is not canonical JSON")
    return question


def question_hash(question: dict) -> str:
    return content_hash(validate_question(question))


def build_geometries(question: dict, params: LabelParams | None = None) -> list[Geometry]:
    """All six symbols x both sides x k x rr_index, one horizon only."""
    params = params or LabelParams()
    validate_question(question)
    horizon = question["horizon_min"]
    if horizon not in params.horizons:
        raise QuestionError("question horizon is not in the label parameters")
    geometries = []
    for symbol in data_lake.SYMBOLS:
        for side in (1, -1):
            for text in question["k_values"]:
                k = exact_from_str(text)
                if k not in params.k_grid:
                    raise QuestionError(f"k {text} is not in the label parameters")
                for rr_index in question["rr_indices"]:
                    if not 0 <= rr_index < len(params.rr_grid):
                        raise QuestionError(f"rr_index {rr_index} is not in the label parameters")
                    geometries.append(Geometry(symbol, horizon, side, k, rr_index))
    return geometries


def build_specs(question: dict, segment: str, load_inputs, strategies=None, params: LabelParams | None = None) -> list:
    """StrategySpecs for the segment window; indicators use all loaded history.

    ``load_inputs(symbol)`` returns one symbol's family inputs; they are released as
    soon as that symbol's signals are built.
    """
    validate_question(question)
    family, params = family_of(question), params or LabelParams()
    first_ms, end_ms = segment_bounds_ms(segment)
    horizon = question["horizon_min"]
    names = question["strategies"] if strategies is None else list(strategies)
    signals = {name: [] for name in names}
    for symbol in data_lake.SYMBOLS:
        inputs = load_inputs(symbol)
        for name in names:
            signals[name].extend(family.signals(inputs, symbol, name, horizon, first_ms, end_ms, params))
        del inputs  # this symbol's bars/candles are released here
    return [StrategySpec(name, family.strategy_version, family.config(name, horizon), signals[name])
            for name in names]


def label_manifest_name(symbol: str) -> str:
    return f"labels__{symbol}.manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def data_snapshot(label_dir, months, params: LabelParams | None = None, symbols=data_lake.SYMBOLS) -> str:
    """content_hash of the label inputs: identities, manifest sha256 and rd tags per symbol.

    Every listed month's label file must be present and match its manifest's
    sha256 (a mismatch or a missing file is a hard error). The id does not
    depend on the segment, so plans and hidden runs share it.
    """
    params = params or LabelParams()
    root = Path(label_dir)
    manifests, rd_tags = {}, {}
    for symbol in sorted(set(symbols)):
        path = root / label_manifest_name(symbol)
        manifest = json.loads(path.read_bytes())
        if (manifest.get("symbol") != symbol or manifest.get("params_identity") != params.identity()
                or manifest.get("cost_model_identity") != params.cost_model.identity()):
            raise ValueError(f"{path.name}: symbol/params/cost model identity mismatch")
        outputs = {item["month"]: item for item in manifest["outputs"]}
        for month in sorted(set(months)):
            if month not in outputs:
                raise ValueError(f"{path.name}: no label output for {month}")
            label = root / outputs[month]["name"]
            if _sha256(label) != outputs[month]["sha256"]:
                raise ValueError(f"{label.name}: sha256 differs from its manifest")
        manifests[symbol] = _sha256(path)
        rd_tags[symbol] = sorted(manifest.get("rd_tags", []))
    return content_hash({"params_identity": params.identity(), "cost_model_identity": params.cost_model.identity(),
                         "label_manifest_sha256": manifests, "rd_tags": rd_tags})


def build_plan(question: dict, finalists, created_utc: str, data_snapshot_id: str) -> Plan:
    """Pre-registered hidden plan for the finalists only: variants = finalists x k x rr."""
    family = family_of(validate_question(question))
    finalists = list(finalists)
    if not finalists or len(set(finalists)) != len(finalists) or any(
            name not in question["strategies"] for name in finalists):
        raise QuestionError("finalists must be distinct strategies of the question")
    return Plan(question_id=question["question_id"], hypothesis=question["hypothesis"],
                strategies=[{"strategy_id": name, "strategy_version": family.strategy_version,
                             "config": family.config(name, question["horizon_min"])} for name in finalists],
                variants=len(finalists) * len(question["k_values"]) * len(question["rr_indices"]),
                statistic="StepM+bootstrap t, K-adjusted", threshold="3", split_id="hidden",
                data_snapshot_id=data_snapshot_id, created_utc=created_utc)


def check_hidden_request(question: dict, plan_id, confirm) -> None:
    """Refuse a hidden run before anything is touched: both arguments are mandatory."""
    if not plan_id:
        raise HiddenStretchLocked("hidden run needs --plan-id")
    if confirm != question["question_id"]:
        raise HiddenStretchLocked("hidden run needs --confirm-hidden equal to the question id")


def open_hidden_stretch(question: dict, plan_id: str, confirm, gate: HiddenGate, *, now_utc: str, persist):
    """Record the opening, make it durable with persist() (commit + push), then return the token.

    If persist raises, the caller aborts: no hidden label or bar has been read.
    """
    check_hidden_request(question, plan_id, confirm)
    token = gate.open_hidden(question["question_id"], plan_id, now_utc=now_utc)
    persist()
    return token


def run_question(question: dict, segment: str, bars_dir, label_dir, log, *, token=None, gate=None, now_utc: str,
                 code_commit: str, data_snapshot_id: str) -> dict:
    """Load the family's inputs one symbol at a time (released after its signals), then run_experiment once."""
    validate_question(question)
    if segment not in SEGMENTS:
        raise ValueError(f"segment must be one of {SEGMENTS}")
    question_id = question["question_id"]
    strategies = question["strategies"]
    if segment == "hidden":
        if token is None or gate is None or token.question_id != question_id or not gate.verify(token):
            raise HiddenStretchLocked("hidden run needs a verified opening token for this question")
        plan = gate.plan_for(token)
        if plan.data_snapshot_id != data_snapshot_id:
            raise HiddenGuardError("label data differ from the data snapshot the plan was registered on")
        strategies = [item["strategy_id"] for item in plan.strategies]
    params = LabelParams()
    geometries = build_geometries(question, params)
    last_month = segment_months(segment)[-1]
    family = family_of(question)
    specs = build_specs(question, segment, lambda symbol: family.load(bars_dir, symbol, last_month,
                                                                      question["horizon_min"], token, gate),
                        strategies, params)
    return run_experiment(question_id, specs, geometries, segment, label_dir, params, log, token=token, gate=gate,
                          B_stats=question["B_stats"], B_placebo=question["B_placebo"], seed=question["seed"],
                          code_commit=code_commit, data_snapshot_id=data_snapshot_id, now_utc=now_utc)


COUNT_SEGMENTS = ("development", "validation")


def count_signals(specs, segment: str, symbols=data_lake.SYMBOLS) -> dict:
    """Outcome-blind counts per strategy and symbol: signals, per day, long/short, first/last UTC date."""
    first_ms, end_ms = segment_bounds_ms(segment)
    days = (end_ms - first_ms) // 86_400_000

    def block(rows):
        times = [row[1] for row in rows]
        return {"signals": len(rows), "per_day": decimal_text(Fraction(len(rows), days), 3),
                "long": sum(1 for row in rows if row[2] == 1), "short": sum(1 for row in rows if row[2] == -1),
                "first": _utc_date(min(times)) if times else None, "last": _utc_date(max(times)) if times else None}

    strategies = []
    for spec in specs:
        if any(not first_ms <= row[1] < end_ms for row in spec.signals):
            raise ValueError(f"{spec.strategy_id}: signal outside the {segment} segment")
        strategies.append({"strategy_id": spec.strategy_id, "strategy_version": spec.strategy_version,
                           "symbols": {symbol: block([row for row in spec.signals if row[0] == symbol])
                                       for symbol in symbols},
                           "total": block(list(spec.signals))})
    return {"segment": segment, "days": days, "symbols": list(symbols), "strategies": strategies,
            "total": block([row for spec in specs for row in spec.signals])}


def _utc_date(ms: int) -> str:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def signal_counts(question: dict, segment: str, bars_dir, params: LabelParams | None = None) -> dict:
    """Build the question's signals for a development/validation segment exactly as run_question does.

    Reads only the family's guarded inputs (no token: hidden months stay locked);
    never labels, outcomes, the ledger, plans or the opening log. Hidden is refused.
    """
    validate_question(question)
    if segment not in COUNT_SEGMENTS:
        raise HiddenStretchLocked("signal counts are for development and validation only")
    family, last_month = family_of(question), segment_months(segment)[-1]
    specs = build_specs(question, segment, lambda symbol: family.load(bars_dir, symbol, last_month,
                                                                      question["horizon_min"], None, None),
                        None, params or LabelParams())
    return {"question_id": question["question_id"], "family": family.family_id, **count_signals(specs, segment)}


COUNTS_SCHEMA = "counts-v1"


def count_public_lines(counts: dict) -> list[str]:
    """Public totals only: no strategy names, per-symbol rows, sides or dates."""
    return [f"question {counts['question_id']} segment {counts['segment']}: outcome-blind signal counts",
            f"symbols {len(counts['symbols'])}, days {counts['days']}, total signals {counts['total']['signals']}"]


def counts_file_bytes(existing: bytes | None, counts: dict, *, code_commit: str, now_utc: str) -> bytes:
    """Private counts/<question>.json: the full per-strategy table per segment (a rerun replaces its segment)."""
    record = {"schema": COUNTS_SCHEMA, "question_id": counts["question_id"], "family": counts["family"],
              "segments": {}}
    if existing is not None:
        record = json.loads(existing)
        if (record.get("schema") != COUNTS_SCHEMA or record.get("question_id") != counts["question_id"]
                or record.get("family") != counts["family"]):
            raise QuestionError("existing counts file belongs to another question or schema")
    record["segments"][counts["segment"]] = {"code_commit": code_commit, "created_utc": now_utc, "counts": counts}
    return canonical_bytes(record)


VERDICTS = ("PASS", "FRAGILE", "FAIL", "NOT_ENOUGH_EVIDENCE")


def public_summary(report: dict) -> list[str]:
    """Public lines only: no means, t-statistics, strategy names, symbols or per-variant trade counts."""
    counts = {name: 0 for name in VERDICTS}
    for variant in report["variants"]:
        counts[variant["verdict"]] += 1
    detectable = sum(1 for variant in report["variants"] if variant.get("projection", {}).get("detectable"))
    projected = any("projection" in variant for variant in report["variants"])
    lines = [f"question {report['question_id']} segment {report['segment']}",
             f"K {report['K']}, n_trials {report['n_trials']}, required t {report['required_t']}",
             "verdicts: " + ", ".join(f"{name} {counts[name]}" for name in VERDICTS)]
    if projected:
        lines.append(f"variants detectable on the hidden stretch (projection): {detectable}")
    lines.append(f"report hash {report['report_hash']}")
    return lines


def report_paths(question_id: str, segment: str, report: dict) -> tuple[str, str]:
    stem = f"reports/{question_id}__{segment}__{report['report_hash'][:16]}"
    return stem + ".json", stem + ".md"


def segment_reports(reports_dir, question_id: str, segment: str) -> list[Path]:
    return sorted(Path(reports_dir).glob(f"{question_id}__{segment}__*.json"))


def snapshot_from_reports(reports_dir, question_id: str) -> str:
    """The data_snapshot_id shared by the question's development and validation reports."""
    snapshots = set()
    for segment in ("development", "validation"):
        paths = segment_reports(reports_dir, question_id, segment)
        if not paths:
            raise QuestionError(f"no {segment} report for {question_id}: plan only after development and validation")
        snapshots |= {json.loads(path.read_bytes())["data_snapshot_id"] for path in paths}
    if len(snapshots) != 1:
        raise QuestionError("development/validation reports were produced on different data snapshots")
    return snapshots.pop()
