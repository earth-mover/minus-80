"""Check cpg0016-jump plates in a repo against load_data.csv, JUMP's tables, tifffile and numpy.

Runs the shared gates of ``minus80.ome.verify`` on each chosen plate, and
these plate gates:

- on every field, from metadata alone: the plate holds exactly the fields its
  ``load_data.csv`` lists (or the subset its annotations say was ingested),
  each declaring that row's TIFFs in the plate's channel order; level 0's
  pixel size follows PLAN decision 5, recomputed from ``load_data.csv`` and
  ``microscope_config.csv``; a field has a translation exactly where
  ``load_data.csv`` states a position, and it is the one the position gives;
- on every well: its perturbation is JUMP's, joined again by
  ``jump.well_annotations``, and the plate's counts of controls, empty wells,
  treatments and compounds equal those of a plain pandas merge of
  ``well.csv.gz`` with ``perturbation_control.csv`` and ``compound.csv.gz``,
  which shares no code with the ingest's join;
- each plate's illumination image holds the plate's channels, its dtype and
  shape are those the ``.npy`` headers state, it references those files, and
  one channel decoded through the store equals ``numpy.load`` of its file. A
  plate without one must say so, and its illumination folder must indeed
  lack a channel's file;
- on a sample, one field per plate plus every field holding a shortened LZW
  chunk on one plate: the chunk references read back from the store with
  VirtualiZarr's ``IcechunkParser`` point, plane by plane, into the TIFF
  ``load_data.csv`` names for that channel, with the path and offset
  ``TiffParser`` gives and its length, except where the plate lists the chunk
  as shortened, where it is the listed length; level 0 decoded through the
  store equals ``tifffile.imread`` of every source TIFF, and level 1 equals
  the block mean of that decode.

Every field went through the same parse and the same decode pass, so the
sample stands for the rest, and verification reads a few TIFFs per plate.

``--stage-axes`` adds a check of the stage axes' directions on plates whose
``load_data.csv`` states positions: neighboring fields of a well, placed by
their stored translations, must continue across their shared edge better
than they would with the axis flipped. It decodes every field of the wells it
checks at full resolution, so it is opt-in.

    MINUS80_S3_ANONYMOUS=1 uv run --package cellpainting-gallery-ingest python \\
        ingests/cellpainting-gallery/verify.py --repo <icechunk dir> [--branch main] \\
        [--source source_4 --batch 2021_04_26_Batch1 --plate BR00117035] [--stage-axes]
"""

from __future__ import annotations

import argparse
import io
import math
import sys
import time
from collections import Counter

import numpy as np
import tifffile
import zarr
from minus80.core import registry_for, remote
from minus80.core.fetch import download_then_index, list_objects, read_range
from minus80.core.icechunk_io import open_repository
from minus80.core.progress import Progress
from minus80.ome import validate
from minus80.ome.verify import GATES, run_gates, series_matches
from minus80.parsers.tiff import TiffParser
from virtualizarr.parsers import IcechunkParser

import ingest
import jump
import plate as cpg


def find_plates(root, source: str | None, batch: str | None,
                name: str | None) -> dict[str, zarr.Group]:
    """The plate groups under ``cpg0016-jump/<source>/<batch>/``, by path, narrowed by each filter given."""
    found = {}
    dataset = root[ingest.DATASET]
    for s in sorted(dataset.group_keys()):
        if source and s != source:
            continue
        for b in sorted(dataset[s].group_keys()):
            if batch and b != batch:
                continue
            for n, group in sorted(dataset[f"{s}/{b}"].groups()):
                if name and n != name:
                    continue
                if "plate" in (dict(group.attrs).get("ome") or {}):
                    found[f"{ingest.DATASET}/{s}/{b}/{n}"] = group
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
        self.manifests: dict[str, object] = {}

    def level0_manifest(self, path: str, session, registry):
        """Level 0 of field ``path`` as the store's manifest holds it, read once."""
        if path not in self.manifests:
            self.manifests[path] = IcechunkParser(group=f"{self.path}/{path}").parse_session(
                session, registry, native_chunks_prefix="native:").group.arrays["0"]
        return self.manifests[path]


def sampled(plates: list[Plate]) -> list[tuple[Plate, str]]:
    """One field per plate, and every field holding a shortened chunk on the first plate with any.

    One plate's shortened chunks cover the shortening rule; the rest of the
    plates' shortened chunks went through the same code (PLAN, verify.py).
    """
    chosen = {(p.path, next(iter(p.fields))): p for p in plates}
    if with_short := next((p for p in plates if p.shortened), None):
        for field, _ in with_short.shortened:
            chosen[(with_short.path, field)] = with_short
    return [(p, field) for (_, field), p in sorted(chosen.items(), key=lambda kv: kv[0])]


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
            level0 = p.group[f"{path}/0"]
            attrs = dict(level0.attrs)
            if attrs["source_url"] != list(p.fields[path].urls):
                problems.append(f"{p.path}/{path}: level 0's source_url is not load_data.csv's "
                                f"row in channel order")
            if attrs["channel_names"] != p.channels or level0.shape[0] != len(p.channels):
                problems.append(f"{p.path}/{path}: channels {attrs['channel_names']}, "
                                f"level 0 {level0.shape}, plate {p.channels}")
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


