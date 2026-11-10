"""Ingest JUMP Cell Painting plates (``cpg0016-jump``) into virtual OME-Zarr HCS plates.

A plate of the Cell Painting Gallery is one TIFF per (well, site, channel),
listed row by row in the plate's ``load_data.csv``. The store turns that into
the tree OME-Zarr defines for a well plate, under the gallery's own path:

- ``cpg0016-jump/<source>/<batch>/<plate>``: the plate, declaring every row
  and column of its physical format;
- ``.../<plate>/<row>/<column>``: a well, with what JUMP says was done to it;
- ``.../<plate>/<row>/<column>/<site>``: a field, a (c, y, x) image whose level
  0 references the TIFFs' strips and whose level 1 is a written 16x overview;
- ``cpg0016-jump/<source>/<batch>/illum/<plate>``: the plate's illumination
  functions, a (c, y, x) image referencing CellProfiler's ``.npy`` files.

Every TIFF is read once. ``TiffParser`` indexes it, its strips are fetched in
one request, each LZW strip's reference is checked against the Zarr codec and
shortened where the codec would read past the image, and the decoded pixels
are reduced to the overview. PLAN.md gives the choices behind the tree and
LAYOUTS.md what the bucket holds.

A batch is written in three steps, so that its plates can be written at once
by separate workers and land in one commit (docs/ingest.md, "Commit often,
and merge parallel writers once"):

1. The batch step writes the groups above the plates (the root, the dataset,
   the source, the batch and the batch's ``illum/``) and commits them.
2. One session per plate is forked from the commit, and each plate is
   written into its own fork. A plate writes only below its own paths.
3. The forks that succeeded are merged into one session and committed once.
   A plate that failed is reported and left out; the batch can be run again
   for it alone.

Icechunk refuses to merge two forks that created the same group, or updated
it to different attributes, which is why step 1 exists and why nothing above
a plate says how many plates sit below it: the plates are the batch group's
children, and a reader lists them.

    MINUS80_S3_ANONYMOUS=1 uv run --package cellpainting-gallery-ingest python \\
        ingests/cellpainting-gallery/ingest.py --source source_4 --batch 2021_04_26_Batch1 \\
        --plate BR00117035,BR00117036 --repo <icechunk dir> [--wells A01,A02 --sites 1,2]

One plate is a batch of one, and goes through the same fork and merge. A
plate already in the repo is deleted and written again in its fork, so the
replacement lands in the batch's commit (PLAN decision 1).
"""

from __future__ import annotations

import argparse
import math
import multiprocessing
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

import numpy as np
import zarr
from minus80.core import registry_for
from minus80.core.fetch import download_then_index, list_objects
from minus80.core.icechunk_io import commit, create_repo
from minus80.core.progress import Progress
from minus80.ome import image_annotation, plate_attrs, plate_grid, well_attrs
from virtualizarr.manifests import ManifestGroup
from zarr.codecs import ZstdCodec

import jump
import plate as cpg

BUCKET = "s3://cellpainting-gallery/"
DATASET = "cpg0016-jump"
PREFIX = f"{BUCKET}{DATASET}/"

GALLERY = {
    "title": "Cell Painting Gallery",
    "description": (
        "Broad Institute's public collection of Cell Painting datasets, as virtual OME-Zarr "
        "referencing the gallery's own files. Each dataset keeps its path in the bucket "
        "(cpg0016-jump/<source>/<batch>/<plate>); each plate is an OME-Zarr HCS plate."),
    "citation": "Weisbart et al. 2024, Cell Painting Gallery: an open resource for image-based "
                "profiling. Nature Methods 21, 1775-1777",
    "doi": "10.1038/s41592-024-02399-z",
    "license": "CC0-1.0",
    "source": BUCKET,
    "source_access": "anonymous; us-east-1",
    "codecs": {
        "imagecodecs_lzw": "level 0 of fields from Opera Phenix and Operetta sources (LZW TIFF "
                           "strips); registered by the imagecodecs-zarr package",
        "zstd": "level 1 of every field; in the Zarr core",
        "bytes": "level 0 of uncompressed sources and the illumination functions",
    },
}

