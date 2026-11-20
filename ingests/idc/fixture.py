"""Two small GTEx-like slide series, and an HTTP server that serves them as IDC's bucket would.

Each slide is a series directory of the instances a GTEx slide has: two
JPEG ``VOLUME`` levels in ``TILED_FULL`` order with a filled Basic Offset
Table and edge tiles that overhang the matrix, an uncompressed
``THUMBNAIL`` and an uncompressed ``OVERVIEW``, each carrying its pixel
spacing in the shared functional groups. The levels are scaled down from
GTEx's (240-pixel tiles over a few tiles, not hundreds), so an end-to-end
run takes seconds. Real GTEx files are too large to commit; the trial in
PLAN.md ran the same code on real ones.

The server answers range GETs with each object's ETag, and refuses a GET
whose ``If-Match`` names another, as S3 does, so a test can check that the
store's references are pinned.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import imagecodecs
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import ExplicitVRLittleEndian, JPEGBaseline8Bit, generate_uid

TILE = 240

#: ContainerIdentifier, PatientID, the series prefix, and (rows, cols) of each level.
SLIDES = [
    ("GTEX-AAAAA-0001", "GTEX-AAAAA", "series-a", [(600, 700), (150, 175)]),
    ("GTEX-BBBBB-0002", "GTEX-BBBBB", "series-b", [(500, 480), (125, 120)]),
]
SPACING_MM = 0.0004942


def instance(image_type: list[str], rows: int, cols: int, frames: list, *, tile: tuple[int, int],
             jpeg: bool, spacing_mm: float) -> Dataset:
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.77.1.6"
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = JPEGBaseline8Bit if jpeg else ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = fm
    ds.SOPClassUID, ds.SOPInstanceUID = fm.MediaStorageSOPClassUID, fm.MediaStorageSOPInstanceUID
    ds.Modality = "SM"
    ds.ImageType = image_type
    ds.Rows, ds.Columns = tile
    ds.TotalPixelMatrixRows, ds.TotalPixelMatrixColumns = rows, cols
    ds.NumberOfFrames = len(frames)
    ds.SamplesPerPixel = 3
    ds.PlanarConfiguration = 0
    ds.PhotometricInterpretation = "YBR_FULL_422" if jpeg else "RGB"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
    ds.PixelRepresentation = 0
    ds.DimensionOrganizationType = "TILED_FULL"
    measures = Dataset()
    measures.PixelSpacing = [spacing_mm, spacing_mm]
    shared = Dataset()
    shared.PixelMeasuresSequence = [measures]
    ds.SharedFunctionalGroupsSequence = [shared]
    if jpeg:
        ds.PixelData = encapsulate(frames, has_bot=True)
        ds["PixelData"].VR = "OB"
        ds["PixelData"].is_undefined_length = True
    else:
        ds.PixelData = frames[0].tobytes()
    return ds


def build(root: Path) -> dict:
    """Write every slide's series under ``root``; return each slide's facts and expected pixels."""
    rng = np.random.default_rng(0)
    truth = {}
    for slide, patient, series, levels in SLIDES:
        directory = root / series
        directory.mkdir(parents=True)
        expected = []
        for k, (rows, cols) in enumerate(levels):
            # Smooth content, so the JPEG tiles decode near what was encoded.
            yy, xx = np.mgrid[0:rows, 0:cols]
            image = np.stack([(yy * 255 // rows), (xx * 255 // cols), rng.integers(100, 200, (rows, cols))],
                             axis=-1).astype(np.uint8)
            grid = (-(-rows // TILE), -(-cols // TILE))
            padded = np.zeros((grid[0] * TILE, grid[1] * TILE, 3), np.uint8)
            padded[:rows, :cols] = image
            frames = [imagecodecs.jpeg8_encode(padded[r * TILE:(r + 1) * TILE, c * TILE:(c + 1) * TILE], level=90)
                      for r in range(grid[0]) for c in range(grid[1])]
            decoded = np.zeros_like(padded)
            for i, f in enumerate(frames):
                r, c = divmod(i, grid[1])
                decoded[r * TILE:(r + 1) * TILE, c * TILE:(c + 1) * TILE] = imagecodecs.jpeg8_decode(f)
            expected.append(decoded[:rows, :cols].transpose(2, 0, 1))
            kind = ["DERIVED", "PRIMARY", "VOLUME", "NONE" if k == 0 else "RESAMPLED"]
            ds = instance(kind, rows, cols, frames, tile=(TILE, TILE), jpeg=True,
                          spacing_mm=SPACING_MM * levels[0][0] / rows)
            pydicom.dcmwrite(directory / f"level-{k}.dcm", ds, enforce_file_format=True)
        thumbnail = rng.integers(0, 256, (60, 70, 3), dtype=np.uint8)
        ds = instance(["DERIVED", "PRIMARY", "THUMBNAIL", "RESAMPLED"], 60, 70, [thumbnail], tile=(60, 70),
                      jpeg=False, spacing_mm=SPACING_MM * levels[0][0] / 60)
        pydicom.dcmwrite(directory / "thumbnail.dcm", ds, enforce_file_format=True)
        overview = rng.integers(0, 256, (40, 90, 3), dtype=np.uint8)
        ds = instance(["DERIVED", "PRIMARY", "OVERVIEW", "NONE"], 40, 90, [overview], tile=(40, 90),
                      jpeg=False, spacing_mm=0.03)
        pydicom.dcmwrite(directory / "overview.dcm", ds, enforce_file_format=True)
        truth[slide] = {"patient": patient, "series": series, "levels": expected,
                        "thumbnail": thumbnail.transpose(2, 0, 1), "overview": overview.transpose(2, 0, 1)}
    return truth


def catalog(truth: dict) -> list[dict]:
    """The slides as ``ingest.catalog`` describes them, without idc-index."""
    return [{
        "collection": "gtex", "patient": t["patient"], "slide": slide, "crdc_series_uuid": t["series"],
        "idc": {"SeriesInstanceUID": f"1.2.3.{i}", "StudyInstanceUID": f"1.2.4.{i}",
                "crdc_series_uuid": t["series"], "license": "CC BY 4.0", "source_DOI": "10.0/fixture",
                "series_init_idc_version": 19, "series_revised_idc_version": 19},
        "specimen": {"tissue": "Skin of lower leg", "tissue_code": "SCT:75144006",
                     "stain": ["hematoxylin stain", "water soluble eosin stain"],
                     "embedding_medium": ["Paraffin wax"], "fixative": ["PAXgene Tissue System"]},
    } for i, (slide, t) in enumerate(truth.items())]


def etag(data: bytes) -> str:
    return f'"{hashlib.md5(data).hexdigest()}"'


@contextmanager
def serve(root: Path):
    """Serve ``root`` over HTTP; yield the server's base URL and a listing of every series."""
    files = {f"/{p.relative_to(root).as_posix()}": p.read_bytes() for p in root.rglob("*.dcm")}
    tags = {path: etag(data) for path, data in files.items()}
    listing: dict[str, list] = {}
    for path, data in sorted(files.items()):
        series, name = path.strip("/").split("/")
        listing.setdefault(series, []).append((name, len(data), tags[path]))

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            data = files.get(self.path)
            if data is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", tags[self.path])
            self.end_headers()

        def do_GET(self):
            data = files.get(self.path)
            if data is None:
                self.send_error(404)
                return
            wanted = self.headers.get("If-Match")
            if wanted is not None and wanted.strip('"') != tags[self.path].strip('"'):
                self.send_error(412)
                return
            start, end = 0, len(data) - 1
            if rng := self.headers.get("Range"):
                lo, hi = rng.removeprefix("bytes=").split("-")
                start, end = int(lo), min(int(hi), len(data) - 1)
            body = data[start:end + 1]
            self.send_response(206 if rng else 200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", tags[self.path])
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/", listing, tags
    finally:
        httpd.shutdown()
