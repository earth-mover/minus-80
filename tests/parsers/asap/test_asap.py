"""The ASAP table round-trips a file in ASAP's own layout, and refuses what it would lose."""

import json

import pytest

from minus80.parsers import asap

XML = b"""<?xml version="1.0"?>
<ASAP_Annotations>
\t<Annotations>
\t\t<Annotation Name="Annotation 0" Type="Polygon" PartOfGroup="Tumor" Color="#F4FA58">
\t\t\t<Coordinates>
\t\t\t\t<Coordinate Order="0" X="36483.8008" Y="36816.1992" />
\t\t\t\t<Coordinate Order="1" X="36404" Y="36816.1992" />
\t\t\t\t<Coordinate Order="2" X="74992.1563" Y="36784.6992" />
\t\t\t</Coordinates>
\t\t</Annotation>
\t\t<Annotation Name="Annotation 1" Type="Dot" PartOfGroup="None" Color="#F4FA58">
\t\t\t<Coordinates>
\t\t\t\t<Coordinate Order="0" X="1.5" Y="2" />
\t\t\t</Coordinates>
\t\t</Annotation>
\t</Annotations>
\t<AnnotationGroups>
\t\t<Group Name="Tumor" PartOfGroup="None" Color="#FF0000">
\t\t\t<Attributes />
\t\t</Group>
\t</AnnotationGroups>
</ASAP_Annotations>
"""


def test_round_trip_is_byte_for_byte():
    table = asap.parse(XML)
    assert table["offset0"].tolist() == [0, 3, 4]
    assert table["type"].tolist() == ["Polygon", "Dot"]
    assert table["groups"] == [{"name": "Tumor", "part_of_group": "None", "color": "#FF0000"}]
    assert asap.to_xml(table) == XML


def test_out_of_order_vertices_refused():
    with pytest.raises(ValueError, match="out of order"):
        asap.parse(XML.replace(b'Order="1"', b'Order="7"'))


HOLE = b"""<?xml version="1.0"?>
<ASAP_Annotations>
\t<Annotations>
\t\t<Annotation Name="outer" Type="Polygon" PartOfGroup="Tumor" Color="#F4FA58">
\t\t\t<Coordinates>
\t\t\t\t<Coordinate Order="0" X="0" Y="0" />
\t\t\t\t<Coordinate Order="1" X="100" Y="0" />
\t\t\t\t<Coordinate Order="2" X="100" Y="100" />
\t\t\t\t<Coordinate Order="3" X="0" Y="100" />
\t\t\t</Coordinates>
\t\t</Annotation>
\t\t<Annotation Name="hole" Type="Polygon" PartOfGroup="Exclusion" Color="#64FE2E">
\t\t\t<Coordinates>
\t\t\t\t<Coordinate Order="0" X="20" Y="20" />
\t\t\t\t<Coordinate Order="1" X="80" Y="20" />
\t\t\t\t<Coordinate Order="2" X="80" Y="80" />
\t\t\t\t<Coordinate Order="3" X="20" Y="80" />
\t\t\t</Coordinates>
\t\t</Annotation>
\t\t<Annotation Name="island" Type="Polygon" PartOfGroup="Tumor" Color="#F4FA58">
\t\t\t<Coordinates>
\t\t\t\t<Coordinate Order="0" X="40" Y="40" />
\t\t\t\t<Coordinate Order="1" X="60" Y="40" />
\t\t\t\t<Coordinate Order="2" X="60" Y="60" />
\t\t\t\t<Coordinate Order="3" X="40" Y="60" />
\t\t\t</Coordinates>
\t\t</Annotation>
\t</Annotations>
\t<AnnotationGroups>
\t\t<Group Name="Tumor" PartOfGroup="None" Color="#FF0000">
\t\t\t<Attributes />
\t\t</Group>
\t\t<Group Name="Exclusion" PartOfGroup="None" Color="#00FF00">
\t\t\t<Attributes />
\t\t</Group>
\t</AnnotationGroups>
</ASAP_Annotations>
"""


def test_group_attributes_refused():
    with pytest.raises(ValueError, match="Attributes"):
        asap.parse(XML.replace(b"<Attributes />", b"<Attributes><Attribute Name='x' /></Attributes>"))


def test_geojson_cuts_holes_but_keeps_islands_inside_them():
    pytest.importorskip("shapely")
    table = asap.parse(HOLE)
    assert asap.to_xml(table) == HOLE
    features = json.loads(json.dumps(asap.to_geojson(table)))["features"]
    by_name = {f["properties"]["name"]: f for f in features}
    assert set(by_name) == {"outer", "island"}
    outer = by_name["outer"]["geometry"]
    assert outer["type"] == "Polygon" and len(outer["coordinates"]) == 2
    assert by_name["island"]["geometry"]["type"] == "Polygon"
    assert by_name["outer"]["properties"]["classification"] == {"name": "Tumor", "color": [255, 0, 0]}
    xs = [x for x, _ in outer["coordinates"][0]]
    assert min(xs) == asap.QUPATH_SHIFT


def test_dot_becomes_a_point():
    pytest.importorskip("shapely")
    features = asap.to_geojson(asap.parse(XML))["features"]
    dot = [f for f in features if f["properties"]["name"] == "Annotation 1"][0]
    assert dot["geometry"]["type"] == "Point"
    assert "classification" not in dot["properties"]
