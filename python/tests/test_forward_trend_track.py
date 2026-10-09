"""Forward trend track (#239 P14, #222): parity with the backtest stream, day shift, gaps, idempotence, funding."""
from __future__ import annotations

from array import array
from decimal import Decimal
from fractions import Fraction
import unittest

import numpy as np

from market_analysis import data_lake
from market_analysis.benchmark import trend
from market_analysis.benchmark.bars import MISSING
from market_analysis.benchmark.funding import FundingSeries
from market_analysis.daily_lake import DailySeries
from market_analysis.forward import trend_track as tt

DAY = tt.DAY_MS
S0 = data_lake.month_bounds_ms("2024-01")[0]
DAYS = 560
SYMBOLS = data_lake.SYMBOLS[:3]
GAPS = {SYMBOLS[1]: (200, 470)}  # one in the warm-up, one inside the tracked window


def fixture(seed=7):
    rng = np.random.default_rng(seed)
    out = {}
    for n, symbol in enumerate(SYMBOLS):
        drift = (0.002, -0.001, 0.0)[n]
        close = 100 * np.exp(np.cumsum(drift + 0.03 * rng.standard_normal(DAYS)))
        open_ = close * np.exp(0.01 * rng.standard_normal(DAYS))
        closes = [int(value * 10 ** 8) for value in close]
        opens = [int(value * 10 ** 8) for value in open_]
        for gap in GAPS.get(symbol, ()):
            closes[gap] = opens[gap] = MISSING
        rates = [Fraction(Decimal(f"{rng.integers(-5, 20) / 100000:.5f}")) for _ in range(3 * (DAYS + 1))]
        out[symbol] = (opens, closes, rates)
    return out


def backtest_symbols(data, first_ms, end_ms):
    symbols = {}
    for symbol, (opens, closes, rates) in data.items():
        high = [max(o, c) if c != MISSING else MISSING for o, c in zip(opens, closes)]
        low = [min(o, c) if c != MISSING else MISSING for o, c in zip(opens, closes)]
        series = DailySeries(symbol, S0, DAYS, array("q", opens), array("q", high), array("q", low),
                             array("q", closes), array("q", [0] * DAYS), array("q", [0] * DAYS))
        times = tuple(S0 + 8 * 3_600_000 * (i + 1) for i in range(len(rates)))
        funding = FundingSeries(times, (8,) * len(times), tuple(rates), S0, times[-1] + 1)
        symbols[symbol] = trend.prepare_symbol(series, funding, first_ms, end_ms)
    return symbols


def forward_symbols(data, *, available=True, funding_to_ms=None, drop=None):
    items = []
    for symbol, (opens, closes, rates) in data.items():
        bars = [{"day_ms": S0 + i * DAY, "open": str(Decimal(o) / 10 ** 8), "close": str(Decimal(c) / 10 ** 8)}
                for i, (o, c) in enumerate(zip(opens, closes)) if c != MISSING and (symbol, i) != drop]
        funding = [{"calc_time_ms": S0 + 8 * 3_600_000 * (i + 1), "rate": str(Decimal(r.numerator) / r.denominator)}
                   for i, r in enumerate(rates)]
        items.append({"symbol": symbol, "bars": bars, "funding": funding, "funding_available": available,
                      "funding_from_ms": S0, "funding_to_ms": funding_to_ms or S0 + (DAYS + 1) * DAY})
    return items


def ledger_of(result, track):
    return [row for row in result["ledger"] if row["track"] == track]


class ParityTests(unittest.TestCase):
    def test_daily_stream_matches_evaluate_trend_day_by_day(self):
        data = fixture()
        first_ms, end_ms = S0 + 420 * DAY, S0 + DAYS * DAY
        symbols = backtest_symbols(data, first_ms, end_ms)
        months = trend._month_labels(first_ms, DAYS - 420)
        forward = tt.evaluate(forward_symbols(data), end_ms - DAY, track_start_ms=first_ms)
        controls = {}
        for variant in trend.VARIANTS:  # the per-variant body of trend.evaluate_trend
            _, daily = trend.evaluate_variant(variant, symbols, months, np.zeros(0, dtype=np.int64),
                                              stream="parity", B=20, seed=1, controls=controls)
            rows = ledger_of(forward, variant.name)
            self.assertEqual([row["day_ms"] for row in rows], list(range(first_ms, end_ms, DAY)))
            self.assertEqual([row["daily_ppm"] for row in rows], daily, variant.name)
        for (sizing, target), daily in controls.items():  # the vol-targeted buy-and-hold controls
            name = f"bh_vt_{sizing}_{target.replace('0.', '')}"
            self.assertEqual([row["daily_ppm"] for row in ledger_of(forward, name)], daily, name)
        self.assertTrue(any(row["daily_ppm"] for row in ledger_of(forward, "ens_ls_25")))
        gap_day = S0 + GAPS[SYMBOLS[1]][1] * DAY
        self.assertEqual({row["symbols_active"] for row in ledger_of(forward, "ew_long") if row["day_ms"] == gap_day},
                         {2})  # the MISSING symbol-day is not evaluated, never filled


