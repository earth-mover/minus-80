"""Check an OpenCell store against the bucket and against tifffile's decode.

Runs the shared gates from ``minus80.ome.verify`` and these OpenCell gates:

- every field of view the bucket holds for the ingested genes is in the
  store: a listed line's field at its plate, well and field, with its
  projection nested and in ``projections/``; an unlisted line's field in
  ``unlisted/``;
- each stack's z scale equals the z step the OpenCell API states for it, and
  every image's y and x scale is the optics' pixel size, ``ingest.PIXEL_UM``;
- each well's Ensembl ID is the one its cell line's API record states;
- for one field per tree and z depth, with its projections: every chunk
  reference, read back from the store, has the path, offset and length of its
  plane's strip in tifffile's index of the file, every strip is
  uncompressed, and the file's dtype, byte order and shape match the array's;
- for the same fields, the middle z plane of each channel and the whole
  projection equal tifffile's decode.

The strips are uncompressed and one per chunk, so a chunk whose reference
matches tifffile's index decodes to the same pixels. Every field was parsed
from the same code path, so a sample covering each layout stands for the rest,
and verification reads a few megabytes of pixels rather than the archive.

    MINUS80_S3_ANONYMOUS=1 uv run --package opencell-ingest python ingests/opencell/verify.py \\
        --api <snapshot dir> --repo <icechunk dir>
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import tifffile
import zarr
from minus80.core import remote
from minus80.core.progress import Progress
from minus80.ome.hierarchy import images
from minus80.ome.verify import GATES, run_gates

import ingest


def _stacks(root) -> dict[str, dict]:
    """Each field's stack image, in the plates and the unlisted collection, with its annotations."""
    return {name: dict(image.attrs)["annotations"] for name, image in images(root).items()
            if name.startswith((f"{ingest.PLATES}/", f"{ingest.UNLISTED}/"))
            and not name.endswith(f"/{ingest.PROJ}")}


def complete(root, bucket, lines, fov_records, genes: set[str] | None) -> list[str]:
    """Every field of ``genes`` (default every gene in the bucket) is in the store, with its projection in both places."""
    problems = []
    for fov in bucket:
        if genes is not None and fov.gene not in genes:
            continue
        name = ingest.field_name(fov)
        if fov.cid in lines:
            well = ingest.place(fov_records[fov.fid]).well
            paths = [f"{ingest.PLATES}/{well}/{name}", f"{ingest.PLATES}/{well}/{name}/{ingest.PROJ}",
                     f"{ingest.PROJECTIONS}/{well}/{name}"]
        else:
            path = f"{ingest.UNLISTED}/{fov.gene}/CID{fov.cid:06d}/{name}"
            paths = [path, f"{path}/{ingest.PROJ}"]
        problems += [f"{p}: FID {fov.fid} is in the bucket, not in the store"
                     for p in paths if p not in root]
    return problems


def z_step(root) -> list[str]:
    """Each listed stack's z scale is the API's z step."""
    problems = []
    for name, fields in _stacks(root).items():
        if "z_step_size" not in fields:
            continue
        scale = dict(root[f"{name}/0"].attrs)["scale"]["z"]
        if fields["z_step_size"] != scale:
            problems.append(f"{name}: z scale {scale}, API z_step_size {fields['z_step_size']}")
    return problems


def pixel_size(root) -> list[str]:
    """Every image's y and x scale is the pixel size the optics give, in the array and in ome."""
    problems = []
    for name, image in images(root).items():
        ms = dict(image.attrs)["ome"]["multiscales"][0]
        axes = [a["name"] for a in ms["axes"]]
        scale = dict(zip(axes, ms["datasets"][0]["coordinateTransformations"][0]["scale"]))
        stated = dict(image["0"].attrs)["scale"]
        for axis in ("y", "x"):
            if scale[axis] != ingest.PIXEL_UM or stated[axis] != ingest.PIXEL_UM:
                problems.append(f"{name}: {axis} scale {scale[axis]} (array {stated[axis]}), "
                                f"expected {ingest.PIXEL_UM}")
    return problems


def ensembl(root, lines) -> list[str]:
    problems = []
    for name in {n.rsplit("/", 1)[0] for n in _stacks(root) if n.startswith(f"{ingest.PLATES}/")}:
        fields = dict(root[name].attrs)["annotations"]
        stated = lines[fields["cell_line_id"]]["metadata"]["ensg_id"]
        if fields["ensg_id"] != stated:
            problems.append(f"{name}: ensg_id {fields['ensg_id']}, API says {stated}")
    return problems


def _decode(url: str) -> np.ndarray:
    with tifffile.TiffFile(remote.reader(url)) as tif:
        return tif.asarray()


def _strips(url: str) -> tuple[tifffile.TiffPageSeries, list[tuple[int, int]]]:
    """The file's series and each page's single strip, as (offset, length)."""
    with tifffile.TiffFile(remote.reader(url)) as tif:
        series = tif.series[0]
        strips = []
        for page in series.pages:
            if len(page.dataoffsets) != 1 or page.compression != 1:
                raise ValueError(f"{url}: page is not one uncompressed strip")
            strips.append((int(page.dataoffsets[0]), int(page.databytecounts[0])))
        return series, strips


