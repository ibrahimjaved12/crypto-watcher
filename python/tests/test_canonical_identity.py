"""Byte-stable identity fixtures captured from the pre-extraction #73 helper."""

from dataclasses import dataclass
from decimal import Decimal
import unittest

from market_analysis.canonical_identity import canonical_digest, canonical_value
from market_analysis.exact_scalar import ExactScalar
from market_analysis.market_episode_lifecycle import MarketEpisodeLifecycleConfig


@dataclass(frozen=True)
class Fixture:
    decimal: Decimal
    nested: tuple


class CanonicalIdentityTests(unittest.TestCase):
    def test_existing_lossless_bytes_and_lifecycle_config_identity(self):
        payload = {'z': Fixture(Decimal('1.2300'), (None, True, 7, 0.5)),
                   'é': {2: 'two', 'a': 'α'}}
        self.assertEqual(canonical_value(payload), {
            'z': {'decimal': '1.2300', 'nested': [None, True, 7, 0.5]},
            'é': {'2': 'two', 'a': 'α'}})
        self.assertEqual(canonical_digest(payload),
                         '7dc5918662ad1eea0665dc60b26df0f2e06119c377f84ec543883b627150a50b')
        self.assertEqual(canonical_digest(MarketEpisodeLifecycleConfig()),
                         '6e1d4cd051db8f8cbfe263bd62c387139f3cdc59e886a811c31e82765a6903b2')

    def test_exact_rational_encoding_uses_normalized_integers(self):
        self.assertEqual(canonical_value(ExactScalar(2, -6)), {'numerator': -1, 'denominator': 3})
        self.assertEqual(canonical_digest(ExactScalar(2, 6)), canonical_digest(ExactScalar(1, 3)))
        self.assertNotEqual(canonical_digest(ExactScalar(1, 3)), canonical_digest(ExactScalar(2, 3)))

    def test_existing_invalid_type_and_nonfinite_float_rejection(self):
        for value in (float('nan'), float('inf'), object()):
            with self.assertRaises(ValueError):
                canonical_digest(value)


if __name__ == '__main__':
    unittest.main()
