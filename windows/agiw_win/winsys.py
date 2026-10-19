"""Windows process and connection readings for the lanes (ctypes, no dependencies).

* Which process listens on each lane port, with its CPU share and working set: the deep lane runs on
  the CPU, so this is the only load figure it has.
* Which local processes hold a connection to a lane right now (names and ids only, never arguments).

Everything here is read-only. A connection is evidence that a client is talking to a lane at the
sample moment; it is not proof that the client is generating.
"""
from __future__ import annotations

import ctypes
import logging
import ipaddress
import math
import ntpath
import os
from pathlib import Path
import re
import socket
import stat
import struct
import threading
import time
from typing import Any

from .jsonutil import loads_strict

IS_WINDOWS = os.name == "nt"
AF_INET = 2
AF_INET6 = 23
LOOPBACK = {"127.0.0.1", "::1"}
ANY = {"0.0.0.0", "::"}
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


class _Row6(ctypes.Structure):
    _fields_ = [("localAddr", ctypes.c_ubyte * 16), ("localScope", ctypes.c_uint32), ("localPort", ctypes.c_uint32),
                ("remoteAddr", ctypes.c_ubyte * 16), ("remoteScope", ctypes.c_uint32), ("remotePort", ctypes.c_uint32),
                ("state", ctypes.c_uint32), ("pid", ctypes.c_uint32)]


def _raw_table(family: int) -> bytes:
    iphlpapi = ctypes.windll.iphlpapi  # type: ignore[attr-defined]
    size = ctypes.c_ulong(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_ALL, 0)
    for _ in range(3):  # the table can grow between the size query and the read
        buffer = ctypes.create_string_buffer(size.value + 4096)
        size = ctypes.c_ulong(len(buffer))
        result = iphlpapi.GetExtendedTcpTable(buffer, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_ALL, 0)
        if result == 0:
            return buffer.raw
        if result != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise OSError(f"GetExtendedTcpTable failed ({result})")
    raise OSError("TCP table kept growing")


def _v6(raw) -> str:
    address = ipaddress.IPv6Address(bytes(raw))
    mapped = address.ipv4_mapped
    return str(mapped) if mapped else str(address)  # IPv4-mapped addresses compare as IPv4


def tcp_table() -> list[dict[str, Any]]:
    """IPv4 and IPv6 TCP connections with owning process ids (GetExtendedTcpTable, both families)."""
    if not IS_WINDOWS:
        raise OSError("TCP table is Windows-only")
    rows = []
    buffer = _raw_table(AF_INET)
    count = struct.unpack_from("<I", buffer, 0)[0]
    for r in (_Row * count).from_buffer_copy(buffer, 4):
        rows.append({"state": r.state, "local": _addr(r.localAddr), "localPort": _port(r.localPort),
                     "remote": _addr(r.remoteAddr), "remotePort": _port(r.remotePort), "pid": r.pid})
    buffer = _raw_table(AF_INET6)
    count = struct.unpack_from("<I", buffer, 0)[0]
    for r in (_Row6 * count).from_buffer_copy(buffer, 4):
        rows.append({"state": r.state, "local": _v6(r.localAddr), "localPort": _port(r.localPort),
                     "remote": _v6(r.remoteAddr), "remotePort": _port(r.remotePort), "pid": r.pid})
    return rows


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


PAIR_IMAGE_MARKERS = ("\\personal ai router\\", "\\program files\\pair\\", "\\program files (x86)\\pair\\")
PAIR_IMAGE_NAMES = ("pair.exe", "nvpair.exe", "nvpair-proxy.exe")


def image_owner(path: Any) -> str | None:
    """'pair' for an NVIDIA PAIR executable (by install folder or image name), else None. The path is only
    classified here; callers keep the image name, never the path."""
    if not isinstance(path, str) or not path:
        return None
    folded = path.replace("/", "\\").casefold()
    name = folded.rsplit("\\", 1)[-1]
    if name in PAIR_IMAGE_NAMES or any(marker in folded for marker in PAIR_IMAGE_MARKERS):
        return "pair"
    return None


