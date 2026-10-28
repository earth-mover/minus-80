"""Convert the 3D-4i zebrafish plate (BioImage Archive S-BIAD3228) to OME-Zarr 0.6.

PLAN.md explains the decisions behind this layout. LAYOUTS.md records the
quirks of the source. This docstring walks through what the code does and why.
The general parts of the work live in the minus80 package. This file holds
only what is specific to this dataset.

What the source is
------------------
The source is one zip file of 591 GB. It holds an OME-Zarr 0.4 plate written
by Fractal, the software the authors used. Inside the zip:

- There are 30 wells. Each well was imaged in 4 staining rounds, numbered 0 to
  3. In each round the embryos were stained for DNA (DAPI) and three proteins,
  imaged, and washed, so every round has 4 channels.
- For each well and round there is one large image the size of the well. We
  call it the well image. It has axes (c, z, y, x) and holds uint16 pixels.
- The microscope took 5 to 9 fields of view in each well, one per embryo.
  Each field is 1000 x 1000 pixels and 271 planes deep. Fractal pasted each
  field onto the well image at the position where it was taken.
- Each well image has 4 pyramid levels. Each level halves the size in y and x,
  rounding down.
- Round 1 also has six segmentations, called labels, on the same pixel grid.
- Each well image has tables in AnnData format. The table ``FOV_ROI_table`` says
  where each field sits on the well image.

Most of each well image is empty. The microscope never imaged the space between
the fields, so those pixels are exactly 0. At level 0 the source does not
store chunks that miss every field. At levels 1 to 3 it does store them,
full of zeros. Inside a field, even the background outside the embryo has
camera noise, with values from 0 to 8.

The zip compressed every file inside it with Deflate. Because of that, we
cannot point at the source chunks in place. This ingest reads the pixels and
writes them again.

What the store is
-----------------
The store is an OME-Zarr 0.6 plate with one image per embryo. Each image
holds every channel of every round, so opening an embryo gives all of its
stains at once.

- ``<row>/<column>`` is a well. Its ``well`` metadata lists the well's images.
  The same group also has a ``scene``. The scene places every image of the
  well in one shared coordinate system, called ``well``, so a viewer can still
  show the whole well as one picture.
- ``<row>/<column>/<n - 1>`` is one embryo, the source's field ``FOV_n``, so
  ``FOV_1`` is image ``0``. Its axes are (c, z, y, x) and its pixels are uint16. Its channel axis
  holds round 0's four channels, then round 1's, and so on: 16 channels for 4
  rounds. Each channel keeps its source name, such as ``DAPI_1`` or
  ``H3K27Ac``, and the image's annotations record each channel's round.
- ``<row>/<column>/<n - 1>/labels/<name>`` holds the labels, cut to the field.

Why the rounds are channels
---------------------------
An OME-Zarr image allows only one axis that is neither space nor time, so
rounds cannot be an axis of their own next to the channels. The rounds of the
published data are already registered to each other, so they can share one
image and one coordinate system. If a later use needs each round as a separate
image, the chunks can serve both layouts: every chunk is one plane of one
channel with the same codec.

Cutting out the fields leaves the empty space of the well image behind. A few
chunks are still all zero, mostly label planes above and below each embryo.
zarr does not write those chunks. When read, they come back as the fill
value, 0.

How the copy works
------------------
The zip is read once, well by well, in the order it stores its bytes. Each
well is one contiguous region of the zip, and every metadata document the
well needs lies inside that region (PLAN.md, decision 14).

- **The index.** The zip's central directory, its table of contents at the
  end of the file, lists every member with its size and where it starts.
  ``minus80.parsers.zip.reading`` reads it once and keeps it as a Parquet
  file. The directory alone says what every member is, because a member's
  name is a Zarr key: ``C/02/2/0/1/13/0/7`` is chunk (1, 13, 0, 7) of array
  ``C/02/2/0`` (``minus80.parsers.zip.zarr_v2``).
- **Fetching.** ``minus80.parsers.zip.reading.stream_members`` reads a well's
  region as 64 MiB range requests, many at once, and cuts the members out
  of them as they arrive. A thread does this ahead of the rest, so the
  network stays busy while the CPU works. It hands over finished channel
  runs and waits once two are queued that the main thread has not taken.
- **Channel runs.** Within one array the zip stores one channel at a time,
  so the chunks arrive as *channel runs*: every chunk of one channel of one
  array, or every chunk of one label array. A destination shard needs four
  z indices of one channel, and the zip stores a channel's z indices in a
  scrambled order, so no shard is complete until nearly the whole channel
  run has arrived. A channel run is therefore the unit of work.
- **Waiting for metadata.** A channel run can be decoded once three things
  have arrived: the field positions (``FOV_ROI_table``), the shape of its
  pyramid level (any ``.zarray`` of that level), and its array's encoding.
  The zip stores the first round's table after that round's two coarsest
  levels, and each array's ``.zarray`` half way through its chunks, so the
  first channel runs wait in memory, compressed, until these arrive: about
  2 GB, until level 0's first ``.zarray`` arrives partway through the first
  level-0 run. Runs that wait are capped (``waiting_limit``, 4 GB by
  default), so a document that is missing or late stops the well with an
  error naming it, instead of buffering the whole well. An array's chunks
  can be decoded with the encoding of an earlier array of the same level
  and kind before its own ``.zarray`` arrives; every array's own
  ``.zarray`` is checked against the encoding used before the well is
  committed.
- **Memory.** At any moment the copy holds: the channel runs waiting for
  metadata, compressed; one channel run being decoded, compressed, and its
  fields' buffers; the previous run's buffers while they upload; up to two
  compressed runs queued by the fetching thread and one it is assembling;
  the range requests in flight, 64 MiB each; and the cutter's buffer, the
  part of a range not yet cut into members.
- **Decoding.** For each chunk in a grid cell that some field touches, a
  thread pool inflates it, checks its Blosc frame, decodes it and copies
  each field's part into that field's buffer. zlib and Blosc release the
  GIL, so this uses every core.
- **Cutting each level.** The module ``minus80.ome.crop`` works out which part
  of each source level belongs to a field. At level 0 this is exactly the
  field's 1000 x 1000 pixels. Levels 1 to 3 copy the source's own coarse
  pixels, so that every value in the store is the authors'. Most fields do
  not start on a coarse pixel boundary. The cut therefore covers every coarse
  pixel the field touches, and it can be one pixel wider than 1000 / 2^k.
  Each level's translation records exactly where the cut sits. A later
  commit can replace these levels with ones rebuilt from level 0.
- **Writing.** Each channel run becomes one zarr assignment per field: a
  whole channel of one level, or a whole label level. zarr encodes and
  uploads its shards while the next channel run decodes. Several fields are
  assigned at once (``--upload-workers``), and zarr's ``async.concurrency``
  sets how many shards each assignment uploads at once. A chunk is one
  plane of one channel and a shard holds 32 planes; for labels, a shard
  holds all 271 planes, because a label plane compresses to only a few KB.
  Every shard is written once and never read back.
- **Checking.** Before a well is committed, its metadata is read the way a
  reader of the source would read it: ``minus80.parsers.fractal`` checks
  that every round's ``FOV_ROI_table`` agrees and that each pyramid level is
  the one above halved, and the fields it finds must be the ones the copy
  used.
- **Commits.** Each well is one commit. The well's own metadata is written in
  that same commit. So a well is either fully in the store or not there at
  all. If a run stops, the next run starts again at the first well that is
  not yet committed. A well that fails is reported and skipped, and the run
  goes on to the next well and exits nonzero at the end, listing the failed
  wells. The shards a failed well uploaded stay in storage, uncommitted and
  unreachable, until Icechunk's garbage collection removes them. At the end,
  every run writes the plate's metadata. That metadata lists exactly the
  wells that are on the branch at that moment.
- **Progress.** Decoding prints a line every few seconds, counting chunks.
  Every 30 s a heartbeat line names the stage the well is in (streaming,
  checking, writing metadata, committing) with what has been fetched and at
  what rate, the chunks decoded, the fields uploaded and memory, so a long
  commit or check is not mistaken for a hang.
- **Speed.** Fetching, decoding and uploading overlap, so a well takes about
  as long as the slowest of them. To go faster still, start more processes
  and give each one different wells with ``--wells``. Each process commits
  its own wells, and Icechunk rebases each commit onto the others.

Terms: a *member* is one file inside the zip; a *chunk* is a member that
holds pixels; a *run* is one array's chunks, stored back to back; a *channel
run* is the part of a run that holds one channel; a *range* is one range
request; a *z index* is a chunk's position along z, counting blocks of 10
planes.

Running it
----------
::

    uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/ingest.py \\
        --source <zip URL> --repo <dir | arraylake://org/repo> [--wells C/02,E/05] \\
        [--upload-workers 4] [--zarr-concurrency 32] [--waiting-limit-gb 4]
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path

import numpy as np
import pyarrow as pa
import zarr
from zarr.codecs import BloscCodec
from zarr.core.buffer import default_buffer_prototype
from zarr.storage import MemoryStore

from minus80.core.fetch import cache_dir
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, commit, storage_for
from minus80.core.progress import Heartbeat, Progress, peak_memory_gb
from minus80.ome import (
    image_annotation,
    image_attrs,
    label_attrs,
    labels_attrs,
    plate_attrs,
    scene_attrs,
    well_attrs,
)
from minus80.ome.image import validate
from minus80.parsers.fractal.plate import (
    FIELDS_TABLE,
    IMAGE_DIMS,
    LABEL_DIMS,
    TABLES,
    Field,
    Plate,
    fields_from_table,
    level_scales,
    multiscale_scales,
    read_fields,
    read_plate,
    read_table,
)
from minus80.parsers.zip.directory import inflate_member
from minus80.parsers.zip.reading import RANGE_BYTES, cached_central_directory, read_members, stream_members
from minus80.parsers.zip.streaming import StoredMember, spans_in_file_order
from minus80.parsers.zip.zarr_v2 import ArraySpec, ChunkKey, array_paths, chunk_key, decode_chunk, frame_problem

SOURCE = (
    "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/228/S-BIAD3228/Files/"
    "Multiplexeddata/AssayPlate_Greiner_655090.zarr.zip"
)
STUDY = {
    "study": "https://www.ebi.ac.uk/biostudies/bioimages/studies/S-BIAD3228",
    "preprint": "https://www.biorxiv.org/content/10.1101/2025.10.27.684723v1",
    "license": "CC0-1.0",
}
VERSION = "0.6"

#: The name of the coordinate system that each well's scene defines.
SCENE = "well"

#: How many planes one image shard holds. See decision 5 in PLAN.md. Because
#: a chunk is one plane of one channel, showing one plane needs only one small
#: read. With 32 planes per shard, one channel of a whole embryo takes 9
#: requests of about 15 MB each.
SHARD_DEPTH = 32

#: See decision 6 in PLAN.md. Blosc with zstd decodes fast, and the Zarr v3
#: specification defines it, so every Zarr reader can open the store.
CODEC = BloscCodec(cname="zstd", clevel=5, shuffle="bitshuffle")

#: Range requests in flight when reading the staged copy in R2, about 100 ms
#: away from a VM in the same region; 64 at once measured 584 MB/s.
READ_CONCURRENCY = 64

#: The same, when reading straight from EBI. EBI starts refusing requests at
#: about 25 connections from one address (PLAN.md), so stay well below that.
EBI_READ_CONCURRENCY = 16

#: How many finished channel runs the fetching thread may queue before the
#: main thread takes them. Once taken, a run is decoded at once or waits for
#: metadata, and :data:`WAITING_LIMIT` bounds the runs that wait.
RUNS_AHEAD = 2

#: The most compressed bytes that channel runs may hold while they wait for
#: metadata. The first round's runs hold about 2 GB until level 0's first
#: ``.zarray`` arrives; without a limit, a document that is missing or late
#: would buffer the whole well, up to 27 GB.
WAITING_LIMIT = 4 * 10**9

#: How many fields are assigned through zarr at once.
UPLOAD_WORKERS = 4

#: zarr's ``async.concurrency``: how many shards one assignment encodes and
#: uploads at once. zarr's own default is 10.
ZARR_CONCURRENCY = 32

#: Tables that describe the whole well rather than one field. Their rows are
#: kept in the well's attributes, so nothing the source recorded is lost.
WELL_TABLES = ("well_ROI_table", "embryoRaw_ROI_table")

#: A level shape larger than any image, for placing fields on a level before
#: its real shape is known; the real shapes are checked before the commit.
_UNKNOWN_EXTENT = 1 << 40


def image_path(field: Field) -> str:
    """The image of field ``FOV_n`` is ``n - 1``, so the first field is image ``0``."""
    return str(field.number - 1)


def round_channels(plate: Plate, src: zarr.Group, well: str) -> list[dict]:
    """Every channel of every round, in the order the image's channel axis holds them.

    Each entry is the source's own ``omero`` channel (its name, display color,
    display window and the microscope's ``wavelength_id``), plus the round it
    came from and its position in that round's image. The names come from
    this well's own metadata: round 3 stains different markers in different
    wells (LAYOUTS.md).
    """
    channels = []
    for rnd in plate.acquisitions:
        for index, channel in enumerate(src[f"{well}/{rnd}"].attrs["omero"]["channels"]):
            channels.append({**channel, "round": rnd, "source_channel": index})
    names = [c["label"] for c in channels]
    if len(set(names)) != len(names):
        raise ValueError(f"{well}: channel names repeat across rounds: {names}")
    return channels


def label_names(plate: Plate, src: zarr.Group, well: str) -> dict[str, int]:
    """Each label's name, with the round whose image it belongs to in the source."""
    labels = {}
    for rnd in plate.acquisitions:
        well_image = src[f"{well}/{rnd}"]
        for name in well_image["labels"].attrs["labels"] if "labels" in well_image else []:
            if name in labels:
                raise ValueError(f"{well}: label {name} appears in rounds {labels[name]} and {rnd}")
            labels[name] = rnd
    return labels


