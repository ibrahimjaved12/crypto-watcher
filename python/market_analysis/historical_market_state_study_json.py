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
