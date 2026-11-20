"""Reference the MICrONS two-photon functional scans in a virtual OME-Zarr 0.6 store.

MICrONS recorded calcium activity in one mouse's visual cortex with a
two-photon mesoscope, in 19 scans, before the same cortex was imaged by
electron microscopy. BossDB publishes each scan as one uncompressed TIFF of
every frame, in two exports, v1 (2021) and v2 (2023). DANDI publishes the same
movies again inside NWB files (dandiset 000402), together with what the TIFFs
lack: each field's pixel size, depth and position, and the time of every
volume.

The store references the TIFFs' own bytes and takes the metadata from the NWB
files:

- ``two-photon/<session>-<scan>`` is one scan, an OME-Zarr 0.6 scene that
  places its fields in the microscope's coordinates. It holds
  ``volume_times``, the time of each volume in seconds.
- ``two-photon/<session>-<scan>/field-<n>`` is one imaging field, an image
  with axes (t, z, y, x): one TIFF page per chunk, one plane deep.
- ``OME`` lists every field image, so a reader that knows OME-Zarr
  collections finds them all.

The store is committed twice. The first commit references the v1 export, as
it declares its pixels (uint16), and is tagged ``v1``; the second references
the v2 export (int16) and is tagged ``v2``.

Running it
----------
::

    uv run --package microns-ingest python ingests/microns/ingest.py \\
        --repo <dir | s3://bucket/prefix | arraylake://org/repo> \\
        [--scans 4-7,9-6] [--parallel 8]

PLAN.md explains the decisions behind this layout and LAYOUTS.md records what
the source taught. ``plumbing.py`` beside this file holds the bucket listing,
the DANDI lookup, the repo and the worker processes.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import h5py
import numpy as np
import zarr
from plumbing import dandi_assets, list_prefix, open_for_writing, run_forks
from virtualizarr.manifests import ManifestArray, ManifestGroup

from minus80.core import remote
from minus80.core.fetch import read_range
from minus80.core.icechunk_io import commit
from minus80.core.manifest import stack, take
from minus80.core.progress import Heartbeat
from minus80.core.registry import registry_for
from minus80.ome import image_annotation, image_attrs, root_attrs, scene_attrs, series_attrs
from minus80.parsers.tiff import TiffParser

SOURCE = ("s3://bossdb-open-data/iarpa_microns/minnie/functional_data/"
          "two_photon_functional_scans/")
DANDISET, DANDI_VERSION = "000402", "0.230307.2132"
VERSION = "0.6"
SCANS = "two-photon"

ROOT = {
    "title": "MICrONS minnie: two-photon functional scans",
    "description": (
        "Calcium imaging (GCaMP6) of mouse 17797's visual cortex with a two-photon "
        "random access mesoscope, in 19 scans of 4 to 8 imaging fields each, recorded "
        "before the same cortex was imaged by electron microscopy for the MICrONS "
        "project. Each field is an image whose chunks are the frames of BossDB's TIFF "
        "export, read in place; pixel sizes, field positions and volume times are "
        "taken from the DANDI 000402 NWB files of the same scans."),
    "license": "CC-BY-4.0",
    "citation": [
        "MICrONS Consortium et al. (2025). Functional connectomics spanning multiple "
        "areas of mouse visual cortex. Nature 640, 435-447. "
        "https://doi.org/10.1038/s41586-025-08790-w",
        "IARPA MICrONS Minnie, BossDB (2022). https://doi.org/10.60533/BOSS-2021-T0SY",
        "MICrONS two-photon data, DANDI Archive dandiset 000402, version "
        f"{DANDI_VERSION}. https://doi.org/10.48324/dandi.000402/{DANDI_VERSION}",
    ],
    "sources": {
        "pixels": SOURCE,
        "pixels_documentation": SOURCE
        + "two_photon_functional_scans_technical_documentation_v2.pdf",
        "metadata": f"https://dandiarchive.org/dandiset/{DANDISET}/{DANDI_VERSION}",
    },
    "codecs": "Zarr v3 core only (bytes); the pixels are uncompressed",
}


# The scans
#
# BossDB names each TIFF functional_scan_<animal>_<session>_<scan>_v<export>.tif,
# and every scan comes in both exports. The documentation's changelog dates
# them: v1 is the first public version, and v2 "corrected pixel over/under-flow
# error in export". The two exports hold the same pixel bytes; v1 declares
# them uint16 and v2 int16, which is right, because the motion-corrected
# frames hold a few negative values (LAYOUTS.md). DANDI names the NWB file of
# the same scan sub-<animal>/sub-<animal>_ses-<session>-scan-<scan>_..., so the
# two are joined on (session, scan), and a scan missing from either side stops
# the ingest.

TIFF_NAME = re.compile(
    r"^functional_scan_(?P<animal>\d+)_(?P<session>\d+)_(?P<scan>\d+)_v(?P<export>\d)\.tif$")
NWB_NAME = re.compile(
    r"^sub-(?P<animal>\d+)/sub-(?P=animal)_ses-(?P<session>\d+)-scan-(?P<scan>\d+)_.*\.nwb$")

EXPORTS = {
    1: {"export": "v1", "date": "2021-07-02", "dtype": "uint16"},
    2: {"export": "v2", "date": "2023-01-13", "dtype": "int16"},
}


@dataclass(frozen=True)
class Scan:
    animal: int
    session: int
    scan: int
    tiffs: dict[int, str]
    nwb: str
    nwb_path: str

    @property
    def name(self) -> str:
        return f"{self.session}-{self.scan}"


def list_scans(source: str = SOURCE) -> list[Scan]:
    """Every scan in the BossDB listing, joined to its DANDI NWB file."""
    tiffs: dict[tuple, dict[int, str]] = {}
    for name in list_prefix(source):
        if m := TIFF_NAME.match(name):
            key = (int(m["animal"]), int(m["session"]), int(m["scan"]))
            tiffs.setdefault(key, {})[int(m["export"])] = source + name
    nwbs = {}
    for path, asset in dandi_assets(DANDISET, DANDI_VERSION).items():
        if m := NWB_NAME.match(path):
            nwbs[(int(m["animal"]), int(m["session"]), int(m["scan"]))] = (asset["url"], path)
    if set(tiffs) != set(nwbs):
        raise ValueError(f"BossDB and DANDI list different scans: only BossDB has "
                         f"{sorted(set(tiffs) - set(nwbs))}, only DANDI has {sorted(set(nwbs) - set(tiffs))}")
    if incomplete := sorted(k for k, v in tiffs.items() if set(v) != set(EXPORTS)):
        raise ValueError(f"scans without both exports: {incomplete}")
    return [Scan(*key, tiffs=tiffs[key], nwb=nwbs[key][0], nwb_path=nwbs[key][1])
            for key in sorted(tiffs)]


# What the NWB file states about each field
#
# The NWB file holds one TwoPhotonSeries per field, acquisition/TwoPhotonSeries<n>,
# linked to general/optophysiology/ImagingPlane<n>. The series' data is the same
# movie as the TIFF's, transposed: TwoPhotonSeries<n> frame v equals TIFF page
# v * F + n - 1 with rows and columns swapped (checked pixel for pixel in the
# trial, PLAN.md). The imaging plane's origin_coords is the field's center in
# the microscope's motor coordinates, in meters, (x, y, depth): the conversion
# code (catalystneuro/MICrONS-to-nwb) fills it from the DataJoint Field table's
# field_x, field_y and field_z, which that schema documents as the field
# center, x running posterior to anterior and y lateral to medial. x runs
# along the TIFF's columns and y along its rows. grid_spacing is the pixel
# size in meters. The frame rate appears only in the series description ("at
# 6.3009 Hz"); imaging_rate is NaN. Every series carries the same timestamps,
# the scan's volume times, so the store keeps them once per scan.
#
# Only the metadata is read, never the series' data, so each file costs a few
# dozen small range reads.

RATE = re.compile(r"at ([0-9.]+) Hz")


@dataclass(frozen=True)
class Field:
    number: int
    depth_um: float
    center_um: dict[str, float]
    pixel_um: float
    rows: int
    columns: int
    stated: dict


@dataclass(frozen=True)
class ScanFacts:
    scan: Scan
    rate_hz: float
    volume_times: np.ndarray
    fields: list[Field]


def read_nwb(scan: Scan) -> ScanFacts:
    """Each field's geometry and the scan's volume times, as the NWB file states them.

    Raises when the series disagree about the volume times, when a pixel is
    not square, or when the stated rate and the volume times differ by more
    than 1%.
    """
    with h5py.File(remote.reader(scan.nwb), "r") as f:
        names = sorted((k for k in f["acquisition"] if k.startswith("TwoPhotonSeries")),
                       key=lambda k: int(k.removeprefix("TwoPhotonSeries")))
        fields, times, rates = [], None, set()
        for name in names:
            series = f[f"acquisition/{name}"]
            number = int(name.removeprefix("TwoPhotonSeries"))
            link = series.get("imaging_plane", getlink=True)
            plane = f[link.path]
            spacing = plane["grid_spacing"][()]
            origin = plane["origin_coords"][()]
            if spacing[0] != spacing[1]:
                raise ValueError(f"{scan.name} {name}: pixels of {spacing} m are not square")
            stamps = series["timestamps"][()]
            if times is None:
                times = stamps
            elif not np.array_equal(stamps, times):
                raise ValueError(f"{scan.name} {name}: timestamps differ from TwoPhotonSeries1's")
            description = text(series.attrs["description"])
            rates.add(float(RATE.search(description)[1]))
            _, columns, rows = series["data"].shape
            fields.append(Field(
                number=number,
                depth_um=round(float(origin[2]) * 1e6, 6),
                center_um={"x": round(float(origin[0]) * 1e6, 6), "y": round(float(origin[1]) * 1e6, 6)},
                pixel_um=round(float(spacing[0]) * 1e6, 9),
                rows=rows, columns=columns,
                stated={
                    "series": f"acquisition/{name}",
                    "description": description,
                    "field_of_view_m": series["field_of_view"][()].tolist(),
                    "dimension": series["dimension"][()].tolist(),
                    "imaging_plane": link.path,
                    "imaging_plane_description": text(plane["description"][()]),
                    "origin_coords_m": origin.tolist(),
                    "grid_spacing_m": spacing.tolist(),
                    "indicator": text(plane["indicator"][()]),
                    "location": text(plane["location"][()]),
                    "excitation_lambda_nm": float(plane["excitation_lambda"][()]),
                    "emission_lambda_nm": float(plane["OpticalChannel/emission_lambda"][()]),
                    "device": text(plane["device"].attrs.get("description", "")),
                }))
    (rate,) = rates
    measured = 1 / float(np.median(np.diff(times)))
    if abs(measured - rate) > 0.01 * rate:
        raise ValueError(f"{scan.name}: the description states {rate} Hz, the timestamps {measured:.4f} Hz")
    return ScanFacts(scan=scan, rate_hz=rate, volume_times=times, fields=fields)


def text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


# The pages of a scan's TIFF
#
# tifffile wrote each TIFF as one series of shape (volumes * fields, rows,
# columns): the first directory at byte 16, then every page's pixels back to
# back as one uncompressed strip each, then the other directories, packed at
# the end of the file. The TIFF parser fetches the file's first 64 KB and the
# directory block (51 to 90 MB) and lets tifffile read every directory; each
# page becomes one chunk, at the offset its own directory states.
#
# The pages interleave the fields: page v * F + (n - 1) is field n of volume
# v. Taking every F-th page from n - 1 gives field n's time series, still one
# chunk per page, and a z axis of length one gives the field its depth.

def field_arrays(url: str, facts: ScanFacts) -> tuple[dict[int, ManifestArray], dict]:
    """Each field's frames in the TIFF at ``url``, as a (t, z, y, x) array, and what the TIFF states.

    Raises when the TIFF's page count or page shape disagrees with the NWB file.
    """
    parsed = TiffParser(prefetch="directories")(url, registry_for(url, patient=True)).group
    pages = parsed["0"]
    volumes, count = len(facts.volume_times), len(facts.fields)
    first = facts.fields[0]
    if pages.shape != (volumes * count, first.rows, first.columns):
        raise ValueError(f"{url}: pages of shape {pages.shape}; the NWB file states {volumes} volumes "
                         f"of {count} fields of {first.rows} x {first.columns}")
    arrays = {}
    for index, field in enumerate(facts.fields):
        frames = take(pages, np.arange(index, volumes * count, count), along=0,
                      dim="t", axis={"type": "time"})
        arrays[field.number] = stack([frames], dim="z", axis={"type": "space"}, position=1)
    return arrays, dict(parsed.metadata.attributes)


# The store
#
# Each field is an OME-Zarr image of its own: OME-Zarr allows one image per
# multiscale group, and the fields of a scan differ in position, so they are
# not one array. The image's own transform is a scale: the volume interval in
# seconds, a nominal 1 micrometer for the single plane, and the pixel size.
# Where the field sits is the scan's business: the scan group is a 0.6 scene,
# whose coordinate system "motor" is the microscope's, and which translates
# each field to its depth, to its first pixel's center, and to the time of the
# scan's first volume. A viewer that reads scenes lays the fields out as they
# were imaged, the coplanar pairs side by side with their 40 micrometer
# overlap.

DIMS = ("t", "z", "y", "x")
UNITS = {"t": "second", "z": "micrometer", "y": "micrometer", "x": "micrometer"}
DIMENSIONS = {"t": {"type": "time"}, "z": {"type": "space"},
              "y": {"type": "space"}, "x": {"type": "space"}}

#: The z scale of a field. A field is one optical section, and no source
#: states its thickness, so the scale is a placeholder that the attributes
#: name as one.
PLANE_UM = 1.0


def scans_attributes(scans: list[ScanFacts]) -> dict:
    return {
        "description": "One group per two-photon scan, named <session>-<scan>; animal 17797.",
        "scans": {f.scan.name: {"fields": len(f.fields), "volumes": len(f.volume_times),
                                "rate_hz": f.rate_hz} for f in scans},
    }


def scan_attributes(facts: ScanFacts) -> dict:
    """The scan group's scene and a summary of what the scan was."""
    translations = {}
    for field in facts.fields:
        translations[f"field-{field.number}"] = {
            "t": float(facts.volume_times[0]),
            "z": field.depth_um,
            "y": field.center_um["y"] - (field.rows - 1) / 2 * field.pixel_um,
            "x": field.center_um["x"] - (field.columns - 1) / 2 * field.pixel_um,
        }
    scene = scene_attrs(translations, name="motor", dims=DIMS, units=UNITS, dimensions=DIMENSIONS)
    scan = facts.scan
    return scene | {"scan": {
        "animal": scan.animal, "session": scan.session, "scan": scan.scan,
        "volumes": len(facts.volume_times), "fields": len(facts.fields),
        "rate_hz": facts.rate_hz,
        "depths_um": sorted({f.depth_um for f in facts.fields}),
        "coordinates": ("motor: the microscope's stage coordinates in micrometers, x posterior "
                        "to anterior, y lateral to medial, z depth below the pia; each field is "
                        "placed by its center from the NWB imaging plane's origin_coords"),
        "nwb": scan.nwb, "nwb_dandi_path": scan.nwb_path,
        "tiff": {EXPORTS[v]["export"]: url for v, url in sorted(scan.tiffs.items())},
    }}