def check_codecs(src: zarr.Group, well: str, plate: Plate) -> None:
    """Raise unless every source array of the well is compressed with Blosc.

    A chunk cut short is found from its Blosc header
    (:func:`~minus80.parsers.zip.zarr_v2.frame_problem`), which is only
    meaningful if every chunk is a Blosc frame.
    """
    for rnd in plate.acquisitions:
        well_image = src[f"{well}/{rnd}"]
        arrays = [well_image[str(k)] for k in range(plate.levels)]
        if "labels" in well_image:
            arrays += [
                well_image[f"labels/{name}/{k}"]
                for name in well_image["labels"].attrs["labels"]
                for k in range(plate.levels)
            ]
        for array in arrays:
            compressor = array.metadata.compressor
            if compressor is None or compressor.codec_id != "blosc":
                raise ValueError(f"{array.path}: compressor {compressor}, expected Blosc")


def empty_source_chunks(plan: WellPlan, src: zarr.Group, field: Field) -> list[str]:
    """The empty source chunks that fall inside this field, at any level.

    The source zip holds some chunks as empty files: 1,244 of them, in every
    well, including chunks inside embryos. The copy reads them as the fill
    value, 0, so the store has zeros where the source has no data. Listing
    them on the image means nobody mistakes those zeros for a measurement.
    """
    found = []
    for name in plan.empty:
        key = plan.chunks.get(name)
        if key is None:
            continue
        level = field.levels[plan.arrays[key.array].level]
        _, chunk_y, chunk_x = src[key.array].chunks[-3:]
        (y0, y1), (x0, x1) = level.window["y"], level.window["x"]
        *_, yi, xi = key.index
        if yi * chunk_y < y1 and y0 < (yi + 1) * chunk_y and xi * chunk_x < x1 and x0 < (xi + 1) * chunk_x:
            found.append(name)
    return sorted(found)


