"""Process-held exclusion for a state folder, without putting files inside it."""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


class StateLockBusy(OSError):
    """Another process holds the state folder's lock."""


def _open_lock(path: Path) -> int:
    if os.name != "nt":
        return os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)

    # Allow purge to unlink the name while still holding the byte-range lock.
    # The usual CRT open denies deletion of its own open file on Windows.
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0xC0000000, 0x7, None, 4, 0x80 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        close(handle)
        raise


class StateLock:
    """Exclusive, non-blocking lock shared by serve and destructive uninstall."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).resolve()
        self.path = self.state_dir.with_name(self.state_dir.name + ".agiw-lock")
        self.fd = None

    def acquire(self):
        if self.fd is not None:
            raise RuntimeError("state lock is already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = _open_lock(self.path)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise OSError("the state lock is not a regular file")
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Purge may have unlinked a previously opened inode. Never admit a
            # process with a lock that no longer protects the named lock file.
            held, named = os.fstat(fd), self.path.stat()
            if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                raise OSError("the state lock file changed; retry the command")
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise StateLockBusy("the state folder lock is held") from exc
            raise
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def remove_if_state_gone(self):
        """Remove the name under the lock, only after purge removed the folder."""
        if self.fd is None:
            raise RuntimeError("state lock is not held")
        try:
            self.state_dir.lstat()
        except FileNotFoundError:
            held, named = os.fstat(self.fd), self.path.stat()
            if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                raise OSError("the state lock file changed during purge")
            self.path.unlink()

    def release(self):
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()
