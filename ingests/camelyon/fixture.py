"""A small copy of the CAMELYON bucket's layout, for testing the ingest without the network.

Everything the ingest relies on is reproduced at a smaller size, with 128 x
128 tiles instead of 512 x 512:

- both TIFF dialects: a Philips-format slide (YCbCr JPEG, its pixel size only
  in the ImageDescription's XML, a Macro page, reduced levels whose tags pad
  them to whole tiles) and generic slides (RGB JPEG, resolution tags in
  centimeters, levels halved rounding down);
- a CAMELYON17 Philips slide whose level 3 is cropped a whole tile short of
  level 0 halved in both directions, as some of the real ones are in one,
  and whose mask keeps the full halved shape at that level;
- LZW masks with one level fewer than their slide, so the ingest derives the
  last; tumor pixels (2) where the outline is, normal tissue (1) around it;
- a CAMELYON16 tissue map on the grid of the slide's level 3, and one that is
  a byte copy of its slide's mask (a hard link, so size and ETag agree);
- ASAP XML with a Tumor polygon and an Exclusion hole cut out of it;
- the challenges' text files: README notes, checksums.md5 (missing the
  negative CAMELYON17 mask, as in the bucket), reference.csv, stages.csv with
  training patients only, results.json and the tissue model's settings.

    uv run --package camelyon-ingest python ingests/camelyon/fixture.py <dir>
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import tifffile

TILE = 128
LEVELS = 5
SHAPE = (1600, 2200)
#: The cropped level of the CAMELYON17 Philips slide.
CROPPED = 3

#: Level-0 pixel sizes, in micrometers: a 3D Histech scan written by ASAP's
#: converter, and a Philips scan's scanned and level-0 representation spacing.
GENERIC_UM = 0.24309381848273193
PHILIPS_SCANNED_UM = 0.243094
PHILIPS_REPRESENTATION_UM = 0.243902

#: A tumor region with a hole, in level-0 pixels, and the region of tissue around it.
TUMOR = [(600, 400), (1400, 400), (1400, 1000), (600, 1000)]
HOLE = [(900, 600), (1100, 600), (1100, 800), (900, 800)]
TISSUE = (slice(200, 1300), slice(300, 1800))

#: The slides, by challenge: dialect, and whether each has a mask, outlines and a tissue map.
SLIDES_16 = {
    "tumor_001": {"dialect": "philips", "shapes": True, "tissue": "map"},
    "normal_001": {"dialect": "generic", "shapes": False, "tissue": "copy of mask"},
}
SLIDES_17 = {
    "patient_000_node_0": {"dialect": "generic", "mask": False, "shapes": False},
    "patient_000_node_1": {"dialect": "generic", "mask": "negative", "shapes": False},
    "patient_080_node_0": {"dialect": "philips", "mask": "annotated", "shapes": True},
    "patient_100_node_0": {"dialect": "generic", "mask": False, "shapes": False},
}
STAGES_17 = {"patient_000": ("pN0", "0", {"patient_000_node_0": "negative", "patient_000_node_1": "negative"}),
             "patient_080": ("pN1mi", "4", {"patient_080_node_0": "micro"})}


def picture(shape: tuple[int, int], seed: int) -> np.ndarray:
    """Smooth RGB content, so JPEG compresses it without blocking artifacts dominating."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[:shape[0], :shape[1]]
    planes = [(np.sin(y / (40 + 9 * c)) + np.cos(x / (55 + 7 * c))) * 50 + 128 + rng.normal(0, 3, shape)
              for c in range(3)]
    return np.clip(np.stack(planes, -1), 0, 255).astype(np.uint8)


def halved(a: np.ndarray, k: int) -> np.ndarray:
    """Level ``k`` of ``a``: every 2^k-th pixel, the shape halved ``k`` times rounding down."""
    f = 2 ** k
    h, w = a.shape[0] // f, a.shape[1] // f
    return a[: h * f : f, : w * f : f]


