"""Small hand-computed doji/aggregation cases; no data downloads or heavy studies."""
from array import array
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch

from market_analysis.benchmark.bars import BarSeries, COMPROMISED_FLAGS, MISSING
from market_analysis.benchmark.candles import CandleSeries, build_candles
from market_analysis.benchmark.ta_strategies import first_signal_index, strategy_config, strategy_sides
from market_analysis.patterns.doji import DOJI_PARAMS, DojiPattern, read_doji, score_tier
from market_analysis.patterns.features import CandleFeatureHistory, CandleFeatures, candle_features


def worked_features(**changes):
    # high=110, low=100, open=104.75, close=105.25: body .5,
    # both wicks 4.75. Prior ATR=10; prior high=112.5 -> distance .25.
    features = CandleFeatures(
        index=60, high=110.0, low=100.0, range=10.0, body=0.5,
        upper=4.75, lower=4.75, body_ratio=0.05, upper_ratio=0.475, lower_ratio=0.475,
        atr_prev=10.0, range_atr=1.0, rel_volume=1.25, adx_prev=30.0, trend_dir=1,
        prior20_high=112.5, prior20_low=97.5,
    )
    return replace(features, **changes)


def feature_options():
    p = DOJI_PARAMS
    return dict(atr_period=p.atr_period, adx_period=p.adx_period,
                ema_fast_period=p.ema_fast_period, ema_slow_period=p.ema_slow_period,
                lookback=p.lookback)


def trending_candles(count=65, minutes=15, direction=1):
    scale = 10 ** 8
    close = [(100 + direction * i) * scale for i in range(count)]
    return CandleSeries(
        "BTCUSDT", minutes, 0, open=list(close),
        high=[value + 2 * scale for value in close],
        low=[value - 2 * scale for value in close], close=close,
        valid=[True] * count, volume=[100 * scale] * count,
        taker_buy_volume=[40 * scale] * count,
    )


def minute_bars(count):
    prices = {name: array("q", [100] * count)
              for name in ("open", "high", "low", "close", "mark_open", "mark_high", "mark_low", "mark_close")}
    return BarSeries("BTCUSDT", 0, count, volume=array("q", range(1, count + 1)),
                     taker_buy_volume=array("q", [2] * count), trades=array("q", [1] * count),
                     flags=array("H", [0] * count), **prices)


