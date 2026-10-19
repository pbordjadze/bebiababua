"""Shared test environment: point babua's paths at a scratch directory.

Imported first by every test module, before babua_lib, because the paths are
read at import time.
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = tempfile.mkdtemp(prefix="babua-test-")
os.environ.update({
    "BABUA_STATE_DIR": os.path.join(ROOT, "cache"),
    "BABUA_RUN_DIR": os.path.join(ROOT, "run"),
    "BABUA_DIR": os.path.join(ROOT, "config"),
    "KODI_HOME": os.path.join(ROOT, "kodi"),
    "BABUA_SCREENSHOT_DIR": os.path.join(ROOT, "screenshots"),
})
sys.path.insert(0, os.path.join(HERE, "..", "device", ".config", "babua", "bin"))


def reset():
    """Empty the scratch directory between tests."""
    for name in os.listdir(ROOT):
        shutil.rmtree(os.path.join(ROOT, name), ignore_errors=True)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()
