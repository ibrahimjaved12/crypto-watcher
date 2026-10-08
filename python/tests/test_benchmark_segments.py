"""Exact chronological boundaries and time-based purge/embargo behavior."""
import unittest

from market_analysis.benchmark.segments import (
    SEGMENTS, allowed_ranges, eligible, excluded_interval, segment_bounds_ms,
    segment_months, segment_of, worst_case_window_end_ms,
)
from market_analysis.data_lake import month_bounds_ms


class SegmentTests(unittest.TestCase):
    def test_exact_millisecond_boundaries(self):
        names = list(SEGMENTS)
        for index, name in enumerate(names):
            with self.subTest(segment=name):
                first, end = segment_bounds_ms(name)
                self.assertEqual(segment_of(first), name)
                self.assertEqual(segment_of(end - 1), name)
                self.assertEqual(segment_of(first - 1), names[index - 1] if index else None)
                self.assertEqual(segment_of(end), names[index + 1] if index + 1 < len(names) else None)
        self.assertIsNone(segment_of(-1))

    def test_months_are_half_open(self):
        for name, count in (("development", 18), ("validation", 6), ("hidden", 9)):
            months = segment_months(name)
            self.assertEqual(len(months), count)
            self.assertEqual(months[0], SEGMENTS[name][0])
            self.assertNotIn(SEGMENTS[name][1], months)
            self.assertEqual(month_bounds_ms(months[-1])[1], segment_bounds_ms(name)[1])

    def test_closed_window_eligibility_at_both_edges(self):
        for name in SEGMENTS:
            first, end = segment_bounds_ms(name)
            with self.subTest(segment=name):
                self.assertTrue(eligible(name, first, first))
                self.assertTrue(eligible(name, first, end - 1))
                self.assertTrue(eligible(name, end - 1, end - 1))
                self.assertFalse(eligible(name, first - 1, first))
                self.assertFalse(eligible(name, end - 1, end))
                self.assertFalse(eligible(name, end, end))
        for left, right in (("development", "validation"), ("validation", "hidden")):
            boundary = segment_bounds_ms(left)[1]
            for end in (boundary, boundary + 1):
                self.assertFalse(eligible(left, boundary - 1, end))
                self.assertFalse(eligible(right, boundary - 1, end))

    def test_worst_case_end_is_exact_and_outcome_independent(self):
        self.assertEqual(worst_case_window_end_ms(1, 15), 3_600_001)
        self.assertEqual(worst_case_window_end_ms(1, 15, 2), 1_800_001)
        boundary = segment_bounds_ms("development")[1]
        signal = boundary - 60_000
        self.assertFalse(eligible("development", signal, worst_case_window_end_ms(signal, 1)))

    def test_purge_and_embargo(self):
        self.assertEqual(excluded_interval(100, 200, 20, 30), (80, 230))
        self.assertEqual(allowed_ranges(0, 300, [(100, 200)], 20, 30), [(0, 80), (230, 300)])
        self.assertEqual(allowed_ranges(0, 300, [(100, 200)], 0, 0), [(0, 100), (200, 300)])

    def test_merge_clip_sort_and_empty(self):
        ranges = [(200, 210), (100, 120), (110, 140), (160, 180), (100, 120)]
        self.assertEqual(allowed_ranges(0, 300, ranges, 10, 10), [(0, 90), (220, 300)])
        self.assertEqual(allowed_ranges(0, 300, [(-10, 10), (290, 400)], 20, 30), [(40, 270)])
        self.assertEqual(allowed_ranges(0, 300, [(-100, -50), (400, 500)], 0, 0), [(0, 300)])
        self.assertEqual(allowed_ranges(0, 300, [(0, 300)], 0, 0), [])
        self.assertEqual(allowed_ranges(0, 0, [], 0, 0), [])
        self.assertEqual(allowed_ranges(0, 300, [(50, 50)], 20, 30), [(0, 300)])
        self.assertEqual(allowed_ranges(0, 300, [], 20, 30), [(0, 300)])

    def test_invalid_ranges_and_non_integer_values(self):
        for call in (lambda: eligible("hidden", 2, 1),
                     lambda: excluded_interval(2, 1, 0, 0),
                     lambda: allowed_ranges(2, 1, [], 0, 0),
                     lambda: allowed_ranges(0, 1, [], -1, 0),
                     lambda: excluded_interval(0, 1, 0, -1),
                     lambda: worst_case_window_end_ms(0, -1)):
            with self.assertRaises(ValueError):
                call()
        for value in (True, "1", None):
            with self.assertRaises(TypeError):
                segment_of(value)


if __name__ == "__main__":
    unittest.main()
