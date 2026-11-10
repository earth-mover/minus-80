"""Check the cpg0016-jump plates in a repo against load_data.csv, JUMP's tables and tifffile.

Runs the shared gates of ``minus80.ome.verify`` over the whole repo, and these
plate gates:

- on every field, from metadata alone: the plate holds exactly the fields its
  ``load_data.csv`` lists (or the subset its annotations say was ingested),
  each with that row's TIFFs in the plate's channel order; level 0's pixel
  size follows PLAN decision 5, recomputed from ``load_data.csv`` and
  ``microscope_config.csv``; a field has a translation exactly where
  ``load_data.csv`` states a position, and it is the one the position gives;
  each field's declared sources are the files its level 0 references; each
  well's perturbation is JUMP's, joined again here;
- each plate's illumination image holds the plate's channels, its dtype and
  shape are those the ``.npy`` headers state, and it references those files;
- on a sample, one field per plate plus every field holding a shortened LZW
  chunk: each chunk reference read back from the store with VirtualiZarr's
  ``IcechunkParser`` has the path and offset ``TiffParser`` gives, and its
  length, except where the plate lists the chunk as shortened, where it is the
  listed length; level 0 decoded through the store equals ``tifffile.imread``
  of every source TIFF, and level 1 equals the block mean of that decode.

Every field went through the same parse and the same decode pass, so the
sample stands for the rest, and verification reads a few TIFFs per plate.

    MINUS80_S3_ANONYMOUS=1 uv run --package cellpainting-gallery-ingest python \\
        ingests/cellpainting-gallery/verify.py --repo <icechunk dir> [--branch main]
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import tifffile
import zarr
from minus80.core import registry_for, remote
from minus80.core.fetch import download_then_index
from minus80.core.icechunk_io import open_repository
from minus80.core.progress import Progress
from minus80.ome import validate
from minus80.ome.verify import GATES, run_gates, series_matches
from minus80.parsers.tiff import TiffParser
from virtualizarr.parsers import IcechunkParser

import ingest
import jump
import plate as cpg


def find_plates(root) -> dict[str, zarr.Group]:
    """Every plate group under ``cpg0016-jump/<source>/<batch>/``, by path."""
    found = {}
    dataset = root[ingest.DATASET]
    for source in dataset.group_keys():
        for batch in dataset[source].group_keys():
            for name, group in dataset[f"{source}/{batch}"].groups():
                if "plate" in (dict(group.attrs).get("ome") or {}):
                    found[f"{ingest.DATASET}/{source}/{batch}/{name}"] = group
    return found


class Plate:
    """One plate in the store, beside what its sources say it should hold."""

    def __init__(self, path: str, group: zarr.Group, tables, registry):
        self.path, self.group = path, group
        self.notes = dict(group.attrs)["annotations"]
        self.source, self.batch, self.name = (self.notes[k] for k in ("source", "batch", "plate"))
        self.frame = cpg.read_load_data(download_then_index(self.notes["load_data_url"],
                                                            registry=registry))
        self.channels = cpg.plate_channels(self.frame)
        fields = cpg.read_fields(self.frame, self.channels)
        ingested = self.notes["ingested"]
        if isinstance(ingested, dict):
            wells, sites = set(ingested["wells"]), set(ingested["sites"])
            fields = [f for f in fields if f.well in wells and f.site in sites]
        self.fields = {f.path: f for f in fields}
        scope = jump.microscope(tables, self.source)
        self.pixel_um, _ = cpg.pixel_size(self.frame, float(scope["Metadata_Pixel_Size_Microns"]))
        self.perturbations, _ = jump.well_annotations(tables, self.source, self.name)
        self.listed = jump.plate_record(tables, self.source, self.batch, self.name) is not None
        self.shortened = {(c["field"], tuple(c["chunk"])): c
                          for c in self.notes["lzw_shortened_chunks"]["chunks"]}

    def sample(self) -> list[str]:
        """The first field, and every field holding a shortened chunk."""
        return sorted({next(iter(self.fields))} | {field for field, _ in self.shortened})


def complete(plates: list[Plate]) -> list[str]:
    problems = []
    for p in plates:
        wells = {w["path"] for w in dict(p.group.attrs)["ome"]["plate"]["wells"]}
        stored = {f"{w}/{i['path']}" for w in wells
                  for i in dict(p.group[w].attrs)["ome"]["well"]["images"]}
        if missing := set(p.fields) - stored:
            problems.append(f"{p.path}: {len(missing)} fields of load_data.csv absent, "
                            f"e.g. {sorted(missing)[:3]}")
        if extra := stored - set(p.fields):
            problems.append(f"{p.path}: {len(extra)} fields not in load_data.csv, "
                            f"e.g. {sorted(extra)[:3]}")
        for path in stored & set(p.fields):
            image = p.group[path]
            notes = dict(image.attrs)["annotations"]
            if notes["source_url"] != list(p.fields[path].urls):
                problems.append(f"{p.path}/{path}: source URLs differ from load_data.csv's row")
            if notes["channels"] != p.channels or image["0"].shape[0] != len(p.channels):
                problems.append(f"{p.path}/{path}: channels {notes['channels']}, "
                                f"level 0 {image['0'].shape}, plate {p.channels}")
    return problems


def pixel_size(plates: list[Plate]) -> list[str]:
    """Level 0's y and x pixel size, in ``ome`` and on the array, is PLAN decision 5's."""
    problems = []
    for p in plates:
        for path in p.fields:
            image = p.group[path]
            ms = dict(image.attrs)["ome"]["multiscales"][0]
            scale = ms["datasets"][0]["coordinateTransformations"][0]["scale"]
            stated = dict(image["0"].attrs)["scale"]
            if scale[1:] != [p.pixel_um] * 2 or [stated["y"], stated["x"]] != [p.pixel_um] * 2:
                problems.append(f"{p.path}/{path}: scale {scale}, array {stated}, "
                                f"expected {p.pixel_um}")
    return problems


def positions(plates: list[Plate]) -> list[str]:
    """A field has a translation exactly where load_data.csv states its position, and it is that position."""
    problems = []
    for p in plates:
        stated = cpg.has_positions(p.frame)
        for path, f in p.fields.items():
            image = p.group[path]
            transforms = dict(image.attrs)["ome"]["multiscales"][0]["datasets"][0][
                "coordinateTransformations"]
            got = next((t["translation"] for t in transforms if t["type"] == "translation"), None)
            if not stated:
                if got is not None:
                    problems.append(f"{p.path}/{path}: translation {got}, but no position stated")
                continue
            want = cpg.translation(f.record, image["0"].shape[1:], p.pixel_um)
            if got != [want["c"], want["y"], want["x"]]:
                problems.append(f"{p.path}/{path}: translation {got}, position gives {want}")
    return problems


def sources(plates: list[Plate]) -> list[str]:
    """Each field's declared sources are exactly the files its level 0 references."""
    problems = []
    for p in plates:
        for path in p.fields:
            image = p.group[path]
            declared = sorted(dict(image.attrs)["annotations"]["source_url"])
            referenced = dict(image["0"].attrs)["source_url"]
            if declared != sorted([referenced] if isinstance(referenced, str) else referenced):
                problems.append(f"{p.path}/{path}: annotations.source_url and level 0's "
                                f"source_url differ")
    return problems


