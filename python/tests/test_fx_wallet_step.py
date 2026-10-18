"""Per-user forex wallet step (#445): exact ledger, per-user stop-out, caps, defaults and the endpoint.

Numbers are the ones test_fx_wallet.py derives: balance 1000 USD, 1 % risk = 10 USD, EURUSD long at 1.0801
with a 0.0020 stop distance = 5000 units, notional 5400.50 USD, leverage 100, stop-out price 1.0746995.
"""

import asyncio
from datetime import datetime, timezone
import copy
import json
import unittest

import httpx

from market_analysis.api import create_app
from market_analysis.fx.setups import FxSetup
from market_analysis.fx.wallet import E8, FxWalletConfig
from market_analysis.fx.wallet_step import fx_wallet_step
from market_analysis.fx.stage import FxStageInputError

MINUTE = 60_000
TOKEN = "test-service-token-" + "x" * 32
T0 = int(datetime(2026, 10, 13, 10, tzinfo=timezone.utc).timestamp()) * 1000  # a Tuesday: the market is open
FILL = 108_010_000  # 1.0801 scaled 10**8
CONTRACTS = {"EURUSD": {"contract_size": "100000", "tick_size": "0.00001", "min_lot": "0.01", "lot_step": "0.01",
                        "base": "EUR", "quote": "USD"}}


def setup(setup_id="a", stop=FILL - 200_000, target=FILL + 400_000, signal_id=None, entry_ms=T0):
    record = FxSetup(setup_id, signal_id or "sig-" + setup_id, "ema_cross_20_50:60", "ta-v1", "EURUSD", 1, 60,
                     entry_ms, entry_ms, "2", "2", "T", "fx-atr-v1", None, 240, None, FILL, 10_000, 1000, 200, stop,
                     target, str(FILL), str(FILL - stop), "h").to_dict()
    return record


def open_event(s, ms=T0):
    return {"type": "open", "ms": ms, "setup": s, "quote_to_account": "1", "symbol": "EURUSD", "sequence": 0,
            "stage_run_id": "00000000-0000-0000-0000-000000000000"}


def close_event(setup_id, exit_fill, ms=T0 + 60 * MINUTE, outcome="T"):
    return {"type": "close", "ms": ms, "setup_id": setup_id, "exit_fill": str(exit_fill), "outcome": outcome,
            "quote_to_account": "1"}


def bar_rows(low_at=None, minutes=61):
    """Flat bid 1.0801 for ``minutes`` open minutes from T0; one minute can dip to 1.07."""
    out = []
    for index in range(minutes):
        low = 1.07 if index == low_at else 1.0801
        out.append({"time_basis": "utc", "venue": "test-venue", "native_symbol": "EURUSDm", "transport": "poll",
                    "open_time_ms": T0 + index * MINUTE, "open": 1.0801, "high": 1.0801, "low": low, "close": 1.0801,
                    "spread_points": 1, "tick_volume": 1})
    return out


def symbols(rows):
    return [{"symbol": "EURUSD", "quote_to_account": "1", "bars": rows,
             "venue_symbol": {"venue": "test-venue", "native_symbol": "EURUSDm", "digits": 5, "tick_size": "0.00001"}}]


def body(events, rows=None, state=None, config=None):
    out = {"state": state, "contracts": CONTRACTS, "events": events,
           "symbols": symbols(bar_rows() if rows is None else rows)}
    if config is not None:
        out["config"] = config
    return out


def types(out):
    return [(line["type"], line["amount_e8"]) for line in out["ledger"]]