def philips_description(levels: int) -> str:
    reps = "".join(
        f'<DataObject ObjectType="PixelDataRepresentation">'
        f'<Attribute Name="PIIM_PIXEL_DATA_REPRESENTATION_NUMBER" PMSVR="IUInt16">{k}</Attribute>'
        f'<Attribute Name="DICOM_PIXEL_SPACING" PMSVR="IDoubleArray">'
        f'"{PHILIPS_REPRESENTATION_UM * 2 ** k / 1000:g}" "{PHILIPS_REPRESENTATION_UM * 2 ** k / 1000:g}"</Attribute>'
        f"</DataObject>" for k in range(levels))
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n<DataObject ObjectType="DPUfsImport">'
        '<Attribute Name="DICOM_MANUFACTURER" PMSVR="IString">3D Histech</Attribute>'
        '<Attribute Name="PIM_DP_SCANNED_IMAGES" PMSVR="IDataObjectArray"><Array>'
        '<DataObject ObjectType="DPScannedImage">'
        '<Attribute Name="PIM_DP_IMAGE_TYPE" PMSVR="IString">WSI</Attribute>'
        f'<Attribute Name="DICOM_PIXEL_SPACING" PMSVR="IDoubleArray">"{PHILIPS_SCANNED_UM / 1000:g}" '
        f'"{PHILIPS_SCANNED_UM / 1000:g}"</Attribute>'
        f'<Attribute Name="PIIM_PIXEL_DATA_REPRESENTATION_SEQUENCE" PMSVR="IDataObjectArray"><Array>{reps}'
        "</Array></Attribute></DataObject></Array></Attribute></DataObject>")


