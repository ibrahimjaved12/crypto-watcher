"""Canonical serialization and exact-number text for the #182 benchmark harness."""
from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
import hashlib
import unittest

from market_analysis.benchmark.canonical import canonical_bytes, content_hash, exact_from_str, exact_to_str


class CanonicalBytesTests(unittest.TestCase):
    def test_golden_vector(self):
        value = {"b": [1, True, None], "a": "é"}
        self.assertEqual(canonical_bytes(value), b'{"a":"\\u00e9","b":[1,true,null]}')
        self.assertEqual(content_hash(value), hashlib.sha256(b'{"a":"\\u00e9","b":[1,true,null]}').hexdigest())

    def test_tuples_encode_as_lists_and_key_order_is_irrelevant(self):
        self.assertEqual(canonical_bytes({"x": (1, 2), "a": {"z": False, "y": ""}}),
                         b'{"a":{"y":"","z":false},"x":[1,2]}')
        self.assertEqual(content_hash({"a": 1, "b": 2}), content_hash({"b": 2, "a": 1}))
        self.assertEqual(canonical_bytes(-12345678901234567890), b"-12345678901234567890")

    def test_rejected_values_name_their_json_path(self):
        cases = (
            ({"a": [0, 1, {"b": 1.5}]}, r"\$\.a\[2\]\.b: float"),
            ({"a": Fraction(1, 3)}, r"\$\.a: Fraction"),
            ([Decimal("1")], r"\$\[0\]: Decimal"),
            ({"a": {1: "x"}}, r"\$\.a: dict key 1 is int"),
            ({"a": b"raw"}, r"\$\.a: bytes"),
            ({"a": {1, 2}}, r"\$\.a: set"),
            (1.0, r"^\$: float"),
            (float("nan"), r"^\$: float"),
        )
        for value, message in cases:
            with self.subTest(value=value), self.assertRaisesRegex(TypeError, message):
                canonical_bytes(value)
        with self.assertRaises(TypeError):
            content_hash({"a": 0.1})


class ExactTextTests(unittest.TestCase):
    def test_round_trips(self):
        for value, text in ((0, "0"), (7, "7"), (-7, "-7"), (Fraction(1, 2), "1/2"), (Fraction(-3, 4), "-3/4"),
                            (Fraction(4, 2), "2"), (Fraction(6, -4), "-3/2"), (10 ** 30, str(10 ** 30))):
            with self.subTest(value=value):
                self.assertEqual(exact_to_str(value), text)
                self.assertEqual(exact_from_str(text), Fraction(value))

    def test_rejected_forms(self):
        for text in ("2/4", "+1", "01", "4/1", "-0", "1/0", "1/-2", "1.5", "", " 1", "1/2 ", "1/02", "0/5", "1e3"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                exact_from_str(text)
        for value in (1.5, True, Decimal("1"), "1"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                exact_to_str(value)


if __name__ == "__main__":
    unittest.main()
