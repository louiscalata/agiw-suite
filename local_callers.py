"""Which local processes are connected to the verified Mac model server.

Only process names and ids are kept, never arguments. lsof runs only while a
local model is busy or has queued requests, and at most every 2 s per listener.
"""
from __future__ import annotations

import time
import re
from ipaddress import ip_address
from typing import Any

_LSOF = "/usr/sbin/lsof"
_MIN_INTERVAL = 2.0
_PROBE_BUDGET = 0.5
_MAX_OUTPUT = 1 << 20


def _endpoint(value: str) -> tuple[str, int] | None:
    try:
        address, port = value.rsplit(":", 1)
        if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
            return None
        return str(ip_address(address.strip("[]"))), int(port)
    except ValueError:
        return None


def parse(output: str, server_pids: set[int], port: int, *, wildcard=False) -> list[dict[str, Any]] | None:
    """Pair the verified server's accepted sockets with visible local client ends."""
    accepted, clients = set(), {}
    local_addresses = set()
    pid = name = None
    for line in output.split("\n"):
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            pid = int(value) if value.isascii() and value.isdigit() else None
            if pid is None or pid <= 0:
                return None
            name = None
        elif tag == "c":
            name = value if 1 <= len(value) <= 64 and value.isprintable() and value.strip() else "unknown"
        elif tag == "n":
            if pid is None:
                return None
            ends = value.split("->")
            endpoints = [_endpoint(end) for end in ends]
            if len(endpoints) != 2 or None in endpoints:
                return None
            local, peer = endpoints
            local_addresses.add(local[0])
            if pid in server_pids and local[1] == port:
                accepted.add((peer, local))
            elif pid not in server_pids and peer[1] == port:
                key = (local, peer)
                clients.setdefault(key, {})[pid] = name or "unknown"
    # Either missing half can mean permissions, a partial listing or a socket race.
    local_clients = {key for key in clients
                     if wildcard or ip_address(key[1][0]).is_loopback or key[1][0] in local_addresses}
    if any(len(clients.get(key, {})) != 1 for key in accepted) or local_clients - accepted:
        return None
    callers: dict[int, dict[str, Any]] = {}
    for key in accepted:
        pid, name = next(iter(clients[key].items()))
        entry = callers.setdefault(pid, {"pid": pid, "name": name, "connections": 0})
        entry["connections"] += 1
    return sorted(callers.values(), key=lambda entry: (entry["name"].lower(), entry["pid"]))


def _listeners(output: str, port: int) -> set[int]:
    pids, pid = set(), None
    for line in output.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isascii() and line[1:].isdigit() else None
        elif line.startswith("n"):
            endpoint = _endpoint(line[1:])
            matches = line == f"n*:{port}" or (endpoint is not None and endpoint[1] == port
                       and (ip_address(endpoint[0]).is_loopback or ip_address(endpoint[0]).is_unspecified))
            if matches and pid is not None and pid > 0:
                pids.add(pid)
    return pids


