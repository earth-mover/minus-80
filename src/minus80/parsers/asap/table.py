"""ASAP annotation XML as a vertex table, and back to the same bytes.

ASAP, the whole-slide annotation tool of the Radboud computational pathology
group, saves outlines as XML: one ``Annotation`` per shape with its vertices in
order, and a list of ``AnnotationGroups``. The CAMELYON challenges publish
their outlines this way, as do other Grand Challenge datasets.

The table is shapely's ragged layout, as SpatialData 0.1 stores shapes in Zarr:
every vertex in one ``coords`` array, and ``offset0`` marking where each
shape's vertices start. ASAP's per-shape name, type, group and color ride
beside it as one string array each, and its annotation groups as a list.

:func:`to_xml` rebuilds the file from the table. That it gives back the source
byte for byte is the proof the table lost nothing, so a caller should refuse a
file where it does not. Across the 210 XML files of CAMELYON16 and CAMELYON17
it does, with float64 coordinates; float32 fails 5 of them, where ASAP printed
a value such as ``74992.1563`` that float32 stores as the tie ``74992.15625``.

No viewer reads vector annotations from inside a Zarr store, so
:func:`to_geojson` also writes the outlines in the one format QuPath imports
natively.
"""

from __future__ import annotations

from xml.etree import ElementTree

import numpy as np

#: The per-shape columns, by the XML attribute each comes from.
COLUMNS = {"name": "Name", "type": "Type", "group": "PartOfGroup", "color": "Color"}

#: How far QuPath's frame is from ASAP's, in level-0 pixels. QuPath puts (0, 0)
#: at the top-left corner of the first pixel. ASAP does not say whether its
#: integer coordinate k is the center or the corner of pixel k; rasterizing
#: CAMELYON's outlines against the challenge's own masks agrees better with the
#: center reading, which is k + 0.5 in QuPath's frame.
QUPATH_SHIFT = 0.5


def parse(xml: bytes) -> dict:
    """The table for one ASAP file: ``coords``, ``offset0``, the :data:`COLUMNS`, and ``groups``.

    Raises ``ValueError`` for vertices out of order and for a group with
    non-empty ``Attributes``, which the table does not hold, rather than
    returning a table that cannot rebuild the file.
    """
    root = ElementTree.fromstring(xml)
    columns = {k: [] for k in COLUMNS}
    coords, offsets = [], [0]
    for annotation in root.iter("Annotation"):
        for key, attr in COLUMNS.items():
            columns[key].append(annotation.get(attr))
        vertices = annotation.find("Coordinates").findall("Coordinate")
        orders = [int(v.get("Order")) for v in vertices]
        if orders != list(range(len(vertices))):
            raise ValueError(f"{annotation.get('Name')}: vertices out of order: {orders[:10]}")
        coords += [(float(v.get("X")), float(v.get("Y"))) for v in vertices]
        offsets.append(len(coords))
    groups = []
    for group in root.iter("Group"):
        attributes = group.find("Attributes")
        if attributes is not None and len(attributes):
            raise ValueError(f"group {group.get('Name')}: non-empty Attributes are not stored")
        groups.append({"name": group.get("Name"), "part_of_group": group.get("PartOfGroup"),
                       "color": group.get("Color")})
    return {
        "coords": np.array(coords, dtype=np.float64).reshape(-1, 2),
        "offset0": np.array(offsets, dtype=np.int64),
        **{k: np.array(v, dtype=np.dtypes.StringDType()) for k, v in columns.items()},
        "groups": groups,
    }


def _number(v: float) -> str:
    return f"{v:.9g}"


