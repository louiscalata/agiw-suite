"""Private installer fixtures with the profile's inherited Windows ACL."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import uuid


def _retry_readonly(function, path, error):
    """Windows refuses to unlink Git's read-only loose objects."""
    if not isinstance(error, PermissionError) or function not in (os.unlink, os.remove, os.rmdir):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)  # A second failure must reach unittest's cleanup reporting.


def remove_tree(root):
    if sys.version_info >= (3, 12):
        shutil.rmtree(root, onexc=_retry_readonly)
    else:
        shutil.rmtree(root, onerror=lambda function, path, info:
                      _retry_readonly(function, path, info[1]))


@contextmanager
def temporary_root(test_case):
    """A temporary tree whose cleanup cannot replace a test-body failure."""
    root = Path(tempfile.mkdtemp())
    test_case.addCleanup(remove_tree, root)
    yield root


@contextmanager
def install_root(test_case):
    """Use the profile's inherited ACL; defer removal to the owning test's cleanups."""
    root = Path.home() / (".agiw-install-test-" + uuid.uuid4().hex)
    # tempfile's 0o700 mode creates a protected OWNER RIGHTS DACL on recent Python.
    os.mkdir(root)
    test_case.addCleanup(remove_tree, root)
    yield root
