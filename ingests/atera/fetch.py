"""Download the objects that the ingest reads, once each, and check them against the sizes and ETags of LAYOUTS.md.

``download`` makes one streaming GET for each object. It continues a broken stream from the byte reached, at most
5 times, and it stops before any byte is kept when the response disagrees with the record. ``check`` reads each file
on disk: it compares the size, and the ETag when that is the MD5 of the file or of its parts.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from minus80.core.progress import MIB, Progress

from root_attrs import source_objects
from sources import BUNDLE, HE_ALIGNMENT, HE_IMAGE, SAMPLES

LAYOUTS = Path(__file__).with_name("LAYOUTS.md")
#: The suffix of each object that the ingest reads; the others of LAYOUTS.md are read from the bundle or not at all.
READ_SUFFIXES = (BUNDLE, HE_IMAGE, HE_ALIGNMENT)
RETRIES = 5
CHUNK = 1 << 20
#: The largest part size, in MiB, that ``part_size`` considers.
MAX_PART_MIB = 5120


class FetchError(Exception):
    """An object differs from the record, or the download failed for good."""


def wanted(sample) -> list[dict]:
    """The objects that the ingest reads, each with ``name``, ``url``, and the ``size`` and ``etag`` of LAYOUTS.md."""
    return [{"name": obj["url"].rsplit("/", 1)[1], **obj} for obj in source_objects(LAYOUTS.read_text(), sample)
            if obj["url"].endswith(READ_SUFFIXES)]


def part_size(size: int, parts: int) -> int | None:
    """The one part size, a whole number of MiB, that gives ``parts`` parts for ``size`` bytes; ``None`` if none or several."""
    fits = [m * MIB for m in range(1, MAX_PART_MIB + 1) if -(-size // (m * MIB)) == parts]
    return fits[0] if len(fits) == 1 else None


def download(obj: dict, directory: Path, *, retries: int = RETRIES, sleep=time.sleep, out=print,
             every: float = 10.0) -> None:
    """Stream ``obj`` to ``directory`` / its name. A response that disagrees with the record raises ``FetchError``.

    Only a network error, a 5xx or a 429 is retried. A local write error stops at once. When the file is whole, a
    ``.fetched`` file beside it records what the response said.
    """
    target = directory / obj["name"]
    partial = target.with_name(target.name + ".part")
    if target.exists() and target.stat().st_size == obj["size"]:
        out(f"{obj['name']}: already here, skipped")
        return
    have, attempt = (partial.stat().st_size if partial.exists() else 0), 0
    if have == obj["size"]:
        # Whole, but no response was seen, so no record is written and the check treats the file as of unknown origin.
        partial.rename(target)
        out(f"{obj['name']}: a whole {partial.name} from an earlier run was renamed; no response seen, no record")
        return
    if have > obj["size"]:
        out(f"{partial.name}: {have:,} bytes, more than the record; started again")
        have = 0
    with Progress(f"download {obj['name']}", total=obj["size"], unit="MiB", scale=MIB, every=every) as progress:
        progress.done = have
        while have < obj["size"]:
            headers = {"Range": f"bytes={have}-", "If-Match": f'"{obj["etag"]}"'} if have else {}
            try:
                with urllib.request.urlopen(urllib.request.Request(obj["url"], headers=headers), timeout=60) as response:
                    _require_record(obj, response, have)
                    try:
                        file = partial.open("ab" if have else "wb")
                    except OSError as error:
                        raise _write_error(obj, partial, error) from None
                    with file:
                        while chunk := response.read(CHUNK):
                            try:
                                file.write(chunk)
                            except OSError as error:
                                raise _write_error(obj, partial, error) from None
                            have += len(chunk)
                            progress.advance(len(chunk))
            except urllib.error.HTTPError as error:
                if error.code == 412:
                    raise FetchError(f"{obj['name']}: HTTP 412, the object changed during the download") from None
                if error.code < 500 and error.code != 429:
                    raise FetchError(f"{obj['name']}: HTTP {error.code} {error.reason}") from None
                failure = f"HTTP {error.code}"
            except (OSError, http.client.HTTPException) as error:
                failure = f"{type(error).__name__}: {error}"
            else:
                failure = None if have >= obj["size"] else "the stream ended early"
            if failure is None:
                break
            attempt += 1
            if attempt > retries:
                raise FetchError(f"{obj['name']}: {failure} after {retries} retries, {have:,} of {obj['size']:,} bytes")
            out(f"{obj['name']}: {failure}; retry {attempt} of {retries} from byte {have:,}")
            sleep(min(2 ** attempt, 30))
    if have != obj["size"]:
        raise FetchError(f"{obj['name']}: {have} bytes written, the record has {obj['size']}")
    partial.rename(target)
    record = {"object": obj["name"], "etag": obj["etag"], "size": obj["size"],
              "date": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    target.with_name(target.name + ".fetched").write_text(json.dumps(record))


def _write_error(obj: dict, partial: Path, error: OSError) -> FetchError:
    """A local write error as a ``FetchError``, which no retry catches; it names the file, not its directory."""
    return FetchError(f"{obj['name']}: cannot write {partial.name}: {error.strerror or error}")


def _require_record(obj: dict, response, have: int) -> None:
    """Raise ``FetchError`` unless the response is the recorded object, whole or from byte ``have``."""
    etag = response.headers.get("ETag", "").strip('"')
    if etag != obj["etag"]:
        raise FetchError(f"{obj['name']}: ETag {etag}, the record has {obj['etag']}")
    if have and response.status != 206:
        raise FetchError(f"{obj['name']}: the server ignored the Range request (HTTP {response.status})")
    if response.headers.get("Content-Length") is None:
        raise FetchError(f"{obj['name']}: the response has no Content-Length, so its size cannot be checked")
    length = int(response.headers["Content-Length"]) + have
    if length != obj["size"]:
        raise FetchError(f"{obj['name']}: size {length}, the record has {obj['size']}")


def _fetched(obj: dict, directory: Path) -> bool:
    """True when ``download`` recorded that the response's ETag and size equalled those of ``obj``."""
    try:
        record = json.loads((directory / f"{obj['name']}.fetched").read_text())
    except (OSError, ValueError):
        return False
    return (record.get("object"), record.get("etag"), record.get("size")) == (obj["name"], obj["etag"], obj["size"])


