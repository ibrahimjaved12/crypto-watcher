"""Question schema, geometries, data snapshot, hidden ordering and public-output redaction (no network)."""
from contextlib import redirect_stderr
from fractions import Fraction
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from market_analysis.benchmark import experiment_run as er, order_flow
from market_analysis.benchmark.canonical import canonical_bytes
from market_analysis.benchmark.evaluate import decimal_text
from market_analysis.benchmark.hidden_guard import HiddenGate, HiddenGuardError, HiddenStretchLocked, write_plan
from market_analysis.benchmark.labels import LabelParams
from market_analysis.benchmark.runner import StrategySpec
from market_analysis.benchmark.segments import segment_bounds_ms
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


OF_STRATEGIES = list(order_flow.STRATEGIES)


def of_question(**changes):
    values = dict(question_id="ord-flow-v1-240m", hypothesis="Taker imbalance continues", horizon_min=240,
                  strategies=OF_STRATEGIES, seed=20261009, label_revision=3, data_revision=1, created_utc=NOW,
                  family="order-flow")
    values.update(changes)
    return er.make_question(values.pop("question_id"), values.pop("hypothesis"), values.pop("horizon_min"),
                            values.pop("strategies"), **values)


class FamilyRegistryTests(unittest.TestCase):
    def test_existing_ta_question_bytes_and_hash_unchanged(self):
        # Pinned from the pre-registry code (slice G): registered ta-baselines questions keep their hash.
        valid = question()
        self.assertEqual(er.question_hash(valid), "8ba062bb71031cddc4b34a43a7648de16e21bbd974663d333af165f9460d707d")
        self.assertEqual((valid["family"], valid["strategy_version"], valid["k_values"], valid["rr_indices"]),
                         ("ta-baselines", "ta-v1", ["1", "2"], [0, 1, 2, 3]))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "q.json"
            path.write_bytes(er.question_bytes(valid))
            self.assertEqual(er.load_question(path), valid)

    def test_order_flow_question_geometry_and_plan(self):
        valid = of_question()
        self.assertEqual((valid["strategy_version"], valid["k_values"], valid["rr_indices"]), ("of-v1", ["2"], [1]))
        geometries = er.build_geometries(valid)
        self.assertEqual(len(geometries), 6 * 2)
        self.assertEqual({(g.horizon_min, g.k, g.rr_index) for g in geometries}, {(240, 2, 1)})
        plan = er.build_plan(valid, ["of_cum240_4h"], NOW, "snapshot-1")
        self.assertEqual(plan.variants, 1)
        self.assertEqual(plan.strategies, [{"strategy_id": "of_cum240_4h", "strategy_version": "of-v1",
                                            "config": order_flow.strategy_config("of_cum240_4h", 240)}])

    def test_family_entry_is_enforced(self):
        valid = of_question()
        for changes in ({"family": "nope"}, {"family": 1}, {"horizon_min": 60}, {"strategies": ["rsi_14_reversion"]},
                        {"k_values": ["1", "2"]}, {"rr_indices": [0, 1, 2, 3]}, {"strategy_version": "ta-v1"}):
            with self.subTest(changes=changes), self.assertRaises(er.QuestionError):
                er.validate_question({**valid, **changes})
        with self.assertRaises(er.QuestionError):
            question(strategies=OF_STRATEGIES)
        with self.assertRaises(er.QuestionError):
            er.build_plan(valid, ["rsi_14_reversion"], NOW, "snapshot-1")

    def test_order_flow_specs_load_one_symbol_at_a_time(self):
        loads = []

        def load(symbol):
            loads.append(symbol)
            return ("bars", symbol), ("funding", symbol)

        def signals(name, bars, funding, horizon, *, first_ms, end_ms, label_step_min):
            self.assertEqual((bars[1], funding[1], horizon, label_step_min), (loads[-1], loads[-1], 240, 15))
            return [(loads[-1], first_ms + 15 * 60_000, 1, horizon)] if name == "of_toh1m_4h" else []

        with patch.object(order_flow, "symbol_signals", signals):
            specs = er.build_specs(of_question(), "development", load)
        self.assertEqual(loads, list(er.data_lake.SYMBOLS))
        self.assertEqual([(spec.strategy_id, spec.strategy_version, len(spec.signals)) for spec in specs],
                         [("of_toh1m_4h", "of-v1", 6), ("of_cum240_4h", "of-v1", 0)])
        self.assertEqual(specs[0].config, order_flow.strategy_config("of_toh1m_4h", 240))


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
            self.assertEqual(er.read_finalists(root, "q", "ta-baselines"), ["rsi_14_reversion", "macd_12_26_9"])
            for text in ("rsi_14_reversion\nrsi_14_reversion\n", "sma_magic\n", "# only a comment\n"):
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(er.QuestionError):
                    er.read_finalists(root, "q", "ta-baselines")

    def test_finalists_restricted_to_the_question_family(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "q.finalists.txt").write_text("of_cum240_4h\n", encoding="utf-8")
            self.assertEqual(er.read_finalists(root, "q", "order-flow"), ["of_cum240_4h"])
            with self.assertRaisesRegex(er.QuestionError, "outside the ta-baselines family"):
                er.read_finalists(root, "q", "ta-baselines")
            (root / "q.finalists.txt").write_text("rsi_14_reversion\n", encoding="utf-8")
            with self.assertRaisesRegex(er.QuestionError, "outside the order-flow family"):
                er.read_finalists(root, "q", "order-flow")
            with self.assertRaises(er.QuestionError):
                er.read_finalists(root, "q", "nope")

    def test_hypothesis_and_missing_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for reader, name in ((er.read_hypothesis, "q.hypothesis.txt"),
                                 (lambda path, qid: er.read_finalists(path, qid, "ta-baselines"), "q.finalists.txt")):
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