def write_slide(path: Path, dialect: str, seed: int, crop: int | None = None) -> list[tuple[int, ...]]:
    """A slide pyramid in one of the two dialects; the level shapes a reader should see."""
    full = picture(SHAPE, seed)
    shapes = []
    with tifffile.TiffWriter(path, bigtiff=True) as tif:
        for k in range(LEVELS):
            level = halved(full, k)
            if dialect == "generic":
                tif.write(level, tile=(TILE, TILE), compression="jpeg", photometric="rgb",
                          subfiletype=1 if k else 0, resolution=(1e4 / GENERIC_UM / 2 ** k,) * 2,
                          resolutionunit="CENTIMETER", metadata=None)
            else:
                if crop == k:
                    level = level[: level.shape[0] // TILE * TILE, : level.shape[1] // TILE * TILE]
                # A Philips level's tags pad it to whole tiles; tifffile trims it back.
                padded = np.full((-(-level.shape[0] // TILE) * TILE, -(-level.shape[1] // TILE) * TILE, 3),
                                 254, np.uint8)
                padded[: level.shape[0], : level.shape[1]] = level
                tif.write(padded if k else level, tile=(TILE, TILE), compression="jpeg", photometric="ycbcr",
                          subsampling=(2, 2), software="Philips DP v1.0", metadata=None,
                          description=philips_description(LEVELS) if k == 0 else f"level={k}")
            shapes.append(level.shape)
        if dialect == "philips":
            tif.write(picture((96, 210), seed + 1), compression="jpeg", photometric="ycbcr",
                      subsampling=(2, 2), software="Philips DP v1.0", description="Macro", metadata=None,
                      rowsperstrip=96)
    return shapes


def label_plane(shape: tuple[int, int], tumor: bool) -> np.ndarray:
    """Level 0 of a mask: tumor inside the outline but not its hole, normal tissue around it."""
    import shapely

    plane = np.zeros(shape, np.uint8)
    plane[TISSUE] = 1
    if tumor:
        region = shapely.Polygon(TUMOR).difference(shapely.Polygon(HOLE))
        yy, xx = np.mgrid[: shape[0], : shape[1]]
        plane[shapely.contains_xy(region, xx + 0.5, yy + 0.5)] = 2
    return plane


def write_mask(path: Path, tumor: bool, levels: int, start: int = 0) -> None:
    """An LZW pyramid of ``levels`` levels from level ``start``, every level the full halved shape."""
    plane = label_plane(SHAPE, tumor)
    with tifffile.TiffWriter(path, bigtiff=True) as tif:
        for k in range(start, start + levels):
            tif.write(halved(plane, k), tile=(TILE, TILE), compression="lzw", subfiletype=1 if k > start else 0,
                      resolution=(1e4 / GENERIC_UM / 2 ** k,) * 2, resolutionunit="CENTIMETER", metadata=None)


def asap_xml() -> bytes:
    def annotation(name, group, color, points):
        coords = "".join(f'\t\t\t\t<Coordinate Order="{i}" X="{x}" Y="{y}" />\n' for i, (x, y) in enumerate(points))
        return (f'\t\t<Annotation Name="{name}" Type="Polygon" PartOfGroup="{group}" Color="{color}">\n'
                f"\t\t\t<Coordinates>\n{coords}\t\t\t</Coordinates>\n\t\t</Annotation>\n")

    return ('<?xml version="1.0"?>\n<ASAP_Annotations>\n\t<Annotations>\n'
            + annotation("Annotation 0", "Tumor", "#F4FA58", TUMOR)
            + annotation("Annotation 1", "Exclusion", "#64FE2E", HOLE)
            + "\t</Annotations>\n\t<AnnotationGroups>\n"
            '\t\t<Group Name="Tumor" PartOfGroup="None" Color="#FF0000">\n\t\t\t<Attributes />\n\t\t</Group>\n'
            '\t\t<Group Name="Exclusion" PartOfGroup="None" Color="#00FF00">\n\t\t\t<Attributes />\n\t\t</Group>\n'
            "\t</AnnotationGroups>\n</ASAP_Annotations>\n").encode()


def checksums(root: Path, challenge: str, leave_out: set[str]) -> None:
    lines = []
    for path in sorted((root / challenge).rglob("*")):
        key = path.relative_to(root / challenge).as_posix()
        if path.is_file() and key not in leave_out and not key.startswith("background_tissue/"):
            lines.append(f"{hashlib.md5(path.read_bytes()).hexdigest()} *{key}")
    (root / challenge / "checksums.md5").write_text("\n".join(lines) + "\n")


def build_16(root: Path) -> None:
    c = root / "CAMELYON16"
    for d in ("images", "masks", "annotations", "background_tissue", "evaluation"):
        (c / d).mkdir(parents=True, exist_ok=True)
    results = []
    for seed, (slide, spec) in enumerate(SLIDES_16.items()):
        write_slide(c / "images" / f"{slide}.tif", spec["dialect"], seed)
        write_mask(c / "masks" / f"{slide}_mask.tif", spec["shapes"], LEVELS - 1)
        tissue = c / "background_tissue" / f"{slide}_tissue.tif"
        if spec["tissue"] == "map":
            write_mask(tissue, False, 2, start=3)
        else:
            os.link(c / "masks" / f"{slide}_mask.tif", tissue)
        if spec["shapes"]:
            (c / "annotations" / f"{slide}.xml").write_bytes(asap_xml())
        results.append({"entity": {"input": f"s3:camelyon/{slide}.tif"},
                        "error_messages": [] if spec["tissue"] == "map" else ["segmentation failed"]})
    (c / "background_tissue" / "results.json").write_text(json.dumps(results))
    (c / "pathology-tissue-background-segmentation.json").write_text(json.dumps({"model": "fixture"}))
    (c / "README.md").write_text(
        "# CAMELYON16\n\n### Notes about the data\n* tumor_001: partially annotated\n\n"
        "The following files are listed.\n")
    (c / "license.txt").write_text("CC0 1.0\n")
    (c / "evaluation" / "reference.csv").write_text(
        "image,type,class,center\ntumor_001.tif,tumor,macro,1\nnormal_001.tif,normal,negative,0\n")
    (c / "evaluation" / "example.csv").write_text("image,score\n")
    (c / "evaluation" / "evaluate.py").write_text("print('evaluate')\n")
    checksums(root, "CAMELYON16", set())


def build_17(root: Path) -> None:
    c = root / "CAMELYON17"
    for d in ("images", "masks", "annotations", "evaluation"):
        (c / d).mkdir(parents=True, exist_ok=True)
    for seed, (slide, spec) in enumerate(SLIDES_17.items(), start=10):
        philips = spec["dialect"] == "philips"
        write_slide(c / "images" / f"{slide}.tif", spec["dialect"], seed, crop=CROPPED if philips else None)
        if spec["mask"]:
            write_mask(c / "masks" / f"{slide}_mask.tif", spec["mask"] == "annotated", LEVELS - 1)
        if spec["shapes"]:
            (c / "annotations" / f"{slide}.xml").write_bytes(asap_xml())
    rows = ["patient,stage,center"]
    for patient, (stage, center, slides) in STAGES_17.items():
        rows.append(f"{patient}.zip,{stage},{center}")
        rows += [f"{slide}.tif,{label},{center}" for slide, label in slides.items()]
    (c / "stages.csv").write_text("\n".join(rows) + "\n")
    (c / "README.md").write_text(
        "# CAMELYON17\n\n### Notes about the data\nDue to file corruption the original "
        "patient_100_node_0.tif image is replaced with a new one.\n\n### Integrity\nchecksums.md5\n")
    (c / "license.txt").write_text("CC0 1.0\n")
    (c / "evaluation" / "example.csv").write_text("patient,stage\npatient_100.zip,pN0\n")
    (c / "evaluation" / "evaluate.py").write_text("print('evaluate')\n")
    checksums(root, "CAMELYON17", {"masks/patient_000_node_1_mask.tif"})


def build(root: Path) -> Path:
    """Write the fixture under ``root``; the source root to pass the ingest, as a URL."""
    build_16(root)
    build_17(root)
    return root


if __name__ == "__main__":
    print(build(Path(sys.argv[1])).as_uri() + "/")
