"""Read an OME-Zarr HCS plate written by Fractal, field of view by field of view.

What Fractal writes
-------------------
`Fractal <https://fractal-analytics-platform.github.io/>`_ stores each well and
acquisition as one well-sized image: the microscope's fields of view are
pasted onto one blank, well-sized image at their stage positions, with zeros between them.
Where each field sits is not in the image metadata but in an AnnData table
beside it, ``tables/FOV_ROI_table``, one row per field, in micrometers.

OME-Zarr's plate layout gives each field its own image instead. To get there,
a reader has to:

- find the fields from the table, and check that every acquisition's copy of
  it agrees: the copies list the same fields in different orders, so rows are
  matched by name;
- turn each row into a window on level 0, checking that it lands on whole
  pixels, inside the well image, and spans every plane;
- carry that window down the pyramid (:func:`minus80.ome.crop.crop_levels`),
  after checking that each level is the one above halved in y and x.

What this module returns
------------------------
- :func:`read_plate`: a :class:`Plate` with the wells and acquisitions found in
  the groups themselves (the plate's own metadata can leave acquisitions out),
  and the pixel size, depth and level count every image shares;
- :func:`read_fields`: each field of a well, ordered by its number, with its
  table row and the window and placement of every pyramid level.

It reads metadata and tables only. Checked against one deposit so far,
BioImage Archive S-BIAD3228 (ingests/zebrafish-3d-4i).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import anndata
import numpy as np
import zarr

from minus80.core.progress import Progress
from minus80.ome.crop import CroppedLevel, crop_levels

IMAGE_DIMS = ("c", "z", "y", "x")
LABEL_DIMS = ("z", "y", "x")
FIELDS_TABLE = "FOV_ROI_table"
#: The group under each image where Fractal keeps its AnnData tables.
TABLES = "tables"
#: How far a position may sit from a whole pixel and still count as one. The
#: tables store micrometers as float64, so 0.65 * 1945 comes back as
#: 1264.2500000000002, not 1264.25.
WHOLE_PIXEL = 1e-6


@dataclass(frozen=True)
class Plate:
    """What every image of the plate shares, checked against each one."""

    #: The plate's own ``plate`` attributes, as written.
    attrs: dict
    wells: tuple[str, ...]
    #: Acquisitions found as image groups in every well.
    acquisitions: tuple[int, ...]
    depth: int
    #: Level-0 pixel size per axis.
    scale: dict[str, float]
    levels: int
    units: dict[str, str]


@dataclass(frozen=True)
class Field:
    """One field of view of a well, placed by its ``FOV_ROI_table`` row."""

    name: str
    #: The ``n`` of its name, ``FOV_<n>``.
    number: int
    #: ``(y, x)`` of its first pixel on the well image's level 0.
    origin: tuple[int, int]
    roi: dict[str, float]
    #: Each level's window into the well image's level, and where it sits.
    levels: tuple[CroppedLevel, ...]


def read_table(group: zarr.Group):
    """An AnnData ROI table as a DataFrame: one row per region, one column per variable."""
    return anndata.io.read_elem(group).to_df()


def level_scales(group: zarr.Group, axes: Sequence[str]) -> list[dict[str, float]]:
    """Each level's pixel size per axis, from an OME-Zarr 0.4 ``multiscales`` block."""
    return multiscale_scales(group.attrs, axes, group.path)


def multiscale_scales(attrs: Mapping, axes: Sequence[str], where: str = "") -> list[dict[str, float]]:
    """:func:`level_scales` from an image's attributes alone, for a reader that has its ``.zattrs`` but not its group."""
    (ms,) = attrs["multiscales"]
    if [a["name"] for a in ms["axes"]] != list(axes):
        raise ValueError(
            f"{where}: axes {[a['name'] for a in ms['axes']]}, expected {list(axes)}"
        )
    if any(len(d["coordinateTransformations"]) != 1 for d in ms["datasets"]):
        raise ValueError(f"{where}: a level carries more than a scale")
    return [dict(zip(axes, d["coordinateTransformations"][0]["scale"])) for d in ms["datasets"]]


