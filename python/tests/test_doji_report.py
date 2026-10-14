"""Small hand-computed report cases; no network, study data or heavy statistics."""
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import csv
import hashlib
import importlib.util
import io
import json
import sys
import unittest
from unittest.mock import Mock, patch

from market_analysis.benchmark import doji_report as dr, experiment_run as er
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


class ReportScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
        definition = importlib.util.spec_from_file_location("test_doji_report_script", scripts / "doji_report.py")
        cls.module = importlib.util.module_from_spec(definition)
        previous = sys.path[:]
        try:
            sys.path.insert(0, str(scripts))
            definition.loader.exec_module(cls.module)
        finally:
            sys.path[:] = previous

    @staticmethod
    def write_release(root, symbol, months, params):
        outputs = []
        for month in months:
            name = f"labels__{symbol}__{month}.csv.gz"
            body = b"PRIVATE-SYNTHETIC-LABEL-ROW"
            (root / name).write_bytes(body)
            outputs.append({"month": month, "name": name, "sha256": hashlib.sha256(body).hexdigest()})
        manifest = {"symbol": symbol, "params_identity": params.identity(),
                    "cost_model_identity": params.cost_model.identity(), "outputs": outputs, "rd_tags": []}
        (root / er.label_manifest_name(symbol)).write_text(json.dumps(manifest), encoding="utf-8")

    def test_revision_three_snapshot_and_report_for_both_segments_and_symbol_subsets(self):
        module, params = self.module, LabelParams()
        for segment, symbols, horizons in (("development", ("BTCUSDT",), (60,)),
                                            ("validation", ("BTCUSDT", "ETHUSDT"), (15, 240))):
            args = module.parse_args(["--segment", segment, "--symbols", ",".join(symbols),
                                      "--horizons", ",".join(map(str, horizons)), "--no-record-trials"])
            directions = {(h, side, str(k)): dr.direction_stats([])
                          for h in horizons for side in (1, -1) for k in params.k_grid}

            def download(repo, tag, segment, label_dir, symbol):
                self.assertEqual(tag, f"lb1-{symbol}-2024-01_2026-09-r3")
                self.write_release(label_dir, symbol, module.cal.check_segment(segment), params)

            with self.subTest(segment=segment), TemporaryDirectory() as directory:
                root, stream = Path(directory), io.StringIO()
                checkout = SimpleNamespace(path=root, commit_and_push=Mock())
                with patch.object(module.audit, "download_labels", side_effect=download), \
                        patch.object(module.xr, "_download_symbol_bars") as bars, \
                        patch.object(dr, "collect_symbol", return_value=([], directions, {})) as collect:
                    module.run(args, checkout, object(), root / "data", module.audit.AuditProgress(stream))
                self.assertEqual(bars.call_count, len(symbols))
                self.assertEqual(collect.call_count, len(symbols))
                self.assertTrue(all(call.args[-1] == params for call in collect.call_args_list))
                report = json.loads(next(root.glob("reports/doji/*/summary.json")).read_bytes())
                self.assertEqual(report["label_params_identity"], params.identity())
                self.assertEqual(report["label_release_tags"],
                                 sorted(f"lb1-{symbol}-2024-01_2026-09-r3" for symbol in symbols))
                self.assertEqual(report["data_snapshot_id"], er.data_snapshot(
                    root / "data" / "labels", module.cal.check_segment(segment), params, symbols=symbols))
                self.assertLess(stream.getvalue().index("phase=snapshot"),
                                stream.getvalue().index("phase=bars_download"))
                checkout.commit_and_push.assert_called_once()

    def test_invalid_snapshot_fails_before_bars_collection_or_push_with_safe_error(self):
        module = self.module
        args = module.parse_args(["--segment", "development", "--symbols", "BTCUSDT", "--horizons", "60"])
        for failure in ("legacy-grid", "cost-model", "checksum"):
            def download(repo, tag, segment, label_dir, symbol):
                params = LabelParams()
                if failure == "legacy-grid":
                    params = replace(params, step_minutes=((15, 5), (60, 15), (240, 60)))
                months = module.cal.check_segment(segment)
                self.write_release(label_dir, symbol, months, params)
                if failure == "cost-model":
                    path = label_dir / er.label_manifest_name(symbol)
                    manifest = json.loads(path.read_bytes())
                    manifest["cost_model_identity"] = "PRIVATE-INVALID-IDENTITY"
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                if failure == "checksum":
                    (label_dir / f"labels__{symbol}__{months[0]}.csv.gz").write_bytes(b"tampered")

            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                root = Path(directory)
                checkout = SimpleNamespace(path=root, commit_and_push=Mock())
                with patch.object(module.audit, "download_labels", side_effect=download), \
                        patch.object(module.xr, "_download_symbol_bars") as bars, \
                        patch.object(dr, "collect_symbol") as collect, \
                        self.assertRaises(module.xr.PublicError) as caught:
                    module.run(args, checkout, object(), root / "data", module.audit.AuditProgress(io.StringIO()))
                self.assertEqual(str(caught.exception),
                                 "label snapshot validation failed: check release parameters and monthly checksums")
                bars.assert_not_called()
                collect.assert_not_called()
                checkout.commit_and_push.assert_not_called()


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
                ("L", -2.0, -2.1, False, False), ("X", None, None, "unmeasured", "unmeasured")):
            with self.subTest(code=code, gross=gross, net=net):
                self.assertEqual(dr.verdict(code, gross), expected_gross)
                self.assertEqual(dr.verdict(code, net), expected_net)
        for status in ("V", "C", "G", "N", "I", "P", "purged"):
            self.assertIsNone(dr.verdict(None, None, status=status))

    def test_costs_funding_and_existing_wallet_floor(self):
        row = SimpleNamespace(wallet_ur=1_000_000)
        values = dr.cell_values(row, CellPair(Cell("T", 1, 800_000, 300_000, 100_000)))
        self.assertEqual((values["gross_r"], values["net_r_1x"], values["net_r_2x"]), (1.0, .8, .5))
        paid = dr.cell_values(row, CellPair(Cell("T", 1, 600_000, 300_000, -100_000)))
        self.assertEqual(paid["gross_r"], 1.0)
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
        self.assertEqual(result["target_first_ts"], .5)
        self.assertIsNone(dr.TradeStats().report(1)["target_first_ts"])

    def test_direction_ties_and_excursions_exclude_exit_minute(self):
        bars = SimpleNamespace(start_ms=START, minutes=17, open=[100] * 17, high=[100] * 17,
                               low=[100] * 17, flags=[0] * 17)
        self.assertEqual(dr.direction(bars, START, 15, 1, "development"), ("tie", None))
        bars.open[15] = 110
        bars.high[14], bars.low[14] = 115, 90
        bars.high[15], bars.low[15] = 200, 1
        self.assertEqual(dr.direction(bars, START, 15, 1, "development"), (True, None))
        self.assertEqual(dr.direction(bars, START, 15, -1, "development"), (False, None))
        bars.flags[15] = COMPROMISED_FLAGS  # Outside the excursion window.
        values, reason = dr.excursions(bars, START, 15, 1, 10 ** 19, Fraction(1))
        self.assertIsNone(reason)
        self.assertEqual(values, {"mfe_r": 1.5, "mae_r": 1.0, "mfe_pct": 15.0, "mae_pct": 10.0})
        bars.minutes = 15
        self.assertEqual(dr.excursions(bars, START, 15, 1, 10 ** 19, Fraction(1)), (values, None))
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
        candles = SimpleNamespace(valid=[True, False, True],
                                  end_ms=[signal, signal + 15 * 60_000, signal + 30 * 60_000],
                                  open=[100 * scale], high=[110 * scale],
                                  low=[90 * scale], close=[100 * scale])
        features = CandleFeatures(index=0, high=110, low=90, range=20, body=0, body_ratio=0,
                                 upper_ratio=.5, lower_ratio=.5, atr_prev=10, range_atr=2,
                                 rel_volume=1, adx_prev=30, trend_dir=1, prior20_high=110, prior20_low=90)
        params = LabelParams(k_grid=(Fraction(1),), rr_grid=(Fraction(1),))
        trade = LabelRow(signal, 15, 1, Fraction(1), "T", 100 * scale, 10 ** 19, 10,
                         3, 10_000_000, (CellPair(Cell("T", 1, 800_000, 200_000, 0)),))
        nontrade = LabelRow(signal, 15, -1, Fraction(1), "V")
        rows = [trade, nontrade, replace(trade, signal_ms=signal + 60_000),
                replace(trade, signal_ms=signal + 15 * 60_000),
                replace(trade, signal_ms=signal + 30 * 60_000)]
        with TemporaryDirectory() as directory:
            (Path(directory) / "labels__BTCUSDT__2024-01.csv.gz").touch()
            with patch.object(dr, "check_segment", return_value=["2024-01"]), \
                    patch.object(dr, "manifest_ticks", return_value={"2024-01": scale}), \
                    patch.object(dr, "load_symbol_bars", return_value=bars), \
                    patch.object(dr, "build_candles", return_value=candles), \
                    patch.object(dr, "doji_readings", return_value=[(0, 1, features, read_doji(features))]), \
                    patch.object(dr, "read_label_csv", return_value=iter(rows)):
                events, unconditional, baseline = dr.collect_symbol(directory, directory, "BTCUSDT",
                                                                     "development", (15,), params)
        self.assertEqual(len(events), 2)
        long, short = events
        self.assertEqual((long["entry_price"], long["stop"], long["target"]), (100, 90, 110))
        self.assertEqual((long["gross_verdict"], long["net_verdict"], long["direction_correct"]), (True, True, "tie"))
        self.assertEqual(short["status"], "V")
        for key in ("gross_verdict", "net_verdict", "direction_correct", "mae_r"):
            self.assertIsNone(short[key])
        self.assertEqual(unconditional[(15, 1, "1")]["ties"], 2)
        self.assertEqual(unconditional[(15, -1, "1")]["ties"], 0)
        self.assertEqual(baseline[("BTCUSDT", 15, 1, "1", "1")].trades, 2)
        self.assertNotIn(("BTCUSDT", 15, -1, "1", "1"), baseline)

    def test_empty_primary_cells_and_public_redaction(self):
        unconditional = {("BTCUSDT", 15, side, str(k)): dr.direction_stats([True, False, "tie"])
                         for side in (1, -1) for k in LabelParams().k_grid}
        report = dr.build_report([], unconditional, {}, segment="development", symbols=("BTCUSDT",),
                                 horizons=(15,), params=LabelParams(), code_commit="abc", created_utc="2026-10-14T00:00:00Z",
                                 data_snapshot_id="snapshot", data_tags=["rd-BTCUSDT-2024-01-r1"], label_tags=["lb1-test"])
        self.assertEqual(len(report["primary"]), 4)
        md = dr.markdown(report, [])
        self.assertIn("## Primary (pre-specified)", md)
        self.assertIn("## Tier (descriptive)", md)
        self.assertIn("## Subtype (descriptive)", md)
        self.assertIn("time UTC | symbol | score | subtype | doji O/H/L/C", md)
        self.assertIn("verdict (X: unmeasured)", md)
        self.assertEqual(dr.public_lines(report, record_trials=False)[-1], "experiment ledger skipped")
        rerun = dr.build_report([], unconditional, {}, segment="development", symbols=("BTCUSDT",),
                                horizons=(15,), params=LabelParams(), code_commit="abc", created_utc="2026-10-15T00:00:00Z",
                                data_snapshot_id="snapshot", data_tags=["rd-BTCUSDT-2024-01-r1"], label_tags=["lb1-test"])
        self.assertEqual(report["report_hash"], rerun["report_hash"])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = dr.write_report(root, report, [])
            original = {path: (root / path).read_bytes() for path in paths}
            self.assertEqual(dr.write_report(root, rerun, []), [])
            self.assertEqual({path: (root / path).read_bytes() for path in paths}, original)
        report["primary"][0]["trade"]["mean_net_r_1x"] = "PRIVATE-RETURN"
        report["primary"][0]["direction"]["hit_rate"] = "PRIVATE-RATE"
        text = "\n".join(dr.public_lines(report))
        self.assertNotIn("PRIVATE", text)
        self.assertIn("n_trades=0", text)
        self.assertIn("private report path reports/doji/development__", text)
        self.assertEqual(len(report["grid"]), 2 * 2 * 2 * 4)

    def test_production_defaults_preserve_six_baselines_and_explicit_doji_questions(self):
        from market_analysis.benchmark.experiment_run import FAMILIES
        from market_analysis.benchmark.ta_strategies import BASELINE_STRATEGIES
        from market_analysis.forward.signals import FORWARD_STRATEGIES

        family = FAMILIES["ta-baselines"]
        self.assertEqual(len(BASELINE_STRATEGIES), 6)
        self.assertEqual(family.default_strategies_for(15), BASELINE_STRATEGIES)
        self.assertTrue(set(dr.STRATEGIES).issubset(family.strategies_for(15)))
        self.assertEqual({spec.name for spec in FORWARD_STRATEGIES.values()}, set(BASELINE_STRATEGIES))


if __name__ == "__main__":
    unittest.main()