@dataclass(frozen=True)
class SourceArray:
    """What a source array's path says: a round's image level, or a label level."""

    path: str
    round: int
    level: int
    #: The label's name, or None for an image level.
    label: str | None


def source_arrays(well: str, paths: set[str]) -> dict[str, SourceArray]:
    """The well's image and label arrays among ``paths``, by path. AnnData table arrays are left out."""
    image = re.compile(rf"^{re.escape(well)}/(\d+)/(\d+)$")
    label = re.compile(rf"^{re.escape(well)}/(\d+)/labels/([^/]+)/(\d+)$")
    arrays = {}
    for path in paths:
        if match := image.match(path):
            arrays[path] = SourceArray(path, int(match[1]), int(match[2]), None)
        elif match := label.match(path):
            arrays[path] = SourceArray(path, int(match[1]), int(match[3]), match[2])
    return arrays


@dataclass
class WellPlan:
    """Everything the central directory says about one well, before any of it is fetched."""

    well: str
    #: The well's members in file order, each a directory row with its span.
    members: list[dict]
    #: Each chunk member's array and chunk index, by member name.
    chunks: dict[str, ChunkKey]
    arrays: dict[str, SourceArray]
    rounds: tuple[int, ...]
    #: Each round's channel count, from the chunk indices of its level 0.
    channels: dict[int, int]
    #: The names of every member of each round's ``FOV_ROI_table``.
    table_members: dict[int, set[str]]
    #: Members that are empty files.
    empty: set[str]

    @classmethod
    def from_directory(cls, directory: pa.Table, well: str) -> WellPlan:
        names = [n for n in directory["name"].to_pylist() if n.startswith(f"{well}/")]
        arrays = source_arrays(well, array_paths(names))
        chunks = {}
        for name in names:
            key = chunk_key(name, set(arrays))
            if key is not None:
                chunks[name] = key
        rounds = tuple(sorted({a.round for a in arrays.values() if a.label is None}))
        channels = {
            r: 1 + max(key.index[0] for key in chunks.values() if key.array == f"{well}/{r}/0") for r in rounds
        }
        tables = {
            r: {n for n in names if n.startswith(f"{well}/{r}/{TABLES}/{FIELDS_TABLE}/")} for r in rounds
        }
        members = spans_in_file_order(directory, names)
        empty = {m["name"] for m in members if m["uncompressed"] == 0}
        return cls(well, members, chunks, arrays, rounds, channels, tables, empty)

    def channel_offset(self, rnd: int) -> int:
        """Where round ``rnd``'s first channel sits on the output image's channel axis: rounds in order."""
        return sum(self.channels[r] for r in self.rounds if r < rnd)

    def run_key(self, key: ChunkKey) -> tuple[str, int | None]:
        """The channel run a chunk belongs to: its array and channel, or its label array alone."""
        array = self.arrays[key.array]
        return (key.array, None if array.label is not None else key.index[0])


@dataclass
class ChannelRun:
    """One channel of one image array, or one label array: the chunks of one unit of work."""

    key: tuple[str, int | None]
    members: list[StoredMember] = dataclass_field(default_factory=list)

    def stored_bytes(self) -> int:
        """The bytes the run holds before decoding: its members as the zip stores them."""
        return sum(len(member.stored) for member in self.members)


def documents_store(documents: Mapping[str, bytes], path: str = "") -> zarr.Group:
    """A read-only zarr group over metadata documents held in memory, opened at ``path``.

    Opening a group at its path reads only that group's own documents, so it
    works before the documents of the groups above it have arrived.
    """
    prototype = default_buffer_prototype()
    store = MemoryStore({name: prototype.buffer.from_bytes(data) for name, data in documents.items()})
    return zarr.open_group(store, path=path, mode="r", zarr_format=2)


