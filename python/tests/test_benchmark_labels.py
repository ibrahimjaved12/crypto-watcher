"""Dense label table (labels-v1) for the #182 benchmark."""
from __future__ import annotations

from array import array
from dataclasses import replace
from fractions import Fraction
import gzip
import io
import random
import unittest

from market_analysis import data_lake
from market_analysis.benchmark.bars import BarSeries
from market_analysis.benchmark.costs import COST_MODEL_V1, with_multiplier
from market_analysis.benchmark.funding import FundingSeries
from market_analysis.benchmark.labels import LabelParams, build_labels, read_label_csv, write_label_csv
from market_analysis.benchmark.volatility import BLOCKS_PER_DAY

MONTH = "2025-01"
START = data_lake.month_bounds_ms(MONTH)[0]
TICK = 10 ** 6
MINUTES = 2 * 1440
PARAMS = LabelParams(horizons=(15,), half_life_days=((15, 1),), step_minutes=((15, 5),))


def walk_rows(minutes=MINUTES, seed=3):
    rng = random.Random(seed)
    close = 100_000 * TICK
    rows = []
    for _ in range(minutes):
        opened = close
        close = opened + rng.randint(-30, 30) * TICK
        rows.append((opened, max(opened, close) + rng.randint(0, 5) * TICK,
                     min(opened, close) - rng.randint(0, 5) * TICK, close))
    return rows


def make_bars(rows):
    columns = {name: array("q", [row[position] for row in rows])
               for position, name in enumerate(("open", "high", "low", "close"))}
    for position, name in enumerate(("mark_open", "mark_high", "mark_low", "mark_close")):
        columns[name] = array("q", [row[position] for row in rows])
    columns.update({name: array("q", [0] * len(rows)) for name in ("volume", "taker_buy_volume", "trades")})
    return BarSeries("BTCUSDT", START, len(rows), flags=array("H", [0] * len(rows)), **columns)


def build(rows=None, params=PARAMS):
    bars = make_bars(rows or walk_rows())
    funding = FundingSeries((), (), (), bars.start_ms, bars.end_ms)
    return list(build_labels("BTCUSDT", bars, funding, params, {MONTH: TICK}))


def csv_bytes(rows, params=PARAMS):
    buffer = io.BytesIO()
    write_label_csv(buffer, rows, params)
    return buffer.getvalue()


class LabelTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (cls.month, cls.rows), = build()

    def test_grid_order_and_first_signal(self):
        self.assertEqual(self.month, MONTH)
        self.assertTrue(all(row.signal_ms % 300_000 == 0 for row in self.rows))
        self.assertEqual(self.rows[0].signal_ms, START + 300_000)  # START itself has d = -1
        keys = [(row.signal_ms, row.horizon_min, -row.side, row.k) for row in self.rows]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(self.rows), (MINUTES // 5) * 2 * 2)  # signals START+5m .. END, both sides, two k
        self.assertEqual([(row.side, row.k) for row in self.rows[:4]],
                         [(1, Fraction(1)), (1, Fraction(2)), (-1, Fraction(1)), (-1, Fraction(2))])

    def test_statuses_warm_up_trades_and_end(self):
        first_published = START + 5 * (BLOCKS_PER_DAY + 1) * 60_000  # end of block 288
        for row in self.rows:
            entry = (row.signal_ms - START) // 60_000
            if entry + 60 - 1 >= MINUTES:
                self.assertEqual(row.status, "I")
            elif row.signal_ms < first_published:
                self.assertEqual(row.status, "V")
            else:
                self.assertEqual(row.status, "T")
        traded = [row for row in self.rows if row.status == "T"]
        self.assertGreater(len(traded), 1000)
        for row in traded[:50]:
            self.assertEqual(len(row.cells), 4)
            self.assertGreaterEqual(row.d_ticks, PARAMS.min_stop_ticks)
            self.assertTrue(all(pair.pess.outcome in "TSELX" for pair in row.cells))
        one, two = traded[0], traded[1]
        self.assertEqual((one.signal_ms, one.side, one.k, two.k), (two.signal_ms, two.side, Fraction(1), Fraction(2)))
        self.assertEqual(one.sigma, two.sigma)
        self.assertIn(two.d_ticks, (2 * one.d_ticks - 1, 2 * one.d_ticks))  # both are ceilings

    def test_g_and_n_paths(self):
        (_, wide), = build(params=replace(PARAMS, min_stop_ticks=10 ** 9))
        self.assertEqual({row.status for row in wide}, {"V", "G", "I"})
        strict = replace(COST_MODEL_V1, liq_buffer_multiple=10 ** 6)
        (_, unleveraged), = build(params=replace(PARAMS, cost_model=strict))
        self.assertEqual({row.status for row in unleveraged}, {"V", "N", "I"})

    def test_4h_grid_is_15_minutes_and_hourly_rows_unchanged(self):
        # Slice H1: 240-minute decision times every 15 minutes; HH:00 rows equal the old 60-minute grid's.
        hourly = LabelParams(horizons=(15, 240), half_life_days=((15, 1), (240, 1)), step_minutes=((15, 5), (240, 60)))
        quarter = replace(hourly, step_minutes=((15, 5), (240, 15)))
        self.assertEqual(LabelParams().step(240), 15)
        (_, old), = build(params=hourly)
        (_, new), = build(params=quarter)
        four_h = [row for row in new if row.horizon_min == 240]
        minutes = {(row.signal_ms - START) // 60_000 % 60 for row in four_h}
        self.assertEqual(minutes, {0, 15, 30, 45})  # START is a UTC month start, hence HH:00
        self.assertFalse(any((row.signal_ms - START) // 60_000 % 60 == 7 for row in four_h))
        self.assertIn("T", {row.status for row in four_h if (row.signal_ms - START) // 60_000 % 60 in (15, 45)})
        on_hour = [row for row in four_h if row.signal_ms % 3_600_000 == 0]
        old_4h = [row for row in old if row.horizon_min == 240]
        self.assertEqual(on_hour, old_4h)
        self.assertIn("T", {row.status for row in old_4h})
        self.assertEqual(csv_bytes(on_hour, quarter), csv_bytes(old_4h, hourly))
        self.assertEqual([row for row in new if row.horizon_min == 15], [row for row in old if row.horizon_min == 15])

    def test_off_tick_entry_open_is_p_not_an_error(self):
        # tick-v2 tolerates rare off-grid prints (SOLUSDT 2025-07); one at an entry open is a non-trade.
        rows = walk_rows()
        traded = next(row for row in self.rows if row.status == "T")
        entry = (traded.signal_ms - START) // 60_000
        opened, high, low, close = rows[entry]
        rows[entry] = (opened + TICK // 2, max(high, opened + TICK // 2), low, close)
        (_, labelled), = build(rows)
        marked = [row for row in labelled if row.signal_ms == traded.signal_ms]
        self.assertEqual({row.status for row in marked}, {"P"})
        self.assertEqual(list(read_label_csv(io.BytesIO(csv_bytes(labelled)), PARAMS)), labelled)

    def test_csv_round_trip_and_determinism(self):
        data = csv_bytes(self.rows)
        self.assertEqual(list(read_label_csv(io.BytesIO(data), PARAMS)), self.rows)
        (_, again), = build()
        self.assertEqual(csv_bytes(again), data)
        text = gzip.decompress(data).decode("ascii")
        header = text.split("\n", 1)[0]
        self.assertEqual(header, "signal_ms,horizon_min,side,k,status,p0,sigma,d_ticks,leverage,wallet_ur,c0,c1,c2,c3")
        non_trade = next(line for line in text.splitlines()[1:] if ",V," in line)
        self.assertTrue(non_trade.endswith(",V" + "," * 9))

    def test_reader_validation(self):
        good = gzip.decompress(csv_bytes(self.rows)).decode("ascii")
        lines = good.splitlines()
        trade_line = next(index for index, line in enumerate(lines) if ",T," in line)
        cases = (
            (lines[1].replace(",V,", ",Q,", 1), 1, "status"),
            (lines[1] + "1", 1, "c3"),  # a non-trade row with a non-empty cell column
            (lines[1].replace(",1,", ",2,", 1), 1, "side"),
            (lines[trade_line].rsplit(",", 1)[0] + ",T:x:1:2:3", trade_line, "c3"),
        )
        for broken, line, column in cases:
            text = "\n".join([lines[0], *lines[1:line], broken]) + "\n"
            with self.subTest(column=column), self.assertRaisesRegex(ValueError, rf"line {line} column {column}"):
                list(read_label_csv(io.StringIO(text), PARAMS))

    def test_no_look_ahead(self):
        rows = walk_rows()
        target = next(row for row in self.rows if row.status == "T" and row.signal_ms > START + 1440 * 60_000)
        entry = (target.signal_ms - START) // 60_000
        perturbed = rows[:entry + 1] + [(o * 2, h * 2, l * 2, c * 2) for o, h, l, c in rows[entry + 1:]]
        (_, changed), = build(perturbed)
        same = next(row for row in changed if (row.signal_ms, row.horizon_min, row.side, row.k)
                    == (target.signal_ms, target.horizon_min, target.side, target.k))
        self.assertEqual((same.status, same.p0, same.sigma, same.d_ticks), (target.status, target.p0, target.sigma,
                                                                             target.d_ticks))


class LabelParamsTests(unittest.TestCase):
    def test_defaults_record_and_validation(self):
        params = LabelParams()
        record = params.to_record()
        self.assertEqual(record["horizons"], [15, 60, 240])
        self.assertEqual(record["half_life_days"], {"15": 1, "60": 3, "240": 7})
        self.assertEqual(record["step_minutes"], {"15": 5, "60": 15, "240": 15})
        self.assertEqual(record["rr_grid"], ["1", "3/2", "2", "3"])
        self.assertEqual(record["cost_model_identity"], COST_MODEL_V1.identity())
        self.assertEqual(params.identity(), LabelParams().identity())
        self.assertEqual(params.window(240), 960)
        for changes in ({"cost_model": with_multiplier(COST_MODEL_V1, 2)}, {"horizons": (15, 7)},
                        {"half_life_days": ((15, 2), (60, 3), (240, 7))}, {"rr_grid": (Fraction(2), Fraction(1))},
                        {"step_minutes": ((15, 3), (60, 15), (240, 15))}, {"schema": "labels-v0"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                LabelParams(**changes)


if __name__ == "__main__":
    unittest.main()
