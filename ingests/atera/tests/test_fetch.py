import hashlib
import http.server
import threading

import pytest

import fetch
from sources import SAMPLES

MIB = 1 << 20


def multipart_etag(data: bytes, part: int) -> str:
    digests = [hashlib.md5(data[i:i + part]).digest() for i in range(0, len(data), part)]
    return f"{hashlib.md5(b''.join(digests)).hexdigest()}-{len(digests)}"


class Bucket:
    """A local HTTP server that holds objects, serves Range reads and can cut a stream short."""

    def __init__(self, objects: dict[str, tuple[bytes, str]]):
        self.objects, self.requests, self.cut_at, self.cut_requests = objects, [], 0, 0
        # What the object becomes after the first request, the number of leading requests that fail with 503,
        # whether the server ignores Range, and whether it leaves out Content-Length.
        self.after_first, self.fail_requests, self.ignore_range, self.no_length = None, 0, False, False
        bucket = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                data, etag = bucket.objects.get(self.path, (None, None))
                bucket.requests.append((self.command, self.path, dict(self.headers)))
                if data is None:
                    self.send_error(404)
                    return
                if len(bucket.requests) <= bucket.fail_requests:
                    self.send_error(503)
                    return
                if "If-Match" in self.headers and self.headers["If-Match"].strip('"') != etag:
                    self.send_error(412)
                    return
                if len(bucket.requests) == 1 and bucket.after_first:
                    bucket.objects[self.path] = bucket.after_first
                start = 0
                if "Range" in self.headers and not bucket.ignore_range:
                    start = int(self.headers["Range"].removeprefix("bytes=").split("-")[0])
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                else:
                    self.send_response(200)
                self.send_header("ETag", f'"{etag}"')
                if not bucket.no_length:
                    self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                body = data[start:]
                if len(bucket.requests) <= bucket.cut_requests:
                    body = body[:bucket.cut_at]
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The client stopped reading, as a test of an early stop makes it do.

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def bucket():
    made = []

    def make(objects):
        made.append(Bucket(objects))
        return made[-1]

    yield make
    for each in made:
        each.close()


def _object(bucket, data: bytes, etag: str, path="/o/file.bin"):
    server = bucket({path: (data, etag)})
    return server, {"name": "file.bin", "url": f"{server.base}{path}", "size": len(data), "etag": etag}


DATA = bytes(range(256)) * 4096  # 1 MiB


def test_an_object_is_one_streaming_get_and_no_head(tmp_path, bucket):
    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    fetch.download(obj, tmp_path, sleep=lambda s: None)
    assert (tmp_path / "file.bin").read_bytes() == DATA
    assert [method for method, _, _ in server.requests] == ["GET"]
    assert not list(tmp_path.glob("*.part"))


def test_a_stream_that_breaks_continues_from_the_byte_reached(tmp_path, bucket):
    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    server.cut_at, server.cut_requests = 300_000, 1
    lines = []
    fetch.download(obj, tmp_path, sleep=lambda s: None, out=lines.append)
    assert (tmp_path / "file.bin").read_bytes() == DATA
    (_, _, first), (_, _, second) = server.requests
    assert "Range" not in first
    assert second["Range"] == "bytes=300000-" and second["If-Match"] == f'"{obj["etag"]}"'
    assert any("retry 1 of 5 from byte 300,000" in line for line in lines)


def test_six_breaks_in_a_row_end_the_download_after_five_retries(tmp_path, bucket):
    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    server.cut_at, server.cut_requests = 1000, 99
    with pytest.raises(fetch.FetchError, match="5 retries"):
        fetch.download(obj, tmp_path, sleep=lambda s: None)
    assert len(server.requests) == 6


def test_an_etag_or_size_that_differs_from_the_record_stops_before_any_byte_is_kept(tmp_path, bucket):
    server, obj = _object(bucket, DATA, "0" * 32)
    with pytest.raises(fetch.FetchError, match="ETag"):
        fetch.download(obj | {"etag": "1" * 32}, tmp_path, sleep=lambda s: None)
    with pytest.raises(fetch.FetchError, match="size"):
        fetch.download(obj | {"size": len(DATA) + 1, "etag": "0" * 32}, tmp_path, sleep=lambda s: None)
    assert list(tmp_path.iterdir()) == []


def test_a_missing_object_is_not_retried(tmp_path, bucket):
    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    with pytest.raises(fetch.FetchError, match="404"):
        fetch.download(obj | {"url": f"{server.base}/absent"}, tmp_path, sleep=lambda s: None)
    assert len(server.requests) == 1


def test_a_single_part_etag_is_the_md5_of_the_file(tmp_path):
    (tmp_path / "file.bin").write_bytes(DATA)
    obj = {"name": "file.bin", "size": len(DATA), "etag": hashlib.md5(DATA).hexdigest()}
    assert fetch.check(obj, tmp_path) == []
    assert fetch.check(obj | {"etag": "0" * 32}, tmp_path) == [
        f"file.bin: MD5 {obj['etag']}, the recorded ETag is {'0' * 32}"]


