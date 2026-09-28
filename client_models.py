"""Passive, bounded model identities from local client metadata.

Only model IDs and record timestamps leave this module. A recorded model does
not establish that the client is currently generating.
"""
from __future__ import annotations

import datetime as dt
import copy
import ctypes
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
from typing import Any

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}\Z")
_MAX_FILES = 4
_MAX_TAIL = 512 * 1024
# Busy Codex sessions write a turn_context (the model identity) far less often
# than tool records. One deeper bounded scan per session file finds the latest
# one; the result is remembered by file identity because sessions only append.
_MAX_CODEX_DEEP_TAIL = 8 * 1024 * 1024
_CODEX_DEEP: dict[tuple[int, int], list[tuple[str, float, str]]] = {}
_MAX_LINE = 256 * 1024
_CACHE_SECONDS = 2.5
_cache: tuple[float, tuple[list[dict], list[dict]]] | None = None
_SUBAGENT_FEED_MAX = 128 * 1024
_SUBAGENT_EVENTS_MAX = 256
_SUBAGENT_AGE_MAX = 30.0
_SUBAGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")


def normalize_claude_subagent_hook(payload: dict[str, Any], observed_at: dt.datetime) -> dict[str, Any]:
    """Reduce a documented Claude hook payload to identity and lifecycle only.

    A future hook sink may call this before writing a private feed. No prompt,
    transcript path, tool input, or assistant response is retained.
    """
    event = {"SubagentStart": "start", "SubagentStop": "terminal"}.get(payload.get("hook_event_name"))
    owner, agent, agent_type = payload.get("session_id"), payload.get("agent_id"), payload.get("agent_type", "")
    if (event is None or not isinstance(owner, str) or not _SUBAGENT_ID.fullmatch(owner)
            or not isinstance(agent, str) or not _SUBAGENT_ID.fullmatch(agent)
            or not isinstance(agent_type, str) or len(agent_type) > 80
            or observed_at.tzinfo is None):
        raise ValueError("invalid Claude subagent hook identity or event")
    return {"schemaVersion": 1, "client": "claude-code", "event": event,
            "observedAt": observed_at.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "ownerSessionId": owner, "agentId": agent, "agentType": agent_type}


def _subagent_unknown(reason: str, *, age: float | None = None,
                      complete: bool = False, owner: str | None = None) -> dict[str, Any]:
    return {"state": "unknown", "active": None, "observedOpenCount": None,
            "ownerSessionId": owner, "sampledAt": None, "ageSeconds": age,
            "source": "claude-code-hooks-v1", "complete": complete, "reason": reason}


def _read_subagent_feed(path: Path) -> list[dict[str, Any]]:
    """Read a complete, private, bounded normalized JSONL feed; reject races."""
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size > _SUBAGENT_FEED_MAX or before.st_mode & 0o077
            or (os.name != "nt" and before.st_uid != os.getuid())
            or _is_windows_reparse_point(path)):
        raise ValueError("untrusted subagent feed")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("subagent feed changed")
        raw = os.read(fd, _SUBAGENT_FEED_MAX + 1)
        after = os.fstat(fd)
        current = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if len(raw) > _SUBAGENT_FEED_MAX or not identity(before) == identity(after) == identity(current):
            raise ValueError("subagent feed changed")
    finally:
        os.close(fd)
    lines = raw.splitlines()
    if len(lines) > _SUBAGENT_EVENTS_MAX:
        raise ValueError("too many subagent events")
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate event field")
            result[key] = value
        return result
    records = [json.loads(line, object_pairs_hook=unique_pairs) for line in lines]
    if any(not isinstance(row, dict) for row in records):
        raise ValueError("invalid subagent event")
    return records


