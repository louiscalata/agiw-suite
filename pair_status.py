"""Read-only NVIDIA PAIR status from PAIR's own files on this Mac.

This replaces the 1.1.x ``pair_connection.py``, which expected an open-source PAIR variant on
127.0.0.1:18444 that no longer exists. NVIDIA PAIR.app keeps its state under
``~/Library/Application Support/Nvidia Corporation/Personal AI Router``. This module:

- reads only the five files in ``ALLOWED`` (never ``cluster/node.key``, the node certificate, the
  ``trusted/`` peer records, identity files or the browser session);
- retains root and child directory descriptors opened with ``O_DIRECTORY | O_NOFOLLOW``, then opens
  allowlisted basenames relative to them with ``O_NOFOLLOW`` and accepts only regular files;
- reads the log as a bounded tail (``TAIL_BYTES``), never the whole file (it rotates near 100 MB);
- opens no socket, starts nothing and writes nothing.

Honesty rules (PLAN C2 and C8):
- ``state`` is ``"up"`` only from fresh evidence that PAIR's own process wrote recently: a log entry
  at most ``LOG_FRESH_SECONDS`` old, or ``workloads-history.json`` rewritten (PAIR does that every
  5 minutes) at most ``HISTORY_FRESH_SECONDS`` ago, after valid bounded history is parsed.
  Anything else is ``"unknown"`` with a reason.
  Files alone never prove PAIR is down, so this module never reports ``"down"``.
- Engine flags are PAIR's claims and appear only under ``pairSays``. They have contradicted LM
  Studio's own status on this Mac (map-mac section 5), so they are hints, not facts.
- Missing or malformed values stay ``None``; nothing unknown is reported as 0.

The output carries no IP addresses, node or cluster UUIDs, certificates or keys.
"""
from __future__ import annotations

import datetime as _dt
import errno
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any

SCHEMA = "agiw.pair-status.v1"
DEFAULT_ROOT = Path.home() / "Library/Application Support/Nvidia Corporation/Personal AI Router"

SETTINGS = "settings.json"
ADMISSION = "cluster/admission.json"
MEMBERS = "cluster/members.json"
WORKLOADS = "workloads-history.json"
LOG = "logs/nvpair.jsonl"
ALLOWED = (SETTINGS, ADMISSION, MEMBERS, WORKLOADS, LOG)
# Never opened, whatever a caller asks for: key material, certificates and identity records.
FORBIDDEN_NAMES = frozenset({"node.key", "node.crt", "identity.json", "node-id.json", ".updaterId"})

MAX_SMALL = 64 * 1024
MAX_WORKLOADS = 2 * 1024 * 1024
TAIL_BYTES = 64 * 1024
LOG_FRESH_SECONDS = 120.0
HISTORY_FRESH_SECONDS = 330.0
NODE_FRESH_SECONDS = 30.0  # PLAN C1: a node observation older than 30 seconds is not current.
FUTURE_TOLERANCE = 5.0
MAX_MEMBERS = 16
MAX_NODES = 16
MAX_ENGINES = 8
MAX_WORKLOAD_ROWS = 10000
MAX_LOADED_NAMES = 16
NOTE = ("PAIR's own engine flags and discovery claims; not verified, and seen to contradict "
        "LM Studio's own status on this Mac")

_TEXT = re.compile(r"[\x20-\x7e]{1,80}\Z")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,31}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z\Z")
_POLLED = re.compile(r"remote engine status polled for ([0-9a-f-]{36})\Z")
_DISCOVERY = "discovery:nodes-changed"
# Plausible epoch seconds: 2020-01-01 to 2100-01-01.
_EPOCH_MIN, _EPOCH_MAX = 1577836800, 4102444800


def _text(value: Any, pattern: re.Pattern[str] = _TEXT) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _int(value: Any, low: int, high: int) -> int | None:
    return value if type(value) is int and low <= value <= high else None


def _bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _epoch_seconds(value: Any) -> float | None:
    """Seconds since the epoch from PAIR's mixed units (discovery uses seconds, workloads milliseconds)."""
    if type(value) is not int:
        return None
    seconds = value / 1000.0 if value > _EPOCH_MAX else float(value)
    return seconds if _EPOCH_MIN <= seconds <= _EPOCH_MAX else None


def _iso_seconds(value: Any) -> float | None:
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        return None
    try:
        parsed = _dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None
    fraction = value[19:-1]
    return parsed.timestamp() + (float(fraction) if fraction else 0.0)


def _raw_age(seen: float | None, now: float) -> float | None:
    """Age in seconds, or None when unknown or implausibly in the future (clock skew is not freshness)."""
    if seen is None or seen - now > FUTURE_TOLERANCE:
        return None
    return max(0.0, now - seen)