def pyramid_geometry(image: zarr.Group) -> tuple:
    """Planes, level-0 pixel size, level count and units, after checking each level halves y and x.

    A field can be carried down the pyramid only if each level is the one
    above halved in y and x with its size rounded down, so anything else
    raises.
    """
    scales = level_scales(image, IMAGE_DIMS)
    shapes = [image[str(k)].shape for k in range(len(scales))]
    for k in range(1, len(scales)):
        expected = {
            "c": scales[0]["c"],
            "z": scales[0]["z"],
            "y": scales[0]["y"] * 2**k,
            "x": scales[0]["x"] * 2**k,
        }
        if not np.allclose(
            [scales[k][d] for d in IMAGE_DIMS], [expected[d] for d in IMAGE_DIMS], rtol=0, atol=1e-9
        ):
            raise ValueError(f"{image.path}: level {k} scale {scales[k]}, expected {expected}")
        before, here = shapes[k - 1], shapes[k]
        if here != (before[0], before[1], before[2] // 2, before[3] // 2):
            raise ValueError(
                f"{image.path}: level {k} shape {here} is not level {k - 1}'s {before} halved"
            )
    (ms,) = image.attrs["multiscales"]
    units = {a["name"]: a["unit"] for a in ms["axes"] if a.get("unit")}
    return shapes[0][1], scales[0], len(scales), units


def read_plate(root: zarr.Group, wells: Sequence[str] | None = None) -> Plate:
    """The plate's wells, acquisitions and shared geometry, checked against every image of ``wells``.

    ``wells`` defaults to every well the plate lists. A reader that holds
    only some wells' metadata, such as a converter reading a zipped plate one
    well at a time, passes those wells; the returned plate still lists every
    well.

    Acquisitions are found from each well's image groups rather than the
    plate's ``acquisitions`` list, which Fractal can leave short: S-BIAD3228
    lists 0 to 2 while every well holds 0 to 3.
    """
    attrs = dict(root.attrs)["plate"]
    listed = tuple(w["path"] for w in attrs["wells"])
    found = {f"{row}/{col}" for row in root.group_keys() for col in root[row].group_keys()}
    if unlisted := found - set(listed):
        raise ValueError(f"well groups the plate does not list: {sorted(unlisted)}")
    checked = tuple(listed if wells is None else wells)
    if unknown := set(checked) - set(listed):
        raise ValueError(f"not wells of the plate: {sorted(unknown)}")
    acquisitions, geometry = None, None
    # Each well's images are checked one after another, and over a slow
    # network each check is several round trips, so this reports progress.
    with Progress(f"checking the pyramids of {len(checked)} wells", len(checked), unit="wells") as note:
        for well in checked:
            present = tuple(sorted(int(a) for a in root[well].group_keys() if a.isdigit()))
            if acquisitions is None:
                acquisitions = present
            elif present != acquisitions:
                raise ValueError(
                    f"{well} holds acquisitions {present}, {checked[0]} holds {acquisitions}"
                )
            for a in present:
                here = pyramid_geometry(root[f"{well}/{a}"])
                if geometry is None:
                    geometry = here
                elif here != geometry:
                    raise ValueError(f"{well}/{a}: {here} differs from {geometry}")
            note.advance()
    depth, scale, levels, units = geometry
    return Plate(
        attrs=attrs,
        wells=listed,
        acquisitions=acquisitions,
        depth=depth,
        scale=scale,
        levels=levels,
        units=units,
    )


def field_number(name: str) -> int:
    """The ``n`` of a field named ``FOV_<n>``, which is how Fractal names fields."""
    match = re.fullmatch(r"FOV_(\d+)", name)
    if match is None:
        raise ValueError(f"field {name!r} is not named FOV_<n>")
    return int(match.group(1))


def whole_pixels(where: str, micrometers: float, pixel: float) -> int:
    """A length in micrometers as whole pixels, or ``ValueError`` if it is not one."""
    n = micrometers / pixel
    if abs(n - round(n)) > WHOLE_PIXEL:
        raise ValueError(
            f"{where}: {micrometers} µm is {n} pixels of {pixel} µm, not a whole number"
        )
    return round(n)


def read_fields(plate: Plate, root: zarr.Group, well: str) -> list[Field]:
    """The well's fields, ordered by field number, each with its window on every level.

    Every acquisition carries its own copy of ``FOV_ROI_table``, and the
    copies must agree field by field. Their rows come in no fixed order: in
    S-BIAD3228's well C/02, acquisitions 0 to 2 list FOV_1, FOV_7, FOV_4, ...
    and acquisition 3 lists FOV_3, FOV_2, ... So rows are matched by name.

    Each field must start on a whole level-0 pixel, lie inside the well image, and
    span every plane.
    """
    tables = []
    for a in plate.acquisitions:
        table = read_table(root[f"{well}/{a}/tables/{FIELDS_TABLE}"])
        tables.append(table.loc[sorted(table.index, key=field_number)])
    for a, table in zip(plate.acquisitions[1:], tables[1:]):
        if not (table.index.equals(tables[0].index) and table.equals(tables[0])):
            raise ValueError(
                f"{well}: acquisition {a}'s {FIELDS_TABLE} differs from "
                f"acquisition {plate.acquisitions[0]}'s"
            )
    well_image = root[f"{well}/{plate.acquisitions[0]}"]
    shapes = [well_image[str(k)].shape for k in range(plate.levels)]
    return fields_from_table(well, tables[0], plate.depth, plate.scale, shapes, level_scales(well_image, IMAGE_DIMS))


def fields_from_table(
    well: str,
    table,
    depth: int,
    scale: dict[str, float],
    shapes: Sequence[Sequence[int]],
    scales: Sequence[dict[str, float]],
) -> list[Field]:
    """The fields of one ``FOV_ROI_table``, ordered by field number, placed on every level.

    ``depth`` and ``scale`` are the level-0 planes and pixel size; ``shapes``
    and ``scales`` describe every level of the well image. :func:`read_fields`
    calls this after checking every acquisition's table. A reader that has
    one table before the levels' shapes, such as a converter streaming a
    zipped plate, can pass shapes it knows to be large enough, and compare
    the result with :func:`read_fields` once the real shapes are known.

    Each field must start on a whole level-0 pixel, lie inside the well image,
    and span every plane.
    """
    table = table.loc[sorted(table.index, key=field_number)]
    fields = []
    for name, row in table.iterrows():
        where = f"{well} {name}"
        roi = {k: float(v) for k, v in row.items()}
        oy, ox = (whole_pixels(where, roi[f"{a}_micrometer"], scale[a]) for a in "yx")
        ny, nx = (whole_pixels(where, roi[f"len_{a}_micrometer"], scale[a]) for a in "yx")
        nz = whole_pixels(where, roi["len_z_micrometer"], scale["z"])
        if roi["z_micrometer"] != 0 or nz != depth:
            raise ValueError(
                f"{where}: planes {roi['z_micrometer']} + {nz}, the well image has {depth}"
            )
        if oy < 0 or ox < 0 or oy + ny > shapes[0][2] or ox + nx > shapes[0][3]:
            raise ValueError(
                f"{where}: field at ({oy}, {ox}) size ({ny}, {nx}) is not inside the well image "
                f"{shapes[0][2:]}"
            )
        levels = crop_levels(
            {"c": 0, "z": 0, "y": oy, "x": ox},
            {"c": shapes[0][0], "z": depth, "y": ny, "x": nx},
            shapes,
            scales,
        )
        fields.append(
            Field(
                name=str(name),
                number=field_number(name),
                origin=(oy, ox),
                roi=roi,
                levels=tuple(levels),
            )
        )
    return fields
