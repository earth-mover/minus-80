r"""Index an ND2's frames from its chunk map and turn them into ``ManifestArray``\ s.

An ND2 file ends with a chunk map: a flat table of ``name -> (offset, size)``
covering every chunk in the file, including each image frame. A reader that
can fetch the tail can therefore address the whole file arithmetically, without
walking from one frame to the next, which is why a remote file indexes in four
range requests.

The parsing is ``nd2``'s. This module fetches ranges, hands them to that
library, and turns what it reports into VirtualiZarr ``ManifestArray``\ s.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

import numpy as np
from minus80.core import as_url
from minus80.core.fetch import object_size, write_ranges
from minus80.core.manifest import channel_last_transpose, manifest_array
from minus80.core.registry import file_path
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


class UnsupportedND2(Exception):
    """The file is an ND2 whose frames cannot be referenced as stored bytes."""


@dataclasses.dataclass(frozen=True)
class ND2Index:
    """Everything one ND2 states about itself, plus where each frame's bytes are.

    ``frame_offsets`` is one byte offset per stage position, in the file's own
    order, pointing at the first pixel byte rather than at the chunk header.
    Each frame is ``(y, x, c)`` and C-contiguous, which is what lets the arrays
    present it as ``(c, y, x)`` without moving a byte.

    ``pixel_size_um`` holds ``y`` and ``x`` only for the axes the file marks as
    calibrated. ``requests`` and ``fetched_bytes`` are what indexing a remote
    file cost, and 0 for a local path or a file object.
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
        """The codec chain that reads one frame's interleaved ``(y, x, c)`` bytes as ``(c, y, x)``."""
        return _codecs(3)

    def position_array(self, position: int, *, attributes: Mapping | None = None) -> ManifestArray:
        """One stage position as a ``(c, y, x)`` array whose single chunk is the stored frame.

        Its attributes carry ``channel_names``, ``scale`` and ``units`` for the
        calibrated image axes, and ``stage_position_um``, the stage coordinates
        as the file states them. ``attributes`` are merged over these.
        """
        stage = {}
        if self.stage_positions_um:
            stage["stage_position_um"] = list(self.stage_positions_um[position])
        return self._array(
            [self.frame_offsets[position]], shape=self.frame_shape,
            dimension_names=("c", "y", "x"), attributes={**stage, **(attributes or {})})

    def position_arrays(self, *, attributes: Mapping | None = None) -> dict[str, ManifestArray]:
        """Every stage position as :meth:`position_array`, keyed ``"0"``, ``"1"``, … in file order."""
        return {str(p): self.position_array(p, attributes=attributes) for p in range(self.positions)}

    def stacked_array(self, *, attributes: Mapping | None = None) -> ManifestArray:
        """Every position of the file as one ``(position, c, y, x)`` array, one frame per chunk.

        It references the same bytes as the position arrays, so writing both
        costs references and no storage. Its attributes are those of
        :meth:`position_array` less the stage position.
        """
        return self._array(
            self.frame_offsets, shape=(self.positions, *self.frame_shape),
            dimension_names=("position", "c", "y", "x"), attributes=attributes)

    def _array(self, offsets: Sequence[int], *, shape, dimension_names, attributes) -> ManifestArray:
        """An array whose chunks are whole frames, one per entry of ``offsets``."""
        chunk_shape = (1,) * (len(shape) - 3) + self.frame_shape
        grid = tuple(s // c for s, c in zip(shape, chunk_shape))
        return manifest_array(
            self.url, np.asarray(offsets, dtype=np.uint64).reshape(grid),
            np.full(grid, self.frame_bytes, dtype=np.uint64),
            shape=shape, chunk_shape=chunk_shape, dtype=self.dtype,
            codecs=_codecs(len(shape)), dimension_names=dimension_names,
            attributes={**self._physical(), **(attributes or {})})

    def _physical(self) -> dict:
        attrs: dict = {}
        if self.channel_names:
            attrs["channel_names"] = list(self.channel_names)
        if self.pixel_size_um:
            attrs["scale"] = dict(self.pixel_size_um)
            attrs["units"] = {axis: "micrometer" for axis in self.pixel_size_um}
        return attrs


def _codecs(rank: int) -> list[dict]:
    return [channel_last_transpose(rank), {"name": "bytes", "configuration": {"endian": "little"}}]


def index_nd2(source: str | Path | BinaryIO, *, url: str | None = None) -> ND2Index:
    """Read one ND2's metadata and frame addresses.

    ``source`` is a URL, a local path, or an open binary file. ``url`` is the
    address the arrays record; it defaults to the source's own URL. A file
    object has no address, so passing one without ``url`` raises ``ValueError``.

    A remote file costs four requests: one for its length, one block from each
    end, and one coalesced range for the metadata chunks between them. No pixel
    byte is fetched. A local path or a file object is read in place.

    Raises :class:`UnsupportedND2` for a file whose frames are compressed, whose
    rows are padded, or whose acquisition loops over anything but stage
    positions.
    """
    if hasattr(source, "read"):
        if url is None:
            raise ValueError("a file object has no address: pass url= for the manifest to record")
        return _index(source, url, requests=0, fetched_bytes=0)
    source_url = as_url(source)
    path = file_path(source_url)
    if path is not None:
        return _index(path, url or source_url, requests=0, fetched_bytes=0)
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp, "source.nd2")
        requests, fetched = _sparse_copy(source_url, local)
        return _index(local, url or source_url, requests=requests, fetched_bytes=fetched)


