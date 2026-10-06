"""Lossless deterministic JSON for #123 scientific artifacts only."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
import json
from json.encoder import encode_basestring_ascii
import math

_NoneType = type(None)
# Exact dataclass type -> field-name tuple, equal to fields(instance) names.
_DATACLASS_FIELD_NAMES = {}
# Exact dataclass type -> ((field name, encoded '"name":' prefix), ...) in sorted order.
_DATACLASS_SORTED_FIELDS = {}


def _dataclass_field_names(value):
    """Cached field names for a dataclass instance, else None."""
    kind = type(value)
    names = _DATACLASS_FIELD_NAMES.get(kind)
    if names is None and is_dataclass(value) and not isinstance(value, type) \
            and not issubclass(kind, type):
        names = _DATACLASS_FIELD_NAMES[kind] = tuple(f.name for f in fields(value))
    return names


def study_json_safe(value):
    kind = type(value)
    if kind is str or kind is int or kind is bool or value is None:
        return value
    if kind is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite float in study artifact")
        return value
    if kind is Decimal:
        if not value.is_finite():
            raise ValueError("nonfinite Decimal in study artifact")
        return str(value)
    if kind is tuple or kind is list:
        return [study_json_safe(v) for v in value]
    if kind is dict:
        return _study_json_safe_mapping(value)
    names = _DATACLASS_FIELD_NAMES.get(kind)
    if names is not None:
        return {name: study_json_safe(getattr(value, name)) for name in names}
    return _study_json_safe_reference(value)


def _study_json_safe_mapping(value):
    if all(isinstance(k, str) for k in value):
        return {k: study_json_safe(v) for k, v in value.items()}
    entries = [[_mapping_key(k), study_json_safe(v)] for k, v in value.items()]
    return sorted(entries, key=lambda entry: (0, entry[0]) if type(entry[0]) is int
                  else (1, canonical_study_json(entry[0])))


def _study_json_safe_reference(value):
    if is_dataclass(value) and not isinstance(value, type):
        _dataclass_field_names(value)
        return {f.name: study_json_safe(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return _study_json_safe_mapping(value)
    if isinstance(value, (tuple, list)):
        return [study_json_safe(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((study_json_safe(v) for v in value), key=canonical_study_json)
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


def _mapping_key(key):
    if key is None or type(key) in (str, bool, int, float):
        return study_json_safe(key)
    # A date/Decimal/tuple key must remain distinct from its string/list value.
    return {'key_type': f'{type(key).__module__}.{type(key).__qualname__}',
            'key_value': study_json_safe(key)}


def canonical_study_json(value):
    return json.dumps(study_json_safe(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _encode_scalar(value, kind):
    """json.dumps spelling of an exact-type scalar, or None if not one."""
    # Mirrors json.dumps(study_json_safe(value), ensure_ascii=True, allow_nan=False).
    if kind is str:
        return encode_basestring_ascii(value)
    if value is None:
        return "null"
    if kind is bool:
        return "true" if value else "false"
    if kind is int:
        return int.__repr__(value)
    if kind is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite float in study artifact")
        return float.__repr__(value)
    if kind is Decimal:
        if not value.is_finite():
            raise ValueError("nonfinite Decimal in study artifact")
        return encode_basestring_ascii(str(value))
    return None


def iter_canonical_study_json(value):
    """Yield precisely the canonical JSON spelling without a full safe graph."""
    kind = type(value)
    # Exact builtin types carry neither hook attribute, so hook precedence holds.
    encoded = _encode_scalar(value, kind)
    if encoded is not None:
        yield encoded
    elif kind is tuple or kind is list:
        yield from _iter_array(value)
    elif kind is dict:
        yield from _iter_mapping(value)
    elif hasattr(value, "__study_json_chunks__"):
        yield from value.__study_json_chunks__()
    elif hasattr(value, "__study_items__"):
        yield from _iter_array(value.__study_items__())
    elif _dataclass_field_names(value) is not None:
        yield from _iter_dataclass(value, kind)
    elif isinstance(value, Mapping):
        yield from _iter_mapping(value)
    elif isinstance(value, (tuple, list)):
        yield from _iter_array(value)
    elif isinstance(value, (set, frozenset)):
        yield from _iter_array(sorted(value, key=canonical_study_json))
    else:
        yield json.dumps(study_json_safe(value), ensure_ascii=True, allow_nan=False,
                         separators=(",", ":"))


def _iter_mapping(value):
    if all(isinstance(key, str) for key in value):
        yield from _iter_object(value)
    else:
        # Only keys/order are materialized; values remain original objects.
        entries = [(_mapping_key(key), item) for key, item in value.items()]
        entries.sort(key=lambda entry: (0, entry[0]) if type(entry[0]) is int
                     else (1, canonical_study_json(entry[0])))
        yield from _iter_array(entries)


def _iter_dataclass(value, kind):
    ordered = _DATACLASS_SORTED_FIELDS.get(kind)
    if ordered is None:
        ordered = _DATACLASS_SORTED_FIELDS[kind] = tuple(
            (name, encode_basestring_ascii(name) + ":")
            for name in sorted(_DATACLASS_FIELD_NAMES[kind]))
    # Read every field before emitting, as the original dict-building path did.
    items = {name: getattr(value, name) for name in _DATACLASS_FIELD_NAMES[kind]}
    yield "{"
    separator = ""
    for name, prefix in ordered:
        yield separator
        yield prefix
        yield from iter_canonical_study_json(items[name])
        separator = ","
    yield "}"


def _iter_array(items):
    yield "["
    separator = ""
    for item in items:
        yield separator
        yield from iter_canonical_study_json(item)
        separator = ","
    yield "]"


def _iter_object(value):
    yield "{"
    separator = ""
    for key in sorted(value):
        yield separator
        yield encode_basestring_ascii(key)
        yield ":"
        yield from iter_canonical_study_json(value[key])
        separator = ","
    yield "}"


def canonical_study_sha256(value):
    import hashlib
    digest = hashlib.sha256()
    for part in iter_canonical_study_json(value):
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()