class WellState:
    """What the stream has delivered for one well: its metadata documents, and what follows from them."""

    def __init__(self, plan: WellPlan, plate_documents: Mapping[str, bytes]) -> None:
        self.plan = plan
        self.documents: dict[str, bytes] = dict(plate_documents)
        #: Each array's own ``.zarray``, once it has arrived.
        self.specs: dict[str, ArraySpec] = {}
        #: The encoding each array's chunks were decoded with, which can be an
        #: earlier array's; checked against the array's own before the commit.
        self.used: dict[str, list[ArraySpec]] = {}
        #: The fields each channel run was cut into, with its level; checked
        #: against the fields the source's metadata gives before the commit.
        self.placed: list[tuple[int, list[Field]]] = []
        #: Each pyramid level's ``(y, x)`` extent, from the first ``.zarray`` of that level.
        self.extents: dict[int, tuple[int, int]] = {}
        self.table = None
        self.scales: list[dict[str, float]] | None = None
        self._fields: dict[int, list[Field]] = {}
        #: Chunks that could not be decoded, by field name, with the reason.
        self.unreadable: dict[str, dict[str, str]] = {}

    def add_document(self, name: str, data: bytes) -> None:
        """Keep a metadata document, and learn what it says."""
        self.documents[name] = data
        if name.endswith("/.zarray"):
            path = name.rsplit("/", 1)[0]
            if path in self.plan.arrays:
                spec = ArraySpec.from_document(data)
                # A channel run is one channel's chunks, and _decode_into keeps
                # only a chunk's first channel, so wider chunks would lose channels.
                if self.plan.arrays[path].label is None and spec.chunks[0] != 1:
                    raise ValueError(f"{path}: chunks {spec.chunks} hold {spec.chunks[0]} channels, not 1")
                self.specs[path] = spec
                self.extents.setdefault(self.plan.arrays[path].level, spec.shape[-2:])
                self._fields.clear()
        if self.table is None:
            for rnd, members in self.plan.table_members.items():
                round_attrs = f"{self.plan.well}/{rnd}/.zattrs"
                if members and members <= self.documents.keys() and round_attrs in self.documents:
                    table = f"{self.plan.well}/{rnd}/{TABLES}/{FIELDS_TABLE}"
                    self.table = read_table(documents_store(self.documents, table))
                    attrs = json.loads(self.documents[round_attrs])
                    self.scales = multiscale_scales(attrs, IMAGE_DIMS, round_attrs)
                    break

    def spec_for(self, array: SourceArray) -> ArraySpec | None:
        """The encoding to decode an array's chunks with: its own, else that of the first array of the same level and kind.

        A borrowed encoding is a guess until the array's own ``.zarray``
        arrives, and :func:`check_well` confirms it before the commit.
        Borrowing only within a level and kind keeps the guess to arrays
        made the same way, so a level whose chunks differ waits for its own
        ``.zarray`` instead of failing the well at the check.
        """
        if array.path in self.specs:
            return self.specs[array.path]
        for path, spec in self.specs.items():
            other = self.plan.arrays[path]
            if other.level == array.level and (other.label is None) == (array.label is None):
                return spec
        return None

    def image_spec(self) -> ArraySpec | None:
        return next((s for p, s in self.specs.items() if self.plan.arrays[p].label is None), None)

    def fields_at(self, level: int) -> list[Field] | None:
        """The well's fields placed on ``level``, once the table and that level's shape have arrived."""
        image = self.image_spec()
        if self.table is None or level not in self.extents or image is None:
            return None
        if level not in self._fields:
            channels, depth = image.shape[0], image.shape[1]
            shapes = [
                (channels, depth, *self.extents.get(k, (_UNKNOWN_EXTENT, _UNKNOWN_EXTENT)))
                for k in range(len(self.scales))
            ]
            self._fields[level] = fields_from_table(
                self.plan.well, self.table, depth, self.scales[0], shapes, self.scales
            )
        return self._fields[level]

    def missing(self, run: ChannelRun) -> list[str]:
        """The metadata a channel run still waits for, named for an error message."""
        array = self.plan.arrays[run.key[0]]
        kind = "image" if array.label is None else "label"
        needs = []
        if self.table is None:
            needs.append(f"a round's {FIELDS_TABLE} and that round's .zattrs")
        if array.level not in self.extents:
            needs.append(f"a .zarray of level {array.level}")
        if self.image_spec() is None:
            needs.append("an image array's .zarray")
        if self.spec_for(array) is None:
            needs.append(f"a .zarray of a level-{array.level} {kind} array")
        return needs

    def ready(self, run: ChannelRun) -> bool:
        return not self.missing(run)


@dataclass
class DecodedRun:
    """One channel run, decoded and cut into each field's window."""

    array: SourceArray
    channel: int | None
    #: The fields the run was cut into. The writer uses these rather than
    #: asking the well's state again, because the main thread can place the
    #: fields anew while this run uploads.
    fields: list[Field]
    #: Each field's pixels for this channel or label at this level, by field name.
    buffers: dict[str, np.ndarray]


def _decode_into(
    member: StoredMember,
    key: ChunkKey,
    array: SourceArray,
    spec: ArraySpec,
    windows: Sequence[tuple[str, tuple[int, int], tuple[int, int]]],
    buffers: dict[str, np.ndarray],
) -> tuple[str, str, list[str]] | None:
    """Decode one chunk and copy each field's part of it into that field's buffer.

    Runs on a worker thread; each chunk writes a different part of each
    buffer, so the chunks of one channel run decode in parallel. Returns None,
    or ``(name, reason, fields)`` for a chunk that cannot be decoded.
    """
    depth, chunk_z, chunk_y, chunk_x = next(iter(buffers.values())).shape[0], *spec.chunks[-3:]
    z_index, y_index, x_index = key.index[-3:]
    y_start, x_start = y_index * chunk_y, x_index * chunk_x
    touched = [
        (name, (max(y_start, y0), min(y_start + chunk_y, y1)), (max(x_start, x0), min(x_start + chunk_x, x1)), y0, x0)
        for name, (y0, y1), (x0, x1) in windows
        if y_start < y1 and y0 < y_start + chunk_y and x_start < x1 and x0 < x_start + chunk_x
    ]
    if not touched or member.entry["uncompressed"] == 0:
        # An empty member holds no chunk; it reads as the fill value, 0, and
        # empty_source_chunks lists it on the images it falls in.
        return None
    data = inflate_member(member.stored, member.entry)
    if (reason := frame_problem(data, spec.compressor)) is not None:
        return member.name, reason, [name for name, *_ in touched]
    chunk = decode_chunk(data, spec)
    if array.label is None:
        chunk = chunk[0]
    z0 = z_index * chunk_z
    z1 = min(z0 + chunk_z, depth)
    for name, (y0, y1), (x0, x1), window_y, window_x in touched:
        buffers[name][z0:z1, y0 - window_y:y1 - window_y, x0 - window_x:x1 - window_x] = chunk[
            : z1 - z0, y0 - y_start:y1 - y_start, x0 - x_start:x1 - x_start
        ]
    return None


def decode_run(run: ChannelRun, state: WellState, pool: ThreadPoolExecutor, note: Progress) -> DecodedRun:
    """Decode one channel run on ``pool`` into one buffer per field, cut to the field's window at its level.

    ``note`` counts the run's chunks as they finish, decoded or skipped.
    """
    array = state.plan.arrays[run.key[0]]
    spec = state.spec_for(array)
    state.used.setdefault(array.path, []).append(spec)
    fields = state.fields_at(array.level)
    state.placed.append((array.level, fields))
    depth = spec.shape[0] if array.label is not None else spec.shape[1]
    windows, buffers = [], {}
    for field in fields:
        window = field.levels[array.level].window
        windows.append((field.name, window["y"], window["x"]))
        buffers[field.name] = np.zeros(
            (depth, window["y"][1] - window["y"][0], window["x"][1] - window["x"][0]), dtype=spec.dtype
        )
    futures = [
        pool.submit(_decode_into, member, state.plan.chunks[member.name], array, spec, windows, buffers)
        for member in run.members
    ]
    for future in futures:
        if (problem := future.result()) is not None:
            name, reason, touched = problem
            for field_name in touched:
                state.unreadable.setdefault(field_name, {})[name] = reason
        note.advance()
    return DecodedRun(array, run.key[1], fields, buffers)


