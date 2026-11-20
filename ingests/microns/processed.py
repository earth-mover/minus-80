"""Phase 2 of the MICrONS ingest: each scan's processed NWB file, its ROI masks, and its EM matches by CAVE version.

Phase 1 (``ingest.py``) put the 19 two-photon scans into the store as OME-Zarr
0.6 scenes of field images. This script adds three things to each scan group,
``two-photon/<session>-<scan>``, and changes none of phase 1's arrays:

- ``nwb``: DANDI 000402's NWB file of the scan, whole, in the unified NWB Zarr
  v3 convention that hdmf-zarr 0.14 reads. Every chunk is a reference into the
  file at DANDI: the traces, the masks as NWB stores them, the summary images,
  behavior, the stimulus movie, the trial tables, and the movie again.
- ``rois-<n>``: field n's ROI masks, written natively as an OME-Zarr image
  (roi, z, y, x) in the field's own orientation, one chunk per ROI.
- ``units``: the scan's units as CAVE numbers them, with the field and ROI
  each one is, and under ``units/em/<table>`` the EM match each table gives.
  The matches change with every CAVE version, so each version is a commit of
  its own.

    MINUS80_S3_ANONYMOUS=1 uv run --package microns-ingest python ingests/microns/processed.py \\
        --repo <dir | arraylake://org/repo> [--scans 4-7] [--parallel 8]

PLAN-phase2.md explains the decisions behind this layout and LAYOUTS.md
("Phase 2") records what the sources taught. ``verify_processed.py`` checks
the result and tags the commits.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

import h5py
import ingest
import numpy as np
import pandas as pd
import zarr
from ingest import SCANS, Scan
from plumbing import open_for_writing, run_forks

from minus80.core import as_url, remote
from minus80.core.icechunk_io import commit
from minus80.core.progress import Heartbeat
from minus80.ome import image_annotation, image_attrs
from minus80.parsers.nwb.unified import name_dimensions, nwb_references, under_group, write_nwb

#: Every NWB file of dandiset 000402 is a blob under this prefix, which the
#: repo's second virtual chunk container covers.
DANDI_BLOBS = "s3://dandiarchive/blobs/"
DANDI_HTTPS = "https://dandiarchive.s3.amazonaws.com/"

#: Where CAVE publishes static dumps of its tables, one directory per version.
CAVE_DUMPS = "https://storage.googleapis.com/mat_dbs/public/minnie65_phase3_v1/"
CAVE_LISTING = "https://storage.googleapis.com/storage/v1/b/mat_dbs/o"

#: The CAVE versions whose dumps hold two-photon match tables, in release order.
CAVE_VERSIONS = (343, 661, 795, 943, 1078, 1181, 1412, 1507)

#: A dump's two-photon match tables: CAVE names them after the coregistration
#: method, and each keys a row by the scan's (session, scan_idx, unit_id).
MATCH_TABLES = ("func_unit_em_match_release", "functional_coreg",
                "apl_functional_coreg_forward_v5", "apl_functional_coreg_vess_fwd",
                "coregistration_manual_v3", "coregistration_manual_v4",
                "coregistration_auto_phase3_fwd", "coregistration_auto_phase3_fwd_apl_vess_combined",
                "coregistration_auto_phase3_fwd_v2", "coregistration_auto_phase3_fwd_apl_vess_combined_v2")

COMPRESSOR = zarr.codecs.BloscCodec(cname="zstd", clevel=5, shuffle="shuffle")

PHASE = "processed"

ROOT_ADDED = {
    "title": "MICrONS minnie: two-photon functional scans, processed data and EM matches",
    "phase2": (
        "Each scan group also holds the scan's DANDI 000402 NWB file whole at nwb/ "
        "(unified NWB Zarr v3 convention, hdmf-zarr 0.14; every chunk a reference to "
        "DANDI), each field's ROI masks as an image, rois-<n>, and units/, the scan's "
        "cells as CAVE numbers them with their EM matches. Tags cave-v<version> pin the "
        "matches of each CAVE version."),
    "codecs": ("Zarr v3 core (bytes, transpose, sharding_indexed, blosc, zstd, vlen-utf8) and "
               "numcodecs' zlib for the NWB file's gzip chunks; zarr-python reads every array"),
}


# The NWB file
# ------------
# DANDI publishes one NWB file per scan, 62 to 96 GB, almost all of it the
# eight field movies phase 1 already references in BossDB's TIFFs. The rest
# is what Baylor's processing made: per field, a PlaneSegmentation table of
# ROI masks (with, on fields 2, 4, 6 and 8, the EM point and IDs CAVE v343
# gave), a RoiResponseSeries of fluorescence traces and two summary images;
# per scan, the eye, pupil and treadmill recordings, the stimulus movie the
# mouse watched and three trial tables. The store keeps the whole file at
# the scan's nwb/ group as shadowzarr maps it, so NWB's paths, types and
# links survive and a reader needs no knowledge of this ingest to find a
# trace. Only dimension_names are added, so xarray opens any series or table.
# shadowzarr reads the headers over HTTPS; the references name the s3://
# location of the same object, which the repo's container covers.

def nwb_location(scan: Scan) -> tuple[str, callable]:
    """Where shadowzarr reads the scan's NWB file, and how its URLs become reference URLs."""
    if scan.nwb.startswith(DANDI_BLOBS):
        https = DANDI_HTTPS + scan.nwb.removeprefix("s3://dandiarchive/")
        return https, lambda url: url.replace(DANDI_HTTPS, "s3://dandiarchive/")
    return unquote(urlparse(scan.nwb).path), lambda url: as_url(url)


