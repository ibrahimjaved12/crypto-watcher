"""Question schema, geometries, data snapshot, hidden ordering and public-output redaction (no network)."""
from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from market_analysis.benchmark import experiment_run as er
from market_analysis.benchmark.canonical import canonical_bytes
from market_analysis.benchmark.hidden_guard import HiddenGate, HiddenGuardError, HiddenStretchLocked, write_plan
from market_analysis.benchmark.labels import LabelParams
from market_analysis.benchmark.ta_strategies import STRATEGIES, strategy_config

NOW = "2026-10-09T12:00:00Z"


def question(**changes):
    values = dict(question_id="ta-baselines-v1-60m", hypothesis="Classic TA baselines beat costs", horizon_min=60,
                  strategies=list(STRATEGIES), seed=20261009, label_revision=2, data_revision=1, created_utc=NOW)
    values.update(changes)
    return er.make_question(values.pop("question_id"), values.pop("hypothesis"), values.pop("horizon_min"),
                            values.pop("strategies"), **values)


class QuestionTests(unittest.TestCase):
    def test_validation(self):
        valid = question()
        self.assertEqual(json.loads(er.question_bytes(valid)), valid)
        self.assertEqual(len(er.question_hash(valid)), 64)
        for changes in ({"extra": 1}, {"B_stats": 1999}, {"B_placebo": 199}, {"strategies": ["sma_magic"]},
                        {"strategies": []}, {"horizon_min": 30}, {"k_values": ["1"]}, {"question_id": "Bad/Id"},
                        {"seed": -1}, {"created_utc": "2026-10-09"}):
            with self.subTest(changes=changes), self.assertRaises(er.QuestionError):
                er.validate_question({**valid, **changes})
        missing = dict(valid)
        del missing["seed"]
        with self.assertRaises(er.QuestionError):
            er.validate_question(missing)

    def test_geometries_one_horizon_96(self):
        geometries = er.build_geometries(question())
        self.assertEqual(len(geometries), 6 * 2 * 2 * 4)
        self.assertEqual({g.horizon_min for g in geometries}, {60})
        self.assertEqual(len(set(geometries)), 96)

    def test_plan_for_finalists(self):
        plan = er.build_plan(question(), ["rsi_14_reversion", "macd_12_26_9"], NOW, "snapshot-1")
        self.assertEqual(plan.variants, 16)
        self.assertEqual(plan.threshold, "3")
        self.assertEqual(plan.strategies[0]["config"], strategy_config("rsi_14_reversion", 60))
        with self.assertRaises(er.QuestionError):
            er.build_plan(question(strategies=["rsi_14_reversion"]), ["macd_12_26_9"], NOW, "snapshot-1")


def write_labels(root, symbol, months, rd_tags, body=b"labels"):
    params = LabelParams()
    outputs = []
    for month in months:
        name = f"labels__{symbol}__{month}.csv.gz"
        (root / name).write_bytes(body + month.encode())
        outputs.append({"month": month, "name": name, "sha256": hashlib.sha256(body + month.encode()).hexdigest()})
    manifest = {"symbol": symbol, "params_identity": params.identity(),
                "cost_model_identity": params.cost_model.identity(), "outputs": outputs, "rd_tags": rd_tags}
    (root / er.label_manifest_name(symbol)).write_bytes(canonical_bytes(manifest))


class SnapshotTests(unittest.TestCase):
    def test_stable_ordering_and_sensitivity(self):
        months = ["2025-07", "2025-08"]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_labels(root, "BTCUSDT", months, ["rd-BTCUSDT-2025-07-r1", "rd-BTCUSDT-2025-08-r1"])
            write_labels(root, "ETHUSDT", months, ["rd-ETHUSDT-2025-07-r1"])
            first = er.data_snapshot(root, months, symbols=["BTCUSDT", "ETHUSDT"])
            self.assertEqual(first, er.data_snapshot(root, list(reversed(months)), symbols=["ETHUSDT", "BTCUSDT"]))
            write_labels(root, "BTCUSDT", months, ["rd-BTCUSDT-2025-08-r1", "rd-BTCUSDT-2025-07-r1"])
            self.assertNotEqual(first, er.data_snapshot(root, months, symbols=["BTCUSDT", "ETHUSDT"]))
            (root / "labels__ETHUSDT__2025-08.csv.gz").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "sha256"):
                er.data_snapshot(root, months, symbols=["BTCUSDT", "ETHUSDT"])
            with self.assertRaisesRegex(ValueError, "no label output"):
                er.data_snapshot(root, ["2025-09"], symbols=["BTCUSDT"])


class HiddenTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "plans").mkdir()
        self.gate = HiddenGate(self.root / "plans", self.root / "opens.jsonl")
        self.question = question()
        self.plan = er.build_plan(self.question, ["rsi_14_reversion"], NOW, "snapshot-1")
        write_plan(self.root / "plans", self.plan)

    def run_hidden(self, token, snapshot="snapshot-1"):
        return er.run_question(self.question, "hidden", self.root / "bars", self.root / "labels", None,
                               token=token, gate=self.gate, now_utc=NOW, code_commit="c",
                               data_snapshot_id=snapshot)

    def test_refusals_before_anything(self):
        with self.assertRaises(HiddenStretchLocked):
            er.check_hidden_request(self.question, "", self.question["question_id"])
        with self.assertRaises(HiddenStretchLocked):
            er.check_hidden_request(self.question, self.plan.plan_id, "ta-baselines-v1-15m")
        with self.assertRaises(HiddenStretchLocked):
            er.open_hidden_stretch(self.question, self.plan.plan_id, "wrong", self.gate, now_utc=NOW,
                                   persist=lambda: None)
        self.assertFalse((self.root / "opens.jsonl").exists())

    def test_token_for_other_question_and_snapshot_mismatch_refused_without_reads(self):
        other = er.build_plan(question(question_id="ta-baselines-v1-15m", horizon_min=15),
                              ["rsi_14_reversion"], NOW, "snapshot-1")
        write_plan(self.root / "plans", other)
        token = self.gate.open_hidden(other.question_id, other.plan_id, now_utc=NOW)
        reads = []
        with patch.object(er, "load_symbol_candles", lambda *a, **k: reads.append(a)):
            with self.assertRaises(HiddenStretchLocked):
                self.run_hidden(token)
            with self.assertRaises(HiddenStretchLocked):
                self.run_hidden(None)
            mine = self.gate.open_hidden(self.question["question_id"], self.plan.plan_id, now_utc=NOW)
            with self.assertRaises(HiddenGuardError):
                self.run_hidden(mine, snapshot="snapshot-2")
        self.assertEqual(reads, [])

    def test_opening_is_persisted_before_any_read(self):
        events = []

        def persist():
            records = self.gate.read()
            events.append(("persist", records[-1]["question_id"]))

        def read(*args, **kwargs):
            events.append(("read", args[1]))
            raise OSError("data dir unavailable")

        token = er.open_hidden_stretch(self.question, self.plan.plan_id, self.question["question_id"], self.gate,
                                       now_utc=NOW, persist=persist)
        with patch.object(er, "load_symbol_candles", read), self.assertRaises(OSError):
            self.run_hidden(token)
        self.assertEqual(events, [("persist", self.question["question_id"]), ("read", "BTCUSDT")])

    def test_failed_persist_returns_no_token(self):
        def persist():
            raise RuntimeError("push rejected")

        with self.assertRaises(RuntimeError):
            er.open_hidden_stretch(self.question, self.plan.plan_id, self.question["question_id"], self.gate,
                                   now_utc=NOW, persist=persist)


class PublicOutputTests(unittest.TestCase):
    def test_redaction(self):
        variants = []
        for index, name in enumerate(STRATEGIES):
            variants.append({"strategy_id": name, "verdict": ("PASS", "FAIL")[index % 2],
                             "metrics": {"mean_net_r": "0.123456", "trades": 4321},
                             "bootstrap_ci": {"t_statistic": "4.56789"},
                             "projection": {"detectable": index < 2, "mde_per_trade_r": "0.0777"}})
        report = {"question_id": "ta-baselines-v1-60m", "segment": "development", "K": 48, "n_trials": 48,
                  "required_t": "3.0", "report_hash": "f" * 64, "variants": variants}
        text = "\n".join(er.public_summary(report))
        for secret in (*STRATEGIES, "mean", "0.123456", "4.56789", "4321", "0.0777", "BTCUSDT"):
            self.assertNotIn(secret, text)
        self.assertIn("PASS 3, FRAGILE 0, FAIL 3, NOT_ENOUGH_EVIDENCE 0", text)
        self.assertIn("projection): 2", text)
        self.assertIn("f" * 64, text)

    def test_plan_needs_development_and_validation_reports(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "q__development__abc.json").write_text(json.dumps({"data_snapshot_id": "s1"}))
            with self.assertRaises(er.QuestionError):
                er.snapshot_from_reports(root, "q")
            (root / "q__validation__def.json").write_text(json.dumps({"data_snapshot_id": "s1"}))
            self.assertEqual(er.snapshot_from_reports(root, "q"), "s1")
            (root / "q__validation__xyz.json").write_text(json.dumps({"data_snapshot_id": "s2"}))
            with self.assertRaises(er.QuestionError):
                er.snapshot_from_reports(root, "q")


