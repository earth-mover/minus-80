"""Build the CAMELYON store: the CAMELYON16 and CAMELYON17 slides, each with its mask, outlines and extra pages.

The CAMELYON challenges published 1,399 whole-slide images of sentinel lymph
nodes from breast cancer patients, with the pathologists' tumor outlines,
reference masks and slide- and patient-level labels, in the public bucket
``s3://camelyon-dataset`` (CC0). This ingest makes one Icechunk store of both:
every slide an OME-Zarr image whose chunks are the TIFF's own 512 x 512 JPEG
tiles, referenced in place, with its mask as a label image, its outlines as
arrays and GeoJSON, and the challenge's labels in the attributes.

- ``CAMELYON16/<slide>/``: ``image`` (with ``labels/mask``), ``tissue``,
  ``macro`` or ``label`` where the file has them, ``shapes`` where an outline
  XML exists. The container lists the slides in ``OME/series`` and their
  labels in ``annotations``.
- ``CAMELYON17/<patient>/<node>/``: the same children, one level deeper,
  because CAMELYON17's labels are per patient as well as per slide.

Each run adds one challenge's slides, 100 per commit: every slide is parsed
in its own fork of one session, on a thread pool, then the forks are merged,
the container's table is rewritten and the session is committed.
``plumbing.py`` holds the source reads, the writes into a session and the
threads. LAYOUTS.md says how the bucket maps onto the store and why; PLAN.md
holds the decisions.

    uv run --package camelyon-ingest python ingests/camelyon/ingest.py \\
        --challenge CAMELYON17 --repo <dir | arraylake://org/repo> [--slides patient_004_node_4,...]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree import ElementTree

import ngff_zarr as nz
import numpy as np
import tifffile
import zarr
from minus80.core.decisions import Decision
from minus80.core.icechunk_io import commit
from minus80.core.progress import Heartbeat
from minus80.ome import (annotation_table, channel_omero, image_annotation, image_attrs, label_attrs,
                         labels_attrs, rgb_omero, root_attrs, series_attrs)
from minus80.parsers import asap
from minus80.parsers.tiff.pages import level_array, page_tags
from plumbing import SOURCE, Source, Writer, in_forks, open_repository, say
from virtualizarr.manifests import ManifestArray

# The archive
#
# The bucket holds the two challenges side by side, each with the same
# directories: ``images/`` (the slides, BigTIFF, 512 x 512 JPEG tiles),
# ``masks/`` (``<slide>_mask.tif``, LZW, one level per slide level),
# ``annotations/`` (``<slide>.xml``, ASAP outlines), a ``checksums.md5``, a
# README and the label tables. CAMELYON16 adds ``background_tissue/``, an
# automatic tissue segmentation per slide. Every file joins to its slide by
# exact basename.
#
# The slides come in two TIFF dialects. A Philips-format TIFF (CAMELYON16's
# 398 converted slides, and CAMELYON17's center 4, scanned on a Philips
# scanner) states its pixel size only in the XML of its ImageDescription and
# carries the scanner's overview photograph as a "Macro" page. A generic TIFF
# written by ASAP's converter (CAMELYON16's normal_144, and CAMELYON17's other
# four centers) states its pixel size in its resolution tags. Both store RGB
# JPEG tiles with shared JPEGTables, which one codec chain decodes.

UM = {"y": "micrometer", "x": "micrometer"}

#: Whether an outline's integer coordinate is a pixel's center or its corner. Neither
#: the XML nor the README says; the evidence is how well each reading reproduces the
#: challenge's own masks, which were rasterized from these polygons.
PIXEL_CONVENTION = {
    "status": "open",
    "stated_by_source": False,
    "evidence": ("Rasterizing six Tumor outlines of tumor_091 and tumor_056 and comparing with the reference "
                 "mask's level 0, reading coordinate k as the center of pixel k disagrees with the mask on "
                 "fewer pixels in all six (for tumor_091 Annotation 5: 416 of 8,238,976, against 4,817 when "
                 "reading k as pixel k's top-left corner). Neither reading reproduces the mask exactly."),
}

CITATION_16 = ("Ehteshami Bejnordi B, Veta M, Johannes van Diest P, et al. Diagnostic Assessment of Deep "
               "Learning Algorithms for Detection of Lymph Node Metastases in Women With Breast Cancer. "
               "JAMA. 2017;318(22):2199-2210. doi:10.1001/jama.2017.14585")
CITATION_17 = ("Bandi P, Geessink O, Manson Q, et al. From Detection of Individual Metastases to "
               "Classification of Lymph Node Status at the Patient Level: The CAMELYON17 Challenge. "
               "IEEE Trans Med Imaging. 2019;38(2):550-560. doi:10.1109/TMI.2018.2867350")
CITATION_DATASET = ("Litjens G, Bandi P, Ehteshami Bejnordi B, et al. 1399 H&E-stained sentinel lymph node "
                    "sections of breast cancer patients: the CAMELYON dataset. GigaScience. 2018;7(6):giy065. "
                    "doi:10.1093/gigascience/giy065")


@dataclass(frozen=True)
class Challenge:
    """What differs between the two challenges: where they sit, how slides are named, what they publish."""

    name: str
    #: OME-Zarr version of the challenge's metadata. CAMELYON16 was published
    #: as 0.5 and stays so; a new challenge is written as 0.6 (AGENTS.md).
    version: str
    landing_page: str
    #: The challenge's own files that describe the slides, stored verbatim.
    source_files: tuple[str, ...]
    #: The mask's values, from the challenge's README.
    mask_properties: tuple[dict, ...]

    @property
    def prefix(self) -> str:
        return f"{self.name}/"


C16 = Challenge(
    name="CAMELYON16", version="0.5", landing_page="https://camelyon16.grand-challenge.org/",
    source_files=("README.md", "license.txt", "checksums.md5", "evaluation/reference.csv",
                  "evaluation/example.csv", "evaluation/evaluate.py",
                  "pathology-tissue-background-segmentation.json", "background_tissue/results.json"),
    mask_properties=(
        {"label-value": 0, "name": "background"},
        {"label-value": 1, "name": "normal tissue",
         "note": ("Detected automatically (doi:10.7717/peerj.8242), so it can be wrong; not provided for "
                  "the slides whose annotation is not exhaustive (README.md).")},
        {"label-value": 2, "name": "tumor", "note": "The annotation polygons converted to pixels (README.md)."},
    ))

C17 = Challenge(
    name="CAMELYON17", version="0.6", landing_page="https://camelyon17.grand-challenge.org/",
    source_files=("README.md", "license.txt", "checksums.md5", "stages.csv", "evaluation/example.csv",
                  "evaluation/evaluate.py"),
    mask_properties=(
        {"label-value": 0, "name": "background"},
        {"label-value": 1, "name": "normal tissue",
         "note": "Detected automatically (doi:10.7717/peerj.8242), so it can be wrong (README.md)."},
        {"label-value": 2, "name": "tumor", "note": "The annotation polygons converted to pixels (README.md)."},
    ))

CHALLENGES = {c.name: c for c in (C16, C17)}


def slide_path(challenge: Challenge, slide: str) -> str:
    """The slide's group: ``CAMELYON16/tumor_056``, or ``CAMELYON17/patient_004/node_4``."""
    if challenge is C17:
        patient, node = slide.rsplit("_node_", 1)
        return f"{challenge.name}/{patient}/node_{node}"
    return f"{challenge.name}/{slide}"


