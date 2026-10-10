"""Assemble one Windows snapshot in the dashboard's schema (``host: "windows"``)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from pathlib import Path
import threading
import time
from typing import Any, Callable

from . import VERSION
from . import probes

WORKER_VERSION = f"win-{VERSION}"  # matches the dashboard's worker-version pattern (<=16 chars)
GPU_KEYS = ("index", "name", "utilizationPercent", "memoryUsedMiB", "memoryTotalMiB", "temperatureC", "powerW")


class LanePoller:
    """One lane polled on its own thread: a busy CPU lane can take seconds to answer /slots."""

    def __init__(self, spec: dict[str, Any], fetch: probes.Fetch, clock: Callable[[], float], interval: float = 1.0):
        self.spec, self.fetch, self.clock, self.interval = spec, fetch, clock, interval
        self._latest: tuple[float, dict[str, Any]] | None = None
        self._decoded: dict[int, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lane-{spec['id']}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                lane = probes.probe_lane(self.spec, self.fetch)
            except Exception:  # noqa: BLE001
                lane = None
            if lane is not None:
                at = self.clock()
                lane["liveTokensPerSecond"], self._decoded = probes.live_decode_rate(self._decoded, lane, at)
                with self._lock:
                    self._latest = (at, lane)
            self._stop.wait(max(0.05, self.interval - (time.monotonic() - started)))

    def read(self) -> tuple[float, dict[str, Any]] | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop.set()


class Collector:
    """Owns the slower caches (GPU, adapter list, client metadata) and the Mac peer thread."""

    def __init__(self, share_root: Path = probes.DEFAULT_SHARE_ROOT, bin_root: Path | None = None, *,
                 fetch: probes.Fetch = probes.http_get_json,
                 gpu_fn: Callable[[], tuple[list, dict]] = probes.nvidia_gpus,
                 adapters_fn: Callable[[], list] = probes.display_adapters,
                 memory_fn: Callable[[], tuple[dict, dict]] = probes.memory_block,
                 clients_fn: Callable[[float], tuple[list, list]] | None = None,
                 mac_peer: probes.MacPeer | None = None, clock: Callable[[], float] = time.time,
                 lane_interval: float = 1.0, lane_wait: float = 0.0, runs_root: Path | None = None,
                 host: str | None = None):
        self.share_root = share_root
        self.bin_root = bin_root or Path.home() / "bin"
        self.fetch, self.gpu_fn, self.adapters_fn, self.memory_fn = fetch, gpu_fn, adapters_fn, memory_fn
        self.clients_fn = clients_fn
        self.mac_peer = mac_peer
        self.clock = clock
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agiw-probe")
        self._gpu_cache: tuple[float, list, dict] | None = None
        self._adapters: list | None = None
        self._lock = threading.Lock()
        self._pollers: dict[tuple, LanePoller] = {}
        self._jobs_cache: tuple[float, dict, dict] | None = None
        self.runs_root = runs_root or Path.home() / "code-runs"
        self.host = host or probes.host_id()
        self.lane_interval, self.lane_wait = lane_interval, lane_wait

    def _lane(self, spec: dict[str, Any], now: float) -> dict[str, Any]:
        """The newest result for this lane (a new poller starts if the declared lane changed)."""
        key = (spec["id"], spec["port"], spec["alias"])
        with self._lock:
            poller = self._pollers.get(key)
            if poller is None:
                for old_key in [k for k in self._pollers if k[0] == spec["id"]]:
                    self._pollers.pop(old_key).stop()
                poller = self._pollers[key] = LanePoller(spec, self.fetch, self.clock, self.lane_interval)
        latest = poller.read()
        if latest is None and self.lane_wait:
            deadline = time.monotonic() + self.lane_wait
            while latest is None and time.monotonic() < deadline:
                time.sleep(0.02)
                latest = poller.read()
        if latest is None:
            return {"id": spec["id"], "port": spec["port"], "endpoint": None, "expectedModel": spec["alias"],
                    "servedModel": None, "status": "unknown", "slots": [], "slotsBusy": None, "slotsTotal": None,
                    "phase": None, "detail": "First lane probe pending", "device": spec.get("device"), "ageSeconds": None}
        at, lane = latest
        return {**lane, "ageSeconds": round(max(0.0, now - at), 2)}

    def close(self) -> None:
        with self._lock:
            for poller in self._pollers.values():
                poller.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self.mac_peer:
            self.mac_peer.stop()

    # nvidia-smi costs ~100-300 ms; two seconds of reuse keeps the sampler at 1 Hz.
    def _gpus(self, now: float) -> tuple[list, dict]:
        with self._lock:
            cached = self._gpu_cache
        if cached and now - cached[0] < 2.0:
            gpus, source = cached[1], dict(cached[2])
            if source["state"] == "live":
                source["ageSeconds"] = round(now - cached[0], 1)
            return gpus, source
        gpus, source = self.gpu_fn()
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
            jobs = {**jobs, "recent": [{**row, "ageSeconds": round(row["ageSeconds"] + extra, 1)} for row in jobs["recent"]],
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

    def collect(self) -> dict[str, Any]:
        sampled = self.clock()
        observed = dt.datetime.fromtimestamp(sampled, dt.timezone.utc).isoformat().replace("+00:00", "Z")
        sources: list[dict[str, Any]] = []
        specs, lane_source = probes.lane_specs(self.share_root)
        sources.append(lane_source)
        lanes = [self._lane(spec, sampled) for spec in specs]
        rows = []
        for lane, spec in zip(lanes, specs):
            row = probes.lane_model_row(lane, spec, sampled)
            row["ageSeconds"] = lane["ageSeconds"]
            row["metadata"]["liveTokensPerSecond"] = lane.get("liveTokensPerSecond")
            rows.append(row)
        complete = all(lane["status"] in ("idle", "busy") for lane in lanes)
        any_up = any(lane["status"] in probes.LANE_UP for lane in lanes)
        sources.append(probes._source(
            "llama-slots", "Lane activity", "live" if complete else "error" if any_up else "unavailable",
            "Slot activity read from every lane" if complete else
            "; ".join(f"{lane['id']}: {lane['detail']}" for lane in lanes if lane["detail"]) or "Lane activity unavailable"))

        fresh_lanes = all(lane["status"] != "unknown" and isinstance(lane["ageSeconds"], (int, float))
                          and lane["ageSeconds"] <= 3 for lane in lanes)
        sources.append(probes._source("lane-inventory", "Lane inventory", "live" if fresh_lanes else "unavailable",
                                      "Every declared lane answered or refused within 3 s" if fresh_lanes
                                      else "A lane result is pending or older than 3 s"))
        gpus, gpu_source = self._gpus(sampled)
        sources.append(gpu_source)
        memory, memory_source = self.memory_fn()
        sources.append(memory_source)
        jobs, jobs_source = self._jobs(specs, sampled)
        sources.append(jobs_source)
        pipeline, queue_source = probes.route_queue(self.bin_root / "online-code-route-queue", sampled)
        sources.append(queue_source)
        share, share_source = probes.share_health(self.share_root)
        sources.append(share_source)
        if self.mac_peer is not None:
            mac, mac_source = self.mac_peer.read(sampled)
        else:
            mac, mac_source = ({"state": "unknown", "detail": "LAN probe disabled", "models": [], "ageSeconds": None},
                               probes._source("mac-peer", "Mac (LAN)", "unavailable", "LAN probe disabled"))
        sources.append(mac_source)
        peers, peers_source = probes.read_peers(self.share_root, self.host, sampled)
        sources.append(peers_source)
        mac_link = next((p for p in peers if p["platform"] == "macos" and p["fresh"]), None)
        if mac_link and mac.get("state") != "reachable":
            # The Mac's AGIW is linked through the share even though its LM Studio is closed to the LAN.
            mac = {**mac, "state": "linked", "via": "sharedchami", "models": mac_link["models"],
                   "loadedCount": sum(m["loadedState"] == "loaded" for m in mac_link["models"]),
                   "linkAgeSeconds": mac_link["ageSeconds"], "linkSource": mac_link["source"],
                   "detail": f"Linked through SharedChami ({mac_link['source']}, {mac_link['ageSeconds']:.0f} s old); "
                             "LM Studio itself is not open to the LAN"}

        clients: list = []
        if self.clients_fn is not None:
            try:
                clients, client_sources = self.clients_fn(sampled)
                sources.extend(client_sources)
            except Exception:  # noqa: BLE001 - client metadata never interrupts the feed
                sources.append(probes._source("client-metadata", "Client metadata", "error",
                                              "Client model records unavailable"))

        up = [lane for lane in lanes if lane["status"] in probes.LANE_UP]
        worker_state = "advertised" if up else "degraded"
        worker = {
            "state": worker_state, "ageSeconds": 0.0, "workerVersion": WORKER_VERSION,
            "scope": "local-observer",
            "detail": "Lanes read directly on this PC" if up else "No lane answers on this PC",
            "lanes": {lane["id"]: {"up": lane["status"] in probes.LANE_UP, "model": lane["expectedModel"],
                                   "slotsBusy": lane["slotsBusy"], "slotsTotal": lane["slotsTotal"],
                                   "status": lane["status"], "servedModel": lane["servedModel"],
                                   "port": lane["port"], "device": lane["device"],
                                   "liveTokensPerSecond": lane.get("liveTokensPerSecond")}
                      for lane in lanes},
            # The dashboard voids the whole GPU sample on one unmeasured field, so only fully read cards go here;
            # pcGpus keeps every card with None for what nvidia-smi did not report.
            "gpus": [{key: gpu[key] for key in GPU_KEYS} for gpu in gpus if all(gpu[key] is not None for key in GPU_KEYS)],
        }
        mismatch = [lane["id"] for lane in lanes if lane["status"] == "identity_mismatch"]
        if mismatch:
            worker["lanesError"] = "identity mismatch on " + ", ".join(mismatch)
        components = [probes.nisi_component(lanes, self.bin_root),
                      {"id": "jev", "label": "Jev", "state": "unknown",
                       "detail": "Jev triage runs through the route queue on this PC; no passive opt-in record is read",
                       "lastJudgedAgeSeconds": None}]
        return {
            "schemaVersion": 1, "host": "windows", "edition": "windows", "observerVersion": VERSION,
            "observedAt": observed, "sampledAt": sampled,
            "models": rows, "sources": sources, "pipeline": pipeline, "components": components,
            "windowsWorker": worker, "windowsJobs": jobs, "memory": memory,
            "macPeer": mac, "share": share, "clients": clients, "peers": peers, "hostId": self.host,
            "pcAdapters": self._adapter_list(), "pcGpus": gpus,
            "lanes": lanes,
            "modelControl": {"supported": False,
                             "reason": "The PC lanes are detached llama-server processes; load and unload are not offered here."},
        }
