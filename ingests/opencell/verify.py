"""Check an OpenCell store against the bucket and against tifffile's decode.

Runs the shared gates from ``minus80.ome.verify`` and these OpenCell gates:

- every published field of view the bucket holds for the ingested genes has a
  stack and a projection at its plate, well and field;
- each stack's z scale equals the z step the OpenCell API states for it;
- each well's Ensembl ID is the one its cell line's API record states;
- every chunk reference, read back from the store, has the path, offset and
  length of its plane's strip in tifffile's index of the file, and the file's
  dtype, byte order, compression and shape match the array's;
- the pixels of one stack and one projection per depth equal tifffile's
  decode, and each sampled projection equals its stack's maximum over z.

The strips are uncompressed and one per chunk, so a chunk whose reference
matches tifffile's index decodes to the same pixels; the reference gate covers
every file by reading only its directories. The pixel gate checks the codec
chain once per distinct layout, so verification never reads the whole archive.

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
from minus80.core.icechunk_io import open_repository
from minus80.core.progress import Progress
from minus80.ome.hierarchy import images
from minus80.ome.verify import GATES, run_gates

import ingest


def _fields(root, kind: str) -> dict[str, dict]:
    """Each field image of one tree, with its annotations."""
    prefix = ingest.TREES[kind] + "/"
    return {name: dict(image.attrs)["annotations"]
            for name, image in images(root).items() if name.startswith(prefix)}


def complete(root, bucket, lines, fov_records, kinds) -> list[str]:
    genes = {a["gene"] for a in _fields(root, "stack").values()}
    problems = []
    for fov in bucket:
        if fov.gene not in genes or fov.cid not in lines:
            continue
        well = ingest.place(fov_records[fov.fid]).well
        for kind in kinds:
            path = f"{ingest.TREES[kind]}/{well}/{ingest.field_name(fov)}"
            if path not in root:
                problems.append(f"{path}: FID {fov.fid} is in the bucket, not in the store")
    return problems


def z_step(root) -> list[str]:
    problems = []
    for name, fields in _fields(root, "stack").items():
        scale = dict(root[f"{name}/0"].attrs)["scale"]["z"]
        if fields["z_step_size"] != scale:
            problems.append(f"{name}: z scale {scale}, API z_step_size {fields['z_step_size']}")
    return problems


def ensembl(root, lines) -> list[str]:
    problems = []
    for name in {n.rsplit("/", 1)[0] for n in _fields(root, "stack")}:
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
    named = {name: dict(image.attrs) for name, image in images(root).items()}

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
    """One stack per (slices, z step), each with its projection."""
    picked = {}
    for name in _fields(root, "stack"):
        shape = root[f"{name}/0"].shape
        scale = dict(root[f"{name}/0"].attrs)["scale"]["z"]
        picked.setdefault((shape[1], scale), name)
    return sorted(picked.values())


def pixels(root) -> list[str]:
    problems = []
    sample = pixel_sample(root)
    with Progress("comparing sampled pixels with tifffile", len(sample), unit="fields of view") as progress:
        for name in sample:
            stack = root[f"{name}/0"][:]
            url = dict(root[name].attrs)["annotations"]["source_url"]
            if not np.array_equal(stack, _decode(url).transpose(1, 0, 2, 3)):
                problems.append(f"{name}: differs from tifffile's decode of {url}")
            proj_name = ingest.TREES["proj"] + name.removeprefix(ingest.TREES["stack"])
            if proj_name in root:
                proj = root[f"{proj_name}/0"][:]
                url = dict(root[proj_name].attrs)["annotations"]["source_url"]
                if not np.array_equal(proj, _decode(url)):
                    problems.append(f"{proj_name}: differs from tifffile's decode of {url}")
                if not np.array_equal(proj, stack.max(axis=1)):
                    problems.append(f"{proj_name}: is not the stack's maximum over z")
            progress.advance()
    print(f"    sampled {len(sample)} fields: {', '.join(sample)}")
    return problems


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", type=Path, required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-pixels", action="store_true", help="skip the sampled pixel gate")
    args = ap.parse_args(argv)

    repo = open_repository(args.repo)
    session = repo.readonly_session("main")
    root = zarr.open_group(session.store, mode="r")
    lines, fov_records = ingest.load_api(args.api)
    bucket = ingest.list_bucket()
    kinds = [k for k in ingest.TREES if ingest.TREES[k] in root]
    print(f"{len(images(root))} images in the store, trees {kinds}")
    gates = dict(GATES) | {
        "complete": lambda s, n: complete(s, bucket, lines, fov_records, kinds),
        "z step": lambda s, n: z_step(s),
        "ensembl id": lambda s, n: ensembl(s, lines),
        "references": lambda s, n: references(s, session, args.workers),
    }
    if not args.no_pixels:
        gates["pixels"] = lambda s, n: pixels(s)
    failed = run_gates(root, gates, {"version": "0.5", "sample": 3})
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