class WellWriter:
    """Writes decoded channel runs into the well's images, creating each array the first time it is written.

    Each field of a run is one assignment, and the assignments run on
    ``pool``, several at once; each writes its own array. The groups and
    arrays are opened or created first, one at a time: zarr formats the
    store's name on its event loop while creating an array, Icechunk's store
    blocks the loop to do it, and writes from other threads to the same
    session then wait on each other forever.
    """

    def __init__(
        self, dest: zarr.Group, plan: WellPlan, shard_depth: int, pool: ThreadPoolExecutor, counters: Counters
    ) -> None:
        self.dest, self.plan, self.shard_depth = dest, plan, shard_depth
        self.pool, self.counters = pool, counters
        self.channels = sum(plan.channels.values())

    def write(self, run: DecodedRun) -> None:
        """One zarr assignment per field: a whole channel of one level, or a whole label level."""
        targets = [(field, self._target(run, field)) for field in run.fields]
        futures = [self.pool.submit(self._assign, run, field, target) for field, target in targets]
        for future in futures:
            future.result()

    def _target(self, run: DecodedRun, field: Field) -> zarr.Array:
        pixels = run.buffers[field.name]
        image = self.dest.require_group(f"{self.plan.well}/{image_path(field)}")
        if run.array.label is None:
            return self._array(image, str(run.array.level), (self.channels, *pixels.shape), pixels.dtype, label=False)
        group = image.require_group(f"labels/{run.array.label}")
        return self._array(group, str(run.array.level), pixels.shape, pixels.dtype, label=True)

    def _assign(self, run: DecodedRun, field: Field, target: zarr.Array) -> None:
        if run.array.label is None:
            target[self.plan.channel_offset(run.array.round) + run.channel] = run.buffers[field.name]
        else:
            target[:] = run.buffers[field.name]
        self.counters.uploaded_field()

    def _array(self, group: zarr.Group, name: str, shape, dtype, *, label: bool) -> zarr.Array:
        if name in group:
            return group[name]
        depth, height, width = shape[-3:]
        return group.create_array(
            name,
            shape=shape,
            chunks=(1, height, width) if label else (1, 1, height, width),
            shards=(depth, height, width) if label else (1, self.shard_depth, height, width),
            dtype=dtype,
            compressors=CODEC,
            fill_value=0,
            dimension_names=LABEL_DIMS if label else IMAGE_DIMS,
        )


class Counters:
    """What one well's copy has done so far, for the heartbeat line.

    The threads of the copy update these as they work, and the heartbeat
    reads them from its own thread.
    """

    def __init__(self, well: str) -> None:
        self.well = well
        #: The stage the well is in, named in the heartbeat line.
        self.stage = "streaming"
        self.started = time.monotonic()
        #: Bytes of the well's region fetched and cut into members.
        self.fetched = 0
        self.decoding: Progress | None = None
        self.uploaded = 0
        #: Compressed bytes of the channel runs waiting for metadata.
        self.waiting = 0
        self._lock = threading.Lock()

    def uploaded_field(self) -> None:
        with self._lock:
            self.uploaded += 1

    def line(self) -> str:
        elapsed = max(time.monotonic() - self.started, 1e-9)
        decoded = self.decoding.done if self.decoding is not None else 0
        return (
            f"well {self.well}, {self.stage}: fetched {self.fetched / 1e9:.2f} GB "
            f"({self.fetched / elapsed / 1e6:.0f} MB/s), {decoded:,} chunks decoded, "
            f"{self.uploaded:,} fields uploaded, {self.waiting / 1e9:.2f} GB waiting for metadata, "
            f"peak memory {peak_memory_gb():.1f} GB"
        )


def _fetch(
    url: str, plan: WellPlan, out: queue.Queue, ahead: threading.Semaphore, stop: threading.Event,
    counters: Counters, **read,
) -> None:
    """Stream the well's members, handing metadata over one by one and chunks as whole channel runs.

    Runs on its own thread. Each channel run is handed over only when the
    next one starts, because only then is it complete. ``ahead`` lets at most
    :data:`RUNS_AHEAD` runs sit in ``out`` before the main thread takes them,
    so the fetching holds those runs, the one it is assembling, the requests
    in flight and the cutter's buffer.
    """
    try:
        run, finished = None, set()
        for member in stream_members(url, plan.members, **read):
            if stop.is_set():
                return
            counters.fetched += member.entry["end"] - member.entry["start"]
            key = plan.chunks.get(member.name)
            if key is None:
                out.put(("document", member))
                continue
            run_key = plan.run_key(key)
            if run is None or run.key != run_key:
                if run is not None:
                    _hand_over(run, out, ahead, stop)
                    finished.add(run.key)
                if run_key in finished:
                    raise ValueError(f"{run_key}: its chunks resume after another channel's began")
                run = ChannelRun(run_key)
            run.members.append(member)
        if run is not None:
            _hand_over(run, out, ahead, stop)
        out.put(("end", None))
    except BaseException as error:  # noqa: BLE001 - every failure must reach the waiting thread
        out.put(("error", error))


def _hand_over(run: ChannelRun, out: queue.Queue, ahead: threading.Semaphore, stop: threading.Event) -> None:
    while not ahead.acquire(timeout=1):
        if stop.is_set():
            raise RuntimeError("stopped")
    out.put(("run", run))


@dataclass
class Timings:
    """Seconds each stage of a well took, for the progress lines."""

    decode: float = 0.0
    waiting_for_writes: float = 0.0
    commit: float = 0.0

    def line(self, total: float) -> str:
        return (
            f"{total:.0f} s in all: decoding {self.decode:.0f} s, waiting for uploads "
            f"{self.waiting_for_writes:.0f} s, commit {self.commit:.0f} s"
        )


#: Seconds to wait for the fetching thread to stop once the copy has ended
#: or failed. It is a daemon thread, so one stuck in a request does not keep
#: the process alive, and the copy's own error surfaces without waiting on it.
FETCHER_JOIN_TIMEOUT = 10.0