def test_a_multipart_etag_is_recomputed_when_one_part_size_fits(tmp_path):
    data = DATA * 2 + DATA[: MIB // 2]  # 2.5 MiB: three parts only if a part is 1 MiB
    (tmp_path / "file.bin").write_bytes(data)
    obj = {"name": "file.bin", "size": len(data), "etag": multipart_etag(data, MIB)}
    lines = []
    assert fetch.check(obj, tmp_path, out=lines.append) == []
    assert any("recomputed" in line and "1 MiB" in line for line in lines)
    wrong = obj | {"etag": multipart_etag(data[:-1] + b"x", MIB)}
    errors = fetch.check(wrong, tmp_path)
    assert errors[0].startswith("file.bin: multipart ETag recomputed") and "the part size is inferred" in errors[0]


def test_a_multipart_etag_with_no_unique_part_size_is_compared_by_size_only_and_says_so(tmp_path):
    data = DATA * 5  # 5 MiB in 2 parts: a part of 3 MiB and one of 4 MiB both fit
    (tmp_path / "file.bin").write_bytes(data)
    obj = {"name": "file.bin", "size": len(data), "etag": f"{'a' * 32}-2"}
    lines = []
    assert fetch.check(obj, tmp_path, out=lines.append) == []
    assert any("not recomputed" in line for line in lines)
    assert fetch.check(obj | {"size": len(data) + 1}, tmp_path) == [
        f"file.bin: size {len(data)}, the record has {len(data) + 1}"]


def test_the_kidney_recorded_etags_have_one_part_size_each():
    for obj in fetch.wanted(SAMPLES["kidney"]):
        if "-" in obj["etag"]:
            assert fetch.part_size(obj["size"], int(obj["etag"].split("-")[1])) == 8 * MIB, obj["name"]


def test_the_wanted_objects_are_the_three_that_the_ingest_reads_with_their_recorded_facts():
    wanted = {o["name"]: o for o in fetch.wanted(SAMPLES["kidney"])}
    name = SAMPLES["kidney"].name
    assert sorted(wanted) == sorted([f"{name}_outs.zip", f"{name}_he_image.ome.tif",
                                     f"{name}_he_imagealignment.csv"])
    bundle = wanted[f"{name}_outs.zip"]
    assert (bundle["size"], bundle["etag"]) == (45_720_445_144, "8c1c07343dbe1b946f5f90a21130782f-5451")
    assert bundle["url"].startswith("https://s3-us-west-2.amazonaws.com/10x.files/samples/atera/")


def test_the_command_line_downloads_then_checks_and_a_bad_file_exits_non_zero(tmp_path, bucket, monkeypatch, capsys):
    obj = {"name": "n.bin", "size": len(DATA), "etag": hashlib.md5(DATA).hexdigest()}
    server = bucket({"/n.bin": (DATA, obj["etag"])})
    monkeypatch.setattr(fetch, "wanted", lambda sample, base=None: [obj | {"url": f"{server.base}/n.bin"}])
    assert fetch.main(["download", "--sample", "kidney", "--directory", str(tmp_path)]) == 0
    assert fetch.main(["check", "--sample", "kidney", "--directory", str(tmp_path)]) == 0
    (tmp_path / "n.bin").write_bytes(DATA[:-1])
    assert fetch.main(["check", "--sample", "kidney", "--directory", str(tmp_path)]) == 1
    assert "size" in capsys.readouterr().out


def _fetched(tmp_path, obj, **changes):
    record = {"object": obj["name"], "etag": obj["etag"], "size": obj["size"], "date": "2026-11-11T00:00:00+00:00"}
    (tmp_path / f"{obj['name']}.fetched").write_text(__import__("json").dumps(record | changes))


def _multipart_file(tmp_path):
    data = DATA * 2 + DATA[: MIB // 2]
    (tmp_path / "file.bin").write_bytes(data)
    return {"name": "file.bin", "size": len(data), "etag": multipart_etag(data[:-1] + b"x", MIB)}


def test_the_download_writes_a_record_of_what_the_response_said(tmp_path, bucket):
    import json

    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    fetch.download(obj, tmp_path, sleep=lambda s: None)
    record = json.loads((tmp_path / "file.bin.fetched").read_text())
    assert (record["object"], record["etag"], record["size"]) == ("file.bin", obj["etag"], len(DATA))
    assert record["date"]


def test_a_multipart_mismatch_of_a_downloaded_file_is_a_loud_warning_and_the_step_passes(tmp_path):
    obj = _multipart_file(tmp_path)
    _fetched(tmp_path, obj)
    lines = []
    assert fetch.check(obj, tmp_path, out=lines.append) == []
    warning = next(line for line in lines if line.startswith("WARNING"))
    assert "inferred" in warning and "1 MiB" in warning and obj["etag"] in warning and "-3" in warning


def test_a_multipart_mismatch_of_a_file_with_no_record_stops_and_says_the_part_size_is_inferred(tmp_path):
    obj = _multipart_file(tmp_path)
    (error,) = fetch.check(obj, tmp_path)
    assert "inferred" in error and obj["etag"] in error


def test_a_record_of_another_etag_does_not_excuse_a_mismatch(tmp_path):
    obj = _multipart_file(tmp_path)
    _fetched(tmp_path, obj, etag="0" * 32)
    assert len(fetch.check(obj, tmp_path)) == 1


def test_a_size_mismatch_and_a_single_part_md5_mismatch_stop_even_with_a_record(tmp_path):
    (tmp_path / "file.bin").write_bytes(DATA)
    obj = {"name": "file.bin", "size": len(DATA), "etag": "0" * 32}
    _fetched(tmp_path, obj)
    assert len(fetch.check(obj, tmp_path)) == 1
    assert len(fetch.check(obj | {"size": len(DATA) + 1}, tmp_path)) == 1


def test_a_local_write_error_stops_at_once_with_the_file_name_and_is_not_retried(tmp_path, bucket, monkeypatch):
    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    real_open = type(tmp_path).open

    def full_disk(self, mode="r", *args, **kwargs):
        if self.name.endswith(".part"):
            raise OSError(28, "No space left on device")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), "open", full_disk)
    lines = []
    with pytest.raises(fetch.FetchError, match=r"file\.bin.*No space left") as raised:
        fetch.download(obj, tmp_path, sleep=lambda s: None, out=lines.append)
    assert "retry" not in " ".join(lines) and len(server.requests) == 1
    assert str(tmp_path) not in str(raised.value)


