"""Bounded, read-only probes for the Windows edition.

Every probe returns plain data plus a ``sources`` row and never raises into the
sampler. Network probes ignore machine proxy settings and refuse redirects, so a
lane or peer cannot turn a status read into an external fetch. Subprocesses run
with CREATE_NO_WINDOW so a windowless observer never flashes a console.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

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


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def http_get_json(url: str, timeout: float = HTTP_TIMEOUT, max_bytes: int = HTTP_MAX_BYTES) -> Any:
    """GET a fixed URL with a byte cap; JSON when it parses, else a short text prefix."""
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with _OPENER.open(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("oversized HTTP response")
    raw = body.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
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
    return json.loads(payload.decode("utf-8-sig"))


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
        raw = config["runtime"]["lanes"]
        specs = []
        for lane_id in ("fast", "deep"):
            lane = raw.get(lane_id) if isinstance(raw, dict) else None
            if not isinstance(lane, dict):
                continue
            port, alias = lane.get("port"), _string(lane.get("alias"), 80)
            if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 and alias:
                specs.append({"id": lane_id, "port": port, "alias": alias,
                              "device": _string(lane.get("device"), 32) or "unknown",
                              "context": _number(lane.get("context"), 1, 1 << 24),
                              "parallel": _number(lane.get("parallel"), 1, 256)})
        if specs:
            layout = _string(((config.get("runtime") or {}).get("lane_layout") or {}).get("name"), 64)
            return specs, _source("lane-config", "Lane layout", "live",
                                  f"Declared in windows-llm-pipeline/config.json{f' · layout {layout}' if layout else ''}")
        raise ValueError("no usable lanes")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
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
    slot = {"id": _number(raw.get("id"), 0, 4096), "isProcessing": raw["is_processing"],
            "nCtx": _number(raw.get("n_ctx"), 0, 1 << 24)}
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
    if any(slot is None for slot in slots):
        lane["status"], lane["detail"] = "slots_unknown", "Slot state incomplete"
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
        state, loaded, source = "unloaded" if status == "unreachable" else "unknown", False if status == "unreachable" else None, "llama-health"
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
    found = shutil.which("nvidia-smi")
    if found:
        return found
    candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
    return str(candidate) if candidate.is_file() else None


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
                        elif state == "claimed" and age > STALE_CLAIM_SECONDS:
                            stale_claims += 1
            counts[state] = total
    except OSError:
        return ({"status": None, "runId": None, "pipelines": [], "queue": None},
                _source("route-queue", "Route queue", "unavailable", "Route queue unreadable"))
    pending = counts.get("pending", 0)
    claimed_live = counts.get("claimed", 0) - stale_claims
    # A job nobody has picked up for a day is a leftover, not work waiting for a lane.
    pending_live = pending if oldest_pending is None or oldest_pending <= STALE_PENDING_SECONDS else 0
    status = "running" if claimed_live > 0 else "queued" if pending_live else "idle"
    pipeline = {"status": status, "stage": "claimed" if status == "running" else None, "runId": None, "pipelines": [],
                "queue": {"pending": pending, "claimed": counts.get("claimed", 0), "staleClaimed": stale_claims,
                          "completed": counts.get("completed", 0),
                          "oldestPendingSeconds": round(oldest_pending, 1) if oldest_pending is not None else None,
                          "staleAfterSeconds": STALE_CLAIM_SECONDS}}
    detail = f"{pending} pending · {counts.get('claimed', 0)} claimed · {counts.get('completed', 0)} completed"
    if stale_claims:
        detail += f" · {stale_claims} claim{'s' if stale_claims != 1 else ''} older than 15 min (likely abandoned)"
    if pending and not pending_live:
        detail += f" · pending jobs untouched for {oldest_pending / 86400:.0f} d (stale, not queued)"
    if claimed_live > 0:
        detail += " · a recent claim is not proof that a worker is still running"
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
                "port": port if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 else 1234,
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
        if address in seen or address in own or address.startswith("127.") or ":" in address:
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
    for address, via in candidate_addresses(hosts, resolve, own):
        base = f"http://{address}:{hosts['port']}"
        started = time.monotonic()
        for path, v0 in (("/api/v0/models", True), ("/v1/models", False)):
            try:
                models = parse_mac_models(fetch(base + path, 1.5, HTTP_MAX_BYTES), v0)
            except Exception:  # noqa: BLE001
                continue
            latency = round((time.monotonic() - started) * 1000)
            loaded = [m for m in models if m["state"] == "loaded"]
            expected = hosts.get("expectedVerify")
            return {"state": "reachable", "address": address, "via": via, "latencyMs": latency,
                    "name": hosts.get("hostname"), "models": models,
                    "loadedCount": len(loaded) if v0 else None,
                    "expectedVerifyModel": expected,
                    "expectedVerifyLoaded": (any(m["id"] == expected or m["id"].split("/")[-1] == expected.split("/")[-1]
                                                 for m in loaded) if v0 and expected else None),
                    "detail": (f"LM Studio answered on {address} ({via}); {len(loaded)} loaded" if v0
                               else f"OpenAI-compatible list answered on {address} ({via}); loaded state not reported"),
                    "observedAt": observed, "tried": tried + [address]}
        tried.append(address)
    return {"state": "unreachable", "address": None, "via": None, "latencyMs": None, "name": hosts.get("hostname"),
            "models": [], "loadedCount": None,
            "detail": ("No answer from the Mac's LM Studio on " + ", ".join(tried) if tried
                       else "The Mac's name did not resolve and no fallback address is recorded"),
            "observedAt": observed, "tried": tried}


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
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.interval)

    def read(self, now: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        now = time.time() if now is None else now
        with self._lock:
            latest = dict(self._latest) if self._latest else None
        if latest is None:
            return ({"state": "unknown", "detail": "First LAN probe pending", "models": [], "ageSeconds": None},
                    _source("mac-peer", "Mac (LAN)", "unavailable", "First LAN probe pending"))
        latest["ageSeconds"] = round(max(0.0, now - latest.get("observedAt", now)), 1)
        state = "live" if latest["state"] == "reachable" else "unavailable"
        return latest, _source("mac-peer", "Mac (LAN)", state, latest.get("detail") or "", latest["ageSeconds"])


# ------------------------------------------------------------- live decode rate
def live_decode_rate(previous: dict[int, tuple[float, float]], lane: dict[str, Any], now: float) -> tuple[float | None, dict]:
    """Tokens per second generated across a lane's busy slots, from the change in each slot's decoded count
    between two polls of the same request. Only slots that were generating in both polls count; a new
    request (decoded count went down) or a long gap contributes nothing rather than a guess."""
    current: dict[int, tuple[float, float]] = {}
    total, counted = 0.0, 0
    for slot in lane.get("slots") or []:
        decoded, slot_id = slot.get("decodedTokens"), slot.get("id")
        if not slot.get("isProcessing") or not isinstance(decoded, (int, float)) or not isinstance(slot_id, int):
            continue
        current[slot_id] = (float(decoded), now)
        before = previous.get(slot_id)
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
            for entry in entries:
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
    for mtime, manifest in runs[:CODEMODE_RUNS_MAX]:
        try:
            data = read_json(manifest, CODEMODE_MANIFEST_MAX)
        except (OSError, ValueError, json.JSONDecodeError):
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
    return jobs, _source("codemode-runs", "codemode runs", "live",
                         f"{len(runs[:CODEMODE_RUNS_MAX])} recent run manifests read{newest}; rates are whole-request, not decode")


# ------------------------------------------------------------ status publisher
def pc_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The PC counterpart of llm-lab/status/mac-status.json (same schema 1 shape), built from one snapshot.
    Model ids, lane states, versions and counts only; no paths, prompts, keys or client metadata."""
    lanes = snapshot.get("lanes") or []
    up = [lane for lane in lanes if lane.get("status") in ("idle", "busy")]
    mismatch = [lane["id"] for lane in lanes if lane.get("status") == "identity_mismatch"]
    health = "ok" if lanes and len(up) == len(lanes) else "degraded" if up else "down"
    models = [{"id": lane.get("expectedModel"), "host": "windows", "lane": lane.get("id"), "port": lane.get("port"),
               "loadedState": "loaded" if lane.get("status") in LANE_UP else "unknown" if lane.get("status") in ("loading", "identity_mismatch", "unknown") else "inactive",
               "activity": lane.get("phase") if lane.get("status") in ("idle", "busy") else "unknown",
               "servedModel": lane.get("servedModel"), "slotsBusy": lane.get("slotsBusy"), "slotsTotal": lane.get("slotsTotal"),
               "liveTokensPerSecond": lane.get("liveTokensPerSecond")} for lane in lanes]
    gpus = [{key: gpu.get(key) for key in ("index", "name", "utilizationPercent", "memoryUsedMiB", "memoryTotalMiB", "temperatureC")}
            for gpu in snapshot.get("pcGpus") or []]
    memory = snapshot.get("memory") or {}
    queue = (snapshot.get("pipeline") or {}).get("queue") or {}
    limitations = []
    if mismatch:
        limitations.append("lane identity mismatch: " + ", ".join(mismatch))
    if (snapshot.get("macPeer") or {}).get("state") != "reachable":
        limitations.append("mac LM Studio not reachable from the PC")
    return {"schemaVersion": 1, "observedAt": snapshot.get("observedAt"), "host": "windows",
            "producer": f"agiw-win-observer {snapshot.get('observerVersion')}", "health": health, "tasks": [],
            "models": models, "gpus": gpus, "adapters": [a.get("name") for a in snapshot.get("pcAdapters") or []],
            "memory": {"level": memory.get("level"), "availablePercent": memory.get("availablePercent")},
            "routeQueue": {key: queue.get(key) for key in ("pending", "claimed", "staleClaimed", "completed")} if queue else None,
            "components": [{"name": c.get("label"), "status": c.get("state")} for c in snapshot.get("components") or []],
            "limitations": limitations}


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def python_info() -> str:
    return f"Python {sys.version.split()[0]}"