def copy_well(
    url: str,
    plan: WellPlan,
    state: WellState,
    dest: zarr.Group,
    *,
    shard_depth: int,
    concurrency: int,
    range_bytes: int,
    decode_threads: int | None,
    upload_workers: int,
    waiting_limit: int,
    registry,
    timings: Timings,
    counters: Counters,
) -> None:
    """Stream one well's members and copy every channel run into the well's images.

    Three things run at once: a thread fetching ranges and cutting members
    out of them; this thread decoding channel runs on a pool; and one thread
    writing the previous channel run through zarr, its fields on a pool of
    ``upload_workers``. A channel run that arrives before the metadata it
    needs waits until that metadata arrives; if the runs waiting hold more
    than ``waiting_limit`` compressed bytes, the well fails, naming the
    metadata still missing.
    """
    out: queue.Queue = queue.Queue()
    ahead, stop = threading.Semaphore(RUNS_AHEAD), threading.Event()
    fetcher = threading.Thread(
        target=_fetch, args=(url, plan, out, ahead, stop, counters),
        kwargs={"concurrency": concurrency, "range_bytes": range_bytes, "registry": registry},
        daemon=True,
    )
    waiting: deque[ChannelRun] = deque()
    writing: Future | None = None
    with (
        ThreadPoolExecutor(decode_threads, thread_name_prefix="decode") as pool,
        ThreadPoolExecutor(upload_workers, thread_name_prefix="field") as fields,
        ThreadPoolExecutor(1, thread_name_prefix="write") as uploads,
        Progress(f"well {plan.well}: decoding chunks", len(plan.chunks), unit="chunks") as note,
    ):
        counters.decoding = note
        writer = WellWriter(dest, plan, shard_depth, fields, counters)
        fetcher.start()
        try:
            ended = False
            while not ended:
                kind, item = out.get()
                if kind == "error":
                    raise item
                if kind == "document":
                    state.add_document(item.name, inflate_member(item.stored, item.entry))
                elif kind == "run":
                    ahead.release()
                    waiting.append(item)
                    counters.waiting += item.stored_bytes()
                else:
                    ended = True
                while waiting and state.ready(waiting[0]):
                    run = waiting.popleft()
                    counters.waiting -= run.stored_bytes()
                    started = time.monotonic()
                    decoded = decode_run(run, state, pool, note)
                    timings.decode += time.monotonic() - started
                    if writing is not None:
                        started = time.monotonic()
                        writing.result()
                        timings.waiting_for_writes += time.monotonic() - started
                    writing = uploads.submit(writer.write, decoded)
                if counters.waiting > waiting_limit:
                    raise ValueError(
                        f"{plan.well}: {len(waiting)} channel runs hold {counters.waiting / 1e9:.2f} GB "
                        f"waiting for metadata, over the limit of {waiting_limit / 1e9:.2f} GB; "
                        f"{waiting[0].key} still needs {', '.join(state.missing(waiting[0]))}"
                    )
            if waiting:
                raise ValueError(
                    f"{plan.well}: {len(waiting)} channel runs never had their metadata, "
                    f"from {waiting[0].key}, which needs {', '.join(state.missing(waiting[0]))}"
                )
            if writing is not None:
                started = time.monotonic()
                writing.result()
                timings.waiting_for_writes += time.monotonic() - started
        finally:
            stop.set()
            fetcher.join(timeout=FETCHER_JOIN_TIMEOUT)


def check_well(plan: WellPlan, state: WellState, src: zarr.Group, plate: Plate) -> list[Field]:
    """Read the well's metadata as a reader of the source would, and check the copy agreed with it.

    Returns the fields, from :func:`~minus80.parsers.fractal.plate.read_fields`,
    which checks every round's ``FOV_ROI_table`` against the first and every
    level's shape. Raises if the copy placed any field differently, if any
    array's chunks were decoded with an encoding other than its own, or if
    the output's channels would not match the source's rounds.
    """
    if plate.acquisitions != plan.rounds:
        raise ValueError(f"{plan.well}: rounds {plate.acquisitions}, the zip's members hold {plan.rounds}")
    fields = read_fields(plate, src, plan.well)
    for level, used in state.placed:
        if [(f.name, f.levels[level]) for f in used] != [(f.name, f.levels[level]) for f in fields]:
            raise ValueError(f"{plan.well}: level {level} was copied with other field windows than its metadata gives")
    for path, specs in state.used.items():
        own = state.specs.get(path)
        if own is None or any(not spec.same_encoding(own) for spec in specs):
            raise ValueError(f"{path}: its chunks were decoded with an encoding other than its own .zarray's")
    check_codecs(src, plan.well, plate)
    channels = round_channels(plate, src, plan.well)
    for rnd in plate.acquisitions:
        if sum(c["round"] == rnd for c in channels) != plan.channels[rnd]:
            raise ValueError(f"{plan.well}: round {rnd}'s omero channels differ from its chunks' channels")
    return fields


def write_image_metadata(
    dest: zarr.Group,
    plate: Plate,
    src: zarr.Group,
    url: str,
    well: str,
    field: Field,
    channels: Sequence[dict],
    labels: dict[str, int],
    missing: Sequence[str],
    unreadable: Mapping[str, str],
) -> None:
    """Write the attributes of one embryo's image and its labels, after its pixels.

    The image's channel axis holds ``channels`` in order, every channel of
    every round.
    """
    shapes = [(len(channels), *level.shape[1:]) for level in field.levels]
    omero_channels = [{k: v for k, v in c.items() if k not in ("round", "source_channel")} for c in channels]
    image = dest[f"{well}/{image_path(field)}"]
    image.attrs.update(
        {
            "ome": image_attrs(
                name=f"{well} {field.name}",
                dims=IMAGE_DIMS,
                level_shapes=shapes,
                scale=field.levels[0].scale,
                units=plate.units,
                version=VERSION,
                omero={"channels": omero_channels},
                level_scales=[level.scale for level in field.levels],
                level_translations=[level.translation for level in field.levels],
                description=(
                    "Levels 1 to 3 are the source's levels of the whole well image, "
                    "cut to the coarse pixels that this field covers."
                ),
            ),
            **image_annotation(
                {
                    "well": well,
                    "field": field.name,
                    "channels": [
                        {
                            "label": c["label"],
                            "round": c["round"],
                            "source_image": f"{well}/{c['round']}",
                            "source_channel": c["source_channel"],
                        }
                        for c in channels
                    ],
                    "FOV_ROI_table": field.roi,
                    "empty_source_chunks": list(missing),
                    "unreadable_source_chunks": dict(sorted(unreadable.items())),
                },
                source_url=url,
            ),
        }
    )
    if not labels:
        return
    group = image.require_group("labels")
    group.attrs["ome"] = labels_attrs(list(labels), version=VERSION)
    for name, rnd in labels.items():
        well_image = src[f"{well}/{rnd}"]
        source = well_image[f"labels/{name}"]
        # The labels are cut with the image's windows and placed with the
        # image's translations. That is only correct if each label level has
        # the same shape and the same pixel size as the image level.
        image_scales = [{d: s[d] for d in LABEL_DIMS} for s in level_scales(well_image, IMAGE_DIMS)]
        if level_scales(source, LABEL_DIMS) != image_scales:
            raise ValueError(f"{well}/{rnd}/labels/{name}: pixel sizes differ from the image's")
        for k in range(plate.levels):
            if source[str(k)].shape != well_image[str(k)].shape[1:]:
                raise ValueError(f"{well}/{rnd}/labels/{name}: level {k} is not on the image's grid")
        group[name].attrs.update(
            {
                "ome": label_attrs(
                    name=name,
                    dims=LABEL_DIMS,
                    level_shapes=[level.shape[1:] for level in field.levels],
                    scale={d: field.levels[0].scale[d] for d in LABEL_DIMS},
                    units=plate.units,
                    version=VERSION,
                    level_scales=[{d: lvl.scale[d] for d in LABEL_DIMS} for lvl in field.levels],
                    level_translations=[{d: lvl.translation[d] for d in LABEL_DIMS} for lvl in field.levels],
                ),
                **image_annotation({"round": rnd, "source_label": f"{well}/{rnd}/labels/{name}"}, source_url=url),
            }
        )