def test_an_object_that_changed_during_the_download_is_a_412_with_that_message(tmp_path, bucket):
    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    server.cut_at, server.cut_requests = 300_000, 1
    server.after_first = (DATA[::-1], "f" * 32)
    with pytest.raises(fetch.FetchError, match="changed during the download"):
        fetch.download(obj, tmp_path, sleep=lambda s: None)
    assert len(server.requests) == 2


def test_a_200_on_a_resume_stops_and_never_appends(tmp_path, bucket):
    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    server.cut_at, server.cut_requests = 300_000, 1
    server.ignore_range = True
    with pytest.raises(fetch.FetchError, match="ignored the Range"):
        fetch.download(obj, tmp_path, sleep=lambda s: None)
    assert (tmp_path / "file.bin.part").stat().st_size == 300_000


def test_a_5xx_is_retried_and_the_download_then_succeeds(tmp_path, bucket):
    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    server.fail_requests = 2
    lines = []
    fetch.download(obj, tmp_path, sleep=lambda s: None, out=lines.append)
    assert (tmp_path / "file.bin").read_bytes() == DATA and len(server.requests) == 3
    assert any("HTTP 503; retry 2 of 5 from byte 0" in line for line in lines)


def test_a_missing_content_length_is_a_clear_error(tmp_path, bucket):
    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    server.no_length = True
    with pytest.raises(fetch.FetchError, match="Content-Length"):
        fetch.download(obj, tmp_path, sleep=lambda s: None)


def test_a_part_file_of_an_earlier_run_is_continued(tmp_path, bucket):
    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    (tmp_path / "file.bin.part").write_bytes(DATA[:300_000])
    fetch.download(obj, tmp_path, sleep=lambda s: None)
    assert (tmp_path / "file.bin").read_bytes() == DATA
    assert server.requests[0][2]["Range"] == "bytes=300000-"


def test_a_part_file_longer_than_the_object_is_started_again(tmp_path, bucket):
    server, obj = _object(bucket, DATA, hashlib.md5(DATA).hexdigest())
    (tmp_path / "file.bin.part").write_bytes(DATA + b"x")
    fetch.download(obj, tmp_path, sleep=lambda s: None)
    assert (tmp_path / "file.bin").read_bytes() == DATA and "Range" not in server.requests[0][2]


def test_a_whole_part_file_is_renamed_with_no_request_and_no_record(tmp_path, bucket):
    data = DATA * 2 + DATA[: MIB // 2]
    server, obj = _object(bucket, data, multipart_etag(data[:-1] + b"x", MIB))
    (tmp_path / "file.bin.part").write_bytes(data)
    lines = []
    fetch.download(obj, tmp_path, sleep=lambda s: None, out=lines.append)
    assert (tmp_path / "file.bin").read_bytes() == data and not (tmp_path / "file.bin.part").exists()
    assert server.requests == [] and not (tmp_path / "file.bin.fetched").exists()
    assert any("no response seen" in line for line in lines)
    # With no record, a multipart mismatch stops.
    assert len(fetch.check(obj | {"name": "file.bin"}, tmp_path)) == 1
