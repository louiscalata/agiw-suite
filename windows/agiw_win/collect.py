"""Assemble one Windows snapshot in the dashboard's schema (``host: "windows"``)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import inspect
from pathlib import Path
import threading
import time
from typing import Any, Callable

from . import VERSION
from . import buildinfo
from . import probes
from . import winsys

WORKER_VERSION = f"win-{VERSION}"  # matches the dashboard's worker-version pattern (<=16 chars)
# Inclusive deadlines: slot activity/capacity lasts 5 s; inventory/health lasts
# 11.8 s (the bounded probe budget), plus max(0, lane_interval - 1). Compare the
# observation timestamp + window to now; rounded ages are display values only.
LANE_ACTIVITY_FRESH_SECONDS = 5.0
LANE_FRESH_SECONDS = 2 * (2 * probes.HTTP_TIMEOUT + probes.SLOTS_TIMEOUT) + 1.0
GPU_KEYS = ("index", "name", "utilizationPercent", "memoryUsedMiB", "memoryTotalMiB", "temperatureC", "powerW")


def merge_adapters(nvidia: list[dict[str, Any]], adapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per hardware adapter. NVIDIA rows keep nvidia-smi's readings (temperature, power) and gain the
    adapter name/LUID; other adapters (the AMD card) get load and memory from the Windows counters only. Every row
    keeps the counters' busiest engine and its per-process split (for the hands-off cause)."""
    rows, used = [], set()
    for adapter in adapters:
        smi = next((g for i, g in enumerate(nvidia) if adapter["vendor"] == "nvidia" and i not in used
                    and (g["name"] == adapter["name"] or len([a for a in adapters if a["vendor"] == "nvidia"]) == 1)), None)
        split = {"busiestEngine": adapter.get("busiestEngine"), "engineShares": adapter.get("engineShares"),
                 "engineSharesComplete": adapter.get("engineSharesComplete")}
        if smi is not None:
            used.add(nvidia.index(smi))
            rows.append({**smi, "luid": adapter["luid"], "source": "nvidia-smi", **split,
                         "utilizationReadings": {"nvidia-smi": smi.get("utilizationPercent"),
                                                 "windows-gpu-counters": adapter.get("utilizationPercent")}})
        else:
            rows.append({"index": 100 + len(rows), "name": adapter["name"], "vendor": adapter["vendor"], "luid": adapter["luid"],
                         "utilizationPercent": adapter["utilizationPercent"], "memoryUsedMiB": adapter["memoryUsedMiB"],
                         "memoryTotalMiB": adapter["memoryTotalMiB"], "temperatureC": None, "powerW": None,
                         "source": "windows-gpu-counters", **split})
    rows += [g for i, g in enumerate(nvidia) if i not in used]
    return rows


