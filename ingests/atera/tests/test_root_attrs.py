import json
from pathlib import Path

import ingest
from root_attrs import (
    KIND_SENTENCES, ROOT_KEYS, citation_text, describe, element_kind, markdown_tables, parse_metrics, root_attrs,
    root_changes, source_objects,
)
from sources import SAMPLES

LAYOUTS = (Path(__file__).parents[1] / "LAYOUTS.md").read_text()
KIDNEY = SAMPLES["kidney"]


def test_markdown_tables_keys_rows_by_header():
    text = "intro\n\n| A | B |\n|---|---|\n| 1 | `x` |\n| 2 | y |\n\nafter\n\n| C |\n|---|\n| 3 |\n"
    assert markdown_tables(text) == [[{"A": "1", "B": "x"}, {"A": "2", "B": "y"}], [{"C": "3"}]]


def test_source_objects_reads_the_five_kidney_objects():
    objects = source_objects(LAYOUTS, KIDNEY)
    assert [o["url"].rsplit("_FFPE", 1)[1] for o in objects] == [
        "_outs.zip", "_he_image.ome.tif", "_he_imagealignment.csv", "_experiment.spatial",
        "_analysis_summary.html",
    ]
    assert objects[0] == {
        "url": KIDNEY.url("_outs.zip"),
        "size": 45_720_445_144,
        "etag": "8c1c07343dbe1b946f5f90a21130782f-5451",
        "last_modified": "2026-10-15",
    }


def test_citation_text_matches_the_rendered_citations():
    rendered = " ".join(LAYOUTS.split())
    for sample in SAMPLES.values():
        assert citation_text(sample) in rendered


def test_parse_metrics_types_each_value():
    text = "a,b,c,d,e\n12,0.5,,S2,NaN\n"
    assert parse_metrics(text) == {"a": 12, "b": 0.5, "c": None, "d": "S2", "e": "NaN"}


def test_root_attrs_splits_experiment_and_names_the_fixture():
    experiment = {
        "run_name": "R", "sample_type": "FFPE", "region_name": "S2B", "slide_name": "S2",
        "panel_wta": {"panel_organism": "Human"}, "pixel_size": 0.2,
        "analysis_sw_version": "atera-1.0.4.1", "instrument_sw_version": "1.0.0.79",
    }
    attrs = root_attrs(KIDNEY, LAYOUTS, experiment, {"m": 1}, kinds=["labels"], missing=[], fixture="one tile")
    json.dumps(attrs, allow_nan=False)
    assert attrs["spatialdata_attrs"] == {"version": "0.2"}
    assert attrs["title"] == KIDNEY.title
    assert attrs["citation"]["software_version_full"] == "atera-1.0.4.1"
    assert attrs["citation"]["instrument_software"] == "1.0.0.79"
    assert attrs["citation"]["text"] == citation_text(KIDNEY)
    assert attrs["sample"]["species"] == "Human"
    assert attrs["sample"]["anatomical_entity"] == "Kidney"
    assert attrs["sample"]["biomaterials"].startswith("A non-diseased human kidney")
    assert attrs["sample"]["sample_type"] == "FFPE"
    assert attrs["panel"] == {"panel_wta": {"panel_organism": "Human"}}
    assert attrs["run"] == {"run_name": "R", "pixel_size": 0.2, "analysis_sw_version": "atera-1.0.4.1",
                            "instrument_sw_version": "1.0.0.79"}
    assert attrs["metrics"] == {"m": 1}
    assert attrs["fixture"] == "one tile"
    assert "0.8.0 cannot open" in attrs["reading"]["spatialdata"]
    assert "authorize_virtual_chunk_access" in attrs["reading"]["virtual_chunks"]
    assert "iceberg_table" in attrs["reading"]["zax"]
    assert "iceberg_tables" in attrs["reading"]["tables"]
    assert len(attrs["source"]) == 5
    assert attrs["documentation"] == {
        "dataset_page": KIDNEY.page,
        "output_formats": "https://www.10xgenomics.com/support/software/atera-onboard-analysis/latest/analysis/output-at-a-glance",
        "software": "https://www.10xgenomics.com/support/software/atera-onboard-analysis/latest",
    }
    assert attrs["description"] == describe(["labels"])
    assert tuple(attrs) == (*ROOT_KEYS, "fixture")


def test_root_changes_compares_json_values_in_the_order_of_the_built_keys():
    present = {"a": [1, 2], "b": {"x": 1}, "c": 1, "d": "kept"}
    built = {"e": None, "c": 1.0, "b": {"x": 1}, "a": (1, 2)}
    assert root_changes(present, built) == ["e", "c"]


KINDS = ("images/morphology", "images/morphology_3d", "images/he_image", "images/qc", "labels", "shapes",
         "points/transcripts", "tables/table", "iceberg_tables")


def _kinds(mode):
    return {element_kind(e) for e in ingest.ELEMENTS[mode]}


def test_each_kind_sentence_is_short_and_names_its_element():
    assert tuple(KIND_SENTENCES) == KINDS
    for kind, sentence in KIND_SENTENCES.items():
        assert kind in sentence
        assert len(sentence.split()) <= 20 and sentence.count(". ") == 0 and sentence.endswith(".")
        assert "\u2014" not in sentence


def test_each_element_of_the_full_mode_except_root_has_a_kind_sentence_and_each_sentence_an_element():
    assert {element_kind(e) for e in ingest.ELEMENTS["full"]} - {"root"} == set(KIND_SENTENCES)


def test_the_full_description_holds_the_sentence_of_each_kind():
    description = describe(_kinds("full"))
    assert all(sentence in description for sentence in KIND_SENTENCES.values())


def test_the_fixture_description_holds_only_the_sentences_of_the_kinds_that_the_fixture_writes():
    description = describe(_kinds("fixture"))
    lacking = ("images/morphology_3d", "images/he_image", "images/qc", "iceberg_tables")
    assert [k for k in KINDS if KIND_SENTENCES[k] not in description] == list(lacking)
