"""Phase 2 run end to end on the small scan from fixture.py: the NWB file, the masks, the units and two CAVE versions.

    uv run --package microns-ingest pytest ingests/microns/test_processed.py
"""

import gzip

import fixture
import ingest
import numpy as np
import pandas as pd
import processed
import pytest
import verify_processed
import zarr

from minus80.core import as_url
from minus80.core.icechunk_io import create_repo

NAME = f"{fixture.SESSION}-{fixture.SCAN}"
#: A second scan, which the match tables cover in one version only.
OTHER_SCAN = fixture.SCAN + 1
OTHER = f"{fixture.SESSION}-{OTHER_SCAN}"
TABLE = "coregistration_manual_v4"
COLUMNS = {"id": "int64", "target_id": "int64", "session": "int64", "scan_idx": "int64",
           "unit_id": "int64", "field": "int64", "residual": "float64", "score": "float64",
           "volume": "float64", "pt_supervoxel_id": "int64", "pt_root_id": "int64",
           "pt_position_x": "int64", "pt_position_y": "int64", "pt_position_z": "int64"}
#: Root IDs above 2^53, which float64 would round.
ROOT = 864691135348268503


def match_row(row_id, unit, field, root, session=fixture.SESSION, scan=fixture.SCAN):
    return [row_id, 500 + row_id, session, scan, unit, field, 1.5, 9.0, 300.0,
            105487283075464806 + row_id, root, 296208 + row_id, 94688, 15803]


def write_dump(directory, version, tables):
    """A CAVE static dump: header-less gzip CSVs, each with a header file of names and types."""
    folder = directory / f"v{version}"
    folder.mkdir(parents=True)
    for name, rows in tables.items():
        (folder / f"{name}_merged_header.csv").write_text(
            "".join(f"{c},{t}\n" for c, t in COLUMNS.items()))
        body = "".join(",".join(str(v) for v in row) + "\n" for row in rows)
        (folder / f"{name}_merged.csv.gz").write_bytes(gzip.compress(body.encode()))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    source = tmp_path_factory.mktemp("source")
    truth = fixture.build(source)
    other = fixture.build(source / "other")
    scan, second = (ingest.Scan(fixture.ANIMAL, fixture.SESSION, number,
                                tiffs={v: as_url(p) for v, p in files.tiffs.items()},
                                nwb=as_url(files.nwb), nwb_path=files.nwb.name)
                    for number, files in ((fixture.SCAN, truth), (OTHER_SCAN, other)))
    repo = create_repo(tmp_path_factory.mktemp("repo"), [], prefixes=[as_url(source) + "/"])
    facts = [ingest.read_nwb(scan), ingest.read_nwb(second)]
    ingest.commit_export(repo, facts, 1, parallel=1)
    first = ingest.committed_exports(repo)[1]
    ingest.commit_export(repo, facts, 2, parallel=1, earlier=ingest.sample_pages(first))
    processed.commit_processed(repo, [scan, second], parallel=1)
    dumps = tmp_path_factory.mktemp("cave")
    # Field 2's first ROI is unit ROIS[0] + 1; field 4's last is the scan's last unit.
    first_of_2, last = fixture.ROIS[0] + 1, sum(fixture.ROIS)
    rows = [match_row(1, first_of_2, 2, ROOT), match_row(2, last, 4, ROOT + 7)]
    # Version 1 repeats a row under another id, as v343's functional_coreg does.
    # Its TABLE has no rows for the second scan, and its other table one.
    write_dump(dumps, 1, {TABLE: rows + [[3] + rows[0][1:]], "apl_functional_coreg_vess_fwd":
                          rows + [match_row(4, last, 4, ROOT + 9, scan=OTHER_SCAN)]})
    write_dump(dumps, 2, {TABLE: [match_row(1, first_of_2, 2, ROOT + 2), rows[1],
                                  match_row(9, 1, 1, ROOT, scan=99)]})
    base = as_url(dumps) + "/"
    published = {NAME, OTHER}
    snapshots = [processed.commit_cave_version(repo, v, [scan, second], published, base=base)
                 for v in (1, 2)]
    return repo, [scan, second], truth, snapshots, base


def scan_group(repo, name=NAME, **ref):
    return zarr.open_group(repo.readonly_session(**ref).store, mode="r")[f"{ingest.SCANS}/{name}"]


def test_every_mask_is_the_nwb_mask_one_chunk_per_roi(built):
    repo, _, truth, _, _ = built
    group = scan_group(repo, branch="main")
    for n in range(1, fixture.FIELDS + 1):
        image = group[f"rois-{n}/0"]
        assert image.shape == (fixture.ROIS[n - 1], 1, fixture.ROWS, fixture.COLUMNS)
        assert image.chunks == (1, 1, fixture.ROWS, fixture.COLUMNS)
        np.testing.assert_array_equal(image[:, 0], truth.masks[n])