# The challenge's tables
#
# What each slide is (its split, label, center, whether its outlines are
# exhaustive) is stated only in the challenge's small text files. They are
# read once per run, before any slide, and every value in a slide's row comes
# from one of them or from the slide's own files.
#
# CAMELYON16: ``evaluation/reference.csv`` gives each slide's type (normal or
# tumor), class (negative, micro, macro) and center; the README lists the
# slides whose outlines are not exhaustive; ``background_tissue/results.json``
# logs the tissue segmentation of each.
#
# CAMELYON17: ``stages.csv`` gives the 100 training patients' pN stage and
# their 500 slides' labels, with each patient's center. The bucket states no
# label and no center for the 100 test patients; ``evaluation/example.csv``
# lists them with made-up values, as a submission format.


def checksums(source: Source, challenge: Challenge) -> dict[str, str]:
    lines = source.read(challenge.prefix + "checksums.md5").decode().splitlines()
    return {name.lstrip("*"): md5 for md5, name in (line.split(maxsplit=1) for line in lines if line.strip())}


def readme_notes(readme: str) -> str:
    """The README's "Notes about the data" section."""
    return readme.split("### Notes about the data", 1)[1].split("###", 1)[0]


def context_16(source: Source) -> dict:
    readme = source.read("CAMELYON16/README.md").decode()
    section = readme.split("### Notes about the data", 1)[1].split("The following files", 1)[0]
    partial = {m.group(1): m.group(2) for m in re.finditer(r"^\* (\w+)(?:: (.*))?$", section, flags=re.M)}
    rows = csv.DictReader(io.StringIO(source.read("CAMELYON16/evaluation/reference.csv").decode()))
    entries = json.loads(source.read("CAMELYON16/background_tissue/results.json"))
    sums = checksums(source, C16)
    return {
        "sums": sums,
        "slides": sorted(k.removeprefix("images/").removesuffix(".tif") for k in sums
                         if k.startswith("images/") and k.endswith(".tif")),
        "reference": {r["image"].removesuffix(".tif"): r for r in rows},
        "partial": partial,
        "results": {PurePosixPath(e["entity"]["input"].split(":", 1)[1]).stem: e for e in entries},
        "settings": json.loads(source.read("CAMELYON16/pathology-tissue-background-segmentation.json")),
    }


def context_17(source: Source) -> dict:
    rows = list(csv.DictReader(io.StringIO(source.read("CAMELYON17/stages.csv").decode())))
    images = [k.rsplit("/", 1)[1].removesuffix(".tif") for k in source.keys("CAMELYON17/images/")]
    notes = readme_notes(source.read("CAMELYON17/README.md").decode())
    patients: dict[str, dict] = {}
    for slide in images:
        patient = slide.rsplit("_node_", 1)[0]
        patients.setdefault(patient, {"patient": patient, "split": None, "stage": None, "center": None,
                                      "slides": []})["slides"].append(slide)
    labels = {}
    for r in rows:
        name = r["patient"]
        if name.endswith(".zip"):
            if (patient := name.removesuffix(".zip")) not in patients:
                raise ValueError(f"stages.csv lists {patient}, which has no slide in images/")
            patients[patient].update(stage=r["stage"], center=r["center"])
        else:
            labels[name.removesuffix(".tif")] = r["stage"]
    for p in patients.values():
        # stages.csv lists the training patients only, and the README says
        # training and test hold 20 patients of each center, 100 each.
        p["split"] = "training" if p["stage"] is not None else "test"
    return {
        "sums": checksums(source, C17),
        "slides": sorted(images),
        "patients": patients,
        "labels": labels,
        "masks": {k.rsplit("/", 1)[1].removesuffix("_mask.tif") for k in source.keys("CAMELYON17/masks/")},
        "annotations": {k.rsplit("/", 1)[1].removesuffix(".xml") for k in source.keys("CAMELYON17/annotations/")},
        "notes": {m.group(0): " ".join(notes.split()) for m in re.finditer(r"patient_\d{3}_node_\d", notes)},
    }


CONTEXTS: dict[str, Callable[[Source], dict]] = {"CAMELYON16": context_16, "CAMELYON17": context_17}


# A slide's scan
#
# The slide's TIFF is opened once, in tifffile over range reads, so only its
# headers are fetched. Its first series is the pyramid, 7 to 10 levels; any
# further series are the Macro and Label pages. Each level is one virtual
# array whose chunks are that page's tiles, built by
# ``minus80.parsers.tiff.pages.level_array``. ``TiffParser`` would open the
# file again for each series and does not return the page tags or the
# ImageDescription, which the store keeps.
#
# Pixel size. A generic TIFF states it in its resolution tags. A Philips TIFF
# states two level-0 spacings in its ImageDescription: the scanned image's and
# the pixel-data representation's. tifffile reports the second; the store
# uses the first, which the slide's mask encodes in its resolution tags.
#
# Level scales. A generic level's pixel size is level 0's times the ratio of
# the shapes. A Philips TIFF's levels are exactly 2^k coarser (its
# representation spacings double per level), and each level is level 0's
# shape halved, rounding down, or the page's own tags where those are
# smaller: in 44 of CAMELYON17's 200 Philips slides some levels stop up to a
# tile short of the halved shape, cropped rather than rescaled. So a Philips
# level's scale is level 0's times 2^k, which is the shape ratio wherever no
# level is cropped.


def without_attributes(array: ManifestArray) -> ManifestArray:
    """``array`` with no attributes of its own.

    ``level_array`` states each level's dimensions, resolution-tag scale,
    channel names and source URL on the array. The image group above it
    already states all of them, in the form OME-Zarr readers use, and the
    published CAMELYON16 levels carry none, so the store keeps the two
    challenges alike.
    """
    return ManifestArray(metadata=array.metadata.update_attributes({}), chunkmanifest=array.manifest)


