"""Private, write-ahead operation journal for explicit LM Studio controls.

The journal is a safety latch, not a process supervisor. A record left pending
without owner-recorded cleanup cannot be cleared by process exit or inventory
alone. Provisioning is an explicit clean-install action, never a startup repair.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid


SCHEMA = 1
MAX_BYTES = 4096
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}\Z")
OPERATION_ID = re.compile(r"[a-f0-9]{32}\Z")


class JournalError(Exception):
    pass


def _finite_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JournalError("Duplicate journal key.")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise JournalError("Non-finite journal number.")


def _validate(record):
    if type(record) is not dict or set(record) != {"schema", "generation", "phase", "operation"}:
        raise JournalError("Invalid journal envelope.")
    if type(record["schema"]) is not int or record["schema"] != SCHEMA:
        raise JournalError("Unsupported journal schema.")
    if type(record["generation"]) is not int or not 0 <= record["generation"] < 2**63:
        raise JournalError("Invalid journal generation.")
    phase = record["phase"]
    if phase not in ("ready", "pending", "confirmed") or type(phase) is not str:
        raise JournalError("Invalid journal phase.")
    operation = record["operation"]
    if phase == "ready":
        if operation is not None or record["generation"] != 0:
            raise JournalError("Invalid ready journal.")
        return record
    if type(operation) is not dict or set(operation) != {
        "operationId", "action", "modelId", "modelKey", "startedAt",
        "updatedAt", "finishedAt", "cleanupConfirmed", "observedAt",
    }:
        raise JournalError("Invalid journal operation.")
    if not (type(operation["operationId"]) is str
            and OPERATION_ID.fullmatch(operation["operationId"])):
        raise JournalError("Invalid journal operation ID.")
    if operation["action"] not in ("load", "unload") or type(operation["action"]) is not str:
        raise JournalError("Invalid journal action.")
    for key in ("modelId", "modelKey"):
        value = operation[key]
        if type(value) is not str or not MODEL_ID.fullmatch(value):
            raise JournalError("Invalid journal model identity.")
    if not _finite_time(operation["startedAt"]) or not _finite_time(operation["updatedAt"]):
        raise JournalError("Invalid journal time.")
    if operation["updatedAt"] < operation["startedAt"]:
        raise JournalError("Journal time moved backward.")
    if operation["finishedAt"] is not None and (
            not _finite_time(operation["finishedAt"])
            or operation["finishedAt"] < operation["startedAt"]):
        raise JournalError("Invalid journal finish time.")
    if type(operation["cleanupConfirmed"]) is not bool:
        raise JournalError("Invalid cleanup evidence.")
    observed = operation["observedAt"]
    if observed is not None and (not _finite_time(observed) or observed <= operation["startedAt"]):
        raise JournalError("Invalid observation time.")
    if phase == "confirmed" and (not operation["cleanupConfirmed"] or observed is None):
        raise JournalError("Confirmed journal lacks cleanup or observation evidence.")
    return record


class DurableModelJournal:
    """One journal path, serialized across Monitor processes by a stable lock file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise JournalError("Journal path must be absolute.")
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _check_parent(self):
        try:
            info = self.path.parent.lstat()
        except OSError as exc:
            raise JournalError("Journal directory is unavailable.") from exc
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise JournalError("Journal directory is not private to this user.")

    @staticmethod
    def _check_file(fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise JournalError("Journal file is not a private regular file.")

    @contextmanager
    def _locked(self, *, provision_lock=False):
        self._check_parent()
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        if provision_lock:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(self.lock_path, flags, 0o600)
            self._check_file(fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            # A replaced lock inode would let two owners act concurrently.
            if os.stat(self.lock_path, follow_symlinks=False).st_ino != os.fstat(fd).st_ino:
                raise JournalError("Journal lock identity changed.")
            yield
        except OSError as exc:
            raise JournalError("Journal lock is unavailable.") from exc
        finally:
            if "fd" in locals():
                os.close(fd)

    def _read_locked(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                self._check_file(fd)
                raw = os.read(fd, MAX_BYTES + 1)
            finally:
                os.close(fd)
        except FileNotFoundError as exc:
            raise JournalError("Journal is missing; explicit clean-install provisioning is required.") from exc
        except OSError as exc:
            raise JournalError("Journal cannot be read safely.") from exc
        if len(raw) > MAX_BYTES:
            raise JournalError("Journal exceeds the safety limit.")
        try:
            record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                                parse_constant=_invalid_constant)
        except (UnicodeError, ValueError) as exc:
            raise JournalError("Journal is corrupt.") from exc
        return _validate(record)

    def read(self):
        with self._locked():
            return self._read_locked()

    def _write_locked(self, record):
        _validate(record)
        data = json.dumps(record, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
        if len(data) > MAX_BYTES:
            raise JournalError("Journal record exceeds the safety limit.")
        temp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        fd = None
        replaced = False
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise JournalError("Journal write made no progress.")
                remaining = remaining[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(temp, self.path)
            replaced = True
            parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError as exc:
            raise JournalError("Durable journal write failed.") from exc
        finally:
            if fd is not None:
                os.close(fd)
            if not replaced:
                try:
                    temp.unlink()
                except FileNotFoundError:
                    pass

    def provision_new(self):
        """Explicit clean-install step. Never called by ModelControl startup."""
        # Check before O_CREAT|O_EXCL opens the lock. If a journal already
        # exists but its lock is missing, a rejected provision must not heal
        # that unsafe state and make the old journal readable again.
        self._check_parent()
        try:
            self.path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise JournalError("Existing journal state cannot be inspected safely.") from exc
        else:
            raise JournalError("Existing journal cannot be reprovisioned.")
        with self._locked(provision_lock=True):
            if self.path.exists() or self.path.is_symlink():
                raise JournalError("Existing journal cannot be reprovisioned.")
            self._write_locked({"schema": SCHEMA, "generation": 0,
                                "phase": "ready", "operation": None})

    def begin(self, *, operation_id, action, model_id, model_key, started_at):
        with self._locked():
            before = self._read_locked()
            if before["phase"] not in ("ready", "confirmed"):
                raise JournalError("A previous model operation is unconfirmed.")
            record = {"schema": SCHEMA, "generation": before["generation"] + 1,
                      "phase": "pending", "operation": {
                          "operationId": operation_id, "action": action,
                          "modelId": model_id, "modelKey": model_key,
                          "startedAt": started_at, "updatedAt": started_at,
                          "finishedAt": None, "cleanupConfirmed": False,
                          "observedAt": None}}
            self._write_locked(record)
            return record

    @staticmethod
    def _match(record, *, operation_id, action, model_id, model_key, generation):
        op = record["operation"]
        if (record["phase"] != "pending" or record["generation"] != generation
                or op["operationId"] != operation_id or op["action"] != action
                or op["modelId"] != model_id or op["modelKey"] != model_key):
            raise JournalError("Exact journal operation identity changed.")

    def finish(self, *, operation_id, action, model_id, model_key, generation,
               finished_at, cleanup_confirmed, observed_at=None):
        with self._locked():
            record = self._read_locked()
            self._match(record, operation_id=operation_id, action=action,
                        model_id=model_id, model_key=model_key, generation=generation)
            if not _finite_time(finished_at) or finished_at < record["operation"]["startedAt"]:
                raise JournalError("Invalid finish time.")
            if type(cleanup_confirmed) is not bool:
                raise JournalError("Invalid cleanup evidence.")
            if observed_at is not None and not cleanup_confirmed:
                raise JournalError("Observation cannot confirm without owner cleanup.")
            record["operation"].update(finishedAt=finished_at,
                                       updatedAt=max(finished_at, record["operation"]["updatedAt"]),
                                       cleanupConfirmed=cleanup_confirmed,
                                       observedAt=observed_at)
            if observed_at is not None:
                record["phase"] = "confirmed"
            self._write_locked(record)
            return record

    def confirm_recovered(self, *, operation_id, action, model_id, model_key,
                          generation, observed_at, recovery_started_at):
        with self._locked():
            record = self._read_locked()
            self._match(record, operation_id=operation_id, action=action,
                        model_id=model_id, model_key=model_key, generation=generation)
            op = record["operation"]
            if not op["cleanupConfirmed"] or op["finishedAt"] is None:
                raise JournalError("Original worker cleanup was not durably confirmed.")
            if (not _finite_time(observed_at) or not _finite_time(recovery_started_at)
                    or observed_at <= max(op["updatedAt"], recovery_started_at)):
                raise JournalError("A fresh post-restart observation is required.")
            op["observedAt"] = observed_at
            op["updatedAt"] = max(op["updatedAt"], observed_at)
            record["phase"] = "confirmed"
            self._write_locked(record)
            return record
