#!/usr/bin/env python3
"""mem-guard: the memory safeguard rail for this Mac (AGIW suite / Inference Monitor).

  mem-guard status [--json]          level, reasons, numbers, consumers, paused jobs
                                     (exit 0 ok/watch, 10 tight, 11 critical, 3 unknown)
  mem-guard admit --need-gb N [--kind light|heavy] [--wait SECONDS] [--label TEXT]
                                     exit 0 admitted, 75 refused
  mem-guard register --pid PID --label TEXT [--json] / unregister --pid PID
  mem-guard resume-all               SIGCONT everything it paused (identity-checked); always safe
  mem-guard resume-overdue           SIGCONT paused jobs past their limit or whose pauser is gone
  mem-guard watch [--interval 2]     foreground watchdog (for use without the monitor)
  mem-guard hook                     Claude Code PreToolUse hook: exit 2 refuses, everything else 0

It never kills a process, never unloads a model and only pauses (SIGSTOP) work that registered
itself; everything it stopped is recorded so it can be resumed after a crash.
Stdlib only, Python 3.9+. Every probe, clock, path, runner, signal sender and notifier is injectable.
Config: ~/.config/agiw/mem-guard.json (closed reader, see DEFAULTS); state: ~/.local/state/agiw/mem-guard/.

Inside an agent sandbox (2026-09-27): /bin/ps and the memorystatus/swap sysctls are refused, so the
process table is read through libproc (ctypes; ps stays the fallback) and the memory level comes from
level.json (written by a process that can read the sysctls: the monitor's sampler or an unsandboxed
`mem-guard status`; owner-checked, 0600, at most `level_file_max_age_seconds` old), then from
`memory_pressure -Q` (availability only).
"""
from __future__ import annotations

import argparse
import calendar
import contextlib
import dataclasses
import errno
import fcntl
import json
import math
import os
import re
import secrets
import shlex
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

GiB = 1 << 30
LEVELS = ("ok", "watch", "tight", "critical")
_RANK = {"ok": 0, "watch": 1, "unknown": 1, "tight": 2, "critical": 3}
PRESSURE_LABEL = {1: "normal", 2: "warning", 4: "critical"}
HEAVY_BYTES = 2 * GiB
SWAP_CONFIRM_SECONDS = 5.1  # longer than the vm_stat cache's five-second refresh interval
SWAP_TREND_MIN_SECONDS = 5.0
SWAP_TREND_MAX_SECONDS = 15.0
SWAP_TREND_MAX_AGE_SECONDS = 1.0
SWAP_TREND_MAX_SWAPINS_BYTES = 1 << 20
SWAP_TREND_MIN_AVAILABLE_PERCENT = 50.0
SWAP_TREND_MIN_VM_FREE_GIB = 20.0
STATUS_EXIT = {"ok": 0, "watch": 0, "tight": 10, "critical": 11, "unknown": 3}
EXIT_REFUSED = 75

STATE_ROOT = Path.home() / ".local/state/agiw/mem-guard"
CONFIG_PATH = Path.home() / ".config/agiw/mem-guard.json"
VM_PATH = "/System/Volumes/VM"
CONFIG_MAX = 16 << 10
HOOK_MAX = 1 << 20
JOURNAL_MAX = 1 << 20
_ENTRY_MAX = 4 << 10
_PAUSED_MAX = 256 << 10
_MAX_ENTRIES = 256
_OUTPUT_MAX = 8 << 20
# TZ=UTC0: `ps -o lstart` prints local time, so a time-zone change (travel, a manual change) would
# otherwise change every recorded start time and make paused jobs unrecognisable.
_PS_ENV = {"LC_ALL": "C", "TZ": "UTC0", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
_MEMBERS_MAX = 512  # processes per paused job (root included): the writer and the reader share it

STATE_KEYS = ("pressure", "availablePercent", "ramBytes", "swapUsedBytes", "swapTotalBytes",
              "compressedBytes", "wiredBytes", "vmFreeBytes", "gpuAllocBytes")

# ---------------------------------------------------------------------------------------------
# Small helpers


def gb(value: Optional[float]) -> str:
    """Bytes as 'N.N GB' (binary gigabytes, as Activity Monitor shows them)."""
    return "?" if value is None else f"{value / GiB:.1f} GB"


def _clean_text(value: Any, limit: int) -> str:
    """Printable characters only, whitespace collapsed, at most `limit` characters."""
    if not isinstance(value, str):
        return ""
    text = "".join(ch if ch.isprintable() else " " for ch in value[: limit * 4])
    return " ".join(text.split())[:limit]


def _int_in(value: Any, low: int, high: int) -> Optional[int]:
    return value if type(value) is int and low <= value <= high else None


def _number_in(value: Any, low: float, high: float) -> Optional[float]:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        return None
    return float(value)


def _bytes_or_none(value: Any) -> Optional[int]:
    return _int_in(value, 0, 1 << 52)


def _run(runner: Callable[..., Any], args: list[str], timeout: float, env: Optional[dict] = None) -> Optional[str]:
    """stdout of a bounded command, or None on any failure (never raises)."""
    try:
        kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout, "check": False}
        if env is not None:
            kwargs["env"] = env
        result = runner(args, **kwargs)
        out = result.stdout if isinstance(result.stdout, str) else ""
        if result.returncode != 0 or len(out) > _OUTPUT_MAX:
            return None
        return out
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------
# Config: closed reader


DEFAULTS: dict[str, Any] = {
    "critical_available_percent": 10.0,
    "tight_available_percent": 20.0,
    "watch_available_percent": 35.0,
    "critical_swap_fraction": 0.50,
    "tight_swap_fraction": 0.25,
    "watch_swap_fraction": 0.10,
    "tight_compressor_fraction": 0.40,
    "watch_compressor_fraction": 0.25,
    "critical_vm_free_gib": 10.0,
    "floor_gib": 4.0,
    "floor_fraction": 0.10,
    "pause_after_seconds": 10.0,
    "resume_after_seconds": 30.0,
    "max_pause_seconds": 1200.0,
    "notify_cooldown_seconds": 300.0,
    "notifications": True,
    "level_file_max_age_seconds": 3.0,
}
_BOUNDS = {
    "critical_available_percent": (0, 100), "tight_available_percent": (0, 100),
    "watch_available_percent": (0, 100),
    "critical_swap_fraction": (0, 4), "tight_swap_fraction": (0, 4), "watch_swap_fraction": (0, 4),
    "tight_compressor_fraction": (0, 1), "watch_compressor_fraction": (0, 1),
    "critical_vm_free_gib": (0, 1024), "floor_gib": (0, 1024), "floor_fraction": (0, 0.9),
    "pause_after_seconds": (1, 3600), "resume_after_seconds": (1, 3600),
    "max_pause_seconds": (60, 86400), "notify_cooldown_seconds": (0, 86400),
    "level_file_max_age_seconds": (1, 600),
}


def default_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg["notes"] = []
    return cfg


def load_config(path: Optional[os.PathLike] = None, *, uid: Optional[int] = None) -> dict[str, Any]:
    """Defaults overlaid with the user's config file. Unknown keys are ignored, out-of-range
    values keep their default, and a file that is not a small regular file owned by the user
    is ignored as a whole; each of those adds a note that classify() reports as a reason."""
    cfg = default_config()
    notes: list[str] = cfg["notes"]
    target = Path(path) if path is not None else CONFIG_PATH
    owner = os.getuid() if uid is None else uid
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return cfg
    except OSError as error:
        why = "a symlink" if error.errno == errno.ELOOP else "unreadable"
        notes.append(f"config ignored: {target.name} is {why}")
        return cfg
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_uid != owner:
            raise ValueError("not owned by you")
        if info.st_size > CONFIG_MAX:
            raise ValueError("larger than 16 KiB")
        data = b""
        while len(data) <= CONFIG_MAX:
            part = os.read(fd, CONFIG_MAX + 1 - len(data))
            if not part:
                break
            data += part
        if len(data) > CONFIG_MAX:
            raise ValueError("larger than 16 KiB")
        raw = json.loads(data.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError, RecursionError) as error:
        # RecursionError: Python 3.9's json raises it for a small but deeply nested file.
        notes.append(f"config ignored: {_clean_text(str(error), 80) or 'unreadable'}")
        return cfg
    finally:
        os.close(fd)
    for key, value in raw.items():
        if key not in DEFAULTS:
            continue
        if isinstance(DEFAULTS[key], bool):
            if type(value) is bool:
                cfg[key] = value
            else:
                notes.append(f"config value {key} ignored")
            continue
        low, high = _BOUNDS[key]
        number = _number_in(value, low, high)
        if number is None:
            notes.append(f"config value {key} ignored (out of range)")
        else:
            cfg[key] = number
    return cfg


def _cfg(cfg: Optional[dict]) -> dict[str, Any]:
    if not cfg:
        return default_config()
    merged = default_config()
    merged.update(cfg)
    return merged


# ---------------------------------------------------------------------------------------------
# Parsers


_VM_PAGE = re.compile(r"page size of (\d{1,7}) bytes")
_VM_LINE = re.compile(r'^"?([A-Za-z][A-Za-z -]{0,60})"?:\s+(\d{1,20})\.?\s*$', re.MULTILINE)


def parse_vm_stat(text: str, page_size: Optional[int] = None) -> dict[str, Optional[int]]:
    """vm_stat output -> page size, memory occupancy and cumulative swap I/O (unknown -> None).
    The header's page size wins; `page_size` (hw.pagesize) is the fallback."""
    header = _VM_PAGE.search(text or "")
    page = int(header.group(1)) if header else page_size
    if type(page) is not int or page < 4096 or page > 65536 or page & (page - 1):
        raise ValueError("vm_stat page size missing or implausible")
    pages = {name.strip(): int(count) for name, count in _VM_LINE.findall(text)}

    def as_bytes(name: str) -> Optional[int]:
        count = pages.get(name)
        return count * page if count is not None and count < 1 << 40 else None

    return {"pageSize": page, "compressedBytes": as_bytes("Pages occupied by compressor"),
            "wiredBytes": as_bytes("Pages wired down"), "freeBytes": as_bytes("Pages free"),
            "swapInBytes": as_bytes("Swapins"), "swapOutBytes": as_bytes("Swapouts")}


_SWAP_TEXT = re.compile(r"total\s*=\s*([\d.]+)([KMGT]?)\s+used\s*=\s*([\d.]+)([KMGT]?)")
_UNIT = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def parse_swapusage_text(text: str) -> tuple[int, int]:
    """`sysctl -n vm.swapusage` text -> (totalBytes, usedBytes)."""
    match = _SWAP_TEXT.search(text or "")
    if not match:
        raise ValueError("unrecognised vm.swapusage text")
    total = int(round(float(match.group(1)) * _UNIT[match.group(2)]))
    used = int(round(float(match.group(3)) * _UNIT[match.group(4)]))
    if not 0 <= used <= total + (1 << 20) or total > 1 << 50:
        raise ValueError("implausible swap numbers")
    return total, min(used, total)


def parse_xsw_usage(raw: bytes) -> tuple[int, int]:
    """struct xsw_usage {u64 total, u64 avail, u64 used, u32 pagesize, u32 encrypted} -> (total, used)."""
    if raw is None or len(raw) < 32:
        raise ValueError("short xsw_usage")
    total, _avail, used, _page, _enc = struct.unpack_from("=QQQIi", raw)
    if used > total or total > 1 << 50:
        raise ValueError("implausible swap numbers")
    return total, used


_PS_ROW = re.compile(r"^\s*(\d{1,10})\s+(\d{1,12})\s+(.+?)\s*$")


def parse_ps(text: str) -> list[tuple[int, int, str]]:
    """`ps -axo pid=,rss=,comm=` -> [(pid, residentBytes, comm)] (rss is in KiB)."""
    rows = []
    for line in (text or "").splitlines()[:20000]:
        match = _PS_ROW.match(line)
        if match:
            rows.append((int(match.group(1)), int(match.group(2)) * 1024, match.group(3)))
    return rows


def parse_simctl_booted(text: str) -> list[str]:
    """`xcrun simctl list devices booted -j` -> booted device names."""
    data = json.loads(text)
    names = []
    for devices in (data.get("devices") or {}).values():
        for device in devices if isinstance(devices, list) else []:
            if isinstance(device, dict) and device.get("state") == "Booted":
                name = _clean_text(device.get("name"), 60)
                if name:
                    names.append(name)
    return names[:16]


# ---------------------------------------------------------------------------------------------
# Consumer grouping


GROUP_LABELS = {"ios-simulator": "iOS Simulators", "llm-server": "Local LLM server", "codex": "Codex",
                "claude": "Claude", "browser": "Browsers"}
_LLM_TOKENS = ("lm studio", "lmstudio", "bionic", "llama", "mlx")
_BROWSER_TOKENS = ("safari", "webkit", "chrome", "firefox")


def group_of(comm: str) -> str:
    low = comm.lower()
    base = low.rsplit("/", 1)[-1]
    if "/coresimulator/" in low:
        return "ios-simulator"
    if base == "lms" or any(token in low for token in _LLM_TOKENS):
        return "llm-server"
    if "codex" in low:
        return "codex"
    if "claude" in low:
        return "claude"
    if any(token in low for token in _BROWSER_TOKENS):
        return "browser"
    return "other"


def _weight(row: dict) -> int:
    return max(row.get("residentBytes") or 0, row.get("gpuAllocBytes") or 0)


def _loaded_model_ids(models: Optional[Iterable[dict]]) -> list[str]:
    ids = []
    for model in models or []:
        if isinstance(model, dict) and model.get("loaded") is True and model.get("host", "mac") == "mac":
            name = _clean_text(model.get("id"), 120)
            if name:
                ids.append(name)
    return ids[:8]


def group_consumers(rows: list[tuple[int, int, str]], *, models: Optional[list] = None,
                    simulators: Optional[list[str]] = None, gpu_alloc_bytes: Optional[int] = None,
                    top_other: int = 3) -> list[dict]:
    """Processes grouped into named consumers, largest first. residentBytes is approximate:
    shared pages are counted once per process."""
    return decorate_consumers(_group_rows(rows, top_other), models=models, simulators=simulators,
                              gpu_alloc_bytes=gpu_alloc_bytes)