def series_levels(series: tifffile.TiffPageSeries, url: str, size: int) -> dict[str, ManifestArray]:
    return {str(i): without_attributes(level_array(level, url, size=size)) for i, level in enumerate(series.levels)}


def level_tags(w: Writer, parent: str, name: str, pages) -> list[dict]:
    """Each page's tags, its ImageDescription inline or as a document pointer."""
    out = []
    for page in pages:
        tags = {"page": page.index, **page_tags(page)}
        if page.description:
            tags["ImageDescription"] = w.text(parent, f"{name}-page{page.index}-ImageDescription",
                                              page.description, of=f"{name} (TIFF page {page.index})",
                                              fmt="TIFF ImageDescription, UTF-8")
        out.append(tags)
    return out


def quoted(value: str) -> list[float]:
    return [float(v) for v in re.findall(r'"([^"]+)"', value)]


def philips_spacing(tf: tifffile.TiffFile) -> dict:
    """The two level-0 pixel spacings a Philips ImageDescription states, in micrometers, as (y, x).

    DICOM PixelSpacing lists the row spacing first, so (y, x) is its own order.
    """
    tree = ElementTree.fromstring(tf.pages[0].description)
    wsi = [d for d in tree.iter("DataObject") if d.get("ObjectType") == "DPScannedImage"
           and d.findtext("Attribute[@Name='PIM_DP_IMAGE_TYPE']") == "WSI"]
    if len(wsi) != 1:
        raise ValueError(f"{len(wsi)} WSI scanned images in the ImageDescription")
    scanned = quoted(wsi[0].findtext("Attribute[@Name='DICOM_PIXEL_SPACING']"))
    reps = sorted((int(d.findtext("Attribute[@Name='PIIM_PIXEL_DATA_REPRESENTATION_NUMBER']")),
                   quoted(d.findtext("Attribute[@Name='DICOM_PIXEL_SPACING']")))
                  for d in wsi[0].iter("DataObject") if d.get("ObjectType") == "PixelDataRepresentation")
    return {"scanned_um": [v * 1000 for v in scanned],
            "representation_um": [[v * 1000 for v in s] for _, s in reps],
            "manufacturer": tree.findtext("Attribute[@Name='DICOM_MANUFACTURER']")}


def level_scales(base: list[float], shapes: list[tuple[int, ...]], philips: bool) -> list[dict]:
    """Each level's (c, y, x) pixel size, as the comment above this section says."""
    out = []
    for k, shape in enumerate(shapes):
        factors = [2.0 ** k] * 2 if philips else [b / s for b, s in zip(shapes[0][1:], shape[1:])]
        out.append({"c": 1 * (shapes[0][0] / shape[0]), "y": base[0] * factors[0], "x": base[1] * factors[1]})
    return out


def write_image(w: Writer, challenge: Challenge, slide: str, source: Source, md5: str | None) -> dict:
    """The slide's pyramid, and whichever extra pages (macro, label) the file has."""
    key = f"{challenge.prefix}images/{slide}.tif"
    url = source.url(key)
    group = slide_path(challenge, slide)
    tf = source.open_tiff(key)
    series = tf.series[0]
    size = tf.filehandle.size
    arrays = series_levels(series, url, size)
    shapes = [a.shape for a in arrays.values()]
    if tf.is_philips:
        spacing = philips_spacing(tf)
        base_um = spacing["scanned_um"]
        reps = spacing["representation_um"]
        if len(reps) < len(shapes) or any(abs(r[0] / reps[0][0] / 2 ** k - 1) > 1e-3 for k, r in enumerate(reps)):
            raise ValueError(f"{slide}: representation spacings {reps} do not double per level")
        agree = np.allclose(reps[0], base_um, rtol=1e-6)
        pixel_size = {
            "um": base_um,
            "source": "DICOM_PIXEL_SPACING of the scanned image (DPScannedImage, type WSI) in the ImageDescription",
            "scanned_um": spacing["scanned_um"],
            "representation_um": reps,
            "decision": ("The ImageDescription states the scan's spacing and each pixel-data representation's "
                         "(representation_um, one per level, the value tifffile reports); at level 0 they agree."
                         if agree else
                         "The ImageDescription states two level-0 spacings: the scan's and the pixel-data "
                         "representation's (representation_um, one per level, the value tifffile reports). "
                         "The scan's is used because the slide's mask and tissue files encode it in their "
                         "resolution tags."),
        }
        scanner = spacing["manufacturer"]
        cropped = [k for k, s in enumerate(shapes) if tuple(s[1:]) != (shapes[0][1] >> k, shapes[0][2] >> k)]
        note = ("Level shapes are tifffile's. For a Philips-format TIFF they are level 0's shape "
                "halved per level; the pages' ImageWidth/ImageLength tags pad each reduced level "
                "up to whole tiles, and those raw values are in tiff_tags.")
        if cropped:
            note += (f" Levels {', '.join(map(str, cropped))} are narrower than that: their tags stop at a "
                     "whole tile short of level 0's halved shape, and the scanner cropped them rather than "
                     "rescaling, so each level's pixel size is still 2^k times level 0's.")
    else:
        um = resolution_um(series.levels[0].keyframe)
        if um is None:
            raise ValueError(f"{slide}: no pixel size stated")
        base_um = list(um)
        pixel_size = {"um": base_um, "source": "XResolution, YResolution, ResolutionUnit of TIFF page 0"}
        scanner = None
        note = None
    scales = level_scales(base_um, shapes, tf.is_philips)
    tile = arrays["0"].metadata.chunk_grid.chunk_shape[1:]
    ome = image_attrs(name=slide, dims=("c", "y", "x"), level_shapes=shapes,
                      scale={"c": 1, "y": base_um[0], "x": base_um[1]}, translation={"c": 0, "y": 0, "x": 0},
                      level_scales=scales, units=UM, omero=rgb_omero(),
                      description="The scanner's own pyramid: each level is one tiled JPEG page of the TIFF.",
                      version=challenge.version)
    attrs = {
        "ome": ome,
        "description": (f"{challenge.name} slide {slide}: whole-slide image of an H&E-stained sentinel lymph node "
                        f"section, RGB. Each chunk is one {tile[0]} x {tile[1]} JPEG tile of the source TIFF."),
        "pixel_size": pixel_size,
        "level_shapes_note": note,
        "tiff_tags": level_tags(w, group, "image", [lvl.keyframe for lvl in series.levels]),
        "source_url": url,
        "source_md5": md5,
    }
    w.virtual(f"{group}/image", arrays, {k: v for k, v in attrs.items() if v is not None})

    extras = []
    for extra in tf.series[1:]:
        name = extra.name.lower()
        if name not in ("macro", "label"):
            raise ValueError(f"{slide}: unexpected series {extra.name!r}")
        page = extra.keyframe
        marr = without_attributes(level_array(extra.levels[0], url, size=size))
        ome = image_attrs(name=f"{slide} {name}", dims=("c", "y", "x"), level_shapes=[marr.shape],
                          scale={"c": 1, "y": 1, "x": 1}, omero=rgb_omero(), version=challenge.version)
        what = {"macro": "overview photograph of the whole glass slide",
                "label": "photograph of the slide's paper label"}[name]
        w.virtual(f"{group}/{name}", {"0": marr}, {
            "ome": ome,
            "description": (f"The scanner's {what} for slide {slide} (TIFF page {page.index}, '{extra.name}'), "
                            "one JPEG strip stored as one chunk. The file states no pixel size for it, so its "
                            "axes carry no unit and its scale is 1; it shares no frame with the slide image."),
            "tiff_tags": level_tags(w, group, name, [page]),
            "source_url": url,
        })
        extras.append(name)
    return {"base_um": list(base_um), "shapes": shapes, "scales": scales, "philips": tf.is_philips,
            "scanner": scanner, "extras": extras}


