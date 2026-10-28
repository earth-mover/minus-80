# minus80.parsers.fractal

Reads an OME-Zarr HCS plate written by
[Fractal](https://fractal-analytics-platform.github.io/), where each well and
acquisition is one well-sized canvas with its fields of view pasted in, and
the fields are listed in an AnnData table, `FOV_ROI_table`.

| Module | What it does |
| --- | --- |
| `plate` | `read_plate` finds the wells and acquisitions and checks that every image shares one pyramid geometry; `read_fields` returns each field of a well, ordered by number, with its window and placement on every level (from `minus80.ome.crop`); `read_table` reads an AnnData ROI table |

It reads metadata and tables only; the pixels are read through zarr by the
caller. The `fractal` extra installs `anndata`, which reads the tables.

## What Fractal plates have needed

Each item was found in a real deposit, BioImage Archive S-BIAD3228:

- The plate's `acquisitions` list can be short: S-BIAD3228 lists 0 to 2 while
  every well holds 0 to 3. Acquisitions are found from the image groups.
- Each acquisition's copy of `FOV_ROI_table` lists the same fields in a
  different order, so rows are matched by field name.
- Positions are float64 micrometers, so a whole pixel can come back as
  `1264.2500000000002`. Positions within 1e-6 of a pixel count as whole.

## Limits

- Checked against one deposit. A plate whose fields are not named `FOV_<n>`,
  or whose levels do not halve y and x, raises rather than guessing.
