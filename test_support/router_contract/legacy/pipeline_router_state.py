"""Contract fixture: a single-run (legacy layout) router journal writer, for Monitor tests only.

This is NOT the router.  It is an independent test double written from the Monitor's own
readers (telemetry.py, activity.py) and docs/router-state-contract.md, with no code from the
separately managed router project.  Standard library only: it is loaded with runpy and run
by `python -I -B`.

Journal layout written under ``root`` (directories 0700, files 0600, one link):

    owner.lock      Held exclusive by the one run from entry to exit.
    active.json     The run's record, written by begin and replaced by checkpoint.
    archive/        archive/<runId>.json is written by finish, which then removes active.json.
    checkpoints/    Created empty; the Monitor only requires that it exists.

This layout has no locks/, active/ or notes/ directory, no policy and no install fence, and
this writer has no admission step: entering the owner takes the lock or fails.
"""
import fcntl
import itertools
import json
import os
import re
import time
from pathlib import Path

CONTRACT_VERSION = "1.0.0"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_SERIAL = itertools.count()


class RouterBusy(RuntimeError):
    """Another process holds owner.lock."""


def _atomic(path, value, limit):
    """Publish ``value`` as private JSON at ``path``: a new inode with one link."""
    path = Path(path)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > limit:
        raise ValueError("record larger than %d bytes" % limit)
    staged = path.with_name(".%s.staged-%d-%d" % (path.name, os.getpid(), next(_SERIAL)))
    fd = os.open(str(staged), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(staged), str(path))


def _private_directory(path):
    try:
        os.mkdir(str(path), 0o700)
    except FileExistsError:
        return
    os.chmod(str(path), 0o700)


class RouterOwner:
    """The one run.  Leaving the block without ``finish`` releases owner.lock and leaves
    active.json behind, which is what a crashed run looks like."""

    def __init__(self, root):
        self.root = Path(root)
        self._l0 = None
        self._run = None
        self._digest = None
        self._record = None

    def __enter__(self):
        _private_directory(self.root)
        for name in ("archive", "checkpoints"):
            _private_directory(self.root / name)
        fd = os.open(str(self.root / "owner.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RouterBusy(str(self.root)) from None
        self._l0 = fd
        return self

    def __exit__(self, *_exc):
        fd, self._l0 = self._l0, None
        if fd is not None:
            os.close(fd)              # closing the description releases its flock
        return False

    def _publish(self):
        _atomic(self.root / "active.json", self._record, 3 * 1024 * 1024)

    def begin(self, run_id, digest):
        if not RUN_ID.match(run_id) or not SHA256.match(digest):
            raise ValueError("invalid run id or input digest")
        if self._l0 is None or self._record is not None:
            raise RuntimeError("begin needs an entered owner with no run")
        self._run, self._digest = run_id, digest
        self._record = {"schemaVersion": 1, "runId": run_id, "inputSha256": digest, "stage": "started",
                        "checkpoint": None, "startedUnix": time.time()}
        self._publish()

    def checkpoint(self, stage, evidence):
        if self._record is None:
            raise RuntimeError("checkpoint needs a begun run")
        sequence = self._record.get("sequence", 0) + 1
        self._record.update(stage=stage, sequence=sequence, checkpoint={
            "schemaVersion": 1, "runId": self._run, "inputSha256": self._digest, "sequence": sequence,
            "stage": stage, "evidence": dict(evidence), "recordedUnix": time.time()})
        self._publish()

    def finish(self, result, exit_code):
        """Archive first, then clear active.json.  owner.lock stays held until exit."""
        if self._record is None:
            raise RuntimeError("finish needs a begun run")
        _atomic(self.root / "archive" / (self._run + ".json"), {
            "schemaVersion": 1, "runId": self._run, "inputSha256": self._digest, "result": result,
            "exitCode": exit_code, "stage": self._record["stage"],
            "checkpoint": self._record["checkpoint"], "finishedUnix": time.time()}, 3 * 1024 * 1024)
        try:
            os.unlink(str(self.root / "active.json"))
        except FileNotFoundError:
            pass
        self._run = self._digest = self._record = None
