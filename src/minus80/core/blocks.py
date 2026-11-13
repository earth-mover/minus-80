"""Compare two readers of the same bytes on a bounded sample of blocks, by SHA-256.

The sample holds the first block, the last block and a seeded set of blocks between them.
The comparison does not find a difference inside an unsampled block.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import numpy as np

from .progress import Progress

#: Reads the bytes ``[start, stop)`` of one source.
RangeReader = Callable[[int, int], bytes]


def sampled_blocks(size: int, block: int, samples: int, seed: int = 0) -> list[int]:
    """The indices of the first block, the last block and ``samples`` blocks between, ascending.

    The sample holds every block when the blocks outside the first and the last number ``samples`` or fewer.
    """
    count = -(-size // block)
    if count <= samples + 2:
        return list(range(count))
    between = np.random.default_rng(seed).choice(np.arange(1, count - 1), samples, replace=False)
    return [0, *sorted(between.tolist()), count - 1]


def compare_blocks(
    read_a: RangeReader, read_b: RangeReader, size: int, *, block: int, samples: int, seed: int = 0,
    progress: Progress | None = None,
) -> tuple[list[int], list[int]]:
    """The sampled block indices and those whose SHA-256 differs between the readers.

    Each reader gets one range of at most ``block`` bytes per sampled block. ``size`` is the length of both sources.
    """
    sampled = sampled_blocks(size, block, samples, seed)
    differing = []
    for index in sampled:
        start, stop = index * block, min((index + 1) * block, size)
        if hashlib.sha256(read_a(start, stop)).digest() != hashlib.sha256(read_b(start, stop)).digest():
            differing.append(index)
        if progress:
            progress.advance()
    return sampled, differing