def write_volume_times(store, path: str, facts: ScanFacts) -> None:
    """The scan's volume times, as a native array beside its fields."""
    group = zarr.open_group(store, path=path, mode="a")
    times = group.create_array(
        "volume_times", shape=facts.volume_times.shape, dtype="float64", chunks=(len(facts.volume_times),),
        dimension_names=("t",), overwrite=True,
        # Real timestamps barely compress with zstd alone; the byte shuffle
        # groups their slowly changing high bytes.
        compressors=zarr.codecs.BloscCodec(cname="zstd", clevel=5, shuffle="shuffle"))
    times[:] = facts.volume_times
    times.attrs.update({
        "units": "second",
        "description": ("The time of each volume, in seconds on the NWB file's clock, from every "
                        "TwoPhotonSeries' timestamps (identical across the scan's fields). "
                        "Volume v of every field image in this scan is at volume_times[v]."),
    })


def field_group(array: ManifestArray, field: Field, facts: ScanFacts, export: int,
                tiff: dict) -> ManifestGroup:
    """One field's image: its frames, its scale, and what the NWB file and the TIFF state about it."""
    scan = facts.scan
    count = len(facts.fields)
    ome = image_attrs(
        name=f"scan {scan.name} field {field.number}", dims=DIMS, level_shapes=[array.shape],
        scale={"t": 1 / facts.rate_hz, "z": PLANE_UM, "y": field.pixel_um, "x": field.pixel_um},
        units=UNITS, dimensions=DIMENSIONS, version=VERSION,
        description="the TIFF's own pages, one chunk per page; a single level")
    fields = {
        "animal": scan.animal, "session": scan.session, "scan": scan.scan, "field": field.number,
        "depth_um": field.depth_um, "center_um": field.center_um, "pixel_um": field.pixel_um,
        "rate_hz": facts.rate_hz,
        "z_scale": f"{PLANE_UM} micrometer is a placeholder: the field is one optical section",
        "volume_times": "../volume_times",
        "tiff_pages": f"page v * {count} + {field.number - 1} of the TIFF is volume v",
        "tiff_export": EXPORTS[export],
        "nwb": {"file": scan.nwb, "dandi_path": scan.nwb_path, **field.stated},
    }
    return ManifestGroup(arrays={"0": array},
                         attributes={"ome": ome, "tiff": tiff}
                         | image_annotation(fields, source_url=scan.tiffs[export]))


