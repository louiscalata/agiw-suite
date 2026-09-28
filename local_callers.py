"""Which local processes are talking to the Mac model server (127.0.0.1:1234) right now.

Only process names and ids are kept, never arguments. lsof costs about 80 ms, so it
runs only while a local model is busy or has queued requests, and at most every 2 s.
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Any

_LSOF = "/usr/sbin/lsof"
_PORT = 1234
_NAME = re.compile(r"[A-Za-z0-9 ._+-]{1,40}\Z")
_MIN_INTERVAL = 2.0


def parse(output: str, server_pids: set[int]) -> list[dict[str, Any]]:
    """lsof -F pcn records -> one entry per calling process, the server itself excluded."""
    callers: dict[int, dict[str, Any]] = {}
    pid = name = None
    for line in output.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else None
            name = None
        elif tag == "c":
            name = value if _NAME.match(value) else "unknown"
        elif tag == "n" and pid is not None and pid not in server_pids:
            entry = callers.setdefault(pid, {"pid": pid, "name": name or "unknown", "connections": 0})
            entry["connections"] += 1
    return sorted(callers.values(), key=lambda entry: (entry["name"].lower(), entry["pid"]))


class LocalCallers:
    def __init__(self, runner=subprocess.run, clock=time.monotonic):
        self._runner, self._clock = runner, clock
        self._last_run = None
        self._last: tuple[list[dict[str, Any]] | None, dict[str, Any]] = (None, self._source("idle", "Not checked while local models are idle"))

    @staticmethod
    def _source(state: str, detail: str) -> dict[str, Any]:
        return {"id": "local-callers", "label": "Local callers", "state": state,
                "ageSeconds": 0.0 if state == "live" else None, "detail": detail}

    def _pids(self, state: str) -> str:
        result = self._runner([_LSOF, "-nP", f"-iTCP:{_PORT}", f"-sTCP:{state}", "-F", "pcn"],
                              capture_output=True, text=True, timeout=1.5, check=False)
        # lsof exits 1 when nothing matches; that is an empty answer, not a failure.
        if result.returncode not in (0, 1) or len(result.stdout) > 1 << 20:
            raise ValueError("lsof unavailable")
        return result.stdout

    def sample(self, models: list[dict[str, Any]]) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
        busy = any(row.get("host") == "mac" and (row.get("state") in ("busy", "generating")
                                                 or (isinstance(row.get("queued"), int) and row["queued"] > 0))
                   for row in models)
        if not busy:
            self._last = (None, self._source("idle", "Not checked while local models are idle"))
            return self._last
        now = self._clock()
        if self._last_run is not None and now - self._last_run < _MIN_INTERVAL:
            return self._last
        self._last_run = now
        try:
            listeners = {int(line[1:]) for line in self._pids("LISTEN").splitlines() if line.startswith("p") and line[1:].isdigit()}
            callers = parse(self._pids("ESTABLISHED"), listeners)
            self._last = (callers, self._source("live", f"{len(callers)} local process(es) connected to the model server"))
        except Exception:
            self._last = (None, self._source("unavailable", "Could not list local callers"))
        return self._last