class TimingTests(unittest.TestCase):
    def test_close_of_day_d_moves_only_the_weight_of_day_d_plus_1(self):
        closes = [100.0 * 1.01 ** i for i in range(420)]
        decision = S0 + 410 * DAY
        base = tt.target_weights(["tsmom_7"], {"BTCUSDT": (S0, closes)}, decision)["tsmom_7"]["BTCUSDT"]
        later = list(closes)
        later[411] = 1.0  # a close after the decision day
        self.assertEqual(tt.target_weights(["tsmom_7"], {"BTCUSDT": (S0, later)}, decision)["tsmom_7"]["BTCUSDT"],
                         base)
        crash = list(closes)
        crash[410] = 1.0  # the decision day's own close flips the 7-day sign
        moved = tt.target_weights(["tsmom_7"], {"BTCUSDT": (S0, crash)}, decision)["tsmom_7"]["BTCUSDT"]
        self.assertGreater(base[0], 0)
        self.assertLess(moved[0], 0)
        same = tt.target_weights(["tsmom_7"], {"BTCUSDT": (S0, crash)}, decision - DAY)["tsmom_7"]["BTCUSDT"]
        self.assertEqual(same, tt.target_weights(["tsmom_7"], {"BTCUSDT": (S0, closes)}, decision - DAY)
                         ["tsmom_7"]["BTCUSDT"])  # nor the weight held on day d itself

    def test_gap_leaves_the_symbol_flat_and_an_incomplete_day_is_ignored(self):
        data = fixture()
        through = S0 + 450 * DAY
        result = tt.evaluate(forward_symbols(data, drop=(SYMBOLS[0], 440)), through, track_start_ms=S0 + 430 * DAY)
        row = next(r for r in result["weights"] if r["track"] == "ens_ls_25" and r["day_ms"] == S0 + 441 * DAY)
        self.assertEqual((row["weights"][SYMBOLS[0]], row["defined"][SYMBOLS[0]]), (0.0, False))
        day = next(r for r in ledger_of(result, "ew_long") if r["day_ms"] == S0 + 440 * DAY)
        self.assertEqual(day["symbols_active"], 2)
        extra = forward_symbols(data, drop=(SYMBOLS[0], 440))
        for item in extra:
            item["bars"] = [bar for bar in item["bars"] if bar["day_ms"] <= through]
        self.assertEqual(tt.evaluate(extra, through, track_start_ms=S0 + 430 * DAY), result)
        extra[0]["bars"].append({"day_ms": through + DAY, "open": "1", "close": "1"})  # beyond through: unused
        self.assertEqual(tt.evaluate(extra, through, track_start_ms=S0 + 430 * DAY), result)


class StateTests(unittest.TestCase):
    def test_idempotent_and_incremental(self):
        data = fixture()
        start, through = S0 + 500 * DAY, S0 + 520 * DAY
        whole = tt.evaluate(forward_symbols(data), through, track_start_ms=start)
        first = tt.evaluate(forward_symbols(data), through - 5 * DAY, track_start_ms=start)
        rest = tt.evaluate(forward_symbols(data), through, first["states"], track_start_ms=start)
        for track in tt.TRACKS:
            self.assertEqual(ledger_of(first, track.name) + ledger_of(rest, track.name), ledger_of(whole, track.name))
        self.assertEqual(rest["states"], whole["states"])
        again = tt.evaluate(forward_symbols(data), through, whole["states"], track_start_ms=start)
        self.assertEqual((again["ledger"], again["states"]), ([], whole["states"]))  # a re-run adds nothing
        state = whole["states"]["ens_ls_25"]
        self.assertEqual(state["n_days"], 21)
        self.assertEqual(state["sum_ppm"], sum(row["daily_ppm"] for row in ledger_of(whole, "ens_ls_25")))
        self.assertIsNotNone(state["mu_min_daily"])

    def test_funding_unavailable_finalises_nothing(self):
        data = fixture()
        start, through = S0 + 500 * DAY, S0 + 510 * DAY
        failed = tt.evaluate(forward_symbols(data, available=False), through, track_start_ms=start)
        self.assertEqual(failed["ledger"], [])
        self.assertEqual(failed["funding_unavailable"], list(SYMBOLS))
        self.assertTrue(all(reason.startswith("funding_unavailable:") for reason in failed["reasons"].values()))
        self.assertIsNone(failed["through_day_ms"])
        self.assertTrue(failed["weights"])  # weights are still decided and recorded
        short = tt.evaluate(forward_symbols(data, funding_to_ms=start + 3 * DAY), through, track_start_ms=start)
        self.assertEqual(short["through_day_ms"], start + 2 * DAY)  # only fully covered days are finalised

    def test_unknown_state_is_refused(self):
        data = fixture()
        with self.assertRaises(ValueError):
            tt.evaluate(forward_symbols(data), S0 + 510 * DAY, {"ens_ls_25": {"version": "x", "track": "ens_ls_25"}},
                        track_start_ms=S0 + 500 * DAY)


class ApiTests(unittest.TestCase):
    def test_forward_trend_endpoint(self):
        try:
            import asyncio
            import httpx
            from market_analysis.api import create_app
        except ImportError:
            self.skipTest("API dependencies are not installed")
        token = "test-service-token-" + "x" * 32
        data = fixture()
        symbols = forward_symbols(data)
        for item in symbols:
            for bar in item["bars"]:
                low, high = sorted((Decimal(bar["open"]), Decimal(bar["close"])))
                bar.update(high=str(high), low=str(low))
        body = {"schema_version": 1, "symbols": symbols, "through_day_ms": S0 + 505 * DAY,
                "track_start_ms": S0 + 500 * DAY}

        async def send(payload, headers):
            app = create_app(token)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.post("/v1/forward/trend", json=payload, headers=headers)

        response = asyncio.run(send(body, {"Authorization": f"Bearer {token}"}))
        self.assertEqual(response.status_code, 200, response.text)
        direct = tt.evaluate(forward_symbols(data), S0 + 505 * DAY, track_start_ms=S0 + 500 * DAY)
        self.assertEqual([row["daily_ppm"] for row in response.json()["ledger"]],
                         [row["daily_ppm"] for row in direct["ledger"]])
        self.assertEqual(asyncio.run(send(body, {})).status_code, 401)


if __name__ == "__main__":
    unittest.main()
