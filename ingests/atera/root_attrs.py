"""The root attributes of an Atera repo, from ``experiment.spatial``, the metrics CSV and ``LAYOUTS.md``."""

from __future__ import annotations

import calendar
import csv
import io
import math

from minus80.ome.spatialdata import container_attrs
from sources import Sample

PRODUCT = "Atera In Situ"
#: The software and output-format pages that the dataset pages link to.
SOFTWARE_DOCS = "https://www.10xgenomics.com/support/software/atera-onboard-analysis/latest"
LICENSE = {
    "name": "Creative Commons Attribution 4.0 International (CC BY 4.0)",
    "url": "https://creativecommons.org/licenses/by/4.0/",
}

#: What each dataset page labels the sample, as LAYOUTS.md, "What each page states", records it.
PAGE_LABELS = {
    "atera-kidney": {"anatomical_entity": "Kidney", "disease_state": "Healthy"},
    "atera-brain": {"anatomical_entity": "Brain", "disease_state": "Healthy"},
    "atera-colorectal": {"anatomical_entity": "Colon", "disease_state": "Colorectal cancer"},
    "atera-breast": {"anatomical_entity": "Breast", "disease_state": "Breast cancer"},
}

SAMPLE_KEYS = ("sample_type", "region_name", "slide_name", "slide_id", "slide_type")
PANEL_KEYS = ("panel_config", "panel_wta", "total_num_targets")

DESCRIPTION = (
    "A SpatialData store of one 10x Atera whole-transcriptome sample. "
    "images/morphology holds the four morphology stains as (c, y, x) uint16. "
    "labels holds the nucleus and cell masks. tables/table is the AnnData cell by gene matrix. "
    "points/transcripts and shapes/*_boundaries are Iceberg tables beside the repo. "
    "Each of those also holds its Parquet files as uint8 arrays. "
    "The global coordinate system is in micrometres."
)

READING = {
    "spatialdata": "Released spatialdata 0.8.0 cannot open an Icechunk store.",
    "shim": (
        "The spatialdata PR #1107 branch with the virtual_omics patch series opens every element. "
        "It needs icechunk_spatialdata.parquet_in_store() for points and shapes."
    ),
    "virtual_chunks": (
        "The uint8 arrays reference the Parquet files in the tables' bucket. "
        "Open the repo with authorize_virtual_chunk_access for that virtual chunk container."
    ),
    "rectilinear_chunks": "The uint8 arrays have a rectilinear chunk grid. Set zarr.config array.rectilinear_chunks to True.",
    "zax": "zax reads points and shapes from the Iceberg table that each group's iceberg_table attribute gives.",
    "tables": (
        "The root attribute iceberg_tables of a full store pins the codewords and differential expression tables, "
        "which no element holds. Each entry has the keys of an iceberg_table attribute."
    ),
}


def markdown_tables(text: str) -> list[list[dict[str, str]]]:
    """Each pipe table in ``text`` as rows keyed by its header, with code backticks removed."""
    tables, lines = [], text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("|") and i + 1 < len(lines) and lines[i + 1].startswith("|---"):
            header = _cells(lines[i])
            rows, i = [], i + 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(dict(zip(header, _cells(lines[i]))))
                i += 1
            tables.append(rows)
        else:
            i += 1
    return tables


def _cells(line: str) -> list[str]:
    return [c.strip().strip("`") for c in line.strip().strip("|").split("|")]


def _rows_for(text: str, columns: set[str], repo: str) -> list[dict[str, str]]:
    """Rows of every table with ``columns`` whose ``Repo`` is ``repo``; a blank ``Repo`` repeats the row above."""
    rows = []
    for table in markdown_tables(text):
        if not table or not columns <= set(table[0]):
            continue
        current = None
        for row in table:
            current = row["Repo"] or current
            if current == repo:
                rows.append(row)
    return rows


def source_objects(text: str, sample: Sample) -> list[dict]:
    """The sample's objects, with the size, ETag and date that LAYOUTS.md records."""
    rows = _rows_for(text, {"Repo", "Object", "Size", "ETag", "Last modified"}, sample.repo)
    return [
        {
            "url": sample.url(row["Object"].removeprefix(sample.name)),
            "size": int(row["Size"].replace(",", "")),
            "etag": row["ETag"],
            "last_modified": row["Last modified"],
        }
        for row in rows
    ]


def citation_text(sample: Sample) -> str:
    """The dataset citation in 10x's format."""
    year, month, day = map(int, sample.published.split("-"))
    return (
        f"{sample.title}, {PRODUCT} Dataset by {sample.software}, 10x Genomics, "
        f"({year}, {calendar.month_name[month]} {day})."
    )


def parse_metrics(text: str) -> dict:
    """The one data row of a metrics CSV: integers and finite floats as numbers, empty as None."""
    header, row = list(csv.reader(io.StringIO(text)))[:2]
    return dict(zip(header, map(_value, row), strict=True))


def _value(text: str):
    if text == "":
        return None
    for kind in (int, float):
        try:
            value = kind(text)
        except ValueError:
            continue
        # JSON has no NaN, so a non-finite value stays as the text the CSV gives.
        return value if math.isfinite(value) else text
    return text


def root_attrs(
    sample: Sample, layouts: str, experiment: dict, metrics: dict, *, missing: list, fixture: str | None = None
) -> dict:
    """All root attributes. ``layouts`` is the text of LAYOUTS.md; ``fixture`` says what a fixture branch leaves out."""
    page = _rows_for(layouts, {"Repo", "Biomaterials"}, sample.repo)[0]
    citation = {
        "text": citation_text(sample),
        "dataset_title": sample.title,
        "product": PRODUCT,
        "software": f"10x Genomics {sample.software}",
        "software_version_full": experiment["analysis_sw_version"],
        "instrument_software": experiment["instrument_sw_version"],
        "publisher": "10x Genomics",
        "published": sample.published,
        "url": sample.page,
        "doi": None,
        "doi_note": "10x lists no publication for Atera, so the dataset has no DOI.",
    }
    attrs = {
        **container_attrs(),
        "title": sample.title,
        "description": DESCRIPTION,
        "source": source_objects(layouts, sample),
        "documentation": {
            "dataset_page": sample.page,
            "output_formats": f"{SOFTWARE_DOCS}/analysis/output-at-a-glance",
            "software": SOFTWARE_DOCS,
        },
        "license": LICENSE,
        "citation": citation,
        "reading": READING,
        "sample": {
            "species": experiment["panel_wta"]["panel_organism"],
            **PAGE_LABELS[sample.repo],
            "biomaterials": page["Biomaterials"],
            "preparation": page["Preparation"],
            "instrument_and_analysis": page["Instrument and analysis"],
            **{k: experiment[k] for k in SAMPLE_KEYS if k in experiment},
        },
        "panel": {k: experiment[k] for k in PANEL_KEYS if k in experiment},
        "run": {k: v for k, v in experiment.items() if k not in SAMPLE_KEYS + PANEL_KEYS},
        "metrics": metrics,
        "missing": missing,
    }
    if fixture is not None:
        attrs["fixture"] = fixture
    return attrs
