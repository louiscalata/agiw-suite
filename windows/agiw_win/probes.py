"""Bounded, read-only probes for the Windows edition.

Every probe returns plain data plus a ``sources`` row and never raises into the
sampler. Network probes ignore machine proxy settings and refuse redirects, so a
lane or peer cannot turn a status read into an external fetch. Subprocesses run
with CREATE_NO_WINDOW so a windowless observer never flashes a console.
"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

HTTP_TIMEOUT = 0.7
HTTP_MAX_BYTES = 256_000
MAX_SLOTS = 32
QUEUE_MAX_ENTRIES = 1000
STALE_CLAIM_SECONDS = 900.0
STALE_PENDING_SECONDS = 86400.0
SLOTS_TIMEOUT = 4.0

DEFAULT_SHARE_ROOT = Path(r"C:\SharedChami")
DEFAULT_LANES = (
    {"id": "fast", "port": 1235, "alias": "openai/gpt-oss-20b", "device": "CUDA0"},
    {"id": "deep", "port": 1234, "alias": "qwen3.8-27b", "device": "none"},
)

Fetch = Callable[[str, float, int], Any]


# --------------------------------------------------------------------- helpers
def _string(value: Any, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(c for c in value if c.isprintable()).strip()
    return cleaned[:limit] or None


def _number(value: Any, low: float = -1e12, high: float = 1e12) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if low <= value <= high else None


def _source(source_id: str, label: str, state: str, detail: str, age: float | None = 0.0) -> dict[str, Any]:
    return {"id": source_id, "label": label, "state": state,
            "ageSeconds": age if state == "live" else None, "detail": detail}


def http_get_json(url: str, timeout: float = HTTP_TIMEOUT, max_bytes: int = HTTP_MAX_BYTES) -> Any:
    """GET a fixed URL with a byte cap and a total deadline; JSON when it parses, else a short text prefix.

    The socket timeout bounds each read; the deadline bounds the whole response, so a peer that
    trickles one byte per timeout cannot hold a poller (it fails at 2 x timeout).
    """
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError("only plain http URLs are probed")
    if parts.username is not None or parts.password is not None:
        raise ValueError("credentials in probe URL refused")
    port = parts.port or 80
    conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
    expired = threading.Event()

    def expire() -> None:
        # A blocking recv cannot see a deadline; shutting the socket down makes it return at once,
        # whether the peer is stalling the headers or trickling the body.
        expired.set()
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(2 * timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn.request("GET", target, headers={"Accept": "application/json", "Host": f"{host}:{port}"})
        response = conn.getresponse()  # http.client never follows redirects
        if response.status >= 300:
            raise OSError(f"HTTP Error {response.status}")
        body = response.read(max_bytes + 1)
    except (OSError, http.client.HTTPException) as error:
        if expired.is_set():
            raise TimeoutError("HTTP response exceeded its total deadline") from error
        raise
    finally:
        timer.cancel()
        conn.close()
    if expired.is_set():
        raise TimeoutError("HTTP response exceeded its total deadline")
    if len(body) > max_bytes:
        raise ValueError("oversized HTTP response")
    raw = body.decode("utf-8", errors="replace")
    try:
        return loads_strict(raw)
    except (json.JSONDecodeError, ValueError):
        return raw[:256]


def read_json(path: Path, max_bytes: int = 256_000) -> Any:
    """A capped JSON read that refuses links and oversized files (utf-8 with or without BOM)."""
    if path.is_symlink():
        raise ValueError("linked file refused")
    if path.stat().st_size > max_bytes:
        raise ValueError("oversized file")
    with path.open("rb") as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError("oversized file")
    return loads_strict(payload.decode("utf-8-sig"))


def _reject_constant(token: str):
    raise ValueError(f"non-finite JSON number {token}")


def loads_strict(text: str) -> Any:
    """json.loads that refuses NaN/Infinity and turns pathological nesting into ValueError."""
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except RecursionError as error:
        raise ValueError("JSON nested too deeply") from error


def run_quiet(args: list[str], timeout: float) -> str:
    """Run a fixed command without a console window; stdout text or an exception."""
    completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                               encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise RuntimeError(f"exit {completed.returncode}")
    return completed.stdout


# ----------------------------------------------------------------- lane config
def lane_specs(share_root: Path = DEFAULT_SHARE_ROOT) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The declared lanes from windows-llm-pipeline/config.json runtime.lanes, else the built-in layout."""
    path = share_root / "windows-llm-pipeline" / "config.json"
    try:
        config = read_json(path)
        runtime = config.get("runtime") if isinstance(config, dict) else None
        raw = runtime.get("lanes") if isinstance(runtime, dict) else None
        if not isinstance(raw, dict):
            raise ValueError("runtime.lanes missing")
        specs = []
        missing = []
        for lane_id in ("fast", "deep"):
            lane = raw.get(lane_id)
            if not isinstance(lane, dict):
                missing.append(lane_id)
                continue
            port, alias = lane.get("port"), _string(lane.get("alias"), 80)
            if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 and alias:
                specs.append({"id": lane_id, "port": port, "alias": alias,
                              "device": _string(lane.get("device"), 32) or "unknown",
                              "context": _number(lane.get("context"), 1, 1 << 24),
                              "parallel": _number(lane.get("parallel"), 1, 256)})
            else:
                missing.append(lane_id)
        if specs:
            layout_block = runtime.get("lane_layout")
            layout = _string(layout_block.get("name"), 64) if isinstance(layout_block, dict) else None
            for lane_id in missing:
                # Never drop a required lane silently: it stays on the map as an explicit unknown row.
                default = next(d for d in DEFAULT_LANES if d["id"] == lane_id)
                specs.append({**default, "undeclared": True})
            specs.sort(key=lambda spec: ("fast", "deep").index(spec["id"]))
            detail = f"Declared in windows-llm-pipeline/config.json{f' · layout {layout}' if layout else ''}"
            if missing:
                return specs, _source("lane-config", "Lane layout", "error",
                                      detail + f" · {', '.join(missing)} lane missing or malformed (shown as unknown, not probed)")
            return specs, _source("lane-config", "Lane layout", "live", detail)
        raise ValueError("no usable lanes")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
        return [dict(lane) for lane in DEFAULT_LANES], _source(
            "lane-config", "Lane layout", "unavailable",
            "Pipeline config unreadable; using the built-in fast :1235 / deep :1234 layout")


# ------------------------------------------------------------------ lane probe
def _model_ids(payload: Any) -> list[str | None]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [item.get("id") if isinstance(item, dict) and isinstance(item.get("id"), str) else None
            for item in data[:33]]


def _slot(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("is_processing"), bool):
        return None
    task = raw.get("id_task")
    slot_id = raw.get("id")
    slot = {"id": slot_id if isinstance(slot_id, int) and not isinstance(slot_id, bool) and 0 <= slot_id <= 4096 else None,
            "isProcessing": raw["is_processing"],
            "nCtx": _number(raw.get("n_ctx"), 0, 1 << 24),
            "taskId": task if isinstance(task, int) and not isinstance(task, bool) and task >= 0 else None}
    if slot["isProcessing"]:
        next_token = raw.get("next_token")
        current = next_token[0] if isinstance(next_token, list) and next_token and isinstance(next_token[0], dict) else {}
        decoded = current.get("n_decoded") if "n_decoded" in current else raw.get("n_decoded")
        slot["decodedTokens"] = _number(decoded, 0, 1 << 30)
    return slot


