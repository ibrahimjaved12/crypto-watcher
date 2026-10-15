"""Doji event study: cells, controls, estimators, Holm, ties, cost arithmetic, guard and report (small data)."""
import re
import unittest

import numpy as np

from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from market_analysis.patterns import pattern_study as ps
from market_analysis.patterns import study_cells as sc

CELL = {cell.id: cell for cell in sc.CELLS}


def toy(n, **columns):
    """A frame of ``n`` rows: every needed column NaN (text '') unless given."""
    frame = {name: np.full(n, np.nan) for name in ps.NUMERIC_COLUMNS}
    frame.update({name: np.array([""] * n, dtype=object) for name in ps.TEXT_COLUMNS})
    frame["prior20_high"], frame["prior20_low"] = np.full(n, np.nan), np.full(n, np.nan)
    for name, values in columns.items():
        frame[name] = np.array(values, dtype=object if name in ps.TEXT_COLUMNS else float)
    return frame


def synthetic(n=600, seed=3):
    rng = np.random.default_rng(seed)
    sign = lambda: rng.choice([-1.0, 1.0], n)  # noqa: E731
    body = rng.uniform(0, 0.5, n)
    ret = rng.normal(0, 1, n)
    return ps.add_prior20(toy(
        n, symbol=rng.choice(["BTCUSDT", "ETHUSDT"], n), segment=["development"] * n, status=["ok"] * n,
        doji_subtype=np.where(body <= 0.1, "standard", ""), tf=rng.choice([15.0, 60.0], n),
        open_ms=np.arange(n) * 900_000.0 + 1_704_067_200_000, decision_ms=np.arange(n) * 900_000.0,
        fwd_ok_1=np.ones(n), fwd_ok_2=np.ones(n), fwd_ok_4=np.ones(n), high=rng.uniform(101, 102, n),
        low=rng.uniform(98, 99, n), range=rng.uniform(1, 3, n), body_ratio=body, range_atr=rng.uniform(0.3, 2, n),
        adx_prev=rng.uniform(10, 40, n), trend_dir=sign(), extension_prev_atr=rng.normal(0, 2, n),
        any_touch=rng.integers(0, 2, n), nearest_level_dist_atr=rng.uniform(0, 2, n),
        sweep_any_up=rng.integers(0, 2, n), sweep_any_dn=rng.integers(0, 2, n), sweep_both=np.zeros(n),
        sweep_dir=sign(), touch_approach_side=sign(), squeeze_on_prev=rng.integers(0, 2, n),
        flush_dir=rng.choice([-1.0, 0.0, 1.0], n), rel_volume_slot=rng.uniform(0.5, 3, n),
        rel_volume_prior20=rng.uniform(0.5, 3, n), ret3_prev_sigma=rng.normal(0, 2, n),
        atr_pct_prev=rng.uniform(0, 1, n), utc_hour=rng.integers(0, 24, n), sigma_tf=np.full(n, 0.002),
        ret_1_sigma=ret, ret_1_bp=np.round(ret * 20), ln_range1_sigma=rng.normal(0, 1, n),
        ret_2_sigma=rng.normal(0, 1, n), ret_4_sigma=rng.normal(0, 1, n), mfe_long_2_sigma=rng.uniform(0, 2, n),
        mae_long_2_sigma=rng.uniform(0, 2, n), mfe_long_4_sigma=rng.uniform(0, 2, n),
        mae_long_4_sigma=rng.uniform(0, 2, n)))


