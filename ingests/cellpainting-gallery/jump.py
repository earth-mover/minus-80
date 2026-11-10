"""JUMP's own metadata for the plates of ``cpg0016-jump``, at a pinned commit.

The gallery holds no platemaps for ``cpg0016-jump``. What each well received,
what kind of plate it sits on and each source's pixel size are published in
`jump-cellpainting/datasets <https://github.com/jump-cellpainting/datasets>`_
under ``metadata/``:

- ``plate.csv.gz``: each plate's source, batch and plate type;
- ``microscope_config.csv``: each source's microscope and pixel size;
- ``well.csv.gz``: each well's JCP2022 identifier;
- ``compound.csv.gz``, ``orf.csv.gz`` and ``crispr.csv.gz``: what a JCP2022
  identifier is (an InChIKey, or a gene and its reagent);
- ``perturbation_control.csv``: which identifiers are controls.

Every table is read from one commit, so an ingest run today and one run a year
from now join against the same rows. The tables are read as strings with
blanks left blank: pandas would otherwise read an empty cell as NaN and an
identifier column with one empty cell as floats.

Fetching (:func:`fetch_tables`) is kept apart from the joins, which take the
tables as arguments, so a test or a second run can hand them local copies.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pandas as pd
from minus80.core.fetch import download_then_index

#: The head of jump-cellpainting/datasets' main branch on 2026-11-10, a merge
#: dated 2026-01-09; ``metadata/`` last changed at 2025-08-13.
COMMIT = "016e865fa0691244e0860943e41c7d6a88ed2580"
ROOT = f"https://raw.githubusercontent.com/jump-cellpainting/datasets/{COMMIT}/metadata/"
REPOSITORY = "https://github.com/jump-cellpainting/datasets"

TABLES = ("plate.csv.gz", "microscope_config.csv", "well.csv.gz", "compound.csv.gz",
          "orf.csv.gz", "crispr.csv.gz", "perturbation_control.csv")

#: How ``perturbation_control.csv``'s ``Metadata_pert_type`` maps onto a
#: well's ``pert_type``. An identifier it does not list is a treatment.
PERT_TYPES = {"negcon": "control", "poscon": "control", "empty": "empty"}

#: The per-well fields, in the order a well's attributes list them.
WELL_FIELDS = ("jcp2022", "pert_type", "control_type", "control_name", "inchikey",
               "gene_symbol", "broad_sample")


def fetch_tables() -> dict[str, Path]:
    """A local copy of every table, downloaded once into minus80's cache."""
    return {name: Path(download_then_index(ROOT + name)) for name in TABLES}


def read_tables(paths: Mapping[str, Path]) -> dict[str, pd.DataFrame]:
    """Each table as strings, with an empty cell as the empty string."""
    return {name: pd.read_csv(path, dtype=str, keep_default_na=False)
            for name, path in paths.items()}


def unique_by(table: pd.DataFrame, keys: list[str], name: str) -> pd.DataFrame:
    """``table`` checked to hold one row per key.

    A join against a table with a repeated key multiplies the wells it
    matches, and nothing downstream would notice.
    """
    repeated = table[table.duplicated(keys, keep=False)]
    if not repeated.empty:
        raise ValueError(f"{name}: {len(repeated)} rows repeat a key {keys}, "
                         f"e.g. {repeated[keys].iloc[0].tolist()}")
    return table


def plate_record(tables: Mapping[str, pd.DataFrame], source: str, batch: str,
                 plate: str) -> dict | None:
    """The plate's row in ``plate.csv.gz``, matched on source, batch and plate; None if absent."""
    plates = unique_by(tables["plate.csv.gz"],
                       ["Metadata_Source", "Metadata_Batch", "Metadata_Plate"], "plate.csv.gz")
    match = plates[(plates["Metadata_Source"] == source) & (plates["Metadata_Batch"] == batch)
                   & (plates["Metadata_Plate"] == plate)]
    return None if match.empty else match.iloc[0].to_dict()