def write_nwb_file(session, scan: Scan) -> dict:
    """Write the scan's NWB file whole at ``two-photon/<scan>/nwb``; return what it holds."""
    started = time.monotonic()
    location, url_for = nwb_location(scan)
    rfs = name_dimensions(under_group(nwb_references(location), f"{SCANS}/{scan.name}/nwb"))
    write_nwb(rfs, session, url_for=url_for)
    arrays = sum(1 for k, v in rfs["refs"].items() if k.endswith("zarr.json") and '"node_type":"array"' in v)
    return {"nwb_arrays": arrays, "nwb_seconds": round(time.monotonic() - started, 1)}


# The ROI masks
# -------------
# Each PlaneSegmentation's image_mask is (rois, columns, rows) float32, one
# weight image per ROI, transposed against the TIFF page like every NWB image
# of these files, and gzip-chunked 87 ROIs at a time by 16 x 28 pixels, so
# one ROI's mask costs 256 chunk reads. The masks overlap: in scan 4-7, 57% of
# the pixels any ROI covers are covered by two or more, so an integer label
# image would drop most of them. They are small once written natively, about
# 5 MB per scan with Blosc, so each field gets an image of its own, in the
# TIFF's orientation, one chunk per ROI inside one shard per field: one ROI
# is one chunk read, and a field is one object. The image places itself in
# the scene's motor coordinates with a translation, because a scene requires
# its images to share its axes and the ROI axis is not time.

ROI_DIMS = ("roi", "z", "y", "x")
ROI_DIMENSIONS = {"roi": {"type": "roi"}, "z": {"type": "space"}, "y": {"type": "space"},
                  "x": {"type": "space"}}


def read_masks(scan: Scan) -> dict[int, np.ndarray]:
    """Every field's masks, (roi, rows, columns), as h5py decodes them from the NWB file."""
    masks = {}
    with h5py.File(remote.reader(scan.nwb, block=4 << 20, cached_blocks=64), "r") as f:
        segmentation = f["processing/ophys/ImageSegmentation"]
        for name in sorted(segmentation, key=lambda k: int(k.removeprefix("PlaneSegmentation"))):
            table = segmentation[name]
            number = int(name.removeprefix("PlaneSegmentation"))
            ids = table["id"][()]
            if not np.array_equal(ids, np.arange(1, len(ids) + 1)):
                raise ValueError(f"{scan.name} {name}: ROI ids are not 1 to {len(ids)}")
            masks[number] = table["image_mask"][()].transpose(0, 2, 1)
    return masks


