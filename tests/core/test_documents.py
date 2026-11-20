import zarr
from zarr.storage import MemoryStore

from minus80.core.documents import DOCUMENTS, read_text, text_or_pointer


def test_small_text_stays_inline_and_large_text_moves_beside_its_node():
    root = zarr.open_group(MemoryStore(), mode="a")
    root.require_group("study/slide")
    assert text_or_pointer(root, "study/slide", "small", "abc", of="x", fmt="text") == "abc"
    assert DOCUMENTS not in root["study/slide"]

    big = "é" * 40_000
    pointer = text_or_pointer(root, "study/slide", "big", big, of="slide header", fmt="UTF-8 text")
    assert pointer == {"array": "documents/big", "relative_to": "study/slide", "format": "UTF-8 text",
                       "bytes": 80_000, "reason": "over the 65536-byte limit for attributes"}
    array = root["study/slide/documents/big"]
    assert dict(array.attrs) == {"document_of": "slide header", "format": "UTF-8 text"}
    assert read_text(root, pointer) == big
    assert read_text(root, "abc") == "abc"


def test_a_root_document_points_from_the_root():
    root = zarr.open_group(MemoryStore(), mode="a")
    pointer = text_or_pointer(root, "", "doc", "x" * 10, of="x", fmt="text", limit=4)
    assert pointer["relative_to"] == "/" and pointer["array"] == "documents/doc"
    assert read_text(root, pointer) == "x" * 10
