"""candle-table tv-v2: tolerant trailing_rank and the insufficient-row manifest fields (small data)."""
from bisect import bisect_right
import io
import random
import unittest

from market_analysis import data_lake
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.patterns import candle_table as ct

COLUMNS = ct.REGISTRY.columns()
START = data_lake.month_bounds_ms("2024-01")[0]


def old_trailing_rank(values, window):
    """The tv-v1 definition, by brute force: all ``window`` values must be present."""
    out = [None] * len(values)
    for j in range(window - 1, len(values)):
        span = values[j - window + 1:j + 1]
        if None not in span:
            out[j] = sum(1 for v in span if v <= values[j]) / window
    return out


def brute_rank(values, window, j):
    span = [v for v in values[max(0, j - window + 1):j + 1] if v is not None]
    return sum(1 for v in span if v <= values[j]) / len(span)


class TrailingRankTests(unittest.TestCase):
    def test_equals_old_behaviour_when_nothing_is_missing(self):
        rng = random.Random(3)
        values = [rng.choice([0.1, 0.2, 0.3, 0.5, 0.9]) + rng.random() * (i % 3) for i in range(300)]
        for window in (1, 5, 52, 90):
            self.assertEqual(ct.trailing_rank(values, window), old_trailing_rank(values, window))

    def test_one_missing_value_does_not_blank_the_next_window(self):
        values = [float(i % 7) for i in range(60)]
        values[20] = None
        out = ct.trailing_rank(values, 10)
        self.assertIsNone(out[20])
        self.assertIsNotNone(out[21])
        self.assertIsNotNone(out[29])
        self.assertEqual(out[25], brute_rank(values, 10, 25))
        self.assertEqual(out[29], brute_rank(values, 10, 29))  # window 20..29 holds 9 present values
        self.assertEqual(out[30], brute_rank(values, 10, 30))  # None has left the window: full again

    def test_too_many_missing_gives_none(self):
        values = [float(i) for i in range(30)]
        for j in (12, 13):
            values[j] = None
        out = ct.trailing_rank(values, 10)  # need ceil(0.8 * 10) = 8 present
        self.assertEqual(out[14], 1.0)  # window 5..14 holds 8 present values: allowed
        values[14] = None
        out = ct.trailing_rank(values, 10)
        self.assertIsNone(out[15])  # window 6..15 holds 7 present values
        self.assertIsNotNone(out[16 + 6])  # recovers once the gap leaves the window

    def test_warm_up_stays_none(self):
        out = ct.trailing_rank([1.0, 2.0, 3.0, 4.0, 5.0], 4)
        self.assertEqual(out[:3], [None, None, None])
        self.assertEqual(out[3:], [1.0, 1.0])
        self.assertEqual(ct.trailing_rank([1.0, 2.0], 5), [None, None])

    def test_missing_value_at_j_gives_none(self):
        values = [float(i) for i in range(20)]
        values[15] = None
        out = ct.trailing_rank(values, 10)
        self.assertIsNone(out[15])
        self.assertIsNotNone(out[14])
        self.assertIsNotNone(out[16])

    def test_uses_only_values_up_to_j(self):
        rng = random.Random(8)
        values = [rng.random() if rng.random() > 0.1 else None for _ in range(200)]
        base = ct.trailing_rank(values, 20)
        changed = values[:120] + [5.0 if v is not None else None for v in values[120:]]
        self.assertEqual(ct.trailing_rank(changed, 20)[:120], base[:120])

    def test_matches_brute_force_with_gaps(self):
        rng = random.Random(11)
        values = [rng.randint(0, 6) if rng.random() > 0.15 else None for _ in range(250)]
        window = 25
        out = ct.trailing_rank(values, window)
        for j, value in enumerate(out):
            span = values[max(0, j - window + 1):j + 1]
            present = [v for v in span if v is not None]
            if values[j] is None or j < window - 1 or len(present) < 0.8 * window:
                self.assertIsNone(value, j)
            else:
                self.assertEqual(value, bisect_right(sorted(present), values[j]) / len(present), j)

    def test_group_version(self):
        self.assertEqual(ct.REGISTRY.versions()["trend_vol"], "tv-v2")
        self.assertEqual(ct.MIN_RANK_FRACTION, 0.8)


