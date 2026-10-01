"""Generated local-archive fixtures for the Binance USD-M Part 2 adapter."""

import csv
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from market_analysis.binance_historical_archive import (
    ARCHIVE_FIRST_SEEN_POLICY, ARCHIVE_SOURCE_STATE_POLICY,
    BINANCE_ARCHIVE_ADAPTER_VERSION, BINANCE_ARCHIVE_DATASET_VERSION,
    BinanceArchiveCoverageError, BinanceUSDMArchiveRequest,
    _AGG_HEADER, _KLINE_HEADER, _agg_row, _archive_rows, _check_ohlc_movement_parity,
    _checksum, _kline_row,
    daily_aggtrades_checksum_relative_path, daily_aggtrades_relative_path,
    daily_kline_checksum_relative_path, daily_kline_relative_path,
    historical_candle_start_ms, load_binance_usdm_historical_replay_dataset,
    required_aggtrade_dates, required_kline_dates,
    verify_binance_usdm_historical_core_archives,
)
from market_analysis import binance_historical_archive as archive_adapter
from market_analysis import historical_experiment_batch as batch
from market_analysis.experiments.market_state_common import validate_experiment_points
from market_analysis.historical_ohlc_evidence import (
    BinanceTradeOHLCEvidence, OHLC_AVAILABILITY_BASIS,
)
from market_analysis.historical_replay import (
    HistoricalReplayConfig, ReplayPartitionPlan, run_historical_market_replay,
    to_market_state_experiment_points,
)
from market_analysis.historical_taker_flow_evidence import (
    HistoricalTakerFlowEvidenceBuilder,
)
from market_analysis.historical_taker_flow_extension import (
    HistoricalTakerFlowExtensionPrepared,
    run_historical_taker_flow_extension,
)
from market_analysis.movement_metrics import MarketMovementConfig, MarketUniverseInput
from market_analysis.market_episode_lifecycle import MarketEpisodeLifecycleConfig
from market_analysis.movement_classifier import MarketClassifierConfig


SYMBOL = "BTCUSDT"
MINUTE = 60_000


def _ms(year=2026, month=8, day=20, hour=12, minute=30, second=0,
        millisecond=0):
    return (int(datetime(year, month, day, hour, minute, second,
                         tzinfo=timezone.utc).timestamp() * 1000)
            + millisecond)


def _config(output=None, end=None, lookback_minutes=2):
    output = _ms() if output is None else output
    return HistoricalReplayConfig(
        output, output + 15_000 if end is None else end,
        movement_config=MarketMovementConfig(
            historical_lookback_ms=lookback_minutes * MINUTE,
            minimum_historical_coverage_ms=MINUTE))


def _agg(timestamp, aggregate_id=12345, price="60000.1", maker="false"):
    return [str(aggregate_id), price, "0.025", "80001", "80003",
            str(timestamp), maker]


def _kline(opening, close="101", volume="2", quote="202"):
    return [str(opening), "100", "102", "99", close, volume,
            str(opening + MINUTE - 1), quote, "3", "1", "100", "0"]


AGG_HEADER = ["agg_trade_id", "price", "quantity", "first_trade_id",
              "last_trade_id", "transact_time", "is_buyer_maker"]
KLINE_HEADER = ["open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_asset_volume", "number_of_trades",
                "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"]


def _write_archive(root, relative, rows, *, header=None, member_name=None):
    archive = root.joinpath(*relative.parts)
    archive.parent.mkdir(parents=True, exist_ok=True)
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    if header is not None:
        writer.writerow(header)
    writer.writerows(rows)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        member = zipfile.ZipInfo(member_name or f"{relative.stem}.csv",
                                 date_time=(1980, 1, 1, 0, 0, 0))
        member.compress_type = zipfile.ZIP_DEFLATED
        bundle.writestr(member, text.getvalue())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(f"{archive}.CHECKSUM").write_text(
        f"{digest}  {relative.name}\n", encoding="utf-8")
    return digest


