"""Forward vs label-engine parity library: row factory equals build_labels, the join, the pre-declared pass rule."""
from __future__ import annotations

from fractions import Fraction
from types import SimpleNamespace
import unittest

from market_analysis.benchmark.labels import LabelParams, build_labels, row_factory
from market_analysis.forward import bars_adapter as ba
from market_analysis.forward import parity
from market_analysis.forward.outcomes import empty_funding
from market_analysis.forward.setups import FORWARD_PARAMS
from test_forward_engine import START, TICK, walk_bars

MINUTE = 60_000
ROBUST = LabelParams(horizons=(15,), half_life_days=((15, 1),), step_minutes=((15, 15),), k_grid=(Fraction(2),),
                     rr_grid=(Fraction(3, 2), Fraction(2)), sigma_model="ewma-robust")


class RowFactoryTests(unittest.TestCase):
    def test_row_factory_rows_equal_build_labels_rows(self):
        bars = walk_bars(20 * 1440, seed=3)
        funding = empty_funding(bars)
        ticks = {"2025-01": TICK}
        rows = [r for _, month in build_labels("BTCUSDT", bars, funding, ROBUST, ticks) for r in month]
        row = row_factory("BTCUSDT", bars, funding, ROBUST, ticks)
        checked = traded = 0
        for expected in rows[::7]:
            self.assertEqual(row(expected.signal_ms, expected.horizon_min, expected.side, expected.k), expected)
            checked += 1
            traded += expected.status == "T"
        self.assertGreater(checked, 150)
        self.assertGreater(traded, 20)
        self.assertEqual(row.tick_at(START + 100 * MINUTE), TICK)
        with self.assertRaises(ValueError):
            row_factory("BTCUSDT", bars, funding, LabelParams(sigma_model="ewma"), ticks)


class BarsToRowsTests(unittest.TestCase):
    def test_rows_round_trip_to_the_same_integers_and_skip_missing_minutes(self):
        bars = walk_bars(3000, seed=2)
        rows = parity.rows_from_bars(bars, START, START + 3000 * MINUTE)
        self.assertEqual(len(rows), 3000)
        for i in (0, 1, 1499, 2999):
            row = rows[i]
            self.assertEqual([ba.to_scaled(row[name]) for name in ("open", "high", "low", "close")],
                             [bars.open[i], bars.high[i], bars.low[i], bars.close[i]])
        window = parity.rows_from_bars(bars, START + 100 * MINUTE, START + 200 * MINUTE)
        self.assertEqual((window[0]["open_time_ms"], len(window)), (START + 100 * MINUTE, 100))


