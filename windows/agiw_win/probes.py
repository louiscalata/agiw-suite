"""Bounded, read-only probes for the Windows edition.

Every probe returns plain data plus a ``sources`` row and never raises into the
sampler. Network probes ignore machine proxy settings and refuse redirects, so a
lane or peer cannot turn a status read into an external fetch. Subprocesses run
with CREATE_NO_WINDOW so a windowless observer never flashes a console.
"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
import datetime as dt
import http.client
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

from . import buildinfo
from . import winsys
from .jsonutil import JSON_MAX_DEPTH, loads_strict

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
# The required lanes. There is deliberately no built-in port or model: a lane is probed only when
# windows-llm-pipeline/config.json declares it. The PC now serves the router on :1234, qwen3.8-27b on
# :1236 and gpt-oss-20b on :1245. NVIDIA PAIR is uninstalled, so an unreadable config must not guess a port.
LANE_IDS = ("fast", "deep")


def unconfigured_lane(lane_id: str, reason: str = "missing") -> dict[str, Any]:
    """A required lane with no declaration: shown as an explicit unknown row, never probed (fail closed)."""
    label = "config unreadable" if reason == "unreadable" else "not configured"
    return {"id": lane_id, "port": None, "alias": f"{lane_id} lane ({label})", "device": "unknown",
            "undeclared": True, "configReason": reason}

class OwnerUnverified(RuntimeError):
    """The listener owner changed or could not be rechecked before an endpoint read."""


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
    """The declared lanes from windows-llm-pipeline/config.json runtime.lanes. Without a usable declaration every
    required lane is an unconfigured, unprobed unknown row: the observer fails closed instead of guessing ports."""
    path = share_root / "windows-llm-pipeline" / "config.json"
    try:
        config = read_json(path)
        runtime = config.get("runtime") if isinstance(config, dict) else None
        raw = runtime.get("lanes") if isinstance(runtime, dict) else None
        if not isinstance(raw, dict):
            raise ValueError("runtime.lanes missing")
        specs = []
        missing = []
        for lane_id in LANE_IDS:
            lane = raw.get(lane_id)
            if not isinstance(lane, dict):
                missing.append(lane_id)
                continue
            port, alias = lane.get("port"), _string(lane.get("alias"), 80)
            if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 and alias and alias == lane.get("alias"):
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
                specs.append(unconfigured_lane(lane_id))
            specs.sort(key=lambda spec: LANE_IDS.index(spec["id"]))
            detail = f"Declared in windows-llm-pipeline/config.json{f' · layout {layout}' if layout else ''}"
            if missing:
                return specs, _source("lane-config", "Lane layout", "error",
                                      detail + f" · {', '.join(missing)} lane missing or malformed (shown as unknown, not probed)")
            return specs, _source("lane-config", "Lane layout", "live", detail)
        return [unconfigured_lane(lane_id) for lane_id in LANE_IDS], _source(
            "lane-config", "Lane layout", "error", "Required lanes missing or malformed in pipeline config; not probed")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
        return [unconfigured_lane(lane_id, "unreadable") for lane_id in LANE_IDS], _source(
            "lane-config", "Lane layout", "unavailable",
            "Pipeline config unreadable; no lane is probed until windows-llm-pipeline/config.json declares it")


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
    undeclared = spec.get("undeclared") or spec.get("port") is None
    endpoint = None if undeclared else f"http://{host}:{spec['port']}"
    lane = {"id": spec["id"], "port": spec.get("port"), "endpoint": endpoint, "expectedModel": spec["alias"],
            "servedModel": None, "modelBytes": None, "status": "unknown", "slots": [], "slotsBusy": None, "slotsTotal": None,
            "phase": None, "detail": None, "device": spec.get("device")}
    if undeclared:
        lane["status"] = "undeclared"
        lane["configReason"] = spec.get("configReason", "missing")
        lane["detail"] = ("Lane config unreadable; lane unknown, not probed" if lane["configReason"] == "unreadable" else
                          "Lane missing or malformed in windows-llm-pipeline/config.json; not probed")
        return lane
    try:
        health = fetch(endpoint + "/health", HTTP_TIMEOUT, HTTP_MAX_BYTES)
    except OwnerUnverified:
        lane["detail"] = "Lane port ownership unavailable; not probed"
        return lane
    except Exception as error:  # noqa: BLE001 - any failure is "unreachable"
        loading = "503" in str(error)
        lane["status"] = "loading" if loading else "unreachable"
        lane["detail"] = "Model loading (health 503)" if loading else "No answer on the lane port"
        return lane
    health_status = health.get("status") if isinstance(health, dict) else None
    if health_status != "ok":
        loading = isinstance(health_status, str) and health_status in ("loading", "loading model")
        lane["status"] = "loading" if loading else "health_unknown"
        lane["detail"] = f"Health reports {health_status}" if loading else "Lane health response invalid or unrecognised"
        return lane
    try:
        payload = fetch(endpoint + "/v1/models", HTTP_TIMEOUT, HTTP_MAX_BYTES)
        models = _model_ids(payload)
    except OwnerUnverified:
        lane["detail"] = "Lane port ownership unavailable; not probed"
        return lane
    except Exception:  # noqa: BLE001
        lane["status"], lane["detail"] = "no_identity", "Served model identity unavailable"
        return lane
    lane["servedModel"] = _string(models[0], 120) if models else None
    if len(models) == 1 and isinstance(payload["data"][0], dict):
        meta = payload["data"][0].get("meta")
        size = meta.get("size") if isinstance(meta, dict) else None
        if isinstance(size, int) and not isinstance(size, bool) and 1 <= size <= 2 ** 52:
            lane["modelBytes"] = size
    if not models or any(not isinstance(model, str) or not model.strip() for model in models):
        lane["status"], lane["detail"] = "no_identity", "Lane returned no valid model identity"
        return lane
    if len(models) != 1 or models[0] != spec["alias"]:
        lane["status"] = "identity_mismatch"
        lane["detail"] = f"Serves {lane['servedModel'] or 'an unnamed model'}, declared {spec['alias']}"
        return lane
    try:
        # /slots is answered through the task queue between decode batches, so a busy CPU lane
        # needs far longer than /health; lanes are polled on their own threads for this reason.
        payload = fetch(endpoint + "/slots", slots_timeout, HTTP_MAX_BYTES)
    except OwnerUnverified:
        lane["detail"] = "Lane port ownership unavailable; not probed"
        return lane
    except Exception:  # noqa: BLE001
        lane["status"], lane["detail"] = "slots_unknown", "Slot endpoint unavailable (start llama-server with --slots)"
        return lane
    raw_slots = payload.get("slots") if isinstance(payload, dict) else payload
    if isinstance(payload, dict) and (payload.get("complete") is False or payload.get("truncated") is True):
        lane["status"], lane["detail"] = "slots_unknown", "Slot list incomplete or truncated"
        return lane
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
# No current evidence about the lane at all: never reported as down.
LANE_NO_EVIDENCE = {"unknown", "stale", "undeclared", "unreachable", "no_identity", "health_unknown"}


def lane_health(lane: dict[str, Any]) -> dict[str, str]:
    """Serving, affirmative non-serving evidence, or unknown, with the observation's reason."""
    status = lane.get("status")
    state = "up" if status in LANE_UP else "down" if status in ("loading", "identity_mismatch", "not_agiw") else "unknown"
    return {"state": state, "reason": lane.get("detail") or
            ("Lane serving the declared model" if state == "up" else "Lane state unknown" if state == "unknown"
             else "Lane not serving the declared model")}


def mark_foreign_lanes(lanes: list[dict[str, Any]]) -> list[str]:
    """A lane whose port is held by an NVIDIA PAIR process is not an AGIW lane, whatever its /v1/models says.
    Rewrites those lanes in place (status not_agiw, no activity) and returns their ids."""
    foreign = []
    for lane in lanes:
        process = lane.get("process")
        if isinstance(process, dict) and process.get("owner") == "pair":
            name = process.get("name") if isinstance(process.get("name"), str) else "a PAIR process"
            lane.update(status="not_agiw", phase=None, slots=[], slotsBusy=None, slotsTotal=None, modelBytes=None,
                        liveTokensPerSecond=None,
                        detail=f"Port :{lane.get('port')} is held by NVIDIA PAIR ({name}), not an AGIW lane; not counted")
            foreign.append(lane["id"])
    return foreign


def lanes_health(lanes: list[dict[str, Any]]) -> str:
    """ok / degraded / down / unknown for the PC's lanes. Only serving lanes can make the PC up;
    failed reads are unknown, while down needs affirmative evidence that every lane is not serving."""
    statuses = [lane.get("status") for lane in lanes if isinstance(lane, dict)]
    if not statuses or len(statuses) != len(lanes):
        return "unknown"
    serving = [status for status in statuses if status in LANE_UP]
    if len(serving) == len(statuses):
        return "ok"
    if any(status not in LANE_UP and status not in ("loading", "identity_mismatch", "not_agiw") for status in statuses):
        return "unknown"
    if serving:
        return "degraded"
    if all(status in ("loading", "identity_mismatch", "not_agiw") for status in statuses):
        return "down"
    return "unknown"


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
    elif status == "not_agiw":
        # The lane port belongs to another program (NVIDIA PAIR): whatever it serves is not this lane's model.
        state, loaded, source = "unknown", None, "lane-processes"
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


def lane_server_pids(lanes: list[dict[str, Any]] | None) -> frozenset[int]:
    """PIDs that own a declared lane port in the TCP table (collect.py attaches them as lane["process"]).

    Only these processes are "Local LLM server". A llama-server.exe found merely by image name may be NVIDIA PAIR's
    engine or someone else's server, so it is never counted as an AGIW lane; neither is a PAIR-owned listener.
    """
    pids = set()
    for lane in lanes or []:
        process = lane.get("process") if isinstance(lane, dict) else None
        pid = process.get("pid") if isinstance(process, dict) else None
        if type(pid) is int and pid > 4 and process.get("owner") == "agiw" and process.get("ownerKnown") is True:
            pids.add(pid)
    return frozenset(pids)


