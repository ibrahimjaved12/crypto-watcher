"""Stop-aware first-touch labeler for the #182 benchmark."""
from __future__ import annotations

from array import array
from fractions import Fraction
from math import floor
import unittest

from market_analysis import data_lake
from market_analysis.benchmark import costs
from market_analysis.benchmark.bars import MISSING, BarSeries
from market_analysis.benchmark.funding import FundingSeries
from market_analysis.benchmark.scan import Cell, CellPair, label_trade, next_compromised

START = data_lake.month_bounds_ms("2025-01")[0]
MODEL = costs.COST_MODEL_V1
FLAT = (10_000, 10_000, 10_000, 10_000)
UR = 10 ** 6


def make_bars(rows, marks=None, flags=None):
    """Minute i = rows[i] (open, high, low, close); marks default to the trade prices."""
    count = len(rows)
    marks = marks or {}
    columns = {name: array("q", [row[position] for row in rows])
               for position, name in enumerate(("open", "high", "low", "close"))}
    mark_rows = [marks.get(i, rows[i]) for i in range(count)]
    columns.update({name: array("q", [row[position] for row in mark_rows])
                    for position, name in enumerate(("mark_open", "mark_high", "mark_low", "mark_close"))})
    columns.update({name: array("q", [0] * count) for name in ("volume", "taker_buy_volume", "trades")})
    flag_values = [0] * count
    for index, value in (flags or {}).items():
        flag_values[index] = value
    return BarSeries("BTCUSDT", START, count, flags=array("H", flag_values), **columns)


def no_funding(bars):
    return FundingSeries((), (), (), bars.start_ms, bars.end_ms)


def series(*minutes, length=8):
    """Minute 0 is the signal minute, minute 1 the entry; unspecified minutes are flat."""
    rows = [FLAT] * length
    for index, row in minutes:
        rows[index] = row
    return rows


def label(rows, side=1, stop=9_900, targets=(10_100, 10_200), window=6, model=MODEL, funding=None, **kwargs):
    bars = make_bars(rows, **kwargs)
    return bars, label_trade(bars, funding or no_funding(bars), model, tick=1, entry_index=1, side=side,
                             stop_price=stop, target_prices=targets, window_minutes=window)


def entry(bars, side, model=MODEL):
    fill = costs.entry_fill_price(side, bars.open[1], bars.high[1], bars.low[1], model)
    return fill, costs.fee(fill, model.taker_rate, model)


def ur(value, risk=100):
    return round(Fraction(value) * UR / risk)


