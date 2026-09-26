"""Deterministic fixtures for the pure #71 calculator. Run with unittest."""

from dataclasses import replace
from decimal import Decimal
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_analysis.movement import MovementBucket, MovementReadiness
from market_analysis.movement_metrics import (
    HistoricalWindowInput, MarketMovementConfig, MarketMovementInput,
    MarketMovementSymbolInput, MarketUniverseInput, calculate_market_movement,
)


BOUNDARY = 1_800_000
COVERAGE = 3 * 24 * 60 * 60 * 1000
HISTORY = (-0.02, -0.01, 0.01, 0.02)


def bucket(symbol, time_ms, price, volume=Decimal("1")):
    return MovementBucket(
        boundary_time_ms=time_ms, source_state="LIVE", price=Decimal(str(price)),
        last_real_price=Decimal(str(price)), base_volume=Decimal("1"),
        quote_volume=volume, trade_count=1, last_real_trade_time_ms=time_ms,
        last_real_event_time_ms=time_ms, last_received_at_ms=time_ms,
        carried_forward=False, provider="binance-usdm",
        instrument_id=f"binance-usdm:{symbol}", price_type="trade",
    )


def readiness(symbol, window, *, start_price="100", middle_price="100", end_price="101",
              state="ready", reason=None, volume=Decimal("1")):
    start = BOUNDARY - 2 * window * 60_000
    middle = BOUNDARY - window * 60_000
    history = tuple(bucket(symbol, time_ms,
                           start_price if time_ms == start else end_price if time_ms == BOUNDARY else middle_price,
                           Decimal("999") if time_ms == middle else volume)
                    for time_ms in range(start, BOUNDARY + 1, 5_000))
    return MovementReadiness(
        state=state, reason=reason, boundary_time_ms=BOUNDARY,
        window_minutes=window, collector_state="LIVE", required_bucket_count=len(history),
        history=history, endpoint=history[-1], last_real_trade_age_ms=0,
    )


def symbol_input(symbol, *, current="101", previous="100", earliest="100",
                 historical_returns=HISTORY, coverage=COVERAGE, volume=Decimal("1"),
                 compatible=True, states=None, prior_volumes=None):
    states = states or {}
    readiness_by_window = {}
    historical_by_window = {}
    for window in (1, 5, 15):
        state, reason = states.get(window, ("ready", None))
        readiness_by_window[window] = readiness(
            symbol, window, start_price=earliest, middle_price=previous,
            end_price=current, state=state, reason=reason, volume=volume,
        )
        historical_by_window[window] = HistoricalWindowInput(
            historical_returns, coverage,
            tuple(prior_volumes if prior_volumes is not None else [Decimal("12")] * 20),
        )
    return MarketMovementSymbolInput(symbol, f"binance-usdm:{symbol}", compatible,
                                     readiness_by_window, historical_by_window)


def request(inputs, *, config=None, universe=None):
    symbols = tuple(inputs) if universe is None else tuple(universe)
    return MarketMovementInput(BOUNDARY, MarketUniverseInput("watched", "v4", symbols),
                               inputs, config or MarketMovementConfig())


def evaluate(inputs, *, config=None, universe=None, window=1):
    return calculate_market_movement(request(inputs, config=config, universe=universe)).windows[window]