def memory_block(status_fn: Callable[[], dict[str, int]] | None = None,
                 consumers_fn: Callable[[], list[dict[str, Any]]] | None = None, *,
                 lanes: list[dict[str, Any]] | None = None, nisi: dict[str, Any] | None = None,
                 route_live: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """snapshot.memory in the shape the dashboard's memoryView reads, and its source row.

    The "Local LLM server" row is built only from consumer rows flagged ``lane`` (processes that own a declared
    lane port); see lane_server_pids.
    """
    try:
        raw = (status_fn or _memory_status)()
        total, avail = raw["totalPhys"], raw["availPhys"]
        if not (type(total) is int and total > 0 and type(avail) is int and 0 <= avail <= total):
            raise ValueError("implausible memory reading")
        available = avail * 100 / total
        limit, commit_avail = raw.get("commitLimit"), raw.get("commitAvail")
        commit = ((limit - commit_avail) * 100 / limit
                  if type(limit) is int and limit > 0 and type(commit_avail) is int and 0 <= commit_avail <= limit else None)
        level = memory_level(available, commit)
        available = round(available, 1)
        commit = round(commit, 1) if commit is not None else None
        gib = 2 ** 30
        reasons = [f"{available:.0f}% of {total / gib:.0f} GB RAM available"]
        if commit is not None:
            reasons.append(f"commit charge {commit:.0f}% of limit")
        up = [lane for lane in (lanes or []) if lane.get("status") in LANE_UP]
        consumers: list[dict[str, Any]] = []
        consumers_known = level == "ok"
        if level != "ok":
            try:
                consumers = [dict(row) for row in (consumers_fn() if consumers_fn else
                                                   top_processes(limit=8, lane_pids=lane_server_pids(lanes)))]
                consumers_known = True
                residency_known = bool(lanes) and len(up) == len(lanes)
                sizes = [lane.get("modelBytes") for lane in up]
                loaded_bytes = (sum(sizes) if sizes and all(isinstance(size, int) and not isinstance(size, bool)
                                                           and 1 <= size <= 2 ** 52 for size in sizes) else None)
                if loaded_bytes is not None and loaded_bytes > 2 ** 52:
                    # The dashboard drops a row whose size is past its byte bound; keep the row and its count instead.
                    loaded_bytes = None
                # Only lane-port owners (rows flagged lane) become the one server row: the lane total is claimed once.
                servers = [row for row in consumers if row.get("lane") is True]
                consumers = [row for row in consumers if row.get("lane") is not True]
                server = None
                if servers:
                    server = {**servers[0], "label": LANE_SERVER_LABEL,
                              "residentBytes": sum(row["residentBytes"] for row in servers),
                              "processCount": sum(row["processCount"] for row in servers)}
                    if residency_known:
                        server["loadedModelCount"] = len(up)
                        # Tensor sizes from /v1/models are not measured RAM or VRAM use.
                        if loaded_bytes is not None:
                            server["loadedModelBytes"] = loaded_bytes
                # As the Mac guard does: the named server row always, plus the three largest others, largest first.
                others = sorted(consumers, key=lambda row: row.get("residentBytes") or 0, reverse=True)[:3]
                consumers = sorted(([server] if server is not None else []) + others,
                                   key=lambda row: max(row.get("residentBytes") or 0,
                                                       row.get("loadedModelBytes") or 0), reverse=True)
            except Exception:  # noqa: BLE001 - consumers are optional detail
                consumers = []
                consumers_known = False
        suggestions = []
        if level in ("tight", "critical"):
            if route_live:
                suggestions.append("Close apps you are not using before stopping a model lane "
                                   "(a Nisi route is running: stopping a lane can stop it)")
            elif up and (nisi or {}).get("state") == "ready":
                suggestions.append("Close apps you are not using before stopping a model lane "
                                   "(Nisi pair: PC edit routes stop while either lane is down)")
            else:
                suggestions.append("Close apps you are not using before loading another model")
        block = {"level": level, "availablePercent": available, "totalBytes": total, "availableBytes": avail,
                 "commitPercent": commit, "swapUsedBytes": None, "compressedBytes": None, "gpuAllocBytes": None,
                 "consumers": consumers, "consumersKnown": consumers_known, "suggestions": suggestions, "reasons": reasons, "paused": []}
        return block, _source("mem-guard", "Memory", "live", "Windows physical memory and commit charge sampled")
    except Exception:  # noqa: BLE001
        return ({"level": "unknown", "consumers": [], "consumersKnown": False, "suggestions": [], "reasons": [], "paused": []},
                _source("mem-guard", "Memory", "unavailable", "Windows memory status unreadable"))


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def top_processes(limit: int = 3, max_pids: int = 4096, lane_pids: frozenset[int] = frozenset()) -> list[dict[str, Any]]:
    """Largest working sets grouped by executable name; names and sizes only, never arguments or paths.
    ``lane_pids`` (owners of declared lane ports) form the "Local LLM server" group; NVIDIA PAIR's processes,
    recognised by their install folder, form one "NVIDIA PAIR" group."""
    if not IS_WINDOWS:
        return []
    psapi, kernel32 = ctypes.windll.psapi, ctypes.windll.kernel32  # type: ignore[attr-defined]
    max_pids = max(1, min(max_pids, 65536))
    while True:
        pids = (ctypes.c_ulong * max_pids)()
        needed = ctypes.c_ulong()
        if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):
            return []
        if needed.value < ctypes.sizeof(pids) or max_pids == 65536:
            break
        max_pids = min(max_pids * 2, 65536)
    samples: list[tuple[str, int, int, str | None]] = []
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
            named = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
            name = Path(buffer.value).name if named else f"pid {pid}"
            # The path classifies the owner and is then dropped; only the name and the class are kept.
            samples.append((name, int(counters.WorkingSetSize), int(pid), image_owner(buffer.value) if named else None))
        finally:
            kernel32.CloseHandle(handle)
    return group_processes(samples, limit, lane_pids)


SERVER_EXECUTABLES = ("llama-server.exe", "llama-server")
LANE_SERVER_LABEL = "Local LLM server"
PAIR_LABEL = "NVIDIA PAIR"
# NVIDIA PAIR (Personal AI Router) is recognised by its install folder or image name (winsys.image_owner). The image
# path is read only to classify the process and is never stored or published.
image_owner = winsys.image_owner


def group_processes(samples: list[tuple], limit: int = 3, lane_pids: frozenset[int] = frozenset()) -> list[dict[str, Any]]:
    """Process samples (name, working set[, pid[, owner]]) as rows grouped by name, ignoring case as Windows does (the
    first spelling seen labels the group).

    A sample whose pid owns a declared lane port joins the "Local LLM server" group (flagged ``lane``); a PAIR-owned
    sample joins one "NVIDIA PAIR" group (``owner: pair``), so PAIR's llama-server is never read as an AGIW lane.
    The largest `limit` groups are kept, plus the lane group and any other llama-server group wherever they rank:
    like the Mac guard's named groups, model servers are never cut before their sizes are attached."""
    groups: dict[str, list[Any]] = {}
    for sample in samples:
        name, size = sample[0], sample[1]
        pid = sample[2] if len(sample) > 2 else None
        owner = sample[3] if len(sample) > 3 else None
        if type(pid) is int and pid in lane_pids:
            key, label, kind = "\0lane", LANE_SERVER_LABEL, "lane"
        elif owner == "pair":
            key, label, kind = "\0pair", PAIR_LABEL, "pair"
        else:
            key, label, kind = name.casefold(), name[:80], None
        group = groups.setdefault(key, [label, 0, 0, kind])
        group[1] += size
        group[2] += 1
    ranked = sorted(groups.values(), key=lambda group: group[1], reverse=True)
    kept = [group for group in ranked if group[3] == "lane" or (group[3] is None and group[0].casefold() in SERVER_EXECUTABLES)]
    kept_ids = {id(group) for group in kept}
    others = [group for group in ranked if id(group) not in kept_ids][:max(0, limit)]
    rows = []
    for label, size, count, kind in sorted(kept + others, key=lambda group: group[1], reverse=True):
        row: dict[str, Any] = {"label": label, "residentBytes": size, "processCount": count, "gpuAllocBytes": None}
        if kind == "lane":
            row["lane"] = True
        elif kind == "pair":
            row["owner"] = "pair"
        rows.append(row)
    return rows


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
    truncated: dict[str, bool] = {}
    oldest_pending = None
    stale_claims = 0
    stale_pending = 0
    complete = True
    try:
        if is_link_or_junction(root) or not root.is_dir():
            raise OSError("queue root is not a real directory")
        for state in ("pending", "claimed", "completed"):
            directory = root / state
            if is_link_or_junction(directory):
                raise OSError("queue state folder is linked")
            total = 0
            truncated[state] = False
            if not directory.is_dir() and state in ("pending", "claimed"):
                raise FileNotFoundError("required queue folder missing")
            if directory.is_dir():
                with os.scandir(directory) as entries:
                    for examined, entry in enumerate(entries):
                        if examined >= QUEUE_MAX_ENTRIES:
                            complete = False
                            truncated[state] = True
                            break
                        if not entry.name.endswith(".json"):
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            complete = False
                            continue
                        if total >= QUEUE_MAX_ENTRIES:
                            truncated[state] = True
                            complete = False
                            break
                        total += 1
                        age = now - entry.stat(follow_symlinks=False).st_mtime
                        if age < 0 or not isinstance(read_json(Path(entry.path)), dict):
                            raise ValueError("queue entry future-dated or malformed")
                        if state == "pending":
                            oldest_pending = age if oldest_pending is None else max(oldest_pending, age)
                            if age > STALE_PENDING_SECONDS:
                                stale_pending += 1
                        elif state == "claimed" and age > STALE_CLAIM_SECONDS:
                            stale_claims += 1
            counts[state] = total
    except (OSError, ValueError):
        return ({"status": None, "runId": None, "pipelines": [], "pipelinesComplete": False, "queue": None},
                _source("route-queue", "Route queue", "unavailable", "Route queue unreadable, hidden, malformed or future-dated"))
    pending = counts.get("pending", 0)
    claimed_live = counts.get("claimed", 0) - stale_claims
    # A job nobody has picked up for a day is a leftover, not work waiting for a lane.
    pending_live = pending - stale_pending
    # A claim file says a worker took a job, not that it is still executing (it may have crashed), so
    # recent claims read "claimed" (execution unknown) and never "running".
    status = "unknown" if not complete else "claimed" if claimed_live > 0 else "queued" if pending_live else "idle"
    pipeline = {"status": status, "stage": None, "runId": None, "pipelines": [],
                # Counts alone cannot certify the empty per-route projection.
                "pipelinesComplete": complete and not any(counts.values()),
                "queue": {"complete": complete, "pending": pending, "stalePending": stale_pending, "claimed": counts.get("claimed", 0), "staleClaimed": stale_claims,
                          "completed": counts.get("completed", 0),
                          "oldestPendingSeconds": round(oldest_pending, 1) if oldest_pending is not None else None,
                          "staleAfterSeconds": STALE_CLAIM_SECONDS}}
    detail = f"{pending} pending · {counts.get('claimed', 0)} claimed · {counts.get('completed', 0)} completed"
    if any(truncated.values()):
        pipeline["status"] = None
        queue = pipeline["queue"]
        queue["truncated"] = truncated
        for state, capped in truncated.items():
            if capped:
                queue[state] = None
        if truncated["pending"]:
            queue["stalePending"] = queue["oldestPendingSeconds"] = None
        if truncated["claimed"]:
            queue["staleClaimed"] = None
        detail = " · ".join(f"{counts[state]}{'+' if truncated[state] else ''} {state}"
                            for state in ("pending", "claimed", "completed")) + "; queue scan incomplete"
        return pipeline, _source("route-queue", "Route queue", "unavailable", detail)
    if not complete:
        # A skipped matching entry may contain work. Preserve the explicit scan
        # failure and withhold exact queue totals, including apparent zeroes.
        for key in ('pending', 'claimed', 'completed', 'stalePending', 'staleClaimed', 'oldestPendingSeconds'):
            pipeline['queue'][key] = None
        detail = 'Queue entry could not be inspected; counts unknown'
    if stale_claims:
        detail += f" · {stale_claims} claim{'s' if stale_claims != 1 else ''} older than 15 min (likely abandoned)"
    if stale_pending:
        detail += (f" · {stale_pending} pending untouched for over a day (stale, not queued; oldest "
                   f"{oldest_pending / 86400:.0f} d)")
    if claimed_live > 0:
        detail += f" · {claimed_live} recent claim{'s' if claimed_live != 1 else ''}; execution unknown"
    return pipeline, _source("route-queue", "Route queue", "live" if complete else "incomplete", detail)