def microscope(tables: Mapping[str, pd.DataFrame], source: str) -> dict:
    """The source's row of ``microscope_config.csv``, as the table states it."""
    config = unique_by(tables["microscope_config.csv"], ["Metadata_Source"],
                       "microscope_config.csv")
    match = config[config["Metadata_Source"] == source]
    if match.empty:
        raise KeyError(f"{source} is not in microscope_config.csv")
    return match.iloc[0].to_dict()


def well_annotations(tables: Mapping[str, pd.DataFrame], source: str,
                     plate: str) -> tuple[dict[str, dict], dict]:
    """Each well's perturbation, keyed by its ``Metadata_Well`` label, and the join coverage.

    ``well.csv.gz`` is matched on source, plate and well. Its JCP2022
    identifier is then looked up in the compound, ORF and CRISPR tables and in
    ``perturbation_control.csv``, each by exact identifier. A field no table
    fills is None. Wells without a row in ``well.csv.gz`` are left out of the
    mapping, so a caller sees them as absent rather than as blank treatments.

    ``pert_type`` is ``trt``, ``control`` or ``empty``; ``control_type`` is
    ``negcon`` or ``poscon`` on controls only, and ``control_name`` is the
    control's name in ``perturbation_control.csv``.
    """
    wells = unique_by(tables["well.csv.gz"], ["Metadata_Source", "Metadata_Plate", "Metadata_Well"],
                      "well.csv.gz")
    wells = wells[(wells["Metadata_Source"] == source) & (wells["Metadata_Plate"] == plate)]
    compound = unique_by(tables["compound.csv.gz"], ["Metadata_JCP2022"], "compound.csv.gz")
    orf = unique_by(tables["orf.csv.gz"], ["Metadata_JCP2022"], "orf.csv.gz")
    crispr = unique_by(tables["crispr.csv.gz"], ["Metadata_JCP2022"], "crispr.csv.gz")
    control = unique_by(tables["perturbation_control.csv"], ["Metadata_JCP2022"],
                        "perturbation_control.csv")
    inchikey = dict(zip(compound["Metadata_JCP2022"], compound["Metadata_InChIKey"]))
    orf_rows = orf.set_index("Metadata_JCP2022")
    crispr_symbol = dict(zip(crispr["Metadata_JCP2022"], crispr["Metadata_Symbol"]))
    control_rows = control.set_index("Metadata_JCP2022")

    out: dict[str, dict] = {}
    counts = {"compound": 0, "orf": 0, "crispr": 0, "control": 0, "no_jcp2022": 0}
    for label, jcp in zip(wells["Metadata_Well"], wells["Metadata_JCP2022"]):
        record = dict.fromkeys(WELL_FIELDS)
        if not jcp:
            counts["no_jcp2022"] += 1
            out[label] = record
            continue
        record["jcp2022"] = jcp
        if jcp in control_rows.index:
            row = control_rows.loc[jcp]
            record["pert_type"] = PERT_TYPES[row["Metadata_pert_type"]]
            if row["Metadata_pert_type"] in ("negcon", "poscon"):
                record["control_type"] = row["Metadata_pert_type"]
                record["control_name"] = row["Metadata_Name"] or None
            counts["control"] += 1
        else:
            record["pert_type"] = "trt"
        if jcp in inchikey:
            record["inchikey"] = inchikey[jcp] or None
            counts["compound"] += 1
        if jcp in orf_rows.index:
            row = orf_rows.loc[jcp]
            record["gene_symbol"] = row["Metadata_Symbol"] or None
            record["broad_sample"] = row["Metadata_broad_sample"] or None
            counts["orf"] += 1
        if jcp in crispr_symbol:
            record["gene_symbol"] = crispr_symbol[jcp] or None
            counts["crispr"] += 1
        out[label] = record
    coverage = {"wells_in_well_table": len(out), "matched": counts}
    return out, coverage
