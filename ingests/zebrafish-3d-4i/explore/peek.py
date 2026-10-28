"""Look at the S-BIAD3228 zip's raw structures, one at a time, by fetching a few bytes each.

This script is for learning the zip format on the real file, so it decodes
the structures with ``struct`` and prints every field, where the library code
(:mod:`minus80.parsers.zip`) leaves the parsing to ``zipfile``. Each command
fetches only the bytes it shows, with :func:`minus80.core.fetch.read_range`,
and the functions below are split the same way: ``fetch_*`` functions return
bytes, ``describe_*`` functions turn bytes into text and fetch nothing.

Commands, run from the repository root::

    uv run --package zebrafish-3d-4i-ingest --group explore \\
        python ingests/zebrafish-3d-4i/explore/peek.py end
    ... peek.py entry C/02/2/0/1/13/0/7   # the central directory's entry for one member
    ... peek.py local C/02/2/0/1/13/0/7   # the member's local header and data descriptor
    ... peek.py chunk C/02/2/0/1/13/0/7   # inflate the member, read its Blosc header, decode it

The README beside this script walks through them in order.
"""

from __future__ import annotations

import argparse
import json
import struct

import numpy as np
import pyarrow.compute as pc
from numcodecs import Blosc

from minus80.core.fetch import read_range
from minus80.parsers.zip.reading import read_members

from inputs import SOURCE, directory_table, load_directory_facts

#: Bytes to fetch from the end of the file: enough for all three end records.
END_BYTES = 120
END_RECORD = struct.Struct("<4sHHHHIIH")              # PK\x05\x06, 22 bytes
ZIP64_LOCATOR = struct.Struct("<4sIQI")                 # PK\x06\x07, 20 bytes
ZIP64_END_RECORD = struct.Struct("<4sQHHIIQQQQ")        # PK\x06\x06, 56 bytes
DIRECTORY_ENTRY = struct.Struct("<4sHHHHHHIIIHHHHHII")  # PK\x01\x02, 46 bytes
LOCAL_HEADER = struct.Struct("<4sHHHHHIIIHH")           # PK\x03\x04, 30 bytes
DATA_DESCRIPTOR = struct.Struct("<4sIII")               # PK\x07\x08, 16 bytes
BLOSC_HEADER = struct.Struct("<BBBBIII")                # 16 bytes at the start of a frame


def fetch_end(source: str) -> tuple[int, bytes]:
    """The archive's last :data:`END_BYTES` bytes, and where they start."""
    size = load_directory_facts(source)["archive_size"]
    return size - END_BYTES, read_range(source, size - END_BYTES, size, patient=True)


def describe_end(start: int, data: bytes) -> list[str]:
    """The three end records, field by field."""
    end_at = data.rfind(b"PK\x05\x06")
    locator_at = data.rfind(b"PK\x06\x07")
    zip64_at = data.rfind(b"PK\x06\x06")
    (_, _, _, _, entries, directory_size, directory_offset, _) = END_RECORD.unpack_from(data, end_at)
    (_, _, zip64_offset, _) = ZIP64_LOCATOR.unpack_from(data, locator_at)
    (_, _, _, _, _, _, _, zip64_entries, zip64_size, zip64_directory) = ZIP64_END_RECORD.unpack_from(data, zip64_at)
    return [
        f"end of central directory record at byte {start + end_at:,}",
        f"  entries {entries:#06x}, directory size {directory_size:,}, directory offset {directory_offset:#010x}",
        "  (0xffff and 0xffffffff mean: too large for this field, see the ZIP64 record)",
        f"ZIP64 locator at byte {start + locator_at:,}: the ZIP64 end record is at byte {zip64_offset:,}",
        f"ZIP64 end record at byte {start + zip64_at:,}",
        f"  entries {zip64_entries:,}",
        f"  central directory: {zip64_size:,} bytes, from byte {zip64_directory:,} to {zip64_directory + zip64_size:,}",
    ]


def fetch_entry(source: str, name: str) -> tuple[dict, bytes]:
    """The member's row of the cached central directory, and the raw bytes of its directory entry."""
    table = directory_table(source)
    row = table.filter(pc.equal(table["name"], name)).to_pylist()
    if not row:
        raise SystemExit(f"no member {name}")
    row = row[0]
    return row, read_range(source, row["entry_offset"], row["entry_offset"] + row["entry_bytes"], patient=True)


def describe_entry(row: dict, data: bytes) -> list[str]:
    """One central-directory entry, field by field."""
    (signature, made_by, needed, flags, method, _, date, crc, compressed, uncompressed,
     name_length, extra_length, _, _, _, _, header_offset) = DIRECTORY_ENTRY.unpack_from(data)
    name = data[46:46 + name_length].decode()
    extra = data[46 + name_length:46 + name_length + extra_length]
    blocks, position = [], 0
    while position + 4 <= len(extra):
        block_id, length = struct.unpack_from("<HH", extra, position)
        blocks.append(f"{block_id:#06x} ({length} bytes: {extra[position + 4:position + 4 + length].hex()})")
        position += 4 + length
    return [
        f"directory entry at byte {row['entry_offset']:,}, {len(data)} bytes: signature {signature!r}",
        f"  name {name!r}",
        f"  version made by {made_by} (system {made_by >> 8}, zip {made_by & 0xFF}), needed {needed}",
        f"  flags {flags:#06x}: data descriptor {bool(flags & 0x08)}, UTF-8 name {bool(flags & 0x800)}",
        f"  method {method} (8 is Deflate), DOS date {date} (33 is 1980-01-01), CRC {crc:08x}",
        f"  compressed {compressed:,}, uncompressed {uncompressed:,}",
        f"  local header offset field {header_offset:#010x}" + (" (see ZIP64 block)" if header_offset == 0xFFFFFFFF else ""),
        f"  extra field, {extra_length} bytes: " + ", ".join(blocks),
        f"  so the local header starts at byte {row['local_header_offset']:,}",
    ]


