"""Commit a GTEx-shaped store at full scale, locally, with made-up offsets.

The slides, donors, instances and level shapes are GTEx's own, from
idc-index (25,503 slides, 128,708 instances), so the node and reference
counts are the real ones; each tile's offset and length are made up and
nothing is read from the bucket. Commits 500 slides at a time, each batch
merged from one fork per slide, as ingest.py does, and prints one JSON line
per batch: the time to write, merge and commit, the snapshot's size and the
manifests', and the commit's CPU time. Takes about 80 minutes and 7.5 GB of
disk. Keep the machine awake (macOS: ``caffeinate -i``): a laptop that
sleeps mid-commit adds the sleep to that commit's wall time.

    uv run --package idc-ingest python ingests/idc/commit_bench.py REPO [SLIDES]
"""
import json
import math
import os
import sys
import time

os.environ["MINUS80_S3_ANONYMOUS"] = "1"
import numpy as np
import zarr
from minus80.core.manifest import manifest_array, channel_last_transpose
from minus80.core.icechunk_io import create_repo, commit, write_pinned
from minus80.ome import image_attrs, rgb_omero
from virtualizarr.manifests import ManifestGroup

REPO = sys.argv[1]; LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else None
def gtex_instances():
    import duckdb
    from idc_index import IDCClient

    client = IDCClient()
    client.fetch_index("sm_instance_index")
    con = duckdb.connect()
    con.register("i", client.index)
    con.register("s", client.sm_instance_index)
    return con.query("""select i.crdc_series_uuid, i.PatientID, s.SeriesInstanceUID, s.crdc_instance_uuid,
        s.ImageType[3] kind, s.TotalPixelMatrixRows r, s.TotalPixelMatrixColumns c
        from s join i using (SeriesInstanceUID) where i.collection_id = 'gtex'""").to_df()


df = gtex_instances()
slides = list(df.groupby("SeriesInstanceUID"))
if LIMIT: slides = slides[:LIMIT]
repo = create_repo(REPO, [], prefixes=["s3://idc-open-data/"])
session = repo.writable_session("main"); root = zarr.open_group(session.store, mode="a")
root.require_group("gtex")
for p in sorted({g.PatientID.iloc[0] for _, g in slides}): root.require_group(f"gtex/{p}")
commit(session, "groups")
T = 240
JPEG = [channel_last_transpose(3), {"name": "imagecodecs_jpeg"}]
RAW = [channel_last_transpose(3), {"name": "bytes", "configuration": {"endian": "little"}}]

def level(url, r, c, codecs, tile):
    grid = (1, math.ceil(r / tile[0]), math.ceil(c / tile[1]))
    n = grid[1] * grid[2]
    offsets = (150_000 + np.arange(n, dtype=np.uint64) * 9000).reshape(grid)
    lengths = np.full(grid, 8000, np.uint64)
    return manifest_array(url, offsets, lengths, shape=(3, r, c), chunk_shape=(3, *tile), dtype="uint8",
                          codecs=codecs, dimension_names=("c", "y", "x"))

def write(store, g):
    sid = g.SeriesInstanceUID.iloc[0]; pat = g.PatientID.iloc[0]; base = f"gtex/{pat}/{sid[-12:]}"
    etags = {}
    vol = g[g.kind == "VOLUME"].sort_values("r", ascending=False)
    arrays = {}
    for k, (_, i) in enumerate(vol.iterrows()):
        url = f"s3://idc-open-data/{i.crdc_series_uuid}/{i.crdc_instance_uuid}.dcm"; etags[url] = '"' + i.crdc_instance_uuid[:32] + '"'
        arrays[str(k)] = level(url, int(i.r), int(i.c), JPEG, (T, T))
    shapes = [a.shape for a in arrays.values()]
    ome = image_attrs(name=sid, dims=("c", "y", "x"), level_shapes=shapes, scale={"c": 1, "y": 0.49, "x": 0.49},
                      translation={"c": 0, "y": 0, "x": 0}, units={"c": None, "y": "micrometer", "x": "micrometer"}, omero=rgb_omero())
    zarr.open_group(store, mode="a").require_group(base)
    write_pinned(store, f"{base}/image", ManifestGroup(arrays=arrays, attributes={"ome": ome, "description": "x" * 200}), etags)
    for kind, name in (("THUMBNAIL", "thumbnail"), ("OVERVIEW", "overview")):
        for _, i in g[g.kind == kind].iterrows():
            url = f"s3://idc-open-data/{i.crdc_series_uuid}/{i.crdc_instance_uuid}.dcm"; etags[url] = '"x"'
            a = level(url, int(i.r), int(i.c), RAW, (int(i.r), int(i.c)))
            write_pinned(store, f"{base}/{name}", ManifestGroup(arrays={"0": a}, attributes={"description": "y" * 200}), etags)
    grp = zarr.open_group(store, mode="a")[base]
    d = grp.create_array("dicom", shape=(4096,), chunks=(4096,), dtype="uint8", compressors=zarr.codecs.ZstdCodec(level=5))
    d[:] = np.frombuffer(os.urandom(2048) * 2, np.uint8)
    return sum(a.manifest._offsets.size for a in arrays.values())

refs = 0; t0 = time.time()
for b in range(0, len(slides), 500):
    session = repo.writable_session("main")
    tw = time.time(); forks = []
    for _, g in slides[b:b + 500]:
        f = session.fork(); refs += write(f.store, g); forks.append(f)
    tm = time.time(); session.merge(*forks)
    tc = time.time(); cpu = time.process_time(); sid = commit(session, f"batch {b}")
    done = time.time(); cpu = time.process_time() - cpu
    snaps = sorted((os.path.getmtime(p), os.path.getsize(p)) for p in (os.path.join(REPO, "snapshots", x) for x in os.listdir(os.path.join(REPO, "snapshots"))))
    mbytes = sum(os.path.getsize(os.path.join(REPO, "manifests", x)) for x in os.listdir(os.path.join(REPO, "manifests")))
    print(json.dumps({"slides": min(b + 500, len(slides)), "refs": refs, "write_s": round(tm - tw, 1), "merge_s": round(tc - tm, 1),
                      "commit_s": round(done - tc, 1), "commit_cpu_s": round(cpu, 1), "snapshot_bytes": snaps[-1][1], "manifests_bytes": mbytes,
                      "manifest_files": len(os.listdir(os.path.join(REPO, "manifests"))), "elapsed_s": round(done - t0)}), flush=True)

t = time.time()
from minus80.core.icechunk_io import open_repository
r = open_repository(REPO); s = r.readonly_session("main"); root = zarr.open_group(s.store, mode="r")
t_open = time.time() - t
t = time.time(); n = sum(1 for _ in root.members(max_depth=None)); t_walk = time.time() - t
print(json.dumps({"open_s": round(t_open, 2), "nodes": n, "walk_s": round(t_walk, 1)}))
