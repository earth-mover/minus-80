"""A small zip with the structure of the S-BIAD3228 plate, for testing the ingest locally.

Everything the converter relies on is reproduced at a smaller size:

- an OME-Zarr 0.4 HCS plate in Zarr v2, zipped with Deflate into ZIP64
  members, each followed by a data descriptor;
- per well, one well image per round, ``(c, z, y, x)`` uint16, holding its
  fields pasted at their positions with zeros between them, in four pyramid
  levels that halve y and x;
- chunks of ``(1, 10, F, F)``: a field is one grid cell wide, and a z-depth
  (:data:`Z`) that is a multiple of neither the chunk nor the store's shards;
- level 0 storing only chunks that hold field pixels, levels 1 to 3 storing
  every chunk, zeros included;
- six labels on round 1, ``(z, y, x)`` uint32, with every embryo labeled 1 in
  ``embryoRaw``;
- the three AnnData ROI tables, with string indexes stored as Fractal's
  anndata stored them (newer anndata adds an empty ``raw`` array the source
  lacks);
- the plate and well metadata listing rounds 0 to 2 though every well holds 0
  to 3;
- ``FOV_ROI_table`` rows in a shuffled order, and a different one in round 3.

Field origins are whole pixels at level 0 and mostly not at the coarser
levels, some fields straddle two or four grid cells, and one sits on the well
image's top edge, as in the real plate.

    uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/fixture.py <dir>
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import anndata
import numcodecs
import numpy as np
import pandas as pd
import zarr

#: Field width and height, and the source's chunk width and height.
F = 40
#: Planes per field; the real 271 is likewise a multiple of neither 10 nor 32.
Z = 27
Z_CHUNK = 10
PIXEL = 0.65
LEVELS = 4
ROUNDS = 4
LISTED_ROUNDS = 3
CHANNELS = [
    ["DAPI_0", "Pol-II-S2P", "FLAG", "PCNA"],
    ["DAPI_1", "H3K27Ac", "bCatenin", "pH3"],
    ["DAPI_2", "H2B", "ALYREF", "Pol-II-S5P"],
    ["DAPI_3", "Nanog", "YAP", "XRN2"],
]
COLORS = ["0000FF", "FFFF00", "FF00FF", "00FF00"]
LABELS = ["cells", "cyto", "embryoRaw", "Pol-II-S2P_foci", "membrane", "nucleiRaw"]
LABEL_ROUND = 1
#: A label chunk the fixture stores as an empty file, as the real zip does.
EMPTY_CHUNK = "B/02/1/labels/cells/0/1/0/5"
#: An image chunk the fixture cuts short, as some real chunks are.
TRUNCATED_CHUNK = "C/03/2/0/1/0/0/1"
PLATE = "AssayPlate_Greiner_#655090"
ROWS = ["B", "C"]
COLUMNS = ["02", "03"]
#: Each well's image size at level 0 and its fields' (y, x) origins in pixels.
WELLS = {
    "B/02": ((151, 263), [(0, 205), (97, 3), (49, 123)]),
    "C/03": ((90, 130), [(13, 77), (50, 0)]),
}
ROI_COLUMNS = [
    "x_micrometer",
    "y_micrometer",
    "z_micrometer",
    "len_x_micrometer",
    "len_y_micrometer",
    "len_z_micrometer",
    "x_micrometer_original",
    "y_micrometer_original",
    "z_micrometer_original",
]


def field_pixels(well: str, field: int, rnd: int) -> np.ndarray:
    """The ``(c, z, y, x)`` pixels the microscope took for one field in one round.

    Camera noise of 0 to 8 everywhere, as in the real fields, under a bright
    disc whose level differs by field, round and channel, so a pixel copied
    from the wrong place cannot match.
    """
    rng = np.random.default_rng([sum(map(ord, well)), field, rnd])
    pixels = rng.integers(0, 9, (4, Z, F, F), dtype=np.uint16)
    z, y, x = np.ogrid[:Z, :F, :F]
    disc = (y - F / 2) ** 2 + (x - F / 2) ** 2 < (F / 3) ** 2
    for c in range(4):
        pixels[c][np.broadcast_to(disc, (Z, F, F))] += np.uint16(100 * (field + 1) + 10 * rnd + c)
    return pixels


def field_labels(field: int) -> dict[str, np.ndarray]:
    """The ``(z, y, x)`` labels of one field, with the top and bottom planes empty."""
    z, y, x = np.ogrid[:Z, :F, :F]
    inside = ((y - F / 2) ** 2 + (x - F / 2) ** 2 < (F / 3) ** 2) & (z > 1) & (z < Z - 2)
    out = {}
    for i, name in enumerate(LABELS):
        values = np.zeros((Z, F, F), np.uint32)
        if name == "embryoRaw":
            values[inside] = 1
        else:
            # Objects change every few planes, as nuclei do through an embryo,
            # so a label copied upside down or shifted in z cannot match.
            objects = (z // 4 * 64 + y // 5 * 8 + x // 5 + 1 + 1000 * i + 10000 * field).astype(
                np.uint32
            )
            objects = np.broadcast_to(objects, (Z, F, F))
            values[inside] = objects[inside]
        out[name] = values
    return out


def _coarsen(level: np.ndarray, label: bool) -> np.ndarray:
    """The next level down, as the source's pyramid makes it: y and x halved, the size rounded down.

    Images average each 2 x 2 block. Labels keep the block's bottom-right
    pixel, which matches the real source's nucleiRaw level 1 exactly.
    """
    h, w = level.shape[-2] // 2 * 2, level.shape[-1] // 2 * 2
    level = level[..., :h, :w]
    if label:
        return level[..., 1::2, 1::2]
    blocks = level.reshape(*level.shape[:-2], h // 2, 2, w // 2, 2).astype(np.float64)
    return blocks.mean(axis=(-3, -1)).astype(level.dtype)


def _levels(base: np.ndarray, label: bool) -> list[np.ndarray]:
    levels = [base]
    for _ in range(1, LEVELS):
        levels.append(_coarsen(levels[-1], label))
    return levels


def _multiscales(name: str, axes: list[str]) -> list[dict]:
    space = {"type": "space", "unit": "micrometer"}
    described = [{"name": a, **({"type": "channel"} if a == "c" else space)} for a in axes]
    datasets = []
    for k in range(LEVELS):
        scale = [1.0 if a in "cz" else PIXEL * 2**k for a in axes]
        datasets.append(
            {"coordinateTransformations": [{"scale": scale, "type": "scale"}], "path": str(k)}
        )
    return [{"axes": described, "datasets": datasets, "name": name, "version": "0.4"}]


def _write_levels(group: zarr.Group, levels: list[np.ndarray], chunks: tuple[int, ...]) -> None:
    for k, data in enumerate(levels):
        arr = zarr.create_array(
            group.store,
            name=f"{group.path}/{k}",
            shape=data.shape,
            chunks=chunks,
            dtype=data.dtype,
            fill_value=0,
            zarr_format=2,
            compressors=numcodecs.Blosc(cname="lz4", clevel=5, shuffle=numcodecs.Blosc.SHUFFLE),
            chunk_key_encoding={"name": "v2", "separator": "/"},
            config={"write_empty_chunks": k > 0},
        )
        arr[...] = data


def _roi_table(group: zarr.Group, name: str, rows: pd.DataFrame, attrs: dict) -> None:
    table = anndata.AnnData(
        X=rows[ROI_COLUMNS].to_numpy(np.float64),
        obs=pd.DataFrame(index=pd.Index(rows.index.astype(str), dtype=object)),
        var=pd.DataFrame(index=pd.Index(ROI_COLUMNS, dtype=object)),
    )
    table.obs.index.name = attrs["index_key"]
    anndata.io.write_elem(group, name, table)
    group[name].attrs.update(attrs)


def build(root: Path) -> dict:
    """Write the plate as a Zarr v2 directory under ``root``; return each field's ground truth."""
    # Fractal's anndata wrote string indexes as plain variable-length arrays.
    anndata.settings.zarr_write_format = 2
    anndata.settings.allow_write_nullable_strings = False
    store = zarr.storage.LocalStore(root / "plate.zarr")
    plate = zarr.open_group(store, mode="w", zarr_format=2)
    plate.attrs["plate"] = {
        "acquisitions": [{"id": r, "name": f"acquisition_{r}"} for r in range(LISTED_ROUNDS)],
        "columns": [{"name": c} for c in COLUMNS],
        "name": PLATE,
        "rows": [{"name": r} for r in ROWS],
        "version": "0.4",
        "wells": [
            {
                "columnIndex": COLUMNS.index(w.split("/")[1]),
                "path": w,
                "rowIndex": ROWS.index(w.split("/")[0]),
            }
            for w in WELLS
        ],
    }
    truth = {}
    for well, ((h, w), origins) in WELLS.items():
        wg = plate.require_group(well)
        wg.attrs["well"] = {
            "images": [{"acquisition": r, "path": str(r)} for r in range(LISTED_ROUNDS)],
            "version": "0.4",
        }
        fovs = pd.DataFrame(
            [
                {
                    "x_micrometer": x * PIXEL,
                    "y_micrometer": y * PIXEL,
                    "z_micrometer": 0.0,
                    "len_x_micrometer": F * PIXEL,
                    "len_y_micrometer": F * PIXEL,
                    "len_z_micrometer": float(Z),
                    "x_micrometer_original": x * PIXEL / 2,
                    "y_micrometer_original": y * PIXEL / 2,
                    "z_micrometer_original": 0.0,
                }
                for y, x in origins
            ],
            index=[f"FOV_{i + 1}" for i in range(len(origins))],
        )
        whole = pd.DataFrame(
            [
                {
                    **dict.fromkeys(ROI_COLUMNS, 0.0),
                    "len_x_micrometer": w * PIXEL,
                    "len_y_micrometer": h * PIXEL,
                    "len_z_micrometer": float(Z),
                }
            ],
            index=["well_1"],
        )
        for rnd in range(ROUNDS):
            image = np.zeros((4, Z, h, w), np.uint16)
            for i, (y, x) in enumerate(origins):
                pixels = field_pixels(well, i, rnd)
                image[:, :, y : y + F, x : x + F] = pixels
                truth[(well, i, rnd)] = pixels
            ig = wg.require_group(str(rnd))
            ig.attrs["multiscales"] = _multiscales(f"{well} round {rnd}", ["c", "z", "y", "x"])
            ig.attrs["omero"] = {
                "channels": [
                    {
                        "active": True,
                        "color": COLORS[c],
                        "label": CHANNELS[rnd][c],
                        "wavelength_id": f"A0{c + 1}_C0{c + 1}",
                        "window": {"end": 600.0, "max": 65535.0, "min": 0.0, "start": 1.0},
                    }
                    for c in range(4)
                ]
            }
            _write_levels(ig, _levels(image, label=False), (1, Z_CHUNK, F, F))
            tables = ig.require_group("tables")
            names = ["well_ROI_table", "FOV_ROI_table"]
            fractal = {"backend": "anndata_v1", "table_version": "1"}
            _roi_table(
                tables,
                "well_ROI_table",
                whole,
                {**fractal, "type": "roi_table", "index_key": "FieldIndex", "index_type": "str"},
            )
            # The real tables list fields in no fixed order, and round 3's in
            # a different order from the other rounds'.
            n = len(fovs)
            order = [(i + 1) % n for i in range(n)] if rnd == 3 else list(range(n))[::-1]
            _roi_table(
                tables,
                "FOV_ROI_table",
                fovs.iloc[order],
                {**fractal, "type": "roi_table", "index_key": "FieldIndex", "index_type": "str"},
            )
            if rnd == LABEL_ROUND:
                lg = ig.require_group("labels")
                lg.attrs["labels"] = LABELS
                per_field = {i: field_labels(i) for i in range(len(origins))}
                for name in LABELS:
                    well_image = np.zeros((Z, h, w), np.uint32)
                    for i, (y, x) in enumerate(origins):
                        well_image[:, y : y + F, x : x + F] = per_field[i][name]
                        truth[(well, i, name)] = per_field[i][name]
                    g = lg.require_group(name)
                    g.attrs.update(
                        {
                            "image-label": {"source": {"image": "../../"}, "version": "0.4"},
                            "multiscales": _multiscales(name, ["z", "y", "x"]),
                        }
                    )
                    _write_levels(g, _levels(well_image, label=True), (Z_CHUNK, F, F))
                box = pd.DataFrame(
                    [
                        {
                            **dict.fromkeys(ROI_COLUMNS, 0.0),
                            "x_micrometer": min(x for _, x in origins) * PIXEL,
                            "y_micrometer": min(y for y, _ in origins) * PIXEL,
                            "len_x_micrometer": (
                                max(x for _, x in origins) + F - min(x for _, x in origins)
                            )
                            * PIXEL,
                            "len_y_micrometer": (
                                max(y for y, _ in origins) + F - min(y for y, _ in origins)
                            )
                            * PIXEL,
                            "len_z_micrometer": float(Z),
                        }
                    ],
                    index=[1],
                )
                _roi_table(
                    tables,
                    "embryoRaw_ROI_table",
                    box,
                    {
                        **fractal,
                        "type": "masking_roi_table",
                        "index_key": "label",
                        "index_type": "int",
                        "instance_key": "label",
                        "region": {"path": "../labels/embryoRaw"},
                    },
                )
                names.append("embryoRaw_ROI_table")
            tables.attrs["tables"] = names
    # The real zip holds some chunks inside embryos as empty files. One here:
    # planes 10 to 19 of label "cells" at level 0, in the grid cell that holds
    # most of B/02 FOV_1 (x 205 to 245, grid columns 5 and 6). An empty chunk
    # reads as the fill value, so the ground truth is 0 there too.
    empty = root / "plate.zarr" / EMPTY_CHUNK
    empty.write_bytes(b"")
    y, x = WELLS["B/02"][1][0]
    truth[("B/02", 0, "cells")][10:20, :, : 6 * F - x] = 0
    # Some real chunks are cut short. One here: channel 1 of round 2, planes 0
    # to 9, the grid cell at y 0 to 39 and x 40 to 79, which holds the corner
    # of C/03 FOV_1 (y 13, x 77).
    cut = root / "plate.zarr" / TRUNCATED_CHUNK
    cut.write_bytes(cut.read_bytes()[:64])
    y, x = WELLS["C/03"][1][0]
    truth[("C/03", 0, 2)][1, 0:10, 0 : F - y, 0 : 2 * F - x] = 0
    return truth


