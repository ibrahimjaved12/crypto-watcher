"""Bars and funding loaders for the #182 benchmark harness (synthetic data-lake files)."""
from __future__ import annotations

from array import array
from fractions import Fraction
import gzip
import io
import unittest

from market_analysis import data_lake
from market_analysis.benchmark.bars import (
    COMPROMISED_FLAGS, MISSING, BarSeries, CompromisedIndex, compromised_in, infer_tick, read_bars_csv,
)
from market_analysis.benchmark.funding import (
    FundingSeries, interval_report, read_funding_csv, settlement_mark,
)

MONTH = "2025-02"
START, END, DAYS = data_lake.month_bounds_ms(MONTH)
MINUTES = DAYS * 1440
HOUR = 3_600_000
E8 = 10 ** 8

DEFAULT_ROW = {
    "open": "100.5", "high": "101", "low": "100", "close": "100.25", "volume": "2.5", "quote_volume": "251.25",
    "taker_buy_volume": "1", "taker_buy_quote_volume": "100.5", "agg_rows": "3", "trades": "4",
    "first_agg_id": "10", "last_agg_id": "12", "mark_open": "100.4", "mark_high": "100.6", "mark_low": "100.3",
    "mark_close": "100.5", "flags": "0",
}


def bars_text(overrides=None, drop=(), header=None):
    """A whole month of data-lake bar rows; ``overrides`` maps a minute index to changed columns."""
    overrides = overrides or {}
    lines = [header if header is not None else ",".join(data_lake.COLUMNS)]
    for index in range(MINUTES):
        if index in drop:
            continue
        values = {name: "" for name in data_lake.COLUMNS}
        values.update(DEFAULT_ROW, open_time_ms=str(START + index * 60_000))
        values.update(overrides.get(index, {}))
        lines.append(",".join(values[name] for name in data_lake.COLUMNS))
    return "\n".join(lines) + "\n"


def series(start_ms, prices, marks=None, flags=None, symbol="BTCUSDT"):
    """A small BarSeries built directly (open = high = low = close = price)."""
    count = len(prices)
    marks = marks if marks is not None else prices
    columns = {name: array("q", prices) for name in ("open", "high", "low", "close")}
    columns.update({name: array("q", marks) for name in ("mark_open", "mark_high", "mark_low", "mark_close")})
    columns.update({name: array("q", [0] * count) for name in ("volume", "taker_buy_volume", "trades")})
    return BarSeries(symbol, start_ms, count, flags=array("H", flags or [0] * count), **columns)


