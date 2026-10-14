"""Small hand-computed report cases; no network, study data or heavy statistics."""
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import csv
import unittest
from unittest.mock import patch

from market_analysis.benchmark import doji_report as dr
from market_analysis.benchmark.bars import COMPROMISED_FLAGS
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from market_analysis.benchmark.labels import LabelParams, LabelRow
from market_analysis.benchmark.scan import Cell, CellPair
from market_analysis.benchmark.ta_strategies import doji_readings, strategy_sides
from market_analysis.patterns.doji import read_doji
from market_analysis.patterns.features import CandleFeatures
from market_analysis.technical import TechnicalCandle, _adx, _adx_series

START = 1704067200000  # 2024-01-01T00:00:00Z, development


def trending_candles(count, direction):
    scale = 10 ** 8
    closes = [(200 + direction * i) * scale for i in range(count)]
    return CandleSeries("BTCUSDT", 15, START, list(closes), [c + 2 * scale for c in closes],
                        [c - 2 * scale for c in closes], closes, [True] * count,
                        volume=[100 * scale] * count)


class AdxSeriesTests(unittest.TestCase):
    def test_exact_prefix_parity_including_initialization_nonfinite_and_gaps(self):
        fixtures = [
            [100.0 + i for i in range(85)],
            [200.0 - i for i in range(85)],
            [100.0 + ((i * 37) % 29 - 14) / 7 for i in range(85)],
            [100.0] * 85,
            [100.0] * 35 + [101.0 + i / 10 for i in range(50)],
        ]
        for fixture_index, closes in enumerate(fixtures):
            candles = [TechnicalCandle(open_ms=i * 60_000, open=c, high=c + (0 if fixture_index == 3 else 1.1),
                                       low=c - (0 if fixture_index == 3 else 0.9), close=c, volume=10.0)
                       for i, c in enumerate(closes)]
            for gaps in ((), (0, 39, 40)):
                source = [None if i in gaps else c for i, c in enumerate(candles)]
                for period in (7, 14):
                    result = _adx_series(source, period)
                    run = []
                    for i, candle in enumerate(source):
                        if candle is None:
                            run = []
                            expected = (None, None, None)
                        else:
                            run.append(candle)
                            expected = _adx(run, period)
                        with self.subTest(fixture=fixture_index, gaps=gaps, period=period, i=i):
                            self.assertEqual(result[i], expected)

    def test_readings_and_both_strategy_signals_equal_original_prefix_helper(self):
        def reference(candles, period=14):
            run, result = [], []
            for candle in candles:
                run = [] if candle is None else run + [candle]
                result.append(_adx(run, period) if run else (None, None, None))
            return result

        for sign in (-1, 1):
            original = trending_candles(count=125, direction=sign)
            valid = list(original.valid)
            valid[60] = False
            for candles in (original, replace(original, valid=valid)):
                actual_readings = list(doji_readings(candles))
                self.assertTrue(actual_readings)
                actual_sides = [strategy_sides(name, candles) for name in dr.STRATEGIES]
                with patch("market_analysis.patterns.features._adx_series", reference):
                    self.assertEqual(list(doji_readings(candles)), actual_readings)
                    self.assertEqual([strategy_sides(name, candles) for name in dr.STRATEGIES], actual_sides)


