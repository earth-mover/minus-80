"""Reference RxRx19a or RxRx19b in place, as OME-Zarr 0.6 HCS plates in one Icechunk repo.

    uv run --package rxrx19-ingest python ingests/rxrx19/ingest.py \\
        --repo <dir | arraylake://earthmover-biology/RxRx19> --dataset rxrx19a \\
        [--plates HRCE-1/1,HRCE-1/2] [--branch main] [--embeddings] [--packed]

Recursion publishes each dataset as one zip of PNGs on ``gs://rxrx``. Every
PNG in the zip is stored, not deflated, so it is one contiguous byte range, and
every member's local header has the same 28-byte extra field, so that range
comes from the zip's central directory alone. The ingest therefore reads no
image data: it builds the store's groups and arrays from ``metadata.csv`` and
points each chunk at its PNG. ``PLAN.md`` argues the layout and ``LAYOUTS.md``
records what the bucket holds.

A run writes one dataset, or some of its plates, in one commit: every commit
writes a whole new snapshot, so committing plate by plate would upload the
growing snapshot once per plate.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import os
import subprocess
import time
import zlib
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.csv
import pyarrow.parquet as pq
import zarr
from imagecodecs.zarr import Png

import layout
from layout import DATASETS, Dataset
from minus80.core.fetch import cache_dir
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, create_repo
from minus80.core.nodes import array_document, group_document, write_nodes
from minus80.parsers.zip.reading import stream_members
from minus80.parsers.zip.streaming import spans_in_file_order
from source import Survey, license_and_readmes, rebased, survey


# The source
#
# The zip's central directory lists every PNG with its offset and size, and
# metadata.csv has one row per site. Both are read once by source.survey; the
# central directory is kept as Parquet, so a rerun makes no request for it.


def plates_to_write(s: Survey, chosen: str | None) -> list[tuple[str, int]]:
    """The plates to write, as ``(experiment, plate)``: those named in ``chosen`` (``HRCE-1/1,…``), or all."""
    every = sorted({(e, int(p)) for e, p in zip(s.metadata.experiment, s.metadata.plate)})
    if not chosen:
        return every
    wanted = [(e, int(p)) for e, p in (item.split("/") for item in chosen.split(",") if item)]
    missing = sorted(set(wanted) - set(every))
    if missing:
        raise SystemExit(f"{s.dataset.key} has no plate {missing}")
    return wanted


# The groups and arrays
#
# Each plate is an HCS plate: a group per row, a group per well carrying the
# well's metadata.csv fields, and a group per site holding one (c, y, x) array
# whose chunks are the site's PNGs. Every site's array has the same zarr.json,
# and every site's group the same document but for its name, so each is
# encoded once per dataset (minus80.core.nodes explains why not
# zarr.create_hierarchy). The nodes are written as bytes into the session.

#: Stands in for a site's id in the encoded site document; site ids are letters, digits, '-' and '_'.
SITE = "@@SITE@@"


def node_documents(s: Survey, plates: list[tuple[str, int]], texts: dict) -> tuple[dict[str, bytes], list[tuple[str, str]]]:
    """Every node of ``plates``, as ``{path: zarr.json}``, and each site's array path with its site id."""
    height, width = s.png_header["height"], s.png_header["width"]
    if height != width:
        raise ValueError(f"{s.dataset.key}: PNGs are {height} × {width}; the layout expects square images")
    d = dataclasses.replace(s.dataset, side=width)
    channels = int(s.pngs.channel.max())
    site_template = group_document(layout.site_document(d, SITE, channels)).decode()
    array = array_document(shape=(channels, height, width), chunks=(1, height, width), dtype="uint8",
                           serializer=Png(), compressors=None, fill_value=0, dimension_names=("c", "y", "x"))
    documents = dataset_documents(d, texts)
    arrays = []
    for experiment, plate in plates:
        rows = s.metadata[(s.metadata.experiment == experiment) & (s.metadata.plate == plate)].to_dict("records")
        documents[f"{d.key}/{experiment}"] = group_document(layout.experiment_document(rows[0]["cell_type"]))
        plate_path = layout.plate_path(d.key, experiment, plate)
        documents[plate_path] = group_document(
            layout.plate_document(experiment, plate, [r["well"] for r in rows], max(r["site"] for r in rows)))
        wells: dict[str, list[dict]] = {}
        for r in rows:
            wells.setdefault(r["well"], []).append(r)
        for well, sites in wells.items():
            row, column = layout.split_well(well)
            documents[f"{plate_path}/{row}"] = group_document()
            documents[f"{plate_path}/{row}/{column}"] = group_document(
                layout.well_document([r["site"] for r in sites], sites[0]))
            for r in sites:
                path = f"{plate_path}/{row}/{column}/{r['site']}"
                documents[path] = site_template.replace(SITE, r["site_id"]).encode()
                documents[f"{path}/0"] = array
                arrays.append((f"{path}/0", r["site_id"]))
    return documents, arrays


