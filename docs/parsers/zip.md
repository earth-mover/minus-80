# Zip archives

This page covers how `minus80.parsers.zip` finds an archive's members. For how
to call the parser, see its [README](../../src/minus80/parsers/zip/README.md).

## Read the central directory, unless it is too large to fetch

A zip lists its members twice. A local header sits in front of each member's
data, and the central directory, one contiguous block near the end of the
archive, repeats every header. The end-of-central-directory record, in the
last bytes of the file, says where the central directory starts and how long
it is.

For most archives, read the central directory. VirtualiZarr's
`parse_zip_index` does that in two or three range reads: the tail, then the
central directory if the tail did not already hold it. It reads the local
headers as well only when the offsets the central directory implies do not
tile the data region, because a local header's extra field can differ in
length from its central-directory copy.

The central directory grows with the member count, and `parse_zip_index`
fetches it in one request. For an archive of 7.7 million members that request
is 1.49 GB.

## Walk the local headers in order instead

Each local header records its member's name and compressed size, so the next
header starts where the member's data ends. Reading the archive from byte 0 in
order finds every member without the central directory. `walk_archive` does
this in fixed-size pieces fetched concurrently, carrying a member that spans
two pieces into the next.

A walk that ends exactly at the central directory's offset also shows that the
members tile the data region with no gap.

The walk cannot step over a member whose general-purpose flag bit 3 is set.
Such a member's sizes sit in a data descriptor after its data, so the walk does
not know where the member ends. Writers that cannot seek back, such as a zip
streamed to a socket, produce these members.

## Scan from the middle only to sample

A window that starts mid-archive has no member boundary to walk from.
`scan_local_headers` searches it for the local-header signature `PK\x03\x04`
instead. Those four bytes can also occur inside compressed data, so check every
name a scan returns. A scan suits sampling members from part of an archive. To
list every member, walk from byte 0.

## ZIP64

An archive over 4 GiB, or with more than 65,535 members, stores some sizes and
offsets as ZIP64 values:

- In a local or central header, a size of `0xFFFFFFFF` means the real value is
  in the ZIP64 extra field.
- At the end of the archive, a ZIP64 end record and a locator precede the
  ordinary end record, and the locator gives the ZIP64 record's absolute
  offset.

Recent Python patch releases make `zipfile` check that offset against the
position the locator was read from. A buffer holding only the archive's tail
must therefore report its true positions in the whole archive, which is why
`end_of_central_directory` takes the archive's size.