class ExactWindowTests(unittest.TestCase):
    def test_exact_returns_velocity_acceleration_for_every_window(self):
        inputs = {f"S{i}": symbol_input(f"S{i}", current="121", previous="110") for i in range(5)}
        evaluation = calculate_market_movement(request(inputs))
        for window in (1, 5, 15):
            item = evaluation.windows[window].symbols[0]
            expected = math.log(1.1)
            self.assertAlmostEqual(item.current_return.value, expected)
            self.assertAlmostEqual(item.previous_return.value, expected)
            self.assertAlmostEqual(item.velocity.value, expected / (window * 60))
            self.assertAlmostEqual(item.previous_velocity.value, expected / (window * 60))
            self.assertAlmostEqual(item.acceleration.value, 0)
            self.assertEqual(evaluation.windows[window].breadth.material_rising.value.count, 5)

    def test_acceleration_uses_adjacent_previous_window(self):
        inputs = {f"S{i}": symbol_input(f"S{i}", current="120", previous="110") for i in range(5)}
        item = evaluate(inputs).symbols[0]
        expected = (math.log(120 / 110) - math.log(110 / 100)) / (60 * 60)
        self.assertAlmostEqual(item.acceleration.value, expected)

    def test_missing_exact_endpoint_is_excluded(self):
        item = symbol_input("A")
        ready = item.readiness[1]
        missing = replace(ready, history=ready.history[:-1])
        item = replace(item, readiness={**item.readiness, 1: missing})
        result = evaluate({"A": item})
        self.assertEqual(result.symbols[0].exclusion_reasons, ("MISSING_EXACT_BOUNDARY",))

    def test_invalid_endpoint_price_is_excluded(self):
        item = symbol_input("A")
        ready = item.readiness[1]
        bad_endpoint = replace(ready.history[-1], price=Decimal("0"))
        bad_ready = replace(ready, history=ready.history[:-1] + (bad_endpoint,), endpoint=bad_endpoint)
        item = replace(item, readiness={**item.readiness, 1: bad_ready})
        result = evaluate({"A": item})
        self.assertEqual(result.symbols[0].exclusion_reasons, ("INVALID_ENDPOINT_PRICE",))

    def test_current_notional_excludes_left_boundary(self):
        inputs = {f"S{i}": symbol_input(f"S{i}") for i in range(5)}
        item = evaluate(inputs).symbols[0]
        self.assertEqual(item.current_notional_volume.value, Decimal("12"))
        self.assertEqual(item.rvol.value, 1)


class NormalizationTests(unittest.TestCase):
    def test_own_history_and_raw_return_sign(self):
        quiet = symbol_input("QUIET", current="101", historical_returns=(-0.002, -0.001, 0.001, 0.002))
        volatile = symbol_input("VOLATILE", current="101", historical_returns=(-0.2, -0.1, 0.1, 0.2))
        negative_z = symbol_input("NEGATIVE_Z", current="101", historical_returns=(0.02, 0.03, 0.04, 0.05))
        inputs = {item.symbol: item for item in (quiet, volatile, negative_z)}
        result = evaluate(inputs)
        by_symbol = {item.symbol: item for item in result.symbols}
        self.assertGreater(by_symbol["QUIET"].normalized_z.value,
                           by_symbol["VOLATILE"].normalized_z.value)
        self.assertLess(by_symbol["NEGATIVE_Z"].normalized_z.value, 0)
        self.assertEqual(by_symbol["NEGATIVE_Z"].direction.value, "RISING")

    def test_zero_return_never_becomes_rising_or_falling(self):
        item = evaluate({"A": symbol_input("A", current="100",
                                            historical_returns=(0.02, 0.03, 0.04, 0.05))}).symbols[0]
        self.assertEqual(item.direction.reason, "DIRECTION_UNDEFINED_FOR_ZERO_RETURN")
        self.assertFalse(item.material_rising)
        self.assertFalse(item.material_falling)

    def test_zero_mad_and_insufficient_coverage(self):
        inputs = {
            "ZERO": symbol_input("ZERO", historical_returns=(0.01,) * 4),
            "SHORT": symbol_input("SHORT", coverage=COVERAGE - 1),
        }
        result = evaluate(inputs)
        by_symbol = {item.symbol: item for item in result.symbols}
        self.assertEqual(by_symbol["ZERO"].exclusion_reasons, ("NORMALIZATION_MAD_UNAVAILABLE",))
        self.assertEqual(by_symbol["SHORT"].exclusion_reasons, ("INSUFFICIENT_NORMALIZATION_HISTORY",))
        self.assertTrue(by_symbol["ZERO"].current_return.available)

    def test_invalid_historical_float_is_excluded(self):
        item = evaluate({"A": symbol_input("A", historical_returns=(0.0, float("nan")))}).symbols[0]
        self.assertEqual(item.exclusion_reasons, ("INVALID_NORMALIZATION_HISTORY",))


