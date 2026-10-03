"""Focused deterministic #37 Part 2 fixtures; no source downloads or studies."""

from dataclasses import FrozenInstanceError, fields, replace
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import weakref
import zipfile

from market_analysis import futures_execution as math
from market_analysis import binance_execution_evidence as trade_evidence
from market_analysis.canonical_identity import canonical_value
from market_analysis.binance_historical_archive import daily_aggtrades_relative_path
from market_analysis.binance_execution_evidence import (
    ExactSettlementMark, ExecutionTrade, ExecutionTradeTapeEvidence, FundingEvidence,
    FundingSettlementEvidence, adapt_funding_evidence, adapt_mark_risk_evidence,
    iter_execution_trades, join_settlement_mark, load_execution_mark_risk_evidence, load_execution_trade_tape,
)
from market_analysis.binance_execution_snapshots import (
    normalize_bracket_snapshot, normalize_contract_snapshot, normalize_fee_snapshot,
    settlement_mark_from_funding_history,
)
from market_analysis.execution_evidence import (
    COMPONENTS, EvidenceReference, ExecutionEvidenceSnapshot, SourceIdentity,
    SourceKind, SourceProvenance, execution_evidence_snapshot,
)
from market_analysis.futures_execution_contracts import (
    OrderIntent, OrderSide, PositionSide, Provenance, RuleEvaluationContext, Scope,
)
from market_analysis.historical_funding_evidence import (
    load_binance_usdm_funding_evidence, monthly_funding_relative_path,
)
from market_analysis.historical_mark_price_evidence import (
    daily_mark_price_relative_path, load_binance_usdm_mark_price_evidence,
)


D = Decimal
SYMBOL = "BTCUSDT"
INSTRUMENT = "binance-usdm:BTCUSDT"
SCOPE = Scope(INSTRUMENT)
DAY = date(2026, 8, 20)
TIME = int(datetime(2026, 8, 20, 1, tzinfo=timezone.utc).timestamp()) * 1000
HASH = "a" * 64


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def current(**changes):
    return replace(SourceProvenance("frozen-binance-snapshot", "fixture-v1",
                                   Provenance.CURRENT_RULE_ASSUMPTION, SourceKind.CURRENT_SNAPSHOT,
                                   observed_at_ms=TIME + 86_400_000, applicable_at_ms=TIME), **changes)


def fixed(**changes):
    return replace(SourceProvenance("simulation-config", "fixture-v1",
                                   Provenance.FIXED_SIMULATION_ASSUMPTION, SourceKind.FIXED_CONFIG), **changes)


def factual(**changes):
    return replace(SourceProvenance("frozen-funding-event", "fixture-v1",
                                   Provenance.ACTUAL_HISTORICAL, SourceKind.EXCHANGE_EVENT,
                                   observed_at_ms=TIME + 100, effective_at_ms=TIME), **changes)


