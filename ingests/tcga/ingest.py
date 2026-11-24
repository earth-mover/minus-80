"""Ingest TCGA whole-slide images from NCI Imaging Data Commons into an Icechunk store.

The Cancer Genome Atlas (TCGA) scanned tens of thousands of H&E slides. NCI
Imaging Data Commons (IDC) republishes them as DICOM in the public bucket
``s3://idc-open-data``, where every file is named by a random UUID. What a file
is (whose slide, which level of its pyramid) is known only from IDC's index.

The store turns that into a hierarchy a reader can walk without the index::

    /<collection>/<patient>/<slide>/image/0..n

Each ``image`` is an OME-Zarr multiscale image whose chunks are the DICOM
file's own JPEG or JPEG 2000 tiles, referenced in place in IDC's bucket. The
collection, patient and slide groups carry what IDC's index says about them.

    uv run --package tcga-ingest python ingests/tcga/ingest.py \\
        --repo arraylake://earthmover-biology/tcga --collection tcga_kirc --limit 100
"""

from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import zarr
from virtualizarr.manifests import ManifestGroup

from minus80.core.fetch import download_then_index
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, commit
from minus80.ome.image import image_attrs, rgb_omero
from minus80.parsers.dicom import wsi_level_array

from slides import idc_tables, jsonable_row, say

BUCKET = "s3://idc-open-data/"
OME_VERSION = "0.6"


# Selecting slides
#
# IDC's index (the ``idc-index`` package) is a set of Parquet tables built from
# IDC's BigQuery release: one row per DICOM series, and for slide microscopy
# one row per instance as well. A TCGA slide is one series. Its pyramid levels
# are the instances whose ImageType says VOLUME; the THUMBNAIL, LABEL and
# OVERVIEW instances are pictures of the glass, not levels of the image.
#
# The diagnostic slides are the formalin-fixed, paraffin-embedded ones, named
# DX1, DX2, ... in the slide barcode; IDC's series descriptions read "FFPE HE
# TP DX1". The rest (TS, BS) are frozen sections. The first ingest takes the
# DX1 slide of the first patients in barcode order, so a rerun picks the same
# slides.

def select_slides(series, slides, collection: str, limit: int | None):
    """The DX1 slides of ``collection``, one per patient, in patient order."""
    rows = series[(series.collection_id == collection) & (series.Modality == "SM")]
    rows = rows.merge(slides, on="SeriesInstanceUID")
    rows = rows[rows.ContainerIdentifier.str.endswith("-DX1")]
    rows = rows.sort_values("PatientID").drop_duplicates("PatientID")
    return rows.head(limit) if limit else rows


def pyramid(instances, series_uid: str):
    """The slide's VOLUME instances, base level first."""
    rows = instances[instances.SeriesInstanceUID == series_uid]
    rows = rows[rows.ImageType.map(lambda t: len(t) > 2 and t[2] == "VOLUME")]
    return rows.sort_values("TotalPixelMatrixColumns", ascending=False)


# Locating the tiles
#
# Each pyramid level is one DICOM instance whose PixelData holds one encoded
# tile per frame. IDC's TCGA files leave the Basic Offset Table empty and carry
# no Extended Offset Table, so the only way to find each tile is to walk every
# fragment header from the start of PixelData to the end of the file. The level
# is downloaded once, beside the data in us-east-1, walked from local disk, and
# deleted; the manifest keeps the bucket URL.

def virtualize_slide(slide, levels) -> dict:
    """Parse every level of one slide into ``ManifestArray``s keyed "0", "1", ..."""
    arrays = {}
    for i, level in enumerate(levels.itertuples()):
        url = f"{BUCKET}{slide.crdc_series_uuid}/{level.crdc_instance_uuid}.dcm"
        local = download_then_index(url, concurrency=16)
        try:
            arrays[str(i)] = wsi_level_array(local, url_for=lambda _, url=url: url)
        finally:
            os.remove(local)
    return arrays


# The image
#
# Every level is a (c, y, x) uint8 array of RGB samples, one chunk per tile.
# IDC states each level's pixel spacing in millimeters. The levels were
# resampled by the scanner software, so a coarser level's spacing is close to
# but not exactly a power of two times the base level's; each level gets the
# spacing its own instance states.
#
# The DICOM headers of the JPEG 2000 slides say YBR_FULL_422, but their tiles
# decode as RGB (LAYOUTS.md). The store's codec decodes the codestream as it
# is, so the store shows the colors the scanner recorded.

def image_group(slide, levels, arrays: dict) -> ManifestGroup:
    spacings = [1000.0 * s for s in levels.PixelSpacing_0]
    level_scales = [{"c": 1.0, "y": s, "x": s} for s in spacings]
    ome = image_attrs(
        name=slide.ContainerIdentifier, dims=("c", "y", "x"),
        level_shapes=[a.shape for a in arrays.values()],
        scale=level_scales[0], level_scales=level_scales,
        units={"y": "micrometer", "x": "micrometer"}, omero=rgb_omero(),
        description="the scanner's own pyramid levels, as IDC publishes them",
        version=OME_VERSION)
    return ManifestGroup(arrays=arrays, attributes={"ome": ome})


# The hierarchy
#
# The collection group says where the data comes from and under what license.
# The patient group names the patient; IDC's slide series carry no age or sex
# for TCGA, and the clinical data waits for a later ingest. The slide group
# carries the slide's row of IDC's series and slide-microscopy tables, which
# includes the identifiers that trace it back to IDC.