class EligibilityBreadthTests(unittest.TestCase):
    def test_source_reasons_remain_distinct(self):
        specs = {
            "RECOVERING": ("unavailable", "collector_recovering"),
            "STALE": ("stale", "collector_stale"),
            "UNAVAILABLE": ("unavailable", "collector_unavailable"),
            "WARMING": ("warming", "insufficient_exact_live_history"),
            "TRADE_STALE": ("stale", "last_real_trade_expired"),
        }
        inputs = {symbol: symbol_input(symbol, states={1: state}) for symbol, state in specs.items()}
        inputs["UNSUPPORTED"] = symbol_input("UNSUPPORTED", compatible=False)
        result = evaluate(inputs, universe=(*inputs, "MISSING"))
        reasons = {item.symbol: item.exclusion_reasons for item in result.symbols}
        self.assertIn("SOURCE_RECOVERING", reasons["RECOVERING"])
        self.assertIn("SOURCE_STALE", reasons["STALE"])
        self.assertIn("SOURCE_UNAVAILABLE", reasons["UNAVAILABLE"])
        self.assertIn("WARMING_INSUFFICIENT_LIVE_HISTORY", reasons["WARMING"])
        self.assertIn("STALE_LAST_TRADE", reasons["TRADE_STALE"])
        self.assertIn("UNSUPPORTED_INSTRUMENT", reasons["UNSUPPORTED"])
        self.assertEqual(reasons["MISSING"], ("MISSING_SYMBOL_INPUT",))

    def test_market_gate_thresholds_and_evidence(self):
        for configured, included in ((10, 5), (10, 6), (8, 4), (8, 5),
                                     (5, 4), (5, 5), (4, 4)):
            with self.subTest(configured=configured, included=included):
                names = tuple(f"S{i}" for i in range(configured))
                inputs = {name: symbol_input(name) for name in names[:included]}
                result = evaluate(inputs, universe=names)
                expected = (configured, included) in ((10, 6), (8, 5), (5, 5))
                self.assertEqual(result.market_wide_eligible, expected)
                self.assertEqual(result.eligible_count, included)
                self.assertEqual(result.eligible_fraction, included / configured)
                self.assertEqual(len(result.symbols), configured)
                self.assertEqual(result.breadth.available, expected)
                if not expected:
                    self.assertEqual(result.aggregates.median_normalized_movement.reason,
                                     "MARKET_UNIVERSE_INELIGIBLE")

    def test_material_breadth_is_distinct_from_direction(self):
        inputs = {f"S{i}": symbol_input(f"S{i}", current="101.2") for i in range(5)}
        result = evaluate(inputs)
        self.assertEqual(result.breadth.rising.value.count, 5)
        self.assertEqual(result.breadth.material_rising.value.count, 0)
        self.assertEqual(result.breadth.denominator, 5)


class AggregateTests(unittest.TestCase):
    def test_median_trim_and_dispersion(self):
        returns = [-0.09, -0.07, -0.05, -0.03, -0.01, 0.01, 0.03, 0.05, 0.07, 0.50]
        inputs = {f"S{i}": symbol_input(f"S{i}", current=str(100 * math.exp(value)))
                  for i, value in enumerate(returns)}
        result = evaluate(inputs)
        z = sorted(item.normalized_z.value for item in result.symbols)
        raw = sorted(item.current_return.value for item in result.symbols)
        self.assertAlmostEqual(result.aggregates.median_normalized_movement.value,
                               (z[4] + z[5]) / 2)
        self.assertAlmostEqual(result.aggregates.median_raw_return.value,
                               (raw[4] + raw[5]) / 2)
        self.assertAlmostEqual(result.aggregates.trimmed_mean_normalized_movement.value,
                               sum(z[1:-1]) / 8)
        center = (z[4] + z[5]) / 2
        deviations = sorted(abs(value - center) for value in z)
        self.assertAlmostEqual(result.aggregates.dispersion_mad_normalized_movement.value,
                               (deviations[4] + deviations[5]) / 2)

    def test_too_few_values_to_trim(self):
        result = evaluate({f"S{i}": symbol_input(f"S{i}") for i in range(5)})
        self.assertEqual(result.aggregates.trimmed_mean_normalized_movement.reason,
                         "TOO_FEW_VALUES_TO_TRIM")


