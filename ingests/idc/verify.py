"""Check a built IDC slide store against IDC's catalog and a sample of its source objects.

Gates on every node, from the store's own metadata (no source requests):

- **completeness**: every slide idc-index lists for the collection is in the
  store or named in the root's ``left_out``, and the store holds no other;
- **codecs**: every array's codecs are among those the root's ``codecs``
  names, each registered by zarr or imagecodecs-zarr, so no reader needs code
  from this repository;
- **metadata**: every slide has an ``image`` of two or more levels in
  micrometers, largest first, with a pixel size other than 1, and a
  ``dicom`` document; one image, thumbnail and overview validate against the
  OME-Zarr 0.6 schema, since the ingest writes every one the same way.

Gates on ``--sample`` slides, a few requests each to the bucket:

- **pixels**: one random tile of a random level equals imagecodecs' decode of
  the same DICOM fragment, located from the instance's header and offset
  table; the thumbnail equals pydicom's decode of its instance;
- **pins** (``--pins``): the first chunk of every virtual array carries the
  ETag the bucket listing reports for its object. Read with ``core-drill
  chunk-ref``, since Icechunk's Python API does not expose a stored
  reference.

    uv run --package idc-ingest python ingests/idc/verify.py REPO [--sample 5] [--pins]
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import time

import imagecodecs
import numpy as np
import zarr
from minus80.core.icechunk_io import open_repo
from minus80.ome.image import validate
from minus80.parsers.dicom import encapsulated_frame_ranges
from pydicom.pixels import pixel_array

from ingest import catalog
from plumbing import Source, local, say


def check_store(root: zarr.Group, listed: set[str], partial: bool) -> tuple[list[str], dict[str, str]]:
    """The gates on every node, over one concurrent walk of the hierarchy."""
    failures = []
    nodes = dict(root.members(max_depth=None))
    stored = {path.rsplit("/", 1)[1]: path for path, node in nodes.items()
              if isinstance(node, zarr.Group) and path.count("/") == 2}
    left_out = set(root.attrs.get("left_out", {}))
    if (missing := listed - set(stored) - left_out) and not partial:
        failures.append(f"completeness: {len(missing)} listed slides neither stored nor left out, "
                        f"e.g. {sorted(missing)[:3]}")
    if extra := set(stored) - listed:
        failures.append(f"completeness: {len(extra)} stored slides idc-index does not list, e.g. {sorted(extra)[:3]}")

    allowed = set(root.attrs["codecs"])
    validated = set()
    for path, node in nodes.items():
        if isinstance(node, zarr.Array):
            if bad := {c["name"] for c in node.metadata.to_dict()["codecs"]} - allowed:
                failures.append(f"codecs: {path} uses {sorted(bad)}")
        elif path.count("/") == 3 and (kind := path.rsplit("/", 1)[1]) not in validated:
            validate(node.attrs["ome"], "0.6", model="image", strict=True)
            validated.add(kind)
    for path in stored.values():
        if f"{path}/dicom" not in nodes:
            failures.append(f"{path}: no dicom document")
        multiscale = nodes[f"{path}/image"].attrs["ome"]["multiscales"][0]
        units = {a["name"]: a.get("unit") for a in multiscale["coordinateSystems"][0]["axes"]}
        scale = multiscale["datasets"][0]["coordinateTransformations"][0]["transformations"][0]["scale"]
        shapes = [nodes[f"{path}/image/{d['path']}"].shape for d in multiscale["datasets"]]
        if len(shapes) < 2 or shapes != sorted(shapes, key=lambda s: -s[1]):
            failures.append(f"{path}: levels {shapes}")
        if units.get("y") != "micrometer" or 1.0 in scale[1:]:
            failures.append(f"{path}: pixel size {scale} {units}")
    return failures, stored


def check_pixels(root: zarr.Group, path: str, source: Source, rng: random.Random) -> list[str]:
    failures = []
    slide = root[path]
    image = slide["image"]
    k = rng.randrange(len(list(image.array_keys())))
    level = image[str(k)]
    ty, tx = level.chunks[1:]
    rows, cols = -(-level.shape[1] // ty), -(-level.shape[2] // tx)
    r, c = rng.randrange(rows), rng.randrange(cols)
    url = level.attrs["source_url"]
    reader = source.reader(url)
    offset, length = encapsulated_frame_ranges(reader)[r * cols + c]
    reader.seek(offset)
    decode = imagecodecs.jpeg2k_decode if level.attrs["transfer_syntax"].endswith(".91") else imagecodecs.jpeg8_decode
    expected = decode(reader.read(length)).transpose(2, 0, 1)
    stored = level[:, r * ty:(r + 1) * ty, c * tx:(c + 1) * tx]
    if not np.array_equal(stored, expected[:, :stored.shape[1], :stored.shape[2]]):
        failures.append(f"pixels: {path}/image/{k} tile ({r}, {c}) differs from its DICOM fragment")
    if "thumbnail" in slide:
        thumbnail = slide["thumbnail/0"]
        reference = pixel_array(local(thumbnail.attrs["source_url"]), index=0)
        if not np.array_equal(thumbnail[:], reference.transpose(2, 0, 1)):
            failures.append(f"pixels: {path}/thumbnail differs from pydicom's decode")
    return failures


def check_pins(repo: str, root: zarr.Group, path: str, source: Source) -> list[str]:
    failures = []
    slide = root[path]
    etags = {url: etag for url, _, etag in source.objects(slide.attrs["idc"]["crdc_series_uuid"])}
    for node in slide.group_keys():
        for name, array in slide[node].arrays():
            out = subprocess.run(["core-drill", repo, "--output", "json", "chunk-ref", f"/{path}/{node}/{name}",
                                  ",".join("0" * len(array.shape))], capture_output=True, text=True, check=True)
            ref = json.loads(out.stdout)["location"]["chunk_ref"]
            listed = etags.get(array.attrs["source_url"])
            if ref.get("checksum_etag") != listed:
                failures.append(f"pins: {path}/{node}/{name} pinned to {ref.get('checksum_etag')!r}, "
                                f"the bucket lists {listed!r}")
    return failures


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("repo")
    p.add_argument("--collection", default="gtex")
    p.add_argument("--sample", type=int, default=5)
    p.add_argument("--pins", action="store_true", help="check ETag pins with core-drill")
    p.add_argument("--partial", action="store_true",
                   help="a trial store of some slides: skip the check that every listed slide is present")
    a = p.parse_args()
    began = time.monotonic()
    root = open_repo(a.repo)
    failures, stored = check_store(root, {s['slide'] for s in catalog(a.collection)}, a.partial)
    say(f"store gates over {len(stored)} slides: {len(failures)} failures, {time.monotonic() - began:.0f} s")
    source = Source()
    rng = random.Random(0)
    for path in rng.sample(sorted(stored.values()), min(a.sample, len(stored))):
        failures += check_pixels(root, path, source, rng)
        if a.pins:
            failures += check_pins(a.repo, root, path, source)
    for f in failures:
        say(f"FAIL {f}")
    say(f"{len(stored)} slides, {a.sample} sampled{' with pins' if a.pins else ''}: "
        f"{len(failures)} failures in {time.monotonic() - began:.0f} s")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
