import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from minus80.core.registry import file_path
from minus80.core.tables import TableWriter, local_catalog, parquet_field_ids

from .stores import zip_v2_store
from tables_extra import codewords_table, diffexp_table
from transcripts import CATEGORY_COLUMNS

CSV = (
    "Feature ID,Feature Name,Cone-OFF-bipolar cell Mean Counts,Cone-OFF-bipolar cell Log2 fold change,"
    "Cone-OFF-bipolar cell Adjusted p value\n"
    "ENSG1,A,0.5,-1.25,0.01\n"
    "ENSG2,B,2.0,0.5,\n"
)


@pytest.fixture
def zf(tmp_path):
    category = np.zeros((4, 9), dtype=bool)
    category[[0, 1, 3], 0] = True
    category[2, 1] = True
    return zip_v2_store(
        tmp_path,
        "transcripts",
        {"codeword_category": (category, {"order": "C"})},
        attrs={"codeword_gene_names": ["GENE1", "GENE2", "NegControl", "GENE1"], "codeword_gene_mapping": [0, 1, 2, 0]},
    )


def test_codewords_table_maps_codewords_to_features(zf):
    t = codewords_table(zf)
    assert t.column_names == ["codeword_index", "feature_index", "feature_name", *CATEGORY_COLUMNS]
    assert t["codeword_index"].to_pylist() == [0, 1, 2, 3]
    assert t["feature_name"].to_pylist() == ["GENE1", "GENE2", "NegControl", "GENE1"]
    assert t["is_gene"].to_pylist() == [True, True, False, True]
    assert t["is_negative_control_codeword"].to_pylist() == [False, False, True, False]
    assert t.schema.field("codeword_index").type == pa.int64()
    assert t.schema.field("feature_index").type == pa.int32()
    assert not t.schema.field("codeword_index").nullable


def test_diffexp_table_keeps_the_csv_columns_and_infers_numbers():
    t = diffexp_table(CSV)
    assert t.column_names == [
        "Feature ID", "Feature Name", "Cone-OFF-bipolar cell Mean Counts",
        "Cone-OFF-bipolar cell Log2 fold change", "Cone-OFF-bipolar cell Adjusted p value",
    ]
    assert t["Cone-OFF-bipolar cell Mean Counts"].type == pa.float64()
    assert t["Cone-OFF-bipolar cell Adjusted p value"].to_pylist() == [0.01, None]
    assert t["Feature ID"].to_pylist() == ["ENSG1", "ENSG2"]
    assert not t.schema.field("Feature ID").nullable


def test_diffexp_table_refuses_a_missing_feature_id():
    with pytest.raises(ValueError):
        diffexp_table("Feature ID,Feature Name\n,A\n")


def test_a_table_with_spaces_and_hyphens_in_column_names_round_trips_through_iceberg(tmp_path):
    table = diffexp_table(CSV)
    tables = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local")
    tables.require("diffexp", schema=table.schema, properties={})
    ref = tables.write("diffexp", table, index_column="Feature ID")
    footer = pq.read_schema(file_path(ref.files[0].location))
    assert all(i is not None for i in parquet_field_ids(footer).values())
    assert tables.catalog.load_table("ns.diffexp").scan().to_arrow().sort_by("Feature ID").equals(table)


def test_diffexp_table_types_a_column_without_values_as_float64():
    t = diffexp_table("Feature ID,Feature Name,G Mean Counts\nENSG1,A,\nENSG2,B,\n")
    assert t["G Mean Counts"].type == pa.float64()
    assert t["G Mean Counts"].to_pylist() == [None, None]


def test_diffexp_table_refuses_a_repeated_column_name():
    with pytest.raises(ValueError, match="G Mean Counts"):
        diffexp_table("Feature ID,G Mean Counts,G Mean Counts\nENSG1,1,2\n")
