"""Lossless deterministic JSON for #123 scientific artifacts only."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
import json
import math


def study_json_safe(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: study_json_safe(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        if all(isinstance(k, str) for k in value):
            return {k: study_json_safe(v) for k, v in value.items()}
        entries = [[_mapping_key(k), study_json_safe(v)] for k, v in value.items()]
        return sorted(entries, key=lambda entry: (0, entry[0]) if type(entry[0]) is int
                      else (1, canonical_study_json(entry[0])))
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


def iter_canonical_study_json(value):
    """Yield precisely the canonical JSON spelling without a full safe graph."""
    if hasattr(value, "__study_json_chunks__"):
        yield from value.__study_json_chunks__()
    elif hasattr(value, "__study_items__"):
        yield from _iter_array(value.__study_items__())
    elif is_dataclass(value) and not isinstance(value, type):
        yield from _iter_object({f.name: getattr(value, f.name) for f in fields(value)})
    elif isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value):
            yield from _iter_object(value)
        else:
            # Only keys/order are materialized; values remain original objects.
            entries = [(_mapping_key(key), item) for key, item in value.items()]
            entries.sort(key=lambda entry: (0, entry[0]) if type(entry[0]) is int
                         else (1, canonical_study_json(entry[0])))
            yield from _iter_array(entries)
    elif isinstance(value, (tuple, list)):
        yield from _iter_array(value)
    elif isinstance(value, (set, frozenset)):
        yield from _iter_array(sorted(value, key=canonical_study_json))
    else:
        yield json.dumps(study_json_safe(value), ensure_ascii=True, allow_nan=False,
                         separators=(",", ":"))


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
        yield json.dumps(key, ensure_ascii=True)
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
