"""Lets the tests import the ingest's modules under the repository's importlib import mode.

The root configuration runs pytest with ``--import-mode=importlib``, which
does not put a test file's directory on ``sys.path``, and the ingest's
modules (``ingest``, ``plumbing``, ``fixture``, ``verify``) import each other
by their plain names, as they do when run as scripts. Worker processes the
ingest spawns inherit this ``sys.path``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