class CellTests(unittest.TestCase):
    def test_c3_excludes_sweep_both(self):
        frame = toy(4, status=["ok"] * 4, sweep_any_up=[1, 1, 0, np.nan], sweep_any_dn=[0, 1, 1, np.nan],
                    sweep_both=[0, 1, 0, np.nan], sweep_dir=[-1, 0, 1, np.nan])
        mask, s = CELL["C3"].rule(frame, True)
        self.assertEqual(mask.tolist(), [True, False, True, False])
        self.assertEqual(s[mask].tolist(), [-1.0, 1.0])

    def test_c4_direction_is_against_the_approach(self):
        frame = toy(4, status=["ok"] * 4, any_touch=[1, 1, 1, 0], touch_approach_side=[1, -1, 0, 1])
        mask, s = CELL["C4"].rule(frame, True)
        self.assertEqual(mask.tolist(), [True, True, False, False])
        self.assertEqual(s[mask].tolist(), [-1.0, 1.0])  # resistance approach -> short, support -> long

    def test_c1_c2_c6_c7_c8_and_unsigned(self):
        frame = toy(3, status=["ok"] * 3, adx_prev=[25, 25, 15], trend_dir=[1, -1, 1],
                    extension_prev_atr=[0.5, -2.5, 0.0], any_touch=[0, 0, 0], nearest_level_dist_atr=[1, 1, 1],
                    high=[110, 100, 100], low=[90, 80, 90], prior20_high=[105, 120, 120], prior20_low=[95, 85, 95],
                    flush_dir=[1, 0, -1], rel_volume_slot=[2, 3, 1], rel_volume_prior20=[1, 1, 5],
                    ret3_prev_sigma=[-2.5, 1, 3], squeeze_on_prev=[1, 0, np.nan])
        self.assertEqual(CELL["C1"].rule(frame, True)[0].tolist(), [True, False, False])
        mask, s = CELL["C2"].rule(frame, True)
        self.assertEqual(mask.tolist(), [False, True, False])  # down trend, stretched 2.5, low 80 <= 85
        self.assertEqual(s[1], 1.0)
        mask, s = CELL["C6"].rule(frame, True)
        self.assertEqual((mask.tolist(), s[2]), ([False, False, True], 1.0))
        mask, s = CELL["C7"].rule(frame, True)
        self.assertEqual(s[mask].tolist(), [-1.0, 1.0])
        self.assertEqual(CELL["C8"].rule(frame, True)[0].tolist(), [True, False, False])  # intraday: slot volume
        mask, s = CELL["C8"].rule(frame, False)
        self.assertEqual((mask.tolist(), s[2]), ([False, False, True], -1.0))  # daily: prior-20 volume
        self.assertEqual(CELL["C5"].rule(frame, True)[0].tolist(), [True, False, False])
        self.assertEqual(sc.K_PRIMARY, 9)

    def test_event_and_control_are_same_size_other_shape(self):
        frame = toy(5, body_ratio=[0.10, 0.11, 0.05, 0.02, np.nan], range_atr=[0.5, 0.5, 0.49, 2, 1],
                    range=[1, 1, 1, 1, 0])
        self.assertEqual(sc.event(frame).tolist(), [True, False, False, True, False])
        self.assertEqual(sc.control(frame).tolist(), [False, True, False, False, False])