def field_paths(scans: list[ScanFacts]) -> list[str]:
    return [f"{SCANS}/{f.scan.name}/field-{field.number}" for f in scans for field in f.fields]


# One export
#
# Each export is one commit. The driver writes the root and the scans group in
# one session, forks the session once per scan, and runs each scan in its own
# worker process: parse the TIFF, write the scan's groups into the fork.
# Merging the forks and committing once keeps the workers from rebasing on
# each other. The collection's list of field images is written after the
# merge, from the scans that succeeded.
#
# The second export rewrites only the field images: same names, new chunk
# references, new dtype. Before writing, each worker checks three pages per
# scan (first, middle and last) byte for byte against the first export's, so
# the commit message's claim that the exports hold the same pixels is
# measured on every scan, not assumed from one.

def write_scan(fork, facts: ScanFacts, export: int, earlier: dict[int, int] | None):
    """Write one scan's groups for ``export`` into ``fork``. Runs in a worker process.

    ``earlier`` maps a few page numbers to their offsets in the first
    export's TIFF, when writing the second; those pages' bytes are compared.
    Returns the fork and a summary holding this export's offsets of the
    sample pages.
    """
    started = time.monotonic()
    url = facts.scan.tiffs[export]
    arrays, tiff = field_arrays(url, facts)
    pages = len(facts.volume_times) * len(facts.fields)
    sample = sample_offsets(arrays, facts, pages)
    if earlier is not None:
        compare_pages(facts.scan.tiffs[1], earlier, url, sample, arrays)
    base = f"{SCANS}/{facts.scan.name}"
    if export == 1:
        ManifestGroup(arrays={}, attributes=scan_attributes(facts)).to_icechunk(fork.store, group=base)
        write_volume_times(fork.store, base, facts)
    for field in facts.fields:
        field_group(arrays[field.number], field, facts, export, tiff).to_icechunk(
            fork.store, group=f"{base}/field-{field.number}", mode="w")
    summary = {"pages": pages, "fields": len(facts.fields), "sample": sample,
               "seconds": round(time.monotonic() - started, 1)}
    print(f"{facts.scan.name} {EXPORTS[export]['export']}: {len(facts.fields)} fields, {pages:,} pages "
          f"in {summary['seconds']} s", flush=True)
    return fork, summary


