"""Read loopback inference endpoints into the map in inference_stats.

PC llama.cpp lanes use /metrics, /slots and /props. When those are absent, a
timed GET /health is the observation. The router is GET /v1/models. LM Studio
in the Mac sampler reuses verified telemetry inventory. The standalone probe
helper uses GET /api/v0/models, then /v1/models. Veritas is GET of
VERITAS_REVIEWER_URL. No request asks a model to generate. Slot prompts are
not kept.
"""
from __future__ import annotations

import datetime as dt
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit
import http.client

from inference_stats import MAP_SCHEMA, apply_rates, client_nodes, edges_for, machine_node, make_node
import remote_machines

PC_LANES = (
    {"id": "pc:router", "role": "router", "label": "Router", "port": 1234},
    {"id": "pc:gpt-oss-20b", "role": "llama", "label": "gpt-oss-20b", "port": 1245,
     "expectedModel": "gpt-oss-20b"},
    {"id": "pc:qwen3.8-27b", "role": "llama", "label": "qwen3.8-27b", "port": 1236,
     "expectedModel": "qwen3.8-27b"},
)
_BY_ID = {lane["id"]: lane for lane in PC_LANES}
_METRIC = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(\S+)")
_COUNTERS = ("prompt_tokens_total", "tokens_predicted_total", "prompt_seconds_total", "tokens_predicted_seconds_total")
_GAUGES = ("requests_deferred", "requests_processing", "n_busy_slots")
_WANTED = set(_COUNTERS + _GAUGES)
_LMSTUDIO = "http://127.0.0.1:1234"


class ProbeError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def collector_diagnostic(stage, error, *, now=None, last_success=None):
    """Bounded public failure context. Never expose exception text or private paths."""
    stage = stage if isinstance(stage, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,47}", stage) else "collector"
    code = ("timeout" if isinstance(error, TimeoutError) else
            "permission_denied" if isinstance(error, PermissionError) else
            "io_error" if isinstance(error, OSError) else
            "missing_field" if isinstance(error, KeyError) else
            "invalid_value" if isinstance(error, ValueError) else
            "invalid_type" if isinstance(error, TypeError) else "collector_failed")
    failed_at = now if _finite(now) else time.time()
    return {"stage": stage, "code": code, "failedAt": failed_at,
            "lastSuccessfulSampleAt": last_success if _finite(last_success) else None}


_MIN_RATE_GAP = 0.5          # seconds between two scrapes before a wall-clock rate means anything
_MIN_COUNTER_SECONDS = 0.01  # llama-server's own busy-seconds delta below this is rounding, not work


class RateMemory:
    """Last prometheus counters, in memory only, so a second scrape can yield tok/s."""

    def __init__(self):
        self.previous = {}

    def update(self, key, counters, now):
        prev = self.previous.get(key)
        self.previous[key] = {"at": now, **counters}
        if not prev:
            return None, None
        elapsed = now - prev["at"]
        if not _finite(elapsed) or elapsed < _MIN_RATE_GAP:
            # Two scrapes a few milliseconds apart turn one token into thousands per second.
            return None, None
        return (_speed(prev, counters, elapsed, "prompt_tokens_total", "prompt_seconds_total"),
                _speed(prev, counters, elapsed, "tokens_predicted_total", "tokens_predicted_seconds_total"))


def _speed(prev, counters, elapsed, tokens, seconds):
    before, after = prev.get(tokens), counters.get(tokens)
    if before is None or after is None or after < before:
        return None
    spent_before, spent_after = prev.get(seconds), counters.get(seconds)
    if spent_before is not None and spent_after is not None and spent_after - spent_before >= _MIN_COUNTER_SECONDS:
        rate = (after - before) / (spent_after - spent_before)
    else:
        rate = (after - before) / elapsed
    return rate if _finite(rate) and rate >= 0 else None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _whole(value):
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and value >= 0 and value.is_integer():
        return int(value)
    return None


