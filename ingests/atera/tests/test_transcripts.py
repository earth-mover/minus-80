import numpy as np
import pyarrow as pa
import pytest

from .stores import zip_v2_store
from transcripts import (
    CATEGORY_COLUMNS,
    TRANSCRIPT_SCHEMA,
    codeword_table,
    tile_table,
    xenium_cell_id,
)


@pytest.fixture
def zf(tmp_path):
    category = np.zeros((4, 9), dtype=bool)
    category[[0, 1, 3], 0] = True
    category[2, 1] = True
    tile = {
        "grid/0,0/location": (np.array([[1.5, 2.5, 3.5]] * 5, dtype="<f4") + np.arange(5)[:, None].astype("<f4") * [0, 0, 1], {"order": "F"}),
        "grid/0,0/codeword_identity": (np.array([[0], [1], [2], [3], [0]], dtype="<u4"), {}),
        "grid/0,0/cell_id": (np.array([[1, 1], [0x1F, 1], [0, 1], [0, 1], [2**32 - 1, 2**32 - 1]], dtype="<u4"), {"order": "F"}),
        "grid/0,0/quality_score": (np.linspace(10, 40, 5, dtype="<f2")[:, None], {}),
        "grid/0,0/overlaps_nucleus": (np.array([[1], [0], [0], [1], [0]], dtype="u1"), {}),
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
