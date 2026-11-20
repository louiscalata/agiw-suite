"""Skip a root monitor test module on Windows.

The root suite tests the macOS/POSIX monitor. Its modules rely on ``fcntl`` locks, ``st_uid``
ownership checks, ``O_NOFOLLOW``/``O_DIRECTORY`` containment, ``os.pread`` and POSIX file modes,
which Windows does not provide. The Windows edition has its own suite under ``windows/tests``.

Import this module first, above an ``# isort: split`` marker, so the skip happens before any
POSIX-only import runs. On other platforms it does nothing.
"""

import sys
import unittest

REASON = "POSIX-only monitor module (fcntl, st_uid ownership, O_NOFOLLOW); Windows edition tests live in windows/tests"

if sys.platform == "win32":
    raise unittest.SkipTest(REASON)
