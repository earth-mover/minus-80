import json
import zipfile

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from minus80.core.tables import TableWriter, local_catalog, parquet_field_ids

from .stores import zip_v2_store
from transcripts import (
    CATEGORY_COLUMNS,
    TRANSCRIPT_SCHEMA,
    cell_ids_arrow,
    codeword_table,
    tile_order,
    tile_slices,
    tile_table,
    write_tile,
    xenium_cell_id,
)


@pytest.fixture
def zf(tmp_path):
    category = np.zeros((4, 9), dtype=bool)
    category[[0, 1, 3], 0] = True
    category[2, 1] = True
    tile = {
        "grid/0,0/location": (np.array([[1.5, 2.5, 3.5]] * 5, dtype="<f4") + np.arange(5)[:, None].astype("<f4") * [0, 0, 1], {"order": "F", "chunks": (2, 3)}),
        "grid/0,0/codeword_identity": (np.array([[0], [1], [2], [3], [0]], dtype="<u4"), {"chunks": (2, 1)}),
        "grid/0,0/cell_id": (np.array([[1, 1], [0x1F, 1], [0, 1], [0, 1], [2**32 - 1, 2**32 - 1]], dtype="<u4"), {"order": "F", "chunks": (2, 2)}),
        "grid/0,0/quality_score": (np.linspace(10, 40, 5, dtype="<f2")[:, None], {"chunks": (2, 1)}),
        "grid/0,0/overlaps_nucleus": (np.array([[1], [0], [0], [1], [0]], dtype="u1"), {"chunks": (2, 1)}),
        "grid/0,0/gene_offset": (np.zeros((2, 2), dtype="<u4"), {}),
    }
    return zip_v2_store(
        tmp_path,
        "transcripts",
        {"codeword_category": (category, {"order": "C"}), **tile},
        attrs={
            "gene_names": ["GENE1", "GENE2", "NegControl"],
            "codeword_gene_names": ["GENE1", "GENE2", "NegControl", "GENE1"],
            "codeword_gene_mapping": [0, 1, 2, 0],
        },
    )


@pytest.fixture
def codewords(zf):
    return codeword_table(zf)


def test_codeword_table_maps_codewords_to_features(codewords):
    assert codewords.column_names == ["codeword_index", "feature_index", "feature_name", *CATEGORY_COLUMNS]
    assert codewords["codeword_index"].to_pylist() == [0, 1, 2, 3]
    assert codewords["feature_name"].to_pylist() == ["GENE1", "GENE2", "NegControl", "GENE1"]
    assert codewords["is_gene"].to_pylist() == [True, True, False, True]
    assert codewords["is_negative_control_codeword"].to_pylist() == [False, False, True, False]
    assert codewords.schema.field("codeword_index").type == pa.int64()
    assert codewords.schema.field("feature_index").type == pa.int32()


def test_tile_table_has_the_schema_and_the_xenium_cell_ids(zf, codewords):
    t = tile_table(zf, "0,0", codewords, first_instance=100)
    assert t.schema.equals(TRANSCRIPT_SCHEMA)
    assert not TRANSCRIPT_SCHEMA.field("instance").nullable
    assert t["instance"].to_pylist() == [100, 101, 102, 103, 104]
    assert t["grid_tile"].to_pylist() == ["0,0"] * 5
    assert t["cell_id"][0].as_py() == "aaaaaaab-1"  # prefix 1, suffix 1
    assert t["cell_id"][1].as_py() == "aaaaaabp-1"  # prefix 0x1F
    assert t["bbox"][0].as_py() == {"xmin": 1.5, "ymin": 2.5, "xmax": 1.5, "ymax": 2.5}
    assert t["feature_name"][0].as_py() == "GENE1"
    assert t["feature_name"].to_pylist() == ["GENE1", "GENE2", "NegControl", "GENE1", "GENE1"]
    assert t["overlaps_nucleus"].to_pylist() == [True, False, False, True, False]
    assert t["quality_score"].to_pylist() == [10.0, 17.5, 25.0, 32.5, 40.0]
    assert t["z"].to_pylist() == [3.5, 4.5, 5.5, 6.5, 7.5]


def test_tile_table_keeps_the_unassigned_sentinel_in_signed_columns(zf, codewords):
    # Iceberg has no unsigned type, so every integer column is int64 and 2**32 - 1 stays positive.
    t = tile_table(zf, "0,0", codewords, first_instance=0)
    for name in ("codeword_index", "cell_prefix", "dataset_suffix"):
        assert TRANSCRIPT_SCHEMA.field(name).type == pa.int64()
    assert t["cell_prefix"][4].as_py() == 2**32 - 1
    assert t["dataset_suffix"][4].as_py() == 2**32 - 1
    assert t["cell_id"][4].as_py() == "pppppppp-4294967295"


def test_xenium_cell_id_orders_letters_most_significant_first():
    got = xenium_cell_id(np.array([0, 1, 0x1F, 2**32 - 1], dtype="uint32"), np.array([1, 1, 2, 3], dtype="uint32"))
    assert got.tolist() == ["aaaaaaaa-1", "aaaaaaab-1", "aaaaaabp-2", "pppppppp-3"]


