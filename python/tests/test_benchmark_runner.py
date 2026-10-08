"""Synthetic planted edge/noise experiments; no private data or network calls."""
from dataclasses import replace
from fractions import Fraction
import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from market_analysis.benchmark.canonical import canonical_bytes
from market_analysis.benchmark.evaluate import Selection, Trade
from market_analysis.benchmark.experiment_log import ExperimentLog, TrialStatus
from market_analysis.benchmark.hidden_guard import (HiddenGate, HiddenGuardError, HiddenStretchLocked, Plan,
                                                    write_plan)
from market_analysis.benchmark.label_store import Geometry
from market_analysis.benchmark.labels import LabelParams
from market_analysis.benchmark.report import canonical_json
from market_analysis.benchmark.rng import u64_words
from market_analysis.benchmark.runner import StrategySpec, required_t, run_experiment, verdict
from market_analysis.benchmark.segments import segment_bounds_ms, segment_months
from market_analysis.data_lake import month_bounds_ms

G = Geometry("BTCUSDT", 15, 1, Fraction(1), 0)
PARAMS = LabelParams(horizons=(15,), half_life_days=((15, 1),), step_minutes=((15, 5),),
                     k_grid=(Fraction(1),), rr_grid=(Fraction(1),))
NOW = "2026-10-08T12:00:00Z"


