"""Convert the 3D-4i zebrafish plate (BioImage Archive S-BIAD3228) into a native OME-Zarr 0.6 store.

The deposit is one zip of an OME-Zarr 0.4 HCS plate whose members are
Deflate-compressed, so its chunks cannot be referenced in place: this ingest
reads the pixels and writes them again. The source pastes each well's fields
of view onto one well-sized image per staining round; the store gives each
field and round an image of its own, the layout OME-Zarr's plate defines for
fields, and places it in its well with a 0.6 scene. PLAN.md gives the reasons
and LAYOUTS.md the mapping.

- ``<row>/<column>``: a well, carrying both the HCS ``well`` metadata and a
  ``scene`` whose ``well`` coordinate system places every image in it;
- ``<row>/<column>/<image>``: one field in one round, ``(c, z, y, x)`` uint16,
  image ``field_position * rounds + round``; levels 1 to 3 are the source's
  own pixels, cut to the coarse pixels the field touches;
- ``<row>/<column>/<image>/labels/<name>``: the round's segmentations cut to the
  field, ``(z, y, x)`` uint32.

Each well is one commit, so a run that stops resumes at the first well not yet
committed. The plate metadata is written last, once every well is in. One
process copies one image at a time, one channel of one level per zarr call,
and zarr keeps many source reads in flight within each call; to go faster,
run more processes on separate ``--wells``, whose commits rebase onto each
other's.

    uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/ingest.py \\
        --source <zip URL> --repo <dir | arraylake://org/repo> [--wells C/02,E/05]
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import anndata
import numpy as np
import zarr
from minus80.core.fetch import cache_dir
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, commit, storage_for
from minus80.core.progress import Progress
from minus80.ome import (image_annotation, image_attrs, label_attrs, labels_attrs, plate_attrs,
                         scene_attrs, well_attrs)
from minus80.ome.image import validate
from minus80.parsers.zip.members import zip_index
from minus80.parsers.zip.store import MemberStore
from virtualizarr.parsers.zarr.zip import ZipEntry
from zarr.codecs import BloscCodec

SOURCE = ("https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/228/S-BIAD3228/Files/"
          "Multiplexeddata/AssayPlate_Greiner_655090.zarr.zip")
STUDY = {
    "study": "https://www.ebi.ac.uk/biostudies/bioimages/studies/S-BIAD3228",
    "preprint": "https://www.biorxiv.org/content/10.1101/2025.10.27.684723v1",
    "license": "CC0-1.0",
}
VERSION = "0.6"
IMAGE_DIMS = ("c", "z", "y", "x")
LABEL_DIMS = ("z", "y", "x")
SCENE = "well"
#: Planes per shard of an image level: decision 5 in PLAN.md. Labels compress
#: to a few KB a plane, so their shards span every plane.
SHARD_DEPTH = 32
CODEC = BloscCodec(cname="zstd", clevel=5, shuffle="bitshuffle")
#: Source reads in flight. Each is one zip member of a few MB, and zarr's
#: default of 10 leaves the network idle.
READ_CONCURRENCY = 64
ROI_TABLE = "FOV_ROI_table"
WELL_TABLES = ("well_ROI_table", "embryoRaw_ROI_table")
#: How far a field origin may sit from a whole pixel before it is an error,
#: in pixels: the tables store micrometres as float64.
WHOLE_PIXEL = 1e-6


@dataclass(frozen=True)
class Level:
    """One pyramid level of one field: where it is cut from the source level, and where it sits."""

    index: int
    #: ``(y0, y1, x0, x1)`` in the source level's pixels.
    window: tuple[int, int, int, int]
    #: Pixel size and first pixel centre, per axis, in the field's own coordinates.
    scale: dict[str, float]
    translation: dict[str, float]

    @property
    def shape(self) -> tuple[int, int]:
        y0, y1, x0, x1 = self.window
        return y1 - y0, x1 - x0


@dataclass(frozen=True)
class Field:
    """One field of view of a well, as its ``FOV_ROI_table`` row places it."""

    name: str
    position: int
    #: ``(y, x)`` of its first pixel on the source's level 0, and its size.
    origin: tuple[int, int]
    size: tuple[int, int]
    roi: dict[str, float]
    levels: tuple[Level, ...]


@dataclass(frozen=True)
class Source:
    """The facts about the source plate that every well shares."""

    url: str
    plate: dict
    wells: tuple[str, ...]
    rounds: tuple[int, ...]
    #: Planes per field, and the pixel size per axis at level 0.
    depth: int
    scale: dict[str, float]
    levels: int
    units: dict[str, str]


def load_index(url: str, cache: Path | None) -> dict[str, ZipEntry]:
    """Every member of the zip, read once from its central directory and kept in ``cache``.

    The central directory of the real archive is 178 MB for two million
    members, so a rerun reads it from the cache instead.
    """
    if cache is not None and cache.exists():
        with np.load(cache, allow_pickle=False) as saved:
            if str(saved["url"]) != url:
                raise ValueError(f"{cache} indexes {saved['url']}, not {url}")
            return {str(n): ZipEntry(int(o), int(c), int(u), int(m)) for n, o, c, u, m in
                    zip(saved["names"], saved["offsets"], saved["compressed"],
                        saved["uncompressed"], saved["methods"])}
    index = zip_index(url)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        entries = list(index.values())
        np.savez(cache, url=url, names=np.array(list(index)),
                 offsets=np.array([e.data_offset for e in entries], np.uint64),
                 compressed=np.array([e.compressed_length for e in entries], np.uint64),
                 uncompressed=np.array([e.uncompressed_length for e in entries], np.uint64),
                 methods=np.array([e.method for e in entries], np.uint16))
    return index


def metadata_store(url: str, index: Mapping[str, ZipEntry]) -> MemberStore:
    """A store over the zip's metadata documents and ROI tables, for the driver to plan from."""
    return MemberStore(url, {k: v for k, v in index.items()
                             if k.rsplit("/", 1)[-1] in (".zattrs", ".zgroup", ".zarray") or "/tables/" in k})