def write_masks(store, scan: Scan, number: int, masks: np.ndarray, field: dict) -> None:
    """Field ``number``'s masks as an image at ``rois-<number>``, placed where the field is."""
    base = f"{SCANS}/{scan.name}"
    rois, rows, columns = masks.shape
    shape = (rois, 1, rows, columns)
    pixel = field["pixel_um"]
    ome = image_attrs(
        name=f"scan {scan.name} field {number} ROI masks", dims=ROI_DIMS, level_shapes=[shape],
        scale={"roi": 1, "z": ingest.PLANE_UM, "y": pixel, "x": pixel},
        translation={"roi": 0, "z": field["z"], "y": field["y"], "x": field["x"]},
        units={"z": "micrometer", "y": "micrometer", "x": "micrometer"},
        dimensions=ROI_DIMENSIONS, version=ingest.VERSION,
        description="the NWB image_mask, transposed to the TIFF's orientation; written natively")
    nwb = f"nwb/processing/ophys/ImageSegmentation/PlaneSegmentation{number}"
    annotation = image_annotation({
        "field": f"../field-{number}", "roi": f"ROI r of this image is ROI id r + 1 of {nwb}",
        "units": f"ROI id r + 1 is unit {field['first_unit']} + r in ../units",
        "traces": f"nwb/processing/ophys/Fluorescence/RoiResponseSeries{number}",
        "weights": "each ROI's mask is a weight per pixel (CaImAn), not a 0/1 label; ROIs overlap",
        "placement": ("the translation is field-{n}'s placement in the scan's motor coordinates; "
                      "the ROI axis has no counterpart there, so the scene does not list this image"
                      ).format(n=number),
    }, source_url=scan.nwb)
    group = zarr.open_group(store, path=f"{base}/rois-{number}", mode="w")
    group.attrs.put({"ome": ome} | annotation)
    array = group.create_array(
        "0", shape=shape, dtype="float32", chunks=(1, 1, rows, columns), shards=shape,
        compressors=COMPRESSOR, dimension_names=ROI_DIMS, fill_value=0)
    array[:] = masks[:, None]


# The units
# ---------
# CAVE names a two-photon cell by (session, scan_idx, unit_id), where unit_id
# runs over the whole scan; the NWB file numbers ROIs 1 to n within each field.
# No file states how the two relate. The rule, from DataJoint's ScanUnit
# table, is that a field's units follow the earlier fields' in order:
#   unit_id = (ROIs in fields 1 .. n-1) + ROI id.
# Every match row that names its field agrees with it (LAYOUTS.md), and the
# ingest raises on any row that does not. units/ holds the rule's result, so
# a reader goes from a unit to its field and ROI, and from there to the trace.

def write_units(store, scan: Scan, counts: dict[int, int]) -> None:
    fields = sorted(counts)
    unit_field = np.concatenate([np.full(counts[n], n, dtype="int64") for n in fields])
    unit_roi = np.concatenate([np.arange(1, counts[n] + 1, dtype="int64") for n in fields])
    group = zarr.open_group(store, path=f"{SCANS}/{scan.name}/units", mode="w")
    group.attrs.put({
        "description": ("The scan's two-photon cells as CAVE numbers them. unit_id is "
                        "(ROIs in the scan's earlier fields) + the ROI's id in its field; no "
                        "source file states this rule, which DataJoint's ScanUnit table holds, and "
                        "every CAVE match row that names a field agrees with it. em/<table> holds "
                        "each CAVE match table's row for a unit, at the CAVE version this commit "
                        "states; a unit the table does not match holds the fill value."),
        "rois_per_field": {str(n): counts[n] for n in fields},
    })
    for name, values in (("unit_id", np.arange(1, len(unit_field) + 1, dtype="int64")),
                         ("field", unit_field), ("roi", unit_roi)):
        group.create_array(name, data=values, chunks=values.shape, compressors=COMPRESSOR,
                           dimension_names=("unit",))


# One scan
# --------
# A worker process writes one scan into its fork: the NWB file, the masks and
# the unit axis, and adds to the scan group's attributes where each of them is.

def write_scan(fork, scan: Scan, fields: dict[int, dict]):
    started = time.monotonic()
    summary = write_nwb_file(fork, scan)
    masks = read_masks(scan)
    if set(masks) != set(fields):
        raise ValueError(f"{scan.name}: NWB masks for fields {sorted(masks)}, store fields {sorted(fields)}")
    counts = {n: len(m) for n, m in masks.items()}
    first = 1
    for n in sorted(masks):
        write_masks(fork.store, scan, n, masks[n], fields[n] | {"first_unit": first})
        first += counts[n]
    write_units(fork.store, scan, counts)
    group = zarr.open_group(fork.store, path=f"{SCANS}/{scan.name}", mode="a")
    attrs = dict(group.attrs)
    attrs["scan"] = attrs["scan"] | {"processed": {
        "nwb": "nwb: DANDI's NWB file of this scan, whole, unified NWB Zarr v3 convention",
        "rois": "rois-<n>: field n's ROI masks, (roi, z, y, x)",
        "units": "units: unit_id, field and roi of every cell; em/<table>: its EM matches",
        "rois_per_field": {str(n): c for n, c in counts.items()},
    }}
    group.attrs.put(attrs)
    summary |= {"rois": sum(counts.values()), "mask_mb": round(sum(m.nbytes for m in masks.values()) / 1e6),
                "seconds": round(time.monotonic() - started, 1)}
    print(f"{scan.name}: NWB {summary['nwb_arrays']} arrays in {summary['nwb_seconds']} s, "
          f"{summary['rois']:,} ROIs in {len(counts)} fields, {summary['seconds']} s", flush=True)
    return fork, summary