# The reference mask
#
# ``masks/<slide>_mask.tif`` is an LZW pyramid on the slide's own grid: its
# level k has the shape of the slide's level k, except where a Philips slide's
# level is cropped (above), where the mask keeps the full halved shape. It is
# the image's OME-Zarr label, which OME-Zarr requires to have as many levels
# as the image. Most masks have one or two levels fewer than their slide, so
# the missing coarse levels are derived from the mask's coarsest level with
# ngff-zarr's label-aware downsampling and stored natively. A tile stored
# with zero bytes, "all background", is a missing chunk.


def on_grid(mask: tuple[int, ...], image: dict, k: int) -> bool:
    """Whether a mask level of shape ``mask`` sits on the image's level ``k``."""
    full = image["shapes"][0][1:]
    return tuple(mask) == tuple(image["shapes"][k][1:]) or (
        image["philips"] and tuple(mask) == (full[0] >> k, full[1] >> k))


def write_mask(w: Writer, challenge: Challenge, slide: str, source: Source, image: dict, md5: str | None) -> dict:
    """``masks/<slide>_mask.tif`` as the image's label, with any coarse levels it lacks derived."""
    key = f"{challenge.prefix}masks/{slide}_mask.tif"
    url = source.url(key)
    tf = source.open_tiff(key)
    series = tf.series[0]
    arrays = series_levels(series, url, tf.filehandle.size)
    file_shapes = [a.shape for a in arrays.values()]
    if len(file_shapes) > len(image["shapes"]) or not all(on_grid(s, image, k) for k, s in enumerate(file_shapes)):
        raise ValueError(f"{slide}: mask levels {file_shapes} are not on the image's {image['shapes']}")
    path = f"{slide_path(challenge, slide)}/image/labels/mask"
    n_missing = len(image["shapes"]) - len(file_shapes)
    derived = {}
    if n_missing:
        last = series.levels[-1].asarray()
        factors = [2 ** (k + 1) for k in range(n_missing)]
        pyramid = nz.to_multiscales(nz.to_ngff_image(last, dims=("y", "x")), scale_factors=factors,
                                    method=nz.Methods.ITKWASM_LABEL_IMAGE)
        for k, im in enumerate(pyramid.images[1:], start=len(file_shapes)):
            plane = np.asarray(im.data)
            if not on_grid(plane.shape, image, k):
                raise ValueError(f"{slide}: derived mask level {k} {plane.shape}, image has {image['shapes'][k]}")
            derived[str(k)] = plane
    shapes = file_shapes + [p.shape for p in derived.values()]
    base = image["base_um"]
    ome = label_attrs(name="mask", dims=("y", "x"), level_shapes=shapes,
                      scale={"y": base[0], "x": base[1]}, translation={"y": 0, "x": 0}, units=UM,
                      level_scales=[{"y": s["y"], "x": s["x"]} for s in image["scales"]],
                      properties=list(challenge.mask_properties),
                      description="Levels are the mask file's own pages; any beyond them are derived.",
                      version=challenge.version)
    attrs = {
        "ome": ome,
        "description": (f"{challenge.name} reference mask of slide {slide}: 0 background, 1 normal tissue, "
                        "2 tumor. Level for level on the slide image's grid. A tile stored with zero bytes "
                        "in the file is a missing chunk, read as 0."),
        "tiff_tags": level_tags(w, slide_path(challenge, slide), "mask", [lvl.keyframe for lvl in series.levels]),
        "resolution_um": list(resolution_um(series.levels[0].keyframe) or []),
        "source_url": url,
        "source_md5": md5,
    }
    if md5 is None:
        del attrs["source_md5"]
        attrs["source_md5_note"] = f"{challenge.prefix}checksums.md5 does not list this file."
    wider = [k for k, s in enumerate(shapes) if tuple(s) != tuple(image["shapes"][k][1:])]
    if wider:
        attrs["grid_note"] = (f"Levels {', '.join(map(str, wider))} are wider than the image's levels of the "
                              "same index, whose pages the scanner cropped at a whole tile; the mask keeps "
                              "level 0's shape halved. Both have the same pixel size, so the mask's extra "
                              "columns lie past the image's right edge.")
    if derived:
        attrs["derived_levels"] = {
            k: (f"Not in the file, which has {len(file_shapes)} levels to the image's {len(image['shapes'])}; "
                f"derived from level {len(file_shapes) - 1} by ngff-zarr ITKWASM_LABEL_IMAGE, stored natively.")
            for k in derived}
    w.virtual(path, arrays, attrs)
    for k, plane in derived.items():
        w.native(f"{path}/{k}", plane, dims=("y", "x"))
    return {"levels": len(file_shapes), "derived": list(derived)}


def resolution_um(page: tifffile.TiffPage) -> tuple[float, float] | None:
    """The pixel size a page's resolution tags state, as (y, x) micrometers, or None.

    None unless all three tags are present and the unit is not NONE: tifffile
    answers INCH for an absent unit, and one pixel per inch is 25,400 µm.
    """
    if not all(t in page.tags for t in ("XResolution", "YResolution", "ResolutionUnit")):
        return None
    if page.resolutionunit == tifffile.RESUNIT.NONE:
        return None
    x, y = page.get_resolution(tifffile.RESUNIT.MICROMETER)
    return 1 / y, 1 / x


