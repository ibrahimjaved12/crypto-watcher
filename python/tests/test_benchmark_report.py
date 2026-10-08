"""Canonical report identity, complete Markdown evidence and unit labels."""
from fractions import Fraction
import unittest

from market_analysis.benchmark.baselines import PoolRow
from market_analysis.benchmark.canonical import content_hash
from market_analysis.benchmark.evaluate import Selection, Trade, daily_series
from market_analysis.benchmark.label_store import Geometry
from market_analysis.benchmark.report import canonical_json, make_report, markdown, report_hash, variant_details
from market_analysis.benchmark.segments import segment_bounds_ms


class ReportTests(unittest.TestCase):
    def body(self):
        g = Geometry("BTCUSDT", 15, 1, Fraction(1), 0)
        row = Trade(g, segment_bounds_ms("validation")[0], "T", 5, 1_000_000, 100_000, -10_000,
                    2_000_000, 0, 1_000_000, 100_000, -10_000)
        selection = Selection([row], signals=3, non_trades={"V": 1}, not_in_grid=1)
        pooled = [PoolRow(row, Fraction(1, 100))]
        variant = variant_details(selection, daily_series(selection, "validation"), pooled, pooled, "validation")
        stats = variant["metrics"]
        variant.update(variant_id="v1", strategy_id="example", verdict="FAIL", stepm_p_value="0.5",
                       power={"passes": False, "T_days": 184}, bootstrap_ci={"lower": "0", "upper": "0.1"},
                       dsr={"scope": "best trial only", "raw": None, "effective": None},
                       baselines={"random_walk": {"expected_target_share": "0.5", "observed_target_share": "1"},
                                  "always_long": stats, "always_short": stats,
                                  "placebo": {"quantiles": {"50": "0"}, "p_placebo": "0.5", "fallback_count": 0}})
        return {"question_id": "q", "segment": "validation", "n_trials": 2,
                "params_identity": "abc", "cost_model_version": "cost-v1", "variants": [variant],
                "spa": {"p_consistent": "0.5"}, "stepm": {"adjusted_p_values": ["0.5"]},
                "best_trial_dsr": {"dsr_raw": None, "dsr_effective": None}}

    def test_deterministic_hash_and_canonical_serialization(self):
        body = self.body()
        report = make_report(body)
        self.assertEqual(report, make_report(dict(reversed(list(body.items())))))
        self.assertEqual(report["report_hash"], report_hash(report))
        self.assertEqual(report["report_hash"], content_hash({k: v for k, v in report.items() if k != "report_hash"}))
        self.assertEqual(canonical_json(report), canonical_json(make_report(self.body())))
        body["variants"][0]["verdict"] = "PASS"
        self.assertEqual(report["variants"][0]["verdict"], "FAIL")
        report["n_trials"] += 1
        with self.assertRaises(ValueError):
            canonical_json(report)

    def test_breakdowns_cost_grid_and_markdown(self):
        report = make_report(self.body())
        variant = report["variants"][0]
        self.assertEqual(set(variant["breakdowns"]), {"direction", "symbol", "horizon", "quarter",
                                                     "weekday_weekend", "volatility_tercile", "funding_sign"})
        self.assertIn("negative", variant["breakdowns"]["funding_sign"])
        self.assertEqual(variant["cost_grid"]["0"]["net_mean_r"], "1.1")
        self.assertEqual(variant["cost_grid"]["3"]["gross_mean_r"], "1.11")
        self.assertEqual(variant["cost_grid"]["3"]["funding_mean_r"], "-0.01")
        text = markdown(report)
        for phrase in ("today's six majors only", "score is a ranking, not a probability", "cost-v1",
                       "Params identity: abc", "entry day", "Matched placebo", "quantiles_net_r",
                       "non_trades", "funding_sign", "max_drawdown_r", "annualized_sharpe",
                       "Placebo p", "Required t", "X share", "Ambiguity share", "Power gate", "B_placebo"):
            self.assertIn(phrase, text)
        self.assertEqual(text, markdown(report))
        top = report["headline"]["variants"][0]
        self.assertEqual((top["p_placebo"], top["stepm_p_value"], top["power_passes"], top["verdict"]),
                         ("0.5", "0.5", False, "FAIL"))
        with self.assertRaises(ValueError):
            make_report({**self.body(), "headline": {}})

    def test_float_rejected(self):
        body = self.body()
        body["spa"]["p_consistent"] = 0.5
        with self.assertRaises(TypeError):
            make_report(body)


if __name__ == "__main__":
    unittest.main()
