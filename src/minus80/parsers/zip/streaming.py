"""Cutting a zip's members out of large consecutive ranges of its bytes.

The problem
-----------
Copying a large zip member by member means one request per member: S-BIAD3228's
well C/02 holds 57,603 members in 20.3 GB, and at a few hundred milliseconds a
request most of the time goes to waiting. The members of a zip sit back to
back, so the same bytes can be read as a few hundred large ranges instead,
each byte once, and the members cut out of the ranges as they arrive.

The pieces
----------
- :class:`MemberCutter` (parsing, bytes only) takes the ranges in order and
  returns each member once all of its bytes have arrived, as the zip stores
  them. A member can start in one range and end in the next; the cutter keeps
  the unfinished part until the rest arrives. It reads each member's local
  header from the bytes it was given
  (:func:`~minus80.parsers.zip.directory.member_stored_bytes`), so it needs
  only what the central directory states.
- :func:`member_regions` merges the members' spans into the byte ranges to
  fetch, and :func:`spans_in_file_order` puts the members in the order the
  cutter needs.

Everything here parses bytes or tables and fetches nothing.
:func:`minus80.parsers.zip.reading.stream_members` fetches the ranges and
feeds them to a cutter.

Members come back stored, not inflated. Inflating is CPU work that a caller
usually wants on a thread pool, with
:func:`~minus80.parsers.zip.directory.inflate_member`.

Terms: a *member* is one file inside the zip; its *span* runs from its local
header to the next member's local header; a *range* is one range request.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

import pyarrow as pa

from .directory import member_spans, member_stored_bytes


class StoredMember(NamedTuple):
    """One member, as the zip stores it."""

    name: str
    #: The member's row of the central directory, with its span added as
    #: ``start`` and ``end``.
    entry: Mapping
    #: The member's data as stored: Deflate-compressed for most members.
    stored: bytes


class MemberCutter:
    """Cut members out of an archive's bytes as consecutive ranges of it arrive.

    Parsing, bytes only. ``members`` are directory rows with each member's
    span as ``start`` and ``end`` (from
    :func:`~minus80.parsers.zip.directory.member_spans`), sorted by ``start``.
    Members not listed are skipped, so a caller can ask for part of an
    archive.

    :meth:`feed` takes each range as ``(offset, data)``. Ranges must arrive in
    order, and every listed member's span must be covered without a gap; a
    range may end in the middle of a member.
    """

    def __init__(self, members: Sequence[Mapping]) -> None:
        self._members = list(members)
        self._next = 0
        self._buffer = bytearray()
        self._buffer_start = 0

    def feed(self, offset: int, data: bytes) -> list[StoredMember]:
        """The members whose last byte is in ``data``, in file order."""
        if self._buffer and offset != self._buffer_start + len(self._buffer):
            raise ValueError(
                f"a range starts at byte {offset}, but the bytes so far end at "
                f"{self._buffer_start + len(self._buffer)}"
            )
        if not self._buffer:
            self._buffer_start = offset
        self._buffer += data
        end = self._buffer_start + len(self._buffer)
        done = []
        with memoryview(self._buffer) as view:
            while self._next < len(self._members) and self._members[self._next]["end"] <= end:
                member = self._members[self._next]
                if member["start"] < self._buffer_start:
                    raise ValueError(f"{member['name']}: its span starts before the bytes given")
                low, high = member["start"] - self._buffer_start, member["end"] - self._buffer_start
                stored = member_stored_bytes(view[low:high], member["start"], member)
                done.append(StoredMember(member["name"], member, stored))
                self._next += 1
        # Keep only the bytes from the next member's start on, so memory holds
        # at most one unfinished member plus the range being cut.
        keep_from = self._members[self._next]["start"] if self._next < len(self._members) else end
        drop = min(max(keep_from - self._buffer_start, 0), len(self._buffer))
        del self._buffer[:drop]
        self._buffer_start += drop
        return done

    def finish(self) -> None:
        """Raise unless every listed member has been cut out."""
        if self._next < len(self._members):
            missing = self._members[self._next]
            raise ValueError(
                f"{len(self._members) - self._next} members never arrived, from {missing['name']} "
                f"at byte {missing['start']}"
            )


def member_regions(members: Sequence[Mapping]) -> list[tuple[int, int]]:
    """The members' spans merged where one ends where the next starts: the byte ranges to fetch.

    Parsing (of the rows). ``members`` must be sorted by ``start``.
    """
    regions: list[list[int]] = []
    for member in members:
        if regions and regions[-1][1] == member["start"]:
            regions[-1][1] = member["end"]
        else:
            regions.append([member["start"], member["end"]])
    return [(start, end) for start, end in regions]


def spans_in_file_order(directory: pa.Table, names: Iterable[str]) -> list[dict]:
    """The named members' directory rows with ``start`` and ``end``, sorted by ``start``.

    Parsing (of the table), for :class:`MemberCutter` and :func:`member_regions`.
    Raises ``KeyError`` for a name the directory does not hold.
    """
    spans = member_spans(directory, names)
    rows = directory.take(spans["entry"]).to_pylist()
    for row, start, end in zip(rows, spans["start"].to_pylist(), spans["end"].to_pylist(), strict=True):
        row["start"], row["end"] = start, end
    return rows