def references(root, session, workers: int) -> list[str]:
    from minus80.core import registry_for
    from virtualizarr.parsers import IcechunkParser

    registry = registry_for(ingest.PREFIX + "x")
    names = []
    for name in pixel_sample(root):
        names += [name, f"{name}/{ingest.PROJ}"]
        if name.startswith(f"{ingest.PLATES}/"):
            names.append(f"{ingest.PROJECTIONS}/{name.removeprefix(f'{ingest.PLATES}/')}")
    named = {name: dict(root[name].attrs) for name in names}

    def check(item) -> list[str]:
        name, attrs = item
        url = (attrs.get("annotations") or {})["source_url"]
        array = IcechunkParser(group=name).parse_session(
            session, registry, native_chunks_prefix="native:").group.arrays["0"]
        manifest, dims = array.manifest, array.metadata.dimension_names
        series, strips = _strips(url)
        found = []
        if series.dtype != array.dtype or series.dtype.byteorder not in "<=|":
            found.append(f"{name}: dtype {series.dtype}, array {array.dtype}")
        if dict(zip(series.axes.lower(), series.shape)) != dict(zip(dims, array.shape)):
            found.append(f"{name}: TIFF {series.axes} {series.shape}, array {dims} {array.shape}")
        # ImageJ hyperstack pages run in the stored axis order with the last
        # two axes inside each page, so a page's index is its position in the
        # stored grid of the leading axes.
        lead = [a for a in series.axes.lower()[:-2]]
        grid = [series.shape[i] for i in range(len(lead))]
        for page, (offset, length) in enumerate(strips):
            where = dict(zip(lead, np.unravel_index(page, grid)))
            key = tuple(int(where.get(d, 0)) for d in dims)
            got = (manifest._paths[key], int(manifest._offsets[key]), int(manifest._lengths[key]))
            if got != (url, offset, length):
                found.append(f"{name} chunk {key}: {got[1:]} from {got[0][-40:]}, TIFF strip {(offset, length)}")
                break
        return found

    problems = []
    with Progress("checking chunk references against tifffile", len(named), unit="images") as progress, \
            ThreadPoolExecutor(workers) as pool:
        for found in pool.map(check, named.items()):
            problems += found
            progress.advance()
    return problems


def pixel_sample(root) -> list[str]:
    """One stack per tree and (slices, z step)."""
    picked = {}
    for name in _stacks(root):
        shape = root[f"{name}/0"].shape
        scale = dict(root[f"{name}/0"].attrs)["scale"]["z"]
        picked.setdefault((name.split("/")[0], shape[1], scale), name)
    return sorted(picked.values())


def pixels(root) -> list[str]:
    problems = []
    sample = pixel_sample(root)
    with Progress("comparing sampled pixels with tifffile", len(sample), unit="fields of view") as progress:
        for name in sample:
            stack = root[f"{name}/0"]
            z = stack.shape[1] // 2
            url = dict(root[name].attrs)["annotations"]["source_url"]
            # ImageJ stores ZCYX one plane per page, so plane (z, c) is page 2z + c.
            with tifffile.TiffFile(remote.reader(url)) as tif:
                planes = tif.asarray(key=[2 * z, 2 * z + 1])
            if not np.array_equal(stack[:, z], planes):
                problems.append(f"{name}: plane z={z} differs from tifffile's decode of {url}")
            proj_name = f"{name}/{ingest.PROJ}"
            proj = root[f"{proj_name}/0"][:]
            url = dict(root[proj_name].attrs)["annotations"]["source_url"]
            if not np.array_equal(proj, _decode(url)):
                problems.append(f"{proj_name}: differs from tifffile's decode of {url}")
            progress.advance()
    print(f"    sampled {len(sample)} fields: {', '.join(sample)}")
    return problems


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", type=Path, required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--genes", help="comma-separated gene symbols the store was built from; default every gene")
    ap.add_argument("--no-pixels", action="store_true", help="skip the sampled pixel gate")
    args = ap.parse_args(argv)

    session = ingest.open_readonly(args.repo)
    root = zarr.open_group(session.store, mode="r")
    lines, fov_records = ingest.load_api(args.api)
    bucket = ingest.list_bucket()
    print(f"{len(images(root))} images in the store")
    # Every image has one level, so there are no levels to align, and the
    # sampled reference gate checks each source_url against its chunks without
    # listing every virtual chunk in the store.
    shared = {k: v for k, v in GATES.items() if k not in ("levels aligned", "provenance")}
    gates = shared | {
        "complete": lambda s, n: complete(s, bucket, lines, fov_records,
                                          set(args.genes.split(",")) if args.genes else None),
        "z step": lambda s, n: z_step(s),
        "pixel size": lambda s, n: pixel_size(s),
        "ensembl id": lambda s, n: ensembl(s, lines),
        "references": lambda s, n: references(s, session, args.workers),
    }
    if not args.no_pixels:
        gates["pixels"] = lambda s, n: pixels(s)
    failed = run_gates(root, gates, {"version": ingest.OME_VERSION, "sample": 3})
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