def _age(seen: float | None, now: float) -> float | None:
    age = _raw_age(seen, now)
    return round(age, 1) if age is not None else None


def _open_directory(path: Path | str, *, dir_fd: int | None = None) -> tuple[int | None, str]:
    """Retain a plain directory; later path replacement cannot redirect reads through this handle."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags, dir_fd=dir_fd), "ok"
    except FileNotFoundError:
        return None, "missing"
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            # Some platforms report ENOTDIR for O_DIRECTORY | O_NOFOLLOW on a symlink.
            # This check only names the error; the failed open is never retried.
            try:
                info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    return None, "symlink"
            except OSError:
                pass
            return None, "not-regular"
        return None, "unreadable"


def _read(directory_fd: int, rel: str, limit: int, *, tail: bool = False) -> tuple[bytes | None, str, float | None, bool]:
    """(data, status, mtime, truncated) for one allowlisted file. Status is ok, missing, symlink,
    not-regular, too-large or unreadable. Never follows a symlink, in the name or in its directories."""
    if rel not in ALLOWED or Path(rel).name in FORBIDDEN_NAMES:
        raise ValueError("PAIR file is not on the read allowlist")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(Path(rel).name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None, "missing", None, False
    except OSError as error:
        return None, "symlink" if error.errno == errno.ELOOP else "unreadable", None, False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None, "not-regular", None, False
        if tail:
            start = max(0, info.st_size - limit)
            data = os.pread(descriptor, limit, start)
            return data, "ok", info.st_mtime, start > 0
        if info.st_size > limit:
            return None, "too-large", info.st_mtime, False
        data = os.pread(descriptor, limit + 1, 0)
        if len(data) > limit:
            return None, "too-large", info.st_mtime, False
        return data, "ok", info.st_mtime, False
    except OSError:
        return None, "unreadable", None, False
    finally:
        os.close(descriptor)


def _json(data: bytes | None) -> Any:
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _members(value: Any) -> tuple[list[dict[str, Any]] | None, dict[str, str], str]:
    """Complete members, nodeUuid -> name and a reason when the list cannot supply an exact count."""
    if not isinstance(value, list):
        return None, {}, "invalid"
    if len(value) > MAX_MEMBERS:
        return None, {}, "too-many-members"
    members, names = [], {}
    for row in value:
        if not isinstance(row, dict):
            return None, {}, "invalid"
        name = _text(row.get("name"))
        state = _text(row.get("state"), _WORD)
        epoch = _int(row.get("admissionEpoch"), 0, 1 << 31)
        uuid = row.get("nodeUuid")
        if name is None or state is None or epoch is None or not isinstance(uuid, str) or not _UUID.fullmatch(uuid):
            return None, {}, "invalid"
        members.append({"name": name, "state": state, "admissionEpoch": epoch})
        names[uuid] = name
    return members, names, "ok"


def _engine_rows(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    rows = []
    for row in value[:MAX_ENGINES]:
        if isinstance(row, dict) and _text(row.get("engine"), _WORD):
            rows.append({"engine": row["engine"], "installed": _bool(row.get("installed")),
                         "running": _bool(row.get("running")), "healthy": _bool(row.get("healthy")),
                         "port": _int(row.get("port"), 1, 65535)})
    return rows


def _list_len(value: Any) -> int | None:
    return len(value) if isinstance(value, list) else None


def _node(row: dict[str, Any], now: float) -> dict[str, Any]:
    last_seen = _epoch_seconds(row.get("lastSeen"))
    age = _age(last_seen, now)
    reason = ("future-last-seen" if last_seen is not None else "unknown-last-seen") if age is None else (
        "stale-last-seen" if now - last_seen > NODE_FRESH_SECONDS else None)
    models = row.get("modelsByEngine")
    loaded = row.get("loadedByEngine")
    engines = sorted(k for k in models if _WORD.fullmatch(k))[:MAX_ENGINES] if isinstance(models, dict) else None
    model_count = loaded_count = None
    loaded_names: list[str] | None = None
    if isinstance(models, dict) and all(isinstance(v, list) for v in models.values()):
        model_count = sum(len(v) for v in models.values())
    if isinstance(loaded, dict) and all(isinstance(v, list) for v in loaded.values()):
        loaded_count = sum(len(v) for v in loaded.values())
        names = [n for v in loaded.values() for n in v]
        loaded_names = [n for n in names if isinstance(n, str) and _MODEL.fullmatch(n)][:MAX_LOADED_NAMES]
        if len(loaded_names) != len(names):
            loaded_names = None  # a name this module cannot show safely: the list is incomplete
    if reason is not None:
        model_count = loaded_count = loaded_names = None
    return {"name": _text(row.get("name")),
            "lastSeenAgeSeconds": age, "residencyReason": reason,
            "trusted": _bool(row.get("trusted")), "clustered": _bool(row.get("clustered")),
            "pairClaimEngines": engines, "pairClaimModelCount": model_count, "pairClaimLoadedCount": loaded_count,
            "pairClaimLoadedModels": loaded_names}


def _tail(data: bytes | None, truncated: bool, now: float) -> dict[str, Any]:
    """Newest log time, the newest discovery snapshot and PAIR's newest engine report per peer."""
    out: dict[str, Any] = {"lines": 0, "skipped": 0, "newest": None, "discovery": None,
                           "discoveryAt": None, "polls": {}}
    if data is None:
        return out
    lines = data.split(b"\n")
    if truncated:
        lines = lines[1:]  # the first line of a mid-file read is partial
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            out["skipped"] += 1
            continue
        if not isinstance(entry, dict):
            out["skipped"] += 1
            continue
        at = _iso_seconds(entry.get("time"))
        message = entry.get("message")
        if at is None or not isinstance(message, str) or not message.strip():
            out["skipped"] += 1
            continue
        out["lines"] += 1
        if at - now <= FUTURE_TOLERANCE and (out["newest"] is None or at > out["newest"]):
            out["newest"] = at
        payload = entry.get("data")
        if message == _DISCOVERY and isinstance(payload, list):
            out["discovery"], out["discoveryAt"] = payload, at
        elif isinstance(message, str):
            polled = _POLLED.fullmatch(message)
            if polled and isinstance(payload, dict):
                engines = _engine_rows(payload.get("engines"))
                if engines is not None:
                    out["polls"][polled.group(1)] = (at, engines)
    return out