JUMP_DATASET = {
    "title": "JUMP Cell Painting (cpg0016-jump)",
    "description": (
        "U2OS cells treated with more than 116,000 compounds and with 15,000 genes by CRISPR "
        "knockout and ORF overexpression, stained by the Cell Painting protocol (DNA, RNA, ER, "
        "AGP, Mito) and imaged by thirteen sources. Below this group: <source>/<batch>/<plate> "
        "is an OME-Zarr HCS plate, one field image per (well, site) with channels in the order "
        "DNA, RNA, ER, AGP, Mito, Brightfield_L, Brightfield, Brightfield_H as present; "
        "<source>/<batch>/illum/<plate> holds the plate's illumination functions."),
    "citation": "Chandrasekaran et al. 2023, JUMP Cell Painting dataset: morphological impact "
                "of 136,000 chemical and genetic perturbations. bioRxiv",
    "doi": "10.1101/2023.03.23.534023",
    "cell_line": "U2OS",
    "metadata": {"repository": jump.REPOSITORY, "commit": jump.COMMIT,
                 "tables": list(jump.TABLES)},
}


# Opening the repo
#
# A directory, an s3:// prefix or arraylake://<org>/<repo>. create_repo
# creates a local or S3 repo the first time and reopens it after; an Arraylake
# repo must already exist. The one container is the whole bucket, so every
# source's references resolve through it.

def open_for_writing(location: str, branch: str):
    """The repo at ``location``, with ``branch`` created from ``main``'s tip if absent."""
    repo = create_repo(location, [], prefixes=[BUCKET], open_existing=True)
    if branch not in repo.list_branches():
        repo.create_branch(branch, repo.lookup_branch("main"))
    return repo


# The groups above the plates
#
# Written and committed before any plate is forked. Each group's attributes
# are written only where they differ from what the repo holds, so running a
# batch again, or a second batch of the same source, changes nothing above
# its own batch group.

def parent_attributes(root, source: str, batch: str, scope: dict) -> dict[str, dict]:
    """The attributes of every group above a batch's plates, by path."""
    batch_path = f"{DATASET}/{source}/{batch}"
    return {
        "": {"annotations": dict(root.attrs).get("annotations", {}) | GALLERY},
        DATASET: {"annotations": JUMP_DATASET},
        f"{DATASET}/{source}": image_annotation({
            "description": f"Every plate {source} imaged for JUMP, by batch.",
            "source": source,
            "microscope": scope,
            "microscope_source": f"jump-cellpainting/datasets microscope_config.csv at "
                                 f"{jump.COMMIT}",
            "pixel_size_um": float(scope["Metadata_Pixel_Size_Microns"]),
            "pixel_size_rule": ("a plate whose load_data.csv states Metadata_ImageResolutionX/Y "
                                "takes that value; any other takes microscope_config.csv's. "
                                "Each plate records which (PLAN decision 5)."),
        }),
        batch_path: image_annotation({
            "description": f"Batch {batch} of {source}: one OME-Zarr HCS plate per plate, and "
                           f"illum/ holding each plate's illumination functions.",
            "source": source, "batch": batch}),
        f"{batch_path}/illum": image_annotation({
            "description": f"One image per plate of batch {batch}: CellProfiler's illumination "
                           f"functions for that plate, (c, y, x) in the plate's channel order."}),
    }


def write_parents(session, source: str, batch: str, scope: dict) -> list[str]:
    """Create or update the groups above the batch's plates; the paths written."""
    root = zarr.open_group(session.store, mode="a")
    written = []
    for path, attrs in parent_attributes(root, source, batch, scope).items():
        group = root.require_group(path) if path else root
        if any(dict(group.attrs).get(k) != v for k, v in attrs.items()):
            group.attrs.update(attrs)
            written.append(path or "/")
    return written


# One plate
#
# Everything below <batch>/<plate> and the plate's <batch>/illum/<plate>,
# nothing above. The session may be a fork on another machine, so the plate
# reads what it needs (load_data.csv, JUMP's tables, the TIFFs) itself.

def chosen(fields: list[cpg.Field], wells: str | None, sites: str | None) -> list[cpg.Field]:
    """The fields of ``wells`` and ``sites`` (comma-separated, as load_data.csv writes them)."""
    for name, wanted, have in (("wells", wells, {f.well for f in fields}),
                               ("sites", sites, {f.site for f in fields})):
        if wanted and (missing := set(wanted.split(",")) - have):
            raise ValueError(f"{name} not in load_data.csv: {sorted(missing)}")
    return [f for f in fields
            if (not wells or f.well in wells.split(",")) and (not sites or f.site in sites.split(","))]


