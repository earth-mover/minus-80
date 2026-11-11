import json
from pathlib import Path

from root_attrs import citation_text, markdown_tables, parse_metrics, root_attrs, source_objects
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
    attrs = root_attrs(KIDNEY, LAYOUTS, experiment, {"m": 1}, missing=[], fixture="one tile")
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
    assert len(attrs["source"]) == 5
    assert attrs["documentation"] == {
        "dataset_page": KIDNEY.page,
        "output_formats": "https://www.10xgenomics.com/support/software/atera-onboard-analysis/latest/analysis/output-at-a-glance",
        "software": "https://www.10xgenomics.com/support/software/atera-onboard-analysis/latest",
    }