def _workloads(value: Any, names: dict[str, str], now: float) -> dict[str, Any] | None:
    """Validate the observed row shape before history mtime can establish freshness.

    Empty history is valid. Nonempty rows require state, engine and createdAt; state and engine
    accept safe words rather than a fixed vendor enum so new values remain readable.
    """
    if not isinstance(value, list) or len(value) > MAX_WORKLOAD_ROWS:
        return None
    by_state: dict[str, int] = {}
    by_engine: dict[str, int] = {}
    by_node: dict[str, int] = {name: 0 for name in names.values()}
    newest = None
    for row in value:
        if not isinstance(row, dict):
            return None
        state = _text(row.get("state"), _WORD)
        engine = _text(row.get("engine"), _WORD)
        if state is None or engine is None or _epoch_seconds(row.get("createdAt")) is None:
            return None
        by_state[state] = by_state.get(state, 0) + 1
        by_engine[engine] = by_engine.get(engine, 0) + 1
        scheduled = row.get("scheduledOn")
        node = (names.get(scheduled) if isinstance(scheduled, str) else None) or "unidentified node"
        by_node[node] = by_node.get(node, 0) + 1
        for key in ("completedAt", "startedAt", "createdAt"):
            at = _epoch_seconds(row.get(key))
            if at is not None and at - now <= FUTURE_TOLERANCE:
                newest = at if newest is None else max(newest, at)
                break
    return {"total": len(value), "byState": by_state, "byEngine": by_engine, "byNode": by_node,
            "newestAgeSeconds": _age(newest, now)}


def read_pair_files(root: Path | str | None = None) -> tuple[str, dict]:
    """Bounded private file readings and timestamps for reuse without freezing public ages."""
    root = Path(root) if root is not None else DEFAULT_ROOT
    root_fd, root_status = _open_directory(root)
    if root_fd is None:
        return root_status, {}
    raw: dict[str, tuple[bytes | None, str, float | None, bool]] = {}
    directories = {".": (root_fd, "ok")}
    try:
        for name in ("cluster", "logs"):
            directories[name] = _open_directory(name, dir_fd=root_fd)
        for rel, limit in ((SETTINGS, MAX_SMALL), (ADMISSION, MAX_SMALL), (MEMBERS, MAX_SMALL),
                           (WORKLOADS, MAX_WORKLOADS), (LOG, TAIL_BYTES)):
            directory_fd, status = directories[str(Path(rel).parent)]
            raw[rel] = (_read(directory_fd, rel, limit, tail=rel == LOG) if directory_fd is not None
                        else (None, status, None, False))
    finally:
        for descriptor, _ in directories.values():
            if descriptor is not None:
                os.close(descriptor)
    return "ok", raw


