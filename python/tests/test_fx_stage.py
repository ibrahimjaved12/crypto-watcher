"""One hourly forex market stage (#397): signals, suppression, setups, outcomes, commit shapes, replay.

Every bar is SYNTHETIC: a flat EURUSD bid with a one-step jump (an exact EMA20/EMA50 cross on 15-minute
candles), built as gateway rows exactly as fx_bars_from_rows takes them.
"""

import asyncio
from datetime import datetime, timezone
import copy
import json
import unittest

import httpx

from market_analysis.api import create_app
from market_analysis.fx.stage import FxStageInputError, fx_market_stage
from market_analysis.trading_calendar import FX_WEEK

MINUTE = 60_000
HOUR = 60 * MINUTE
EMA15 = "ema_cross_20_50:15"
PRICE, JUMPED = 1.08, 1.085
TOKEN = "test-service-token-" + "x" * 32


def utc(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 1000


def rows(start, end, jump_ms, drop=()):
    """One row per OPEN minute in [start, end] (closed minutes have no row), flat then jumped at jump_ms."""
    out = []
    for minute in range(start, end + MINUTE, MINUTE):
        if not FX_WEEK.is_open(minute) or minute in drop:
            continue
        price = PRICE if minute < jump_ms else JUMPED
        out.append({"time_basis": "utc", "venue": "test-venue", "native_symbol": "EURUSDm", "transport": "poll",
                    "open_time_ms": minute, "open": price, "high": price, "low": price, "close": price,
                    "spread_points": 1, "tick_volume": 1})
    return out


def symbol(bars, open_setups=()):
    return {"symbol": "EURUSD",
            "venue_symbol": {"venue": "test-venue", "native_symbol": "EURUSDm", "digits": 5, "tick_size": "0.00001"},
            "contract": {"contract_size": "100000", "tick_size": "0.00001", "min_lot": "0.01", "lot_step": "0.01",
                         "base": "EUR", "quote": "USD"},
            "swap_rates": {"long_per_unit": "-0.000073", "short_per_unit": "0.000021"},
            "quote_to_account": "1", "bars": bars, "open_setups": list(open_setups)}


# A Tuesday: 30 hours of 15-minute warm-up from Monday 00:00, the jump at 06:00, the signal at 06:15.
START = utc("2026-10-12T00:00")
JUMP = utc("2026-10-13T06:00")
SIGNAL = JUMP + 15 * MINUTE
BOUNDARY = utc("2026-10-13T07:00")


def request(bars=None, boundary=BOUNDARY, from_ms=BOUNDARY - HOUR, events=None, open_setups=()):
    out = {"boundary_ms": boundary, "from_ms": from_ms, "strategy_ids": [EMA15],
           "symbols": [symbol(rows(START, boundary, JUMP) if bars is None else bars, open_setups)]}
    if events is not None:
        out["calendar_events"] = events
    return out


class StageTests(unittest.TestCase):
    def test_a_known_signal_gives_tradeable_setups_and_open_outcomes(self):
        out = fx_market_stage(request())
        self.assertEqual([(s["signal_ms"], s["side"], s["symbol"], s["instrument_id"]) for s in out["signals"]],
                         [(SIGNAL, 1, "EURUSD", "fx:EURUSD")])
        signal = out["signals"][0]
        self.assertEqual(set(signal), {"signal_id", "strategy_id", "version", "symbol", "instrument_id", "signal_ms",
                                       "side", "horizon_min", "sessions", "candle_version"})
        self.assertIn("london", signal["sessions"])  # 06:15 UTC on a Tuesday in BST
        self.assertEqual(out["suppressed"], [])
        # One setup per (k, rr) of the default grid (k 2; rr 3/2 and 2), all tradeable.
        self.assertEqual([s["status"] for s in out["setups"]], ["T", "T"])
        self.assertEqual(len({s["setup_id"] for s in out["setups"]}), 2)
        setup = out["setups"][0]
        self.assertEqual(set(setup), {"setup_id", "signal_id", "strategy_id", "version", "symbol", "side",
                                      "horizon_min", "signal_ms", "entry_ms", "k", "rr", "status", "geometry_version",
                                      "params_hash", "payload"})
        self.assertEqual(setup["signal_id"], signal["signal_id"])
        self.assertEqual(setup["payload"]["p0"], 108_501_000)  # bid open 1.08500 + spread 1 point, scaled 10**8
        self.assertIsInstance(setup["payload"]["stop"], int)
        # 46 of the 60 window minutes exist by the boundary: still open, never a made-up exit.
        self.assertEqual([(o["setup_id"], o["status"], o["final"]) for o in out["outcomes"]],
                         [(s["setup_id"], "open", False) for s in out["setups"]])
        self.assertEqual(set(out["outcomes"][0]), {"setup_id", "status", "final", "resolved_through_ms", "exit_ms",
                                                   "net_ur", "cost_ur", "fund_ur", "payload"})
        self.assertFalse(out["outcomes"][0]["payload"]["excluded_from_stats"])
        stage = out["stage"]
        self.assertEqual(set(stage), {"id", "boundary_ms", "versions", "params_hash", "payload", "freshness"})
        self.assertEqual((stage["boundary_ms"], stage["params_hash"]), (BOUNDARY, out["params_hash"]))
        self.assertEqual(out["processed_to_ms"], BOUNDARY)
        self.assertEqual(out["events"], [])

    def test_a_friday_close_signal_is_suppressed_market_closed_at_entry(self):
        jump = utc("2026-10-16T20:45")
        boundary = utc("2026-10-16T21:00")  # the FX week closes here
        bars = rows(utc("2026-10-15T12:00"), boundary, jump)
        out = fx_market_stage(request(bars=bars, boundary=boundary, from_ms=boundary - HOUR))
        self.assertEqual(out["signals"], [])
        self.assertEqual([(s["signal_ms"], s["reason"], s["detail"]) for s in out["suppressed"]],
                         [(boundary, "market_closed_at_entry", {})])
        self.assertIn("sessions", out["suppressed"][0])
        self.assertEqual((out["setups"], out["outcomes"]), ([], []))

    def test_a_news_blackout_moves_the_signal_to_suppressed(self):
        event = {"event_id": "test-feed:nfp:1", "ts_ms": SIGNAL, "currency": "USD", "impact": "high",
                 "title": "Non-Farm Payrolls", "first_seen_ms": SIGNAL - 7 * 24 * HOUR, "source": "test-feed",
                 "tags": []}
        out = fx_market_stage(request(events=[event]))
        self.assertEqual(out["signals"], [])
        self.assertEqual([(s["signal_ms"], s["reason"]) for s in out["suppressed"]], [(SIGNAL, "news_blackout")])
        self.assertEqual(out["suppressed"][0]["detail"]["event_ids"], ["test-feed:nfp:1"])
        self.assertEqual((out["setups"], out["outcomes"]), ([], []))
        # Not yet known at the decision time: no blackout (point in time).
        late = {**event, "first_seen_ms": SIGNAL + MINUTE}
        self.assertEqual(len(fx_market_stage(request(events=[late]))["signals"]), 1)

    def test_a_missing_minute_gives_an_x_outcome_excluded_from_stats(self):
        # Inside the window: the minute 06:30 is missing while the market is open.
        gap = fx_market_stage(request(bars=rows(START, BOUNDARY, JUMP, drop={SIGNAL + 15 * MINUTE})))
        self.assertEqual({(o["status"], o["exit_ms"]) for o in gap["outcomes"]}, {("X", SIGNAL + 15 * MINUTE)})
        for outcome in gap["outcomes"]:
            self.assertTrue(outcome["final"])
            self.assertTrue(outcome["payload"]["excluded_from_stats"])
            self.assertIsNone(outcome["net_ur"])  # no fill is invented
        # The entry minute itself missing for a saved open setup at the next stage.
        first = fx_market_stage(request())
        saved = [{"setup": setup["payload"], "previous": None} for setup in first["setups"]]
        later = utc("2026-10-13T08:00")
        out = fx_market_stage(request(bars=rows(START, later, JUMP, drop={SIGNAL}), boundary=later,
                                      from_ms=BOUNDARY, open_setups=saved))
        self.assertEqual([(o["status"], o["exit_ms"], o["net_ur"]) for o in out["outcomes"]],
                         [("X", SIGNAL, None), ("X", SIGNAL, None)])
        self.assertTrue(all(o["payload"]["excluded_from_stats"] for o in out["outcomes"]))

    def test_a_replay_is_byte_identical_and_the_stage_id_is_deterministic(self):
        first = json.dumps(fx_market_stage(request()), sort_keys=True)
        second = json.dumps(fx_market_stage(copy.deepcopy(request())), sort_keys=True)
        self.assertEqual(first, second)
        later = fx_market_stage(request(boundary=BOUNDARY + HOUR, from_ms=BOUNDARY))
        self.assertNotEqual(json.loads(first)["stage"]["id"], later["stage"]["id"])

    def test_unknown_keys_and_loose_types_are_rejected(self):
        bad = []
        bad.append({**request(), "extra": 1})
        one = request()
        one["symbols"][0]["mystery"] = True
        bad.append(one)
        two = request()
        two["symbols"][0]["venue_symbol"]["point"] = "0.00001"
        bad.append(two)
        bad.append({**request(), "boundary_ms": float(BOUNDARY)})
        bad.append({**request(), "boundary_ms": BOUNDARY + MINUTE})  # not hour-aligned
        bad.append({**request(), "from_ms": True})
        three = request()
        three["symbols"][0]["quote_to_account"] = 1.0  # a float in the money path
        bad.append(three)
        four = request()
        four["symbols"][0]["contract"]["quote"] = "JPY"
        bad.append(four)
        bad.append({**request(), "symbols": [symbol(rows(START, BOUNDARY, JUMP)) | {"symbol": "EUR/USD"}]})
        for item in bad:
            with self.assertRaises(FxStageInputError):
                fx_market_stage(item)


EMA240 = "ema_cross_20_50:240"


def weekly_rows(first_sunday, weeks):
    """Every open minute of ``weeks`` consecutive FX weeks from the Sunday 21:00 UTC open (summer time:
    open Sunday 21:00 to Friday 21:00 UTC), flat price. Built by arithmetic, not per-minute calendar calls."""
    out = []
    for week in range(weeks):
        open_ms = first_sunday + week * 7 * 24 * HOUR
        for minute in range(open_ms, open_ms + 120 * HOUR, MINUTE):
            out.append({"time_basis": "utc", "venue": "test-venue", "native_symbol": "EURUSDm", "transport": "poll",
                        "open_time_ms": minute, "open": PRICE, "high": PRICE, "low": PRICE, "close": PRICE,
                        "spread_points": 1, "tick_volume": 1})
    return out


class WarmupTests(unittest.TestCase):
    """#440: a 240-minute strategy needs 103 candles (24,720 open minutes) before it can signal, which only a
    paged bar read can supply; with that much history it no longer reports insufficient_history."""

    FIRST_SUNDAY = utc("2026-08-02T21:00")  # four summer weeks end Friday 2026-08-28 21:00 UTC
    BOUNDARY = utc("2026-08-28T20:00")

    def reason(self, weeks):
        rows = [row for row in weekly_rows(self.FIRST_SUNDAY + (4 - weeks) * 7 * 24 * HOUR, weeks)
                if row["open_time_ms"] <= self.BOUNDARY]
        request = {"boundary_ms": self.BOUNDARY, "from_ms": self.BOUNDARY - HOUR, "strategy_ids": [EMA240],
                   "symbols": [symbol(rows)]}
        return fx_market_stage(request)["stage"]["payload"]["reasons"]["EURUSD"].get(EMA240)

    def test_two_weeks_are_not_enough_and_four_weeks_are(self):
        self.assertEqual(self.reason(2), "insufficient_history")  # about 60 candles
        self.assertIsNone(self.reason(4))  # about 120 candles


class EndpointTests(unittest.TestCase):
    def post(self, body, token=TOKEN):
        app = create_app(TOKEN)

        async def send():
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.post("/v1/fx/forward/stage", json=body,
                                             headers={"Authorization": f"Bearer {token}"})
        return asyncio.run(send())

    def test_auth_success_and_422_for_bad_input(self):
        self.assertEqual(self.post(request(), token="y" * 40).status_code, 401)
        response = self.post(request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["signals"][0]["signal_ms"], SIGNAL)
        self.assertEqual(self.post({**request(), "extra": 1}).status_code, 422)
        nested = request()
        nested["symbols"][0]["mystery"] = True
        self.assertEqual(self.post(nested).status_code, 422)
        broken = request(bars=[{"open_time_ms": START}])
        self.assertEqual(self.post(broken).status_code, 422)


if __name__ == "__main__":
    unittest.main()