def collect_claude_subagents(now: float, *, feed_path: Path | None = None,
                             owner_session_id: str | None = None,
                             source_complete: bool = False) -> dict[str, Any]:
    """Summarize exact hook events for one owner; never infer life from recency.

    Completeness is an explicit external attestation, not inferred from a file.
    An unmatched start has no supported child process or crash witness, so its
    active count stays unknown even with a fresh, complete feed.
    """
    if feed_path is None or owner_session_id is None:
        return _subagent_unknown("unconfigured")
    if not isinstance(owner_session_id, str) or not _SUBAGENT_ID.fullmatch(owner_session_id):
        return _subagent_unknown("invalid-owner")
    if not source_complete:
        return _subagent_unknown("partial-source", owner=owner_session_id)
    try:
        records = _read_subagent_feed(feed_path)
    except (OSError, ValueError, TypeError, UnicodeError):
        return _subagent_unknown("unavailable-or-invalid-feed", owner=owner_session_id)
    relevant = []
    for record in records:
        if (set(record) != {"schemaVersion", "client", "event", "observedAt",
                            "ownerSessionId", "agentId", "agentType"}
                or record.get("schemaVersion") != 1 or record.get("client") != "claude-code"
                or record.get("event") not in ("start", "terminal")
                or not isinstance(record.get("ownerSessionId"), str)
                or not _SUBAGENT_ID.fullmatch(record["ownerSessionId"])
                or not isinstance(record.get("agentId"), str)
                or not _SUBAGENT_ID.fullmatch(record["agentId"])
                or not isinstance(record.get("agentType"), str)
                or len(record["agentType"]) > 80
                or not isinstance(record.get("observedAt"), str)):
            return _subagent_unknown("malformed-event", owner=owner_session_id)
        try:
            clock = dt.datetime.fromisoformat(record["observedAt"].replace("Z", "+00:00"))
            when = clock.timestamp() if clock.tzinfo is not None else None
        except (ValueError, OverflowError):
            when = None
        if when is None or not 946684800 <= when <= 4102444800:
            return _subagent_unknown("malformed-event", owner=owner_session_id)
        if record["ownerSessionId"] == owner_session_id:
            relevant.append((when, record))
    if not relevant:
        return _subagent_unknown("missing-events", complete=True, owner=owner_session_id)
    latest = max(when for when, _ in relevant)
    age = round(now - latest, 3)
    if age < 0:
        return _subagent_unknown("clock-skew", age=age, complete=True, owner=owner_session_id)
    if age > _SUBAGENT_AGE_MAX:
        return _subagent_unknown("stale-source", age=age, complete=True, owner=owner_session_id)
    open_agents: set[str] = set()
    for _, record in sorted(relevant, key=lambda item: item[0]):
        if record["event"] == "start":
            if record["agentId"] in open_agents:
                return _subagent_unknown("inconsistent-events", age=age,
                                         complete=True, owner=owner_session_id)
            open_agents.add(record["agentId"])
        else:
            if record["agentId"] not in open_agents:
                return _subagent_unknown("inconsistent-events", age=age,
                                         complete=True, owner=owner_session_id)
            open_agents.discard(record["agentId"])
    return {"state": "unknown" if open_agents else "known",
            "active": None if open_agents else 0, "observedOpenCount": len(open_agents),
            "ownerSessionId": owner_session_id, "sampledAt": now, "ageSeconds": age,
            "source": "claude-code-hooks-v1", "complete": True,
            "reason": "unmatched-start-no-child-process-witness" if open_agents else None}


def _is_windows_reparse_point(path: Path) -> bool:
    """Reject every Windows reparse point, including junctions on Python 3.10."""
    if os.name != "nt":
        return False
    get_attributes = ctypes.WinDLL("kernel32", use_last_error=True).GetFileAttributesW
    get_attributes.argtypes = (ctypes.c_wchar_p,)
    get_attributes.restype = ctypes.c_ulong
    attributes = get_attributes(str(path))
    if attributes == 0xFFFFFFFF:
        raise OSError(ctypes.get_last_error(), "file attributes unavailable", str(path))
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _roots() -> dict[str, Path]:
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    if os.name == "nt" and "XDG_DATA_HOME" not in os.environ:
        data = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
    if os.name == "nt" and "XDG_CONFIG_HOME" not in os.environ:
        config = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    roots = {
        "codex": home / ".codex/sessions",
        "claude": home / ".claude/projects",
        "opencode": data / "opencode/opencode.db",
    }
    # Cursor's local state database is discoverable, but its current schema
    # stores conversation/agent data in opaque blobs and does not expose a
    # supported model-identity record. Keep the client visible as unknown
    # until a bounded, identity-only source is available.
    roots["cursor"] = (home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
                       if sys.platform == "darwin"
                       else config / "Cursor/User/globalStorage/state.vscdb")
    return roots


