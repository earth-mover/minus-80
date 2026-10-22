"""FLAC audio as Zarr chunk references. See README.md."""
from .parser import ChunkTable, FrameScan, chunk_table, scan_frames, write_array

__all__ = ["ChunkTable", "FrameScan", "chunk_table", "scan_frames", "write_array"]
