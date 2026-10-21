"""The gates any OME-Zarr store of images has to pass, whatever archive it came from.

Each gate takes the group that holds the images and returns a list of
problems, empty when it passes. They are code because a description of a check
("compare against the source") is a check each person writes differently, and
the point of gates is that every store was held to the same one.

**A floor, not a ceiling.** Each gate exists because something got through
once, so together they recognise the mistakes already made and described. They
cannot recognise a new one, and the largest classes of error are not gateable
at all: a shape that does not suit the experiment, a collection only partly
ingested, metadata the format expects that no source states.

What is not here is the one gate every archive needs and none can share:
reading pixels back against the source, which has to use the source format's
own reader. An archive adds that to :data:`GATES` and hands the whole table to
:func:`run_gates`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import ngff_zarr as nz
import numpy as np
import zarr
from zarr.core.sync import sync

from .annotations import KEY as ANNOTATIONS
from .hierarchy import images, multiscale_groups, series

#: The least a coarse level may correlate with the next finer level averaged
#: down to its size. A level that is really some other page or directory scores
#: near zero. A correct pair scores above 0.95 where there is tissue to see and
#: falls to about 0.8 on the 96-pixel-wide last level of a nearly blank slide,
#: where a few specks are most of the variance -- so the bar sits between.
LEVEL_CORRELATION = 0.5

#: The range a microscopy pixel can plausibly span, in micrometres. The bounds
#: are wide because one range has to cover electron microscopy (nanometres) and
#: macro imaging (near a millimetre) at once.
#:
#: That width is the limitation: it catches a unit mistake of the size a TIFF
#: with no resolution tags produces -- one pixel per inch, 25400 -- and a small
#: -direction error of a thousandfold or more. A ten- or hundredfold error in
#: either direction passes, because for some modality it would be a real
#: number. Narrowing it needs the imaging modality, which this gate is not
#: given.
PLAUSIBLE_PIXEL_UM = (1e-5, 1e3)


def levels(image) -> list[str]:
    """The resolution levels, in the order `multiscales` names them.

    Empty when the image says nothing usable. A store malformed enough to hit
    that has already failed the schema gate, and the later gates should still
    report rather than stop at the first bad image.
    """
    try:
        ms = dict(image.attrs)["ome"]["multiscales"][0]
        return [d["path"] for d in ms["datasets"]]
    except (KeyError, IndexError, TypeError):
        return []


def schema(root, version: str) -> list[str]:
    """Every group's OME metadata validates against the spec's own schema."""
    problems = []
    for name, image in images(root).items():
        try:
            nz.validate({"ome": dict(image.attrs)["ome"]},
                        version=version, model="image", strict=True)
        except Exception as exc:
            problems.append(f"{name}: {type(exc).__name__}: {exc}"[:120])
    for node, model in ((root, "bf2raw"), (root["OME"], "ome")):
        try:
            nz.validate(dict(node.attrs), version=version, model=model)
        except Exception as exc:
            problems.append(f"{model}: {type(exc).__name__}: {exc}"[:120])
    return problems


def dimension_names(root) -> list[str]:
    """Arrays name their axes, and the names match the image's own axes.

    OME-Zarr 0.5 requires this of every level. It also catches the failure that
    makes a store unreadable by a human: axes left as `phony_dim_0`.
    """
    problems = []
    for name, image in images(root).items():
        ms = dict(image.attrs)["ome"]["multiscales"][0]
        axes = ms.get("axes") or ms["coordinateSystems"][0]["axes"]
        expected = tuple(a["name"] for a in axes)
        for level in levels(image):
            got = image[level].metadata.dimension_names
            if tuple(got or ()) != expected:
                problems.append(f"{name}/{level}: {got} != {expected}")
    return problems


def physical_size(root) -> list[str]:
    """Level 0 states a pixel size that is real, and could be a microscope's.

    Two ways this goes wrong and neither raises. A scale of exactly 1.0 is a
    placeholder nobody filled in. A scale far outside microscopy's range is a
    unit mistake -- most often a file that carries no resolution tags at all,
    whose reader substitutes TIFF's default of one pixel per inch.
    """
    problems = []
    for name, image in images(root).items():
        ms = dict(image.attrs)["ome"]["multiscales"][0]
        axes = ms.get("axes") or ms["coordinateSystems"][0]["axes"]
        if not ms.get("datasets"):
            continue
        scale = ms["datasets"][0]["coordinateTransformations"][0]["scale"]
        spatial = [(a, s) for a, s in zip(axes, scale) if a["type"] == "space"]
        if all(s == 1.0 for _, s in spatial):
            if any(a.get("unit") for a, _ in spatial):
                problems.append(f"{name}: spatial scale is 1.0 but a unit is declared")
            continue
        low, high = PLAUSIBLE_PIXEL_UM
        for axis, size in spatial:
            if axis.get("unit") == "micrometer" and not low <= size <= high:
                problems.append(
                    f"{name}: {axis['name']} pixel size {size} um is outside "
                    f"{low}–{high}; likely a unit mistake, not a measurement")
    return problems


def series_matches(root) -> list[str]:
    """The collection lists exactly the images it holds."""
    listed = set(series(root))
    present = set(multiscale_groups(root))
    problems = []
    if listed - present:
        problems.append(f"series names {len(listed - present)} absent groups: "
                        f"{sorted(listed - present)[:3]}")
    if present - listed:
        problems.append(f"{len(present - listed)} groups missing from series: "
                        f"{sorted(present - listed)[:3]}")
    return problems


def levels_ordered(root) -> list[str]:
    """Levels run highest resolution first, sharing a dtype and rank."""
    problems = []
    for name, image in images(root).items():
        arrays = [image[p] for p in levels(image)]
        if not arrays:
            continue
        shapes = [a.shape for a in arrays]
        if any(b[-1] > a[-1] for a, b in zip(shapes, shapes[1:])):
            problems.append(f"{name}: levels not ordered high to low: {shapes}")
        if len({a.dtype for a in arrays}) > 1:
            problems.append(f"{name}: levels disagree on dtype")
        if len({len(s) for s in shapes}) > 1:
            problems.append(f"{name}: levels disagree on rank")
    return problems


def _grey(array) -> np.ndarray:
    """A level as one 2-D plane: the middle of t and z, samples averaged.

    Reads one plane's worth of chunks, so a level of a thousand-plane volume
    costs one plane and not the whole level; and the same plane at every level,
    so two levels of one volume still describe one slice.
    """
    names = list(array.metadata.dimension_names or ())
    index = []
    for d, n in zip(names[:-2], array.shape[:-2]):
        index.append(slice(None) if d == "c" else n // 2)
    data = np.asarray(array[tuple(index)]).astype(np.float64)
    return data.mean(0) if data.ndim == 3 else data


def _correlation(fine: np.ndarray, coarse: np.ndarray) -> float:
    """How well the finer level, block-averaged, predicts the coarser one.

    Each level of a scanner pyramid is an area average of the one above, so a
    correct pair correlates near one and a wrong page or directory near zero.
    The ratio is rounded to the nearest integer per axis and both planes are
    cropped to the overlap, which forgives the scanner rounding a 26681-row
    level to 13340 rather than 13340.5.
    """
    fy, fx = (max(1, round(fine.shape[0] / coarse.shape[0])),
              max(1, round(fine.shape[1] / coarse.shape[1])))
    ny, nx = min(coarse.shape[0], fine.shape[0] // fy), min(coarse.shape[1], fine.shape[1] // fx)
    down = fine[:ny * fy, :nx * fx].reshape(ny, fy, nx, fx).mean((1, 3))
    a, b = down.ravel(), coarse[:ny, :nx].ravel()
    if a.std() == 0 or b.std() == 0:
        return 1.0 if a.std() == b.std() else 0.0
    return float(np.corrcoef(a, b)[0, 1])


def levels_aligned(root, sample: int) -> list[str]:
    """Coarse levels describe the same object in the same place as finer ones.

    Two checks. The declared scale of every level must match its shape: a
    level half the size of level 0 has pixels twice as large, to within a
    pixel of rounding. That is exact, cheap, and catches a mis-stated scale.
    And each level small enough to read whole must be what the next finer one
    averages down to, which catches a level whose *pixels* are not what its
    metadata says -- the wrong page, the wrong directory -- and which no
    arithmetic on shapes can see. The largest levels go unexamined; their
    scale is what the others are measured against.
    """
    problems = []
    for name, image in list(images(root).items())[:sample]:
        ms = dict(image.attrs)["ome"]["multiscales"][0]
        by_path = {d["path"]: d["coordinateTransformations"][0]["scale"]
                   for d in ms.get("datasets", [])}
        paths = levels(image)
        if len(paths) < 2:
            continue                        # one level: nothing to align it against
        base_shape, base_scale = image[paths[0]].shape, by_path[paths[0]]
        for p in paths[1:]:
            for axis in (-2, -1):
                expected = base_scale[axis] * base_shape[axis] / image[p].shape[axis]
                # One pixel of rounding at this level, in physical units.
                if abs(by_path[p][axis] - expected) > expected / image[p].shape[axis]:
                    problems.append(f"{name}/{p}: scale {by_path[p][axis]:.4g} on axis "
                                    f"{axis} but shape implies {expected:.4g}")
        small = [p for p in paths if max(image[p].shape) <= 2048]
        planes = [_grey(image[p]) for p in small]
        for (pa, a), (pb, b) in zip(zip(small, planes), zip(small[1:], planes[1:])):
            r = _correlation(a, b)
            if r < LEVEL_CORRELATION:
                problems.append(f"{name}/{pb}: correlates {r:.2f} with {pa} averaged down; "
                                f"expected at least {LEVEL_CORRELATION}")
    return problems


def _declared_sources(node) -> list[str]:
    """The source URLs a node's attrs name, at top level or in its annotations."""
    attrs = dict(node.attrs)
    declared = attrs.get("source_url")
    if declared is None and isinstance(attrs.get(ANNOTATIONS), dict):
        declared = attrs[ANNOTATIONS].get("source_url")
    if declared is None:
        return []
    return [declared] if isinstance(declared, str) else list(declared)