def field_placements(repo, scans: list[Scan]) -> dict[str, dict[int, dict]]:
    """Each field's translation in its scene and its pixel size, as phase 1 wrote them."""
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    out = {}
    for scan in scans:
        group = root[f"{SCANS}/{scan.name}"]
        scene = dict(group.attrs)["ome"]["scene"]["coordinateTransformations"]
        fields = {}
        for transform in scene:
            path = transform["input"]["path"]
            _, z, y, x = transform["translation"]
            number = int(path.removeprefix("field-"))
            pixel = dict(group[path].attrs)["annotations"]["pixel_um"]
            fields[number] = {"z": z, "y": y, "x": x, "pixel_um": pixel}
        out[scan.name] = fields
    return out


def committed(repo) -> dict[str, object]:
    """The latest snapshot on main of each phase-2 commit, by the step its metadata names."""
    found = {}
    for snapshot in repo.ancestry(branch="main"):
        step = (snapshot.metadata or {}).get("phase2")
        if step and step not in found:
            found[step] = snapshot
    return found


def commit_processed(repo, scans: list[Scan], *, parallel: int) -> str:
    """The NWB files, masks and unit axes of every scan, as one commit on main."""
    names = sorted(s.name for s in scans)
    if (done := committed(repo).get(PHASE)) is not None:
        if missing := sorted(set(names) - set(done.metadata.get("scans", []))):
            raise ValueError(f"the processed data is committed in {done.id} without scans {missing}")
        print(f"the processed data is already committed in {done.id}", flush=True)
        return done.id
    placements = field_placements(repo, scans)
    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    root.attrs.put(dict(root.attrs) | ROOT_ADDED)
    jobs = {s.name: (write_scan, (s, placements[s.name])) for s in scans}
    done, failed = run_forks(session, jobs, parallel=parallel,
                             what=f"writing the processed data of {len(scans)} scans, {parallel} at a time")
    if failed:
        raise RuntimeError(f"{len(failed)} scans failed, nothing committed: {failed}")
    rois = sum(s["rois"] for s in done.values())
    message = (f"MICrONS processed two-photon data: each of {len(scans)} scans' DANDI 000402 NWB file "
               f"(version {ingest.DANDI_VERSION}), whole, at two-photon/<scan>/nwb in the unified NWB "
               f"Zarr v3 convention, every chunk referenced at DANDI; the {rois:,} ROI masks written as "
               "one image per field, rois-<n>; and units/, each scan's cells as CAVE numbers them. "
               "The EM matches here are those the NWB files carry, from CAVE v343.")
    with Heartbeat(lambda: "committing the processed data", every=30.0):
        snapshot = commit(session, message, metadata={"phase2": PHASE, "scans": names,
                                                     "dandiset": f"000402/{ingest.DANDI_VERSION}",
                                                     "rois": rois})
    print(f"committed the processed data in {snapshot}", flush=True)
    return snapshot


# The EM matches, one CAVE version at a time
# ------------------------------------------
# CAVE's static dumps hold each table as a header-less, whole-file gzip CSV
# with a second file naming its columns and their types. A table keeps its
# rows, ids and supervoxels from version to version; what changes is each
# row's root ID, as proofreading edits the cell. Each version is one commit:
# the tables it lists are written on each scan's unit axis, a table it no
# longer lists is deleted, and an array that did not change is left alone,
# so Icechunk's diff between two versions' tags names exactly the arrays
# that changed. Rows for scans with no NWB file (5-4, 5-5 and 8-4, published
# only in platinum_camovies/) are counted in the commit's metadata and left
# to the full tables beside the store.

