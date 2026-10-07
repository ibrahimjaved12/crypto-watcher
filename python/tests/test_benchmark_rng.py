"""Counter-based random words for the #182 benchmark harness."""
from __future__ import annotations

import hashlib
import unittest

import numpy as np

from market_analysis.benchmark.rng import u64_words


def rederived(seed, stream, index):
    """Independent re-derivation of word ``index`` straight from hashlib."""
    digest = hashlib.sha256(f"{seed}|{stream}|{index // 4}".encode("ascii")).digest()
    position = 8 * (index % 4)
    return int.from_bytes(digest[position:position + 8], "big")


class RngTests(unittest.TestCase):
    def test_golden_words(self):
        words = u64_words(0, "s", 0, 6)
        self.assertEqual(words.dtype, np.uint64)
        self.assertEqual([int(word) for word in words], [rederived(0, "s", i) for i in range(6)])

    def test_streams_and_seeds_differ(self):
        base = u64_words(0, "s", 0, 8)
        self.assertFalse(np.array_equal(base, u64_words(1, "s", 0, 8)))
        self.assertFalse(np.array_equal(base, u64_words(0, "t", 0, 8)))
        self.assertFalse(np.array_equal(base, u64_words(0, "s/rep/0", 0, 8)))

    def test_range_split_equals_full(self):
        full = u64_words(7, "stream", 0, 37)
        for start, count in ((0, 37), (3, 5), (4, 4), (5, 32), (36, 1), (10, 0)):
            with self.subTest(start=start, count=count):
                self.assertTrue(np.array_equal(u64_words(7, "stream", start, count), full[start:start + count]))
        joined = np.concatenate([u64_words(7, "stream", 0, 11), u64_words(7, "stream", 11, 26)])
        self.assertTrue(np.array_equal(joined, full))
        self.assertEqual([int(w) for w in u64_words(7, "stream", 35, 2)], [rederived(7, "stream", 35),
                                                                            rederived(7, "stream", 36)])

    def test_invalid_arguments(self):
        for args in ((-1, "s", 0, 1), (0, "", 0, 1), (0, "a|b", 0, 1), (0, "s", -1, 1), (0, "s", 0, -1),
                     (True, "s", 0, 1), (0, "é", 0, 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                u64_words(*args)


if __name__ == "__main__":
    unittest.main()
