"""Forex bid/ask resolver (#374): triggers, gaps, ambiguity, commission and swap in R."""

from array import array
from datetime import datetime, timezone
from fractions import Fraction
import unittest

from market_analysis.benchmark.bars import MISSING
from market_analysis.fx.bars import FLAG_CLOSED, FLAG_MISSING_OPEN, FLAG_PRESENT, FxBarSeries
from market_analysis.fx.costs import FxContract, FxCostModel, SwapRates
from market_analysis.fx.resolve import FxResolution, resolve_fx_setup
from market_analysis.fx.setups import FxSetup
from market_analysis.trading_calendar import FX_WEEK

MINUTE = 60_000
ZERO = FxCostModel(version="zero-slip", market_slip_spread_fraction=0, market_slip_range_mult=0,
                   stop_slip_spread_fraction=0, stop_slip_range_mult=0)
EURUSD = FxContract(Fraction(100_000), Fraction(1, 100_000), Fraction(1, 100), Fraction(1, 100), "EUR", "USD")


def utc(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 1000


ENTRY = utc("2026-10-14T12:00")


def setup(side, p0, stop, target, entry=ENTRY, horizon=60):
    return FxSetup("s", "sig", "ema_cross_20_50:60", "ta-v1", "EURUSD", side, horizon, entry, entry, "2", "2", "T",
                   "fx-atr-v1", None, 4 * horizon, None, p0, 10_000, 1000, 200, stop, target, str(p0),
                   str(abs(p0 - stop)), "h")


def bars(minutes, flat=108_000_000, start=ENTRY, overrides=None, symbol="EURUSD"):
    """``minutes`` present minutes at a flat bid; ``overrides`` maps j -> (open, high, low, close)."""
    cols = {name: array("q", [flat]) * minutes for name in ("open", "high", "low", "close")}
    cols["spread"] = array("q", [10_000]) * minutes
    cols["tick_volume"] = array("q", [1]) * minutes
    flags = array("H", [FLAG_PRESENT]) * minutes
    for j, values in (overrides or {}).items():
        for name, value in zip(("open", "high", "low", "close"), values):
            cols[name][j] = value
    return FxBarSeries("fx:EURUSD", "exness-mt5-demo", symbol, FX_WEEK.calendar_id, start, minutes, 1000,
                       flags=flags, **cols)


LONG = setup(1, 108_010_000, 107_810_000, 108_410_000)   # p0 = 1.08000 + spread 0.00010
SHORT = setup(-1, 108_000_000, 108_200_000, 107_600_000)


def resolve(s, b, model=ZERO, **kwargs):
    return resolve_fx_setup(s, b, FX_WEEK, model, EURUSD, **kwargs)


class TriggerTests(unittest.TestCase):
    def test_long_target(self):
        r = resolve(LONG, bars(240, overrides={1: (108_000_000, 108_420_000, 108_000_000, 108_400_000)}))
        self.assertEqual((r.status, r.exit_offset, r.net_ur, r.cost_ur), ("T", 1, 2_000_000, 0))
        self.assertEqual(r.exit_ms, ENTRY + MINUTE)
        self.assertIsInstance(r, FxResolution)

    def test_long_gap_through_the_stop(self):
        r = resolve(LONG, bars(240, overrides={1: (107_800_000, 107_800_000, 107_800_000, 107_800_000)}))
        self.assertEqual((r.status, r.exit_ref_price, r.net_ur), ("S", 107_800_000, -1_050_000))

    def test_short_stop_and_target_use_the_ask(self):
        stop = resolve(SHORT, bars(240, overrides={1: (108_000_000, 108_195_000, 108_000_000, 108_000_000)}))
        self.assertEqual((stop.status, stop.net_ur), ("S", -1_000_000))
        target = resolve(SHORT, bars(240, overrides={1: (108_000_000, 108_000_000, 107_589_000, 107_600_000)}))
        self.assertEqual((target.status, target.net_ur), ("T", 2_000_000))

    def test_ambiguous_reports_the_pessimistic_result(self):
        r = resolve(LONG, bars(240, overrides={1: (108_010_000, 108_500_000, 107_700_000, 108_000_000)}))
        self.assertEqual((r.status, r.net_ur), ("ambiguous", -1_000_000))
        self.assertEqual(r.optimistic["outcome"], "T")
        self.assertEqual(r.optimistic["net_ur"], 2_000_000)


class GapTests(unittest.TestCase):
    def test_gaps_decide_before_intrabar_ambiguity(self):
        # long: opens above the target then trades below the stop -> target first
        r = resolve(LONG, bars(240, overrides={1: (108_450_000, 108_450_000, 107_700_000, 107_700_000)}))
        self.assertEqual((r.status, r.net_ur), ("T", 2_000_000))
        # long: opens below the stop then trades above the target -> stop first, filled at the gap open
        r = resolve(LONG, bars(240, overrides={1: (107_700_000, 108_500_000, 107_700_000, 108_500_000)}))
        self.assertEqual((r.status, r.exit_ref_price), ("S", 107_700_000))
        # short: ask opens below the target (bid 1.07580 + 0.00010) then above the stop -> target first
        r = resolve(SHORT, bars(240, overrides={1: (107_580_000, 108_300_000, 107_580_000, 108_300_000)}))
        self.assertEqual((r.status, r.net_ur), ("T", 2_000_000))
        # short: ask opens above the stop (bid 1.08250 + 0.00010) then below the target -> stop first
        r = resolve(SHORT, bars(240, overrides={1: (108_250_000, 108_250_000, 107_500_000, 107_500_000)}))
        self.assertEqual((r.status, r.exit_ref_price), ("S", 108_260_000))


class MoneyTests(unittest.TestCase):
    def test_commission(self):
        model = FxCostModel(version="zero-slip-commission", market_slip_spread_fraction=0, market_slip_range_mult=0,
                            stop_slip_spread_fraction=0, stop_slip_range_mult=0, commission_per_lot_side=Fraction(7, 2))
        r = resolve(LONG, bars(240, overrides={1: (108_000_000, 108_420_000, 108_000_000, 108_400_000)}), model)
        self.assertEqual((r.net_ur, r.commission_ur, r.cost_ur, r.slippage_ur), (1_965_000, 35_000, 35_000, 0))
        self.assertEqual(r.spread_entry_ur, 50_000)  # 0.00010 / 0.00200, informational only

    def test_swap_over_the_triple_rollover(self):
        entry = utc("2026-10-14T20:00")  # Wednesday; 21:00Z is the 17:00 New York triple rollover
        s = setup(1, 108_010_000, 107_810_000, 108_410_000, entry=entry, horizon=240)
        b = bars(960, flat=108_010_000, start=entry, overrides={0: (108_000_000, 108_010_000, 108_000_000, 108_010_000)})
        rates = SwapRates(long_per_unit=Fraction(-2, 100_000), short_per_unit=Fraction(0))
        r = resolve(s, b, rates=rates)
        self.assertEqual((r.status, r.exit_offset, r.fund_ur, r.net_ur), ("E", 959, -30_000, -30_000))
        self.assertEqual(resolve(s, b).fund_ur, 0)  # no rates, no swap


class StateTests(unittest.TestCase):
    def test_missing_pending_open_previous_and_errors(self):
        gap = bars(240)
        gap.flags[2] = FLAG_MISSING_OPEN
        r = resolve(LONG, gap)
        self.assertEqual((r.status, r.exit_offset, r.net_ur, r.exit_ref_price), ("X", 2, None, None))
        self.assertEqual(resolve(LONG, bars(60, start=ENTRY - 60 * MINUTE)).status, "pending")
        no_entry = bars(240)
        no_entry.flags[0] = FLAG_MISSING_OPEN
        for name in ("open", "high", "low", "close", "spread"):
            getattr(no_entry, name)[0] = MISSING
        r = resolve(LONG, no_entry)
        self.assertEqual((r.status, r.exit_offset, r.entry_fill, r.net_ur), ("X", 0, None, None))
        moved = bars(240, overrides={0: (108_001_000, 108_001_000, 108_000_000, 108_000_000)})
        with self.assertRaisesRegex(ValueError, "entry open/spread"):
            resolve(LONG, moved)  # a corrected entry open must not reuse the old p0
        partial = resolve(LONG, bars(100))
        self.assertEqual((partial.status, partial.resolved_through_ms), ("open", ENTRY + 99 * MINUTE))
        final = FxResolution("s", "T", net_ur=1)
        self.assertIs(resolve(LONG, bars(240), previous=final), final)
        with self.assertRaises(ValueError):
            resolve(LONG, bars(240, symbol="GBPUSD"))
        closed = bars(240)
        closed.flags[3] = FLAG_CLOSED
        with self.assertRaisesRegex(ValueError, "closed market"):
            resolve(LONG, closed)
        with self.assertRaises(ValueError):
            resolve(FxSetup(**{**LONG.to_dict(), "status": "G"}), bars(240))


if __name__ == "__main__":
    unittest.main()