def archive(root, relative, rows):
    path = root.joinpath(*relative.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    csv = "\n".join(",".join(str(v) for v in row) for row in rows) + "\n"
    with zipfile.ZipFile(path, "w") as z:
        info = zipfile.ZipInfo(relative.stem + ".csv", (2026, 1, 1, 0, 0, 0))
        z.writestr(info, csv)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(str(path) + ".CHECKSUM").write_text(f"{digest}  {relative.name}\n")
    return digest


def agg(ident=10, timestamp=TIME, price="100", maker="false"):
    return [ident, price, "2.5", 100, 105, timestamp, maker]


def mark_row(timestamp=TIME):
    return [timestamp, "100", "110", "90", "105", "0", timestamp + 59999, "0", 0, "0", "0", "0"]


def rule_payload():
    return {"symbols": [{"symbol": SYMBOL, "status": "TRADING", "contractType": "PERPETUAL",
                         "quoteAsset": "USDT", "marginAsset": "USDT", "triggerProtect": "0.05",
                         "filters": [
                             {"filterType": "PRICE_FILTER", "minPrice": "0", "maxPrice": "0", "tickSize": "0"},
                             {"filterType": "LOT_SIZE", "minQty": "0.1", "maxQty": "100", "stepSize": "0.2"},
                             {"filterType": "MARKET_LOT_SIZE", "minQty": "0.2", "maxQty": "10", "stepSize": "0.2"},
                             {"filterType": "MIN_NOTIONAL", "notional": "5"},
                             {"filterType": "PERCENT_PRICE", "multiplierDown": "0.9", "multiplierUp": "1.1"},
                         ]}]}


def rules(payload=None, provenance=None):
    return normalize_contract_snapshot(encoded(payload or rule_payload()), SYMBOL,
                                       provenance or current(), reduce_only_exempt=True)


def bracket_payload(coef=None):
    row = {"symbol": SYMBOL, "brackets": [
        {"bracket": 1, "initialLeverage": 20, "notionalFloor": 0, "notionalCap": 1000,
         "maintMarginRatio": "0.01", "cum": 0},
        {"bracket": 2, "initialLeverage": 10, "notionalFloor": 1000, "notionalCap": 10000,
         "maintMarginRatio": "0.02", "cum": 10},
    ]}
    if coef is not None:
        row["notionalCoef"] = coef
    return row


def brackets(payload=None, **options):
    return normalize_bracket_snapshot(encoded(payload or bracket_payload()), SYMBOL, current(),
                                      account_specific=options.pop("account_specific", False),
                                      values_basis=options.pop("values_basis", "EFFECTIVE_TIERS"), **options)


def fees(maker="0.0002", provenance=None):
    return normalize_fee_snapshot(encoded({"symbol": SYMBOL, "makerCommissionRate": maker,
                                          "takerCommissionRate": "0.0005"}), SYMBOL,
                                  provenance or fixed(), account_specific=False)


def funding():
    return FundingSettlementEvidence(SYMBOL, INSTRUMENT, TIME, D("0.0001"), 8, TIME + 1,
                                     "calc-time-plus-one-millisecond-surrogate-v1",
                                     SourceIdentity(factual(), HASH), upstream_evidence_sha256=HASH,
                                     factual_fields_json='{"rateType":"Regular"}')


def exact_mark(event, price="100"):
    return ExactSettlementMark(SYMBOL, INSTRUMENT, TIME, event.event_identity, D(price),
                               SourceIdentity(factual(source="official-funding-history"), "b" * 64))


class TradeEvidenceTests(unittest.TestCase):
    def test_verified_parser_facts_order_and_identity(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            relative = daily_aggtrades_relative_path(SYMBOL, DAY)
            rows = [agg(11, maker="true"), agg(10), agg(10)]
            digest = archive(Path(a), relative, rows)
            archive(Path(b), relative, rows)
            tape = load_execution_trade_tape(a, SYMBOL, [DAY])
            self.assertEqual(tape, load_execution_trade_tape(b, SYMBOL, [DAY]))
            streamed = list(iter_execution_trades(a, tape))
            self.assertEqual([r.aggregate_trade_id for r in streamed], [10, 11])
            self.assertEqual([r.aggressor_side for r in streamed], [OrderSide.BUY, OrderSide.SELL])
            self.assertEqual((tape.row_count, tape.duplicate_count), (2, 1))
            self.assertEqual((tape.first_event_key, tape.last_event_key), ((TIME, 10), (TIME, 11)))
            row = streamed[0]
            self.assertEqual((row.first_trade_id, row.last_trade_id, row.buyer_is_maker), (100, 105, False))
            self.assertEqual((row.price, row.quantity), (D(100), D("2.5")))
            self.assertEqual(row.source_archive_relative_path, str(relative))
            self.assertEqual(row.source_archive_sha256, digest)
            self.assertEqual(row.available_at_ms, TIME)
            self.assertEqual(row.timestamp_ms, TIME)
            self.assertIn("surrogate", row.availability_policy)
            self.assertNotIn(a, str(tape))
            with self.assertRaises(FrozenInstanceError):
                row.quantity = D(9)
            with self.assertRaises(FrozenInstanceError):
                tape.row_count = 9
            revised_package = replace(row.package, archive_sha256="c" * 64)
            revised = replace(tape, packages=(revised_package,))
            self.assertNotEqual(tape.identity, revised.identity)
            with self.assertRaisesRegex(ValueError, "hash conflict"):
                replace(tape, packages=tape.packages + (revised_package,))
            with self.assertRaisesRegex(ValueError, "hash conflict"):
                next(iter_execution_trades(a, revised))

    def test_reject_conflicting_duplicates_malformed_and_outside_day(self):
        invalid = [agg(price="NaN"), agg(price="Infinity"), agg(maker="1"), agg(timestamp=TIME + 86400000)]
        for column, value in ((0, "10.5"), (2, "-1"), (3, 106), (5, "bad")):
            row = agg()
            row[column] = value
            invalid.append(row)
        with tempfile.TemporaryDirectory() as folder:
            relative = daily_aggtrades_relative_path(SYMBOL, DAY)
            for row in invalid:
                with self.subTest(row=row):
                    archive(Path(folder), relative, [row])
                    with self.assertRaises(ValueError):
                        load_execution_trade_tape(folder, SYMBOL, [DAY])
            archive(Path(folder), relative, [agg(), agg(price="101")])
            with self.assertRaisesRegex(ValueError, "conflicting aggTrade ID"):
                load_execution_trade_tape(folder, SYMBOL, [DAY])

    def test_checksum_and_direct_contract_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            relative = daily_aggtrades_relative_path(SYMBOL, DAY)
            archive(Path(folder), relative, [agg()])
            tape = load_execution_trade_tape(folder, SYMBOL, [DAY])
            with closing(iter_execution_trades(folder, tape)) as stream:
                row = next(stream)
            for changes in ({"instrument_id": "binance-usdm:ETHUSDT"}, {"buyer_is_maker": 1},
                            {"price": 100.0}, {"timestamp_ms": True}, {"first_trade_id": 106},
                            {"availability_policy": "historical-network-receipt"}):
                with self.assertRaises(ValueError):
                    replace(row, **changes)
            Path(str(Path(folder).joinpath(*relative.parts)) + ".CHECKSUM").write_text(f'{"0" * 64}  {relative.name}')
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                load_execution_trade_tape(folder, SYMBOL, [DAY])

    def test_same_timestamp_has_no_cross_stream_chronology_or_fill_allocation(self):
        with tempfile.TemporaryDirectory() as folder:
            archive(Path(folder), daily_aggtrades_relative_path(SYMBOL, DAY), [agg(11), agg(10)])
            archive(Path(folder), daily_mark_price_relative_path(SYMBOL, DAY), [mark_row()])
            tape = load_execution_trade_tape(folder, SYMBOL, [DAY])
            risk = load_execution_mark_risk_evidence(folder, SYMBOL, TIME + 60000, TIME + 60000)
            with closing(iter_execution_trades(folder, tape)) as stream:
                row = next(stream)
            self.assertEqual(row.timestamp_ms, risk.minutes[0].candle.open_time_ms)
            self.assertIn("no-cross-stream-order", tape.ordering_policy)
            self.assertIsNone(risk.minutes[0].exact_intraminute_timestamp_ms)
            names = {f.name for f in fields(ExecutionTrade)}
            self.assertFalse(names & {"fill_quantity", "queue_position", "stream_sequence", "individual_trades"})

    def test_source_order_and_decimal_spelling_do_not_change_canonical_stream(self):
        with tempfile.TemporaryDirectory() as folder:
            relative = daily_aggtrades_relative_path(SYMBOL, DAY)
            rows = [agg(100, timestamp=TIME + 1), agg(20), agg(9), agg(2, timestamp=TIME + 2)]
            archive(Path(folder), relative, rows + [agg(9, price="100.00")])
            a = load_execution_trade_tape(folder, SYMBOL, [DAY])
            a_rows = [(r.timestamp_ms, r.aggregate_trade_id) for r in iter_execution_trades(folder, a)]
            archive(Path(folder), relative, [agg(9, price="100.00")] + list(reversed(rows)))
            b = load_execution_trade_tape(folder, SYMBOL, [DAY])
            b_rows = [(r.timestamp_ms, r.aggregate_trade_id) for r in iter_execution_trades(folder, b)]
            self.assertEqual(a_rows, [(TIME, 9), (TIME, 20), (TIME + 1, 100), (TIME + 2, 2)])
            self.assertEqual(a_rows, b_rows)
            self.assertEqual(a.normalized_rows_sha256, b.normalized_rows_sha256)
            self.assertEqual((a.row_count, a.duplicate_count), (b.row_count, b.duplicate_count))
            self.assertNotEqual(a.identity, b.identity)  # Raw source byte order is still hash-bound.
            with self.assertRaisesRegex(ValueError, "hash conflict"):
                next(iter_execution_trades(folder, a))
            archive(Path(folder), relative, rows[:-1] + [agg(2, timestamp=TIME + 2, price="101")])
            changed = load_execution_trade_tape(folder, SYMBOL, [DAY])
            self.assertNotEqual(b.normalized_rows_sha256, changed.normalized_rows_sha256)
            self.assertNotEqual(b.identity, changed.identity)

    def test_manifest_and_top_level_identity_never_embed_or_load_trade_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            archive(Path(folder), daily_aggtrades_relative_path(SYMBOL, DAY), [agg(10), agg(11)])
            tape = load_execution_trade_tape(folder, SYMBOL, [DAY])
            self.assertIsInstance(tape, ExecutionTradeTapeEvidence)
            self.assertNotIn("rows", {f.name for f in fields(ExecutionTradeTapeEvidence)})
            self.assertFalse(hasattr(tape, "rows"))
            self.assertNotIn("rows", canonical_value(tape))
            with patch.object(trade_evidence, "_ExecutionTradeIndex", side_effect=AssertionError("index opened")), \
                    patch.object(trade_evidence, "_iter_archive_rows", side_effect=AssertionError("rows loaded")):
                identity = tape.identity
                top = execution_evidence_snapshot(SCOPE, aggtrades=tape)
                ref = next(r for r in top.components if r.component == "aggtrades")
                self.assertEqual(ref.evidence_identity, identity)
                self.assertEqual(len(top.identity), 64)
                self.assertEqual(top.identity, execution_evidence_snapshot(SCOPE, aggtrades=tape).identity)
                self.assertNotIn("price", json.dumps(canonical_value(tape)))

    def test_synthetic_iteration_keeps_only_bounded_live_rows_and_cleans_temp_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            archive(Path(folder), daily_aggtrades_relative_path(SYMBOL, DAY),
                    [agg(i) for i in range(128, 0, -1)])
            live, peak, temporary_paths = weakref.WeakSet(), [0], []
            real_index = trade_evidence._ExecutionTradeIndex
            test = self
            class ObservedIndex(real_index):
                def __enter__(self):
                    super().__enter__()
                    test.assertEqual(self._connection.execute("PRAGMA cache_size").fetchone(), (-8192,))
                    test.assertEqual(self._connection.execute("PRAGMA temp_store").fetchone(), (1,))
                    temporary_paths.append(Path(self._temporary.name))
                    return self
            def tracked_trade(*args, **kwargs):
                row = ExecutionTrade(*args, **kwargs)
                live.add(row)
                peak[0] = max(peak[0], len(live))
                return row
            # Plain factory replacement avoids mocks retaining all constructor arguments.
            with patch.object(trade_evidence, "ExecutionTrade", new=tracked_trade), \
                    patch.object(trade_evidence, "_ExecutionTradeIndex", new=ObservedIndex):
                tape = load_execution_trade_tape(folder, SYMBOL, [DAY])
                self.assertEqual(tape.row_count, 128)
                self.assertEqual(len(live), 0)
                self.assertFalse(temporary_paths[-1].exists())
                count = 0
                for row in iter_execution_trades(folder, tape):
                    count += 1
                    self.assertEqual(row.aggregate_trade_id, count)
                del row
                self.assertEqual(count, 128)
                self.assertEqual(len(live), 0)
                self.assertLessEqual(peak[0], 3)
                self.assertFalse(temporary_paths[-1].exists())
                stream = iter_execution_trades(folder, tape)
                first = next(stream)
                self.assertTrue(temporary_paths[-1].exists())
                stream.close()
                self.assertFalse(temporary_paths[-1].exists())
                del first
                self.assertEqual(len(live), 0)

    def test_iterator_verifies_manifest_before_yield_and_rejects_changed_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            archive(Path(folder), daily_aggtrades_relative_path(SYMBOL, DAY), [agg(10), agg(11)])
            tape = load_execution_trade_tape(folder, SYMBOL, [DAY])
            for changes in ({"row_count": 3}, {"duplicate_count": 1},
                            {"normalized_rows_sha256": "c" * 64}, {"last_event_key": (TIME, 12)}):
                with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "manifest/content identity mismatch"):
                    next(iter_execution_trades(folder, replace(tape, **changes)))
            for changes in ({"ordering_policy": "ZIP-order"}, {"parser_version": "unknown"},
                            {"tape_version": "unknown"}, {"row_count": True}, {"duplicate_count": -1},
                            {"packages": ()}, {"instrument_id": "binance-usdm:ETHUSDT"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    replace(tape, **changes)

    def test_empty_and_multiple_packages_selection_and_big_integer_order(self):
        with tempfile.TemporaryDirectory() as folder:
            first_path = daily_aggtrades_relative_path(SYMBOL, DAY)
            archive(Path(folder), first_path, [["agg_trade_id", "price", "quantity", "first_trade_id",
                                              "last_trade_id", "transact_time", "is_buyer_maker"]])
            empty = load_execution_trade_tape(folder, SYMBOL, [DAY])
            self.assertEqual((empty.row_count, empty.duplicate_count, empty.first_event_key, empty.last_event_key), (0, 0, None, None))
            self.assertEqual(list(iter_execution_trades(folder, empty)), [])
            ref = next(r for r in execution_evidence_snapshot(SCOPE, aggtrades=empty).components if r.component == "aggtrades")
            self.assertIn("TRADE_ROWS_UNAVAILABLE", ref.limitations)
            next_day = date(2026, 8, 21)
            huge = 10 ** 30
            archive(Path(folder), first_path, [agg(huge + 10), agg(huge + 2), agg(9)])
            archive(Path(folder), daily_aggtrades_relative_path(SYMBOL, next_day), [agg(1, timestamp=TIME + 86400000)])
            tape = load_execution_trade_tape(folder, SYMBOL, [next_day, DAY, DAY])
            self.assertEqual(tape.identity, load_execution_trade_tape(folder, SYMBOL, [DAY, next_day]).identity)
            self.assertEqual(tape.identity, replace(tape, packages=tuple(reversed(tape.packages))).identity)
            self.assertEqual([r.aggregate_trade_id for r in iter_execution_trades(folder, tape)], [9, huge + 2, huge + 10, 1])
            self.assertNotEqual(tape.identity, load_execution_trade_tape(folder, SYMBOL, [DAY]).identity)
            # A conflicting aggregate ID across days is also caught by the shared disk index.
            archive(Path(folder), daily_aggtrades_relative_path(SYMBOL, next_day), [agg(9, timestamp=TIME + 86400000)])
            with self.assertRaisesRegex(ValueError, "conflicting aggTrade ID"):
                load_execution_trade_tape(folder, SYMBOL, [DAY, next_day])


class FundingEvidenceTests(unittest.TestCase):
    def test_missing_exact_mark_and_exact_join_with_part1(self):
        event = funding()
        self.assertEqual(event.funding_mark_status, "UNAVAILABLE_FUNDING_MARK")
        self.assertIsNone(event.settlement_mark)
        self.assertEqual(math.funding(PositionSide.LONG, D(2), event.funding_rate, event.settlement_mark).status,
                         "UNAVAILABLE_FUNDING_MARK")
        joined = join_settlement_mark(event, exact_mark(event))
        self.assertEqual((joined.funding_rate, joined.funding_interval_hours), (D(".0001"), 8))
        self.assertEqual(joined.event_identity, event.event_identity)
        self.assertEqual(math.funding(PositionSide.LONG, D(2), joined.funding_rate, joined.settlement_mark).value, D("-.02"))
        self.assertNotEqual(joined.identity, event.identity)
        self.assertNotEqual(joined.identity, join_settlement_mark(event, exact_mark(event, "101")).identity)
        self.assertNotEqual(event.identity, replace(event, funding_rate=D(".0002")).identity)
        changed_source = replace(event.source, provenance=replace(event.source.provenance, source="another-factual-source"))
        self.assertNotEqual(event.identity, replace(event, source=changed_source).identity)
        self.assertEqual(joined.exact_cashflow_evidence_available_at_ms, TIME + 100)
        unknown_observation = replace(exact_mark(event), source=SourceIdentity(
            factual(observed_at_ms=None), "b" * 64))
        self.assertIsNone(join_settlement_mark(event, unknown_observation).exact_cashflow_evidence_available_at_ms)
        self.assertEqual(unknown_observation.availability_policy, "OBSERVATION_TIME_UNAVAILABLE")

    def test_mismatches_cannot_join_and_candle_cannot_substitute(self):
        event = funding()
        mark = exact_mark(event)
        for wrong in (replace(mark, symbol="ETHUSDT", instrument_id="binance-usdm:ETHUSDT"),
                      replace(mark, funding_event_identity="c" * 64)):
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                join_settlement_mark(event, wrong)
        with self.assertRaises(ValueError):
            replace(mark, funding_timestamp_ms=TIME + 1)
        with self.assertRaises(ValueError):
            join_settlement_mark(event, D(100))
        with self.assertRaises(ValueError):
            replace(event, exact_mark=mark_row())
        with self.assertRaises(ValueError):
            replace(mark, source=SourceIdentity(fixed(), HASH))
        with self.assertRaises(ValueError):
            join_settlement_mark(join_settlement_mark(event, mark), exact_mark(event, "101"))
        with self.assertRaises(ValueError):
            replace(event, factual_fields_json='{"rateType":[]}')
        with self.assertRaises(ValueError):
            replace(event, factual_fields_json='{"fundingIntervalHours":4}')
        with self.assertRaises(ValueError):
            FundingEvidence(SYMBOL, INSTRUMENT, (event, replace(event, funding_rate=D(".001"))), HASH)

    def test_reuse_existing_funding_archive_without_identity_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            relative = monthly_funding_relative_path(SYMBOL, DAY.replace(day=1))
            digest = archive(Path(folder), relative, [
                ["calc_time", "funding_interval_hours", "last_funding_rate"], [TIME, 8, "0.0001"]])
            original = load_binance_usdm_funding_evidence(folder, (SYMBOL,), TIME, TIME)
            before = original.evidence_sha256
            adapted = adapt_funding_evidence(original, SYMBOL)
            self.assertEqual(original.evidence_sha256, before)
            event = adapted.events[0]
            self.assertEqual(event.source.content_sha256, digest)
            self.assertEqual(event.package_relative_path, str(relative))
            self.assertEqual(event.upstream_evidence_sha256, before)
            self.assertIsNone(event.source.provenance.observed_at_ms)
            self.assertEqual(event.funding_mark_status, "UNAVAILABLE_FUNDING_MARK")
            self.assertEqual(event.factual_fields_json, "{}")
            for rate_type in ("Regular", "Special"):
                with self.subTest(rate_type=rate_type):
                    record = {"symbol": SYMBOL, "fundingTime": TIME, "fundingRate": "0.00010000",
                              "markPrice": "100.123456789", "rateType": rate_type,
                              "fundingIntervalHours": 8, "factualFlag": "retained"}
                    joined = settlement_mark_from_funding_history(encoded([record]), event, factual())
                    self.assertEqual(json.loads(joined.factual_fields_json), {
                        "rateType": rate_type, "fundingIntervalHours": 8, "factualFlag": "retained"})
                    self.assertEqual(joined.settlement_mark, D("100.123456789"))
                    self.assertEqual(joined.exact_mark.funding_event_identity, joined.event_identity)
                    self.assertNotEqual(joined.event_identity, event.event_identity)
                    self.assertEqual(joined.upstream_evidence_sha256, before)
                    self.assertEqual(joined.package_relative_path, event.package_relative_path)
                    self.assertEqual(joined.source, event.source)
                    self.assertEqual(replace(adapted, events=(joined,)).upstream_evidence_sha256, before)
            self.assertEqual(event.factual_fields_json, "{}")
            self.assertEqual(original.evidence_sha256, before)

    def test_frozen_official_funding_history_exact_rate_type_and_hash(self):
        event = funding()
        record = {"symbol": SYMBOL, "fundingTime": TIME, "fundingRate": "0.0001", "markPrice": "100.123456789",
                  "rateType": "Regular", "fundingIntervalHours": 8, "factualFlag": "retained"}
        raw = encoded([record])
        joined = settlement_mark_from_funding_history(raw, event, factual())
        self.assertEqual(joined.settlement_mark, D("100.123456789"))
        self.assertEqual(json.loads(joined.factual_fields_json)["rateType"], "Regular")
        self.assertEqual(json.loads(joined.factual_fields_json)["factualFlag"], "retained")
        self.assertEqual(joined.exact_mark.source.content_sha256, hashlib.sha256(raw).hexdigest())
        for key, value in (("symbol", "ETHUSDT"), ("fundingTime", TIME + 1), ("fundingRate", "0.0002"),
                           ("rateType", "Special"), ("fundingIntervalHours", 4), ("markPrice", "NaN")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                settlement_mark_from_funding_history(encoded([dict(record, **{key: value})]), event, factual())
        enriched = settlement_mark_from_funding_history(raw, replace(event, factual_fields_json="{}"), factual())
        self.assertEqual(enriched.factual_fields_json, joined.factual_fields_json)
        self.assertEqual(enriched.exact_mark.funding_event_identity, enriched.event_identity)
        with self.assertRaisesRegex(ValueError, "hash conflict"):
            settlement_mark_from_funding_history(raw, event, factual(), expected_sha256=HASH)

    def test_missing_rate_type_multiple_matches_fail_ambiguous_before_mark_validation(self):
        event = replace(funding(), factual_fields_json="{}")
        regular = {"symbol": SYMBOL, "fundingTime": TIME, "fundingRate": "0.0001",
                   "markPrice": "100", "rateType": "Regular"}
        special = dict(regular, rateType="Special", markPrice="101")
        # Neither input order nor even malformed markPrice may decide the event.
        for records in ([regular, special], [special, regular],
                        [dict(regular, markPrice="NaN"), special], [regular, regular]):
            with self.subTest(records=records), self.assertRaisesRegex(ValueError, "ambiguous funding-history event"):
                settlement_mark_from_funding_history(encoded(records), event, factual())
        with self.assertRaisesRegex(ValueError, "ambiguous funding-history event"):
            settlement_mark_from_funding_history(encoded([regular, special]),
                replace(event, factual_fields_json='{"markPrice":"100"}'), factual())
        self.assertEqual(event.factual_fields_json, "{}")
        self.assertIsNone(event.exact_mark)

    def test_candidates_filter_exact_rate_interval_and_known_facts_before_uniqueness(self):
        event = replace(funding(), factual_fields_json='{"factualFlag":"known"}')
        good = {"symbol": SYMBOL, "fundingTime": TIME, "fundingRate": "0.0001000000",
                "markPrice": "100.123456789", "rateType": "Special", "factualFlag": "known"}
        for key, value in (("symbol", "ETHUSDT"), ("fundingTime", TIME + 1),
                           ("fundingRate", "0.00010000000000000000000000001"),
                           ("fundingIntervalHours", 4), ("fundingIntervalHours", True),
                           ("factualFlag", "different")):
            wrong = dict(good, **{key: value})
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ValueError, "mismatch"):
                    settlement_mark_from_funding_history(encoded([wrong]), event, factual())
                for records in ([wrong, good], [good, wrong]):
                    with localcontext() as context:
                        context.prec = 1
                        joined = settlement_mark_from_funding_history(encoded(records), event, factual())
                    self.assertEqual(joined.settlement_mark, D("100.123456789"))
                    self.assertEqual(json.loads(joined.factual_fields_json)["rateType"], "Special")
        missing_fact = dict(good)
        del missing_fact["factualFlag"]
        with self.assertRaisesRegex(ValueError, "mismatch"):
            settlement_mark_from_funding_history(encoded([missing_fact]), event, factual())
        matched = settlement_mark_from_funding_history(encoded([missing_fact, good]), event, factual())
        self.assertEqual(json.loads(matched.factual_fields_json)["factualFlag"], "known")

    def test_explicit_rate_type_selects_matching_event_and_requires_known_facts(self):
        event = replace(funding(), factual_fields_json='{"rateType":"Regular","factualFlag":"known"}')
        regular = {"symbol": SYMBOL, "fundingTime": TIME, "fundingRate": "0.0001",
                   "markPrice": "100", "rateType": "Regular", "factualFlag": "known"}
        special = dict(regular, rateType="Special", markPrice="101")
        joined = settlement_mark_from_funding_history(encoded([special, regular]), event, factual())
        self.assertEqual(joined.settlement_mark, D(100))
        self.assertEqual(json.loads(joined.factual_fields_json)["rateType"], "Regular")
        for row in (special, dict(regular, factualFlag="different"),
                    {k: v for k, v in regular.items() if k != "factualFlag"},
                    {k: v for k, v in regular.items() if k != "rateType"}):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, "mismatch"):
                settlement_mark_from_funding_history(encoded([row]), event, factual())

    def test_funding_collection_requires_every_events_actual_upstream_hash(self):
        event = funding()
        collection = FundingEvidence(SYMBOL, INSTRUMENT, (event,), HASH)
        self.assertEqual(collection.events[0].upstream_evidence_sha256, HASH)
        with self.assertRaisesRegex(ValueError, "upstream evidence hash mismatch"):
            replace(collection, upstream_evidence_sha256="b" * 64)
        for wrong in (replace(event, upstream_evidence_sha256="b" * 64),
                      replace(event, upstream_evidence_sha256=None)):
            with self.assertRaisesRegex(ValueError, "upstream evidence hash mismatch"):
                replace(collection, events=(wrong,))
            later = replace(wrong, funding_timestamp_ms=TIME + 1, available_at_ms=TIME + 2,
                            source=replace(wrong.source, provenance=replace(
                                wrong.source.provenance, effective_at_ms=TIME + 1)))
            with self.assertRaisesRegex(ValueError, "upstream evidence hash mismatch"):
                replace(collection, events=(event, later))


class MarkRiskTests(unittest.TestCase):
    def test_existing_loader_ohlc_completion_and_bounds_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            relative = daily_mark_price_relative_path(SYMBOL, DAY)
            digest = archive(Path(folder), relative, [mark_row()])
            existing = load_binance_usdm_mark_price_evidence(folder, (SYMBOL,), TIME + 60000, TIME + 60000)
            before = existing.evidence_sha256
            risk = adapt_mark_risk_evidence(existing, SYMBOL)
            minute = risk.minutes[0]
            self.assertEqual(minute.candle, existing.candles[0])
            self.assertEqual((minute.candle.open, minute.candle.high, minute.candle.low, minute.candle.close),
                             (D(100), D(110), D(90), D(105)))
            self.assertEqual((minute.candle.open_time_ms, minute.candle.close_time_ms, minute.available_at_ms),
                             (TIME, TIME + 59999, TIME + 60000))
            self.assertEqual(minute.source.content_sha256, digest)
            self.assertEqual(minute.upstream_evidence_sha256, before)
            self.assertEqual(minute.resolution, "ONE_MINUTE_OHLC")
            self.assertIsNone(minute.exact_intraminute_mark)
            self.assertIsNone(minute.exact_intraminute_timestamp_ms)
            self.assertEqual(existing.evidence_sha256, before)
            with self.assertRaises(ValueError):
                replace(minute, exact_intraminute_mark=D(105))
            with self.assertRaises(ValueError):
                replace(minute, exact_intraminute_timestamp_ms=TIME)
            with patch("market_analysis.binance_execution_evidence.load_binance_usdm_mark_price_evidence", return_value=existing) as loader:
                self.assertEqual(load_execution_mark_risk_evidence(folder, SYMBOL, TIME, TIME), risk)
                loader.assert_called_once_with(folder, (SYMBOL,), TIME, TIME, download=False)

    def test_missing_packages_stay_visible_and_no_fabricated_mark(self):
        with tempfile.TemporaryDirectory() as folder:
            risk = load_execution_mark_risk_evidence(folder, SYMBOL, TIME, TIME)
            self.assertEqual(risk.minutes, ())
            snapshot = execution_evidence_snapshot(SCOPE, mark_price=risk)
            ref = next(c for c in snapshot.components if c.component == "mark_price")
            self.assertIn("MARK_MINUTES_UNAVAILABLE", ref.limitations)


class SnapshotAdapterTests(unittest.TestCase):
    def test_rules_exact_filters_and_distinct_field_provenance(self):
        adapted = rules()
        r = adapted.rules
        self.assertEqual(r.scope, SCOPE)
        self.assertEqual((r.lot.minimum, r.lot.maximum, r.lot.increment), (D(".1"), D(100), D(".2")))
        self.assertEqual((r.market_lot.minimum, r.market_lot.increment), (D(".2"), D(".2")))
        self.assertEqual((r.price.min_price, r.price.max_price, r.price.tick_size), (D(0), D(0), D(0)))
        self.assertEqual((r.percent_price.multiplier_down, r.percent_price.multiplier_up), (D(".9"), D("1.1")))
        self.assertEqual(r.min_notional.minimum, D(5))
        self.assertEqual(r.evidence.classification, Provenance.CURRENT_RULE_ASSUMPTION)
        self.assertEqual(r.min_notional.evidence.classification, Provenance.FIXED_SIMULATION_ASSUMPTION)
        self.assertEqual(r.evidence.observed_at, str(TIME + 86400000))
        self.assertIsNone(r.evidence.effective_at)
        self.assertEqual(adapted.normalized_identity, r.identity)
        self.assertEqual(adapted.source.content_sha256, hashlib.sha256(encoded(rule_payload())).hexdigest())

    def test_part1_order_applicability_and_no_adjustment(self):
        r = rules().rules
        context = RuleEvaluationContext(D(100), r.evidence)
        market = OrderIntent(OrderSide.BUY, D(".2"), None, market=True)
        limit = OrderIntent(OrderSide.BUY, D(".1"), D(100))
        self.assertEqual(math.validate_order(market, r, context).status, "VALID")
        self.assertEqual(math.validate_order(limit, r, context).status, "VALID")
        self.assertIn("MARKET_LOT_SIZE:OFF_GRID", math.validate_order(replace(market, quantity=D(".3")), r, context).reasons)
        self.assertIn("LOT_SIZE:OFF_GRID", math.validate_order(replace(limit, quantity=D(".2")), r, context).reasons)
        self.assertEqual(math.validate_order(replace(limit, price=D(1)), r, context).status, "REJECTED_RULE")
        self.assertEqual(math.validate_order(market, r).status, "UNAVAILABLE_RULE")
        # BUY lower and SELL upper percent limits are deliberately not checked.
        self.assertNotIn("PERCENT_PRICE:BELOW_MINIMUM", math.validate_order(replace(limit, quantity=D(1), price=D(80)), r, context).reasons)
        self.assertEqual(math.validate_order(replace(limit, side=OrderSide.SELL, price=D(120)), r, context).status, "VALID")
        self.assertEqual(market.quantity, D(".2"))
        for changes in ({"price": D(100)},):
            with self.assertRaises(ValueError):
                replace(market, **changes)
        with self.assertRaises(ValueError):
            replace(limit, price=None)

    def test_rule_identity_changes_with_parameters_and_provenance(self):
        a = rules()
        payload = rule_payload()
        payload["symbols"][0]["filters"][1]["stepSize"] = "0.1"
        self.assertNotEqual(a.identity, rules(payload).identity)
        self.assertNotEqual(a.normalized_identity, rules(payload).normalized_identity)
        self.assertNotEqual(a.identity, rules(provenance=fixed()).identity)
        self.assertNotEqual(a.rules.identity, rules(provenance=fixed()).rules.identity)

    def test_malformed_duplicate_required_filters_and_hash_conflicts_rejected(self):
        for change in ("missing", "zero-step", "bad-bounds", "duplicate", "bad-percent", "nan"):
            payload = rule_payload()
            f = payload["symbols"][0]["filters"]
            if change == "missing":
                f.pop()
            elif change == "zero-step":
                f[1]["stepSize"] = "0"
            elif change == "bad-bounds":
                f[1]["maxQty"] = "0.01"
            elif change == "duplicate":
                f.append(f[0])
            elif change == "bad-percent":
                f[4]["multiplierDown"] = "1.2"
            else:
                f[0]["tickSize"] = "NaN"
            with self.subTest(change=change), self.assertRaises(ValueError):
                rules(payload)
        with self.assertRaisesRegex(ValueError, "hash conflict"):
            normalize_contract_snapshot(encoded(rule_payload()), SYMBOL, current(), reduce_only_exempt=True, expected_sha256=HASH)
        with self.assertRaises(ValueError):
            normalize_contract_snapshot(b'{"symbols":[],"symbols":[]}', SYMBOL, current(), reduce_only_exempt=True)
        with self.assertRaises(ValueError):
            normalize_fee_snapshot(b'{"symbol":"BTCUSDT","makerCommissionRate":NaN,"takerCommissionRate":"0"}', SYMBOL, fixed(), account_specific=False)

    def test_current_and_fixed_cannot_masquerade_as_historical(self):
        with self.assertRaisesRegex(ValueError, "masquerade"):
            current(classification=Provenance.ACTUAL_HISTORICAL)
        with self.assertRaises(ValueError):
            fixed(classification=Provenance.ACTUAL_HISTORICAL)
        with self.assertRaises(ValueError):
            current(kind=SourceKind.HISTORICAL_SNAPSHOT, classification=Provenance.ACTUAL_HISTORICAL)
        historical = current(kind=SourceKind.HISTORICAL_SNAPSHOT, classification=Provenance.ACTUAL_HISTORICAL,
                             effective_at_ms=TIME - 1000, historical_valid_until_ms=TIME + 1000)
        self.assertEqual(rules(provenance=historical).rules.evidence.classification, Provenance.ACTUAL_HISTORICAL)
        with self.assertRaises(ValueError):
            replace(historical, applicable_at_ms=TIME + 1001)

    def test_brackets_exact_caps_and_no_extrapolation(self):
        b = brackets()
        self.assertEqual(b.table.rows[0].cap, D(1000))
        self.assertEqual(b.table.rows[1].cum, D(10))
        self.assertEqual(b.normalized_identity, b.table.identity)
        for value, tier in ((D(0), 0), (D(1000), 0), (D("1000.01"), 1), (D(10000), 1)):
            self.assertEqual(math.select_bracket(value, b.table).bracket_identity, b.table.rows[tier].identity)
        self.assertEqual(math.select_bracket(D(10001), b.table).status, "UNAVAILABLE_BRACKETS")
        self.assertEqual(b.table.evidence.classification, Provenance.CURRENT_RULE_ASSUMPTION)
        self.assertIsNone(b.table.evidence.effective_at)

    def test_notional_coef_changes_effective_values_and_cum_exactly(self):
        raw = bracket_payload("1.5")
        base = brackets(raw, account_specific=True, values_basis="BASE_TIERS")
        effective = brackets(raw, account_specific=True, values_basis="EFFECTIVE_TIERS")
        self.assertEqual(base.notional_coef, D("1.5"))
        self.assertTrue(base.account_specific)
        self.assertEqual((base.table.rows[0].cap, base.table.rows[1].floor, base.table.rows[1].cum),
                         (D(1500), D(1500), D(15)))
        self.assertEqual(math.validate_brackets(base.table), ())
        self.assertEqual(effective.table.rows, effective.raw_brackets)
        self.assertNotEqual(base.identity, effective.identity)
        self.assertNotEqual(base.normalized_identity, effective.normalized_identity)
        self.assertEqual(math.select_bracket(D(1500), base.table).bracket_identity, base.table.rows[0].identity)
        with localcontext() as context:
            context.prec = 1
            precise = brackets(bracket_payload("1.123456789"), account_specific=True, values_basis="BASE_TIERS")
            self.assertEqual(precise.table.rows[1].cum, D("11.23456789"))
            self.assertEqual(math.validate_brackets(precise.table), ())
        with self.assertRaises(ValueError):
            brackets(raw, account_specific=False, values_basis="BASE_TIERS")
        with self.assertRaises(ValueError):
            replace(base, table=effective.table)

    def test_malformed_brackets_integer_leverage_and_continuity(self):
        for key, value in (("initialLeverage", True), ("initialLeverage", 10.5), ("cum", 9),
                           ("notionalFloor", 1001), ("notionalCap", 999), ("maintMarginRatio", "1")):
            payload = bracket_payload()
            payload["brackets"][1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                brackets(payload)
        with self.assertRaises(ValueError):
            brackets(values_basis="UNKNOWN")

    def test_fees_rates_fixed_assumption_and_identity(self):
        policy = fees()
        self.assertEqual((policy.policy.maker_rate, policy.policy.taker_rate), (D(".0002"), D(".0005")))
        self.assertEqual(policy.policy.evidence.classification, Provenance.FIXED_SIMULATION_ASSUMPTION)
        self.assertEqual(policy.normalized_identity, policy.policy.identity)
        self.assertNotEqual(policy.identity, fees(".0003").identity)
        self.assertNotEqual(policy.normalized_identity, fees(".0003").normalized_identity)
        self.assertNotEqual(policy.identity, fees(provenance=current()).identity)
        with self.assertRaises(ValueError):
            fees("-0.1")
        with self.assertRaises(ValueError):
            replace(policy, source=SourceIdentity(current(), HASH))

    def test_unavailable_supplied_snapshots_do_not_become_available(self):
        provenance = current(classification=Provenance.UNAVAILABLE)
        rule_snapshot = rules(provenance=provenance)
        fee_snapshot = fees(provenance=provenance)
        bracket_snapshot = normalize_bracket_snapshot(encoded(bracket_payload()), SYMBOL, provenance,
                                                       account_specific=False, values_basis="EFFECTIVE_TIERS")
        self.assertEqual(math.validate_order(OrderIntent(OrderSide.BUY, D(".2"), None, market=True),
                                            rule_snapshot.rules).status, "UNAVAILABLE_RULE")
        self.assertEqual(math.validate_brackets(bracket_snapshot.table), ("UNAVAILABLE_BRACKETS",))
        top = execution_evidence_snapshot(SCOPE, contract_rules=rule_snapshot, brackets=bracket_snapshot, fees=fee_snapshot)
        for ref in top.components[:3]:
            self.assertIn(Provenance.UNAVAILABLE, ref.provenance)
            self.assertIn("SOURCE_PROVENANCE_UNAVAILABLE", ref.limitations)


class TopLevelEvidenceTests(unittest.TestCase):
    def test_deterministic_partial_identity_provenance_and_missing_components(self):
        empty = execution_evidence_snapshot(SCOPE)
        self.assertEqual(tuple(r.component for r in empty.components), COMPONENTS)
        self.assertTrue(all(r.evidence_identity is None and r.unavailable_reason for r in empty.components))
        partial = execution_evidence_snapshot(SCOPE, contract_rules=rules(), brackets=brackets(), fees=fees())
        self.assertEqual(partial.identity, replace(partial, components=tuple(reversed(partial.components))).identity)
        self.assertEqual(partial.snapshot_sha256, partial.identity)
        self.assertNotEqual(partial.identity, empty.identity)
        self.assertNotEqual(partial.identity, execution_evidence_snapshot(SCOPE, contract_rules=rules(), brackets=brackets(), fees=fees(".0003")).identity)
        ref = next(r for r in partial.components if r.component == "contract_rules")
        self.assertEqual(set(ref.provenance), {Provenance.CURRENT_RULE_ASSUMPTION, Provenance.FIXED_SIMULATION_ASSUMPTION})
        self.assertFalse({f.name for f in fields(ExecutionEvidenceSnapshot)} &
                         {"wallet", "position", "fills", "realized_pnl", "margin_reservations", "ledger"})
        with self.assertRaises(FrozenInstanceError):
            partial.components = ()

    def test_instrument_type_and_missing_reference_validation(self):
        with self.assertRaises(ValueError):
            execution_evidence_snapshot(Scope("binance-usdm:ETHUSDT"), fees=fees())
        with self.assertRaises(ValueError):
            execution_evidence_snapshot(SCOPE, fees=rules())
        with self.assertRaises(ValueError):
            ExecutionEvidenceSnapshot(SCOPE, ())
        with self.assertRaises(ValueError):
            EvidenceReference(INSTRUMENT, "fees", HASH)
        with self.assertRaises(ValueError):
            EvidenceReference(INSTRUMENT, "fees", unavailable_reason=None)

    def test_funding_mark_unavailable_and_material_changes_bound(self):
        event = funding()
        collection = FundingEvidence(SYMBOL, INSTRUMENT, (event,), HASH)
        snapshot = execution_evidence_snapshot(SCOPE, funding=collection)
        ref = next(r for r in snapshot.components if r.component == "funding")
        self.assertIn(f"UNAVAILABLE_FUNDING_MARK:{event.event_identity}", ref.limitations)
        joined = replace(collection, events=(join_settlement_mark(event, exact_mark(event)),))
        self.assertNotEqual(snapshot.identity, execution_evidence_snapshot(SCOPE, funding=joined).identity)
        self.assertEqual(next(r for r in execution_evidence_snapshot(SCOPE, funding=joined).components
                              if r.component == "funding").limitations, ())


if __name__ == "__main__":
    unittest.main()