def _ms(elapsed):
    return round(float(elapsed), 1) if _finite(elapsed) and elapsed >= 0 else None


def _iso(when):
    return dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_chunked_read(response, limit):
    """Require every chunk boundary and the final empty trailer line.

    HTTPResponse tolerates EOF while discarding trailers. That tolerance cannot
    establish a complete identity document, so registry reads check the framing.
    """
    body = bytearray()
    while True:
        line = response.fp.readline(8193)
        if (len(line) > 8192 or not line.endswith(b"\r\n")
                or any(byte < 32 or byte > 126 for byte in line[:-2])):
            raise ProbeError("incomplete_body")
        size_text = line[:-2].split(b";", 1)[0]
        if not re.fullmatch(rb"[0-9a-fA-F]{1,16}", size_text):
            raise ProbeError("incomplete_body")
        size = int(size_text, 16)
        if not size:
            trailer_bytes = 0
            while True:
                trailer = response.fp.readline(8193)
                trailer_bytes += len(trailer)
                if (len(trailer) > 8192 or trailer_bytes > 65536
                        or not trailer.endswith(b"\r\n")):
                    raise ProbeError("incomplete_body")
                if trailer == b"\r\n":
                    return bytes(body)
                if b":" not in trailer:
                    raise ProbeError("incomplete_body")
        if len(body) + size > limit:
            raise ProbeError("oversize")
        chunk = response.fp.read(size)
        if len(chunk) != size or response.fp.read(2) != b"\r\n":
            raise ProbeError("incomplete_body")
        body.extend(chunk)


def loopback_get(url, timeout=0.4, limit=1024 * 1024, *, strict=False):
    """GET one loopback URL within a total deadline; never follow redirects.

    Registry identity requires strict framing and UTF-8. Legacy text endpoint
    readers retain their existing permissive decoding and close-delimited reads.
    """
    parts = urlsplit(url)
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ProbeError("refused")
    started = time.monotonic()
    deadline = started + timeout
    connection = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=timeout)
    response = None
    timer = None
    expired = threading.Event()
    try:
        connection.connect()
        sock = connection.sock
        def expire():
            expired.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError("timeout")
        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        connection.request("GET", parts.path or "/", headers={"Accept": "application/json, text/plain"})
        response = connection.getresponse()
        expected_length = None
        strict_chunked = False
        if strict:
            lengths = response.headers.get_all("Content-Length", [])
            transfers = response.headers.get_all("Transfer-Encoding", [])
            if (len(lengths) > 1 or len(transfers) > 1 or lengths and transfers
                    or transfers and transfers[0].strip().lower() != "chunked"):
                raise ProbeError("ambiguous_framing")
            if lengths:
                value = lengths[0].strip()
                if not re.fullmatch(r"[0-9]+", value) or len(value) > 20:
                    raise ProbeError("invalid_length")
                expected_length = int(value)
                if expected_length > limit:
                    raise ProbeError("oversize")
            elif not transfers:
                raise ProbeError("missing_length")
            strict_chunked = bool(transfers)
        body = (_strict_chunked_read(response, limit) if strict_chunked
                else response.read(limit + 1))
        if expired.is_set() or time.monotonic() >= deadline:
            raise ProbeError("timeout")
        if len(body) > limit:
            raise ProbeError("oversize")
        if strict and expected_length is not None and len(body) != expected_length:
            raise ProbeError("incomplete_body")
        try:
            decoded = body.decode("utf-8", "strict" if strict else "replace")
        except UnicodeDecodeError:
            raise ProbeError("invalid_utf8")
        return response.status, decoded, (time.monotonic() - started) * 1000
    except ProbeError:
        if expired.is_set() or time.monotonic() >= deadline:
            raise ProbeError("timeout")
        raise
    except TimeoutError:
        raise ProbeError("timeout")
    except ConnectionRefusedError:
        raise ProbeError("refused")
    except http.client.IncompleteRead:
        raise ProbeError("timeout" if expired.is_set() or time.monotonic() >= deadline else "incomplete_body")
    except (OSError, http.client.HTTPException):
        raise ProbeError("timeout" if expired.is_set() or time.monotonic() >= deadline else "unavailable")
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        if response is not None:
            response.close()
        connection.close()