def dataset_documents(d: Dataset, texts: dict) -> dict[str, bytes]:
    """The root and the dataset's group: license, READMEs, citation and the store's choices (layout.dataset_document)."""
    return {"": group_document(layout.root_document()), d.key: group_document(layout.dataset_document(d, texts))}


# The references
#
# Each PNG becomes the chunk (channel - 1, 0, 0) of its site's array, pointing
# at the gs:// URL of the zip with the PNG's own offset and length. By default
# they go into the same session as the nodes; with --packed, they go to a
# Parquet file that ice-pack commits as one manifest per plate.


def reference_table(s: Survey, arrays: list[tuple[str, str]]) -> pa.Table:
    """One row per PNG of the sites in ``arrays``: array path, chunk index, location, offset, length."""
    paths = dict((site_id, path) for path, site_id in arrays)
    pngs = s.pngs[s.pngs.site_id.isin(paths)].sort_values(["site_id", "channel"])
    return pa.table({
        "array_path": pa.array([paths[i] for i in pngs.site_id]),
        "chunk": pa.array([[c - 1, 0, 0] for c in pngs.channel], pa.list_(pa.uint32())),
        "location": pa.array([s.dataset.images] * len(pngs)),
        "offset": pa.array(pngs.data_offset.to_numpy(), pa.uint64()),
        "length": pa.array(pngs.bytes.to_numpy(), pa.uint64()),
    })


def set_references(store, references: pa.Table) -> None:
    import icechunk

    rows = references.to_pydict()
    by_array: dict[str, list] = {}
    for path, chunk, location, offset, length in zip(*rows.values()):
        by_array.setdefault(path, []).append(
            icechunk.VirtualChunkSpec(index=chunk, location=location, offset=offset, length=length))
    for path, specs in by_array.items():
        store.set_virtual_refs(path, specs, validate_containers=False)


def pack_references(repo_location: str, branch: str, references: pa.Table, message: str) -> None:
    """Commit ``references`` with ice-pack, one manifest per plate (``<dataset>/<experiment>/Plate<n>``).

    ice-pack is a separate Rust process, so it cannot use the arraylake
    client's storage object. For an ``arraylake://`` repo it is pointed at the
    repo's own prefix of the org's bucket, with temporary credentials scoped
    to that prefix, which the client mints but exposes only through a private
    method; they are passed in ice-pack's environment and never written down.
    """
    path = Path(cache_dir()) / "rxrx19" / "references.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(references, path)
    command = [os.environ.get("ICE_PACK", "ice-pack"), repo_location, "--branch", branch, "--refs", str(path),
               "--group-depth", "3", "--message", message]
    environment = None
    if repo_location.startswith(ARRAYLAKE):
        from arraylake import Client
        from minus80.core.aio import run_sync

        name = repo_location.removeprefix(ARRAYLAKE)
        org, repo = name.split("/")
        client = Client()
        model = client.get_repo_object(name).model_dump()
        bucket = model["bucket"]
        credentials = run_sync(client.aclient._get_s3_delegated_credentials_from_repo(org, repo))
        command[1] = f"s3://{bucket['name']}/{model['prefix']}"
        command += ["--endpoint", bucket["extra_config"]["endpoint_url"],
                    "--region", bucket["extra_config"].get("region_name", "auto")]
        environment = {**os.environ, "AWS_ACCESS_KEY_ID": credentials.aws_access_key_id,
                       "AWS_SECRET_ACCESS_KEY": credentials.aws_secret_access_key,
                       "AWS_SESSION_TOKEN": credentials.aws_session_token}
    subprocess.run(command, check=True, env=environment)


# The embeddings
#
# embeddings.csv has one row per site: site_id, then one value per feature. It
# is a sites × features matrix of floats, so it is stored as a native float32
# array (float32 holds every value the CSV prints), with its rows in
# metadata.csv's order and a site_id array beside it. The CSV is a deflated
# member, so it is read and inflated as one stream.


class Inflating(io.RawIOBase):
    """A deflated byte string, read as the bytes it inflates to."""

    def __init__(self, data: bytes):
        self.inflater, self.data, self.pending = zlib.decompressobj(-15), memoryview(data), b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        while not self.pending and self.data:
            piece, self.data = self.data[: 1 << 22], self.data[1 << 22:]
            self.pending = self.inflater.decompress(piece)
        n = min(len(buffer), len(self.pending))
        buffer[:n], self.pending = self.pending[:n], self.pending[n:]
        return n