SLIDE_FIELDS = [
    "ContainerIdentifier", "SeriesInstanceUID", "StudyInstanceUID", "crdc_series_uuid",
    "SeriesDescription", "Manufacturer", "ManufacturerModelName", "transfer_syntax_name",
    "ObjectiveLensPower", "min_PixelSpacing_2sf", "max_TotalPixelMatrixRows",
    "max_TotalPixelMatrixColumns", "staining_usingSubstance_CodeMeaning",
    "tissueFixative_CodeMeaning", "embeddingMedium_CodeMeaning",
    "primaryAnatomicStructure_CodeMeaning", "source_DOI", "license_short_name",
]


def collection_attrs(collection: str, rows, idc_version: str) -> dict:
    return {"collection": collection,
            "description": f"Whole-slide H&E images of TCGA {collection.removeprefix('tcga_').upper()}, "
                           f"from NCI Imaging Data Commons {idc_version}.",
            "source": "https://portal.imaging.datacommons.cancer.gov/",
            "source_doi": sorted(set(rows.source_DOI)),
            "license": sorted(set(rows.license_short_name))}


def write_slide(store, collection: str, slide, image: ManifestGroup) -> None:
    root = zarr.open_group(store, mode="a")
    patient = root.require_group(f"{collection}/{slide.PatientID}")
    patient.attrs.update({"patient_id": slide.PatientID, "collection": collection})
    group = patient.require_group(slide.ContainerIdentifier)
    group.attrs.update({"idc": jsonable_row(slide, SLIDE_FIELDS)})
    image.to_icechunk(store, group=f"{collection}/{slide.PatientID}/{slide.ContainerIdentifier}/image")


# Committing
#
# Slides are parsed on a pool of threads, since each spends most of its time
# downloading, and written to the store on the main thread. Every few slides
# the batch is committed, so a run that stops loses at most one batch, and a
# rerun skips every slide already on the branch.

def open_repository(location: str):
    import icechunk

    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location, source_prefixes=[BUCKET])
    from minus80.core.icechunk_io import create_repo
    return create_repo(location, [], prefixes=[BUCKET], open_existing=True)


def present(repo, branch: str, collection: str) -> set[str]:
    try:
        root = zarr.open_group(repo.readonly_session(branch).store, mode="r")
    except zarr.errors.GroupNotFoundError:  # a new repo has no root group yet
        return set()
    if collection not in root:
        return set()
    return {f"{p}/{s}" for p, g in root[collection].groups() for s, _ in g.groups()}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="local path, s3:// URL or arraylake://org/repo")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--collection", default="tcga_kirc")
    ap.add_argument("--limit", type=int, help="ingest only the first N slides")
    ap.add_argument("--only", nargs="+", metavar="BARCODE",
                    help="of the selected slides, ingest only these, e.g. to test on small ones")
    ap.add_argument("--workers", type=int, default=8, help="slides parsed at once")
    ap.add_argument("--batch", type=int, default=10, help="slides per commit")
    args = ap.parse_args(argv)

    series, slides, instances, idc_version = idc_tables()
    rows = select_slides(series, slides, args.collection, args.limit)
    repo = open_repository(args.repo)
    done = present(repo, args.branch, args.collection)
    if args.only:
        rows = rows[rows.ContainerIdentifier.isin(args.only)]
    todo = [r for r in rows.itertuples() if f"{r.PatientID}/{r.ContainerIdentifier}" not in done]
    say(f"{len(rows)} slides selected from {args.collection} (IDC {idc_version}); "
        f"{len(rows) - len(todo)} already on {args.branch}, {len(todo)} to ingest")

    session = repo.writable_session(args.branch)
    root = zarr.open_group(session.store, mode="a")
    root.attrs.update({"description": "Whole-slide images from The Cancer Genome Atlas, "
                                      "as published by NCI Imaging Data Commons."})
    root.require_group(args.collection).attrs.update(collection_attrs(args.collection, rows, idc_version))

    started, pending, written = time.monotonic(), 0, 0
    with ThreadPoolExecutor(args.workers) as pool:
        futures = {}
        for slide in todo:
            levels = pyramid(instances, slide.SeriesInstanceUID)
            futures[pool.submit(virtualize_slide, slide, levels)] = (slide, levels)
        for future in as_completed(futures):
            slide, levels = futures[future]
            arrays = future.result()
            write_slide(session.store, args.collection, slide, image_group(slide, levels, arrays))
            written += 1
            pending += 1
            elapsed = time.monotonic() - started
            say(f"{written} of {len(todo)} slides: {slide.ContainerIdentifier}, "
                f"{len(arrays)} levels, base {arrays['0'].shape[1]} x {arrays['0'].shape[2]}; "
                f"{elapsed / written:.1f} s a slide, about {elapsed / written * (len(todo) - written):.0f} s left")
            if pending == args.batch or written == len(todo):
                snapshot = commit(session, f"Add {pending} {args.collection} slides from IDC {idc_version}",
                                  metadata={"collection": args.collection, "slides": pending,
                                            "idc_version": idc_version})
                say(f"committed {pending} slides as {snapshot}")
                session = repo.writable_session(args.branch)
                pending = 0
    if session.has_uncommitted_changes:
        say(f"committed {commit(session, 'Describe the root and collection groups')}")


if __name__ == "__main__":
    main()
