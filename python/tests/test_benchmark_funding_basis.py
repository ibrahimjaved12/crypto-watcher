"""Funding/basis family fb-v1: premium z, contrarian crossings, unconfirmed premium, cross-section ties,
gaps, no look-ahead, premium loading and the family registry (synthetic, no network)."""
from array import array
from fractions import Fraction
import gzip
import io
import random
import unittest
from unittest.mock import patch

from market_analysis import data_lake
from market_analysis.benchmark import experiment_run as er, funding_basis as fb
from market_analysis.benchmark.bars import MISSING
from market_analysis.benchmark.premium import PremiumSeries, read_premium_csv

START = data_lake.month_bounds_ms("2024-03")[0]
HOUR, MINUTE = 3_600_000, 60_000
PRICE = 10 ** 10
NOW = "2026-10-09T12:00:00Z"
# z = mean premium / 100 (window sum / (60 * 100)): isolates the state machines from the robust z-score
LINEAR_Z = lambda history, x, min_history: Fraction(x, 60 * 100)  # noqa: E731
Z_PARAMS = {"window_min": 60, "history_days": 30, "min_history": 500}


def make_series(premium, close=None, flags=None, symbol="BTCUSDT"):
    minutes = len(premium)
    flags = list(flags or [0] * minutes)
    for i, value in enumerate(premium):
        if value is None:
            flags[i] |= data_lake.FLAG_PREMIUM_MISSING
    values = array("q", [MISSING if value is None else value for value in premium])
    return PremiumSeries(symbol, START, minutes, premium_open=values, premium_high=values, premium_low=values,
                         premium_close=values, close=array("q", close or [PRICE] * minutes),
                         flags=array("H", flags))


def hourly(z_per_hour):
    """Per-minute premium for hour-level z (None: one missing premium minute in that hour)."""
    premium = []
    for z in z_per_hour:
        hour = [0 if z is None else int(z * 100)] * 60
        if z is None:
            hour[30] = None
        premium += hour
    return premium


class CrossingTests(unittest.TestCase):
    def test_contrarian_crossing_rearm_cooldown_and_gap_restart(self):
        z = [0, 0, 3, 3, 0, -3, 0, 0, -3, 0, 0, 0, 0, None, 3, 0, 3, 0]
        with patch.object(fb, "robust_z", LINEAR_Z):
            signals = fb.prem_z_cross(make_series(hourly(z)), theta=Fraction(2), rearm_below=Fraction(1),
                                      cooldown_min=240, step_min=60, **Z_PARAMS)
        # hour j is decided at (j + 1) h. j2 high premium -> short; j5 inside the cooldown; j8 low -> long;
        # j13 has a missing premium minute, so j14 has no previous value; j16 crosses again -> short.
        self.assertEqual(signals, [(START + 3 * HOUR, -1), (START + 9 * HOUR, 1), (START + 17 * HOUR, -1)])

    def test_window_with_a_missing_minute_is_invalid_and_not_history(self):
        premium = [100] * (5 * 60)
        premium[90] = None
        seen = []
        with patch.object(fb, "robust_z", lambda history, x, m: seen.append((len(history), x)) or None):
            fb.premium_z(make_series(premium), step_min=60, **Z_PARAMS)
        # decisions 1h..5h; the 2h window (60..119) holds the missing minute
        self.assertEqual(seen, [(0, 6000), (1, 6000), (2, 6000), (3, 6000)])


class UnconfirmedTests(unittest.TestCase):
    def run_rule(self, close, flags=None):
        premium = [0] * 120 + [300] * 240  # z ramps to 3 from minute 120 on
        with patch.object(fb, "robust_z", LINEAR_Z):
            return fb.prem_unconfirmed(make_series(premium, close, flags), theta=Fraction(2), return_min=60,
                                       cooldown_min=60, step_min=15, **Z_PARAMS)

    def test_signal_only_when_price_disagrees(self):
        falling = [PRICE - i * 10 ** 6 for i in range(360)]
        # first |z| >= 2 at 2h45 (45 of 60 window minutes at 300 -> z 2.25); cooldown 60 minutes
        self.assertEqual(self.run_rule(falling), [(START + 165 * MINUTE + i * HOUR, -1) for i in range(4)])
        rising = [PRICE + i * 10 ** 6 for i in range(360)]
        self.assertEqual(self.run_rule(rising), [])   # confirmed by price
        self.assertEqual(self.run_rule([PRICE] * 360), [])  # zero return is no signal

    def test_invalid_close_skips_the_decision(self):
        falling = [PRICE - i * 10 ** 6 for i in range(360)]
        flags = [0] * 360
        flags[164] = data_lake.FLAG_NO_AGGTRADES  # minute d of the 2h45 decision
        self.assertEqual(self.run_rule(falling, flags), [(START + 180 * MINUTE + i * HOUR, -1) for i in range(4)])


