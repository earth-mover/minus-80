"""Reference IDC's whole-slide images from an OME-Zarr Icechunk store, slide by slide.

NCI Imaging Data Commons (IDC) publishes slide microscopy as DICOM in the
public bucket ``s3://idc-open-data``: one prefix per series, one object per
instance. A slide is one series. Each pyramid level is one tiled instance,
whose frames are the level's JPEG (or JPEG 2000) tiles. The store references
those tiles where they lie, each reference pinned to its object's ETag, so
every slide stays one bucket away from its pixels and a rewritten object is
refused instead of read at stale offsets.

The store holds, per slide::

    <collection>/<patient>/<slide>/
        image/       OME-Zarr multiscale (c, y, x), every VOLUME level, virtual
        thumbnail/   the THUMBNAIL instance, one uncompressed frame, virtual
        overview/    the OVERVIEW instance (photograph of the glass), virtual
        dicom        every instance's DICOM JSON header, one native document

For TCGA, ``nuclei.py`` adds the Pan-Cancer-Nuclei-Seg mask and outlines
(phase 2 of PLAN.md, trial code).

Run::

    uv run --package idc-ingest python ingests/idc/ingest.py REPO --collection gtex [--limit N]
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from collections import Counter
from functools import partial
from importlib.metadata import version

import numpy as np
import pydicom
import zarr
from minus80.core.icechunk_io import commit
from minus80.ome import image_attrs, rgb_omero
from minus80.parsers.dicom import dicom_json, wsi_level_array

from plumbing import Source, Writer, in_forks, open_repository, say

UM = {"c": None, "y": "micrometer", "x": "micrometer"}

#: Slides per commit. Each slide adds about ten nodes and five manifests,
#: and one batch is one merge of that many forks, all held in memory at once.
BATCH = 250

#: The codecs a reader needs, and the package that provides each. verify.py
#: refuses any other codec name in the store.
CODECS = {
    "bytes": "zarr", "transpose": "zarr", "zstd": "zarr", "blosc": "zarr",
    "imagecodecs_jpeg": "imagecodecs", "imagecodecs_jpeg2k": "imagecodecs",
}

#: How a reader gets those codecs, stated in the root so the store says it itself.
READING = (
    "Reading the slide tiles needs imagecodecs, whose Zarr codecs decode the DICOM JPEG and "
    "JPEG 2000 tiles: pip install imagecodecs, then call imagecodecs.zarr.register_codecs() "
    "before opening an array, or install imagecodecs-zarr, which registers them with zarr "
    "automatically. Thumbnails, overviews and DICOM headers need only zarr. Virtual chunks "
    "read s3://idc-open-data anonymously (AWS us-east-1).")


# Selection
#
# idc-index is IDC's own catalog: one row per series, with the collection,
# patient, license and the bucket prefix, and per slide its tissue, stain and
# preparation (sm_index). Its version names the IDC release the store
# reflects. A slide's name is its ContainerIdentifier, the barcode on the
# glass; a patient is a TCGA participant or a GTEx donor.

def catalog(collection: str) -> list[dict]:
    from idc_index import IDCClient

    client = IDCClient()
    client.fetch_index("sm_index")
    rows = client.index.merge(client.sm_index, on="SeriesInstanceUID")
    rows = rows[rows.collection_id == collection] if collection == "gtex" else \
        rows[rows.collection_id.str.startswith("tcga_")]
    slides = []
    for _, r in rows.sort_values("ContainerIdentifier").iterrows():
        slides.append({
            "collection": r.collection_id, "patient": r.PatientID, "slide": r.ContainerIdentifier,
            "crdc_series_uuid": r.crdc_series_uuid,
            "idc": {"SeriesInstanceUID": r.SeriesInstanceUID, "StudyInstanceUID": r.StudyInstanceUID,
                    "crdc_series_uuid": r.crdc_series_uuid, "license": r.license_short_name,
                    "source_DOI": r.source_DOI, "series_init_idc_version": int(r.series_init_idc_version),
                    "series_revised_idc_version": int(r.series_revised_idc_version)},
            # sm_index holds a list where a slide states several codes, such as
            # its two stains.
            "specimen": {k: plain(v) for k, v in {
                "tissue": r.primaryAnatomicStructure_CodeMeaning,
                "tissue_code": r.primaryAnatomicStructure_code_designator_value_str,
                "stain": r.staining_usingSubstance_CodeMeaning,
                "embedding_medium": r.embeddingMedium_CodeMeaning,
                "fixative": r.tissueFixative_CodeMeaning,
                "magnification": r.ObjectiveLensPower,
            }.items()},
        })
    if not slides:
        raise ValueError(f"idc-index lists no slides for {collection!r}")
    duplicates = [name for name, n in Counter(s["slide"] for s in slides).items() if n > 1]
    if duplicates:
        raise ValueError(f"slides named twice: {sorted(duplicates)[:5]}")
    return slides


def plain(value):
    """An idc-index value as JSON: a list for an array, None for a missing number."""
    if isinstance(value, np.ndarray):
        return [str(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return None if np.isnan(value) else float(value)
    return value


def about(collection: str) -> dict:
    """What IDC states about the collection as a whole: its release and citations."""
    from idc_index import IDCClient

    client = IDCClient()
    ids = ["gtex"] if collection == "gtex" else None
    return {"idc_version": client.get_idc_version(), "idc_index_data": version("idc-index-data"),
            "citations": [c.strip() for c in client.citations_from_selection(collection_id=ids)]}


# The slide
#
# A slide series holds one VOLUME instance per pyramid level, a THUMBNAIL, and
# for GTEx an OVERVIEW photograph of the glass slide. Each instance is read
# through a block reader that fetches its header and, for a level whose Basic
# Offset Table is filled in, that table and the last frame's Item header:
# about 200 KB of a file of tens or hundreds of megabytes. A level whose
# table is empty is read whole by the parser's walk.
#
# The levels share the total pixel matrix's top-left corner, so their
# translations make the corners coincide. The pixel size is the shared
# functional group's PixelSpacing, stated in millimeters; each level's own
# stated value is kept beside the shape-derived scale OME-Zarr receives.
#
# Every instance's DICOM JSON goes into one document per slide, keyed as the
# store names the instance. The five headers of a GTEx slide repeat the same
# 140 KB ICC profile, which compression stores about once.

def pixel_spacing_um(ds: pydicom.Dataset) -> list[float]:
    measures = ds.SharedFunctionalGroupsSequence[0].PixelMeasuresSequence[0]
    return [float(v) * 1000 for v in measures.PixelSpacing]


def write_slide(w: Writer, source: Source, s: dict, group: str) -> dict:
    """Read every instance of one slide's series, then write the slide.

    All reads come first, so a read that fails leaves the fork untouched and
    a retry in the same fork starts clean.
    """
    kinds: dict[str, list] = {}
    etags = {}
    for url, _, etag in source.objects(s["crdc_series_uuid"]):
        f = source.reader(url)
        ds = pydicom.dcmread(f, stop_before_pixels=True)
        kinds.setdefault(str(ds.ImageType[2]), []).append((url, f, ds))
        etags[url] = etag
    unknown = set(kinds) - {"VOLUME", "THUMBNAIL", "OVERVIEW", "LABEL"}
    if unknown or "VOLUME" not in kinds:
        raise ValueError(f"{s['slide']}: instance types {sorted(kinds)}")
    if "LABEL" in kinds:
        raise ValueError(f"{s['slide']}: has a LABEL photograph, which this ingest does not place")
    for kind in ("THUMBNAIL", "OVERVIEW"):
        if len(kinds.get(kind, [])) > 1:
            raise ValueError(f"{s['slide']}: {len(kinds[kind])} {kind} instances")

    levels = sorted(kinds["VOLUME"], key=lambda t: -int(t[2].TotalPixelMatrixRows))
    arrays = {str(k): wsi_level_array(f, url_for=lambda _f, u=url: u) for k, (url, f, _) in enumerate(levels)}
    extras = {name: (wsi_level_array(f, url_for=lambda _f, u=url: u), ds)
              for kind, name in (("THUMBNAIL", "thumbnail"), ("OVERVIEW", "overview"))
              for url, f, ds in kinds.get(kind, [])}
    headers = {f"image/{k}": dicom_json(f) for k, (_, f, _) in enumerate(levels)}
    headers.update({name: dicom_json(f) for kind, name in (("THUMBNAIL", "thumbnail"), ("OVERVIEW", "overview"))
                    for _, f, _ in kinds.get(kind, [])})

    shapes = [a.shape for a in arrays.values()]
    spacing = [pixel_spacing_um(ds) for _, _, ds in levels]
    tile = arrays["0"].metadata.chunk_grid.chunk_shape[1:]
    codec = arrays["0"].metadata.codecs[-1].to_dict()["name"]
    spec = s["specimen"]
    magnification = f", scanned at {spec['magnification']:g}x" if spec.get("magnification") else ""
    w.virtual(f"{group}/image", arrays, {
        "ome": image_attrs(name=s["slide"], dims=("c", "y", "x"), level_shapes=shapes,
                           scale={"c": 1, "y": spacing[0][0], "x": spacing[0][1]},
                           translation={"c": 0, "y": 0, "x": 0}, units=UM, omero=rgb_omero(),
                           description="The scanner's own pyramid, as IDC publishes it."),
        "description": (f"H&E-stained section of {spec['tissue'].lower()} from donor {s['patient']}, slide "
                        f"{s['slide']}{magnification}: {shapes[0][2]:,} x {shapes[0][1]:,} pixels of "
                        f"{spacing[0][0]:.3f} µm at full resolution, and {len(shapes) - 1} coarser levels."),
        "stated_pixel_spacing_um": spacing,
        "layout": (f"Each chunk is one {tile[0]} x {tile[1]} {codec.removeprefix('imagecodecs_')} tile of "
                   "the level's DICOM instance in IDC's bucket; level k's header is ../dicom['image/k']."),
    }, etags)

    for name, (marr, ds) in extras.items():
        if name == "thumbnail":
            um = pixel_spacing_um(ds)
            # Placed as a coarser level would be: its first pixel's corner on level 0's.
            ome = image_attrs(name=f"{s['slide']} {name}", dims=("c", "y", "x"), level_shapes=[marr.shape],
                              scale={"c": 1, "y": um[0], "x": um[1]}, units=UM, omero=rgb_omero(),
                              translation={"c": 0, "y": (um[0] - spacing[0][0]) / 2,
                                           "x": (um[1] - spacing[0][1]) / 2})
            about = "A small image of the whole slide, in the same micrometer frame as the image."
        else:
            ome = image_attrs(name=f"{s['slide']} {name}", dims=("c", "y", "x"), level_shapes=[marr.shape],
                              scale={"c": 1, "y": 1, "x": 1}, omero=rgb_omero())
            about = ("A photograph of the whole glass slide. It is not aligned with the scan, so its axes "
                     "carry no unit.")
        w.virtual(f"{group}/{name}", {"0": marr}, {"ome": ome, "description": about}, etags)

    text = json.dumps(headers).encode()
    w.native(f"{group}/dicom", np.frombuffer(text, np.uint8), dims=("byte",), attrs={
        "description": ("The DICOM header of each image of this slide, as IDC publishes it: scanner, "
                        "specimen, optical path, color profile and pixel geometry. One UTF-8 JSON object "
                        "(DICOM JSON, PS3.18) keyed by the node holding the image: image/0, image/1, ..., "
                        "thumbnail, overview."),
        "format": "JSON, UTF-8",
    })
    return {"levels": len(shapes), "codec": codec, "extras": list(extras), "shape": list(shapes[0][1:]),
            "tile": list(tile), "instances": sum(len(v) for v in kinds.values()),
            "header_bytes": len(text), "pixel_um": spacing[0][0]}


# The hierarchy
#
# A collection group states its license, DOI and citation once. A patient
# group holds that patient's slides, since a TCGA patient has several and a
# GTEx donor dozens. Every collection and patient group is committed before
# the slides, because two forks that each create the same group cannot be
# merged. Slides are then written in batches, each slide in its own fork, and
# every batch is one commit. A rerun skips slides already present, so a
# stopped run resumes where it stopped.

def prepare(w: Writer, slides: list[dict], facts: dict) -> None:
    donors: dict[str, list[dict]] = {}
    for s in slides:
        donors.setdefault(f"{s['collection']}/{s['patient']}", []).append(s)
    for name, s in {s["collection"]: s for s in slides}.items():
        if name not in w.root:
            w.group(name, {
                "description": DESCRIPTIONS[name]["collection"],
                "license": s["idc"]["license"], "source_DOI": s["idc"]["source_DOI"],
                "citations": facts["citations"]})
    for name, held in sorted(donors.items()):
        if name not in w.root:
            tissues = sorted({s["specimen"]["tissue"] for s in held})
            w.group(name, {
                "description": (f"Donor {held[0]['patient']}: {len(held)} H&E slides of {len(tissues)} "
                                f"tissues: {', '.join(t.lower() for t in tissues)}."),
                "PatientID": held[0]["patient"]})


#: What each collection is, in the words a reader of the store needs.
DESCRIPTIONS = {
    "gtex": {
        "title": "GTEx histology: H&E whole-slide images of healthy human tissues",
        "collection": ("The Genotype-Tissue Expression (GTEx) project's histology: H&E-stained sections of "
                       "healthy human tissues sampled across the body, one slide per tissue sample, from GTEx "
                       "donors, as NCI Imaging Data Commons publishes them."),
    },
}


def slide_path(s: dict) -> str:
    return f"{s['collection']}/{s['patient']}/{s['slide']}"


def root_attributes(collection: str, slides: list[dict], facts: dict, source: Source,
                    refused: dict[str, str]) -> dict:
    tissues = {s["specimen"]["tissue"] for s in slides}
    donors = {s["patient"] for s in slides}
    license = sorted({s["idc"]["license"] for s in slides})
    return {
        "title": DESCRIPTIONS[collection]["title"],
        "description": (
            f"{len(slides):,} H&E whole-slide images of {len(tissues)} healthy human tissue types from "
            f"{len(donors):,} GTEx donors, from NCI Imaging Data Commons release {facts['idc_version']}. "
            "Each slide is an OME-Zarr image at full resolution and the scanner's coarser levels, in "
            "micrometers, with a thumbnail, a photograph of the glass slide and its DICOM headers, under "
            f"gtex/<donor>/<slide>. License {', '.join(license)}; cite the works in citations."),
        "license": license[0] if len(license) == 1 else license,
        "citations": facts["citations"],
        "idc_version": facts["idc_version"],
        "idc_index_data": facts["idc_index_data"],
        "codecs": dict(CODECS),
        "reading": READING,
        "layout": {
            "hierarchy": "<collection>/<donor>/<slide>, named as IDC names them",
            "source": f"{source.root}<crdc_series_uuid>/<crdc_instance_uuid>.dcm, read anonymously",
            "chunks": ("each chunk of image, thumbnail and overview is one tile or frame of a DICOM "
                       "instance in IDC's bucket, read where it lies"),
            "pinning": "each chunk reference is pinned to its object's ETag, as the bucket listing reported it",
        },
        "left_out": refused,
    }


def slide_job(source: Source, fork, s: dict) -> dict:
    """One slide written into its own fork, in a worker process."""
    return write_slide(Writer(fork), source, s, slide_path(s))


def run(location: str, collection: str, slides: list[dict], source: Source, facts: dict, *,
        workers: int) -> None:
    repo = open_repository(location, source.root)
    session = repo.writable_session("main")
    w = Writer(session)
    todo = [s for s in slides if slide_path(s) + "/image" not in w.root]
    say(f"{len(slides)} slides listed, {len(slides) - len(todo)} already in the store, {len(todo)} to write")
    prepare(w, slides, facts)
    if session.has_uncommitted_changes:
        say(f"groups: {commit(session, f'Add the {collection} collection and its donors')}")

    refused: dict[str, str] = dict(w.root.attrs.get("left_out", {}))
    by_name = {s["slide"]: s for s in todo}
    job = partial(slide_job, source)
    batches = -(-len(todo) // BATCH)
    started, written = time.monotonic(), 0

    for number, start in enumerate(range(0, len(todo), BATCH), 1):
        batch = [s["slide"] for s in todo[start:start + BATCH]]
        session = repo.writable_session("main")
        began = time.monotonic()
        done, failed = in_forks(session, {name: by_name[name] for name in batch}, job, workers=workers)
        session.merge(*(fork for fork, _ in done.values()))
        merged_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        w = Writer(session)
        for name, (_, result) in done.items():
            s = by_name[name]
            w.group(slide_path(s), {
                "description": (f"Slide {name}: {s['specimen']['tissue'].lower()} from donor {s['patient']}, "
                                f"H&E, {result['levels']} levels from {result['shape'][1]:,} x "
                                f"{result['shape'][0]:,} pixels of {result['pixel_um']:.3f} µm."),
                "ContainerIdentifier": name, "PatientID": s["patient"],
                "specimen": s["specimen"], "idc": s["idc"],
                "source_prefix": f"{source.root}{s['crdc_series_uuid']}/",
                "levels": result["levels"], "level_0_shape": result["shape"]})
        refused.update(failed)
        for name, reason in failed.items():
            say(f"{name}: left out: {reason}")
        read = time.monotonic() - began
        if done:
            tissues = len({by_name[n]["specimen"]["tissue"] for n in done})
            snapshot = commit(session, f"Add {len(done)} GTEx slides of {tissues} tissues, {batch[0]} to {batch[-1]}",
                              metadata={"slides": len(done), "left_out": len(failed)})
        else:
            snapshot = "nothing to commit"
        written += len(batch)
        elapsed = time.monotonic() - started
        rate = written / elapsed
        say(f"batch {number} of {batches}: {written:,} of {len(todo):,} slides, {rate:.1f} per s, about "
            f"{(len(todo) - written) / rate / 60:.0f} min left; read {read:.0f} s, committed {snapshot} in "
            f"{time.monotonic() - began - read:.0f} s; peak memory {peak_gb(merged_peak):.1f} GB")

    session = repo.writable_session("main")
    w = Writer(session)
    # A slide left out by an earlier run and written by this one is no longer left out.
    every = {s["slide"]: s for s in slides}
    refused = {name: why for name, why in refused.items()
               if name not in every or slide_path(every[name]) + "/image" not in w.root}
    w.group("", root_attributes(collection, slides, facts, source, refused))
    if session.has_uncommitted_changes:
        say(f"root: {commit(session, 'Describe the GTEx histology collection')}")


def peak_gb(maxrss: int) -> float:
    """``ru_maxrss`` in GB: bytes on macOS, kilobytes on Linux."""
    return maxrss / (1e9 if sys.platform == "darwin" else 1e6)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("repo")
    p.add_argument("--collection", choices=("gtex",), default="gtex")
    p.add_argument("--slides", nargs="*", help="only these ContainerIdentifiers")
    p.add_argument("--limit", type=int, help="only the first N slides, by name")
    p.add_argument("--workers", type=int, default=os.cpu_count(),
                   help="worker processes; the parse is CPU-bound, so one per core")
    a = p.parse_args()
    slides = catalog(a.collection)
    if a.slides:
        slides = [s for s in slides if s["slide"] in set(a.slides)]
    if a.limit:
        slides = slides[:a.limit]
    run(a.repo, a.collection, slides, Source(), about(a.collection), workers=a.workers)


if __name__ == "__main__":
    main()