class _Unseekable(io.RawIOBase):
    """A write-only stream: zipfile then writes each member with a data descriptor."""

    def __init__(self, f):
        self._f = f

    def writable(self):
        return True

    def write(self, b):
        return self._f.write(b)


#: The order the real zip stores rounds in, and channels within an array. It is
#: neither numeric nor alphabetical, and the converter must not assume one.
ARCHIVE_ROUNDS = [2, 3, 0, 1]
ARCHIVE_CHANNELS = [2, 3, 0, 1]
#: Within a channel, the real zip's z indices come in a fixed scrambled order.
ARCHIVE_Z = [2, 0, 1]
#: Within a round: its attributes, levels 2 and 3, its tables, its labels,
#: its group marker, then levels 0 and 1. The tables come after the coarse
#: levels, so field positions arrive after the first chunks.
ARCHIVE_SECTIONS = {".zattrs": 0, "2": 1, "3": 2, "tables": 3, "labels": 4, ".zgroup": 5, "0": 6, "1": 7}


def archive_order(name: str) -> tuple:
    """A sort key that puts the plate's members in the order S-BIAD3228's zip stores them.

    Measured on the real zip (well C/02): wells one after another; within a
    well, rounds 2, 3, 0, 1; within a round, the sections in
    :data:`ARCHIVE_SECTIONS`; within an array, channels 2 and 3, then the
    array's ``.zarray``, then channels 0 and 1, each channel's z indices in a
    scrambled order. Plate-level documents come first here; in the real zip
    they sit between wells, which the converter reads separately.
    """
    # Every key is (well, round, section, label, level, position in array, name),
    # with -1 or "" where a part does not apply, so any two keys compare.
    parts = name.split("/")
    if len(parts) < 3:
        return ("", -1, -1, "", -1, (), name)
    well, rest = "/".join(parts[:2]), parts[2:]
    if rest[0].startswith("."):
        return (well, -1, -1, "", -1, (), name)
    round_rank = ARCHIVE_ROUNDS.index(int(rest[0]))
    section = rest[1]
    if section == "tables" or section.startswith("."):
        return (well, round_rank, ARCHIVE_SECTIONS[section], "", -1, (), name)
    if section == "labels":
        if len(rest) < 4 or rest[3].startswith("."):
            return (well, round_rank, ARCHIVE_SECTIONS["labels"], "/".join(rest[2:3]), -1, (), name)
        label, level, inner = rest[2], rest[3], rest[4:]
        return (well, round_rank, ARCHIVE_SECTIONS["labels"], label, ARCHIVE_SECTIONS[level],
                _within_array(inner, channels=False), name)
    return (well, round_rank, ARCHIVE_SECTIONS[section], "", -1, _within_array(rest[2:], channels=True), name)


def _within_array(inner: list[str], *, channels: bool) -> tuple:
    """The order of an array's members: its ``.zarray`` after the first half of its chunks."""
    if inner[0].startswith("."):
        return (1.5 if channels else 0.5,)
    if channels:
        c, z, *cell = (int(i) for i in inner)
        return (ARCHIVE_CHANNELS.index(c), ARCHIVE_Z.index(z), *cell)
    z, *cell = (int(i) for i in inner)
    return (ARCHIVE_Z.index(z), *cell)


def zip_plate(root: Path) -> Path:
    """Zip ``root/plate.zarr`` as the deposit is zipped.

    Every member is Deflate-compressed, ZIP64, and followed by a data
    descriptor, like every member of the real zip, and the members come in
    the real zip's order (:func:`archive_order`).
    """
    source = root / "plate.zarr"
    path = root / "plate.zarr.zip"
    names = sorted(
        (member.relative_to(source).as_posix() for member in source.rglob("*") if member.is_file()),
        key=archive_order,
    )
    with open(path, "wb") as f, zipfile.ZipFile(_Unseekable(f), "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            with zf.open(name, "w", force_zip64=True) as out:
                out.write((source / name).read_bytes())
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    build(out)
    print(zip_plate(out))