def _sparse_copy(url: str, local: Path) -> tuple[int, int]:
    """Write ``url``'s metadata chunks into a sparse file of the same length at ``local``.

    ``nd2.ND2File`` reads the file it is given by seeking, so it needs a file
    that has the right length and the right bytes wherever it looks. Every
    frame region stays a hole, and nothing reads one. Returns the requests made
    and the bytes fetched.
    """
    from nd2._parse._chunk_decode import get_chunkmap

    size = object_size(url, patient=True)
    with open(local, "wb") as sparse:
        sparse.truncate(size)
        ends = [(0, min(HEAD_BLOCK, size)), (max(0, size - TAIL_BLOCK), size)]
        fetched = write_ranges(url, ends, sparse, progress=False, patient=True)
        sparse.flush()
        with open(local, "rb") as fh:
            chunkmap = get_chunkmap(fh)
        # Each range is padded past its chunk's payload, so the last one can run
        # past the end of the file.
        wanted = [(lo, min(hi, size)) for lo, hi in _metadata_ranges(chunkmap, ends)]
        fetched += write_ranges(url, wanted, sparse, progress=False, patient=True)
    return 1 + len(ends) + len(wanted), fetched


def _coalesce(ranges: Sequence[tuple[int, int]], gap: int = COALESCE_GAP) -> list[tuple[int, int]]:
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
    by ``nd2``'s header alignment and short entries are given a fixed allowance.
    """
    from nd2._parse._chunk_decode import CHUNK_ALIGNMENT

    wanted = [
        (offset, offset + (size if size > 0 else HEAD_BLOCK) + CHUNK_ALIGNMENT)
        for name, (offset, size) in chunkmap.items()
        if not name.startswith(b"ImageDataSeq|")
    ]
    for lo, hi in _coalesce(wanted):
        if not any(lo >= clo and hi <= chi for clo, chi in covered):
            yield lo, hi


def _index(file: Path | BinaryIO, url: str, *, requests: int, fetched_bytes: int) -> ND2Index:
    """Describe ``file``: a path, opened and closed here, or a caller's open file, left open."""
    import nd2

    if isinstance(file, Path):
        with nd2.ND2File(file) as f:
            return _describe(f, url=url, size=file.stat().st_size, requests=requests,
                             fetched_bytes=fetched_bytes)
    size = file.seek(0, os.SEEK_END)
    # ``ND2File.close`` closes the handle it was given, which belongs to the
    # caller; left unclosed, ``nd2`` releases its reader without closing it.
    return _describe(nd2.ND2File(file), url=url, size=size, requests=requests,
                     fetched_bytes=fetched_bytes)