# The tissue map (CAMELYON16 only)
#
# ``background_tissue/<slide>_tissue.tif`` is an automatic tissue
# segmentation, 1 tissue and 0 background, whose level 0 has the shape of the
# slide's level 3. With 5 to 7 levels to the image's 8 to 10 it cannot be a
# label, which needs as many levels as its image, so it is an image beside the
# slide, on the slide's frame. Two of the 399 files are byte copies of the
# slide's mask, not segmentations, and are left out.


def write_tissue(w: Writer, slide: str, source: Source, image: dict, result: dict, settings: dict) -> dict:
    """``background_tissue/<slide>_tissue.tif`` as an image beside the slide."""
    key = f"CAMELYON16/background_tissue/{slide}_tissue.tif"
    url = source.url(key)
    tf = source.open_tiff(key)
    series = tf.series[0]
    arrays = series_levels(series, url, tf.filehandle.size)
    shapes = [a.shape for a in arrays.values()]
    um = resolution_um(series.levels[0].keyframe)
    if um is None:
        raise ValueError(f"{slide}: tissue file states no pixel size")
    matched = [k for k, s in enumerate(image["shapes"]) if tuple(s[1:]) == shapes[0]]
    if not matched:
        raise ValueError(f"{slide}: tissue level 0 {shapes[0]} matches no image level")
    level = matched[0]
    base = image["base_um"]
    expected = [b * image["shapes"][0][d + 1] / shapes[0][d] for d, b in enumerate(base)]
    if not np.allclose(um, expected, rtol=1e-3):
        raise ValueError(f"{slide}: tissue pixel size {um} disagrees with the image's frame {expected}")
    ome = image_attrs(name=f"{slide} tissue", dims=("y", "x"), level_shapes=shapes,
                      scale={"y": um[0], "x": um[1]},
                      translation={"y": (um[0] - base[0]) / 2, "x": (um[1] - base[1]) / 2}, units=UM,
                      omero=channel_omero([{"label": "tissue", "start": 0, "end": 1}], max_value=255),
                      description="The tissue file's own pyramid.", version=C16.version)
    w.virtual(f"CAMELYON16/{slide}/tissue", arrays, {
        "ome": ome,
        "description": (f"Automatic tissue segmentation of slide {slide}: 1 tissue, 0 background. Its level 0 "
                        f"is the grid of the slide image's level {level}. An image beside the slide rather than "
                        "a label of it, because it has fewer levels than the image and OME-Zarr 0.5 requires a "
                        "label to have as many. A tile stored with zero bytes is a missing chunk, read as 0."),
        "matches_image_level": level,
        "segmentation_settings": settings,
        "segmentation_result": result,
        "tiff_tags": level_tags(w, f"CAMELYON16/{slide}", "tissue", [lvl.keyframe for lvl in series.levels]),
        "source_url": url,
    })
    return {"levels": len(shapes)}


# The outlines
#
# ``annotations/<slide>.xml`` holds the pathologists' tumor outlines in ASAP's
# XML. The store keeps them twice: as a table of arrays that rebuilds the file
# byte for byte (``minus80.parsers.asap``), refused unless it does, and as
# GeoJSON in QuPath's feature schema, because no viewer reads vector
# annotations from inside Zarr. The GeoJSON cuts the Exclusion shapes out of
# the Tumor shapes as holes, which is how the README defines Exclusion.


def write_shapes(w: Writer, challenge: Challenge, slide: str, source: Source, base_um, md5: str) -> int:
    """``annotations/<slide>.xml`` as a vertex table, refused unless it regenerates the file exactly."""
    key = f"{challenge.prefix}annotations/{slide}.xml"
    xml = source.read(key)
    table = asap.parse(xml)
    if asap.to_xml(table) != xml:
        raise ValueError(f"{slide}: the table does not regenerate the XML byte for byte")
    path = f"{slide_path(challenge, slide)}/shapes"
    n = len(table["offset0"]) - 1
    w.group(path, {
        "description": (f"The pathologists' outlines of slide {slide} from its ASAP XML: {n} shapes. coords holds "
                        "every vertex as (x, y) in level-0 pixels of ../image, in file order; shape i is "
                        "coords[offset0[i]:offset0[i+1]]. name, type, group and color are ASAP's per-shape "
                        "Name, Type, PartOfGroup and Color. Group 'Exclusion' is a hole cut out of the "
                        "'Tumor' regions (README.md). geojson is the same outlines as QuPath annotations, holes already "
                        "cut, ready for File > Import objects."),
        "encoding": ("ragged arrays: coords + offset0, as shapely and SpatialData 0.1 lay out shapes; geojson holds the "
                     "same outlines as QuPath annotations"),
        "coordinate_frame": {"image": "../image", "level": 0, "units": "pixel", "columns": ["x", "y"],
                             "to_micrometer": {"x": base_um[1], "y": base_um[0]},
                             "pixel_convention": PIXEL_CONVENTION},
        "asap_groups": table["groups"],
        "regenerates_source": ("minus80.parsers.asap.to_xml rebuilds the source file byte for byte from "
                               "these arrays: coordinates printed with %.9g, ASAP's tab indentation."),
        "source_url": source.url(key),
        "source_md5": md5,
    })
    w.native(f"{path}/coords", table["coords"], dims=("vertex", "xy"),
             attrs={"columns": ["x", "y"], "units": "level-0 pixel"})
    w.native(f"{path}/offset0", table["offset0"], dims=("boundary",),
             attrs={"description": "Start of each shape's vertices in coords, then the total vertex count."})
    for column in asap.COLUMNS:
        w.native(f"{path}/{column}", table[column], dims=("shape",))
    fc = asap.to_geojson(table)
    w.native(f"{path}/geojson", np.frombuffer(json.dumps(fc).encode(), dtype=np.uint8), dims=("byte",), attrs={
        "description": (f"The outlines as QuPath annotations: a GeoJSON FeatureCollection in QuPath's feature schema "
                        f"(objectType, classification), UTF-8. {len(fc['features'])} features: each Tumor shape with "
                        "the Exclusion shapes cut out of it as holes, as the README defines Exclusion; a "
                        "self-intersecting outline is repaired to its polygonal parts. Import it in QuPath with "
                        "File > Import objects, over this image or the source TIFF. The ASAP shapes as drawn are "
                        "the sibling arrays."),
        "media_type": "application/geo+json",
        "decode": "json.loads(bytes(array[:]))",
        "coordinate_frame": {"image": "../image", "level": 0, "units": "pixel", "origin": "top-left corner of pixel (0, 0)",
                             "shift_from_asap": asap.QUPATH_SHIFT,
                             "shift_reason": ("ASAP's integer coordinate k is read as the center of pixel k, the reading "
                                              "the challenge's masks favor (coordinate_frame.pixel_convention on this "
                                              "group); QuPath's frame puts that center at k + 0.5.")},
    })
    return n


