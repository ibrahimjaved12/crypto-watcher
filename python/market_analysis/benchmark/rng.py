"""Counter-based random words for the #182 benchmark harness.

Word ``i`` of ``(seed, stream)`` is a pure function of its coordinates: bytes
``[8*(i % 4), 8*(i % 4) + 8)`` (big endian) of
``sha256(f"{seed}|{stream}|{i // 4}")``. Any slice of words is therefore
identical however the work is sharded, and nothing depends on numpy.random,
the random module, platform or thread count.
"""
from __future__ import annotations

import hashlib

import numpy as np

WORDS_PER_BLOCK = 4


def _check_stream(stream: str) -> None:
    if type(stream) is not str or not stream or "|" in stream or not stream.isascii():
        raise ValueError(f"stream must be a non-empty ASCII string without '|', got {stream!r}")


def u64_words(seed: int, stream: str, start: int, count: int) -> np.ndarray:
    """``count`` uint64 words starting at global word index ``start``."""
    if type(seed) is not int or seed < 0:
        raise ValueError(f"seed must be an int >= 0, got {seed!r}")
    _check_stream(stream)
    if type(start) is not int or start < 0 or type(count) is not int or count < 0:
        raise ValueError(f"start and count must be ints >= 0, got {start!r}, {count!r}")
    if count == 0:
        return np.empty(0, dtype=np.uint64)
    first = start // WORDS_PER_BLOCK
    last = (start + count - 1) // WORDS_PER_BLOCK
    digests = b"".join(hashlib.sha256(f"{seed}|{stream}|{block}".encode("ascii")).digest()
                       for block in range(first, last + 1))
    words = np.frombuffer(digests, dtype=">u8").astype(np.uint64)
    offset = start - first * WORDS_PER_BLOCK
    return words[offset:offset + count]
