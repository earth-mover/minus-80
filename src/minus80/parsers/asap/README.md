# minus80.parsers.asap

ASAP annotation XML, the outline format of the CAMELYON challenges and other
Grand Challenge pathology datasets, as a table of arrays that rebuilds the
file byte for byte, and as GeoJSON that QuPath imports.

```bash
uv sync --extra asap
```

This is not a VirtualiZarr parser. An ASAP file holds vector outlines, not
pixels, so there is nothing to reference in place: the ingest stores the table
natively.

```python
from minus80.parsers import asap

table = asap.parse(xml_bytes)          # coords, offset0, name, type, group, color, groups
assert asap.to_xml(table) == xml_bytes
features = asap.to_geojson(table)      # a FeatureCollection in QuPath's feature schema
```

| Key | Shape and type | What it holds |
| --- | --- | --- |
| `coords` | (n_vertices, 2) float64 | vertex `x`, `y` in level-0 pixels, in file order |
| `offset0` | (n_shapes + 1,) int64 | start of each shape's vertices in `coords`, then the total |
| `name`, `type`, `group`, `color` | (n_shapes,) string | ASAP's `Name`, `Type`, `PartOfGroup` and `Color` |
| `groups` | list of dicts | the `AnnotationGroups`: `name`, `part_of_group`, `color` |

`coords` and `offset0` are shapely's ragged layout, the one SpatialData 0.1
uses for shapes in Zarr.

## Limits

- `parse` refuses vertices out of order and groups with non-empty
  `Attributes`, because the table would not rebuild the file.
- Coordinates must stay float64: in float32, 5 of CAMELYON's 210 files do not
  rebuild.
- ASAP does not say whether an integer coordinate is a pixel's center or its
  corner. `to_geojson` reads it as the center (`QUPATH_SHIFT`), which agrees
  better with CAMELYON's masks; it is a measured preference, not a stated
  convention.
- `to_geojson` needs shapely.