class _ReadLog(zipfile.ZipFile):
    """A zip that records the members read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read_members = []

    def read(self, name, *args, **kwargs):
        self.read_members.append(name if isinstance(name, str) else name.filename)
        return super().read(name, *args, **kwargs)


@pytest.mark.parametrize("rows", [1, 2, 5, 100])
def test_slices_equal_the_whole_tile(zf, codewords, rows):
    pieces = list(tile_slices(zf, "0,0", codewords, first_instance=7, rows=rows))
    step = max(2, rows // 2 * 2)  # a slice holds whole 2-row chunks
    assert all(len(p) <= step for p in pieces)
    assert len(pieces) == -(-5 // step)
    sliced = pa.concat_tables(pieces)
    whole = tile_table(zf, "0,0", codewords, first_instance=7)
    assert sliced.equals(whole)
    assert sliced["instance"].to_pylist() == list(range(7, 12))
    assert all(p.schema.equals(TRANSCRIPT_SCHEMA) for p in pieces)


def test_a_slice_reads_only_the_chunks_of_its_rows(zf, codewords):
    log = _ReadLog(zf.filename)
    first = next(iter(tile_slices(log, "0,0", codewords, first_instance=0, rows=2)))
    assert first["cell_id"].to_pylist() == ["aaaaaaab-1", "aaaaaabp-1"]
    chunks = [m for m in log.read_members if m.startswith("grid/0,0/") and not m.endswith(".zarray")]
    assert chunks and not any(m.rsplit("/", 1)[1].split(".")[0] != "0" for m in chunks)


@pytest.fixture
def tables(tmp_path):
    writer = TableWriter(local_catalog(tmp_path / "catalog"), "ns", catalog_name="local", org="local")
    writer.require("transcripts", schema=TRANSCRIPT_SCHEMA, properties={}, partition_by="grid_tile")
    return writer


def test_write_tile_writes_one_row_group_per_slice(zf, codewords, tables, tmp_path):
    ids = tables.field_ids("transcripts")
    path = tmp_path / "tile.parquet"
    assert write_tile(zf, "0,0", codewords, path, first_instance=3, field_ids=ids, rows=2) == 5
    file = pq.ParquetFile(path)
    assert [file.metadata.row_group(i).num_rows for i in range(file.num_row_groups)] == [2, 2, 1]
    assert parquet_field_ids(file.schema_arrow) == ids
    assert not file.schema_arrow.field("instance").nullable
    assert json.loads(file.schema_arrow.metadata[b"pandas"])["index_columns"] == ["instance"]
    assert b"geo" not in file.schema_arrow.metadata
    assert pq.read_table(path).equals(tile_table(zf, "0,0", codewords, first_instance=3))
    # The Iceberg table accepts the file as it is written.
    ref = tables.write_parts("transcripts", [path])
    assert len(ref.files) == 1


def test_a_tile_written_in_one_slice_equals_one_in_many(zf, codewords, tables, tmp_path):
    ids = tables.field_ids("transcripts")
    one, many = tmp_path / "one.parquet", tmp_path / "many.parquet"
    write_tile(zf, "0,0", codewords, one, first_instance=0, field_ids=ids, rows=100)
    write_tile(zf, "0,0", codewords, many, first_instance=0, field_ids=ids, rows=2)
    assert pq.read_table(one).equals(pq.read_table(many))


def test_cell_id_from_arrow_gives_the_xenium_form():
    prefix, suffix = pa.array([0, 1, 0x1F, 2**32 - 1]), pa.array([1, 1, 2, 2**32 - 1])
    assert cell_ids_arrow(prefix, suffix).to_pylist() == [
        "aaaaaaaa-1", "aaaaaaab-1", "aaaaaabp-2", "pppppppp-4294967295",
    ]


def test_cell_id_from_arrow_equals_the_numpy_form_on_a_random_sample():
    rng = np.random.default_rng(0)
    prefix = rng.integers(0, 2**32, 20_000, dtype=np.uint32)
    suffix = rng.integers(0, 2**32, 20_000, dtype=np.uint32)
    got = cell_ids_arrow(pa.array(prefix.astype(np.int64)), pa.array(suffix.astype(np.int64)))
    assert got.to_pylist() == xenium_cell_id(prefix, suffix).tolist()


def test_tile_order_gives_running_first_instances():
    attrs = {"grid_keys": ["-1,0", "0,0", "0,1"], "grid_number_objects": [1, 5, 2]}
    assert tile_order(attrs) == [("-1,0", 0), ("0,0", 1), ("0,1", 6)]


def test_tile_order_refuses_lists_of_unequal_length():
    with pytest.raises(ValueError):
        tile_order({"grid_keys": ["0,0", "0,1"], "grid_number_objects": [1]})


def test_slices_hold_whole_source_chunks_and_decode_each_chunk_once(zf, codewords):
    log = _ReadLog(zf.filename)
    pieces = list(tile_slices(log, "0,0", codewords, first_instance=0, rows=5))
    assert [len(p) for p in pieces] == [4, 1]
    chunks = [m for m in log.read_members if not m.endswith(".zarray")]
    assert len(chunks) == len(set(chunks))


def test_write_tile_of_a_tile_with_no_rows_writes_no_row(tmp_path, tables, codewords):
    empty = zip_v2_store(
        tmp_path,
        "empty",
        {
            "grid/0,0/location": (np.zeros((0, 3), dtype="<f4"), {"chunks": (2, 3)}),
            "grid/0,0/codeword_identity": (np.zeros((0, 1), dtype="<u4"), {"chunks": (2, 1)}),
            "grid/0,0/cell_id": (np.zeros((0, 2), dtype="<u4"), {"chunks": (2, 2)}),
            "grid/0,0/quality_score": (np.zeros((0, 1), dtype="<f2"), {"chunks": (2, 1)}),
            "grid/0,0/overlaps_nucleus": (np.zeros((0, 1), dtype="u1"), {"chunks": (2, 1)}),
        },
    )
    path = tmp_path / "empty.parquet"
    assert write_tile(empty, "0,0", codewords, path, first_instance=0, field_ids=tables.field_ids("transcripts")) == 0
    assert pq.read_table(path).num_rows == 0