def _is_virtual(session, array) -> bool:
    """Whether the array's chunks are references into another object.

    Judged by one chunk: the origin, which answers without a manifest scan,
    or for a sparse array (a mask whose corner tile is empty) the first chunk
    that exists.
    """
    path = "/" + array.path
    kind = session.chunk_type(path, [0] * array.ndim)
    if kind == type(kind).uninitialized:
        async def first():
            async for coords in session.chunk_coordinates(path, batch_size=1):
                return coords
        coords = sync(first())
        if coords is None:
            return False
        kind = session.chunk_type(path, list(coords))
    return kind == type(kind).virtual


def provenance(root) -> list[str]:
    """Every virtual array names the object its bytes come from.

    A store whose attrs say only which folder a study came from lets a reader
    see the pixels without being able to say which file holds them. So each
    array with virtual chunks, image levels and label levels alike, must carry
    a ``source_url`` -- a URL or a list of them -- on itself or on the group
    that holds it, the latter being where one file backs every level. The
    ``annotations`` block that :func:`virtual_ome.image_annotation` writes
    counts as the group's own attrs.

    Every declared URL must also be one the store's virtual chunks actually
    reference, so the attr cannot drift from the manifests. Icechunk lists
    virtual chunk locations only for the whole store, not per array, so the
    check is that the declared URLs are a subset of all locations: an array
    naming another array's file passes. The listing has one entry per virtual
    chunk, so on a store of millions of chunks it is most of the gate's cost.
    Only an Icechunk store can be checked.
    """
    session = getattr(root.store, "session", None)
    if session is None:
        return [f"the store is {type(root.store).__name__}, not Icechunk; "
                f"virtual chunk locations cannot be listed"]
    located = set(session.all_virtual_chunk_locations())
    problems = []
    for name, image in images(root).items():
        groups = [("", image)] + [(p, g) for p, g in image.members(max_depth=None)
                                  if isinstance(g, zarr.Group)]
        for prefix, group in groups:
            for key, array in group.arrays():
                where = "/".join(p for p in (name, prefix, key) if p)
                if not _is_virtual(session, array):
                    continue
                declared = _declared_sources(array) or _declared_sources(group)
                if not declared:
                    problems.append(f"{where}: virtual, but neither it nor its group names a source_url")
                for url in declared:
                    if url not in located:
                        problems.append(f"{where}: source_url {url} is not among the store's "
                                        f"virtual chunk locations")
    return problems


