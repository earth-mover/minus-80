"""A scaled-down CHAMMI-75: the bucket's layout with a few images, written locally.

``build(root)`` writes ``root/CHAMMI-75/`` the way the bucket lays it out:

- ``CHAMMI-75_train/<study>/<plate>/<image>_ch<slot>.png``, one 8-bit grayscale
  PNG per channel;
- ``CHAMMI-75_train_metadata.csv``, one row per PNG, every column text;
- ``CHAMMI-75_small_metadata.csv``, the small subset, whose paths start with
  the ``CHAMMI-75_small/`` prefix the bucket does not have;
- ``CHAMMI-75_guidance.zip``, one STORED safetensors file per PNG, with
  identical positions in each of an image's channel files.

The studies cover each case the ingest decides between:

- ``nidr0013`` (CC BY 4.0): one nucleus channel, Cellpose files with ``sizes``;
  one image has no position file.
- ``idr0001`` (CC BY 4.0): two channels at two sizes, and two channel orders
  under one size, whose stack names collide and take their channel types.
  Its Cellpose files have no ``sizes``. One image's ``microscopy.fov`` is
  ``9`` on one channel's row and ``9.0`` on the other's, and one image's
  stated width is wrong.
- ``idr0025`` (CC BY-SA 3.0): one image, so the store holds a share-alike study.
- ``nidr0007`` (CC0): bright-field, which the maintainers segmented with the
  snake method, so its files are kept whole.
- ``wtc0001``: excluded by its license; in the metadata, the bucket and the zip.
- ``idr0020``: in the zip only, as in the real archive.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import imagecodecs
import numpy as np
from safetensors.numpy import save as save_safetensors

COLUMNS = ["experiment.study", "experiment.plate", "experiment.well", "experiment.control",
           "experiment.reagent", "biology.organism", "biology.cell_line", "biology.cell_type",
           "imaging.panel", "imaging.multi_channel_id", "imaging.channel", "imaging.channel_type",
           "microscopy.type", "microscopy.magnification", "microscopy.fov", "geometry.width",
           "geometry.height", "geometry.depth", "geometry.channels", "geometry.z_slice",
           "geometry.timepoint", "storage.path", "experiment.study_type"]

#: Each study's images: (image id, width, height, channel types in slot order,
#: has a position file).
IMAGES = {
    "nidr0013": [(f"nidr0013-{i:03d}", 40, 32, ["nucleus"], i != 2) for i in range(4)],
    "idr0001": (
        [(f"idr0001-a{i}", 48, 36, ["nucleus", "protein"], True) for i in range(3)]
        + [(f"idr0001-b{i}", 48, 36, ["protein", "nucleus"], True) for i in range(2)]
        + [(f"idr0001-c{i}", 24, 20, ["nucleus", "protein"], True) for i in range(2)]),
    "idr0025": [("idr0025-0", 28, 24, ["nucleus"], True)],
    "nidr0007": [(f"nidr0007-{i}", 32, 32, ["bright-field"], True) for i in range(3)],
    "wtc0001": [(f"wtc0001-{i}", 32, 24, ["membrane", "nucleus"], True) for i in range(2)],
}
#: The allowlisted studies, which a test ingests.
STUDIES = ["idr0001", "idr0025", "nidr0007", "nidr0013"]
#: Studies whose position files carry a ``sizes`` tensor.
WITH_SIZES = {"nidr0013", "wtc0001"}
#: Images in the small subset.
SMALL = {"nidr0013-001", "idr0001-b0", "wtc0001-0"}
#: An image whose metadata states a wrong width, and one whose fov is written two ways.
WRONG_WIDTH = "idr0001-c1"
TWO_FOV = "idr0001-a1"


def png_key(study: str, image: str, slot: int) -> str:
    return f"CHAMMI-75_train/{study}/plate1/{image}_ch{slot}.png"


def build(root: Path) -> dict:
    """Write the archive under ``root/CHAMMI-75/``; return what each image holds.

    The result maps an image ID to ``{"pixels": [plane per slot], "entries":
    (n, 2) int64 positions as the file holds them, "sizes": (n,) or None}``.
    """
    archive = root / "CHAMMI-75"
    rng = np.random.default_rng(7)
    rows, small_rows, truth = [], [], {}
    members: list[tuple[str, bytes]] = []
    for study, images in IMAGES.items():
        for image, width, height, types, positioned in images:
            planes = []
            n = int(rng.integers(2, 6))
            entries = np.stack([rng.integers(0, width, n), rng.integers(0, height, n)], axis=1).astype(np.int64)
            sizes = rng.integers(10, 400, n).astype(np.int64) if study in WITH_SIZES else None
            tensors = {"data": entries} | ({"sizes": sizes} if sizes is not None else {})
            for slot, kind in enumerate(types, start=1):
                plane = rng.integers(0, 256, (height, width), dtype=np.uint8)
                planes.append(plane)
                key = png_key(study, image, slot)
                path = archive / key
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(imagecodecs.png_encode(plane))
                stated = width + 8 if image == WRONG_WIDTH else width
                fov = "9.0" if image == TWO_FOV and slot == 2 else "9"
                row = {
                    "experiment.study": study, "experiment.plate": "plate1", "experiment.well": "A01",
                    "experiment.control": "", "experiment.reagent": "DMSO",
                    "biology.organism": "Homo sapiens", "biology.cell_line": "U2OS",
                    "biology.cell_type": "", "imaging.panel": ",".join(types),
                    "imaging.multi_channel_id": image, "imaging.channel": f"{slot}.0",
                    "imaging.channel_type": kind, "microscopy.type": "fluorescence",
                    "microscopy.magnification": "20", "microscopy.fov": fov,
                    "geometry.width": str(stated), "geometry.height": str(height),
                    "geometry.depth": "1", "geometry.channels": str(len(types)),
                    "geometry.z_slice": "0", "geometry.timepoint": "0", "storage.path": key,
                    "experiment.study_type": "IDR" if study.startswith("idr") else "other",
                }
                rows.append(row)
                if image in SMALL:
                    small_rows.append(row | {"storage.path": key.replace("CHAMMI-75_train/", "CHAMMI-75_small/")})
                if positioned:
                    name = key.replace("CHAMMI-75_train/", "CHAMMI-75_guidance/").removesuffix(".png")
                    members.append((f"{name}.safetensors", save_safetensors(tensors)))
            truth[image] = {"study": study, "types": types, "pixels": planes,
                            "entries": entries if positioned else None,
                            "sizes": sizes if positioned else None}
    members.append(("CHAMMI-75_guidance/idr0020/plate1/x_ch1.safetensors",
                    save_safetensors({"data": np.zeros((1, 2), np.int64)})))

    for name, table in (("CHAMMI-75_train_metadata.csv", rows), ("CHAMMI-75_small_metadata.csv", small_rows)):
        with open(archive / name, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(table)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as z:
        for name, data in members:
            z.writestr(name, data)
    (archive / "CHAMMI-75_guidance.zip").write_bytes(buffer.getvalue())
    return truth