class CrossSectionTests(unittest.TestCase):
    def test_spread_tie_missing_symbol_and_per_symbol_cooldown(self):
        t = [START + (i + 1) * HOUR for i in range(6)]
        rows = {symbol: {} for symbol in data_lake.SYMBOLS}

        def put(when, values):
            for symbol in data_lake.SYMBOLS:
                if symbol in values or values.get("*") is not None:
                    rows[symbol][when] = Fraction(values.get(symbol, values.get("*")))

        put(t[0], {"BTCUSDT": 2, "ETHUSDT": Fraction(-3, 2), "*": 0})
        put(t[1], {"BTCUSDT": Fraction(5, 2), "SOLUSDT": -1, "*": 0})   # BTC leg in cooldown
        put(t[2], {"BTCUSDT": 2, "ETHUSDT": 2, "XRPUSDT": -2, "*": 0})  # shared top: no signal at all
        put(t[3], {"BTCUSDT": Fraction(29, 20), "ETHUSDT": Fraction(-29, 20), "*": 0})  # spread 2.9
        put(t[4], {"BTCUSDT": 5, "ETHUSDT": -5, "*": 0})
        del rows["DOGEUSDT"][t[4]]                                      # a symbol without z: no signal
        put(t[5], {"BTCUSDT": Fraction(3, 2), "DOGEUSDT": Fraction(-3, 2), "*": 0})  # spread exactly 3
        per_symbol = {symbol: sorted(items.items()) for symbol, items in rows.items()}
        signals = fb.cross_section(per_symbol, 240, min_spread=Fraction(3), cooldown_min=240)
        self.assertEqual(signals, [("BTCUSDT", t[0], -1, 240), ("ETHUSDT", t[0], 1, 240), ("SOLUSDT", t[1], 1, 240),
                                   ("BTCUSDT", t[5], -1, 240), ("DOGEUSDT", t[5], 1, 240)])

    def test_shared_bottom_also_blocks(self):
        per_symbol = {symbol: [(START + HOUR, Fraction(-2))] for symbol in data_lake.SYMBOLS}
        per_symbol["BTCUSDT"] = [(START + HOUR, Fraction(2))]
        self.assertEqual(fb.cross_section(per_symbol, 240, min_spread=Fraction(3), cooldown_min=240), [])


