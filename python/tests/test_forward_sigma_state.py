"""Incremental sigma on small synthetic histories; no servers, files, or lake access."""
from array import array
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from market_analysis.benchmark.bars import BarSeries, MISSING
from market_analysis.benchmark import volatility as v
from market_analysis.forward.setups import FORWARD_PARAMS, ForwardSigma
from market_analysis.forward.sigma_state import SigmaState, advance, empty_state, seed_state, sigma_at

START = 1735689600000
MINUTE = 60_000


def bars():
    n = 24 * 1440 + 20
    prices = array("q", (10**10 + ((i // 5 * 37) % 997) * 10**5 for i in range(n)))
    flags = array("H", [0]) * n
    # An invalid block and a missing minute exercise held levels and skipped windows.
    flags[20 * 1440 + 9] = 65535
    return BarSeries("BTCUSDT", START, n, *[array("q", prices) for _ in range(4)],
                     *[array("q", [0]) * n for _ in range(3)],
                     *[array("q", prices) for _ in range(4)], flags)


def cut(source, start, end):
    columns = [getattr(source, name)[start:end] for name in (
        "open", "high", "low", "close", "volume", "taker_buy_volume", "trades",
        "mark_open", "mark_high", "mark_low", "mark_close", "flags")]
    return BarSeries(source.symbol, source.start_ms + start * MINUTE, end - start, *columns)


class SigmaStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history = bars()
        # Shorten only the calendar gate, not the EWMA, seasonal, ratio or rounding math.
        cls.gate = patch.object(v, "HCAL_MIN_DAYS", 2)
        cls.gate.start()
        cls.addClassCleanup(cls.gate.stop)
        cls.one = seed_state(cls.history, cls.history.end_ms)
        cls.reference = ForwardSigma(cls.history, FORWARD_PARAMS)

    def test_chunked_equals_one_shot_and_full_recompute(self):
        state = empty_state("BTCUSDT", START)
        for a, b in ((0, 22001), (22001, 30004), (30004, self.history.minutes)):
            advance(state, cut(self.history, a, b))
            state = SigmaState.from_record(json.loads(json.dumps(state.to_record())))
        self.assertEqual(state.to_record(), self.one.to_record())
        for h in FORWARD_PARAMS.horizons:
            for minute in range(23 * 1440 + 60, 24 * 1440, FORWARD_PARAMS.step(h)):
                t = START + minute * MINUTE
                expected = self.reference.robust_point(h, t)
                self.assertIsNotNone(expected)
                self.assertEqual(sigma_at(state, t, h), expected[2])
                self.assertEqual(state.points[str(h)][str(t)], list(expected))

    def test_seeded_geometry_equals_full_history_for_both_sides(self):
        from market_analysis.forward.sigma_state import StateSigma
        from market_analysis.forward.setups import build_setup
        from market_analysis.forward.signals import Signal
        adapter = StateSigma(deepcopy(self.one), self.history)
        for h in FORWARD_PARAMS.horizons:
            for side in (-1, 1):
                t = START + (23 * 1440 + 120) * MINUTE
                signal = Signal("signal", f"test:{h}", "test", "BTCUSDT", t, side, h)
                self.assertEqual(build_setup(signal, self.history, adapter, tick=10**5),
                                 build_setup(signal, self.history, self.reference, tick=10**5))

    def test_overlap_is_noop_and_future_is_rejected(self):
        state = deepcopy(self.one)
        before = state.to_record()
        advance(state, self.history)
        advance(state, cut(self.history, 0, 100))
        self.assertEqual(state.to_record(), before)
        with self.assertRaises(ValueError):
            sigma_at(state, state.as_of_ms + MINUTE)

    def test_checksum_version_and_identity_mismatch_raise(self):
        record = self.one.to_record()
        for key, value in (("checksum", "0" * 64), ("sigma_version", "unknown"), ("as_of_ms", 0)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                SigmaState.from_record({**record, key: value})
        with self.assertRaises(ValueError):
            SigmaState.from_record(record, symbol="ETHUSDT")
        self.assertIsInstance(record["payload"]["ewma"]["1"][0], str)

    def test_seed_cutoff_and_pending_windows_do_not_peek(self):
        cutoff = START + 23 * v.DAY_MS
        state = seed_state(self.history, cutoff)
        truncated = seed_state(cut(self.history, 0, 23 * 1440), cutoff)
        self.assertEqual(state.to_record(), truncated.to_record())
        self.assertTrue(state.pending)
        self.assertTrue(all(window[0] >= cutoff for window in state.pending))
        advance(state, self.history)
        self.assertEqual(state.to_record(), self.one.to_record())


class MarketStateTests(unittest.TestCase):
    def test_market_round_trip_and_corruption_requires_explicit_rebuild(self):
        from market_analysis.forward.evaluate import market_evaluate
        from market_analysis.forward.sigma_state import SigmaStateMismatch
        data = [{"open_time_ms": START + i * MINUTE, "open": 100.0, "high": 100.1,
                 "low": 99.9, "close": 100.0, "transport": "rest"} for i in range(60)]
        options = dict(strategy_ids=[], from_ms=START, to_ms=START + 59 * MINUTE, use_sigma_state=True)
        first = market_evaluate("BTCUSDT", data, **options)
        second = market_evaluate("BTCUSDT", data, sigma_state=first["sigma_state"], **options)
        self.assertEqual(first, second)
        corrupt = {**first["sigma_state"], "checksum": "0" * 64}
        with self.assertRaises(SigmaStateMismatch):
            market_evaluate("BTCUSDT", data, sigma_state=corrupt, **options)
        self.assertEqual(market_evaluate("BTCUSDT", data, **options)["sigma_state"], first["sigma_state"])