def decorate_consumers(base: list[dict], *, models: Optional[list] = None, simulators: Optional[list[str]] = None,
                       gpu_alloc_bytes: Optional[int] = None) -> list[dict]:
    """Grouped rows (from _group_rows, never modified) plus simulator names, loaded models and the
    GPU allocation, largest first. Cheap: it touches only the few group rows."""
    out = []
    for source in base:
        row = dict(source)
        if row["group"] == "ios-simulator" and simulators:
            row["devices"] = list(simulators)
            row["label"] = f"iOS Simulators ({', '.join(simulators[:3])})"[:80]
        elif row["group"] == "llm-server":
            loaded = _loaded_model_ids(models)
            if loaded:
                row["models"] = loaded
            if _bytes_or_none(gpu_alloc_bytes):
                row["gpuAllocBytes"] = gpu_alloc_bytes
        out.append(row)
    return sorted(out, key=_weight, reverse=True)


def _group_rows(rows: list[tuple[int, int, str]], top_other: int = 3) -> list[dict]:
    groups: dict[str, dict] = {}
    others: dict[str, dict] = {}
    for _pid, rss, comm in rows:
        group = group_of(comm)
        if group == "other":
            name = _clean_text(comm.rsplit("/", 1)[-1], 60) or "unknown"
            row = others.setdefault(name, {"group": "other", "label": name, "residentBytes": 0, "processCount": 0})
        else:
            row = groups.setdefault(group, {"group": group, "label": GROUP_LABELS[group],
                                            "residentBytes": 0, "processCount": 0})
        row["residentBytes"] += rss
        row["processCount"] += 1
    top = sorted(others.values(), key=lambda r: r["residentBytes"], reverse=True)[:max(0, top_other)]
    return sorted(list(groups.values()) + top, key=_weight, reverse=True)


def describe_consumer(row: dict) -> str:
    text = f"{row.get('label')} about {gb(row.get('residentBytes'))}"
    if row.get("gpuAllocBytes"):
        text += f" (+ GPU allocation {gb(row['gpuAllocBytes'])})"
    return text


def suggestions(consumers: Optional[list], models: Optional[list] = None, paused_count: int = 0) -> list[str]:
    """Text only, never executed; ordered by the size of what each would free."""
    items: list[tuple[int, str]] = []
    for row in consumers or []:
        if row.get("group") == "ios-simulator":
            items.append((_weight(row), "Shut down unused iOS Simulators (`xcrun simctl shutdown all`)"))
    for model in models or []:
        if (isinstance(model, dict) and model.get("loaded") is True and model.get("host", "mac") == "mac"
                and model.get("state") in ("idle", "loaded")):
            name = _clean_text(model.get("id"), 120)
            if name:
                size = _bytes_or_none(model.get("sizeBytes")) or 0
                items.append((size, f"Unload idle model {name} from the monitor"))
    if paused_count:
        items.append((0, f"{paused_count} background job{'s' if paused_count != 1 else ''} paused"))
    items.sort(key=lambda item: item[0], reverse=True)
    return [text for _size, text in items]


# ---------------------------------------------------------------------------------------------
# Probes


_LIBC: Any = None


def _ctypes_sysctl(name: str, size: int) -> Optional[bytes]:
    """sysctlbyname(3) through ctypes; None whenever it cannot answer."""
    global _LIBC
    if _LIBC is False:
        return None
    try:
        if _LIBC is None:
            import ctypes
            try:
                lib = ctypes.CDLL(None, use_errno=True)
                fn = lib.sysctlbyname
            except (OSError, AttributeError):
                lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
                fn = lib.sysctlbyname
            fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                           ctypes.c_void_p, ctypes.c_size_t]
            fn.restype = ctypes.c_int
            _LIBC = (ctypes, fn)
        ctypes, fn = _LIBC
        buf = ctypes.create_string_buffer(size)
        length = ctypes.c_size_t(size)
        if fn(name.encode("ascii"), buf, ctypes.byref(length), None, 0) != 0:
            return None
        return buf.raw[: length.value]
    except Exception:
        _LIBC = False if _LIBC is None else _LIBC
        return None


_PROC_PIDTBSDINFO = 3
_PROC_PIDT_SHORTBSDINFO = 13
_PROC_PIDPATHINFO_MAXSIZE = 4096
SSTOP = 4  # pbi_status of a stopped (SIGSTOP) process


