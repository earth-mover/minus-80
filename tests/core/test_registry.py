from pathlib import Path

import obstore
import pytest

from minus80.core.registry import (
    CLIENT_OPTIONS,
    RETRY_CONFIG,
    as_url,
    file_path,
    registry_for,
    store_and_key,
)

DATA = bytes(range(256)) * 4
TOKEN = "token=abc&dl=1"


def test_query_string_reaches_the_server(range_server):
    range_server.files[f"/f.bin?{TOKEN}"] = DATA
    store, key = store_and_key(f"{range_server.url}/f.bin?{TOKEN}")
    assert obstore.head(store, key)["size"] == len(DATA)
    assert bytes(obstore.get_range(store, key, start=10, end=20)) == DATA[10:20]


def test_http_file_in_a_subdirectory_is_requested_at_its_own_path(range_server):
    range_server.files["/d/f.bin"] = DATA
    store, key = store_and_key(f"{range_server.url}/d/f.bin")
    assert bytes(obstore.get_range(store, key, start=0, end=4)) == DATA[:4]
    assert range_server.requests[-1][1] == "/d/f.bin"


@pytest.mark.parametrize("directory", ["sp ace", "pl+us", "pct%41"])
def test_local_file_in_an_encoded_directory_reads(tmp_path, directory):
    path = tmp_path / directory / "a b+c.bin"
    path.parent.mkdir()
    path.write_bytes(DATA)
    store, key = store_and_key(as_url(path))
    assert bytes(obstore.get_range(store, key, start=0, end=8)) == DATA[:8]


def test_registry_is_impatient_by_default(range_server):
    store, _ = store_and_key(f"{range_server.url}/d/f.bin")
    assert store.retry_config is None
    assert (store.client_options or {}).get("timeout") is None


def test_patient_store_carries_the_long_timeout_and_backoff(range_server):
    store, _ = store_and_key(f"{range_server.url}/d/f.bin", patient=True)
    assert store.client_options["timeout"] == CLIENT_OPTIONS["timeout"]
    assert store.client_options["connect_timeout"] == CLIENT_OPTIONS["connect_timeout"]
    assert store.retry_config["max_retries"] == RETRY_CONFIG["max_retries"]
    assert store.retry_config["retry_timeout"] == RETRY_CONFIG["retry_timeout"]


def test_patient_registry_carries_the_backoff(range_server):
    url = f"{range_server.url}/d/f.bin"
    store, _ = registry_for(url, patient=True).resolve(url)
    assert store.retry_config["max_retries"] == RETRY_CONFIG["max_retries"]


@pytest.mark.parametrize(("url", "expected"), [
    ("/data/a b.bin", Path("/data/a b.bin")),
    ("file:///data/a%20b.bin", Path("/data/a b.bin")),
    ("file:///data/a%2Bb/c.bin", Path("/data/a+b/c.bin")),
    ("https://example.org/a%20b.bin", None),
])
def test_file_path(url, expected):
    assert file_path(url) == expected


@pytest.mark.xfail(strict=True, raises=FileNotFoundError,
                   reason="obspec-utils resolve compares a decoded prefix with an encoded path "
                          "(docs/upstream.md)")
def test_registry_reads_a_file_in_an_encoded_directory(tmp_path):
    path = tmp_path / "sp ace" / "a.bin"
    path.parent.mkdir()
    path.write_bytes(DATA)
    url = as_url(path)
    store, key = registry_for(url).resolve(url)
    obstore.get_range(store, key, start=0, end=8)