def _describe(f, *, url: str, size: int, requests: int, fetched_bytes: int) -> ND2Index:
    attrs = f.attributes
    if attrs.compressionType is not None:
        raise UnsupportedND2(
            f"{url}: frames are {attrs.compressionType}-compressed; there is "
            "no Zarr codec for ND2's compression, so its bytes cannot be referenced"
        )
    channels = attrs.componentCount
    height, width = attrs.heightPx, attrs.widthPx
    dtype = np.dtype(f.dtype)
    if attrs.widthBytes != width * channels * dtype.itemsize:
        raise UnsupportedND2(
            f"{url}: a row occupies {attrs.widthBytes} bytes where "
            f"{width} x {channels} x {dtype.itemsize} pixels need "
            f"{width * channels * dtype.itemsize}; padded rows are not a Zarr chunk"
        )
    _check_loops(f, url)
    offsets = _frame_offsets(f)
    channel_meta = f.metadata.channels or ()
    return ND2Index(
        url=url,
        size=size,
        positions=len(offsets),
        channels=channels,
        height=height,
        width=width,
        dtype=dtype,
        frame_offsets=offsets,
        frame_bytes=height * attrs.widthBytes,
        channel_names=tuple(c.channel.name for c in channel_meta),
        pixel_size_um=_pixel_size_um(channel_meta),
        stage_positions_um=_stage_positions(f),
        objective=(f.text_info or {}).get("optics"),
        text_info=dict(f.text_info or {}),
        requests=requests,
        fetched_bytes=fetched_bytes,
    )


def _check_loops(f, url: str) -> None:
    """Refuse a file whose frames are not one per stage position.

    Each frame becomes a position, so a file that loops over time or z as well
    would have its timepoints presented as fields of view, and silently, since
    the shapes are indistinguishable.
    """
    kinds = [getattr(loop, "type", type(loop).__name__) for loop in f.experiment]
    if [k for k in kinds if k != "XYPosLoop"]:
        raise UnsupportedND2(
            f"{url}: the acquisition loops over {', '.join(kinds)}; this parser "
            "addresses one frame per stage position and would present "
            "the other axes as fields of view"
        )


def _pixel_size_um(channel_meta) -> dict[str, float]:
    """The ``y`` and ``x`` pixel size in micrometres, for each axis the file marks as calibrated.

    ``nd2``'s ``voxel_size()`` answers 1.0 for an axis with no calibration, a
    placeholder that would read as a real micrometre.
    """
    if not channel_meta:
        return {}
    volume = channel_meta[0].volume
    (x, y, _), (x_ok, y_ok, _) = volume.axesCalibration, volume.axesCalibrated
    return {axis: float(v) for axis, v, ok in (("y", y, y_ok), ("x", x, x_ok)) if ok}


def _stage_positions(f) -> tuple[tuple[float, float, float], ...]:
    """Each position's stage coordinates in micrometres, from the acquisition's ``XYPosLoop``.

    A file with no such loop has one field of view and no coordinate to give.
    """
    for loop in f.experiment:
        points = getattr(getattr(loop, "parameters", None), "points", None)
        if points:
            return tuple(tuple(float(v) for v in p.stagePositionUm) for p in points)
    return ()


def _frame_offsets(f) -> tuple[int, ...]:
    """Where each frame's pixels start, in position order, as ``nd2``'s reader computes them from the chunk map."""
    offsets = f._rdr._frame_offsets
    return tuple(offsets[i] for i in sorted(offsets))
