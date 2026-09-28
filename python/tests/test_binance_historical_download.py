"""Fake-HTTP fixtures for official USD-M daily archive acquisition."""

import argparse
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import zipfile

from market_analysis import binance_historical_download as download
from market_analysis.binance_historical_archive import (
    daily_aggtrades_checksum_relative_path, daily_aggtrades_relative_path,
    daily_kline_checksum_relative_path, daily_kline_relative_path,
    required_aggtrade_dates, required_kline_dates,
)
from market_analysis.historical_replay import HistoricalReplayConfig
from market_analysis.movement_metrics import MarketMovementConfig, MarketUniverseInput


OUTPUT = int(datetime(2026, 8, 20, 12, 30, tzinfo=timezone.utc).timestamp() * 1000)
MINUTE = 60_000


def _request(root, *, symbols=("BTCUSDT",), output=OUTPUT):
    config = HistoricalReplayConfig(
        output, output + 15_000,
        movement_config=MarketMovementConfig(
            historical_lookback_ms=2 * MINUTE,
            minimum_historical_coverage_ms=MINUTE))
    return download.BinanceHistoricalDownloadRequest(
        root, MarketUniverseInput("pilot", "v1", symbols), config)


def _zip_bytes(relative, rows):
    buffer = io.BytesIO()
    info = zipfile.ZipInfo(f"{relative.stem}.csv", (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, "\n".join(",".join(str(value) for value in row)
                                          for row in rows))
    return buffer.getvalue()


def _responses(request, *, price="100", bad_csv=False):
    responses = {}
    for item in download.plan_binance_usdm_historical_download(request):
        rows = ()
        if item.family == "aggTrades" and item.utc_date == date(2026, 8, 20):
            rows = (("not", "a", "valid", "archive"),) if bad_csv else (
                (1, price, "1", 1, 1, OUTPUT, "false"),)
        payload = _zip_bytes(item.relative_zip_path, rows)
        checksum = hashlib.sha256(payload).hexdigest()
        responses[download.official_archive_url(item.relative_checksum_path)] = (
            f"{checksum}  {item.relative_zip_path.name}\n".encode("ascii"))
        responses[download.official_archive_url(item.relative_zip_path)] = payload
    return responses


class _FakeResponse:
    def __init__(self, payload, *, fail_after_reads=None):
        self.stream = io.BytesIO(payload)
        self.fail_after_reads = fail_after_reads
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.stream.close()

    def read(self, size=-1):
        if self.fail_after_reads is not None and len(self.read_sizes) >= self.fail_after_reads:
            raise URLError("interrupted response")
        self.read_sizes.append(size)
        return self.stream.read(size)


class _FakeTransport:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []
        self.returned = []

    def open(self, url, timeout):
        self.calls.append((url, timeout))
        choice = self.responses[url]
        if isinstance(choice, list):
            behavior = choice.pop(0) if len(choice) > 1 else choice[0]
        else:
            behavior = choice
        if isinstance(behavior, BaseException):
            raise behavior
        response = behavior() if callable(behavior) else _FakeResponse(behavior)
        self.returned.append(response)
        return response


def _acquire(request, transport, sleeps=None):
    pauses = [] if sleeps is None else sleeps
    return download.acquire_binance_usdm_historical_archives(
        request, _transport=transport, _sleeper=pauses.append)


class BinanceHistoricalDownloadTests(unittest.TestCase):
    def test_plan_exact_part2_paths_order_and_official_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            output = int(datetime(2026, 9, 1, 0, 10, tzinfo=timezone.utc).timestamp() * 1000)
            request = _request(Path(folder), symbols=("ETHUSDT", "BTCUSDT"), output=output)
            plan = download.plan_binance_usdm_historical_download(request)
            self.assertEqual(len(plan), 8)
            self.assertEqual(tuple(item.symbol for item in plan),
                             ("ETHUSDT",) * 4 + ("BTCUSDT",) * 4)
            for index, symbol in ((0, "ETHUSDT"), (4, "BTCUSDT")):
                for offset, day in enumerate(required_aggtrade_dates(request.replay_config)):
                    item = plan[index + offset]
                    self.assertEqual(item.family, "aggTrades")
                    self.assertEqual(item.utc_date, day)
                    self.assertEqual(item.relative_zip_path,
                                     daily_aggtrades_relative_path(symbol, day))
                    self.assertEqual(item.relative_checksum_path,
                                     daily_aggtrades_checksum_relative_path(symbol, day))
                for offset, day in enumerate(required_kline_dates(request.replay_config)):
                    item = plan[index + 2 + offset]
                    self.assertEqual(item.family, "klines/1m")
                    self.assertEqual(item.utc_date, day)
                    self.assertEqual(item.relative_zip_path,
                                     daily_kline_relative_path(symbol, day))
                    self.assertEqual(item.relative_checksum_path,
                                     daily_kline_checksum_relative_path(symbol, day))
            agg = daily_aggtrades_relative_path("BTCUSDT", date(2026, 8, 20))
            kline = daily_kline_relative_path("BTCUSDT", date(2026, 8, 20))
            self.assertEqual(download.official_archive_url(agg),
                             "https://data.binance.vision/data/futures/um/daily/aggTrades/"
                             "BTCUSDT/BTCUSDT-aggTrades-2026-08-20.zip")
            self.assertEqual(download.official_archive_url(kline),
                             "https://data.binance.vision/data/futures/um/daily/klines/"
                             "BTCUSDT/1m/BTCUSDT-1m-2026-08-20.zip")
            self.assertEqual(download.official_archive_url(
                daily_kline_checksum_relative_path("BTCUSDT", date(2026, 8, 20))),
                download.official_archive_url(kline) + ".CHECKSUM")
            self.assertEqual(download.official_archive_url(
                daily_aggtrades_checksum_relative_path("BTCUSDT", date(2026, 8, 20))),
                download.official_archive_url(agg) + ".CHECKSUM")

    def test_new_download_cache_reuse_and_checksum_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder))
            transport = _FakeTransport(_responses(request))
            first = _acquire(request, transport)
            plan = download.plan_binance_usdm_historical_download(request)
            self.assertEqual(first.download.requested_item_count, 2)
            self.assertEqual(first.download.downloaded_count, 2)
            self.assertEqual(first.download.reused_count, 0)
            self.assertEqual(tuple(item.status for item in first.download.items),
                             ("DOWNLOADED", "DOWNLOADED"))
            self.assertEqual(tuple(item.symbol for item in first.download.items),
                             ("BTCUSDT", "BTCUSDT"))
            for item in plan:
                path = request.archive_root.joinpath(*item.relative_zip_path.parts)
                self.assertEqual(path.read_bytes(), transport.responses[
                    download.official_archive_url(item.relative_zip_path)])
                self.assertTrue(request.archive_root.joinpath(
                    *item.relative_checksum_path.parts).is_file())
            expected_urls = tuple(url for item in plan for url in (
                download.official_archive_url(item.relative_checksum_path),
                download.official_archive_url(item.relative_zip_path)))
            self.assertEqual(tuple(url for url, _ in transport.calls), expected_urls)
            self.assertTrue(all(timeout == 30 for _, timeout in transport.calls))
            checksum = request.archive_root.joinpath(*plan[0].relative_checksum_path.parts)
            checksum.unlink()
            before_calls = len(transport.calls)
            second = _acquire(request, transport)
            self.assertEqual(second.download.reused_count, 2)
            self.assertEqual(second.download.downloaded_count, 0)
            self.assertEqual(second.download.bytes_downloaded, 0)
            self.assertEqual(tuple(url for url, _ in transport.calls[before_calls:]),
                             tuple(download.official_archive_url(item.relative_checksum_path)
                                   for item in plan))
            self.assertTrue(checksum.is_file())
            self.assertEqual(first.dataset, second.dataset)
            checksum.write_text("stale\n", encoding="utf-8")
            third = _acquire(request, transport)
            self.assertEqual(third.download.reused_count, 2)
            self.assertNotEqual(checksum.read_text(encoding="utf-8"), "stale\n")

    def test_multiple_symbol_result_preserves_universe_order(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder), symbols=("ETHUSDT", "BTCUSDT"))
            result = _acquire(request, _FakeTransport(_responses(request)))
            self.assertEqual(tuple(item.symbol for item in result.download.items),
                             ("ETHUSDT", "ETHUSDT", "BTCUSDT", "BTCUSDT"))

    def test_remote_revision_refreshes_zip_and_dataset_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder))
            transport = _FakeTransport(_responses(request))
            before = _acquire(request, transport)
            item = download.plan_binance_usdm_historical_download(request)[0]
            path = request.archive_root.joinpath(*item.relative_zip_path.parts)
            old_bytes = path.read_bytes()
            revised_zip = _zip_bytes(item.relative_zip_path,
                                     ((1, "101", "1", 1, 1, OUTPUT, "false"),))
            revised_sha = hashlib.sha256(revised_zip).hexdigest()
            transport.responses[download.official_archive_url(item.relative_checksum_path)] = (
                f"{revised_sha}  {item.relative_zip_path.name}\n".encode("ascii"))
            transport.responses[download.official_archive_url(item.relative_zip_path)] = revised_zip
            after = _acquire(request, transport)
            self.assertEqual(after.download.refreshed_count, 1)
            self.assertEqual(after.download.reused_count, 1)
            self.assertNotEqual(path.read_bytes(), old_bytes)
            self.assertEqual(path.read_bytes(), revised_zip)
            self.assertNotEqual(before.dataset.archive_manifest.content_sha256,
                                after.dataset.archive_manifest.content_sha256)
            self.assertEqual(after.download.items[0].expected_sha256, revised_sha)

    def test_failed_refresh_and_checksum_mismatch_preserve_old_zip(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder))
            transport = _FakeTransport(_responses(request))
            _acquire(request, transport)
            item = download.plan_binance_usdm_historical_download(request)[0]
            path = request.archive_root.joinpath(*item.relative_zip_path.parts)
            old_bytes = path.read_bytes()
            new_zip = _zip_bytes(item.relative_zip_path,
                                 ((1, "103", "1", 1, 1, OUTPUT, "false"),))
            new_sha = hashlib.sha256(new_zip).hexdigest()
            checksum_url = download.official_archive_url(item.relative_checksum_path)
            zip_url = download.official_archive_url(item.relative_zip_path)
            transport.responses[checksum_url] = f"{new_sha}  {item.relative_zip_path.name}\n".encode()
            transport.responses[zip_url] = URLError("network failed")
            sleeps = []
            with self.assertRaisesRegex(download.BinanceArchiveDownloadError,
                                        r"after 3 attempt"):
                _acquire(request, transport, sleeps)
            self.assertEqual(sleeps, [1, 2])
            self.assertEqual(path.read_bytes(), old_bytes)
            transport.responses[zip_url] = old_bytes
            with self.assertRaisesRegex(ValueError, "download checksum mismatch"):
                _acquire(request, transport)
            self.assertEqual(path.read_bytes(), old_bytes)
            self.assertEqual(tuple(path.parent.glob(f".{path.name}.download-*.tmp")), ())

    def test_missing_remote_permanent_errors_and_retries(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder))
            item = download.plan_binance_usdm_historical_download(request)[0]
            checksum_url = download.official_archive_url(item.relative_checksum_path)
            zip_url = download.official_archive_url(item.relative_zip_path)
            base = _responses(request)
            missing = _FakeTransport({**base, checksum_url: HTTPError(
                checksum_url, 404, "missing", None, None)})
            with self.assertRaises(download.BinanceArchiveRemoteMissingError) as caught:
                _acquire(request, missing)
            self.assertIn("BTCUSDT aggTrades 2026-08-20", str(caught.exception))
            self.assertIn(str(item.relative_checksum_path), str(caught.exception))
            self.assertEqual(len(missing.calls), 1)
            missing_zip = _FakeTransport({**base, zip_url: HTTPError(
                zip_url, 404, "missing", None, None)})
            with self.assertRaises(download.BinanceArchiveRemoteMissingError) as caught:
                _acquire(request, missing_zip)
            self.assertIn(str(item.relative_zip_path), str(caught.exception))
            self.assertEqual(len(missing_zip.calls), 2)
            forbidden = _FakeTransport({**base, checksum_url: HTTPError(
                checksum_url, 403, "forbidden", None, None)})
            with self.assertRaisesRegex(download.BinanceArchiveDownloadError,
                                        r"after 1 attempt"):
                _acquire(request, forbidden)
            self.assertEqual(len(forbidden.calls), 1)
            transient = _FakeTransport({**base, checksum_url: [
                HTTPError(checksum_url, 503, "busy", None, None),
                HTTPError(checksum_url, 503, "busy", None, None),
                base[checksum_url],
            ]})
            sleeps = []
            result = _acquire(request, transient, sleeps)
            self.assertEqual(result.download.items[0].attempts, 4)
            self.assertEqual(sleeps, [1, 2])
            self.assertEqual(sum(url == checksum_url for url, _ in transient.calls), 3)
            self.assertEqual(result.download.downloaded_count, 2)
            with tempfile.TemporaryDirectory() as other:
                alternate = _request(Path(other))
                throttle = _FakeTransport({**_responses(alternate), checksum_url: [
                    HTTPError(checksum_url, 429, "rate limit", None, None),
                    base[checksum_url],
                ]})
                pauses = []
                _acquire(alternate, throttle, pauses)
                self.assertEqual(pauses, [1])

    def test_partial_transfer_cleanup_and_multichunk_streaming(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder))
            item = download.plan_binance_usdm_historical_download(request)[0]
            zip_url = download.official_archive_url(item.relative_zip_path)
            base = _responses(request)
            partial = _FakeTransport({**base, zip_url: lambda: _FakeResponse(
                base[zip_url], fail_after_reads=1)})
            with self.assertRaises(download.BinanceArchiveDownloadError):
                _acquire(request, partial)
            destination = request.archive_root.joinpath(*item.relative_zip_path.parts)
            self.assertFalse(destination.exists())
            self.assertEqual(tuple(destination.parent.glob(f".{destination.name}.download-*.tmp")), ())
            payload = b"x" * (download.BINANCE_DOWNLOAD_CHUNK_BYTES + 17)
            expected = hashlib.sha256(payload).hexdigest()
            transport = _FakeTransport({zip_url: payload})
            temporary, count = download._download_verified_zip(
                item, destination, expected, transport, lambda _: None)[0]
            try:
                self.assertEqual(count, len(payload))
                self.assertEqual(hashlib.sha256(temporary.read_bytes()).hexdigest(), expected)
                self.assertEqual(transport.returned[0].read_sizes,
                                 [download.BINANCE_DOWNLOAD_CHUNK_BYTES] * 3)
            finally:
                temporary.unlink(missing_ok=True)

    def test_malformed_remote_checksum_stops_before_zip(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder))
            item = download.plan_binance_usdm_historical_download(request)[0]
            self.assertEqual(download._parse_remote_checksum(
                f"  {'A' * 64} *{item.relative_zip_path.name}  \n".encode(), item),
                "a" * 64)
            checksum_url = download.official_archive_url(item.relative_checksum_path)
            zip_url = download.official_archive_url(item.relative_zip_path)
            base = _responses(request)
            cases = (b"", b"0" * 64 + b"  wrong.zip\n",
                     b"invalid  " + item.relative_zip_path.name.encode() + b"\n",
                     base[checksum_url] + base[checksum_url])
            for payload in cases:
                with self.subTest(payload=payload):
                    transport = _FakeTransport({**base, checksum_url: payload})
                    with self.assertRaises(ValueError):
                        _acquire(request, transport)
                    self.assertNotIn(zip_url, tuple(url for url, _ in transport.calls))

    def test_root_invariance_and_part2_semantic_failure(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            left = _request(Path(first))
            right = _request(Path(second))
            acquired_left = _acquire(left, _FakeTransport(_responses(left)))
            acquired_right = _acquire(right, _FakeTransport(_responses(right)))
            self.assertEqual(acquired_left.dataset.archive_manifest,
                             acquired_right.dataset.archive_manifest)
            self.assertEqual(acquired_left.dataset.replay_request,
                             acquired_right.dataset.replay_request)
            self.assertEqual(acquired_left.dataset.archive_manifest.content_sha256,
                             acquired_right.dataset.archive_manifest.content_sha256)
            malformed = _responses(right, bad_csv=True)
            item = download.plan_binance_usdm_historical_download(right)[0]
            bad_zip = malformed[download.official_archive_url(item.relative_zip_path)]
            with self.assertRaisesRegex(ValueError, "CSV row 1"):
                _acquire(right, _FakeTransport(malformed))
            self.assertEqual(right.archive_root.joinpath(*item.relative_zip_path.parts).read_bytes(),
                             bad_zip)

    def test_cli_timestamp_symbol_order_and_compact_stdout(self):
        self.assertEqual(download.parse_utc_cli_timestamp("2026-08-20T00:00:00Z"),
                         int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp() * 1000))
        for value in ("2026-08-20", "2026-08-20T00:00:00",
                      "2026-08-20T01:00:00+01:00", "tomorrow"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                download.parse_utc_cli_timestamp(value)
        args = ["--archive-root", "/tmp/archive", "--symbols", "ETHUSDT", "BTCUSDT",
                "--universe-id", "pilot", "--universe-version", "v1",
                "--start", "2026-08-20T12:30:00Z", "--end", "2026-08-20T12:30:15Z"]
        parsed = download.build_cli_parser().parse_args(args)
        self.assertEqual(tuple(parsed.symbols), ("ETHUSDT", "BTCUSDT"))
        self.assertEqual(parsed.finalization_grace_ms, 2_000)
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder))
            result = _acquire(request, _FakeTransport(_responses(request)))
            stdout = io.StringIO()
            with (patch.object(download, "acquire_binance_usdm_historical_archives",
                               return_value=result), patch("sys.stdout", stdout)):
                self.assertEqual(download.main(args), 0)
            self.assertEqual(stdout.getvalue(), download.acquisition_summary_json(result) + "\n")
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["dataset_content_sha256"],
                             result.dataset.archive_manifest.content_sha256)
            self.assertNotIn(folder, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