# ------------------------------------------------------------------- share
def share_health(share_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    lab = share_root / "llm-lab"
    try:
        present = any(share_root.iterdir())
    except FileNotFoundError:
        present = False
    except OSError:
        detail = "Share unreadable; presence unknown"
        return ({"state": "unknown", "root": str(share_root), "detail": detail},
                _source("sharedchami", "SharedChami", "unavailable", detail))
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
    up = [lane for lane in lanes if lane["status"] in LANE_UP]
    mismatched = [lane["id"] for lane in lanes if lane["status"] == "identity_mismatch"]
    if not gate.is_file():
        return {"id": "nisi", "label": "Nisi gate", "state": "unknown",
                "detail": "Nisi v0.2 gate adapter not found in the bin folder", "version": version}
    if mismatched:
        return {"id": "nisi", "label": "Nisi gate", "state": "needs-action", "version": version,
                "detail": f"Lane identity mismatch on {', '.join(mismatched)}; the gate would review with the wrong model"}
    if len(up) == len(lanes) and len(lanes) >= 2:
        names = {lane["id"]: public_id(lane["expectedModel"]) for lane in up}
        return {"id": "nisi", "label": "Nisi gate", "state": "ready", "version": version,
                "authorModel": names.get("deep"), "reviewerModel": names.get("fast"),
                "detail": "Gate installed; author and reviewer lanes answering as declared"}
    unknown = [lane["id"] for lane in lanes if lane["status"] not in LANE_UP | {"loading", "identity_mismatch", "not_agiw"}]
    if unknown or not lanes:
        return {"id": "nisi", "label": "Nisi gate", "state": "unknown", "version": version,
                "detail": "Gate installed; lane readiness unknown" + (" on " + ", ".join(unknown) if unknown else "")}
    if up:
        return {"id": "nisi", "label": "Nisi gate", "state": "partial", "version": version,
                "detail": f"Only the {up[0]['id']} lane answers; the gate needs both"}
    return {"id": "nisi", "label": "Nisi gate", "state": "partial", "version": version,
            "detail": "Gate installed; no lane answers"}


# --------------------------------------------------------------- Mac identity
# 0.2.0 dropped the LAN probe of the Mac's LM Studio. The Mac's :1234 is NVIDIA PAIR's loopback-only proxy, so the
# probe failed by design and only produced a permanent "LAN check failed" limitation. The Mac now appears from its own
# presence on SharedChami (and the paired peer link when enabled), attributed only to the identity configured in
# llm-lab/mac-reach/hosts.json. Nothing on this PC sends a request to the Mac.
def configured_mac_name(share_root: Path) -> str | None:
    """The configured Mac identity (hostname, else mDNS name) from llm-lab/mac-reach/hosts.json."""
    return configured_mac_identity(share_root)[0]


def configured_mac_identity(share_root: Path) -> tuple[str | None, str]:
    """Mac name plus a reason distinguishing absent configuration from an unreadable identity."""
    try:
        data = read_json(share_root / "llm-lab" / "mac-reach" / "hosts.json")
    except FileNotFoundError:
        return None, "unconfigured"
    except (OSError, ValueError, json.JSONDecodeError):
        return None, "config-unreadable"
    if not isinstance(data, dict):
        return None, "config-unreadable"
    if "mac" not in data:
        return None, "unconfigured"
    mac = data.get("mac") if isinstance(data, dict) else None
    if not isinstance(mac, dict):
        return None, "config-unreadable"
    for key in ("hostname", "mdns"):
        raw = mac.get(key)
        if not isinstance(raw, str) or not raw.isprintable():
            continue
        name = raw.strip()
        labels = (name[:-1] if name.endswith(".") else name).split(".")
        if len(name) <= 253 and all(PEER_HOST.fullmatch(label) for label in labels):
            return name, "configured"
    return None, "config-unreadable"


def short_host(name: Any) -> str | None:
    """The first DNS label, case-folded: how a configured name is matched to a report's host."""
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip().split(".")[0].casefold() or None


# ---------------------------------------------------------------- NVIDIA PAIR
# Ports PAIR is known to listen on (proxy :1234/:8080/:11434, engine :8081, cluster 14318-14323). A listener on one of
# them that is not owned by a recognised PAIR process is reported as unattributed, never as PAIR.
PAIR_KNOWN_PORTS = frozenset((1234, 8080, 8081, 11434, *range(14318, 14324)))
PAIR_MAX_ROWS = 16


def _bind_scope(address: Any) -> str:
    if address in ("127.0.0.1", "::1"):
        return "loopback"
    if address in ("0.0.0.0", "::"):
        return "all"
    return "address"  # bound to one specific (LAN) address


def pair_component(census: dict[str, Any] | None) -> dict[str, Any]:
    """NVIDIA PAIR on this PC from process and listener ownership only; no HTTP request is ever sent to PAIR.

    ``census`` is winsys.PairCensus.sample(): {"ok", "processes": [{pid, name, owner}], "listeners": [{pid, local,
    port}], "uninspected"}. PAIR's own engine flags are not read here, so nothing is claimed about its models, and
    its llama-server is listed as PAIR's engine, never as an AGIW lane.
    """
    base = {"id": "pair", "label": "NVIDIA PAIR", "evidence": "process+listener"}
    if not isinstance(census, dict) or census.get("ok") is not True:
        return {**base, "state": "unknown", "processCount": None, "engineCount": None, "listeners": [],
                "unattributedPorts": [], "lanExposed": None, "uninspected": None,
                "detail": "NVIDIA PAIR presence unknown (process list unavailable)"}
    processes = [p for p in census.get("processes") or []
                 if isinstance(p, dict) and p.get("owner") == "pair" and type(p.get("pid")) is int]
    pids = {p["pid"] for p in processes}
    engine_pids = {p["pid"] for p in processes
                   if isinstance(p.get("name"), str) and p["name"].casefold() in SERVER_EXECUTABLES}
    listeners: list[dict[str, Any]] = []
    unattributed: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for row in census.get("listeners") or []:
        if not isinstance(row, dict) or type(row.get("port")) is not int or type(row.get("pid")) is not int:
            continue
        scope = _bind_scope(row.get("local"))
        owned = row["pid"] in pids
        key = (row["port"], scope, owned)
        if key in seen or not (owned or row["port"] in PAIR_KNOWN_PORTS):
            continue
        seen.add(key)
        if owned:
            listeners.append({"port": row["port"], "bind": scope,
                              "role": "engine" if row["pid"] in engine_pids else "pair"})
        else:
            unattributed.append({"port": row["port"], "bind": scope})
    listeners.sort(key=lambda item: (item["port"], item["bind"]))
    unattributed.sort(key=lambda item: (item["port"], item["bind"]))
    lan = sorted({item["port"] for item in listeners if item["bind"] != "loopback"})
    uninspected = census.get("uninspected") if type(census.get("uninspected")) is int else 0
    if processes:
        state = "running"
        where = ", ".join(f":{item['port']}" + (" (loopback)" if item["bind"] == "loopback" else "")
                          for item in listeners[:8]) or "no listener seen"
        qualifier = "at least " if uninspected else ""
        detail = (f"{qualifier}{len(processes)} PAIR process{'es' if len(processes) != 1 else ''}, {qualifier}{len(engine_pids)} "
                  f"engine{'s' if len(engine_pids) != 1 else ''} (not AGIW lanes); listening on {where}")
        if lan:
            detail += "; open to the LAN on " + ", ".join(f":{port}" for port in lan[:8]) + " (PAIR serves plain HTTP)"
    else:
        state = "unknown" if uninspected or unattributed else "not-seen"
        detail = "No NVIDIA PAIR process seen" + (f" ({uninspected} process{'es' if uninspected != 1 else ''} "
                                                  "could not be inspected)" if uninspected else "")
    if unattributed:
        detail += "; PAIR's usual ports held by unidentified processes: " + ", ".join(
            f":{item['port']}" for item in unattributed[:8])
    return {**base, "state": state, "processCount": len(processes) if state != "unknown" and not uninspected else None,
            "engineCount": len(engine_pids) if state != "unknown" and not uninspected else None,
            "processCountAtLeast": len(processes) if uninspected else None,
            "engineCountAtLeast": len(engine_pids) if uninspected else None, "uninspected": uninspected,
            "listeners": listeners[:PAIR_MAX_ROWS], "unattributedPorts": unattributed[:PAIR_MAX_ROWS],
            "lanExposed": True if lan else None if uninspected or unattributed else False, "detail": detail}


def pair_public(component: Any) -> dict[str, Any] | None:
    """The PAIR row as published on the share: state, counts and listener ports only."""
    if not isinstance(component, dict) or component.get("id") != "pair":
        return None
    state = component.get("state") if component.get("state") in ("running", "not-seen", "unknown") else "unknown"
    rows = component.get("listeners") if isinstance(component.get("listeners"), list) else []
    unattributed = component.get("unattributedPorts") if isinstance(component.get("unattributedPorts"), list) else []
    return {"state": state,
            "uninspected": component.get("uninspected") if type(component.get("uninspected")) is int else None,
            "processCount": component.get("processCount") if type(component.get("processCount")) is int else None,
            "engineCount": component.get("engineCount") if type(component.get("engineCount")) is int else None,
            "processCountAtLeast": component.get("processCountAtLeast") if type(component.get("processCountAtLeast")) is int else None,
            "engineCountAtLeast": component.get("engineCountAtLeast") if type(component.get("engineCountAtLeast")) is int else None,
            "listeners": [{"port": row["port"], "bind": row["bind"]} for row in rows[:PAIR_MAX_ROWS]
                          if isinstance(row, dict) and type(row.get("port")) is int
                          and row.get("bind") in ("loopback", "all", "address")],
            "unattributedPorts": [{"port": row["port"], "bind": row["bind"]} for row in unattributed[:PAIR_MAX_ROWS]
                                  if isinstance(row, dict) and type(row.get("port")) is int
                                  and 1 <= row["port"] <= 65535 and row.get("bind") in ("loopback", "all", "address")],
            "lanExposed": component.get("lanExposed") if isinstance(component.get("lanExposed"), bool) else None}


# ------------------------------------------------------------------- hands off
# D5: hands off the PC while any GPU is above 40 % or a game is running. This is a threshold, not a game detector:
# AGIW's own lanes also lift the 3080 Ti past 40 %, so every busy GPU carries the cause (AGIW lanes, other processes,
# mixed, or unknown), split by process from Windows' per-process GPU engine counters.
HANDS_OFF_GPU_PERCENT = 40.0
ATTRIBUTION_NEGLIGIBLE_PERCENT = 10.0
# SHQueryUserNotificationState (shellapi.h): 2 QUNS_BUSY (full-screen app), 3 QUNS_RUNNING_D3D_FULL_SCREEN,
# 4 QUNS_PRESENTATION_MODE, 7 QUNS_APP (a Windows Store app); 1 not present (locked, screen saver),
# 5 accepts notifications, 6 quiet time.
FULLSCREEN_REASONS = {3: "fullscreen-game", 2: "fullscreen-app", 7: "store-app", 4: "presentation-mode"}
QUIET_NOTIFICATION_STATES = (5, 6)


def _gpu_split(gpu: dict[str, Any], lane_pids: frozenset[int] | None) -> tuple[float | None, float | None]:
    """(AGIW lane %, other %) of one adapter's utilisation, from its busiest engine's per-process shares."""
    shares = gpu.get("engineShares")
    utilization = _number(gpu.get("utilizationPercent"), 0, 100)
    readings = gpu.get("utilizationReadings")
    if isinstance(readings, dict) and _number(readings.get("windows-gpu-counters"), 0, 100) != utilization:
        return None, None  # PDH shares cannot explain a higher reading from another source.
    if lane_pids is None or utilization is None or not isinstance(shares, list) or not shares \
            or len(shares) > 64 or gpu.get("engineSharesComplete") is False:
        return None, None
    lane = other = 0.0
    for share in shares[:64]:
        pid = share.get("pid") if isinstance(share, dict) else None
        value = _number(share.get("percent"), 0, 10000) if isinstance(share, dict) else None
        if type(pid) is not int or value is None:
            return None, None  # one unreadable share voids the split rather than guessing
        if pid in lane_pids:
            lane += value
        else:
            other += value
    total = lane + other
    if total <= 0:
        return None, None
    return round(utilization * lane / total, 1), round(utilization * other / total, 1)


def hands_off(gpus: list[dict[str, Any]] | None, *, gpu_live: bool, lane_pids: frozenset[int] | None,
              notification_state: Any, gpu_complete: bool = True) -> dict[str, Any]:
    """The hands-off flag (critic correction 3: ``handsOff``, never ``gaming``).

    active True: a GPU is above 40 % or a game/app/presentation suppresses notifications. active False: every GPU has a live
    reading at or below 40 % and the session reports no full-screen app. Anything unread makes it None (unknown);
    unknown is never shown as clear.
    """
    reasons: list[str] = []
    unknown: list[str] = []
    rows: list[dict[str, Any]] = []
    readable = gpu_live is True and isinstance(gpus, list) and bool(gpus)
    if not readable or not gpu_complete:
        unknown.append("gpu-load-unknown")
    causes = []
    for gpu in (gpus if readable else []):
        if not isinstance(gpu, dict):
            unknown.append("gpu-load-unknown")
            continue
        utilization = _number(gpu.get("utilizationPercent"), 0, 100)
        readings = gpu.get("utilizationReadings")
        readings = {key: _number(readings.get(key), 0, 100) for key in ("nvidia-smi", "windows-gpu-counters")} \
            if isinstance(readings, dict) else {}
        known = [value for value in (utilization, *readings.values()) if value is not None]
        utilization = max(known) if known else None
        agiw, other = _gpu_split({**gpu, "utilizationPercent": utilization}, lane_pids)
        engine = _string(gpu.get("busiestEngine"), 32)
        row = {"index": gpu.get("index") if type(gpu.get("index")) is int else None, "name": _string(gpu.get("name"), 64),
               "utilizationPercent": utilization, "agiwPercent": agiw, "otherPercent": other, "cause": None}
        if readings:
            row["utilizationReadings"] = readings
        if utilization is None:
            unknown.append("gpu-load-unknown")
        elif utilization > HANDS_OFF_GPU_PERCENT:
            if agiw is None:
                cause = "unknown"
            elif other < ATTRIBUTION_NEGLIGIBLE_PERCENT:
                cause = "agiw-lanes"
            elif agiw < ATTRIBUTION_NEGLIGIBLE_PERCENT:
                cause = "other-processes"
            else:
                cause = "mixed"
            row["cause"] = cause
            # Load on the 3D engine from other processes is what a game looks like; compute lanes use other engines.
            if cause in ("other-processes", "mixed") and engine and engine.casefold() == "3d":
                row["graphics"] = True
            causes.append(cause)
        rows.append(row)
    if causes:
        reasons.append("gpu-busy")
    fullscreen = None
    if type(notification_state) is int and notification_state in FULLSCREEN_REASONS:
        fullscreen = FULLSCREEN_REASONS[notification_state]
        reasons.append(fullscreen)
    elif type(notification_state) is int and notification_state in QUIET_NOTIFICATION_STATES:
        fullscreen = "none"
    else:
        unknown.append("fullscreen-unknown")
    unknown = list(dict.fromkeys(unknown))
    active = True if reasons else None if unknown else False
    if fullscreen == "fullscreen-game":
        cause = "game"
    elif not causes:
        cause = "fullscreen" if reasons else None
    elif "unknown" in causes:
        cause = "unknown"
    elif "mixed" in causes or {"agiw-lanes", "other-processes"}.issubset(causes):
        cause = "mixed"
    elif "other-processes" in causes:
        cause = "other-processes"
    else:
        cause = "agiw-lanes" if fullscreen == "none" and not unknown else "unknown"
    busy = [row for row in rows if row["cause"]]
    if active is True:
        parts = [f"{row['name'] or 'GPU'} at {row['utilizationPercent']:.0f}%"
                 + (f" (AGIW lanes {row['agiwPercent']:.0f}%, other {row['otherPercent']:.0f}%)"
                    if row["agiwPercent"] is not None else " (cause unknown)") for row in busy[:8]]
        if len(busy) > 8:
            parts.append(f"{len(busy) - 8} more busy GPUs")
        if fullscreen in FULLSCREEN_REASONS.values():
            parts.append({"fullscreen-game": "a full-screen game is running", "fullscreen-app": "a full-screen app is running",
                          "store-app": "a Windows Store app is running", "presentation-mode": "presentation mode is on"}[fullscreen])
        detail = "Hands off: " + "; ".join(parts)
        if cause == "agiw-lanes":
            detail += " (AGIW's own lane work)"
    elif active is False:
        detail = f"Clear: every GPU at or below {HANDS_OFF_GPU_PERCENT:.0f}% and no full-screen app"
    else:
        detail = "Unknown: " + ", ".join({"gpu-load-unknown": "GPU load not read",
                                          "fullscreen-unknown": "full-screen state not read"}[u] for u in unknown)
    return {"active": active, "state": {True: "hands-off", False: "clear", None: "unknown"}[active],
            "reasons": reasons, "unknown": unknown, "cause": cause,
            "agiwOnly": (cause == "agiw-lanes") if active is True and cause != "unknown" else None,
            "thresholdPercent": HANDS_OFF_GPU_PERCENT, "fullscreen": fullscreen,
            "gpus": sorted(rows, key=lambda row: row["cause"] is None)[:8], "detail": detail}


def hands_off_public(value: Any) -> dict[str, Any]:
    """handsOff as published in presence and status: categories and percentages only, never process names."""
    if not isinstance(value, dict) or value.get("active") not in (True, False, None):
        return {"active": None, "state": "unknown", "reasons": [], "unknown": [], "cause": None, "agiwOnly": None,
                "thresholdPercent": HANDS_OFF_GPU_PERCENT, "gpus": []}
    words = lambda items: [item for item in items if isinstance(item, str) and item.isprintable()][:8] \
        if isinstance(items, list) else []  # noqa: E731
    gpus = []
    for row in (value.get("gpus") if isinstance(value.get("gpus"), list) else [])[:8]:
        if isinstance(row, dict):
            gpus.append({key: row.get(key) for key in ("index", "name", "utilizationPercent", "agiwPercent",
                                                       "otherPercent", "cause", "utilizationReadings")})
    active = value.get("active")
    return {"active": active, "state": {True: "hands-off", False: "clear", None: "unknown"}[active],
            "reasons": words(value.get("reasons")), "unknown": words(value.get("unknown")), "cause": _string(value.get("cause"), 24),
            "agiwOnly": value.get("agiwOnly") if isinstance(value.get("agiwOnly"), bool) else None,
            "thresholdPercent": HANDS_OFF_GPU_PERCENT, "gpus": gpus}


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
    empty = {"schemaVersion": 1, "journal": "codemode-runs", "complete": False, "inFlight": [], "recent": [], "lastSuccess": None,
             "clientsRecent": {}, "availability": {"state": "unknown", "reason": "code-runs folder unreadable"},
             "liveAvailable": False}
    unreadable = without_manifest = 0
    complete = True
    try:
        if is_link_or_junction(runs_root) or not runs_root.is_dir():
            raise OSError("code-runs root is not a real directory")
        runs = []
        with os.scandir(runs_root) as entries:
            for examined, entry in enumerate(entries):
                if examined >= QUEUE_MAX_ENTRIES:
                    complete = False
                    unreadable += 1
                    break
                if len(entry.name) >= 15 and entry.name[:8].isdigit():
                    if not entry.is_dir(follow_symlinks=False) or is_link_or_junction(Path(entry.path)):
                        complete = False
                        unreadable += 1
                        continue
                    manifest = Path(entry.path) / "manifest.json"
                    try:
                        runs.append((manifest.stat().st_mtime, manifest))
                    except FileNotFoundError:
                        without_manifest += 1
                        continue
                    except OSError:
                        complete = False
                        unreadable += 1
                        continue
    except OSError:
        return empty, _source("codemode-runs", "codemode runs", "unavailable", "code-runs folder unreadable")
    runs.sort(reverse=True)
    complete = complete and len(runs) <= CODEMODE_RUNS_MAX
    recent: list[dict[str, Any]] = []
    malformed = 0
    truncated = max(0, len(runs) - CODEMODE_RUNS_MAX)
    for mtime, manifest in runs[:CODEMODE_RUNS_MAX]:
        if mtime > now:
            malformed += 1
            continue
        try:
            data = read_json(manifest, CODEMODE_MANIFEST_MAX)
        except (OSError, ValueError, json.JSONDecodeError):
            malformed += 1
            continue
        if not isinstance(data, dict) or not isinstance(data.get("metrics"), list):
            malformed += 1
            continue
        if any(not isinstance(metric, dict) for metric in data["metrics"]):
            malformed += 1
        if len(data["metrics"]) > 8:
            truncated += 1
        run_id = _string(data.get("run_id"), 40) or manifest.parent.name
        certified = data.get("verification") == "CERTIFIED"
        for index, metric in enumerate((data.get("metrics") or [])[:8]):
            if not isinstance(metric, dict) or metric.get("kind") != "local":
                continue
            endpoint = _string(metric.get("endpoint"), 80) or ""
            try:
                parts = urlsplit(endpoint)
                lane = ports.get(parts.port) if parts.hostname in ("127.0.0.1", "localhost", "::1") else None
            except ValueError:
                lane = None
            tokens, seconds = _number(metric.get("tok"), 0, 1 << 24), _number(metric.get("sec"), 0, 86400)
            stage = _string(metric.get("stage"), 20)
            model = _string(metric.get("served") or metric.get("model"), 80)
            finish = _string(metric.get("finish"), 40)
            if not stage or not model or tokens is None or seconds is None or not finish:
                complete = False
                continue
            mismatch = metric.get("served_mismatch") is True
            finish = metric.get("finish")
            state = "success" if finish == "stop" else "error" if finish in ("length", "error", "content_filter") else "unknown"
            if state == 'unknown':
                complete = False
            recent.append({"id": f"{manifest.parent.name}:{run_id}:{stage}:{index}", "lane": lane, "client": "codemode", "stage": stage,
                           "model": _string(metric.get("served") or metric.get("model"), 80),
                           "state": "invalid-result" if mismatch else state,
                           "completionTokens": tokens, "elapsedSeconds": seconds,
                           "approxPerSecond": round(tokens / seconds, 1) if seconds > 0 else None,
                           "predictedPerSecond": None, "promptPerSecond": None,
                           "flags": ["hit-token-limit"] if metric.get("finish") == "length" else [],
                           "runVerification": _string(data.get("verification"), 24), "certified": certified,
                           "ageSeconds": round(max(0.0, now - mtime), 1)})
    complete = complete and not malformed and len(recent) <= 40
    recent = recent[:40]
    success = next((row for row in recent if row["state"] == "success" and row["model"] and row["elapsedSeconds"] is not None), None)
    available = complete and not (unreadable or malformed or truncated or without_manifest)
    jobs = {**empty, "recent": recent, "complete": available, "observationAgeSeconds": 0.0,
            "availability": {"state": "available" if available else "unknown",
                             "reason": "Completed run manifests read; live jobs not observed" if available else
                                       "Run history incomplete; a manifest was unreadable, malformed or beyond the scan limit"},
            "truncated": bool(truncated),
            "lastSuccess": ({key: success[key] for key in ("id", "lane", "model", "elapsedSeconds", "ageSeconds", "client")}
                            if success else None)}
    newest = f"; newest run {recent[0]['ageSeconds'] / 86400:.1f} d ago" if recent else ""
    skipped = f"; {malformed} malformed manifest{'s' if malformed != 1 else ''} skipped" if malformed else ""
    if without_manifest:
        skipped += f"; {without_manifest} runs without manifest"
    if truncated:
        skipped += f"; {truncated} manifests exceeded the stage limit"
    return jobs, _source("codemode-runs", "codemode runs", "live" if available else "unavailable",
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


def source_current(source: Any) -> bool:
    """An explicit degraded read never authorizes retained values from that source."""
    if not isinstance(source, dict) or source.get("state") not in ("live", "recorded"):
        return False
    age = source.get("ageSeconds", 0)  # older source rows omitted the age
    return (_number(age, 0, 30) is not None and source.get("complete") is not False
            and source.get("truncated") is not True)


def enforce_snapshot_evidence(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Fail closed at publication, including cached payloads whose reader lost coverage.

    Legacy direct projections without a source envelope retain their old contract;
    the collector always provides the envelope. Work on a copy, not a probe cache.
    """
    import copy
    data = copy.deepcopy(snapshot)
    if "sources" not in data:
        return data
    sources = {s["id"]: s for s in data.get("sources") or [] if isinstance(s, dict) and "id" in s}
    known = lambda sid: source_current(sources.get(sid))
    inventory = known("lane-inventory") and known("lane-config")
    invalid_lanes, unknown_lanes = set(), set()
    lane_by_id = {lane.get("id"): lane for lane in data.get("lanes") or []}
    for row in data.get("models") or []:
        age = row.get("ageSeconds")
        if type(row.get("loaded")) is not bool or _number(age, 0, data.get("laneFreshSeconds", 11.8)) is None:
            row.update(loaded=None, state="unknown", queued=None)
            lane_id = (row.get("metadata") or {}).get("lane")
            invalid_lanes.add(lane_id)
            # Residency unknown is compatible with affirmative non-serving evidence
            # (loading, a different model, or a pinned PAIR owner). Preserve that reason.
            status = lane_by_id.get(lane_id, {}).get("status")
            if _number(age, 0, data.get("laneFreshSeconds", 11.8)) is None or status not in ("loading", "identity_mismatch", "not_agiw"):
                unknown_lanes.add(lane_id)
    activity = inventory and known("llama-slots") and not invalid_lanes
    if "lanes" in data:
        for lane in data.get("lanes") or []:
            if lane.get("status") == "undeclared":
                continue
            if not inventory or lane.get("id") in unknown_lanes:
                lane.update(status="unknown", phase=None, slots=[], slotsBusy=None, slotsTotal=None,
                            liveTokensPerSecond=None, detail="Lane inventory unavailable or incomplete")
            elif not activity and lane.get("status") in ("idle", "busy"):
                lane.update(status="slots_unknown", phase=None, slots=[], slotsBusy=None, slotsTotal=None,
                            liveTokensPerSecond=None, detail="Slot activity unavailable or incomplete")
        for row in data.get("models") or []:
            if not activity:
                row.update(parallel=None, context=None)
                row.setdefault("metadata", {}).update(slotsBusy=None, slotsTotal=None, liveTokensPerSecond=None,
                    laneStatus="unknown", detail="Slot activity unavailable or incomplete")
            if not inventory:
                row.update(loaded=None, state="unknown", queued=None)
            elif not activity:
                row.update(state="loaded" if row.get("loaded") is True else "unknown", queued=None)
        for lane_id, lane in (data.get("windowsWorker") or {}).get("lanes", {}).items():
            if not inventory or lane_id in unknown_lanes:
                lane.update(up=None, health={"state": "unknown", "reason": "Lane inventory unavailable or incomplete"})
            if not activity:
                lane.update(slotsBusy=None, slotsTotal=None)
    if not known("lane-processes"):
        data["localCallers"] = None
        for lane in data.get("lanes") or []:
            lane["callers"] = None
        for row in data.get("models") or []:
            row.setdefault("metadata", {})["callers"] = None
    if not known("route-queue"):
        data["pipeline"] = {"status": None, "pipelines": [], "queue": None,
                            "reason": "Route queue unavailable or incomplete"}
    if not known("codemode-runs") and isinstance(data.get("windowsJobs"), dict):
        data["windowsJobs"].update(availability={"state": "unknown", "reason": "Job history unavailable or incomplete"}, liveAvailable=False)
    hands = data.get("handsOff")
    # A fresh positive signal is independent of attribution: a busy GPU or
    # full-screen notification can each establish hands-off on its own. Clear
    # requires the complete GPU and notification census, but never lane owners.
    if not known("hands-off") or (not known("pc-gpu") and not (isinstance(hands, dict) and hands.get("active") is True)):
        data["handsOff"] = {"state": "unknown", "active": None, "cause": None,
                            "detail": "GPU load or process attribution unavailable or incomplete"}
    elif not known("lane-processes") and isinstance(hands, dict) and hands.get("cause") in ("agiw-lanes", "other-processes", "mixed"):
        hands.update(cause="unknown", agiwOnly=None)
        hands["detail"] = "Hands off: load detected; process attribution unknown"
        for gpu in hands.get("gpus") or []:
            gpu.update(cause="unknown", agiwPercent=None, otherPercent=None)
    if not known("pc-gpu"):
        data["pcGpus"] = []
        if isinstance(data.get("windowsWorker"), dict):
            data["windowsWorker"]["gpus"] = []
    if not known("nvidia-pair") and isinstance(data.get("components"), list):
        data["components"] = [pair_component(None) if component.get("id") == "pair" else component
                              for component in data["components"]]
    if not known("mem-guard"):
        data["memory"] = {"level": "unknown", "consumersKnown": False,
                          "reasons": ["Memory source unavailable or incomplete"]}
    mac = data.get("macPeer")
    if isinstance(mac, dict) and mac.get("via") == "sharedchami" and "peers" in data:
        want = short_host(mac.get("configuredName") or mac.get("name"))
        matching = next((peer for peer in data.get("peers") or []
                         if peer.get("platform") == "macos" and short_host(peer.get("host")) == want), None)
        if not matching or matching.get("modelsComplete") is not True or matching.get("fresh") is not True:
            mac.update(loadedCount=None, models=[], detail="Mac model inventory unavailable or incomplete")
    if not known("agiw-peers"):
        if isinstance(data.get("macPeer"), dict):
            data["macPeer"].update(state="unknown", via=None, models=[], loadedCount=None, ageSeconds=None,
                reason="report-unreadable", detail="Mac presence source unavailable or incomplete")
        for peer in data.get("peers") or []:
            peer.update(modelsComplete=False, fresh=False)
    for machine in data.get("machines") or []:
        local = machine.get("role") == "this-machine"
        if local and (not inventory or unknown_lanes) or not local and not known("agiw-peers"):
            machine.update(state="unknown", health="unknown", loadedCount=None,
                           reason="evidence-incomplete", detail="Machine evidence unavailable or incomplete")
        if local:
            machine["handsOff"] = (data.get("handsOff") or {}).get("active")
    # Publish invalid source states as unknown as well, so consumers cannot reuse a live flag.
    for source in data.get("sources") or []:
        if source.get("state") == "live" and not source_current(source):
            source["state"] = "unavailable"
    return data


def pc_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    """llm-lab/status/windows-status.json for the Mac's status reader (schema 1, host windows), built from one
    snapshot. ``id`` is the inventory name the reader expects; the lane alias and served name ride along as
    ``aliases``/``servedModel``. Model ids, lane states, versions and counts only; no paths, prompts or keys."""
    snapshot = enforce_snapshot_evidence(snapshot)
    lanes = snapshot.get("lanes") or []
    mismatch = [lane["id"] for lane in lanes if lane.get("status") == "identity_mismatch"]
    foreign = [lane["id"] for lane in lanes if lane.get("status") == "not_agiw"]
    # Failed reads are unknown; only affirmative non-serving evidence can say down (lanes_health).
    health = lanes_health(lanes)
    models = [{"id": public_id(INVENTORY_IDS.get(lane.get("expectedModel"), lane.get("expectedModel"))) or "unknown",
               "aliases": [public_id(lane.get("expectedModel"))], "host": "windows", "lane": lane.get("id"), "port": lane.get("port"),
               # Only an answering lane is evidence of residency; no answer (or a stale one) is unknown, never inactive.
               "loadedState": "loaded" if lane.get("status") in LANE_UP else "unknown",
               "activity": lane.get("phase") if lane.get("status") in ("idle", "busy") else "unknown",
               "servedModel": public_id(lane.get("servedModel")), "slotsBusy": lane.get("slotsBusy"), "slotsTotal": lane.get("slotsTotal"),
               "liveTokensPerSecond": lane.get("liveTokensPerSecond")} for lane in lanes if lane.get("status") != "undeclared"]
    gpus = [{key: gpu.get(key) for key in ("index", "name", "utilizationPercent", "memoryUsedMiB", "memoryTotalMiB", "temperatureC")}
            for gpu in snapshot.get("pcGpus") or []]
    memory = snapshot.get("memory") or {}
    queue = (snapshot.get("pipeline") or {}).get("queue") or {}
    components = []
    raw_components = snapshot.get("components")
    for component in raw_components if isinstance(raw_components, (list, tuple)) else []:
        if not isinstance(component, dict):
            continue
        row = {"name": component.get("label"), "status": component.get("state")}
        if component.get("id") in ("nisi", "jev"):
            row["id"] = component["id"]
            for key in ("label", "state", "detail"):
                value = _string(component.get(key), 128)
                if value is not None:
                    row[key] = value
            # The peer projector converts numbers to float when checking finiteness.
            age = _number(component.get("lastJudgedAgeSeconds"), low=0, high=sys.float_info.max)
            if age is not None:
                row["lastJudgedAgeSeconds"] = age
        components.append(row)
    limitations = []
    undeclared = [lane["id"] for lane in lanes if lane.get("status") == "undeclared"]
    if undeclared:
        unreadable = [lane["id"] for lane in lanes if lane.get("status") == "undeclared" and lane.get("configReason") == "unreadable"]
        missing = [lane_id for lane_id in undeclared if lane_id not in unreadable]
        if unreadable:
            limitations.append("lane config unreadable; lane unknown: " + ", ".join(unreadable))
        if missing:
            limitations.append("lanes not configured: " + ", ".join(missing))
    if mismatch:
        limitations.append("lane identity mismatch: " + ", ".join(mismatch))
    if foreign:
        limitations.append("lane port held by NVIDIA PAIR, not counted: " + ", ".join(foreign))
    mac_peer = snapshot.get("macPeer")
    mac_peer = mac_peer if isinstance(mac_peer, dict) else {}
    if mac_peer.get("state") != "linked":
        # 0.2.0 has no LAN probe: the Mac is known only from its own fresh presence on the share.
        limitations.append("no Mac configured in hosts.json; Mac reports not attributed"
                           if mac_peer.get("reason") == "unconfigured" else
                           "Mac identity unknown; hosts.json unreadable or invalid"
                           if mac_peer.get("reason") == "config-unreadable" else
                           "mac presence on SharedChami not fresh; Mac state unknown")
    if mac_peer.get("lanObservationAvailable") is True:
        failure = mac_peer.get("lanFailure")
        limitations.append(f"mac LM Studio LAN check failed ({failure}); cause unknown" if failure
                           else "mac LM Studio LAN reachability unknown")
    pair = pair_public(next((c for c in raw_components if isinstance(c, dict) and c.get("id") == "pair"), None)
                       if isinstance(raw_components, (list, tuple)) else None)
    status = {"schemaVersion": 1, "observedAt": snapshot.get("observedAt"), "host": "windows",
              "producer": buildinfo.producer(snapshot.get("build"), snapshot.get("observerVersion")),
              "health": health, "tasks": [],
              "models": models, "modelsComplete": len(models) == len(lanes), "gpus": gpus, "adapters": [a.get("name") for a in snapshot.get("pcAdapters") or []],
              "memory": {"level": memory.get("level"), "availablePercent": memory.get("availablePercent")},
              "routeQueue": {key: queue.get(key) for key in ("pending", "claimed", "staleClaimed", "completed")} if queue else None,
              "components": components,
              "limitations": limitations,
              "handsOff": hands_off_public(snapshot.get("handsOff"))}
    if pair is not None:
        status["pair"] = pair
    return status


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


# Darwin's temp and config directories are these symlinks. They are not a
# junction planted in a share or a peer-link file.
_DARWIN_DIRECTORY_SYMLINKS = frozenset({"/var", "/tmp", "/etc"})


def _platform_directory_symlink(path: Path) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return os.path.abspath(path) in _DARWIN_DIRECTORY_SYMLINKS
    except (OSError, ValueError):
        return False


def _publish_anchor(path: Path, top: Path) -> tuple[Path, list[tuple[Path, str]]]:
    """Real directory to open, then each directory at ``top`` or below it.

    Symlinks above ``top`` (Darwin ``/var`` -> ``/private/var``) are resolved so the
    handle walk does not have to open them. A reparse point at ``top`` or below stays
    a logical path and is refused by the caller before that name is opened.
    """
    if is_link_or_junction(top):
        raise ValueError("linked status directory refused")
    missing = []
    current = top
    while not current.exists():
        if current == current.parent:
            raise ValueError("publish root unavailable")
        if is_link_or_junction(current):
            raise ValueError("linked status directory refused")
        missing.append(current.name)
        current = current.parent
    if is_link_or_junction(current):
        raise ValueError("linked status directory refused")
    anchor = Path(os.path.realpath(current))
    logical = current
    steps = []
    for name in reversed(missing):
        logical = logical / name
        steps.append((logical, name))
    if path.parent != top:
        logical = top
        for name in path.parent.relative_to(top).parts:
            logical = logical / name
            steps.append((logical, name))
    return anchor, steps


@contextmanager
def _locked_publish_parent(path: Path, stop_at: Path | None):
    """Open the target's parent by handle, never following a directory reparse point."""
    path = Path(os.path.abspath(path))
    top = Path(os.path.abspath(stop_at)) if stop_at is not None else path.parent.parent
    if top != path.parent and top not in path.parent.parents:
        raise ValueError("publish target is outside its root")
    anchor, steps = _publish_anchor(path, top)
    if not IS_WINDOWS:
        # Hold each parent while opening its child. All creation, replacement and cleanup
        # below use the retained final descriptor, never the checked absolute pathname.
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise ValueError("descriptor-anchored publication unavailable")
        handles = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            handles.append(os.open(anchor, flags))
            for logical, name in steps:
                if is_link_or_junction(logical) and not _platform_directory_symlink(logical):
                    raise ValueError("linked status directory refused")
                try:
                    os.mkdir(name, dir_fd=handles[-1])
                except FileExistsError:
                    pass
                handles.append(os.open(name, flags, dir_fd=handles[-1]))
            try:
                target = os.stat(path.name, dir_fd=handles[-1], follow_symlinks=False)
            except FileNotFoundError:
                target = None
            if target is not None and stat.S_ISLNK(target.st_mode):
                raise ValueError("linked status directory refused")
            yield handles[-1]
        finally:
            for handle in reversed(handles):
                os.close(handle)
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
        # The resolved publish root is stable. Each child is opened relative to the
        # preceding handle, so renaming any visible path cannot redirect later operations.
        handle = create(str(anchor), 0x80, 0x7, None, 3, 0x02200000, None)
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        handles.append(handle)
        if _handle_is_reparse(kernel32, handle):
            raise ValueError("linked status directory refused")
        for logical, name in steps:
            if is_link_or_junction(logical) and not _platform_directory_symlink(logical):
                raise ValueError("linked status directory refused")
            handle = _nt_create_relative(handles[-1], name, directory=True)
            if handle == invalid:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            if _handle_is_reparse(kernel32, handle):
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
        fd = os.open(tmp.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            # Windows refuses to replace a file another process (e.g. the Mac reading over SMB) holds open at that
            # instant; the reader closes within milliseconds, so a few short retries ride it out.
            for attempt in range(6):
                try:
                    os.replace(tmp.name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except BaseException:
            try:
                os.unlink(tmp.name, dir_fd=parent)
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
    status_models = status["models"]
    models_total = len(snapshot.get("lanes") or [])
    if models_total > 32:
        status_models = sorted(status_models, key=lambda model: model["loadedState"] != "loaded")[:32]
    models = []
    for model in status_models:
        row = {key: model[key] for key in ("id", "aliases", "loadedState", "activity", "slotsBusy", "slotsTotal")}
        # The Mac can select presence ahead of status from the same sample.
        # Keep its model details, using pc_status's path-safe servedModel.
        for key in ("lane", "servedModel"):
            value = model.get(key)
            if isinstance(value, str) and value.strip() and value.isprintable():
                row[key] = value[:64] if key == "lane" else value
        rate = _number(model.get("liveTokensPerSecond"), low=0, high=sys.float_info.max)
        if rate is not None:
            row["liveTokensPerSecond"] = rate
        models.append(row)
    presence = {"schemaVersion": 1, "kind": "agiw-peer", "host": host, "platform": "windows",
                "edition": "windows", "version": snapshot.get("observerVersion"), "producer": status["producer"],
                "build": buildinfo.build_public(snapshot.get("build")),
                "observedAt": snapshot.get("observedAt"),
                "observedUnix": snapshot.get("sampledAt"), "health": status["health"],
                "modelsTotal": models_total, "models": models,
                "gpus": status["gpus"], "memory": status["memory"],
                # D5 hands-off flag: the Mac shows "PC: hands off" with its cause (AGIW lanes vs other processes).
                "handsOff": status["handsOff"],
                "dashboard": {"bind": "loopback", "port": snapshot.get("dashboardPort")},
                "reads": ["agiw-peers", "mac-status.json"]}
    if "pair" in status:
        presence["pair"] = status["pair"]
    return presence


PEER_LINK_STATUS_FILE_NAME = "peer-link-status.json"


def peer_link_status_doc(snapshot: dict[str, Any], host: str) -> dict[str, Any]:
    """The private local file the shipped pairing service serves (``peer_link.py run --status-file``): the presence
    document plus the Nisi and Jev rows. peer_link.project_status reduces it to its allowlist before anything leaves
    this PC, and the file lives in the install's private state folder, never on the guest-writable share."""
    doc = peer_presence(snapshot, host)
    doc["components"] = [row for row in pc_status(snapshot)["components"] if row.get("id") in ("nisi", "jev")]
    return doc


def _peer_models(raw_models: Any, legacy: bool = False) -> tuple[list[dict[str, Any]], bool]:
    """Keep a bounded, unique list and distinguish retained rows from a complete inventory."""
    if not isinstance(raw_models, list):
        return [], False
    models, seen = [], set()
    valid = True
    for model in raw_models[:32]:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"].strip() \
                or legacy and model.get("host") != "mac":
            continue
        model_id = model["id"][:120]
        if model_id in seen:
            continue
        seen.add(model_id)
        loaded = model.get("loadedState")
        if loaded not in STATUS_LOADED_STATES:
            valid = False
            loaded = "unknown"
        models.append({"id": model_id,
                       "loadedState": loaded,
                       "activity": _string(model.get("activity"), 24) or "unknown"})
    complete = valid and len(raw_models) <= 32 and len(models) == len(raw_models)
    return models, complete


RELAYED_PLATFORMS = {"mac": ("linux",)}  # relayer -> platforms it may relay (PLAN 3.2: the Mac relays Linux only)


def _validate_peer(value: Any, now: float) -> dict[str, Any] | None:
    if not isinstance(value, dict) or type(value.get("schemaVersion")) is not int \
            or value["schemaVersion"] != 1 or value.get("kind") != "agiw-peer":
        return None
    host = value.get("host")
    observed = _number(value.get("observedUnix"))
    if not isinstance(host, str) or not PEER_HOST.match(host) or observed is None or observed > now + 5:
        return None
    platform = _string(value.get("platform"), 24) or "unknown"
    # A relayed report (Linux, written by the Mac) keeps the source machine's observedUnix, so its age is the age of
    # the observation itself, never of the relay. Only a known relayer for a platform it may relay is accepted.
    relayed_by = value.get("relayedBy")
    if relayed_by is not None and (not isinstance(relayed_by, str) or relayed_by not in RELAYED_PLATFORMS
                                   or platform not in RELAYED_PLATFORMS[relayed_by]):
        return None
    raw_models = value.get("models")
    models, complete = _peer_models(raw_models)
    loaded_complete = (complete and value.get("loadedListComplete") is True
                       and all(model["loadedState"] in ("loaded", "inactive") for model in models))
    # Complete only when the peer sent a model list and every entry survived: a missing list, a dropped row or a cut at
    # 32 is not evidence of fewer loaded models, so the dashboard makes no count claim from it.
    complete = complete and value.get("modelsComplete") is not False and type(value.get("modelsTotal")) is int and value["modelsTotal"] == len(models)
    age = max(0.0, now - observed)
    return {"host": host, "platform": platform,
            "edition": _string(value.get("edition"), 24), "version": _string(value.get("version"), 24),
            "health": _string(value.get("health"), 16) or "unknown", "models": models, "modelsComplete": complete,
            "loadedListComplete": loaded_complete, "ageSeconds": age, "fresh": age <= PEER_FRESH_SECONDS, "source": "agiw-peers",
            "observedUnix": observed, "relayedBy": relayed_by}


def _legacy_mac_status(share_root: Path, now: float, *, on_refusal=None) -> dict[str, Any] | None:
    """The Mac's on-demand mac-status.json (written by Codex's status script) as a peer record."""
    path = share_root / "llm-lab" / "status" / "mac-status.json"

    def refused(reason):
        if on_refusal is not None:
            on_refusal(reason, "mac")
        return None

    try:
        value = read_json(path, PEER_MAX_BYTES)
        observed = __import__("datetime").datetime.fromisoformat(str(value["observedAt"]).replace("Z", "+00:00")).timestamp()
    except FileNotFoundError:
        return None
    except OSError:
        return refused("report-unreadable")
    except (ValueError, KeyError, TypeError, OverflowError):
        return refused("report-refused")
    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1 or value.get("host") != "mac":
        return refused("report-refused")
    if observed > now + 5:
        return refused("clock-ahead")
    models, complete = _peer_models(value.get("models"), legacy=True)
    age = max(0.0, now - observed)
    return {"host": "mac", "platform": "macos", "edition": "mac-status", "version": None, "verified": False,
            "health": _string(value.get("health"), 16) or "unknown", "models": models, "modelsComplete": complete,
            "ageSeconds": age,
            "fresh": age <= PEER_FRESH_SECONDS, "source": "mac-status.json", "observedUnix": observed, "relayedBy": None}


def read_peers(share_root: Path, self_host: str, now: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every other AGIW instance that has published presence, newest first, plus the Mac's status file."""
    peers: list[dict[str, Any]] = []
    directory = peer_dir(share_root)
    entries, examined, truncated = [], 0, False
    refusals: dict[str, int] = {}
    refusal_hosts: dict[str, str] = {}

    def refused(reason: str, host: str | None = None) -> None:
        refusals[reason] = refusals.get(reason, 0) + 1
        if host:
            refusal_hosts[host.casefold()] = reason

    scan_complete = False
    try:
        if is_link_or_junction(directory):
            refused("linked-folder")
        else:
            with os.scandir(directory) as scan:
                scan_complete = True
                for entry in scan:
                    examined += 1
                    if examined > PEER_SCAN_LIMIT:  # a flooded folder costs a bounded amount of work
                        truncated = True
                        break
                    stem = entry.name[:-5] if entry.name.endswith(".json") else None
                    if stem and PEER_HOST.match(stem) and entry.is_file(follow_symlinks=False):
                        entries.append(entry)
    except FileNotFoundError:
        entries = []
    except OSError:
        refused("peers-unreadable")
        entries = []
        scan_complete = False
    dated_entries = []
    for entry in entries:
        try:
            dated_entries.append((entry.stat(follow_symlinks=False).st_mtime, entry))
        except FileNotFoundError:
            scan_complete = False
            continue  # A peer can remove its file between enumeration and stat.
        except OSError:
            scan_complete = False
            refused("report-unreadable", entry.name[:-5])
    if len(dated_entries) > PEER_MAX_FILES:
        truncated = True
    scan_complete = scan_complete and not truncated and len(dated_entries) <= PEER_MAX_FILES
    entries = [entry for _, entry in sorted(dated_entries, key=lambda pair: pair[0], reverse=True)[:PEER_MAX_FILES]]
    seen_hosts: set[str] = set()
    for entry in entries:
        try:
            value = read_json(Path(entry.path), PEER_MAX_BYTES)
            peer = _validate_peer(value, now)
            if peer is None:
                observed = _number(value.get("observedUnix")) if isinstance(value, dict) else None
                refused("clock-ahead" if observed is not None and observed > now + 5 else "report-refused", entry.name[:-5])
        except OSError:
            refused("report-unreadable", entry.name[:-5])
            peer = None
        except (ValueError, TypeError, OverflowError, json.JSONDecodeError):
            refused("report-refused", entry.name[:-5])
            peer = None
        if peer is None:
            scan_complete = False
        if peer and entry.name[:-5].casefold() != peer["host"].casefold():
            refused("report-refused", entry.name[:-5])
            scan_complete = False
        # The file name must be the host it claims, and each host counts once (newest wins).
        if (peer and entry.name[:-5].lower() == peer["host"].lower() and peer["host"].lower() not in seen_hosts
                and peer["host"] != self_host and peer["host"] != host_id()):
            seen_hosts.add(peer["host"].lower())
            peer["verified"] = False  # unsigned share report; authenticated peers arrive with the pairing protocol
            peers.append(peer)
    if not any(p["platform"] == "macos" and p["fresh"] for p in peers):
        legacy = _legacy_mac_status(share_root, now, on_refusal=refused)
        if legacy:
            # Every candidate is stale here; show whichever report for that host is newest.
            same = [peer for peer in peers if peer["host"].lower() == legacy["host"].lower()]
            if all(legacy["ageSeconds"] <= peer["ageSeconds"] for peer in same):
                peers = [peer for peer in peers if peer not in same]
                peers.append(legacy)
    peers.sort(key=lambda p: p["ageSeconds"])
    fresh = [p for p in peers if p["fresh"]]
    detail = (f"{len(fresh)} reported ({', '.join(p['host'] for p in fresh)}; unsigned share files, not verified)" if fresh
              else "No AGIW peer has published in the last 30 s" if scan_complete
              else "Peer presence unknown; folder scan unavailable or incomplete")
    if truncated:
        refused("scan-truncated")
    detail = (f"{'at least ' if refusals else ''}{len(fresh)} reported ({', '.join(p['host'] + (' claims relay by ' + p['relayedBy'] if p.get('relayedBy') else '') for p in fresh)}; "
              "unsigned share files, not verified)" if fresh
              else "Peer presence unknown; reports could not be fully read" if refusals else
              "No AGIW peer has published in the last 30 s" if scan_complete else "Peer presence unknown; no complete folder scan")
    if truncated:
        detail += f"; folder scan limited to {PEER_SCAN_LIMIT} entries and {PEER_MAX_FILES} reports"
    if refusals:
        detail += "; " + ", ".join(f"{reason}: {count}" for reason, count in refusals.items())
    stale = [p for p in peers if not p["fresh"]]
    if stale:
        detail += f"; {len(stale)} stale ({', '.join(p['host'] + ' ' + str(int(p['ageSeconds'] // 3600)) + ' h' for p in stale)})"
    reason = ("peers-unreadable" if "peers-unreadable" in refusals or "linked-folder" in refusals else
              "clock-ahead" if "clock-ahead" in refusals else
              "report-unreadable" if "report-unreadable" in refusals else
              "report-refused" if "report-refused" in refusals else
              "scan-truncated" if "scan-truncated" in refusals else
              "available" if fresh else "no-report")
    scan_complete = scan_complete and not bool(refusals)
    return peers, {**_source("agiw-peers", "AGIW peers", "live" if fresh else "recorded" if scan_complete and not peers else "unavailable", detail),
                   "scanComplete": scan_complete, "observedAgeSeconds": 0, "reason": reason, "complete": not bool(refusals), "truncated": truncated,
                   "refusals": refusals, "refusalHosts": refusal_hosts}


# ------------------------------------------------------------------- machines
# One row per machine (this PC, the Mac, the Linux node): up / down / unknown plus a named reason and how the evidence
# arrived. A missing, stale or unattributable report is unknown, never down; "down" comes only from a fresh report
# that says nothing is serving, and a share report is labelled share-reported (unsigned) rather than verified.
MACHINE_REASONS = {
    ("local", "ok"): ("up", "serving"), ("local", "degraded"): ("up", "partly-serving"),
    ("local", "down"): ("down", "no-lane-serving"), ("local", "unknown"): ("unknown", "lane-state-unknown"),
    ("peer", "ok"): ("up", "reported-serving"), ("peer", "degraded"): ("up", "reported-partly-serving"),
    ("peer", "down"): ("down", "reported-down"),
}
MACHINE_LABELS = {"windows": "PC", "macos": "Mac", "linux": "Linux"}
RELAYER_LABELS = {"mac": "Mac"}


def _iso_utc(unix: Any) -> str | None:
    value = _number(unix, low=0, high=4e9)
    if value is None:
        return None
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_words(age: float) -> str:
    return f"{age:.0f} s" if age < 120 else f"{age / 60:.0f} min" if age < 7200 else f"{age / 3600:.1f} h"


def _peer_machine(record: dict[str, Any], platform: str, paired: list[dict[str, Any]] | None) -> dict[str, Any]:
    relayed = record.get("relayedBy")
    trust = ("relayed" if relayed else "legacy-share" if record.get("source") == "mac-status.json" else "share-reported")
    age = record.get("ageSeconds")
    observed = record.get("observedUnix")
    health = record.get("health") if record.get("health") in ("ok", "degraded", "down") else "unknown"
    models = record.get("models") if isinstance(record.get("models"), list) else []
    # As everywhere else: a count only from a complete, non-empty list with every residency known.
    known = record.get("modelsComplete") is True and bool(models) and all(
        isinstance(m, dict) and m.get("loadedState") in ("loaded", "inactive") for m in models)
    how = (f"relayed by the {RELAYER_LABELS.get(relayed, relayed)} (unsigned share file, not verified)" if relayed
           else f"reported through SharedChami ({record.get('source')}; unsigned, not verified)")
    if record.get("fresh") is True:
        state, reason = MACHINE_REASONS.get(("peer", health), ("unknown", "health-unknown"))
        detail = f"{MACHINE_LABELS.get(platform, platform)} {how}, observed {_age_words(age)} ago; health {health}"
        if health == "degraded" and not any(isinstance(m, dict) and m.get("loadedState") == "loaded" for m in models):
            state, reason = "unknown", "reported-service-unknown"
            detail += "; no model reported serving"
    else:
        state, reason = "unknown", "stale"
        since = _iso_utc(observed)
        detail = (f"Not observed since {since} ({_age_words(age)}); state unknown" if since and isinstance(age, (int, float))
                  else "Last report too old; state unknown")
    # The paired link names only a host kind, so it can annotate the one Mac and the one Linux row, not other PCs.
    kind = {"macos": "mac", "linux": "linux"}.get(platform)
    link = next((row for row in paired or [] if isinstance(row, dict) and kind and row.get("host") == kind
                 and row.get("paired") is True), None)
    return {"id": record.get("host"), "name": record.get("host"), "platform": platform,
            "label": MACHINE_LABELS.get(platform, platform), "role": "peer", "state": state, "reason": reason,
            "detail": detail, "trust": trust, "relayedBy": relayed, "source": record.get("source"),
            "ageSeconds": age if isinstance(age, (int, float)) else None, "observedUnix": observed,
            "health": health if record.get("fresh") is True else "unknown",
            "loadedCount": sum(m["loadedState"] == "loaded" for m in models) if known and record.get("fresh") is True else None,
            "version": record.get("version"), "handsOff": None,
            "peerLink": link.get("state") if link else None}


def _missing_machine(platform: str, name: str | None, reason: str, detail: str) -> dict[str, Any]:
    return {"id": short_host(name) or {"macos": "mac"}.get(platform, platform), "name": name, "platform": platform,
            "label": MACHINE_LABELS.get(platform, platform), "role": "peer", "state": "unknown", "reason": reason,
            "detail": detail, "trust": None, "relayedBy": None, "source": None, "ageSeconds": None,
            "observedUnix": None, "health": "unknown", "loadedCount": None, "version": None, "handsOff": None,
            "peerLink": None}


def machine_rows(*, host: str, sampled_at: Any, lanes: list[dict[str, Any]], hands: dict[str, Any] | None,
                 version: str, peers: list[dict[str, Any]], mac_name: str | None,
                 paired: list[dict[str, Any]] | None = None, peers_source: dict[str, Any] | None = None,
                 mac_config_reason: str | None = None) -> list[dict[str, Any]]:
    """machines[]: this PC and the configured Mac, then any other PCs.

    Linux is retired. A share file that claims to be Linux stays in peers[] for
    diagnosis and does not become a machine row, so an unsigned relay cannot
    supply health, age or a loaded count.
    """
    health = lanes_health(lanes)
    state, reason = MACHINE_REASONS[("local", health)]
    statuses = [lane.get("status") for lane in lanes]
    serving = sum(status in LANE_UP for status in statuses)
    rows = [{"id": host, "name": host, "platform": "windows", "label": "This PC", "role": "this-machine",
             "state": state, "reason": reason,
             "detail": {"serving": "Every lane answers as declared", "partly-serving": f"{serving} of {len(lanes)} lanes answer as declared",
                        "no-lane-serving": "No lane serves as declared (loading, identity mismatch or port held by PAIR)",
                        "lane-state-unknown": "Lane state unknown (not configured, pending, stale or unreadable)"}[reason],
             "trust": "local", "relayedBy": None, "source": "local", "ageSeconds": 0.0, "observedUnix": _number(sampled_at, low=0),
             "health": health,
             # Residency is known only when every lane answered; otherwise a count would hide unknown lanes.
             "loadedCount": serving if lanes and serving == len(lanes) else None,
             "version": version, "handsOff": hands.get("active") if isinstance(hands, dict) else None, "peerLink": None}]
    records = sorted((p for p in peers or [] if isinstance(p, dict)), key=lambda p: p.get("ageSeconds") or 0)
    want = short_host(mac_name)
    if want is None:
        reason = "config-unreadable" if mac_config_reason == "config-unreadable" else "unconfigured"
        detail = ("Mac identity unknown; hosts.json unreadable or invalid" if reason == "config-unreadable" else
                  "No Mac configured in llm-lab/mac-reach/hosts.json; Mac reports are not attributed")
        rows.append(_missing_machine("macos", None, reason, detail))
    else:
        mac = next((p for p in records if p.get("platform") == "macos" and short_host(p.get("host")) == want), None)
        reason = (peers_source or {}).get("refusalHosts", {}).get(want) or (peers_source or {}).get("reason", "no-report")
        if reason not in ("clock-ahead", "report-refused", "peers-unreadable", "report-unreadable", "scan-truncated"):
            reason = "no-report"
        detail = ((peers_source or {}).get("detail") or "Mac presence report could not be read") if reason != "no-report" else "No report from the configured Mac on SharedChami; state unknown"
        rows.append(_peer_machine(mac, "macos", paired) if mac else _missing_machine(
            "macos", mac_name, reason, detail))
    for record in records:
        if record.get("platform") == "windows" and short_host(record.get("host")) != short_host(host):
            rows.append(_peer_machine(record, "windows", paired))
    return rows


PEER_LINK_MAX_BYTES = 65536
PEER_LINK_MAX_ROWS = 32
PEER_LINK_STATES = ("available", "connecting", "connected", "stale", "offline")
PEER_LINK_UUID = __import__("re").compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


def peer_link_status_path() -> Path:
    """The local pairing service's fixed Windows path; no config-supplied path or service actions."""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "AGIW" / "peer-link" / "peers.json"


def _peer_link_opened_path(stream, path: Path) -> None:
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        raise ValueError("not a regular file")
    if not IS_WINDOWS:
        return
    import msvcrt  # noqa: PLC0415 - Windows only

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = msvcrt.get_osfhandle(stream.fileno())
    if _handle_is_reparse(kernel32, handle):
        raise ValueError("linked file refused")
    final_path = kernel32.GetFinalPathNameByHandleW
    final_path.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32)
    final_path.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = final_path(handle, buffer, len(buffer), 0)
    if not 0 < length < len(buffer):
        raise ValueError("opened path unavailable")
    actual = buffer.value
    if actual.startswith("\\\\?\\"):
        actual = actual[4:]
    if os.path.normcase(actual) != os.path.normcase(str(path)):
        raise ValueError("opened path redirected")


def _read_peer_link_file(path: Path) -> Any:
    if not path.is_absolute() or path.drive.startswith("\\\\"):
        raise ValueError("local absolute path required")
    for entry in (*path.parents, path):
        if is_link_or_junction(entry) and not _platform_directory_symlink(entry):
            raise ValueError("linked path refused")
    path = Path(os.path.realpath(path))
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("not a regular file")
    with path.open("rb") as stream:
        _peer_link_opened_path(stream, path)
        payload = stream.read(PEER_LINK_MAX_BYTES + 1)
    if len(payload) > PEER_LINK_MAX_BYTES:
        raise ValueError("oversized peer report")

    return loads_strict(payload.decode("utf-8-sig"))


def _peer_link_age(value: Any, now: float) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        observed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return None
        age = now - observed.timestamp()
        return age if math.isfinite(age) else None
    except (ValueError, OverflowError, OSError):
        return None


def _peer_link_status(value: Any, host: str, now: float) -> dict[str, Any] | None:
    if host not in ("mac", "windows", "linux") or not isinstance(value, dict) \
            or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        return None
    kind = value.get("platform") if value.get("kind") == "agiw-peer" else value.get("host")
    age = _peer_link_age(value.get("observedAt"), now)
    models = value.get("models")
    if kind != host or age is None or not -5 <= age <= 30 or not isinstance(models, list) or len(models) > 32:
        return None
    rows = []
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"].strip() \
                or not model["id"].isprintable() or model.get("loadedState") not in STATUS_LOADED_STATES \
                or model.get("host") not in (None, host):
            return None
        row = {"id": model["id"][:256], "loadedState": model["loadedState"]}
        if model.get("activity") in ("idle", "busy", "generating", "unknown"):
            row["activity"] = model["activity"]
        rows.append(row)
    components = value.get("components")
    component_rows = []
    for item in components[:4] if isinstance(components, list) else []:
        if isinstance(item, dict) and item.get("id") in ("nisi", "jev"):
            row = {"id": item["id"]}
            for key in ("label", "state", "detail"):
                text = _string(item.get(key), 128)
                if text is not None:
                    row[key] = text
            component_rows.append(row)
    out = {"schemaVersion": 1, "host": host, "observedAt": value["observedAt"], "models": rows}
    if component_rows:
        out["components"] = component_rows
    return out


def read_peer_link(now: float, *, enabled: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Read the pairing service's report, never start it or independently assert authentication."""
    if enabled is not True:
        return [], None
    unknown = _source("peer-link", "Peer link", "unavailable", "Pairing service status unknown (invalid report)")
    try:
        if _number(now, low=0, high=sys.float_info.max) is None:
            return [], unknown
        doc = _read_peer_link_file(peer_link_status_path())
        identity = doc.get("self") if isinstance(doc, dict) else None
        if not isinstance(doc, dict) or type(doc.get("schemaVersion")) is not int or doc["schemaVersion"] != 1 \
                or doc.get("protocol") != "agiw-peer/2" or not isinstance(doc.get("peers"), list) \
                or not isinstance(identity, dict) or identity.get("host") != "windows" \
                or not isinstance(identity.get("node"), str) or not PEER_LINK_UUID.fullmatch(identity["node"]):
            return [], unknown
        age = _peer_link_age(doc.get("observedAt"), now)
        fresh = age is not None and -5 <= age <= 30
        rows, seen = [], set()
        for item in doc["peers"][:PEER_LINK_MAX_ROWS]:
            if not isinstance(item, dict) or not isinstance(item.get("node"), str) \
                    or not PEER_LINK_UUID.fullmatch(item["node"]) or item["node"] == identity["node"]:
                continue
            if item["node"] in seen:
                for row in rows:
                    if row["node"] == item["node"]:
                        row.update(name=None, host="unknown", paired=None, state="unknown", status=None)
                continue
            seen.add(item["node"])
            paired = item.get("paired") if isinstance(item.get("paired"), bool) else None
            host = item.get("host") if item.get("host") in ("mac", "windows", "linux") else "unknown"
            state = item.get("state") if fresh and item.get("state") in PEER_LINK_STATES else "unknown"
            status = None
            if state == "connected":
                status = _peer_link_status(item.get("status"), host, now) if paired is True else None
                if status is None:
                    state = "unknown"
            rows.append({"node": item["node"], "name": _string(item.get("name"), 64), "host": host,
                         "paired": paired, "state": state, "status": status})
        if not fresh:
            detail = (f"Pairing service report stale ({age:.1f} s); status unknown" if age is not None and age > 30
                      else "Pairing service report clock unknown; status unknown")
            return rows, _source("peer-link", "Peer link", "unavailable", detail)
        paired_count = sum(row["paired"] is True for row in rows)
        connected_count = sum(row["state"] == "connected" for row in rows)
        return rows, _source("peer-link", "Peer link", "live",
                             f"Pairing service reports {paired_count} paired, {connected_count} connected (authenticated)",
                             round(max(0.0, age), 1))
    except FileNotFoundError:
        return [], _source("peer-link", "Peer link", "unavailable", "Pairing service status unavailable (no peers.json)")
    except (OSError, ValueError, TypeError, RecursionError):
        return [], unknown


def python_info() -> str:
    return f"Python {sys.version.split()[0]}"