class CompromisedIndexTests(unittest.TestCase):
    def test_matches_the_scanning_helper_for_every_range(self):
        flags = [0, 2, 0, 16, 0, 1, 0, 0]
        bars = series(START, [1] * 8, flags=flags)
        for mask in (COMPROMISED_FLAGS, 2, 16):
            index = CompromisedIndex(bars, mask)
            for start in range(8):
                for end in range(start, 8):
                    with self.subTest(mask=mask, start=start, end=end):
                        self.assertEqual(index.any_in(start, end), compromised_in(bars, start, end, mask=mask))
        for start, end in ((-1, 2), (3, 2), (0, 8)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                CompromisedIndex(bars).any_in(start, end)


class ReadBarsTests(unittest.TestCase):
    def test_parsing_missing_and_flags(self):
        bars = read_bars_csv(io.StringIO(bars_text({
            1: {"open": "", "high": "", "low": "", "close": "", "volume": "0", "taker_buy_volume": "0",
                "trades": "0", "flags": str(data_lake.FLAG_NO_AGGTRADES | data_lake.FLAG_KLINE_ROW_MISSING)},
            2: {"mark_open": "", "mark_high": "", "mark_low": "", "mark_close": "", "flags": "16"},
            3: {"volume": "2.1845367E7"},  # exponent form is parsed exactly
        })), "BTCUSDT", MONTH)
        self.assertEqual((bars.symbol, bars.start_ms, bars.minutes, bars.end_ms), ("BTCUSDT", START, MINUTES, END))
        self.assertEqual((bars.open[0], bars.high[0], bars.low[0], bars.close[0]),
                         (1005 * E8 // 10, 101 * E8, 100 * E8, 10025 * E8 // 100))
        self.assertEqual((bars.volume[0], bars.taker_buy_volume[0], bars.trades[0]), (25 * E8 // 10, E8, 4))
        self.assertEqual(bars.mark_open[0], 1004 * E8 // 10)
        self.assertEqual([bars.open[1], bars.close[1], bars.trades[1]], [MISSING, MISSING, 0])
        self.assertEqual(bars.flags[1], 3)
        self.assertEqual((bars.mark_open[2], bars.mark_close[2], bars.open[2]), (MISSING, MISSING, 1005 * E8 // 10))
        self.assertEqual(bars.volume[3], 21845367 * E8)
        self.assertEqual(bars.index_of(START + 120_000), 2)
        for bad in (START - 60_000, START + 30_000, END):
            with self.subTest(ms=bad), self.assertRaises(ValueError):
                bars.index_of(bad)

    def test_gzip_binary_equals_text(self):
        text = bars_text()
        self.assertEqual(read_bars_csv(io.BytesIO(gzip.compress(text.encode("ascii"))), "BTCUSDT", MONTH),
                         read_bars_csv(io.StringIO(text), "BTCUSDT", MONTH))

    def test_structural_errors_name_line_and_column(self):
        cases = (
            (bars_text(header="open_time_ms,open"), r"header"),
            (bars_text(drop=(5,)), r"line 6 column open_time_ms: expected open time"),
            (bars_text({5: {"open_time_ms": str(START + 4 * 60_000)}}), r"line 6 column open_time_ms"),
            (bars_text(drop=(MINUTES - 1,)), rf"expected {MINUTES} data lines, got {MINUTES - 1}"),
            (bars_text({7: {"volume": "abc"}}), r"line 8 column volume"),
            (bars_text({7: {"high": "-1"}}), r"line 8 column high"),
            (bars_text({9: {"trades": "1.5"}}), r"line 10 column trades"),
            (bars_text({9: {"flags": "70000"}}), r"line 10 column flags"),
        )
        for text, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                read_bars_csv(io.StringIO(text), "BTCUSDT", MONTH)
        extra = bars_text() + ",".join(["1"] * len(data_lake.COLUMNS)) + "\n"
        with self.assertRaisesRegex(ValueError, rf"line {MINUTES + 1}"):
            read_bars_csv(io.StringIO(extra), "BTCUSDT", MONTH)
        with self.assertRaises(ValueError):
            read_bars_csv(io.StringIO(bars_text()), "ADAUSDT", MONTH)


class SeriesTests(unittest.TestCase):
    def test_concat_contiguity(self):
        first, second = series(START, [10, 20]), series(START + 120_000, [30])
        joined = BarSeries.concat([first, second])
        self.assertEqual((joined.minutes, list(joined.open), joined.end_ms), (3, [10, 20, 30], START + 180_000))
        self.assertEqual(joined.index_of(START + 120_000), 2)
        with self.assertRaisesRegex(ValueError, "not contiguous"):
            BarSeries.concat([first, series(START + 180_000, [30])])
        with self.assertRaisesRegex(ValueError, "not contiguous"):
            BarSeries.concat([first, series(START, [30])])
        with self.assertRaisesRegex(ValueError, "cannot concat"):
            BarSeries.concat([first, series(START + 120_000, [30], symbol="ETHUSDT")])

    def test_infer_tick(self):
        prices = [100_10 * 10 ** 6, 100_20 * 10 ** 6, MISSING, 99_90 * 10 ** 6]  # 100.10, 100.20, -, 99.90
        self.assertEqual(infer_tick(series(START, prices)), 10 * 10 ** 6)  # 0.1
        odd = prices + [100_15 * 10 ** 6]  # one price at 100.15 halves the gcd
        self.assertEqual(infer_tick(series(START, odd)), 5 * 10 ** 6)
        with self.assertRaises(ValueError):
            infer_tick(series(START, [MISSING, MISSING]))

    def test_compromised_in(self):
        flags = [0, data_lake.FLAG_KLINE_ROW_MISSING, 0, data_lake.FLAG_MARK_MISSING, 0]
        bars = series(START, [1] * 5, flags=flags)
        self.assertFalse(compromised_in(bars, 0, 2))  # KLINE_ROW_MISSING is not compromising
        self.assertTrue(compromised_in(bars, 0, 3))
        self.assertTrue(compromised_in(bars, 3, 3))
        self.assertTrue(compromised_in(bars, 0, 2, mask=data_lake.FLAG_KLINE_ROW_MISSING))
        self.assertEqual(COMPROMISED_FLAGS, 1 | 8 | 16)
        for start, end in ((-1, 2), (3, 2), (0, 5)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                compromised_in(bars, start, end)


def funding_text(rows, header=None):
    lines = [header if header is not None else ",".join(data_lake.FUNDING_COLUMNS)]
    lines += [f"{t},{hours},{rate},{published}" for t, hours, rate, published in rows]
    return "\n".join(lines) + "\n"


FUNDING_ROWS = (
    (START, 8, "0.0001", "0.00010000"),
    (START + 8 * HOUR, 8, "0.00000098", "9.8E-7"),
    (START + 16 * HOUR, 4, "-0.00025", "-2.50E-4"),
    (START + 20 * HOUR, 1, "0.0003", "0.0003"),
    (START + 21 * HOUR, 8, "0", "0"),
    (START + 24 * HOUR, 8, "0.0001", "0.0001"),  # 3h after an 8h row: a spacing mismatch
)


class FundingTests(unittest.TestCase):
    def test_read_exponent_rates_and_report(self):
        text = funding_text(FUNDING_ROWS)
        funding = read_funding_csv(io.StringIO(text), MONTH)
        self.assertEqual(funding, read_funding_csv(io.BytesIO(gzip.compress(text.encode())), MONTH))
        self.assertEqual(funding.rate[1], Fraction(98, 10 ** 8))
        self.assertEqual(funding.rate[2], Fraction(-1, 4000))
        self.assertEqual(funding.interval_hours, (8, 8, 4, 1, 8, 8))
        self.assertEqual((funding.start_ms, funding.end_ms), (START, END))
        self.assertEqual(interval_report(funding), {"settlements": 6, "interval_hours_counts": {"1": 1, "4": 1, "8": 4},
                                                    "spacing_mismatches": 1})
        # The published spelling column is not what the rate is read from.
        exponent_only = funding_text([(START, 8, "9.8E-7", "ignored")])
        self.assertEqual(read_funding_csv(io.StringIO(exponent_only), MONTH).rate, (Fraction(98, 10 ** 8),))

    def test_header_only_and_errors(self):
        empty = read_funding_csv(io.StringIO(funding_text([])), MONTH)
        self.assertEqual((empty.calc_time_ms, interval_report(empty)["settlements"]), ((), 0))
        cases = (
            (funding_text([], header="calc_time,funding_interval_hours,last_funding_rate"), "header"),
            (funding_text([FUNDING_ROWS[1], FUNDING_ROWS[0]]), "line 2: calc_time_ms"),
            (funding_text([FUNDING_ROWS[0], FUNDING_ROWS[0]]), "line 2: calc_time_ms"),
            (funding_text([(END, 8, "0", "0")]), "line 1: calc_time_ms .* outside"),
            (funding_text([(START, 8, "NaN", "NaN")]), "line 1 column last_funding_rate"),
            (funding_text([(START, "8h", "0", "0")]), "line 1: calc_time_ms/funding_interval_hours"),
        )
        for text, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                read_funding_csv(io.StringIO(text), MONTH)

    def test_events_between_bounds(self):
        funding = read_funding_csv(io.StringIO(funding_text(FUNDING_ROWS)), MONTH)
        self.assertEqual(funding.events_between(START, START + 16 * HOUR),
                         [(START + 8 * HOUR, Fraction(98, 10 ** 8), 8), (START + 16 * HOUR, Fraction(-1, 4000), 4)])
        self.assertEqual(funding.events_between(START + 8 * HOUR, START + 8 * HOUR), [])
        self.assertEqual(len(funding.events_between(START, END - 1)), 5)  # START itself is excluded (after < t)
        for after, upto in ((START - 1, START), (START, END), (START + 2, START + 1)):
            with self.subTest(after=after, upto=upto), self.assertRaises(ValueError):
                funding.events_between(after, upto)

    def test_concat_contiguous_months(self):
        march_start, march_end, _ = data_lake.month_bounds_ms("2025-03")
        february = read_funding_csv(io.StringIO(funding_text(FUNDING_ROWS[:2])), MONTH)
        march = read_funding_csv(io.StringIO(funding_text([(march_start, 8, "0.0001", "0.0001")])), "2025-03")
        joined = FundingSeries.concat([february, march])
        self.assertEqual((joined.start_ms, joined.end_ms, len(joined.calc_time_ms)), (START, march_end, 3))
        self.assertEqual(joined.events_between(START + 8 * HOUR, march_start),
                         [(march_start, Fraction(1, 10_000), 8)])  # the month-boundary settlement
        with self.assertRaisesRegex(ValueError, "not contiguous"):
            FundingSeries.concat([march, february])

    def test_settlement_mark_rule(self):
        bars = series(START, [10, 20, 30], marks=[100, MISSING, 300])
        self.assertEqual(settlement_mark(bars, START), 100)
        self.assertEqual(settlement_mark(bars, START + 30_000), 100)  # open of the containing minute
        self.assertIsNone(settlement_mark(bars, START + 60_000))  # MISSING mark
        self.assertEqual(settlement_mark(bars, START + 179_999), 300)
        self.assertIsNone(settlement_mark(bars, START + 180_000))
        self.assertIsNone(settlement_mark(bars, START - 1))


if __name__ == "__main__":
    unittest.main()
