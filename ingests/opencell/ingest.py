"""Ingest OpenCell's raw images into virtual OME-Zarr plates in Icechunk.

OpenCell imaged its cell lines live in 96-well plates, one cell line per well
and a few fields of view per well. The store follows that: an HCS plate per
imaging plate, a well per cell line, and a field image per field of view,
referencing the bucket's TIFF strips without copying or decoding them.

Two plate trees share those paths:

- ``z-stacks/<plate>/<row>/<column>/<field>``: the live-cell z-stack, (c, z, y, x);
- ``projections/<plate>/<row>/<column>/<field>``: the maximum over z that
  OpenCell published beside each stack, (c, y, x).

The bucket's paths name only the gene, cell line and field of view. The
imaging plate, the well and each field's acquisition settings come from a
snapshot of the OpenCell site's JSON API, the only place they are published,
so the 86 cell lines the API does not publish have no plate to go in and are
left out. LAYOUTS.md records how the bucket maps onto this tree, and PLAN.md
the choices behind it.

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
from minus80.core.icechunk_io import commit, create_repo
from minus80.core.manifest import permute_dims
from minus80.core.progress import Progress
from minus80.core.registry import s3_region
from minus80.ome import (annotation_table, channel_omero, image_annotation, image_attrs,
                         plate_attrs, plate_grid, well_attrs)
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

TREES = {"stack": "z-stacks", "proj": "projections"}
ORDER = {"stack": ("c", "z", "y", "x"), "proj": ("c", "y", "x")}

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
                omero: dict, annotations: dict | None = None) -> ManifestGroup:
    """One OME-Zarr image from a parsed TIFF, its axes in OME-Zarr order.

    The parser's group attributes (the TIFF's series, resolution tags and
    ImageJ metadata) stay on the image under ``tiff``, as the file states them.
    """
    array = permute_dims(parsed["0"], order)
    attrs = array.metadata.attributes
    ome = image_attrs(name=name, dims=order, level_shapes=[array.shape],
                      scale={d: attrs["scale"].get(d, 1.0) for d in order},
                      units=attrs["units"], dimensions=attrs["dimensions"], omero=omero,
                      description="the TIFF's single full-resolution level")
    return ManifestGroup(arrays={"0": array},
                         attributes={"ome": ome, "tiff": dict(parsed.metadata.attributes)}
                         | (annotations or {}))


def field_name(fov: Fov) -> str:
    return f"FID{fov.fid:08d}"


def fov_fields(fov: Fov, record: dict, where: Place) -> dict:
    meta, annotation = record["metadata"], record.get("annotation") or {}
    return ({"gene": fov.gene, "cell_line_id": fov.cid, "fov_id": fov.fid,
             "position_in_well": where.position}
            | {k: meta.get(k) for k in FOV_FIELDS}
            | {"roi_position_left": annotation.get("roi_position_left"),
               "roi_position_top": annotation.get("roi_position_top")})


def well_fields(fov: Fov, line: dict) -> dict:
    """What the API says about how the well's cell line was made.

    The line's localization grades are a result of the study, like the
    site's tables, and wait with them for a licence (PLAN.md).
    """
    meta = line["metadata"]
    return {"gene": fov.gene, "cell_line_id": fov.cid, "ensg_id": meta["ensg_id"],
            "ensg_id_in_path": fov.path_ensg,
            "cell_line": {k: meta.get(k) for k in LINE_FIELDS}}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", type=Path, required=True,
                    help="directory holding lines.json and fovs.json from the OpenCell API")
    ap.add_argument("--repo", required=True, help="Icechunk repo, a directory or s3:// URL")
    ap.add_argument("--genes", help="comma-separated gene symbols; default every gene")
    ap.add_argument("--no-proj", action="store_true", help="leave out the projections")
    ap.add_argument("--workers", type=int, default=32)
    args = ap.parse_args(argv)

    lines, fov_records = load_api(args.api)
    bucket = list_bucket()
    if args.genes:
        wanted = set(args.genes.split(","))
        if missing := wanted - {f.gene for f in bucket}:
            raise SystemExit(f"not in the bucket: {sorted(missing)}")
        bucket = {f: k for f, k in bucket.items() if f.gene in wanted}
    unpublished = [f for f in bucket if f.cid not in lines]
    if unpublished:
        print(f"leaving out {len(unpublished)} fields of view of "
              f"{len({f.cid for f in unpublished})} unpublished cell lines")
    kinds = ["stack"] if args.no_proj else ["stack", "proj"]
    fovs = {}
    for fov, have in bucket.items():
        if fov.cid not in lines:
            continue
        if lacking := set(kinds) - have:
            raise ValueError(f"FID {fov.fid} has no {sorted(lacking)} file")
        if fov.fid not in fov_records:
            raise ValueError(f"FID {fov.fid} of published cell line {fov.cid} has no API record")
        fovs[fov] = place(fov_records[fov.fid])
    wells: dict[str, list[Fov]] = {}
    for fov, where in sorted(fovs.items(), key=lambda kv: (kv[1].well, kv[0].fid)):
        wells.setdefault(where.well, []).append(fov)
    for well, members in wells.items():
        if len({f.cid for f in members}) > 1:
            raise ValueError(f"{well} holds more than one cell line: {sorted({f.cid for f in members})}")
    plates = sorted({w.split("/")[0] for w in wells})
    print(f"{len(fovs)} fields of view in {len(wells)} wells on {len(plates)} plates")

    started = time.monotonic()
    parsed = parse_all([f.url(k) for f in fovs for k in kinds], args.workers)
    print(f"parsed in {time.monotonic() - started:.1f} s")

    omero = channel_omero(CHANNELS, max_value=65535)
    rows, columns = plate_grid(96)
    repo = create_repo(args.repo, [], prefixes=[PREFIX])
    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    root.attrs.update(
        {"annotations": DATASET}
        | {"wells": annotation_table({
            w: {"gene": m[0].gene, "cell_line_id": m[0].cid,
                "ensg_id": lines[m[0].cid]["metadata"]["ensg_id"], "fields": len(m)}
            for w, m in wells.items()})["annotations"]})

    started = time.monotonic()
    with Progress("writing fields of view", len(fovs), unit="fields of view") as progress:
        for kind in kinds:
            tree = root.require_group(TREES[kind])
            tree.attrs.update({"plates": plates})
            for plate in plates:
                in_plate = {w: m for w, m in wells.items() if w.startswith(f"{plate}/")}
                tree.create_group(plate).attrs.update(plate_attrs(
                    name=plate, rows=rows, columns=columns,
                    wells=[tuple(w.split("/")[1:]) for w in in_plate],
                    field_count=max(len(m) for m in in_plate.values())))
                for well, members in in_plate.items():
                    tree.create_group(well).attrs.update(
                        well_attrs([field_name(f) for f in members])
                        | image_annotation(well_fields(members[0], lines[members[0].cid])))
                    for fov in members:
                        url = fov.url(kind)
                        group = image_group(
                            parsed[url], name=f"{fov.gene} FID{fov.fid:08d} {kind}",
                            order=ORDER[kind], omero=omero,
                            annotations=image_annotation(
                                fov_fields(fov, fov_records[fov.fid], fovs[fov]), source_url=url))
                        group.to_icechunk(session.store,
                                          group=f"{TREES[kind]}/{well}/{field_name(fov)}")
                        if kind == "stack":
                            progress.advance()
    written = time.monotonic() - started
    started = time.monotonic()
    snapshot = commit(session, f"OpenCell: {len(fovs)} fields of view on {len(plates)} plates")
    print(f"wrote {len(fovs) * len(kinds)} images in {written:.1f} s, "
          f"committed {snapshot} in {time.monotonic() - started:.1f} s")


if __name__ == "__main__":
    main()