#: Columns that locate a row rather than describe its match.
KEYS = ("session", "scan_idx", "unit_id", "field", "pt_position_x", "pt_position_y", "pt_position_z")
#: Values for a unit a table does not match.
FILLS = {"uint64": 0, "int64": -1, "float64": np.nan}


def fetch(url: str) -> bytes:
    if url.startswith("http"):
        with urllib.request.urlopen(url, timeout=300) as response:
            return response.read()
    return Path(url.removeprefix("file://")).read_bytes()


def dump_tables(version: int, base: str = CAVE_DUMPS) -> dict[str, tuple[pd.DataFrame, dict]]:
    """The version's match tables, each with what its dump objects are (name, generation, date)."""
    listed = {}
    if base == CAVE_DUMPS:
        prefix = f"public/minnie65_phase3_v1/v{version}/"
        page = json.loads(fetch(f"{CAVE_LISTING}?prefix={prefix}&fields=items(name,generation,updated,md5Hash)"))
        listed = {item["name"].removeprefix(prefix): item for item in page.get("items", [])}
        names = [t for t in MATCH_TABLES if f"{t}_merged.csv.gz" in listed]
    else:
        names = [t for t in MATCH_TABLES if Path(base.removeprefix("file://"), f"v{version}",
                                                 f"{t}_merged.csv.gz").exists()]
    tables = {}
    for name in names:
        header = pd.read_csv(io.BytesIO(fetch(f"{base}v{version}/{name}_merged_header.csv")), header=None)
        types = dict(zip(header[0], header[1]))
        frame = pd.read_csv(io.BytesIO(gzip.decompress(fetch(f"{base}v{version}/{name}_merged.csv.gz"))),
                            header=None, names=list(types), dtype=types)
        source = {"url": f"{base}v{version}/{name}_merged.csv.gz"}
        if item := listed.get(f"{name}_merged.csv.gz"):
            source |= {"generation": item["generation"], "updated": item["updated"], "md5": item["md5Hash"]}
        tables[name] = (frame, source)
    return tables


def unit_rows(table: str, frame: pd.DataFrame, counts: dict[str, dict[int, int]]) -> tuple[dict, dict]:
    """The table's rows by scan, each scan's frame indexed by unit_id; and what was left out.

    Rows repeated in everything but ``id`` are kept once. Raises if a unit has
    two different rows, a unit_id is outside its scan, or a row's field
    disagrees with the unit rule.
    """
    content = [c for c in frame.columns if c != "id"]
    deduplicated = frame.drop_duplicates(subset=content)
    left_out = {"repeated_rows": len(frame) - len(deduplicated), "rows_for_scans_without_nwb": {}}
    out = {}
    for (session, scan_idx), rows in deduplicated.groupby(["session", "scan_idx"]):
        name = f"{session}-{scan_idx}"
        if name not in counts:
            left_out["rows_for_scans_without_nwb"][name] = len(rows)
            continue
        if rows.unit_id.duplicated().any():
            raise ValueError(f"{table} {name}: units with two different rows: "
                             f"{sorted(rows.unit_id[rows.unit_id.duplicated()])[:5]}")
        edges = np.cumsum([0] + [counts[name][n] for n in sorted(counts[name])])
        units = rows.unit_id.to_numpy()
        if units.min() < 1 or units.max() > edges[-1]:
            raise ValueError(f"{table} {name}: unit_id outside 1..{edges[-1]}")
        if "field" in rows:
            field = np.asarray(sorted(counts[name]))[np.searchsorted(edges, units, side="left") - 1]
            if (bad := int((field != rows.field.to_numpy()).sum())):
                raise ValueError(f"{table} {name}: {bad} rows whose field disagrees with the unit rule")
        out[name] = rows.set_index("unit_id")
    return out, left_out


def match_arrays(rows: pd.DataFrame, units: int) -> dict[str, np.ndarray]:
    """One array per column of the table on the scan's unit axis; the EM point as (unit, 3)."""
    at = rows.index.to_numpy() - 1
    arrays = {}
    for column in rows.columns:
        if column in KEYS:
            continue
        dtype = "uint64" if column in ("pt_root_id", "pt_supervoxel_id") else str(rows[column].dtype)
        values = np.full(units, FILLS[dtype], dtype=dtype)
        values[at] = rows[column].to_numpy().astype(dtype)
        arrays[column] = values
    point = np.full((units, 3), -1, dtype="int64")
    point[at] = rows[["pt_position_x", "pt_position_y", "pt_position_z"]].to_numpy()
    arrays["pt_position"] = point
    return arrays


