"""TIFF pages as VirtualiZarr arrays, from tifffile's own parse."""

from .pages import page_array, page_codecs, page_tags, resolution_um, series_arrays

__all__ = ["page_array", "page_codecs", "page_tags", "resolution_um", "series_arrays"]
