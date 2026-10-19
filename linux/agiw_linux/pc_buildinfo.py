"""Pure Windows-format identity helpers for the vendored parity projection only.

Linux publication keeps its own service identity in collect.py. No file reads.
"""
from typing import Any
import re
SERVICE = "agiw-win-observer"
VERSION = "0.2.0"
COMMIT = re.compile(r"[0-9a-f]{40}\Z")

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
