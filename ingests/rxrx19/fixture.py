"""A small copy of gs://rxrx with the structure of RxRx19a's zips, for testing the ingest locally.

Everything the ingest relies on is reproduced at a smaller size:

- an images zip of stored (not deflated) 8-bit grayscale PNGs named
  ``RxRx19a/images/<experiment>/Plate<n>/<well>_s<site>_w<channel>.png``,
  each local header carrying a 28-byte extra field, as Recursion's zips do,
  beside a deflated LICENSE;
- two experiments, plates of different well counts, four sites and five
  channels per well, wells named with one- and two-letter rows;
- a metadata zip with ``RxRx19a/metadata.csv``, one row per site, including a
  blank treatment and a blank condition;
- an embeddings zip with a deflated ``RxRx19a/embeddings.csv`` whose rows are
  not in metadata.csv's order.

    uv run --package rxrx19-ingest python ingests/rxrx19/fixture.py <dir>
"""

from __future__ import annotations

import csv
import io
import struct
import sys
import time
import zipfile
from pathlib import Path

import imagecodecs
import numpy as np

SIDE = 16
CHANNELS = 5
SITES = 4
FEATURES = 6
#: (experiment, plate) → wells.
PLATES = {
    ("HRCE-1", 1): ["B02", "B03", "AA02"],
    ("HRCE-1", 2): ["AE47"],
    ("VERO-1", 1): ["C10", "AB05"],
}
CELL_TYPE = {"HRCE-1": "HRCE", "VERO-1": "VERO"}

#: A Unix timestamp field (UT, 9 bytes) and a Unix ids field (ux, 11 bytes): 28 bytes with their headers.
EXTRA = struct.pack("<HHBII", 0x5455, 9, 3, 1588000000, 1588000000) + struct.pack("<HHBBIBI", 0x7875, 11, 1, 4, 1001, 4, 1002)
assert len(EXTRA) == 28


def member(zf: zipfile.ZipFile, name: str, data: bytes, method: int) -> None:
    info = zipfile.ZipInfo(name, date_time=time.localtime(1588000000)[:6])
    info.compress_type = method
    info.extra = EXTRA
    zf.writestr(info, data)


def build(root: Path) -> dict:
    """Write the fixture's three zips under ``root`` (as ``gs://rxrx/`` lays them out) and return what they hold.

    The return value maps each PNG's name to its pixels, each site id to its
    metadata row, and each site id to its embedding.
    """
    folder = root / "RxRx19a"
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(19)
    pixels, rows, embeddings = {}, [], {}
    with zipfile.ZipFile(folder / "RxRx19a-images.zip", "w") as zf:
        member(zf, "RxRx19a/LICENSE", b"Creative Commons Attribution 4.0\n" * 20, zipfile.ZIP_DEFLATED)
        for (experiment, plate), wells in PLATES.items():
            for well in wells:
                for site in range(1, SITES + 1):
                    site_id = f"{experiment}_{plate}_{well}_{site}"
                    blank = site_id == "HRCE-1_1_B03_1" or well == "C10"
                    rows.append({
                        "site_id": site_id, "well_id": f"{experiment}_{plate}_{well}", "cell_type": CELL_TYPE[experiment],
                        "experiment": experiment, "plate": plate, "well": well, "site": site,
                        "disease_condition": "" if well == "C10" else "Active SARS-CoV-2",
                        "treatment": "" if blank else "Remdesivir (GS-5734)",
                        "treatment_conc": "" if blank else 0.3,
                        "SMILES": "" if blank else "CCC(CC)COC(=O)C(C)NP(=O)(OCC1C(C(C(O1)(C#N)C2=CC=C3N2N=CN=C3N)O)O)OC4=CC=CC=C4",
                    })
                    embeddings[site_id] = rng.standard_normal(FEATURES).astype("float32")
                    for channel in range(1, CHANNELS + 1):
                        name = f"RxRx19a/images/{experiment}/Plate{plate}/{well}_s{site}_w{channel}.png"
                        image = rng.integers(0, 256, (SIDE, SIDE), dtype="uint8")
                        pixels[name] = image
                        member(zf, name, imagecodecs.png_encode(image), zipfile.ZIP_STORED)

    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    with zipfile.ZipFile(folder / "RxRx19a-metadata.zip", "w") as zf:
        member(zf, "RxRx19a/metadata.csv", text.getvalue().encode(), zipfile.ZIP_DEFLATED)

    text = io.StringIO()
    text.write(",".join(["site_id"] + [f"feature_{i}" for i in range(FEATURES)]) + "\n")
    for site_id in reversed(list(embeddings)):
        text.write(",".join([site_id] + [repr(float(v)) for v in embeddings[site_id]]) + "\n")
    with zipfile.ZipFile(folder / "RxRx19a-DL-embeddings.zip", "w") as zf:
        member(zf, "RxRx19a/embeddings.csv", text.getvalue().encode(), zipfile.ZIP_DEFLATED)
    return {"pixels": pixels, "rows": {r["site_id"]: r for r in rows}, "embeddings": embeddings}


if __name__ == "__main__":
    out = Path(sys.argv[1])
    truth = build(out)
    print(f"{len(truth['pixels'])} PNGs, {len(truth['rows'])} sites under {out}")
