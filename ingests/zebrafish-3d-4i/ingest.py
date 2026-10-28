"""Convert the 3D-4i zebrafish plate (BioImage Archive S-BIAD3228) to OME-Zarr 0.6.

PLAN.md explains the decisions behind this layout. LAYOUTS.md records the
quirks of the source. This docstring walks through what the code does and why.
The general parts of the work live in the minus80 package. This file holds
only what is specific to this dataset.

What the source is
------------------
The source is one zip file of 591 GB. It holds an OME-Zarr 0.4 plate written
by Fractal, the software the authors used. Inside the zip:

- There are 30 wells. Each well was imaged in 4 staining rounds, numbered 0 to 3.
- For each well and round there is one large image the size of the well. We
  call it the canvas. It has axes (c, z, y, x) and holds uint16 pixels.
- The microscope took 5 to 9 fields of view in each well, one per embryo.
  Each field is 1000 x 1000 pixels and 271 planes deep. Fractal pasted each
  field onto the canvas at the position where it was taken.
- Each canvas has 4 pyramid levels. Each level halves the size in y and x,
  rounding down.
- Round 1 also has six segmentations, called labels, on the same pixel grid.
- Each canvas has tables in AnnData format. The table ``FOV_ROI_table`` says
  where each field sits on the canvas.

Most of each canvas is empty. The microscope never imaged the space between
the fields, so those pixels are exactly 0. At level 0 the source does not
store chunks that miss every field. At levels 1 to 3 it does store them,
full of zeros. Inside a field, even the background outside the embryo has
camera noise, with values from 0 to 8.

The zip compressed every file inside it with Deflate. Because of that, we
cannot point at the source chunks in place. This ingest reads the pixels and
writes them again.

What the store is
-----------------
The store is an OME-Zarr 0.6 plate. Each field in each round becomes an image
of its own. This is the layout that the OME-Zarr plate specification defines
for fields of view.

- ``<row>/<column>`` is a well. Its ``well`` metadata lists the well's images
  and the round of each one. The same group also has a ``scene``. The scene
  places every image of the well in one shared coordinate system, called
  ``well``, so a viewer can still show the whole well as one picture.
- ``<row>/<column>/<image>`` is one field in one round. Field ``FOV_n`` in
  round ``r`` is image number ``rounds * (n - 1) + r``. Its axes are
  (c, z, y, x) and its pixels are uint16.
- ``<row>/<column>/<image>/labels/<name>`` holds that round's labels, cut to
  the same field.

Cutting out the fields leaves the empty space of the canvas behind. A few
chunks are still all zero, mostly label planes above and below each embryo.
zarr does not write those chunks. When read, they come back as the fill
value, 0.

How the copy works
------------------
- **Reading the zip.** The module ``minus80.parsers.zip`` reads the zip's
  table of contents once and keeps it in a file. Its ``MemberStore`` lets
  zarr open the zip where it is. zarr can then fetch many chunks at once.
- **Finding the fields.** The module ``minus80.parsers.fractal`` reads each
  field's position from ``FOV_ROI_table``. Every round has its own copy of
  that table, and the module checks that the copies agree.
- **Cutting each level.** The module ``minus80.ome.crop`` works out which part
  of each source level belongs to a field. At level 0 this is exactly the
  field's 1000 x 1000 pixels. Levels 1 to 3 copy the source's own coarse
  pixels, so that every value in the store is the authors'. Most fields do
  not start on a coarse pixel boundary. The cut therefore covers every coarse
  pixel the field touches, and it can be one pixel wider than 1000 / 2^k.
  Each level's translation records exactly where the cut sits. A later
  commit can replace these levels with ones rebuilt from level 0.
- **Chunks and shards.** A chunk is one plane of one channel. A shard holds
  32 planes. For labels, a shard holds all 271 planes, because a label plane
  compresses to only a few KB. Each call to zarr writes a whole channel of
  one level. That way every shard is written once and never read back.
- **Commits.** Each well is one commit. The well's own metadata is written in
  that same commit. So a well is either fully in the store or not there at
  all. If a run stops, the next run starts again at the first well that is
  not yet committed. At the end, every run writes the plate's metadata. That
  metadata lists exactly the wells that are on the branch at that moment.
- **Speed.** One process copies one image at a time. Within each zarr call,
  zarr keeps up to ``READ_CONCURRENCY`` reads from the source in flight. To
  go faster, start more processes and give each one different wells with
  ``--wells``. Each process commits its own wells. Icechunk rebases each
  commit onto the commits the other processes made first.

Running it
----------
::

    uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/ingest.py \\
        --source <zip URL> --repo <dir | arraylake://org/repo> [--wells C/02,E/05]
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from pathlib import Path

import zarr
from zarr.codecs import BloscCodec

from minus80.core.fetch import cache_dir
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, commit, storage_for
from minus80.core.progress import Progress, peak_memory_gb
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
    IMAGE_DIMS,
    LABEL_DIMS,
    TABLES,
    Field,
    Plate,
    level_scales,
    read_fields,
    read_plate,
    read_table,
)
from minus80.parsers.zip.members import cached_zip_index
from minus80.parsers.zip.store import MemberStore, metadata_members

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
#: read. With 32 planes per shard, a whole embryo volume takes 9 requests of
#: about 15 MB each.
SHARD_DEPTH = 32

#: See decision 6 in PLAN.md. Blosc with zstd decodes fast, and the Zarr v3
#: specification defines it, so every Zarr reader can open the store.
CODEC = BloscCodec(cname="zstd", clevel=5, shuffle="bitshuffle")

#: How many reads from the source zarr keeps in flight at once. Each read is a
#: network round trip of 100 to 400 ms. zarr's default of 10 would leave the
#: network idle most of the time.
READ_CONCURRENCY = 64

#: Tables that describe the whole well rather than one field. Their rows are
#: kept in the well's attributes, so nothing the source recorded is lost.
WELL_TABLES = ("well_ROI_table", "embryoRaw_ROI_table")


def image_path(plate: Plate, field: Field, rnd: int) -> str:
    """The path of field ``FOV_n`` in round ``r``: image number ``rounds * (n - 1) + r``."""
    return str(len(plate.acquisitions) * (field.number - 1) + plate.acquisitions.index(rnd))


def define_image(
    dest: zarr.Group,
    plate: Plate,
    src: zarr.Group,
    url: str,
    well: str,
    field: Field,
    rnd: int,
    shard_depth: int,
) -> tuple[str, ...]:
    """Create the image group for one field in one round, with its labels.

    This writes the metadata and creates empty arrays. The pixels are copied
    later by :func:`copy_image`. Returns the names of the round's labels,
    because the copy needs them.
    """
    canvas = src[f"{well}/{rnd}"]
    image = dest.require_group(f"{well}/{image_path(plate, field, rnd)}")
    image.attrs.update(
        {
            "ome": image_attrs(
                name=f"{well} {field.name} round {rnd}",
                dims=IMAGE_DIMS,
                level_shapes=[level.shape for level in field.levels],
                scale=field.levels[0].scale,
                units=plate.units,
                version=VERSION,
                omero=dict(canvas.attrs["omero"]),
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
                    "round": rnd,
                    "source_image": f"{well}/{rnd}",
                    "FOV_ROI_table": field.roi,
                },
                source_url=url,
            ),
        }
    )
    for k, level in enumerate(field.levels):
        _, _, height, width = level.shape
        image.create_array(
            str(k),
            shape=level.shape,
            chunks=(1, 1, height, width),
            shards=(1, shard_depth, height, width),
            dtype=canvas[str(k)].dtype,
            compressors=CODEC,
            fill_value=0,
            dimension_names=IMAGE_DIMS,
        )

    if "labels" not in canvas:
        return ()
    names = tuple(canvas["labels"].attrs["labels"])
    group = image.require_group("labels")
    group.attrs["ome"] = labels_attrs(names, version=VERSION)
    image_scales = [{d: s[d] for d in LABEL_DIMS} for s in level_scales(canvas, IMAGE_DIMS)]
    for name in names:
        source = canvas[f"labels/{name}"]
        # The labels are cut with the image's windows and placed with the
        # image's translations. That is only correct if each label level has
        # the same shape and the same pixel size as the image level.
        if level_scales(source, LABEL_DIMS) != image_scales:
            raise ValueError(f"{well}/{rnd}/labels/{name}: pixel sizes differ from the image's")
        for k in range(plate.levels):
            if source[str(k)].shape != canvas[str(k)].shape[1:]:
                raise ValueError(
                    f"{well}/{rnd}/labels/{name}: level {k} is not on the image's grid"
                )
        label = group.require_group(name)
        label.attrs.update(
            {
                "ome": label_attrs(
                    name=name,
                    dims=LABEL_DIMS,
                    level_shapes=[level.shape[1:] for level in field.levels],
                    scale={d: field.levels[0].scale[d] for d in LABEL_DIMS},
                    units=plate.units,
                    version=VERSION,
                    level_scales=[{d: lvl.scale[d] for d in LABEL_DIMS} for lvl in field.levels],
                    level_translations=[
                        {d: lvl.translation[d] for d in LABEL_DIMS} for lvl in field.levels
                    ],
                ),
                **image_annotation({"source_label": f"{well}/{rnd}/labels/{name}"}, source_url=url),
            }
        )
        for k, level in enumerate(field.levels):
            depth, height, width = level.shape[1:]
            label.create_array(
                str(k),
                shape=(depth, height, width),
                chunks=(1, height, width),
                shards=(depth, height, width),
                dtype=source[str(k)].dtype,
                compressors=CODEC,
                fill_value=0,
                dimension_names=LABEL_DIMS,
            )
    return names


def copy_image(
    pixels: MemberStore, dst, well: str, rnd: int, image: str, field: Field, labels: Sequence[str]
) -> None:
    """Copy the pixels of one field in one round, at every level, with its labels.

    Each assignment below copies a whole channel of one level, or a whole
    level of one label. zarr then fetches every source chunk that the
    assignment needs at the same time. It also writes each shard exactly once.
    The largest single assignment is one label at level 0: 271 x 1000 x 1000
    uint32 values, about 1.1 GB.
    """
    for k, level in enumerate(field.levels):
        source = zarr.open_array(pixels, path=f"{well}/{rnd}/{k}", mode="r")
        target = zarr.open_array(dst, path=f"{well}/{image}/{k}", mode="r+")
        _, z, y, x = level.slices(IMAGE_DIMS)
        for c in range(target.shape[0]):
            target[c] = source[c, z, y, x]
        for name in labels:
            source = zarr.open_array(pixels, path=f"{well}/{rnd}/labels/{name}/{k}", mode="r")
            target = zarr.open_array(dst, path=f"{well}/{image}/labels/{name}/{k}", mode="r+")
            target[:] = source[level.slices(LABEL_DIMS)]


def well_document(
    plate: Plate, src: zarr.Group, url: str, well: str, fields: Sequence[Field]
) -> dict:
    """The attributes of one well group.

    They hold three things:

    - the ``well`` metadata, which lists the well's images and the round of each;
    - the ``scene``, which places each image in the well's coordinate system;
    - the rows of the tables that describe the whole well.

    An image's place in the scene is its field's position on the source's
    canvas. Every round of a field gets the same position, because the
    published rounds are already aligned to each other.
    """
    paths, rounds, translations = [], [], {}
    for field in fields:
        for rnd in plate.acquisitions:
            path = image_path(plate, field, rnd)
            paths.append(path)
            rounds.append(rnd)
            translations[path] = {
                "c": 0.0,
                "z": field.roi["z_micrometer"],
                "y": field.roi["y_micrometer"],
                "x": field.roi["x_micrometer"],
            }

    # The OME-Zarr 0.6 schemas allow one group to be both a well and a scene.
    # The well schema does not forbid a "scene" key, and the scene schema does
    # not forbid a "well" key. So the well group carries both, and the
    # document is checked against both schemas.
    well_metadata = well_attrs(paths, acquisitions=rounds, version=VERSION)["ome"]
    scene = scene_attrs(translations, name=SCENE, dims=IMAGE_DIMS, units=plate.units)
    ome = well_metadata | {"scene": scene["ome"]["scene"]}
    validate(ome, VERSION, model="well")
    validate(ome, VERSION, model="scene")

    tables = {}
    for name in WELL_TABLES:
        copies = {
            rnd: read_table(src[f"{well}/{rnd}/tables/{name}"])
            for rnd in plate.acquisitions
            if f"{well}/{rnd}/tables/{name}" in src
        }
        if not copies:
            continue
        first, table = next(iter(copies.items()))
        for rnd, other in copies.items():
            if not other.sort_index().equals(table.sort_index()):
                raise ValueError(f"{well}: round {rnd}'s {name} differs from round {first}'s")
        tables[name] = {
            "source": f"{well}/{first}/tables/{name}",
            "rows": {str(i): {k: float(v) for k, v in row.items()} for i, row in table.iterrows()},
        }
    return {"ome": ome, **image_annotation({"source_well": well, **tables}, source_url=url)}


def committed_wells(root: zarr.Group, wells: Sequence[str]) -> list[str]:
    """The wells already on the branch, in plate order.

    A well counts as committed when its group has its ``well`` metadata,
    because that metadata is written in the same commit as the well's pixels.
    """
    return [w for w in wells if w in root and "well" in dict(root[w].attrs).get("ome", {})]


def write_plate(repo, branch: str, plate: Plate, src: zarr.Group, url: str) -> None:
    """Commit plate metadata that lists exactly the wells on the branch now.

    The list of wells is read from the same session that commits the plate.
    So the list cannot be out of date when it is committed.

    Two runs might write the plate at the same moment. In that case the
    second commit conflicts with the first one on the plate's attributes, and
    Icechunk raises an error. Running either one again writes the plate.
    """
    session = repo.writable_session(branch)
    root = zarr.open_group(session.store, mode="a", zarr_format=3)
    wells = committed_wells(root, plate.wells)
    if not wells:
        return
    channels = {
        rnd: [c["label"] for c in src[f"{plate.wells[0]}/{rnd}"].attrs["omero"]["channels"]]
        for rnd in plate.acquisitions
    }
    rounds = len(plate.acquisitions)
    most_fields = max(len(dict(root[w].attrs)["ome"]["well"]["images"]) // rounds for w in wells)
    document = plate_attrs(
        name=plate.attrs["name"],
        rows=[row["name"] for row in plate.attrs["rows"]],
        columns=[column["name"] for column in plate.attrs["columns"]],
        wells=[tuple(w.split("/")) for w in wells],
        field_count=most_fields,
        acquisitions=[
            {
                "id": rnd,
                "name": f"acquisition_{rnd}",
                "description": f"staining round {rnd}: {', '.join(channels[rnd])}",
            }
            for rnd in plate.acquisitions
        ],
        version=VERSION,
    )
    document["source"] = {
        **STUDY,
        "url": url,
        "layout": (
            "One image per field of view and round, cut from the source's well-sized images "
            "and placed in each well by its scene."
        ),
        "source_acquisitions": [a["id"] for a in plate.attrs.get("acquisitions", [])],
    }
    if dict(root.attrs) != document:
        root.attrs.update(document)
        commit(session, f"Plate metadata for {len(wells)} wells")


def open_repository(location: str):
    """Open an Icechunk repo at a local path, creating it if needed, or open an Arraylake repo."""
    import icechunk

    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location)
    return icechunk.Repository.open_or_create(storage_for(location))


def convert(
    url: str,
    repo,
    index_cache: Path,
    *,
    branch: str = "main",
    wells: Sequence[str] | None = None,
    shard_depth: int = SHARD_DEPTH,
) -> list[str]:
    """Convert some or all wells of the zip at ``url`` into ``repo``.

    Each well becomes one commit on ``branch``. Wells already on the branch
    are skipped. With ``wells`` left out, every well of the plate is
    converted. The run ends by writing the plate's metadata, which lists
    every well on the branch. Returns the wells this run converted.

    ``index_cache`` is the file that keeps the zip's table of contents. Keep a
    copy of it: it records exactly what the zip held.
    """
    zarr.config.set({"async.concurrency": READ_CONCURRENCY})
    index = cached_zip_index(url, index_cache)
    src = zarr.open_group(MemberStore(url, metadata_members(index, also=[TABLES])), mode="r")
    pixels = MemberStore(url, index)
    plate = read_plate(src)

    chosen = tuple(wells) if wells else plate.wells
    if unknown := set(chosen) - set(plate.wells):
        raise ValueError(f"not wells of the plate: {sorted(unknown)}")
    branch_root = zarr.open_group(repo.writable_session(branch).store, mode="a", zarr_format=3)
    done = committed_wells(branch_root, chosen)
    todo = [w for w in chosen if w not in done]
    started = time.monotonic()
    print(f"converting {len(todo)} wells ({len(done)} already committed)", flush=True)

    for n, well in enumerate(todo, 1):
        fields = read_fields(plate, src, well)
        session = repo.writable_session(branch)
        dest = zarr.open_group(session.store, mode="a", zarr_format=3)
        images = [(field, rnd) for field in fields for rnd in plate.acquisitions]
        labels = {
            (field.number, rnd): define_image(dest, plate, src, url, well, field, rnd, shard_depth)
            for field, rnd in images
        }
        description = f"{len(fields)} fields x {len(plate.acquisitions)} rounds"
        with Progress(
            f"well {well} ({n} of {len(todo)}): {description}", len(images), unit="images"
        ) as note:
            for field, rnd in images:
                path = image_path(plate, field, rnd)
                copy_image(
                    pixels, session.store, well, rnd, path, field, labels[(field.number, rnd)]
                )
                note.advance()
        dest[well].attrs.update(well_document(plate, src, url, well, fields))
        commit(session, f"Add well {well}: {description}")
        print(f"  committed {well}; peak memory so far {peak_memory_gb():.1f} GB", flush=True)

    write_plate(repo, branch, plate, src, url)
    print(f"wrote {len(todo)} wells in {time.monotonic() - started:.0f} s", flush=True)
    return todo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", default=SOURCE, help="URL of the zip (default: EBI)")
    parser.add_argument("--repo", required=True, help="local directory or arraylake://org/repo")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--wells", help="comma-separated wells, such as C/02,E/05; default all")
    parser.add_argument("--shard-depth", type=int, default=SHARD_DEPTH)
    parser.add_argument(
        "--index-cache",
        type=Path,
        default=Path(cache_dir()) / "zebrafish-3d-4i" / "zip-index.npz",
        help="the file that keeps the zip's table of contents; keep a copy of it",
    )
    args = parser.parse_args()
    convert(
        args.source,
        open_repository(args.repo),
        args.index_cache,
        branch=args.branch,
        wells=args.wells.split(",") if args.wells else None,
        shard_depth=args.shard_depth,
    )


if __name__ == "__main__":
    main()