class EstimatorTests(unittest.TestCase):
    def test_regression_recovers_a_planted_beta(self):
        rng = np.random.default_rng(11)
        n = 4000
        D = (rng.random(n) < 0.3).astype(float)
        X = rng.normal(0, 1, (n, 3))
        group = rng.choice(["BTCUSDT|15", "ETHUSDT|60"], n).astype(object)
        hour = rng.integers(0, 24, n).astype(float)
        y = 0.3 * D + 0.5 * X[:, 0] + np.where(group == "ETHUSDT|60", 1.0, 0.0) + rng.normal(0, 1, n)
        sample = {"D": D, "y": y, "X": X, "group": group, "hour": hour, "day": rng.integers(0, 200, n)}
        fit = ps.regression(sample, hour_effects=True)
        self.assertLess(abs(fit["beta"] - 0.3), 4 * fit["se"])
        self.assertLess(fit["se"], 0.1)
        self.assertEqual(fit["n"], n)

    def test_stratified_by_hand(self):
        # stratum range_atr 0.6: members 1, 3 vs control 0 -> +2 (weight 2); stratum 1.2: member 5 vs 4, 6 -> 0
        # (weight 1); stratum 3.0 has no control and is dropped. Estimate (2 * 2 + 1 * 0) / 3.
        sample = {"D": np.array([1, 1, 0, 1, 0, 0, 1.0]), "y": np.array([1, 3, 0, 5, 4, 6, 100.0]),
                  "group": np.array(["B|15"] * 7, dtype=object), "hour": np.zeros(7),
                  "atr_pct": np.full(7, 0.1), "range_atr": np.array([0.6, 0.6, 0.6, 1.2, 1.2, 1.2, 3.0]),
                  "rel_volume": np.ones(7), "day": np.arange(7)}
        out = ps.stratified(sample, hour_effects=True, B=50)
        self.assertAlmostEqual(out["estimate"], 4 / 3)
        self.assertEqual(out["strata_used"], 2)
        self.assertEqual(len(out["ci95"]), 2)

    def test_holm(self):
        adjusted = ps.holm([0.01, 0.04, 0.03, None])
        self.assertAlmostEqual(adjusted[0], 0.04)
        self.assertAlmostEqual(adjusted[1], 0.09)  # step-down keeps the running maximum
        self.assertAlmostEqual(adjusted[2], 0.09)
        self.assertIsNone(adjusted[3])
        self.assertEqual(ps.holm([0.5, 0.6]), [1.0, 1.0])

    def test_ties_are_excluded_and_counted(self):
        sample = {"signed": True, "D": np.array([1, 1, 1, 0.0]), "s": np.array([1, 1, -1, 1.0]),
                  "ret_bp": np.array([5, 0, -3, -2.0])}
        out = ps.hit_rates(sample)
        self.assertEqual((out["members"]["hits"], out["members"]["n"], out["members"]["ties"]), (2, 2, 1))
        self.assertEqual((out["controls"]["hits"], out["controls"]["n"]), (0, 1))
        self.assertEqual(out["difference"], 1.0)
        self.assertIsNone(ps.hit_rates({"signed": False}))
        low, high = ps.wilson(2, 2)
        self.assertTrue(0 < low < 1)
        self.assertAlmostEqual(high, 1.0)

    def test_mde_and_hurdle(self):
        out = ps.cost_lines(0.1, 0.05, 20.0)
        self.assertAlmostEqual(out["beta_bp"], 2.0)
        self.assertAlmostEqual(out["mde_sigma"], 0.14)
        self.assertAlmostEqual(out["mde_bp"], 2.8)
        self.assertAlmostEqual(out["hurdle_sigma"], 0.55)
        self.assertAlmostEqual(out["effect_minus_hurdle_bp"], -9.0)


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        frame = synthetic()
        cls.primary = ps.primary(frame, B=20)
        cls.descriptive = ps.descriptive(frame, {}, B=20)

    def report(self, commit="a", created="2026-10-15T00:00:00Z"):
        return ps.build_report("development", self.primary, self.descriptive, table_report="reports/candle-table/x",
                               table_counts={"intraday_rows": 600}, code_commit=commit, created_utc=created)

    def test_hidden_segment_is_refused(self):
        with self.assertRaises(HiddenStretchLocked):
            ps.build_report("hidden", self.primary, self.descriptive, table_report="x", table_counts={},
                            code_commit="a", created_utc="b")
        with self.assertRaises(HiddenStretchLocked):
            ps.run_study("/nonexistent", "hidden", table_report="x", code_commit="a", created_utc="b")

    def test_report_hash_is_idempotent(self):
        first, second = self.report(), self.report("b", "2026-10-16T00:00:00Z")
        self.assertEqual(first["report_hash"], second["report_hash"])
        ps.check_report(first)
        self.assertTrue(ps.report_dir(first).startswith("reports/pattern-study/doji-development__"))
        self.assertEqual(len(self.primary), 9)
        self.assertEqual(first["hypotheses_examined"]["primary"], 9)
        tampered = {**first, "segment": "validation"}
        with self.assertRaises(ValueError):
            ps.check_report(tampered)

    def test_public_lines_have_no_effect_sizes(self):
        lines = ps.public_lines(self.report())
        text = "\n".join(lines)
        self.assertNotRegex(text, r"\d\.\d")  # no decimal numbers at all
        for word in ("beta", "p_holm", "mean", "bp", "sigma"):
            self.assertNotIn(word, text)
        self.assertIn("9 primary hypotheses", lines[0])
        self.assertTrue(any(line.startswith("private path reports/pattern-study/") for line in lines))
        self.assertEqual(sum(1 for line in lines if line.startswith("cell ")), 9)

    def test_outputs_render(self):
        report = self.report()
        csv_text = ps.cells_csv(report)
        self.assertEqual(len(csv_text.strip().splitlines()), 10)
        self.assertIn("## Primary", ps.markdown(report))
        self.assertTrue(re.search(r"\| A \| \d+ \|", ps.markdown(report)))


if __name__ == "__main__":
    unittest.main()
