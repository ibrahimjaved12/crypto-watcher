"""Instrument identity and strategy compatibility vocabulary (#374)."""

from fractions import Fraction
import unittest

from market_analysis.instruments import (
    AssetClass, DataField, FOREX_DEFAULTS, FOREX_OPTIONAL, Instrument, InstrumentKind, Market,
    StrategyCapabilities, VenueSymbol, check_compatibility, crypto_instrument, forex_instrument,
    provided_fields)

ANY_VOLUME = frozenset({DataField.VOLUME_REAL, DataField.VOLUME_TICK})


class InstrumentIdentityTests(unittest.TestCase):
    def test_crypto_id_format_is_unchanged(self):
        btc = crypto_instrument("btcusdt")
        self.assertEqual(btc.instrument_id, "binance-usdm:BTCUSDT")
        self.assertEqual((btc.base, btc.quote), ("BTC", "USDT"))
        self.assertIs(btc.market, Market.CRYPTO)
        self.assertIs(btc.kind, InstrumentKind.PERPETUAL)

    def test_non_usdt_crypto_symbol_is_rejected(self):
        for symbol in ("BTCUSD", "BTCBUSD", "USDT", ""):
            with self.assertRaises(ValueError):
                crypto_instrument(symbol)

    def test_kind_market_mismatch_raises(self):
        with self.assertRaises(ValueError):
            Instrument("binance-usdm:BTCUSDT", Market.CRYPTO, InstrumentKind.SPOT_CFD,
                       AssetClass.CRYPTO, "BTC", "USDT", "BTCUSDT")
        with self.assertRaises(ValueError):
            Instrument("fx:EURUSD", Market.FOREX, InstrumentKind.PERPETUAL,
                       AssetClass.FX, "EUR", "USD", "EURUSD")

    def test_bad_id_format_raises(self):
        for bad in ("EURUSD", "FX:EURUSD", "fx:eurusd", "fx:", ":EURUSD", "fx:EUR USD"):
            with self.assertRaises(ValueError):
                Instrument(bad, Market.FOREX, InstrumentKind.SPOT_CFD,
                           AssetClass.FX, "EUR", "USD", "EURUSD")
        with self.assertRaises(ValueError):
            Instrument("fut:EURUSD", Market.FOREX, InstrumentKind.SPOT_CFD,
                       AssetClass.FX, "EUR", "USD", "EURUSD")

    def test_forex_defaults_and_optional(self):
        self.assertEqual([i.instrument_id for i in FOREX_DEFAULTS],
                         ["fx:XAUUSD", "fx:EURUSD", "fx:USDJPY", "fx:USOIL"])
        self.assertEqual([i.instrument_id for i in FOREX_OPTIONAL], ["fx:DXY", "fx:UKOIL"])
        self.assertIs(FOREX_DEFAULTS[0].asset_class, AssetClass.METAL)
        self.assertEqual(FOREX_DEFAULTS[3].display, "WTI crude")

    def test_forex_instrument_base_quote_rules(self):
        eur = forex_instrument("EURUSD")
        self.assertEqual((eur.base, eur.quote, eur.instrument_id), ("EUR", "USD", "fx:EURUSD"))
        self.assertEqual(forex_instrument("eurusd", kind=InstrumentKind.FUTURE).instrument_id,
                         "fut:EURUSD")
        with self.assertRaises(ValueError):
            forex_instrument("USOIL")
        with self.assertRaises(ValueError):
            forex_instrument("EURUSD", kind=InstrumentKind.PERPETUAL)

    def test_venue_symbol_validation(self):
        venue = VenueSymbol("exness-mt5-demo", "EURUSDm", "fx:EURUSD",
                            digits=5, tick_size=Fraction(1, 100000))
        self.assertEqual(venue.tick_size, Fraction(1, 100000))
        for kwargs in ({"tick_size": Fraction(0)}, {"tick_size": Fraction(-1, 10)},
                       {"contract_size": Fraction(0)}, {"digits": -1}):
            with self.assertRaises(ValueError):
                VenueSymbol("exness-mt5-demo", "EURUSDm", "fx:EURUSD", **kwargs)
        with self.assertRaises(ValueError):
            VenueSymbol("", "EURUSDm", "fx:EURUSD")


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.btc = crypto_instrument("BTCUSDT")
        self.eur = forex_instrument("EURUSD")
        self.eur_future = forex_instrument("EURUSD", kind=InstrumentKind.FUTURE)

    def test_taker_flow_strategy(self):
        crypto_only = StrategyCapabilities(frozenset({Market.CRYPTO}),
                                           frozenset({DataField.OHLC, DataField.TAKER_FLOW}))
        self.assertTrue(check_compatibility(crypto_only, self.btc).ok)
        self.assertEqual(check_compatibility(crypto_only, self.eur).reasons,
                         ("market_not_supported:forex", "missing_field:taker_flow"))
        both = StrategyCapabilities(frozenset({Market.CRYPTO, Market.FOREX}),
                                    frozenset({DataField.OHLC, DataField.TAKER_FLOW}))
        result = check_compatibility(both, self.eur)
        self.assertFalse(result.ok)
        self.assertEqual(result.reasons, ("missing_field:taker_flow",))

    def test_volume_requirements(self):
        markets = frozenset({Market.CRYPTO, Market.FOREX})
        any_volume = StrategyCapabilities(markets, frozenset({DataField.OHLC}), (ANY_VOLUME,))
        self.assertTrue(check_compatibility(any_volume, self.eur).ok)
        real_volume = StrategyCapabilities(markets, frozenset({DataField.VOLUME_REAL}))
        self.assertEqual(check_compatibility(real_volume, self.eur).reasons,
                         ("missing_field:volume_real",))
        self.assertTrue(check_compatibility(real_volume, self.eur_future).ok)
        self.assertIn(DataField.VOLUME_REAL, provided_fields(self.eur_future))

    def test_fails_closed_without_capabilities(self):
        result = check_compatibility(None, self.btc)
        self.assertEqual((result.ok, result.reasons), (False, ("no_capabilities_declared",)))

    def test_reason_order_is_deterministic(self):
        caps = StrategyCapabilities(
            frozenset({Market.CRYPTO}),
            frozenset({DataField.TAKER_FLOW, DataField.FUNDING, DataField.OHLC}),
            (frozenset({DataField.OPEN_INTEREST, DataField.MARK}),))
        expected = ("market_not_supported:forex", "missing_field:funding",
                    "missing_field:taker_flow", "missing_any_of:mark|open_interest")
        self.assertEqual(check_compatibility(caps, self.eur).reasons, expected)
        self.assertEqual(check_compatibility(caps, self.eur).reasons, expected)

    def test_explicit_provided_overrides_market_defaults(self):
        caps = StrategyCapabilities(frozenset({Market.CRYPTO}), frozenset({DataField.FUNDING}))
        self.assertEqual(check_compatibility(caps, self.btc, frozenset({DataField.OHLC})).reasons,
                         ("missing_field:funding",))

    def test_empty_markets_rejected(self):
        with self.assertRaises(ValueError):
            StrategyCapabilities(frozenset())


if __name__ == '__main__':
    unittest.main()
