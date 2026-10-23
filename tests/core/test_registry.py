import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import obstore
import pytest

from minus80.core import registry
from minus80.core.registry import (
    CLIENT_OPTIONS,
    RETRY_CONFIG,
    as_url,
    bucket_region,
    file_path,
    registry_for,
    s3_region,
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


@pytest.fixture
def region_server():
    """Answers ``HEAD /<bucket>`` as S3 does: ``answers`` maps a bucket to a
    status and the region to report, and any other bucket is a bare 404."""
    answers: dict[str, tuple[int, str]] = {}
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            bucket = self.path.lstrip("/")
            hits.append(bucket)
            status, region = answers.get(bucket, (404, ""))
            self.send_response(status)
            if status == 301:
                self.send_header("Location", f"http://elsewhere.invalid/{bucket}")
            if region:
                self.send_header("x-amz-bucket-region", region)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    bucket_region.cache_clear()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", answers, hits
    bucket_region.cache_clear()
    httpd.shutdown()


@pytest.mark.parametrize("status", [200, 301, 403])
def test_bucket_region_is_read_from_the_header(region_server, status):
    endpoint, answers, _ = region_server
    answers["b"] = (status, "us-west-2")
    assert bucket_region("b", endpoint) == "us-west-2"


def test_bucket_region_is_looked_up_once_per_bucket(region_server):
    endpoint, answers, hits = region_server
    answers["b"] = (301, "eu-central-1")
    bucket_region("b", endpoint)
    bucket_region("b", endpoint)
    assert hits == ["b"]


def test_a_failed_lookup_raises_and_is_not_cached(region_server):
    endpoint, answers, hits = region_server
    with pytest.raises(OSError):
        bucket_region("b", endpoint)
    answers["b"] = (200, "ap-south-1")
    assert bucket_region("b", endpoint) == "ap-south-1"
    assert hits == ["b", "b"]


@pytest.fixture
def no_env_region(monkeypatch):
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ENDPOINT_URL", "AWS_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)


def test_s3_region_asks_for_the_bucket_region(region_server, no_env_region, monkeypatch):
    endpoint, answers, _ = region_server
    answers["b"] = (301, "us-west-2")
    monkeypatch.setattr(registry, "S3_REGION_ENDPOINT", endpoint)
    assert s3_region("b") == "us-west-2"


@pytest.mark.parametrize("var", ["AWS_REGION", "AWS_DEFAULT_REGION"])
def test_env_region_skips_the_lookup(no_env_region, monkeypatch, var):
    monkeypatch.setenv(var, "ca-central-1")
    monkeypatch.setattr(registry, "bucket_region", _no_lookup)
    assert s3_region("b") == "ca-central-1"


def test_custom_endpoint_skips_the_lookup(no_env_region, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setattr(registry, "bucket_region", _no_lookup)
    assert s3_region("b") is None


def test_failed_lookup_leaves_the_region_to_the_backend(no_env_region, monkeypatch):
    def unreachable(bucket, endpoint=None):
        raise OSError("no network")

    monkeypatch.setattr(registry, "bucket_region", unreachable)
    assert s3_region("b") is None


def _no_lookup(bucket, endpoint=None):
    raise AssertionError("looked up a region the environment already gave")


OPENCELL_TIFF = (
    "s3://czb-opencell/microscopy/raw/AAMP_ENSG00000127837/"
    "OC-FOV_AAMP_ENSG00000127837_CID001050_FID00026369_stack.tif"
)


@pytest.mark.skipif(
    os.environ.get("MINUS80_NETWORK_TESTS") != "1",
    reason="reads a public S3 bucket; set MINUS80_NETWORK_TESTS=1",
)
def test_public_bucket_outside_us_east_1_reads_without_a_region(no_env_region, monkeypatch):
    monkeypatch.setenv("MINUS80_S3_ANONYMOUS", "1")
    store, key = store_and_key(OPENCELL_TIFF)
    assert len(bytes(obstore.get_range(store, key, start=0, length=16))) == 16
