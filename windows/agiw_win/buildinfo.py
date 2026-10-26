"""The installed build's identity: VERSION plus the commit that install.ps1 records in build.json.

install.ps1 writes ``build.json`` beside the observer (the install folder root). A source checkout has none, so its
commit is unknown and the producer string carries only the version. Nothing here runs git or reads a repository.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Any

from . import SERVICE, VERSION
from .jsonutil import loads_strict

BUILD_FILE_NAME = "build.json"
BUILD_MAX_BYTES = 4096
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
INSTALLED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


def default_build_path() -> Path:
    """<install>/build.json: this module lives in <install>/agiw_win/."""
    return Path(__file__).resolve().parent.parent / BUILD_FILE_NAME


def _read(path: Path) -> Any:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400) or not stat.S_ISREG(info.st_mode):
        raise ValueError("build.json must be a regular file")
    if info.st_size > BUILD_MAX_BYTES:
        raise ValueError("build.json too large")
    with path.open("rb") as stream:
        payload = stream.read(BUILD_MAX_BYTES + 1)
    if len(payload) > BUILD_MAX_BYTES:
        raise ValueError("build.json too large")
    return loads_strict(payload.decode("utf-8-sig"))


def read_build(path: Path | None = None, version: str = VERSION) -> dict[str, Any]:
    """Build identity and provenance. A commit alone does not prove clean, signed source.

    A commit is reported only from a well-formed build.json written for this exact VERSION; a file left behind by
    another version, or a malformed one, gives commit None rather than a misleading identity.
    """
    path = default_build_path() if path is None else path
    base = {"version": version, "commit": None, "dirty": None, "installedAt": None, "provenance": "unverified"}
    try:
        doc = _read(path)
    except FileNotFoundError:
        return {**base, "state": "absent", "detail": "No build.json (source checkout); commit unknown"}
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return {**base, "state": "invalid", "detail": "build.json unreadable or malformed; commit unknown"}
    if not isinstance(doc, dict) or type(doc.get("schemaVersion")) is not int or doc["schemaVersion"] != 1 \
            or doc.get("service") != SERVICE:
        return {**base, "state": "invalid", "detail": "build.json is not an agiw-win-observer build record; commit unknown"}
    recorded = doc.get("version")
    if recorded != version:
        shown = recorded[:32] if isinstance(recorded, str) and recorded.isprintable() else "another version"
        return {**base, "state": "mismatch",
                "detail": f"build.json records {shown}, but the code is {version}; commit unknown"}
    commit = doc.get("commit")
    if commit is not None and not (isinstance(commit, str) and COMMIT.fullmatch(commit)):
        return {**base, "state": "invalid", "detail": "build.json commit is not a full lower-case hash; commit unknown"}
    dirty = doc.get("dirty") if isinstance(doc.get("dirty"), bool) and commit else None
    installed = doc.get("installedAt") if isinstance(doc.get("installedAt"), str) \
        and INSTALLED_AT.fullmatch(doc["installedAt"]) else None
    if commit is None:
        return {**base, "installedAt": installed, "state": "no-commit",
                "detail": "Installed without a git commit (git unavailable at install time)"}
    verified = dirty is False and doc.get("signatureVerified") is True and doc.get("filesMatchHead") is True
    provenance = "signed-head" if verified else "unverified"
    reason = " with uncommitted changes" if dirty is True else "; source provenance unverified"
    return {"version": version, "commit": commit, "dirty": dirty, "installedAt": installed, "state": "recorded",
            "provenance": provenance,
            "detail": f"Installed from {commit[:12]}{'; staged bytes match verified signed commit' if verified else reason}"}


def producer(build: Any, version: Any = None) -> str:
    """Commit suffixes distinguish dirty and unverified installs from verified signed source."""
    build = build if isinstance(build, dict) else {}
    shown = build.get("version") if isinstance(build.get("version"), str) else version
    shown = shown if isinstance(shown, str) and shown.isprintable() and 0 < len(shown) <= 32 else VERSION
    commit = build.get("commit")
    suffix = ""
    if isinstance(commit, str) and COMMIT.fullmatch(commit):
        qualifier = ".dirty" if build.get("dirty") is True else ""
        if build.get("dirty") is not False or build.get("provenance") != "signed-head":
            qualifier += ".unverified"
        suffix = f"+{commit[:12]}{qualifier}"
    return f"{SERVICE} {shown}{suffix}"


def build_public(build: Any) -> dict[str, Any]:
    """The build identity for presence, without local paths or signature material."""
    build = build if isinstance(build, dict) else {}
    commit = build.get("commit") if isinstance(build.get("commit"), str) and COMMIT.fullmatch(build["commit"]) else None
    version = build.get("version") if isinstance(build.get("version"), str) and build["version"].isprintable() else None
    return {"version": version[:32] if version else None, "commit": commit[:12] if commit else None,
            "dirty": build.get("dirty") if commit and isinstance(build.get("dirty"), bool) else None,
            "provenance": "signed-head" if commit and build.get("dirty") is False
            and build.get("provenance") == "signed-head" else "unverified"}