def _request(fetch, url):
    try:
        status, body, elapsed = fetch(url)
    except ProbeError as error:
        return error.reason, None
    if type(status) is not int or not isinstance(body, str):
        return "unavailable", None
    return None, (status, body, elapsed)


def parse_metrics(text):
    found = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _METRIC.match(line)
        if not match:
            continue
        name = match.group(1)
        if name.startswith("llamacpp:"):
            name = name[len("llamacpp:"):]
        elif name.startswith("llamacpp_"):
            name = name[len("llamacpp_"):]
        if name not in _WANTED:
            continue
        try:
            value = float(match.group(2))
        except ValueError:
            continue
        if math.isfinite(value) and value >= 0:
            found[name] = value
    return found


def parse_slots(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    busy = total = 0
    limit = used = None
    for slot in data:
        if not isinstance(slot, dict):
            continue
        total += 1
        if slot.get("is_processing") is True:
            busy += 1
        ctx = _whole(slot.get("n_ctx"))
        if ctx is not None:
            limit = ctx if limit is None else max(limit, ctx)
        past = _whole(slot.get("n_past"))
        if past is not None:
            used = past if used is None else max(used, past)
    return {"busy": busy, "total": total, "limit": limit, "used": used} if total else None


def parse_props(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    model = None
    for key in ("model_alias", "model", "model_path"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            model = value.strip()
            break
    settings = data.get("default_generation_settings")
    ctx = _whole(settings.get("n_ctx")) if isinstance(settings, dict) else None
    return {"model": model, "context": ctx}


def parse_router_models(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    models = []
    for item in rows[:16]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or len(item["id"]) > 1024:
            continue
        upstream = _loopback_upstream(item)
        models.append({"id": item["id"], "upstream": upstream})
    return models


def _loopback_upstream(item):
    sources = [item]
    if isinstance(item.get("meta"), dict):
        sources.append(item["meta"])
    for source in sources:
        for key in ("upstream", "upstream_url", "url"):
            value = source.get(key)
            if isinstance(value, str) and value.startswith("http://127.0.0.1:") and len(value) <= 200:
                return value
    return None


def _lmstudio_loaded(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    loaded = []
    for item in rows:
        if not isinstance(item, dict) or item.get("state") not in ("loaded", "not-loaded"):
            return None
        if item["state"] != "loaded":
            continue
        if not isinstance(item.get("id"), str) or not item["id"] or len(item["id"]) > 1024:
            continue
        ctx = _whole(item.get("max_context_length"))
        loaded.append({"id": item["id"], "context": ctx})
    return loaded


def _model_fields(model, expected):
    if model is not None and len(model) > 1024:
        return None, "degraded", "model_id_too_long"
    if expected and model and expected not in model:
        return model, "degraded", "model_mismatch"
    if model:
        return model, "ok", None
    return None, "ok", "model_unreported"


def _blank(node_id, machine, role, label, observed, *, health, reason, endpoint=None, **extra):
    return make_node(id=node_id, machine=machine, role=role, label=label, health=health, reason=reason,
                     endpoint=endpoint, observed_at=observed, **extra)


def _transport_failure(spec, endpoint, observed, reason):
    health = "down" if reason == "refused" else "not_measured"
    return _blank(spec["id"], spec.get("machine", "pc"), spec["role"], spec["label"], observed,
                  health=health, reason=reason, endpoint=endpoint)


def direct_targets():
    return [{"id": lane["id"], "service": f"127.0.0.1:{lane['port']}",
             "base": f"http://127.0.0.1:{lane['port']}"} for lane in PC_LANES]


def closed_targets():
    return [{"id": lane["id"], "service": f"127.0.0.1:{lane['port']}", "base": None} for lane in PC_LANES]


def _read_llama(spec, base, fetch, rates, now, observed, endpoint):
    metrics = slots = props = None
    latency = None
    for path, kind in (("/metrics", "metrics"), ("/slots", "slots"), ("/props", "props")):
        failure, result = _request(fetch, base + path)
        if failure in ("refused", "timeout") and metrics is None and slots is None and props is None:
            return _transport_failure(spec, endpoint, observed, failure)
        if result and result[0] == 200:
            latency = latency if latency is not None else _ms(result[2])
            if kind == "metrics":
                metrics = parse_metrics(result[1])
            elif kind == "slots":
                slots = parse_slots(result[1])
            else:
                props = parse_props(result[1])
    if metrics is None and slots is None and props is None:
        failure, result = _request(fetch, base + "/health")
        if failure:
            return _transport_failure(spec, endpoint, observed, failure)
        if result and 200 <= result[0] < 300:
            return _blank(spec["id"], "pc", "llama", spec["label"], observed, health="ok", reason=None,
                          endpoint=endpoint, latency_ms=_ms(result[2]), detail="Timed health probe")
        return _blank(spec["id"], "pc", "llama", spec["label"], observed, health="down",
                      reason="upstream_error", endpoint=endpoint, latency_ms=_ms(result[2]) if result else None)
    prompt = generation = None
    if metrics is not None:
        prompt, generation = rates.update(spec["id"], {key: metrics[key] for key in _COUNTERS if key in metrics}, now)
    model = props.get("model") if props else None
    context = props.get("context") if props else None
    if slots and slots.get("limit") is not None:
        context = slots["limit"]
    model_id, health, reason = _model_fields(model, spec.get("expectedModel"))
    if slots:
        slots_busy, slots_total, context_used = slots["busy"], slots["total"], slots.get("used")
    else:
        gauge = metrics.get("n_busy_slots") if metrics else None
        if gauge is None and metrics:
            gauge = metrics.get("requests_processing")
        slots_busy, slots_total, context_used = _whole(gauge), None, None
    return _blank(spec["id"], "pc", "llama", spec["label"], observed, health=health, reason=reason,
                  endpoint=endpoint, model_id=model_id, latency_ms=latency,
                  prompt_tokens_per_second=prompt, generation_tokens_per_second=generation,
                  queue_depth=_whole(metrics.get("requests_deferred")) if metrics else None,
                  slots_busy=slots_busy, slots_total=slots_total,
                  context_used=context_used, context_limit=context)


def _read_router(spec, base, fetch, observed, endpoint):
    failure, result = _request(fetch, base + "/v1/models")
    if failure:
        return _transport_failure({**spec, "machine": "pc"}, endpoint, observed, failure)
    if not result or result[0] != 200:
        return _blank(spec["id"], "pc", "router", spec["label"], observed, health="down",
                      reason="upstream_error", endpoint=endpoint, latency_ms=_ms(result[2]) if result else None)
    models = parse_router_models(result[1])
    if models is None:
        return _blank(spec["id"], "pc", "router", spec["label"], observed, health="degraded",
                      reason="models_unreadable", endpoint=endpoint, latency_ms=_ms(result[2]))
    return _blank(spec["id"], "pc", "router", spec["label"], observed, health="ok" if models else "degraded",
                  reason=None if models else "no_models", endpoint=endpoint, latency_ms=_ms(result[2]),
                  models=models)


def _read_pc(target, fetch, rates, now, observed, *, unconfigured=False):
    spec = _BY_ID.get(target.get("id"))
    endpoint = target.get("base")
    if spec is None:
        return _blank(str(target.get("id") or "pc:unknown"), "pc", "llama", "Unknown lane", observed,
                      health="not_measured", reason="unknown_lane", endpoint=endpoint)
    if not target.get("base"):
        # No SSH user/host set up at all is "not configured"; a set-up tunnel that is closed stays "tunnel_down".
        return _blank(spec["id"], "pc", spec["role"], spec["label"], observed, health="not_measured",
                      reason="not_configured" if unconfigured else "tunnel_down", endpoint=endpoint)
    if spec["role"] == "router":
        return _read_router(spec, target["base"], fetch, observed, endpoint)
    node = _read_llama(spec, target["base"], fetch, rates, now, observed, endpoint)
    if target.get("service") and endpoint != "http://" + target["service"]:
        node["upstream"] = "http://" + target["service"]
    return node


def _read_lmstudio(fetch, observed):
    spec = {"id": "mac:lmstudio", "machine": "mac", "role": "lmstudio", "label": "LM Studio"}
    failure, result = _request(fetch, _LMSTUDIO + "/api/v0/models")
    if failure in ("refused", "timeout"):
        return _transport_failure(spec, "127.0.0.1:1234", observed, failure)
    loaded = _lmstudio_loaded(result[1]) if result and result[0] == 200 else None
    if loaded is None:
        failure, result = _request(fetch, _LMSTUDIO + "/v1/models")
        if failure:
            return _transport_failure(spec, "127.0.0.1:1234", observed, failure)
        if result and result[0] == 200:
            return _blank(spec["id"], "mac", "lmstudio", spec["label"], observed, health="not_measured",
                          reason="load_state_unreported", endpoint="127.0.0.1:1234", latency_ms=_ms(result[2]))
        return _blank(spec["id"], "mac", "lmstudio", spec["label"], observed, health="down",
                      reason="upstream_error", endpoint="127.0.0.1:1234")
    latency = _ms(result[2])
    if len(loaded) == 1:
        model_id, health, reason = _model_fields(loaded[0]["id"], None)
        return _blank(spec["id"], "mac", "lmstudio", spec["label"], observed, health=health, reason=reason,
                      endpoint="127.0.0.1:1234", model_id=model_id, latency_ms=latency,
                      context_limit=loaded[0]["context"])
    if not loaded:
        return _blank(spec["id"], "mac", "lmstudio", spec["label"], observed, health="degraded",
                      reason="model_not_loaded", endpoint="127.0.0.1:1234", latency_ms=latency)
    return _blank(spec["id"], "mac", "lmstudio", spec["label"], observed, health="ok",
                  reason="multiple_loaded", endpoint="127.0.0.1:1234", latency_ms=latency)


def _read_veritas(fetch, observed, url):
    spec = {"id": "mac:veritas", "machine": "mac", "role": "veritas", "label": "Veritas reviewer"}
    if not url:
        return _blank(spec["id"], "mac", "veritas", spec["label"], observed,
                      health="not_measured", reason="not_configured")
    parts = urlsplit(url)
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None
            or parts.username or parts.query or parts.fragment):
        return _blank(spec["id"], "mac", "veritas", spec["label"], observed,
                      health="not_measured", reason="url_not_loopback")
    failure, result = _request(fetch, url)
    if failure:
        return _transport_failure(spec, f"127.0.0.1:{parts.port}", observed, failure)
    if not result or not 200 <= result[0] < 300:
        return _blank(spec["id"], "mac", "veritas", spec["label"], observed, health="down",
                      reason="upstream_error", endpoint=f"127.0.0.1:{parts.port}",
                      latency_ms=_ms(result[2]) if result else None)
    model_id = None
    try:
        body = json.loads(result[1])
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        for key in ("model", "model_id", "modelId"):
            value = body.get(key)
            if isinstance(value, str) and value and len(value) <= 1024:
                model_id = value
                break
    return _blank(spec["id"], "mac", "veritas", spec["label"], observed, health="ok", reason=None,
                  endpoint=f"127.0.0.1:{parts.port}", model_id=model_id, latency_ms=_ms(result[2]))


def _mac_absent(observed):
    return [
        _blank("mac:lmstudio", "mac", "lmstudio", "LM Studio", observed, health="not_measured", reason="not_observed",
               endpoint="127.0.0.1:1234"),
        _blank("mac:veritas", "mac", "veritas", "Veritas reviewer", observed, health="not_measured",
               reason="not_observed"),
    ]


def collect_map(host, *, fetch=None, rates=None, now=None, targets=None, veritas_url=None, tunnel=None, vram=None,
                lmstudio=None, remote_config=None, remote_probes=None):
    """The same node ids on the Mac and the PC. A machine only fills what it can probe."""
    if host not in ("mac", "pc"):
        raise ValueError(host)
    if fetch is None:
        deadline = time.monotonic() + .8
        def fetch(url):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError("timeout")
            return loopback_get(url, timeout=min(.4, remaining))
    rates = rates or RateMemory()
    now = time.time() if now is None else now
    observed = _iso(now)
    pc_targets = (targets or direct_targets()) if host == "pc" else (targets if targets is not None else closed_targets())
    registry_enabled = host == "mac" and remote_config is not None
    if registry_enabled and remote_machines.replaces_legacy_pc(remote_config):
        pc_targets = []
    remote_nodes = ((remote_probes.snapshot(remote_config, now=now) if remote_probes is not None
                     else remote_machines.background_snapshot(remote_config, now=now))
                    if host == "mac" else None)
    # Each lane keeps its serial parsing/fallbacks, but independent lanes share
    # the deadline concurrently so a slow successful lane cannot starve Qwen.
    with ThreadPoolExecutor(max_workers=len(PC_LANES) + 2) as pool:
        if host == "mac":
            studio = pool.submit(_read_lmstudio, fetch, observed) if lmstudio is None else None
            veritas = pool.submit(_read_veritas, fetch, observed,
                                  os.environ.get("VERITAS_REVIEWER_URL") if veritas_url is None else veritas_url)
        unconfigured = host == "mac" and isinstance(tunnel, dict) and tunnel.get("reason") == "host_unconfigured"
        pc_reads = [pool.submit(_read_pc, item, fetch, rates, now, observed, unconfigured=unconfigured)
                    for item in pc_targets]
        mac_children = (_mac_absent(observed) if host == "pc" else
                        [studio.result() if studio is not None else lmstudio, veritas.result()])
        pc_children = [read.result() for read in pc_reads]
    if host == "pc":
        tunnel = None
    nodes = [machine_node("mac", "Mac", mac_children, observed_at=observed, vram=vram if host == "mac" else None),
             *mac_children,
             machine_node("pc", "PC", pc_children, observed_at=observed, vram=vram if host == "pc" else None),
             *pc_children]
    nodes.extend(client_nodes(nodes))
    document = {"schema": MAP_SCHEMA, "observedAt": observed, "tunnel": tunnel, "nodes": nodes, "edges": edges_for(nodes)}
    return remote_machines.extend_map(document, remote_config, remote_nodes) if registry_enabled else document


def _snapshot_lmstudio(snapshot):
    """Reuse telemetry's inventory and verified endpoint; do not probe a second listener."""
    sampled = snapshot.get("fullSampledAt", snapshot.get("sampledAt"))
    observed = _iso(sampled) if _finite(sampled) else None
    node = _blank("mac:lmstudio", "mac", "lmstudio", "LM Studio", observed,
                  health="not_measured", reason="inventory_unverified")
    sources = [source for source in snapshot.get("sources", []) if source.get("id") == "lmstudio-api"]
    if len(sources) != 1:
        return node
    source = sources[0]
    owner = source.get("verifiedListener") or {}
    endpoint = source.get("endpoint")
    if (source.get("state") != "live" or source.get("route") != "direct"
            or source.get("modelsComplete") is not True or owner.get("kind") != "lmstudio"
            or type(owner.get("pid")) is not int or owner["pid"] <= 0 or not owner.get("executable")
            or not isinstance(endpoint, str) or not re.fullmatch(r"127\.0\.0\.1:[1-9][0-9]{0,4}", endpoint)
            or int(endpoint.rsplit(":", 1)[1]) > 65535):
        return node
    node["endpoint"] = "http://" + endpoint
    rows = [row for row in snapshot.get("models", []) if row.get("host") == "mac"]
    unknown = any(type(row.get("loaded")) is not bool for row in rows)
    rows = [row for row in rows if type(row.get("loaded")) is bool]
    if (not _finite(sampled) or not -2 <= time.time() - sampled <= 3
            or any(not _finite(row.get("ageSeconds"))
                   or not 0 <= row["ageSeconds"] <= 3 for row in rows)):
        return node
    loaded = {row.get("modelKey") or row["id"]: row for row in rows if row.get("loaded") is True}
    if unknown and not loaded:
        node["reason"] = "load_state_unreported"
        return node
    node.update(health="ok" if loaded else "degraded", reason=None if loaded else "model_not_loaded")
    if len(loaded) == 1:
        model, row = next(iter(loaded.items()))
        node.update(modelId=model, contextLimit=_whole(row.get("context")))
    elif loaded:
        node["reason"] = "multiple_loaded"
    return node


def attach(snapshot, host, *, previous=None, **kwargs):
    """Add the map and copy measured tok/s onto the machine rows. A probe failure does not drop the sample."""
    if not isinstance(snapshot, dict):
        return None
    try:
        if host == "mac":
            kwargs["lmstudio"] = _snapshot_lmstudio(snapshot)
        document = collect_map(host, **kwargs)
    except Exception as error:
        # Keep historical observations and their original times. A diagnostic is
        # a newly observed failure, not a successful replacement measurement.
        document = (copy.deepcopy(previous) if isinstance(previous, dict) and previous.get("schema") == MAP_SCHEMA
                    else {"schema": MAP_SCHEMA, "nodes": [], "edges": []})
        last_success = None
        try:
            stamp = dt.datetime.fromisoformat(document.get("observedAt", "").replace("Z", "+00:00"))
            last_success = stamp.timestamp() if stamp.tzinfo is not None else None
        except (ValueError, TypeError, AttributeError, OverflowError):
            pass
        diagnostic = collector_diagnostic("inference", error, now=kwargs.get("now"), last_success=last_success)
        document.update(error="collector_failed", collectorDiagnostics=[diagnostic])
        snapshot.setdefault("collectorDiagnostics", []).append(diagnostic)
        config = kwargs.get("remote_config")
        if not document["nodes"] and host == "mac" and config is not None:
            try:
                nodes = remote_machines.unavailable_nodes(config, now=kwargs.get("now"))
                remote_machines.extend_map(document, config, nodes)
            except (ValueError, TypeError, KeyError, AttributeError):
                pass  # Invalid configuration cannot replace a legacy machine.
    snapshot["inference"] = document
    remote_machines.project_snapshot(snapshot)
    if document.get("nodes") and not document.get("error"):
        apply_rates(snapshot.get("machines"), document["nodes"])
    return document


def vram_from_reading(used_bytes, total_bytes):
    if type(used_bytes) is not int or type(total_bytes) is not int or used_bytes < 0 or total_bytes < used_bytes:
        return None
    return {"usedMiB": used_bytes // (1024 * 1024), "totalMiB": total_bytes // (1024 * 1024)}


def vram_from_gpus(gpus):
    """One complete GPU reading. Two devices are not added into a single figure."""
    if not isinstance(gpus, list) or len(gpus) != 1 or not isinstance(gpus[0], dict):
        return None
    used, total = gpus[0].get("memoryUsedMiB"), gpus[0].get("memoryTotalMiB")
    if type(used) is not int or type(total) is not int or used < 0 or total < used:
        return None
    return {"usedMiB": used, "totalMiB": total}