class LibProc:
    """The process table through /usr/lib/libproc.dylib (ctypes). Agent sandboxes refuse /bin/ps but
    allow proc_listallpids, proc_pidinfo and proc_pidpath: PROC_PIDT_SHORTBSDINFO (ppid, uid, pgid,
    status) answers for every process, PROC_PIDTBSDINFO (start time) and proc_pidpath for the
    caller's own. Methods return ('ok', value), ('gone', None) or ('denied', None); only ESRCH is
    'gone' (a zombie reads as gone too), every other failure is 'denied'. Construction raises when
    the library or the struct layout is not what this code expects; callers then use ps."""

    def __init__(self, path: str = "/usr/lib/libproc.dylib"):
        import ctypes
        lib = ctypes.CDLL(path, use_errno=True)
        lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.proc_listallpids.restype = ctypes.c_int
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidinfo.restype = ctypes.c_int
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_pidpath.restype = ctypes.c_int
        u32 = ctypes.c_uint32

        class Short(ctypes.Structure):  # struct proc_bsdshortinfo
            _fields_ = [("pid", u32), ("ppid", u32), ("pgid", u32), ("status", u32), ("comm", ctypes.c_char * 16),
                        ("flags", u32), ("uid", u32), ("gid", u32), ("ruid", u32), ("rgid", u32),
                        ("svuid", u32), ("svgid", u32), ("rfu", u32)]

        class Bsd(ctypes.Structure):  # struct proc_bsdinfo
            _fields_ = [("flags", u32), ("status", u32), ("xstatus", u32), ("pid", u32), ("ppid", u32),
                        ("uid", u32), ("gid", u32), ("ruid", u32), ("rgid", u32), ("svuid", u32), ("svgid", u32),
                        ("rfu", u32), ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                        ("nfiles", u32), ("pgid", u32), ("pjobc", u32), ("tdev", u32), ("tpgid", u32),
                        ("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]

        if ctypes.sizeof(Short) != 64 or ctypes.sizeof(Bsd) != 136:
            raise OSError("unexpected proc_bsdinfo layout")
        self._ctypes, self._lib, self._Short, self._Bsd = ctypes, lib, Short, Bsd

    def _info(self, pid: int, flavor: int, struct_type: Any) -> tuple[str, Any]:
        ctypes = self._ctypes
        if type(pid) is not int or not 0 < pid <= _PID_MAX:
            return "gone", None
        info = struct_type()
        ctypes.set_errno(0)
        size = self._lib.proc_pidinfo(pid, flavor, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size == ctypes.sizeof(info) and info.pid == pid:
            return "ok", info
        return ("gone" if ctypes.get_errno() == errno.ESRCH else "denied"), None

    def pids(self) -> Optional[list[int]]:
        """Every pid, or None when the list cannot be read."""
        ctypes = self._ctypes
        for _ in range(4):
            count = self._lib.proc_listallpids(None, 0)
            if count <= 0:
                return None
            capacity = min(count + 256, 1 << 20)
            buf = (ctypes.c_int * capacity)()
            got = self._lib.proc_listallpids(buf, ctypes.sizeof(buf))
            if got <= 0:
                return None
            if got < capacity:  # a full buffer may have been truncated: read again, larger
                return [buf[i] for i in range(got) if buf[i] > 0]
        return None

    def short(self, pid: int) -> tuple[str, Optional[tuple[int, int, int, int]]]:
        """(ppid, uid, pgid, status)."""
        state, info = self._info(pid, _PROC_PIDT_SHORTBSDINFO, self._Short)
        return state, ((info.ppid, info.uid, info.pgid, info.status) if info is not None else None)

    def bsd(self, pid: int) -> tuple[str, Optional[tuple[int, int, int, str]]]:
        """(ppid, uid, start time in epoch seconds, 16-character kernel comm)."""
        state, info = self._info(pid, _PROC_PIDTBSDINFO, self._Bsd)
        if info is None:
            return state, None
        return state, (info.ppid, info.uid, int(info.start_sec), info.comm.decode("utf-8", "replace"))

    def path(self, pid: int) -> Optional[str]:
        """The executable's path (proc_pidpath), or None."""
        ctypes = self._ctypes
        buf = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
        size = self._lib.proc_pidpath(pid, buf, _PROC_PIDPATHINFO_MAXSIZE)
        if size <= 0:
            return None
        return buf.raw[:size].decode("utf-8", "replace")


_LIBPROC: Any = None


def default_procs() -> Optional[LibProc]:
    """The process-wide LibProc, or None where libproc is unavailable (then callers use ps)."""
    global _LIBPROC
    if _LIBPROC is None:
        try:
            _LIBPROC = LibProc()
        except Exception:
            _LIBPROC = False
    return _LIBPROC or None


LEVEL_FILE_MAX = 4 << 10
_LEVEL_FIELDS = ("pressure", "availablePercent", "ramBytes", "swapUsedBytes", "swapTotalBytes")


class LevelFile:
    """level.json in the mem-guard state directory: the kernel's memory numbers as last read by a
    process allowed to read them (the monitor's sampler, an unsandboxed `mem-guard status`), for
    callers inside agent sandboxes that refuse the memorystatus and swap sysctls. Advisory only: it is
    read only when it is a regular file owned by the caller with no group or other permission bits,
    opened without following a symlink, written from sysctl readings (never from another level
    file) and at most `max_age` seconds old. write() and read() never raise."""

    def __init__(self, path: Optional[os.PathLike] = None, *, max_age: float = DEFAULTS["level_file_max_age_seconds"],
                 clock: Optional[Callable[[], float]] = None, uid: Optional[int] = None):
        self.path = Path(path) if path is not None else STATE_ROOT / "level.json"
        self.max_age = float(max_age)
        self._clock = clock or time.time
        self._uid = os.getuid() if uid is None else uid

    def write(self, state: Any) -> bool:
        """Publish the kernel reading in `state` (only a state whose levelSource is 'sysctl')."""
        try:
            if not isinstance(state, dict) or state.get("levelSource") != "sysctl":
                return False
            record: dict[str, Any] = {"schemaVersion": 1, "source": "sysctl", "writtenAt": round(self._clock(), 3),
                                      "pid": os.getpid()}
            record.update(_level_fields(state))
            if record["pressure"] is None and record["availablePercent"] is None:
                return False
            directory = self.path.parent
            _ensure_private_dir(directory, self._uid)
            tmp = directory / f".{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    _write_all(fd, _dumps(record))
                finally:
                    os.close(fd)
                os.replace(tmp, self.path)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
            return True
        except Exception:
            return False

    def read(self) -> Optional[dict]:
        """{pressure, availablePercent, ramBytes, swapUsedBytes, swapTotalBytes, ageSeconds} or None."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self._uid or info.st_mode & 0o077
                    or info.st_size > LEVEL_FILE_MAX):
                return None
            data = os.read(fd, LEVEL_FILE_MAX + 1)
            if len(data) > LEVEL_FILE_MAX:
                return None
            raw = json.loads(data.decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            return None
        finally:
            os.close(fd)
        if not isinstance(raw, dict) or raw.get("schemaVersion") != 1 or raw.get("source") != "sysctl":
            return None
        written = _number_in(raw.get("writtenAt"), 0, 1 << 40)
        if written is None:
            return None
        age = self._clock() - written
        if not -1.0 <= age <= self.max_age:  # stale, or written by a clock ahead of ours
            return None
        level = _level_fields(raw)
        if level["pressure"] is None and level["availablePercent"] is None:
            return None
        level["ageSeconds"] = max(0.0, age)
        return level


def _level_fields(source: dict) -> dict[str, Any]:
    pressure = _int_in(source.get("pressure"), 1, 4)
    return {"pressure": pressure if pressure in PRESSURE_LABEL else None,
            "availablePercent": _int_in(source.get("availablePercent"), 0, 100),
            "ramBytes": _int_in(source.get("ramBytes"), GiB // 4, 1 << 50),
            "swapUsedBytes": _bytes_or_none(source.get("swapUsedBytes")),
            "swapTotalBytes": _bytes_or_none(source.get("swapTotalBytes"))}


_MEMORY_PRESSURE = re.compile(r"System-wide memory free percentage:\s*(\d{1,3})%")


class Probes:
    """Read-only memory probes. Every failure becomes None; nothing here raises to callers.

    background=True (the monitor): the slow probes (vm_stat, statvfs, ps, simctl) are refreshed on
    short-lived daemon threads and a call returns the latest value at once, so the 1 Hz sampler
    never waits on a subprocess. The CLI keeps the default, synchronous reads.

    When neither the pressure level nor the availability sysctl answers (an agent sandbox), read()
    falls back to `level_file` (a fresh LevelFile) and then, with memory_pressure=True, to
    `memory_pressure -Q` for the availability alone. state['levelSource'] says which answered:
    'sysctl', 'level-file', 'memory_pressure' or None. Both fallbacks are off unless asked for (the
    CLI's Context turns them on; the monitor, which can read the sysctls, does not need them)."""

    VM_STAT_EVERY = 5.0
    VM_FREE_EVERY = 10.0
    CONSUMERS_EVERY = 10.0
    SIMULATORS_EVERY = 30.0
    CLI_RETRY = 30.0

    def __init__(self, *, sysctl: Optional[Callable[[str, int], Optional[bytes]]] = None,
                 runner: Optional[Callable[..., Any]] = None, statvfs: Optional[Callable[[str], Any]] = None,
                 clock: Optional[Callable[[], float]] = None, wall: Optional[Callable[[], float]] = None,
                 vm_path: str = VM_PATH, background: bool = False, level_file: Optional[LevelFile] = None,
                 memory_pressure: bool = False):
        self._level_file = level_file
        self._memory_pressure = memory_pressure
        self._sysctl = sysctl if sysctl is not None else _ctypes_sysctl
        self._runner = runner or subprocess.run
        self._statvfs = statvfs or os.statvfs
        self._clock = clock or time.monotonic
        self._wall = wall or time.time
        self._vm_path = vm_path
        self._background = background
        self._cache: dict[str, tuple[float, Any]] = {}
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._grouped: Optional[tuple[Any, list[dict]]] = None  # (the ps rows it was built from, groups)
        self._cli_failed: dict[str, float] = {}
        self._ram: Optional[int] = None
        self._page: Optional[int] = None

    # -- sysctl ---------------------------------------------------------------------------
    def _raw(self, name: str, size: int) -> Optional[bytes]:
        try:
            raw = self._sysctl(name, size)
            return raw if isinstance(raw, (bytes, bytearray)) and raw else None
        except Exception:
            return None

    def _cli(self, name: str) -> Optional[str]:
        now = self._clock()
        failed = self._cli_failed.get(name)
        if failed is not None and now - failed < self.CLI_RETRY:
            return None
        out = _run(self._runner, ["/usr/sbin/sysctl", "-n", name], 2.0)
        if out is None or len(out) > 4096:
            self._cli_failed[name] = now
            return None
        self._cli_failed.pop(name, None)
        return out.strip()

    def _int(self, name: str) -> Optional[int]:
        raw = self._raw(name, 8)
        if raw is not None and len(raw) in (4, 8):
            return int.from_bytes(bytes(raw), sys.byteorder, signed=len(raw) == 4)
        text = self._cli(name)
        if text and re.fullmatch(r"-?\d{1,20}", text):
            return int(text)
        return None

    def pressure(self) -> Optional[int]:
        value = self._int("kern.memorystatus_vm_pressure_level")
        return value if value in PRESSURE_LABEL else None

    def available_percent(self) -> Optional[int]:
        return _int_in(self._int("kern.memorystatus_level"), 0, 100)

    def ram_bytes(self) -> Optional[int]:
        if self._ram is None:
            self._ram = _int_in(self._int("hw.memsize"), GiB // 4, 1 << 50)
        return self._ram

    def page_size(self) -> Optional[int]:
        if self._page is None:
            self._page = _int_in(self._int("hw.pagesize"), 4096, 65536)
        return self._page

    def swap(self) -> Optional[tuple[int, int]]:
        raw = self._raw("vm.swapusage", 32)
        if raw is not None:
            try:
                return parse_xsw_usage(bytes(raw))
            except ValueError:
                pass
        text = self._cli("vm.swapusage")
        if not text:
            return None
        try:
            return parse_swapusage_text(text)
        except ValueError:
            return None

    # -- cached slow probes -----------------------------------------------------------------
    def _cached(self, key: str, every: float, fn: Callable[[], Any]) -> Any:
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and 0 <= now - hit[0] < every:
            return hit[1]
        if self._background:
            self._refresh(key, fn)
            return hit[1] if hit is not None else None  # the latest value; a fresh one follows
        try:
            value = fn()
        except Exception:
            value = None
        self._cache[key] = (now, value)
        return value

    def _refresh(self, key: str, fn: Callable[[], Any]) -> None:
        with self._pending_lock:
            if key in self._pending:
                return
            self._pending.add(key)

        def run() -> None:
            try:
                value = fn()
            except Exception:
                value = None
            with self._pending_lock:
                self._cache[key] = (self._clock(), value)
                self._pending.discard(key)
        try:
            threading.Thread(target=run, name=f"mem-guard-{key}", daemon=True).start()
        except Exception:
            with self._pending_lock:
                self._pending.discard(key)

    def settle(self, timeout: float = 5.0) -> bool:
        """Wait until no background refresh is in flight (tests and the first status read)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._pending_lock:
                if not self._pending:
                    return True
            time.sleep(0.005)
        return False

    def vm_stat(self) -> Optional[dict]:
        def read() -> Optional[dict]:
            out = _run(self._runner, ["/usr/bin/vm_stat"], 2.0)
            if out is None:
                return None
            fallback = None if _VM_PAGE.search(out) else self.page_size()
            return parse_vm_stat(out, fallback)
        return self._cached("vm_stat", self.VM_STAT_EVERY, read)

    def vm_free(self) -> Optional[int]:
        def read() -> Optional[int]:
            for path in (self._vm_path, "/"):
                try:
                    info = self._statvfs(path)
                    return int(info.f_bavail) * int(info.f_frsize)
                except Exception:
                    continue
            return None
        return self._cached("vm_free", self.VM_FREE_EVERY, read)

    def _ps_rows(self) -> Optional[list]:
        out = _run(self._runner, ["/bin/ps", "-axo", "pid=,rss=,comm="], 2.0, env=_PS_ENV)
        return parse_ps(out) if out is not None else None

    def simulators(self) -> Optional[list[str]]:
        def read() -> Optional[list[str]]:
            out = _run(self._runner, ["/usr/bin/xcrun", "simctl", "list", "devices", "booted", "-j"], 5.0)
            return parse_simctl_booted(out) if out is not None else None
        return self._cached("simulators", self.SIMULATORS_EVERY, read)

    def consumers(self, models: Optional[list] = None, gpu_alloc_bytes: Optional[int] = None) -> Optional[list]:
        """Named consumer groups (ps at most every 10 s); None when ps cannot be read. The process
        table is grouped once per ps read; each call only decorates the few group rows."""
        try:
            rows = self._cached("ps", self.CONSUMERS_EVERY, self._ps_rows)
            if rows is None:
                return None
            grouped = self._grouped
            if grouped is None or grouped[0] is not rows:
                grouped = (rows, _group_rows(rows))
                self._grouped = grouped
            base = grouped[1]
            sims = self.simulators() if any(row["group"] == "ios-simulator" for row in base) else None
            return decorate_consumers(base, models=models, simulators=sims, gpu_alloc_bytes=gpu_alloc_bytes)
        except Exception:
            return None

    # -- one state ----------------------------------------------------------------------------
    def read(self, gpu_alloc_bytes: Optional[int] = None) -> dict[str, Any]:
        state: dict[str, Any] = dict.fromkeys(STATE_KEYS)
        state["sampledAt"] = self._wall()

        def safe(fn: Callable[[], Any]) -> Any:
            try:
                return fn()
            except Exception:
                return None

        state["pressure"] = safe(self.pressure)
        state["availablePercent"] = safe(self.available_percent)
        state["levelSource"] = "sysctl" if state["pressure"] is not None or state["availablePercent"] is not None else None
        state["ramBytes"] = safe(self.ram_bytes)
        swap = safe(self.swap)
        if swap:
            state["swapTotalBytes"], state["swapUsedBytes"] = swap
        if state["levelSource"] is None:
            safe(lambda: self._fallback(state))
        vm = safe(self.vm_stat)
        if vm:
            state["compressedBytes"] = vm.get("compressedBytes")
            state["wiredBytes"] = vm.get("wiredBytes")
            state["swapInBytes"] = vm.get("swapInBytes")
            state["swapOutBytes"] = vm.get("swapOutBytes")
            cached_vm = self._cache.get("vm_stat")
            state["vmStatSampledAt"] = cached_vm[0] if cached_vm is not None else None
        state["vmFreeBytes"] = safe(self.vm_free)
        state["gpuAllocBytes"] = _bytes_or_none(gpu_alloc_bytes)
        return state

    def _fallback(self, state: dict) -> None:
        """The sysctls were refused: a fresh level file first, then memory_pressure -Q."""
        level = self._level_file.read() if self._level_file is not None else None
        if level:
            state["pressure"], state["availablePercent"] = level["pressure"], level["availablePercent"]
            for key in ("ramBytes", "swapUsedBytes", "swapTotalBytes"):
                if state.get(key) is None and level.get(key) is not None:
                    state[key] = level[key]
            state["levelSource"] = "level-file"
            state["levelAgeSeconds"] = round(level["ageSeconds"], 1)
            return
        if self._memory_pressure:
            percent = self._memory_pressure_percent()
            if percent is not None:
                state["availablePercent"] = percent
                state["levelSource"] = "memory_pressure"

    def _memory_pressure_percent(self) -> Optional[int]:
        """`memory_pressure -Q` prints the same number as kern.memorystatus_level (checked 2026-09-27:
        87% from both) and runs inside agent sandboxes. A failure backs off like the sysctl CLI."""
        now = self._clock()
        failed = self._cli_failed.get("memory_pressure")
        if failed is not None and now - failed < self.CLI_RETRY:
            return None
        out = _run(self._runner, ["/usr/bin/memory_pressure", "-Q"], 2.0)
        match = _MEMORY_PRESSURE.search(out or "")
        if match is None:
            self._cli_failed["memory_pressure"] = now
            return None
        self._cli_failed.pop("memory_pressure", None)
        return _int_in(int(match.group(1)), 0, 100)


# ---------------------------------------------------------------------------------------------
# Classification


@dataclasses.dataclass(frozen=True)
class SwapEvidence:
    """Two samples taken by one caller. A prior CLI status or level file cannot supply this."""
    first: dict
    second: dict
    windowSeconds: float
    ageSeconds: float


def _swap_sample_eligible(state: Any, cfg: dict) -> bool:
    """A kernel sample with ample signals; availability is not free physical bytes."""
    if not isinstance(state, dict) or state.get("levelSource") != "sysctl":
        return False
    ram = _int_in(state.get("ramBytes"), 1, 1 << 52)
    swap = _bytes_or_none(state.get("swapUsedBytes"))
    avail = _number_in(state.get("availablePercent"), 0, 100)
    vm_free = _bytes_or_none(state.get("vmFreeBytes"))
    if (_int_in(state.get("pressure"), 1, 4) != 1 or ram is None or swap is None
            or avail is None or vm_free is None
            or avail < max(SWAP_TREND_MIN_AVAILABLE_PERCENT, cfg["watch_available_percent"])
            or vm_free < max(SWAP_TREND_MIN_VM_FREE_GIB, 2 * cfg["critical_vm_free_gib"]) * GiB):
        return False
    fraction = swap / ram
    if not cfg["tight_swap_fraction"] <= fraction < cfg["critical_swap_fraction"]:
        return False
    return (_bytes_or_none(state.get("swapInBytes")) is not None
            and _bytes_or_none(state.get("swapOutBytes")) is not None
            and _number_in(state.get("vmStatSampledAt"), 0, 1 << 40) is not None)


def _swap_only_tight(state: dict, cfg: dict) -> bool:
    if not _swap_sample_eligible(state, cfg):
        return False
    # A concurrent pressure, availability, compressor or VM-space reason retains its own level.
    without_swap = dict(state, swapUsedBytes=None)
    return classify(without_swap, cfg)[0] in ("ok", "watch")


def _quiet_swap_evidence(state: dict, cfg: dict, evidence: Any) -> bool:
    if (not isinstance(evidence, SwapEvidence) or evidence.second is not state
            or _number_in(evidence.windowSeconds, SWAP_TREND_MIN_SECONDS, SWAP_TREND_MAX_SECONDS) is None
            or _number_in(evidence.ageSeconds, 0, SWAP_TREND_MAX_AGE_SECONDS) is None
            or not _swap_sample_eligible(evidence.first, cfg) or not _swap_only_tight(state, cfg)
            or evidence.first["ramBytes"] != state["ramBytes"]
            or state["vmStatSampledAt"] <= evidence.first["vmStatSampledAt"]):
        return False
    first, second = evidence.first, state
    if second["swapUsedBytes"] > first["swapUsedBytes"]:
        return False
    swapins = second["swapInBytes"] - first["swapInBytes"]
    swapouts = second["swapOutBytes"] - first["swapOutBytes"]
    return 0 <= swapins <= SWAP_TREND_MAX_SWAPINS_BYTES and swapouts == 0


def classify(state: Optional[dict], cfg: Optional[dict] = None, *,
             swap_evidence: Optional[SwapEvidence] = None) -> tuple[str, list[str]]:
    """(level, reasons): ok < watch < tight < critical, or 'unknown' when neither the kernel's
    pressure level nor its available percentage is readable. Missing inputs never raise the
    level by themselves."""
    cfg = _cfg(cfg)
    s = state if isinstance(state, dict) else {}
    pressure = _int_in(s.get("pressure"), 1, 4)
    pressure = pressure if pressure in PRESSURE_LABEL else None
    avail = _number_in(s.get("availablePercent"), 0, 100)
    ram = _int_in(s.get("ramBytes"), 1, 1 << 52)
    swap = _bytes_or_none(s.get("swapUsedBytes"))
    compressed = _bytes_or_none(s.get("compressedBytes"))
    vm_free = _bytes_or_none(s.get("vmFreeBytes"))
    hits: dict[str, list[str]] = {"critical": [], "tight": [], "watch": []}

    if pressure == 4:
        hits["critical"].append("macOS memory pressure: critical")
    elif pressure == 2:
        hits["tight"].append("macOS memory pressure: warning")
    if avail is not None:
        text = f"only {avail:.0f}% of memory available"
        if avail < cfg["critical_available_percent"]:
            hits["critical"].append(text)
        elif avail < cfg["tight_available_percent"]:
            hits["tight"].append(text)
        elif avail < cfg["watch_available_percent"]:
            hits["watch"].append(text)
    if swap is not None and ram:
        fraction = swap / ram
        text = f"swap {gb(swap)} ({fraction * 100:.0f}% of RAM)"
        if fraction >= cfg["critical_swap_fraction"]:
            hits["critical"].append(text)
        elif fraction >= cfg["tight_swap_fraction"]:
            if _quiet_swap_evidence(s, cfg, swap_evidence):
                hits["watch"].append(text + " (no swap growth or swapouts during fresh confirmation)")
            else:
                hits["tight"].append(text)
        elif fraction >= cfg["watch_swap_fraction"]:
            hits["watch"].append(text)
    if compressed is not None and ram:
        fraction = compressed / ram
        text = f"compressor {gb(compressed)} ({fraction * 100:.0f}% of RAM)"
        # Compressor occupancy lags: it stays high long after pressure has passed. On its own it
        # raises the level only to watch; it counts toward tight when the kernel agrees memory is
        # short (pressure warning or worse, or availability already below the watch threshold).
        corroborated = (pressure is not None and pressure >= 2) or (
            avail is not None and avail < cfg["watch_available_percent"])
        if fraction >= cfg["tight_compressor_fraction"] and corroborated:
            hits["tight"].append(text)
        elif fraction >= cfg["tight_compressor_fraction"]:
            hits["watch"].append(text)
        elif fraction >= cfg["watch_compressor_fraction"]:
            hits["watch"].append(text)
    if vm_free is not None and vm_free < cfg["critical_vm_free_gib"] * GiB:
        hits["critical"].append(f"VM volume has only {gb(vm_free)} free")

    level = next((name for name in ("critical", "tight", "watch") if hits[name]), "ok")
    reasons = hits["critical"] + hits["tight"] + hits["watch"]
    if pressure is None and avail is None:
        level = "unknown"
        reasons.insert(0, "memory state unknown: macOS pressure and availability unreadable")
    reasons.extend(str(note) for note in cfg.get("notes") or [])
    # Said last, so a refusal's first reason is still the memory reason itself.
    source = s.get("levelSource")
    if level != "unknown" and source == "level-file":
        age = _number_in(s.get("levelAgeSeconds"), 0, 86400)
        reasons.append("read from the monitor's level file" + (f" ({age:.0f} s old)" if age is not None else "")
                       + ": this process cannot read the kernel's memory sysctls")
    elif level != "unknown" and source == "memory_pressure":
        reasons.append("availability from memory_pressure -Q: this process cannot read the kernel's "
                       "pressure level or swap (no fresh level file)")
    return level, reasons


# ---------------------------------------------------------------------------------------------
# Admission


@dataclasses.dataclass(frozen=True)
class Decision:
    allowed: bool
    level: str
    reason: str
    availableBytes: Optional[int]
    kind: str = "light"
    needBytes: int = 0
    suggestion: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _kind_and_need(need_bytes: Any, kind: Optional[str]) -> tuple[str, int]:
    need = _number_in(need_bytes, 0, float(1 << 50))
    if need is None:
        return "heavy", HEAVY_BYTES  # an unknown size is treated as heavy work
    size = int(need)
    derived = "heavy" if size >= HEAVY_BYTES else "light"
    # An explicit kind wins only when it is stricter than the size implies.
    return ("heavy" if kind == "heavy" else derived), size


def admit(need_bytes: Any, kind: Optional[str] = None, state: Optional[dict] = None,
          cfg: Optional[dict] = None, *, swap_evidence: Optional[SwapEvidence] = None) -> Decision:
    """May this work start? Never raises: on any internal failure light work is admitted
    (fail-open) and heavy work refused (fail-closed)."""
    try:
        return _admit(need_bytes, kind, state or {}, _cfg(cfg), swap_evidence)
    except Exception:
        k, need = ("heavy", HEAVY_BYTES)
        try:
            k, need = _kind_and_need(need_bytes, kind)
        except Exception:
            pass
        if k == "light":
            return Decision(True, "unknown", f"memory state unknown; light work ({gb(need)}) admitted", None, k, need)
        return Decision(False, "unknown", f"memory state unknown; heavy work ({gb(need)}) refused", None, k, need)


def _admit(need_bytes: Any, kind: Optional[str], state: dict, cfg: dict,
           swap_evidence: Optional[SwapEvidence] = None) -> Decision:
    k, need = _kind_and_need(need_bytes, kind)
    level, reasons = classify(state, cfg, swap_evidence=swap_evidence)
    ram = _int_in(state.get("ramBytes"), 1, 1 << 52)
    percent = _number_in(state.get("availablePercent"), 0, 100)
    available = int(percent * ram / 100) if percent is not None and ram else None
    floor = max(cfg["floor_gib"] * GiB, cfg["floor_fraction"] * ram if ram else 0.0)
    fits = available is not None and available - need >= floor
    what = f"{k} work ({gb(need)})"
    why = reasons[0] if reasons else level

    def decide(allowed: bool, reason: str) -> Decision:
        return Decision(allowed, level, reason, available, k, need)

    def headroom() -> str:
        return f"{gb(available)} available, {what} would leave {gb(max(0, available - need))} (floor {gb(floor)})"

    if level == "critical":
        return decide(False, f"memory is critical ({why}); {what} refused until it recovers")
    if level == "tight":
        if k == "heavy":
            return decide(False, f"memory is tight ({why}); {what} refused")
        if available is None:
            return decide(True, f"memory is tight ({why}) and the available amount is unknown; {what} admitted")
        if fits:
            return decide(True, f"memory is tight ({why}) but {headroom()}; admitted")
        return decide(False, f"memory is tight ({why}); {headroom()}; refused")
    if level in ("watch", "unknown"):
        prefix = "memory state unknown" if level == "unknown" else f"memory needs watching ({why})"
        if k == "light":
            return decide(True, f"{prefix}; {what} admitted")
        if available is None:
            return decide(False, f"{prefix}; {what} refused until memory can be measured")
        if fits:
            return decide(True, f"{prefix}; {headroom()}; admitted")
        return decide(False, f"{prefix}; {headroom()}; refused")
    # ok
    if available is None:
        if k == "light":
            return decide(True, f"memory ok but the available amount is unknown; {what} admitted")
        return decide(False, f"the available amount is unknown; {what} refused")
    if fits:
        return decide(True, f"memory ok; {headroom()}; admitted")
    return decide(False, f"{headroom()}; refused")


def check(need_bytes: Any, kind: Optional[str] = None, *, probes: Optional[Probes] = None,
          cfg: Optional[dict] = None, models: Optional[list] = None) -> Decision:
    """admit() against a fresh probe read; a refusal carries the top suggestion. Never raises."""
    try:
        probes = probes or Probes()
        state = probes.read()
        decision = admit(need_bytes, kind, state, cfg)
        if not decision.allowed:
            consumers = probes.consumers(models=models) or []
            tips = suggestions(consumers, models)
            if tips:
                decision = dataclasses.replace(decision, suggestion=tips[0])
        return decision
    except Exception:
        return admit(need_bytes, kind, None, cfg)


# ---------------------------------------------------------------------------------------------
# Journal


class Journal:
    """Append-only JSON lines (0600), rotated to `.1` at 1 MiB. Best effort: never raises."""

    def __init__(self, path: os.PathLike, clock: Optional[Callable[[], float]] = None, max_bytes: int = JOURNAL_MAX):
        self.path = Path(path)
        self._clock = clock or time.time
        self._max = max_bytes

    def write(self, event: dict) -> bool:
        try:
            _ensure_private_dir(self.path.parent)
            line = json.dumps({"at": round(self._clock(), 3), **event}, allow_nan=False, default=str)
            try:
                if os.lstat(self.path).st_size >= self._max:
                    os.replace(self.path, str(self.path) + ".1")
            except FileNotFoundError:
                pass
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                _write_all(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------------------------
# Notifications


_APPLESCRIPT = ("on run argv",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "end run")


class OsascriptNotifier:
    """macOS notification; title and body travel as argv to an `on run argv` script and are
    never interpolated into AppleScript source."""

    def __init__(self, runner: Optional[Callable[..., Any]] = None):
        self._runner = runner or subprocess.run

    @staticmethod
    def _arg(text: str, limit: int) -> str:
        # A leading dash could be read as an osascript option; strip it.
        return _clean_text(text, limit).lstrip("- ") or "(no text)"

    def __call__(self, title: str, body: str) -> bool:
        args = ["/usr/bin/osascript"]
        for line in _APPLESCRIPT:
            args += ["-e", line]
        args += [self._arg(title, 80), self._arg(body, 400)]
        try:
            self._runner(args, capture_output=True, text=True, timeout=5, check=False)
            return True
        except Exception:
            return False


class BackgroundNotifier:
    """Fire and forget: the wrapped notifier runs on a daemon thread, so a slow osascript (5 s
    timeout) never blocks the monitor's sampler. At most two notifications are in flight."""

    def __init__(self, notifier: Callable[[str, str], Any]):
        self._notifier = notifier
        self._slots = threading.BoundedSemaphore(2)

    def __call__(self, title: str, body: str) -> bool:
        if not self._slots.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                self._notifier(title, body)
            except Exception:
                pass
            finally:
                self._slots.release()
        try:
            threading.Thread(target=run, name="mem-guard-notify", daemon=True).start()
            return True
        except Exception:
            self._slots.release()
            return False


# ---------------------------------------------------------------------------------------------
# Pausable registry


class RegistryError(Exception):
    pass


def _ensure_private_dir(path: Path, uid: Optional[int] = None) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != (os.getuid() if uid is None else uid):
        raise RegistryError(f"{path} is not a private directory owned by you")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _read_small_json(path: Any, limit: int, uid: int, *, dir_fd: Optional[int] = None,
                     inode: Optional[list] = None) -> Any:
    """A small regular JSON file owned by `uid`, or None (missing, symlink, oversize, invalid).
    `inode`, when given, receives the file's inode number (to unlink exactly what was read)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=dir_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_size > limit:
            return None
        if inode is not None:
            inode.append(info.st_ino)
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            return None
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    """os.write until every byte is written; a write that makes no progress (disk full) raises."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.ENOSPC, "short write")
        view = view[written:]


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, allow_nan=False, sort_keys=True).encode("utf-8")


def _write_file(directory: Path, name: str, obj: Any, *, exclusive: bool) -> None:
    """Write via a private temp file that is read back and compared before it is installed;
    exclusive -> hard-link (fails if present), else os.replace. A short write (a full disk)
    raises and leaves the old file in place."""
    data = _dumps(obj)
    tmp = directory / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            back = b""
            while len(back) <= len(data):
                part = os.read(fd, len(data) + 1 - len(back))
                if not part:
                    break
                back += part
            if back != data:
                raise OSError(errno.EIO, f"{name} did not read back as written")
        finally:
            os.close(fd)
        if exclusive:
            os.link(tmp, directory / name)
        else:
            os.replace(tmp, directory / name)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


_LSTART = r"[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{1,2}:\d{2}:\d{2}\s+\d{4}"
_ID_ROW = re.compile(r"^\s*(\d{1,10})\s+(\d{1,10})\s+(\d{1,10})\s+(" + _LSTART + r")(?:\s+(.*?))?\s*$")
_WDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _start_epoch(text: str) -> Optional[int]:
    """A normalised `ps -o lstart` string read under TZ=UTC0 -> epoch seconds (None if malformed)."""
    parts = text.split() if isinstance(text, str) else []
    if len(parts) != 5 or parts[1] not in _MONTHS:
        return None
    try:
        hour, minute, second = (int(part) for part in parts[3].split(":"))
        return calendar.timegm((int(parts[4]), _MONTHS.index(parts[1]) + 1, int(parts[2]), hour, minute, second))
    except (ValueError, OverflowError):
        return None


def _lstart_text(moment: time.struct_time) -> str:
    return (f"{_WDAYS[moment.tm_wday]} {_MONTHS[moment.tm_mon - 1]} {moment.tm_mday} "
            f"{moment.tm_hour:02d}:{moment.tm_min:02d}:{moment.tm_sec:02d} {moment.tm_year}")


def same_start(recorded: Any, current: Any) -> bool:
    """Is `recorded` the start time that ps (TZ=UTC0) reports now as `current`? Records written
    before ps ran in UTC hold local time; they still match while the time zone is unchanged."""
    if not isinstance(recorded, str) or not isinstance(current, str):
        return False
    if recorded == current:
        return True
    epoch = _start_epoch(current)
    if epoch is None:
        return False
    try:
        return recorded == _lstart_text(time.localtime(epoch))
    except (OverflowError, OSError, ValueError):
        return False
_TREE_ROW = re.compile(r"^\s*(\d{1,10})\s+(\d{1,10})\s+(\d{1,10})\s*$")
_ENTRY_NAME = re.compile(r"^(\d{1,10})\.json$")
_PID_MAX = (1 << 31) - 1


def _valid_pid(pid: Any) -> int:
    if type(pid) is not int or not 1 < pid <= _PID_MAX:
        raise RegistryError("pid must be an integer greater than 1")
    return pid


class Registry:
    """Jobs WE started that may be paused under critical memory. Only registered pids and their
    live same-user descendants are ever signalled, and only after an identity check.

    Process facts come from libproc (`procs`, default LibProc) and fall back to /bin/ps when libproc
    is unavailable or cannot answer for one of our own processes. An explicitly injected ps `runner`
    without `procs` keeps the registry on ps alone (the ps-based tests). Both sources give the same
    identity: start time as `ps -o lstart` prints it under TZ=UTC0, comm the executable's basename
    (libproc: proc_pidpath; ps: the comm column); a process that renamed its argv[0] reads
    differently under the two and is then treated as a different process (dropped, never signalled)."""

    def __init__(self, root: Optional[os.PathLike] = None, *, runner: Optional[Callable[..., Any]] = None,
                 kill: Optional[Callable[[int, int], None]] = None, uid: Optional[int] = None,
                 pid: Optional[int] = None, clock: Optional[Callable[[], float]] = None, procs: Any = None):
        self.root = Path(root) if root is not None else STATE_ROOT
        self.pausable_dir = self.root / "pausable"
        self.paused_path = self.root / "paused.json"
        self._procs = procs if procs is not None else (None if runner is not None else default_procs())
        self._runner = runner or subprocess.run
        self._kill = kill or os.kill
        self._uid = os.getuid() if uid is None else uid
        self._pid = os.getpid() if pid is None else pid
        self._clock = clock or time.time
        # Set by stop_pausing() (monitor shutdown); pause_all checks it under the lock just
        # before it signals, so a pause racing the shutdown's resume never lands.
        self._closing = threading.Event()

    def stop_pausing(self, timeout: float = 2.0) -> None:
        """No pause lands after this returns: the flag stops any pause that has not reached its
        locked section, and taking the lock once waits out one that already has, so its record is
        in paused.json when the shutdown resume reads it."""
        self._closing.set()
        if os.path.isdir(self.root):  # no state directory: no pause can be inside the lock
            with contextlib.suppress(Exception):
                with self._lock(timeout):
                    pass

    # -- process facts ------------------------------------------------------------------------
    def identities(self, pids: Iterable[int]) -> Optional[dict[int, dict]]:
        """{pid: {ppid, uid, startTime, comm, path}} for live pids; None when neither libproc nor ps
        can answer. startTime is `lstart` in UTC; comm is the executable's basename (never empty).
        Another user's process that libproc may not read in full is reported with its ppid and uid
        and an empty startTime (every caller rejects it on the uid first)."""
        wanted = sorted({p for p in pids if type(p) is int and 1 < p <= _PID_MAX})
        if not wanted:
            return {}
        if self._procs is not None:
            try:
                found = self._identities_libproc(wanted)
            except Exception:
                found = None
            if found is not None:
                return found
        return self._identities_ps(wanted)

    def _identities_libproc(self, wanted: list[int]) -> Optional[dict[int, dict]]:
        procs = self._procs
        found: dict[int, dict] = {}
        for pid in wanted:
            state, info = procs.bsd(pid)
            if state == "gone":
                continue
            if state == "ok":
                ppid, uid, start, kernel_comm = info
                path = _clean_text(procs.path(pid) or "", 1024)
                comm = _clean_text(path.rsplit("/", 1)[-1], 255) or _clean_text(kernel_comm, 255) or "?"
                try:
                    start_text = _lstart_text(time.gmtime(start))
                except (OverflowError, OSError, ValueError):
                    return None
                found[pid] = {"ppid": int(ppid), "uid": int(uid), "startTime": start_text, "comm": comm, "path": path}
                continue
            short_state, short = procs.short(pid)
            if short_state == "gone":
                continue
            if short_state != "ok" or short[1] == self._uid:
                return None  # one of ours that libproc cannot read in full: ask ps instead
            found[pid] = {"ppid": int(short[0]), "uid": int(short[1]), "startTime": "", "comm": "?", "path": ""}
        return found

    def _identities_ps(self, wanted: list[int]) -> Optional[dict[int, dict]]:
        try:
            result = self._runner(["/bin/ps", "-o", "pid=,ppid=,uid=,lstart=,comm=", "-p", ",".join(map(str, wanted))],
                                  capture_output=True, text=True, timeout=2.0, check=False, env=_PS_ENV)
        except Exception:
            return None
        out = result.stdout if isinstance(result.stdout, str) else ""
        # ps exits 1 when some pid is gone; that is an answer, not a failure.
        if result.returncode not in (0, 1) or (result.returncode == 1 and (result.stderr or "").strip()):
            return None
        found = {}
        for line in out.splitlines()[:4096]:
            match = _ID_ROW.match(line)
            if match and int(match.group(1)) in wanted:
                path = _clean_text(match.group(5) or "", 1024)
                comm = _clean_text(path.rsplit("/", 1)[-1], 255) or path[-255:] or "?"
                found[int(match.group(1))] = {"ppid": int(match.group(2)), "uid": int(match.group(3)),
                                              "startTime": " ".join(match.group(4).split()),
                                              "comm": comm, "path": path}
        return found

    def tree(self) -> Optional[dict[int, tuple[int, int]]]:
        """{pid: (ppid, uid)} for every process; None when neither libproc nor ps can list the
        process table with our own pid in it."""
        if self._procs is not None:
            try:
                rows = self._tree_libproc()
            except Exception:
                rows = None
            if rows is not None:
                return rows
        out = _run(self._runner, ["/bin/ps", "-axo", "pid=,ppid=,uid="], 2.0, env=_PS_ENV)
        if out is None:
            return None
        rows = {}
        for line in out.splitlines()[:50000]:
            match = _TREE_ROW.match(line)
            if match:
                rows[int(match.group(1))] = (int(match.group(2)), int(match.group(3)))
        return rows if self._pid in rows else None

    def _tree_libproc(self) -> Optional[dict[int, tuple[int, int]]]:
        pids = self._procs.pids()
        if not pids:
            return None
        rows = {}
        for pid in pids[:50000]:
            state, short = self._procs.short(pid)
            if state == "ok" and 0 < pid <= _PID_MAX:
                rows[pid] = (int(short[0]), int(short[1]))
        return rows if self._pid in rows else None

    def _protected(self, tree: dict[int, tuple[int, int]]) -> set[int]:
        """Our own pid, every ancestor of it, and pids 0 and 1."""
        protected = {0, 1, self._pid}
        if self._pid == os.getpid():
            protected.add(os.getppid())
        current = self._pid
        for _ in range(128):
            parent = tree.get(current, (0, 0))[0]
            protected.add(parent)
            if parent <= 1 or parent == current:
                break
            current = parent
        return protected

    @staticmethod
    def _descends_from(tree: dict[int, tuple[int, int]], pid: int, anchor: int) -> bool:
        """Is `pid` the process `anchor` or one of its descendants?"""
        current = pid
        for _ in range(256):
            if current == anchor:
                return True
            parent = tree.get(current, (0, 0))[0]
            if parent <= 1 or parent == current:
                return False
            current = parent
        return False

    @staticmethod
    def _descendants(tree: dict[int, tuple[int, int]], root: int, limit: int = _MEMBERS_MAX - 1) -> list[int]:
        children: dict[int, list[int]] = {}
        for pid, (ppid, _uid) in tree.items():
            children.setdefault(ppid, []).append(pid)
        found, queue, seen = [], [root], {root}
        while queue and len(found) < limit:
            for child in sorted(children.get(queue.pop(0), [])):
                if child not in seen:
                    seen.add(child)
                    found.append(child)
                    queue.append(child)
        return found

    # -- locking and files -----------------------------------------------------------------
    @contextlib.contextmanager
    def _lock(self, timeout: float = 2.0) -> Iterator[None]:
        _ensure_private_dir(self.root, self._uid)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RegistryError("mem-guard state is busy") from None
                    time.sleep(0.02)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _valid_entry(obj: Any) -> Optional[dict]:
        if not isinstance(obj, dict) or obj.get("schemaVersion") != 1:
            return None
        pid, start, comm = obj.get("pid"), obj.get("startTime"), obj.get("comm")
        if type(pid) is not int or not 1 < pid <= _PID_MAX:
            return None
        if not isinstance(start, str) or not 0 < len(start) <= 64 or not isinstance(comm, str) or not 0 < len(comm) <= 255:
            return None
        entry = {"schemaVersion": 1, "pid": pid, "startTime": start, "comm": comm,
                 "label": _clean_text(obj.get("label"), 80) or f"pid {pid}",
                 "registeredAt": obj.get("registeredAt") if type(obj.get("registeredAt")) in (int, float) else None}
        hold = obj.get("noRepauseUntil")
        if type(hold) in (int, float) and math.isfinite(hold):
            entry["noRepauseUntil"] = hold
        return entry

    @staticmethod
    def _valid_member(obj: Any) -> Optional[dict]:
        """A paused member exactly as pause_all writes it. comm is informational (a process may
        exec between the ps read and SIGSTOP), so any string up to 255 characters is accepted."""
        if not isinstance(obj, dict):
            return None
        pid, start, comm = obj.get("pid"), obj.get("startTime"), obj.get("comm")
        if type(pid) is not int or not 1 < pid <= _PID_MAX or not isinstance(start, str) or not 0 < len(start) <= 64:
            return None
        return {"pid": pid, "startTime": start, "comm": comm[:255] if isinstance(comm, str) else ""}

    @contextlib.contextmanager
    def _pausable_fd(self) -> Iterator[Optional[int]]:
        """pausable/ opened without following a symlink, or None when it is missing, a symlink or
        not a directory of ours: entries are then neither read nor deleted."""
        try:
            fd = os.open(self.pausable_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            yield None
            return
        try:
            info = os.fstat(fd)
            yield fd if stat.S_ISDIR(info.st_mode) and info.st_uid == self._uid else None
        finally:
            os.close(fd)

    def _entries(self, fd: int) -> list[tuple[str, Optional[dict], Optional[int]]]:
        """(name, entry or None, inode) for each `<pid>.json` in the pausable directory `fd`."""
        try:
            with os.scandir(fd) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError:
            return []
        found = []
        for name in names:
            match = _ENTRY_NAME.match(name)
            if not match:
                continue
            inode: list = []
            entry = self._valid_entry(_read_small_json(name, _ENTRY_MAX, self._uid, dir_fd=fd, inode=inode))
            if entry is not None and entry["pid"] != int(match.group(1)):
                entry = None
            found.append((name, entry, inode[0] if inode else None))
            if len(found) >= _MAX_ENTRIES:
                break
        return found

    @staticmethod
    def _unlink_if_same(fd: int, name: str, inode: Optional[int]) -> None:
        """Delete an entry only if it is still the file that was read (a concurrent register()
        may have replaced it)."""
        with contextlib.suppress(OSError):
            if inode is None or os.stat(name, dir_fd=fd, follow_symlinks=False).st_ino == inode:
                os.unlink(name, dir_fd=fd)

    def has_entries(self) -> bool:
        with self._pausable_fd() as fd:
            if fd is None:
                return False
            try:
                with os.scandir(fd) as entries:
                    return any(_ENTRY_NAME.match(entry.name) for entry in entries)
            except OSError:
                return False

    def _read_paused(self) -> list[dict]:
        data = _read_small_json(self.paused_path, _PAUSED_MAX, self._uid)
        if not isinstance(data, dict) or data.get("schemaVersion") != 1 or not isinstance(data.get("jobs"), list):
            return []
        jobs = []
        for job in data["jobs"][:_MAX_ENTRIES]:
            entry = self._valid_entry({**job, "schemaVersion": 1}) if isinstance(job, dict) else None
            members = job.get("members") if isinstance(job, dict) else None
            paused_at = job.get("pausedAt") if isinstance(job, dict) else None
            if (entry is None or not isinstance(members, list) or type(paused_at) not in (int, float)
                    or not math.isfinite(paused_at)):
                continue
            clean_members = [m for m in (self._valid_member(member) for member in members[:_MEMBERS_MAX]) if m]
            resume_by = job.get("resumeBy")
            if type(resume_by) not in (int, float) or not math.isfinite(resume_by):
                resume_by = float(paused_at) + DEFAULTS["max_pause_seconds"]  # records from before resumeBy
            pauser = job.get("pauser")
            if not (isinstance(pauser, dict) and type(pauser.get("pid")) is int and isinstance(pauser.get("startTime"), str)
                    and 0 < len(pauser["startTime"]) <= 64):
                pauser = None
            jobs.append({"pid": entry["pid"], "startTime": entry["startTime"], "comm": entry["comm"],
                         "label": entry["label"], "pausedAt": float(paused_at), "resumeBy": float(resume_by),
                         "pauser": {"pid": pauser["pid"], "startTime": pauser["startTime"]} if pauser else None,
                         "members": clean_members})
        return jobs

    def _write_paused(self, jobs: list[dict]) -> None:
        if not jobs:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.paused_path)
            return
        _write_file(self.root, self.paused_path.name, {"schemaVersion": 1, "jobs": jobs}, exclusive=False)

    def _signal(self, pid: int, sig: int) -> str:
        """'sent', 'gone' (no such process) or 'failed' (refused, e.g. EPERM)."""
        if type(pid) is not int or pid <= 1 or pid == self._pid:
            return "failed"  # never a process group, never init, never ourselves
        try:
            self._kill(pid, sig)
            return "sent"
        except ProcessLookupError:
            return "gone"
        except OSError:
            return "failed"

    def _sources_failed(self) -> str:
        return "libproc and ps both failed" if self._procs is not None else "ps failed"

    # -- public API ------------------------------------------------------------------------
    def register(self, pid: Any, label: Any) -> dict:
        """Make a job pausable. Only work the caller started itself: the pid must be the process
        that ran mem-guard (a script's `$$`) or one of its descendants (a script's `$!`), must not
        have been started by launchd and must not be an app's main executable."""
        pid = _valid_pid(pid)
        if pid == self._pid:
            raise RegistryError("refusing to register mem-guard itself")
        tree = self.tree()
        if tree is None:
            raise RegistryError(f"cannot read the process table ({self._sources_failed()})")
        if pid not in tree:
            raise RegistryError(f"no process {pid}")
        caller = tree[self._pid][0]
        if caller <= 1:
            raise RegistryError("run `mem-guard register` from the job's own script")
        if not self._descends_from(tree, pid, caller):
            raise RegistryError(f"process {pid} was not started by the calling script; a job may register "
                                "only itself ($$) or what it started ($!)")
        if pid != caller and pid in self._protected(tree):
            raise RegistryError("refusing to register mem-guard or one of its ancestors")
        if tree[pid][0] <= 1:
            raise RegistryError(f"process {pid} was started by launchd (an app or a daemon); refusing")
        ids = self.identities([pid])
        if ids is None:
            raise RegistryError(f"cannot read the process identity ({self._sources_failed()})")
        ident = ids.get(pid)
        if ident is None:
            raise RegistryError(f"no process {pid}")
        if ident["uid"] != self._uid:
            raise RegistryError(f"process {pid} belongs to another user")
        if ".app/Contents/MacOS/" in ident["path"]:
            raise RegistryError(f"process {pid} is an app ({ident['comm']}); refusing")
        entry = {"schemaVersion": 1, "pid": pid, "startTime": ident["startTime"], "comm": ident["comm"],
                 "label": _clean_text(label, 80) or f"pid {pid}", "registeredAt": round(self._clock(), 3)}
        with self._lock():
            _ensure_private_dir(self.pausable_dir, self._uid)
            path = self.pausable_dir / f"{pid}.json"
            if os.path.lexists(path):
                existing = self._valid_entry(_read_small_json(path, _ENTRY_MAX, self._uid))
                if existing and same_start(existing["startTime"], entry["startTime"]) and existing["comm"] == entry["comm"]:
                    return existing
                os.unlink(path)  # stale entry from a reused pid, or an invalid file
            try:
                _write_file(self.pausable_dir, path.name, entry, exclusive=True)
            except FileExistsError:
                raise RegistryError(f"process {pid} is already registered") from None
        return entry

    def _has_entry(self, name: str) -> bool:
        with self._pausable_fd() as fd:
            if fd is None:
                return False
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
                return True
            except OSError:
                return False

    def unregister(self, pid: Any) -> bool:
        """Forget a job and, if it is paused, resume it (identity-checked). The registration goes
        first, so no watchdog can pause the job again in between."""
        pid = _valid_pid(pid)
        name = f"{pid}.json"
        removed = False
        if self._has_entry(name):
            with self._lock(), self._pausable_fd() as fd:
                if fd is not None:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(name, dir_fd=fd)
                        removed = True
        if any(job["pid"] == pid for job in self._read_paused()):
            self.resume({pid})
        return removed

    def list_pausable(self) -> list[dict]:
        """Registered jobs whose identity still matches; dead or reused pids are dropped (never
        signalled). An unreadable process table returns [] and deletes nothing; a pausable/
        that is a symlink or not our own directory is never listed or cleaned."""
        with self._pausable_fd() as fd:
            if fd is None:
                return []
            entries = self._entries(fd)
            if not entries:
                return []
            ids = self.identities(entry["pid"] for _name, entry, _ino in entries if entry)
            if ids is None:
                return []
            valid = []
            for name, entry, inode in entries:
                current = ids.get(entry["pid"]) if entry else None
                if (entry is None or current is None or current["uid"] != self._uid
                        or not same_start(entry["startTime"], current["startTime"]) or current["comm"] != entry["comm"]):
                    self._unlink_if_same(fd, name, inode)
                    continue
                valid.append(entry)
            return valid

    def hold(self, keys: Iterable[tuple[int, str]], seconds: float) -> None:
        """Keep these registered jobs (pid, startTime) from being paused again for `seconds`.
        Stored in their registration, so a restarted or a second watchdog respects it."""
        keys = set(keys)
        if not keys:
            return
        until = round(self._clock() + seconds, 3)
        with self._lock():
            _ensure_private_dir(self.pausable_dir, self._uid)
            with self._pausable_fd() as fd:
                entries = self._entries(fd) if fd is not None else []
            for name, entry, _inode in entries:
                if entry is not None and (entry["pid"], entry["startTime"]) in keys:
                    _write_file(self.pausable_dir, name, {**entry, "noRepauseUntil": until}, exclusive=False)

    def paused(self) -> list[dict]:
        """What is paused now (read-only)."""
        return [{"pid": j["pid"], "label": j["label"], "comm": j["comm"], "pausedAt": j["pausedAt"],
                 "members": len(j["members"])} for j in self._read_paused()]

    def paused_jobs(self) -> list[dict]:
        return self._read_paused()

    def pause_all(self, exclude: Iterable[tuple[int, str]] = (), *,
                  max_pause_seconds: Optional[float] = None) -> list[dict]:
        """SIGSTOP every registered job not yet paused (and its live same-user descendants).
        The process table is read before taking the lock (ps can be slow on a loaded Mac). Under
        the lock paused.json is re-read, then written and verified before any signal, so a crash
        can always be resumed; nothing is signalled once stop_pausing() was called, and a job whose
        record would not fit paused.json's read limit is not paused at all."""
        if self._closing.is_set():
            return []
        skip = set(exclude)
        now = self._clock()
        limit = max_pause_seconds if max_pause_seconds is not None else DEFAULTS["max_pause_seconds"]
        already = {(job["pid"], job["startTime"]) for job in self._read_paused()}
        todo = [e for e in self.list_pausable()
                if (e["pid"], e["startTime"]) not in already and (e["pid"], e["startTime"]) not in skip
                and not e.get("noRepauseUntil", 0) > now]
        if not todo:
            return []
        tree = self.tree()
        if tree is None:
            return []
        protected = self._protected(tree)
        plans = []
        for entry in todo:
            if entry["pid"] in protected:
                continue
            pids = [entry["pid"]] + self._descendants(tree, entry["pid"])
            pids = [p for p in pids if p > 1 and p not in protected and tree.get(p, (0, -1))[1] == self._uid]
            plans.append((entry, pids))
        # One ps for every member plus ourselves (the pauser, so a later CLI call can tell when it
        # is gone). ppid is re-read: a descendant that exited after the tree walk and whose pid was
        # reused by an unrelated process no longer has the parent the walk saw, and is skipped.
        ids = self.identities([self._pid] + [p for _entry, pids in plans for p in pids])
        if ids is None:
            return []
        me = ids.get(self._pid)
        pauser = {"pid": self._pid, "startTime": me["startTime"]} if me else None
        jobs = []
        for entry, pids in plans:
            root = ids.get(entry["pid"])
            if (root is None or root["uid"] != self._uid or not same_start(entry["startTime"], root["startTime"])
                    or root["comm"] != entry["comm"]):
                continue
            members = []
            for p in pids:
                ident = ids.get(p)
                if ident is None or ident["uid"] != self._uid:
                    continue
                if p != entry["pid"] and ident["ppid"] != tree[p][0]:
                    continue
                members.append({"pid": p, "startTime": ident["startTime"], "comm": ident["comm"]})
            jobs.append({"pid": entry["pid"], "startTime": entry["startTime"], "comm": entry["comm"],
                         "label": entry["label"], "pausedAt": round(now, 3), "resumeBy": round(now + limit, 3),
                         "pauser": pauser, "members": members[:_MEMBERS_MAX]})
        if not jobs:
            return []
        with self._lock():
            if self._closing.is_set():
                return []
            record = self._read_paused()
            keys = {(job["pid"], job["startTime"]) for job in record}
            added = []
            for job in jobs:
                if (job["pid"], job["startTime"]) in keys or len(record) >= _MAX_ENTRIES:
                    continue
                if not self._has_entry(f"{job['pid']}.json"):
                    continue  # unregistered while the process table was read
                if len(_dumps({"schemaVersion": 1, "jobs": record + [job]})) > _PAUSED_MAX:
                    continue  # the reader would drop the whole file: never stop what cannot be recorded
                record.append(job)
                added.append(job)
            if not added:
                return []
            self._write_paused(record)
            for job in added:
                for member in job["members"]:
                    self._signal(member["pid"], signal.SIGSTOP)
            return added

    def resume(self, pids: Optional[Iterable[int]] = None, *, timeout: float = 2.0) -> list[dict]:
        """SIGCONT paused jobs (all, or those whose root pid is in `pids`), re-checking every
        member's identity (pid, uid, start time) first. A member stays recorded until it is
        continued or verifiably gone: unverifiable (ps failed) jobs and members whose SIGCONT
        failed are kept for a retry and reported under 'failed'."""
        wanted = set(pids) if pids is not None else None
        targets = [job for job in self._read_paused() if wanted is None or job["pid"] in wanted]
        if not targets:
            return []  # nothing to do: stay read-only (no lock, no state directory)
        # ps runs before the lock so a slow process table never holds up other writers.
        fetched = {m["pid"] for job in targets for m in job["members"]}
        ids = self.identities(fetched)
        if ids is None:
            return []
        with self._lock(timeout):
            return self._resume_locked(wanted, ids, fetched)

    def _resume_locked(self, pids: Optional[set[int]], ids: dict[int, dict], fetched: set[int]) -> list[dict]:
        jobs = self._read_paused()
        targets = [job for job in jobs if pids is None or job["pid"] in pids]
        if not targets:
            return []
        missing = {m["pid"] for job in targets for m in job["members"]} - fetched
        if missing:  # a job paused between the ps read and the lock
            more = self.identities(missing)
            if more is None:
                return []
            ids = {**ids, **more}
        resumed, keep = [], []
        for job in jobs:
            if not any(job is target for target in targets):
                keep.append(job)
                continue
            sent, left = [], []
            for member in reversed(job["members"]):
                now = ids.get(member["pid"])
                if now is None or now["uid"] != self._uid or not same_start(member["startTime"], now["startTime"]):
                    continue  # gone, or the pid now belongs to another process: nothing to continue
                outcome = self._signal(member["pid"], signal.SIGCONT)
                if outcome == "sent":
                    sent.append(member["pid"])
                elif outcome == "failed":
                    left.append(member)
            resumed.append({**job, "resumed": sent, "failed": [m["pid"] for m in left]})
            if left:
                keep.append({**job, "members": [m for m in job["members"] if m in left]})
        self._write_paused(keep)
        return resumed

    def resume_overdue(self) -> list[dict]:
        """SIGCONT jobs whose pause deadline (resumeBy) has passed or whose pauser is gone, so the
        pause limit holds even when no watchdog is running. Read-only when nothing is paused."""
        jobs = self._read_paused()
        if not jobs:
            return []
        now = self._clock()
        pausers = {job["pauser"]["pid"] for job in jobs if job["pauser"]}
        ids = self.identities(pausers) if pausers else {}
        due = set()
        for job in jobs:
            pauser = job["pauser"]
            if now >= job["resumeBy"]:
                due.add(job["pid"])
            elif pauser and ids is not None and pauser["pid"] != self._pid:
                current = ids.get(pauser["pid"])
                if current is None or not same_start(pauser["startTime"], current["startTime"]):
                    due.add(job["pid"])
        return self.resume(due) if due else []


# ---------------------------------------------------------------------------------------------
# Snapshot block


def memory_block(state: dict, cfg: Optional[dict] = None, *, consumers: Optional[list] = None,
                 paused: Optional[list] = None, models: Optional[list] = None) -> dict[str, Any]:
    level, reasons = classify(state, cfg)
    block: dict[str, Any] = {"level": level, "reasons": reasons}
    for key in STATE_KEYS:
        block[key] = state.get(key)
    block["consumers"] = list(consumers or [])
    block["paused"] = list(paused or [])
    block["suggestions"] = suggestions(block["consumers"], models, len(block["paused"]))
    block["sampledAt"] = state.get("sampledAt")
    return block


def unknown_block(now: Optional[float] = None, reason: str = "memory probes failed") -> dict[str, Any]:
    block: dict[str, Any] = {"level": "unknown", "reasons": [reason]}
    block.update(dict.fromkeys(STATE_KEYS))
    block.update({"consumers": [], "paused": [], "suggestions": [], "sampledAt": time.time() if now is None else now})
    return block


# ---------------------------------------------------------------------------------------------
# Watchdog


def _job_summary(job: dict) -> dict:
    summary = {"pid": job.get("pid"), "label": job.get("label"), "members": len(job.get("members") or [])}
    if job.get("failed"):
        summary["notContinued"] = list(job["failed"])  # still stopped; kept in paused.json for a retry
    return summary


class Watchdog:
    """Hysteresis over classified levels: pause registered jobs after `pause_after_seconds` of
    continuous critical, resume them after `resume_after_seconds` at watch or better, and never
    keep a job paused longer than `max_pause_seconds`. Notifies once per escalation (cooldown per
    level) and once on recovery to ok after a critical episode. tick() never raises."""

    TITLE = "AGIW memory"

    def __init__(self, clock: Optional[Callable[[], float]] = None, probes: Any = None,
                 notifier: Optional[Callable[[str, str], Any]] = None, registry: Optional[Registry] = None,
                 cfg: Optional[dict] = None, *, journal: Optional[Journal] = None):
        self.clock = clock or time.time
        self.probes = probes
        self.notifier = notifier
        self.registry = registry
        self.cfg = _cfg(cfg)
        if journal is None and registry is not None:
            journal = Journal(registry.root / "events.jsonl", self.clock)
        self.journal = journal
        self.level: Optional[str] = None
        self.reasons: list[str] = []
        self._critical_since: Optional[float] = None
        self._calm_since: Optional[float] = None
        self._last_notice: dict[str, float] = {}
        self._critical_episode = False
        self._exempt: set[tuple[int, str]] = set()
        self._last_pause_try: Optional[float] = None
        self._last_resume_try: Optional[float] = None
        self.startup_events = self._resume("startup")

    def _log(self, event: dict) -> None:
        if self.journal is not None:
            self.journal.write(event)

    def _notify(self, body: str) -> None:
        if self.notifier is None or not self.cfg.get("notifications", True):
            return
        try:
            self.notifier(self.TITLE, body)
        except Exception:
            pass

    def _resume(self, why: str, pids: Optional[set[int]] = None, timeout: float = 2.0) -> list[dict]:
        if self.registry is None:
            return []
        try:
            if not self.registry.paused_jobs():
                return []
            resumed = self.registry.resume(pids, timeout=timeout)
        except Exception:
            return []
        if not resumed:
            return []
        event = {"type": "resume", "why": why, "jobs": [_job_summary(job) for job in resumed]}
        self._log(event)
        return [event]

    def close(self, timeout: float = 2.0) -> list[dict]:
        """Resume everything we paused (monitor shutdown / end of `watch`)."""
        return self._resume("shutdown", timeout=timeout)

    def _body(self, level: str, reasons: list[str], state: dict) -> str:
        consumers = state.get("consumers")
        if not consumers and self.probes is not None and hasattr(self.probes, "consumers"):
            with contextlib.suppress(Exception):
                consumers = self.probes.consumers(gpu_alloc_bytes=state.get("gpuAllocBytes"))
        consumers = consumers or []
        paused = state.get("paused") or []
        tips = state.get("suggestions") or suggestions(consumers, state.get("models"), len(paused))
        parts = [f"Memory is {level}" + (f": {reasons[0]}." if reasons else ".")]
        if consumers:
            parts.append("Top: " + ", ".join(describe_consumer(row) for row in consumers[:2]) + ".")
        if tips:
            parts.append(tips[0] + ".")
        return " ".join(parts)

    def tick(self, state: Optional[dict] = None) -> list[dict]:
        events: list[dict] = []
        try:
            now = self.clock()
            if state is None:
                state = self.probes.read() if self.probes is not None else {}
            level, reasons = classify(state, self.cfg)
            previous = self.level
            if level != previous:
                event = {"type": "level", "from": previous, "to": level, "reasons": reasons[:4]}
                events.append(event)
                self._log(event)
            self.level, self.reasons = level, reasons

            if level == "critical":
                if self._critical_since is None:
                    self._critical_since = now
                self._critical_episode = True
            else:
                self._critical_since = None
            if level in ("ok", "watch"):
                if self._calm_since is None:
                    self._calm_since = now
                if now - self._calm_since >= self.cfg["resume_after_seconds"]:
                    # The episode ends only after sustained calm, not on one non-critical sample.
                    self._exempt.clear()
            else:
                self._calm_since = None

            self._notifications(level, previous, reasons, state, now, events)
            if self.registry is not None:
                self._relief(level, now, events)
        except Exception:
            pass
        return events

    def _notifications(self, level: str, previous: Optional[str], reasons: list[str], state: dict,
                       now: float, events: list[dict]) -> None:
        if level in ("tight", "critical") and _RANK[level] > _RANK.get(previous or "ok", 0):
            last = self._last_notice.get(level)
            if last is None or now - last >= self.cfg["notify_cooldown_seconds"]:
                self._last_notice[level] = now
                body = self._body(level, reasons, state)
                self._notify(body)
                events.append({"type": "notify", "level": level, "body": body})
        if level == "ok" and previous not in (None, "ok") and self._critical_episode:
            self._critical_episode = False
            body = "Memory recovered: back to ok after a critical episode."
            self._notify(body)
            events.append({"type": "notify", "level": "ok", "body": body})

    def _relief(self, level: str, now: float, events: list[dict]) -> None:
        cfg = self.cfg
        if (level == "critical" and self._critical_since is not None
                and now - self._critical_since >= cfg["pause_after_seconds"]
                and (self._last_pause_try is None or now - self._last_pause_try >= 5.0)
                and self.registry.has_entries()):
            self._last_pause_try = now
            try:
                jobs = self.registry.pause_all(exclude=self._exempt, max_pause_seconds=cfg["max_pause_seconds"])
            except Exception:
                jobs = []
            if jobs:
                event = {"type": "pause", "level": level, "jobs": [_job_summary(job) for job in jobs]}
                events.append(event)
                self._log(event)
        try:
            paused = self.registry.paused_jobs()
        except Exception:
            paused = []
        if not paused:
            return
        # A member whose SIGCONT failed stays recorded; retry at most every 5 s, not every tick.
        if self._last_resume_try is not None and 0 <= now - self._last_resume_try < 5.0:
            return
        if self._calm_since is not None and now - self._calm_since >= cfg["resume_after_seconds"]:
            self._last_resume_try = now
            events.extend(self._resume("recovered"))
            return
        limit = cfg["max_pause_seconds"]
        overdue = {job["pid"] for job in paused if now - job["pausedAt"] >= limit}
        if not overdue:
            return
        self._last_resume_try = now
        resumed = self._resume("limit", overdue)
        if resumed:
            held = {(job["pid"], job["startTime"]) for job in paused if job["pid"] in overdue}
            # Not paused again in this episode, and (persisted, so a restarted or second watchdog
            # agrees) not for another `max_pause_seconds`: a job may be stopped at most half the time.
            self._exempt |= held
            with contextlib.suppress(Exception):
                self.registry.hold(held, limit)
            count = len(resumed[0]["jobs"])
            body = (f"{count} paused job{'s' if count != 1 else ''} resumed after the {limit / 60:.0f}-minute limit; "
                    f"memory is still {level}.")
            self._notify(body)
            events.extend(resumed)
            events.append({"type": "notify", "level": level, "body": body})


# ---------------------------------------------------------------------------------------------
# Monitor facade


class Guard:
    """What the Inference Monitor holds: sample() -> (memory block, source row), never raises."""

    def __init__(self, *, cfg: Optional[dict] = None, probes: Any = None, registry: Optional[Registry] = None,
                 notifier: Optional[Callable[[str, str], Any]] = None, clock: Optional[Callable[[], float]] = None,
                 watchdog: bool = True, level_file: Optional[LevelFile] = None):
        # level_file: when given, every sample's kernel reading is published there for sandboxed
        # mem-guard callers (the monitor's sampler passes LevelFile(); off by default).
        self.level_file = level_file
        self.cfg = cfg if cfg is not None else load_config()
        # background: the sampler never waits on ps, simctl, vm_stat or statvfs.
        self.probes = probes if probes is not None else Probes(background=True)
        self.registry = registry if registry is not None else Registry()
        self.watchdog = None
        self._tick_lock = threading.Lock()
        self._closed = False
        if watchdog:
            # Notifications run off the sampler thread (osascript can take up to 5 s).
            self.watchdog = Watchdog(clock, self.probes,
                                     BackgroundNotifier(notifier if notifier is not None else OsascriptNotifier()),
                                     self.registry, self.cfg)

    @staticmethod
    def _source(state: str, detail: str) -> dict:
        return {"id": "mem-guard", "label": "Memory guard", "state": state,
                "ageSeconds": 0.0 if state == "live" else None, "detail": detail}

    def sample(self, gpu_alloc_bytes: Optional[int] = None, models: Optional[list] = None) -> tuple[dict, dict]:
        try:
            state = self.probes.read(gpu_alloc_bytes)
            if self.level_file is not None:
                self.level_file.write(state)  # never raises; publishes sysctl readings only
            level, _reasons = classify(state, self.cfg)
            consumers = None
            if level != "ok":
                consumers = self.probes.consumers(models=models, gpu_alloc_bytes=state.get("gpuAllocBytes"))
            block = memory_block(state, self.cfg, consumers=consumers, paused=self.registry.paused(), models=models)
            if self.watchdog is not None:
                with self._tick_lock:
                    events = [] if self._closed else self.watchdog.tick(block)
                if any(event.get("type") in ("pause", "resume") for event in events):
                    block["paused"] = self.registry.paused()
                    block["suggestions"] = suggestions(block["consumers"], models, len(block["paused"]))
            if block["level"] == "unknown":
                return block, self._source("unavailable", "macOS memory pressure is unreadable")
            return block, self._source("live", f"Memory {block['level']}")
        except Exception:
            return unknown_block(), self._source("unavailable", "Memory probes failed")

    def close(self, timeout: float = 1.0) -> list[dict]:
        """Stop ticking and resume everything the watchdog paused; never raises. It does not wait
        for a tick in progress: the registry refuses to signal once stop_pausing() is set (checked
        under its lock just before SIGSTOP), so a pause racing this close either lands before the
        resume below, which then continues it, or not at all. `timeout` bounds the wait for the
        registry lock."""
        self._closed = True
        try:
            self.registry.stop_pausing(timeout)
        except Exception:
            pass
        try:
            return self.watchdog.close(timeout) if self.watchdog is not None else []
        except Exception:
            return []


# ---------------------------------------------------------------------------------------------
# Claude Code hook


HOOK_TRIGGER_SUBSTRINGS = ("lms", "simctl", "Simulator", "iphonesimulator", "xcodebuild", "docker", "npm",
                           "astra-review", "codex", "ollama", "mlx_lm", "Workflow", "Agent")
HOOK_NEEDS = {"model": (8 * GiB, "heavy"), "simulator": (4 * GiB, "heavy"), "build": (4 * GiB, "heavy"),
              "docker": (4 * GiB, "heavy"), "review": (1 * GiB, "light"), "agent": (1 * GiB, "light")}
_WRAPPERS = {"sudo", "env", "nohup", "time", "command", "exec", "nice", "caffeinate", "stdbuf", "then", "do",
             "else", "elif", "if", "while", "until", "!", "{", "}", "timeout", "gtimeout", "xargs"}
# Wrapper options that take a separate value (`nice -n 10 xcodebuild`): the value is skipped too.
_WRAPPER_VALUE_OPTIONS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-T", "-U"}, "env": {"-u", "-C", "-P", "-S"},
    "nice": {"-n"}, "timeout": {"-s", "-k"}, "gtimeout": {"-s", "-k"}, "caffeinate": {"-t", "-w"},
    "stdbuf": {"-i", "-o", "-e"}, "xargs": {"-n", "-I", "-L", "-P", "-s", "-E", "-J", "-R", "-S"}, "exec": {"-a"}}
# xcodebuild invocations that only print information and never build.
_XCODEBUILD_INFO = {"-version", "-list", "-showsdks", "-showBuildSettings", "-showdestinations", "-showTestPlans",
                    "-help", "-usage", "-h", "--help"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_OPERATOR_CHARS = set("();<>|&")
_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z_][A-Za-z0-9_.-]*))")
_WORD_BREAK = set(" \t\r\n;&|()<>")


def _shell_text(command: str) -> str:
    """The command without what bash never runs: comments (a '#' that starts an unquoted word, to
    the end of its line), heredoc bodies (up to the delimiter line) and backslash-newline line
    continuations. Quotes are kept for shlex; newlines still separate commands."""
    out: list[str] = []
    pending: list[tuple[str, bool]] = []  # heredoc delimiters whose bodies start after this line
    quote = ""  # "'", '"' or "$'"
    word_start = True
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            out.append(ch)
            i += 1
            if ch == "'":
                quote, word_start = "", False
            continue
        if quote:  # '"' or "$'": a backslash escapes the next character
            if ch == "\\" and i + 1 < n:
                if not (quote == '"' and command[i + 1] == "\n"):
                    out.append(command[i:i + 2])
                i += 2
                continue
            out.append(ch)
            i += 1
            if ch == quote[-1]:
                quote, word_start = "", False
            continue
        if ch == "\\" and i + 1 < n:
            if command[i + 1] != "\n":
                out.append(command[i:i + 2])
                word_start = False
            i += 2
            continue
        if ch == "#" and word_start:
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch == "<" and command.startswith("<<", i) and not command.startswith("<<<", i):
            match = _HEREDOC.match(command, i)
            if match:
                word = next(group for group in match.groups()[1:] if group is not None)
                pending.append((word, match.group(1) == "-"))
                out.append(" ; ")
                i = match.end()
                word_start = True
                continue
        if ch == "\n":
            out.append(ch)
            i += 1
            word_start = True
            for word, strip_tabs in pending:
                while i < n:
                    end = command.find("\n", i)
                    line = command[i:] if end < 0 else command[i:end]
                    i = n if end < 0 else end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == word:
                        break
            pending = []
            continue
        if ch in "'\"":
            quote = "$'" if ch == "'" and out and out[-1].endswith("$") else ch
            out.append(ch)
            i += 1
            word_start = False
            continue
        out.append(ch)
        i += 1
        word_start = ch in _WORD_BREAK
    return "".join(out)


def _simple_commands(command: str) -> Iterator[list[str]]:
    text = _shell_text(command).replace("\n", " ; ")
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""  # comments were removed above, the way bash reads them
        tokens = []
        for token in lexer:
            tokens.append(token)
            if len(tokens) > 4096:
                break
    except ValueError:
        tokens = re.sub(r"[;&|()]", " ; ", text).split()[:4096]
    current: list[str] = []
    for token in tokens:
        if token and all(ch in _OPERATOR_CHARS for ch in token):
            if current:
                yield current
            current = []
        else:
            current.append(token)
    if current:
        yield current


def _match_simple(tokens: list[str], depth: int) -> Optional[tuple[str, str]]:
    i = 0
    while i < len(tokens):
        word = tokens[i].lstrip("`$")
        if not word or _ASSIGN.match(word):
            i += 1
            continue
        base = word.rsplit("/", 1)[-1]
        if base in _WRAPPERS:
            i += 1
            takes_value = _WRAPPER_VALUE_OPTIONS.get(base, set())
            while i < len(tokens) and tokens[i].startswith("-"):
                option = tokens[i]
                i += 1
                if option == "--":
                    break
                if option in takes_value:
                    i += 1  # `nice -n 10`, `timeout -s KILL`, `sudo -u NAME`
            if base in ("timeout", "gtimeout") and i < len(tokens):
                i += 1
            continue
        break
    if i >= len(tokens):
        return None
    base = tokens[i].lstrip("`$").rstrip("`").rsplit("/", 1)[-1]
    args = [arg.rstrip("`") for arg in tokens[i + 1: i + 24]]
    plain = [arg for arg in args if not arg.startswith("-")]
    shown = _clean_text(" ".join([base] + args[:3]), 60)
    if base in _SHELLS and depth < 3:
        for index, arg in enumerate(args[:-1]):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
                return _match_command(args[index + 1], depth + 1)
        return None
    # Asking for help never starts heavy work (docker's -h is --hostname, so only --help there).
    if "--help" in args or ("-h" in args and base not in ("docker", "docker-compose")):
        return None
    if base == "xcodebuild" and any(arg in _XCODEBUILD_INFO for arg in args):
        return None  # -version, -list, -showsdks, -showBuildSettings ... print and exit
    if base == "lms" and (args[:1] == ["load"] or args[:2] == ["server", "start"]):
        return shown, "model"
    if (base == "xcrun" and args[:2] == ["simctl", "boot"]) or (base == "simctl" and args[:1] == ["boot"]):
        return shown, "simulator"
    if base == "open":
        for flag, value in zip(args, args[1:]):
            app = value.rstrip("/").rsplit("/", 1)[-1]
            if (flag == "-a" and app in ("Simulator", "Simulator.app")) or (
                    flag == "-b" and value == "com.apple.iphonesimulator"):
                return shown, "simulator"
        return None
    if base == "xcodebuild":
        return shown, "build"
    if (base == "docker" and (plain[:1] == ["run"] or plain[:2] == ["compose", "up"])) or (
            base == "docker-compose" and plain[:1] == ["up"]):
        return shown, "docker"
    if base == "npm" and plain[:1] == ["ci"]:
        return shown, "build"
    if base == "astra-review":
        return shown, "review"
    if base == "codex" and "exec" in plain[:3]:
        return shown, "review"
    if base == "ollama" and plain[:1] in (["run"], ["pull"]):
        return shown, "model"
    if base.startswith("mlx_lm") or (base.startswith("python") and any(a.startswith("mlx_lm") for a in args[:4])):
        return shown, "model"
    return None


def _match_command(command: Any, depth: int = 0) -> Optional[tuple[str, str]]:
    if not isinstance(command, str) or len(command) > 64 << 10:
        return None
    for tokens in _simple_commands(command):
        found = _match_simple(tokens, depth)
        if found is not None:
            return found
    return None


def hook_match(payload: Any) -> Optional[tuple[str, str]]:
    """(what, category) for a PreToolUse payload the hook decides on, else None."""
    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        return _match_command(tool_input.get("command"))
    if tool == "mcp__Claude_Code_iOS_Simulator__control" and tool_input.get("action") == "launch":
        return "iOS Simulator app launch", "simulator"
    if tool == "mcp__Claude_Code_iOS_Simulator__build":
        return "iOS Simulator build", "build"
    if tool in ("Workflow", "Agent"):
        return f"a {tool} run", "agent"
    return None


def hook_decide(payload: Any, probes: Any, cfg: Optional[dict] = None,
                gpu: Optional[Callable[[], Optional[int]]] = None,
                publish: Optional[Callable[[dict], Any]] = None) -> Optional[tuple[Decision, str, list]]:
    """None to allow silently, else (refused decision, what, consumers). The GPU allocation is
    read only on the refusal path, so the local LLM server ranks by what it really holds.
    `publish` (LevelFile.write) receives the reading: the hook runs outside the agent sandbox, so
    it can refresh level.json just before a sandboxed review asks mem-guard for admission."""
    match = hook_match(payload)
    if match is None:
        return None
    what, category = match
    need, kind = HOOK_NEEDS[category]
    state = probes.read()
    if publish is not None:
        with contextlib.suppress(Exception):
            publish(state)
    decision = admit(need, kind, state, cfg)
    # An unknown level never refuses from the hook: a hook must not wedge the session.
    if decision.allowed or decision.level == "unknown":
        return None
    gpu_bytes = None
    if gpu is not None:
        with contextlib.suppress(Exception):
            gpu_bytes = _bytes_or_none(gpu())
    consumers = probes.consumers(gpu_alloc_bytes=gpu_bytes) or []
    return decision, what, consumers


def hook_message(decision: Decision, what: str, consumers: list) -> str:
    tops = ", ".join(describe_consumer(row) for row in consumers[:3]) or "unknown (process list unreadable)"
    tips = suggestions(consumers)
    todo = tips[0] if tips else "quit or shut down something large you are not using"
    return (f"mem-guard refused `{what}` because {decision.reason}. Top consumers: {tops}. "
            f"What to do: {todo}, then retry; `mem-guard status` shows the details. Nothing was stopped or killed.")


# ---------------------------------------------------------------------------------------------
# CLI


def _default_gpu() -> Optional[int]:
    try:
        import gpu_probe
        gpu, _source = gpu_probe.mac_gpu()
        return _bytes_or_none((gpu or {}).get("allocatedBytes"))
    except Exception:
        return None


class Context:
    """Everything the CLI touches, injectable for tests."""

    def __init__(self, *, state_root: Optional[os.PathLike] = None, config_path: Optional[os.PathLike] = None,
                 cfg: Optional[dict] = None, probes: Any = None, registry: Optional[Registry] = None,
                 notifier: Optional[Callable[[str, str], Any]] = None, clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None, gpu: Optional[Callable[[], Optional[int]]] = None,
                 stdin: Any = None, stdout: Any = None, stderr: Any = None):
        self.state_root = Path(state_root) if state_root is not None else STATE_ROOT
        self._config_path = config_path
        self._cfg = cfg
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self._probes = probes
        self._registry = registry
        self._notifier = notifier
        self.gpu = gpu or _default_gpu
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr

    @property
    def cfg(self) -> dict:
        if self._cfg is None:
            self._cfg = load_config(self._config_path)
        return self._cfg

    @property
    def level_file(self) -> LevelFile:
        max_age = self.cfg.get("level_file_max_age_seconds", DEFAULTS["level_file_max_age_seconds"])
        return LevelFile(self.state_root / "level.json", max_age=max_age, clock=self.clock)

    @property
    def probes(self) -> Any:
        if self._probes is None:
            # The CLI may run inside an agent sandbox: fall back to the level file, then memory_pressure.
            self._probes = Probes(level_file=self.level_file, memory_pressure=True)
        return self._probes

    @property
    def registry(self) -> Registry:
        if self._registry is None:
            self._registry = Registry(self.state_root, clock=self.clock)
        return self._registry

    @property
    def notifier(self) -> Callable[[str, str], Any]:
        if self._notifier is None:
            self._notifier = OsascriptNotifier()
        return self._notifier

    @property
    def journal(self) -> Journal:
        return Journal(self.state_root / "events.jsonl", self.clock)

    def out(self, text: str) -> None:
        self.stdout.write(text + "\n")


def _read_stdin(stream: Any, limit: int) -> Optional[bytes]:
    source = getattr(stream, "buffer", stream)
    data = source.read(limit + 1)
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return None if len(data) > limit else data


def _status_text(block: dict) -> str:
    lines = [f"Memory: {block['level'].upper()}"]
    lines += [f"  - {reason}" for reason in block["reasons"]]
    pressure = block.get("pressure")
    numbers = [f"RAM {gb(block.get('ramBytes'))}",
               f"pressure {PRESSURE_LABEL.get(pressure, '?')}",
               f"available {block['availablePercent']}%" if block.get("availablePercent") is not None else "available ?",
               f"swap {gb(block.get('swapUsedBytes'))} of {gb(block.get('swapTotalBytes'))}",
               f"compressor {gb(block.get('compressedBytes'))}", f"wired {gb(block.get('wiredBytes'))}",
               f"VM volume free {gb(block.get('vmFreeBytes'))}"]
    if block.get("gpuAllocBytes") is not None:
        numbers.append(f"GPU allocation {gb(block['gpuAllocBytes'])}")
    lines.append(" | ".join(numbers))
    if block["consumers"]:
        lines.append("Consumers (approximate: shared pages count once per process):")
        lines += [f"  {describe_consumer(row)} in {row.get('processCount')} processes" for row in block["consumers"]]
    paused = block["paused"]
    lines.append("Paused jobs: " + (", ".join(f"{job['label']} (pid {job['pid']})" for job in paused) if paused else "none"))
    if block["suggestions"]:
        lines.append("Suggestions:")
        lines += [f"  - {tip}" for tip in block["suggestions"]]
    return "\n".join(lines)


def _resume_overdue(ctx: Context) -> list[dict]:
    """Every CLI entry point enforces the pause limit itself: jobs past their deadline, or whose
    pauser (the monitor or `watch`) is gone, are resumed. Best effort; never raises."""
    try:
        resumed = ctx.registry.resume_overdue()
    except Exception:
        return []
    if resumed:
        ctx.journal.write({"type": "resume", "why": "overdue", "jobs": [_job_summary(job) for job in resumed]})
    return resumed


def _read_and_publish(ctx: Context, gpu: Optional[int] = None) -> dict:
    """A probe read; a kernel (sysctl) reading is also published to level.json for callers inside
    agent sandboxes. Publishing is best effort and never raises (LevelFile.write)."""
    state = ctx.probes.read(gpu)
    ctx.level_file.write(state)  # writes only a state whose levelSource is 'sysctl'
    return state


def _cmd_status(ctx: Context, as_json: bool) -> int:
    gpu = ctx.gpu()
    state = _read_and_publish(ctx, gpu)
    level, _reasons = classify(state, ctx.cfg)
    consumers = ctx.probes.consumers(gpu_alloc_bytes=state.get("gpuAllocBytes")) if level != "ok" else None
    block = memory_block(state, ctx.cfg, consumers=consumers, paused=ctx.registry.paused())
    ctx.out(json.dumps(block, indent=2, allow_nan=False) if as_json else _status_text(block))
    return STATUS_EXIT.get(block["level"], 3)


def _source_note(state: dict) -> str:
    """Where the level came from, when it was not the kernel itself (inside an agent sandbox)."""
    source = state.get("levelSource") if isinstance(state, dict) else None
    if source == "level-file":
        age = _number_in(state.get("levelAgeSeconds"), 0, 86400)
        return f" [level from level.json{f', {age:.0f} s old' if age is not None else ''}; the sysctls are refused here]"
    if source == "memory_pressure":
        return " [availability from memory_pressure -Q only; the sysctls are refused here and level.json is not fresh]"
    return ""


def _cmd_admit(ctx: Context, need_gb: float, kind: Optional[str], wait: float, label: str) -> int:
    need = int(need_gb * GiB)
    deadline = ctx.clock() + wait
    while True:
        state = _read_and_publish(ctx)
        decision = admit(need, kind, state, ctx.cfg)
        if not decision.allowed and decision.kind == "heavy" and _swap_only_tight(state, ctx.cfg):
            # Even --wait 0 collects a second sample in this process. A previous status or a
            # sandbox level-file reading cannot establish a quiet trend. Missing evidence refuses.
            first = state
            first_at = ctx.clock()
            ctx.sleep(SWAP_CONFIRM_SECONDS)
            state = _read_and_publish(ctx)
            last_at = ctx.clock()
            evidence = SwapEvidence(first, state, last_at - first_at, ctx.clock() - last_at)
            decision = admit(need, kind, state, ctx.cfg, swap_evidence=evidence)
        if decision.allowed:
            ctx.out(f"admitted: {decision.reason}{_source_note(state)}")
            return 0
        remaining = deadline - ctx.clock()
        if remaining <= 0:
            break
        ctx.sleep(min(2.0, remaining))
    ctx.journal.write({"type": "refusal", "via": "admit", "label": label, "kind": decision.kind,
                       "needBytes": decision.needBytes, "level": decision.level, "reason": decision.reason})
    ctx.out(f"refused: {decision.reason}{_source_note(state)}")
    return EXIT_REFUSED


def _cmd_watch(ctx: Context, interval: float, ticks: int) -> int:
    watchdog = Watchdog(ctx.clock, ctx.probes, ctx.notifier, ctx.registry, ctx.cfg, journal=ctx.journal)

    def show(events: list[dict]) -> None:
        for event in events:
            ctx.out(json.dumps(event, allow_nan=False, default=str))

    show(watchdog.startup_events)
    count = 0
    try:
        while True:
            state = _read_and_publish(ctx)
            level, _reasons = classify(state, ctx.cfg)
            consumers = None
            if _RANK.get(level, 1) >= 2:
                state["gpuAllocBytes"] = _bytes_or_none(ctx.gpu())
                consumers = ctx.probes.consumers(gpu_alloc_bytes=state["gpuAllocBytes"])
            show(watchdog.tick(memory_block(state, ctx.cfg, consumers=consumers, paused=ctx.registry.paused())))
            count += 1
            if ticks and count >= ticks:
                break
            ctx.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        show(watchdog.close())
    return 0


def _cmd_hook(ctx: Context) -> int:
    try:
        raw = _read_stdin(ctx.stdin, HOOK_MAX)
        if raw is None:
            return 0
        payload = json.loads(raw.decode("utf-8"))
        if hook_match(payload) is None:
            return 0
        _resume_overdue(ctx)  # the slow path only: unrelated calls never touch the registry
        found = hook_decide(payload, ctx.probes, ctx.cfg, ctx.gpu, publish=ctx.level_file.write)
        if found is None:
            return 0
        decision, what, consumers = found
        ctx.journal.write({"type": "refusal", "via": "hook", "label": what, "kind": decision.kind,
                           "needBytes": decision.needBytes, "level": decision.level, "reason": decision.reason})
        ctx.stderr.write(hook_message(decision, what, consumers) + "\n")
        return 2
    except (Exception, KeyboardInterrupt):
        return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mem-guard", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="level, reasons, numbers, consumers, paused jobs")
    status.add_argument("--json", action="store_true")
    admit_p = sub.add_parser("admit", help="exit 0 admitted, 75 refused")
    admit_p.add_argument("--need-gb", type=float, required=True)
    admit_p.add_argument("--kind", choices=("light", "heavy"))
    admit_p.add_argument("--wait", type=float, default=0.0)
    admit_p.add_argument("--label", default="")
    register = sub.add_parser("register", help="make a job we started pausable")
    register.add_argument("--pid", type=int, required=True)
    register.add_argument("--label", required=True)
    register.add_argument("--json", action="store_true", help="print the entry (or the error) as JSON")
    unregister = sub.add_parser("unregister", help="forget a job (resumes it first if paused)")
    unregister.add_argument("--pid", type=int, required=True)
    sub.add_parser("resume-all", help="SIGCONT everything mem-guard paused")
    sub.add_parser("resume-overdue", help="SIGCONT paused jobs past their limit or whose pauser is gone")
    watch = sub.add_parser("watch", help="foreground watchdog loop")
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument("--ticks", type=int, default=0, help=argparse.SUPPRESS)
    sub.add_parser("hook", help="Claude Code PreToolUse hook (reads JSON on stdin)")
    return parser


def main(argv: Optional[list[str]] = None, ctx: Optional[Context] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ctx = ctx or Context()
    if argv[:1] == ["hook"]:
        return _cmd_hook(ctx)  # before argparse: a hook must never exit 2 on a usage error
    args = _parser().parse_args(argv)
    if args.command in ("status", "admit", "register", "unregister", "resume-overdue"):
        resumed = _resume_overdue(ctx)
        if args.command == "resume-overdue":
            ctx.out(f"resumed {len(resumed)} overdue paused job{'s' if len(resumed) != 1 else ''}")
            return 0
    if args.command == "status":
        return _cmd_status(ctx, args.json)
    if args.command == "admit":
        if not 0 <= args.need_gb <= 4096 or not 0 <= args.wait <= 86400 or not math.isfinite(args.need_gb):
            ctx.stderr.write("mem-guard: --need-gb must be 0..4096 and --wait 0..86400\n")
            return 2
        return _cmd_admit(ctx, args.need_gb, args.kind, args.wait, _clean_text(args.label, 80))
    if args.command == "register":
        try:
            entry = ctx.registry.register(args.pid, args.label)
        except (RegistryError, OSError) as error:
            ctx.stderr.write(f"mem-guard: register failed: {error}\n")
            if args.json:
                ctx.out(json.dumps({"ok": False, "error": _clean_text(str(error), 300)}))
            return 1
        if args.json:
            ctx.out(json.dumps({"ok": True, "pid": entry["pid"], "startTime": entry["startTime"],
                                "comm": entry["comm"], "label": entry["label"]}))
        else:
            ctx.out(f"registered pid {entry['pid']} ({entry['label']})")
        return 0
    if args.command == "unregister":
        try:
            removed = ctx.registry.unregister(args.pid)
        except (RegistryError, OSError) as error:
            ctx.stderr.write(f"mem-guard: unregister failed: {error}\n")
            return 1
        ctx.out(f"unregistered pid {args.pid}" if removed else f"pid {args.pid} was not registered")
        return 0
    if args.command == "resume-all":
        try:
            resumed = ctx.registry.resume()
        except (RegistryError, OSError) as error:
            ctx.stderr.write(f"mem-guard: resume failed: {error}\n")
            return 1
        if resumed:
            ctx.journal.write({"type": "resume", "why": "manual", "jobs": [_job_summary(job) for job in resumed]})
        ctx.out(f"resumed {len(resumed)} paused job{'s' if len(resumed) != 1 else ''}")
        stuck = [pid for job in resumed for pid in job.get("failed") or []]
        if stuck:
            ctx.stderr.write(f"mem-guard: could not continue {len(stuck)} process{'es' if len(stuck) != 1 else ''} "
                             f"({', '.join(map(str, stuck[:10]))}); they stay recorded, run resume-all again\n")
            return 1
        return 0
    if args.command == "watch":
        return _cmd_watch(ctx, max(0.5, min(args.interval, 3600.0)), max(0, args.ticks))
    return 2


if __name__ == "__main__":
    sys.exit(main())
