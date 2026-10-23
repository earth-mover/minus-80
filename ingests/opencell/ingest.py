"""Ingest OpenCell's raw images into virtual OME-Zarr plates in Icechunk.

OpenCell imaged its cell lines live in 96-well plates, one cell line per well
and a few fields of view per well. The store follows that: an HCS plate per
imaging plate, a well per cell line, and a field image per field of view,
referencing the bucket's TIFF strips without copying or decoding them.

- ``plates/<plate>/<row>/<column>/FID<fid>``: the field's live-cell z-stack,
  (c, z, y, x), with the maximum over z that OpenCell published beside it
  nested as ``proj``, (c, y, x);
- ``projections/<plate>/<row>/<column>/FID<fid>``: the same projections as a
  plate of their own, for browsing, referencing the same bytes;
- ``unlisted/<GENE>/CID<cid>/FID<fid>``: the fields of the 86 cell lines the
  OpenCell website does not list, as a ``bioformats2raw.layout`` collection.

The bucket's paths name only the gene, cell line and field of view. The
imaging plate, the well and each field's acquisition settings come from a
snapshot of the OpenCell website's JSON API, the only place they are
published; the unlisted lines have no such record, so no plate or well.
LAYOUTS.md records how the bucket maps onto this tree, and PLAN.md the
choices behind it.

    MINUS80_S3_ANONYMOUS=1 uv run --package opencell-ingest python ingests/opencell/ingest.py \\
        --api <snapshot dir> --repo <icechunk dir> [--genes AAMP,BAG6]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import obstore
import zarr
from minus80.core import registry_for
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, commit, create_repo
from minus80.core.manifest import permute_dims
from minus80.core.progress import Progress
from minus80.core.registry import s3_region
from minus80.ome import (annotation_table, channel_omero, image_annotation, image_attrs,
                         plate_attrs, plate_grid, root_attrs, series_attrs, well_attrs)
from minus80.ome.hierarchy import DERIVED
from minus80.parsers.tiff import TiffParser
from obstore.store import S3Store
from virtualizarr.manifests import ManifestGroup

BUCKET = "czb-opencell"
PREFIX = f"s3://{BUCKET}/microscopy/raw/"

NAME = re.compile(
    r"^(?P<gene>[^/]+)_(?P<ensg>ENSG\d+)/OC-FOV_(?P=gene)_(?P=ensg)"
    r"_CID(?P<cid>\d+)_FID(?P<fid>\d+)_(?P<kind>stack|proj)\.tif$"
)

#: The well and the microscope's position number within it, from the original
#: Micro-Manager filename: ``.../B12_14_AAMP.ome.tif`` on the earlier plates,
#: ``.../MMStack_451-B12-19.ome.tif`` on the later ones.
SOURCE_WELL = re.compile(r"(?:/|MMStack_\d+-)(?P<row>[A-H])(?P<column>\d{1,2})[-_](?P<position>\d+)")

PLATES, PROJECTIONS, UNLISTED = "plates", "projections", "unlisted"
#: The name of each field image's nested projection.
PROJ = "proj"
ORDER = {"stack": ("c", "z", "y", "x"), "proj": ("c", "y", "x")}

#: The sample's pixel size in µm: the Methods' camera pixel, 13 µm (Andor iXon
#: Ultra 888), through the 63x objective. The TIFFs state 0.2 µm, a rounded
#: value, which stays in each image's ``tiff`` attributes (LAYOUTS.md).
PIXEL_UM = 13 / 63
PIXEL_SOURCE = ("13 µm camera pixel (Andor iXon Ultra 888) / 63x objective, from "
                "Cho et al. 2022, Materials and Methods, Live-cell fluorescence microscopy; "
                "the TIFFs state 0.2 µm (XResolution 5/1, unit um)")
PROJ_DESCRIPTION = ("maximum intensity over z of each channel, as published by OpenCell "
                    "in the bucket's _proj.tif")

#: Channel order and stains as the OpenCell download page states them; the
#: TIFFs name neither.
CHANNELS = [
    {"label": "Hoechst 33342", "color": "0000FF"},
    {"label": "mNeonGreen", "color": "00FF00"},
]

#: Per-field fields copied from the API's ``metadata`` record, under its own names.
FOV_FIELDS = ("z_step_size", "exposure_time_488", "laser_power_488", "max_intensity_488",
              "cell_layer_center", "score", "src_filename")
#: Per-cell-line fields from ``/api/lines``. ``plate_id`` and ``well_id`` there
#: are where the line was made, not where it was imaged.
LINE_FIELDS = ("ensg_id", "enst_id", "plate_id", "well_id", "protospacer_sequence",
               "sort_count", "target_family", "target_name", "target_terminus")

DATASET = {
    "title": "OpenCell: live-cell images of 1,310 endogenously tagged human proteins",
    "description": (
        "Spinning-disk confocal z-stacks of HEK293T cell lines, each with one protein "
        "tagged at its endogenous locus with split mNeonGreen2, and nuclei stained with "
        "Hoechst 33342. One OME-Zarr HCS plate per imaging plate, one well per cell line, "
        "one field image per field of view. z-stacks/ holds the stacks and projections/ "
        "the maximum over z that OpenCell published for each."),
    "citation": "Cho et al. 2022, OpenCell: Endogenous tagging for the cartography of "
                "human cellular organization. Science 375, eabi6983",
    "doi": "10.1126/science.abi6983",
    "license": "CC-BY-SA-4.0",
    "license_source": "https://registry.opendata.aws/czb-opencell/",
    "source": PREFIX,
    "website": "https://opencell.sf.czbiohub.org",
    "channels_source": "https://opencell.sf.czbiohub.org/download",
    "metadata_source": "OpenCell API /api/lines and /api/lines/<cid>/fovs, 2026-10-23",
    "pixel_size_um": {"y": PIXEL_UM, "x": PIXEL_UM, "source": PIXEL_SOURCE},
    "trees": {
        PLATES: "one OME-Zarr HCS plate per imaging plate; each field image is the z-stack, "
                "with its projection nested as proj",
        PROJECTIONS: "the same plates holding only the projections, for browsing; "
                     "they reference the same bytes as plates/.../proj",
        UNLISTED: "the fields of view of the 86 cell lines that are in the bucket but that the "
                  "OpenCell website does not list; no plate, well or acquisition record exists "
                  "for them, so they are grouped by gene and cell line",
    },
    "related_accessions": {
        "PRIDE PXD024909": "IP-MS interactome raw data",
        "PRIDE PXD029191": "whole-cell proteome, protein abundance",
        "GEO GSE186192": "HEK293T bulk RNA-seq, RNA abundance",
    },
}


@dataclass(frozen=True)
class Fov:
    gene: str
    path_ensg: str
    cid: int
    fid: int

    def url(self, kind: str) -> str:
        return (f"{PREFIX}{self.gene}_{self.path_ensg}/OC-FOV_{self.gene}_{self.path_ensg}"
                f"_CID{self.cid:06d}_FID{self.fid:08d}_{kind}.tif")


@dataclass(frozen=True)
class Place:
    """Where a field of view was imaged, from its API record."""
    plate: str
    row: str
    column: str
    position: int

    @property
    def well(self) -> str:
        return f"{self.plate}/{self.row}/{self.column}"


def list_bucket() -> dict[Fov, set[str]]:
    """Every field of view in the bucket, with the kinds of file it has."""
    store = S3Store(BUCKET, region=s3_region(BUCKET), skip_signature=True)
    found: dict[Fov, set[str]] = {}
    unmatched = []
    for batch in obstore.list(store, prefix="microscopy/raw/"):
        for obj in batch:
            key = obj["path"].removeprefix("microscopy/raw/")
            if not (m := NAME.match(key)):
                unmatched.append(key)
                continue
            fov = Fov(m["gene"], m["ensg"], int(m["cid"]), int(m["fid"]))
            found.setdefault(fov, set()).add(m["kind"])
    if unmatched:
        raise ValueError(f"{len(unmatched)} keys under {PREFIX} don't follow the naming "
                         f"pattern, e.g. {unmatched[:3]}")
    return found


def load_api(snapshot: Path) -> tuple[dict[int, dict], dict[int, dict]]:
    """Cell lines and fields of view from a snapshot of the OpenCell API.

    ``lines.json`` is ``/api/lines?publication_ready=true``; ``fovs.json``
    maps each cell line ID to ``/api/lines/<cid>/fovs``, or to the error the
    API answered with: it answers 404 for the 86 unpublished lines.
    """
    lines = {rec["metadata"]["cell_line_id"]: rec
             for rec in json.loads((snapshot / "lines.json").read_text())}
    fovs = {}
    for records in json.loads((snapshot / "fovs.json").read_text()).values():
        if isinstance(records, dict):
            continue
        for rec in records:
            fovs[rec["metadata"]["id"]] = rec
    return lines, fovs


def place(record: dict) -> Place:
    meta = record["metadata"]
    if not (m := SOURCE_WELL.search(meta["src_filename"])):
        raise ValueError(f"FID {meta['id']}: no well in {meta['src_filename']!r}")
    return Place(meta["pml_id"], m["row"], str(int(m["column"])), int(m["position"]))


def open_target(location: str):
    """The repo to write: an existing Arraylake repo, or a new local or S3 one."""
    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location, source_prefixes=[PREFIX])
    return create_repo(location, [], prefixes=[PREFIX])


def open_readonly(location: str):
    """A read-only session on the repo ``location`` names, its virtual chunks authorized."""
    from minus80.core.icechunk_io import open_repository

    repo = (arraylake_repository(location) if location.startswith(ARRAYLAKE)
            else open_repository(location))
    return repo.readonly_session("main")


def parse_all(urls: list[str], workers: int) -> dict[str, ManifestGroup]:
    """Parse every file once, in parallel. Raises after the run if any file failed."""
    parser = TiffParser()
    parsed, failed = {}, {}
    with Progress("parsing TIFFs", len(urls), unit="files") as progress, \
            ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(parser, u, registry_for(u, patient=True)): u for u in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                parsed[url] = future.result().group
            except Exception as exc:  # noqa: BLE001 -- recorded and raised below
                failed[url] = f"{type(exc).__name__}: {exc}"
            progress.advance()
    if failed:
        for url, why in sorted(failed.items()):
            print(f"  failed {url}: {why}", file=sys.stderr)
        raise RuntimeError(f"{len(failed)} of {len(urls)} files failed to parse")
    return parsed


def image_group(parsed: ManifestGroup, *, name: str, order: tuple[str, ...],
                omero: dict, annotations: dict | None = None,
                derived: dict | None = None) -> ManifestGroup:
    """One OME-Zarr image from a parsed TIFF, its axes in OME-Zarr order.

    y and x take :data:`PIXEL_UM`; the parser's group attributes (the TIFF's
    series, resolution tags and ImageJ metadata) stay on the image under
    ``tiff``, as the file states them. ``derived`` names the image subgroups
    derived from this one (``minus80.ome.hierarchy.DERIVED``).
    """
    array = permute_dims(parsed["0"], order)
    attrs = array.metadata.attributes
    attrs["scale"] = attrs["scale"] | {"y": PIXEL_UM, "x": PIXEL_UM}
    # The channel axis is a list of labels; its scale entry exists only
    # because the spec gives every axis one.
    ome = image_attrs(name=name, dims=order, level_shapes=[array.shape],
                      scale={"c": 1.0} | attrs["scale"],
                      units=attrs["units"], dimensions=attrs["dimensions"], omero=omero,
                      description="the TIFF's single full-resolution level")
    return ManifestGroup(arrays={"0": array},
                         attributes={"ome": ome, "tiff": dict(parsed.metadata.attributes)}
                         | (annotations or {}) | ({DERIVED: derived} if derived else {}))


def field_name(fov: Fov) -> str:
    return f"FID{fov.fid:08d}"


def fov_fields(fov: Fov, record: dict, where: Place) -> dict:
    meta, annotation = record["metadata"], record.get("annotation") or {}
    return ({"gene": fov.gene, "cell_line_id": fov.cid, "fov_id": fov.fid,
             "position_in_well": where.position}
            | {k: meta.get(k) for k in FOV_FIELDS}
            | {"roi_position_left": annotation.get("roi_position_left"),
               "roi_position_top": annotation.get("roi_position_top")})


def path_fields(fov: Fov) -> dict:
    """What a bucket path alone states, for a field the API has no record of."""
    return {"gene": fov.gene, "ensg_id_in_path": fov.path_ensg, "cell_line_id": fov.cid,
            "fov_id": fov.fid, "listed_by_opencell": False}


def well_fields(fov: Fov, line: dict) -> dict:
    """What the API says about how the well's cell line was made.

    The line's localization grades are a result of the study, like the
    site's tables, and wait with them for a licence (PLAN.md).
    """
    meta = line["metadata"]
    return {"gene": fov.gene, "cell_line_id": fov.cid, "ensg_id": meta["ensg_id"],
            "ensg_id_in_path": fov.path_ensg,
            "cell_line": {k: meta.get(k) for k in LINE_FIELDS}}


def write_field(store, path: str, fov: Fov, parsed: dict, fields: dict, omero: dict) -> None:
    """The field's stack at ``path`` and its projection nested inside it."""
    stack, proj = fov.url("stack"), fov.url("proj")
    image_group(parsed[stack], name=f"{fov.gene} {field_name(fov)}", order=ORDER["stack"],
                omero=omero, annotations=image_annotation(fields, source_url=stack),
                derived={PROJ: PROJ_DESCRIPTION}).to_icechunk(store, group=path)
    image_group(parsed[proj], name=f"{fov.gene} {field_name(fov)} projection", order=ORDER["proj"],
                omero=omero, annotations=image_annotation(fields, source_url=proj)
                ).to_icechunk(store, group=f"{path}/{PROJ}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", type=Path, required=True,
                    help="directory holding lines.json and fovs.json from the OpenCell API")
    ap.add_argument("--repo", required=True,
                    help="Icechunk repo: a directory, an s3:// URL, or arraylake://<org>/<repo>")
    ap.add_argument("--genes", help="comma-separated gene symbols; default every gene")
    ap.add_argument("--workers", type=int, default=32)
    args = ap.parse_args(argv)

    lines, fov_records = load_api(args.api)
    bucket = list_bucket()
    if args.genes:
        wanted = set(args.genes.split(","))
        if missing := wanted - {f.gene for f in bucket}:
            raise SystemExit(f"not in the bucket: {sorted(missing)}")
        bucket = {f: k for f, k in bucket.items() if f.gene in wanted}
    for fov, have in bucket.items():
        if lacking := {"stack", "proj"} - have:
            raise ValueError(f"FID {fov.fid} has no {sorted(lacking)} file")
    unlisted = sorted((f for f in bucket if f.cid not in lines), key=lambda f: (f.gene, f.cid, f.fid))
    fovs = {}
    for fov in bucket:
        if fov.cid not in lines:
            continue
        if fov.fid not in fov_records:
            raise ValueError(f"FID {fov.fid} of listed cell line {fov.cid} has no API record")
        fovs[fov] = place(fov_records[fov.fid])
    wells: dict[str, list[Fov]] = {}
    for fov, where in sorted(fovs.items(), key=lambda kv: (kv[1].well, kv[0].fid)):
        wells.setdefault(where.well, []).append(fov)
    for well, members in wells.items():
        if len({f.cid for f in members}) > 1:
            raise ValueError(f"{well} holds more than one cell line: {sorted({f.cid for f in members})}")
    plates = sorted({w.split("/")[0] for w in wells})
    print(f"{len(fovs)} fields of view in {len(wells)} wells on {len(plates)} plates; "
          f"{len(unlisted)} fields of {len({f.cid for f in unlisted})} unlisted cell lines")

    # Opened before parsing, so a repo that can't be written fails in seconds.
    repo = open_target(args.repo)
    session = repo.writable_session("main")
    store = session.store
    root = zarr.open_group(store, mode="a")
    if list(root.keys()):
        raise SystemExit(f"{args.repo} already holds {sorted(root.keys())}; "
                         f"the ingest writes a fresh store")

    started = time.monotonic()
    parsed = parse_all([f.url(k) for f in [*fovs, *unlisted] for k in ("stack", "proj")],
                       args.workers)
    print(f"parsed in {time.monotonic() - started:.1f} s")

    omero = channel_omero(CHANNELS, max_value=65535)
    rows, columns = plate_grid(96)
    root.attrs.update(
        {"annotations": DATASET}
        | {"wells": annotation_table({
            w: {"gene": m[0].gene, "cell_line_id": m[0].cid,
                "ensg_id": lines[m[0].cid]["metadata"]["ensg_id"], "fields": len(m)}
            for w, m in wells.items()})["annotations"]})

    started = time.monotonic()
    with Progress("writing fields of view", len(fovs) + len(unlisted),
                  unit="fields of view") as progress:
        for tree in (PLATES, PROJECTIONS):
            root.require_group(tree).attrs.update({"plates": plates})
        for plate in plates:
            in_plate = {w: m for w, m in wells.items() if w.startswith(f"{plate}/")}
            document = plate_attrs(name=plate, rows=rows, columns=columns,
                                   wells=[tuple(w.split("/")[1:]) for w in in_plate],
                                   field_count=max(len(m) for m in in_plate.values()))
            for tree in (PLATES, PROJECTIONS):
                root.create_group(f"{tree}/{plate}").attrs.update(document)
            for well, members in in_plate.items():
                attrs = (well_attrs([field_name(f) for f in members])
                         | image_annotation(well_fields(members[0], lines[members[0].cid])))
                for tree in (PLATES, PROJECTIONS):
                    root.create_group(f"{tree}/{well}").attrs.update(attrs)
                for fov in members:
                    fields = fov_fields(fov, fov_records[fov.fid], fovs[fov])
                    write_field(store, f"{PLATES}/{well}/{field_name(fov)}", fov, parsed,
                                fields, omero)
                    proj = fov.url("proj")
                    image_group(parsed[proj], name=f"{fov.gene} {field_name(fov)} projection",
                                order=ORDER["proj"], omero=omero,
                                annotations=image_annotation(fields, source_url=proj)
                                ).to_icechunk(store, group=f"{PROJECTIONS}/{well}/{field_name(fov)}")
                    progress.advance()
        collection = root.create_group(UNLISTED)
        collection.attrs.update(root_attrs() | image_annotation({
            "description": DATASET["trees"][UNLISTED],
            "cell_lines": sorted({f.cid for f in unlisted})}))
        series = []
        for fov in unlisted:
            path = f"{fov.gene}/CID{fov.cid:06d}/{field_name(fov)}"
            write_field(store, f"{UNLISTED}/{path}", fov, parsed, path_fields(fov), omero)
            series.append(path)
            progress.advance()
        collection.create_group("OME").attrs.update(series_attrs(series))
    written = time.monotonic() - started
    started = time.monotonic()
    snapshot = commit(session, f"OpenCell: {len(fovs)} fields of view on {len(plates)} plates, "
                               f"{len(unlisted)} unlisted")
    print(f"wrote {len(fovs) + len(unlisted)} fields in {written:.1f} s, "
          f"committed {snapshot} in {time.monotonic() - started:.1f} s")


if __name__ == "__main__":
    main()
