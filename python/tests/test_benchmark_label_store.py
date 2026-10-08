"""Small hand-built labels-v1 files, guarded reads and exact boundary purging."""
from array import array
from fractions import Fraction
import gzip
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market_analysis.benchmark.hidden_guard import HiddenGate, HiddenStretchLocked, Plan, write_plan
from market_analysis.benchmark.label_store import Geometry, load_geometry_columns
from market_analysis.benchmark.labels import LabelParams
from market_analysis.data_lake import month_bounds_ms

PARAMS = LabelParams(horizons=(15,), half_life_days=((15, 1),), step_minutes=((15, 5),),
                     k_grid=(Fraction(1),), rr_grid=(Fraction(1), Fraction(2)))
G = Geometry("BTCUSDT", 15, 1, Fraction(1), 0)


def row(ms, cell="T:5:1000000:100000:-50000", *, status="T", side=1):
    values = [str(ms), "15", str(side), "1", status]
    if status == "T":
        values += ["1000000", "1000", "10", "2", "2000000", cell, "E:59:200000:100000:-50000"]
    else:
        values += [""] * (len(PARAMS.header) - 5)
    return ",".join(values)


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def write(self, month, rows):
        path = self.directory / f"labels__BTCUSDT__{month}.csv.gz"
        with gzip.open(path, "wt", encoding="ascii", newline="") as stream:
            stream.write(",".join(PARAMS.header) + "\n" + "\n".join(rows) + "\n")

    def load(self, months, geometries=(G,), **kwargs):
        return load_geometry_columns(self.directory, "BTCUSDT", months, geometries, PARAMS, **kwargs)

    def test_requested_columns_reasons_x_and_optimism(self):
        start = month_bounds_ms("2025-01")[0]
        self.write("2025-01", [row(start), row(start, side=-1), row(start + 300_000, status="V"),
                               row(start + 600_000, "S:2:-1000000:200000:-50000|T:2:2000000:100000:-50000"),
                               row(start + 900_000, "X:10:::")])
        columns = self.load(["2025-01"])
        self.assertEqual(list(columns), [G])
        column = columns[G]
        for values in column.data.values():
            self.assertIsInstance(values, array)
            self.assertEqual((values.typecode, values.itemsize), ("q", 8))
        self.assertEqual(list(column["signal_ms"]), [start, start + 600_000, start + 900_000])
        self.assertEqual(list(column["outcome"]), list(map(ord, "TSX")))
        self.assertEqual(list(column["net_ur"]), [1_000_000, -1_000_000, 0])
        self.assertEqual(list(column["opt_net_ur"]), [1_000_000, 2_000_000, 0])
        self.assertEqual(list(column["amb"]), [0, 1, 0])
        self.assertEqual(list(column.non_trades["reason"]), [ord("V")])
        self.assertEqual(list(column.non_trades["signal_ms"]), [start + 300_000])
        second = G._replace(rr_index=1)
        self.assertEqual(list(self.load(["2025-01"], [second])[second]["net_ur"]), [200_000] * 3)

    def test_purge_worst_case_at_validation_hidden_boundary(self):
        boundary = month_bounds_ms("2026-01")[0]
        self.write("2025-12", [row(boundary - 3_900_000), row(boundary - 3_600_000),
                               row(boundary - 300_000, "T:0:1000000:0:0")])
        column = self.load(["2025-12"], segment="validation")[G]
        self.assertEqual(list(column["signal_ms"]), [boundary - 3_900_000])
        self.assertEqual(list(column.purged_signal_ms), [boundary - 3_600_000, boundary - 300_000])
        self.assertEqual(len(self.load(["2025-12"], segment="hidden")[G]), 0)
        self.assertEqual(list(self.load(["2025-12"])[G]["signal_ms"]), [boundary - 3_900_000])

    def test_guard_before_opening_any_file_and_valid_token(self):
        # December is absent: hidden authorization must fail before any file read.
        with self.assertRaises(HiddenStretchLocked):
            self.load(iter(["2025-12", "2026-01"]))
        with self.assertRaises(HiddenStretchLocked):
            self.load(["2026-01"], segment="development")
        now = "2026-10-08T12:00:00Z"
        plan = Plan("q", "test", [{"strategy_id": "s", "strategy_version": "1", "config": {}}],
                    1, "mean", "1/10", "hidden", "snapshot", now)
        write_plan(self.directory, plan)
        gate = HiddenGate(self.directory, self.directory / "opens.jsonl")
        token = gate.open_hidden("q", plan.plan_id, now_utc=now)
        first = month_bounds_ms("2026-01")[0]
        self.write("2026-01", [row(first)])
        self.assertEqual(list(self.load(["2026-01"], token=token, gate=gate)[G]["signal_ms"]), [first])

    def test_sorted_months_and_invalid_rows(self):
        jan, feb = month_bounds_ms("2025-01")[0], month_bounds_ms("2025-02")[0]
        self.write("2025-01", [row(jan)])
        self.write("2025-02", [row(feb)])
        self.assertEqual(list(self.load(["2025-02", "2025-01"])[G]["signal_ms"]), [jan, feb])
        for rows in ([row(jan), row(jan)], [row(jan + 300_000), row(jan)], [row(feb)]):
            self.write("2025-01", rows)
            with self.assertRaises(ValueError):
                self.load(["2025-01"])
        with self.assertRaises(ValueError):
            self.load(["2025-02", "2025-02"])
        with self.assertRaises(ValueError):
            self.load(["2025-02"], [G._replace(rr_index=2)])


if __name__ == "__main__":
    unittest.main()