class DojiReadingTests(unittest.TestCase):
    def test_inclusive_gate_boundaries(self):
        cases = (
            ("body boundary", dict(body=1.0, body_ratio=0.10, upper=4.5, lower=4.5,
                                   upper_ratio=0.45, lower_ratio=0.45), "ok"),
            ("size boundary", dict(atr_prev=20.0, range_atr=0.5), "ok"),
            ("both boundaries", dict(body_ratio=0.10, atr_prev=20.0, range_atr=0.5), "ok"),
            ("body above", dict(body_ratio=0.100001), "not_pattern"),
            ("size below", dict(range_atr=0.499999), "not_pattern"),
            ("zero range", dict(range=0.0, body_ratio=None, range_atr=0.0), "insufficient"),
            ("zero ATR", dict(atr_prev=0.0, range_atr=None), "not_pattern"),
        )
        for label, changes, status in cases:
            with self.subTest(label=label):
                reading = read_doji(worked_features(**changes))
                self.assertEqual(reading.status, status)
                if status != "ok":
                    self.assertIsNone(reading.score)
                    self.assertIsNone(reading.tier)
                    self.assertEqual(reading.components, ())

    def test_subtypes_and_exclusive_precedence(self):
        # All cases have range 10, body 1, and wick ratios summing to .9.
        # The opposite-wick .10 and long-wick .30 boundaries are inclusive.
        # Under the frozen thresholds, dragonfly/gravestone cannot overlap
        # long_legged: the <=.10 opposite wick excludes its >=.30 condition.
        cases = (
            (0.0, 0.9, "dragonfly"),
            (0.1, 0.8, "dragonfly"),
            (0.9, 0.0, "gravestone"),
            (0.8, 0.1, "gravestone"),
            (0.3, 0.6, "long_legged"),
            (0.6, 0.3, "long_legged"),
            (0.7, 0.2, "standard"),
            (0.2, 0.7, "standard"),
        )
        for upper, lower, subtype in cases:
            with self.subTest(upper=upper, lower=lower):
                reading = read_doji(worked_features(body=1.0, body_ratio=0.1,
                    upper=10 * upper, lower=10 * lower, upper_ratio=upper, lower_ratio=lower))
                self.assertEqual(reading.status, "ok")
                self.assertEqual(reading.subtype, subtype)
                self.assertEqual(reading.reasons, ("gate_passed", f"subtype:{subtype}"))

    def test_fully_worked_score_and_every_component(self):
        reading = read_doji(worked_features())
        self.assertEqual((reading.pattern_id, reading.version), ("doji", "doji-v1"))
        self.assertEqual((reading.subtype, reading.tier), ("long_legged", "strong"))
        # 100 * (.20*.50 + .25*.50 + .20*.50 + .20*.50 + .15*.75)
        self.assertAlmostEqual(reading.score, 53.75)
        expected = (
            ("tightness", 0.05, 0.50, 0.20, 10.0),
            ("size", 1.0, 0.50, 0.25, 12.5),
            ("volume", 1.25, 0.50, 0.20, 10.0),
            ("trend", 30.0, 0.50, 0.20, 10.0),
            ("location", 0.25, 0.75, 0.15, 11.25),
        )
        self.assertEqual(len(reading.components), len(expected))
        for component, (name, raw, normalized, weight, contribution) in zip(reading.components, expected):
            self.assertEqual(component.name, name)
            for actual, target in ((component.raw, raw), (component.normalized, normalized),
                                   (component.weight, weight), (component.contribution, contribution)):
                self.assertAlmostEqual(actual, target)
        self.assertAlmostEqual(sum(component.contribution for component in reading.components), reading.score)

    def test_normalization_clips_and_location_directions(self):
        reading = read_doji(worked_features(body_ratio=0.0, range_atr=3.0,
                                           rel_volume=4.0, adx_prev=70.0, prior20_high=130.0))
        self.assertEqual([component.normalized for component in reading.components], [1, 1, 1, 1, 0])
        self.assertAlmostEqual(reading.score, 85.0)
        low = read_doji(worked_features(rel_volume=0.0, adx_prev=10.0))
        self.assertEqual([component.normalized for component in low.components][2:4], [0, 0])
        bearish = read_doji(worked_features(trend_dir=-1))
        self.assertAlmostEqual(bearish.components[-1].raw, 0.25)
        breakout = read_doji(worked_features(prior20_high=105.0))
        self.assertEqual((breakout.components[-1].raw, breakout.components[-1].normalized), (0.0, 1.0))
        neutral = read_doji(worked_features(trend_dir=0))
        self.assertEqual((neutral.components[-1].raw, neutral.components[-1].normalized), (None, 0.0))
        self.assertAlmostEqual(neutral.score, 42.5)

    def test_tier_edges(self):
        for score, tier in ((49.99, "weak"), (50, "strong"), (74.99, "strong"), (75, "very_strong")):
            with self.subTest(score=score):
                self.assertEqual(score_tier(score), tier)

    def test_unready_prior_inputs_never_produce_partial_scores(self):
        for field in ("atr_prev", "rel_volume", "adx_prev", "trend_dir", "prior20_high", "prior20_low"):
            with self.subTest(field=field):
                reading = read_doji(worked_features(**{field: None}))
                self.assertEqual(reading.status, "insufficient")
                self.assertIsNone(reading.score)
                self.assertEqual(reading.components, ())
        # Unready inputs take precedence even when the geometry is not a doji.
        self.assertEqual(read_doji(worked_features(body_ratio=0.5, adx_prev=None)).status, "insufficient")

    def test_definitions_and_records_are_frozen(self):
        for value, attribute in ((DOJI_PARAMS, "body_max"), (worked_features(), "range"),
                                 (read_doji(worked_features()), "score")):
            with self.subTest(attribute=attribute), self.assertRaises(FrozenInstanceError):
                setattr(value, attribute, 123)
        self.assertEqual(DojiPattern().read(worked_features()), read_doji(worked_features()))