def _safe(path: Path, root: Path, *, directory: bool = False) -> bool:
    """Reject links, foreign ownership and writable peers along the path."""
    try:
        path.relative_to(root)
        parts = [root, *list(path.relative_to(root).parts)]
        current = Path(parts[0])
        for part in [None, *parts[1:]]:
            if part is not None:
                current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or _is_windows_reparse_point(current):
                return False
            if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
                return False
        info = path.lstat()
        if directory:
            return stat.S_ISDIR(info.st_mode)
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    except (OSError, ValueError):
        return False


def _model(value: Any) -> str | None:
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


def _timestamp(value: Any) -> float | None:
    try:
        if isinstance(value, str):
            result = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        elif type(value) in (float, int):
            result = float(value)
            if result > 10**11:
                result /= 1000
        else:
            return None
        return result if 946684800 <= result <= 4102444800 else None
    except (ValueError, OverflowError):
        return None


def _recent_files(root: Path, *, codex: bool, deadline: float) -> list[Path]:
    if not _safe(root, root, directory=True):
        return []
    candidates: list[tuple[float, Path]] = []
    if codex:
        dates = [dt.date.today() - dt.timedelta(days=i) for i in range(4)]
        directories = [root / d.strftime("%Y/%m/%d") for d in dates]
    else:
        directories = []
        try:
            with os.scandir(root) as entries:
                for i, entry in enumerate(entries):
                    if i >= 128 or time.monotonic() >= deadline:
                        break
                    directory = Path(entry.path)
                    if _safe(directory, root, directory=True):
                        directories.append(directory)
        except OSError:
            return []
    for directory in directories:
        if time.monotonic() >= deadline:
            break
        # Codex day directories need not be contiguous. A missing current day
        # must not hide yesterday's otherwise valid session records.
        if not _safe(directory, root, directory=True):
            continue
        try:
            with os.scandir(directory) as entries:
                for i, entry in enumerate(entries):
                    if i >= 256 or time.monotonic() >= deadline:
                        break
                    if not entry.name.endswith(".jsonl"):
                        continue
                    path = Path(entry.path)
                    if _safe(path, root):
                        candidates.append((entry.stat(follow_symlinks=False).st_mtime, path))
        except OSError:
            continue
    candidates.sort(reverse=True)
    return [path for _, path in candidates[:_MAX_FILES]]