def probe_lane(spec: dict[str, Any], fetch: Fetch = http_get_json, host: str = "127.0.0.1",
               slots_timeout: float = SLOTS_TIMEOUT) -> dict[str, Any]:
    """One llama-server lane: health, served identity (must equal the declared alias) and slot activity.

    status: unreachable | loading | identity_mismatch | no_identity | slots_unknown | idle | busy.
    Activity is claimed only from a complete, well-formed slot list on a lane serving
    exactly the declared model, because llama-server ignores the request's model field.
    """
    endpoint = f"http://{host}:{spec['port']}"
    lane = {"id": spec["id"], "port": spec["port"], "endpoint": endpoint, "expectedModel": spec["alias"],
            "servedModel": None, "status": "unknown", "slots": [], "slotsBusy": None, "slotsTotal": None,
            "phase": None, "detail": None, "device": spec.get("device")}
    if spec.get("undeclared"):
        lane["status"], lane["detail"] = "undeclared", "Lane missing from windows-llm-pipeline/config.json; not probed"
        return lane
    try:
        health = fetch(endpoint + "/health", HTTP_TIMEOUT, HTTP_MAX_BYTES)
    except Exception as error:  # noqa: BLE001 - any failure is "unreachable"
        loading = "503" in str(error)
        lane["status"] = "loading" if loading else "unreachable"
        lane["detail"] = "Model loading (health 503)" if loading else "No answer on the lane port"
        return lane
    if isinstance(health, dict) and health.get("status") not in (None, "ok"):
        lane["status"], lane["detail"] = "loading", f"Health reports {_string(health.get('status'), 40)}"
        return lane
    try:
        models = _model_ids(fetch(endpoint + "/v1/models", HTTP_TIMEOUT, HTTP_MAX_BYTES))
    except Exception:  # noqa: BLE001
        lane["status"], lane["detail"] = "no_identity", "Served model identity unavailable"
        return lane
    lane["servedModel"] = _string(models[0], 120) if models else None
    if not models:
        lane["status"], lane["detail"] = "no_identity", "Lane returned no model identity"
        return lane
    if len(models) != 1 or models[0] != spec["alias"]:
        lane["status"] = "identity_mismatch"
        lane["detail"] = f"Serves {lane['servedModel'] or 'an unnamed model'}, declared {spec['alias']}"
        return lane
    try:
        # /slots is answered through the task queue between decode batches, so a busy CPU lane
        # needs far longer than /health; lanes are polled on their own threads for this reason.
        payload = fetch(endpoint + "/slots", slots_timeout, HTTP_MAX_BYTES)
    except Exception:  # noqa: BLE001
        lane["status"], lane["detail"] = "slots_unknown", "Slot endpoint unavailable (start llama-server with --slots)"
        return lane
    raw_slots = payload.get("slots") if isinstance(payload, dict) else payload
    if not isinstance(raw_slots, list) or not raw_slots or len(raw_slots) > MAX_SLOTS:
        lane["status"], lane["detail"] = "slots_unknown", "Slot list malformed or too long"
        return lane
    slots = [_slot(raw) for raw in raw_slots]
    ids = [slot["id"] if slot else None for slot in slots]
    # Complete evidence only: every slot has a distinct integer id, so none can hide behind a duplicate.
    if any(slot is None for slot in slots) or any(not isinstance(i, int) or isinstance(i, bool) for i in ids) \
            or len(set(ids)) != len(ids):
        lane["status"], lane["detail"] = "slots_unknown", "Slot state incomplete or ambiguous"
        return lane
    busy = [slot for slot in slots if slot["isProcessing"]]
    lane.update(slots=slots, slotsBusy=len(busy), slotsTotal=len(slots),
                status="busy" if busy else "idle")
    if busy:
        # Decoded tokens > 0 on any busy slot means output is being generated; otherwise
        # the lane is still evaluating a prompt (the Mac edition's "busy" phase).
        lane["phase"] = "generating" if any((slot.get("decodedTokens") or 0) > 0 for slot in busy) else "busy"
    else:
        lane["phase"] = "idle"
    return lane


LANE_UP = {"idle", "busy", "slots_unknown"}


def lane_model_row(lane: dict[str, Any], spec: dict[str, Any], now: float) -> dict[str, Any]:
    """The dashboard's model row for one lane (host windows, source llama-slots)."""
    status = lane["status"]
    if status in ("idle", "busy"):
        state, loaded, source = lane["phase"], True, "llama-slots"
    elif status == "slots_unknown":
        state, loaded, source = "loaded", True, "llama-models"
    elif status == "loading":
        state, loaded, source = "unknown", None, "llama-health"
    elif status == "identity_mismatch":
        state, loaded, source = "unknown", None, "llama-models"
    else:
        # No answer is no evidence about residency: a hung or restarting server may still hold the model.
        state, loaded, source = "unknown", None, "llama-health"
    total = lane["slotsTotal"]
    context = None
    if lane["slots"]:
        contexts = [slot["nCtx"] for slot in lane["slots"] if isinstance(slot.get("nCtx"), (int, float))]
        context = int(sum(contexts)) if contexts else None
    return {
        "id": spec["alias"], "name": spec["alias"].split("/")[-1], "host": "windows",
        "state": state, "loaded": loaded, "queued": None,
        "parallel": total if total else spec.get("parallel"),
        "context": context or spec.get("context"), "sizeBytes": None,
        "source": source, "ageSeconds": 0.0, "role": f"{spec['id']} lane",
        "metadata": {"lane": spec["id"], "port": spec["port"], "device": spec.get("device"),
                     "laneStatus": status, "servedModel": lane["servedModel"],
                     "slotsBusy": lane["slotsBusy"], "slotsTotal": total, "detail": lane["detail"]},
        "modelKey": None, "loadedInstanceIds": None, "instanceId": None,
        "observedAt": now,
    }


# ---------------------------------------------------------------------- memory
class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _memory_status() -> dict[str, int]:
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        raise OSError("GlobalMemoryStatusEx failed")
    return {"totalPhys": status.ullTotalPhys, "availPhys": status.ullAvailPhys,
            "commitLimit": status.ullTotalPageFile, "commitAvail": status.ullAvailPageFile}


# Fixed thresholds on available physical memory. Windows has no kernel "pressure"
# signal like macOS, so commit charge is reported beside it as a second reason.
MEMORY_LEVELS = ((20.0, "ok"), (12.0, "watch"), (6.0, "tight"), (0.0, "critical"))


def memory_level(available_percent: float, commit_percent: float | None) -> str:
    level = next(name for floor, name in MEMORY_LEVELS if available_percent >= floor)
    # A nearly exhausted commit limit fails allocations even with free RAM.
    if commit_percent is not None and commit_percent >= 95 and level in ("ok", "watch"):
        level = "tight"
    return level