class LocalCallers:
    def __init__(self, runner=None, clock=time.monotonic, executable=None):
        from telemetry import _listener_executable
        self._runner, self._clock = runner, clock
        self._executable = executable or _listener_executable
        self._last_run = None
        self._last_identity = None
        self._last: tuple[list[dict[str, Any]] | None, dict[str, Any]] = (None, self._source("unavailable", "Local callers not checked yet"))

    @staticmethod
    def _source(state: str, detail: str) -> dict[str, Any]:
        return {"id": "local-callers", "label": "Local callers", "state": state,
                "ageSeconds": 0.0 if state == "live" else None, "detail": detail}

    def _pids(self, state: str, port: int, deadline: float) -> str:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("local caller budget exhausted")
        args = [_LSOF, "-nP", "-b", "-w", f"-iTCP:{port}", f"-sTCP:{state}", "-a", "-F", "pcn"]
        if self._runner is None:
            from telemetry import _bounded_command
            output = _bounded_command(args, limit=_MAX_OUTPUT, timeout=remaining, ok_codes=(0, 1),
                                      merge_stderr=True)
        else:
            result = self._runner(args, capture_output=True, text=True, timeout=remaining, check=False,
                                  start_new_session=True)
            if (result.returncode not in (0, 1) or result.stderr.strip()
                    or (result.returncode == 1 and result.stdout.strip()) or len(result.stdout) > _MAX_OUTPUT):
                raise ValueError("lsof unavailable")
            output = result.stdout
        if any(line.strip() and line[0] not in 'pcnf' for line in output.split('\n')):
            raise ValueError("lsof diagnostic or partial output")
        # A malformed/partial process envelope cannot establish an empty list.
        if output.strip() and (not output.endswith('\n') or
                any(line.strip() and line[0] not in 'pcnf' for line in output.splitlines())):
            raise ValueError("lsof output incomplete")
        pid = None
        named = False
        for line in output.splitlines():
            if not line.strip():
                continue
            tag, value = line[0], line[1:]
            if tag == 'p':
                if pid is not None and not named:
                    raise ValueError("lsof process incomplete")
                if not re.fullmatch(r'[0-9]{1,10}', value) or not 0 < int(value) < 2**31:
                    raise ValueError("lsof PID invalid")
                pid, named = int(value), False
            elif tag == 'c':
                if pid is None or named or not value:
                    raise ValueError("lsof command without process")
                named = True
            elif tag == 'n':
                if pid is None or not named or not value:
                    raise ValueError("lsof socket without process")
                if state == 'LISTEN' and value != f'*:{port}' and _endpoint(value) is None:
                    raise ValueError("lsof listener endpoint malformed")
        if pid is not None and not named:
            raise ValueError("lsof process incomplete")
        return output

    def sample(self, models: list[dict[str, Any]], *, port: int | None = None,
               listener: tuple[int, str] | None = None,
               activity_known: bool = False) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
        if (type(port) is not int or not 1 <= port <= 65535 or not isinstance(listener, tuple)
                or len(listener) != 2 or type(listener[0]) is not int or listener[0] <= 0
                or not isinstance(listener[1], str) or not listener[1]):
            self._last_run = self._last_identity = None
            self._last = (None, self._source("unavailable", "Direct model-server port and listener identity are not verified"))
            return self._last
        busy = any(row.get("host") == "mac" and (row.get("state") in ("busy", "generating")
                                                 or (type(row.get("queued")) is int and row["queued"] > 0))
                   for row in models)
        if not busy:
            self._last_run = self._last_identity = None
            complete = all(type(row.get("loaded")) is bool for row in models if row.get("host") == "mac")
            source = (self._source("not-checked", "Not sampled; no local model requests observed")
                      if activity_known is True and complete else
                      self._source("unavailable", "Not checked while local model activity is unknown"))
            self._last = (None, source)
            return self._last
        now = self._clock()
        identity = (port, *listener)
        if self._last_identity == identity and self._last_run is not None and now - self._last_run < _MIN_INTERVAL:
            callers, source = self._last
            return callers, {**source, "ageSeconds": max(0.0, now - self._last_run) if source["state"] == "live" else None}
        self._last_run = now
        self._last_identity = identity
        try:
            listener_output = self._pids("LISTEN", port, now + _PROBE_BUDGET)
            listeners = _listeners(listener_output, port)
            if listeners != {listener[0]} or self._executable(listener[0]) != listener[1]:
                self._last = (None, self._source("unavailable", "Model-server listener changed or is ambiguous; local callers unknown"))
                return self._last
            wildcard = any(line in (f'n*:{port}', f'n0.0.0.0:{port}', f'n[::]:{port}')
                           for line in listener_output.splitlines())
            callers = parse(self._pids("ESTABLISHED", port, now + _PROBE_BUDGET), listeners, port, wildcard=wildcard)
            if self._executable(listener[0]) != listener[1]:
                self._last = (None, self._source("unavailable", "Model-server executable changed; local callers unknown"))
            elif callers is None:
                self._last = (None, self._source("unavailable", "Some caller socket ends are not visible; local callers unknown"))
            else:
                self._last = (callers, self._source("live", f"{len(callers)} local process(es) connected to the model server"))
        except Exception:
            self._last = (None, self._source("unavailable", "Could not list local callers or verify listener"))
        return self._last
