"""Contract fixture: a per-run router journal writer, for Monitor tests only.

This is NOT the router.  It is an independent test double written from the Monitor's own
readers (telemetry.py, activity.py) and docs/router-state-contract.md.  It contains no code
from the separately managed router project and makes none of that project's decisions: no
admission policy beyond the two lock modes below, no lanes, no recovery gates, no model
calls.  It writes exactly the files, modes and lock states the contract says the Monitor
reads, so the Monitor can be exercised without the router installed.

It is loaded with runpy and also run by `python -I -B`, so it uses the standard library
only and imports nothing from this repository.

Journal layout written under ``root`` (all directories 0700, all files 0600, one link):

    owner.lock            L0.  Held shared by each run while policy.json says multi,
                          exclusive otherwise.
    locks/<runId>.lock    L1.  Held exclusive by the run from claim to exit.
    active/<runId>.json   The run's record, written by begin and replaced by checkpoint.
    notes/<runId>.json    The run's advisory note.  claim records one (phase admitting) and
                          begin replaces it (phase running), so a run shows the client and
                          host its claim named; note replaces it with what the caller says.
    archive/<runId>.json  Written by finish, which then removes the record and the note.
    checkpoints/          Created empty; the Monitor only requires that it exists.
    policy.json           Written by tests through _atomic.
"""

import fcntl
import itertools
import json
import os
import re
import time
from pathlib import Path

CONTRACT_VERSION = "1.0.0"
NOTE_KIND = "codemode.router.note.v1"
POLICY_KIND = "codemode.router.policy.v1"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
DIRECTORIES = ("archive", "checkpoints", "active", "notes", "locks")
POLL_SECONDS = 0.005
_SERIAL = itertools.count()


class RouterBusy(RuntimeError):
    """The locks a claim needs stayed held past its deadline."""


def _atomic(path, value, limit):
    """Publish ``value`` as private JSON at ``path``: a new inode with one link, never a
    partly written file under the final name."""
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


def _lock_file(path):
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def _try_lock(fd, mode):
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _policy_is_multi(root):
    try:
        value = json.loads((root / "policy.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("kind") == POLICY_KIND and value.get("concurrency") == "multi"


class RouterOwner:
    """One run's hold on the journal.  ``with RouterOwner(root)`` creates the layout; leaving
    the block without ``finish`` releases both locks and leaves the record behind, which is
    what a crashed run looks like."""

    def __init__(self, root):
        self.root = Path(root)
        self._l0 = None
        self._l1 = None
        self._run = None
        self._digest = None
        self._record = None
        self._meta = {}
        self._admission = None

    def __enter__(self):
        _private_directory(self.root)
        for name in DIRECTORIES:
            _private_directory(self.root / name)
        self._l0 = _lock_file(self.root / "owner.lock")
        return self

    def __exit__(self, *_exc):
        for name in ("_l1", "_l0"):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                os.close(fd)  # closing the description releases its flock
        return False

    def claim(self, run_id, digest, until, meta=None):
        """Take L0 (shared under multi, exclusive otherwise) and this run's L1, polling
        without blocking until ``until`` (a time.monotonic() deadline)."""
        if not RUN_ID.match(run_id) or not SHA256.match(digest):
            raise ValueError("invalid run id or input digest")
        if self._l0 is None or self._run is not None:
            raise RuntimeError("claim needs an entered owner with no run")
        shared = _policy_is_multi(self.root)
        l1 = _lock_file(self.root / "locks" / (run_id + ".lock"))
        try:
            while True:
                if _try_lock(self._l0, fcntl.LOCK_SH if shared else fcntl.LOCK_EX):
                    if _try_lock(l1, fcntl.LOCK_EX):
                        break
                    fcntl.flock(self._l0, fcntl.LOCK_UN)
                if time.monotonic() >= until:
                    raise RouterBusy(run_id)
                time.sleep(POLL_SECONDS)
        except BaseException:
            os.close(l1)
            raise
        self._l1 = l1
        self._run, self._digest = run_id, digest
        self._admission = "shared" if shared else "exclusive"
        self._meta = dict(meta or {})
        self.note("admitting", None, "pre-begin", None)

    def _publish(self):
        _atomic(self.root / "active" / (self._run + ".json"), self._record, 3 * 1024 * 1024)

    def begin(self, run_id, digest):
        if (run_id, digest) != (self._run, self._digest) or self._record is not None:
            raise RuntimeError("begin needs the claimed run")
        lock = os.fstat(self._l1)
        self._record = {
            "schemaVersion": 1,
            "runId": run_id,
            "inputSha256": digest,
            "stage": "started",
            "checkpoint": None,
            "startedUnix": time.time(),
            "runLock": {"dev": lock.st_dev, "ino": lock.st_ino},
        }
        self._publish()  # the record first: a run with a record has begun
        self.note("running", None, None, None)

    def checkpoint(self, stage, evidence):
        if self._record is None:
            raise RuntimeError("checkpoint needs a begun run")
        sequence = self._record.get("sequence", 0) + 1
        self._record.update(
            stage=stage,
            sequence=sequence,
            checkpoint={
                "schemaVersion": 1,
                "runId": self._run,
                "inputSha256": self._digest,
                "sequence": sequence,
                "stage": stage,
                "evidence": dict(evidence),
                "recordedUnix": time.time(),
            },
        )
        self._publish()

    def note(self, phase, resource, step, until):
        """The advisory note; ``until`` is a time.monotonic() deadline (None for none)."""
        if self._run is None:
            raise RuntimeError("note needs a claimed run")
        now = time.time()
        _atomic(
            self.root / "notes" / (self._run + ".json"),
            {
                "schemaVersion": 1,
                "kind": NOTE_KIND,
                "runId": self._run,
                "inputSha256": self._digest,
                "operation": self._meta.get("operation"),
                "client": self._meta.get("client"),
                "host": self._meta.get("host"),
                "admission": self._admission,
                "phase": phase,
                "resource": resource,
                "step": step,
                "sinceUnix": now,
                "untilUnix": None if until is None else now + (until - time.monotonic()),
                "pid": os.getpid(),
            },
            4096,
        )

    def finish(self, result, exit_code):
        """Archive first, then clear the record and the note.  The locks stay held until
        the owner exits, and the lock file stays in place."""
        if self._record is None:
            raise RuntimeError("finish needs a begun run")
        _atomic(
            self.root / "archive" / (self._run + ".json"),
            {
                "schemaVersion": 1,
                "runId": self._run,
                "inputSha256": self._digest,
                "result": result,
                "exitCode": exit_code,
                "stage": self._record["stage"],
                "checkpoint": self._record["checkpoint"],
                "finishedUnix": time.time(),
            },
            3 * 1024 * 1024,
        )
        for directory in ("active", "notes"):
            try:
                os.unlink(str(self.root / directory / (self._run + ".json")))
            except FileNotFoundError:
                pass
        self._run = self._digest = self._record = None