def write_plate(session, source: str, batch: str, name: str, *, tables, registry,
                wells: str | None = None, sites: str | None = None, workers: int = 32) -> dict:
    """Write one plate and its illumination image into ``session``; what was written.

    A plate or illumination image already at the plate's paths is deleted
    first, in the same session.
    """
    started = time.monotonic()
    label = f"{name}: "

    # The plate as load_data.csv defines it. Its source, batch and plate
    # columns must name the plate asked for, since the path alone could be a
    # copy filed under the wrong batch.
    load_data_url = f"{PREFIX}{source}/workspace/load_data_csv/{batch}/{name}/load_data.csv"
    frame = cpg.read_load_data(download_then_index(load_data_url, registry=registry))
    for column, expected in (("Metadata_Source", source), ("Metadata_Batch", batch),
                             ("Metadata_Plate", name)):
        if set(frame[column]) != {expected}:
            raise ValueError(f"{load_data_url}: {column} is {sorted(set(frame[column]))}, "
                             f"expected {expected}")
    channels = cpg.plate_channels(frame)
    every_field = cpg.read_fields(frame, channels)
    fields = sorted(chosen(every_field, wells, sites),
                    key=lambda f: (cpg.row_index(f.row), int(f.column), cpg.site_key(f.site)))
    subset = bool(wells or sites)
    wells_format = cpg.plate_format(every_field)
    print(f"{source}/{batch}/{name}: {len(every_field)} fields in load_data.csv, "
          f"{len(fields)} chosen; channels {', '.join(channels)}; {wells_format}-well plate",
          flush=True)

    # JUMP's tables: the plate's type, the source's pixel size and each
    # well's perturbation.
    plate_row = jump.plate_record(tables, source, batch, name)
    scope = jump.microscope(tables, source)
    pixel_um, pixel_source = cpg.pixel_size(frame, float(scope["Metadata_Pixel_Size_Microns"]))
    perturbations, coverage = jump.well_annotations(tables, source, name)
    times = cpg.acquisition_times(frame)
    positions = cpg.has_positions(frame)
    constant, varying = cpg.split_columns(frame)

    # One pass over every TIFF: index, check LZW lengths, reduce to the overview.
    urls = [u for f in fields for u in f.urls]
    indexed = cpg.index_all(urls, registry, pixel_um, workers, label=label)
    print(f"{label}indexed {len(urls)} TIFFs in {time.monotonic() - started:.0f} s", flush=True)
    tiff_common, tiff_each = cpg.split_tiff([indexed[u].tiff for u in urls])
    tiff_varying = dict(zip(urls, tiff_each))

    # The illumination functions: one .npy per channel, found by name in the
    # plate's illumination folder, each header read for dtype, shape and offset.
    batch_path = f"{DATASET}/{source}/{batch}"
    plate_path = f"{batch_path}/{name}"
    illum_path = f"{batch_path}/illum/{name}"
    illum_folder = f"{PREFIX}{source}/images/{batch}/illum/{name}/"
    illum_listing = list_objects(illum_folder, registry=registry)
    illum_urls = {c: f"{illum_folder}{name}_Illum{c}.npy" for c in channels}
    illum_missing = [c for c, u in illum_urls.items() if u not in illum_listing]
    illum_headers = [] if illum_missing else [
        cpg.read_npy_header(u, illum_listing[u], registry) for u in illum_urls.values()]
    illum_unreferenced = sorted(u.rsplit("/", 1)[-1] for u in illum_listing
                                if u not in illum_urls.values())

    # What the image folders hold beyond load_data.csv (PLAN decision 9).
    folders = sorted({u.rsplit("/", 1)[0] + "/" for f in every_field for u in f.urls})
    listed = [u for folder in folders for u in list_objects(folder, registry=registry)]
    extras = cpg.folder_extras(listed, {u for f in every_field for u in f.urls},
                               {f.well for f in every_field})

    store = session.store
    root = zarr.open_group(store, mode="a")
    for path in (plate_path, illum_path):
        if path in root:
            del root[path]

    # The plate, its wells and its fields. A field states what varies from
    # field to field; what holds for the whole plate is on the plate.
    by_well: dict[str, list[cpg.Field]] = {}
    for f in fields:
        by_well.setdefault(f.well, []).append(f)
    windows = cpg.display_windows(
        [np.stack([indexed[u].overview for u in f.urls]) for f in fields], channels)
    shortened, unfixable = [], []
    rows, columns = plate_grid(wells_format)
    acquisitions = None
    if times:
        acquisitions = [{"id": 0, "maximumfieldcount": max(len(m) for m in by_well.values()),
                         "starttime": math.floor(min(times).timestamp()),
                         "endtime": math.ceil(max(times).timestamp())}]
    plate_group = root.create_group(plate_path)

    with Progress(f"{label}writing fields", len(fields), unit="fields") as progress:
        for well, members in by_well.items():
            row, column = members[0].row, members[0].column
            well_group = plate_group.create_group(f"{row}/{column}")
            well_group.attrs.update(
                well_attrs([f.site for f in members])
                | image_annotation(cpg.well_fields(well, perturbations, plate_row is not None)))
            for f in members:
                parts = [indexed[u] for u in f.urls]
                level0_shape = parts[0].array.shape
                offset = (cpg.translation(f.record, level0_shape, pixel_um) if positions else None)
                level0 = cpg.field_level0(parts, channels, offset)
                overview = np.stack([p.overview for p in parts])
                ome = cpg.field_ome(name=f"{name} {well} site {f.site}", level0=level0,
                                    overview_shape=overview.shape, pixel_um=pixel_um,
                                    windows=windows, translation_um=offset)
                attributes = {"ome": ome, **image_annotation(
                    {"source": source, "batch": batch, "plate": name, "well": well,
                     "site": f.site, "load_data": {c: f.record[c] for c in varying}})}
                if any(tiff_varying[u] for u in f.urls):
                    attributes["tiff"] = {c: tiff_varying[u] for c, u in zip(channels, f.urls)}
                path = f"{plate_path}/{f.path}"
                ManifestGroup(arrays={"0": level0}, attributes=attributes).to_icechunk(
                    store, group=path)
                zarr.open_group(store, path=path, mode="a").create_array(
                    "1", data=overview, chunks=overview.shape, compressors=[ZstdCodec()],
                    dimension_names=("c", "y", "x"))
                short, broken = cpg.lzw_records(f, parts, channels)
                shortened += short
                unfixable += broken
                progress.advance()

    compression = sorted({indexed[u].compression for u in urls})
    plate_group.attrs.update(
        plate_attrs(name=name, rows=rows, columns=columns,
                    wells=[(m[0].row, m[0].column) for m in by_well.values()],
                    field_count=max(len(m) for m in by_well.values()), acquisitions=acquisitions)
        | image_annotation(cpg.plate_fields(
            source=source, batch=batch, name=name, load_data_url=load_data_url,
            every_field=every_field, fields=fields, subset=subset,
            wells_format=wells_format, channels=channels, plate_row=plate_row,
            coverage=coverage, pixel_um=pixel_um, pixel_source=pixel_source,
            positions=positions, times=times is not None, constant=constant, varying=varying,
            compression=compression, shortened=shortened, unfixable=unfixable, extras=extras,
            tiff_common=tiff_common, illum_path=None if illum_missing else illum_path,
            illum_missing=illum_missing)))

    # The illumination image, in the plate's channel order.
    if illum_headers:
        illum = cpg.illum_array(illum_headers, channels, pixel_um)
        if illum.shape[1:] != indexed[urls[0]].array.shape:
            raise ValueError(f"illumination functions are {illum.shape[1:]}, fields are "
                             f"{indexed[urls[0]].array.shape}")
        ManifestGroup(arrays={"0": illum}, attributes={
            "ome": cpg.illum_ome(name=f"{name} illumination functions", array=illum,
                                 pixel_um=pixel_um),
            **image_annotation({
                "description": (f"The illumination-correction functions JUMP's CellProfiler "
                                f"pipeline computed for plate {name}, one per channel, on the "
                                f"same pixel grid as the plate's fields."),
                "source": source, "batch": batch, "plate": name, "channels": channels,
                "plate_path": plate_path,
                "dtype": str(illum_headers[0].dtype), "shape": list(illum_headers[0].shape),
                "files_not_referenced": illum_unreferenced,
            }, source_url=[h.url for h in illum_headers])}).to_icechunk(store, group=illum_path)

    summary = {"plate": name, "fields": len(fields), "wells": len(by_well), "subset": subset,
               "shortened": len(shortened), "unfixable": len(unfixable),
               "illumination": "missing " + ",".join(illum_missing) if illum_missing
               else "written", "seconds": round(time.monotonic() - started)}
    print(f"{label}{summary['fields']} fields, {summary['shortened']} shortened LZW chunks, "
          f"{summary['unfixable']} unfixable; illumination {summary['illumination']}; "
          f"written in {summary['seconds']} s", flush=True)
    return summary


