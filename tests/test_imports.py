"""A parser's dependencies load only when that parser is imported.

Each parser's third-party reader is an optional extra, so importing the package
or the ``parsers`` namespace must not pull any of them in.
"""

import subprocess
import sys

PARSER_DEPENDENCIES = ["av", "czifile", "h5py", "imagecodecs", "mrcfile", "nd2", "pydicom", "pynwb", "tifffile"]


def test_package_imports_no_parser_dependency():
    code = (
        "import sys, minus80, minus80.parsers, minus80.core, minus80.ome; "
        f"print(sorted(m for m in {PARSER_DEPENDENCIES!r} if m in sys.modules))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert loaded == "[]"