class TargetAndStopTests(unittest.TestCase):
    def test_long_target_first(self):
        bars, trade = label(series((3, (10_040, 10_101, 10_030, 10_090))))
        self.assertEqual(trade.status, "T")
        first, second = trade.cells
        self.assertEqual((first.pess.outcome, first.pess.exit_offset, first.opt), ("T", 2, None))
        self.assertEqual((second.pess.outcome, second.pess.exit_offset), ("E", 5))
        fill, fee_in = entry(bars, 1)
        net = 10_100 - fill - fee_in - costs.fee(10_100, MODEL.maker_rate, MODEL)
        self.assertEqual(first.pess.net_ur, ur(net))
        self.assertEqual(first.pess.net_ur + first.pess.cost_ur - first.pess.fund_ur, UR)  # gross = +1R
        self.assertEqual(trade.p0, 10_000)
        self.assertEqual(trade.wallet_ur, round(fill / trade.leverage / 100 * UR))

    def test_short_target_first(self):
        _, trade = label(series((3, (9_960, 9_970, 9_899, 9_910))), side=-1, stop=10_100, targets=(9_900, 9_800))
        self.assertEqual([pair.pess.outcome for pair in trade.cells], ["T", "E"])
        self.assertEqual(trade.cells[0].pess.exit_offset, 2)

    def test_stop_first(self):
        bars, trade = label(series((2, (10_000, 10_000, 9_900, 9_950))))
        self.assertEqual([(pair.pess.outcome, pair.pess.exit_offset) for pair in trade.cells], [("S", 1), ("S", 1)])
        fill, fee_in = entry(bars, 1)
        exit_fill = costs.stop_fill_price(1, 9_900, 10_000, 10_000, 9_900, MODEL)
        net = exit_fill - fill - fee_in - costs.fee(exit_fill, MODEL.taker_rate, MODEL)
        cell = trade.cells[0].pess
        self.assertEqual(cell.net_ur, ur(net))
        self.assertEqual(cell.net_ur + cell.cost_ur - cell.fund_ur, ur(9_900 - 10_000))  # exit_ref = stop

    def test_same_minute_tie(self):
        _, trade = label(series((2, (10_000, 10_101, 9_900, 10_000))))
        ambiguous, plain = trade.cells
        self.assertEqual((ambiguous.pess.outcome, ambiguous.opt.outcome), ("S", "T"))
        self.assertEqual((ambiguous.pess.exit_offset, ambiguous.opt.exit_offset), (1, 1))
        self.assertEqual((plain.pess.outcome, plain.opt), ("S", None))  # 10200 was not traded through
        # An open through the target or through the stop resolves the tie.
        _, through_target = label(series((2, (10_101, 10_150, 9_900, 10_000))))
        self.assertEqual((through_target.cells[0].pess.outcome, through_target.cells[0].opt), ("T", None))
        _, through_stop = label(series((2, (9_900, 10_150, 9_850, 10_000))))
        self.assertEqual((through_stop.cells[0].pess.outcome, through_stop.cells[0].opt), ("S", None))

    def test_gap_through_the_stop_fills_at_the_open(self):
        bars, trade = label(series((2, (9_850, 9_860, 9_840, 9_850))))
        cell = trade.cells[0].pess
        self.assertEqual(cell.outcome, "S")
        fill, fee_in = entry(bars, 1)
        exit_fill = costs.stop_fill_price(1, 9_900, 9_850, 9_860, 9_840, MODEL)
        self.assertEqual(exit_fill, 9_850 - 5)  # min(stop, open) - max(2bps floor, range / 4)
        self.assertEqual(cell.net_ur, ur(exit_fill - fill - fee_in - costs.fee(exit_fill, MODEL.taker_rate, MODEL)))
        self.assertEqual(cell.net_ur + cell.cost_ur - cell.fund_ur, ur(9_850 - 10_000))

    def test_touch_without_trading_through_is_not_a_hit(self):
        _, trade = label(series((3, (10_050, 10_100, 10_040, 10_090))))
        self.assertEqual(trade.cells[0].pess.outcome, "E")

    def test_entry_minute_ignores_targets_but_not_the_stop(self):
        _, trade = label(series((1, (10_000, 10_300, 10_000, 10_000))))
        self.assertEqual([pair.pess.outcome for pair in trade.cells], ["E", "E"])
        _, stopped = label(series((1, (10_000, 10_000, 9_900, 9_950))))
        self.assertEqual((stopped.cells[0].pess.outcome, stopped.cells[0].pess.exit_offset), ("S", 0))

    def test_expiry(self):
        bars, trade = label(series())
        cell = trade.cells[0].pess
        self.assertEqual((cell.outcome, cell.exit_offset), ("E", 5))
        fill, fee_in = entry(bars, 1)
        exit_fill = costs.entry_fill_price(-1, 10_000, 10_000, 10_000, MODEL)
        self.assertEqual(cell.net_ur, ur(exit_fill - fill - fee_in - costs.fee(exit_fill, MODEL.taker_rate, MODEL)))