# One plate on a worker
#
# The unit a launcher hands out: a fork goes in, the same fork comes back
# with the plate written into it. Both cross a process boundary by pickling,
# so a Coiled launcher can run this on one VM per plate and return the fork
# exactly as the local process pool below does.

def plate_job(fork, source: str, batch: str, name: str, options: dict):
    """Write plate ``name`` into ``fork``; the fork and the plate's summary."""
    registry = registry_for(PREFIX + "x", patient=True)
    tables = jump.read_tables(jump.fetch_tables())
    summary = write_plate(fork, source, batch, name, tables=tables, registry=registry, **options)
    return fork, summary


# A batch

#: Seconds between the driver's progress lines while it waits on plates.
PROGRESS_EVERY = 30.0


def ingest_batch(location: str, branch: str, source: str, batch: str, names: list[str], *,
                 options: dict, parallel: int) -> tuple[list[dict], dict[str, str]]:
    """Write ``names`` of one batch as one fork each, merged into one commit.

    Returns the summaries of the plates committed and the error of each plate
    that failed. Plates run in ``parallel`` worker processes.
    """
    started = time.monotonic()
    repo = open_for_writing(location, branch)
    tables = jump.read_tables(jump.fetch_tables())

    session = repo.writable_session(branch)
    if written := write_parents(session, source, batch, jump.microscope(tables, source)):
        snapshot = commit(session, f"{DATASET} {source} {batch}: the groups above its plates")
        print(f"batch step: wrote {', '.join(written)}; committed {snapshot}", flush=True)
    else:
        print("batch step: the groups above the plates are already as written", flush=True)

    session = repo.writable_session(branch)
    forks = {name: session.fork() for name in names}
    done, failed = {}, {}
    # spawn, not fork: a forked child would inherit the parent's Icechunk and
    # zarr runtime threads mid-state.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(parallel, mp_context=context) as pool, \
            Progress(f"writing {len(names)} plates of {source}/{batch}, {parallel} at a time",
                     len(names), unit="plates", every=PROGRESS_EVERY) as progress:
        futures = {pool.submit(plate_job, forks[n], source, batch, n, options): n for n in names}
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, timeout=PROGRESS_EVERY, return_when=FIRST_COMPLETED)
            for future in finished:
                name = futures[future]
                try:
                    done[name] = future.result()
                except Exception as exc:  # noqa: BLE001 -- reported below, the batch goes on
                    print(f"{name}: FAILED, {type(exc).__name__}: {exc}", file=sys.stderr,
                          flush=True)
                    failed[name] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            progress.advance(len(finished))

    if done:
        merging = time.monotonic()
        session.merge(*(fork for fork, _ in done.values()))
        summaries = [summary for _, summary in done.values()]
        fields = sum(s["fields"] for s in summaries)
        subset = any(s["subset"] for s in summaries)
        message = (f"{DATASET} {source} {batch}: {len(summaries)} plates "
                   f"({', '.join(sorted(done))}), {fields} fields"
                   + (" (subsets of load_data.csv)" if subset else ""))
        snapshot = commit(session, message)
        print(f"merged {len(done)} forks and committed {snapshot} to {branch} in "
              f"{time.monotonic() - merging:.1f} s; batch took {time.monotonic() - started:.0f} s",
              flush=True)
    else:
        summaries = []
        print("no plate succeeded; nothing committed", flush=True)
    for name, why in sorted(failed.items()):
        print(f"not committed: {name}: {why}", flush=True)
    return summaries, failed


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", required=True, help="e.g. source_4")
    ap.add_argument("--batch", required=True)
    ap.add_argument("--plate", required=True, help="one plate, or several comma-separated")
    ap.add_argument("--repo", required=True,
                    help="Icechunk repo: a directory, an s3:// URL or arraylake://<org>/<repo>")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--wells", help="comma-separated Metadata_Well labels; default every well")
    ap.add_argument("--sites", help="comma-separated Metadata_Site values; default every site")
    ap.add_argument("--workers", type=int, default=32, help="TIFFs indexed at once per plate")
    ap.add_argument("--parallel", type=int, default=4, help="plates written at once")
    args = ap.parse_args(argv)
    names = args.plate.split(",")
    if len(set(names)) != len(names):
        raise SystemExit(f"a plate is named twice: {args.plate}")
    _, failed = ingest_batch(
        args.repo, args.branch, args.source, args.batch, names,
        options={"wells": args.wells, "sites": args.sites, "workers": args.workers},
        parallel=min(args.parallel, len(names)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