class InsufficientCountsTests(unittest.TestCase):
    WHERE = {name: COLUMNS.index(name) for name in ("symbol", "tf", "status", "segment", "reasons", "decision_ms")}

    def row(self, symbol, status, reasons, decision_ms, segment="development"):
        row = dict.fromkeys(COLUMNS)
        row.update(symbol=symbol, tf=15, status=status, segment=segment, reasons=reasons, decision_ms=decision_ms)
        return tuple(row[name] for name in COLUMNS)

    def counted(self):
        day = 86_400_000
        rows = [self.row("BTCUSDT", "insufficient", "trend_vol", START + day),
                self.row("BTCUSDT", "insufficient", "trend_vol", START + 2 * day),
                self.row("BTCUSDT", "insufficient", "geometry;trend_vol", START + 40 * day),
                self.row("BTCUSDT", "insufficient", "trend_vol", START + 40 * day, "validation"),
                self.row("ETHUSDT", "insufficient", "trend_vol", START + 40 * day),
                self.row("BTCUSDT", "ok", "", START + 3 * day),
                self.row("BTCUSDT", "invalid", "invalid_candle", START + 4 * day)]
        counts = {}
        for row in rows:
            ct.count_row(counts, row, self.WHERE)
        return counts

    def test_counts_by_reason_and_month(self):
        counts = self.counted()
        self.assertEqual(counts["insufficient_reason"], {
            "BTCUSDT|15|development|trend_vol": 2, "BTCUSDT|15|development|geometry;trend_vol": 1,
            "BTCUSDT|15|validation|trend_vol": 1, "ETHUSDT|15|development|trend_vol": 1})
        self.assertEqual(counts["insufficient_month"],
                         {"BTCUSDT|15|2024-01": 2, "BTCUSDT|15|2024-02": 2, "ETHUSDT|15|2024-02": 1})

    def test_manifest_has_and_hashes_the_fields(self):
        common = dict(files=[], counts=self.counted(), inputs={}, through_month="2025-12", parity={},
                      symbols=["BTCUSDT", "ETHUSDT"], timeframes=[15], code_commit="a", created_utc="b")
        manifest = ct.build_manifest(**common)
        self.assertEqual(manifest["insufficient_reason_counts"]["BTCUSDT|15|development|trend_vol"], 2)
        self.assertEqual(manifest["insufficient_by_month"]["BTCUSDT|15|2024-02"], 2)
        ct.check_manifest(manifest)
        for key in ("insufficient_reason_counts", "insufficient_by_month"):
            tampered = {**manifest, key: {}}
            with self.assertRaises(ValueError):
                ct.check_manifest(tampered)
        empty = ct.build_manifest(**{**common, "counts": {}})
        self.assertEqual((empty["insufficient_reason_counts"], empty["insufficient_by_month"]), ({}, {}))
        self.assertNotEqual(empty["content_hash"], manifest["content_hash"])

    def test_write_table_feeds_the_counts(self):
        n, price = 30, 100 * 10 ** 8
        candles = CandleSeries("BTCUSDT", 15, START, open=[price] * n, high=[price + 10 ** 8] * n,
                               low=[price - 10 ** 8] * n, close=[price] * n, valid=[True] * n,
                               volume=[10 ** 9] * n, taker_buy_volume=[5 * 10 ** 8] * n)
        ctx = ct.TableContext("BTCUSDT", 15, candles, [0.01] * n, "test", ct.table_segments(15), None)
        counts = {}
        written = ct.write_table(io.BytesIO(), ct.build_rows(ctx), counts=counts)
        self.assertEqual(sum(counts["insufficient_month"].values()), sum(
            n for key, n in counts["rows"].items() if "|insufficient|" in key))
        self.assertEqual(sum(counts["insufficient_reason"].values()), sum(counts["insufficient_month"].values()))
        self.assertEqual(written, 30)


if __name__ == "__main__":
    unittest.main()
