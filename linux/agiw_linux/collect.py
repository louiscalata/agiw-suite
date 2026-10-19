"""Assemble Linux observations and a bounded, allowlisted peer projection."""
from __future__ import annotations

import datetime as dt
from collections import Counter
import math
from pathlib import Path
import re
import socket
import time

from . import VERSION
from . import lanes, linsys, probes

MAX_MODELS = 32
GPU_KEYS = ("utilizationPercent", "memoryUsedMiB", "memoryTotalMiB", "temperatureC", "powerW")
MAX_NETWORK_ROWS = 32
GPU_FILES = {"utilizationPercent": "gpu_busy_percent", "memoryUsedMiB": "mem_info_vram_used",
             "memoryTotalMiB": "mem_info_vram_total", "temperatureC": "hwmon/hwmon[0-9]{1,4}/temp1_input",
             "powerW": "hwmon/hwmon[0-9]{1,4}/power1_input"}
CALLER_LANES = ("pair-llamacpp", "pair-ollama", "gpu-wrapper")
LANE_ID = re.compile(r"gpu-wrapper(?:-[0-9]{1,10})?|pair-llamacpp|pair-ollama|qwen35-vulkan|coordinator")
LANE_REASONS = frozenset(("observed", "not-observed", "unavailable", "timeout", "refused", "permission-denied",
    "invalid-response", "invalid-health", "invalid-models", "unverified-endpoint", "unverified-listener-owner",
    "unreachable", "loading", "identity_mismatch", "no_identity", "slots_unknown", "undeclared",
    "application-health-unverified", "process-inventory-unavailable", "listener-not-correlated-with-ollama-process",
    "staged-not-probed", "process-not-observed", "unit-state-unavailable", "intended-unit-release-unconfirmed",
    "process-unit-state-conflict", "intended-unit-active-observed", "intended-unit-disabled-observed",
    "intended-system-unit-absent-observed", "lane-inventory-unavailable-or-incomplete"))
OMITTED_SOURCES = ("cpu", "disks", "temperatures", "gpuLock", "pairCluster", "authority", "lastCodexRun", "idleAnalysis")


def number(value, low=0, high=1e15):
    return value if type(value) in (int, float) and math.isfinite(value) and low <= value <= high else None


def word(value, limit=128):
    return value[:limit] if isinstance(value, str) and value and value.isprintable() else None


def observed_activity(item, activity):
    busy, total = item.get("slotsBusy"), item.get("slotsTotal")
    if type(busy) is not int or type(total) is not int or not 0 <= busy <= total <= 32 or total == 0:
        return "unknown"
    return activity if activity == "idle" and busy == 0 or activity in ("busy", "generating") and busy > 0 else "unknown"


def iso(at):
    return dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def host_id(value=None):
    value = value if value is not None else socket.gethostname()
    value = re.sub(r"[^a-z0-9-]", "-", value.split(".")[0].lower())[:63].strip("-")
    return value or "linux"


def gpu_sources(item):
    raw = item.get("sources")
    raw = raw if isinstance(raw, dict) else {}
    return {key: raw.get(key) if isinstance(raw.get(key), str)
            and re.fullmatch(r"/sys/class/drm/card[0-9]{1,4}/device/" + GPU_FILES[key], raw[key]) else None
            for key in GPU_KEYS}


def historical_rates(raw, at):
    complete = isinstance(raw, list) and len(raw) <= 2 and all(isinstance(row, dict) for row in raw) \
        and len({row.get("role") for row in raw if isinstance(row.get("role"), str)}) == len(raw)
    rows, seen = [], set()
    for item in (raw if isinstance(raw, list) else [])[:16]:
        if not isinstance(item, dict) or item.get("role") not in ("code", "extract") or item["role"] in seen:
            continue
        role = item["role"]
        seen.add(role)
        observed = number(item.get("observedUnix"), high=at)
        rates = {key: number(item.get(key), high=1e7)
                 for key in ("promptTokensPerSecond", "generationTokensPerSecond")}
        known = complete and item.get("kind") == "last-measured" and observed is not None and any(v is not None for v in rates.values())
        reason = item.get("reason")
        rows.append({"role": role, "kind": "last-measured" if known else "unknown", "model": None,
                     "expectedModel": probes.ROLE_MODELS[role], "modelLabelSource": "role-map-unverified",
                     **(rates if known else {key: None for key in rates}),
                     "observedUnix": observed if known else None, "ageSeconds": round(at - observed, 1) if known else None,
                     "source": "wrapper log timings", "reason": "observed" if known else reason
                     if reason in ("unavailable", "unsupported-role", "clock-invalid", "no-timings") else "unavailable"})
    return rows