class ReportMathTests(unittest.TestCase):
    def test_verdict_mapping(self):
        for code, gross, net, expected_gross, expected_net in (
                ("T", 1.0, .8, True, True), ("T", .1, -.1, True, False),
                ("S", -1.0, -1.2, False, False),
                ("E", .2, .1, True, True), ("E", .1, -.1, True, False),
                ("E", -.1, -.3, False, False), ("E", 0.0, 0.0, False, False),
                ("L", -2.0, -2.1, False, False), ("X", None, None, "ambiguous", "ambiguous")):
            with self.subTest(code=code, gross=gross, net=net):
                self.assertEqual(dr.verdict(code, gross), expected_gross)
                self.assertEqual(dr.verdict(code, net), expected_net)
        for status in ("V", "C", "G", "N", "I", "P", "purged"):
            self.assertIsNone(dr.verdict(None, None, status=status))

    def test_costs_funding_and_existing_wallet_floor(self):
        row = SimpleNamespace(wallet_ur=1_000_000)
        values = dr.cell_values(row, CellPair(Cell("T", 1, 800_000, 300_000, 100_000)))
        self.assertEqual((values["gross_r"], values["net_r_1x"], values["net_r_2x"]), (1.0, .8, .5))
        values = dr.cell_values(row, CellPair(Cell("L", 1, -900_000, 300_000, 0)))
        self.assertEqual(values["net_r_2x"], -1.0)
        values = dr.cell_values(row, CellPair(Cell("X", 1, None, None, None)))
        self.assertIsNone(values["gross_r"])
        self.assertIsNone(values["net_r_2x"])

    def test_wilson_hand_values(self):
        self.assertIsNone(dr.wilson(0, 0))
        low, high = dr.wilson(5, 10)
        self.assertAlmostEqual(low, .2365930905)
        self.assertAlmostEqual(high, .7634069095)
        self.assertAlmostEqual(dr.wilson(0, 10)[1], .2775327999)
        self.assertAlmostEqual(dr.wilson(10, 10)[0], .7224672001)

    def test_x_loss_denominator_nontrades_and_monetary_means(self):
        stats = dr.TradeStats()
        row = SimpleNamespace(wallet_ur=10_000_000)
        for code, net, cost in (("T", 800_000, 200_000), ("S", -1_200_000, 200_000),
                                ("E", -200_000, 100_000), ("X", None, None)):
            stats.add(dr.cell_values(row, CellPair(Cell(code, 1, net, cost, 0 if net is not None else None))))
        stats.add({"status": "V"})
        result = stats.report(1)
        self.assertEqual((result["n_signals"], result["n_trades"], result["n_measured"]), (5, 4, 3))
        self.assertEqual(result["net_hit_rate"], .25)
        self.assertEqual(result["gross_hit_rate"], .25)
        self.assertEqual(result["x_share"], .25)
        self.assertEqual(result["non_trades"], {"V": 1})
        self.assertAlmostEqual(result["mean_net_r_1x"], -.2)
        self.assertEqual(result["barrier_base_rate"], .5)

    def test_direction_ties_and_inclusive_excursions(self):
        bars = SimpleNamespace(start_ms=START, minutes=17, open=[100] * 17, high=[100] * 17,
                               low=[100] * 17, flags=[0] * 17)
        self.assertEqual(dr.direction(bars, START, 15, 1, "development"), ("tie", None))
        bars.open[15] = 110
        bars.high[15], bars.low[15] = 115, 90
        self.assertEqual(dr.direction(bars, START, 15, 1, "development"), (True, None))
        self.assertEqual(dr.direction(bars, START, 15, -1, "development"), (False, None))
        values, reason = dr.excursions(bars, START, 15, 1, 10 ** 19, Fraction(1))
        self.assertIsNone(reason)
        self.assertEqual(values, {"mfe_r": 1.5, "mae_r": 1.0, "mfe_pct": 15.0, "mae_pct": 10.0})
        bars.flags[5] = COMPROMISED_FLAGS
        self.assertEqual(dr.excursions(bars, START, 15, 1, 10 ** 19, 1), ({}, "compromised_path"))
        a = dr.direction_stats([True, False, "tie", None])
        self.assertEqual((a["n"], a["ties"], a["unavailable"], a["hit_rate"]), (2, 1, 1, .5))

    def test_even_sampling_is_deterministic_and_outcome_blind(self):
        events = [{"decision_ms": i, "symbol": "BTCUSDT", "strategy": "doji_continuation_v1",
                   "timeframe": 15, "side": "long", "k": "1", "rr": "1", "net_r_1x": i}
                  for i in range(10)]
        self.assertEqual([e["decision_ms"] for e in dr.sample_events(events, 4)], [0, 3, 6, 9])
        shuffled = [{**e, "net_r_1x": -e["net_r_1x"]} for e in reversed(events)]
        self.assertEqual([e["decision_ms"] for e in dr.sample_events(shuffled, 4)], [0, 3, 6, 9])
        self.assertEqual(dr.sample_events(events), events)
        self.assertEqual(dr.sample_events([]), [])
        irregular = [{**e, "decision_ms": 100 if e["decision_ms"] == 9 else e["decision_ms"]} for e in events]
        self.assertEqual([e["decision_ms"] for e in dr.sample_events(irregular, 3)], [0, 8, 100])

    def test_csv_has_full_schema_even_without_events(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.csv"
            dr.write_events(path, [])
            with path.open(newline="") as stream:
                self.assertEqual(next(csv.reader(stream)), list(dr.EVENT_FIELDS))


class ReportBoundaryTests(unittest.TestCase):
    def test_hidden_guard_runs_before_any_store_access(self):
        with patch.object(dr, "manifest_ticks") as ticks, patch.object(dr, "load_symbol_bars") as bars:
            with self.assertRaises(HiddenStretchLocked):
                dr.collect_symbol("unused", "unused", "BTCUSDT", "hidden", (15,), LabelParams())
            ticks.assert_not_called()
            bars.assert_not_called()

    def test_event_label_join_geometry_and_nontrade_verdicts(self):
        signal = START + 60 * 60_000
        scale = 10 ** 8
        bars = SimpleNamespace(start_ms=START, minutes=200, open=[100 * scale] * 200,
                               high=[110 * scale] * 200, low=[90 * scale] * 200, flags=[0] * 200,
                               index_of=lambda ms: (ms - START) // 60_000)
        candles = SimpleNamespace(valid=[True], end_ms=[signal], open=[100 * scale], high=[110 * scale],
                                  low=[90 * scale], close=[100 * scale])
        features = CandleFeatures(index=0, high=110, low=90, range=20, body=0, body_ratio=0,
                                 upper_ratio=.5, lower_ratio=.5, atr_prev=10, range_atr=2,
                                 rel_volume=1, adx_prev=30, trend_dir=1, prior20_high=110, prior20_low=90)
        params = LabelParams(k_grid=(Fraction(1),), rr_grid=(Fraction(1),))
        trade = LabelRow(signal, 15, 1, Fraction(1), "T", 100 * scale, 10 ** 19, 10,
                         3, 10_000_000, (CellPair(Cell("T", 1, 800_000, 200_000, 0)),))
        nontrade = LabelRow(signal, 15, -1, Fraction(1), "V")
        with TemporaryDirectory() as directory:
            (Path(directory) / "labels__BTCUSDT__2024-01.csv.gz").touch()
            with patch.object(dr, "check_segment", return_value=["2024-01"]), \
                    patch.object(dr, "manifest_ticks", return_value={"2024-01": scale}), \
                    patch.object(dr, "load_symbol_bars", return_value=bars), \
                    patch.object(dr, "build_candles", return_value=candles), \
                    patch.object(dr, "doji_readings", return_value=[(0, 1, features, read_doji(features))]), \
                    patch.object(dr, "read_label_csv", return_value=iter([trade, nontrade])):
                events, unconditional, baseline = dr.collect_symbol(directory, directory, "BTCUSDT",
                                                                     "development", (15,), params)
        self.assertEqual(len(events), 2)
        long, short = events
        self.assertEqual((long["entry_price"], long["stop"], long["target"]), (100, 90, 110))
        self.assertEqual((long["gross_verdict"], long["net_verdict"], long["direction_correct"]), (True, True, "tie"))
        self.assertEqual(short["status"], "V")
        for key in ("gross_verdict", "net_verdict", "direction_correct", "mae_r"):
            self.assertIsNone(short[key])
        self.assertEqual(unconditional[(15, 1)]["ties"], 1)
        self.assertEqual(baseline[("BTCUSDT", 15, 1, "1", "1")].trades, 1)

    def test_empty_primary_cells_and_public_redaction(self):
        unconditional = {("BTCUSDT", 15, side): dr.direction_stats([True, False, "tie"]) for side in (1, -1)}
        report = dr.build_report([], unconditional, {}, segment="development", symbols=("BTCUSDT",),
                                 horizons=(15,), params=LabelParams(), code_commit="abc", created_utc="2026-10-14T00:00:00Z",
                                 data_snapshot_id="snapshot", data_tags=["rd-BTCUSDT-2024-01-r1"], label_tags=["lb1-test"])
        self.assertEqual(len(report["primary"]), 4)
        md = dr.markdown(report, [])
        self.assertIn("## Primary (pre-specified)", md)
        self.assertIn("## Tier (descriptive)", md)
        self.assertIn("## Subtype (descriptive)", md)
        self.assertIn("time UTC | symbol | score | subtype | doji O/H/L/C", md)
        report["primary"][0]["trade"]["mean_net_r_1x"] = "PRIVATE-RETURN"
        report["primary"][0]["direction"]["hit_rate"] = "PRIVATE-RATE"
        text = "\n".join(dr.public_lines(report))
        self.assertNotIn("PRIVATE", text)
        self.assertIn("n_trades=0", text)
        self.assertIn("private report path reports/doji/development__", text)
        self.assertEqual(len(report["grid"]), 2 * 2 * 2 * 4)


if __name__ == "__main__":
    unittest.main()
