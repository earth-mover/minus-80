"""The ``ome`` attributes for one multiscale image, in OME-Zarr 0.5 or 0.6.

The arrays are already in the store as byte-range references, so nothing here
touches pixels. The functions return the JSON a reader needs to see those arrays
as an image: which axes they have, what a pixel measures, and which array is
which resolution level.

0.5 is the default. 0.6 moves the axes inside a named ``coordinateSystems``
entry and gives every transform an ``input`` and ``output``. Only the metadata
differs between the two, so one store can carry both on separate branches over
the same chunk references.

ome-zarr-models builds each document: it models ``multiscales``, ``omero``,
``image-label`` and ``labels`` for both versions, and its ``omero`` models allow
the transitional ``label``, ``active`` and ``rdefs`` fields a viewer reads.
ngff-zarr then validates the result against its vendored copy of the published
JSON schema.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

import ngff_zarr as nz
from ome_zarr_models.common.omero import Channel, Omero, Window
from ome_zarr_models.v05 import axes as v05_axes
from ome_zarr_models.v05 import coordinate_transformations as v05_ct
from ome_zarr_models.v05 import image as v05_image
from ome_zarr_models.v05 import image_label as v05_label
from ome_zarr_models.v05 import image_label_types as v05_label_types
from ome_zarr_models.v05 import labels as v05_labels
from ome_zarr_models.v05 import multiscales as v05_ms
from ome_zarr_models.v06 import coordinate_transforms as v06_ct
from ome_zarr_models.v06 import image as v06_image
from ome_zarr_models.v06 import image_label as v06_label
from ome_zarr_models.v06 import image_label_types as v06_label_types
from ome_zarr_models.v06 import labels as v06_labels
from ome_zarr_models.v06 import multiscales as v06_ms

#: What ``version`` to write for each supported spec version.
VERSIONS = {"0.5": "0.5", "0.6": "0.6"}

#: The version strings the published 0.6 schema accepts
#: (https://ngff.openmicroscopy.org/0.6/schemas/_version.schema).
_PUBLISHED_VERSIONS = {"0.5": {"0.5"}, "0.6": {"0.6", "0.6rc0"}}

DEFAULT_VERSION = "0.5"

#: The coordinate system 0.6 maps every resolution level onto. The name is
#: arbitrary; "intrinsic" is what the spec's own example uses.
INTRINSIC = "intrinsic"

#: Axis names whose type is unambiguous: OME-Zarr's own ``t``, ``c``, ``z``,
#: ``y``, ``x``, and ``time`` and ``channel`` spelled out. Any other name takes
#: its type from the array's ``dimensions`` attribute or raises, because a
#: guess of ``space`` makes a time or position axis spatial.
_AXIS_TYPES = {"t": "time", "time": "time", "c": "channel", "channel": "channel",
               "z": "space", "y": "space", "x": "space"}

#: Axis types whose values are labels rather than positions, so a reader must not
#: interpolate between them. The spec allows saying so; it does not require it.
_DISCRETE = {"channel"}

#: Display colours for the samples of an RGB image, in storage order.
RGB_COLORS = ("FF0000", "00FF00", "0000FF")
RGB_LABELS = ("red", "green", "blue")


def axis_types(dims: Sequence[str], dimensions: Mapping[str, Mapping] | None = None) -> list[str]:
    """The OME-Zarr type of each axis in ``dims``.

    ``dimensions`` is an array's ``dimensions`` attribute, keyed by dimension
    name; an axis's ``type`` there is used as given. OME-Zarr 0.5 and 0.6
    define ``space``, ``time`` and ``channel``, and their schemas accept any
    other string for an axis those do not describe, such as ``fov``. An axis
    ``dimensions`` leaves out is typed from :data:`_AXIS_TYPES`, and an axis
    in neither raises ``ValueError``.
    """
    dimensions = dimensions or {}
    types = []
    for d in dims:
        if d in dimensions:
            types.append(dimensions[d]["type"])
        elif d in _AXIS_TYPES:
            types.append(_AXIS_TYPES[d])
        else:
            raise ValueError(f"axis {d!r}: no type in dimensions, and the name does not say "
                             f"whether it is space, time or channel")
    return types


def check_axis_types(dims: Sequence[str], types: Sequence[str]) -> None:
    """Raise ``ValueError`` unless ``types`` follow the multiscale axis rules of OME-Zarr 0.5 and 0.6.

    Both versions require 2 or 3 ``space`` axes, at most one ``time`` axis,
    and at most one axis that is ``channel`` or of a custom type, ordered
    time, then channel or custom, then space. The JSON schema ngff-zarr
    validates against checks none of the counts, so ``(fov, c, z, y, x)``
    would otherwise be written as a valid image.
    """
    rank = {"time": 0, "space": 2}
    ranks = [rank.get(t, 1) for t in types]
    described = ", ".join(f"{d}: {t}" for d, t in zip(dims, types))
    if (spatial := ranks.count(2)) not in (2, 3):
        raise ValueError(f"axes ({described}) have {spatial} of type space; OME-Zarr requires 2 or 3")
    if ranks.count(0) > 1:
        raise ValueError(f"axes ({described}) have more than one of type time; OME-Zarr allows one")
    if ranks.count(1) > 1:
        raise ValueError(f"axes ({described}) have more than one channel or custom-typed axis; "
                         f"OME-Zarr allows one")
    if ranks != sorted(ranks):
        raise ValueError(f"axes ({described}) are out of order; OME-Zarr requires time, then "
                         f"channel or custom, then space")


def _channel(label: str, color: str, *, minimum: float, maximum: float,
             start: float, end: float) -> Channel:
    """One ``omero`` channel. ``label`` and ``active`` are extras the models allow."""
    return Channel(color=color.upper(),
                   window=Window(min=minimum, max=maximum, start=start, end=end),
                   label=label, active=True)


def _omero(channels: Sequence[Channel], *, model: str) -> dict:
    """The validated ``omero`` document.

    ``rdefs`` rides as an extra field: the spec calls the whole block
    transitional and defines no model for it, but a viewer reads it to decide
    whether to composite the channels or show one greyscale. Returned as JSON
    rather than as a model, so every document this module hands out is the same
    kind of thing.
    """
    return _document(Omero(channels=list(channels), rdefs={"model": model}))


def rgb_omero(max_value: int = 255) -> dict:
    """An ``omero`` block for an image whose channels are the R, G and B samples.

    A brightfield slide is one colour photograph, not three independent
    fluorescence channels, but OME-Zarr has no axis type that says so -- the
    samples are a ``channel`` axis like any other. Declaring the colours and a
    full display window is what lets a viewer composite them back into the
    picture that was taken; left to guess, it renders three arbitrary colours
    stretched over whatever range the data happens to span.
    """
    return _omero(
        [_channel(label, color, minimum=0, maximum=max_value, start=0, end=max_value)
         for label, color in zip(RGB_LABELS, RGB_COLORS)],
        model="color")


def channel_omero(channels: Sequence[Mapping], *, max_value: int,
                  min_value: float = 0) -> dict:
    """An ``omero`` block from per-channel display settings.

    Each entry gives a ``label``, a display ``window`` as ``start``/``end`` and
    optionally a ``color`` (hex, no ``#``). One channel with no colour is shown
    greyscale, which is how a single fluorescence stain is looked at; several
    channels are composited. ``max_value`` is the dtype's ceiling and
    ``min_value`` its floor, which is below zero for a signed image such as a CT
    stored as ``int16``; a window left unstated spans the two.
    """
    built = [_channel(ch["label"], ch.get("color") or "FFFFFF",
                      minimum=min_value, maximum=max_value,
                      start=ch.get("start", min_value), end=ch.get("end", max_value))
             for ch in channels]
    greyscale = len(built) == 1 and built[0].color == "FFFFFF"
    return _omero(built, model="greyscale" if greyscale else "color")


def _document(attrs) -> dict:
    """A pydantic attributes model as the JSON that goes in the group.

    ``by_alias`` because ``image-label`` is spelled with a hyphen on disk and
    cannot be a Python identifier; ``exclude_none`` because a literal
    ``"unit": null`` fails schema validation.
    """
    return attrs.model_dump(by_alias=True, exclude_none=True, mode="json")


def _level_scales(dims, level_shapes, scale):
    """The pixel size of each level, from how much smaller it is than level 0.

    Derived rather than assumed to be a clean factor of two: a pyramid written
    by a scanner rounds, so level 3 of a 26681-row image is 3335 rows, not
    3335.125, and the true ratio is what a reader needs to line the levels up.
    """
    base = level_shapes[0]
    for shape in level_shapes:
        ratio = (b / s for b, s in zip(base, shape))
        yield [scale[d] * r for d, r in zip(dims, ratio)]


def _level_translations(dims, scales, translation):
    """Where each level's first pixel centre sits, given level 0's.

    OME-Zarr places a transform at pixel centres, and a coarser pixel's centre
    lies half a pixel further in than a finer one's when the two share a corner.
    Shifting each level by half the difference in pixel size keeps every level's
    top-left corner on level 0's, as the levels of a DICOM slide pyramid share
    one ``TotalPixelMatrixOrigin``.
    """
    base = scales[0]
    for s in scales:
        yield [translation[d] + (sl - sb) / 2 for d, sl, sb in zip(dims, s, base)]


def _method_metadata(description: str):
    """The ``metadata`` block describing how the levels were produced.

    Present even when empty: the spec's strict variant requires the key, so
    omitting it fails validation for an image whose levels came with no
    description.
    """
    return {"description": description} if description else {}


def _multiscale_05(*, name, dims, types, paths, scales, translations, units, downsampling,
                   description):
    def transforms(s, t):
        scale = v05_ct.VectorScale(type="scale", scale=list(s))
        if t is None:
            return (scale,)
        return (scale, v05_ct.VectorTranslation(type="translation", translation=list(t)))

    return v05_ms.Multiscale(
        axes=[v05_axes.Axis(name=d, type=t, unit=units.get(d)) for d, t in zip(dims, types)],
        datasets=[v05_image.Dataset(path=p, coordinateTransformations=transforms(s, t))
                  for p, s, t in zip(paths, scales, translations)],
        # 0.5 allows a whole-image transform beside the per-level ones; the
        # levels carry everything here, so there is nothing left to say.
        coordinateTransformations=None,
        name=name, type=downsampling, metadata=_method_metadata(description))


def _multiscale_06(*, name, dims, types, paths, scales, translations, units, downsampling,
                   description):
    axes = []
    for d, axis_type in zip(dims, types):
        axes.append(v06_ct.Axis(name=d, type=axis_type, unit=units.get(d),
                                discrete=True if axis_type in _DISCRETE else None))
    return v06_ms.Multiscale(
        coordinateSystems=[v06_ms.CoordinateSystem(name=INTRINSIC, axes=axes)],
        datasets=[v06_ms.Dataset(path=p, coordinateTransformations=(_transform_06(p, s, t),))
                  for p, s, t in zip(paths, scales, translations)],
        name=name, type=downsampling, metadata=_method_metadata(description))


def _transform_06(path, scale, translation):
    """One level's transform onto the intrinsic system: a scale, then a shift if any."""
    ends = dict(input=v06_ms.CoordinateSystemIdentifier(path=path),
                output=v06_ms.CoordinateSystemIdentifier(name=INTRINSIC))
    if translation is None:
        return v06_ms.Scale(type="scale", scale=list(scale), **ends)
    return v06_ms.Sequence(type="sequence", transformations=(
        v06_ms.Scale(type="scale", scale=list(scale)),
        v06_ms.Translation(type="translation", translation=tuple(translation))), **ends)


class _Models(NamedTuple):
    """The models one spec version's documents are built from."""

    multiscale: Callable
    image: type
    image_label: type
    label: type
    label_source: type
    labels: type


_BUILDERS = {
    "0.5": _Models(_multiscale_05, v05_image.ImageAttrs, v05_label.ImageLabelAttrs,
                   v05_label_types.Label, v05_label_types.Source, v05_labels.LabelsAttrs),
    "0.6": _Models(_multiscale_06, v06_image.ImageAttrs, v06_label.ImageLabelAttrs,
                   v06_label_types.Label, v06_label_types.Source, v06_labels.LabelsAttrs),
}


def _per_level(dims, level_shapes, values, what):
    """``values``, one per-axis mapping per level, as lists in ``dims`` order."""
    if len(values) != len(level_shapes):
        raise ValueError(f"{len(values)} level {what}s for {len(level_shapes)} levels")
    for i, v in enumerate(values):
        if missing := set(dims) - set(v):
            raise ValueError(f"level {i}: no {what} given for axes {sorted(missing)}")
    return [[v[d] for d in dims] for v in values]


def _multiscale(*, name, dims, level_shapes, scale, units, downsampling,
                description, version, translation=None, dimensions=None,
                level_scales=None, level_translations=None):
    if version not in VERSIONS:
        raise ValueError(f"version must be one of {sorted(VERSIONS)}, not {version!r}")
    missing = set(dims) - set(scale)
    if missing:
        raise ValueError(f"no scale given for axes {sorted(missing)}")
    if level_scales is None:
        scales = list(_level_scales(dims, level_shapes, scale))
    else:
        scales = _per_level(dims, level_shapes, level_scales, "scale")
        if scales[0] != [scale[d] for d in dims]:
            raise ValueError(f"level 0's scale {scales[0]} is not scale {dict(scale)}")
    if translation is not None and level_translations is not None:
        raise ValueError("give translation or level_translations, not both")
    if level_translations is not None:
        translations = _per_level(dims, level_shapes, level_translations, "translation")
    elif translation is None:
        translations = [None] * len(scales)
    else:
        missing = set(dims) - set(translation)
        if missing:
            raise ValueError(f"no translation given for axes {sorted(missing)}")
        translations = list(_level_translations(dims, scales, translation))
    types = axis_types(dims, dimensions)
    check_axis_types(dims, types)
    return _BUILDERS[version].multiscale(
        name=name, dims=dims, types=types, paths=[str(i) for i in range(len(level_shapes))],
        scales=scales, translations=translations,
        units=units or {}, downsampling=downsampling, description=description)


def image_attrs(
    *,
    name: str,
    dims: Sequence[str],
    level_shapes: Sequence[Sequence[int]],
    scale: Mapping[str, float],
    units: Mapping[str, str] | None = None,
    downsampling: str = "unknown",
    description: str = "",
    omero: Mapping | None = None,
    version: str = DEFAULT_VERSION,
    translation: Mapping[str, float] | None = None,
    dimensions: Mapping[str, Mapping] | None = None,
    level_scales: Sequence[Mapping[str, float]] | None = None,
    level_translations: Sequence[Mapping[str, float]] | None = None,
) -> dict:
    """Build the validated ``ome`` attributes for a multiscale image.

    ``scale`` is the size of one pixel of the *full-resolution* level, per axis,
    in ``units``.

    ``translation`` is where the centre of level 0's first pixel sits, per axis,
    in ``units``: a CT slice's ``ImagePositionPatient``, say. Coarser levels are
    shifted so that every level's top-left corner coincides. Left out, no level
    carries a translation.

    ``dimensions`` is level 0's ``dimensions`` attribute, which states each
    axis's type; :func:`axis_types` says how an axis it leaves out is typed.

    ``level_scales`` and ``level_translations`` give each level's pixel size
    and first pixel center outright, one per-axis mapping per level, for levels
    whose geometry does not follow from their shapes: a window cut from a
    coarser level of a larger image can be a pixel wider than level 0 divided
    by the factor, and start part of a pixel off level 0's corner. Level 0's
    entry in ``level_scales`` must equal ``scale``. ``level_translations``
    replaces ``translation``.

    ``downsampling`` and ``description`` record how the levels were produced;
    for a referenced pyramid they describe the source file's own levels.

    ``omero`` is how a viewer is told what the channels look like -- colour and
    display window per channel. Both spec versions call it transitional, and
    both still need it: without it a viewer has to guess, and for a three-sample
    brightfield image it guesses wrong. Either a document from
    :func:`rgb_omero` or :func:`channel_omero`, or one read back from a store
    that is being restated.
    """
    multiscale = _multiscale(name=name, dims=dims, level_shapes=level_shapes,
                             scale=scale, units=units, downsampling=downsampling,
                             description=description, version=version,
                             translation=translation, dimensions=dimensions,
                             level_scales=level_scales, level_translations=level_translations)
    if omero is not None:
        omero = Omero.model_validate(omero)
    attrs = _BUILDERS[version].image(version=VERSIONS[version], multiscales=[multiscale],
                                     **({"omero": omero} if omero else {}))
    document = _document(attrs)
    validate(document, version, model="image", strict=True)
    return document


def label_attrs(
    *,
    name: str,
    dims: Sequence[str],
    level_shapes: Sequence[Sequence[int]],
    scale: Mapping[str, float],
    units: Mapping[str, str] | None = None,
    source_image: str = "../../",
    description: str = "",
    version: str = DEFAULT_VERSION,
    translation: Mapping[str, float] | None = None,
    colors: Sequence[Mapping] | None = None,
    properties: Sequence[Mapping] | None = None,
    dimensions: Mapping[str, Mapping] | None = None,
    level_scales: Sequence[Mapping[str, float]] | None = None,
    level_translations: Sequence[Mapping[str, float]] | None = None,
) -> dict:
    """The ``ome`` attributes for a label image beside the image it segments.

    A label array is a multiscale image carrying one extra block, ``image-label``,
    whose ``source.image`` is the relative path back to that image. The spec's
    strict variant additionally requires a ``colors`` list; a study that states no
    colour per label gets none, because inventing one puts a fabrication in the
    store. Validated without the strict variant for that reason.

    ``translation`` is level 0's first pixel centre, as for :func:`image_attrs`;
    a label sits on its image's grid, so it takes the image's translation.

    ``colors`` and ``properties`` are the spec's per-value lists, each entry
    keyed by ``label-value``: ``colors`` entries carry an ``rgba`` of four 0-255
    integers, and ``properties`` entries any further fields describing that
    value. Pass them only when the source states them, such as a DICOM
    segment's recommended display colour and its coded name.

    ``dimensions``, ``level_scales`` and ``level_translations`` are as for
    :func:`image_attrs`.
    """
    multiscale = _multiscale(name=name, dims=dims, level_shapes=level_shapes,
                             scale=scale, units=units, downsampling="unknown",
                             description=description, version=version,
                             translation=translation, dimensions=dimensions,
                             level_scales=level_scales, level_translations=level_translations)
    models = _BUILDERS[version]
    label = {"source": models.label_source(image=source_image)}
    if colors is not None:
        label["colors"] = [dict(c) for c in colors]
    if properties is not None:
        label["properties"] = [dict(p) for p in properties]
    attrs = models.image_label(
        version=VERSIONS[version], multiscales=[multiscale],
        image_label=models.label.model_validate(label))
    document = _document(attrs)
    validate(document, version, model="label", strict=colors is not None)
    return document


def labels_attrs(names: Sequence[str], version: str = DEFAULT_VERSION) -> dict:
    """The ``ome`` attributes for the ``labels`` group listing its label images."""
    attrs = _BUILDERS[version].labels(version=VERSIONS[version], labels=list(names))
    return _document(attrs)


def validate(attrs: Mapping, version: str, *, model: str, strict: bool = False) -> None:
    """Check an ``ome`` document against the spec schema.

    The declared version string is checked against :data:`_PUBLISHED_VERSIONS`,
    then the document is validated as ``version``, so one declaring ``0.6rc0``
    is held to the released 0.6 schema.
    """
    if attrs["version"] not in _PUBLISHED_VERSIONS[version]:
        raise ValueError(f"{attrs['version']!r} is not a published {version} version")
    nz.validate({"ome": {**attrs, "version": version}},
                version=version, model=model, strict=strict)
