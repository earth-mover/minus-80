"""The ANN parser references each annotation array at the bytes pydicom reads for it."""
from __future__ import annotations

import numpy as np


def write_ann(path, polygons, areas):
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    def code(value, meaning, scheme="SCT"):
        c = Dataset()
        c.CodeValue, c.CodingSchemeDesignator, c.CodeMeaning = value, scheme, meaning
        return c

    coords = np.concatenate(polygons).astype("<f4")
    starts = np.cumsum([0] + [len(p) for p in polygons[:-1]])
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.91.1"
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = fm
    ds.SOPClassUID, ds.SOPInstanceUID = fm.MediaStorageSOPClassUID, fm.MediaStorageSOPInstanceUID
    ds.Modality = "ANN"
    ds.AnnotationCoordinateType = "2D"
    group = Dataset()
    group.AnnotationGroupNumber = 1
    group.PointCoordinatesData = coords.tobytes()
    group.LongPrimitivePointIndexList = (starts * 2 + 1).astype("<u4").tobytes()
    values = Dataset()
    values.FloatingPointValues = np.asarray(areas, "<f4").tobytes()
    measurement = Dataset()
    measurement.MeasurementUnitsCodeSequence = [code("um2", "square micrometer", "UCUM")]
    measurement.ConceptNameCodeSequence = [code("42798000", "Area")]
    measurement.MeasurementValuesSequence = [values]
    group.MeasurementsSequence = [measurement]
    group.AnnotationGroupLabel = "Nuclei"
    group.NumberOfAnnotations = len(polygons)
    group.GraphicType = "POLYGON"
    ds.AnnotationGroupSequence = [group]
    pydicom.dcmwrite(path, ds, enforce_file_format=True)


def test_annotation_arrays_reference_the_values_pydicom_reads(tmp_path, monkeypatch):
    import pydicom

    from minus80.parsers.dicom import ann

    monkeypatch.setattr(ann, "CHUNK", 8)
    rng = np.random.default_rng(3)
    polygons = [rng.uniform(0, 1000, (n, 2)) for n in (5, 9, 4, 7)]
    path = tmp_path / "ann.dcm"
    write_ann(path, polygons, [1.5, 2.5, 3.5, 4.5])
    (group,) = ann.bulk_annotation_groups(path)
    assert (group.label, group.graphic_type, group.count) == ("Nuclei", "POLYGON", 4)
    data = path.read_bytes()
    full = pydicom.dcmread(path).AnnotationGroupSequence[0]
    area, unit = group.measurements["Area"]
    assert unit == "um2"
    for ref, expected in [
        (group.coords, np.frombuffer(full.PointCoordinatesData, "<f4").reshape(-1, 2)),
        (group.point_index, np.frombuffer(full.LongPrimitivePointIndexList, "<u4")),
        (area, np.frombuffer(full.MeasurementsSequence[0].MeasurementValuesSequence[0].FloatingPointValues, "<f4")),
    ]:
        np.testing.assert_array_equal(ref.values(data), expected)
        manifest = ref.array.manifest
        rows = ref.array.metadata.chunk_grid.chunk_shape[0]
        offsets = np.asarray(manifest._offsets).reshape(-1)
        lengths = np.asarray(manifest._lengths).reshape(-1)
        for k, (o, n) in enumerate(zip(offsets, lengths)):
            if n:
                got = np.frombuffer(data[o:o + n], expected.dtype).reshape(-1, *expected.shape[1:])
                np.testing.assert_array_equal(got, expected[k * rows:(k + 1) * rows])
        np.testing.assert_array_equal(ref.tail(data), expected[ref.tail_start:])