class StepTests(unittest.TestCase):
    def test_open_then_close_updates_balance_and_ledger_with_exact_integers(self):
        out = fx_wallet_step(body([open_event(setup()), close_event("a", FILL + 400_000)]))
        self.assertEqual(types(out), [("open_fee", 0), ("pnl", 2_000_000_000), ("close_fee", 0)])  # 5000 x 0.0040
        state = out["wallet_state"]
        self.assertEqual((state["balance_e8"], state["used_margin_e8"], state["seq"], state["positions"],
                          state["done"]), (102_000_000_000, 0, 3, {}, ["a"]))
        self.assertEqual(out["ledger"][0]["units"], "5000")
        self.assertEqual(out["wallet_config"]["initial_balance_e8"], 1000 * E8)
        self.assertEqual(out["wallet_config"]["max_positions"], 1000)
        self.assertEqual(out["wallet_config"]["max_exposure_multiple"], "10")
        self.assertEqual(out["wallet_config"]["risk_fraction"], "1/100")
        # The same batch split over two calls, the state travelling through JSON, gives the same account.
        first = fx_wallet_step(body([open_event(setup())]))
        carried = json.loads(json.dumps(first["wallet_state"]))
        second = fx_wallet_step(body([close_event("a", FILL + 400_000)], state=carried))
        self.assertEqual(second["wallet_state"]["balance_e8"], 102_000_000_000)
        self.assertEqual([line["seq"] for line in second["ledger"]], [2, 3])

    def test_a_stop_out_inside_the_window_liquidates_and_the_later_close_is_ignored(self):
        out = fx_wallet_step(body([open_event(setup()), close_event("a", FILL + 400_000)], rows=bar_rows(low_at=5)))
        self.assertEqual(types(out), [("open_fee", 0), ("liquidation", -2_700_250_000)])  # 5000 x (1.0746995 - 1.0801)
        self.assertEqual(out["ledger"][1]["ms"], T0 + 5 * MINUTE)
        state = out["wallet_state"]
        self.assertEqual((state["balance_e8"], state["used_margin_e8"], state["positions"], state["done"]),
                         (97_299_750_000, 0, {}, ["a"]))
        # A position carried in the state is scanned too, from its own entry.
        carried = fx_wallet_step(body([open_event(setup())]))["wallet_state"]
        later = fx_wallet_step(body([], rows=bar_rows(low_at=7), state=carried))
        self.assertEqual([(l["type"], l["ms"]) for l in later["ledger"]], [("liquidation", T0 + 7 * MINUTE)])

    def test_a_minimum_lot_that_risks_more_than_one_percent_is_rejected(self):
        # 100 USD balance: 1 USD of risk over 0.0020 is 500 units, below the 0.01 lot (1000 units) minimum.
        out = fx_wallet_step(body([open_event(setup())], config={"initial_balance_e8": 100 * E8}))
        self.assertEqual([(l["type"], l["reason"], l["amount_e8"]) for l in out["ledger"]],
                         [("rejected", "below_min_lot", 0)])
        self.assertEqual((out["wallet_state"]["balance_e8"], out["wallet_state"]["positions"]), (100 * E8, {}))

    def test_total_notional_over_ten_times_the_balance_is_rejected(self):
        # A 0.0010 stop is 10000 units = 10801 USD of notional, above 10 x 1000.
        tight = setup(stop=FILL - 100_000)
        out = fx_wallet_step(body([open_event(tight)]))
        self.assertEqual([(l["type"], l["reason"]) for l in out["ledger"]], [("rejected", "cap:total_exposure")])
        # The cap is configurable in the request.
        roomy = fx_wallet_step(body([open_event(tight)], config={"max_exposure_multiple": "20"}))
        self.assertEqual([l["type"] for l in roomy["ledger"]], ["open_fee"])

    def test_two_setups_of_one_signal_open_as_two_positions(self):
        one = setup("a", stop=FILL - 400_000, target=FILL + 600_000, signal_id="sig-1")
        two = setup("b", stop=FILL - 400_000, target=FILL + 800_000, signal_id="sig-1")
        out = fx_wallet_step(body([open_event(one), open_event(two)]))
        self.assertEqual([l["type"] for l in out["ledger"]], ["open_fee", "open_fee"])
        self.assertEqual(sorted(out["wallet_state"]["positions"]), ["a", "b"])

    def test_the_old_position_cap_of_six_no_longer_rejects_a_seventh(self):
        self.assertEqual(FxWalletConfig().max_positions, 1000)
        events = [open_event(setup(name, stop=FILL - 1_000_000, target=FILL + 2_000_000)) for name in "abcdefg"]
        out = fx_wallet_step(body(events))
        self.assertEqual([l["type"] for l in out["ledger"]], ["open_fee"] * 7)
        self.assertEqual(len(out["wallet_state"]["positions"]), 7)
        # The cap stays configurable and keeps its reason.
        capped = fx_wallet_step(body(events[:2], config={"max_positions": 1}))
        self.assertEqual([l.get("reason") for l in capped["ledger"]], [None, "cap:max_positions"])

    def test_input_is_strict(self):
        def refused(request, pattern):
            with self.assertRaisesRegex(FxStageInputError, pattern):
                fx_wallet_step(request)
        refused({**body([]), "extra": 1}, "unknown")
        refused(body([{"type": "liquidation", "ms": T0, "setup_id": "a", "quote_to_account": "1"}]), "computed per user")
        refused(body([{**close_event("a", 1), "exit_fill": 1.5}]), "exact")
        refused(body([], state={"version": "wrong"}), "state")
        refused(body([], config={"risk_fraction": 0.01}), "risk_fraction")
        refused(body([open_event(setup())], rows=[]) | {"symbols": []}, "no bars supplied for EURUSD")
        refused(body([open_event(setup())]) | {"contracts": {"EURUSD": {"contract_size": "1"}}}, "missing")


class EndpointTests(unittest.TestCase):
    def post(self, payload, token=TOKEN):
        app = create_app(TOKEN)

        async def send():
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.post("/v1/fx/forward/wallet_step", json=payload,
                                             headers={"Authorization": f"Bearer {token}"})
        return asyncio.run(send())

    def test_401_200_and_422(self):
        request = body([open_event(setup()), close_event("a", FILL + 400_000)])
        self.assertEqual(self.post(request, token="y" * 40).status_code, 401)
        ok = self.post(request)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(set(ok.json()), {"schema_version", "wallet_state", "ledger", "wallet_config", "assumptions"})
        self.assertEqual(ok.json()["wallet_state"]["balance_e8"], 102_000_000_000)
        self.assertEqual(self.post({**request, "extra": 1}).status_code, 422)
        bad = copy.deepcopy(request)
        bad["events"][0]["quote_to_account"] = 1.0  # a float in the money path
        self.assertEqual(self.post(bad).status_code, 422)
        bad = copy.deepcopy(request)
        bad["symbols"][0]["bars"][0] = {"open_time_ms": T0}
        self.assertEqual(self.post(bad).status_code, 422)
        self.assertEqual(self.post({**request, "state": None, "contracts": []}).status_code, 422)

    def test_replay_is_byte_identical(self):
        request = body([open_event(setup()), close_event("a", FILL + 400_000)], rows=bar_rows(low_at=5))
        first, second = self.post(request), self.post(copy.deepcopy(request))
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.content, second.content)


if __name__ == "__main__":
    unittest.main()