def process_info(pid: int) -> dict[str, Any] | None:
    """Image name, owner class, creation time, CPU time (100 ns units) and working set of one process; None if
    unreadable."""
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
        named = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
        name = Path(buffer.value).name if named else None
        owner = image_owner(buffer.value) if named else None
        created, exited, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
        times = kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user))
        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        memory = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        return {"pid": pid, "name": name if name and NAME.match(name) else "unknown", "owner": owner,
                "image": buffer.value if named else None,
                "created": _filetime(created) if times else None,
                "cpuTime": _filetime(kernel) + _filetime(user) if times else None,
                "workingSetBytes": int(counters.WorkingSetSize) if memory else None}
    finally:
        kernel32.CloseHandle(handle)


def client_for(name: str) -> str | None:
    stem = name.lower().removesuffix(".exe")
    return CLIENT_BY_PROCESS.get(stem)


def accepted_lane_identities(entries: list[list[str]]) -> dict[str, dict[str, Any]]:
    """Validate explicit local operator enrollment, never shared configuration or discovered listeners.

    Creation time is the exact Windows FILETIME integer from GetProcessTimes. Replacing a lane requires a
    new enrollment tuple; neither a reused PID nor an executable at the same path renews the acceptance.
    No image paths are included in errors or published snapshots.
    """
    identities = {}
    for lane, raw_pid, raw_created, image in entries:
        if lane not in ("fast", "deep") or lane in identities:
            raise ValueError("each enrolled lane must be fast or deep and appear once")
        if not raw_pid.isascii() or not raw_pid.isdecimal() or not raw_created.isascii() or not raw_created.isdecimal():
            raise ValueError("lane PID and creation time must be positive decimal integers")
        pid, created = int(raw_pid), int(raw_created)
        if not 4 < pid <= 0xFFFFFFFF or not 0 < created <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("lane PID or creation time is out of range")
        drive, tail = ntpath.splitdrive(image)
        if (not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith(("\\", "/"))
                or any(ord(char) < 32 for char in image) or ":" in tail or image_owner(image) == "pair"):
            raise ValueError("lane image must be an absolute local non-PAIR executable path")
        identities[lane] = {"pid": pid, "created": created, "image": ntpath.normpath(image)}
    return identities


def _check_enrollment_permissions(owner: str, entries: list | None, me: str, inside_profile: bool) -> None:
    trusted = {me, "S-1-5-18", "S-1-5-32-544",
               "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}
    if owner not in trusted or entries is None:
        raise PermissionError("enrollment parent ownership or access list is not trusted")
    writes = 0x500d0156 if inside_profile else 0x500d0040
    for sid, mask, inherit_only in entries:
        if (inside_profile or not inherit_only) and mask & writes and sid not in trusted \
                and sid != "S-1-3-4" and not (inherit_only and sid == "S-1-3-0"):
            raise PermissionError("enrollment parent is writable by another account")


def _check_enrollment_parent(handle: int, inside_profile: bool) -> None:
    """Check write permissions on the retained directory, without re-opening its path."""
    import peer_link
    _, _, advapi32, kernel32 = peer_link._win_api()
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    rc = advapi32.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None,
                                 ctypes.byref(descriptor))
    if rc:
        raise PermissionError("enrollment parent access list unreadable")
    try:
        owner_sid = peer_link._win_sid_text(owner.value) if owner.value else ""
        entries = None
        if dacl.value:
            info = (ctypes.c_uint32 * 3)()
            if not advapi32.GetAclInformation(dacl, info, ctypes.sizeof(info), 2):
                raise PermissionError("enrollment parent access list unreadable")
            entries = []
            for index in range(info[0]):
                ace = ctypes.c_void_p()
                if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                    raise PermissionError("enrollment parent access entry unreadable")
                size = ctypes.c_uint16.from_address(ace.value + 2).value
                raw = ctypes.string_at(ace.value, size)
                sid = peer_link._ace_allow_sid(raw)
                if sid is not None:
                    entries.append((peer_link._win_sid_text(ctypes.create_string_buffer(sid, len(sid))),
                                    int.from_bytes(raw[4:8], "little"), bool(raw[1] & 0x08)))
        _check_enrollment_permissions(owner_sid, entries, peer_link._win_current_sid(), inside_profile)
    finally:
        kernel32.LocalFree(descriptor)


