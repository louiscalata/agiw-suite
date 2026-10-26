"""Hermetic process-wide resources for the batched publisher/renderer matrix."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import os
import socket
import subprocess
import tempfile
import time
from unittest.mock import patch

NOW = 1800000000.0
_active = None


class AmbientAccess(BaseException):
    """Cannot be swallowed by a production unavailable-data fallback."""


def forbidden(*args, **kwargs):
    raise AmbientAccess('ambient home, process, network or filesystem access forbidden')


def fixture_path(name):
    if _active is None:
        raise RuntimeError('pipeline requires fixture_environment')
    return _active / name


@contextmanager
def fixture_environment():
    global _active
    # Resolve the caller's TMPDIR once. Production deliberately refuses symlink
    # parents; fixtures must not mistake /var or /tmp aliases for bad evidence.
    with tempfile.TemporaryDirectory(prefix='agiw-pipeline-') as temporary:
        _active = Path(temporary).resolve(strict=True)
        from agiw_linux import probes, server, linsys
        original_record, original_read = probes._record, linsys.read_text
        original_names = probes._journal_names
        original_entries, original_path = linsys._entries, linsys._path
        original_private = server.private_directory
        original_cached = server.cached_status
        empty_caches = set()

        def cached(path, now):
            checked(path)
            if path.parent.name != 'no-cache':
                return original_cached(path, now)
            # These fixture directories never exist or get written. Exercise
            # the real miss once, then reuse that immutable filesystem input.
            if path not in empty_caches:
                if original_cached(path, now) is not None:
                    raise AssertionError('empty fixture cache unexpectedly contains data')
                empty_caches.add(path)
            return None

        def checked(path):
            # Lexical check preserves intentional fixture symlinks/failure cases.
            candidate = Path(os.path.abspath(path))
            if not candidate.is_relative_to(_active):
                raise AmbientAccess(f'non-fixture source: {candidate}')
            return path

        def record(path, *args, **kwargs):
            return original_record(checked(path), *args, **kwargs)

        def names(path):
            return original_names(checked(path))

        def read(path, *args, **kwargs):
            return original_read(checked(path), *args, **kwargs)

        def entries(path, *args, **kwargs):
            return original_entries(checked(path), *args, **kwargs)

        def rooted(root, name):
            return original_path(checked(root), name)

        def private(path, *args, **kwargs):
            return original_private(checked(path), *args, **kwargs)

        try:
            with ExitStack() as stack:
                replacements = [(tempfile, 'tempdir', str(_active)),
                    (time, 'time', lambda: NOW + float(os.environ.get('AGIW_FIXTURE_WALL_OFFSET', '0'))),
                    (Path, 'home', classmethod(forbidden)), (subprocess, 'Popen', forbidden),
                    (socket, 'socket', forbidden), (os, 'statvfs', forbidden),
                    (probes, '_http_get', forbidden), (probes, '_record', record),
                    (probes, '_journal_names', names),
                    (linsys, 'read_text', read), (linsys, '_entries', entries), (linsys, '_path', rooted),
                    (server, 'default_state_dir', forbidden), (server, 'private_directory', private),
                    (server, 'cached_status', cached)]
                for owner, name, value in replacements:
                    stack.enter_context(patch.object(owner, name, new=value))
                yield
        finally:
            _active = None