def put(group, name: str, values: np.ndarray, dims: tuple[str, ...], fill) -> bool:
    """Write ``values`` at ``name``, in place when the array exists; False if it already held them."""
    if name in group:
        existing = group[name]
        if existing.shape == values.shape and existing.dtype == values.dtype:
            if np.array_equal(existing[...], values, equal_nan=values.dtype.kind == "f"):
                return False
            existing[...] = values
            return True
        del group[name]
    array = group.create_array(name, shape=values.shape, dtype=values.dtype, chunks=values.shape,
                               compressors=COMPRESSOR, dimension_names=dims, fill_value=fill)
    array[...] = values
    return True


def commit_cave_version(repo, version: int, scans: list[Scan], *, base: str = CAVE_DUMPS) -> str:
    step = f"cave-v{version}"
    if (done := committed(repo).get(step)) is not None:
        print(f"CAVE v{version} is already committed in {done.id}", flush=True)
        return done.id
    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    counts = {s.name: {int(k): v for k, v in
                       dict(root[f"{SCANS}/{s.name}/units"].attrs)["rois_per_field"].items()} for s in scans}
    tables = dump_tables(version, base)
    if not tables:
        raise ValueError(f"CAVE v{version}'s dump holds none of the match tables")
    changed, report = 0, {}
    for table, (frame, source) in tables.items():
        by_scan, left_out = unit_rows(table, frame, counts)
        report[table] = source | {"rows": len(frame)} | left_out
        for scan in scans:
            em = root.require_group(f"{SCANS}/{scan.name}/units/em")
            group = em.require_group(table)
            group.attrs.put({"cave_version": version, "table": table, "source": source["url"],
                             "rows": int(len(by_scan.get(scan.name, ()))),
                             "description": (f"CAVE table {table} at materialization version {version}, "
                                             "one value per unit; pt_position is the EM point in 4 x 4 x 40 nm "
                                             "voxels; id is the table's row id")})
            units = sum(counts[scan.name].values())
            rows = by_scan.get(scan.name, pd.DataFrame(columns=frame.columns).set_index("unit_id"))
            for name, values in match_arrays(rows, units).items():
                dims = ("unit", "axis") if values.ndim == 2 else ("unit",)
                changed += put(group, name, values, dims, FILLS.get(str(values.dtype), -1))
    for scan in scans:
        em = root[f"{SCANS}/{scan.name}/units/em"]
        for gone in sorted(set(em.group_keys()) - set(tables)):
            del em[gone]
            changed += 1
    message = (f"EM matches of the two-photon units at CAVE materialization version {version}, from "
               f"CAVE's static dumps: {', '.join(sorted(tables))}. Root IDs are as of this version; "
               "proofreading changes them between versions.")
    snapshot = commit(session, message, metadata={"phase2": step, "cave_version": version,
                                                  "tables": report, "arrays_changed": changed})
    print(f"CAVE v{version}: {len(tables)} tables, {changed} arrays changed, committed in {snapshot}", flush=True)
    return snapshot


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="a directory or arraylake://org/repo holding phase 1")
    ap.add_argument("--scans", help="scans, as session-scan, comma-separated; default all")
    ap.add_argument("--parallel", type=int, default=8, help="scans written at once, one process each")
    args = ap.parse_args(argv)
    # DANDI's bucket is public, and requests signed with other credentials would be refused.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    started = time.monotonic()
    scans = ingest.list_scans()
    if args.scans:
        wanted = set(args.scans.split(","))
        if unknown := wanted - {s.name for s in scans}:
            raise SystemExit(f"no such scans: {sorted(unknown)}")
        scans = [s for s in scans if s.name in wanted]
    print(f"{len(scans)} scans: {', '.join(s.name for s in scans)}", flush=True)
    repo = open_for_writing(args.repo, DANDI_BLOBS)
    commit_processed(repo, scans, parallel=args.parallel)
    for version in CAVE_VERSIONS:
        commit_cave_version(repo, version, scans)
    print(f"done in {time.monotonic() - started:.0f} s; verify_processed.py --tag tags the commits "
          "once the store check passes", flush=True)


if __name__ == "__main__":
    sys.exit(main())
