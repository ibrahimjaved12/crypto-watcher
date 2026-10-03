"""Differential tests: performance fast paths against the original implementations.

Each ``_reference_*`` function below is a verbatim copy of the code it was
optimized from, so these tests pin bit-identical outputs and identical
exception types/messages across randomized inputs (fixed seeds).
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
from types import MappingProxyType, SimpleNamespace, UnionType
from typing import ClassVar, Optional, Union, get_args, get_origin
import enum
import json
import math
import random
import unittest

from market_analysis import movement_metrics
from market_analysis.canonical_identity import canonical_digest, canonical_value
from market_analysis.historical_market_state_study_json import (
    canonical_study_json, canonical_study_sha256, iter_canonical_study_json,
    study_json_safe,
)
from market_analysis.historical_replay import _ReplayHistoricalInputCache
from market_analysis.historical_replay_runtime import (
    _ALLOWED, _dataclass_metadata, _decode,
)
from market_analysis.movement_history import (
    MINUTE_MS, CompletedMovementCandle, IncrementalHistoricalWindowInputs,
    build_historical_window_inputs,
)
from market_analysis.movement_metrics import (
    BreadthSide, ExcludedSymbol, HistoricalWindowInput, MarketMovementConfig,
    Metric, WindowBreadth, _finite_float, _median, _normalized,
)


def _outcome(function, *args):
    try:
        return ("ok", function(*args))
    except Exception as exc:  # noqa: BLE001 - the exception itself is compared
        return ("error", type(exc), str(exc))


# ---------------------------------------------------------------------------
# (a) canonical_identity
# ---------------------------------------------------------------------------

def _reference_canonical_value(value):
    if is_dataclass(value):
        return {field.name: _reference_canonical_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _reference_canonical_value(item) for key, item in
                sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_reference_canonical_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("canonical evidence must contain only finite numbers")
        return value
    raise ValueError(f"unsupported canonical evidence type: {type(value).__name__}")


def _reference_canonical_digest(payload):
    encoded = json.dumps(_reference_canonical_value(payload), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class _Leaf:
    name: str
    amount: Decimal
    ratio: float


@dataclass(frozen=True)
class _LeafChild(_Leaf):
    extra: object = None


@dataclass(frozen=True)
class _Node:
    label: str
    items: object
    nested: object = None
    shared: ClassVar[int] = 3


@dataclass
class _Defaults:
    first: int = 1
    second: str = "two"


class _Color(enum.Enum):
    RED = "red"


class _Text(str):
    pass


class _Number(int):
    pass


_SPECIAL_FLOATS = (-0.0, 0.0, 5e-324, -5e-324, 2.2250738585072014e-308,
                   1e-310, 1.7976931348623157e308, 0.1, 1 / 3)
_SPECIAL_DECIMALS = ("-0", "0E-7", "1E+3", "1.10", "0.000", "-12345678901234567890.123",
                     "1e-30")
_STRINGS = ("", "a", "plain", "quote\"back\\slash", "tab\tnew\nline", "\x00\x1f",
            "é", "日本", "\U0001f600", "\ud800")


def _random_scalar(rng, *, errors):
    choice = rng.randrange(14 if errors else 10)
    if choice == 0:
        return None
    if choice == 1:
        return rng.choice((True, False))
    if choice == 2:
        return rng.choice((0, -1, 1, 2 ** 53 + 1, -(10 ** 30), rng.randint(-10 ** 6, 10 ** 6)))
    if choice == 3:
        return rng.choice((rng.uniform(-1e6, 1e6),) + _SPECIAL_FLOATS)
    if choice == 4:
        return Decimal(rng.choice(_SPECIAL_DECIMALS))
    if choice == 5:
        return Decimal(rng.randint(-10 ** 9, 10 ** 9)).scaleb(rng.randint(-12, 12))
    if choice == 6:
        return rng.choice(_STRINGS)
    if choice == 7:
        return _Text(rng.choice(_STRINGS))
    if choice == 8:
        return _Number(rng.randint(-5, 5))
    if choice == 9:
        return "".join(chr(rng.randint(32, 0x2FF)) for _ in range(rng.randint(0, 6)))
    if choice == 10:
        return rng.choice((math.nan, math.inf, -math.inf))
    if choice == 11:
        return rng.choice((Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")))
    if choice == 12:
        return date(2024, rng.randint(1, 12), rng.randint(1, 28))
    return rng.choice((_Color.RED, object(), {1, 2}, b"bytes"))


def _random_key(rng, *, mixed):
    if not mixed or rng.random() < 0.5:
        return rng.choice(_STRINGS + ("k1", "k2", "1", "None", "True"))
    return rng.choice((0, 1, -7, 10 ** 20, None, True, 2.5, Decimal("1.5"),
                       ("tuple", 1), date(2024, 1, 2)))


def _random_value(rng, depth, *, errors):
    if depth <= 0 or rng.random() < 0.3:
        return _random_scalar(rng, errors=errors)
    choice = rng.randrange(10)
    child = lambda: _random_value(rng, depth - 1, errors=errors)  # noqa: E731
    size = rng.randint(0, 4)
    if choice == 0:
        return tuple(child() for _ in range(size))
    if choice == 1:
        return [child() for _ in range(size)]
    if choice in (2, 3):
        return {_random_key(rng, mixed=choice == 3): child() for _ in range(size)}
    if choice == 4:
        return _Leaf(rng.choice(_STRINGS), Decimal(rng.randint(-99, 99)).scaleb(-2),
                     rng.choice(_SPECIAL_FLOATS))
    if choice == 5:
        return _LeafChild("child", Decimal("1.0"), 2.0, child())
    if choice == 6:
        return _Node(rng.choice(_STRINGS), child(), child())
    if choice == 7:
        return MappingProxyType({_random_key(rng, mixed=False): child() for _ in range(size)})
    if choice == 8:
        return _Defaults(rng.randint(0, 3), rng.choice(_STRINGS))
    return frozenset(rng.choice((1, 2, 3, "a", "b", 2.5, None)) for _ in range(size))


class CanonicalValueEquivalenceTests(unittest.TestCase):
    def assert_same(self, value):
        new = _outcome(canonical_value, value)
        old = _outcome(_reference_canonical_value, value)
        self.assertEqual(new[0], old[0], value)
        if new[0] == "ok":
            self.assertEqual(repr(new[1]), repr(old[1]))
            self.assertEqual(_outcome(canonical_digest, value),
                             _outcome(_reference_canonical_digest, value))
        else:
            self.assertEqual(new, old)
            self.assertEqual(_outcome(canonical_digest, value),
                             _outcome(_reference_canonical_digest, value))

    def test_randomized_structures_match_reference(self):
        rng = random.Random(20261003)
        for _ in range(3000):
            self.assert_same(_random_value(rng, 4, errors=rng.random() < 0.2))

    def test_explicit_edge_cases(self):
        for value in (-0.0, 5e-324, math.nan, math.inf, -math.inf, True, False, None,
                      Decimal("NaN"), _Text("x"), _Number(3), _Color.RED, {1, 2},
                      date(2024, 1, 1), {1: "a", "1": "b"}, {"1": "b", 1: "a"},
                      (math.nan,), [{"k": math.inf}], _Leaf, _Defaults, _Defaults(),
                      _Node("n", (1, 2)), _LeafChild("c", Decimal(1), 1.0, [None]),
                      MappingProxyType({"b": 1, "a": (2.5, Decimal("-0"))})):
            self.assert_same(value)
        # The same dataclass type twice: cached field names must match fields().
        self.assert_same(_Leaf("x", Decimal("1"), 1.0))
        self.assert_same(_Leaf("y", Decimal("2"), math.nan))


# ---------------------------------------------------------------------------
# (b) historical_market_state_study_json
# ---------------------------------------------------------------------------

def _reference_study_json_safe(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _reference_study_json_safe(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        if all(isinstance(k, str) for k in value):
            return {k: _reference_study_json_safe(v) for k, v in value.items()}
        entries = [[_reference_mapping_key(k), _reference_study_json_safe(v)] for k, v in value.items()]
        return sorted(entries, key=lambda entry: (0, entry[0]) if type(entry[0]) is int
                      else (1, _reference_canonical_study_json(entry[0])))
    if isinstance(value, (tuple, list)):
        return [_reference_study_json_safe(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_reference_study_json_safe(v) for v in value), key=_reference_canonical_study_json)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite Decimal in study artifact")
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite float in study artifact")
        return value
    raise TypeError(f"unsupported study value: {type(value).__name__}")


def _reference_mapping_key(key):
    if key is None or type(key) in (str, bool, int, float):
        return _reference_study_json_safe(key)
    return {'key_type': f'{type(key).__module__}.{type(key).__qualname__}',
            'key_value': _reference_study_json_safe(key)}


def _reference_canonical_study_json(value):
    return json.dumps(_reference_study_json_safe(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _reference_iter_canonical_study_json(value):
    if hasattr(value, "__study_json_chunks__"):
        yield from value.__study_json_chunks__()
    elif hasattr(value, "__study_items__"):
        yield from _reference_iter_array(value.__study_items__())
    elif is_dataclass(value) and not isinstance(value, type):
        yield from _reference_iter_object({f.name: getattr(value, f.name) for f in fields(value)})
    elif isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value):
            yield from _reference_iter_object(value)
        else:
            entries = [(_reference_mapping_key(key), item) for key, item in value.items()]
            entries.sort(key=lambda entry: (0, entry[0]) if type(entry[0]) is int
                         else (1, _reference_canonical_study_json(entry[0])))
            yield from _reference_iter_array(entries)
    elif isinstance(value, (tuple, list)):
        yield from _reference_iter_array(value)
    elif isinstance(value, (set, frozenset)):
        yield from _reference_iter_array(sorted(value, key=_reference_canonical_study_json))
    else:
        yield json.dumps(_reference_study_json_safe(value), ensure_ascii=True, allow_nan=False,
                         separators=(",", ":"))


def _reference_iter_array(items):
    yield "["
    separator = ""
    for item in items:
        yield separator
        yield from _reference_iter_canonical_study_json(item)
        separator = ","
    yield "]"


def _reference_iter_object(value):
    yield "{"
    separator = ""
    for key in sorted(value):
        yield separator
        yield json.dumps(key, ensure_ascii=True)
        yield ":"
        yield from _reference_iter_canonical_study_json(value[key])
        separator = ","
    yield "}"


def _joined(iterator_function, value):
    return "".join(iterator_function(value))


class _ChunkHook:
    def __study_json_chunks__(self):
        yield '{"hook":'
        yield "1"
        yield "}"


class _ItemsHook:
    def __init__(self, items):
        self.items = items

    def __study_items__(self):
        return iter(self.items)


@dataclass(frozen=True)
class _HookedLeaf:
    a: int

    def __study_items__(self):
        return iter((self.a, "hooked"))


@dataclass(frozen=True)
class _BothHooks:
    a: int

    def __study_json_chunks__(self):
        yield '"chunks-win"'

    def __study_items__(self):
        return iter(("items-lose",))


class _HookedDict(dict):
    def __study_json_chunks__(self):
        yield '"hooked-dict"'


class StudyJsonEquivalenceTests(unittest.TestCase):
    def assert_same(self, value):
        old_canonical = _outcome(_reference_canonical_study_json, value)
        self.assertEqual(_outcome(canonical_study_json, value), old_canonical, value)
        self.assertEqual(_outcome(study_json_safe, value)[0], old_canonical[0])
        old_iter = _outcome(_joined, _reference_iter_canonical_study_json, value)
        new_iter = _outcome(_joined, iter_canonical_study_json, value)
        self.assertEqual(new_iter, old_iter, value)
        if old_canonical[0] == "ok":
            self.assertEqual(repr(study_json_safe(value)),
                             repr(_reference_study_json_safe(value)))
            self.assertEqual(new_iter, old_canonical)
            self.assertEqual(canonical_study_sha256(value),
                             sha256(old_canonical[1].encode("utf-8")).hexdigest())

    def test_randomized_structures_match_reference(self):
        rng = random.Random(123)
        for _ in range(2500):
            self.assert_same(_random_value(rng, 4, errors=rng.random() < 0.2))

    def test_explicit_edge_cases(self):
        for value in (-0.0, 5e-324, 1e308, 2 ** 64, -(2 ** 70), True, None, "\ud800",
                      "日本", _Text("t"), _Number(4), Decimal("-0"), Decimal("1E+3"),
                      Decimal("NaN"), math.nan, date(2024, 2, 29), _Color.RED,
                      {_Text("b"): 1, "a": 2}, {1: "x", "y": 2, None: 3, 2.5: 4},
                      {(1, 2): "t", Decimal("1"): "d", date(2024, 1, 1): "date"},
                      {math.nan: 1}, frozenset({1, "a", None}), {1.0, 2},
                      _Leaf, _Defaults(), (_Leaf("l", Decimal("1.0"), -0.0),) * 3,
                      MappingProxyType({1: 2})):
            self.assert_same(value)

    def test_hooks_keep_precedence(self):
        rng = random.Random(77)
        for value in (_ChunkHook(), _ItemsHook([1, "a", None]), _HookedLeaf(5),
                      _BothHooks(1), _HookedDict(a=1), [_HookedLeaf(2), {"k": _ChunkHook()}],
                      {"z": _ItemsHook((_BothHooks(3), 2.5)), "a": _HookedDict()}):
            self.assertEqual(_joined(iter_canonical_study_json, value),
                             _joined(_reference_iter_canonical_study_json, value))
        for _ in range(500):
            items = [_random_value(rng, 2, errors=False) for _ in range(rng.randint(0, 3))]
            value = rng.choice((_ItemsHook(items), [_ChunkHook(), items],
                                {"h": _HookedLeaf(rng.randint(0, 9)), "v": items}))
            self.assertEqual(_outcome(_joined, iter_canonical_study_json, value),
                             _outcome(_joined, _reference_iter_canonical_study_json, value))


# ---------------------------------------------------------------------------
# (c) historical_replay_runtime._decode
# ---------------------------------------------------------------------------

def _reference_decode(annotation, value, substitutions=None):
    substitutions = substitutions or {}
    if annotation in substitutions:
        annotation = substitutions[annotation]
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, UnionType):
        if value is None and type(None) in args:
            return None
        choices = [item for item in args if item is not type(None)]
        for choice in choices:
            try:
                return _reference_decode(choice, value, substitutions)
            except (TypeError, ValueError, KeyError, IndexError):
                continue
        raise ValueError("checkpoint union field has invalid value")
    if annotation is type(None):
        if value is not None:
            raise ValueError("checkpoint field must be null")
        return None
    if annotation in (str, int, float, bool):
        if type(value) is not annotation:
            raise ValueError("checkpoint scalar has wrong type")
        return value
    if annotation is Decimal:
        if type(value) is not str:
            raise ValueError("checkpoint Decimal must be a string")
        try:
            result = Decimal(value)
        except Exception as exc:
            raise ValueError("invalid checkpoint Decimal") from exc
        if not result.is_finite():
            raise ValueError("nonfinite checkpoint Decimal")
        return result
    if origin is tuple:
        if type(value) is not list:
            raise ValueError("checkpoint tuple must be an array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_reference_decode(args[0], item, substitutions) for item in value)
        if len(args) != len(value):
            raise ValueError("checkpoint tuple length mismatch")
        return tuple(_reference_decode(kind, item, substitutions)
                     for kind, item in zip(args, value))
    if origin in (Mapping, dict):
        key_kind, value_kind = args
        if key_kind is int:
            if type(value) is not list:
                raise ValueError("checkpoint integer mapping must be pairs")
            pairs = [_reference_decode(tuple[int, value_kind], row, substitutions)
                     for row in value]
            if len({key for key, _ in pairs}) != len(pairs):
                raise ValueError("duplicate checkpoint mapping key")
            return dict(pairs)
        if type(value) is not dict:
            raise ValueError("checkpoint mapping must be an object")
        return {_reference_decode(key_kind, key, substitutions):
                _reference_decode(value_kind, item, substitutions)
                for key, item in value.items()}
    cls = origin or annotation
    if cls in _ALLOWED and is_dataclass(cls):
        if type(value) is not dict:
            raise ValueError("checkpoint dataclass must be an object")
        declared, names, hints = _dataclass_metadata(cls)
        if set(value) != names:
            raise ValueError(f"checkpoint {cls.__name__} fields mismatch")
        local = dict(substitutions)
        if origin is not None:
            local.update(zip(cls.__parameters__, args))
        kwargs = {item.name: _reference_decode(hints[item.name], value[item.name], local)
                  for item in declared}
        return cls(**kwargs)
    raise ValueError(f"unsupported checkpoint type: {annotation}")


def _breadth_json(rng):
    return {"count": rng.choice((0, 3, 1.5, "3", None)),
            "fraction": rng.choice((0.5, 1, None, "0.5", 0.0))}


def _metric_json(rng, inner):
    return {"available": rng.choice((True, False, 1, None)),
            "value": inner(rng), "reason": rng.choice((None, "R", 5))}


_JSON_POOL = (None, True, False, 0, 1, -5, 1.5, -0.0, "x", "", "1.5", "-0", "NaN",
              "Infinity", "1e999999999", "abc", [], [1, 2], [1, "a"], ["a", 1],
              [[1, "a"], [2, "b"]], [[1, "a"], [1, "b"]], [[1, "a", 3]], [[True, "a"]],
              {}, {"a": 1}, {"a": "1.5"}, {"count": 1, "fraction": 0.5},
              {"count": 1, "fraction": 0.5, "extra": 1}, {"symbol": "BTC", "reasons": ["a"]},
              {"symbol": "BTC", "reasons": "a"},
              {"available": True, "value": 1.5, "reason": None},
              {"available": True, "value": [1, 2], "reason": None},
              {"available": False, "value": None, "reason": "MISSING"})

_ANNOTATIONS = (
    int, str, float, bool, Decimal, type(None), int | None, Optional[str],
    Union[int, str], Union[str, int], str | int, float | int | None,
    tuple[int, ...], tuple[str, int], tuple[()], tuple[Decimal, ...],
    Mapping[int, str], dict[str, int], Mapping[str, Decimal], dict[int, tuple[int, ...]],
    Metric[float], Metric[int], Metric[BreadthSide], Metric[tuple[int, ...]],
    Metric[Decimal] | None, BreadthSide, ExcludedSymbol, tuple[ExcludedSymbol, ...],
    Union[Mapping[str, float | int], BreadthSide], Union[BreadthSide, Mapping[str, float | int]],
    list[int], set, dict, Mapping, object, _Leaf,
)


class DecodeEquivalenceTests(unittest.TestCase):
    def assert_same(self, annotation, value):
        new = _outcome(_decode, annotation, value)
        old = _outcome(_reference_decode, annotation, value)
        self.assertEqual(new[0], old[0], (annotation, value))
        if new[0] == "ok":
            self.assertEqual(type(new[1]), type(old[1]))
            self.assertEqual(repr(new[1]), repr(old[1]))
        else:
            self.assertEqual(new, old, (annotation, value))

    def test_pool_against_every_annotation(self):
        for annotation in _ANNOTATIONS:
            for value in _JSON_POOL:
                self.assert_same(annotation, value)

    def test_randomized_nested_values(self):
        rng = random.Random(9)
        for _ in range(1500):
            choice = rng.randrange(4)
            if choice == 0:
                annotation = Metric[BreadthSide]
                value = _metric_json(rng, _breadth_json)
            elif choice == 1:
                annotation = tuple[Metric[float] | None, ...]
                value = [rng.choice((None, _metric_json(rng, lambda r: r.choice((1.5, 1, None, "x")))))
                         for _ in range(rng.randint(0, 3))]
            elif choice == 2:
                annotation = Mapping[int, Metric[Decimal]]
                value = [[rng.choice((1, 2, 3, "1")),
                          _metric_json(rng, lambda r: r.choice(("1.25", "NaN", 1, None)))]
                         for _ in range(rng.randint(0, 3))]
            else:
                annotation = rng.choice(_ANNOTATIONS)
                value = rng.choice(_JSON_POOL)
            self.assert_same(annotation, value)

    def test_union_order_is_per_annotation_object(self):
        # typing considers these equal; each must still try its own order.
        value = {"count": 1, "fraction": 0.5}
        first = Union[Mapping[str, float | int], BreadthSide]
        second = Union[BreadthSide, Mapping[str, float | int]]
        self.assertEqual(first, second)
        self.assertEqual(_decode(first, value), {"count": 1, "fraction": 0.5})
        self.assertEqual(_decode(second, value), BreadthSide(1, 0.5))
        self.assertEqual(_decode(first, value), _reference_decode(first, value))

    def test_round_trip_through_canonical_json(self):
        rng = random.Random(4)
        for _ in range(200):
            def side():
                return Metric.present(BreadthSide(rng.randint(0, 9), rng.random()))
            breadth = WindowBreadth(True, rng.choice((None, "R")), rng.randint(0, 9),
                                    side(), side(), Metric.missing("NONE"), side(), side())
            excluded = ExcludedSymbol("BTC", tuple(rng.choice("ABC") for _ in range(3)))
            for annotation, original in ((WindowBreadth, breadth), (ExcludedSymbol, excluded)):
                raw = json.loads(canonical_study_json(original))
                self.assertEqual(_decode(annotation, raw), original)
                self.assert_same(annotation, raw)


# ---------------------------------------------------------------------------
# (d) movement_metrics._normalized
# ---------------------------------------------------------------------------

def _reference_normalized(current, historical, config):
    absent = Metric.missing("INSUFFICIENT_NORMALIZATION_HISTORY")
    if historical is None or historical.usable_coverage_ms < config.minimum_historical_coverage_ms or not historical.returns:
        return absent, absent, absent, "INSUFFICIENT_NORMALIZATION_HISTORY"
    values = tuple(_finite_float(value) for value in historical.returns)
    if any(value is None for value in values):
        absent = Metric.missing("INVALID_NORMALIZATION_HISTORY")
        return absent, absent, absent, "INVALID_NORMALIZATION_HISTORY"
    center = _median(values)
    mad = _median(abs(value - center) for value in values)
    if not math.isfinite(center) or not math.isfinite(mad):
        absent = Metric.missing("INVALID_NORMALIZATION_HISTORY")
        return absent, absent, absent, "INVALID_NORMALIZATION_HISTORY"
    if mad <= 0:
        absent = Metric.missing("NORMALIZATION_MAD_UNAVAILABLE")
        return Metric.present(center), absent, absent, "NORMALIZATION_MAD_UNAVAILABLE"
    if current is None:
        return Metric.present(center), Metric.present(mad), Metric.missing("SYMBOL_EXCLUDED"), None
    z = 0.6745 * ((current - center) / mad)
    if not math.isfinite(z):
        absent = Metric.missing("INVALID_NORMALIZATION_HISTORY")
        return Metric.present(center), Metric.present(mad), absent, "INVALID_NORMALIZATION_HISTORY"
    return Metric.present(center), Metric.present(mad), Metric.present(z), None


def _random_returns(rng):
    choice = rng.randrange(7)
    size = rng.choice((0, 1, 2, 3, 4, 7, 20))
    if choice == 0:
        return tuple(rng.gauss(0, 0.01) for _ in range(size))
    if choice == 1:
        return (rng.choice((0.0, 0.001, -0.0)),) * size
    if choice == 2:
        return tuple(rng.choice((1, 2, 3, 0.5)) for _ in range(size))
    if choice == 3:
        return tuple(rng.choice((1e308, -1e308, 1.7976931348623157e308, 5e-324))
                     for _ in range(size))
    if choice == 4:
        values = [rng.gauss(0, 1) for _ in range(size)]
        if values:
            values[rng.randrange(len(values))] = rng.choice((math.nan, math.inf, -math.inf))
        return tuple(values)
    if choice == 5:
        return tuple(rng.choice((True, 1.0, Decimal("1"), 10 ** 400, 2)) for _ in range(size))
    return tuple(rng.choice((0.01, 0.02)) for _ in range(size))


class NormalizedEquivalenceTests(unittest.TestCase):
    CONFIGS = (MarketMovementConfig(), MarketMovementConfig(
        historical_lookback_ms=30 * MINUTE_MS, minimum_historical_coverage_ms=MINUTE_MS))

    def assert_same(self, current, historical, config):
        expected = _outcome(_reference_normalized, current, historical, config)
        self.assertEqual(repr(_outcome(_normalized, current, historical, config)), repr(expected))
        # Second call hits the memo.
        self.assertEqual(repr(_outcome(_normalized, current, historical, config)), repr(expected))

    def test_randomized_histories_with_and_without_memo(self):
        rng = random.Random(55)
        for _ in range(1500):
            config = rng.choice(self.CONFIGS)
            historical = HistoricalWindowInput(
                _random_returns(rng),
                rng.choice((0, MINUTE_MS, config.minimum_historical_coverage_ms,
                            config.minimum_historical_coverage_ms - 1, 10 ** 12)),
                ())
            current = rng.choice((None, 0.0, -0.0, rng.gauss(0, 0.02), 1e308, math.nan, math.inf))
            self.assert_same(current, historical, config)
            movement_metrics._NORMALIZATION_SUMMARIES.clear()
            self.assert_same(current, historical, config)
            # A different current value on a cached history still recomputes z.
            self.assert_same(rng.gauss(0, 0.02), historical, config)

    def test_untyped_inputs_bypass_memo(self):
        config = self.CONFIGS[1]
        duck = SimpleNamespace(returns=[0.01, 0.03, -0.02], usable_coverage_ms=10 ** 9)
        self.assert_same(0.02, duck, config)
        duck.returns = [0.5, 0.5, 0.5]
        self.assert_same(0.02, duck, config)
        self.assert_same(0.02, None, config)

    def test_memo_is_bounded(self):
        config = self.CONFIGS[0]
        for index in range(movement_metrics._NORMALIZATION_SUMMARY_LIMIT + 10):
            _normalized(0.0, HistoricalWindowInput((float(index), 0.0), 10 ** 12, ()), config)
        self.assertLessEqual(len(movement_metrics._NORMALIZATION_SUMMARIES),
                             movement_metrics._NORMALIZATION_SUMMARY_LIMIT)


# ---------------------------------------------------------------------------
# (e) incremental build_historical_window_inputs
# ---------------------------------------------------------------------------

_START = 1_800_000_000_000 - 1_800_000_000_000 % 3_600_000
_SMALL_CONFIG = MarketMovementConfig(historical_lookback_ms=30 * MINUTE_MS,
                                     minimum_historical_coverage_ms=MINUTE_MS,
                                     rvol_comparison_windows=3)


class _ReferenceReplayCache:
    """The pre-change _ReplayHistoricalInputCache behavior."""

    def __init__(self):
        from market_analysis.historical_replay import _eligible_historical_end_ranges
        self._ranges = _eligible_historical_end_ranges
        self._entries = {}

    def get(self, symbol, candles, boundary, config, generation):
        key = (generation, self._ranges(boundary, config.historical_lookback_ms))
        entry = self._entries.get(symbol)
        if entry is not None and entry[0] == key:
            return entry[1]
        result = build_historical_window_inputs(candles, boundary, config)
        self._entries[symbol] = (key, result)
        return result


def _candle(rng, open_time, *, close=None):
    if close is None:
        close = Decimal(rng.randint(9_000, 11_000)) / 100
    return CompletedMovementCandle(open_time, close, Decimal(rng.randint(0, 50)),
                                   Decimal(rng.randint(0, 50_000)) / 10)


def _events(rng, minutes, *, gap=0.0, duplicate=0.0, conflict=0.0, late=0.0,
            invalid=0.0, foreign=0.0):
    events = []
    for index in range(minutes):
        open_time = _START + index * MINUTE_MS
        if rng.random() < gap:
            continue
        candle = _candle(rng, open_time)
        arrival = open_time + MINUTE_MS + rng.randint(0, 8_000)
        if rng.random() < late:
            arrival += rng.randint(1, 12) * MINUTE_MS
        events.append((arrival, candle))
        if rng.random() < duplicate:
            events.append((arrival + rng.randint(0, 3 * MINUTE_MS),
                           CompletedMovementCandle(candle.open_time_ms, candle.close,
                                                   candle.volume, candle.quote_volume)))
        if rng.random() < conflict:
            events.append((arrival + rng.randint(0, 3 * MINUTE_MS),
                           _candle(rng, open_time, close=candle.close + 1)))
        if rng.random() < invalid:
            events.append((arrival, _candle(rng, open_time, close=Decimal(0))))
        if rng.random() < foreign:
            events.append((arrival, ("not", "a", "candle")))
    events.sort(key=lambda event: event[0])
    return events


class IncrementalWindowInputTests(unittest.TestCase):
    def assert_same(self, builder, candles, boundary, config, generation):
        expected = _outcome(build_historical_window_inputs, candles, boundary, config)
        actual = _outcome(builder.build, candles, boundary, config, generation)
        self.assertEqual(actual[0], expected[0], boundary)
        if expected[0] == "ok":
            self.assertEqual(actual[1], expected[1])
            self.assertEqual(repr(actual[1]), repr(expected[1]))
        else:
            self.assertEqual(actual, expected)

    def replay(self, rng, events, config, *, backward=0.0, misaligned=0.0,
               steps=(5_000, 10_000, 30_000, 60_000), end=None, first_boundary=None):
        builder = IncrementalHistoricalWindowInputs()
        visible = []
        generation = index = 0
        boundary = _START if first_boundary is None else first_boundary
        end = end if end is not None else events[-1][0] + 5 * MINUTE_MS
        while boundary <= end:
            while index < len(events) and events[index][0] <= boundary + 2_000:
                visible.append(events[index][1])
                generation += 1
                index += 1
            call_boundary = boundary
            if rng.random() < backward:
                call_boundary = max(0, boundary - rng.randint(1, 30) * 5_000)
            if rng.random() < misaligned:
                call_boundary += rng.choice((1, 2_500, -1))
            self.assert_same(builder, visible, call_boundary, config, generation)
            boundary += rng.choice(steps)
        return builder

    def test_contiguous_stream_uses_incremental_state(self):
        rng = random.Random(1)
        builder = self.replay(rng, _events(rng, 150), _SMALL_CONFIG)
        self.assertIsNotNone(builder._state)

    def test_gaps_and_late_arrivals(self):
        rng = random.Random(2)
        for _ in range(3):
            self.replay(rng, _events(rng, 120, gap=0.03, late=0.05), _SMALL_CONFIG)

    def test_identical_and_conflicting_duplicates(self):
        rng = random.Random(3)
        self.replay(rng, _events(rng, 120, duplicate=0.05), _SMALL_CONFIG)
        self.replay(rng, _events(rng, 120, duplicate=0.02, conflict=0.01), _SMALL_CONFIG)

    def test_out_of_order_backward_and_invalid_boundaries(self):
        rng = random.Random(4)
        self.replay(rng, _events(rng, 120, late=0.2), _SMALL_CONFIG, backward=0.05,
                    misaligned=0.02)

    def test_invalid_candles(self):
        rng = random.Random(5)
        self.replay(rng, _events(rng, 80, invalid=0.01), _SMALL_CONFIG)
        self.replay(rng, _events(rng, 80, foreign=0.01), _SMALL_CONFIG)

    def test_list_replacement_and_generation_mismatch(self):
        rng = random.Random(6)
        events = _events(rng, 60)
        builder = IncrementalHistoricalWindowInputs()
        visible = [candle for _, candle in events[:30]]
        boundary = _START + 40 * MINUTE_MS
        self.assert_same(builder, visible, boundary, _SMALL_CONFIG, 30)
        copied = list(visible) + [candle for _, candle in events[30:40]]
        self.assert_same(builder, copied, boundary + 10 * MINUTE_MS, _SMALL_CONFIG, 40)
        copied.append(events[40][1])
        self.assert_same(builder, copied, boundary + 11 * MINUTE_MS, _SMALL_CONFIG, 99)
        other_config = MarketMovementConfig(historical_lookback_ms=30 * MINUTE_MS,
                                            minimum_historical_coverage_ms=MINUTE_MS,
                                            rvol_comparison_windows=3)
        self.assert_same(builder, copied, boundary + 12 * MINUTE_MS, other_config, 99)
        copied[-1] = events[41][1]
        self.assert_same(builder, copied, boundary + 13 * MINUTE_MS, other_config, 99)

    def test_seven_day_lookback_trimming(self):
        rng = random.Random(7)
        config = MarketMovementConfig()
        # An unbroken run longer than the lookback, so the front rows are trimmed.
        minutes = 7 * 24 * 60 + 90
        events = _events(rng, minutes)
        first = _START + (minutes - 100) * MINUTE_MS
        self.replay(rng, events, config, first_boundary=first,
                    steps=(5 * MINUTE_MS, 7 * MINUTE_MS, 15 * MINUTE_MS + 5_000))

    def test_replay_cache_matches_pre_change_cache(self):
        rng = random.Random(8)
        events = _events(rng, 120, gap=0.02, late=0.05, duplicate=0.02)
        new, old = _ReplayHistoricalInputCache(), _ReferenceReplayCache()
        visible = []
        generation = index = 0
        for boundary in range(_START, events[-1][0] + 5 * MINUTE_MS, 5_000):
            while index < len(events) and events[index][0] <= boundary + 2_000:
                visible.append(events[index][1])
                generation += 1
                index += 1
            expected = old.get("BTCUSDT", visible, boundary, _SMALL_CONFIG, generation)
            actual = new.get("BTCUSDT", visible, boundary, _SMALL_CONFIG, generation)
            self.assertEqual(repr(actual), repr(expected))


if __name__ == "__main__":
    unittest.main()
