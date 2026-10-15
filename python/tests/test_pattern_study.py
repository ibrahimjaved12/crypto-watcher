"""Doji event study: cells, controls, estimators, Holm, ties, cost arithmetic, guard and report (small data)."""
import json
from math import sqrt
import re
import unittest

import numpy as np

from market_analysis.benchmark.canonical import content_hash
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
        open_ms=np.arange(n) * 900_000.0 + 1_704_067_200_000,
        decision_ms=np.arange(n) * 900_000.0 + 1_704_067_200_000 + 900_000.0,
        fwd_ok_1=np.ones(n), fwd_ok_2=np.ones(n), fwd_ok_4=np.ones(n), high=rng.uniform(101, 102, n),
        low=rng.uniform(98, 99, n), range=rng.uniform(1, 3, n), body_ratio=body, range_atr=rng.uniform(0.3, 2, n),
        adx_prev=rng.uniform(10, 40, n), trend_dir=sign(), extension_prev_atr=rng.normal(0, 2, n),
        any_touch=rng.integers(0, 2, n), nearest_level_dist_atr=rng.uniform(0, 2, n),
        sweep_any_up=rng.integers(0, 2, n), sweep_any_dn=rng.integers(0, 2, n), sweep_both=np.zeros(n),
        sweep_dir=sign(), touch_approach_side=sign(), squeeze_on_prev=rng.integers(0, 2, n),
        flush_dir=rng.choice([-1.0, 0.0, 1.0], n), rel_volume_slot=rng.uniform(0.5, 3, n),
        rel_volume_prior20=rng.uniform(0.5, 3, n), ret3_prev_sigma=rng.normal(0, 2, n),
        atr_pct_prev=rng.uniform(0, 1, n), utc_hour=rng.integers(0, 24, n), sigma_tf=np.full(n, 0.002),
        touch_round=rng.integers(0, 2, n), touch_prev_day=np.ones(n), touch_prev_week=rng.integers(0, 2, n),
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
                    ret3_prev_sigma=[-3.5, 1, 4], squeeze_on_prev=[1, 0, np.nan])
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

    def test_c8_threshold_is_two_sigma_of_three_candles(self):
        edge = 2 * sqrt(3)
        self.assertAlmostEqual(sc.RET3_SHOCK_THRESHOLD, edge)
        frame = toy(4, status=["ok"] * 4, rel_volume_slot=[3] * 4, rel_volume_prior20=[3] * 4,
                    ret3_prev_sigma=[edge - 0.01, edge + 0.01, -(edge - 0.01), -(edge + 0.01)])
        mask, s = CELL["C8"].rule(frame, True)
        self.assertEqual(mask.tolist(), [False, True, False, True])
        self.assertEqual(s[mask].tolist(), [-1.0, 1.0])
        self.assertIn("2*sqrt(3)", CELL["C8"].description)

    def test_population_keeps_insufficient_and_drops_invalid_or_windowless(self):
        frame = toy(4, status=["ok", "insufficient", "invalid", "ok"], fwd_ok_1=[1, 1, 1, 0])
        self.assertEqual(sc.population(frame).tolist(), [True, True, False, False])
        counts = ps.population_counts(toy(4, status=["ok", "insufficient", "invalid", "ok"],
                                          fwd_ok_1=[1, 1, np.nan, 0]))
        self.assertEqual(counts, {"segment_rows": 4, "invalid": 1, "no_forward_window": 1, "population": 2})

    def test_sessions_at_the_boundary_hours(self):
        hour = 3_600_000
        ms = [0, 8 * hour - 1, 8 * hour, 13 * hour - 1, 13 * hour, 21 * hour - 1, 21 * hour, 24 * hour - 1,
              24 * hour, 5 * 86_400_000 + 8 * hour]
        expected = ["asia", "asia", "europe", "europe", "us", "us", "late", "late", "asia", "europe"]
        self.assertEqual(sc.session_labels(ms, True).tolist(), expected)
        self.assertEqual(sc.session_labels(ms[:3], False).tolist(), ["all"] * 3)
        self.assertEqual(sc.session_labels([np.nan], True).tolist(), [""])

    def test_dose_response_uses_row_level_spearman(self):
        x = np.arange(60.0)
        rho = ps.row_spearman(x, x ** 3, np.repeat(np.arange(12), 5), B=50)
        self.assertAlmostEqual(rho["rho"], 1.0)
        self.assertEqual(rho["n"], 60)
        self.assertTrue(np.isnan(ps.row_spearman([1.0, 2.0], [1.0, 2.0], [0, 0])["rho"]))
        sample = {"D": np.ones(60), "y": -x, "day": np.repeat(np.arange(12), 5), "rows": {"body_ratio": x}}
        out = ps.dose_response(sample, "body_ratio", B=50)
        self.assertAlmostEqual(out["spearman_row"], -1.0)
        self.assertNotIn("spearman", out)  # the 5-point Spearman over bucket means is gone
        self.assertEqual(len(out["bucket_means"]), ps.DOSE_BUCKETS)

    def test_degenerate_touch_thresholds(self):
        self.assertEqual((sc.TOUCH_SHARE_MAX, sc.TOUCH_SHARE_MIN), (0.80, 0.01))
        self.assertFalse(sc.degenerate_touch(0.80))
        self.assertTrue(sc.degenerate_touch(0.8000001))
        self.assertTrue(sc.degenerate_touch(0.96))  # any_touch on 96-99.8 % of rows in the first build
        self.assertFalse(sc.degenerate_touch(0.01))
        self.assertTrue(sc.degenerate_touch(0.0099))
        self.assertFalse(sc.degenerate_touch(0.5))

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

    def test_holm_mde_multiplier_from_the_normal_quantile(self):
        from statistics import NormalDist
        normal = NormalDist()
        expected = normal.inv_cdf(1 - 0.05 / 18) + normal.inv_cdf(0.8)  # alpha / (2 K) per tail
        self.assertAlmostEqual(ps.holm_mde_multiplier(), expected)
        self.assertGreater(ps.holm_mde_multiplier(), 3.5)
        self.assertLess(ps.holm_mde_multiplier(), 3.7)
        self.assertAlmostEqual(ps.mde_multiplier(0.05), 2.8016, places=3)

    def test_day_mean_clusters_by_day(self):
        out = ps.day_mean([1.0, 3.0, 5.0, np.nan], [0, 0, 1, 1], B=200)
        self.assertEqual((out["n"], out["mean"]), (3, 3.0))
        self.assertLessEqual(out["ci95"][0], 3.0)
        self.assertGreaterEqual(out["ci95"][1], 3.0)
        self.assertEqual(ps.day_mean([np.nan], [0])["n"], 0)

    def test_cost_lines_report_members_mean_and_both_hurdle_differences(self):
        members = {"n": 10, "mean": 15.0, "se": 1.0, "ci95": [13.0, 17.0]}
        controls = {"n": 20, "mean": 1.0, "se": 1.0, "ci95": [-1.0, 3.0]}
        out = ps.cost_lines(0.1, 0.05, 20.0, members, controls)
        self.assertEqual(out["members_minus_hurdle_bp"], 4.0)
        self.assertAlmostEqual(out["effect_minus_hurdle_bp"], -9.0)
        self.assertEqual(out["members_mean_bp"]["ci95"], [13.0, 17.0])
        self.assertAlmostEqual(out["mde_sigma_holm"], ps.holm_mde_multiplier() * 0.05)
        self.assertAlmostEqual(out["mde_bp_holm"], ps.holm_mde_multiplier() * 0.05 * 20.0)
        self.assertGreater(out["mde_bp_holm"], out["mde_bp"])

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
        cls.diagnostics = ps.diagnostics(ps.tf_frames(frame, (15, 60)), {})
        cls.frame = frame

    def report(self, commit="a", created="2026-10-15T00:00:00Z"):
        return ps.build_report("development", self.primary, self.descriptive, self.diagnostics,
                               table_report="reports/candle-table/x", table_counts={"intraday_rows": 600},
                               code_commit=commit, created_utc=created)

    def test_hidden_segment_is_refused(self):
        with self.assertRaises(HiddenStretchLocked):
            ps.build_report("hidden", self.primary, self.descriptive, self.diagnostics, table_report="x",
                            table_counts={}, code_commit="a", created_utc="b")
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
        self.assertTrue(any(line.startswith("population intraday segment_rows=") for line in lines))

    def test_outputs_render(self):
        report = self.report()
        csv_text = ps.cells_csv(report)
        self.assertEqual(len(csv_text.strip().splitlines()), 10)
        self.assertIn("## Primary", ps.markdown(report))
        self.assertTrue(re.search(r"\| A \| \d+ \|", ps.markdown(report)))

    def test_new_fields_are_required_and_replication_round_trips(self):
        report = self.report()
        cost = next(row["cost"] for row in report["primary"] if row.get("cost"))
        for key in ps.REQUIRED_COST:
            self.assertIn(key, cost)
        for key in ("sessions", "sigma_seasonality"):
            self.assertIn(key, report["descriptive"])
        self.assertIn("stratified_session", report["primary"][0])
        for key in ("population", "base_rates", "level_diagnostics"):
            broken = {k: v for k, v in report.items() if k != key}
            broken["report_hash"] = content_hash({k: v for k, v in broken.items() if k not in ps.HASH_EXCLUDED})
            with self.assertRaisesRegex(ValueError, key):
                ps.check_report(broken)
        stripped = json.loads(json.dumps(report))  # the stored form: strings for floats
        ps.check_report(stripped)
        validation = ps.build_report("validation", self.primary, self.descriptive, self.diagnostics,
                                     table_report="reports/candle-table/x", table_counts={}, reference=stripped,
                                     code_commit="a", created_utc="b")
        self.assertEqual(validation["replication"]["reference_hash"], report["report_hash"])
        self.assertTrue(all(value is True for value in validation["replication"]["same_sign"].values()
                            if value is not None))
        ps.check_report(json.loads(json.dumps(validation)))
        with self.assertRaises(ValueError):  # a validation report is not a reference
            ps.build_report("validation", self.primary, self.descriptive, self.diagnostics, table_report="x",
                            table_counts={}, reference=json.loads(json.dumps(validation)), code_commit="a",
                            created_utc="b")

    def test_sessions_are_descriptive_and_sum_to_the_cell_sample(self):
        sessions = self.descriptive["sessions"]
        self.assertIn("DESCRIPTIVE", sessions["label"])
        for tf in (15, 60):
            part = ps.tf_frames(self.frame, (15, 60))[tf]
            sample = ps.cell_sample(part, CELL["A"], intraday=True)
            rows = sessions["cells"]["A"][str(tf)]
            self.assertEqual(sum(r["events"] for r in rows.values()), int(sample["D"].sum()))
            self.assertEqual(sum(r["controls"] for r in rows.values()), int((1 - sample["D"]).sum()))
            self.assertEqual(set(rows), set(sc.SESSION_NAMES))
            for row in rows.values():
                self.assertEqual(row["status"] == "too few", row["events"] < sc.MIN_EVENTS_DESCRIPTIVE)
                if row["status"] == "ok":
                    self.assertEqual(row["unit"], "ln range, sigma")  # cell A is unsigned
        signed = sessions["cells"]["C6"]["15"]
        self.assertTrue(any(r["status"] == "ok" for r in signed.values()) or
                        all(r["status"] == "too few" for r in signed.values()))

    def test_session_is_a_robustness_stratum_outside_the_holm_family(self):
        a = ps.cell_sample(self.frame, CELL["A"], intraday=True)
        plain = ps.strata_codes(a, hour_effects=True)
        with_session = ps.strata_codes(a, hour_effects=True, with_session=True)
        self.assertGreaterEqual(int(with_session.max()), int(plain.max()))
        row = self.primary[0]
        self.assertIn("stratified_session", row)
        self.assertEqual(len(self.primary), 9)


