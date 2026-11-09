"""FLAC files as Zarr chunk references, decoded by the ``flac`` Zarr v3 codec.

A FLAC stream is a short metadata section followed by a flat run of frames. Each
frame is byte-aligned, decodes on its own, and states its own sample rate, bit
depth and channel layout in its header, so a run of consecutive frames is a
complete Zarr chunk: the codec decodes it with no file header and no per-file
state. One codec configuration therefore serves every file that shares a sample
rate and blocksize, and any number of such files can be concatenated on one time
axis.

Three steps, each usable alone:

* :func:`scan_frames` reads one file through libavformat (PyAV) and returns every
  frame's byte offset, byte length and sample count.
* :func:`chunk_table` groups the frames of one or more scans into chunks and
  checks that one codec configuration can decode them all.
* :func:`write_array` creates the array in an Icechunk store and sets its virtual
  references.

The chunk grid is rectilinear: every file's last chunk ends at that file's last
sample, so chunk lengths differ at file boundaries. VirtualiZarr's
``ManifestArray`` accepts only regular grids, so :func:`write_array` writes the
references with Icechunk's ``set_virtual_refs`` rather than through a
``ManifestGroup``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np
import zarr

#: libavformat's names for 16-bit integer samples, interleaved and planar. The
#: codec decodes 16-bit FLAC only.
INT16_FORMATS = {"s16", "s16p"}


@dataclass(frozen=True)
class FrameScan:
    """Every frame of one FLAC file.

    ``url`` is where references will point. ``offsets[i]`` and ``lengths[i]`` are
    frame ``i``'s byte range in the file, and ``samples[i]`` its sample count per
    channel.
    """

    url: str
    sample_rate: int
    channels: int
    sample_format: str
    offsets: np.ndarray
    lengths: np.ndarray
    samples: np.ndarray

    @property
    def n_samples(self) -> int:
        return int(self.samples.sum())

    def save(self, path) -> None:
        """Write the scan to one ``.npz``, so a long scan can be cached and resumed."""
        np.savez(path, **asdict(self))

    @classmethod
    def load(cls, path) -> FrameScan:
        with np.load(path, allow_pickle=False) as z:
            return cls(url=str(z["url"]), sample_rate=int(z["sample_rate"]),
                       channels=int(z["channels"]), sample_format=str(z["sample_format"]),
                       offsets=z["offsets"], lengths=z["lengths"], samples=z["samples"])


def scan_frames(source, *, url: str | None = None) -> FrameScan:
    """Locate every frame of a FLAC file with libavformat.

    ``source`` is anything ``av.open`` reads: a local path, an ``http(s)`` URL
    that ffmpeg streams itself, or a seekable file-like object. ``url`` is what the
    references will record; it defaults to ``source`` as a URL, and must be given
    when ``source`` is a file-like object or a different spelling of the same
    object (an HTTPS mirror of a ``gs://`` key, say).

    The scan reads the whole file. A SEEKTABLE would locate seekpoints from the
    file's first megabyte, but it is an optional metadata block that many
    encoders omit, and it indexes only every few hundred frames.
    """
    import av

    from minus80.core import as_url

    if url is None:
        if not isinstance(source, (str, Path)):
            raise TypeError("url is required when source is a file-like object")
        url = as_url(source)
    with av.open(source) as container:
        stream = container.streams.audio[0]
        if stream.codec_context.name != "flac":
            raise ValueError(f"{url}: audio stream is {stream.codec_context.name}, not flac")
        rate = stream.codec_context.sample_rate
        if stream.time_base != Fraction(1, rate):
            raise ValueError(f"{url}: stream time base {stream.time_base} is not "
                             f"1/{rate}, so packet durations are not sample counts")
        packets = [(p.pos, p.size, p.duration) for p in container.demux(stream)
                   if p.size]
        scan = FrameScan(
            url=url, sample_rate=rate,
            channels=stream.codec_context.layout.nb_channels,
            sample_format=stream.codec_context.format.name,
            offsets=np.array([p[0] for p in packets], dtype=np.int64),
            lengths=np.array([p[1] for p in packets], dtype=np.int64),
            samples=np.array([p[2] for p in packets], dtype=np.int64))
    # A frame the demuxer split or merged would decode to valid audio in the
    # wrong place, so demand that the frames tile the audio exactly.
    gaps = np.flatnonzero(scan.offsets[:-1] + scan.lengths[:-1] != scan.offsets[1:])
    if gaps.size:
        raise ValueError(f"{url}: frames do not tile the stream; frame {gaps[0]} ends "
                         f"at {scan.offsets[gaps[0]] + scan.lengths[gaps[0]]} but the "
                         f"next starts at {scan.offsets[gaps[0] + 1]}")
    return scan


@dataclass(frozen=True)
class ChunkTable:
    """Chunk references for one array concatenating one or more FLAC files in time.

    Chunk ``k`` holds ``edge[k]`` samples per channel, read from ``length[k]``
    bytes at ``offset[k]`` of ``source_uri[rec[k]]``. ``n_samples[j]`` is file
    ``j``'s sample count, so file ``j`` starts at sample ``start_sample[j]``.

    The fields are plain arrays so the table round-trips through ``np.savez``:
    a saved table rebuilds the array without reading any audio.
    """

    sample_rate: int
    blocksize: int
    channels: int
    source_uri: np.ndarray
    n_samples: np.ndarray
    edge: np.ndarray
    rec: np.ndarray
    offset: np.ndarray
    length: np.ndarray

    @property
    def start_sample(self) -> np.ndarray:
        return np.concatenate([[0], np.cumsum(self.n_samples)[:-1]]).astype(np.int64)

    def save(self, path) -> None:
        np.savez_compressed(path, **asdict(self))

    @classmethod
    def load(cls, path) -> ChunkTable:
        with np.load(path, allow_pickle=False) as z:
            return cls(**{k: z[k] for k in cls.__dataclass_fields__})


def chunk_table(scans: list[FrameScan], *, frames_per_chunk: int = 1) -> ChunkTable:
    """Group each file's frames into chunks of ``frames_per_chunk`` whole frames.

    The default, one frame per chunk, matches the file's own storage unit. Merging
    frames cuts the reference count (an hour of 44.1 kHz audio in 4096-sample
    frames is about 39,000 frames) at the cost of coarser reads; a caller that
    merges should record why. A file's last chunk runs to its last frame, so it
    may be shorter, and no chunk spans two files.

    One codec configuration decodes the whole array, so every file must share its
    sample rate, blocksize and channel count, hold 16-bit samples, and keep a fixed
    blocksize, in which only its last frame may be shorter.
    """
    if frames_per_chunk < 1:
        raise ValueError(f"frames_per_chunk must be at least 1, got {frames_per_chunk}")
    for s in scans:
        if s.sample_format not in INT16_FORMATS:
            raise ValueError(f"{s.url}: samples are {s.sample_format}; only 16-bit "
                             "FLAC decodes")
        if np.any(s.samples[:-1] != s.samples[0]) or s.samples[-1] > s.samples[0]:
            raise ValueError(f"{s.url}: blocksize varies between frames")
    rates = {s.sample_rate for s in scans}
    blocksizes = {int(s.samples[0]) for s in scans}
    channels = {s.channels for s in scans}
    if len(rates) != 1 or len(blocksizes) != 1 or len(channels) != 1:
        raise ValueError(f"files disagree: sample rates {sorted(rates)}, blocksizes "
                         f"{sorted(blocksizes)}, channel counts {sorted(channels)}")

    edge, rec, offset, length = [], [], [], []
    for j, s in enumerate(scans):
        for f0 in range(0, len(s.offsets), frames_per_chunk):
            f1 = min(f0 + frames_per_chunk, len(s.offsets))
            edge.append(int(s.samples[f0:f1].sum()))
            rec.append(j)
            offset.append(int(s.offsets[f0]))
            length.append(int(s.offsets[f1 - 1] + s.lengths[f1 - 1] - s.offsets[f0]))
    return ChunkTable(
        sample_rate=rates.pop(), blocksize=blocksizes.pop(), channels=channels.pop(),
        source_uri=np.array([s.url for s in scans]),
        n_samples=np.array([s.n_samples for s in scans], dtype=np.int64),
        edge=np.array(edge, dtype=np.int64), rec=np.array(rec, dtype=np.int64),
        offset=np.array(offset, dtype=np.int64), length=np.array(length, dtype=np.int64))


def write_array(group: zarr.Group, name: str, table: ChunkTable, *,
                channel_axis: bool = True,
                dimension_names: tuple[str, ...] | None = None,
                attributes: dict | None = None) -> zarr.Array:
    """(Re)create ``group[name]`` from ``table`` and set its virtual references.

    ``group`` must live in a writable Icechunk session whose repository has a
    virtual chunk container for every ``table.source_uri``.

    The array is ``("time", "channel")``, with a length-1 channel axis for mono,
    so mono and multi-channel arrays index the same way. FLAC interleaves
    channels within a frame, so a chunk always holds every channel.

    ``channel_axis=False`` writes a mono table as a 1-D ``("time",)`` array
    instead, for an archive that is mono throughout, where a length-1 axis is
    one more index for every reader. It raises on a table with more than one
    channel. The codec decodes a mono chunk to either shape.

    Reading the array needs ``zarr.config.set({"array.rectilinear_chunks": True})``
    and the ``flac`` codec registered.
    """
    import icechunk
    from flac_numcodecs.zarr3 import Flac

    sample_rate, blocksize = int(table.sample_rate), int(table.blocksize)
    channels = int(table.channels)
    edges = [int(e) for e in table.edge]
    if channel_axis:
        shape, chunks, dims = (sum(edges), channels), [edges, [channels]], ("time", "channel")
    elif channels == 1:
        shape, chunks, dims = (sum(edges),), [edges], ("time",)
    else:
        raise ValueError(f"channel_axis=False needs a mono table; this one has "
                         f"{channels} channels")

    empty = np.flatnonzero(table.length <= 0)
    if empty.size:
        raise ValueError(f"{empty.size} chunks have no bytes, first at chunk {empty[0]}")

    with zarr.config.set({"array.rectilinear_chunks": True}):
        array = group.create_array(
            name, shape=shape, chunks=chunks, dtype="<i2",
            dimension_names=dimension_names or dims,
            serializer=Flac(blocksize=blocksize, sample_rate=sample_rate),
            compressors=None, attributes=attributes or {}, overwrite=True)
    uris = [str(u) for u in table.source_uri]
    specs = [icechunk.VirtualChunkSpec(index=[k] + [0] * (len(shape) - 1),
                                       location=uris[r], offset=int(o), length=int(n))
             for k, (r, o, n) in enumerate(zip(table.rec, table.offset, table.length))]
    failed = group.store.set_virtual_refs(array_path=array.path, chunks=specs,
                                          validate_containers=True)
    # Icechunk drops a reference whose location matches no virtual chunk container
    # and returns its index instead of raising; the array would read as fill.
    if failed:
        raise ValueError(f"{len(failed)} references match no virtual chunk container, "
                         f"first {specs[failed[0][0]].location}")
    return array
