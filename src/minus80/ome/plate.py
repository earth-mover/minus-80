"""The ``ome`` attributes for a high-content screening plate and its wells.

OME-Zarr's HCS layout is plate / row / column / field: the plate group lists
its rows, columns and the wells that hold images, each well group lists its
fields, and each field is an ordinary multiscale image. Images taken in a
multi-well plate go in this layout rather than in a ``bioformats2raw.layout``
collection, and the spec does not let one group be both.

Every row and column of the physical plate is declared, wells or not, so a
viewer can draw the plate's real grid around a sparse set of wells.

Unlike :func:`minus80.ome.image_attrs`, these return the whole attribute
document, ``ome`` key included, as :func:`minus80.ome.root_attrs` does.
"""

from __future__ import annotations

import string
from collections.abc import Mapping, Sequence

from ome_zarr_models.v05.plate import Acquisition, Column, Plate, Row, WellInPlate
from ome_zarr_models.v05.well_types import WellImage, WellMeta

from .image import DEFAULT_VERSION, VERSIONS, _document, validate

#: Row and column names of the standard plate formats, by well count.
FORMATS = {
    6: (2, 3), 12: (3, 4), 24: (4, 6), 48: (6, 8), 96: (8, 12), 384: (16, 24), 1536: (32, 48),
}


def plate_grid(wells: int) -> tuple[list[str], list[str]]:
    """Row names ``A``, ``B``, ... and column names ``1``, ``2``, ... of a standard plate."""
    rows, columns = FORMATS[wells]
    letters = list(string.ascii_uppercase) + [a + b for a in "A" for b in string.ascii_uppercase]
    return letters[:rows], [str(c) for c in range(1, columns + 1)]


def plate_attrs(*, name: str, rows: Sequence[str], columns: Sequence[str],
                wells: Sequence[tuple[str, str]], field_count: int | None = None,
                acquisitions: Sequence[Mapping] | None = None,
                version: str = DEFAULT_VERSION) -> dict:
    """Attributes for a plate group holding ``wells``, each a (row, column) pair.

    ``rows`` and ``columns`` name the whole physical plate, not only the wells
    that hold images. ``field_count`` is the most fields any well holds.
    ``acquisitions`` are the plate's imaging runs, each with an integer ``id``.
    """
    rows, columns = list(rows), list(columns)
    unknown = [(r, c) for r, c in wells if r not in rows or c not in columns]
    if unknown:
        raise ValueError(f"wells outside the plate's {len(rows)} x {len(columns)} grid: {unknown}")
    plate = Plate(
        version=VERSIONS[version], name=name,
        rows=[Row(name=r) for r in rows], columns=[Column(name=c) for c in columns],
        wells=[WellInPlate(path=f"{r}/{c}", rowIndex=rows.index(r), columnIndex=columns.index(c))
               for r, c in wells],
        field_count=field_count,
        acquisitions=[Acquisition(**a) for a in acquisitions] if acquisitions else None,
    )
    document = {"version": VERSIONS[version], "plate": _document(plate)}
    document["plate"].pop("version", None)
    validate(document, version, model="plate")
    return {"ome": document}


def well_attrs(images: Sequence[str], *, acquisitions: Sequence[int] | None = None,
               version: str = DEFAULT_VERSION) -> dict:
    """Attributes for a well group whose fields are the image groups ``images``.

    ``acquisitions`` gives each image's acquisition ``id``; the spec requires
    it when the plate declares more than one acquisition.
    """
    if acquisitions is not None and len(acquisitions) != len(images):
        raise ValueError(f"{len(images)} images but {len(acquisitions)} acquisition ids")
    well = WellMeta(images=[
        WellImage(path=p, acquisition=None if acquisitions is None else acquisitions[i])
        for i, p in enumerate(images)])
    document = {"version": VERSIONS[version], "well": _document(well)}
    validate(document, version, model="well")
    return {"ome": document}
