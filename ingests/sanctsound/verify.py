"""Byte-identity check: virtual chunks against libsndfile's own decode.

Spot-checks recordings spread across a deployment (first, a third, two thirds,
last unless ``--segments`` names others), each at its head, its middle and its
short final frame, which is where a grid error would show.

libsndfile reads each recording through ranged HTTPS reads, so a check downloads
a few megabytes per sample rather than the 770 MB file. Then the store's
resolution of detection times is run with ``selftest_from_store_only``.

    uv run --package sanctsound-ingest python ingests/sanctsound/verify.py \\
        --repo <icechunk dir | arraylake://org/repo> --deployment CI01_01
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import soundfile as sf
import zarr
from minus80.core import remote

import ingest
from storage import BASE, GCS, repository

N = 200_000


def check(waveform, seg_start: int, seg_n: int, url: str) -> bool:
    """Compare three windows of one recording with libsndfile's decode of the file."""
    https = GCS + url.removeprefix(BASE)
    ok = True
    with sf.SoundFile(remote.reader(https)) as f:
        for name, off in [("head", 0), ("mid", seg_n // 2), ("tail", max(0, seg_n - N))]:
            n = min(N, seg_n - off)
            f.seek(off)
            want = f.read(n, dtype="int16")
            got = waveform[seg_start + off: seg_start + off + n]
            same = got.shape == want.shape and np.array_equal(got, want)
            ok &= same
            print(f"    {name:5s} off={off:>12,} n={n:>7,} "
                  f"{'identical' if same else 'MISMATCH'}", flush=True)
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--deployment", default="CI01_01")
    ap.add_argument("--segments", type=int, nargs="*",
                    help="segment indices; default is first, a third, two thirds, last")
    args = ap.parse_args(argv)

    repo = repository(args.repo)
    root = zarr.open_group(repo.readonly_session(args.branch).store, mode="r")
    dg = root[args.deployment]
    with zarr.config.set({"array.rectilinear_chunks": True}):
        w = dg["audio/waveform"]
        s = dg["audio/segments"]
        starts, ns, uris = s["start_sample"][:], s["n_samples"][:], s["source_uri"][:]
        last = len(starts) - 1
        picks = args.segments or sorted({0, last // 3, 2 * last // 3, last})
        ok = True
        for k in picks:
            print(f"  segment {k}: {str(uris[k]).split('/')[-1]}", flush=True)
            ok &= check(w, int(starts[k]), int(ns[k]), str(uris[k]))
        print("\nselftest_from_store_only:")
        for row in ingest.selftest_from_store_only(root):
            print(f"  {row}")
    print("\nALL IDENTICAL" if ok else "\nFAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