def process_name(value):
    return value if isinstance(value, str) and linsys.NAME.fullmatch(value) else "unknown"


def lanes_complete(raw):
    return isinstance(raw, list) and len(raw) <= 32 and all(isinstance(row, dict)
        and isinstance(row.get("id"), str) and LANE_ID.fullmatch(row["id"]) for row in raw) \
        and len({row["id"] for row in raw}) == len(raw)


def lane_status(raw):
    """Keep bounded lane failure evidence without URLs, paths, prompts or argv."""
    rows = []
    complete = lanes_complete(raw)
    for item in (raw if isinstance(raw, list) else [])[:32]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not LANE_ID.fullmatch(item["id"]):
            continue
        status = item.get("status", item.get("state"))
        status = status if status in ("idle", "busy", "serving", "slots_unknown", "unreachable", "loading",
            "identity_mismatch", "no_identity", "absent", "disabled", "active") else "unknown"
        reason = item.get("reason")
        reason = reason if isinstance(reason, str) and reason in LANE_REASONS else "not-observed"
        if not complete:
            status, reason = "unknown", "lane-inventory-unavailable-or-incomplete"
        activity = item.get("phase", item.get("activity"))
        activity = observed_activity(item, activity) if status in ("idle", "busy") else "unknown"
        health = "up" if complete and item.get("health") == "up" and status in (
            "idle", "busy", "serving", "slots_unknown", "active") else "down" if complete \
            and item["id"] == "coordinator" and item.get("health") == "down" and status in (
                "absent", "disabled") else "unknown"
        if health == "unknown":
            # Preserve diagnostic states (e.g. unreachable/timeout), never a claim
            # of observed activity, residency, absence or disabled service state.
            if status in ("idle", "busy", "serving", "active", "absent", "disabled"):
                status = "unknown"
            activity = "unknown"
        rows.append({"id": item["id"], "status": status,
                     "health": health,
                     "reason": reason, "activity": activity,
                     **{key: number(item.get(key)) if activity != "unknown" else None
                        for key in ("slotsBusy", "slotsTotal", "liveTokensPerSecond")}})
    return rows


def network_status(raw):
    raw = raw if isinstance(raw, dict) else {}
    listeners, callers = [], []
    raw_listeners, raw_callers = raw.get("listeners"), raw.get("callers")
    listeners_valid = isinstance(raw_listeners, list) and len(raw_listeners) <= MAX_NETWORK_ROWS
    callers_valid = isinstance(raw_callers, list) and len(raw_callers) <= MAX_NETWORK_ROWS
    for item in (raw_listeners if isinstance(raw_listeners, list) else [])[:MAX_NETWORK_ROWS]:
        if not isinstance(item, dict) or type(item.get("localPort")) is not int or not 1 <= item["localPort"] <= 65535:
            listeners_valid = False
            continue
        listeners.append({"localPort": item["localPort"], "name": process_name(item.get("name")),
                          "exposed": item.get("exposed") if type(item.get("exposed")) is bool else None,
                          "plaintext": item.get("plaintext") if type(item.get("plaintext")) is bool else None})
    for item in (raw_callers if isinstance(raw_callers, list) else [])[:MAX_NETWORK_ROWS]:
        if not isinstance(item, dict) or item.get("lane") not in CALLER_LANES \
                or type(item.get("connections")) is not int or not 1 <= item["connections"] <= 65536:
            callers_valid = False
            continue
        name = process_name(item.get("name"))
        if name == "unknown":
            callers_valid = False
        callers.append({"name": name, "lane": item["lane"], "connections": item["connections"]})
    complete = raw.get("state") == "up" and listeners_valid
    callers_complete = complete and callers_valid and raw.get("callersState") == "known"
    return {"state": "up" if complete else "unknown", "reason": "observed" if complete else "unavailable-or-incomplete",
            "listeners": listeners, "callers": callers,
            "callersState": "known" if callers_complete else "unknown",
            "callersReason": "observed" if callers_complete else "caller-visibility-unavailable-or-incomplete"}


