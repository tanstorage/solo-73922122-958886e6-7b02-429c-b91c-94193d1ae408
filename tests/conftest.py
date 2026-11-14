"""Test configuration.

Set environment defaults *before* importing ``app.main`` (which builds a
default app/store at import time) so tests never touch the repository's data
directory.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_TEMP_ROOT = tempfile.mkdtemp(prefix="bom-tests-")
os.environ.setdefault("BOM_DB_PATH", os.path.join(_TEMP_ROOT, "import-time.db"))
os.environ.setdefault("BOM_SEED_DEMO", "0")
os.environ.setdefault("BOM_DEMO_MODE", "1")


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TEMP_ROOT, ignore_errors=True)
