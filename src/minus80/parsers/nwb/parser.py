"""Parse NWB (Neurodata Without Borders) files into VirtualiZarr references.

NWB is HDF5, so the parsing is VirtualiZarr's ``HDFParser``. It is called once
per HDF5 group, with every subgroup dropped, because ``drop_variables`` applies
only to the group parsed and a dataset ``HDFParser`` cannot read has to be
dropped where it sits. The result mirrors the file: numeric datasets are
references into it, strings, compound tables and object references are inlined
by ``HDFParser``, and links are recorded under the draft Links and Object
References convention instead of being copied.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from minus80.core import as_url, registry_for
from minus80.core.fetch import local_copy
from minus80.core.jsonable import jsonable
from minus80.core.manifest import source_urls

#: Group attribute holding the datasets HDFParser cannot represent, read into JSON.
NATIVE = "nwb_native"


@contextmanager
def _opened(source, url: str | None):
    """Yield ``(binary file, url to record)`` for a path, a URL or an open binary file.

    A remote file is indexed from a local copy, because HDF5's chunk B-tree is
    scattered through the file and walking it over the network costs a round
    trip per node. The manifest records the remote URL either way.
    """
    if hasattr(source, "read"):
        if url is None:
            raise ValueError("an open file needs url=, the address the manifest records")
        yield source, url
        return
    source_url = as_url(source)
    with open(local_copy(source_url), "rb") as fh:
        yield fh, url or source_url


def _layout(fh) -> list[tuple[str, set[str], dict]]:
    """Every hard-linked group as ``(path, names to drop, native values)``.

    The names to drop are the group's subgroups, parsed on their own, and its
    compact-layout datasets, for which ``HDFParser`` records no bytes and so
    reads back only the fill value. Those datasets are read here instead.
    """
    import h5py

    out = []
    with h5py.File(fh, "r") as f:
        def visit(g):
            drop, native = set(), {}
            for key in g:
                if not isinstance(g.get(key, getlink=True), h5py.HardLink):
                    continue
                item = g[key]
                if isinstance(item, h5py.Group):
                    drop.add(key)
                elif item.id.get_create_plist().get_layout() == h5py.h5d.COMPACT:
                    drop.add(key)
                    native[key] = _native(item)
            out.append((g.name, drop, native))
            for key in sorted(drop):
                if isinstance(g[key], h5py.Group):
                    visit(g[key])

        visit(f["/"])
    return out


def _native(ds) -> dict:
    return {"value": jsonable(ds[()]), "attrs": jsonable(dict(ds.attrs))}


def _parse(fh, url: str, registry, group: str, drop: set[str]):
    from virtualizarr.parsers import HDFParser

    parser = HDFParser(group=group, drop_variables=drop, links_and_references=True,
                       reader_factory=lambda store, path: fh)
    return parser(url, registry).group


def _group_contents(fh, url: str, registry, group: str, drop: set[str], native: dict):
    """One group's arrays and attributes, with what ``HDFParser`` refuses read natively.

    ``HDFParser`` raises ``NotImplementedError`` on region references and on
    variable-length sequences other than strings. Only when the group raises
    is each dataset parsed alone, to find which.
    """
    import h5py

    try:
        parsed = _parse(fh, url, registry, group, drop)
        return dict(parsed.arrays), dict(parsed.metadata.attributes), native
    except NotImplementedError:
        pass
    with h5py.File(fh, "r") as f:
        g = f[group]
        names = [k for k in g if k not in drop and isinstance(g.get(k), h5py.Dataset)]
        refused = set()
        for name in names:
            try:
                _parse(fh, url, registry, group, set(g) - {name})
            except NotImplementedError:
                refused.add(name)
                native = {**native, name: _native(g[name])}
    parsed = _parse(fh, url, registry, group, drop | refused)
    return dict(parsed.arrays), dict(parsed.metadata.attributes), native


def _finish(marr):
    """Make ``marr``'s attributes strict JSON, name its source and unname invented axes.

    NWB files attach no HDF5 dimension scales, so ``HDFParser`` names every axis
    ``phony_dim_<n>``. The file stated no name, so the axis is left unnamed.
    """
    from virtualizarr.manifests import ManifestArray

    attrs = jsonable(dict(marr.metadata.attributes))
    if sources := source_urls(marr.manifest):
        attrs["source_url"] = sources
    marr = ManifestArray(metadata=marr.metadata.update_attributes(attrs),
                         chunkmanifest=marr.manifest)
    dims = marr.metadata.dimension_names
    if dims is not None:
        kept = tuple(None if n == f"phony_dim_{i}" else n for i, n in enumerate(dims))
        marr = marr.with_dimension_names(None if all(n is None for n in kept) else kept)
    return marr


def file_contents(fh, url: str) -> dict[str, tuple[dict, dict]]:
    """``{group path: (arrays, attributes)}`` for every group of one open NWB file.

    Paths are relative to the file root, which is ``""``.
    """
    registry = registry_for(url)
    out = {}
    for group, drop, native in _layout(fh):
        arrays, attrs, native = _group_contents(fh, url, registry, group, drop, native)
        attrs = jsonable(attrs)
        if native:
            attrs[NATIVE] = native
        out[group.lstrip("/")] = ({k: _finish(a) for k, a in arrays.items()}, attrs)
    return out


def _nest(contents: dict[str, tuple[dict, dict]]):
    """A ``ManifestGroup`` hierarchy from ``{path: (arrays, attributes)}``."""
    from virtualizarr.manifests import ManifestGroup

    def build(path: str):
        prefix = f"{path}/" if path else ""
        children = {p[len(prefix):]: build(p) for p in contents
                    if p.startswith(prefix) and p != path and "/" not in p[len(prefix):]}
        arrays, attrs = contents[path]
        return ManifestGroup(arrays=arrays, groups=children, attributes=attrs)

    return build("")


def _unique(base: str, seen: set[str]) -> str:
    """A collision-free group name, suffixing ``_1``, ``_2`` … on clash."""
    name = base
    i = 1
    while name in seen:
        name = f"{base}_{i}"
        i += 1
    seen.add(name)
    return name


def open_collection(sources, *, urls=None, names=None, attributes=None):
    """Virtualize a collection of NWB files as one ``ManifestGroup``, a group per file.

    Each source is a path, a URL or an open binary file. ``urls``, aligned with
    ``sources``, gives the address each manifest records; an open file needs
    one. ``names``, aligned with ``sources``, names each file's group; the
    default is the file's stem, which for a content-addressed blob URL is an
    opaque id, so pass the archive's asset path (``sub-…/ses-…``) instead.

    Each file's group mirrors its HDF5 hierarchy (see the module docstring).
    ``attributes`` are merged into the root group's.
    """
    from virtualizarr.manifests import ManifestGroup

    sources = [sources] if isinstance(sources, (str, Path)) or hasattr(sources, "read") \
        else list(sources)
    urls = [None] * len(sources) if urls is None else list(urls)
    if names is not None:
        names = list(names)
    for label, given in (("urls", urls), ("names", names)):
        if given is not None and len(given) != len(sources):
            raise ValueError(f"{label} has {len(given)} entries for {len(sources)} sources")
    groups: dict = {}
    seen: set[str] = set()
    for i, (source, url) in enumerate(zip(sources, urls)):
        with _opened(source, url) as (fh, recorded):
            if names is not None:
                name = _unique(names[i], seen)
            else:
                name = _unique(PurePosixPath(recorded.split("?", 1)[0]).stem, seen)
            groups[name] = _nest(file_contents(fh, recorded))
    attrs = {"format": "nwb", "files": list(groups), **jsonable(dict(attributes or {}))}
    return ManifestGroup(arrays={}, groups=groups, attributes=attrs)


def virtual_arrays(grp) -> dict:
    """Map every array in an opened repo to its path, ``<file>/<nwb path>``.

    Returns ``{path: zarr.Array}``.
    """
    import zarr

    return {path: node for path, node in grp.members(max_depth=None)
            if isinstance(node, zarr.Array)}