class LanePoller:
    """One lane polled on its own thread: a busy CPU lane can take seconds to answer /slots."""

    def __init__(self, spec: dict[str, Any], fetch: probes.Fetch, clock: Callable[[], float], interval: float = 1.0):
        self.spec, self.fetch, self.clock, self.interval = spec, fetch, clock, interval
        self._latest: tuple[float, dict[str, Any]] | None = None
        self._decoded: dict[int, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._polling = True
        self._revision = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lane-{spec['id']}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            with self._lock:
                polling, revision = self._polling, self._revision

            def fetch(url, timeout, limit):
                with self._lock:
                    if not self._polling or revision != self._revision or self._stop.is_set():
                        raise RuntimeError("Lane polling parked")
                return self.fetch(url, timeout, limit)

            try:
                lane = probes.probe_lane(self.spec, fetch) if polling else None
            except Exception:  # noqa: BLE001
                lane = None
            if lane is not None:
                at = self.clock()
                with self._lock:
                    if self._polling and revision == self._revision and not self._stop.is_set():
                        lane["liveTokensPerSecond"], self._decoded = probes.live_decode_rate(self._decoded, lane, at)
                        self._latest = (at, lane)
            self._stop.wait(max(0.05, self.interval - (time.monotonic() - started)))

    def set_polling(self, allowed: bool) -> None:
        with self._lock:
            if self._polling != allowed:
                self._polling = allowed
                self._revision += 1
                self._latest = None
                self._decoded = {}

    def read(self) -> tuple[float, dict[str, Any]] | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop.set()


def mac_from_presence(peers: list[dict[str, Any]], name: str | None, *,
                      peers_source: dict[str, Any] | None = None, config_reason: str | None = None) -> dict[str, Any]:
    """snapshot.macPeer from the Mac's own presence only (0.2.0 has no LAN probe of the Mac).

    The share link stands in for the configured Mac only: its hosts.json name must match the report's host (ignoring
    case and any domain). Without a configured name, no report can be attributed. The shape keeps the dashboard's
    macPeerView contract: "linked" with its share age, else "unknown" with a reason, never a guessed state."""
    want = probes.short_host(name)
    matching = [p for p in peers if p.get("platform") == "macos" and probes.short_host(p.get("host")) == want] if want else []
    mac_link = next((p for p in matching if p.get("fresh") is True), None)
    if mac_link:
        models = mac_link["models"]
        return {"state": "linked", "via": "sharedchami", "name": name, "configuredName": name,
                "ageSeconds": mac_link["ageSeconds"],
                "models": [{"id": m["id"], "state": {"loaded": "loaded", "inactive": "not-loaded"}.get(m["loadedState"], "unknown")}
                           for m in models],
                # Counted only from a complete list (modelsComplete): a cut or partly rejected one is no count.
                "loadedCount": (sum(m["loadedState"] == "loaded" for m in models)
                                if (mac_link.get("loadedListComplete") is True or mac_link.get("modelsComplete") is True and models)
                                and all(m["loadedState"] in ("loaded", "inactive") for m in models) else None),
                "latencyMs": None, "address": None, "reason": None,
                "linkAgeSeconds": mac_link["ageSeconds"], "linkSource": mac_link["source"],
                "verified": False,
                "detail": f"Reported through SharedChami ({mac_link['source']}, {mac_link['ageSeconds']:.0f} s old); "
                          "unsigned share file, not verified; this PC does not probe the Mac over the LAN"}
    if config_reason == "config-unreadable":
        reason, detail = "config-unreadable", "Mac identity unknown: hosts.json unreadable or invalid; reports not attributed"
    elif not want:
        reason, detail = "unconfigured", "No Mac configured in llm-lab/mac-reach/hosts.json; Mac reports are not attributed"
    elif matching:
        reason, detail = "stale", (f"The configured Mac's last report is {matching[0]['ageSeconds']:.0f} s old; "
                                   "Mac state unknown")
    elif peers_source and peers_source.get("reason") not in (None, "available", "no-report"):
        reason = peers_source.get("refusalHosts", {}).get(want) or peers_source["reason"]
        detail = f"Mac report unavailable ({reason}): {peers_source['detail']}"
    else:
        reason, detail = "no-report", "No presence from the configured Mac on SharedChami; Mac state unknown"
    return {"state": "unknown", "via": None, "name": name, "configuredName": name, "ageSeconds": None, "models": [],
            "loadedCount": None, "latencyMs": None, "address": None, "reason": reason, "verified": False,
            "detail": detail}


def merge_mac_lan_observation(mac: dict[str, Any], observation: dict[str, Any], now: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge an already observed LAN result without probing or renewing its clock.

    The passive observer has no LAN acquisition service. An embedding owner may
    supply its last result; share freshness never extends that result's lifetime.
    """
    mac = dict(mac)
    mac.pop("lanFailure", None)
    mac.pop("lanFailureAgeSeconds", None)
    at = probes._number(observation.get("observedAt"))
    age = now - at if at is not None else None
    configured = probes.short_host(mac.get("configuredName"))
    current = (age is not None and -5 <= age <= 45 and configured is not None
               and probes.short_host(observation.get("name")) == configured)
    failure = observation.get("reason") if current and observation.get("state") == "unreachable" else None
    failure = failure if failure in ("refused", "timeout", "unreachable", "unresolved") else None
    source = probes._source("mac-peer", "Mac LAN observation", "error" if failure else "unavailable",
                            f"Mac LM Studio LAN check failed ({failure}); cause unknown" if failure else "Mac LM Studio LAN reachability unknown")
    source["ageSeconds"] = max(0.0, age) if current else age
    mac["lanObservationAvailable"] = True
    if failure:
        mac.update(lanFailure=failure, lanFailureAgeSeconds=max(0.0, age))
    return mac, source


class Collector:
    """Owns the slower caches (GPU, adapter list, PAIR census, client metadata)."""

    def __init__(self, share_root: Path = probes.DEFAULT_SHARE_ROOT, bin_root: Path | None = None, *,
                 fetch: probes.Fetch = probes.http_get_json,
                 gpu_fn: Callable[[], tuple[list, dict]] = probes.nvidia_gpus,
                 adapters_fn: Callable[[], list] = probes.display_adapters,
                 memory_fn: Callable[..., tuple[dict, dict]] = probes.memory_block,
                 clients_fn: Callable[[float], tuple[list, list]] | None = None,
                 clock: Callable[[], float] = time.time,
                 lane_interval: float = 1.0, lane_wait: float = 0.0, runs_root: Path | None = None,
                 host: str | None = None, lane_processes: "winsys.LaneProcesses | None" = None,
                 peer_link_reader: bool = False,
                 pair_census: "winsys.PairCensus | None" = None,
                 notification_fn: Callable[[], int | None] | None = None,
                 build: dict[str, Any] | None = None):
        self.share_root = share_root
        self.bin_root = bin_root or Path.home() / "bin"
        self.fetch, self.gpu_fn, self.adapters_fn, self.memory_fn = fetch, gpu_fn, adapters_fn, memory_fn
        self.clients_fn = clients_fn
        self.clock = clock
        self.peer_link_reader = peer_link_reader is True
        # Optional evidence supplied by the embedding owner; never an active probe.
        self.mac_lan_observation: dict[str, Any] | None = None
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agiw-probe")
        self._gpu_cache: tuple[float, list, dict] | None = None
        self._adapters: list | None = None
        self._lock = threading.Lock()
        self._pollers: dict[tuple, LanePoller] = {}
        self._lane_ownership: dict[str, str] = {}
        self._lane_owners: dict[str, dict[str, Any]] = {}
        self._jobs_cache: tuple[float, dict, dict] | None = None
        self.gpu_counters = None
        self._gpu_counters_failed = gpu_fn is not probes.nvidia_gpus  # tests inject gpu_fn and skip PDH
        self.runs_root = runs_root or Path.home() / "code-runs"
        self.host = host or probes.host_id()
        self.lane_processes = lane_processes if lane_processes is not None else (
            winsys.LaneProcesses() if winsys.IS_WINDOWS else None)
        self.pair_census = pair_census if pair_census is not None else (
            winsys.PairCensus() if winsys.IS_WINDOWS else None)
        self.notification_fn = notification_fn if notification_fn is not None else winsys.notification_state
        self.build = build if build is not None else buildinfo.read_build()
        self.lane_interval, self.lane_wait = lane_interval, lane_wait

    def _lane(self, spec: dict[str, Any], now: float) -> dict[str, Any]:
        """The newest result for this lane (a new poller starts if the declared lane changed)."""
        if spec.get("undeclared"):
            # Never served from a poller: a lane removed from config must not keep its old evidence.
            with self._lock:
                for old_key in [k for k in self._pollers if k[0] == spec["id"]]:
                    self._pollers.pop(old_key).stop()
            return {**probes.probe_lane(spec, self.fetch), "ageSeconds": 0.0}
        owner = self._lane_owners.get(spec["id"], {})
        key = (spec["id"], spec["port"], spec["alias"], owner.get("pid"), owner.get("created"))
        ownership = self._lane_ownership.get(spec["id"], "unknown")
        with self._lock:
            poller = self._pollers.get(key)
            for old_key in [k for k in self._pollers if k[0] == spec["id"] and k != key]:
                self._pollers.pop(old_key).stop()
            if poller is not None:
                poller.set_polling(ownership == "allowed")
            elif ownership == "allowed":
                def fetch(url, timeout, limit):
                    # Recheck before every endpoint: permission for /health never authorizes a later replacement.
                    recheck = (self.lane_processes.ownership if hasattr(type(self.lane_processes), "ownership")
                               else self.lane_processes.sample)
                    servers, _, source = recheck({spec["port"]: spec["id"]})
                    current = servers.get(spec["id"], {})
                    if source.get("state") != "live" or current.get("owner") != "agiw" \
                            or current.get("ownerKnown") is not True \
                            or any(current.get(field) != owner.get(field) for field in ("pid", "created")):
                        raise probes.OwnerUnverified("Lane owner no longer verified")
                    return self.fetch(url, timeout, limit)
                poller = self._pollers[key] = LanePoller(spec, fetch, self.clock, self.lane_interval)
        if ownership != "allowed":
            return {"id": spec["id"], "port": spec["port"], "endpoint": None, "expectedModel": spec["alias"],
                    "servedModel": None, "modelBytes": None, "status": "not_agiw" if ownership == "pair" else "unknown",
                    "slots": [], "slotsBusy": None, "slotsTotal": None, "phase": None,
                    "detail": "Port held by NVIDIA PAIR; not probed" if ownership == "pair" else
                              "Lane port ownership unavailable; not probed",
                    "device": spec.get("device"), "ageSeconds": 0.0 if ownership == "pair" else None}
        latest = poller.read()
        if latest is None and self.lane_wait:
            deadline = time.monotonic() + self.lane_wait
            while latest is None and time.monotonic() < deadline:
                time.sleep(0.02)
                latest = poller.read()
        if latest is None:
            return {"id": spec["id"], "port": spec["port"], "endpoint": None, "expectedModel": spec["alias"],
                    "servedModel": None, "modelBytes": None, "status": "unknown", "slots": [], "slotsBusy": None, "slotsTotal": None,
                    "phase": None, "detail": "First lane probe pending", "device": spec.get("device"), "ageSeconds": None}
        at, lane = latest
        if type(at) not in (int, float) or not at <= max(now, self.clock()):
            return {**lane, "ageSeconds": None, "status": "unknown", "phase": None,
                    "slots": [], "slotsBusy": None, "slotsTotal": None, "liveTokensPerSecond": None,
                    "detail": "Lane observation timestamp missing, invalid or future-dated"}
        age = now - at
        display_age = round(max(0.0, age), 2)
        history = {"observedAt": at, **{key: lane.get(key) for key in
                   ("status", "phase", "slotsBusy", "slotsTotal", "liveTokensPerSecond")}}
        if now > at + LANE_FRESH_SECONDS + max(0.0, self.lane_interval - 1.0):
            # A poller that stopped refreshing (hung lane, stalled socket) proves nothing about now:
            # keep identity for the inspector, drop every activity and liveness claim.
            return {**lane, "lastObservation": history, "ageSeconds": display_age, "status": "stale", "phase": None,
                    "slots": [], "slotsBusy": None, "slotsTotal": None,
                    "liveTokensPerSecond": None,
                    "detail": f"Last lane answer is {display_age:.0f} s old ({lane['status']} then)"}
        if now > at + LANE_ACTIVITY_FRESH_SECONDS and lane["status"] in ("idle", "busy"):
            return {**lane, "lastObservation": history, "ageSeconds": display_age,
                    "status": "slots_unknown", "phase": None, "slots": [], "slotsBusy": None, "slotsTotal": None,
                    "liveTokensPerSecond": None,
                    "detail": f"Activity unknown; last observation {display_age:.0f} s ago ({lane['status']} then)"}
        return {**lane, "ageSeconds": display_age,
                "liveTokensPerSecond": lane.get("liveTokensPerSecond") if age <= 5.0 else None}

    def close(self) -> None:
        with self._lock:
            for poller in self._pollers.values():
                poller.stop()
        if self.gpu_counters is not None:
            self.gpu_counters.close()
            self.gpu_counters = None
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _adapter_load(self) -> list[dict[str, Any]]:
        """Every adapter's load and memory from Windows' GPU counters (PDH + DXGI); [] when unavailable."""
        if self.gpu_counters is None:
            if self._gpu_counters_failed or not winsys.IS_WINDOWS:
                return []
            try:
                self.gpu_counters = winsys.GpuCounters()
            except Exception:  # noqa: BLE001 - counters are optional detail
                self._gpu_counters_failed = True
                return []
        return self.gpu_counters.sample()

    # nvidia-smi costs ~100-300 ms; two seconds of reuse keeps the sampler at 1 Hz.
    def _gpus(self, now: float) -> tuple[list, dict]:
        with self._lock:
            cached = self._gpu_cache
        if cached and now - cached[0] < 2.0:
            gpus, source = cached[1], dict(cached[2]) if isinstance(cached[2], dict) else None
            if source is not None and source["state"] == "live":
                source["ageSeconds"] = round(now - cached[0], 1)
            return gpus, source
        gpus, source = self.gpu_fn()
        try:
            adapters = self._adapter_load()
        except Exception:  # noqa: BLE001
            adapters = []
        if adapters:
            gpus = merge_adapters(gpus, adapters)
            source = {**source, "state": "live", "ageSeconds": 0.0,
                      "detail": "Every adapter's load and memory from Windows GPU counters; NVIDIA temperature and power from nvidia-smi"}
        # A failed or partial PDH read must not erase an unread card from the hands-off decision.
        gpus = list(gpus)
        matched = set()
        missing = []
        inventory = self._adapter_list()
        for adapter in inventory:
            match = next((i for i, gpu in enumerate(gpus) if i not in matched and
                          (adapter["luid"] == gpu.get("luid") if adapter.get("luid") and gpu.get("luid") else
                           adapter["name"].casefold() == gpu["name"].casefold())), None)
            if match is not None:
                matched.add(match)
                continue
            size = adapter.get("memoryBytes")
            index = max((gpu["index"] for gpu in gpus), default=99) + 1
            gpus.append({"index": index, "name": adapter["name"], "luid": adapter.get("luid"),
                         "utilizationPercent": None, "memoryUsedMiB": None,
                         "memoryTotalMiB": size / 2 ** 20 if type(size) is int and size > 0 else None,
                         "temperatureC": None, "powerW": None, "source": "adapter-inventory",
                         "busiestEngine": None, "engineShares": None, "engineSharesComplete": False})
            matched.add(len(gpus) - 1)
            missing.append(adapter["name"])
        if missing and source is not None:
            source = {**source, "detail": source["detail"] + "; GPU load unavailable for " + ", ".join(missing)}
        if source is not None:
            source = {**source, "inventoryComplete": bool(adapters or inventory) or not winsys.IS_WINDOWS}
        with self._lock:
            self._gpu_cache = (now, gpus, source)
        return gpus, source

    # Manifests change only when a codemode run ends; ten seconds of reuse is plenty, re-aged on read.
    def _jobs(self, specs: list[dict[str, Any]], now: float) -> tuple[dict, dict]:
        cached = self._jobs_cache
        if cached is None or now - cached[0] >= 10.0:
            jobs, source = probes.codemode_jobs(self.runs_root, specs, now)
            self._jobs_cache = cached = (now, jobs, source)
        at, jobs, source = cached
        extra = now - at
        if extra:
            jobs = {**jobs, "observationAgeSeconds": extra, "recent": [{**row, "ageSeconds": round(row["ageSeconds"] + extra, 1)} for row in jobs["recent"]],
                    "lastSuccess": ({**jobs["lastSuccess"], "ageSeconds": round(jobs["lastSuccess"]["ageSeconds"] + extra, 1)}
                                    if jobs["lastSuccess"] else None)}
        return jobs, source

    def _adapter_list(self) -> list:
        if self._adapters is None:
            try:
                self._adapters = self.adapters_fn()
            except Exception:  # noqa: BLE001
                self._adapters = []
        return self._adapters

    def _guard(self, sources: list, source_id: str, label: str, fn, fallback):
        """Run one probe; a crash becomes an error source and a neutral value instead of a lost sample."""
        try:
            return fn()
        except Exception as error:  # noqa: BLE001
            sources.append(probes._source(source_id, label, "error", f"Probe failed ({type(error).__name__})"))
            return fallback

    def _memory(self, lanes: list[dict[str, Any]], nisi: dict[str, Any], route_live: bool) -> tuple[dict, dict]:
        context = {"lanes": lanes, "nisi": nisi, "route_live": route_live}
        try:
            inspect.signature(self.memory_fn).bind(**context)
        except (TypeError, ValueError):
            # Existing injected probes take no arguments. Never retry a failed probe call.
            return self.memory_fn()
        return self.memory_fn(**context)

    def _pair(self) -> dict[str, Any]:
        census = self.pair_census.sample() if self.pair_census is not None else None
        return probes.pair_component(census)

    def collect(self) -> dict[str, Any]:
        sampled = self.clock()
        observed = dt.datetime.fromtimestamp(sampled, dt.timezone.utc).isoformat().replace("+00:00", "Z")
        sources: list[dict[str, Any]] = []
        results: dict[str, Any] = {}
        specs, lane_source = self._guard(
            sources, "lane-config", "Lane layout", lambda: probes.lane_specs(self.share_root),
            ([probes.unconfigured_lane(lane_id, "unreadable") for lane_id in probes.LANE_IDS], None))
        if lane_source is not None:
            sources.append(lane_source)
        servers: dict[str, Any] = {}
        callers: list[dict[str, Any]] | None = None  # None = not checked; [] only after a complete read
        process_source = None
        if self.lane_processes is not None:
            ports = {spec["port"]: spec["id"] for spec in specs if not spec.get("undeclared")}
            servers, callers, process_source = self._guard(sources, "lane-processes", "Lane processes",
                                                          lambda: self.lane_processes.sample(ports), ({}, None, None))
            if process_source is not None:
                sources.append(process_source)
                if process_source.get("state") != "live":
                    callers = None
        declared = bool(specs) and all(not spec.get("undeclared") for spec in specs)
        if not declared:
            callers = None
            if process_source is not None:
                process_source["detail"] += "; lane ports incomplete; caller coverage unknown"
        # Check ownership before HTTP; an unknown listener or unreadable table never releases a parked lane.
        for spec in specs:
            process = servers.get(spec["id"])
            if isinstance(process, dict) and process.get("owner") == "pair":
                ownership = "pair"
            elif isinstance(process_source, dict) and process_source.get("state") == "live" \
                    and isinstance(process, dict) and process.get("owner") == "agiw" \
                    and process.get("ownerKnown") is True:
                ownership = "allowed"
            else:
                ownership = "unknown"
            self._lane_ownership[spec["id"]] = ownership
            self._lane_owners[spec["id"]] = dict(process) if isinstance(process, dict) else {}
            with self._lock:
                for key, poller in self._pollers.items():
                    if key[0] == spec["id"]:
                        poller.set_polling(ownership == "allowed" and key == (
                            spec["id"], spec["port"], spec["alias"], process.get("pid"), process.get("created")))
        lanes = [self._lane(spec, sampled) for spec in specs]
        for lane in lanes:
            lane["process"] = servers.get(lane["id"])
            lane["callers"] = None if callers is None else [c for c in callers if c["lane"] == lane["id"]]
        # A lane port held by NVIDIA PAIR is never an AGIW lane, whatever it serves (P2).
        foreign = probes.mark_foreign_lanes(lanes)
        rows = []
        for lane, spec in zip(lanes, specs):
            row = probes.lane_model_row(lane, spec, sampled)
            row["ageSeconds"] = lane["ageSeconds"]
            row["metadata"]["liveTokensPerSecond"] = lane.get("liveTokensPerSecond")
            if "lastObservation" in lane:
                row["metadata"]["lastObservation"] = lane["lastObservation"]
            row["metadata"]["process"] = lane.get("process")
            row["metadata"]["callers"] = (None if lane.get("callers") is None else
                                          [{k: c[k] for k in ("name", "client", "connections")} for c in lane["callers"]])
            rows.append(row)
        complete = all(lane["status"] in ("idle", "busy") for lane in lanes)
        any_up = any(lane["status"] in probes.LANE_UP for lane in lanes)
        sources.append(probes._source(
            "llama-slots", "Lane activity", "live" if complete else "error" if any_up else "unavailable",
            "Slot activity read from every lane" if complete else
            "; ".join(f"{lane['id']}: {lane['detail']}" for lane in lanes if lane["detail"]) or "Lane activity unavailable"))

        fresh_lanes = all(lane["status"] not in ("unknown", "stale") and isinstance(lane["ageSeconds"], (int, float))
                          and lane["ageSeconds"] <= LANE_FRESH_SECONDS + max(0.0, self.lane_interval - 1.0) for lane in lanes)
        sources.append(probes._source("lane-inventory", "Lane inventory", "live" if fresh_lanes else "unavailable",
                                      "Every declared lane answered or refused within its probe deadline" if fresh_lanes
                                      else "A lane result is pending or older than its probe deadline"))
        g = lambda sid, label, fn, fallback: self._guard(sources, sid, label, fn, fallback)  # noqa: E731
        empty_jobs = {"schemaVersion": 1, "journal": "codemode-runs", "complete": False, "inFlight": [], "recent": [], "lastSuccess": None,
                      "clientsRecent": {}, "availability": {"state": "unknown", "reason": "Job feed unavailable"},
                      "liveAvailable": False}
        result_sources: dict[str, Any] = {}
        for value_name, sid, label, fn, fallback in (
                ("gpus", "pc-gpu", "PC GPU", lambda: self._gpus(sampled), ([], None)),
                ("jobs", "codemode-runs", "codemode runs", lambda: self._jobs(specs, sampled), (empty_jobs, None)),
                ("pipeline", "route-queue", "Route queue", lambda: probes.route_queue(self.bin_root / "online-code-route-queue", sampled),
                 ({"status": None, "runId": None, "pipelines": [], "pipelinesComplete": False, "queue": None}, None)),
                ("share", "sharedchami", "SharedChami", lambda: probes.share_health(self.share_root), ({"state": "unknown"}, None))):
            value, source = g(sid, label, fn, fallback)
            if source is not None:
                sources.append(source)
            results[value_name] = value
            result_sources[value_name] = source
        gpus, jobs, pipeline, share = (results[k] for k in ("gpus", "jobs", "pipeline", "share"))

        # The Mac is known only from its own presence (PLAN A3: the LAN probe of its LM Studio is gone), attributed
        # only by the current configured identity (hosts.json).
        name, config_reason = probes.configured_mac_identity(self.share_root)
        peers, peers_source = self._guard(sources, "agiw-peers", "AGIW peers",
                                          lambda: probes.read_peers(self.share_root, self.host, sampled), ([], None))
        if peers_source is not None:
            sources.append(peers_source)
        mac = mac_from_presence(peers, name, peers_source=peers_source, config_reason=config_reason)
        if isinstance(self.mac_lan_observation, dict):
            mac, lan_source = merge_mac_lan_observation(mac, self.mac_lan_observation, sampled)
            sources.append(lan_source)
        sources.append(probes._source("mac-presence", "Mac (SharedChami presence)",
                                      "live" if mac["state"] == "linked" else "unavailable", mac["detail"],
                                      mac["ageSeconds"]))

        clients: list = []
        if self.clients_fn is not None:
            try:
                clients, client_sources = self.clients_fn(sampled)
                sources.extend(client_sources)
            except Exception:  # noqa: BLE001 - client metadata never interrupts the feed
                sources.append(probes._source("client-metadata", "Client metadata", "error",
                                              "Client model records unavailable"))

        up = [lane for lane in lanes if lane["status"] in probes.LANE_UP]
        health = probes.lanes_health(lanes)
        worker_state = "advertised" if up else "unknown" if health == "unknown" else "degraded"
        worker = {
            "state": worker_state, "ageSeconds": 0.0, "workerVersion": WORKER_VERSION,
            "scope": "local-observer",
            "detail": "Lanes read directly on this PC" if up else "Lane availability unknown on this PC" if health == "unknown"
                      else "Declared lanes are not serving on this PC",
            "lanes": {lane["id"]: {"up": {"up": True, "down": False, "unknown": None}[probes.lane_health(lane)["state"]],
                                   "health": probes.lane_health(lane), "model": lane["expectedModel"],
                                   "ageSeconds": lane.get("ageSeconds"),
                                   "slotsBusy": lane["slotsBusy"], "slotsTotal": lane["slotsTotal"],
                                   "status": lane["status"], "servedModel": lane["servedModel"],
                                   "port": lane["port"], "device": lane["device"],
                                   "liveTokensPerSecond": lane.get("liveTokensPerSecond"),
                                   "cpuPercent": (lane.get("process") or {}).get("cpuPercent"),
                                   "workingSetBytes": (lane.get("process") or {}).get("workingSetBytes")}
                      for lane in lanes},
            # The dashboard voids the whole GPU sample on one unmeasured field, so only fully read cards go here;
            # pcGpus keeps every card with None for what nvidia-smi did not report.
            "gpus": [{key: gpu[key] for key in GPU_KEYS} for gpu in gpus if all(gpu[key] is not None for key in GPU_KEYS)],
        }
        mismatch = [lane["id"] for lane in lanes if lane["status"] == "identity_mismatch"]
        if mismatch or foreign:
            worker["lanesError"] = "; ".join(filter(None, (
                "identity mismatch on " + ", ".join(mismatch) if mismatch else None,
                "port held by NVIDIA PAIR on " + ", ".join(foreign) if foreign else None)))
        nisi = self._guard(sources, "nisi", "Nisi gate", lambda: probes.nisi_component(lanes, self.bin_root),
                           {"id": "nisi", "label": "Nisi gate", "state": "unknown", "detail": "Nisi check failed"})
        memory, memory_source = g("mem-guard", "Memory",
                                  lambda: self._memory(lanes, nisi, pipeline.get("status") == "running"),
                                  ({"level": "unknown", "consumers": [], "suggestions": [], "reasons": [], "paused": []}, None))
        if memory_source is not None:
            sources.append(memory_source)
        try:
            pair = self._pair()
            sources.append(probes._source("nvidia-pair", "NVIDIA PAIR",
                                          "unavailable" if pair["state"] == "unknown" else "live", pair["detail"]))
        except Exception as error:  # noqa: BLE001 - PAIR presence is detail; the sample survives
            pair = probes.pair_component(None)
            sources.append(probes._source("nvidia-pair", "NVIDIA PAIR", "error", f"Probe failed ({type(error).__name__})"))
        components = [nisi,
                      {"id": "jev", "label": "Jev", "state": "unknown",
                       "detail": "Jev triage runs through the route queue on this PC; no passive opt-in record is read",
                       "lastJudgedAgeSeconds": None},
                      pair]
        # Hands-off (D5): lane load is attributed by the lane-port owners from the TCP table; PAIR's are excluded.
        gpu_source = result_sources.get("gpus")
        try:
            notification = self.notification_fn()
        except Exception:  # noqa: BLE001 - an unread full-screen state is unknown, which hands_off reports
            notification = None
        owners_known = declared and len(servers) == len(specs) and isinstance(process_source, dict) \
            and process_source.get("state") == "live" and all(
            isinstance(process, dict) and process.get("owner") in ("agiw", "pair")
            and process.get("ownerKnown") is True for process in servers.values())
        hands = probes.hands_off(gpus, gpu_live=isinstance(gpu_source, dict) and gpu_source.get("state") == "live",
                                 gpu_complete=isinstance(gpu_source, dict) and gpu_source.get("inventoryComplete") is True,
                                 lane_pids=probes.lane_server_pids(lanes) if owners_known else None,
                                 notification_state=notification)
        sources.append(probes._source("hands-off", "Hands off", "unavailable" if hands["active"] is None else "live",
                                      hands["detail"]))
        paired: list[dict[str, Any]] | None = None
        snapshot = {
            "schemaVersion": 1, "host": "windows", "edition": "windows", "observerVersion": VERSION,
            "laneActivityFreshSeconds": LANE_ACTIVITY_FRESH_SECONDS,
            "laneFreshSeconds": LANE_FRESH_SECONDS + max(0.0, self.lane_interval - 1.0),
            "build": self.build, "observedAt": observed, "sampledAt": sampled,
            "models": rows, "sources": sources, "pipeline": pipeline, "components": components,
            "windowsWorker": worker, "windowsJobs": jobs, "memory": memory,
            "activity": {"runs": [], "complete": jobs.get("complete") is True and pipeline.get("pipelinesComplete") is True,
                         "historyBound": {"truncated": jobs.get("complete") is not True or pipeline.get("pipelinesComplete") is not True,
                                          "missingFromAgeSeconds": None if jobs.get("complete") is True and pipeline.get("pipelinesComplete") is True else 0.0}},
            "macPeer": mac, "share": share, "clients": clients, "peers": peers, "hostId": self.host,
            # Mac-compatible shape (localCallersView reads name); lane and client are Windows additions.
            "localCallers": [{k: c[k] for k in ("pid", "name", "lane", "client", "connections", "connectedSeconds")}
                             for c in callers] if callers is not None else None,
            "pcAdapters": self._adapter_list(), "pcGpus": gpus,
            "lanes": lanes, "handsOff": hands,
            "modelControl": {"supported": False,
                             "reason": "This Windows dashboard monitors models only. Load and unload are unavailable."},
        }
        if self.peer_link_reader:
            paired, source = self._guard(sources, "peer-link", "Peer link",
                                         lambda: probes.read_peer_link(sampled, enabled=True), ([], None))
            snapshot["pairedPeers"] = paired
            if source is not None:
                sources.append(source)
        snapshot["machines"] = self._guard(
            sources, "machines", "Machines",
            lambda: probes.machine_rows(host=self.host, sampled_at=sampled, lanes=lanes, hands=hands, version=VERSION,
                                        peers=peers, mac_name=name, paired=paired, peers_source=peers_source,
                                        mac_config_reason=config_reason), [])
        return snapshot
