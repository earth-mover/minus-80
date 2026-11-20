"""Survey the MICrONS minnie archive: every object under the BossDB prefix, every
precomputed layer's ``info``, and DANDI 000402's assets.

    uv run --package microns-ingest python ingests/microns/survey.py <output.json>

Run it beside the bucket (us-east-1). The unsharded segmentation scales hold
millions of objects each, and the whole prefix holds tens of millions. The job
prints its progress and memory every 30 seconds.

The output is the listing ``diagrams.py`` and ``PLAN.md`` take their numbers
from. For each directory that holds files it records the object count, total
bytes, the count per file extension and a few example keys; directories with
few objects keep their full listing.

Listing goes through obstore, whose Rust client parses S3's XML responses
outside Python. boto3 parses each response in Python under the GIL, which held
an earlier version of this survey to about 9,200 objects a second however many
threads it ran. Only running totals per directory are kept, never the keys, so
memory stays flat however many objects a prefix holds.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import obstore
from obstore.store import S3Store

BUCKET = "bossdb-open-data"
ROOT = "iarpa_microns/minnie/"
DANDI = "https://api.dandiarchive.org/api/dandisets/000402/versions/draft/assets/?page_size=200"
FULL_LISTING_BELOW = 200
# Precomputed keys start with a digit (unsharded chunks, shard numbers), a hex digit, or a
# name such as "info"; each scale directory is listed as one stream per possible first
# character, so the largest scales list in parallel. "." is left out because obstore
# rejects a prefix ending in a "." segment; a key starting with "." would go uncounted.
KEY_START = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"
ONLY = [t for t in os.environ.get("SURVEY_ONLY", "").split(",") if t]

store = S3Store(BUCKET, region="us-east-1", skip_signature=True)
started = time.time()


@dataclass
class Directory:
    objects: int = 0
    bytes: int = 0
    extensions: Counter = field(default_factory=Counter)
    examples: list = field(default_factory=list)
    files: dict = field(default_factory=dict)


directories: dict[str, Directory] = {}
lock = threading.Lock()
listed = 0
units_done = 0


def subdirectories(prefix: str) -> tuple[list[str], list[tuple[str, int]]]:
    result = obstore.list_with_delimiter(store, prefix=prefix)
    dirs = [p if p.endswith("/") else p + "/" for p in result["common_prefixes"]]
    return dirs, [(o["path"], o["size"]) for o in result["objects"]]


def record(keys: list[str], sizes: list[int]) -> None:
    global listed
    local: dict[str, Directory] = {}
    for key, size in zip(keys, sizes):
        cut = key.rindex("/") + 1
        name = key[cut:]
        entry = local.get(key[:cut])
        if entry is None:
            entry = local[key[:cut]] = Directory()
        entry.objects += 1
        entry.bytes += size
        entry.extensions[name.rsplit(".", 1)[1] if "." in name else ""] += 1
        if len(entry.examples) < 5:
            entry.examples.append(key)
        if len(entry.files) < FULL_LISTING_BELOW:
            entry.files[name] = size
    with lock:
        listed += len(keys)
        for path, part in local.items():
            entry = directories.setdefault(path, Directory())
            entry.objects += part.objects
            entry.bytes += part.bytes
            entry.extensions.update(part.extensions)
            entry.examples = (entry.examples + part.examples)[:5]
            if len(entry.files) < FULL_LISTING_BELOW:
                entry.files.update(part.files)


def list_unit(unit: str) -> None:
    global units_done
    if unit.endswith("\0files"):
        _, files = subdirectories(unit[:-6])
        record([k for k, _ in files], [s for _, s in files])
    else:
        for batch in obstore.list(store, prefix=unit, return_arrow=True, chunk_size=50_000):
            record(batch.column("path").to_pylist(), batch.column("size").to_pylist())
    with lock:
        units_done += 1


def resident_gb() -> float:
    try:
        with open("/proc/self/status") as f:
            return int(next(line.split()[1] for line in f if line.startswith("VmRSS"))) / 1e6
    except OSError:
        return float("nan")


def heartbeat(stop: threading.Event, total_units: int) -> None:
    while not stop.wait(30):
        elapsed = time.time() - started
        print(
            f"[{elapsed:7.0f}s] {listed:,} objects, {listed / elapsed:,.0f}/s, "
            f"{units_done}/{total_units} units, {resident_gb():.1f} GB resident",
            flush=True,
        )


def units(prefix: str, depth: int) -> list[str]:
    """Prefixes to list, one stream each: the functional tree is split three levels down."""
    dirs, _ = subdirectories(prefix)
    if depth == 0 or not dirs:
        return [prefix]
    out = [prefix + "\0files"]
    for d in dirs:
        out += units(d, depth - 1)
    return out


def main(output: str) -> None:
    work = []
    top, _ = subdirectories(ROOT)
    for t in top:
        name = t[len(ROOT) : -1]
        if ONLY and name not in ONLY:
            continue
        if name in ("minnie65", "minnie35"):
            for layer in subdirectories(t)[0]:
                for scale in subdirectories(layer)[0]:
                    work += [scale + c for c in KEY_START]
                work.append(layer + "\0files")
        else:
            work += units(t, 3)
    print(f"{len(work)} listing units", flush=True)

    stop = threading.Event()
    threading.Thread(target=heartbeat, args=(stop, len(work)), daemon=True).start()
    with ThreadPoolExecutor(64) as pool:
        list(pool.map(list_unit, work))
    stop.set()

    result_dirs = {}
    for path, entry in sorted(directories.items()):
        item = {
            "objects": entry.objects,
            "bytes": entry.bytes,
            "extensions": dict(entry.extensions),
            "examples": entry.examples,
        }
        if entry.objects < FULL_LISTING_BELOW:
            item["files"] = entry.files
        result_dirs[path[len(ROOT) :]] = item

    infos = {}
    for path, item in result_dirs.items():
        parts = path.rstrip("/").split("/")
        is_layer = len(parts) == 2 and parts[0] in ("minnie65", "minnie35", "microCT")
        if is_layer and "info" in item.get("files", {}):
            infos[path.rstrip("/")] = json.loads(bytes(obstore.get(store, ROOT + path + "info").bytes()))

    assets, url = [], DANDI
    while url:
        page = json.load(urllib.request.urlopen(url))
        assets += [{"path": a["path"], "size": a["size"], "asset_id": a["asset_id"]} for a in page["results"]]
        url = page["next"]

    with open(output, "w") as f:
        json.dump(
            {
                "surveyed": time.strftime("%Y-%m-%d"),
                "bucket": BUCKET,
                "root": ROOT,
                "directories": result_dirs,
                "precomputed_info": infos,
                "dandi_000402": assets,
            },
            f,
            indent=1,
        )
    total = sum(e["objects"] for e in result_dirs.values())
    size = sum(e["bytes"] for e in result_dirs.values())
    print(
        f"done: {total:,} objects, {size / 1e12:.1f} TB, {len(result_dirs)} directories, "
        f"{time.time() - started:.0f} s",
        flush=True,
    )


if __name__ == "__main__":
    main(sys.argv[1])