class LookAheadTests(unittest.TestCase):
    def test_minutes_after_d_are_never_used(self):
        rng = random.Random(7)
        minutes = 10 * 1440
        premium = [rng.randint(-5000, 5000) for _ in range(minutes)]
        close = [PRICE + rng.randint(-10 ** 8, 10 ** 8) for _ in range(minutes)]
        small = {"window_min": 60, "history_days": 1, "min_history": 10}
        rules = (("prem_z_cross", lambda s: fb.prem_z_cross(s, theta=Fraction(2), rearm_below=Fraction(1),
                                                              cooldown_min=240, step_min=60, **small)),
                 ("prem_unconfirmed", lambda s: fb.prem_unconfirmed(s, theta=Fraction(3, 2), return_min=60,
                                                                    cooldown_min=60, step_min=15, **small)),
                 ("defined_z", lambda s: fb._defined_z(s, step_min=60, **small)))
        for name, rule in rules:
            full = rule(make_series(premium, close))
            with self.subTest(rule=name):
                self.assertTrue(full)
                cut = full[len(full) // 2][0]
                e = (cut - START) // MINUTE
                changed = premium[:e] + [rng.choice((None, rng.randint(-10 ** 6, 10 ** 6)))
                                         for _ in range(minutes - e)]
                moved = close[:e] + [PRICE * 2] * (minutes - e)
                again = rule(make_series(changed, moved))
                self.assertEqual([s for s in again if s[0] <= cut], [s for s in full if s[0] <= cut])


class PremiumReaderTests(unittest.TestCase):
    def csv_bytes(self, month, edit=None):
        start, _, days = data_lake.month_bounds_ms(month)
        lines = [",".join(data_lake.COLUMNS)]
        index = {name: position for position, name in enumerate(data_lake.COLUMNS)}
        for minute in range(days * 1440):
            row = [""] * len(data_lake.COLUMNS)
            row[index["open_time_ms"]] = str(start + minute * MINUTE)
            row[index["close"]] = "100.5"
            for name in ("premium_open", "premium_high", "premium_low", "premium_close"):
                row[index[name]] = "-0.00012345" if minute % 2 else "0"
            row[index["flags"]] = "0"
            if edit:
                edit(minute, row, index)
            lines.append(",".join(row))
        return gzip.compress(("\n".join(lines) + "\n").encode("ascii"))

    def test_signed_exact_values_missing_flag_and_consistency(self):
        def missing(minute, row, index):
            if minute == 3:
                for name in ("premium_open", "premium_high", "premium_low", "premium_close"):
                    row[index[name]] = ""
                row[index["flags"]] = str(data_lake.FLAG_PREMIUM_MISSING)

        series = read_premium_csv(io.BytesIO(self.csv_bytes("2024-02", missing)), "BTCUSDT", "2024-02")
        self.assertEqual(series.minutes, 29 * 1440)
        self.assertEqual((series.premium_close[0], series.premium_close[1], series.close[0]), (0, -12345, 10050000000))
        self.assertFalse(series.premium_valid(3))
        self.assertEqual(series.premium_close[3], MISSING)
        self.assertTrue(series.premium_valid(4) and series.price_valid(4))

        def flag_only(minute, row, index):
            if minute == 5:
                row[index["flags"]] = str(data_lake.FLAG_PREMIUM_MISSING)

        def too_fine(minute, row, index):
            if minute == 7:
                row[index["premium_close"]] = "0.000000001"

        for edit in (flag_only, too_fine):
            with self.subTest(edit=edit.__name__), self.assertRaises(ValueError):
                read_premium_csv(io.BytesIO(self.csv_bytes("2024-02", edit)), "BTCUSDT", "2024-02")


def fb_question(horizon=240, strategies=None, **changes):
    values = dict(seed=20261009, label_revision=3, data_revision=1, created_utc=NOW, family="funding-basis")
    values.update(changes)
    names = list(er.FAMILIES["funding-basis"].strategies_for(horizon)) if strategies is None else strategies
    return er.make_question(f"fb-v1-{horizon}m", "Premium extremes fade", horizon, names, **values)


class RegistryTests(unittest.TestCase):
    def test_geometry_per_horizon_and_strategy_horizons(self):
        four = fb_question(240)
        self.assertEqual((four["strategies"], four["k_values"], four["rr_indices"]),
                         (["fb_prem_z_4h", "fb_xs_4h"], ["2"], [1]))
        one = fb_question(60)
        self.assertEqual((one["strategies"], one["k_values"], one["rr_indices"]), (["fb_prem_unconf_1h"], ["2"], [0]))
        self.assertEqual(len(er.build_geometries(four)), 6 * 2)
        self.assertEqual({(g.horizon_min, g.k, g.rr_index) for g in er.build_geometries(one)}, {(60, 2, 0)})
        for horizon, names in ((240, ["fb_prem_unconf_1h"]), (60, ["fb_prem_z_4h"]), (15, ["fb_prem_z_4h"])):
            with self.subTest(horizon=horizon), self.assertRaises(er.QuestionError):
                fb_question(horizon, names)
        with self.assertRaises(er.QuestionError):
            er.validate_question({**four, "rr_indices": [0]})
        plan = er.build_plan(four, ["fb_xs_4h"], NOW, "snapshot-1")
        self.assertEqual((plan.variants, plan.strategies[0]["config"]), (1, fb.strategy_config("fb_xs_4h", 240)))
        self.assertEqual(fb.strategy_config("fb_prem_unconf_1h", 60)["rr_indices"], [0])
        with self.assertRaises(ValueError):
            fb.symbol_signals("fb_prem_z_4h", make_series([0] * 60), 60, first_ms=0, end_ms=1, label_step_min=15)

    def test_build_specs_and_counts_combine_the_cross_section(self):
        z = {symbol: Fraction(index) for index, symbol in enumerate(data_lake.SYMBOLS)}  # spread 5, unique ends
        first, _ = er.segment_bounds_ms("development")
        loads = []

        def load(bars_dir, symbol, first_month, last_month, *, token=None, gate=None):
            loads.append((symbol, first_month, last_month, token, gate))
            return symbol

        def part(name, series, minutes, *, first_ms, end_ms, label_step_min):
            self.assertEqual(label_step_min, 15)
            if name == "fb_xs_4h":
                return [(first_ms + HOUR, z[series])]
            return [(series, first_ms + HOUR, -1, minutes)] if series == "BTCUSDT" else []

        with patch.object(er, "load_symbol_premium", load), patch.object(fb, "symbol_signals", part):
            counts = er.signal_counts(fb_question(240), "development", "bars")
        self.assertEqual(loads, [(symbol, "2024-01", "2025-06", None, None) for symbol in data_lake.SYMBOLS])
        prem, xs = counts["strategies"]
        self.assertEqual((prem["strategy_id"], prem["total"]["signals"], prem["total"]["short"]),
                         ("fb_prem_z_4h", 1, 1))
        self.assertEqual((xs["strategy_id"], xs["total"]["long"], xs["total"]["short"]), ("fb_xs_4h", 1, 1))
        self.assertEqual((xs["symbols"][data_lake.SYMBOLS[-1]]["short"], xs["symbols"][data_lake.SYMBOLS[0]]["long"]),
                         (1, 1))
        self.assertEqual(counts["total"]["first"], er._utc_date(first + HOUR))


if __name__ == "__main__":
    unittest.main()
