"""Address an ND2's frames over the network without moving its pixels.

An ND2 file ends with a chunk map: a flat table of ``name -> (offset, size)``
covering every chunk in the file, including each image frame. So a reader that
can fetch the tail can address the whole file arithmetically, with no walk from
one frame to the next. That is the whole reason a 300 MB file indexes in four
range requests.

The parsing is ``nd2``'s. This module fetches ranges, hands them to that
library, and turns what it reports into VirtualiZarr ``ManifestArray``\ s.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
from collections.abc import Sequence

import numpy as np
from minus80.core import as_url
from minus80.core.fetch import object_size, write_ranges
from minus80.core.manifest import channel_last_transpose, manifest_array
from virtualizarr.manifests import ManifestArray

#: Bytes taken from the start of the file. The version chunk lives here.
HEAD_BLOCK = 64 << 10

#: Bytes taken from the end. The chunk map, its signature, and every metadata
#: chunk of the files seen so far fit inside this.
TAIL_BLOCK = 512 << 10

#: Two metadata chunks closer together than this are fetched as one range.
#: Reading a megabyte of padding costs less than a second round trip to a
#: server 90 ms away.
COALESCE_GAP = 1 << 20

#: A chunk's pixel data starts this far past the offset the chunk map records:
#: a fixed-size chunk header, the chunk's name, and padding to a page boundary.
#: ``nd2`` exposes the alignment it uses, so the number is not written here.
try:  # pragma: no cover - the constant has moved between nd2 releases
    from nd2._parse._chunk_decode import CHUNK_ALIGNMENT as FRAME_HEADER_BYTES
except ImportError:  # pragma: no cover
    FRAME_HEADER_BYTES = 4096


class UnsupportedND2(Exception):
    """The file is an ND2 whose frames cannot be referenced as stored bytes."""


@dataclasses.dataclass(frozen=True)
class ND2Index:
    """Everything one ND2 states about itself, plus where each frame's bytes are.

    ``frame_offsets`` is one byte offset per stage position, in the file's own
    order, pointing at the first pixel byte rather than at the chunk header.
    Each frame is ``(y, x, c)`` and C-contiguous, which is what lets
    :meth:`position_array` present it as ``(c, y, x)`` without moving a byte.
    """

    url: str
    size: int
    positions: int
    channels: int
    height: int
    width: int
    dtype: np.dtype
    frame_offsets: tuple[int, ...]
    frame_bytes: int
    channel_names: tuple[str, ...]
    pixel_size_um: dict[str, float]
    stage_positions_um: tuple[tuple[float, float, float], ...]
    objective: str | None
    text_info: dict
    requests: int
    fetched_bytes: int

    @property
    def frame_shape(self) -> tuple[int, int, int]:
        """One position as an OME-Zarr image would have it: ``(c, y, x)``."""
        return (self.channels, self.height, self.width)

    @property
    def codecs(self) -> list[dict]:
        """The codec chain that reads interleaved bytes as ``(c, y, x)``.

        Zarr v3's ``transpose`` is an array-to-array codec: it declares that the
        stored order is a permutation of the array's, so a conformant reader
        does the permutation and the stored bytes are never rewritten. Here the
        array is ``(c, y, x)`` and the file holds ``(y, x, c)``.
        """
        return _transpose_codecs(3)

    def position_array(self, position: int) -> ManifestArray:
        """One stage position as a ``(c, y, x)`` array of a single chunk.

        The chunk is the frame as the file stores it. Chunking is never chosen
        here: a chunk of the store is a chunk of the source, so no read of this
        array asks the server for a range the file does not already have.
        """
        return _frames(
            self.url, shape=self.frame_shape, chunk_shape=self.frame_shape,
            offsets=np.array([[[self.frame_offsets[position]]]], dtype="u8"),
            frame_bytes=self.frame_bytes, dtype=self.dtype, codecs=self.codecs,
            dimension_names=("c", "y", "x"))

    def stacked_array(self, *, attrs: dict | None = None) -> ManifestArray:
        """Every position of the file as one ``(position, c, y, x)`` array.

        The same bytes as :meth:`position_array`, addressed once more. A chunk is
        still one frame, so the second presentation costs references and no
        storage.
        """
        return _frames(
            self.url, shape=(self.positions, *self.frame_shape),
            chunk_shape=(1, *self.frame_shape),
            offsets=np.array(self.frame_offsets, dtype="u8").reshape(-1, 1, 1, 1),
            frame_bytes=self.frame_bytes, dtype=self.dtype,
            codecs=_transpose_codecs(4),
            dimension_names=("position", "c", "y", "x"), attrs=attrs)


def _frames(url: str, *, shape, chunk_shape, offsets: np.ndarray, frame_bytes: int,
            dtype, codecs, dimension_names, attrs: dict | None = None) -> ManifestArray:
    """A virtual array whose every chunk is one whole frame of ``url``."""
    return manifest_array(
        url, offsets, np.full(offsets.shape, frame_bytes, dtype=np.uint64),
        shape=shape, chunk_shape=chunk_shape, dtype=dtype, codecs=codecs,
        dimension_names=dimension_names, attributes=attrs)


def _transpose_codecs(rank: int) -> list[dict]:
    """``transpose`` then ``bytes`` for a chunk whose channel axis is stored last."""
    return [channel_last_transpose(rank), {"name": "bytes", "configuration": {"endian": "little"}}]


@dataclasses.dataclass(frozen=True)
class _Fetched:
    """What indexing one file cost: the URL, its length, and the requests and bytes it took."""

    url: str
    size: int
    requests: int
    bytes: int


def _coalesce(ranges: Sequence[tuple[int, int]], gap: int = COALESCE_GAP):
    merged: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if merged and lo - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


def _metadata_ranges(chunkmap, covered: Sequence[tuple[int, int]]):
    """Byte ranges of every chunk that is not an image frame, less what is held.

    A chunk map records a chunk's payload size but not its header, and records
    ``-1`` for a few chunks whose size it does not know, so each range is padded
    by a header's worth and short entries are given a fixed allowance.
    """
    wanted = [
        (offset, offset + (size if size > 0 else HEAD_BLOCK) + FRAME_HEADER_BYTES)
        for name, (offset, size) in chunkmap.items()
        if not name.startswith(b"ImageDataSeq|")
    ]
    for lo, hi in _coalesce(wanted):
        if not any(lo >= clo and hi <= chi for clo, chi in covered):
            yield lo, hi


def _check_loops(f, ranges) -> None:
    """Refuse a file whose frames are not one per stage position.

    Each frame becomes a position of :meth:`ND2Index.stacked_array` and an array
    of its own, so a file that loops over time or z as well would have its
    timepoints presented as fields of view -- wrong, and wrong silently, since
    the shapes are indistinguishable. An acquisition with those axes is
    describable, but it is not what this parser builds today.
    """
    kinds = [getattr(loop, "type", type(loop).__name__) for loop in f.experiment]
    if [k for k in kinds if k != "XYPosLoop"]:
        raise UnsupportedND2(
            f"{ranges.url}: the acquisition loops over {', '.join(kinds)}; this parser "
            "addresses one frame per stage position and would present "
            "the other axes as fields of view"
        )


def _stage_positions(nd2_file) -> tuple[tuple[float, float, float], ...]:
    """Each position's stage coordinates in micrometres, in the file's order.

    The positions come from the acquisition's own ``XYPosLoop``. A file with no
    such loop has one field of view and no coordinate to give.
    """
    for loop in nd2_file.experiment:
        points = getattr(getattr(loop, "parameters", None), "points", None)
        if points:
            return tuple(tuple(float(v) for v in p.stagePositionUm) for p in points)
    return ()


def index_nd2(url: str) -> ND2Index:
    """Read one ND2's metadata and frame addresses over bounded range requests.

    Costs four requests against a remote file: one for its length, one block
    from each end, and one coalesced range for the metadata chunks that fall
    between them. No pixel byte is fetched.

    Raises :class:`UnsupportedND2` for a file whose frames are compressed, or
    whose rows are padded, because neither can be referenced as stored bytes.
    """
    import nd2
    from nd2._parse._chunk_decode import get_chunkmap

    url = as_url(url)
    size = object_size(url)
    requests, fetched = 1, 0
    with tempfile.TemporaryDirectory() as tmp:
        # ``nd2.ND2File`` takes a path and memory-maps it, so the whole public
        # API needs a file that is the right length and has the right bytes
        # wherever it looks. Fetching only the metadata chunks into an
        # otherwise empty file gives it exactly that: every frame region stays
        # a hole, and nothing here reads one.
        local = os.path.join(tmp, "source.nd2")
        with open(local, "wb") as sparse:
            sparse.truncate(size)
            blocks = [(0, min(HEAD_BLOCK, size)), (max(0, size - TAIL_BLOCK), size)]
            fetched += write_ranges(url, blocks, sparse, progress=False)
            requests += len(blocks)
            sparse.flush()

            with open(local, "rb") as fh:
                chunkmap = get_chunkmap(fh)
            # Each range is padded past its chunk's payload, so the last one
            # can run past the end of the file.
            wanted = [(lo, min(hi, size)) for lo, hi in _metadata_ranges(chunkmap, blocks)]
            fetched += write_ranges(url, wanted, sparse, progress=False)
            requests += len(wanted)

        with nd2.ND2File(local) as f:
            return _describe(f, chunkmap, _Fetched(url, size, requests, fetched))


def _describe(f, chunkmap, ranges) -> ND2Index:
    attrs = f.attributes
    if attrs.compressionType is not None:
        raise UnsupportedND2(
            f"{ranges.url}: frames are {attrs.compressionType}-compressed; there is "
            "no Zarr codec for ND2's compression, so its bytes cannot be referenced"
        )
    channels = attrs.componentCount
    height, width = attrs.heightPx, attrs.widthPx
    dtype = np.dtype(f.dtype)
    if attrs.widthBytes != width * channels * dtype.itemsize:
        raise UnsupportedND2(
            f"{ranges.url}: a row occupies {attrs.widthBytes} bytes where "
            f"{width} x {channels} x {dtype.itemsize} pixels need "
            f"{width * channels * dtype.itemsize}; padded rows are not a Zarr chunk"
        )

    _check_loops(f, ranges)
    offsets = _frame_offsets(f, chunkmap)
    voxel = f.voxel_size()
    names = tuple(c.channel.name for c in (f.metadata.channels or ()))
    return ND2Index(
        url=ranges.url,
        size=ranges.size,
        positions=len(offsets),
        channels=channels,
        height=height,
        width=width,
        dtype=dtype,
        frame_offsets=offsets,
        frame_bytes=height * attrs.widthBytes,
        channel_names=names,
        # z is left out deliberately: the reader reports 1.0 for a file with no
        # z loop, which is a placeholder for an absent axis and not a length.
        pixel_size_um={"y": float(voxel.y), "x": float(voxel.x)},
        stage_positions_um=_stage_positions(f),
        objective=(f.text_info or {}).get("optics"),
        text_info=dict(f.text_info or {}),
        requests=ranges.requests,
        fetched_bytes=ranges.bytes,
    )


def _frame_offsets(f, chunkmap) -> tuple[int, ...]:
    """Where each frame's pixels start, from the chunk map, in position order.

    The reader computes the same addresses from the same table, so its answer is
    taken where it has one and the chunk map is the check on it. They have
    agreed on every file seen so far; a disagreement means the header size
    assumed here is wrong for that file, and the reader is the one to believe.
    """
    from_map = {
        int(name[len(b"ImageDataSeq|"): -1]): offset + FRAME_HEADER_BYTES
        for name, (offset, _) in chunkmap.items()
        if name.startswith(b"ImageDataSeq|")
    }
    reader = dict(getattr(f._rdr, "_frame_offsets", {}) or {})
    if reader and reader != from_map:
        from_map = reader
    return tuple(from_map[i] for i in sorted(from_map))
