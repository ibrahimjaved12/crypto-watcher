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


def canonical_value(value):
    """Lossless, stable JSON-compatible representation of pure domain values."""
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