def unknown_snapshot(reason="not-observed", now=None, host=None):
    at = time.time() if now is None else now
    return {"schemaVersion": 1, "host": "linux", "hostId": host_id(host), "edition": "linux",
            "observerVersion": VERSION, "observedAt": iso(at), "sampledAt": at,
            "health": "unknown", "reason": reason, "models": [], "modelsComplete": False,
            "gpus": None, "gpusComplete": False, "gpusState": "unknown",
            "memory": {"level": "unknown", "availablePercent": None},
            "lanes": [], "components": [], "activityKnown": False,
            "liveTokensPerSecond": None, "modelControl": {"supported": False}}


def project_status(snapshot):
    """Bounded peer fields only; private paths, arguments and arbitrary journal text stay local."""
    at = number(snapshot.get("sampledAt"))
    if at is None:
        raise ValueError("missing observation time")
    raw = snapshot.get("models")
    valid = isinstance(raw, list)
    raw = raw if valid else []
    # Check identities and lane attribution before the output cap. An ambiguous
    # alias in the omitted tail must still invalidate its retained counterpart.
    model_ids = Counter(word(lanes.public_id(row.get("id")), 120)
                        for row in raw if isinstance(row, dict))
    model_lanes = Counter(row.get("lane") for row in raw
                          if isinstance(row, dict) and isinstance(row.get("lane"), str))
    models = []
    raw_lanes = snapshot.get("lanes")
    complete_lanes = lanes_complete(raw_lanes)
    projected_lanes = lane_status(raw_lanes)
    observed_lanes = {row["id"]: row for row in projected_lanes} if complete_lanes else {}
    for item in sorted((m for m in raw if isinstance(m, dict)),
                       key=lambda m: m.get("loadedState") != "loaded")[:MAX_MODELS]:
        model_id = word(lanes.public_id(item.get("id")), 120)
        if model_id is None or item.get("loadedState") not in ("loaded", "inactive", "unknown"):
            valid = False
            continue
        row = {"id": model_id, "host": "linux", "loadedState": item["loadedState"],
               "activity": observed_activity(item, item.get("activity")) if item["loadedState"] == "loaded" else "unknown"}
        lane_id = item.get("lane")
        if not isinstance(lane_id, str) or not (lane_id == "pair-llamacpp"
                or re.fullmatch(r"gpu-wrapper(?:-[0-9]{1,10})?", lane_id)) \
                or observed_lanes.get(lane_id, {}).get("health") != "up":
            row.update(loadedState="unknown", activity="unknown", reason="lane-evidence-unavailable-or-incomplete")
        elif lane_id.startswith("gpu-wrapper") and model_lanes[lane_id] > 1:
            # A wrapper serves one identity. PAIR may enumerate several models;
            # only its per-model activity (not residency) is then unobserved.
            row.update(loadedState="unknown", activity="unknown", reason="conflicting-wrapper-models")
            valid = False
        elif (row["activity"] != observed_lanes[lane_id]["activity"]
                or any(item.get(key) != observed_lanes[lane_id].get(key) for key in ("slotsBusy", "slotsTotal"))):
            row.update(activity="unknown", reason="lane-activity-conflict")
        elif isinstance(lane_id, str) and model_lanes[lane_id] > 1:
            row.update(activity="unknown", reason="per-model-activity-unavailable")
        for key in ("lane", "lifecycle"):
            row[key] = word(item.get(key), 64)
        row["servedModel"] = word(lanes.public_id(item.get("servedModel")), 120)
        for key in ("slotsBusy", "slotsTotal", "liveTokensPerSecond"):
            row[key] = number(item.get(key)) if row["activity"] != "unknown" else None
        if row["activity"] != "generating" or row["liveTokensPerSecond"] == 0:
            row["liveTokensPerSecond"] = None
        models.append(row)
    # Public aliases and bounded IDs can collide even when raw endpoint IDs differ.
    # Retain the observations, but no one row can certify the ambiguous identity.
    for model in models:
        if model_ids[model["id"]] > 1:
            model.update(loadedState="unknown", activity="unknown", slotsBusy=None, slotsTotal=None,
                         liveTokensPerSecond=None, reason="ambiguous-model-identity")
    complete = snapshot.get("modelsComplete") is True and valid and len(models) == len(raw) \
        and len({model["id"] for model in models}) == len(models)
    # A complete bit and a syntactically valid subset are not a source witness.
    # The collector records all inventory producers before projection; cache
    # revalidation must carry that same bounded witness, including every wrapper.
    expected = snapshot.get("modelInventoryLanes")
    expected_valid = isinstance(expected, list) and 2 <= len(expected) <= 32 \
        and all(isinstance(lane, str) and (lane == "pair-llamacpp"
            or re.fullmatch(r"gpu-wrapper(?:-[0-9]{1,10})?", lane)) for lane in expected) \
        and len(set(expected)) == len(expected) and "pair-llamacpp" in expected \
        and any(lane.startswith("gpu-wrapper") for lane in expected)
    inventory_lanes = {lane_id for lane_id in observed_lanes
                       if lane_id == "pair-llamacpp" or lane_id.startswith("gpu-wrapper")}
    complete = complete and expected_valid and complete_lanes and set(expected) == inventory_lanes \
        and all(observed_lanes[lane]["health"] == "up" for lane in expected) \
        and all(model.get("lane") in inventory_lanes for model in models) \
        and all(count <= 1 for lane, count in model_lanes.items() if lane.startswith("gpu-wrapper"))
    gpu_rows = []
    raw_gpus = snapshot.get("gpus")
    for item in (raw_gpus if isinstance(raw_gpus, list) else [])[:8]:
        if isinstance(item, dict):
            sources = gpu_sources(item)
            gpu_rows.append({"name": word(item.get("name"), 100) or "GPU",
                             **{key: number(item.get(key), high={"utilizationPercent": 100, "temperatureC": 150,
                                 "powerW": 2000}.get(key, 2 ** 30)) if sources[key] is not None else None
                                for key in GPU_KEYS}, "sources": sources})
            gpu = gpu_rows[-1]
            if gpu["memoryTotalMiB"] == 0:
                gpu["memoryTotalMiB"] = None
            if gpu["memoryUsedMiB"] is not None and gpu["memoryTotalMiB"] is not None \
                    and gpu["memoryUsedMiB"] > gpu["memoryTotalMiB"]:
                gpu["memoryUsedMiB"] = None
    gpus_complete = snapshot.get("gpusComplete") is True and isinstance(raw_gpus, list) \
        and len(gpu_rows) == len(raw_gpus) and len(raw_gpus) <= 8 \
        and all(word(item.get("name"), 100) is not None for item in raw_gpus) \
        and all(all(row["sources"][key] is not None for key in
                    ("utilizationPercent", "memoryUsedMiB", "memoryTotalMiB")) for row in gpu_rows)
    memory = snapshot.get("memory")
    memory = memory if isinstance(memory, dict) else {}
    memory_valid = number(memory.get("availablePercent"), high=100) is not None
    try:
        probes.require_complete(memory)
    except ValueError:
        memory_valid = False
    memory_level = memory.get("level") if memory_valid and memory.get("level") in (
        "ok", "watch", "tight", "critical") else "unknown"
    components = []
    raw_components = snapshot.get("components")
    components_valid = isinstance(raw_components, list) and len(raw_components) <= 12 \
        and all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in raw_components) \
        and len({row["id"] for row in raw_components}) == len(raw_components) \
        and snapshot.get("componentsComplete") is not False
    for item in (raw_components if isinstance(raw_components, list) else [])[:12]:
        if isinstance(item, dict) and item.get("id") in ("coordinator", "heartbeatUnit", "nisi", "jev", "pair", "pair-ollama"):
            health = item.get("health", item.get("state")) if components_valid else "unknown"
            component = {"id": item["id"], "state": health if health in ("up", "down", "unknown") else "unknown",
                         "reason": (word(item.get("reason"), 96) or "not-observed") if components_valid
                             else "inventory-unavailable-or-incomplete"}
            if item["id"] == "coordinator":
                component.update(health=component["state"], state=item.get("state")
                                 if components_valid and item.get("state") in ("absent", "disabled", "active") else "unknown")
            if item["id"] == "pair-ollama":
                component.update({key: item.get(key) if components_valid and type(item.get(key)) is bool else None
                                  for key in ("processPresent", "listenerPresent")})
            components.append(component)
    release = snapshot.get("releaseCommit")
    release = release if isinstance(release, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", release) else None
    return {"schemaVersion": 1, "kind": "agiw-peer", "host": host_id(snapshot.get("hostId", "linux")),
            "platform": "linux", "edition": "linux", "version": VERSION,
            "releaseCommit": release,
            "producer": "agiw-linux-observer", "observedAt": iso(at), "observedUnix": at,
            "health": snapshot.get("health") if snapshot.get("health") in ("up", "down", "unknown")
                and (gpus_complete or snapshot.get("health") != "up") else "unknown",
            "reason": "gpu-inventory-unavailable-or-incomplete" if snapshot.get("health") == "up" and not gpus_complete
                else word(snapshot.get("reason"), 96) or "not-observed", "models": models,
            "modelsTotal": len(raw) if complete else None, "modelsComplete": complete,
            "modelInventoryLanes": list(expected) if expected_valid else None,
            "gpus": gpu_rows if gpu_rows or gpus_complete else None,
            "gpusComplete": gpus_complete, "gpusState": "known" if gpus_complete else "unknown",
            "memory": {"level": memory_level,
                       "availablePercent": number(memory.get("availablePercent"), high=100) if memory_valid else None},
            "sourceReasons": {"models": "observed" if complete else "inventory-unavailable-or-incomplete",
                              "lanes": "observed" if complete_lanes else "lane-inventory-unavailable-or-incomplete",
                              "components": "observed" if components_valid else "inventory-unavailable-or-incomplete",
                              "history": "last-measured-only" if historical_rates(snapshot.get("historicalRates"), at)
                                  else "history-unavailable-or-incomplete",
                              "routes": "route-inventory-not-observed",
                              **{key: "not-in-bounded-peer-projection" for key in OMITTED_SOURCES}},
            "components": components, "lanes": projected_lanes,
            "historicalRates": historical_rates(snapshot.get("historicalRates"), at),
            "network": network_status(snapshot.get("network"))}


class Collector:
    def __init__(self, root=Path("/"), home=None, *, fetch=None, clock=time.time, system=None, host=None):
        self.root, self.home = Path(root), Path.home() if home is None else Path(home)
        self.fetch, self.clock, self.host = fetch, clock, host_id(host)
        self.system = system or linsys.LinuxSystem(self.root, self.home)
        self.previous = None
        self.decoded = {}

    def collect(self):
        at = self.clock()
        system = self.system.snapshot(self.previous)
        self.previous = system
        processes = system.get("processes")
        network = system.get("network") or {}
        pair = probes.probe_pair(fetch=self.fetch, processes=processes, network=network)
        wrappers = probes.probe_wrapper(processes, fetch=self.fetch, network=network)
        ollama_processes = [p for p in processes or [] if isinstance(p, dict) and p.get("kind") == "ollama"]
        ollama_owned = any(probes.listener_owned(process, network, 11435) for process in ollama_processes)
        if ollama_owned:
            ollama = {"id": "pair-ollama", "status": "unknown", "health": "unknown",
                      "reason": "application-health-unverified", "port": 11435,
                      "processPresent": True, "listenerPresent": True,
                      "loadedState": "unknown", "activity": "unknown", "liveTokensPerSecond": None}
        else:
            ollama = {"id": "pair-ollama", "status": "unknown", "health": "unknown",
                      "reason": "process-inventory-unavailable" if processes is None else
                      "listener-not-correlated-with-ollama-process",
                      "processPresent": bool(ollama_processes) if processes is not None else None,
                      "listenerPresent": None,
                      "port": 11435, "loadedState": "unknown", "activity": "unknown",
                      "liveTokensPerSecond": None}
        models = list(pair.get("models") or [])
        decoded = {}
        for lane in wrappers:
            process = next((p for p in processes or [] if p.get("pid") == lane.get("pid")), {})
            identity = process.get("startTimeTicks")
            boot = system.get("boot", {}).get("bootId")
            key = (boot, lane.get("pid"), lane.get("port"), identity)
            if type(identity) is int and boot:
                lane["liveTokensPerSecond"], decoded[key] = lanes.live_decode_rate(self.decoded.get(key, {}), lane, at)
            else:
                lane["liveTokensPerSecond"] = None
            models.append({"id": lane.get("servedModel") or lane.get("expectedModel") or "gpu-wrapper",
                           "host": "linux", "lane": lane["id"],
                           "loadedState": "loaded" if lane.get("health") == "up" else "unknown",
                           "activity": lane.get("phase", "unknown") if lane.get("status") in ("idle", "busy") else "unknown",
                           "servedModel": lane.get("servedModel"), "slotsBusy": lane.get("slotsBusy"),
                           "slotsTotal": lane.get("slotsTotal"), "liveTokensPerSecond": lane["liveTokensPerSecond"]})
        self.decoded = decoded
        coordinator = probes.coordinator_status(processes, units=system.get("coordinatorUnits"))
        state = probes.read_codex_state(self.home / ".local/state", now=at)
        pair_files = probes.read_pair_files(self.home, now=at)
        staged = {"id": "qwen35-vulkan", "health": "unknown", "status": "unknown", "reason": "staged-not-probed",
                  "liveTokensPerSecond": None}
        wrapper_rows = wrappers or [{"id": "gpu-wrapper", "health": "unknown", "status": "unknown",
                                    "reason": "process-not-observed" if processes is not None else "process-inventory-unavailable",
                                    "liveTokensPerSecond": None}]
        contention = {"state": "unknown", "reason": "lane-activity-not-fully-observed"}
        if any(model.get("loadedState") == "loaded" for model in pair.get("models", [])) and wrappers:
            contention = {"state": "detected", "reason": "pair-model-loaded-with-gpu-wrapper-process",
                          "resource": "cpu", "nextStep": "Owner must coordinate PAIR and AGIW lanes; observer takes no action"}
        snapshot = unknown_snapshot("local-sample", at, self.host)
        observed = bool(system.get("boot", {}).get("bootId"))
        gpus_complete = system.get("gpusComplete") is True
        snapshot.update({"health": "up" if observed and gpus_complete else "unknown",
                         "reason": "system-unavailable" if not observed else "local-sample" if gpus_complete
                             else "gpu-inventory-unavailable-or-incomplete",
                         "models": models, "modelsComplete": False, "lanes": [pair, ollama, *wrapper_rows, staged, coordinator],
                         "modelInventoryLanes": ["pair-llamacpp", *[row["id"] for row in wrapper_rows]],
                         "components": [coordinator, {"id": "pair", "health": pair.get("health"), "reason": pair.get("reason")},
                                        {"id": "pair-ollama", "health": ollama["health"], "reason": ollama["reason"],
                                         "processPresent": ollama["processPresent"], "listenerPresent": ollama["listenerPresent"]},
                                        {"id": "jev", "health": "unknown", "reason": "no-passive-record"}],
                         "authority": state, "coordinator": coordinator, "pairCluster": pair_files,
                         "contention": contention, "lastCodexRun": state.get("lastRun"), "idleAnalysis": state.get("idleAnalysis"),
                         "historicalRates": [probes.last_rates(
                             self.root / "srv/storage/work/claude-llama-vulkan/logs" / f"last-{role}.log", now=at)
                             for role in ("code", "extract")],
                         **{key: system.get(key) for key in ("gpus", "memory", "cpu", "boot", "disks", "temperatures", "network", "gpuLock")},
                         "gpusComplete": gpus_complete, "gpusState": "known" if gpus_complete else "unknown",
                         "modelControl": {"supported": False, "reason": "Read-only observer"}})
        # A staged profile and unobserved CPU roles are not a complete serving inventory.
        return snapshot
