"""A VirtualiZarr parser for TIFF series, built on tifffile's own parse."""

from .pages import lzw_valid_length
from .parser import TiffParser

__all__ = ["TiffParser", "lzw_valid_length"]
