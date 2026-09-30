"""Opt-in wrapper for the native, fixed-payload hosted Jev connection check.

The key and HTTPS exchange stay inside the signed native helper. This module
passes only the explicit ``check`` action and accepts a small fixed receipt.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
from typing import Any

_MAX_STATUS = 1024
_CHECK_TIMEOUT = 35.0
_CHECK_STATES = {
    "connected": "The hosted Jev connection check succeeded.",
    "not-configured": "Add a TypeSafe API key to enable the optional check.",
    "auth-failed": "The TypeSafe API key was rejected.",
    "rate-limited": "The hosted Jev service is rate limiting requests. Try again later.",
    "network-error": "The hosted Jev connection could not be reached.",
    "invalid-response": "The hosted Jev response did not match the expected format.",
    "unavailable": "Secure Jev credential storage is unavailable.",
    "error": "The hosted Jev connection could not be checked.",
}


def _default_helper() -> Path | None:
    """Return the signed bundled helper path when running inside the app bundle."""
    if os.name != "posix" or not hasattr(os, "uname") or os.uname().sysname != "Darwin":
        return None
    # Installed app: Contents/Resources/jev_connection.py and Contents/MacOS/JevKeychain.
    # Development checkouts intentionally have no helper.
    resources = Path(__file__).resolve().parent
    if resources.name != 'Resources' or resources.parent.name != 'Contents':
        return None
    candidate = resources.parent / "MacOS" / "JevKeychain"
    return candidate if candidate.is_file() else None


class JevConnection:
    """Expose a manual connection check without exposing key or provider data."""

    def __init__(self, helper_path: str | os.PathLike[str] | None = None):
        self._helper = Path(helper_path) if helper_path is not None else _default_helper()
        self._lock = threading.Lock()
        self._state = "idle"
        self._message = "Connection check has not been run."
        self._configured = False
        self._worker: threading.Thread | None = None

    def _helper_command(self, command: str, *, timeout: float = 1.5) -> subprocess.CompletedProcess[bytes] | None:
        if self._helper is None or not self._helper.is_absolute() or not self._helper.is_file():
            return None
        try:
            return subprocess.run(
                [str(self._helper), command], capture_output=True, timeout=timeout,
                check=False, stdin=subprocess.DEVNULL, env={'PATH': '/usr/bin:/bin'},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _key_status(self) -> tuple[bool, bool]:
        result = self._helper_command("status")
        if result is None or result.returncode != 0 or len(result.stdout) > _MAX_STATUS:
            return False, False
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeError, ValueError):
            return False, False
        if not isinstance(payload, dict) or set(payload) != {"configured", "available"}:
            return False, False
        configured = payload.get("configured")
        available = payload.get("available")
        if type(configured) is not bool or type(available) is not bool or (configured and not available):
            return False, False
        return configured, available

    def status(self) -> dict[str, Any]:
        """Return local helper configuration and current state; never reads the key."""
        configured, available = self._key_status()
        with self._lock:
            self._configured = configured
            if not available:
                state, message = "unavailable", "Secure Jev credential storage is unavailable."
            elif not configured:
                state, message = "not-configured", _CHECK_STATES["not-configured"]
            else:
                state, message = self._state, self._message
            return {"configured": configured, "state": state, "message": message}

    def request_check(self) -> dict[str, Any]:
        """Start one explicit asynchronous synthetic probe, or return its current state."""
        with self._lock:
            if self._state == "checking":
                return self._snapshot_locked()

        configured, available = self._key_status()
        with self._lock:
            self._configured = configured
            if not available:
                self._state, self._message = "unavailable", "Secure Jev credential storage is unavailable."
                return self._snapshot_locked()
            if not configured:
                self._state, self._message = "not-configured", _CHECK_STATES["not-configured"]
                return self._snapshot_locked()
            # Recheck under lock: concurrent callers must not launch two probes.
            if self._state == "checking":
                return self._snapshot_locked()
            self._state, self._message = "checking", "Checking the hosted Jev connection."
            try:
                self._worker = threading.Thread(target=self._run_probe, name="JevConnectionCheck", daemon=True)
                self._worker.start()
            except RuntimeError:
                self._state, self._message = "error", "The connection check could not be started."
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        return {"configured": self._configured, "state": self._state, "message": self._message}

    def _run_probe(self) -> None:
        try:
            configured, available = self._key_status()
            if not available or not configured:
                state = "unavailable" if not available else "not-configured"
                message = _CHECK_STATES[state]
            else:
                helper = self._helper_command("check", timeout=_CHECK_TIMEOUT)
                if helper is None or helper.returncode != 0 or len(helper.stdout) > _MAX_STATUS:
                    state = "error"
                else:
                    try:
                        receipt = json.loads(helper.stdout.decode("utf-8"))
                    except (UnicodeError, ValueError):
                        receipt = None
                    state = (receipt.get("state") if isinstance(receipt, dict)
                             and set(receipt) == {"state"} else None)
                    if state not in _CHECK_STATES:
                        state = "error"
                message = _CHECK_STATES[state]
        except Exception:
            # Never include helper stderr/stdout or implementation details.
            state, message = "error", _CHECK_STATES["error"]
        with self._lock:
            self._configured = configured if "configured" in locals() else False
            self._state, self._message = state, message