def memory_block(status_fn: Callable[[], dict[str, int]] | None = None,
                 consumers_fn: Callable[[], list[dict[str, Any]]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """snapshot.memory in the shape the dashboard's memoryView reads, and its source row."""
    try:
        raw = (status_fn or _memory_status)()
        total, avail = raw["totalPhys"], raw["availPhys"]
        if not (isinstance(total, int) and total > 0 and isinstance(avail, int) and 0 <= avail <= total):
            raise ValueError("implausible memory reading")
        available = round(avail * 100 / total, 1)
        limit, commit_avail = raw.get("commitLimit"), raw.get("commitAvail")
        commit = (round((limit - commit_avail) * 100 / limit, 1)
                  if isinstance(limit, int) and limit > 0 and isinstance(commit_avail, int) and 0 <= commit_avail <= limit else None)
        level = memory_level(available, commit)
        gib = 2 ** 30
        reasons = [f"{available:.0f}% of {total / gib:.0f} GB RAM available"]
        if commit is not None:
            reasons.append(f"commit charge {commit:.0f}% of limit")
        consumers: list[dict[str, Any]] = []
        if level != "ok":
            try:
                consumers = (consumers_fn or top_processes)()
            except Exception:  # noqa: BLE001 - consumers are optional detail
                consumers = []
        suggestions = []
        if level in ("tight", "critical"):
            suggestions.append("Close apps you are not using before loading another model")
        block = {"level": level, "availablePercent": available, "totalBytes": total, "availableBytes": avail,
                 "commitPercent": commit, "swapUsedBytes": None, "compressedBytes": None, "gpuAllocBytes": None,
                 "consumers": consumers, "suggestions": suggestions, "reasons": reasons, "paused": []}
        return block, _source("mem-guard", "Memory", "live", "Windows physical memory and commit charge sampled")
    except Exception:  # noqa: BLE001
        return ({"level": "unknown", "consumers": [], "suggestions": [], "reasons": [], "paused": []},
                _source("mem-guard", "Memory", "unavailable", "Windows memory status unreadable"))


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def top_processes(limit: int = 3, max_pids: int = 4096) -> list[dict[str, Any]]:
    """Largest working sets grouped by executable name; names and sizes only, never arguments."""
    if not IS_WINDOWS:
        return []
    psapi, kernel32 = ctypes.windll.psapi, ctypes.windll.kernel32  # type: ignore[attr-defined]
    pids = (ctypes.c_ulong * max_pids)()
    needed = ctypes.c_ulong()
    if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    groups: dict[str, list[int]] = {}
    query = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
    for pid in pids[:needed.value // ctypes.sizeof(ctypes.c_ulong)]:
        if not pid:
            continue
        handle = kernel32.OpenProcess(query, False, pid)
        if not handle:
            continue
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                continue
            size = ctypes.c_ulong(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            name = (Path(buffer.value).name if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
                    else f"pid {pid}")
            group = groups.setdefault(name[:80], [0, 0])
            group[0] += int(counters.WorkingSetSize)
            group[1] += 1
        finally:
            kernel32.CloseHandle(handle)
    ranked = sorted(groups.items(), key=lambda item: item[1][0], reverse=True)[:limit]
    return [{"label": label, "residentBytes": size, "processCount": count, "gpuAllocBytes": None}
            for label, (size, count) in ranked]


# ------------------------------------------------------------------------ GPUs
_NVIDIA_QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"


def nvidia_smi_path() -> str | None:
    """Only run the driver copy, never a user-writable PATH replacement."""
    for candidate in (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe",
                      Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def parse_nvidia_csv(text: str) -> list[dict[str, Any]]:
    """nvidia-smi --format=csv,noheader,nounits rows -> pcGpuView rows; one bad row voids the sample."""
    gpus = []
    for line in text.strip().splitlines()[:4]:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 7:
            raise ValueError("unexpected nvidia-smi columns")
        index, name, util, used, total, temp, power = parts
        def number(value: str) -> float | None:
            if value in ("[N/A]", "N/A", "[Not Supported]"):
                return None
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError("non-finite reading")
            return parsed
        row = {"index": int(index), "name": name[:64], "utilizationPercent": number(util),
               "memoryUsedMiB": number(used), "memoryTotalMiB": number(total),
               "temperatureC": number(temp), "powerW": number(power),
               "vendor": "nvidia"}
        limits = (("utilizationPercent", 0, 100), ("memoryUsedMiB", 0, 1048576), ("memoryTotalMiB", 1, 1048576),
                  ("temperatureC", 0, 150), ("powerW", 0, 2000))
        if row["memoryTotalMiB"] is None or row["memoryUsedMiB"] is None:
            raise ValueError("memory not reported")
        if any(row[key] is not None and not low <= row[key] <= high for key, low, high in limits) \
                or row["memoryUsedMiB"] > row["memoryTotalMiB"]:
            raise ValueError("implausible GPU reading")
        gpus.append(row)
    return gpus


def nvidia_gpus(runner: Callable[[list[str], float], str] = run_quiet) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = nvidia_smi_path() if runner is run_quiet else "nvidia-smi"
    if not path:
        return [], _source("pc-gpu", "PC GPU", "unavailable", "nvidia-smi not found")
    try:
        text = runner([path, f"--query-gpu={_NVIDIA_QUERY}", "--format=csv,noheader,nounits"], 2.5)
        gpus = parse_nvidia_csv(text)
        return gpus, _source("pc-gpu", "PC GPU", "live",
                             "NVIDIA load sampled with nvidia-smi; other adapters are listed without load")
    except Exception:  # noqa: BLE001
        return [], _source("pc-gpu", "PC GPU", "error", "nvidia-smi failed or returned an unexpected reading")


_ADAPTER_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def display_adapters() -> list[dict[str, Any]]:
    """Every display adapter the driver registry lists, with dedicated VRAM when recorded.

    nvidia-smi and Win32_VideoController each see only part of a mixed NVIDIA + AMD box;
    the class key lists both cards. Names and sizes only.
    """
    if not IS_WINDOWS:
        return []
    import winreg  # noqa: PLC0415 - Windows only
    adapters = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ADAPTER_CLASS) as root:
        for index in range(32):
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            if not sub.isdigit():
                continue
            try:
                with winreg.OpenKey(root, sub) as key:
                    name = winreg.QueryValueEx(key, "DriverDesc")[0]
                    try:
                        vram = winreg.QueryValueEx(key, "HardwareInformation.qwMemorySize")[0]
                    except OSError:
                        vram = None
            except OSError:
                continue
            label = _string(name, 64)
            if not label or "basic display" in label.lower() or "remote" in label.lower():
                continue
            size = int.from_bytes(vram, "little") if isinstance(vram, bytes) else vram if isinstance(vram, int) else None
            adapters.append({"name": label, "memoryBytes": size if isinstance(size, int) and size > 0 else None})
    seen, unique = set(), []
    for adapter in adapters:
        if adapter["name"] not in seen:
            seen.add(adapter["name"])
            unique.append(adapter)
    return unique


# ------------------------------------------------------------------ route queue
def route_queue(root: Path, now: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Counts of the jev/nisi online-code route queue, with claims that have outlived any run."""
    now = time.time() if now is None else now
    counts: dict[str, int] = {}
    oldest_pending = None
    stale_claims = 0
    stale_pending = 0
    if not root.is_dir():
        return ({"status": None, "runId": None, "pipelines": [], "queue": None},
                _source("route-queue", "Route queue", "unavailable", f"Route queue folder not found ({root.name})"))
    try:
        for state in ("pending", "claimed", "completed"):
            directory = root / state
            total = 0
            if directory.is_dir():
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if total >= QUEUE_MAX_ENTRIES:
                            break
                        if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                            continue
                        total += 1
                        age = now - entry.stat(follow_symlinks=False).st_mtime
                        if state == "pending":
                            oldest_pending = age if oldest_pending is None else max(oldest_pending, age)
                            if age > STALE_PENDING_SECONDS:
                                stale_pending += 1
                        elif state == "claimed" and age > STALE_CLAIM_SECONDS:
                            stale_claims += 1
            counts[state] = total
    except OSError:
        return ({"status": None, "runId": None, "pipelines": [], "queue": None},
                _source("route-queue", "Route queue", "unavailable", "Route queue unreadable"))
    pending = counts.get("pending", 0)
    claimed_live = counts.get("claimed", 0) - stale_claims
    # A job nobody has picked up for a day is a leftover, not work waiting for a lane.
    pending_live = pending - stale_pending
    # A claim file says a worker took a job, not that it is still executing (it may have crashed), so
    # recent claims read "claimed" (execution unknown) and never "running".
    status = "claimed" if claimed_live > 0 else "queued" if pending_live else "idle"
    pipeline = {"status": status, "stage": None, "runId": None, "pipelines": [],
                "queue": {"pending": pending, "stalePending": stale_pending, "claimed": counts.get("claimed", 0), "staleClaimed": stale_claims,
                          "completed": counts.get("completed", 0),
                          "oldestPendingSeconds": round(oldest_pending, 1) if oldest_pending is not None else None,
                          "staleAfterSeconds": STALE_CLAIM_SECONDS}}
    detail = f"{pending} pending · {counts.get('claimed', 0)} claimed · {counts.get('completed', 0)} completed"
    if stale_claims:
        detail += f" · {stale_claims} claim{'s' if stale_claims != 1 else ''} older than 15 min (likely abandoned)"
    if stale_pending:
        detail += (f" · {stale_pending} pending untouched for over a day (stale, not queued; oldest "
                   f"{oldest_pending / 86400:.0f} d)")
    if claimed_live > 0:
        detail += f" · {claimed_live} recent claim{'s' if claimed_live != 1 else ''}; execution unknown"
    return pipeline, _source("route-queue", "Route queue", "live", detail)


# ------------------------------------------------------------------- share
def share_health(share_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    lab = share_root / "llm-lab"
    try:
        present = share_root.is_dir() and any(share_root.iterdir())
    except OSError:
        present = False
    config_ok = False
    if present:
        try:
            config_ok = isinstance(read_json(lab / "config.json", 1_000_000), dict)
        except (OSError, ValueError, json.JSONDecodeError):
            config_ok = False
    state = "ready" if present and config_ok else "degraded" if present else "missing"
    detail = {"ready": f"{share_root} present; llm-lab/config.json readable",
              "degraded": f"{share_root} present but llm-lab/config.json is unreadable",
              "missing": f"{share_root} is missing or empty; this PC serves the share"}[state]
    return ({"state": state, "root": str(share_root), "detail": detail},
            _source("sharedchami", "SharedChami", "live" if state == "ready" else "error", detail))


# ---------------------------------------------------------------- components
def nisi_component(lanes: list[dict[str, Any]], bin_root: Path) -> dict[str, Any]:
    """Nisi readiness on this PC: the gate adapter installed and both lanes answering as declared."""
    gate = bin_root / "nisi-gate-v0.2" / "nisi_gate.mjs"
    package = bin_root / "nisi-v0.2" / "node_modules" / "nisi" / "package.json"
    version = None
    try:
        version = _string(read_json(package).get("version"), 40)
    except (OSError, ValueError, AttributeError, json.JSONDecodeError):
        version = None
    up = [lane for lane in lanes if lane["status"] in ("idle", "busy")]
    mismatched = [lane["id"] for lane in lanes if lane["status"] == "identity_mismatch"]
    if not gate.is_file():
        return {"id": "nisi", "label": "Nisi gate", "state": "unknown",
                "detail": "Nisi v0.2 gate adapter not found in the bin folder", "version": version}
    if mismatched:
        return {"id": "nisi", "label": "Nisi gate", "state": "needs-action", "version": version,
                "detail": f"Lane identity mismatch on {', '.join(mismatched)}; the gate would review with the wrong model"}
    if len(up) == len(lanes) and len(lanes) >= 2:
        names = {lane["id"]: lane["expectedModel"] for lane in up}
        return {"id": "nisi", "label": "Nisi gate", "state": "ready", "version": version,
                "authorModel": names.get("deep"), "reviewerModel": names.get("fast"),
                "detail": f"Gate installed{f' (nisi {version})' if version else ''}; author {names.get('deep')} + reviewer {names.get('fast')} answering"}
    if up:
        return {"id": "nisi", "label": "Nisi gate", "state": "partial", "version": version,
                "detail": f"Only the {up[0]['id']} lane answers; the gate needs both"}
    return {"id": "nisi", "label": "Nisi gate", "state": "partial", "version": version,
            "detail": "Gate installed; no lane answers"}


# ------------------------------------------------------------------- Mac peer
def mac_hosts(share_root: Path) -> dict[str, Any] | None:
    try:
        data = read_json(share_root / "llm-lab" / "mac-reach" / "hosts.json")
        mac = data.get("mac") if isinstance(data, dict) else None
        if not isinstance(mac, dict):
            return None
        own = (data.get("windows") or {}).get("ip") if isinstance(data.get("windows"), dict) else None
        port = mac.get("lm_studio_port")
        return {"mdns": _string(mac.get("mdns"), 120), "hostname": _string(mac.get("hostname"), 80),
                "ips": [ip for ip in mac.get("ips", []) if isinstance(ip, str)][:4],
                "port": port if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535 else 1234,
                "expectedVerify": _string(mac.get("expected_verify_model"), 80), "ownIp": _string(own, 64)}
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _own_addresses() -> set[str]:
    addresses = {"127.0.0.1", "0.0.0.0", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addresses.add(info[4][0])
    except OSError:
        pass
    return addresses


def candidate_addresses(hosts: dict[str, Any], resolve: Callable[[str], list[str]],
                        own: set[str] | None = None) -> list[tuple[str, str]]:
    """(address, via) in probe order: mDNS name first, then the recorded fallbacks; never this machine.
    ``own`` defaults to this machine's addresses; tests pass their own set so results do not depend on the host."""
    own = (_own_addresses() if own is None else set(own)) | ({hosts["ownIp"]} if hosts.get("ownIp") else set())
    ordered: list[tuple[str, str]] = []
    if hosts.get("mdns"):
        try:
            for address in resolve(hosts["mdns"]):
                ordered.append((address, "mdns"))
        except OSError:
            pass
    ordered += [(ip, "recorded-ip") for ip in hosts.get("ips", [])]
    seen, result = set(), []
    for address, via in ordered:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue  # names such as "localhost" are refused: only literal addresses are probed
        if (parsed.version != 4 or parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast
                or parsed.is_link_local or not parsed.is_private or address in own or address in seen):
            continue
        seen.add(address)
        result.append((address, via))
    return result


def _resolve_ipv4(name: str) -> list[str]:
    return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(name, None, socket.AF_INET)))


def parse_mac_models(payload: Any, v0: bool) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("model list missing")
    models = []
    for item in data[:64]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        if v0:
            if item.get("type") not in (None, "llm", "vlm"):
                continue
            state = {"loaded": "loaded", "not-loaded": "not-loaded"}.get(item.get("state"), "unknown")
        else:
            state = "listed"
        models.append({"id": item["id"][:120], "state": state})
    return models


def probe_mac(hosts: dict[str, Any] | None, fetch: Fetch = http_get_json,
              resolve: Callable[[str], list[str]] = _resolve_ipv4, clock: Callable[[], float] = time.time,
              own: set[str] | None = None) -> dict[str, Any]:
    """Reach the Mac's LM Studio over the LAN: LM Studio's REST list first (has loaded state), then /v1/models."""
    observed = clock()
    if not hosts:
        return {"state": "unknown", "detail": "llm-lab/mac-reach/hosts.json unreadable", "observedAt": observed,
                "address": None, "via": None, "latencyMs": None, "models": [], "loadedCount": None, "tried": []}
    tried = []
    reasons: set[str] = set()
    for address, via in candidate_addresses(hosts, resolve, own):
        base = f"http://{address}:{hosts['port']}"
        started = time.monotonic()
        for path, v0 in (("/api/v0/models", True), ("/v1/models", False)):
            try:
                models = parse_mac_models(fetch(base + path, 1.5, HTTP_MAX_BYTES), v0)
            except ConnectionRefusedError:
                reasons.add("refused")  # the address answered with a TCP refusal; the cause is not known
                continue
            except (TimeoutError, socket.timeout):  # distinct classes before Python 3.10
                reasons.add("timeout")
                continue
            except (ValueError, json.JSONDecodeError):
                reasons.add("invalid")  # something answered, but not with a model listing
                continue
            except Exception as error:  # noqa: BLE001
                reasons.add("invalid" if str(error).startswith("HTTP Error") else "error")
                continue
            latency = round((time.monotonic() - started) * 1000)
            loaded = [m for m in models if m["state"] == "loaded"]
            unknown = sum(m["state"] == "unknown" for m in models)
            # Only a listing with every residency reported proves a total; an empty listing proves 0.
            complete = v0 and not unknown
            expected = hosts.get("expectedVerify")
            expected_loaded = None
            if v0 and expected:
                if any(m["id"] == expected for m in loaded):
                    expected_loaded = True
                elif complete or any(m["id"] == expected and m["state"] == "not-loaded" for m in models):
                    expected_loaded = False
            residency = (f"{len(loaded)} loaded" if complete
                         else f"at least {len(loaded)} loaded; residency unknown for {unknown} model(s)")
            return {"state": "reachable", "address": address, "via": via, "latencyMs": latency,
                    "name": hosts.get("hostname"), "models": models,
                    "loadedCount": len(loaded) if complete else None,
                    "expectedVerifyModel": expected,
                    "expectedVerifyLoaded": expected_loaded,
                    "detail": (f"LM Studio answered on {address} ({via}); {residency}" if v0
                               else f"OpenAI-compatible list answered on {address} ({via}); loaded state not reported"),
                    "observedAt": observed, "tried": tried + [address]}
        tried.append(address)
    # The strongest evidence across every address: an answer beats a refusal beats silence.
    reason = next((r for r in ("invalid", "refused", "timeout", "error") if r in reasons), None) if tried else "unresolved"
    where = ", ".join(tried)
    detail = {"invalid": f"Something answered on {where} port {hosts['port']}, but not with an LM Studio model list",
              # A TCP refusal says the address answered; it does not say why (no listener, or a firewall reset).
              "refused": f"Connection refused on {where} port {hosts['port']}",
              "timeout": f"No answer from the Mac's LM Studio on {where}",
              "error": f"No answer from the Mac's LM Studio on {where}",
              "unresolved": "The Mac's name did not resolve and no fallback address is recorded"}[reason or "error"]
    return {"state": "unreachable", "address": None, "via": None, "latencyMs": None, "name": hosts.get("hostname"),
            "models": [], "loadedCount": None, "reason": reason or "error", "detail": detail,
            "observedAt": observed, "tried": tried}


PEER_PROBE_MAX_AGE = 45.0


class MacPeer:
    """Background LAN probe; the sampler only reads its latest result (aged at read time)."""

    def __init__(self, share_root: Path, interval: float = 10.0, probe: Callable[..., dict[str, Any]] = probe_mac):
        self.share_root, self.interval, self._probe = share_root, interval, probe
        self._lock = threading.Lock()
        self._latest: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mac-peer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> dict[str, Any]:
        result = self._probe(mac_hosts(self.share_root))
        with self._lock:
            self._latest = result
        return result

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as error:  # noqa: BLE001 - recorded, never left looking like the last good probe
                with self._lock:
                    self._latest = {"state": "unknown", "detail": f"LAN probe failed ({type(error).__name__})",
                                    "models": [], "observedAt": time.time(), "address": None}
            self._stop.wait(self.interval)

    def read(self, now: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        now = time.time() if now is None else now
        with self._lock:
            latest = dict(self._latest) if self._latest else None
        if latest is None:
            return ({"state": "unknown", "detail": "First LAN probe pending", "models": [], "ageSeconds": None},
                    _source("mac-peer", "Mac (LAN)", "unavailable", "First LAN probe pending"))
        latest["ageSeconds"] = round(max(0.0, now - latest.get("observedAt", now)), 1)
        if latest["ageSeconds"] > PEER_PROBE_MAX_AGE:
            # A probe that stopped refreshing says nothing about the Mac now.
            latest.update(state="unknown", models=[], loadedCount=None, reason=None,
                          detail=f"Last LAN probe is {latest['ageSeconds']:.0f} s old; Mac state unknown")
        state = "live" if latest["state"] == "reachable" else "unavailable"
        return latest, _source("mac-peer", "Mac (LAN)", state, latest.get("detail") or "", latest["ageSeconds"])


# ------------------------------------------------------------- live decode rate
def live_decode_rate(previous: dict[tuple[int, int], tuple[float, float]], lane: dict[str, Any], now: float) -> tuple[float | None, dict]:
    """Tokens per second generated across a lane's busy slots, from the change in each slot's decoded count
    between two polls of the same request. Only slots that were generating in both polls count; a new
    request (decoded count went down) or a long gap contributes nothing rather than a guess."""
    current: dict[tuple[int, int], tuple[float, float]] = {}
    total, counted = 0.0, 0
    for slot in lane.get("slots") or []:
        decoded, slot_id, task = slot.get("decodedTokens"), slot.get("id"), slot.get("taskId")
        # Keyed by (slot, task): without llama-server's task id, two requests in one slot are
        # indistinguishable, so no rate is shown rather than a difference across requests.
        if not slot.get("isProcessing") or not isinstance(decoded, (int, float)) or not isinstance(slot_id, int) \
                or not isinstance(task, int):
            continue
        key = (slot_id, task)
        current[key] = (float(decoded), now)
        before = previous.get(key)
        if before is None:
            continue
        delta, gap = decoded - before[0], now - before[1]
        if delta > 0 and 0.2 <= gap <= 10.0:
            total += delta / gap
            counted += 1
    return (round(total, 1) if counted else None), current


# ------------------------------------------------------------ codemode history
CODEMODE_RUNS_MAX = 20
CODEMODE_MANIFEST_MAX = 128_000


def codemode_jobs(runs_root: Path, specs: list[dict[str, Any]], now: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recent codemode stage calls as dashboard job rows (schemaVersion 1, journal codemode-runs).

    Each stage's rate is completion tokens over the stage's whole request time, prompt included,
    so it is published as approxPerSecond, never as a decode rate (predictedPerSecond stays null).
    """
    ports = {spec["port"]: spec["id"] for spec in specs}
    empty = {"schemaVersion": 1, "journal": "codemode-runs", "inFlight": [], "recent": [], "lastSuccess": None, "clientsRecent": {}}
    try:
        runs = []
        with os.scandir(runs_root) as entries:
            for examined, entry in enumerate(entries):
                if examined >= QUEUE_MAX_ENTRIES:
                    break
                if entry.is_dir(follow_symlinks=False) and len(entry.name) >= 15 and entry.name[:8].isdigit():
                    manifest = Path(entry.path) / "manifest.json"
                    try:
                        runs.append((manifest.stat().st_mtime, manifest))
                    except OSError:
                        continue
    except OSError:
        return empty, _source("codemode-runs", "codemode runs", "unavailable", "code-runs folder unreadable")
    runs.sort(reverse=True)
    recent: list[dict[str, Any]] = []
    malformed = 0
    for mtime, manifest in runs[:CODEMODE_RUNS_MAX]:
        try:
            data = read_json(manifest, CODEMODE_MANIFEST_MAX)
        except (OSError, ValueError, json.JSONDecodeError):
            malformed += 1
            continue
        if not isinstance(data, dict) or not isinstance(data.get("metrics", []), list):
            malformed += 1
            continue
        run_id = _string(data.get("run_id"), 40) or manifest.parent.name
        certified = data.get("verification") == "CERTIFIED"
        for metric in (data.get("metrics") or [])[:8]:
            if not isinstance(metric, dict) or metric.get("kind") != "local":
                continue
            endpoint = _string(metric.get("endpoint"), 80) or ""
            try:
                lane = ports.get(int(endpoint.rsplit(":", 1)[1].split("/")[0]))
            except (IndexError, ValueError):
                lane = None
            tokens, seconds = _number(metric.get("tok"), 0, 1 << 24), _number(metric.get("sec"), 0.001, 86400)
            stage = _string(metric.get("stage"), 20) or "stage"
            mismatch = metric.get("served_mismatch") is True
            recent.append({"id": f"{run_id}:{stage}", "lane": lane, "client": "codemode", "stage": stage,
                           "model": _string(metric.get("served") or metric.get("model"), 80),
                           "state": "invalid-result" if mismatch else "success" if metric.get("finish") == "stop" else "error",
                           "completionTokens": tokens, "elapsedSeconds": seconds,
                           "approxPerSecond": round(tokens / seconds, 1) if tokens and seconds else None,
                           "predictedPerSecond": None, "promptPerSecond": None,
                           "flags": ["hit-token-limit"] if metric.get("finish") == "length" else [],
                           "runVerification": _string(data.get("verification"), 24), "certified": certified,
                           "ageSeconds": round(max(0.0, now - mtime), 1)})
    recent = recent[:40]
    success = next((row for row in recent if row["state"] == "success" and row["model"] and row["elapsedSeconds"]), None)
    jobs = {**empty, "recent": recent,
            "lastSuccess": ({key: success[key] for key in ("id", "lane", "model", "elapsedSeconds", "ageSeconds", "client")}
                            if success else None)}
    newest = f"; newest run {recent[0]['ageSeconds'] / 86400:.1f} d ago" if recent else ""
    skipped = f"; {malformed} malformed manifest{'s' if malformed != 1 else ''} skipped" if malformed else ""
    return jobs, _source("codemode-runs", "codemode runs", "live",
                         f"{len(runs[:CODEMODE_RUNS_MAX])} recent run manifests read{newest}{skipped}; rates are whole-request, not decode")


# ------------------------------------------------------------ status publisher
STATUS_FILE_NAME = "windows-status.json"  # the name the Mac's pipeline_status_snapshot.py reads (Codex-owned)
STATUS_MAX_BYTES = 65536
STATUS_LOADED_STATES = ("loaded", "inactive", "unknown")  # any other value voids the whole file for that reader
# The Mac reader reconciles against chami-dispatch inventory names; lanes serve under their aliases.
INVENTORY_IDS = {"openai/gpt-oss-20b": "gpt-oss-20b", "qwen3.8-27b": "Qwen3.8-27B Q4_K_M"}


def public_id(value: Any) -> str | None:
    """A model identifier fit to publish on the share: path-shaped values keep only their final name."""
    if not isinstance(value, str) or not value:
        return None
    if value not in INVENTORY_IDS and ("\\" in value or "/" in value):
        value = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return value[:120] or None


def pc_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    """llm-lab/status/windows-status.json for the Mac's status reader (schema 1, host windows), built from one
    snapshot. ``id`` is the inventory name the reader expects; the lane alias and served name ride along as
    ``aliases``/``servedModel``. Model ids, lane states, versions and counts only; no paths, prompts or keys."""
    lanes = snapshot.get("lanes") or []
    up = [lane for lane in lanes if lane.get("status") in ("idle", "busy")]
    mismatch = [lane["id"] for lane in lanes if lane.get("status") == "identity_mismatch"]
    health = "ok" if lanes and len(up) == len(lanes) else "degraded" if up else "down"
    models = [{"id": public_id(INVENTORY_IDS.get(lane.get("expectedModel"), lane.get("expectedModel"))) or "unknown",
               "aliases": [public_id(lane.get("expectedModel"))], "host": "windows", "lane": lane.get("id"), "port": lane.get("port"),
               # Only an answering lane is evidence of residency; no answer (or a stale one) is unknown, never inactive.
               "loadedState": "loaded" if lane.get("status") in LANE_UP else "unknown",
               "activity": lane.get("phase") if lane.get("status") in ("idle", "busy") else "unknown",
               "servedModel": public_id(lane.get("servedModel")), "slotsBusy": lane.get("slotsBusy"), "slotsTotal": lane.get("slotsTotal"),
               "liveTokensPerSecond": lane.get("liveTokensPerSecond")} for lane in lanes]
    gpus = [{key: gpu.get(key) for key in ("index", "name", "utilizationPercent", "memoryUsedMiB", "memoryTotalMiB", "temperatureC")}
            for gpu in snapshot.get("pcGpus") or []]
    memory = snapshot.get("memory") or {}
    queue = (snapshot.get("pipeline") or {}).get("queue") or {}
    limitations = []
    if mismatch:
        limitations.append("lane identity mismatch: " + ", ".join(mismatch))
    mac_peer = snapshot.get("macPeer")
    mac_peer = mac_peer if isinstance(mac_peer, dict) else {}
    if mac_peer.get("state") != "reachable":
        reason = mac_peer.get("reason")
        sampled_at = _number(snapshot.get("sampledAt"), low=0)
        probed_at = _number(mac_peer.get("observedAt"), low=0)
        # A linked peer's ageSeconds describes the share report, not its LAN check.
        fresh_probe = sampled_at is not None and probed_at is not None \
            and -5 <= sampled_at - probed_at <= PEER_PROBE_MAX_AGE
        failed_probe = fresh_probe and mac_peer.get("state") in ("unreachable", "linked") \
            and isinstance(reason, str) and reason in ("refused", "timeout", "invalid", "error", "unresolved")
        limitations.append(f"mac LM Studio LAN check failed ({reason}); cause unknown" if failed_probe
                           else "mac LM Studio LAN reachability unknown")
    return {"schemaVersion": 1, "observedAt": snapshot.get("observedAt"), "host": "windows",
            "producer": f"agiw-win-observer {snapshot.get('observerVersion')}", "health": health, "tasks": [],
            "models": models, "gpus": gpus, "adapters": [a.get("name") for a in snapshot.get("pcAdapters") or []],
            "memory": {"level": memory.get("level"), "availablePercent": memory.get("availablePercent")},
            "routeQueue": {key: queue.get(key) for key in ("pending", "claimed", "staleClaimed", "completed")} if queue else None,
            "components": [{"name": c.get("label"), "status": c.get("state")} for c in snapshot.get("components") or []],
            "limitations": limitations}


def validate_status(value: dict[str, Any]) -> None:
    """The reader's own rules, checked before writing so a bad file never replaces a good one."""
    models = value.get("models")
    if value.get("schemaVersion") != 1 or value.get("host") != "windows" or not isinstance(models, list) or len(models) > 32:
        raise ValueError("status header invalid")
    for model in models:
        if not isinstance(model.get("id"), str) or not model["id"] or model.get("host") != "windows" \
                or model.get("loadedState") not in STATUS_LOADED_STATES:
            raise ValueError("status model row invalid")


def is_link_or_junction(path: Path) -> bool:
    """True for symlinks, junctions and any other Windows reparse point."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


@contextmanager
def _locked_publish_parent(path: Path, stop_at: Path | None):
    """Walk from the volume root by handle, never following a directory reparse point."""
    path = Path(os.path.abspath(path))
    top = Path(os.path.abspath(stop_at)) if stop_at is not None else path.parent.parent
    if top != path.parent and top not in path.parent.parents:
        raise ValueError("publish target is outside its root")
    directories = list(reversed((path.parent, *path.parent.parents)))
    if not IS_WINDOWS:
        for directory in directories:
            if is_link_or_junction(directory):
                raise ValueError("linked status directory refused")
            directory.mkdir(exist_ok=True)
        if is_link_or_junction(path):
            raise ValueError("linked status directory refused")
        yield None
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
    create.restype = ctypes.c_void_p
    close = kernel32.CloseHandle
    close.argtypes = (ctypes.c_void_p,)
    invalid = ctypes.c_void_p(-1).value
    handles = []
    try:
        # The volume root is stable. Each child is opened relative to the preceding handle,
        # so renaming any visible path while this runs cannot redirect later operations.
        handle = create(str(directories[0]), 0x80, 0x7, None, 3, 0x02200000, None)
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        handles.append(handle)
        if _handle_is_reparse(kernel32, handle):
            raise ValueError("linked status directory refused")
        for directory in directories[1:]:
            handle = _nt_create_relative(handles[-1], directory.name, directory=True)
            if handle == invalid:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            if _handle_is_reparse(kernel32, handle) or is_link_or_junction(directory):
                raise ValueError("linked status directory refused")
        if is_link_or_junction(path):
            raise ValueError("linked status directory refused")
        yield handles[-1]
    finally:
        for handle in reversed(handles):
            close(handle)


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_wchar_p)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ulong), ("RootDirectory", ctypes.c_void_p),
                ("ObjectName", ctypes.POINTER(_UnicodeString)), ("Attributes", ctypes.c_ulong),
                ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _AttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", ctypes.c_ulong), ("ReparseTag", ctypes.c_ulong)]


def _handle_is_reparse(kernel32, handle: int) -> bool:
    info = _AttributeTagInfo()
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong)
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):  # FileAttributeTagInfo
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(info.FileAttributes & 0x400)


def _nt_error(status: int, call: str) -> OSError:
    """An OSError with the Win32 code, so a sharing or access refusal surfaces as PermissionError."""
    to_dos = ctypes.WinDLL("ntdll").RtlNtStatusToDosError
    to_dos.argtypes, to_dos.restype = (ctypes.c_ulong,), ctypes.c_ulong
    status &= 0xffffffff
    return OSError(0, f"{call} failed (0x{status:08x})", None, to_dos(status))


def _nt_create_relative(parent: int, name: str, directory: bool) -> int:
    """Create/open one child of a checked directory handle without path traversal."""
    buffer = ctypes.create_unicode_buffer(name)
    chars = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(chars, chars + 2, ctypes.cast(buffer, ctypes.c_wchar_p))
    attributes = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), parent, ctypes.pointer(unicode_name),
                                   0x40, None, None)  # OBJ_CASE_INSENSITIVE
    iosb = _IoStatusBlock()
    handle = ctypes.c_void_p()
    nt_create = ctypes.WinDLL("ntdll").NtCreateFile
    nt_create.argtypes = (ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong, ctypes.POINTER(_ObjectAttributes),
                          ctypes.POINTER(_IoStatusBlock), ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong)
    nt_create.restype = ctypes.c_long
    # FILE_READ_ATTRIBUTES for directories; WRITE_DATA, READ_ATTRIBUTES and DELETE for a temp file.
    access = 0x80 if directory else 0x110082  # file also needs SYNCHRONIZE for CRT writes
    disposition = 3 if directory else 2  # FILE_OPEN_IF / FILE_CREATE (exclusive)
    options = 0x200000 | (0x1 if directory else 0x60)  # file adds SYNCHRONOUS_IO_NONALERT
    status = nt_create(ctypes.byref(handle), access, ctypes.byref(attributes), ctypes.byref(iosb),
                       None, 0x10 if directory else 0x80, 0x7 if directory else 0,
                       disposition, options, None, 0)
    if status < 0:
        raise _nt_error(status, "NtCreateFile")
    return handle.value


def _nt_replace_relative(parent: int, temp: str, target: str, payload: bytes) -> None:
    """Write and replace one file relative to the same checked parent handle."""
    import msvcrt  # noqa: PLC0415 - Windows only

    handle = _nt_create_relative(parent, temp, directory=False)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_disposition = kernel32.SetFileInformationByHandle
    set_disposition.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong)

    def mark_for_delete(flag: bool) -> bool:
        delete = ctypes.c_byte(1 if flag else 0)
        return bool(set_disposition(handle, 4, ctypes.byref(delete), 1))  # FileDispositionInfo

    # Delete-pending until the payload is complete: if this process dies mid-write, closing the
    # handle removes the temp file instead of leaving it on the share.
    mark_for_delete(True)
    fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    stream = os.fdopen(fd, "wb")
    try:
        stream.write(payload)
        stream.flush()
        if not mark_for_delete(False):  # a delete-pending file cannot be renamed
            raise ctypes.WinError(ctypes.get_last_error())
        encoded = target.encode("utf-16-le")
        # FILE_RENAME_INFORMATION and its _EX form share this layout: a 4-byte first field (BOOLEAN
        # ReplaceIfExists plus padding, or ULONG Flags), the root handle, then the name length and name.
        class RenameInfo(ctypes.Structure):
            _fields_ = [("Flags", ctypes.c_ulong), ("RootDirectory", ctypes.c_void_p),
                        ("FileNameLength", ctypes.c_ulong)]
        prefix = RenameInfo(0, parent, len(encoded))
        name_offset = RenameInfo.FileNameLength.offset + ctypes.sizeof(ctypes.c_ulong)
        raw = ctypes.create_string_buffer(name_offset + len(encoded))
        set_info = ctypes.WinDLL("ntdll").NtSetInformationFile
        set_info.argtypes = (ctypes.c_void_p, ctypes.POINTER(_IoStatusBlock), ctypes.c_void_p,
                             ctypes.c_ulong, ctypes.c_int)
        set_info.restype = ctypes.c_long
        # POSIX semantics replace the name even while a reader holds the old file open with delete
        # sharing, as the SMB server does for a Mac that keeps the status file open; that reader keeps the
        # old contents and its next open gets the new file. Classic rename is the fallback where the
        # volume or Windows version lacks it.
        info_class, flags = 65, 0x3  # FileRenameInformationEx; REPLACE_IF_EXISTS | POSIX_SEMANTICS
        for attempt in range(6):
            prefix.Flags = flags
            ctypes.memmove(raw, ctypes.byref(prefix), name_offset)
            ctypes.memmove(ctypes.addressof(raw) + name_offset, encoded, len(encoded))
            iosb = _IoStatusBlock()
            status = set_info(handle, ctypes.byref(iosb), raw, len(raw), info_class)
            if status >= 0:
                return
            code = status & 0xffffffff
            if info_class == 65 and code in (0xC0000003, 0xC00000BB, 0xC000000D):
                info_class, flags = 10, 1  # FileRenameInformation, ReplaceIfExists
                continue
            if code not in (0xC0000022, 0xC0000043) or attempt == 5:
                raise _nt_error(status, "NtSetInformationFile")
            time.sleep(0.05 * (attempt + 1))
        raise _nt_error(status, "NtSetInformationFile")  # never report an unfinished rename as success
    except BaseException:
        # The temporary file is tied to this handle even if its parent was renamed.
        mark_for_delete(True)
        raise
    finally:
        stream.close()


def write_json_atomic(path: Path, value: Any, max_bytes: int | None = None, stop_at: Path | None = None) -> None:
    """Write via an unpredictable, exclusively created temp file, refusing links/junctions on the way.

    Every directory from ``stop_at`` (or the grandparent) down to the target, and the target itself,
    must not be a reparse point, so a planted junction cannot redirect the write.
    """
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if max_bytes is not None and len(payload) > max_bytes:
        raise ValueError("payload too large")
    with _locked_publish_parent(path, stop_at) as parent:
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        if IS_WINDOWS:
            _nt_replace_relative(parent, tmp.name, path.name, payload)
            return
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            # Windows refuses to replace a file another process (e.g. the Mac reading over SMB) holds open at that
            # instant; the reader closes within milliseconds, so a few short retries ride it out.
            for attempt in range(6):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ------------------------------------------------------------------ peer link
# Every AGIW instance keeps its observer on loopback, so instances meet on the share instead of the
# network: each writes llm-lab/status/agiw-peers/<host>.json every 10 s and reads the others. Nothing
# here opens a listener, and a peer file is evidence of what that host observed, not a live connection.
PEER_DIR = ("llm-lab", "status", "agiw-peers")
PEER_FRESH_SECONDS = 30.0
PEER_MAX_FILES = 16
PEER_MAX_BYTES = 65536
PEER_SCAN_LIMIT = 256
PEER_HOST = __import__("re").compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")


def peer_dir(share_root: Path) -> Path:
    return share_root.joinpath(*PEER_DIR)


def host_id() -> str:
    name = socket.gethostname().split(".")[0].lower()
    return name if PEER_HOST.match(name) else "windows-pc"


def peer_presence(snapshot: dict[str, Any], host: str) -> dict[str, Any]:
    """This instance's presence: who it is and the verified model states it sees. No paths, prompts or keys."""
    status = pc_status(snapshot)
    return {"schemaVersion": 1, "kind": "agiw-peer", "host": host, "platform": "windows",
            "edition": "windows", "version": snapshot.get("observerVersion"), "observedAt": snapshot.get("observedAt"),
            "observedUnix": snapshot.get("sampledAt"), "health": status["health"],
            "models": [{"id": m["id"], "aliases": m["aliases"], "loadedState": m["loadedState"], "activity": m["activity"],
                        "slotsBusy": m["slotsBusy"], "slotsTotal": m["slotsTotal"]} for m in status["models"]],
            "gpus": status["gpus"], "memory": status["memory"],
            "dashboard": {"bind": "loopback", "port": snapshot.get("dashboardPort")},
            "reads": ["agiw-peers", "mac-status.json"]}


def _validate_peer(value: Any, now: float) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("kind") != "agiw-peer":
        return None
    host = value.get("host")
    observed = value.get("observedUnix")
    if not isinstance(host, str) or not PEER_HOST.match(host) or not isinstance(observed, (int, float)) \
            or isinstance(observed, bool) or not math.isfinite(observed) or observed > now + 5:
        return None
    if not isinstance(value.get("models", []), list):
        return None
    models = []
    for model in (value.get("models") or [])[:32]:
        if isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"].strip():
            models.append({"id": model["id"][:120],
                           "loadedState": model.get("loadedState") if model.get("loadedState") in STATUS_LOADED_STATES else "unknown",
                           "activity": _string(model.get("activity"), 24) or "unknown"})
    age = round(max(0.0, now - observed), 1)
    return {"host": host, "platform": _string(value.get("platform"), 24) or "unknown",
            "edition": _string(value.get("edition"), 24), "version": _string(value.get("version"), 24),
            "health": _string(value.get("health"), 16) or "unknown", "models": models, "ageSeconds": age,
            "fresh": age <= PEER_FRESH_SECONDS, "source": "agiw-peers"}


def _legacy_mac_status(share_root: Path, now: float) -> dict[str, Any] | None:
    """The Mac's on-demand mac-status.json (written by Codex's status script) as a peer record."""
    path = share_root / "llm-lab" / "status" / "mac-status.json"
    try:
        value = read_json(path, PEER_MAX_BYTES)
        observed = __import__("datetime").datetime.fromisoformat(str(value["observedAt"]).replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if value.get("schemaVersion") != 1 or value.get("host") != "mac" or observed > now + 5 \
            or not isinstance(value.get("models") or [], list):
        return None
    models = [{"id": m["id"][:120], "loadedState": m.get("loadedState") if m.get("loadedState") in STATUS_LOADED_STATES else "unknown",
               "activity": _string(m.get("activity"), 24) or "unknown"}
              for m in (value.get("models") or [])[:32]
              if isinstance(m, dict) and isinstance(m.get("id"), str) and m.get("host") == "mac"]
    age = round(max(0.0, now - observed), 1)
    return {"host": "mac", "platform": "macos", "edition": "mac-status", "version": None, "verified": False,
            "health": _string(value.get("health"), 16) or "unknown", "models": models, "ageSeconds": age,
            "fresh": age <= PEER_FRESH_SECONDS, "source": "mac-status.json"}


def read_peers(share_root: Path, self_host: str, now: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every other AGIW instance that has published presence, newest first, plus the Mac's status file."""
    peers: list[dict[str, Any]] = []
    directory = peer_dir(share_root)
    entries, examined, truncated = [], 0, False
    try:
        if not is_link_or_junction(directory):
            with os.scandir(directory) as scan:
                for entry in scan:
                    examined += 1
                    if examined > PEER_SCAN_LIMIT:  # a flooded folder costs a bounded amount of work
                        truncated = True
                        break
                    stem = entry.name[:-5] if entry.name.endswith(".json") else None
                    if stem and PEER_HOST.match(stem) and entry.is_file(follow_symlinks=False):
                        entries.append(entry)
    except OSError:
        entries = []
    dated_entries = []
    for entry in entries:
        try:
            dated_entries.append((entry.stat(follow_symlinks=False).st_mtime, entry))
        except OSError:
            continue  # A peer can remove its file between enumeration and stat.
    entries = [entry for _, entry in sorted(dated_entries, key=lambda pair: pair[0], reverse=True)[:PEER_MAX_FILES]]
    seen_hosts: set[str] = set()
    for entry in entries:
        try:
            peer = _validate_peer(read_json(Path(entry.path), PEER_MAX_BYTES), now)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            peer = None
        # The file name must be the host it claims, and each host counts once (newest wins).
        if (peer and entry.name[:-5].lower() == peer["host"].lower() and peer["host"].lower() not in seen_hosts
                and peer["host"] != self_host and peer["host"] != host_id()):
            seen_hosts.add(peer["host"].lower())
            peer["verified"] = False  # unsigned share report; authenticated peers arrive with the pairing protocol
            peers.append(peer)
    if not any(p["platform"] == "macos" and p["fresh"] for p in peers):
        legacy = _legacy_mac_status(share_root, now)
        if legacy:
            # Every candidate is stale here; show whichever report for that host is newest.
            same = [peer for peer in peers if peer["host"].lower() == legacy["host"].lower()]
            if all(legacy["ageSeconds"] <= peer["ageSeconds"] for peer in same):
                peers = [peer for peer in peers if peer not in same]
                peers.append(legacy)
    peers.sort(key=lambda p: p["ageSeconds"])
    fresh = [p for p in peers if p["fresh"]]
    detail = (f"{len(fresh)} reported ({', '.join(p['host'] for p in fresh)}; unsigned share files, not verified)" if fresh
              else "No AGIW peer has published in the last 30 s")
    if truncated:
        detail += f"; folder scan stopped after {PEER_SCAN_LIMIT} entries"
    stale = [p for p in peers if not p["fresh"]]
    if stale:
        detail += f"; {len(stale)} stale ({', '.join(p['host'] + ' ' + str(int(p['ageSeconds'] // 3600)) + ' h' for p in stale)})"
    return peers, _source("agiw-peers", "AGIW peers", "live" if fresh else "unavailable", detail)


def python_info() -> str:
    return f"Python {sys.version.split()[0]}"