class LiquidationAndDataTests(unittest.TestCase):
    def test_mark_liquidation_beats_same_minute_stop(self):
        rows = series((2, (10_000, 10_000, 9_900, 9_950)))
        bars, trade = label(rows, marks={2: (10_000, 10_000, 9_500, 9_950)})
        cell = trade.cells[0].pess
        self.assertEqual((cell.outcome, cell.exit_offset), ("L", 1))
        fill, fee_in = entry(bars, 1)
        lp = costs.liquidation_price(1, fill, trade.leverage, MODEL.mmr)
        self.assertLessEqual(9_500, floor(lp))
        loss = costs.liquidation_loss(1, fill, lp, MODEL, leverage=trade.leverage)
        self.assertEqual(cell.net_ur, ur(max(-loss - fee_in, -fill / trade.leverage)))
        self.assertEqual(cell.net_ur + cell.cost_ur - cell.fund_ur, ur(lp - 10_000))

    def test_compromised_minutes(self):
        broken = (MISSING, MISSING, MISSING, MISSING)
        flags = {4: data_lake.FLAG_NO_AGGTRADES | data_lake.FLAG_MARK_MISSING}
        _, nothing_before = label(series((4, broken)), flags=flags, marks={4: broken})
        self.assertEqual([(p.pess.outcome, p.pess.exit_offset) for p in nothing_before.cells], [("X", 3), ("X", 3)])
        self.assertEqual(nothing_before.cells[0].pess.net_ur, None)
        _, target_first = label(series((3, (10_040, 10_101, 10_030, 10_090)), (4, broken)), flags=flags,
                                marks={4: broken})
        self.assertEqual([(p.pess.outcome, p.pess.exit_offset) for p in target_first.cells], [("T", 2), ("X", 3)])
        _, entry_broken = label(series((1, broken)), flags={1: data_lake.FLAG_NO_AGGTRADES}, marks={1: broken})
        self.assertEqual(entry_broken.status, "C")
        _, too_long = label(series(), window=8)
        self.assertEqual(too_long.status, "I")
        self.assertEqual(list(next_compromised(make_bars(series(), flags={2: 1, 5: 16}))), [2, 2, 2, 5, 5, 5, 8, 8])

    def test_funding_boundaries_and_sign(self):
        for side, stop, targets in ((1, 9_900, (10_100,)), (-1, 10_100, (9_900,))):
            rows = series()
            bars = make_bars(rows)
            funding = FundingSeries((bars.open_time(1), bars.open_time(6)), (8, 8),
                                    (Fraction(1, 1_000), Fraction(1, 10_000)), bars.start_ms, bars.end_ms)
            trade = label_trade(bars, funding, MODEL, tick=1, entry_index=1, side=side, stop_price=stop,
                                target_prices=targets, window_minutes=6)
            cell = trade.cells[0].pess
            with self.subTest(side=side):
                self.assertEqual((cell.outcome, cell.exit_offset), ("E", 5))
                # Only the settlement at open(x) is charged; open(e) is not.
                expected = costs.funding_cash(side, 10_000, Fraction(1, 10_000), MODEL)
                self.assertEqual(expected, -side)
                self.assertEqual(cell.fund_ur, ur(expected))
                plain = label_trade(bars, no_funding(bars), MODEL, tick=1, entry_index=1, side=side, stop_price=stop,
                                    target_prices=targets, window_minutes=6).cells[0].pess
                self.assertEqual(cell.net_ur - plain.net_ur, ur(expected))
        bars = make_bars(series())
        bars.mark_open[3] = MISSING  # a settlement without a mark price makes the cell X
        funding = FundingSeries((bars.open_time(3),), (8,), (Fraction(1, 10_000),), bars.start_ms, bars.end_ms)
        cell = label_trade(bars, funding, MODEL, tick=1, entry_index=1, side=1, stop_price=9_900,
                           target_prices=(10_100,), window_minutes=6).cells[0].pess
        self.assertEqual((cell.outcome, cell.net_ur), ("X", None))


class AccountingTests(unittest.TestCase):
    SCENARIOS = (
        series((3, (10_040, 10_101, 10_030, 10_090))),
        series((2, (10_000, 10_000, 9_900, 9_950))),
        series((2, (10_000, 10_101, 9_900, 10_000))),
        series((2, (9_850, 9_860, 9_840, 9_850))),
        series((3, (10_100, 10_250, 10_090, 10_200))),
        series(),
    )

    def test_cost_multiplier_zero_is_gross_plus_funding(self):
        free = costs.with_multiplier(MODEL, 0)
        for rows in self.SCENARIOS:
            _, trade = label(rows, model=free)
            for pair in trade.cells:
                for cell in (pair.pess, pair.opt):
                    if cell is None:
                        continue
                    with self.subTest(rows=rows, cell=cell):
                        self.assertEqual(cell.cost_ur, 0)
                        self.assertEqual(cell.fund_ur, 0)

    def test_identity_and_one_pass_equals_single_targets(self):
        for rows in self.SCENARIOS:
            bars, together = label(rows, targets=(10_100, 10_200))
            for index, target in enumerate((10_100, 10_200)):
                _, alone = label(rows, targets=(target,))
                with self.subTest(rows=rows, target=target):
                    self.assertEqual(alone.cells[0], together.cells[index])
                    self.assertEqual((alone.p0, alone.leverage, alone.wallet_ur),
                                     (together.p0, together.leverage, together.wallet_ur))
            # gross = net + cost - fund must equal side * (exit_ref - p0) / d, computed independently.
            targets = {0: 10_100, 1: 10_200}
            for index, pair in enumerate(together.cells):
                for cell, is_opt in ((pair.pess, False), (pair.opt, True)):
                    if cell is None:
                        continue
                    x = 1 + cell.exit_offset
                    exit_ref = {"T": targets[index], "S": min(9_900, bars.open[x]), "E": bars.close[x]}[cell.outcome]
                    with self.subTest(rows=rows, target=index, optimistic=is_opt):
                        self.assertEqual(cell.net_ur + cell.cost_ur - cell.fund_ur, ur(exit_ref - 10_000))

    def test_invalid_levels(self):
        for kwargs in ({"stop": 10_100}, {"targets": (9_900,)}, {"targets": ()}, {"side": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                label(series(), **kwargs)


if __name__ == "__main__":
    unittest.main()
