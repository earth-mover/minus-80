"""FLAC references read back through Icechunk match libsndfile's decode of the file."""

import numpy as np
import pytest
import zarr

sf = pytest.importorskip("soundfile")
pytest.importorskip("av")
pytest.importorskip("flac_numcodecs.zarr3")

from minus80.core import as_url  # noqa: E402
from minus80.core.icechunk_io import create_repo  # noqa: E402
from minus80.parsers.flac import ChunkTable, chunk_table, scan_frames, write_array  # noqa: E402

RATE = 48_000


def flac(path, n_samples, channels=1, rate=RATE, subtype="PCM_16", seed=0):
    """A FLAC file of noise, which no predictor compresses to nothing."""
    rng = np.random.default_rng(seed)
    samples = (rng.standard_normal((n_samples, channels)) * 3000).astype("int16")
    sf.write(path, samples, rate, subtype=subtype, format="FLAC")
    return str(path)


def read_back(tmp_path, paths, frames_per_chunk=1, attributes=None):
    """Write ``paths`` as one array to a local Icechunk repo and read it back."""
    table = chunk_table([scan_frames(p) for p in paths], frames_per_chunk=frames_per_chunk)
    repo = create_repo(tmp_path / "repo", [as_url(p) for p in paths])
    session = repo.writable_session("main")
    write_array(zarr.open_group(session.store, mode="w"), "audio", table,
                attributes=attributes)
    session.commit("audio")
    with zarr.config.set({"array.rectilinear_chunks": True}):
        array = zarr.open_group(repo.readonly_session("main").store, mode="r")["audio"]
        return table, array, array[:]


def assert_same_frames(a, b):
    for field in ("sample_rate", "channels", "sample_format", "offsets", "lengths", "samples"):
        assert np.array_equal(getattr(a, field), getattr(b, field)), field


def test_scan_covers_the_file(tmp_path):
    path = flac(tmp_path / "a.flac", 50_000)
    scan = scan_frames(path)
    assert scan.url == as_url(path)
    assert (scan.sample_rate, scan.channels, scan.n_samples) == (RATE, 1, sf.info(path).frames)
    assert scan.offsets[-1] + scan.lengths[-1] == (tmp_path / "a.flac").stat().st_size


def test_remote_scan_matches_local(tmp_path, range_server):
    path = flac(tmp_path / "a.flac", 300_000, channels=2)
    range_server.files["/a.flac"] = (tmp_path / "a.flac").read_bytes()
    url = f"{range_server.url}/a.flac"
    scan = scan_frames(url)
    assert scan.url == url
    assert_same_frames(scan, scan_frames(path))
    # One block covers the whole file, so the scan is one request.
    assert len(range_server.gets()) == 1


def test_open_file_needs_url(tmp_path):
    path = flac(tmp_path / "a.flac", 20_000)
    with open(path, "rb") as f:
        with pytest.raises(TypeError, match="url is required"):
            scan_frames(f)
        scan = scan_frames(f, url="s3://bucket/a.flac")
    assert scan.url == "s3://bucket/a.flac"
    assert_same_frames(scan, scan_frames(path))


def test_mono_reads_back_identical(tmp_path):
    path = flac(tmp_path / "a.flac", 50_000)
    table, array, got = read_back(tmp_path, [path], frames_per_chunk=3,
                                  attributes={"gain": float("nan")})
    want, _ = sf.read(path, dtype="int16", always_2d=True)
    assert array.metadata.dimension_names == ("time", "channel")
    assert array.attrs["gain"] is None
    assert got.shape == want.shape
    assert np.array_equal(got, want)
    # The short last chunk is where a wrong grid shows.
    assert table.edge[-1] < table.edge[0]
    assert np.array_equal(array[-100:], want[-100:])


def test_stereo_reads_back_identical(tmp_path):
    path = flac(tmp_path / "a.flac", 30_000, channels=2)
    _, array, got = read_back(tmp_path, [path])
    want, _ = sf.read(path, dtype="int16")
    assert array.metadata.dimension_names == ("time", "channel")
    assert np.array_equal(got, want)


def test_files_concatenate_on_one_time_axis(tmp_path):
    paths = [flac(tmp_path / f"{i}.flac", n, seed=i)
             for i, n in enumerate([50_000, 9_000, 31_234])]
    table, _, got = read_back(tmp_path, paths, frames_per_chunk=4)
    wants = [sf.read(p, dtype="int16", always_2d=True)[0] for p in paths]
    assert np.array_equal(got, np.concatenate(wants))
    assert np.array_equal(table.n_samples, [len(w) for w in wants])
    for start, want in zip(table.start_sample, wants):
        assert np.array_equal(got[start:start + len(want)], want)
    # No chunk spans two files.
    assert list(np.cumsum(table.edge)[np.r_[np.flatnonzero(np.diff(table.rec)), -1]]) == \
        list(np.cumsum(table.n_samples))


def test_table_round_trips_through_npz(tmp_path):
    path = flac(tmp_path / "a.flac", 20_000)
    table = chunk_table([scan_frames(path)], frames_per_chunk=2)
    table.save(tmp_path / "table.npz")
    loaded = ChunkTable.load(tmp_path / "table.npz")
    for field in ChunkTable.__dataclass_fields__:
        assert np.array_equal(getattr(loaded, field), getattr(table, field))


def test_refuses_24_bit(tmp_path):
    path = flac(tmp_path / "a.flac", 10_000, subtype="PCM_24")
    with pytest.raises(ValueError, match="only 16-bit"):
        chunk_table([scan_frames(path)], frames_per_chunk=1)


def test_refuses_files_that_need_different_codecs(tmp_path):
    a = flac(tmp_path / "a.flac", 10_000)
    b = flac(tmp_path / "b.flac", 10_000, rate=44_100)
    with pytest.raises(ValueError, match="disagree"):
        chunk_table([scan_frames(a), scan_frames(b)], frames_per_chunk=1)


def test_raises_when_no_container_matches(tmp_path):
    path = flac(tmp_path / "a.flac", 10_000)
    table = chunk_table([scan_frames(path)], frames_per_chunk=1)
    other = tmp_path / "elsewhere"
    other.mkdir()
    repo = create_repo(tmp_path / "repo", [as_url(other / "x.flac")])
    session = repo.writable_session("main")
    with pytest.raises(ValueError, match="no virtual chunk container"):
        write_array(zarr.open_group(session.store, mode="w"), "audio", table)