def _open_enrollment_relative(parent: int, name: str) -> int:
    from . import probes
    buffer = ctypes.create_unicode_buffer(name)
    size = len(name.encode("utf-16-le"))
    unicode_name = probes._UnicodeString(size, size + 2, ctypes.cast(buffer, ctypes.c_wchar_p))
    attributes = probes._ObjectAttributes(ctypes.sizeof(probes._ObjectAttributes), parent,
                                         ctypes.pointer(unicode_name), 0x40, None, None)
    iosb, handle = probes._IoStatusBlock(), ctypes.c_void_p()
    create = ctypes.WinDLL("ntdll").NtCreateFile
    create.argtypes = (ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong, ctypes.POINTER(probes._ObjectAttributes),
                       ctypes.POINTER(probes._IoStatusBlock), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                       ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong)
    create.restype = ctypes.c_long
    status = create(ctypes.byref(handle), 0x120089, ctypes.byref(attributes), ctypes.byref(iosb),
                    None, 0x80, 1, 1, 0x200060, None, 0)  # read, share READ, OPEN, no reparse, synchronous file
    if status < 0:
        raise probes._nt_error(status, "NtCreateFile enrollment")
    return handle.value


def _read_local_enrollment(path: Path) -> str:
    """Read only: fixed local file, private owner/DACL, retained no-reparse parents and bounded contents.

    Reuses the shipped peer-link SID/DACL verifier, without loading keys or starting its service.
    Directory data-access handles deny deletion while held; the file also denies concurrent writers.
    """
    if not IS_WINDOWS:
        raise OSError("local enrollment requires Windows ownership verification")
    from . import probes
    import peer_link
    import msvcrt

    path = Path(os.path.abspath(path))
    profile = Path(os.path.abspath(Path.home()))
    if profile not in path.parents or "sharedchami" in {part.casefold() for part in path.parts}:
        raise ValueError("enrollment must be in the private local profile")
    drive, _ = ntpath.splitdrive(str(path))
    if not re.fullmatch(r"[A-Za-z]:", drive):
        raise ValueError("local enrollment drive required")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    drive_type = kernel32.GetDriveTypeW
    drive_type.argtypes, drive_type.restype = (ctypes.c_wchar_p,), ctypes.c_uint32
    if drive_type(drive + "\\") != 3:  # DRIVE_FIXED; excludes mapped network and removable drives
        raise ValueError("local fixed enrollment drive required")
    create, close = kernel32.CreateFileW, kernel32.CloseHandle
    create.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
    create.restype = ctypes.c_void_p
    close.argtypes = (ctypes.c_void_p,)
    handles = []
    try:
        for directory in reversed(path.parents):
            handle = create(str(directory), 0x20081, 0x3, None, 3, 0x02200000, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise OSError("enrollment parent unavailable")
            handles.append(handle)
            if probes._handle_is_reparse(kernel32, handle):
                raise ValueError("linked enrollment parent refused")
            _check_enrollment_parent(handle, directory == profile or profile in directory.parents)
        handle = _open_enrollment_relative(handles[-1], path.name)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close(handle)
            raise
        with os.fdopen(fd, "rb") as stream:
            probes._peer_link_opened_path(stream, path)
            peer_link._win_check_private(stream.fileno(), "lane enrollment")
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384:
                raise ValueError("invalid enrollment file")
            raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError("oversized enrollment")
            return raw.decode("utf-8-sig")
    finally:
        for handle in reversed(handles):
            close(handle)


def local_lane_identities(path: Path) -> dict[str, dict[str, Any]]:
    """Accept only the private local operator's exact pins. Any read/schema failure parks every lane."""
    try:
        data = loads_strict(_read_local_enrollment(path))
        if not isinstance(data, dict) or set(data) != {"schema", "lanes"} or type(data["schema"]) is not int \
                or data["schema"] != 1 or not isinstance(data["lanes"], dict) or len(data["lanes"]) > 2:
            return {}
        entries = []
        for lane, pin in data["lanes"].items():
            if not isinstance(pin, dict) or set(pin) != {"pid", "created", "image"} \
                    or any(type(pin.get(key)) is not int for key in ("pid", "created")) \
                    or not isinstance(pin.get("image"), str):
                return {}
            entries.append([lane, str(pin["pid"]), str(pin["created"]), pin["image"]])
        return accepted_lane_identities(entries)
    except (OSError, ValueError, TypeError, ImportError, RecursionError) as error:
        logging.getLogger(__name__).warning("Lane enrollment refused: %s (%s)", error, type(error).__name__)
        return {}


class LaneProcesses:
    """Per-lane server process and callers, with CPU share from the change in CPU time between samples.

    CPU share is of the whole machine (100% = every logical CPU busy). A process identity is
    (pid, creation time), so a restarted lane never inherits the previous process's counters.
    """

    def __init__(self, table_fn=None, info_fn=None, clock=time.monotonic, cpus: int | None = None,
                 self_pid: int | None = None, identities: dict[str, dict[str, Any]] | None = None):
        self.table_fn, self.info_fn, self.clock = table_fn or tcp_table, info_fn or process_info, clock
        self.cpus = cpus or os.cpu_count() or 1
        self.self_pid = os.getpid() if self_pid is None else self_pid
        # Explicit, locally accepted process identities; never inferred from the guest-writable lane config.
        # No pins is the default. Native ownership acceptance must supply PID, creation time and exact image.
        self.identities = {lane: dict(identity) for lane, identity in (identities or {}).items()}
        self._previous: dict[tuple[int, int | None], tuple[float, int]] = {}
        self._first_seen: dict[tuple[int, int], float] = {}
        self._lock = threading.Lock()

    def _owner(self, lane: str, info: dict[str, Any]) -> str | None:
        if info.get("owner") == "pair" or image_owner(info.get("image")) == "pair":
            return "pair"
        pin = self.identities.get(lane)
        if not isinstance(pin, dict) or any(type(pin.get(key)) is not int or pin[key] <= 0
                                            or pin[key] != info.get(key) for key in ("pid", "created")):
            return None
        image, expected = info.get("image"), pin.get("image")
        if not all(isinstance(path, str) and ntpath.isabs(path) and not path.startswith(("\\\\", "//"))
                   for path in (image, expected)):
            return None
        return "agiw" if ntpath.normcase(ntpath.normpath(image)) == ntpath.normcase(ntpath.normpath(expected)) else None

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

    def ownership(self, ports: dict[int, str]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        return self.sample(ports, record=False)

    def sample(self, ports: dict[int, str], *, record: bool = True) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
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
            # The lanes are probed at 127.0.0.1, so only a listener on loopback (or every address) can be the
            # lane; one bound to 127.0.0.2 or a LAN address on the same port is another program.
            listeners = sorted((r for r in rows if r["state"] == MIB_TCP_STATE_LISTEN and ports.get(r["localPort"])
                                and r["local"] in ("127.0.0.1", "0.0.0.0", "::")),
                               key=lambda r: ("127.0.0.1", "0.0.0.0", "::").index(r["local"]))
            selected_pids: dict[str, int] = {}
            for row in listeners:
                lane = ports.get(row["localPort"])
                if lane and lane not in servers:
                    info = self.info_fn(row["pid"])
                    server_pids.add(row["pid"])
                    selected_pids[lane] = row["pid"]
                    # owner "pair" marks a lane port held by NVIDIA PAIR: collect.py never counts it as an AGIW lane.
                    servers[lane] = ({"pid": row["pid"], "name": info["name"], "workingSetBytes": info["workingSetBytes"],
                                      "created": info.get("created"),
                                      "cpuPercent": self._cpu_percent(info, now) if record else None,
                                      "ownerKnown": isinstance(info.get("name"), str) and info["name"] != "unknown"
                                                    and NAME.fullmatch(info["name"]) is not None,
                                      "owner": self._owner(lane, info)} if info else
                                     {"pid": row["pid"], "name": "unknown", "workingSetBytes": None, "cpuPercent": None,
                                      "owner": None, "ownerKnown": False})
            if not record:
                return servers, [], {"id": "lane-processes", "label": "Lane processes", "state": "live",
                                     "ageSeconds": 0.0, "detail": "Lane port ownership rechecked"}
            # Match both sides of a local TCP connection. The reverse row proves that the selected
            # server PID owns this exact endpoint, including a LAN IP reached through a wildcard bind.
            server_connections = {
                (lane, row["remote"], row["remotePort"], row["local"], row["localPort"])
                for row in rows for lane in (ports.get(row["localPort"]),)
                if lane and row["state"] == MIB_TCP_STATE_ESTAB and row["pid"] == selected_pids.get(lane)
            }
            callers: dict[tuple[int, str], dict[str, Any]] = {}
            live_keys = set()
            names: dict[int, str] = {}
            for row in rows:
                lane = ports.get(row["remotePort"])
                matched = (lane, row["local"], row["localPort"], row["remote"], row["remotePort"])
                if (not lane or row["state"] != MIB_TCP_STATE_ESTAB or matched not in server_connections
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


# ----------------------------------------------------------------- NVIDIA PAIR
def process_images(max_pids: int = 4096) -> tuple[list[dict[str, Any]], int]:
    """([{pid, name, owner}], uninspected count) for every process this user may query. Names only, never paths or
    arguments; a process that cannot be opened (another user's, protected) is counted, not guessed."""
    if not IS_WINDOWS:
        raise OSError("process list is Windows-only")
    psapi, kernel32 = ctypes.windll.psapi, ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    max_pids = max(1, min(max_pids, 65536))
    while True:
        pids = (ctypes.c_ulong * max_pids)()
        needed = ctypes.c_ulong()
        if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):
            raise OSError("EnumProcesses failed")
        if needed.value < ctypes.sizeof(pids) or max_pids == 65536:
            break
        max_pids = min(max_pids * 2, 65536)
    rows, uninspected = [], 0
    for pid in pids[:needed.value // ctypes.sizeof(ctypes.c_ulong)]:
        if pid <= 4:  # the idle and system processes have no image
            continue
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            uninspected += 1
            continue
        handle = ctypes.c_void_p(handle)
        try:
            size = ctypes.c_ulong(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                uninspected += 1
                continue
            name = Path(buffer.value).name
            rows.append({"pid": int(pid), "name": name if NAME.match(name) else "unknown", "owner": image_owner(buffer.value)})
        finally:
            kernel32.CloseHandle(handle)
    return rows, uninspected


class PairCensus:
    """NVIDIA PAIR's processes and the TCP listeners they own, from the process list and the TCP table only.

    No request is sent to PAIR. The process list costs tens of milliseconds, so a census is reused for ``ttl``
    seconds; PAIR starts and stops far less often than that.
    """

    def __init__(self, images_fn=process_images, table_fn=tcp_table, clock=time.monotonic, ttl: float = 10.0):
        self.images_fn, self.table_fn, self.clock, self.ttl = images_fn, table_fn, clock, ttl
        self._cached: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def sample(self) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            if self._cached is not None and 0 <= now - self._cached[0] < self.ttl:
                return self._cached[1]
        try:
            processes, uninspected = self.images_fn()
            rows = self.table_fn()
            listeners = [{"pid": r["pid"], "local": r["local"], "port": r["localPort"]}
                         for r in rows if r.get("state") == MIB_TCP_STATE_LISTEN]
            census = {"ok": True, "processes": [p for p in processes if p.get("owner") == "pair"],
                      "listeners": listeners, "uninspected": uninspected}
        except Exception as error:  # noqa: BLE001 - an unreadable census is unknown, not "PAIR stopped"
            census = {"ok": False, "error": type(error).__name__}
        with self._lock:
            self._cached = (now, census)
        return census


# QUERY_USER_NOTIFICATION_STATE from SHQueryUserNotificationState: 3 means a Direct3D full-screen app (a game) is up.
def notification_state() -> int | None:
    """The signed-in session's notification state (shellapi.h QUNS_*), or None when Windows does not say."""
    if not IS_WINDOWS:
        return None
    try:
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:  # type: ignore[attr-defined]
            return None
        return state.value if 1 <= state.value <= 7 else None
    except (OSError, AttributeError):
        return None


# ------------------------------------------------------------------------ GPUs
# Windows' own GPU counters (what Task Manager shows) cover every adapter, including the AMD card that
# nvidia-smi cannot see. Counter instances name an adapter by LUID; DXGI maps each LUID to its description,
# so attribution is exact rather than inferred from memory sizes.
PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
_LUID = re.compile(r"luid_0x([0-9A-Fa-f]{8})_0x([0-9A-Fa-f]{8})_phys_(\d+)")
_ENGINE = re.compile(r"_eng_(\d+)_engtype_(.+)$")
_PID = re.compile(r"^pid_(\d+)_")
ENGINE_SHARES_MAX = 16


class _Luid(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _AdapterDesc1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint), ("DeviceId", ctypes.c_uint),
                ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", _Luid), ("Flags", ctypes.c_uint)]


class _Guid(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]


_IID_IDXGIFactory1 = _Guid(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
VENDORS = {0x10DE: "nvidia", 0x1002: "amd", 0x8086: "intel", 0x1414: "microsoft"}


def _vcall(obj: ctypes.c_void_p, index: int, restype, argtypes: tuple = (), *args):
    """Call COM vtable slot `index` on obj (this pointer first) with explicit argument types."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(vtable[index])(obj, *args)


def dxgi_adapters() -> list[dict[str, Any]]:
    """Hardware adapters from DXGI: description, vendor, dedicated VRAM and LUID (software adapters skipped)."""
    if not IS_WINDOWS:
        return []
    dxgi = ctypes.windll.dxgi  # type: ignore[attr-defined]
    factory = ctypes.c_void_p()
    if dxgi.CreateDXGIFactory1(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory)) != 0 or not factory:
        raise OSError("CreateDXGIFactory1 failed")
    adapters = []
    try:
        for index in range(16):
            adapter = ctypes.c_void_p()
            if _vcall(factory, 12, ctypes.c_long, (ctypes.c_uint, ctypes.c_void_p), index, ctypes.addressof(adapter)) != 0:  # EnumAdapters1
                break
            try:
                desc = _AdapterDesc1()
                if _vcall(adapter, 10, ctypes.c_long, (ctypes.c_void_p,), ctypes.addressof(desc)) == 0 and not desc.Flags & 0x2:  # GetDesc1; skip SOFTWARE
                    luid = f"{desc.AdapterLuid.HighPart & 0xFFFFFFFF:08X}{desc.AdapterLuid.LowPart:08X}"
                    adapters.append({"name": desc.Description.strip()[:64], "vendor": VENDORS.get(desc.VendorId, "other"),
                                     "luid": luid, "memoryTotalMiB": round(desc.DedicatedVideoMemory / 2**20)})
            finally:
                _vcall(adapter, 2, ctypes.c_ulong)  # Release (no arguments)
    finally:
        _vcall(factory, 2, ctypes.c_ulong)
    return adapters


class _PdhItem(ctypes.Structure):
    class _Value(ctypes.Structure):
        _fields_ = [("CStatus", ctypes.c_uint32), ("doubleValue", ctypes.c_double)]
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _Value)]


class GpuCounters:
    """Per-adapter engine load and dedicated memory from PDH, kept open so rate counters have a baseline."""

    def __init__(self):
        if not IS_WINDOWS:
            raise OSError("PDH is Windows-only")
        self.pdh = ctypes.windll.pdh  # type: ignore[attr-defined]
        self.query = ctypes.c_void_p()
        if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query)) != 0:
            raise OSError("PdhOpenQuery failed")
        self.engine, self.memory = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            for path, handle in (("\\GPU Engine(*)\\Utilization Percentage", self.engine),
                                 ("\\GPU Adapter Memory(*)\\Dedicated Usage", self.memory)):
                if self.pdh.PdhAddEnglishCounterW(self.query, path, None, ctypes.byref(handle)) != 0:
                    raise OSError(f"counter unavailable: {path}")
            self.pdh.PdhCollectQueryData(self.query)
            self.adapters = dxgi_adapters()
        except Exception:
            self.close()
            raise

    def _array(self, counter: ctypes.c_void_p) -> list[tuple[str | None, float | None]]:
        size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
        # PDH returns 32-bit status codes; ctypes hands them back as signed ints, so mask before comparing.
        status = self.pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None) & 0xFFFFFFFF
        if status not in (0, PDH_MORE_DATA):
            raise OSError("PdhGetFormattedCounterArray size query failed")
        if size.value == 0:
            return []
        buffer = ctypes.create_string_buffer(size.value)
        if self.pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buffer) & 0xFFFFFFFF:
            raise OSError("PdhGetFormattedCounterArray read failed")
        items = ctypes.cast(buffer, ctypes.POINTER(_PdhItem))
        # Keep failed instances: dropping one can make a partly unread GPU appear quiet.
        return [(items[i].szName, items[i].FmtValue.doubleValue if items[i].FmtValue.CStatus in (0, 1) else None)
                for i in range(count.value)]  # PDH_CSTATUS_VALID_DATA / NEW_DATA

    def sample(self) -> list[dict[str, Any]]:
        if self.pdh.PdhCollectQueryData(self.query) & 0xFFFFFFFF:
            raise OSError("PdhCollectQueryData failed")
        return aggregate_gpu_counters(self._array(self.engine), self._array(self.memory), self.adapters)

    def close(self) -> None:
        if self.query:
            self.pdh.PdhCloseQuery(self.query)
            self.query = ctypes.c_void_p()


def aggregate_gpu_counters(engine_items: list[tuple[str | None, float | None]],
                           memory_items: list[tuple[str | None, float | None]],
                           adapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-adapter utilisation, dedicated memory in use, busiest engine type, and that engine's split by process.

    Engine load as Task Manager defines it: sum every process's share of each physical engine (adapter, phys, engine
    index); the adapter's utilisation is its busiest single engine. Each counter instance is named
    ``pid_<pid>_luid_..._eng_<n>_engtype_<type>``, so the busiest engine's load is also split by process id
    (``engineShares``, largest first). Process ids only: names are resolved, if at all, by the caller.
    """
    engines: dict[str, dict[tuple[str, str], float]] = {}
    engine_type: dict[tuple[str, str, str], str] = {}
    by_pid: dict[tuple[str, str, str], dict[int, float]] = {}
    incomplete = set()
    unread_load = set()
    unidentified = False
    for name, value in engine_items:
        luid = _LUID.search(name) if isinstance(name, str) else None
        if not luid:
            unidentified = True
            continue
        key = (luid.group(1) + luid.group(2)).upper()
        engine = _ENGINE.search(name)
        if not engine or type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            unread_load.add(key)
            continue
        if luid and engine:
            ident = (luid.group(3), engine.group(1))
            engines.setdefault(key, {}).setdefault(ident, 0.0)
            engines[key][ident] += value
            if not math.isfinite(engines[key][ident]):
                unread_load.add(key)
            engine_type[(key, *ident)] = engine.group(2)
            pid = _PID.search(name)
            if pid and int(pid.group(1)) > 0:
                shares = by_pid.setdefault((key, *ident), {})
                shares[int(pid.group(1))] = shares.get(int(pid.group(1)), 0.0) + value
            elif value > 0:
                incomplete.add(key)
    used: dict[str, float] = {}
    unread_memory = set()
    unidentified_memory = False
    for name, value in memory_items:
        luid = _LUID.search(name) if isinstance(name, str) else None
        if not luid:
            unidentified_memory = True
            continue
        key = (luid.group(1) + luid.group(2)).upper()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            unread_memory.add(key)
            continue
        used[key] = used.get(key, 0.0) + value
        if not math.isfinite(used[key]):
            unread_memory.add(key)
    rows = []
    for adapter in adapters:
        per_engine = engines.get(adapter["luid"])
        load_known = bool(per_engine) and not unidentified and adapter["luid"] not in unread_load
        busiest = max(per_engine, key=per_engine.get) if load_known else None
        shares = by_pid.get((adapter["luid"], *busiest), {}) if busiest else {}
        positive_shares = sorted(((pid, value) for pid, value in shares.items() if value > 0),
                                 key=lambda item: item[1], reverse=True)
        # A bounded busiest-engine split cannot prove who owns positive work on another engine.
        complete = bool(busiest) and adapter["luid"] not in incomplete \
            and len(positive_shares) <= ENGINE_SHARES_MAX \
            and not any(value > 0 for ident, value in per_engine.items() if ident != busiest)
        rows.append({**adapter,
                     "utilizationPercent": round(min(100.0, per_engine[busiest]), 1) if load_known else None,
                     "memoryUsedMiB": round(used[adapter["luid"]] / 2**20) if adapter["luid"] in used
                                      and not unidentified_memory and adapter["luid"] not in unread_memory else None,
                     "busiestEngine": engine_type.get((adapter["luid"], *busiest)) if busiest else None,
                     "engineSharesComplete": complete,
                     "engineShares": [{"pid": pid, "percent": round(value, 1)} for pid, value in
                                      positive_shares[:ENGINE_SHARES_MAX]] if busiest else None})
    return rows
