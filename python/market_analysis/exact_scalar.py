"""Immutable reduced rational scalars; no implicit decimal rounding or floats."""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from functools import total_ordering
from math import gcd

from .canonical_identity import canonical_digest


@total_ordering
@dataclass(frozen=True, eq=False)
class ExactScalar:
    numerator: int
    denominator: int = 1

    def __post_init__(self):
        if type(self.numerator) is not int or type(self.denominator) is not int:
            raise ValueError("exact scalar requires integer numerator and denominator (no bool)")
        if self.denominator == 0:
            raise ValueError("exact scalar denominator must not be zero")
        n, d = self.numerator, self.denominator
        if d < 0:
            n, d = -n, -d
        factor = gcd(n, d)
        object.__setattr__(self, 'numerator', n // factor)
        object.__setattr__(self, 'denominator', d // factor)

    @classmethod
    def from_decimal(cls, value):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("finite Decimal required")
        return cls(*value.as_integer_ratio())

    @property
    def identity(self):
        return canonical_digest({'type': 'exact-scalar-v1', 'value': self})

    @property
    def decimal_value(self):
        """Exact finite view, or None for a repeating expansion. Never round."""
        d = self.denominator
        twos = fives = 0
        while d % 2 == 0:
            twos += 1
            d //= 2
        while d % 5 == 0:
            fives += 1
            d //= 5
        if d != 1:
            return None
        scale = max(twos, fives)
        coefficient = abs(self.numerator) * 2 ** (scale - twos) * 5 ** (scale - fives)
        return Decimal((int(self.numerator < 0), tuple(map(int, str(coefficient))), -scale))

    def as_integer_ratio(self):
        return self.numerator, self.denominator

    def __bool__(self):
        return self.numerator != 0

    def __hash__(self):
        return hash(Fraction(self.numerator, self.denominator))

    def __eq__(self, other):
        other = _operand(other)
        if other is NotImplemented:
            return NotImplemented
        return (self.numerator, self.denominator) == (other.numerator, other.denominator)

    def __lt__(self, other):
        other = _operand(other)
        if other is NotImplemented:
            return NotImplemented
        return self.numerator * other.denominator < other.numerator * self.denominator

    def __add__(self, other):
        other = _operand(other)
        if other is NotImplemented:
            return NotImplemented
        return ExactScalar(self.numerator * other.denominator + other.numerator * self.denominator,
                           self.denominator * other.denominator)

    __radd__ = __add__

    def __neg__(self):
        return ExactScalar(-self.numerator, self.denominator)

    def copy_negate(self):
        return -self

    def __sub__(self, other):
        other = _operand(other)
        return NotImplemented if other is NotImplemented else self + (-other)

    def __rsub__(self, other):
        other = _operand(other)
        return NotImplemented if other is NotImplemented else other - self

    def __mul__(self, other):
        other = _operand(other)
        if other is NotImplemented:
            return NotImplemented
        return ExactScalar(self.numerator * other.numerator, self.denominator * other.denominator)

    __rmul__ = __mul__

    def __truediv__(self, other):
        other = _operand(other)
        if other is NotImplemented:
            return NotImplemented
        if not other.numerator:
            raise ValueError("zero denominator")
        return ExactScalar(self.numerator * other.denominator, self.denominator * other.numerator)

    def __rtruediv__(self, other):
        other = _operand(other)
        return NotImplemented if other is NotImplemented else other / self


def _operand(value):
    if isinstance(value, ExactScalar):
        return value
    if isinstance(value, Decimal):
        return ExactScalar.from_decimal(value)
    if type(value) is int:
        return ExactScalar(value)
    return NotImplemented


def exact_scalar(value):
    """Financial boundary: accept only finite Decimal or an exact scalar."""
    if isinstance(value, ExactScalar):
        return value
    return ExactScalar.from_decimal(value)


def finite_or_exact(value):
    """Keep the finite Decimal API when possible, otherwise retain the scalar."""
    view = value.decimal_value
    return value if view is None else view


Scalar = Decimal | ExactScalar
