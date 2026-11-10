import sys
import importlib.util
from pathlib import Path

# Reuse the root suite's range server; a package import of ``tests`` would find this directory.
_root = Path(__file__).parents[3] / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("root_conftest", _root)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
range_server = _module.range_server

# The ingest's modules sit beside its script, so they import as top-level names.
sys.path.insert(0, str(Path(__file__).parents[1]))
