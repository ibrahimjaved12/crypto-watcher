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
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from market_analysis.benchmark.label_store import Geometry
from market_analysis.benchmark.labels import LabelParams
from market_analysis.benchmark.report import canonical_json
from market_analysis.benchmark.rng import u64_words
from market_analysis.benchmark.runner import StrategySpec, run_experiment, verdict
from market_analysis.benchmark.segments import segment_bounds_ms, segment_months
from market_analysis.data_lake import month_bounds_ms

G = Geometry("BTCUSDT", 15, 1, Fraction(1), 0)
PARAMS = LabelParams(horizons=(15,), half_life_days=((15, 1),), step_minutes=((15, 5),),
                     k_grid=(Fraction(1),), rr_grid=(Fraction(1),))
NOW = "2026-10-08T12:00:00Z"


def make_labels(root):
    first, end = segment_bounds_ms("validation")
    days = (end - first) // 86_400_000
    # Symmetric, nonconstant noise: exact zero mean, order from the counter RNG.
    words = u64_words(21, "fixture", 0, days // 2)
    half = [10_000 + int(word % 20_000) for word in words]
    noise = half + [-value for value in reversed(half)]
    signals = {"planted": [], "noise": []}
    for month in segment_months("validation"):
        start, finish, _ = month_bounds_ms(month)
        lines = [",".join(PARAMS.header)]
        for day_ms in range(start, finish, 86_400_000):
            day = (day_ms - first) // 86_400_000
            for hour, name in ((0, "planted"), (1, "noise")):
                ms = day_ms + hour * 3_600_000
                signals[name].append(("BTCUSDT", ms, 1, 15))
                net = 500_000 + noise[day] if name == "planted" else noise[day]
                for side in (1, -1):
                    actual = net if side == 1 else -net
                    outcome = "T" if actual > 0 else "S"
                    lines.append(f"{ms},15,{side},1,T,1000000000,100,10,2,2000000,"
                                 f"{outcome}:5:{actual}:1000:0")
        with gzip.open(root / f"labels__BTCUSDT__{month}.csv.gz", "wt", encoding="ascii") as stream:
            stream.write("\n".join(lines) + "\n")
    manifest = {"schema": PARAMS.schema, "symbol": "BTCUSDT", "params_identity": PARAMS.identity(),
                "ticks": {month: 100 for month in segment_months("validation")}}
    (root / "labels__BTCUSDT.manifest.json").write_bytes(canonical_bytes(manifest))
    return [StrategySpec(name, "1", {}, rows) for name, rows in signals.items()]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = ExperimentLog(self.root / "trials.jsonl")
        self.specs = make_labels(self.root)

    def run_experiment(self, **changes):
        args = dict(question_id="q-c2", specs=self.specs, geometries=[G], segment="validation", label_dir=self.root,
                    params=PARAMS, log=self.log, B=80, seed=5, code_commit="abc123",
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
            self.run_experiment(segment="hidden", label_dir=self.root / "does-not-exist")
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

    def test_verdict_precedence(self):
        row = Trade(G, 0, "S", 1, 10, 20, 0, 100, 0, 10, 20, 0)
        chosen = Selection([row], signals=1)
        ci = {"lower": "1", "t_statistic": "4"}
        self.assertEqual(verdict(chosen, {"passes": False}, ci, True), "NOT_ENOUGH_EVIDENCE")
        self.assertEqual(verdict(chosen, {"passes": True}, ci, True), "FRAGILE")
        row = replace(row, net_ur=100, cost_ur=1, opt_net_ur=100, amb=1)
        self.assertEqual(verdict(Selection([row]), {"passes": True}, ci, True), "FRAGILE")
        row = replace(row, amb=0)
        self.assertEqual(verdict(Selection([row]), {"passes": True}, ci, False), "FAIL")
        self.assertEqual(verdict(Selection([row]), {"passes": True}, ci, True), "PASS")


if __name__ == "__main__":
    unittest.main()