def to_xml(table) -> bytes:
    """The ASAP file ``table`` came from, in ASAP's own layout.

    ``table`` is anything indexable by the names :func:`parse` returns, such as
    that dict or a Zarr group holding the arrays, with ``groups`` supplied
    separately when it lives in attributes. Coordinates print with ``%.9g``
    and ASAP's fixed tab indentation.
    """
    coords, offsets = np.asarray(table["coords"]), np.asarray(table["offset0"])
    columns = {k: [str(v) for v in np.asarray(table[k])] for k in COLUMNS}
    lines = ['<?xml version="1.0"?>', "<ASAP_Annotations>", "\t<Annotations>"]
    for i in range(len(offsets) - 1):
        attrs = " ".join(f'{attr}="{columns[key][i]}"' for key, attr in COLUMNS.items())
        lines += [f"\t\t<Annotation {attrs}>", "\t\t\t<Coordinates>"]
        for order, (x, y) in enumerate(coords[offsets[i]:offsets[i + 1]]):
            lines.append(f'\t\t\t\t<Coordinate Order="{order}" X="{_number(x)}" Y="{_number(y)}" />')
        lines += ["\t\t\t</Coordinates>", "\t\t</Annotation>"]
    lines += ["\t</Annotations>", "\t<AnnotationGroups>"]
    for g in table["groups"]:
        lines += [f'\t\t<Group Name="{g["name"]}" PartOfGroup="{g["part_of_group"]}" Color="{g["color"]}">',
                  "\t\t\t<Attributes />", "\t\t</Group>"]
    lines += ["\t</AnnotationGroups>", "</ASAP_Annotations>", ""]
    return "\n".join(lines).encode()


def _rgb(hex_color: str | None) -> list[int] | None:
    if not hex_color or not hex_color.startswith("#") or len(hex_color) != 7:
        return None
    return [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]


def to_geojson(table, *, region: str = "Tumor", holes: str = "Exclusion") -> dict:
    """The outlines as a GeoJSON FeatureCollection in QuPath's feature schema, which QuPath imports as annotations.

    Each shape of group ``region`` becomes one annotation with the shapes of
    group ``holes`` cut out of it, which is how the CAMELYON README defines
    its Exclusion group: "non-tumor areas which have been cut-out from the
    original annotations in the Tumor group". A ``holes`` shape is not a
    feature of its own, and is cut only from the ``region`` shapes it does not
    contain: a region drawn inside a hole is an island inside the cut-out
    area, which CAMELYON's masks keep (``test_026`` has 18). Shapes of other
    groups, and shapes in no group, become annotations as drawn. A Dot
    becomes a point. Coordinates are level-0 pixels shifted by
    :data:`QUPATH_SHIFT`. Needs shapely.
    """
    import shapely
    from shapely.geometry import mapping

    coords, offsets = np.asarray(table["coords"]), np.asarray(table["offset0"])
    columns = {k: [str(v) for v in np.asarray(table[k])] for k in COLUMNS}
    group_color = {g["name"]: g["color"] for g in table["groups"]}

    def geometry(i):
        xy = coords[offsets[i]:offsets[i + 1]] + QUPATH_SHIFT
        if columns["type"][i] == "Dot":
            return shapely.Point(xy[0])
        # A self-intersecting outline repairs to a collection that can include
        # stray lines; an annotation is its area, so only the polygonal parts are kept.
        fixed = shapely.make_valid(shapely.Polygon(xy))
        return shapely.union_all([g for g in getattr(fixed, "geoms", [fixed]) if g.geom_type.endswith("Polygon")])

    shapes = range(len(offsets) - 1)
    cutouts = [geometry(i) for i in shapes if columns["group"][i] == holes]
    features = []
    for i in shapes:
        group = columns["group"][i]
        if group == holes:
            continue
        geom = geometry(i)
        if group == region:
            cut = [e for e in cutouts if e.intersects(geom) and not geom.within(e)]
            if cut:
                geom = geom.difference(shapely.union_all(cut))
        if geom.is_empty:
            continue
        properties = {"objectType": "annotation", "name": columns["name"][i]}
        if group != "None":
            properties["classification"] = {"name": group}
            if (rgb := _rgb(group_color.get(group) or columns["color"][i])) is not None:
                properties["classification"]["color"] = rgb
        features.append({"type": "Feature", "geometry": mapping(geom), "properties": properties})
    return {"type": "FeatureCollection", "features": features}