def read_source(url: str, index: Mapping[str, ZipEntry]) -> tuple[Source, zarr.Group]:
    """The plate's shared geometry, checked against every image, and its root group."""
    root = zarr.open_group(metadata_store(url, index), mode="r", zarr_format=2)
    plate = dict(root.attrs)["plate"]
    wells = tuple(w["path"] for w in plate["wells"])
    found = {"/".join(k.split("/")[:2]) for k in index if k.count("/") >= 2 and k.split("/")[2] == ".zattrs"}
    if unlisted := found - set(wells):
        raise ValueError(f"well groups the plate does not list: {sorted(unlisted)}")
    rounds = None
    geometry = None
    for well in wells:
        present = tuple(sorted(int(r) for r in root[well].group_keys() if r.isdigit()))
        if rounds is None:
            rounds = present
        elif present != rounds:
            raise ValueError(f"{well} has rounds {present}, {wells[0]} has {rounds}")
        for rnd in present:
            found_geometry = _image_geometry(root[f"{well}/{rnd}"])
            if geometry is None:
                geometry = found_geometry
            elif found_geometry != geometry:
                raise ValueError(f"{well}/{rnd}: {found_geometry} differs from {geometry}")
    depth, scale, levels, units = geometry
    return Source(url=url, plate=plate, wells=wells, rounds=rounds, depth=depth, scale=scale,
                  levels=levels, units=units), root