def sample_offsets(arrays: dict[int, ManifestArray], facts: ScanFacts, pages: int) -> dict[int, int]:
    """The offsets of the first, middle and last pages, by page number, from the field arrays."""
    count = len(facts.fields)
    out = {}
    for page in (0, pages // 2, pages - 1):
        volume, index = divmod(page, count)
        entry = arrays[facts.fields[index].number].manifest.get_entry((volume, 0, 0, 0))
        out[page] = int(entry["offset"])
    return out


def compare_pages(earlier_url: str, earlier: dict[int, int], url: str, offsets: dict[int, int],
                  arrays: dict[int, ManifestArray]) -> None:
    """Raise unless the sample pages hold the same bytes in both exports."""
    length = int(next(iter(arrays.values())).manifest.get_entry((0, 0, 0, 0))["length"])
    for page, offset in offsets.items():
        a = read_range(earlier_url, earlier[page], earlier[page] + length)
        b = read_range(url, offset, offset + length)
        if a != b:
            raise ValueError(f"page {page}: {earlier_url} and {url} hold different bytes")


MESSAGES = {
    1: ("MICrONS two-photon functional scans: {fields} fields of {scans} scans "
        "({pages:,} frames), referencing BossDB's v1 TIFF export (2021-07-02) in place. "
        "The v1 export declares its pixels uint16, and so does this commit."),
    2: ("Reference BossDB's v2 TIFF export (2023-01-13) for the same {fields} fields. "
        "The v2 files hold the same pixel bytes as v1, at other offsets, and declare them "
        "int16 where v1 declared uint16. The pixels are int16: the frames hold a few "
        "negative values, which v1 read as 65,431 to 65,535. Every field's dtype is now "
        "int16 and its chunks reference the v2 files. Checked on the first, middle and "
        "last page of each scan, byte for byte."),
}


def commit_export(repo, scans: list[ScanFacts], export: int, *, parallel: int,
                  earlier: dict[str, dict[int, int]] | None = None) -> dict[str, dict]:
    """Write one export of every scan as one commit on main, tag it, and return each scan's summary."""
    tag = EXPORTS[export]["export"]
    if tag in set(repo.list_tags()):
        raise ValueError(f"the repo already has a tag {tag}")
    session = repo.writable_session("main")
    zarr.open_group(session.store, mode="a").attrs.put(
        ROOT | root_attrs(VERSION) | {"tiff_export": EXPORTS[export]})
    zarr.open_group(session.store, path=SCANS, mode="a").attrs.put(scans_attributes(scans))
    jobs = {f.scan.name: (write_scan, (f, export, None if earlier is None else earlier[f.scan.name]))
            for f in scans}
    done, failed = run_forks(session, jobs, parallel=parallel,
                             what=f"writing the {tag} export of {len(scans)} scans, {parallel} at a time")
    if failed:
        raise RuntimeError(f"{len(failed)} scans failed, nothing committed: {failed}")
    paths = field_paths(scans)
    zarr.open_group(session.store, path="OME", mode="a").attrs.update(series_attrs(paths, VERSION))
    message = MESSAGES[export].format(fields=len(paths), scans=len(scans),
                                      pages=sum(s["pages"] for s in done.values()))
    with Heartbeat(lambda: f"committing the {tag} export", every=30.0):
        snapshot = commit(session, message, metadata={"tiff_export": EXPORTS[export]})
    repo.create_tag(tag, snapshot_id=snapshot)
    print(f"committed {snapshot} and tagged it {tag}", flush=True)
    return done


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="a directory, s3://bucket/prefix or arraylake://org/repo")
    ap.add_argument("--scans", help="scans to ingest, as session-scan, comma-separated; default all")
    ap.add_argument("--parallel", type=int, default=8, help="scans written at once, one process each")
    args = ap.parse_args(argv)
    # Both buckets are public, and requests signed with whatever credentials
    # the environment holds would be refused.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    started = time.monotonic()

    scans = list_scans()
    if args.scans:
        wanted = set(args.scans.split(","))
        if unknown := wanted - {s.name for s in scans}:
            raise SystemExit(f"no such scans: {sorted(unknown)}")
        scans = [s for s in scans if s.name in wanted]
    print(f"{len(scans)} scans: {', '.join(s.name for s in scans)}", flush=True)

    with ThreadPoolExecutor(8) as pool:
        facts = list(pool.map(read_nwb, scans))
    print(f"read the NWB metadata of {len(facts)} scans in {time.monotonic() - started:.0f} s", flush=True)

    repo = open_for_writing(args.repo, SOURCE)
    first = commit_export(repo, facts, 1, parallel=args.parallel)
    commit_export(repo, facts, 2, parallel=args.parallel,
                  earlier={name: summary["sample"] for name, summary in first.items()})
    print(f"done in {time.monotonic() - started:.0f} s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
