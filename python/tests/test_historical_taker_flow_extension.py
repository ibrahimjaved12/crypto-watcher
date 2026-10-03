"""Focused deterministic fixtures for EXP-75-12 side-aware archive evidence."""

from dataclasses import replace
from decimal import Decimal
import unittest

from market_analysis.historical_taker_flow_evidence import (
    HistoricalTakerFlowEvidenceBuilder,
    TAKER_FLOW_ALGORITHM_VERSION,
    TAKER_FLOW_CONFIG_VERSION,
)
from market_analysis.historical_taker_flow_extension import _flow_symbol_output
from market_analysis.movement import BUCKET_INTERVAL_MS


BASE = 1_800_000_000_000
ENGINE_START = BASE - 15 * 60_000
DATASET_SHA = "a" * 64


def _builder(*, start=ENGINE_START, end=BASE, grace=2_000,
             symbols=("BTCUSDT", "ETHUSDT"), dataset_sha=DATASET_SHA):
    return HistoricalTakerFlowEvidenceBuilder(
        dataset_id="fixture-dataset", dataset_version="v1",
        dataset_content_sha256=dataset_sha, configured_symbols=symbols,
        engine_start_boundary_time_ms=start, output_end_boundary_time_ms=end,
        finalization_grace_ms=grace,
    )