# One slide
#
# A slide is written whole in its own fork: the scan, the mask, the tissue map
# and the outlines, then the slide group's attributes, its row of the
# challenge's table. The row is written last, so a slide group with a row is
# complete, and a rerun skips exactly the slides that are.


def row_16(slide: str, ctx: dict, image: dict, mask: dict, tissue, copy_of_mask: bool, shapes) -> dict:
    r = ctx["reference"][slide]
    result = ctx["results"][slide]
    return {
        "slide": slide,
        "split": "test" if slide.startswith("test_") else "training",
        "type": r["type"], "class": r["class"], "center": r["center"],
        "scanner": image["scanner"],
        "annotation_exhaustive": slide not in ctx["partial"],
        "annotation_note": ctx["partial"].get(slide),
        "has_shapes": shapes is not None, "n_shapes": shapes,
        "has_macro": "macro" in image["extras"], "has_label": "label" in image["extras"],
        "has_tissue": tissue is not None,
        "tissue_note": ("Omitted: the tissue file has the same size and ETag as the mask file, so it is a "
                        "copy of the mask, not a segmentation.") if copy_of_mask else None,
        "tissue_segmentation": "; ".join(result["error_messages"]) or "ok",
        "mask_levels": mask["levels"], "mask_levels_derived": mask["derived"],
        "pixel_size_um": image["base_um"],
        "source_md5": ctx["sums"][f"images/{slide}.tif"],
    }


def row_17(slide: str, ctx: dict, image: dict, mask, shapes) -> dict:
    patient, node = slide.rsplit("_node_", 1)
    p = ctx["patients"][patient]
    return {
        "slide": slide,
        "patient": patient,
        "node": int(node),
        "split": p["split"],
        "stage": ctx["labels"].get(slide),
        "center": p["center"],
        "scanner": image["scanner"],
        "has_mask": mask is not None,
        "has_shapes": shapes is not None, "n_shapes": shapes,
        "has_macro": "macro" in image["extras"], "has_label": "label" in image["extras"],
        "mask_levels": mask["levels"] if mask else None,
        "mask_levels_derived": mask["derived"] if mask else None,
        "pixel_size_um": image["base_um"],
        "source_md5": ctx["sums"].get(f"images/{slide}.tif"),
        "note": ctx["notes"].get(slide),
    }


def ingest_slide(session, challenge: Challenge, slide: str, source: Source, ctx: dict, replace: bool) -> dict:
    """Write one slide whole into ``session``; its row of the challenge's table."""
    w = Writer(session)
    path = slide_path(challenge, slide)
    if replace and path in w.root:
        del w.root[path]
    sums = ctx["sums"]
    p = challenge.prefix
    image = write_image(w, challenge, slide, source, sums.get(f"images/{slide}.tif"))
    if challenge is C16:
        mask = write_mask(w, challenge, slide, source, image, sums[f"masks/{slide}_mask.tif"])
        w.group(f"{path}/image/labels", {"ome": labels_attrs(["mask"], version=challenge.version)})
        tissue_head = source.head(f"{p}background_tissue/{slide}_tissue.tif")
        mask_head = source.head(f"{p}masks/{slide}_mask.tif")
        copy_of_mask = tissue_head["size"] == mask_head["size"] and tissue_head["e_tag"] == mask_head["e_tag"]
        tissue = None if copy_of_mask else write_tissue(w, slide, source, image, ctx["results"][slide],
                                                        ctx["settings"])
        shapes = None
        if source.head(f"{p}annotations/{slide}.xml"):
            shapes = write_shapes(w, challenge, slide, source, image["base_um"], sums[f"annotations/{slide}.xml"])
        row = row_16(slide, ctx, image, mask, tissue, copy_of_mask, shapes)
        description = (f"CAMELYON16 slide {slide}: the scan, its reference mask, tissue map and outlines, and "
                       "the scanner's extra pages.")
    else:
        mask = None
        if slide in ctx["masks"]:
            mask = write_mask(w, challenge, slide, source, image, sums.get(f"masks/{slide}_mask.tif"))
            w.group(f"{path}/image/labels", {"ome": labels_attrs(["mask"], version=challenge.version)})
        shapes = None
        if slide in ctx["annotations"]:
            shapes = write_shapes(w, challenge, slide, source, image["base_um"], sums[f"annotations/{slide}.xml"])
        row = row_17(slide, ctx, image, mask, shapes)
        description = (f"CAMELYON17 slide {slide}: the scan, with its reference mask and outlines where the "
                       "challenge published them, and the scanner's overview photograph where the file has one.")
    w.group(path, {"description": description, **image_annotation(row)})
    return row


# The challenge's container and the root
#
# Each challenge's ``annotations`` attribute is the slide table (one column per
# field, one entry per slide, keyed by image path), and its ``source_files``
# keep the challenge's own text files verbatim, since the table's columns come
# from them. CAMELYON17's patients are groups of their own, each with its row
# of the patient table.
#
# CAMELYON16 was published as OME-Zarr 0.5 with a ``bioformats2raw.layout``
# container: the layout key on the group and an ``OME/series`` list of every
# image. OME-Zarr 0.6 keeps that layout only as transitional metadata and has
# no adopted replacement for a set of unrelated images (RFC-8, collections,
# is in review), so CAMELYON17, written as 0.6, is a plain group: a reader
# finds its images as the multiscale groups under it, and the slide table
# lists them. A container written with the layout loses it here.
#
# Both are written from what the session holds once the slides are merged,
# so a run that adds slides to a challenge rewrites its table whole.

CENTER_NAMES = Decision(
    field="center_names",
    wrote={"0": "Canisius-Wilhelmina Hospital (CWZ)", "1": "Rijnstate Hospital (RST)",
           "2": "University Medical Center Utrecht (UMCU)", "3": "Radboud University Medical Center (RUMC)",
           "4": "Laboratorium Pathologie Oost-Nederland (LPON)"},
    kind="interpretation",
    locus="stages.csv center column, numbers only",
    because=("stages.csv numbers the centers 0 to 4 and no file in the bucket names them. The slide-label counts "
             "per center in stages.csv equal the dataset paper's Table 4 (doi:10.1093/gigascience/giy065) "
             "exactly for CWZ (0), UMCU (2) and RUMC (3), and within three slides for RST (1) and LPON (4). The "
             "TIFFs agree: the paper says UMCU scanned on a Hamamatsu and LPON on a Philips scanner, and only "
             "center 2's slides have a Hamamatsu pixel size (0.226 um) and only center 4's are Philips TIFFs. "
             "The README lists the centers in another order."),
)


