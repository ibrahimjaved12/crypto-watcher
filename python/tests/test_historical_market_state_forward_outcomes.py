"""Generated trade-candle fixtures for Part-B forward-label semantics."""

from decimal import Decimal
import math
import unittest

from market_analysis.historical_market_state_forward_outcomes import (
    END_PRICE_UNAVAILABLE, INCOMPLETE_FUTURE_MINUTE_PATH,
    STATE_PATH_OUTSIDE_EVALUATION_INTERVAL, TradePriceForwardEvidence,
    continuous_decision_times, evaluate_continuous_grids,
    evaluate_event_outcomes, evaluate_forward_outcome, evaluate_v1_state_path,
    reject_retrospective_forward_labels,
)
from market_analysis.historical_ohlc_evidence import CompletedTradeOHLCCandle


MINUTE = 60_000
DAY = 86_400_000
SOURCE_SHA = "a" * 64


def candle(symbol, opening, close):
    price = Decimal(str(close))
    return CompletedTradeOHLCCandle(
        symbol, f"binance-usdm:{symbol}", opening, opening + MINUTE - 1,
        price, price, price, price, opening + MINUTE)


def evidence(*, omit=(), symbols=("AAAUSDT", "BBBUSDT", "CCCUSDT"),
             start=60 * MINUTE, end=60 * MINUTE + DAY):
    rows = []
    for symbol_index, symbol in enumerate(symbols):
        for offset in range(-1, 61):
            opening = start + offset * MINUTE
            if (symbol, opening) in omit:
                continue
            close = (100 + symbol_index * 10) * (1 + offset * .01)
            rows.append(candle(symbol, opening, close))
    return TradePriceForwardEvidence(
        symbols, tuple(rows), SOURCE_SHA, start - MINUTE, end)


class ForwardOutcomeTests(unittest.TestCase):
    def test_exact_minute_anchor_and_raw_forward_formula(self):
        source = evidence(symbols=("AAAUSDT",))
        decision = 60 * MINUTE
        outcome = evaluate_forward_outcome(source, decision, 1)
        row = outcome.per_symbol[0]
        self.assertEqual(row.start_candle_open_time_ms, decision - MINUTE)
        self.assertEqual(row.end_candle_open_time_ms, decision)
        self.assertAlmostEqual(row.forward_log_return, math.log(100 / 99))

    def test_five_second_event_uses_latest_causally_complete_close(self):
        source = evidence(symbols=("AAAUSDT",))
        decision = 60 * MINUTE + 35_000
        outcome = evaluate_forward_outcome(source, decision, 1)
        row = outcome.per_symbol[0]
        self.assertEqual(row.start_candle_open_time_ms, decision - MINUTE - 35_000)
        self.assertEqual(row.end_candle_open_time_ms, decision - 35_000)

    def test_breadth_denominator_rv_and_unscaled_mad(self):
        source = evidence()
        outcome = evaluate_forward_outcome(source, 60 * MINUTE, 1)
        self.assertEqual(outcome.future_directional_breadth_denominator, 3)
        self.assertEqual(outcome.future_directional_breadth, 1.0)
        values = tuple(item.forward_log_return for item in outcome.per_symbol)
        center = sorted(values)[1]
        self.assertAlmostEqual(outcome.forward_cross_sectional_dispersion,
                               sorted(abs(value - center) for value in values)[1])
        self.assertAlmostEqual(outcome.per_symbol[0].forward_realized_volatility,
                               abs(math.log(100 / 99)))

    def test_missing_future_minute_is_never_imputed_for_rv(self):
        decision = 60 * MINUTE
        missing = ("AAAUSDT", decision)
        source = evidence(omit=(missing,), symbols=("AAAUSDT",))
        outcome = evaluate_forward_outcome(source, decision, 1)
        self.assertEqual(outcome.per_symbol[0].status, "UNAVAILABLE")
        self.assertEqual(outcome.per_symbol[0].unavailable_reason, END_PRICE_UNAVAILABLE)
        source = evidence(omit=(("AAAUSDT", decision + MINUTE),),
                          symbols=("AAAUSDT",))
        outcome = evaluate_forward_outcome(source, decision, 5)
        self.assertEqual(outcome.per_symbol[0].realized_volatility_unavailable_reason,
                         INCOMPLETE_FUTURE_MINUTE_PATH)

    def test_late_day_event_uses_separately_available_next_day_label_tail(self):
        decision = DAY - MINUTE
        source = evidence(start=decision, end=decision + DAY + 60 * MINUTE,
                         symbols=("AAAUSDT",))

        outcome = evaluate_forward_outcome(source, decision, 60)

        row = outcome.per_symbol[0]
        self.assertEqual(row.status, "AVAILABLE")
        self.assertEqual(row.end_candle_open_time_ms, decision + 59 * MINUTE)
        self.assertGreater(row.end_candle_open_time_ms, DAY)

    def test_continuous_utc_grid_has_1896_nonoverlapping_decisions(self):
        start, end = 60 * MINUTE, 60 * MINUTE + DAY
        self.assertEqual(tuple(len(continuous_decision_times(start, end, h))
                               for h in (1, 5, 15, 30, 60)),
                         (1440, 288, 96, 48, 24))
        grids = evaluate_continuous_grids(evidence(), start, end)
        self.assertEqual(sum(len(rows) for _, rows in grids), 1896)

    def test_event_overlap_is_horizon_specific_and_retains_events(self):
        source = evidence(symbols=("AAAUSDT",))
        start = 60 * MINUTE
        rows = evaluate_event_outcomes(
            source, (start, start + 5 * MINUTE, start + 6 * MINUTE), 5)
        self.assertEqual(len(rows), 3)
        self.assertEqual(tuple(row["confirmatory_independent"] for row in rows),
                         (True, True, False))

    def test_state_path_censoring_and_pelt_guard(self):
        start, end = 60 * MINUTE, 60 * MINUTE + 10 * MINUTE
        result = evaluate_v1_state_path({}, end - MINUTE, 5, start, end)
        self.assertEqual(result["unavailable_reason"],
                         STATE_PATH_OUTSIDE_EVALUATION_INTERVAL)
        path = {boundary: "BROAD_RISE"
                for boundary in range(start, start + MINUTE + 5_000, 5_000)}
        no_reversal = evaluate_v1_state_path(path, start, 1, start, end)
        self.assertEqual(no_reversal["reversal_observation_status"],
                         "CENSORED_NO_REVERSAL")
        self.assertFalse(no_reversal["reversed_within_horizon"])
        incomplete = dict(path)
        incomplete[start + 5_000] = None
        unknown_reversal = evaluate_v1_state_path(incomplete, start, 1, start, end)
        self.assertEqual(unknown_reversal["reversal_observation_status"], "UNAVAILABLE")
        self.assertIsNone(unknown_reversal["reversed_within_horizon"])
        with self.assertRaises(ValueError):
            reject_retrospective_forward_labels("RETROSPECTIVE")
        reject_retrospective_forward_labels("CONTINUOUS")


if __name__ == "__main__":
    unittest.main()