def sources(plates: list[Plate], session, registry) -> list[str]:
    """Each sampled field's stored manifest points plane by plane into load_data.csv's TIFF for that channel.

    Read from the manifests themselves, not from the attributes, which the
    ingest wrote from the same values it built the manifests from.
    """
    problems = []
    for p, path in sampled(plates):
        array = p.level0_manifest(path, session, registry)
        urls = p.fields[path].urls
        rows, columns = array.manifest.shape_chunk_grid[1:]
        for c, url in enumerate(urls):
            paths = {array.manifest.get_entry((c, s, x))["path"]
                     for s in range(rows) for x in range(columns)}
            if paths != {url}:
                problems.append(f"{p.path}/{path} channel {p.channels[c]}: chunks reference "
                                f"{sorted(paths)[:2]}, load_data.csv names {url}")
    print(f"    {len(sampled(plates))} sampled fields' manifests read")
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


def perturbation_counts(plates: list[Plate], tables) -> list[str]:
    """Each plate's counts of negcon, poscon, empty and trt wells, and of compounds, equal a plain pandas merge's.

    The merge is the one a reader would write: the plate's rows of
    ``well.csv.gz``, left-joined to ``perturbation_control.csv`` and to
    ``compound.csv.gz`` on ``Metadata_JCP2022``. A well with no row counts as
    ``absent``, a row with no identifier as ``none``, and an identifier the
    control table does not list as ``trt``.
    """
    wells = tables["well.csv.gz"]
    control = tables["perturbation_control.csv"][["Metadata_JCP2022", "Metadata_pert_type"]]
    compound = tables["compound.csv.gz"][["Metadata_JCP2022", "Metadata_InChIKey"]]
    problems = []
    for p in plates:
        labels = sorted({f.well for f in p.fields.values()})
        rows = wells[(wells["Metadata_Source"] == p.source) & (wells["Metadata_Plate"] == p.name)
                     & wells["Metadata_Well"].isin(labels)]
        merged = (rows.merge(control, on="Metadata_JCP2022", how="left")
                  .merge(compound, on="Metadata_JCP2022", how="left"))
        want = Counter("none" if not jcp else (kind if isinstance(kind, str) else "trt")
                       for jcp, kind in zip(merged["Metadata_JCP2022"],
                                            merged["Metadata_pert_type"]))
        want["absent"] = len(set(labels) - set(rows["Metadata_Well"]))
        want_compounds = int(merged["Metadata_InChIKey"].fillna("").ne("").sum())
        got, got_compounds = Counter(), 0
        for well_path in {f.rsplit("/", 1)[0] for f in p.fields}:
            notes = dict(p.group[well_path].attrs)["annotations"]
            if "perturbation" in notes:
                got["absent"] += 1
                continue
            got[notes["control_type"] or notes["pert_type"] or "none"] += 1
            got_compounds += notes["inchikey"] is not None
        if +got != +want or got_compounds != want_compounds:
            problems.append(f"{p.path}: wells say {dict(+got)} and {got_compounds} compounds; "
                            f"pandas merge gives {dict(+want)} and {want_compounds}")
    return problems