def _bundle(root, config=None, symbols=(SYMBOL,), *, trade_rows=None,
            kline_rows=None, reverse=False):
    config = _config() if config is None else config
    trade_rows = {} if trade_rows is None else trade_rows
    kline_rows = {} if kline_rows is None else kline_rows
    items = []
    for symbol in symbols:
        for day in required_aggtrade_dates(config):
            items.append((daily_aggtrades_relative_path(symbol, day),
                          trade_rows.get((symbol, day), ())))
        for day in required_kline_dates(config):
            items.append((daily_kline_relative_path(symbol, day),
                          kline_rows.get((symbol, day), ())))
    for relative, rows in reversed(items) if reverse else items:
        _write_archive(root, relative, rows)
    universe = MarketUniverseInput("test-universe", "v1", symbols)
    return BinanceUSDMArchiveRequest(root, universe, config)


def _single_day_bundle(root, *, trades=None, klines=None, symbols=(SYMBOL,),
                       reverse=False):
    config = _config()
    day = date(2026, 8, 20)
    if trades is None:
        trades = (_agg(config.output_start_boundary_time_ms),)
    if klines is None:
        klines = (_kline(historical_candle_start_ms(config)),)
    return _bundle(root, config, symbols,
                   trade_rows={(symbol, day): trades for symbol in symbols},
                   kline_rows={(symbol, day): klines for symbol in symbols},
                   reverse=reverse)