def read_pair_status(root: Path | str | None = None, now: float | None = None, *,
                     readings: tuple[str, dict] | None = None) -> dict[str, Any]:
    """Project bounded file readings at now; cached readings never freeze ages or freshness."""
    now = time.time() if now is None else now
    doc: dict[str, Any] = {"schema": SCHEMA, "state": "unknown", "reason": None, "detail": None,
                           "installed": None, "evidence": {"logAgeSeconds": None, "historyAgeSeconds": None},
                           "cluster": {"name": None, "epoch": None, "memberCount": None, "members": None},
                           "nodes": None, "discoveryAgeSeconds": None, "workloads": None,
                           "pairSays": {"note": NOTE, "engines": []}, "files": {},
                           "tail": {"bytes": 0, "lines": 0, "skipped": 0}}
    root_status, raw = read_pair_files(root) if readings is None else readings
    if root_status == "missing":
        doc.update(installed=False, reason="not-found",
                   detail="NVIDIA PAIR's data folder was not found on this Mac; its state is unknown")
        return doc
    if root_status == "unreadable":
        doc.update(reason="unreadable", detail="NVIDIA PAIR's data folder could not be inspected")
        return doc
    if root_status == "reader-failed":
        doc.update(reason="reader-failed", detail="NVIDIA PAIR status reader failed; state unknown")
        return doc
    if root_status != "ok":
        doc.update(reason="not-a-directory", detail="NVIDIA PAIR's data path is not a plain folder; not read")
        return doc
    doc["installed"] = True

    doc["files"] = {rel: raw[rel][1] for rel in ALLOWED}

    settings = _json(raw[SETTINGS][0])
    if isinstance(settings, dict):
        doc["cluster"]["name"] = _text(settings.get("cluster_friendly_name"))
    admission = _json(raw[ADMISSION][0])
    if isinstance(admission, dict):
        doc["cluster"]["epoch"] = _int(admission.get("epoch"), 0, 1 << 31)
    members, names, members_status = _members(_json(raw[MEMBERS][0]))
    if members is not None:
        doc["cluster"]["members"] = members
        doc["cluster"]["memberCount"] = len(members)
    elif raw[MEMBERS][1] == "ok":
        doc["files"][MEMBERS] = members_status
    for rel in (SETTINGS, ADMISSION, MEMBERS, WORKLOADS):
        if raw[rel][1] == "ok" and _json(raw[rel][0]) is None:
            doc["files"][rel] = "invalid"

    workloads_value = _json(raw[WORKLOADS][0])
    doc["workloads"] = _workloads(workloads_value, names, now)
    if raw[WORKLOADS][1] == "ok" and doc["workloads"] is None:
        doc["files"][WORKLOADS] = "invalid"
    history_mtime = raw[WORKLOADS][2] if doc["workloads"] is not None else None
    doc["evidence"]["historyAgeSeconds"] = _age(history_mtime, now)

    log_data, _, _, truncated = raw[LOG]
    tail = _tail(log_data, truncated, now)
    doc["tail"] = {"bytes": len(log_data) if log_data is not None else 0,
                   "lines": tail["lines"], "skipped": tail["skipped"]}
    doc["evidence"]["logAgeSeconds"] = _age(tail["newest"], now)
    if tail["discovery"] is not None:
        doc["nodes"] = [_node(row, now) for row in tail["discovery"][:MAX_NODES] if isinstance(row, dict)]
        doc["discoveryAgeSeconds"] = _age(tail["discoveryAt"], now)
        for row in tail["discovery"][:MAX_NODES]:
            if isinstance(row, dict) and isinstance(row.get("hostUuid"), str) and _text(row.get("name")):
                names.setdefault(row["hostUuid"], row["name"])
    doc["pairSays"]["engines"] = [
        {"node": names.get(uuid, "unidentified peer"), "reportedAgeSeconds": _age(at, now), "engines": engines}
        for uuid, (at, engines) in sorted(tail["polls"].items(), key=lambda item: names.get(item[0], "~"))]

    log_age = _raw_age(tail["newest"], now)
    history_age = _raw_age(history_mtime, now)
    if log_age is not None and log_age <= LOG_FRESH_SECONDS:
        doc.update(state="up", detail=f"PAIR wrote its log {int(log_age)} s ago")
    elif history_age is not None and history_age <= HISTORY_FRESH_SECONDS:
        doc.update(state="up", detail=f"PAIR rewrote its workload history {int(history_age)} s ago")
    elif log_age is None and history_age is None:
        doc.update(reason="no-evidence", detail="No readable PAIR log entry or workload history; state unknown")
    else:
        newest = min(age for age in (log_age, history_age) if age is not None)
        doc.update(reason="stale", detail=f"PAIR's newest write is {int(newest)} s old; state unknown, not down")
    return doc


def source_row(status: dict[str, Any]) -> dict[str, Any]:
    """The snapshot ``sources[]`` row for PAIR. Unknown is "unavailable", never "error" or down."""
    up = status.get("state") == "up"
    ages = [age for age in (status.get("evidence") or {}).values() if isinstance(age, (int, float))]
    return {"id": "nvidia-pair", "label": "NVIDIA PAIR (read from its files)",
            "state": "live" if up else "unavailable",
            "ageSeconds": min(ages) if ages else None,
            "detail": status.get("detail") or "NVIDIA PAIR state unknown"}