def _image_geometry(group: zarr.Group) -> tuple:
    """Planes, level-0 pixel size, level count and units, after checking each level halves y and x.

    The source's levels are its well image downsampled twice in y and x per
    level with the size rounded down; a field can be cut from them only if
    that holds, so anything else raises.
    """
    (ms,) = group.attrs["multiscales"]
    axes = [a["name"] for a in ms["axes"]]
    if axes != list(IMAGE_DIMS):
        raise ValueError(f"{group.path}: axes {axes}, expected {list(IMAGE_DIMS)}")
    scales = [d["coordinateTransformations"][0]["scale"] for d in ms["datasets"]]
    if any(len(d["coordinateTransformations"]) != 1 for d in ms["datasets"]):
        raise ValueError(f"{group.path}: a level carries more than a scale")
    shapes = [group[d["path"]].shape for d in ms["datasets"]]
    for k in range(1, len(scales)):
        expected = [scales[0][0], scales[0][1], scales[0][2] * 2 ** k, scales[0][3] * 2 ** k]
        if not np.allclose(scales[k], expected, rtol=0, atol=1e-9):
            raise ValueError(f"{group.path}: level {k} scale {scales[k]}, expected {expected}")
        prev, here = shapes[k - 1], shapes[k]
        if here != (prev[0], prev[1], prev[2] // 2, prev[3] // 2):
            raise ValueError(f"{group.path}: level {k} shape {here} is not level {k - 1}'s {prev} halved")
    units = {a["name"]: a["unit"] for a in ms["axes"] if a.get("unit")}
    return shapes[0][1], dict(zip(IMAGE_DIMS, scales[0])), len(scales), units


def _table(group: zarr.Group):
    """An AnnData ROI table as a DataFrame: one row per region, one column per variable."""
    table = anndata.io.read_elem(group)
    return table.to_df()


def read_fields(source: Source, root: zarr.Group, well: str) -> list[Field]:
    """The well's fields from its ``FOV_ROI_table``, which must agree across rounds.

    Every origin must be a whole level-0 pixel and every field the same size
    and depth; the windows at levels 1 to 3 cover every coarse pixel the
    field touches that the source stored.
    """
    tables = [_table(root[f"{well}/{r}/tables/{ROI_TABLE}"]) for r in source.rounds]
    for r, t in zip(source.rounds[1:], tables[1:]):
        if not (t.index.equals(tables[0].index) and t.equals(tables[0])):
            raise ValueError(f"{well}: round {r}'s {ROI_TABLE} differs from round {source.rounds[0]}'s")
    rows = tables[0]
    shape0 = root[f"{well}/{source.rounds[0]}/0"].shape
    shapes = [root[f"{well}/{source.rounds[0]}/{k}"].shape for k in range(source.levels)]
    fields = []
    for position, (name, row) in enumerate(rows.iterrows()):
        roi = {k: float(v) for k, v in row.items()}
        oy, ox = (_pixels(well, name, roi[f"{a}_micrometer"], source.scale[a]) for a in "yx")
        ny, nx = (_pixels(well, name, roi[f"len_{a}_micrometer"], source.scale[a]) for a in "yx")
        if roi["z_micrometer"] != 0 or _pixels(well, name, roi["len_z_micrometer"], source.scale["z"]) != source.depth:
            raise ValueError(f"{well} {name}: z {roi['z_micrometer']} + {roi['len_z_micrometer']} is not "
                             f"the image's {source.depth} planes")
        if oy + ny > shape0[2] or ox + nx > shape0[3]:
            raise ValueError(f"{well} {name}: field runs past the well image {shape0[2:]}")
        levels = []
        for k, shape in enumerate(shapes):
            f = 2 ** k
            # The source rounds each level's size down, so a field on the well
            # image's far edge can touch a coarse pixel the source never stored;
            # the window stops at the level's edge.
            y0, y1 = oy // f, min(-(-(oy + ny) // f), shape[2])
            x0, x1 = ox // f, min(-(-(ox + nx) // f), shape[3])
            scale = {"c": source.scale["c"], "z": source.scale["z"],
                     "y": source.scale["y"] * f, "x": source.scale["x"] * f}
            # A coarse pixel j averages fine pixels j*f to j*f + f - 1, so its
            # centre sits (f - 1) / 2 fine pixels past its first one.
            translation = {"c": 0.0, "z": 0.0,
                           "y": (y0 * f - oy + (f - 1) / 2) * source.scale["y"],
                           "x": (x0 * f - ox + (f - 1) / 2) * source.scale["x"]}
            levels.append(Level(k, (y0, y1, x0, x1), scale, translation))
        fields.append(Field(name=str(name), position=position, origin=(oy, ox), size=(ny, nx),
                            roi=roi, levels=tuple(levels)))
    return fields


def _pixels(well: str, field: str, micrometres: float, pixel: float) -> int:
    """A length in micrometres as whole pixels, or ``ValueError`` if it is not one."""
    n = micrometres / pixel
    if abs(n - round(n)) > WHOLE_PIXEL:
        raise ValueError(f"{well} {field}: {micrometres} µm is {n} pixels of {pixel} µm, not a whole number")
    return round(n)


def image_path(source: Source, field: Field, rnd: int) -> str:
    return str(field.position * len(source.rounds) + source.rounds.index(rnd))


def _create_level(group: zarr.Group, name: str, shape, chunks, shards, dtype, dims) -> None:
    group.create_array(name, shape=shape, chunks=chunks, shards=shards, dtype=dtype,
                       compressors=CODEC, fill_value=0, dimension_names=dims)


def define_well(dest: zarr.Group, source: Source, src: zarr.Group, well: str, fields: Sequence[Field],
                shard_depth: int) -> dict[tuple[int, int], tuple[str, ...]]:
    """Create the well's image and label groups, with their metadata and empty arrays.

    Returns each (field position, round)'s label names. The well's own
    metadata is written later, once its pixels are in, because its presence
    is what marks the well as done.
    """
    labels = {}
    for field in fields:
        for rnd in source.rounds:
            image = dest.require_group(f"{well}/{image_path(source, field, rnd)}")
            srcimg = src[f"{well}/{rnd}"]
            channels = srcimg["0"].shape[0]
            level_shapes = [(channels, source.depth, *lvl.shape) for lvl in field.levels]
            image.attrs.update({
                "ome": image_attrs(
                    name=f"{well} {field.name} round {rnd}", dims=IMAGE_DIMS, level_shapes=level_shapes,
                    scale=field.levels[0].scale, units=source.units, version=VERSION,
                    omero=dict(srcimg.attrs["omero"]),
                    level_scales=[lvl.scale for lvl in field.levels],
                    level_translations=[lvl.translation for lvl in field.levels],
                    description=("levels 1 to 3 are the source's levels of the whole well image, "
                                 "cut to the coarse pixels this field covers")),
                **image_annotation({"well": well, "field": field.name, "round": rnd,
                                    "source_image": f"{well}/{rnd}", ROI_TABLE: field.roi},
                                   source_url=source.url),
            })
            for lvl, shape in zip(field.levels, level_shapes):
                _create_level(image, str(lvl.index), shape, (1, 1, *shape[2:]), (1, shard_depth, *shape[2:]),
                              srcimg[str(lvl.index)].dtype, IMAGE_DIMS)
            names = tuple(srcimg["labels"].attrs["labels"]) if "labels" in srcimg else ()
            labels[(field.position, rnd)] = names
            if not names:
                continue
            group = image.require_group("labels")
            group.attrs["ome"] = labels_attrs(names, version=VERSION)
            for name in names:
                srclabel = srcimg[f"labels/{name}"]
                shapes = [(source.depth, *lvl.shape) for lvl in field.levels]
                for k, lvl in enumerate(field.levels):
                    if srclabel[str(k)].shape[1:] != srcimg[str(k)].shape[2:]:
                        raise ValueError(f"{well}/{rnd}/labels/{name} level {k} is not on the image's grid")
                label = group.require_group(name)
                label.attrs.update({
                    "ome": label_attrs(
                        name=name, dims=LABEL_DIMS, level_shapes=shapes,
                        scale={a: field.levels[0].scale[a] for a in LABEL_DIMS}, units=source.units,
                        version=VERSION,
                        level_scales=[{a: lvl.scale[a] for a in LABEL_DIMS} for lvl in field.levels],
                        level_translations=[{a: lvl.translation[a] for a in LABEL_DIMS} for lvl in field.levels]),
                    **image_annotation({"source_label": f"{well}/{rnd}/labels/{name}"}, source_url=source.url),
                })
                for lvl, shape in zip(field.levels, shapes):
                    _create_level(label, str(lvl.index), shape, (1, *shape[1:]), shape,
                                  srclabel[str(lvl.index)].dtype, LABEL_DIMS)
    return labels


def copy_image(src: MemberStore, dst, well: str, rnd: int, image: str, field: Field,
               labels: Sequence[str]) -> None:
    """Copy one field in one round, every level and label, from the zip into ``dst``.

    Each call writes a whole channel of a level, or a whole label level, so
    every shard is written once and never read back.
    """
    for lvl in field.levels:
        y0, y1, x0, x1 = lvl.window
        source = zarr.open_array(src, path=f"{well}/{rnd}/{lvl.index}", mode="r", zarr_format=2)
        target = zarr.open_array(dst, path=f"{well}/{image}/{lvl.index}", mode="r+")
        for c in range(source.shape[0]):
            target[c] = source[c, :, y0:y1, x0:x1]
        for name in labels:
            source = zarr.open_array(src, path=f"{well}/{rnd}/labels/{name}/{lvl.index}", mode="r", zarr_format=2)
            target = zarr.open_array(dst, path=f"{well}/{image}/labels/{name}/{lvl.index}", mode="r+")
            target[:] = source[:, y0:y1, x0:x1]


def well_document(source: Source, src: zarr.Group, well: str, fields: Sequence[Field]) -> dict:
    """The well's attributes: its images and their rounds, the scene placing them, and its other tables."""
    paths, acquisitions, translations = [], [], {}
    for field in fields:
        for rnd in source.rounds:
            path = image_path(source, field, rnd)
            paths.append(path)
            acquisitions.append(rnd)
            translations[path] = {"c": 0.0, "z": field.roi["z_micrometer"],
                                  "y": field.roi["y_micrometer"], "x": field.roi["x_micrometer"]}
    ome = (well_attrs(paths, acquisitions=acquisitions, version=VERSION)["ome"]
           | {"scene": scene_attrs(translations, name=SCENE, dims=IMAGE_DIMS,
                                   units=source.units)["ome"]["scene"]})
    validate(ome, VERSION, model="well")
    validate(ome, VERSION, model="scene")
    tables = {}
    for rnd in source.rounds:
        for name in WELL_TABLES:
            key = f"{well}/{rnd}/tables/{name}"
            if key in src:
                rows = _table(src[key])
                tables.setdefault(name, {"source": key, "rows": {
                    str(i): {k: float(v) for k, v in r.items()} for i, r in rows.iterrows()}})
    return {"ome": ome, **image_annotation({"source_well": well, **tables}, source_url=source.url)}


def plate_document(source: Source, wells: Sequence[str], field_counts: Sequence[int],
                   channels: Mapping[int, Sequence[str]]) -> dict:
    """The plate's attributes: the source's plate, every round listed, and where it came from."""
    plate = source.plate
    rows = [r["name"] for r in plate["rows"]]
    columns = [c["name"] for c in plate["columns"]]
    acquisitions = [{"id": r, "name": f"acquisition_{r}",
                     "description": f"staining round {r}: {', '.join(channels[r])}"} for r in source.rounds]
    document = plate_attrs(name=plate["name"], rows=rows, columns=columns,
                           wells=[tuple(w.split("/")) for w in wells], field_count=max(field_counts),
                           acquisitions=acquisitions, version=VERSION)
    listed = [a["id"] for a in plate.get("acquisitions", [])]
    return {**document, "source": {
        **STUDY, "url": source.url,
        "layout": ("one image per field of view and round, cut from the source's well-sized images "
                   "and placed in each well by its scene"),
        "source_acquisitions": listed,
    }}


def done_wells(repo, branch: str, wells: Sequence[str]) -> set[str]:
    """Wells already committed on ``branch``: those whose group carries its well metadata."""
    try:
        root = zarr.open_group(repo.readonly_session(branch).store, mode="r")
    except Exception:
        return set()
    done = set()
    for well in wells:
        try:
            if "well" in dict(root[well].attrs).get("ome", {}):
                done.add(well)
        except KeyError:
            pass
    return done


def open_repository(location: str):
    """An existing or new Icechunk repo at a local path, or an Arraylake repo."""
    import icechunk

    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location)
    return icechunk.Repository.open_or_create(storage_for(location))


