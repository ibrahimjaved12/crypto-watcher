import math
import unittest

from pydantic import ValidationError

from market_analysis.api_models import SlowMovementRequest
from market_analysis.slow_movement import (
    DEFAULT_SLOW_CONFIG,
    SLOW_MOVEMENT_ALGORITHM_VERSION,
    SLOW_MOVEMENT_CONFIG_VERSION,
    config_hash,
    evaluate_slow_movement,
)

B = 1_760_000_400_000 - (1_760_000_400_000 % 900_000)  # synthetic boundary, a multiple of 15 minutes
N = 672


def wavy(drift_last_hour: float = 0.0, amplitude: float = 0.001, phase: int = 0) -> list:
    """Synthetic grid (index 0 = boundary): a deterministic wiggle so the reference scale is non-zero, plus an
    optional log move over the latest hour (grid steps 0..3 relative to step 4)."""
    log_prices = [amplitude * math.sin((k + phase) * 1.7) for k in range(N)]
    for k in range(4):
        log_prices[k] += drift_last_hour
    return [100 * math.exp(x) for x in log_prices]


def horizon(result, minutes):
    return next(h for h in result["horizons"] if h["horizonMin"] == minutes)


class SlowMovementTests(unittest.TestCase):
    def test_constant_series_is_unavailable(self):
        result = evaluate_slow_movement(B, [60], [("BTCUSDT", [100.0] * N)])
        row = horizon(result, 60)["symbols"][0]
        self.assertEqual(row["status"], "unavailable")  # s = 0
        self.assertEqual(horizon(result, 60)["included"], 0)

    def test_known_drift_gives_expected_return_and_z(self):
        grid = wavy(drift_last_hour=0.02)
        result = evaluate_slow_movement(B, [60], [("BTCUSDT", grid)])
        row = horizon(result, 60)["symbols"][0]
        self.assertEqual(row["status"], "ok")
        r0 = math.log(grid[0] / grid[4])
        self.assertAlmostEqual(row["returnPct"], (math.exp(r0) - 1) * 100, places=12)
        reference = [math.log(grid[j] / grid[j + 4]) for j in range(4, 4 + 576)]
        m = sorted(reference)[287:289]
        m = (m[0] + m[1]) / 2
        dev = sorted(abs(r - m) for r in reference)
        s = 1.4826 * (dev[287] + dev[288]) / 2
        self.assertAlmostEqual(row["z"], (r0 - m) / s, places=9)
        self.assertGreater(row["z"], 2)

    def test_missing_endpoint_excludes_symbol(self):
        grid = wavy()
        grid[0] = None
        row = horizon(evaluate_slow_movement(B, [240], [("BTCUSDT", grid)]), 240)["symbols"][0]
        self.assertEqual(row["status"], "collecting_history")
        self.assertIsNone(row["z"])

    def test_short_history_is_collecting(self):
        grid = wavy()
        for k in range(4 + 287 + 4, N):  # leaves 287 valid reference samples for h = 4
            grid[k] = None
        row = horizon(evaluate_slow_movement(B, [60], [("BTCUSDT", grid)]), 60)["symbols"][0]
        self.assertEqual(row["status"], "collecting_history")
        self.assertEqual(row["referenceSamples"], 287)

    def test_coordination_needs_share_median_and_five_pairs(self):
        names = ["A1USDT", "B2USDT", "C3USDT", "D4USDT", "E5USDT"]
        all_up = [(name, wavy(0.02, phase=i)) for i, name in enumerate(names)]
        self.assertEqual(horizon(evaluate_slow_movement(B, [60], all_up), 60)["coordinated"], "rise")
        all_down = [(name, wavy(-0.02, phase=i)) for i, name in enumerate(names)]
        self.assertEqual(horizon(evaluate_slow_movement(B, [60], all_down), 60)["coordinated"], "fall")
        three = [(name, wavy(0.02 if i < 3 else 0.0, phase=i)) for i, name in enumerate(names)]
        agg = horizon(evaluate_slow_movement(B, [60], three), 60)
        self.assertEqual(agg["up"], 3)
        self.assertIsNone(agg["coordinated"])
        four = horizon(evaluate_slow_movement(B, [60], all_up[:4]), 60)
        self.assertEqual(four["up"], 4)
        self.assertIsNone(four["coordinated"])

    def test_symbol_order_does_not_change_aggregates(self):
        names = ["A1USDT", "B2USDT", "C3USDT", "D4USDT", "E5USDT", "F6USDT"]
        items = [(name, wavy(0.004 * (i - 2), phase=i)) for i, name in enumerate(names)]
        forward = evaluate_slow_movement(B, [60, 240, 720], items)
        backward = evaluate_slow_movement(B, [720, 60, 240], list(reversed(items)))
        for minutes in (60, 240, 720):
            a, b = horizon(forward, minutes), horizon(backward, minutes)
            for key in ("included", "medianReturnPct", "medianZ", "up", "down", "neutral", "coordinated"):
                self.assertEqual(a[key], b[key])

    def test_identity_and_request_validation(self):
        result = evaluate_slow_movement(B, [60], [("BTCUSDT", wavy())])
        self.assertEqual(result["algorithmVersion"], SLOW_MOVEMENT_ALGORITHM_VERSION)
        self.assertEqual(result["configVersion"], SLOW_MOVEMENT_CONFIG_VERSION)
        self.assertEqual(result["configHash"], config_hash(DEFAULT_SLOW_CONFIG))
        self.assertEqual(len(result["configHash"]), 16)
        good = {"boundaryMs": B, "horizonsMin": [60], "symbols": [{"symbol": "BTCUSDT", "grid": [1.0] * N}]}
        SlowMovementRequest.model_validate(good)
        for bad in (
            {**good, "boundaryMs": B + 60_000},
            {**good, "horizonsMin": [30]},
            {**good, "symbols": [{"symbol": "BTCUSDT", "grid": [1.0] * (N - 1)}]},
            {**good, "symbols": [{"symbol": "BTCUSDT", "grid": [0.0] * N}]},
            {**good, "symbols": [{"symbol": f"S{i}USDT", "grid": [1.0] * N} for i in range(21)]},
        ):
            with self.assertRaises(ValidationError):
                SlowMovementRequest.model_validate(bad)


if __name__ == "__main__":
    unittest.main()