def slide_groups(root: zarr.Group, challenge: Challenge) -> dict[str, dict]:
    """``{image path: row}`` for every complete slide in the challenge's container, in slide order."""
    container = root[challenge.name]
    rows = {}
    if challenge is C16:
        candidates = [(name, container[name]) for name in sorted(container.group_keys())]
    else:
        candidates = [(f"{p}/{n}", container[p][n]) for p in sorted(container.group_keys()) if p.startswith("patient_")
                      for n in sorted(container[p].group_keys())]
    for path, group in candidates:
        row = dict(group.attrs).get("annotations")
        if row is not None and "image" in group:
            rows[f"{path}/image"] = row
    return rows


def finish(w: Writer, challenge: Challenge, source: Source, ctx: dict) -> int:
    """The container's table and source files, CAMELYON16's series list, and the root's attributes."""
    rows = slide_groups(w.root, challenge)
    sources = {key: w.text(challenge.name, key.replace("/", "--"), source.read(challenge.prefix + key).decode(),
                           of=f"{challenge.name}/{key}", fmt="UTF-8 text")
               for key in challenge.source_files}
    if challenge is C16:
        series = []
        for image, row in rows.items():
            slide = image.removesuffix("/image")
            series += [image] + [f"{slide}/{x}" for x in ("macro", "label") if row[f"has_{x}"]]
            series += [f"{slide}/tissue"] if row.get("has_tissue") else []
        w.group(f"{challenge.name}/OME", series_attrs(series, version=challenge.version))
        attrs = {
            **root_attrs(version=challenge.version),
            "description": ("CAMELYON16: 399 H&E whole-slide images of sentinel lymph nodes from Radboud UMC and "
                            "UMC Utrecht, with tumor masks, tissue maps and pathologists' outlines. This store holds "
                            f"{len(rows)} of them. Each slide is a group <slide>/ with image/ (the scan, and "
                            "labels/mask), tissue/, macro/ or label/ where the file has them, and shapes/ where "
                            "an outline XML exists. OME/series lists every image; annotations is the slide table."),
            **annotation_table(rows),
            "source_files": sources,
            "source_files_note": "The challenge's own files, verbatim: the slide table's columns come from them.",
        }
    else:
        held = {row["patient"] for row in rows.values()}
        patients = [p for name, p in sorted(ctx["patients"].items()) if name in held]
        drop_bioformats2raw(w.root[challenge.name])
        attrs = {
            "description": (
                "CAMELYON17: 1,000 H&E whole-slide images of sentinel lymph nodes, five per patient from 200 "
                f"breast cancer patients at five Dutch medical centers. This store holds {len(rows)} of them, "
                f"from {len(patients)} patients. Each patient is a group <patient>/ holding one group per lymph "
                "node, <patient>/<node>/, with image/ (the scan, and labels/mask for the 100 slides with a "
                "mask), macro/ where the file has one, and shapes/ for the 50 slides with outlines. Every "
                "OME-Zarr 0.6 multiscale group below this one is an image. annotations is the slide table, "
                "keyed by image path, and patients the patient table, both from stages.csv, which gives "
                "labels and centers for the 100 training patients only."),
            **annotation_table(rows),
            "patients": {
                "group": [p["patient"] for p in patients],
                **{col: [p[col] for p in patients] for col in ("split", "stage", "center", "slides")},
            },
            "columns": {
                "stage": ("stages.csv, verbatim: a slide's metastasis category (negative, itc, micro, macro) or a "
                          "patient's pN stage. Null for the test patients and their slides, which the bucket "
                          "does not label."),
                "center": "stages.csv, verbatim: the patient's medical center, 0 to 4; null for test patients.",
                "split": "training for the patients stages.csv lists, test for the others (README.md).",
                "note": "the README's notes about this slide.",
            },
            CENTER_NAMES.field: CENTER_NAMES.wrote,
            CENTER_NAMES.key: CENTER_NAMES.prose(),
            "citation": f"{CITATION_17} Dataset: {CITATION_DATASET}",
            "source_files": sources,
            "source_files_note": ("The challenge's own files, verbatim: the tables' columns come from them. "
                                  "evaluation/example.csv is a sample submission, not labels."),
        }
    w.group(challenge.name, attrs)
    w.root.attrs.update(root_attributes(sorted(set(w.root.group_keys()) & set(CHALLENGES))))
    return len(rows)


def drop_bioformats2raw(container: zarr.Group) -> bool:
    """Remove the ``bioformats2raw.layout`` container from ``container``; whether anything changed.

    Deletes the ``OME`` group and the ``bioformats2raw.layout`` key, and the
    ``ome`` attribute itself when nothing but its ``version`` is left, since
    a group holding only a version is no OME-Zarr structure.
    """
    changed = False
    if "OME" in container:
        del container["OME"]
        changed = True
    attrs = dict(container.attrs)
    ome = attrs.get("ome")
    if isinstance(ome, dict) and "bioformats2raw.layout" in ome:
        rest = {k: v for k, v in ome.items() if k != "bioformats2raw.layout"}
        if set(rest) <= {"version"}:
            attrs.pop("ome")
        else:
            attrs["ome"] = rest
        container.attrs.put(attrs)
        changed = True
    return changed


def root_attributes(present: list[str]) -> dict:
    """The store's own description, for the challenges it holds."""
    requires = {
        "slides": ("Slide, macro and label tiles decode with imagecodecs' own Zarr 3 codec imagecodecs_jpeg "
                   "(the imagecodecs and imagecodecs-zarr packages)."),
        "masks": "Mask and tissue tiles decode with imagecodecs_lzw (the same two packages).",
        "everything else": "bytes, transpose, zstd and vlen-utf8 only.",
    }
    if present == ["CAMELYON16"]:
        return {
            "title": "CAMELYON16 lymph node metastases, virtualized",
            "description": ("Whole-slide images from the CAMELYON16 challenge (detection of breast cancer metastases "
                            "in lymph node sections), as OME-Zarr 0.5 over the challenge's own TIFF bytes in "
                            "s3://camelyon-dataset, with each slide's mask, tissue map and tumor outlines beside it. "
                            "The images are under CAMELYON16/."),
            "license": "CC0-1.0",
            "license_text": "CAMELYON16/ source_files['license.txt']",
            "citation": CITATION_16,
            "source": {"bucket": SOURCE, "prefix": "CAMELYON16/", "region": "us-west-2",
                       "access": "anonymous (public bucket)", "landing_page": C16.landing_page},
            "requires": requires,
            "text_limit_bytes": 64 * 1024,
        }
    return {
        "title": "CAMELYON16 and CAMELYON17 lymph node metastases, virtualized",
        "description": ("Whole-slide images from the CAMELYON16 and CAMELYON17 challenges (detection of breast "
                        "cancer metastases in sentinel lymph node sections, and patient-level pN staging), over the "
                        "challenges' own TIFF bytes in s3://camelyon-dataset, with each slide's mask and the "
                        "pathologists' tumor outlines beside it. CAMELYON16/ is OME-Zarr 0.5 and CAMELYON17/ "
                        "OME-Zarr 0.6; each container's description says how its slides are laid out."),
        "license": "CC0-1.0",
        "license_text": "source_files['license.txt'] of each challenge group",
        "citation": f"CAMELYON16: {CITATION_16} CAMELYON17: {CITATION_17} Dataset: {CITATION_DATASET}",
        "citations": {"CAMELYON16": CITATION_16, "CAMELYON17": CITATION_17, "dataset": CITATION_DATASET},
        "source": {"bucket": SOURCE, "prefixes": [f"{name}/" for name in present], "region": "us-west-2",
                   "access": "anonymous (public bucket)",
                   "landing_pages": {name: CHALLENGES[name].landing_page for name in present}},
        "requires": requires,
        "text_limit_bytes": 64 * 1024,
    }