def fetch_local(source: str, row: dict) -> tuple[bytes, bytes]:
    """The member's local header with its name and extra field, and the 20 bytes after its data."""
    start = row["local_header_offset"]
    header = read_range(source, start, start + LOCAL_HEADER.size + 512, patient=True)
    (_, _, _, _, _, _, _, _, _, name_length, extra_length) = LOCAL_HEADER.unpack_from(header)
    data_end = start + LOCAL_HEADER.size + name_length + extra_length + row["compressed"]
    return header, read_range(source, data_end, data_end + DATA_DESCRIPTOR.size + 4, patient=True)


def describe_local(row: dict, header: bytes, after: bytes) -> list[str]:
    """The local header, the data descriptor after the data, and the next member's signature."""
    start = row["local_header_offset"]
    (signature, _, flags, method, _, _, crc, compressed, uncompressed, name_length, extra_length) = LOCAL_HEADER.unpack_from(header)
    data_start = start + LOCAL_HEADER.size + name_length + extra_length
    data_end = data_start + row["compressed"]
    (descriptor_signature, descriptor_crc, descriptor_compressed, descriptor_uncompressed) = DATA_DESCRIPTOR.unpack_from(after)
    return [
        f"local header at byte {start:,}: signature {signature!r}, flags {flags:#06x}, method {method}",
        f"  CRC {crc:#x}, compressed {compressed}, uncompressed {uncompressed}: zeros, the sizes follow the data",
        f"  name {header[30:30 + name_length].decode()!r}, extra field {extra_length} bytes",
        f"  data: bytes {data_start:,} to {data_end:,} ({row['compressed']:,} bytes, from the directory)",
        f"data descriptor at byte {data_end:,}: signature {descriptor_signature!r}, CRC {descriptor_crc:08x}, "
        f"compressed {descriptor_compressed:,}, uncompressed {descriptor_uncompressed:,}",
        f"next local header at byte {data_end + DATA_DESCRIPTOR.size:,}: starts with {after[16:20]!r}",
    ]


def describe_chunk(name: str, frame: bytes, zarray: dict) -> list[str]:
    """A chunk's Blosc header, and its pixels decoded with the array's codec."""
    lines = [f"{name}: inflated to {len(frame):,} bytes, CRC checked against the directory"]
    if not frame:
        return lines + ["an empty member: there is no Blosc frame to decode; it reads as 0"]
    version, _, flags, type_size, decoded_size, block_size, frame_size = BLOSC_HEADER.unpack_from(frame)
    lines += [
        f"Blosc header: version {version}, flags {flags:#04x} (byte shuffle: {bool(flags & 1)}), type size {type_size}",
        f"  decoded size {decoded_size:,}, block size {block_size:,}, frame size {frame_size:,}",
    ]
    if frame_size > len(frame):
        return lines + [f"cut short: the header promises {frame_size:,} bytes but only {len(frame):,} are present"]
    pixels = np.frombuffer(Blosc().decode(frame), dtype=zarray["dtype"]).reshape(zarray["chunks"])
    return lines + [
        f"decoded with {zarray['compressor']}: shape {pixels.shape}, dtype {pixels.dtype}",
        f"  min {pixels.min()}, max {pixels.max()}, mean {pixels.mean():.1f}, zeros {np.mean(pixels == 0):.1%}",
    ]


def array_of(name: str) -> str:
    """The array a chunk belongs to: the name without its 3 (label) or 4 (image) chunk indices."""
    parts = name.split("/")
    return "/".join(parts[:-3] if "labels" in parts else parts[:-4])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=SOURCE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("end", help="the end records: where the central directory is")
    for command, text in (("entry", "one member's central-directory entry"),
                          ("local", "one member's local header and data descriptor"),
                          ("chunk", "inflate one member and decode its Blosc frame")):
        commands.add_parser(command, help=text).add_argument("name")
    args = parser.parse_args()

    if args.command == "end":
        lines = describe_end(*fetch_end(args.source))
    elif args.command == "entry":
        lines = describe_entry(*fetch_entry(args.source, args.name))
    elif args.command == "local":
        row, _ = fetch_entry(args.source, args.name)
        lines = describe_local(row, *fetch_local(args.source, row))
    else:
        array = array_of(args.name)
        fetched = read_members(args.source, [args.name, array + "/.zarray"], directory_table(args.source))
        lines = describe_chunk(args.name, fetched[args.name], json.loads(fetched[array + "/.zarray"]))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