def setup_dict(n=1, *, signal_ms=START + 15 * MINUTE * 10, rr="2", sigma=1000, level=500, c_h=1_000_000, d_ticks=40,
               tick=TICK, p0=100_000 * TICK, side=1, status="T"):
    ratio = Fraction(rr)
    target = p0 + side * (-(-ratio.numerator * d_ticks // ratio.denominator)) * tick
    return {"setup_id": f"{n:064x}", "signal_ms": signal_ms, "entry_ms": signal_ms, "horizon_min": 15, "side": side,
            "k": "2", "rr": rr, "status": status, "sigma": str(sigma), "var": str(level), "factor_weight": str(c_h),
            "d_ticks": d_ticks, "tick": tick, "p0": p0, "stop": p0 - side * d_ticks * tick, "target": target}


def reference_row(setup, *, sigma=1000, d_ticks=40, status="T", exit_offset=12, net=1_500_000, outcome="T", opt=None):
    pair = SimpleNamespace(pess=SimpleNamespace(outcome=outcome, exit_offset=exit_offset, net_ur=net), opt=opt)
    return SimpleNamespace(status=status, sigma=sigma, d_ticks=d_ticks, p0=setup["p0"], cells=(pair, pair))


def resolution(setup, **kw):
    values = {"setup_id": setup["setup_id"], "status": "T", "exit_offset": 12, "exit_ms": setup["entry_ms"] + 12 * MINUTE,
              "net_ur": 1_500_000, **kw}
    return values


class CompareTests(unittest.TestCase):
    def compare(self, setups, resolutions, rows, *, level=500, c_h=1_000_000, tick=TICK):
        return parity.compare(setups, resolutions, lambda ms, h, side, k: rows[ms],
                              lambda h, ms: level, lambda h, ms: c_h, lambda entry: tick, FORWARD_PARAMS)

    def test_perfect_agreement_passes_every_rule(self):
        s = setup_dict()
        stats = self.compare([s], [resolution(s)], {s["signal_ms"]: reference_row(s)})
        self.assertEqual((stats["unmatched"], stats["identical_geometry"]), (0, 1))
        self.assertEqual(stats["identical_geometry_status_exit_net_share"], 1.0)
        self.assertEqual(stats["abs_rel_delta_c_h"]["median"], 0.0)
        self.assertTrue(stats["verdict"]["pass"], stats["verdict"])
        self.assertNotRegex(parity.public_line("BTCUSDT", "2025-05", stats), r"\d{5,}")

    def test_unmatched_setups_fail_rule_i_and_list_their_keys(self):
        s = setup_dict(signal_ms=START + 7 * MINUTE)          # not on the label step grid (5 minutes)
        stats = self.compare([s], [], {})
        self.assertEqual(stats["unmatched"], 1)
        self.assertEqual(stats["unmatched_first_keys"][0][0], START + 7 * MINUTE)
        self.assertFalse(stats["verdict"]["i_no_unmatched"])
        self.assertFalse(stats["verdict"]["pass"])

    def test_identical_geometry_must_agree_on_status_exit_and_net(self):
        s = setup_dict()
        for bad in ({"status": "S"}, {"exit_offset": 13}, {"net_ur": 1}, {"exit_ms": 1}):
            stats = self.compare([s], [resolution(s, **bad)], {s["signal_ms"]: reference_row(s)})
            self.assertFalse(stats["verdict"]["ii_identical_geometry_agrees"], bad)
        ambiguous = reference_row(s, outcome="S", opt=SimpleNamespace(outcome="T"))
        stats = self.compare([s], [resolution(s, status="ambiguous")], {s["signal_ms"]: ambiguous})
        self.assertTrue(stats["verdict"]["ii_identical_geometry_agrees"])

    def test_different_geometry_is_judged_by_sigma_and_attributed_to_c_h_or_the_level(self):
        a, b = setup_dict(1), setup_dict(2, signal_ms=START + 15 * MINUTE * 11)
        rows = {a["signal_ms"]: reference_row(a, sigma=1000, d_ticks=39), b["signal_ms"]: reference_row(b, sigma=1000, d_ticks=39)}
        a["sigma"], b["sigma"] = "1020", "1200"          # +2 % (within 3 %) and +20 %
        stats = self.compare([a, b], [], rows, level=500, c_h=1_030_000)
        self.assertEqual(stats["identical_geometry"], 0)
        self.assertEqual(stats["geometry_or_sigma_within_tolerance"], 1)
        self.assertAlmostEqual(stats["geometry_or_sigma_share"], 0.5)
        self.assertEqual(stats["different_geometry"]["causes"], {"c_h": 2})   # level equal, multiplier off by 2.9 %
        self.assertAlmostEqual(stats["abs_rel_delta_sigma"]["p95"], 0.2)
        self.assertFalse(stats["verdict"]["iii_geometry_or_sigma_95"])
        self.assertTrue(stats["verdict"]["iv_median_c_h_3pct"])
        far = self.compare([a], [], rows, level=500, c_h=1_100_000)
        self.assertFalse(far["verdict"]["iv_median_c_h_3pct"])

    def test_a_different_tick_is_a_different_geometry(self):
        s = setup_dict()
        stats = self.compare([s], [resolution(s)], {s["signal_ms"]: reference_row(s)}, tick=TICK * 2)
        self.assertEqual(stats["identical_geometry"], 0)

    def test_no_setups_is_a_failure_not_a_pass(self):
        self.assertFalse(self.compare([], [], {})["verdict"]["pass"])


if __name__ == "__main__":
    unittest.main()
