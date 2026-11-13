"""Survey the MICrONS minnie archive: every object under the BossDB prefix, every
precomputed layer's ``info``, and DANDI 000402's assets.

    uv run --no-project --with boto3 python survey.py <output.json>

Run it beside the bucket (us-east-1): the unsharded segmentation scales hold
millions of objects each, and listing them from a laptop takes hours. It prints
a progress line every 30 seconds.

The output is the listing ``diagrams.py`` and ``PLAN.md`` take their numbers
from. For each directory that holds files it records the object count, total
bytes, the count per file extension and a few example keys; directories with
few objects keep their full listing.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore import UNSIGNED
from botocore.config import Config

BUCKET = "bossdb-open-data"
ROOT = "iarpa_microns/minnie/"
DANDI = "https://api.dandiarchive.org/api/dandisets/000402/versions/draft/assets/?page_size=200"
FULL_LISTING_BELOW = 200
# Precomputed keys start with a digit (unsharded chunks, shard numbers), a hex digit, or a
# name such as "info"; a scale is listed as one unit per possible first character.
KEY_START = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_."
ONLY = [t for t in os.environ.get("SURVEY_ONLY", "").split(",") if t]

s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED, max_pool_connections=128))
lock = threading.Lock()
listed = 0
started = time.time()


def subdirectories(prefix: str) -> tuple[list[str], list[dict]]:
    dirs, files = [], []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix, Delimiter="/"):
        dirs += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
        files += page.get("Contents", [])
    return dirs, files


def list_all(prefix: str) -> list[tuple[str, int]]:
    global listed
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        batch = [(o["Key"], o["Size"]) for o in page.get("Contents", [])]
        out += batch
        with lock:
            listed += len(batch)
    return out


def heartbeat(stop: threading.Event) -> None:
    while not stop.wait(30):
        rate = listed / (time.time() - started)
        print(f"[{time.time() - started:7.0f}s] {listed:,} objects listed, {rate:,.0f}/s", flush=True)


def units(prefix: str, depth: int) -> list[str]:
    """Directories to list as one unit each: a precomputed scale, or a leaf of the functional tree.

    A scale directory is split again by the first character of its keys, so the
    largest unsharded scales list in parallel.
    """
    dirs, _ = subdirectories(prefix)
    if depth == 0 or not dirs:
        return [prefix]
    out = [prefix + "\0files"]
    for d in dirs:
        out += units(d, depth - 1)
    return out


def main(output: str) -> None:
    stop = threading.Event()
    threading.Thread(target=heartbeat, args=(stop,), daemon=True).start()

    work = []
    top, _ = subdirectories(ROOT)
    for t in top:
        name = t[len(ROOT):-1]
        if ONLY and name not in ONLY:
            continue
        if name in ("minnie65", "minnie35"):
            for layer in subdirectories(t)[0]:
                for scale in subdirectories(layer)[0]:
                    work += [scale + c for c in KEY_START]
                work.append(layer + "\0files")
        else:
            work += [u for u in units(t, 3)]
    print(f"{len(work)} listing units", flush=True)

    def run(unit: str) -> tuple[str, list[tuple[str, int]]]:
        if unit.endswith("\0files"):
            return unit[:-6], [(f["Key"], f["Size"]) for f in subdirectories(unit[:-6])[1]]
        if "\0" not in unit and unit.endswith("/"):
            return unit, list_all(unit)
        return unit[: unit.rindex("/") + 1], list_all(unit)

    by_dir: dict[str, list[tuple[str, int]]] = defaultdict(list)
    with ThreadPoolExecutor(96) as pool:
        for directory, keys in pool.map(run, work):
            for key, size in keys:
                by_dir[key[: key.rindex("/") + 1]].append((key, size))
    stop.set()

    directories = {}
    for directory, keys in sorted(by_dir.items()):
        entry = {
            "objects": len(keys),
            "bytes": sum(s for _, s in keys),
            "extensions": dict(Counter(k.rsplit("/", 1)[1].rsplit(".", 1)[1] if "." in k.rsplit("/", 1)[1] else "" for k, _ in keys)),
            "examples": [k for k, _ in keys[:5]],
        }
        if len(keys) < FULL_LISTING_BELOW:
            entry["files"] = {k[len(directory):]: s for k, s in keys}
        directories[directory[len(ROOT):]] = entry

    infos = {}
    for directory in directories:
        parts = directory.rstrip("/").split("/")
        precomputed = (len(parts) == 2 and parts[0] in ("minnie65", "minnie35")) or (len(parts) == 2 and parts[0] == "microCT")
        if precomputed and "info" in directories[directory].get("files", {}):
            body = s3.get_object(Bucket=BUCKET, Key=ROOT + directory + "info")["Body"].read()
            infos[directory.rstrip("/")] = json.loads(body)

    assets, url = [], DANDI
    while url:
        page = json.load(urllib.request.urlopen(url))
        assets += [{"path": a["path"], "size": a["size"], "asset_id": a["asset_id"]} for a in page["results"]]
        url = page["next"]

    result = {
        "surveyed": time.strftime("%Y-%m-%d"),
        "bucket": BUCKET,
        "root": ROOT,
        "directories": directories,
        "precomputed_info": infos,
        "dandi_000402": assets,
    }
    with open(output, "w") as f:
        json.dump(result, f, indent=1)
    total = sum(e["objects"] for e in directories.values())
    size = sum(e["bytes"] for e in directories.values())
    print(f"done: {total:,} objects, {size / 1e12:.1f} TB, {len(directories)} directories, "
          f"{time.time() - started:.0f} s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
