"""Lossless canonical serialization and hashing for pure Python domain values.

Extracted from the lifecycle domain without changing its byte-level semantics.
ExactScalar is a normalized dataclass and uses the existing dataclass encoding.
"""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from decimal import Decimal
from hashlib import sha256
import json
import math


_NoneType = type(None)
_SCALAR_TYPES = frozenset((str, int, bool, _NoneType))
# Exact dataclass type -> field-name tuple, equal to fields(instance) names.
_DATACLASS_FIELD_NAMES = {}


def _str_of_key(pair):
    return str(pair[0])


def canonical_value(value):
    """Lossless, stable JSON-compatible representation of pure domain values."""
    kind = type(value)
    if kind in _SCALAR_TYPES:
        return value
    if kind is float:
        if not math.isfinite(value):
            raise ValueError("canonical evidence must contain only finite numbers")
        return value
    if kind is tuple or kind is list:
        return [canonical_value(item) for item in value]
    if kind is Decimal:
        return str(value)
    if kind is dict:
        return {str(key): canonical_value(item) for key, item in
                sorted(value.items(), key=_str_of_key)}
    names = _DATACLASS_FIELD_NAMES.get(kind)
    if names is not None:
        return {name: canonical_value(getattr(value, name)) for name in names}
    if is_dataclass(value) and not isinstance(value, type) and not issubclass(kind, type):
        _DATACLASS_FIELD_NAMES[kind] = tuple(field.name for field in fields(value))
    return _canonical_value_reference(value)


def _canonical_value_reference(value):
    """Original exact-semantics implementation; the fast path defers to it."""
    if is_dataclass(value):
        return {field.name: canonical_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): canonical_value(item) for key, item in
                sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [canonical_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("canonical evidence must contain only finite numbers")
        return value
    raise ValueError(f"unsupported canonical evidence type: {type(value).__name__}")


def canonical_digest(payload):
    encoded = json.dumps(canonical_value(payload), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest()