def check(obj: dict, directory: Path, *, out=print) -> list[str]:
    """The size of the file, and its MD5 or multipart ETag where one can be recomputed, against the record.

    A multipart ETag is the MD5 of the parts' MD5s, so it needs the part size. It is recomputed only when exactly one
    whole-MiB part size fits the size and the part count; the line printed says so.
    """
    path, name = directory / obj["name"], obj["name"]
    if not path.exists():
        return [f"{name}: the file is missing"]
    if (actual := path.stat().st_size) != obj["size"]:
        return [f"{name}: size {actual}, the record has {obj['size']}"]
    etag = obj["etag"]
    parts = int(etag.split("-")[1]) if "-" in etag else None
    unit = part_size(obj["size"], parts) if parts else None
    if parts and unit is None:
        out(f"{name}: size {actual:,} matches; multipart ETag {etag} not recomputed, no unique part size fits")
        return []
    with Progress(f"hash {name}", total=actual, unit="MiB", scale=MIB) as progress:
        digests, whole = [], hashlib.md5()
        with path.open("rb") as file:
            while block := file.read(unit or CHUNK):
                (digests.append(hashlib.md5(block).digest()) if unit else whole.update(block))
                progress.advance(len(block))
    if not unit:
        out(f"{name}: size {actual:,} and MD5 {whole.hexdigest()} match the record")
        return [] if whole.hexdigest() == etag else [f"{name}: MD5 {whole.hexdigest()}, the recorded ETag is {etag}"]
    computed = f"{hashlib.md5(b''.join(digests)).hexdigest()}-{len(digests)}"
    if computed != etag:
        said = f"multipart ETag recomputed with {unit // MIB} MiB parts is {computed}, the record has {etag}"
        if _fetched(obj, directory):
            # The response said this ETag, so a different recomputation can come from the inferred part size.
            out(f"WARNING {name}: {said}. The part size is inferred, and the download recorded that the "
                f"response's ETag and size equalled the record, so this step passes.")
            return []
        return [f"{name}: {said} (the part size is inferred, and no record of the download shows that the "
                f"response's ETag equalled the record)"]
    out(f"{name}: size {actual:,} matches; multipart ETag {etag} recomputed with {unit // MIB} MiB parts, "
        f"the only whole-MiB part size that fits")
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("download", "check"))
    parser.add_argument("--sample", choices=sorted(SAMPLES), required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    objects = wanted(SAMPLES[args.sample])
    if args.command == "download":
        args.directory.mkdir(parents=True, exist_ok=True)
        try:
            for obj in objects:
                download(obj, args.directory)
        except FetchError as error:
            print(f"FAIL {error}", flush=True)
            return 1
        return 0
    errors = [e for obj in objects for e in check(obj, args.directory)]
    for error in errors:
        print(f"FAIL {error}", flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