def well_document(plate: Plate, src: zarr.Group, url: str, well: str, fields: Sequence[Field]) -> dict:
    """The attributes of one well group.

    They hold three things:

    - the ``well`` metadata, which lists the well's images, one per embryo;
    - the ``scene``, which places each image in the well's coordinate system;
    - the rows of the tables that describe the whole well.

    An image's place in the scene is its field's position on the source's
    well image.
    """
    translations = {
        image_path(field): {
            "c": 0.0,
            "z": field.roi["z_micrometer"],
            "y": field.roi["y_micrometer"],
            "x": field.roi["x_micrometer"],
        }
        for field in fields
    }

    # The OME-Zarr 0.6 schemas allow one group to be both a well and a scene.
    # The well schema does not forbid a "scene" key, and the scene schema does
    # not forbid a "well" key. So the well group carries both, and the
    # document is checked against both schemas.
    well_metadata = well_attrs([image_path(field) for field in fields], version=VERSION)["ome"]
    scene = scene_attrs(translations, name=SCENE, dims=IMAGE_DIMS, units=plate.units)
    ome = well_metadata | {"scene": scene["ome"]["scene"]}
    validate(ome, VERSION, model="well")
    validate(ome, VERSION, model="scene")

    tables = {}
    for name in WELL_TABLES:
        copies = {
            rnd: read_table(src[f"{well}/{rnd}/{TABLES}/{name}"])
            for rnd in plate.acquisitions
            if f"{well}/{rnd}/{TABLES}/{name}" in src
        }
        if not copies:
            continue
        first, table = next(iter(copies.items()))
        for rnd, other in copies.items():
            if not other.sort_index().equals(table.sort_index()):
                raise ValueError(f"{well}: round {rnd}'s {name} differs from round {first}'s")
        tables[name] = {
            "source": f"{well}/{first}/{TABLES}/{name}",
            "rows": {str(i): {k: float(v) for k, v in row.items()} for i, row in table.iterrows()},
        }
    return {"ome": ome, **image_annotation({"source_well": well, **tables}, source_url=url)}


def committed_wells(root: zarr.Group, wells: Sequence[str]) -> list[str]:
    """The wells already on the branch, in plate order.

    A well counts as committed when its group has its ``well`` metadata,
    because that metadata is written in the same commit as the well's pixels.
    """
    return [w for w in wells if w in root and "well" in dict(root[w].attrs).get("ome", {})]


def rounds_by_well(root: zarr.Group, wells: Sequence[str]) -> dict[str, dict[str, list[str]]]:
    """Each well's channel names by round, from the channel annotations of the well's first image."""
    found = {}
    for well in wells:
        first = dict(root[well].attrs)["ome"]["well"]["images"][0]["path"]
        channels = dict(root[f"{well}/{first}"].attrs)["annotations"]["channels"]
        by_round: dict[str, list[str]] = {}
        for channel in channels:
            by_round.setdefault(str(channel["round"]), []).append(channel["label"])
        found[well] = by_round
    return found


def recorded_rounds(by_well: Mapping[str, Mapping[str, list[str]]]) -> dict:
    """Each round's channel names: one list where every well agrees, else one list per well.

    Round 3 stains different markers in different wells (LAYOUTS.md), so a
    single list per round would be wrong for some wells.
    """
    rounds = {}
    for rnd in sorted({r for by_round in by_well.values() for r in by_round}, key=int):
        per_well = {well: by_round[rnd] for well, by_round in by_well.items() if rnd in by_round}
        distinct = {tuple(names) for names in per_well.values()}
        rounds[rnd] = list(next(iter(distinct))) if len(distinct) == 1 else per_well
    return rounds


def write_plate(repo, branch: str, plate_metadata: Mapping, url: str) -> None:
    """Commit plate metadata that lists exactly the wells on the branch now.

    The list of wells is read from the same session that commits the plate.
    So the list cannot be out of date when it is committed.

    Two runs might write the plate at the same moment. In that case the
    second commit conflicts with the first one on the plate's attributes, and
    Icechunk raises an error. Running either one again writes the plate.

    The plate declares no acquisitions. In OME-Zarr an image belongs to one
    acquisition, and each image here holds all four rounds. The rounds and
    their channels are recorded in the plate's ``source`` attributes instead,
    together with the acquisitions the source's own plate listed.
    """
    session = repo.writable_session(branch)
    root = zarr.open_group(session.store, mode="a", zarr_format=3)
    wells = committed_wells(root, [w["path"] for w in plate_metadata["wells"]])
    if not wells:
        return
    most_fields = max(len(dict(root[w].attrs)["ome"]["well"]["images"]) for w in wells)
    document = plate_attrs(
        name=plate_metadata["name"],
        rows=[row["name"] for row in plate_metadata["rows"]],
        columns=[column["name"] for column in plate_metadata["columns"]],
        wells=[tuple(w.split("/")) for w in wells],
        field_count=most_fields,
        version=VERSION,
    )
    document["source"] = {
        **STUDY,
        "url": url,
        "layout": (
            "One image per field of view (one embryo), cut from the source's well-sized "
            "images and placed in each well by its scene. Each image's channels are every "
            "channel of every staining round, in round order."
        ),
        "rounds": recorded_rounds(rounds_by_well(root, wells)),
        "source_acquisitions": [a["id"] for a in plate_metadata.get("acquisitions", [])],
    }
    if dict(root.attrs) != document:
        root.attrs.update(document)
        commit(session, f"Plate metadata for {len(wells)} wells")


def say(line: str) -> None:
    """Print one progress line at once, with the time, so a remote run's log shows where it is."""
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


def open_repository(location: str):
    """Open an Icechunk repo at a local path, creating it if needed, or open an Arraylake repo."""
    import icechunk

    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location)
    return icechunk.Repository.open_or_create(storage_for(location))


def plate_documents(url: str, directory: pa.Table, registry=None) -> dict[str, bytes]:
    """The plate's own metadata documents and its rows' group markers, which sit between wells.

    A handful of small members, fetched once at the start, one request each.
    """
    names = [n for n in directory["name"].to_pylist() if n.count("/") <= 1]
    return read_members(url, names, directory, registry=registry)


class WellsFailed(Exception):
    """Some wells failed; the others were committed and the plate lists them."""

    def __init__(self, failed: Mapping[str, str]) -> None:
        self.failed = dict(failed)
        super().__init__(f"{len(failed)} wells failed: {', '.join(failed)}")


