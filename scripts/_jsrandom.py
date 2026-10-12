"""A faithful port of the JavaScript ``mulberry32`` PRNG used by the mock generator.

The fixture dataset is seeded, so reproducing the exact sequence is what keeps the
generated data identical across the port: the RNG decides which operator each alert
definition gets and which generic message some definitions carry, and a different sequence
would silently reshuffle the realistic teams' data.

JavaScript semantics have to be emulated exactly: ``|0`` truncates to a signed 32-bit
integer, ``>>>`` is an unsigned shift, and ``Math.imul`` is a signed 32-bit multiply.
Python integers are arbitrary precision, so each step masks explicitly.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

__all__ = ["Random", "mulberry32"]

T = TypeVar("T")

_MASK = 0xFFFFFFFF


def _to_int32(value: int) -> int:
    value &= _MASK
    return value - 0x100000000 if value >= 0x80000000 else value


def _imul(a: int, b: int) -> int:
    """JavaScript ``Math.imul``: C-like 32-bit signed integer multiplication."""
    return _to_int32((_to_int32(a) * _to_int32(b)) & _MASK)


def mulberry32(seed: int) -> Callable[[], float]:
    """Return a function yielding the same float sequence as the JavaScript original."""
    state = _to_int32(seed)

    def next_float() -> float:
        nonlocal state
        state = _to_int32(state + 0x6D2B79F5)
        t = _imul(state ^ ((state & _MASK) >> 15), 1 | state)
        t = _to_int32(_to_int32(t + _imul(t ^ ((t & _MASK) >> 7), 61 | t)) ^ t)
        return ((t ^ ((t & _MASK) >> 14)) & _MASK) / 4294967296

    return next_float


class Random:
    """The seeded helpers the generator uses, in the same call order as the original."""

    def __init__(self, seed: int) -> None:
        self._next = mulberry32(seed)

    def pick(self, items: Sequence[T]) -> T:
        return items[int(self._next() * len(items))]

    def integer(self, minimum: int, maximum: int) -> int:
        return minimum + int(self._next() * (maximum - minimum + 1))