def illumination(plates: list[Plate], root, registry) -> list[str]:
    """Each plate's illumination image is its .npy files, or the plate says why it has none."""
    problems = []
    for p in plates:
        where = p.notes["illumination"]
        folder = f"{ingest.PREFIX}{p.source}/images/{p.batch}/illum/{p.name}/"
        listing = list_objects(folder, registry=registry)
        urls = [f"{folder}{p.name}_Illum{c}.npy" for c in p.channels]
        absent = [u.rsplit("/", 1)[-1] for u in urls if u not in listing]
        if where.startswith("not written"):
            # Declared missing: the declaration must be true, and must not
            # hide an image that was written anyway.
            if not absent:
                problems.append(f"{p.path}: says {where!r}, but the folder holds every channel's file")
            elif not all(name in where for name in absent):
                problems.append(f"{p.path}: says {where!r}; the folder lacks {absent}")
            if f"{p.path.rsplit('/', 1)[0]}/illum/{p.name}" in root:
                problems.append(f"{p.path}: says no illumination image, but one is in the store")
            continue
        if where not in root:
            problems.append(f"{p.path}: no illumination image at {where}")
            continue
        if absent:
            problems.append(f"{where}: the folder lacks {absent}")
            continue
        image = root[where]
        attrs = dict(image.attrs)
        try:
            validate(attrs["ome"], "0.5", model="image", strict=True)
        except Exception as exc:
            problems.append(f"{where}: {type(exc).__name__}: {exc}"[:120])
        notes = attrs["annotations"]
        headers = [cpg.read_npy_header(u, listing[u], registry) for u in urls]
        array = image["0"]
        if notes["channels"] != p.channels or notes["source_url"] != urls:
            problems.append(f"{where}: channels or sources differ from the plate's")
        if array.shape != (len(urls), *headers[0].shape) or array.dtype != headers[0].dtype.newbyteorder("="):
            problems.append(f"{where}: {array.dtype} {array.shape}, headers state "
                            f"{headers[0].dtype} {headers[0].shape}")
            continue
        # One channel per plate, a different one from plate to plate.
        c = plates.index(p) % len(urls)
        reference = np.load(io.BytesIO(read_range(urls[c], 0, listing[urls[c]],
                                                  registry=registry)))
        if not np.array_equal(array[c], reference):
            problems.append(f"{where} channel {p.channels[c]}: differs from numpy.load of "
                            f"{urls[c]}")
    return problems


def references(plates: list[Plate], session, registry) -> list[str]:
    """Each sampled chunk's reference is TiffParser's, or the listed shortened length."""
    problems = []
    fields = sampled(plates)
    with Progress("checking chunk references against TiffParser", len(fields),
                  unit="fields") as progress:
        for p, path in fields:
            array = p.level0_manifest(path, session, registry)
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
    short_fields = {(p.path, f) for p in plates for f, _ in p.shortened}
    print(f"    sampled {len(fields)} fields, "
          f"{sum(1 for p, f in fields if (p.path, f) in short_fields)} of them with shortened "
          f"chunks")
    return problems


def pixels(plates: list[Plate], registry) -> list[str]:
    """Level 0 through the store equals tifffile's decode; level 1 is its block mean."""
    problems = []
    fields = sampled(plates)
    with Progress("comparing sampled fields with tifffile", len(fields), unit="fields") as progress:
        for p, path in fields:
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


# The stage axes
#
# load_data.csv records each field's stage position but not which way the
# stage axes run on the image. Where two fields touch, the true arrangement
# makes their shared edge continuous: the last column of the left field
# matches the first column of the right one. For every touching pair the
# stored translations imply, the edge correlation of the stored arrangement
# is compared with the one the flipped axis would give, over a few
# illumination-corrected channels, and a one-sided sign test is run per axis.

#: Channels whose texture runs across a field edge: cytoplasm, ER and the
#: brightfield plane, where present. DNA is mostly background between nuclei.
STAGE_CHANNELS = ("AGP", "ER", "Brightfield")

#: The sign test's significance level; an axis that does not reach it is
#: inconclusive, which the check reports as a problem.
STAGE_ALPHA = 0.01


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a - a.mean(), b - b.mean()
    denominator = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / denominator) if denominator else 0.0


def sign_test(wins: int, n: int) -> float:
    """The one-sided binomial p-value of ``wins`` or more of ``n`` at even odds."""
    return sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n


def touching_pairs(origins: dict[str, tuple[float, float]], extent: tuple[float, float]):
    """``(axis, first, second, step)`` for fields that abut or overlap, ``first`` above or to the left.

    ``step`` is the distance between their origins. Opera Phenix fields abut
    (step = extent); Operetta fields overlap by about 2% (source_11).
    """
    for a, (ya, xa) in origins.items():
        for b, (yb, xb) in origins.items():
            if abs(ya - yb) < extent[1] * 0.01 and extent[1] * 0.8 < xb - xa < extent[1] * 1.01:
                yield "x", a, b, xb - xa
            if abs(xa - xb) < extent[0] * 0.01 and extent[0] * 0.8 < yb - ya < extent[0] * 1.01:
                yield "y", a, b, yb - ya


