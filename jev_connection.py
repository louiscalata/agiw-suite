"""Opt-in wrapper for the native, fixed-payload hosted Jev connection check.

The key and HTTPS exchange stay inside the signed native helper. This module
passes only the explicit ``check`` action and accepts a small fixed receipt.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
import re
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


class CredentialState(Enum):
    UNAVAILABLE = auto()
    UNCONFIGURED = auto()
    CONFIGURED = auto()


class ProbeState(Enum):
    NOT_RUN = auto()
    CHECKING = auto()
    FINISHED = auto()


@dataclass(frozen=True)
class Credential:
    state: CredentialState = CredentialState.UNAVAILABLE
    revision: str | None = None


@dataclass(frozen=True)
class Probe:
    state: ProbeState = ProbeState.NOT_RUN
    revision: str | None = None
    result: str | None = None


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
        self._transition_locked("reset")
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

    def _key_status(self) -> Credential:
        # Private metadata-only receipt; the public native `status` is unchanged.
        result = self._helper_command("status-revision")
        if result is None or result.returncode != 0 or len(result.stdout) > _MAX_STATUS:
            return Credential()
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeError, ValueError):
            return Credential()
        if not isinstance(payload, dict) or set(payload) != {"configured", "available", "revision"}:
            return Credential()
        configured = payload.get("configured")
        available = payload.get("available")
        if type(configured) is not bool or type(available) is not bool or (configured and not available):
            return Credential()
        revision = payload["revision"]
        if configured:
            if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9:-]{1,128}", revision):
                return Credential()
            return Credential(CredentialState.CONFIGURED, revision)
        if revision is not None:
            return Credential()
        return Credential(CredentialState.UNCONFIGURED if available else CredentialState.UNAVAILABLE)

    def _transition_locked(self, event: str, *, credential: Credential | None = None,
                           revision: str | None = None, result: str | None = None) -> None:
        """Only writer of association state; callers serialize observations under _lock."""
        if event == "reset":
            self._credential, self._probe = Credential(), Probe()
        elif event == "observe":
            assert credential is not None
            if credential != self._credential:
                self._credential, self._probe = credential, Probe()
        elif event == "start":
            assert self._credential.state is CredentialState.CONFIGURED
            self._probe = Probe(ProbeState.CHECKING, self._credential.revision)
        elif event == "finish":
            if (self._probe.state is ProbeState.CHECKING
                    and self._probe.revision == revision == self._credential.revision):
                self._probe = Probe(ProbeState.FINISHED, revision, result)
        else:
            raise ValueError("Unknown Jev association transition")

    def status(self) -> dict[str, Any]:
        """Return local helper configuration and current state; never reads the key."""
        with self._lock:
            self._transition_locked("observe", credential=self._key_status())
            return self._snapshot_locked()

    def request_check(self) -> dict[str, Any]:
        """Start one explicit asynchronous synthetic probe, or return its current state."""
        with self._lock:
            self._transition_locked("observe", credential=self._key_status())
            if self._credential.state is not CredentialState.CONFIGURED:
                return self._snapshot_locked()
            # An invalidated check still owns the worker until its bounded call ends.
            if self._probe.state is ProbeState.CHECKING or (self._worker and self._worker.is_alive()):
                return self._snapshot_locked()
            self._transition_locked("start")
            revision = self._credential.revision
            try:
                self._worker = threading.Thread(target=self._run_probe, args=(revision,),
                                                name="JevConnectionCheck", daemon=True)
                self._worker.start()
            except RuntimeError:
                self._transition_locked("finish", revision=revision, result="start-failed")
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        """Derive the unchanged public projection from configuration and probe evidence."""
        configured = self._credential.state is CredentialState.CONFIGURED
        if self._credential.state is CredentialState.UNAVAILABLE:
            state, message = "unavailable", _CHECK_STATES["unavailable"]
        elif self._credential.state is CredentialState.UNCONFIGURED:
            state, message = "not-configured", _CHECK_STATES["not-configured"]
        elif self._probe.state is ProbeState.NOT_RUN:
            state, message = "idle", "Connection check has not been run."
        elif self._probe.state is ProbeState.CHECKING:
            state, message = "checking", "Checking the hosted Jev connection."
        elif self._probe.result == "start-failed":
            state, message = "error", "The connection check could not be started."
        else:
            state = self._probe.result
            message = _CHECK_STATES[state]
        return {"configured": configured, "state": state, "message": message}

    def _run_probe(self, revision: str) -> None:
        try:
            with self._lock:
                self._transition_locked("observe", credential=self._key_status())
                if self._probe.state is not ProbeState.CHECKING or self._credential.revision != revision:
                    return
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
        except Exception:
            # Never include helper stderr/stdout or implementation details.
            state = "error"
        with self._lock:
            try:
                credential = self._key_status()
            except Exception:
                credential = Credential()
            self._transition_locked("observe", credential=credential)
            self._transition_locked("finish", revision=revision, result=state)
