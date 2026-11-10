"""Stored zip members as seekable files, so a zip inside a zip opens without extraction."""

from __future__ import annotations

import io
import struct
import zipfile
from typing import BinaryIO

LOCAL_HEADER = struct.Struct("<4s22xHH")


class Window(io.RawIOBase):
    """A read-only, seekable view of ``size`` bytes of ``base`` from ``start``."""

    def __init__(self, base: BinaryIO, start: int, size: int):
        self.base, self.start, self.size, self.pos = base, start, size, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        origin = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = max(0, origin + offset)
        return self.pos

    def readinto(self, buffer) -> int:
        # Remote readers lack readinto, so read and copy.
        want = min(len(buffer), max(0, self.size - self.pos))
        self.base.seek(self.start + self.pos)
        data = self.base.read(want) if want else b""
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)


def member_file(archive: BinaryIO, name: str) -> io.BufferedReader:
    """A seekable file over the data of the STORED member ``name`` of ``archive``."""
    info = zipfile.ZipFile(archive).getinfo(name)
    if info.compress_type != zipfile.ZIP_STORED:
        method = {zipfile.ZIP_DEFLATED: "deflate"}.get(info.compress_type, info.compress_type)
        raise NotImplementedError(f"zip member {name} uses method {method}; only stored members open")
    # The local header's name and extra lengths can differ from the central directory's.
    archive.seek(info.header_offset)
    signature, name_length, extra_length = LOCAL_HEADER.unpack(archive.read(LOCAL_HEADER.size))
    if signature != zipfile.stringFileHeader:
        raise ValueError(f"no local header for {name} at {info.header_offset}")
    start = info.header_offset + LOCAL_HEADER.size + name_length + extra_length
    return io.BufferedReader(Window(archive, start, info.file_size))


def member_zip(archive: BinaryIO, name: str) -> zipfile.ZipFile:
    """The zip stored as member ``name`` of ``archive``."""
    return zipfile.ZipFile(member_file(archive, name))
