"""Opt-in loopback lane identity observations; never owns transport or inference."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import threading
import time

from inference_stats import edges_for, expire_map, machine_node, make_node

_ID = re.compile(r"[a-z][a-z0-9_-]{0,15}\Z")
_LABEL = re.compile(r"[A-Za-z0-9 ._-]{1,80}\Z")
MAX_MACHINES = 32
MAX_LANES = 64
MAX_CONFIG_BYTES = 65536
PROBE_INTERVAL = 3.0
PROTECTED_PORTS = frozenset({8081, 1234, 1235, 8080, 11434, 18082, 8768, 8769})
_LOG = logging.getLogger(__name__)
_CONFIG_ERRORS = {}
_ERROR_LOCK = threading.Lock()


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _invalid(machine_id, field, value, rule, lane_id=None):
    """Include enough context to repair a rejected registry entry."""
    location = f"machine {machine_id!r}"
    if lane_id is not None:
        location += f", lane {lane_id!r}"
    raise ValueError(f"{location}, field {field!r}, value {value!r}: {rule}")


def _fields(value, required, optional, machine_id, field, lane_id=None):
    if not isinstance(value, dict):
        _invalid(machine_id, field, value, "must be an object", lane_id)
    for key in sorted(required):
        if key not in value:
            _invalid(machine_id, key, "<missing>", "required field", lane_id)
    for key in sorted(set(value) - required - optional, key=repr):
        _invalid(machine_id, key, value[key], "field is not allowed", lane_id)


def validate_config(document):
    """Strict port-only schema. A registry machine is a separate namespace."""
    no_machine = "<unavailable>"
    _fields(document, {"schemaVersion", "machines"}, set(), no_machine, "registry")
    version = document["schemaVersion"]
    if type(version) is not int or version != 1:
        _invalid(no_machine, "schemaVersion", version, "must be integer 1")
    machines = document["machines"]
    if not isinstance(machines, list) or not 1 <= len(machines) <= MAX_MACHINES:
        _invalid(no_machine, "machines", machines, f"must be a list of 1 to {MAX_MACHINES} machines")
    seen_machines, seen_ports, total, replacements = set(), set(), 0, 0
    id_rule = "must start with a lowercase ASCII letter, followed by up to 15 lowercase ASCII letters, digits, underscores or hyphens"
    label_rule = "must be 1 to 80 ASCII letters, digits, spaces, periods, underscores or hyphens"
    for machine in machines:
        identity = machine.get("id", "<missing>") if isinstance(machine, dict) else no_machine
        _fields(machine, {"id", "label", "lanes"}, {"replaces"}, identity, "machine")
        label, lanes = machine["label"], machine["lanes"]
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            _invalid(identity, "id", identity, id_rule)
        if identity in seen_machines:
            _invalid(identity, "id", identity, "machine id must be unique")
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            _invalid(identity, "label", label, label_rule)
        if not isinstance(lanes, list) or not 1 <= len(lanes) <= 8:
            _invalid(identity, "lanes", lanes, "must be a list of 1 to 8 lanes")
        if "replaces" in machine:
            if machine["replaces"] != "legacy-pc":
                _invalid(identity, "replaces", machine["replaces"], "must be 'legacy-pc'")
            replacements += 1
            if replacements > 1:
                _invalid(identity, "replaces", machine["replaces"], "only one machine may replace legacy-pc")
        seen_machines.add(identity)
        seen_lanes = set()
        for lane in lanes:
            lane_id = lane.get("id", "<missing>") if isinstance(lane, dict) else "<unavailable>"
            _fields(lane, {"id", "label", "localPort"}, {"timeoutSeconds", "contextTokens", "thinking"},
                    identity, "lane", lane_id)
            port = lane["localPort"]
            if type(port) is not int or not 1024 <= port <= 65535:
                _invalid(identity, "localPort", port, "must be an integer from 1024 to 65535", lane_id)
            if port in PROTECTED_PORTS:
                _invalid(identity, "localPort", port, "must not be a protected port", lane_id)
            if not isinstance(lane_id, str) or not _ID.fullmatch(lane_id):
                _invalid(identity, "id", lane_id, id_rule, lane_id)
            if lane_id in seen_lanes:
                _invalid(identity, "id", lane_id, "lane id must be unique within its machine", lane_id)
            if not isinstance(lane["label"], str) or not _LABEL.fullmatch(lane["label"]):
                _invalid(identity, "label", lane["label"], label_rule, lane_id)
            if port in seen_ports:
                _invalid(identity, "localPort", port, "port must be unique across the registry", lane_id)
            timeout = lane.get("timeoutSeconds", 1.5)
            if not _number(timeout) or not 0 < timeout <= 6:
                _invalid(identity, "timeoutSeconds", timeout, "must be a finite number greater than 0 and at most 6", lane_id)
            if "contextTokens" in lane and (type(lane["contextTokens"]) is not int or lane["contextTokens"] < 0):
                _invalid(identity, "contextTokens", lane["contextTokens"], "must be a nonnegative integer", lane_id)
            if "thinking" in lane and lane["thinking"] not in ("on", "off", "unknown"):
                _invalid(identity, "thinking", lane["thinking"], "must be 'on', 'off' or 'unknown'", lane_id)
            seen_lanes.add(lane_id)
            seen_ports.add(port)
            total += 1
            if total > MAX_LANES:
                _invalid(identity, "lanes", total, f"registry must have at most {MAX_LANES} lanes", lane_id)
    return document


def _reject_constant(value):
    raise ValueError("invalid_json_constant")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _log_disabled(selected, reason):
    # Suppress the same failure across sampler ticks.
    with _ERROR_LOCK:
        key = str(selected)
        if _CONFIG_ERRORS.get(key) != reason:
            if len(_CONFIG_ERRORS) >= 64:
                _CONFIG_ERRORS.clear()
            _CONFIG_ERRORS[key] = reason
            _LOG.warning("Remote machine registry disabled: %s", reason)
    return {"error": "config_invalid"}


def load_config(path=None, environ=None):
    """Read only an owned, safe regular descriptor; special files never block."""
    env = os.environ if environ is None else environ
    explicit = path is not None or bool(env.get("AGIW_REMOTE_MACHINES_CONFIG"))
    selected = path if path is not None else env.get("AGIW_REMOTE_MACHINES_CONFIG")
    selected = Path(selected) if selected is not None else Path.home() / ".config/agiw/remote-machines.json"
    descriptor = None
    reason = "open_failed"
    try:
        # Unsupported platforms disable just the feature instead of weakening custody.
        if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK", "getuid")):
            raise ValueError("safe_open_unsupported")
        descriptor = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not_regular")
        if info.st_uid != os.getuid():
            raise ValueError("wrong_owner")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("unsafe_mode")
        if info.st_size > MAX_CONFIG_BYTES:
            raise ValueError("size_limit")
        raw = bytearray()
        while len(raw) <= MAX_CONFIG_BYTES:
            chunk = os.read(descriptor, min(8192, MAX_CONFIG_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("size_limit")
        reason = "json_invalid"
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant)
        reason = "schema_invalid"
        result = validate_config(document)
        with _ERROR_LOCK:
            _CONFIG_ERRORS.pop(str(selected), None)
        return result
    except FileNotFoundError:
        return _log_disabled(selected, "missing") if explicit else None
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError) as error:
        if type(error) is ValueError and reason != "json_invalid":
            reason = str(error)
        return _log_disabled(selected, reason)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def replaces_legacy_pc(config):
    if config is None or config.get("error"):
        return False
    validate_config(config)
    return any(machine.get("replaces") == "legacy-pc" for machine in config["machines"])


def lane_targets(config):
    """Stable namespaced IDs and local bases, without a configured model claim."""
    if config is None or config.get("error"):
        return []
    validate_config(config)
    targets = []
    for machine in config["machines"]:
        machine_id = f"registry:{machine['id']}"
        for lane in machine["lanes"]:
            target = {"id": f"{machine_id}:{lane['id']}", "machine": machine_id,
                      "lane": lane["id"], "label": lane["label"],
                      "base": f"http://127.0.0.1:{lane['localPort']}",
                      "timeoutSeconds": lane.get("timeoutSeconds", 1.5)}
            target.update({key: lane[key] for key in ("contextTokens", "thinking") if key in lane})
            targets.append(target)
    return targets


def _identity(body):
    parsed = json.loads(body, object_pairs_hook=_unique, parse_constant=_reject_constant)
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("identity")
    identity = data[0].get("id")
    if (not isinstance(identity, str) or not identity.strip() or len(identity) > 1024
            or not identity.isprintable()):
        raise ValueError("identity")
    return identity


def _iso(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _lane_node(target, result, now, interval, success=None, started=None, running=False):
    model, latency, reason, completed = result or (None, None, "pending", None)
    node = make_node(id=target["id"], machine=target["machine"], role="llama", label=target["label"],
                     health="ok" if model is not None else "not_measured", endpoint=target["base"],
                     model_id=model, reason=reason, observed_at=_iso(completed) if completed is not None else None,
                     latency_ms=latency,
                     detail="Server-reported identity only. Inference and hardware are not measured.")
    node.update(observationSource="registry", probeIntervalSeconds=interval,
                ageSeconds=max(0, now - completed) if completed is not None else None)
    node.update({key: target[key] for key in ("contextTokens", "thinking") if key in target})
    if success is None and model is not None and completed is not None:
        success = (model, completed)
    node.update(configuredEndpoint=target["base"], probeTimeoutSeconds=target["timeoutSeconds"],
                probeState="running" if running else "completed" if completed is not None else "pending",
                probeStartedAt=_iso(started) if started is not None else None,
                lastProbeAt=_iso(completed) if completed is not None else None,
                lastSuccessAt=_iso(success[1]) if success is not None else None,
                lastSuccessModelId=success[0] if success is not None else None)
    return node


def _probe(target, fetch):
    model, latency, reason = None, None, "probe_failed"
    try:
        if fetch is None:
            from inference_collect import loopback_get
            status, body, elapsed = loopback_get(target["base"] + "/v1/models",
                                                  timeout=target["timeoutSeconds"], strict=True)
        else:
            status, body, elapsed = fetch(target["base"] + "/v1/models")
        if type(status) is int and status == 200:
            model = _identity(body)
            reason = None
            if _number(elapsed) and elapsed >= 0:
                latency = elapsed
        else:
            reason = "upstream_error"
    except (ValueError, TypeError, KeyError, RecursionError):
        reason = "identity_unknown"
    except Exception as error:
        # Only public reason codes may cross the boundary; never exception text.
        from inference_collect import ProbeError
        reason = error.reason if isinstance(error, ProbeError) else "probe_failed"
    return model, latency, reason


def _nodes(config, targets, results, now, interval, successes=None, starts=None, active=None):
    successes, starts, active = successes or {}, starts or {}, active or set()
    lanes = [_lane_node(target, results.get(target["id"]), now, interval,
                        successes.get(target["id"]), starts.get(target["id"]), target["id"] in active)
             for target in targets]
    nodes = []
    for machine in config["machines"]:
        machine_id = f"registry:{machine['id']}"
        children = [lane for lane in lanes if lane["machine"] == machine_id]
        # Age each completed lane before rolling up the root.
        expire_map({"nodes": children}, now)
        stamps = [child["observedAt"] for child in children if child["observedAt"]]
        observed = min(stamps) if len(stamps) == len(children) else None
        root = machine_node(machine_id, machine["label"], children, observed_at=observed)
        if any(child["health"] != "ok" for child in children):
            root.update(health="not_measured", reason="lane_unknown")
        root.update(detail="Configured lane identities only. Whole-machine health and inventory are unknown.",
                    inventoryScope="configured-lanes", observationSource="registry", probeIntervalSeconds=interval,
                    ageSeconds=max((child["ageSeconds"] for child in children if child["ageSeconds"] is not None), default=None))
        if machine.get("replaces") == "legacy-pc":
            root["replaces"] = "legacy-pc"
        nodes.extend([root, *children])
    return nodes


def collect(config, *, fetch=None, now=None):
    """Explicit synchronous observation helper; snapshots use BackgroundProbes."""
    targets = lane_targets(config)
    if not targets:
        return []
    now = time.time() if now is None else now
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        readings = list(pool.map(lambda target: _probe(target, fetch), targets))
    results = {target["id"]: (*reading, now) for target, reading in zip(targets, readings)}
    return _nodes(config, targets, results, now, PROBE_INTERVAL)


def unavailable_nodes(config, *, now=None):
    """Retain configured machines when another part of the collector fails."""
    targets = lane_targets(config)
    if not targets:
        return []
    results = {target["id"]: (None, None, "collector_failed", None) for target in targets}
    return _nodes(config, targets, results, time.time() if now is None else now, PROBE_INTERVAL)


class BackgroundProbes:
    """Eight independently bounded probes; snapshots never wait for network I/O.

    Due lanes are ordered by last start, so queued lanes precede repeat probes.
    A configuration change invalidates every old result and in-flight generation.
    """
    def __init__(self, *, interval=PROBE_INTERVAL):
        if not _number(interval) or interval <= 0:
            raise ValueError("probe_interval")
        self.interval = interval
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="registry-probe")
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.signature = None
        self.generation = 0
        self.targets = []
        self.results = {}
        self.successes = {}
        self.probe_started = {}
        self.started = {}
        self.running = {}
        self.thread = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(1)
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _complete(self, future, generation, identity):
        try:
            reading = future.result()
        except Exception:
            reading = (None, None, "probe_failed")
        completed = time.time()
        with self.lock:
            self.running.pop(future, None)
            if generation == self.generation and not self.stopped.is_set():
                self.results[identity] = (*reading, completed)
                if reading[0] is not None:
                    self.successes[identity] = (reading[0], completed)
        self.wake.set()

    def _run(self):
        while not self.stopped.is_set():
            with self.lock:
                now = time.monotonic()
                active = {identity for generation, identity in self.running.values() if generation == self.generation}
                due = [target for target in self.targets if target["id"] not in active
                       and now - self.started.get(target["id"], -math.inf) >= self.interval]
                due.sort(key=lambda target: self.started.get(target["id"], -math.inf))
                for target in due[:max(0, 8 - len(self.running))]:
                    identity, generation = target["id"], self.generation
                    self.started[identity] = now
                    self.probe_started[identity] = time.time()
                    future = self.pool.submit(_probe, target, None)
                    self.running[future] = (generation, identity)
                    future.add_done_callback(lambda done, generation=generation, identity=identity:
                                             self._complete(done, generation, identity))
            self.wake.wait(.02)
            self.wake.clear()

    def snapshot(self, config, *, now=None):
        targets = lane_targets(config)
        signature = json.dumps(config, sort_keys=True, allow_nan=False)
        with self.lock:
            if signature != self.signature:
                self.generation += 1
                self.signature = signature
                self.targets = copy.deepcopy(targets)
                self.results = {}
                self.successes = {}
                self.probe_started = {}
                self.started = {}
            if not targets:
                return []
            if self.thread is None and not self.stopped.is_set():
                self.thread = threading.Thread(target=self._run, name="registry-scheduler", daemon=True)
                self.thread.start()
            results = dict(self.results)
            successes = dict(self.successes)
            starts = dict(self.probe_started)
            active = {identity for generation, identity in self.running.values() if generation == self.generation}
        self.wake.set()
        return _nodes(config, targets, results, time.time() if now is None else now, self.interval,
                      successes, starts, active)


_DEFAULT_PROBES = None
_DEFAULT_LOCK = threading.Lock()


def background_snapshot(config, *, now=None):
    global _DEFAULT_PROBES
    with _DEFAULT_LOCK:
        if _DEFAULT_PROBES is None:
            if config is None or config.get("error"):
                return []
            _DEFAULT_PROBES = BackgroundProbes()
    return _DEFAULT_PROBES.snapshot(config, now=now)


def extend_map(document, config, nodes):
    """Preserve legacy visibility unless replacement is explicit and valid."""
    if replaces_legacy_pc(config):
        document["nodes"] = [node for node in document["nodes"] if node["machine"] != "pc"]
        document["tunnel"] = None
    document["nodes"].extend(nodes)
    document["edges"] = edges_for(document["nodes"])
    document["remoteMachines"] = {"schemaVersion": 1, "state": "invalid" if config.get("error") else "configured",
                                  "replacesLegacyPc": replaces_legacy_pc(config)}
    return document


def project_snapshot(snapshot):
    """Project the same registry observations into every machine summary.

    These rows describe configured lanes only: their identities are not a local
    model inventory, machine-wide health measurement, or inference admission.
    Calling this again after ``expire_map`` keeps API reads and the map in step.
    Without registry roots the legacy snapshot is left byte-for-byte alone.
    """
    document = snapshot.get("inference")
    if not isinstance(document, dict):
        return
    nodes = document.get("nodes") or []
    roots = [node for node in nodes if node.get("role") == "machine"
             and node.get("observationSource") == "registry"]
    if not roots:
        return
    replaces = any(root.get("replaces") == "legacy-pc" for root in roots)
    def placeholder(row):
        return (row.get("observedAt") is None and row.get("trust", "unverified") == "unverified"
                and row.get("reasonCode", "not_observed") == "not_observed"
                and (row.get("id") or row.get("host")) in ("pc", "windows"))

    rows = [row for row in snapshot.get("machines", [])
            if row.get("observationSource") != "registry"
            and not (replaces and row.get("platform") == "windows"
                     and placeholder(row))]
    for root in roots:
        identity = root["id"]
        interval = root.get("probeIntervalSeconds", PROBE_INTERVAL)
        row = {"id": identity, "host": identity, "platform": "remote", "label": root["label"],
               "observationSource": "registry", "inventoryScope": "configured-lanes",
               "state": "up" if root.get("health") == "ok" else "unknown",
               "health": root.get("health", "not_measured"), "reason": root.get("reason"),
               "reasonCode": root.get("reason") or "observed", "trust": "registry",
               "observedAt": root.get("observedAt"), "ageSeconds": root.get("ageSeconds"),
               "freshForSeconds": 2 * interval, "modelsComplete": False,
               "loadedCount": None, "busyCount": None, "promptTokensPerSecond": None,
               "generationTokensPerSecond": None, "handsOff": None,
               "lanes": [copy.deepcopy(node) for node in nodes if node.get("machine") == identity
                         and node.get("role") not in ("machine", "client")]}
        if root.get("replaces") == "legacy-pc":
            row["replaces"] = "legacy-pc"
        rows.append(row)
    snapshot["machines"] = rows
