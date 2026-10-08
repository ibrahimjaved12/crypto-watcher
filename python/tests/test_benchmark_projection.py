"""Power projection to another segment length: textbook numbers and selection wrapper."""
from fractions import Fraction
import unittest

from market_analysis.benchmark.evaluate import Selection
from market_analysis.benchmark.projection import (
    HIDDEN_DAYS, judgeable_report, project_power, project_selection, z_value,
)
from market_analysis.benchmark.scan import UR

UNIT_SIGMA = [UR, 0, -UR]  # sample variance exactly 1 R**2


class ProjectionTests(unittest.TestCase):
    def test_textbook_numbers(self):
        self.assertEqual(HIDDEN_DAYS, 273)
        z = z_value()
        self.assertTrue(Fraction(28015, 10 ** 4) < z < Fraction(28016, 10 ** 4))  # 1.959964 + 0.841621
        result = project_power(UNIT_SIGMA, 1, 785)
        self.assertEqual(result["sigma_daily_r"], "1")
        self.assertEqual(result["required_days"], 785)  # 0.1 R at sigma 1 needs 785 observations
        self.assertTrue(result["detectable"])
        self.assertFalse(project_power(UNIT_SIGMA, 1, 784)["detectable"])
        self.assertEqual(project_power(UNIT_SIGMA, 4, 785)["required_days"], 50)  # ceil(785 / 16)
        self.assertTrue(all(type(value) is not float for value in result.values()))
        mde = Fraction(project_power(UNIT_SIGMA, 2, 400)["mde_per_trade_r"])
        self.assertTrue(abs(mde - z / 20 / 2) < Fraction(1, 10 ** 11))

    def test_undefined_and_wrappers(self):
        flat = project_power([0, 0, 0], 1, 273)
        self.assertEqual(flat["sigma_daily_r"], "0")
        self.assertTrue(flat["detectable"])
        none = project_power(UNIT_SIGMA, 0, 273)
        self.assertIsNone(none["mde_per_trade_r"])
        self.assertFalse(none["detectable"])
        selection = Selection(trades=[object()] * 3)
        self.assertEqual(project_selection(selection, UNIT_SIGMA)["trades_per_day"], "1")
        report = judgeable_report({"many": (Selection(trades=[object()] * 3000), UNIT_SIGMA),
                                   "few": (selection, UNIT_SIGMA)})
        self.assertEqual(report["target_days"], 273)
        self.assertEqual(report["judgeable"], ["many"])
        with self.assertRaises(ValueError):
            project_power(UNIT_SIGMA, 1, 0)


if __name__ == "__main__":
    unittest.main()