class SignalCountTests(unittest.TestCase):
    def specs(self):
        first, end = segment_bounds_ms("development")
        minute, day = 60_000, 86_400_000
        toh = StrategySpec("of_toh1m_4h", "of-v1", {}, [
            ("BTCUSDT", first + 15 * minute, 1, 240), ("BTCUSDT", first + day + 15 * minute, -1, 240),
            ("BTCUSDT", first + 2 * day, 1, 240), ("ETHUSDT", end - 15 * minute, -1, 240)])
        return [toh, StrategySpec("of_cum240_4h", "of-v1", {}, [])]

    def test_counts_dates_and_table(self):
        counts = er.count_signals(self.specs(), "development")
        days = counts["days"]
        self.assertEqual(days, 547)  # 2024-01-01 .. 2025-06-30
        toh, cum = counts["strategies"]
        self.assertEqual(toh["symbols"]["BTCUSDT"], {"signals": 3, "per_day": decimal_text(Fraction(3, days), 3),
                                                     "long": 2, "short": 1, "first": "2024-01-01",
                                                     "last": "2024-01-03"})
        self.assertEqual((toh["symbols"]["ETHUSDT"]["last"], toh["symbols"]["SOLUSDT"]["signals"]), ("2025-06-30", 0))
        self.assertEqual((toh["total"]["signals"], toh["total"]["long"], toh["total"]["short"]), (4, 2, 2))
        self.assertEqual((cum["total"]["signals"], cum["total"]["first"]), (0, None))
        self.assertEqual(counts["total"]["signals"], 4)
        self.assertEqual(counts["symbols"], list(er.data_lake.SYMBOLS))
        lines = er.count_public_lines({"question_id": "ord-flow-v1-240m", "family": "order-flow", **counts})
        self.assertEqual(lines, ["question ord-flow-v1-240m segment development: outcome-blind signal counts",
                                 "symbols 6, days 547, total signals 4"])
        public = "\n".join(lines)
        for secret in ("of_toh1m_4h", "of_cum240_4h", "BTCUSDT", "ETHUSDT", "2024-01-01", "long", "short"):
            self.assertNotIn(secret, public)

    def test_private_counts_file_keeps_one_entry_per_segment(self):
        development = {"question_id": "q", "family": "order-flow", **er.count_signals(self.specs(), "development")}
        validation = {"question_id": "q", "family": "order-flow",
                      **er.count_signals(self.specs_for_validation(), "validation")}
        first = er.counts_file_bytes(None, development, code_commit="c1", now_utc=NOW)
        both = er.counts_file_bytes(first, validation, code_commit="c2", now_utc=NOW)
        record = json.loads(both)
        self.assertEqual((record["schema"], record["question_id"], record["family"]), ("counts-v1", "q", "order-flow"))
        self.assertEqual(sorted(record["segments"]), ["development", "validation"])
        self.assertEqual(record["segments"]["development"]["counts"], json.loads(canonical_bytes(development)))
        self.assertEqual(record["segments"]["validation"]["code_commit"], "c2")
        rerun = json.loads(er.counts_file_bytes(both, development, code_commit="c3", now_utc=NOW))
        self.assertEqual((rerun["segments"]["development"]["code_commit"], sorted(rerun["segments"])),
                         ("c3", ["development", "validation"]))
        with self.assertRaises(er.QuestionError):
            er.counts_file_bytes(both, {**development, "question_id": "other"}, code_commit="c", now_utc=NOW)
        outside = [StrategySpec("x", "of-v1", {}, [("BTCUSDT", segment_bounds_ms("validation")[0], 1, 240)])]
        with self.assertRaises(ValueError):
            er.count_signals(outside, "development")

    def test_hidden_refused_before_any_read(self):
        reads = []
        record = lambda *a, **k: reads.append(a)  # noqa: E731
        with patch.object(er, "load_symbol_bars", record), patch.object(er, "load_symbol_funding", record), \
                patch.object(er, "load_symbol_candles", record):
            for q in (of_question(), question()):
                with self.subTest(family=q["family"]), self.assertRaises(HiddenStretchLocked):
                    er.signal_counts(q, "hidden", "bars")
        self.assertEqual(reads, [])

    def test_specs_built_as_run_with_unprivileged_guarded_loads(self):
        loads = []

        def load(*args, token=None, gate=None):
            loads.append((args[1], args[2], args[3], token, gate))
            return args[1]

        def build(q, segment, load_inputs, strategies, params, progress=None):
            self.assertEqual((segment, strategies), ("validation", None))
            load_inputs("BTCUSDT")
            return self.specs_for_validation()

        with patch.object(er, "load_symbol_bars", load), patch.object(er, "load_symbol_funding", load), \
                patch.object(er, "build_specs", build):
            counts = er.signal_counts(of_question(), "validation", "bars")
        self.assertEqual(loads, [("BTCUSDT", "2024-01", "2025-12", None, None)] * 2)
        self.assertEqual((counts["question_id"], counts["family"], counts["total"]["signals"]),
                         ("ord-flow-v1-240m", "order-flow", 1))

    def specs_for_validation(self):
        first, _ = segment_bounds_ms("validation")
        return [StrategySpec("of_toh1m_4h", "of-v1", {}, [("XRPUSDT", first, 1, 240)])]

    def test_script_count_reads_no_labels_and_commits_only_the_private_counts(self):
        module = load_script()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = FakeCheckout(root)
            (root / "questions").mkdir()
            q = of_question()
            (root / "questions" / f"{q['question_id']}.json").write_bytes(er.question_bytes(q))
            calls = []

            def no_labels(*args, **kwargs):
                raise AssertionError("count must not download labels")

            def bars(repo, question_, segment, bars_dir, label_dir, progress=None):
                calls.append((segment, label_dir))

            counts = {"question_id": q["question_id"], "family": "order-flow",
                      **er.count_signals(self.specs(), "development")}
            args = module.parse_args(["count", "--question-id", q["question_id"], "--segment", "development"])
            with patch.object(module, "download_labels", no_labels), patch.object(module, "download_bars", bars), \
                    patch.object(er, "signal_counts", lambda *a, **k: counts):
                lines = module.count(args, checkout, None, root / "data")
            self.assertEqual(calls, [("development", None)])
            name = f"counts/{q['question_id']}.json"
            self.assertEqual(checkout.commits, [([name], f"Signal counts for {q['question_id']} on development")])
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["counts", "data", "questions"])
            stored = json.loads((root / name).read_bytes())
            self.assertEqual(stored["segments"]["development"]["counts"], json.loads(canonical_bytes(counts)))
            self.assertEqual(lines, er.count_public_lines(counts))
            public = "\n".join(lines + [message for _, message in checkout.commits])
            for secret in ("of_toh1m_4h", "of_cum240_4h", "BTCUSDT", "2024-01-01"):
                self.assertNotIn(secret, public)
            for argv in (["count", "--question-id", "q", "--segment", "hidden"], ["count", "--question-id", "q"]):
                with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    module.parse_args(argv)
            missing = module.parse_args(["count", "--question-id", "unregistered", "--segment", "validation"])
            with self.assertRaises(module.PublicError):
                module.count(missing, checkout, None, root / "data2")


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
