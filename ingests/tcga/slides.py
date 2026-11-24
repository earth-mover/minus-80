"""Plumbing for ``ingest.py``: IDC's index tables, JSON-safe rows and progress lines."""

from __future__ import annotations

import math
import time

import numpy as np


def say(line: str) -> None:
    """Print one progress line at once, with the time, so a remote run's log shows where it is."""
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


def idc_tables():
    """IDC's series, slide-microscopy and slide-instance tables, and the IDC release they describe.

    The ``idc-index`` release pinned in ``pyproject.toml`` fixes the IDC
    release, so a rerun selects the same slides.
    """
    from idc_index import IDCClient

    client = IDCClient()
    client.fetch_index("sm_index")
    client.fetch_index("sm_instance_index")
    return client.index, client.sm_index, client.sm_instance_index, client.get_idc_version()


def jsonable(value):
    """``value`` as plain JSON: arrays to lists, numpy scalars to Python, NaN to None."""
    if isinstance(value, np.ndarray):
        return [jsonable(v) for v in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def jsonable_row(row, fields: list[str]) -> dict:
    return {f: jsonable(getattr(row, f)) for f in fields}