def perturbations(plates: list[Plate]) -> list[str]:
    """Each well's perturbation fields are JUMP's at the pinned commit, joined again."""
    problems = []
    for p in plates:
        for well_path in {f.rsplit("/", 1)[0] for f in p.fields}:
            notes = dict(p.group[well_path].attrs)["annotations"]
            label = notes["well"]
            want = cpg.well_fields(label, p.perturbations, p.listed)
            if notes != want:
                problems.append(f"{p.path}/{well_path}: {notes} != {want}")
    return problems


def illumination(plates: list[Plate], root, registry) -> list[str]:
    problems = []
    for p in plates:
        where = p.notes["illumination"]
        if where not in root:
            problems.append(f"{p.path}: no illumination image ({where})")
            continue
        image = root[where]
        attrs = dict(image.attrs)
        try:
            validate(attrs["ome"], "0.5", model="image", strict=True)
        except Exception as exc:
            problems.append(f"{where}: {type(exc).__name__}: {exc}"[:120])
        notes = attrs["annotations"]
        folder = f"{ingest.PREFIX}{p.source}/images/{p.batch}/illum/{p.name}/"
        urls = [f"{folder}{p.name}_Illum{c}.npy" for c in p.channels]
        listing = cpg.list_objects(registry, folder)
        headers = [cpg.read_npy_header(u, listing[u], registry) for u in urls]
        array = image["0"]
        if notes["channels"] != p.channels or notes["source_url"] != urls:
            problems.append(f"{where}: channels or sources differ from the plate's")
        if array.shape != (len(urls), *headers[0].shape) or array.dtype != headers[0].dtype.newbyteorder("="):
            problems.append(f"{where}: {array.dtype} {array.shape}, headers state "
                            f"{headers[0].dtype} {headers[0].shape}")
    return problems