def load_script():
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
    definition = importlib.util.spec_from_file_location("test_experiment_run_script", scripts / "experiment_run.py")
    module = importlib.util.module_from_spec(definition)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(scripts))
        definition.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


SECRET_HYPOTHESIS = "Private idea: momentum after funding spikes"


class DraftTests(unittest.TestCase):
    def test_finalists_parsing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "q.finalists.txt"
            path.write_text("# finalists\n\nrsi_14_reversion  # strongest\n  macd_12_26_9\n\n", encoding="utf-8")
            self.assertEqual(er.read_finalists(root, "q"), ["rsi_14_reversion", "macd_12_26_9"])
            for text in ("rsi_14_reversion\nrsi_14_reversion\n", "sma_magic\n", "# only a comment\n"):
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(er.QuestionError):
                    er.read_finalists(root, "q")

    def test_hypothesis_and_missing_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for reader, name in ((er.read_hypothesis, "q.hypothesis.txt"), (er.read_finalists, "q.finalists.txt")):
                with self.assertRaisesRegex(er.QuestionError,
                                            f"write drafts/{name} in the research-data repo first"):
                    reader(root, "q")
            (root / "q.hypothesis.txt").write_text("  \n", encoding="utf-8")
            with self.assertRaisesRegex(er.QuestionError, "write drafts/q.hypothesis.txt"):
                er.read_hypothesis(root, "q")
            (root / "q.hypothesis.txt").write_text(f"\n {SECRET_HYPOTHESIS} \n", encoding="utf-8")
            self.assertEqual(er.read_hypothesis(root, "q"), SECRET_HYPOTHESIS)
            (root / "q.hypothesis.txt").write_text("x" * 2001, encoding="utf-8")
            with self.assertRaises(er.QuestionError):
                er.read_hypothesis(root, "q")


class FakeCheckout:
    def __init__(self, path):
        self.path, self.commits = path, []

    def commit_and_push(self, paths, message):
        self.commits.append((list(paths), message))


class ScriptDraftTests(unittest.TestCase):
    def test_register_and_plan_lines_and_commits_carry_no_draft_content(self):
        module = load_script()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = FakeCheckout(root)
            qid = "ta-baselines-v1-60m"
            args = module.parse_args(["register", "--question-id", qid, "--horizon", "60", "--label-revision", "2",
                                      "--data-revision", "1", "--seed", "7"])
            with self.assertRaises(module.PublicError) as caught:
                module.register(args, checkout)
            self.assertIn(f"write drafts/{qid}.hypothesis.txt", str(caught.exception))
            (root / "drafts").mkdir()
            (root / "drafts" / f"{qid}.hypothesis.txt").write_text(SECRET_HYPOTHESIS, encoding="utf-8")
            (root / "drafts" / f"{qid}.finalists.txt").write_text("rsi_14_reversion\nmacd_12_26_9\n", encoding="utf-8")
            lines = module.register(args, checkout)
            (root / "reports").mkdir()
            for segment in ("development", "validation"):
                (root / "reports" / f"{qid}__{segment}__0.json").write_text(json.dumps({"data_snapshot_id": "s"}))
            lines += module.plan(module.parse_args(["plan", "--question-id", qid]), checkout)
            public = "\n".join(lines + [message for _, message in checkout.commits])
            for secret in (SECRET_HYPOTHESIS, "momentum", *STRATEGIES):
                self.assertNotIn(secret, public)
            committed = [path for paths, _ in checkout.commits for path in paths]
            self.assertEqual(len(committed), 2)
            self.assertFalse(any(path.startswith("drafts") for path in committed))
            stored = json.loads((root / "questions" / f"{qid}.json").read_bytes())
            self.assertEqual(stored["hypothesis"], SECRET_HYPOTHESIS)


class ScriptArgumentTests(unittest.TestCase):
    def test_hidden_run_refused_by_arguments(self):
        module = load_script()
        for argv in (["run", "--question-id", "q", "--segment", "hidden"],
                     ["run", "--question-id", "q", "--segment", "hidden", "--plan-id", "a" * 64,
                      "--confirm-hidden", "other"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                module.parse_args(argv)
        args = module.parse_args(["run", "--question-id", "q", "--segment", "hidden", "--plan-id", "a" * 64,
                                  "--confirm-hidden", "q"])
        self.assertEqual(args.segment, "hidden")


if __name__ == "__main__":
    unittest.main()