class HistoricalTakerFlowExtensionTests(unittest.TestCase):
    def test_side_mapping_exact_decimal_and_imbalance_bounds(self):
        builder = _builder()
        self.assertTrue(builder.add_trade(
            "BTCUSDT", BASE, BASE, Decimal("0.1"), Decimal("0.3"), False))
        self.assertTrue(builder.add_trade(
            "BTCUSDT", BASE, BASE, Decimal("0.2"), Decimal("0.3"), True))
        evidence = builder.build()
        sums, reason = evidence.query_window("BTCUSDT", BASE, 1)
        output = _flow_symbol_output("BTCUSDT", 1, sums, reason, None)
        self.assertEqual((output.buy_quote_notional,
                          output.sell_quote_notional,
                          output.gross_quote_notional,
                          output.signed_net_quote_notional),
                         (Decimal("0.03"), Decimal("0.06"),
                          Decimal("0.09"), Decimal("-0.03")))
        self.assertEqual(output.buy_aggtrade_count, 1)
        self.assertEqual(output.sell_aggtrade_count, 1)
        self.assertEqual(output.total_aggtrade_count, 2)
        self.assertTrue(Decimal(-1) <= output.imbalance <= Decimal(1))
        self.assertEqual(TAKER_FLOW_ALGORITHM_VERSION,
                         "taker-buy-sell-imbalance-v2-exact-sign")
        self.assertIn("1m-5m-15m", TAKER_FLOW_CONFIG_VERSION)

    def test_balanced_activity_and_empty_window_have_distinct_statuses(self):
        builder = _builder()
        builder.add_trade("BTCUSDT", BASE, BASE, Decimal("2"), Decimal("3"), False)
        builder.add_trade("BTCUSDT", BASE, BASE, Decimal("2"), Decimal("3"), True)
        evidence = builder.build()
        balanced, reason = evidence.query_window("BTCUSDT", BASE, 1)
        active = _flow_symbol_output("BTCUSDT", 1, balanced, reason, None)
        empty, reason = evidence.query_window("ETHUSDT", BASE, 1)
        no_observation = _flow_symbol_output("ETHUSDT", 1, empty, reason, None)
        self.assertEqual((active.status, active.imbalance), ("ACTIVE", Decimal(0)))
        self.assertEqual(no_observation.status, "NO_OBSERVED_AGGTRADES")
        self.assertIsNone(no_observation.imbalance)
        self.assertEqual(no_observation.total_aggtrade_count, 0)
        outside, reason = evidence.query_window("BTCUSDT", BASE + 5_000, 1)
        unavailable = _flow_symbol_output("BTCUSDT", 1, outside, reason, None)
        self.assertEqual(unavailable.status, "FLOW_EVIDENCE_UNAVAILABLE")
        self.assertEqual(unavailable.reason, "FLOW_EVIDENCE_UNAVAILABLE")

    def test_right_closed_window_excludes_lower_edge_and_future_trade(self):
        builder = _builder(end=BASE + BUCKET_INTERVAL_MS)
        builder.add_trade("BTCUSDT", BASE - 60_000, BASE - 60_000,
                          Decimal("10"), Decimal("1"), False)
        builder.add_trade("BTCUSDT", BASE - 59_999, BASE - 59_999,
                          Decimal("20"), Decimal("1"), False)
        builder.add_trade("BTCUSDT", BASE, BASE,
                          Decimal("30"), Decimal("1"), False)
        builder.add_trade("BTCUSDT", BASE + 1, BASE + 1,
                          Decimal("40"), Decimal("1"), False)
        evidence = builder.build()
        sums, reason = evidence.query_window("BTCUSDT", BASE, 1)
        self.assertIsNone(reason)
        self.assertEqual(sums.buy_quote_notional, Decimal("50"))
        self.assertEqual(sums.buy_aggtrade_count, 2)
        self.assertEqual(sums.sell_aggtrade_count, 0)

    def test_grace_is_inclusive_late_rows_are_permanent_and_finalized_output_is_fixed(self):
        builder = _builder(start=BASE, end=BASE + BUCKET_INTERVAL_MS)
        self.assertTrue(builder.add_trade(
            "BTCUSDT", BASE, BASE + 2_000,
            Decimal("5"), Decimal("2"), False))
        self.assertFalse(builder.add_trade(
            "BTCUSDT", BASE, BASE + 2_001,
            Decimal("100"), Decimal("2"), False))
        before = builder.finalize_bucket(BASE)
        self.assertEqual(before[0].buy_quote_notional, Decimal("10"))
        self.assertFalse(builder.add_trade(
            "BTCUSDT", BASE, BASE,
            Decimal("999"), Decimal("1"), True))
        self.assertEqual(builder._finalized_outputs[0], (BASE, before))
        with self.assertRaisesRegex(ValueError, "finalize once"):
            builder.finalize_bucket(BASE)
        after = builder.finalize_bucket(BASE + BUCKET_INTERVAL_MS)
        self.assertEqual(after[0].buy_quote_notional, Decimal(0))
        self.assertEqual(after[0].sell_quote_notional, Decimal(0))

    def test_prefix_queries_match_brute_force_bucket_sums(self):
        builder = _builder()
        samples = (
            (BASE - 120_000, "1.25", False),
            (BASE - 60_000, "2.50", True),
            (BASE - 5_000, "3.75", False),
            (BASE, "4.00", True),
        )
        for timestamp, price, maker in samples:
            builder.add_trade("BTCUSDT", timestamp, timestamp,
                              Decimal(price), Decimal("2"), maker)
        evidence = builder.build()
        series = evidence.symbol_buckets[0]
        for start, end in ((BASE - 120_000, BASE),
                           (BASE - 60_000, BASE - 5_000),
                           (BASE - 15_000, BASE)):
            indexed = series.sum_boundaries(start, end)
            selected = [item for item in series.buckets
                        if start < item.boundary_time_ms <= end]
            self.assertEqual(indexed.buy_quote_notional,
                             sum((item.buy_quote_notional for item in selected), Decimal(0)))
            self.assertEqual(indexed.sell_quote_notional,
                             sum((item.sell_quote_notional for item in selected), Decimal(0)))
            self.assertEqual(indexed.buy_aggtrade_count,
                             sum(item.buy_aggtrade_count for item in selected))
            self.assertEqual(indexed.sell_aggtrade_count,
                             sum(item.sell_aggtrade_count for item in selected))

    def test_digest_binds_dataset_order_range_grace_and_bucket_values(self):
        first = _builder()
        first.add_trade("BTCUSDT", BASE, BASE, Decimal("10"), Decimal("1"), False)
        evidence = first.build()
        changed_grace = _builder(grace=2_001)
        changed_grace.add_trade("BTCUSDT", BASE, BASE, Decimal("10"), Decimal("1"), False)
        changed_content = _builder(dataset_sha="b" * 64)
        changed_content.add_trade("BTCUSDT", BASE, BASE, Decimal("10"), Decimal("1"), False)
        changed_values = _builder()
        changed_values.add_trade("BTCUSDT", BASE, BASE, Decimal("11"), Decimal("1"), False)
        self.assertNotEqual(evidence.evidence_sha256, changed_grace.build().evidence_sha256)
        self.assertNotEqual(evidence.evidence_sha256, changed_content.build().evidence_sha256)
        self.assertNotEqual(evidence.evidence_sha256, changed_values.build().evidence_sha256)
        self.assertTrue(evidence.matches_dataset(
            "fixture-dataset", "v1", DATASET_SHA, ("BTCUSDT", "ETHUSDT")))
        self.assertFalse(evidence.matches_dataset(
            "other-dataset", "v1", DATASET_SHA, ("BTCUSDT", "ETHUSDT")))
        self.assertFalse(evidence.matches_dataset(
            "fixture-dataset", "v1", DATASET_SHA, ("ETHUSDT", "BTCUSDT")))
        with self.assertRaises(ValueError):
            replace(evidence, configured_symbols=("ETHUSDT", "BTCUSDT"))


if __name__ == "__main__":
    unittest.main()
