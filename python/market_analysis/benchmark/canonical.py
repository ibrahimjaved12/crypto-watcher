"""Canonical serialization and exact numbers for the #182 benchmark harness.

``canonical_bytes`` is the one byte representation every benchmark record is
hashed from: UTF-8 JSON with sorted keys, no whitespace and ASCII escapes.
Only exact JSON types are accepted (str, int, bool, None, list/tuple, dict with
str keys). Floats and every other numeric type are rejected, so a hash can never
depend on float formatting; exact rationals travel as strings through
``exact_to_str`` / ``exact_from_str``.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import re

_EXACT = re.compile(r"-?[0-9]+(?:/[1-9][0-9]*)?\Z")


def _check(value, path: str) -> None:
    kind = type(value)
    if value is None or kind is str or kind is bool or kind is int:
        return
    if kind is list or kind is tuple:
        for position, item in enumerate(value):
            _check(item, f"{path}[{position}]")
        return
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"{path}: dict key {key!r} is {type(key).__name__}, only str keys are canonical")
            _check(item, f"{path}.{key}")
        return
    raise TypeError(f"{path}: {kind.__name__} is not canonical (allowed: str, int, bool, None, list, tuple, "
                    "dict with str keys)")


def canonical_bytes(value) -> bytes:
    """Canonical UTF-8 JSON bytes of ``value``; TypeError names the JSON path of any rejected value."""
    _check(value, "$")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def content_hash(value) -> str:
    """sha256 hex digest of ``canonical_bytes(value)``."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def exact_to_str(value: int | Fraction) -> str:
    """Canonical text: ``n`` when the reduced denominator is 1, else ``n/d`` (sign on the numerator)."""
    if type(value) is int:
        return str(value)
    if type(value) is Fraction:
        # Fraction is always reduced with a positive denominator.
        if value.denominator == 1:
            return str(value.numerator)
        return f"{value.numerator}/{value.denominator}"
    raise TypeError(f"exact value must be int or Fraction, not {type(value).__name__}")


def exact_from_str(text: str) -> Fraction:
    """Parse the canonical form produced by ``exact_to_str``; any other spelling raises ValueError."""
    if type(text) is not str or not _EXACT.match(text):
        raise ValueError(f"not a canonical exact number: {text!r}")
    numerator, _, denominator = text.partition("/")
    value = Fraction(int(numerator), int(denominator or "1"))
    if exact_to_str(value) != text:
        raise ValueError(f"not a canonical exact number (expected {exact_to_str(value)!r}): {text!r}")
    return value