def convert(source_url: str, repo, *, branch: str = "main", wells: Sequence[str] | None = None,
            shard_depth: int = SHARD_DEPTH, index_cache: Path | None = None) -> list[str]:
    """Convert ``wells`` (default all) into ``repo``, one commit per well; return the wells written.

    Wells already committed on ``branch`` are skipped. Once every well of the
    plate is in, the plate metadata is committed.
    """
    zarr.config.set({"async.concurrency": READ_CONCURRENCY})
    index = load_index(source_url, index_cache)
    source, src = read_source(source_url, index)
    pixels = MemberStore(source_url, index)
    chosen = tuple(wells) if wells else source.wells
    if unknown := set(chosen) - set(source.wells):
        raise ValueError(f"not wells of the plate: {sorted(unknown)}")
    done = done_wells(repo, branch, source.wells)
    todo = [w for w in chosen if w not in done]
    written = []
    started = time.monotonic()
    print(f"converting {len(todo)} wells ({len(set(chosen) & done)} already committed)", flush=True)
    for n, well in enumerate(todo, 1):
        fields = read_fields(source, src, well)
        session = repo.writable_session(branch)
        dest = zarr.open_group(session.store, mode="a", zarr_format=3)
        labels = define_well(dest, source, src, well, fields, shard_depth)
        with Progress(f"well {well} ({n} of {len(todo)}): {len(fields)} fields x {len(source.rounds)} rounds",
                      len(fields) * len(source.rounds), unit="images") as note:
            for field in fields:
                for rnd in source.rounds:
                    copy_image(pixels, session.store, well, rnd, image_path(source, field, rnd), field,
                               labels[(field.position, rnd)])
                    note.advance()
        dest[well].attrs.update(well_document(source, src, well, fields))
        commit(session, f"Add well {well}: {len(fields)} fields x {len(source.rounds)} rounds")
        written.append(well)
    complete = done | set(written)
    if complete >= set(chosen):
        plate_wells = [w for w in source.wells if w in complete]
        session = repo.writable_session(branch)
        root = zarr.open_group(session.store, mode="a", zarr_format=3)
        counts = [len(read_fields(source, src, w)) for w in plate_wells]
        channels = {r: [c["label"] for c in src[f"{source.wells[0]}/{r}"].attrs["omero"]["channels"]]
                    for r in source.rounds}
        document = plate_document(source, plate_wells, counts, channels)
        if dict(root.attrs) != document:
            root.attrs.update(document)
            commit(session, f"Plate metadata for {len(plate_wells)} wells")
    print(f"wrote {len(written)} wells in {time.monotonic() - started:.0f} s", flush=True)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", default=SOURCE, help="URL of the zip (default: EBI)")
    parser.add_argument("--repo", required=True, help="local directory or arraylake://org/repo")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--wells", help="comma-separated wells, such as C/02,E/05; default all")
    parser.add_argument("--shard-depth", type=int, default=SHARD_DEPTH)
    parser.add_argument("--index-cache", type=Path,
                        default=Path(cache_dir()) / "zebrafish-3d-4i" / "zip-index.npz")
    args = parser.parse_args()
    convert(args.source, open_repository(args.repo), branch=args.branch,
            wells=args.wells.split(",") if args.wells else None, shard_depth=args.shard_depth,
            index_cache=args.index_cache)


if __name__ == "__main__":
    main()