class VolumeOutlierTests(unittest.TestCase):
    def test_rvol_uses_last_n_comparable_windows(self):
        prior = [Decimal("100")] + [Decimal("6")] * 20
        inputs = {f"S{i}": symbol_input(f"S{i}", prior_volumes=prior) for i in range(5)}
        result = evaluate(inputs)
        self.assertEqual(result.symbols[0].rvol.value, 2)

    def test_rvol_unavailable_reasons(self):
        short = symbol_input("SHORT", prior_volumes=[Decimal("6")] * 19)
        zero = symbol_input("ZERO", prior_volumes=[Decimal("0")] * 20)
        result = evaluate({"SHORT": short, "ZERO": zero})
        by_symbol = {item.symbol: item for item in result.symbols}
        self.assertEqual(by_symbol["SHORT"].rvol.reason, "RVOL_HISTORY_UNAVAILABLE")
        self.assertEqual(by_symbol["ZERO"].rvol.reason, "RVOL_DENOMINATOR_INVALID")

    def test_capped_weights_and_impossible_allocation(self):
        inputs = {f"S{i}": symbol_input(f"S{i}", volume=Decimal("10000") if i == 0 else Decimal("1"))
                  for i in range(5)}
        result = evaluate(inputs)
        weights = result.aggregates.liquidity_weights.value
        self.assertAlmostEqual(sum(weight for _, weight in weights), 1)
        self.assertLessEqual(max(weight for _, weight in weights), 0.25)
        zero = {f"S{i}": symbol_input(f"S{i}", volume=Decimal("0") if i == 4 else Decimal("1"))
                for i in range(5)}
        feasible = evaluate(zero)
        self.assertTrue(feasible.aggregates.liquidity_weights.available)
        zero["S3"] = symbol_input("S3", volume=Decimal("0"))
        impossible = evaluate(zero)
        self.assertEqual(impossible.aggregates.liquidity_weights.reason,
                         "LIQUIDITY_WEIGHT_CAP_UNSATISFIABLE")

    def test_extreme_outlier_and_zero_cross_mad(self):
        inputs = {f"S{i}": symbol_input(f"S{i}", current=str(100 * math.exp(0.01 * i)))
                  for i in range(4)}
        inputs["EXTREME"] = symbol_input("EXTREME", current=str(100 * math.exp(0.5)))
        result = evaluate(inputs)
        extreme = next(item for item in result.symbols if item.symbol == "EXTREME")
        self.assertTrue(extreme.outlier_candidate)
        self.assertTrue(extreme.cross_sectional_z.available)
        identical = evaluate({f"S{i}": symbol_input(f"S{i}") for i in range(5)})
        self.assertTrue(all(item.cross_sectional_z.reason == "CROSS_SECTIONAL_MAD_UNAVAILABLE"
                            and not item.outlier_candidate for item in identical.symbols))


class ContractTests(unittest.TestCase):
    def test_same_explicit_input_is_deterministic(self):
        inputs = {f"S{i}": symbol_input(f"S{i}") for i in range(5)}
        fixed = request(inputs)
        self.assertEqual(calculate_market_movement(fixed), calculate_market_movement(fixed))

    def test_invalid_config_is_rejected(self):
        invalid = (
            {"historical_lookback_ms": 0}, {"minimum_historical_coverage_ms": 8 * 24 * 60 * 60 * 1000},
            {"flat_z": float("nan")}, {"material_z": 0.1}, {"trim_fraction": 0},
            {"liquidity_weight_cap": 1.1}, {"rvol_comparison_windows": 0},
            {"minimum_eligible_fraction": 0}, {"minimum_eligible_count": 0},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                MarketMovementConfig(**change)


if __name__ == "__main__":
    unittest.main()
