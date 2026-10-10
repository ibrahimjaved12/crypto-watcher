"""Experiment-run progress lines: allowlisted phases and integer counts only, heartbeats, timings (no network)."""
from contextlib import redirect_stdout
from fractions import Fraction
import importlib.util
import io
from pathlib import Path
import re
import sys
import time
import unittest

from market_analysis import data_lake
from market_analysis.benchmark import funding_basis, order_flow, order_flow_v2, ta_strategies


def load_script():
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
    definition = importlib.util.spec_from_file_location("test_progress_script", scripts / "experiment_run.py")
    module = importlib.util.module_from_spec(definition)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(scripts))
        definition.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


OUTCOME_WORDS = ("verdict", "pass", "fail", "fragile", "evidence", "p_value", "pvalue", "mean", "return", "net",
                 "t_stat", "sharpe", "dsr", "spa_p", "placebo_p", "profit", "pnl", "outcome", "hash")
LINE = re.compile(r"progress (phase|heartbeat)=[a-z_-]+ t=\d+s( [a-z_]+=\d+)* maxrss_mb=\d+\Z")


class ProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_script()

    def progress(self, **kwargs):
        stream, clock = io.StringIO(), FakeClock()
        return self.module.Progress(stream, clock=clock, memory=lambda: 321, **kwargs), stream, clock

    def test_full_run_sequence_has_only_phase_names_counts_time_and_memory(self):
        progress, stream, clock = self.progress(interval=3600)
        progress.phase("checkout")
        for name in ("label download", "bars download", "spec build", "label load"):
            for index in range(1, 7):
                clock.now += 2
                progress.phase(name, symbol_index=index, symbols=6, months=18)
        with progress.stage("evaluate", total=48) as set_variant:
            for variant in range(1, 49):
                set_variant(variant)
                for name in ("evaluate", "bootstrap ci", "placebo"):
                    progress.phase(name, variant=variant, of=48)
        for name in ("spa bootstrap", "step-down"):
            with progress.stage(name, total=3) as set_step:
                set_step(2)
        progress.phase("report write")
        progress.phase("push", files=3)
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1 + 24 + 1 + 144 + 2 + 2)
        names = {*ta_strategies.STRATEGIES, *order_flow.STRATEGIES, *order_flow_v2.STRATEGIES, *funding_basis.STRATEGIES,
                 *data_lake.SYMBOLS}
        for line in lines:
            self.assertRegex(line, LINE)
            lowered = line.lower()
            for word in OUTCOME_WORDS:
                self.assertNotIn(word, lowered.replace("maxrss_mb", ""))
            for name in names:
                self.assertNotIn(name.lower(), lowered)
        self.assertIn("progress phase=label_load t=48s symbol_index=6 symbols=6 months=18 maxrss_mb=321", lines)

    def test_outcome_like_keys_values_and_unknown_phases_are_refused(self):
        progress, stream, _ = self.progress()
        refused = (("evaluate", {"verdict": 1}), ("evaluate", {"p_value": 1}), ("evaluate", {"mean_net_r": 2}),
                   ("evaluate", {"strategy": 1}), ("evaluate", {"symbol": 1}), ("evaluate", {"variant": "PASS"}),
                   ("evaluate", {"variant": Fraction(1, 2)}), ("evaluate", {"variant": 0.5}),
                   ("evaluate", {"variant": True}), ("rsi_14_reversion", {}), ("BTCUSDT", {}), ("verdict", {}))
        for name, counts in refused:
            with self.subTest(name=name, counts=counts), self.assertRaises(ValueError):
                progress.phase(name, **counts)
        with self.assertRaises(ValueError):
            with progress.stage("PASS"):
                pass
        self.assertEqual(stream.getvalue(), "")

    def test_heartbeats_fire_during_a_long_stage_and_stop_after(self):
        stream = io.StringIO()
        progress = self.module.Progress(stream, memory=lambda: 1, interval=0.02)
        with progress.stage("spa bootstrap", total=3) as set_step:
            set_step(2)
            deadline = time.monotonic() + 5
            while stream.getvalue().count("heartbeat=") < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        beats = [line for line in stream.getvalue().splitlines() if line.startswith("progress heartbeat=")]
        self.assertGreaterEqual(len(beats), 2)
        self.assertTrue(all(line.startswith("progress heartbeat=spa_bootstrap ") and " step=2 of=3 " in line
                            for line in beats))
        after = stream.getvalue()
        time.sleep(0.1)
        self.assertEqual(stream.getvalue(), after)  # the heartbeat thread stopped with the stage
        self.assertEqual(self.module.HEARTBEAT_SECONDS, 60.0)

    def test_stage_stops_its_heartbeat_when_the_stage_raises(self):
        stream = io.StringIO()
        progress = self.module.Progress(stream, memory=lambda: 1, interval=0.02)
        with self.assertRaises(RuntimeError):
            with progress.stage("evaluate", total=2):
                raise RuntimeError("variant failed")
        after = stream.getvalue()
        time.sleep(0.1)
        self.assertEqual(stream.getvalue(), after)

    def test_timing_table_and_quiet_wrapper(self):
        progress, stream, clock = self.progress()
        progress.phase("checkout")
        clock.now += 5
        progress.phase("bars download", symbol_index=1, symbols=6, months=18)
        clock.now += 7
        progress.phase("bars download", symbol_index=2, symbols=6, months=18)
        clock.now += 3
        self.assertEqual(progress.finish(), ["### Experiment run timings", "", "| phase | seconds |",
                                             "| --- | ---: |", "| checkout | 5 |", "| bars download | 10 |",
                                             "| total | 15 |", ""])
        captured = io.StringIO()
        with redirect_stdout(captured):
            quiet_progress = self.module.Progress(memory=lambda: 1)
        self.module._quiet(quiet_progress.phase, "snapshot", symbols=6, months=6)
        self.assertIn("progress phase=snapshot", captured.getvalue())  # survives the /dev/null redirect


if __name__ == "__main__":
    unittest.main()