def test_the_nwb_file_sits_whole_under_the_scan(built):
    repo, _, truth, _, _ = built
    group = scan_group(repo, branch="main")
    assert group["nwb"].attrs["neurodata_type"] == "NWBFile"
    traces = group["nwb/processing/ophys/Fluorescence/RoiResponseSeries2/data"]
    assert traces.metadata.dimension_names == ("time", "roi")
    masks = group["nwb/processing/ophys/ImageSegmentation/PlaneSegmentation2/image_mask"]
    assert masks.metadata.dimension_names[0] == "roi"
    np.testing.assert_array_equal(group["nwb/acquisition/TwoPhotonSeries1/data"][:],
                                  truth.pages[0::fixture.FIELDS].transpose(0, 2, 1))


def test_units_follow_the_fields_in_order(built):
    repo, _, _, _, _ = built
    units = scan_group(repo, branch="main")["units"]
    want_field = np.repeat(np.arange(1, fixture.FIELDS + 1), fixture.ROIS)
    np.testing.assert_array_equal(units["field"][:], want_field)
    np.testing.assert_array_equal(units["roi"][:], np.concatenate([np.arange(1, k + 1) for k in fixture.ROIS]))


def test_each_version_holds_its_own_root_ids_exactly(built):
    repo, _, _, snapshots, _ = built
    first, second = (scan_group(repo, snapshot_id=s)["units/em"] for s in snapshots)
    unit = fixture.ROIS[0]
    assert first[f"{TABLE}/pt_root_id"][unit] == ROOT
    assert second[f"{TABLE}/pt_root_id"][unit] == ROOT + 2
    assert second[f"{TABLE}/pt_root_id"][0] == 0
    assert list(second[f"{TABLE}/pt_position"][unit]) == [296209, 94688, 15803]
    assert sorted(first.group_keys()) == ["apl_functional_coreg_vess_fwd", TABLE]
    assert sorted(second.group_keys()) == [TABLE]


def test_a_scan_gets_only_the_tables_with_rows_for_it(built):
    repo, _, _, snapshots, base = built
    first, second = (scan_group(repo, OTHER, snapshot_id=s)["units/em"] for s in snapshots)
    assert sorted(first.group_keys()) == ["apl_functional_coreg_vess_fwd"]
    assert first["apl_functional_coreg_vess_fwd/pt_root_id"][sum(fixture.ROIS) - 1] == ROOT + 9
    assert sorted(second.group_keys()) == []
    import random
    for snapshot_id in snapshots:
        snapshot = next(s for s in repo.ancestry(branch="main") if s.id == snapshot_id)
        assert verify_processed.matches(repo, snapshot, [NAME, OTHER], random.Random(0), base) == []


def test_a_version_changes_only_what_changed(built):
    repo, _, _, snapshots, _ = built
    diff = repo.diff(from_snapshot_id=snapshots[0], to_snapshot_id=snapshots[1])
    dropped = {f"/{ingest.SCANS}/{name}/units/em/apl_functional_coreg_vess_fwd" for name in (NAME, OTHER)}
    changed = {path.rsplit("/", 1)[1] for path in diff.updated_chunks
               if not any(path.startswith(g + "/") for g in dropped)}
    assert changed == {"pt_root_id"}
    assert diff.deleted_groups == dropped
    metadata = next(s for s in repo.ancestry(branch="main") if s.id == snapshots[1]).metadata
    assert metadata["tables"][TABLE]["rows_for_scans_without_nwb"] == {"4-99": 1}


def test_a_row_that_breaks_the_unit_rule_is_refused():
    frame = pd.DataFrame([match_row(1, 1, 2, ROOT)], columns=list(COLUMNS)).astype(COLUMNS)
    with pytest.raises(ValueError, match="disagrees with the unit rule"):
        processed.unit_rows(TABLE, frame, {NAME: dict(enumerate(fixture.ROIS, 1))}, {NAME})


def test_two_different_rows_for_one_unit_are_refused():
    frame = pd.DataFrame([match_row(1, 4, 2, ROOT), match_row(2, 4, 2, ROOT + 1)],
                         columns=list(COLUMNS)).astype(COLUMNS)
    with pytest.raises(ValueError, match="two different rows"):
        processed.unit_rows(TABLE, frame, {NAME: dict(enumerate(fixture.ROIS, 1))}, {NAME})


def test_store_checks_pass(built):
    repo, _, _, _, _ = built
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    assert verify_processed.complete(root, [NAME, OTHER]) == []
    assert verify_processed.images(root, [NAME, OTHER]) == []
    chains = verify_processed.codecs(root)
    assert all(set(chain) <= verify_processed.READABLE_CODECS for chain in chains), chains


def test_a_rerun_writes_nothing(built):
    repo, scans, _, _, base = built
    before = [s.id for s in repo.ancestry(branch="main")]
    processed.commit_processed(repo, scans, parallel=1)
    for v in (1, 2):
        processed.commit_cave_version(repo, v, scans, {NAME, OTHER}, base=base)
    assert [s.id for s in repo.ancestry(branch="main")] == before