def _tail_records(path: Path, root: Path, limit: int = _MAX_TAIL) -> list[dict[str, Any]]:
    if not _safe(path, root):
        return []
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if ((info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            return []
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
            return []
        size = info.st_size
        os.lseek(fd, max(0, size - limit), os.SEEK_SET)
        chunks, total = [], 0
        while total < limit:
            chunk = os.read(fd, min(1024 * 1024, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    lines = raw.split(b"\n")
    if size > limit:
        lines = lines[1:]
    records = []
    for line in lines:
        if 0 < len(line) <= _MAX_LINE:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    records.append(value)
            except (UnicodeError, ValueError):
                continue
    return records


def _file_observations(root: Path, client: str, deadline: float) -> list[tuple[str, float, str]]:
    observations = []
    for path in _recent_files(root, codex=client == "codex", deadline=deadline):
        if time.monotonic() >= deadline:
            break
        try:
            records = _tail_records(path, root)
        except OSError:
            continue
        if client == "codex" and not any(r.get("type") == "turn_context" for r in records):
            try:
                info = path.lstat()
                key = (info.st_dev, info.st_ino)
                if key not in _CODEX_DEEP and time.monotonic() < deadline:
                    deep = _codex_turn_contexts(_tail_records(path, root, _MAX_CODEX_DEEP_TAIL))
                    _CODEX_DEEP[key] = deep[-1:]
                observations.extend(_CODEX_DEEP.get(key, []))
            except OSError:
                pass
            continue
        for record in records:
            if client == "codex":
                payload = record.get("payload")
                if record.get("type") != "turn_context" or not isinstance(payload, dict):
                    continue
                model = _model(payload.get("model"))
                source = "codex-turn-context"
            else:
                message = record.get("message")
                if record.get("type") != "assistant" or not isinstance(message, dict):
                    continue
                model = _model(message.get("model"))
                source = "claude-assistant-record"
            when = _timestamp(record.get("timestamp"))
            if model and when is not None:
                observations.append((model, when, source))
    return observations


def _codex_turn_contexts(records: list[dict[str, Any]]) -> list[tuple[str, float, str]]:
    found = []
    for record in records:
        payload = record.get("payload")
        if record.get("type") != "turn_context" or not isinstance(payload, dict):
            continue
        model = _model(payload.get("model"))
        when = _timestamp(record.get("timestamp"))
        if model and when is not None:
            found.append((model, when, "codex-turn-context"))
    return found


def _opencode_observations(path: Path, deadline: float) -> list[tuple[str, float, str, str]]:
    root = path.parent
    if not _safe(root, root, directory=True) or not _safe(path, root):
        return []
    before = path.lstat()
    identity = (before.st_dev, before.st_ino)
    if before.st_size > 4 * 1024**3:
        return []
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
    try:
        # SQLite must open by pathname so a live read-only database can use its
        # adjacent WAL. Revalidate immediately after open and after the bounded
        # queries. This fails closed on ordinary rename/link swaps. A hostile
        # same-user process could theoretically swap and restore the path wholly
        # within one SQLite operation; Python's sqlite3 API exposes no database
        # descriptor with which to eliminate that residual cross-platform race.
        def same_file() -> bool:
            if not _safe(path, root):
                return False
            current = path.lstat()
            return (current.st_dev, current.st_ino) == identity

        if not same_file():
            raise OSError("OpenCode database changed while opening")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=100")
        connection.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
        def observed_message(raw: Any, created: Any) -> tuple[str, float, str, str] | None:
            if not isinstance(raw, str) or len(raw) > _MAX_LINE:
                return None
            try:
                data = json.loads(raw)
            except ValueError:
                return None
            if not isinstance(data, dict) or data.get("role") != "assistant":
                return None
            model = _model(data.get("modelID"))
            clock = data.get("time")
            when = _timestamp(clock.get("created") if isinstance(clock, dict) else None)
            when = when or _timestamp(created)
            return (model, when, "opencode-assistant-record", "observed") if model and when else None

        found = []
        # Recent messages can belong to an old session resumed today. A short
        # rowid walk catches them without scanning the full unindexed store.
        for raw, created in connection.execute(
                "SELECT data, time_created FROM message ORDER BY rowid DESC LIMIT 64").fetchall():
            if time.monotonic() >= deadline:
                break
            item = observed_message(raw, created)
            if item:
                found.append(item)
        # Rowid is cheap to walk backwards. An older resumed session can be
        # missed by this fallback, so the message walk above runs first.
        sessions = connection.execute(
            "SELECT id, model, time_updated FROM session ORDER BY rowid DESC LIMIT 32"
        ).fetchall()
        sessions.sort(key=lambda row: row[2] or 0, reverse=True)
        for session_id, configured, updated in sessions[:8]:
            if time.monotonic() >= deadline:
                break
            rows = connection.execute(
                "SELECT data, time_created FROM message WHERE session_id=? "
                "ORDER BY time_created DESC LIMIT 12", (session_id,)
            ).fetchall()
            for raw, created in rows:
                item = observed_message(raw, created)
                if item:
                    found.append(item)
            if isinstance(configured, str) and len(configured) <= 4096:
                try:
                    choice = json.loads(configured)
                    model = _model(choice.get("id")) if isinstance(choice, dict) else None
                except ValueError:
                    model = None
                when = _timestamp(updated)
                if model and when is not None:
                    found.append((model, when, "opencode-session-choice", "configured"))
        if not same_file():
            raise OSError("OpenCode database changed while reading")
        return found
    finally:
        connection.close()


def _project(client: str, observations: list[tuple], now: float) -> tuple[dict, dict]:
    labels = {"codex": "Codex", "claude": "Claude", "opencode": "OpenCode",
              "cursor": "Cursor", "grok": "Grok"}
    eligible = [item for item in observations if item[1] <= now]
    observed = [item for item in eligible if len(item) == 3 or item[3] == "observed"]
    selected = observed if observed else eligible
    selected.sort(key=lambda item: item[1], reverse=True)
    models = []
    seen = set()
    for item in selected:
        model, when, source = item[:3]
        # A future record is clock-skewed evidence, not a just-sampled model.
        if model in seen or when > now:
            continue
        seen.add(model)
        models.append({"id": model, "modelState": "observed" if item in observed else "configured",
                       "observedAt": when,
                       "ageSeconds": round(max(0, now - when), 3), "activity": "unknown", "source": source})
        if len(models) == 3:
            break
    top = models[0] if models else None
    state = top["modelState"] if top else "unknown"
    detail = ("Recorded model identity; current activity unknown" if state == "observed" else
              "Saved session choice; current activity unknown" if state == "configured" else
              "No bounded model metadata available")
    row = {"id": client, "label": labels[client], "model": top["id"] if top else None,
           "modelState": state, "observedAt": top["observedAt"] if top else None,
           "ageSeconds": top["ageSeconds"] if top else None, "activity": "unknown",
           "source": top["source"] if top else None, "detail": detail, "models": models}
    source = {"id": f"{client}-metadata", "label": f"{labels[client]} metadata",
              "state": "recorded" if top else "unavailable",
              "ageSeconds": top["ageSeconds"] if top else None,
              "detail": detail}
    return row, source


def collect_clients(now: float | None = None, *, roots: dict[str, Path] | None = None,
                    subagent_feed_path: Path | None = None,
                    subagent_owner_session_id: str | None = None,
                    subagent_source_complete: bool = False) -> tuple[list[dict], list[dict]]:
    """Return client rows and source rows; never infer live activity.

    ``roots`` supports isolated fixtures. Default roots are cached briefly so
    a one-second monitor tick does not repeatedly read large metadata stores.
    """
    global _cache
    sampled = time.time() if now is None else now
    if (roots is None and subagent_feed_path is None and _cache
            and time.monotonic() - _cache[0] < _CACHE_SECONDS):
        clients, sources = copy.deepcopy(_cache[1])
        if not any(model["observedAt"] > sampled for row in clients for model in row["models"]):
            for row, source in zip(clients, sources):
                for model in row["models"]:
                    model["ageSeconds"] = round(sampled - model["observedAt"], 3)
                if row["models"]:
                    row["ageSeconds"] = row["models"][0]["ageSeconds"]
                    source["ageSeconds"] = row["ageSeconds"]
                    source["state"] = "recorded"
            return clients, sources
    paths = _roots() if roots is None else roots
    deadline = time.monotonic() + 0.35
    results = []
    sources = []
    for client in ("claude", "codex", "opencode", "cursor", "grok"):
        try:
            if client == "opencode":
                observations = _opencode_observations(paths[client], deadline)
            elif client in ("cursor", "grok"):
                # No supported local source currently yields only model IDs
                # and timestamps for these clients. Do not mine conversation
                # content or infer activity from editor/app presence.
                observations = []
            else:
                observations = _file_observations(paths[client], client, deadline)
            row, source = _project(client, observations, sampled)
            if client == "cursor":
                cursor_path = paths.get(client)
                root = cursor_path.parent if cursor_path else None
                if cursor_path and root and _safe(root, root, directory=True) and _safe(cursor_path, root):
                    source["detail"] = "Cursor local database found; no supported model identity field. Current activity unknown"
                else:
                    source["detail"] = "No supported bounded Cursor model metadata available; activity unknown"
                row["detail"] = source["detail"]
                source["detail"] = row["detail"]
            elif client == "grok":
                row["detail"] = "No supported bounded local Grok model metadata source; activity unknown"
                source["detail"] = row["detail"]
        except (OSError, sqlite3.Error, ValueError):
            row, source = _project(client, [], sampled)
            source["state"] = "error"
            source["detail"] = "Local metadata unavailable or invalid"
        row["subagents"] = (collect_claude_subagents(
            sampled, feed_path=subagent_feed_path,
            owner_session_id=subagent_owner_session_id,
            source_complete=subagent_source_complete)
            if client == "claude" else _subagent_unknown("unsupported-client"))
        results.append(row)
        sources.append(source)
    if roots is None and subagent_feed_path is None:
        _cache = (time.monotonic(), copy.deepcopy((results, sources)))
    return results, sources