class DojiFeatureTests(unittest.TestCase):
    def test_hand_computed_prior_helpers_and_volume(self):
        candles = trending_candles()
        volumes = list(candles.volume)
        volumes[60] = 250 * 10 ** 8
        features = candle_features(replace(candles, volume=volumes), 60, **feature_options())
        # Each prior true range is exactly 4; each upward DM is 1, downward DM
        # is 0: Wilder ATR stays 4, ADX stays 100; the EMA20 exceeds EMA50.
        self.assertEqual(features.atr_prev, 4.0)
        self.assertEqual(features.adx_prev, 100.0)
        self.assertEqual(features.trend_dir, 1)
        self.assertEqual(features.rel_volume, 2.5)  # prior 20 mean=100, excludes current 250
        self.assertEqual(features.range_atr, 1.0)
        self.assertEqual((features.prior20_high, features.prior20_low), (161.0, 138.0))
        self.assertTrue(features.ready)

    def test_lookahead_guard_for_single_and_batch_readings(self):
        candles = trending_candles()
        baseline = candle_features(candles, 60, **feature_options())
        changed = {}
        for name in ("open", "high", "low", "close", "volume", "taker_buy_volume"):
            column = list(getattr(candles, name))
            for i in range(61, len(candles)):
                column[i] *= 1000
            changed[name] = column
        future = replace(candles, **changed)
        self.assertEqual(candle_features(future, 60, **feature_options()), baseline)
        self.assertEqual(CandleFeatureHistory(future, **feature_options()).at(60), baseline)
        self.assertEqual(read_doji(baseline), read_doji(candle_features(future, 60, **feature_options())))

    def test_current_candle_cannot_change_prior_atr_adx_or_ema(self):
        candles = trending_candles()
        baseline = candle_features(candles, 60, **feature_options())
        high = list(candles.high)
        high[60] += 100 * 10 ** 8
        after = candle_features(replace(candles, high=high), 60, **feature_options())
        self.assertEqual((after.atr_prev, after.adx_prev, after.trend_dir),
                         (baseline.atr_prev, baseline.adx_prev, baseline.trend_dir))

    def test_warmup_gap_and_zero_volume_reference(self):
        candles = trending_candles()
        self.assertEqual(read_doji(candle_features(candles, 49, **feature_options())).status, "insufficient")
        self.assertEqual(read_doji(candle_features(candles, 50, **feature_options())).status, "ok")
        valid = list(candles.valid)
        valid[30] = False
        gapped = replace(candles, valid=valid)
        for index in (30, 60):
            self.assertEqual(read_doji(candle_features(gapped, index, **feature_options())).status, "insufficient")
        volumes = list(candles.volume)
        volumes[40:60] = [0] * 20
        self.assertEqual(read_doji(candle_features(replace(candles, volume=volumes), 60,
                                                  **feature_options())).status, "insufficient")

    def test_two_registered_variants_both_directions_and_timeframes(self):
        for minutes in (15, 60, 240):
            for direction in (-1, 1):
                with self.subTest(minutes=minutes, direction=direction):
                    candles = trending_candles(minutes=minutes, direction=direction)
                    for name, side in (("doji_continuation_v1", direction), ("doji_reversal_v1", -direction)):
                        sides = strategy_sides(name, candles)
                        self.assertEqual(sides[:50], [0] * 50)
                        self.assertEqual(sides[50:], [side] * (len(candles) - 50))
                        self.assertEqual(first_signal_index(name), 50)
                        self.assertEqual(strategy_config(name, minutes), {
                            "kind": "doji_continuation" if side == direction else "doji_reversal",
                            "timeframe_min": minutes, "pattern_id": "doji", "pattern_version": "doji-v1",
                            "status": "unvalidated",
                        })

    def test_strategy_adx_boundary_and_no_signal_conditions(self):
        # Isolate the strategy decision from indicator preparation with known
        # complete feature records; ineligible readings remain the zero side.
        candles = trending_candles(count=1)
        cases = (
            (dict(adx_prev=20.0, trend_dir=1), 1),
            (dict(adx_prev=20.0, trend_dir=-1), -1),
            (dict(adx_prev=19.99), 0),
            (dict(trend_dir=0), 0),
            (dict(adx_prev=None), 0),
            (dict(body_ratio=0.100001), 0),
            (dict(range_atr=0.499999), 0),
        )
        for changes, direction in cases:
            with self.subTest(changes=changes), patch(
                    "market_analysis.benchmark.ta_strategies.CandleFeatureHistory") as history:
                history.return_value.at.return_value = worked_features(**changes)
                self.assertEqual(strategy_sides("doji_continuation_v1", candles), [direction])
                self.assertEqual(strategy_sides("doji_reversal_v1", candles), [-direction])

    def test_existing_baselines_ignore_added_quantities(self):
        candles = trending_candles()
        price_only = replace(candles, volume=[], taker_buy_volume=[])
        for name in ("ema_cross_20_50", "ema_cross_50_200", "rsi_14_reversion", "macd_12_26_9",
                     "bollinger_20_2_reversion", "keltner_breakout_20_14_2"):
            with self.subTest(name=name):
                self.assertEqual(strategy_sides(name, candles), strategy_sides(name, price_only))
        self.assertEqual(strategy_sides("doji_continuation_v1", price_only), [0] * len(candles))


class CandleVolumeTests(unittest.TestCase):
    def test_sums_and_trailing_partial_visibility(self):
        for minutes in (15, 60, 240):
            with self.subTest(minutes=minutes):
                candles = build_candles(minute_bars(minutes + 5), minutes)
                self.assertEqual(len(candles), 1)  # five developing minutes are not a candle
                self.assertEqual(candles.end_ms, [minutes * 60_000])
                self.assertEqual(candles.volume, [minutes * (minutes + 1) // 2])
                self.assertEqual(candles.taker_buy_volume, [2 * minutes])
                self.assertEqual(candles.column("volume"), candles.volume)

    def test_missing_or_compromised_minute_invalidates_both_sums(self):
        for minutes in (15, 60, 240):
            for kind in ("missing_price", "compromised"):
                with self.subTest(minutes=minutes, kind=kind):
                    bars = minute_bars(2 * minutes)
                    if kind == "missing_price":
                        bars.open[minutes - 1] = MISSING
                    else:
                        bars.flags[minutes - 1] = COMPROMISED_FLAGS
                    candles = build_candles(bars, minutes)
                    self.assertEqual(candles.valid, [False, True])
                    for name in ("open", "high", "low", "close", "volume", "taker_buy_volume"):
                        self.assertEqual(getattr(candles, name)[0], MISSING)
                        self.assertIsNone(candles.column(name)[0])
                    self.assertEqual(candles.volume[1], minutes * (3 * minutes + 1) // 2)
                    self.assertEqual(candles.taker_buy_volume[1], 2 * minutes)


if __name__ == "__main__":
    unittest.main()
