"""Windows process and connection readings for the lanes (ctypes, no dependencies).

* Which process listens on each lane port, with its CPU share and working set: the deep lane runs on
  the CPU, so this is the only load figure it has.
* Which local processes hold a connection to a lane right now (names and ids only, never arguments).

Everything here is read-only. A connection is evidence that a client is talking to a lane at the
sample moment; it is not proof that the client is generating.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import socket
import struct
import threading
import time
from typing import Any

IS_WINDOWS = os.name == "nt"
AF_INET = 2
TCP_TABLE_OWNER_PID_ALL = 5
MIB_TCP_STATE_LISTEN = 2
MIB_TCP_STATE_ESTAB = 5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
NAME = re.compile(r"[A-Za-z0-9 ._+()-]{1,64}\Z")

# Process image name (lower case, without .exe) -> dashboard client id. Anything else keeps its name only.
CLIENT_BY_PROCESS = {"codex": "codex", "opencode": "opencode", "opencode-cli": "opencode", "claude": "claude",
                     "cursor": "cursor", "grok": "grok"}


class _Row(ctypes.Structure):
    _fields_ = [("state", ctypes.c_uint32), ("localAddr", ctypes.c_uint32), ("localPort", ctypes.c_uint32),
                ("remoteAddr", ctypes.c_uint32), ("remotePort", ctypes.c_uint32), ("pid", ctypes.c_uint32)]


def _port(raw: int) -> int:
    return socket.ntohs(raw & 0xFFFF)


def _addr(raw: int) -> str:
    return socket.inet_ntoa(struct.pack("<I", raw))


def tcp_table() -> list[dict[str, Any]]:
    """IPv4 TCP connections with owning process ids (GetExtendedTcpTable)."""
    if not IS_WINDOWS:
        raise OSError("TCP table is Windows-only")
    iphlpapi = ctypes.windll.iphlpapi  # type: ignore[attr-defined]
    size = ctypes.c_ulong(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    for _ in range(3):  # the table can grow between the size query and the read
        buffer = ctypes.create_string_buffer(size.value + 4096)
        size = ctypes.c_ulong(len(buffer))
        result = iphlpapi.GetExtendedTcpTable(buffer, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
        if result == 0:
            break
        if result != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise OSError(f"GetExtendedTcpTable failed ({result})")
    else:
        raise OSError("TCP table kept growing")
    count = struct.unpack_from("<I", buffer, 0)[0]
    rows = (_Row * count).from_buffer_copy(buffer, 4)
    return [{"state": r.state, "local": _addr(r.localAddr), "localPort": _port(r.localPort),
             "remote": _addr(r.remoteAddr), "remotePort": _port(r.remotePort), "pid": r.pid} for r in rows]


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


def _filetime(value: _FileTime) -> int:
    return (value.high << 32) | value.low


class _Counters(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def process_info(pid: int) -> dict[str, Any] | None:
    """Image name, creation time, CPU time (100 ns units) and working set of one process; None if unreadable."""
    if not IS_WINDOWS or pid <= 4:
        return None
    kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    handle = ctypes.c_void_p(handle)
    try:
        size = ctypes.c_ulong(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        name = Path(buffer.value).name if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else None
        created, exited, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
        times = kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user))
        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        memory = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        return {"pid": pid, "name": name if name and NAME.match(name) else "unknown",
                "created": _filetime(created) if times else None,
                "cpuTime": _filetime(kernel) + _filetime(user) if times else None,
                "workingSetBytes": int(counters.WorkingSetSize) if memory else None}
    finally:
        kernel32.CloseHandle(handle)


def client_for(name: str) -> str | None:
    stem = name.lower().removesuffix(".exe")
    return CLIENT_BY_PROCESS.get(stem)


class LaneProcesses:
    """Per-lane server process and callers, with CPU share from the change in CPU time between samples.

    CPU share is of the whole machine (100% = every logical CPU busy). A process identity is
    (pid, creation time), so a restarted lane never inherits the previous process's counters.
    """

    def __init__(self, table_fn=tcp_table, info_fn=process_info, clock=time.monotonic, cpus: int | None = None,
                 self_pid: int | None = None):
        self.table_fn, self.info_fn, self.clock = table_fn, info_fn, clock
        self.cpus = cpus or os.cpu_count() or 1
        self.self_pid = os.getpid() if self_pid is None else self_pid
        self._previous: dict[tuple[int, int | None], tuple[float, int]] = {}
        self._first_seen: dict[tuple[int, int], float] = {}
        self._lock = threading.Lock()

    def _cpu_percent(self, info: dict[str, Any], now: float) -> float | None:
        key = (info["pid"], info["created"])
        cpu = info["cpuTime"]
        before = self._previous.get(key)
        if isinstance(cpu, int):
            self._previous[key] = (now, cpu)
        if before is None or not isinstance(cpu, int):
            return None
        wall = now - before[0]
        if wall <= 0.2:
            return None
        share = (cpu - before[1]) / 1e7 / wall / self.cpus * 100
        return round(max(0.0, min(100.0, share)), 1)

    def sample(self, ports: dict[int, str]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        """({lane_id: server process}, [callers], source) for lane ports {port: lane_id}."""
        now = self.clock()
        try:
            rows = self.table_fn()
        except Exception as error:  # noqa: BLE001
            return {}, [], {"id": "lane-processes", "label": "Lane processes", "state": "unavailable", "ageSeconds": None,
                            "detail": f"Windows TCP table unreadable ({type(error).__name__})"}
        servers: dict[str, Any] = {}
        server_pids: set[int] = set()
        with self._lock:
            for row in rows:
                lane = ports.get(row["localPort"])
                if lane and row["state"] == MIB_TCP_STATE_LISTEN and lane not in servers:
                    info = self.info_fn(row["pid"])
                    server_pids.add(row["pid"])
                    servers[lane] = ({"pid": row["pid"], "name": info["name"], "workingSetBytes": info["workingSetBytes"],
                                      "cpuPercent": self._cpu_percent(info, now)} if info else
                                     {"pid": row["pid"], "name": "unknown", "workingSetBytes": None, "cpuPercent": None})
            callers: dict[tuple[int, str], dict[str, Any]] = {}
            live_keys = set()
            names: dict[int, str] = {}
            for row in rows:
                lane = ports.get(row["remotePort"])
                if (not lane or row["state"] != MIB_TCP_STATE_ESTAB or not row["remote"].startswith("127.")
                        or row["pid"] in server_pids or row["pid"] in (0, self.self_pid)):
                    continue
                if row["pid"] not in names:
                    info = self.info_fn(row["pid"])
                    names[row["pid"]] = info["name"] if info else "unknown"
                key = (row["pid"], lane)
                live_keys.add(key)
                self._first_seen.setdefault(key, now)
                entry = callers.setdefault(key, {"pid": row["pid"], "name": names[row["pid"]], "lane": lane,
                                                 "client": client_for(names[row["pid"]]), "connections": 0,
                                                 "connectedSeconds": round(now - self._first_seen[key], 1)})
                entry["connections"] += 1
            for key in [k for k in self._first_seen if k not in live_keys]:
                del self._first_seen[key]
            if len(self._previous) > 256:  # bounded: old (pid, created) entries are never read again
                self._previous.clear()
        caller_list = sorted(callers.values(), key=lambda c: (c["lane"], c["name"].lower(), c["pid"]))
        detail = (f"{len(servers)} lane server process(es); "
                  + (f"{len(caller_list)} local caller(s): " + ", ".join(f"{c['name']} -> {c['lane']}" for c in caller_list[:6])
                     if caller_list else "no local process is connected to a lane"))
        return servers, caller_list, {"id": "lane-processes", "label": "Lane processes", "state": "live",
                                      "ageSeconds": 0.0, "detail": detail}