def references(plates: list[Plate], session, registry) -> list[str]:
    """Each sampled chunk's reference is TiffParser's, or the listed shortened length."""
    problems = []
    sampled = [(p, path) for p in plates for path in p.sample()]
    with Progress("checking chunk references against TiffParser", len(sampled),
                  unit="fields") as progress:
        for p, path in sampled:
            array = IcechunkParser(group=f"{p.path}/{path}").parse_session(
                session, registry, native_chunks_prefix="native:").group.arrays["0"]
            for c, url in enumerate(p.fields[path].urls):
                parsed = TiffParser()(url, registry).group.arrays["0"]
                rows = parsed.manifest.shape_chunk_grid[0]
                for s in range(rows):
                    got = array.manifest.get_entry((c, s, 0))
                    want = parsed.manifest.get_entry((s, 0))
                    short = p.shortened.get((path, (c, s, 0)))
                    length = short["referenced_bytes"] if short else int(want["length"])
                    if short and int(want["length"]) != short["stored_bytes"]:
                        problems.append(f"{p.path}/{path} chunk {(c, s, 0)}: listed as stored "
                                        f"{short['stored_bytes']} bytes, TiffParser says "
                                        f"{want['length']}")
                    if (got["path"], int(got["offset"]), int(got["length"])) != (
                            want["path"], int(want["offset"]), length):
                        problems.append(f"{p.path}/{path} chunk {(c, s, 0)}: store "
                                        f"{(got['offset'], got['length'])}, expected "
                                        f"{(want['offset'], length)} of {url}")
            progress.advance()
    print(f"    sampled {len(sampled)} fields, "
          f"{sum(len(p.shortened) for p in plates)} shortened chunks among them")
    return problems


def pixels(plates: list[Plate], registry) -> list[str]:
    """Level 0 through the store equals tifffile's decode; level 1 is its block mean."""
    problems = []
    sampled = [(p, path) for p in plates for path in p.sample()]
    with Progress("comparing sampled fields with tifffile", len(sampled), unit="fields") as progress:
        for p, path in sampled:
            image = p.group[path]
            stored = image["0"][:]
            overview = image["1"][:]
            for c, url in enumerate(p.fields[path].urls):
                with tifffile.TiffFile(remote.reader(url, registry=registry)) as tif:
                    reference = tif.asarray()
                if not np.array_equal(stored[c], reference):
                    problems.append(f"{p.path}/{path} channel {p.channels[c]}: level 0 differs "
                                    f"from tifffile's decode of {url}")
                if not np.array_equal(overview[c], cpg.block_mean(reference, cpg.OVERVIEW_FACTOR)):
                    problems.append(f"{p.path}/{path} channel {p.channels[c]}: level 1 is not "
                                    f"the block mean of tifffile's decode")
            progress.advance()
    return problems


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--no-pixels", action="store_true", help="skip the sampled pixel gate")
    args = ap.parse_args(argv)

    repo = open_repository(args.repo)
    session = repo.readonly_session(args.branch)
    root = zarr.open_group(session.store, mode="r")
    registry = registry_for(ingest.PREFIX + "x", patient=True)
    tables = jump.read_tables(jump.fetch_tables())
    plates = [Plate(path, group, tables, registry) for path, group in find_plates(root).items()]
    print(f"{len(plates)} plates, {sum(len(p.fields) for p in plates)} fields expected")

    # The illumination images are standalone images beside the plates, which
    # the spec gives no container to list, so the shared series gate runs per
    # plate. Its provenance gate lists every virtual chunk in the repo, which
    # on the full dataset is tens of millions; the sources gate below checks
    # each field's declared sources against its own manifest instead.
    shared = {k: v for k, v in GATES.items() if k not in ("series matches", "provenance")}
    gates = shared | {
        "series matches": lambda s, n: [f"{p.path}: {x}" for p in plates
                                        for x in series_matches(p.group)],
        "complete": lambda s, n: complete(plates),
        "pixel size": lambda s, n: pixel_size(plates),
        "positions": lambda s, n: positions(plates),
        "sources": lambda s, n: sources(plates),
        "perturbations": lambda s, n: perturbations(plates),
        "illumination": lambda s, n: illumination(plates, s, registry),
        "references": lambda s, n: references(plates, session, registry),
    }
    if not args.no_pixels:
        gates["pixels"] = lambda s, n: pixels(plates, registry)
    failed = run_gates(root, gates, {"version": "0.5", "sample": 3})
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