def convert_well(
    url: str,
    repo,
    branch: str,
    plan: WellPlan,
    documents: Mapping[str, bytes],
    counters: Counters,
    timings: Timings,
    **copy,
) -> None:
    """Copy one well, check it against its metadata, write its metadata and commit it."""
    well = plan.well
    state = WellState(plan, documents)
    session = repo.writable_session(branch)
    dest = zarr.open_group(session.store, mode="a", zarr_format=3)
    copy_well(url, plan, state, dest, timings=timings, counters=counters, **copy)
    counters.stage = "checking metadata"
    src = documents_store(state.documents)
    plate = read_plate(src, wells=[well])
    fields = check_well(plan, state, src, plate)
    channels = round_channels(plate, src, well)
    labels = label_names(plate, src, well)
    counters.stage = "writing image metadata"
    for field in fields:
        missing = empty_source_chunks(plan, src, field)
        unreadable = state.unreadable.get(field.name, {})
        write_image_metadata(dest, plate, src, url, well, field, channels, labels, missing, unreadable)
        if unreadable:
            say(f"  {field.name}: {len(unreadable)} unreadable source chunks, read as 0")
    counters.stage = "writing well metadata"
    dest[well].attrs.update(well_document(plate, src, url, well, fields))
    counters.stage = "committing"
    # A commit writes one manifest per array, so its cost grows with the
    # number of arrays (docs/ingest.md). Timing it shows that cost early.
    committing = time.monotonic()
    commit(session, f"Add well {well}: {len(fields)} embryos x {len(channels)} channels")
    timings.commit = time.monotonic() - committing


def convert(
    url: str,
    repo,
    directory_cache: Path,
    *,
    branch: str = "main",
    wells: Sequence[str] | None = None,
    shard_depth: int = SHARD_DEPTH,
    read_concurrency: int = READ_CONCURRENCY,
    range_bytes: int = RANGE_BYTES,
    decode_threads: int | None = None,
    upload_workers: int = UPLOAD_WORKERS,
    zarr_concurrency: int = ZARR_CONCURRENCY,
    waiting_limit: int = WAITING_LIMIT,
    registry=None,
) -> list[str]:
    """Convert some or all wells of the zip at ``url`` into ``repo``.

    Each well becomes one commit on ``branch``. Wells already on the branch
    are skipped. With ``wells`` left out, every well of the plate is
    converted. A well that fails is reported, and the run goes on to the
    next. The run ends by writing the plate's metadata, which lists every
    well on the branch. Returns the wells this run converted, or raises
    :class:`WellsFailed` after the plate's metadata if any well failed.

    ``directory_cache`` is the Parquet file that keeps the zip's central
    directory. Keep a copy of it: it records exactly what the zip held.

    ``read_concurrency`` is how many range requests are in flight. Use
    ``EBI_READ_CONCURRENCY`` when ``url`` is EBI's own copy of the zip.
    ``upload_workers`` is how many fields are assigned through zarr at once,
    and ``zarr_concurrency`` sets zarr's ``async.concurrency`` for the run.
    ``waiting_limit`` caps the compressed bytes of channel runs waiting for
    metadata. ``registry`` decides how ``url`` is read, as for any minus80
    reader.
    """
    started = time.monotonic()
    say(
        f"converting {url}: {read_concurrency} range requests of {range_bytes >> 20} MiB in flight, "
        f"{decode_threads or 'default'} decode threads, {upload_workers} upload workers, "
        f"zarr async concurrency {zarr_concurrency}, waiting limit {waiting_limit / 1e9:.1f} GB"
    )
    say(f"reading the zip's central directory (kept at {directory_cache})")
    directory = cached_central_directory(url, directory_cache, registry=registry)
    say(f"  {directory.num_rows:,} members, in {time.monotonic() - started:.0f} s")
    documents = plate_documents(url, directory, registry=registry)
    plate_metadata = dict(documents_store(documents).attrs)["plate"]
    listed = [w["path"] for w in plate_metadata["wells"]]
    chosen = list(wells) if wells else listed
    if unknown := set(chosen) - set(listed):
        raise ValueError(f"not wells of the plate: {sorted(unknown)}")
    branch_root = zarr.open_group(repo.writable_session(branch).store, mode="a", zarr_format=3)
    done = committed_wells(branch_root, chosen)
    todo = [w for w in chosen if w not in done]
    say(f"converting {len(todo)} wells ({len(done)} already committed)")
    copy = {
        "shard_depth": shard_depth,
        "concurrency": read_concurrency,
        "range_bytes": range_bytes,
        "decode_threads": decode_threads,
        "upload_workers": upload_workers,
        "waiting_limit": waiting_limit,
        "registry": registry,
    }

    converted, failed = [], {}
    with zarr.config.set({"async.concurrency": zarr_concurrency}):
        for n, well in enumerate(todo, 1):
            well_started = time.monotonic()
            plan = WellPlan.from_directory(directory, well)
            size = plan.members[-1]["end"] - plan.members[0]["start"]
            say(f"well {well} ({n} of {len(todo)}): {len(plan.members):,} members, {size / 1e9:.2f} GB")
            counters, timings = Counters(well), Timings()
            try:
                with Heartbeat(counters.line):
                    convert_well(url, repo, branch, plan, documents, counters, timings, **copy)
            except Exception as error:  # noqa: BLE001 - one well's failure must not stop the others
                failed[well] = repr(error)
                say(f"  well {well} failed after {time.monotonic() - well_started:.0f} s: {error!r}")
                continue
            converted.append(well)
            say(f"  committed {well}: {timings.line(time.monotonic() - well_started)}; "
                f"peak memory so far {peak_memory_gb():.1f} GB")

    say("writing the plate metadata")
    write_plate(repo, branch, plate_metadata, url)
    say(f"wrote {len(converted)} wells in {time.monotonic() - started:.0f} s")
    if failed:
        raise WellsFailed(failed)
    return converted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", default=SOURCE, help="URL of the zip (default: EBI)")
    parser.add_argument("--repo", required=True, help="local directory or arraylake://org/repo")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--wells", help="comma-separated wells, such as C/02,E/05; default all")
    parser.add_argument("--shard-depth", type=int, default=SHARD_DEPTH)
    parser.add_argument(
        "--read-concurrency",
        type=int,
        help=f"range requests in flight (default {READ_CONCURRENCY}, or {EBI_READ_CONCURRENCY} "
        "when the source is EBI)",
    )
    parser.add_argument(
        "--directory-cache",
        type=Path,
        default=Path(cache_dir()) / "zebrafish-3d-4i" / "central-directory.parquet",
        help="the Parquet file that keeps the zip's central directory; keep a copy of it",
    )
    parser.add_argument(
        "--upload-workers", type=int, default=UPLOAD_WORKERS, help="fields assigned through zarr at once"
    )
    parser.add_argument(
        "--zarr-concurrency", type=int, default=ZARR_CONCURRENCY,
        help="zarr's async.concurrency: shards each assignment encodes and uploads at once",
    )
    parser.add_argument(
        "--waiting-limit-gb", type=float, default=WAITING_LIMIT / 1e9,
        help="the most compressed GB of channel runs that may wait for metadata",
    )
    args = parser.parse_args()
    try:
        convert(
            args.source,
            open_repository(args.repo),
            args.directory_cache,
            branch=args.branch,
            wells=args.wells.split(",") if args.wells else None,
            shard_depth=args.shard_depth,
            read_concurrency=args.read_concurrency
            or (EBI_READ_CONCURRENCY if "ebi.ac.uk" in args.source else READ_CONCURRENCY),
            upload_workers=args.upload_workers,
            zarr_concurrency=args.zarr_concurrency,
            waiting_limit=int(args.waiting_limit_gb * 1e9),
        )
    except WellsFailed as error:
        for well, reason in error.failed.items():
            say(f"failed: {well}: {reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
