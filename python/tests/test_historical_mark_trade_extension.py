"""Generated-archive fixtures for the separate #127 mark/trade diagnostic."""

import csv
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from market_analysis import historical_experiment_batch as batch
from market_analysis.historical_mark_price_evidence import (
    daily_mark_price_relative_path, load_binance_usdm_mark_price_evidence,
)
from market_analysis.historical_mark_trade_extension import (
    MARK_TRADE_CONFIG_VERSION, MarkTradePointOutput, MarkTradeWindowOutput,
    _build_candidate_points, _extension_manifest, _partition_summaries,
    _symbol_window_output,
)
from market_analysis.historical_ohlc_evidence import CompletedTradeOHLCCandle
from market_analysis.movement_metrics import Metric


MINUTE = 60_000
BASE = int(datetime(2027, 1, 15, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
END = BASE + 2 * MINUTE
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT")
DAY = datetime.fromtimestamp(BASE / 1000, tz=timezone.utc).date()


def _mark_row(opening, price="100"):
    return [str(opening), price, price, price, price, "0",
            str(opening + MINUTE - 1), "0", "0", "0", "0", "0"]


def _write_mark_archive(root: Path, symbol: str, rows, *, checksum=True):
    relative = daily_mark_price_relative_path(symbol, DAY)
    archive = root.joinpath(*relative.parts)
    archive.parent.mkdir(parents=True, exist_ok=True)
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(f"{relative.stem}.csv", output.getvalue())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if checksum:
        Path(f"{archive}.CHECKSUM").write_text(
            f"{digest}  {relative.name}\n", encoding="ascii")
    return archive, digest


def _expected_opens(start=BASE, end=END):
    first = start - 16 * MINUTE
    last = end // MINUTE * MINUTE
    return tuple(range(first, last, MINUTE))


def _write_universe(root: Path, *, btc_prices=None, btc_rows=None,
                    symbols=SYMBOLS, checksum=True):
    prices = btc_prices or {}
    for symbol in symbols:
        rows = (btc_rows if symbol == "BTCUSDT" and btc_rows is not None else
                [_mark_row(opening, prices.get(opening, "100"))
                 for opening in _expected_opens()])
        _write_mark_archive(root, symbol, rows, checksum=checksum)


def _load(root: Path, symbols=SYMBOLS, *, start=BASE, end=END):
    return load_binance_usdm_mark_price_evidence(
        root, tuple(symbols), start, end)


def _trade_candle(symbol, opening, price="100", *, first_seen=None):
    close_time = opening + MINUTE - 1
    return CompletedTradeOHLCCandle(
        symbol, f"binance-usdm:{symbol}", opening, close_time,
        Decimal(price), Decimal(price), Decimal(price), Decimal(price),
        close_time + 1 if first_seen is None else first_seen,
    )


def _v1_symbol(direction="RISING"):
    return SimpleNamespace(
        included=True, direction=Metric.present(direction),
        material_rising=False, material_falling=False,
    )


def _trade_index(symbol="BTCUSDT", *, prices=None, first_seen_by_open=None):
    prices = prices or {}
    first_seen_by_open = first_seen_by_open or {}
    return {
        (symbol, opening): _trade_candle(
            symbol, opening, prices.get(opening, "100"),
            first_seen=first_seen_by_open.get(opening))
        for opening in _expected_opens()
    }


def _fake_v1_window(window, symbols=SYMBOLS):
    return SimpleNamespace(
        window_minutes=window,
        market_wide_eligible=True,
        eligible_count=len(symbols),
        eligible_fraction=1.0,
        aggregates=SimpleNamespace(
            median_normalized_movement=Metric.present(0.25),
            median_raw_return=Metric.present(0.01),
        ),
        breadth=SimpleNamespace(
            available=True, reason=None, denominator=len(symbols),
            rising=Metric.present(SimpleNamespace(count=2)),
            falling=Metric.present(SimpleNamespace(count=1)),
            flat=Metric.present(SimpleNamespace(count=2)),
        ),
        symbols=tuple(SimpleNamespace(symbol=symbol, **vars(_v1_symbol()))
                      for symbol in symbols),
    )


class HistoricalMarkPriceEvidenceTests(unittest.TestCase):
    def test_verified_checksum_and_invalid_unverified_packages_are_not_usable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_universe(root)
            evidence = _load(root)
            self.assertEqual(len(evidence.candles), len(SYMBOLS) * len(_expected_opens()))
            self.assertTrue(all(item.checksum_verified for item in evidence.packages))

            archive, _ = _write_mark_archive(
                root, "BTCUSDT", [_mark_row(opening) for opening in _expected_opens()])
            Path(f"{archive}.CHECKSUM").write_text(
                f"{'0' * 64}  {archive.name}\n", encoding="ascii")
            mismatched = _load(root)
            btc_package = next(item for item in mismatched.packages
                               if item.symbol == "BTCUSDT")
            self.assertEqual(btc_package.status, "CHECKSUM_MISMATCH")
            self.assertIsNone(mismatched.candle("BTCUSDT", BASE - 16 * MINUTE))
            self.assertEqual(mismatched.unavailable_reason(
                "BTCUSDT", BASE - 16 * MINUTE), "MARK_CHECKSUM_MISMATCH")

            Path(f"{archive}.CHECKSUM").unlink()
            missing_checksum = _load(root)
            self.assertEqual(next(item for item in missing_checksum.packages
                                  if item.symbol == "BTCUSDT").status,
                             "MISSING_CHECKSUM")

    def test_malformed_duplicate_off_grid_and_missing_minutes_have_distinct_statuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [_mark_row(opening) for opening in _expected_opens()]
            malformed_open = BASE - 15 * MINUTE
            rows = [row for row in rows if int(row[0]) != malformed_open]
            rows.append(_mark_row(malformed_open)[:-1])
            duplicate_open = BASE - 14 * MINUTE
            rows.append(_mark_row(duplicate_open))
            rows.append(_mark_row(BASE + 30_000))
            _write_universe(root, btc_rows=rows)
            evidence = _load(root)
            package = next(item for item in evidence.packages
                           if item.symbol == "BTCUSDT")
            self.assertIn("MALFORMED_ROW", package.statuses)
            self.assertIn("DUPLICATE_MINUTE", package.statuses)
            self.assertIn("OFF_GRID_MINUTE", package.statuses)
            self.assertIn("MISSING_VALID_MINUTE", package.statuses)
            self.assertEqual(evidence.unavailable_reason(
                "BTCUSDT", malformed_open), "MARK_INVALID_ROW")
            self.assertEqual(evidence.unavailable_reason(
                "BTCUSDT", duplicate_open), "MARK_DUPLICATE_MINUTE")

    def test_order_and_verified_package_checksum_change_mark_evidence_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_universe(root)
            forward = _load(root)
            reverse = _load(root, tuple(reversed(SYMBOLS)))
            self.assertNotEqual(forward.evidence_sha256, reverse.evidence_sha256)

            prices = {BASE - 16 * MINUTE: "101"}
            changed_root = Path(temporary) / "changed"
            changed_root.mkdir()
            _write_universe(changed_root, btc_prices=prices)
            changed = _load(changed_root)
            self.assertNotEqual(forward.evidence_sha256, changed.evidence_sha256)


class HistoricalMarkTradeDiagnosticTests(unittest.TestCase):
    def test_positive_negative_and_zero_basis_divergence_match_return_difference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            start_open, end_open = BASE - 2 * MINUTE, BASE - MINUTE
            _write_universe(root, btc_prices={
                start_open: "110", end_open: "121",
            })
            mark = _load(root)
            positive = _symbol_window_output(
                "BTCUSDT", 1, BASE, mark,
                {(item.symbol, item.open_time_ms): item for item in mark.candles},
                _trade_index(), _v1_symbol())
            self.assertEqual(positive.status, "READY")
            self.assertGreater(Decimal(positive.divergence), 0)
            with localcontext() as context:
                context.prec = 50
                return_difference = (Decimal(positive.mark_return)
                                     - Decimal(positive.trade_return))
                basis_difference = (Decimal(positive.basis_at_end)
                                    - Decimal(positive.basis_at_start))
            self.assertLessEqual(abs(Decimal(positive.divergence)
                                     - return_difference), Decimal("1e-47"))
            self.assertLessEqual(abs(Decimal(positive.divergence)
                                     - basis_difference), Decimal("1e-47"))

            _write_universe(root, btc_prices={start_open: "110", end_open: "100"})
            negative_mark = _load(root)
            negative = _symbol_window_output(
                "BTCUSDT", 1, BASE, negative_mark,
                {(item.symbol, item.open_time_ms): item for item in negative_mark.candles},
                _trade_index(), _v1_symbol())
            self.assertLess(Decimal(negative.divergence), 0)

            _write_universe(root, btc_prices={start_open: "110", end_open: "110"})
            zero_mark = _load(root)
            zero = _symbol_window_output(
                "BTCUSDT", 1, BASE, zero_mark,
                {(item.symbol, item.open_time_ms): item for item in zero_mark.candles},
                _trade_index(), _v1_symbol())
            self.assertEqual(zero.status, "READY")
            self.assertEqual(Decimal(zero.divergence), 0)

    def test_missing_endpoint_and_interior_minutes_report_source_specific_reasons(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            end_open = BASE - MINUTE
            interior_open = BASE - 3 * MINUTE
            rows = [_mark_row(opening) for opening in _expected_opens()
                    if opening not in (end_open, interior_open)]
            _write_universe(root, btc_rows=rows)
            mark = _load(root)
            mark_index = {(item.symbol, item.open_time_ms): item
                          for item in mark.candles}
            trade_index = _trade_index()
            missing_mark_endpoint = _symbol_window_output(
                "BTCUSDT", 1, BASE, mark, mark_index, trade_index, _v1_symbol())
            self.assertEqual(missing_mark_endpoint.status, "UNAVAILABLE")
            self.assertTrue(any(reason.source == "MARK"
                                and reason.open_time_ms == end_open
                                and reason.reason == "MARK_MISSING_MINUTE"
                                for reason in missing_mark_endpoint.reasons))

            start_open = BASE - 2 * MINUTE
            trade_index.pop(("BTCUSDT", start_open))
            missing_trade_endpoint = _symbol_window_output(
                "BTCUSDT", 1, BASE, mark, mark_index | {
                    (item.symbol, item.open_time_ms): item for item in mark.candles
                }, trade_index, _v1_symbol())
            self.assertTrue(any(reason.source == "TRADE"
                                and reason.open_time_ms == start_open
                                and reason.reason == "TRADE_MISSING_MINUTE"
                                for reason in missing_trade_endpoint.reasons))

            interior = _symbol_window_output(
                "BTCUSDT", 5, BASE, mark, mark_index, trade_index, _v1_symbol())
            self.assertTrue(any(reason.source == "MARK"
                                and reason.open_time_ms == interior_open
                                for reason in interior.reasons))

    def test_minute_boundary_availability_and_16_minute_warmup_ignore_replay_grace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_universe(root)
            mark = _load(root, end=BASE + MINUTE)
            self.assertEqual(mark.expected_open_time_start_ms, BASE - 16 * MINUTE)
            self.assertEqual(mark.first_output_minute_boundary_time_ms, BASE)
            mark_index = {(item.symbol, item.open_time_ms): item
                          for item in mark.candles}
            point_minutes = (BASE + 55_000, BASE + MINUTE)
            windows = {window: _fake_v1_window(window) for window in (1, 5, 15)}
            replay_points = tuple(SimpleNamespace(
                evaluation_boundary_time_ms=boundary,
                point_id=f"point-{boundary}",
                movement_evaluation=SimpleNamespace(windows=windows),
            ) for boundary in point_minutes)
            experiment_points = tuple(SimpleNamespace(partition="development")
                                      for _ in replay_points)
            trade = SimpleNamespace(candles=tuple(
                _trade_candle(symbol, opening) for symbol in SYMBOLS
                for opening in _expected_opens(start=BASE, end=BASE + MINUTE)))
            prepared = SimpleNamespace(
                mark_evidence=mark,
                archive_dataset=SimpleNamespace(ohlc_evidence=trade),
                replay_result=SimpleNamespace(
                    manifest=SimpleNamespace(configured_universe=SYMBOLS),
                    points=replay_points),
                experiment_points=experiment_points,
            )
            output = _build_candidate_points(prepared)
            self.assertEqual([point.evaluation_boundary_time_ms for point in output],
                             [BASE + MINUTE])
            one_minute = output[0].windows[0].symbols[0]
            self.assertEqual(one_minute.mark_valid_candle_open_times_ms[-1], BASE)
            self.assertEqual(one_minute.mark_valid_candle_close_times_ms[-1], BASE + MINUTE - 1)

            late_open = BASE - MINUTE
            late_trade = _trade_index(first_seen_by_open={
                late_open: BASE + 2_000,
            })
            unavailable = _symbol_window_output(
                "BTCUSDT", 1, BASE, mark, mark_index, late_trade, _v1_symbol())
            self.assertTrue(any(reason.reason == "TRADE_CANDLE_NOT_AVAILABLE"
                                for reason in unavailable.reasons))
            self.assertEqual(unavailable.status, "UNAVAILABLE")
            now_visible = _symbol_window_output(
                "BTCUSDT", 5, BASE + MINUTE, mark, mark_index,
                late_trade, _v1_symbol())
            self.assertEqual(now_visible.status, "READY")
            self.assertIn(late_open, now_visible.trade_valid_candle_open_times_ms)
            self.assertEqual(now_visible.reasons, ())

    def test_common_ready_summary_and_extension_identity_changes_are_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_universe(root)
            mark = _load(root)
            mark_index = {(item.symbol, item.open_time_ms): item
                          for item in mark.candles}
            trade_index = {(symbol, opening): _trade_candle(symbol, opening)
                           for symbol in SYMBOLS for opening in _expected_opens()}
            window_outputs = []
            for window in (1, 5, 15):
                rows = tuple(_symbol_window_output(
                    symbol, window, BASE, mark, mark_index, trade_index,
                    _v1_symbol()) for symbol in SYMBOLS)
                window_outputs.append(MarkTradeWindowOutput(
                    window, SimpleNamespace(), True, rows))
            point = MarkTradePointOutput("id", BASE, "development",
                                         tuple(window_outputs))
            summary = _partition_summaries((point,), SYMBOLS)
            self.assertEqual(summary["development"][0]
                             ["all_configured_symbols_ready_boundary_count"], 1)
            self.assertEqual(summary["development"][0]
                             ["per_symbol_coverage"][0]["ready_count"], 1)

            replay_manifest = SimpleNamespace(
                configured_universe=SYMBOLS,
                output_start_boundary_time_ms=BASE,
                output_end_boundary_time_ms=END,
                dataset_id="trade-data", dataset_version="v1",
                dataset_content_sha256="a" * 64,
                run_fingerprint="b" * 64,
                universe_id="fixture", universe_version="v1",
            )
            prepared = SimpleNamespace(
                archive_dataset=SimpleNamespace(ohlc_evidence=SimpleNamespace(
                    evidence_sha256="c" * 64)),
                replay_result=SimpleNamespace(manifest=replay_manifest),
                mark_evidence=mark,
                partition_plan=SimpleNamespace(
                    development_end_boundary_time_ms=BASE,
                    validation_end_boundary_time_ms=BASE + MINUTE),
                experiment_points=(),
            )
            with patch("market_analysis.historical_mark_trade_extension.experiment_stream_sha256",
                       return_value="d" * 64):
                first = _extension_manifest(prepared, "e" * 64, "fixture-revision")
                diagnostic_changed = _extension_manifest(prepared, "f" * 64,
                                                         "fixture-revision")
                reordered = replace(mark, configured_symbols=tuple(reversed(SYMBOLS)))
                order_prepared = SimpleNamespace(
                    **{**vars(prepared), "mark_evidence": reordered})
                order_changed = _extension_manifest(order_prepared, "e" * 64,
                                                    "fixture-revision")
                package = replace(mark.packages[0], archive_sha256="f" * 64)
                package_changed_mark = replace(
                    mark, packages=(package,) + mark.packages[1:])
                package_prepared = SimpleNamespace(
                    **{**vars(prepared), "mark_evidence": package_changed_mark})
                package_changed = _extension_manifest(package_prepared, "e" * 64,
                                                      "fixture-revision")
                with patch("market_analysis.historical_mark_trade_extension.MARK_TRADE_CONFIG_VERSION",
                           "changed-config-v1"):
                    config_changed = _extension_manifest(prepared, "e" * 64,
                                                         "fixture-revision")
            self.assertEqual(first.config_version, MARK_TRADE_CONFIG_VERSION)
            self.assertNotEqual(first.extension_run_fingerprint,
                                diagnostic_changed.extension_run_fingerprint)
            self.assertNotEqual(first.extension_run_fingerprint,
                                order_changed.extension_run_fingerprint)
            self.assertNotEqual(first.extension_run_fingerprint,
                                package_changed.extension_run_fingerprint)
            self.assertNotEqual(first.extension_run_fingerprint,
                                config_changed.extension_run_fingerprint)
            self.assertEqual(first.replay_run_fingerprint,
                             package_changed.replay_run_fingerprint)
            self.assertEqual(first.point_stream_sha256,
                             package_changed.point_stream_sha256)
            suite = tuple((item.experiment_id, item.algorithm_version,
                           item.config_version) for item in batch.EXPERIMENT_SUITE_V1)
            self.assertEqual(len(suite), 28)
            self.assertEqual(tuple((item.experiment_id, item.algorithm_version,
                                    item.config_version)
                                   for item in batch.EXPERIMENT_SUITE_V1), suite)


if __name__ == "__main__":
    unittest.main()
