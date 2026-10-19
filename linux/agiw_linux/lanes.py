"""Portable PC observer logic, vendored for a fixture-tested Linux parity boundary.

The Windows projection helpers remain unchanged for parity tests. Linux publication
uses collect's platform projection. Network I/O must be explicitly supplied by the
Linux allowlisted probe layer; importing this module never probes a machine.
"""
from __future__ import annotations

import json
from . import pc_buildinfo as buildinfo
from .jsonutil import loads_strict, _reject_constant
import math
import re
import sys
from typing import Any, Callable

HTTP_TIMEOUT = 0.7
HTTP_MAX_BYTES = 256_000
MAX_SLOTS = 32
SLOTS_TIMEOUT = 4.0
Fetch = Callable[[str, float, int], Any]
LANE_UP = {"idle", "busy", "slots_unknown"}
MEMORY_LEVELS = ((20.0, "ok"), (12.0, "watch"), (6.0, "tight"), (0.0, "critical"))
STATUS_LOADED_STATES = ("loaded", "inactive", "unknown")
INVENTORY_IDS = {"openai/gpt-oss-20b": "gpt-oss-20b", "qwen3.8-27b": "Qwen3.8-27B Q4_K_M"}
PEER_PROBE_MAX_AGE = 45.0
PEER_FRESH_SECONDS = 30.0
PEER_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")


SERVER_EXECUTABLES = ("llama-server.exe", "llama-server")
PAIR_KNOWN_PORTS = frozenset((1234, 8080, 8081, 11434, *range(14318, 14324)))
PAIR_MAX_ROWS = 16
HANDS_OFF_GPU_PERCENT = 40.0
RELAYED_PLATFORMS = {"mac": ("linux",)}


class OwnerUnverified(ValueError):
    """A lane listener could not be correlated to its owner."""


def http_get_json(url: str, timeout: float = HTTP_TIMEOUT, max_bytes: int = HTTP_MAX_BYTES) -> Any:
    """Require a Linux allowlisted transport; the portable layer never opens sockets."""
    raise ValueError("an explicit allowlisted fetch is required")

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

def memory_level(available_percent: float, commit_percent: float | None) -> str:
    level = next(name for floor, name in MEMORY_LEVELS if available_percent >= floor)
    # A nearly exhausted commit limit fails allocations even with free RAM.
    if commit_percent is not None and commit_percent >= 95 and level in ("ok", "watch"):
        level = "tight"
    return level

def short_host(name: Any) -> str | None:
    """The first DNS label, case-folded: how a configured name is matched to a report's host."""
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip().split(".")[0].casefold() or None

def _bind_scope(address: Any) -> str:
    if address in ("127.0.0.1", "::1"):
        return "loopback"
    if address in ("0.0.0.0", "::"):
        return "all"
    return "address"

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

def live_decode_rate(previous: dict[tuple[int, int], tuple[float, float]], lane: dict[str, Any], now: float) -> tuple[float | None, dict]:
    """Tokens per second generated across a lane's busy slots, from the change in each slot's decoded count
    between two polls of the same request. The lane rate is unknown unless every busy slot has complete,
    valid two-poll evidence; a partial sum would understate aggregate throughput."""
    current: dict[tuple[int, int], tuple[float, float]] = {}
    busy_slots = [slot for slot in lane.get("slots") or [] if slot.get("isProcessing")]
    if not busy_slots:
        return None, current
    total = 0.0
    complete = True
    for slot in busy_slots:
        decoded, slot_id, task = slot.get("decodedTokens"), slot.get("id"), slot.get("taskId")
        # Keyed by (slot, task): without llama-server's task id, two requests in one slot are
        # indistinguishable, so no rate is shown rather than a difference across requests.
        if not isinstance(decoded, (int, float)) or isinstance(decoded, bool) or not isinstance(slot_id, int) \
                or isinstance(slot_id, bool) or not isinstance(task, int) or isinstance(task, bool):
            complete = False
            continue
        key = (slot_id, task)
        current[key] = (float(decoded), now)
        before = previous.get(key)
        if before is None:
            complete = False
            continue
        delta, gap = decoded - before[0], now - before[1]
        if delta > 0 and 0.2 <= gap <= 10.0:
            total += delta / gap
        else:
            complete = False
    return (round(total, 1) if complete else None), current

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
    if not known("pc-gpu") or not known("lane-processes") or not known("hands-off"):
        data["handsOff"] = {"state": "unknown", "active": None, "cause": None,
                            "detail": "GPU load or process attribution unavailable or incomplete"}
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