class DiagnosticsTests(unittest.TestCase):
    def test_level_diagnostics_label_degenerate_sets(self):
        n = 200
        frame = toy(n, status=["ok"] * n, symbol=["ETHUSDT"] * n, range=[10.0] * n, tf=[15.0] * n,
                    touch_round=[1.0] * 190 + [0.0] * 10,           # 95 %: above the upper limit
                    touch_prev_day=[1.0] + [0.0] * 199,             # 0.5 %: below the lower limit
                    touch_prev_week=[1.0] * 100 + [0.0] * 100,      # 50 %: interpretable
                    any_touch=[1.0] * 160 + [0.0] * 40)             # 80 %: not above 80 %
        out = ps.level_diagnostics({15: frame}, "intraday")["by_tf"]["15"]
        self.assertEqual(out["round"]["evaluated"], n)
        self.assertAlmostEqual(out["round"]["touch_share"], 0.95)
        self.assertEqual(out["round"]["label"], "degenerate: not interpretable")
        self.assertEqual(out["prev_day"]["label"], "degenerate: not interpretable")
        self.assertNotIn("label", out["prev_week"])
        self.assertNotIn("label", out["any"])
        self.assertTrue(out["prev_month"]["label"].startswith("not applicable"))  # empty column
        self.assertAlmostEqual(out["round"]["round_step_over_range"]["median"], 10.0)  # 100 / 10 for ETH
        self.assertEqual(set(out["round"]["round_step_over_range"]["by_symbol"]), {"ETHUSDT"})

    def test_invalid_rows_are_not_evaluated(self):
        frame = toy(4, status=["ok", "invalid", "ok", "insufficient"], symbol=["BTCUSDT"] * 4, range=[1.0] * 4,
                    touch_round=[1.0, 1.0, 0.0, np.nan])
        out = ps.level_diagnostics({15: frame}, "x")["by_tf"]["15"]["round"]
        self.assertEqual((out["evaluated"], out["touch_share"]), (2, 0.5))

    def test_hourly_diagnostic_uses_control_rows_only(self):
        hour = 3_600_000
        n = 6
        frame = toy(n, status=["ok"] * n, tf=[15.0] * n, fwd_ok_1=[1.0] * n, range=[1.0] * n,
                    range_atr=[1.0] * n, body_ratio=[0.3, 0.3, 0.3, 0.05, 0.3, 0.3],
                    ret_1_sigma=[1.0, -3.0, -4.0, 100.0, 5.0, np.nan],
                    decision_ms=[1 * hour, 1 * hour + 5, 2 * hour, 1 * hour, 2 * hour, 2 * hour])
        out = ps.sigma_seasonality({15: frame})["by_tf"]["15"]
        self.assertEqual(out["by_hour"]["1"], {"mean": 2.0, "count": 2})       # |1|, |-3|; the event row is excluded
        self.assertEqual(out["by_hour"]["2"], {"mean": 4.5, "count": 2})       # |-4|, |5|; NaN outcome excluded
        self.assertEqual(out["controls"], 4)
        self.assertAlmostEqual(out["max_over_min"], 2.25)
        self.assertEqual(set(out["by_hour"]), {"1", "2"})

    def test_context_base_rates(self):
        frame = toy(6, status=["ok", "insufficient", "invalid", "ok", "ok", "ok"], fwd_ok_1=[1.0] * 6,
                    body_ratio=[0.05, 0.3, 0.05, 0.3, 0.05, 0.3], range=[1.0] * 6,
                    range_atr=[1.0, 1.0, 1.0, 1.0, 0.2, 1.0], tf=[15.0] * 6)
        out = ps.context_base_rates({15: frame})["A"]["15"]
        self.assertEqual((out["population"], out["context"], out["events"], out["controls"]), (5, 5, 1, 3))
        self.assertAlmostEqual(out["event_share"], 0.2)
        self.assertAlmostEqual(out["control_share"], 0.6)


if __name__ == "__main__":
    unittest.main()