def make_labels(root, drift=False, segment="validation"):
    """Hour 0 has four signals a day; only minute 0 carries the planted edge.

    The planted strategy picks minute 0 out of its hour-of-week placebo cell, so
    it must beat the matched placebo. With drift=True every hour-0 long earns the
    edge (a rising market) and the "drift" strategy simply takes all of them.
    """
    first, end = segment_bounds_ms(segment)
    days = (end - first) // 86_400_000
    # Symmetric, nonconstant noise: exact zero mean, order from the counter RNG.
    # (Exact zero mean for an even day count; the 273-day hidden stretch is odd.)
    words = u64_words(21, "fixture", 0, (days + 1) // 2)
    half = [10_000 + int(word % 20_000) for word in words]
    noise = half + [-value for value in reversed(half)]
    signals = {"planted": [], "noise": [], "drift": []}
    for month in segment_months(segment):
        start, finish, _ = month_bounds_ms(month)
        lines = [",".join(PARAMS.header)]
        for day_ms in range(start, finish, 86_400_000):
            day = (day_ms - first) // 86_400_000
            rows = [(0, 0, "planted", 500_000 + noise[day]),
                    (0, 15, None, 500_000 + noise[day] if drift else noise[day]),
                    (0, 30, None, 500_000 - noise[day] if drift else -noise[day]),
                    (0, 45, None, 500_000 + noise[(day + 1) % days] if drift else noise[(day + 1) % days]),
                    (1, 0, "noise", noise[day])]
            for hour, minute, name, net in rows:
                ms = day_ms + hour * 3_600_000 + minute * 60_000
                if name is not None:
                    signals[name].append(("BTCUSDT", ms, 1, 15))
                if hour == 0:
                    signals["drift"].append(("BTCUSDT", ms, 1, 15))
                for side in (1, -1):
                    actual = net if side == 1 else -net
                    outcome = "T" if actual > 0 else "S"
                    lines.append(f"{ms},15,{side},1,T,1000000000,100,10,2,2000000,"
                                 f"{outcome}:5:{actual}:1000:0")
        with gzip.open(root / f"labels__BTCUSDT__{month}.csv.gz", "wt", encoding="ascii") as stream:
            stream.write("\n".join(lines) + "\n")
    manifest = {"schema": PARAMS.schema, "symbol": "BTCUSDT", "params_identity": PARAMS.identity(),
                "ticks": {month: 100 for month in segment_months(segment)}}
    (root / "labels__BTCUSDT.manifest.json").write_bytes(canonical_bytes(manifest))
    specs = [StrategySpec(name, "1", {}, rows) for name, rows in signals.items()]
    return specs[:2] if not drift else specs[2:]


def good(n, x=0):
    row = Trade(G, 0, "T", 1, 100, 1, 0, 100, 0, 100, 1, 0)
    return Selection([row] * (n - x) + [replace(row, outcome="X", net_ur=0, cost_ur=0)] * x, signals=n)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = ExperimentLog(self.root / "trials.jsonl")
        self.specs = make_labels(self.root)

    def run_experiment(self, **changes):
        args = dict(question_id="q-c2", specs=self.specs, geometries=[G], segment="validation", label_dir=self.root,
                    params=PARAMS, log=self.log, B_stats=80, B_placebo=99, seed=5, code_commit="abc123",
                    data_snapshot_id="synthetic-v1", now_utc=NOW)
        args.update(changes)
        return run_experiment(**args)

    def test_planted_pass_noise_does_not_and_one_final_append(self):
        with patch.object(self.log, "append_many", wraps=self.log.append_many) as append:
            report = self.run_experiment()
        append.assert_called_once()
        by_strategy = {row["strategy_id"]: row for row in report["variants"]}
        self.assertEqual(by_strategy["planted"]["verdict"], "PASS")
        self.assertNotEqual(by_strategy["noise"]["verdict"], "PASS")
        self.assertEqual(report["n_trials"], 2)
        records = self.log.read()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.status is TrialStatus.OK for _, record in records))
        self.assertTrue(all(record.count_reason == "variant evaluated" for _, record in records))
        self.assertEqual(self.log.trial_count(question_id="q-c2"), 2)
        for _, record in records:
            self.assertEqual(record.result_summary["verdict"], by_strategy[record.strategy_id]["verdict"])
            self.assertTrue(all(type(value) in (str, int) for value in record.result_summary.values()))
        canonical_json(report)  # rejects floats anywhere in the completed report

    def test_replay_determinism_and_historical_trial_count(self):
        first = self.run_experiment()
        replay = self.run_experiment(specs=list(reversed(self.specs)))
        self.assertEqual(first, replay)
        records = self.log.read()
        self.assertEqual(len(records), 4)
        self.assertTrue(all(record.status is TrialStatus.REPLAY for _, record in records[2:]))
        historical = replace(records[0][1], strategy_id="historical", created_utc=NOW)
        self.log.append(historical)
        larger = self.run_experiment()
        self.assertEqual(larger["n_trials"], 3)
        self.assertEqual(larger["best_trial_dsr"]["n_trials"], 3)

    def test_hidden_refused_before_read_or_log_mutation(self):
        with self.assertRaises(HiddenStretchLocked):
            self.run_experiment(segment="hidden", label_dir=self.root / "does-not-exist", B_stats=1000)
        self.assertFalse(self.log.path.exists())

    def test_single_variant_reports_dsr_unavailable(self):
        report = self.run_experiment(specs=self.specs[:1])
        self.assertEqual(report["K"], 1)
        self.assertIsNone(report["best_trial_dsr"]["dsr_raw"])
        self.assertEqual(report["best_trial_dsr"]["reason"], "need at least 2 non-constant trials")
        self.assertEqual(len(self.log.read()), 1)

    def test_missing_or_mismatched_tick_metadata_is_not_silently_approximated(self):
        path = self.root / "labels__BTCUSDT.manifest.json"
        data = json.loads(path.read_bytes())
        data["params_identity"] = "bad"
        path.write_bytes(canonical_bytes(data))
        with self.assertRaises(ValueError):
            self.run_experiment()
        self.assertFalse(self.log.path.exists())

    def test_planted_beats_placebo_and_report_headline(self):
        report = self.run_experiment()
        planted = next(row for row in report["variants"] if row["strategy_id"] == "planted")
        self.assertLessEqual(Fraction(planted["baselines"]["placebo"]["p_placebo"]), Fraction(1, 20))
        self.assertEqual((report["B_stats"], report["B_placebo"], report["plan_id"]), (80, 99, None))
        self.assertEqual(report["required_t"], repr(3.0))
        self.assertEqual(planted["required_t"], report["required_t"])
        heading = {row["variant_id"]: row for row in report["headline"]["variants"]}
        self.assertEqual(heading[planted["variant_id"]]["verdict"], "PASS")
        self.assertEqual(heading[planted["variant_id"]]["p_placebo"], planted["baselines"]["placebo"]["p_placebo"])

    def test_drift_strategy_does_not_pass_because_of_placebo(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            specs = make_labels(root, drift=True)
            report = self.run_experiment(specs=specs, label_dir=root, log=ExperimentLog(root / "trials.jsonl"))
        drift = report["variants"][0]
        # Everything except the placebo would say PASS: positive CI, StepM rejection, large t.
        self.assertTrue(drift["power"]["passes"])
        self.assertTrue(drift["stepm_rejected"])
        self.assertGreater(Fraction(drift["bootstrap_ci"]["lower"]), 0)
        self.assertGreaterEqual(Fraction(drift["bootstrap_ci"]["t_statistic"]), Fraction(drift["required_t"]))
        self.assertGreater(Fraction(drift["baselines"]["placebo"]["p_placebo"]), Fraction(1, 20))
        self.assertEqual(drift["verdict"], "FAIL")

    def test_required_t_grows_with_trial_history(self):
        self.assertEqual(required_t(1), 3.0)
        self.assertLess(required_t(1000), required_t(10_000))
        self.assertGreater(required_t(1000), 3.0)
        base = self.run_experiment()
        _, record = self.log.read()[0]
        self.log.append_many([replace(record, strategy_id=f"history-{i}") for i in range(300)])
        larger = self.run_experiment()
        self.assertEqual(larger["n_trials"], 302)
        self.assertEqual(larger["required_t"], repr(required_t(302)))
        self.assertGreater(Fraction(larger["required_t"]), Fraction(base["required_t"]))
        self.assertTrue(all(row["required_t"] == larger["required_t"] for row in larger["variants"]))

    def hidden_gate(self, *, question_id="q-c2", strategies=("planted", "noise"), variants=2, config=None):
        plans = self.root / "plans"
        plans.mkdir(exist_ok=True)
        gate = HiddenGate(plans, self.root / "opens.jsonl")
        plan = Plan(question_id=question_id, hypothesis="planted edge survives",
                    strategies=[{"strategy_id": name, "strategy_version": "1", "config": config or {}}
                                for name in strategies],
                    variants=variants, statistic="t", threshold="1/10", split_id="hidden",
                    data_snapshot_id="synthetic-v1", created_utc=NOW)
        write_plan(plans, plan)
        return gate, gate.open_hidden(question_id, plan.plan_id, now_utc=NOW)

    def assert_hidden_refused(self, error, gate, token, **changes):
        missing = self.root / "does-not-exist"
        with self.assertRaises(error):
            self.run_experiment(segment="hidden", label_dir=missing, token=token, gate=gate, B_stats=1000, **changes)
        self.assertFalse(self.log.path.exists())

    def test_hidden_token_bound_to_question_and_plan(self):
        gate, token = self.hidden_gate(question_id="q-other")
        self.assert_hidden_refused(HiddenStretchLocked, gate, token)
        gate, token = self.hidden_gate(variants=1)
        self.assert_hidden_refused(HiddenGuardError, gate, token)

    def test_hidden_strategy_outside_plan_refused(self):
        gate, token = self.hidden_gate(strategies=("planted",))
        self.assert_hidden_refused(HiddenGuardError, gate, token)

    def test_hidden_config_must_match_plan(self):
        gate, token = self.hidden_gate(config={"window": 20})
        self.assert_hidden_refused(HiddenGuardError, gate, token)

    def test_hidden_budget_counts_earlier_runs_but_not_replays(self):
        hidden = self.root / "hidden"
        hidden.mkdir()
        specs = make_labels(hidden, segment="hidden")
        gate, token = self.hidden_gate(variants=3)
        args = dict(specs=specs, segment="hidden", label_dir=hidden, token=token, gate=gate, B_stats=1000)
        first = self.run_experiment(**args)
        self.assertEqual(first["plan_id"], gate.plan_for(token).plan_id)
        replay = self.run_experiment(**args)  # same identities: REPLAY lines, budget unchanged
        self.assertEqual(first, replay)
        records = self.log.read()
        self.assertEqual([record.status for _, record in records],
                         [TrialStatus.OK, TrialStatus.OK, TrialStatus.REPLAY, TrialStatus.REPLAY])
        # Both sides in one group changes the universe, so two new identities: 2 + 2 > 3 in total,
        # although this run alone (K = 2) fits the plan.
        with self.assertRaisesRegex(HiddenGuardError, "distinct hidden variants"):
            self.run_experiment(**{**args, "geometries": [G, G._replace(side=-1)],
                                   "label_dir": self.root / "does-not-exist"})
        self.assertEqual(len(self.log.read()), 4)

    def test_hidden_needs_large_b_stats(self):
        gate, token = self.hidden_gate()
        with self.assertRaisesRegex(ValueError, "B_stats"):
            self.run_experiment(segment="hidden", label_dir=self.root / "does-not-exist", token=token, gate=gate,
                                B_stats=999)
        with self.assertRaisesRegex(ValueError, "B_placebo"):
            self.run_experiment(B_placebo=98)
        self.assertFalse(self.log.path.exists())

    def test_verdict_precedence(self):
        row = Trade(G, 0, "S", 1, 10, 20, 0, 100, 0, 10, 20, 0)
        chosen = Selection([row], signals=1)
        ci = {"lower": "1", "t_statistic": "4"}
        ok = dict(p_placebo="0.01", required_t=3.0)
        self.assertEqual(verdict(chosen, {"passes": False}, ci, True, **ok), "NOT_ENOUGH_EVIDENCE")
        self.assertEqual(verdict(chosen, {"passes": True}, ci, True, **ok), "FRAGILE")
        row = replace(row, net_ur=100, cost_ur=1, opt_net_ur=100, amb=1)
        self.assertEqual(verdict(Selection([row]), {"passes": True}, ci, True, **ok), "FRAGILE")
        row = replace(row, amb=0)
        self.assertEqual(verdict(Selection([row]), {"passes": True}, ci, False, **ok), "FAIL")
        self.assertEqual(verdict(Selection([row]), {"passes": True}, ci, True, **ok), "PASS")
        self.assertEqual(verdict(Selection([row]), {"passes": True}, ci, True,
                                 p_placebo="0.05", required_t="4"), "PASS")
        for changes in ({"p_placebo": "0.06"}, {"p_placebo": None}, {"required_t": 4.5}):
            with self.subTest(changes=changes):
                self.assertEqual(verdict(Selection([row]), {"passes": True}, ci, True, **{**ok, **changes}), "FAIL")

    def test_x_share_above_five_percent_is_fragile(self):
        ci = {"lower": "1", "t_statistic": "4"}
        ok = dict(p_placebo="0.01", required_t=3.0)
        self.assertEqual(verdict(good(20, x=1), {"passes": True}, ci, True, **ok), "PASS")
        self.assertEqual(verdict(good(20, x=2), {"passes": True}, ci, True, **ok), "FRAGILE")
        self.assertEqual(verdict(good(20, x=2), {"passes": False}, ci, True, **ok), "NOT_ENOUGH_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
