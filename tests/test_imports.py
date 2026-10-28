"""A parser's dependencies load only when that parser is imported.

Each parser's third-party reader is an optional extra, so importing the package
or the ``parsers`` namespace must not pull any of them in.
"""

import subprocess
import sys

PARSER_DEPENDENCIES = ["anndata", "av", "czifile", "flac_numcodecs", "h5py", "imagecodecs", "mrcfile", "nd2", "pydicom", "pynwb", "soundfile", "tifffile"]


def test_package_imports_no_parser_dependency():
    code = (
        "import sys, minus80, minus80.parsers, minus80.core, minus80.ome; "
        f"print(sorted(m for m in {PARSER_DEPENDENCIES!r} if m in sys.modules))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert loaded == "[]"


def test_zip_parsing_loads_no_io_and_no_virtualizarr():
    """The parsing modules work on bytes alone, so they must not pull in a store or VirtualiZarr."""
    code = (
        "import sys, minus80.parsers.zip.structures, minus80.parsers.zip.directory; "
        "print(sorted(m for m in ('obstore', 'virtualizarr', 'minus80.core.fetch') if m in sys.modules))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert loaded == "[]"