# The run
#
# The container group and, for CAMELYON17, the patient groups are committed
# before any slide is written, because two forks that both create a group
# cannot be merged. Then the slides not yet in the store are written in
# batches: each slide in its own fork, the batch's forks merged into one
# session, the container and root described from what that session holds,
# and the session committed.


def prepare(repo, challenge: Challenge, slides: list[str], ctx: dict) -> None:
    """Commit the container and any patient groups the slides need, if missing."""
    w = Writer(repo.writable_session("main"))
    if challenge.name not in w.root:
        w.group(challenge.name)
    if challenge is C17:
        container = w.root[challenge.name]
        for patient in sorted({s.rsplit("_node_", 1)[0] for s in slides}):
            if patient not in container:
                p = ctx["patients"][patient]
                w.group(f"{challenge.name}/{patient}", {
                    "description": (f"CAMELYON17 patient {patient}: one group per lymph node slide. The "
                                    "pN stage is the patient's label, from stages.csv."),
                    **image_annotation(dict(p)),
                })
    if w.session.has_uncommitted_changes:
        what = "and its patient groups" if challenge is C17 else "group"
        say(f"prepared: {commit(w.session, f'Add the {challenge.name} container {what}')}")


def present(root: zarr.Group, challenge: Challenge, slide: str) -> bool:
    path = slide_path(challenge, slide)
    return path in root and dict(root[path].attrs).get("annotations") is not None


#: Slides per commit. A fork holds its slide's chunk references in memory
#: until the commit, about 50 MB for a CAMELYON17 slide, so a batch of 100
#: needs about 5 GB, where all 1,000 at once would need 50 GB.
BATCH = 100


def run(location: str, challenge: Challenge, requested: list[str] | None, source: Source, *,
        threads: int, replace: set[str], batch: int = BATCH) -> str | None:
    """Add the slides not yet in the store, ``batch`` at a time, one commit per batch; the last snapshot.

    Each commit also rewrites the container's table and series list, so every
    snapshot describes exactly the slides it holds. A failed slide stops the
    run before its batch is committed; the batches before it stay, and a
    rerun skips them.
    """
    started = time.monotonic()
    ctx = CONTEXTS[challenge.name](source)
    slides = requested or ctx["slides"]
    if unknown := sorted(set(slides) - set(ctx["slides"])):
        raise SystemExit(f"not in {challenge.name}: {unknown}")
    say(f"{challenge.name}: {len(slides)} slides; the challenge's tables read in {time.monotonic() - started:.0f} s")
    repo = open_repository(location, source)
    prepare(repo, challenge, slides, ctx)
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    todo = [s for s in slides if s in replace or not present(root, challenge, s)]
    say(f"{len(slides) - len(todo)} already in the store; {len(todo)} to write, {batch} per commit, "
        f"on {threads} threads")
    snapshot = None
    for first in range(0, len(todo), batch):
        part = todo[first:first + batch]
        session = repo.writable_session("main")
        say(f"batch {first // batch + 1} of {-(-len(todo) // batch)}: {part[0]} to {part[-1]}")
        done, failed = in_forks(session, part, lambda fork, s: ingest_slide(fork, challenge, s, source, ctx,
                                                                            s in replace), threads=threads)
        for slide, why in sorted(failed.items()):
            say(f"FAILED {slide}: {why}")
        if failed:
            raise SystemExit(f"{len(failed)} slides failed; this batch is not committed")
        merging = time.monotonic()
        stage = ["merging the forks"]
        with Heartbeat(lambda: f"batch {first // batch + 1}: {stage[0]}, {time.monotonic() - merging:.0f} s"):
            session.merge(*(fork for fork, _ in done.values()))
            del done
            stage[0] = "describing the container"
            held = finish(Writer(session), challenge, source, ctx)
            say(f"merged {len(part)} forks and described {held} slides in {time.monotonic() - merging:.0f} s")
            stage[0] = "committing"
            added = [s for s in part if s not in replace]
            rebuilt = [s for s in part if s in replace]
            parts = [f"add {len(added)} slide{'s' * (len(added) != 1)} ({added[0]} to {added[-1]})" if added else "",
                     f"rebuild {', '.join(rebuilt)}" if rebuilt else ""]
            message = f"{challenge.name}: {' and '.join(p for p in parts if p)}"
            committing = time.monotonic()
            snapshot = commit(session, message)
        say(f"committed {snapshot} in {time.monotonic() - committing:.0f} s; "
            f"{time.monotonic() - started:.0f} s since the start")
    return snapshot


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="a local directory or arraylake://org/repo; an existing repo is added to")
    ap.add_argument("--challenge", choices=sorted(CHALLENGES), required=True)
    ap.add_argument("--slides", help="comma-separated slide names, such as tumor_056 or patient_004_node_4; default all")
    ap.add_argument("--replace", default="", help="comma-separated slides to rebuild even if already present")
    ap.add_argument("--source", default=SOURCE, help="the bucket, or a local directory laid out like it")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--batch", type=int, default=BATCH, help="slides per commit")
    args = ap.parse_args(argv)
    replace = set(filter(None, args.replace.split(",")))
    requested = args.slides.split(",") if args.slides else None
    run(args.repo, CHALLENGES[args.challenge], requested, Source(args.source), threads=args.threads,
        replace=replace, batch=args.batch)


if __name__ == "__main__":
    main()