class BinanceHistoricalArchiveTests(unittest.TestCase):
    def test_exact_daily_paths_versions_and_midnight_dates(self):
        day = date(2026, 8, 20)
        agg = "data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-08-20.zip"
        kline = "data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-08-20.zip"
        self.assertEqual(str(daily_aggtrades_relative_path(SYMBOL, day)), agg)
        self.assertEqual(str(daily_aggtrades_checksum_relative_path(SYMBOL, day)),
                         agg + ".CHECKSUM")
        self.assertEqual(str(daily_kline_relative_path(SYMBOL, day)), kline)
        self.assertEqual(str(daily_kline_checksum_relative_path(SYMBOL, day)),
                         kline + ".CHECKSUM")
        self.assertEqual(BINANCE_ARCHIVE_ADAPTER_VERSION,
                         "binance-usdm-daily-archive-adapter-v1")
        self.assertEqual(BINANCE_ARCHIVE_DATASET_VERSION,
                         "binance-usdm-daily-archive-v1")
        self.assertEqual(ARCHIVE_FIRST_SEEN_POLICY, "exchange-timestamp-surrogate-v1")
        self.assertEqual(ARCHIVE_SOURCE_STATE_POLICY,
                         "verified-archive-coverage-live-v1")
        with self.assertRaises(ValueError):
            daily_kline_relative_path(SYMBOL, day, "5m")

    def test_required_dates_cross_month_and_midnight_boundary(self):
        output = _ms(2026, 9, 1, 0, 10)
        config = _config(output=output)
        self.assertEqual(required_aggtrade_dates(config),
                         (date(2026, 8, 31), date(2026, 9, 1)))
        self.assertEqual(historical_candle_start_ms(config),
                         output - 2 * MINUTE - 16 * MINUTE)
        self.assertEqual(required_kline_dates(config),
                         (date(2026, 8, 31), date(2026, 9, 1)))
        midnight = _config(output=_ms(2026, 9, 1, 0, 0))
        self.assertEqual(required_aggtrade_dates(midnight)[-1], date(2026, 9, 1))
        self.assertEqual(required_kline_dates(midnight)[-1], date(2026, 9, 1))
        with self.assertRaises(ValueError):
            required_kline_dates(_config(output=_ms(1970, 1, 1, 0, 31),
                                         lookback_minutes=60))

    def test_checksum_success_star_mismatch_and_wrong_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            relative = daily_aggtrades_relative_path(SYMBOL, date(2026, 8, 20))
            expected = _write_archive(root, relative, [_agg(_ms())])
            self.assertEqual(_checksum(root, relative, SYMBOL, "aggTrades", date(2026, 8, 20)),
                             expected)
            checksum = Path(f"{root.joinpath(*relative.parts)}.CHECKSUM")
            checksum.write_text(f"{expected.upper()} *{relative.name}\n", encoding="utf-8")
            self.assertEqual(_checksum(root, relative, SYMBOL, "aggTrades", date(2026, 8, 20)),
                             expected)
            checksum.write_text(f"{'0' * 64}  {relative.name}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, f"expected {'0' * 64}, actual {expected}"):
                _checksum(root, relative, SYMBOL, "aggTrades", date(2026, 8, 20))
            checksum.write_text(f"{expected}  wrong.zip\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must name"):
                _checksum(root, relative, SYMBOL, "aggTrades", date(2026, 8, 20))
            checksum.write_text(f"{expected}  {relative.name}\n{expected}  {relative.name}\n",
                                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "one entry"):
                _checksum(root, relative, SYMBOL, "aggTrades", date(2026, 8, 20))

    def test_adjacent_utc_daily_archives_keep_midnight_trades(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = _config(output=_ms(2026, 9, 1, 0, 10))
            before = _ms(2026, 8, 31, 23, 59, 59, 999)
            midnight = _ms(2026, 9, 1, 0, 0)
            request = _bundle(root, config, trade_rows={
                (SYMBOL, date(2026, 8, 31)): (_agg(before, 1),),
                (SYMBOL, date(2026, 9, 1)): (_agg(midnight, 2),),
            })
            dataset = load_binance_usdm_historical_replay_dataset(request)
            self.assertEqual(tuple(trade.trade_time_ms for trade in dataset.replay_request.trades),
                             (before, midnight))
            self.assertEqual(dataset.diagnostics.earliest_trade_time_ms, before)
            self.assertEqual(dataset.diagnostics.latest_trade_time_ms, midnight)

    def test_zip_member_safety_and_csv_row_context(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = _single_day_bundle(root)
            relative = daily_aggtrades_relative_path(SYMBOL, date(2026, 8, 20))
            archive = root.joinpath(*relative.parts)
            cases = (
                ((f"{relative.stem}.csv", "other.csv"), "exactly one"),
                (("../escape.csv",), "unsafe ZIP member"),
                (("/absolute.csv",), "unsafe ZIP member"),
                (("wrong.csv",), "exactly one"),
            )
            for names, message in cases:
                with self.subTest(names=names):
                    with zipfile.ZipFile(archive, "w") as bundle:
                        for name in names:
                            bundle.writestr(name, "")
                    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                    Path(f"{archive}.CHECKSUM").write_text(
                        f"{digest}  {relative.name}\n", encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_binance_usdm_historical_replay_dataset(request)
            _write_archive(root, relative, [["bad", "row"]])
            with self.assertRaisesRegex(ValueError, r"BTCUSDT-aggTrades-2026-08-20.zip CSV row 1"):
                load_binance_usdm_historical_replay_dataset(request)

    def test_aggtrade_exact_headerless_header_and_malformed_fields(self):
        day = date(2026, 8, 20)
        timestamp = _ms(millisecond=123)
        row = _agg(timestamp)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            relative = daily_aggtrades_relative_path(SYMBOL, day)
            _write_archive(root, relative, [row])
            from_csv = _archive_rows(root, relative, _AGG_HEADER, _agg_row, day)
            _write_archive(root, relative, [row], header=AGG_HEADER)
            with_header = _archive_rows(root, relative, _AGG_HEADER, _agg_row, day)
            self.assertEqual(from_csv, with_header)
            self.assertEqual(from_csv[0].aggregate_trade_id, 12345)
            self.assertEqual(from_csv[0].price, Decimal("60000.1"))
            self.assertEqual(from_csv[0].quantity, Decimal("0.025"))
            request = _single_day_bundle(root, trades=(row,))
            trade = load_binance_usdm_historical_replay_dataset(request).replay_request.trades[0]
            self.assertEqual((trade.event_time_ms, trade.trade_time_ms,
                              trade.first_seen_at_ms), (timestamp,) * 3)
            self.assertEqual(trade.price, Decimal("60000.1"))
            self.assertEqual(trade.quantity, Decimal("0.025"))
        for index, value in ((0, "-1"), (1, "0"), (1, "-1"), (2, "0"),
                             (2, "-1"), (3, "80004"), (6, "unknown"),
                             (5, str(_ms(day=21)))):
            with self.subTest(index=index, value=value):
                malformed = row.copy()
                malformed[index] = value
                with self.assertRaises(ValueError):
                    _agg_row(malformed, day)
        with self.assertRaises(ValueError):
            _agg_row(row[:-1], day)
        with self.assertRaises(ValueError):
            _agg_row(AGG_HEADER, day)
        self.assertEqual(_agg_row(_agg(_ms(2026, 8, 20, 23, 59, 59, 999)), day).timestamp_ms,
                         _ms(2026, 8, 20, 23, 59, 59, 999))
        self.assertEqual(_agg_row(_agg(_ms(2026, 8, 21, 0, 0)), date(2026, 8, 21)).timestamp_ms,
                         _ms(2026, 8, 21, 0, 0))
        with self.assertRaises(ValueError):
            _agg_row(_agg(_ms(2026, 8, 21, 0, 0)), day)

    def test_kline_exact_header_and_malformed_fields(self):
        day = date(2026, 8, 20)
        opening = _ms(minute=12)
        row = _kline(opening)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            relative = daily_kline_relative_path(SYMBOL, day)
            _write_archive(root, relative, [row])
            headerless = _archive_rows(root, relative, _KLINE_HEADER, _kline_row, day)
            _write_archive(root, relative, [row], header=KLINE_HEADER)
            self.assertEqual(_archive_rows(root, relative, _KLINE_HEADER,
                                           _kline_row, day), headerless)
            request = _single_day_bundle(root, klines=(row,))
            dataset = load_binance_usdm_historical_replay_dataset(request)
            candle = dataset.replay_request.candles[0]
            ohlc = dataset.ohlc_evidence.candles[0]
            self.assertEqual(candle.open_time_ms, opening)
            self.assertEqual(candle.close, Decimal("101"))
            self.assertEqual(candle.volume, Decimal("2"))
            self.assertEqual(candle.quote_volume, Decimal("202"))
            self.assertEqual(candle.first_seen_at_ms, opening + MINUTE)
            self.assertEqual(candle.first_seen_at_ms, int(row[6]) + 1)
            self.assertEqual((ohlc.open, ohlc.high, ohlc.low, ohlc.close),
                             tuple(Decimal(value) for value in row[1:5]))
            self.assertEqual(ohlc.instrument_id, "binance-usdm:BTCUSDT")
            self.assertEqual(ohlc.availability_basis, OHLC_AVAILABILITY_BASIS)
            self.assertEqual((ohlc.symbol, ohlc.open_time_ms, ohlc.close,
                              ohlc.first_seen_at_ms),
                             (candle.symbol, candle.open_time_ms, candle.close,
                              candle.first_seen_at_ms))
            self.assertEqual(dataset.ohlc_evidence.dataset_content_sha256,
                             dataset.archive_manifest.content_sha256)
            self.assertEqual(dataset.ohlc_evidence.as_of(
                SYMBOL, "1m", opening + MINUTE - 5_000).candles, ())
            self.assertEqual(dataset.ohlc_evidence.as_of(
                SYMBOL, "1m", opening + MINUTE).candles[-1].candle, ohlc)
            with self.assertRaisesRegex(ValueError, "disagrees"):
                _check_ohlc_movement_parity(ohlc,
                    replace(candle, close=Decimal("100")))
        for index, value in ((0, str(opening + 1)), (0, str(_ms(day=21))),
                             (6, str(opening + MINUTE)), (5, "-1"),
                             (7, "-1"), (2, "98"), (1, "103"), (4, "103")):
            with self.subTest(index=index, value=value):
                malformed = row.copy()
                malformed[index] = value
                with self.assertRaises(ValueError):
                    _kline_row(malformed, day)
        with self.assertRaises(ValueError):
            _kline_row(row[:-1], day)
        with self.assertRaises(ValueError):
            _kline_row(KLINE_HEADER, day)

    def test_root_and_write_order_invariance_archive_revisions(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            request_a = _single_day_bundle(Path(first), reverse=False)
            request_b = _single_day_bundle(Path(second), reverse=True)
            a = load_binance_usdm_historical_replay_dataset(request_a)
            b = load_binance_usdm_historical_replay_dataset(request_b)
            self.assertEqual(a, b)
            self.assertEqual(a.archive_manifest.content_sha256,
                             b.replay_request.dataset.content_sha256)
            self.assertNotIn(first, str(a.archive_manifest))
            relative = daily_aggtrades_relative_path(SYMBOL, date(2026, 8, 20))
            _write_archive(Path(second), relative, [_agg(_ms(), price="60000.2")])
            revised = load_binance_usdm_historical_replay_dataset(request_b)
            self.assertNotEqual(a.archive_manifest.archive_files,
                                revised.archive_manifest.archive_files)
            self.assertNotEqual(a.archive_manifest.content_sha256,
                                revised.archive_manifest.content_sha256)
            self.assertNotEqual(a.replay_request.dataset.content_sha256,
                                revised.replay_request.dataset.content_sha256)

    def test_duplicate_aggtrades_klines_and_internal_gaps(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = _config()
            day = date(2026, 8, 20)
            first_open = historical_candle_start_ms(config)
            rows = (_kline(first_open), _kline(first_open + MINUTE),
                    _kline(first_open + 3 * MINUTE), _kline(first_open))
            request = _single_day_bundle(
                root, trades=(_agg(_ms()), _agg(_ms(), price="60000.10")),
                klines=rows)
            dataset = load_binance_usdm_historical_replay_dataset(request)
            compact = verify_binance_usdm_historical_core_archives(request)
            self.assertEqual(compact.archive_manifest, dataset.archive_manifest)
            self.assertEqual(compact.ohlc_evidence, dataset.ohlc_evidence)
            self.assertTrue(compact.raw_replayable_trade_evidence_present)
            self.assertEqual(dataset.diagnostics.duplicate_aggtrade_count, 1)
            self.assertEqual(dataset.diagnostics.aggtrade_row_count, 2)
            self.assertEqual(dataset.diagnostics.kline_row_count, 4)
            self.assertEqual(len(dataset.replay_request.trades), 1)
            self.assertEqual(tuple(candle.open_time_ms for candle in dataset.replay_request.candles),
                             (first_open, first_open + MINUTE,
                              first_open + 3 * MINUTE))
            self.assertEqual(tuple(candle.open_time_ms for candle in dataset.ohlc_evidence.candles),
                             (first_open, first_open + MINUTE,
                              first_open + 3 * MINUTE))
            self.assertEqual(dataset.diagnostics.missing_kline_minute_count, 1)
            self.assertEqual(dataset.diagnostics.symbols_with_kline_gaps, (SYMBOL,))
            relative = daily_aggtrades_relative_path(SYMBOL, day)
            _write_archive(root, relative, (_agg(_ms()), _agg(_ms(), price="60000.2")))
            with self.assertRaisesRegex(ValueError, "conflicting aggTrade ID"):
                load_binance_usdm_historical_replay_dataset(request)
            with self.assertRaisesRegex(ValueError, "conflicting aggTrade ID"):
                verify_binance_usdm_historical_core_archives(request)
            _write_archive(root, relative, (_agg(_ms()),))
            relative = daily_kline_relative_path(SYMBOL, day)
            _write_archive(root, relative, (_kline(first_open),
                                            _kline(first_open, close="100")))
            with self.assertRaisesRegex(ValueError, "conflicting kline open"):
                load_binance_usdm_historical_replay_dataset(request)

    def test_core_verifier_consumes_streaming_rows_not_materializing_wrapper(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _single_day_bundle(Path(folder))
            with patch.object(archive_adapter, "_archive_rows",
                              side_effect=AssertionError("materializing parser used")):
                result = verify_binance_usdm_historical_core_archives(request)
            self.assertTrue(result.raw_replayable_trade_evidence_present)

    def test_full_daily_rows_are_filtered_to_replay_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            config = _config()
            start = config.engine_start_boundary_time_ms
            output = config.output_start_boundary_time_ms
            candle_start = historical_candle_start_ms(config)
            request = _bundle(
                Path(folder), config,
                trade_rows={(SYMBOL, date(2026, 8, 20)): (
                    _agg(start - 5_001, 1), _agg(start - 1, 2),
                    _agg(output + 15_001, 3))},
                kline_rows={(SYMBOL, date(2026, 8, 20)): (
                    _kline(candle_start - MINUTE), _kline(candle_start),
                    _kline(output + MINUTE))})
            dataset = load_binance_usdm_historical_replay_dataset(request)
            self.assertEqual(tuple(trade.aggregate_trade_id
                                   for trade in dataset.replay_request.trades), (2,))
            self.assertEqual(tuple(candle.open_time_ms
                                   for candle in dataset.replay_request.candles),
                             (candle_start,))
            self.assertEqual(dataset.diagnostics.aggtrade_row_count, 3)
            self.assertEqual(dataset.diagnostics.kline_row_count, 3)

    def test_multiday_coverage_universe_order_and_missing_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = _ms(2026, 9, 1, 0, 10)
            config = _config(output=output)
            symbols = ("ETHUSDT", "BTCUSDT")
            request = _bundle(root, config, symbols,
                              trade_rows={(symbol, date(2026, 9, 1)): (_agg(output, index + 1),)
                                          for index, symbol in enumerate(symbols)})
            dataset = load_binance_usdm_historical_replay_dataset(request)
            replay = dataset.replay_request
            self.assertEqual(tuple(item.symbol for item in replay.instruments), symbols)
            self.assertEqual(tuple(item.symbol for item in replay.source_intervals), symbols)
            self.assertEqual(tuple(item.symbol for item in replay.trades), symbols)
            self.assertEqual(dataset.diagnostics.archive_file_count, 8)
            self.assertEqual(dataset.diagnostics.verified_archive_file_count, 8)
            self.assertEqual(dataset.diagnostics.aggtrade_archive_count, 4)
            self.assertEqual(dataset.diagnostics.kline_archive_count, 4)
            for item in replay.source_intervals:
                self.assertEqual((item.start_boundary_time_ms, item.end_boundary_time_ms,
                                  item.source_state),
                                 (config.engine_start_boundary_time_ms,
                                  config.output_end_boundary_time_ms, "LIVE"))
            missing = root.joinpath(*daily_kline_relative_path(
                SYMBOL, date(2026, 8, 31)).parts)
            missing.unlink()
            with self.assertRaisesRegex(BinanceArchiveCoverageError,
                                        r"klines/1m.*BTCUSDT.*2026-08-31.*data/futures"):
                load_binance_usdm_historical_replay_dataset(request)
            _write_archive(root, daily_kline_relative_path(SYMBOL, date(2026, 8, 31)), ())
            Path(f"{missing}.CHECKSUM").unlink()
            with self.assertRaisesRegex(BinanceArchiveCoverageError, r"\.zip\.CHECKSUM"):
                load_binance_usdm_historical_replay_dataset(request)
            with self.assertRaises(ValueError):
                BinanceUSDMArchiveRequest(root,
                                          MarketUniverseInput("u", "v", ("BTCUSDT_260925",)),
                                          config)
            self.assertEqual(request.universe.symbols, symbols)

    def test_end_to_end_replay_and_experiment_export(self):
        with tempfile.TemporaryDirectory() as folder:
            dataset = load_binance_usdm_historical_replay_dataset(
                _single_day_bundle(Path(folder)))
            replay = run_historical_market_replay(dataset.replay_request)
            self.assertEqual(len(replay.points), 4)
            self.assertEqual(replay.manifest.dataset_content_sha256,
                             dataset.archive_manifest.content_sha256)
            self.assertEqual(dataset.ohlc_evidence.dataset_content_sha256,
                             replay.manifest.dataset_content_sha256)
            start = dataset.replay_request.config.output_start_boundary_time_ms
            points = to_market_state_experiment_points(
                replay, ReplayPartitionPlan(start + 5_000, start + 10_000))
            validate_experiment_points(points)
            self.assertEqual(len(points), len(replay.points))
            original = batch._suite_manifest(
                replay, dataset, ReplayPartitionPlan(start + 5_000, start + 10_000),
                points, MarketClassifierConfig(), MarketEpisodeLifecycleConfig())
            altered = replace(dataset, ohlc_evidence=BinanceTradeOHLCEvidence(
                dataset.archive_manifest.dataset_id,
                dataset.archive_manifest.dataset_version,
                dataset.archive_manifest.content_sha256,
                dataset.replay_request.universe.symbols, ()))
            self.assertNotEqual(dataset.ohlc_evidence.evidence_sha256,
                                altered.ohlc_evidence.evidence_sha256)
            self.assertEqual(dataset.replay_request, altered.replay_request)
            self.assertEqual(run_historical_market_replay(altered.replay_request), replay)
            self.assertEqual(original, batch._suite_manifest(
                replay, altered, ReplayPartitionPlan(start + 5_000, start + 10_000),
                points, MarketClassifierConfig(), MarketEpisodeLifecycleConfig()))

    def test_dataset_rejects_mismatched_ohlc_dataset_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            dataset = load_binance_usdm_historical_replay_dataset(
                _single_day_bundle(Path(folder)))
            identity_changes = (
                ("dataset_id", "different-dataset"),
                ("dataset_version", "different-version"),
                ("dataset_content_sha256", hashlib.sha256(
                    b"different-dataset-content").hexdigest()),
            )
            for field, value in identity_changes:
                with self.subTest(field=field):
                    evidence = replace(dataset.ohlc_evidence, **{field: value})
                    with self.assertRaisesRegex(ValueError, "dataset identity"):
                        replace(dataset, ohlc_evidence=evidence)

    def test_dataset_rejects_ohlc_symbols_in_different_order(self):
        symbols = ("BTCUSDT", "ETHUSDT")
        with tempfile.TemporaryDirectory() as folder:
            dataset = load_binance_usdm_historical_replay_dataset(
                _single_day_bundle(Path(folder), symbols=symbols))
            evidence = replace(
                dataset.ohlc_evidence,
                configured_symbols=tuple(reversed(symbols)),
            )
            with self.assertRaisesRegex(ValueError, "symbols in order"):
                replace(dataset, ohlc_evidence=evidence)

    def test_taker_flow_archive_parity_and_legacy_identity_isolation(self):
        symbols = ("BTCUSDT", "ETHUSDT")
        config = _config()
        day = date(2026, 8, 20)
        output = config.output_start_boundary_time_ms
        trades = {}
        klines = {}
        for symbol_index, symbol in enumerate(symbols):
            rows = []
            aggregate_id = symbol_index * 10_000
            baseline_maker = "false" if symbol_index == 0 else "true"
            for boundary in range(config.engine_start_boundary_time_ms,
                                  config.output_end_boundary_time_ms + 1,
                                  5_000):
                aggregate_id += 1
                rows.append(_agg(boundary, aggregate_id, maker=baseline_maker))
            for price, maker in (("10", "false"), ("30", "true")):
                aggregate_id += 1
                rows.append(_agg(output, aggregate_id, price=price, maker=maker))
            trades[symbol, day] = tuple(rows)
            klines[symbol, day] = (_kline(historical_candle_start_ms(config)),)

        with tempfile.TemporaryDirectory() as folder:
            request = _bundle(
                Path(folder), config, symbols,
                trade_rows=trades, kline_rows=klines)
            suite_before = tuple((item.experiment_id, item.config_version)
                                 for item in batch.EXPERIMENT_SUITE_V1)
            self.assertEqual(len(suite_before), 28)
            legacy_dataset = load_binance_usdm_historical_replay_dataset(request)
            flow_dataset = load_binance_usdm_historical_replay_dataset(
                request, include_taker_flow_evidence=True)
            self.assertIsNone(legacy_dataset.taker_flow_evidence)
            self.assertIsNotNone(flow_dataset.taker_flow_evidence)
            self.assertEqual(legacy_dataset.archive_manifest,
                             flow_dataset.archive_manifest)
            self.assertEqual(legacy_dataset.replay_request,
                             flow_dataset.replay_request)
            legacy_replay = run_historical_market_replay(legacy_dataset.replay_request)
            flow_replay = run_historical_market_replay(flow_dataset.replay_request)
            self.assertEqual(legacy_replay, flow_replay)
            start = config.output_start_boundary_time_ms
            plan = ReplayPartitionPlan(start + 5_000, start + 10_000)
            legacy_points = to_market_state_experiment_points(legacy_replay, plan)
            flow_points = to_market_state_experiment_points(flow_replay, plan)
            self.assertEqual(legacy_points, flow_points)
            legacy_suite = batch._suite_manifest(
                legacy_replay, legacy_dataset, plan, legacy_points,
                MarketClassifierConfig(), MarketEpisodeLifecycleConfig())
            flow_suite = batch._suite_manifest(
                flow_replay, flow_dataset, plan, flow_points,
                MarketClassifierConfig(), MarketEpisodeLifecycleConfig())
            self.assertEqual(legacy_suite, flow_suite)
            self.assertEqual(suite_before, tuple(
                (item.experiment_id, item.config_version)
                for item in batch.EXPERIMENT_SUITE_V1))
            prepared = HistoricalTakerFlowExtensionPrepared(
                flow_dataset, flow_replay, flow_points, plan)
            report = run_historical_taker_flow_extension(
                prepared, code_revision="fixture-revision")
            for point in report.candidate_points:
                for window in point.windows:
                    for metric in window.symbols:
                        self.assertEqual(metric.parity_status, "MATCH")
                        self.assertEqual(metric.gross_quote_notional,
                                         metric.v1_current_notional_volume)

            identity_mismatch = HistoricalTakerFlowEvidenceBuilder(
                dataset_id="different-dataset", dataset_version="v1",
                dataset_content_sha256=flow_dataset.archive_manifest.content_sha256,
                configured_symbols=symbols,
                engine_start_boundary_time_ms=config.engine_start_boundary_time_ms,
                output_end_boundary_time_ms=config.output_end_boundary_time_ms,
                finalization_grace_ms=config.finalization_grace_ms,
            ).build()
            with self.assertRaisesRegex(ValueError, "taker flow evidence"):
                replace(flow_dataset, taker_flow_evidence=identity_mismatch)
            order_mismatch = HistoricalTakerFlowEvidenceBuilder(
                dataset_id=flow_dataset.archive_manifest.dataset_id,
                dataset_version=flow_dataset.archive_manifest.dataset_version,
                dataset_content_sha256=flow_dataset.archive_manifest.content_sha256,
                configured_symbols=tuple(reversed(symbols)),
                engine_start_boundary_time_ms=config.engine_start_boundary_time_ms,
                output_end_boundary_time_ms=config.output_end_boundary_time_ms,
                finalization_grace_ms=config.finalization_grace_ms,
            ).build()
            with self.assertRaisesRegex(ValueError, "taker flow evidence"):
                replace(flow_dataset, taker_flow_evidence=order_mismatch)


if __name__ == "__main__":
    unittest.main()