def has_images(root) -> list[str]:
    """The collection holds at least one image. Every other gate passes vacuously on an empty store."""
    return [] if images(root) else ["the collection holds no images"]


#: The archive-independent gates, in the order they run. Each takes the root
#: group and a context of ``version`` and ``sample``.
GATES: dict[str, Callable[..., list[str]]] = {
    "has images": lambda s, n: has_images(s),
    "schema": lambda s, n: schema(s, n["version"]),
    "dimension names": lambda s, n: dimension_names(s),
    "physical size": lambda s, n: physical_size(s),
    "series matches": lambda s, n: series_matches(s),
    "levels ordered": lambda s, n: levels_ordered(s),
    "levels aligned": lambda s, n: levels_aligned(s, n["sample"]),
    "provenance": lambda s, n: provenance(s),
}


def run_gates(root, gates: Mapping[str, Callable[..., list[str]]], context: Mapping) -> int:
    """Run every gate against ``root``, print a line each, and return how many failed."""
    failures = 0
    for label, gate in gates.items():
        try:
            problems = gate(root, context)
        except Exception as exc:
            # A gate that cannot run is a failure, not a reason to stop: the
            # remaining gates still have something to say about the store.
            problems = [f"the check itself raised {type(exc).__name__}: {exc}"[:120]]
        failures += bool(problems)
        print(f"  {'FAIL' if problems else 'pass'}  {label}")
        for problem in problems[:5]:
            print(f"          {problem}")
    return failures