def stage_axes(plates: list[Plate], root) -> list[str]:
    """On plates with positions, each axis's stored direction continues field edges better than its flip."""
    problems = []
    for p in plates:
        if not cpg.has_positions(p.frame):
            print(f"    {p.path}: load_data.csv states no positions; not checked")
            continue
        where = p.notes["illumination"]
        written = not where.startswith("not written") and where in root
        illum = root[f"{where}/0"][:].astype(float) if written else None
        if illum is not None:
            illum /= illum.mean(axis=(1, 2), keepdims=True)
        channels = [p.channels.index(c) for c in STAGE_CHANNELS if c in p.channels]
        wins = {"x": [0, 0], "y": [0, 0]}
        by_well: dict[str, list[str]] = {}
        for path in p.fields:
            by_well.setdefault(path.rsplit("/", 1)[0], []).append(path)
        for members in by_well.values():
            origins, planes = {}, {}
            for path in members:
                base = dict(p.group[path].attrs)["ome"]["multiscales"][0]["datasets"][0]
                transforms = {t["type"]: t for t in base["coordinateTransformations"]}
                origins[path] = tuple(transforms["translation"]["translation"][1:])
            height, width = p.group[f"{members[0]}/0"].shape[1:]
            extent = (height * p.pixel_um, width * p.pixel_um)

            def plane(path, c):
                if (path, c) not in planes:
                    pixels = p.group[f"{path}/0"][c].astype(float)
                    planes[path, c] = pixels / illum[c] if illum is not None else pixels
                return planes[path, c]

            for axis, a, b, step in touching_pairs(origins, extent):
                # Abutting fields compare neighboring lines; overlapping ones
                # the same line, which ``b`` starts on at line ``offset`` of ``a``.
                size = width if axis == "x" else height
                offset = min(round(step / p.pixel_um), size - 1)
                for c in channels:
                    first, second = plane(a, c), plane(b, c)
                    if axis == "y":
                        first, second = first.T, second.T
                    stored = correlation(first[:, offset], second[:, 0])
                    flipped = correlation(first[:, size - 1 - offset], second[:, -1])
                    wins[axis][0 if stored > flipped else 1] += 1
        for axis, (stored, flipped) in wins.items():
            n = stored + flipped
            if not n:
                problems.append(f"{p.path}: no touching fields along {axis} among the ingested "
                                f"fields")
                continue
            pvalue = sign_test(stored, n)
            verdict = "stored direction holds" if pvalue < STAGE_ALPHA else "INCONCLUSIVE or WRONG"
            print(f"    {p.path} {axis}: stored better in {stored} of {n} edge comparisons, "
                  f"p = {pvalue:.2g}: {verdict}")
            if pvalue >= STAGE_ALPHA:
                problems.append(f"{p.path} {axis}: stored better in {stored} of {n}, "
                                f"p = {pvalue:.2g}")
    return problems


#: The shared gates run plate by plate, so a filter limits what they read.
#: ``provenance`` lists every virtual chunk in the repo, tens of millions on
#: the full dataset; the sources gate reads the sampled fields' manifests
#: instead. ``series matches`` is run per plate below too.
SHARED = ("has images", "schema", "dimension names", "physical size", "levels ordered",
          "levels aligned")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--source", help="check only this source's plates")
    ap.add_argument("--batch", help="check only this batch's plates")
    ap.add_argument("--plate", help="check only this plate")
    ap.add_argument("--no-pixels", action="store_true", help="skip the sampled pixel gate")
    ap.add_argument("--stage-axes", action="store_true",
                    help="check the stage axes' directions by edge continuity; decodes every "
                         "field of the plates with positions")
    args = ap.parse_args(argv)
    started = time.monotonic()

    repo = open_repository(args.repo)
    session = repo.readonly_session(args.branch)
    root = zarr.open_group(session.store, mode="r")
    registry = registry_for(ingest.PREFIX + "x", patient=True)
    tables = jump.read_tables(jump.fetch_tables())
    found = find_plates(root, args.source, args.batch, args.plate)
    plates = [Plate(path, group, tables, registry) for path, group in found.items()]
    print(f"{len(plates)} plates, {sum(len(p.fields) for p in plates)} fields expected")

    def per_plate(gate):
        return lambda s, n: [f"{p.path}/{x}" for p in plates for x in gate(p.group, n)]

    gates = {label: per_plate(GATES[label]) for label in SHARED} | {
        "series matches": lambda s, n: [f"{p.path}: {x}" for p in plates
                                        for x in series_matches(p.group)],
        "complete": lambda s, n: complete(plates),
        "pixel size": lambda s, n: pixel_size(plates),
        "positions": lambda s, n: positions(plates),
        "sources": lambda s, n: sources(plates, session, registry),
        "perturbations": lambda s, n: perturbations(plates),
        "perturbation counts": lambda s, n: perturbation_counts(plates, tables),
        "illumination": lambda s, n: illumination(plates, s, registry),
        "references": lambda s, n: references(plates, session, registry),
    }
    if not args.no_pixels:
        gates["pixels"] = lambda s, n: pixels(plates, registry)
    if args.stage_axes:
        gates["stage axes"] = lambda s, n: stage_axes(plates, s)
    failed = run_gates(root, gates, {"version": "0.5", "sample": 1})
    print(f"{len(plates)} plates in {time.monotonic() - started:.0f} s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