def write_embeddings(s: Survey, store) -> None:
    name = next(n for n in s.embeddings_zip["name"].to_pylist() if n.endswith(".csv"))
    # Concurrent range requests, not one GET: RxRx19a's member is 1.4 GB deflated.
    (member,) = stream_members(layout.readable(s.dataset.embeddings), spans_in_file_order(s.embeddings_zip, [name]))
    deflated = member.stored
    order = {site_id: i for i, site_id in enumerate(s.metadata.site_id)}
    reader = pyarrow.csv.open_csv(io.BufferedReader(Inflating(deflated), buffer_size=1 << 22))
    features = len(reader.schema) - 1
    values = np.zeros((len(order), features), dtype="float32")
    seen, started, reported = 0, time.monotonic(), time.monotonic()
    for batch in reader:
        rows = [order[site_id] for site_id in batch.column(0).to_pylist()]
        values[rows] = np.column_stack([batch.column(i).to_numpy().astype("float32") for i in range(1, features + 1)])
        seen += len(rows)
        if time.monotonic() - reported > 10:
            reported = time.monotonic()
            rate = seen / (reported - started)
            print(f"  embeddings: {seen:,} of {len(order):,} rows, {rate:,.0f} rows/s, "
                  f"{(len(order) - seen) / rate:.0f} s left", flush=True)
    if seen != len(order):
        raise ValueError(f"embeddings.csv has {seen:,} rows for {len(order):,} sites")
    key = s.dataset.key
    embeddings = zarr.create_array(store, name=f"{key}/embeddings", shape=values.shape, dtype="float32",
                                   chunks=(min(4096, len(values)), features), dimension_names=("site", "feature"),
                                   attributes={"source": s.dataset.embeddings, "rows": "in metadata.csv's order"})
    embeddings[:] = values
    site_ids = zarr.create_array(store, name=f"{key}/site_id", shape=(len(order),), dtype=str,
                                 chunks=(min(65536, len(order)),), dimension_names=("site",))
    site_ids[:] = np.array(list(order), dtype=object)


# Running it


def open_repo(location: str, d: Dataset):
    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location, source_prefixes=[d.images])
    return create_repo(location, [d.images], prefixes=[d.images.rsplit("/", 2)[0] + "/"], open_existing=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="a directory, or arraylake://<org>/<repo>")
    parser.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    parser.add_argument("--plates", help="HRCE-1/1,HRCE-1/2,…; all of the dataset's plates when left out")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--embeddings", action="store_true", help="also copy embeddings.csv into a float32 array")
    parser.add_argument("--packed", action="store_true", help="commit the references with ice-pack, one manifest per plate")
    parser.add_argument("--source-root", help="read the zips from this copy of gs://rxrx/ (the tests' fixture)")
    parser.add_argument("--cache", type=Path, help="where to keep the central directories (default: the minus80 cache)")
    parser.add_argument("--attributes-only", action="store_true",
                        help="rewrite only the root and dataset groups' attributes, in their own commit")
    args = parser.parse_args()

    run(args.repo, args.dataset, plates=args.plates, branch=args.branch, embeddings=args.embeddings,
        packed=args.packed, source_root=args.source_root, cache=args.cache, attributes_only=args.attributes_only)


def run(repo_location: str, dataset: str, *, plates: str | None = None, branch: str = "main",
        embeddings: bool = False, packed: bool = False, source_root: str | None = None,
        cache: Path | None = None, attributes_only: bool = False) -> str:
    """Write ``plates`` of ``dataset`` (all when ``None``) to ``branch`` of the repo, and return the last snapshot id."""
    d = DATASETS[dataset]
    if source_root:
        d = rebased(d, source_root)
    started = time.monotonic()
    s = survey(d, cache)
    texts = license_and_readmes(s)
    if attributes_only:
        repo = open_repo(repo_location, d)
        session = repo.writable_session(branch)
        write_nodes(session.store, dataset_documents(d, texts))
        if not session.has_uncommitted_changes:
            print(f"{d.key}: the attributes on {branch} are already current", flush=True)
            return repo.lookup_branch(branch)
        snapshot = session.commit(f"{d.key}: the license, both READMEs, the citation and the store's choices, "
                                  f"on the root and the {d.key} group")
        print(f"committed {snapshot} ({time.monotonic() - started:.1f} s)", flush=True)
        return snapshot
    plates = plates_to_write(s, plates)
    documents, arrays = node_documents(s, plates, texts)
    references = reference_table(s, arrays)
    print(f"{d.key}: {len(plates)} plates, {len(arrays):,} sites, {len(documents):,} nodes, "
          f"{references.num_rows:,} references ({time.monotonic() - started:.1f} s)", flush=True)

    repo = open_repo(repo_location, d)
    if branch not in repo.list_branches():
        repo.create_branch(branch, repo.lookup_branch("main"))
    session = repo.writable_session(branch)
    write_nodes(session.store, documents)
    if not packed:
        set_references(session.store, references)
    if embeddings:
        write_embeddings(s, session.store)
    message = f"{d.key}: {len(plates)} plates, {len(arrays):,} sites referenced in place"
    snapshot = session.commit(message + ("; references follow" if packed else ""))
    print(f"committed {snapshot} ({time.monotonic() - started:.1f} s)", flush=True)
    if packed:
        pack_references(repo_location, branch, references, message + ": references")
        snapshot = repo.lookup_branch(branch)
        print(f"packed into {snapshot} ({time.monotonic() - started:.1f} s)", flush=True)
    return snapshot


if __name__ == "__main__":
    main()
