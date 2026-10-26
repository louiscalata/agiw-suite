"""Read loopback inference endpoints into the map in inference_stats.

PC llama.cpp lanes use /metrics, /slots and /props. When those are absent, a
timed GET /health is the observation. The router is GET /v1/models. LM Studio
on this Mac is GET /api/v0/models, then /v1/models. Veritas is GET of
VERITAS_REVIEWER_URL. No request asks a model to generate. Slot prompts are
not kept.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import time
from urllib.parse import urlsplit
import http.client

from inference_stats import MAP_SCHEMA, apply_rates, client_nodes, edges_for, machine_node, make_node

PC_LANES = (
    {"id": "pc:router", "role": "router", "label": "Router", "port": 1234},
    {"id": "pc:gpt-oss-20b", "role": "llama", "label": "gpt-oss-20b", "port": 1245,
     "expectedModel": "gpt-oss-20b"},
    {"id": "pc:qwen3.8-27b", "role": "llama", "label": "qwen3.8-27b", "port": 1236,
     "expectedModel": "qwen3.8-27b"},
)
_BY_ID = {lane["id"]: lane for lane in PC_LANES}
_METRIC = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(\S+)")
_COUNTERS = ("prompt_tokens_total", "tokens_predicted_total", "prompt_seconds_total", "predicted_seconds_total")
_GAUGES = ("requests_deferred", "requests_processing", "n_busy_slots")
_WANTED = set(_COUNTERS + _GAUGES)
_LMSTUDIO = "http://127.0.0.1:1234"


class ProbeError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


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
        if not elapsed or elapsed <= 0:
            return None, None
        return (_speed(prev, counters, elapsed, "prompt_tokens_total", "prompt_seconds_total"),
                _speed(prev, counters, elapsed, "tokens_predicted_total", "predicted_seconds_total"))


def _speed(prev, counters, elapsed, tokens, seconds):
    before, after = prev.get(tokens), counters.get(tokens)
    if before is None or after is None or after < before:
        return None
    spent_before, spent_after = prev.get(seconds), counters.get(seconds)
    if spent_before is not None and spent_after is not None and spent_after > spent_before:
        return (after - before) / (spent_after - spent_before)
    return (after - before) / elapsed


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


def loopback_get(url, timeout=0.4, limit=1024 * 1024):
    """GET one http://127.0.0.1 URL. Raises ProbeError. Does not follow a redirect."""
    parts = urlsplit(url)
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ProbeError("refused")
    started = time.monotonic()
    connection = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=timeout)
    try:
        connection.request("GET", parts.path or "/", headers={"Accept": "application/json, text/plain"})
        response = connection.getresponse()
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ProbeError("oversize")
        return response.status, body.decode("utf-8", "replace"), (time.monotonic() - started) * 1000
    except ProbeError:
        raise
    except TimeoutError:
        raise ProbeError("timeout")
    except ConnectionRefusedError:
        raise ProbeError("refused")
    except OSError:
        raise ProbeError("unavailable")
    finally:
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
        if not isinstance(item, dict) or item.get("state") != "loaded":
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


def _read_pc(target, fetch, rates, now, observed):
    spec = _BY_ID.get(target.get("id"))
    endpoint = target.get("service")
    if spec is None:
        return _blank(str(target.get("id") or "pc:unknown"), "pc", "llama", "Unknown lane", observed,
                      health="not_measured", reason="unknown_lane", endpoint=endpoint)
    if not target.get("base"):
        return _blank(spec["id"], "pc", spec["role"], spec["label"], observed,
                      health="not_measured", reason="tunnel_down", endpoint=endpoint)
    if spec["role"] == "router":
        return _read_router(spec, target["base"], fetch, observed, endpoint)
    return _read_llama(spec, target["base"], fetch, rates, now, observed, endpoint)


def _read_lmstudio(fetch, observed):
    spec = {"id": "mac:lmstudio", "machine": "mac", "role": "lmstudio", "label": "LM Studio",
            "expectedModel": "gpt-oss-20b"}
    failure, result = _request(fetch, _LMSTUDIO + "/api/v0/models")
    if failure in ("refused", "timeout"):
        return _transport_failure(spec, "127.0.0.1:1234", observed, failure)
    loaded = _lmstudio_loaded(result[1]) if result and result[0] == 200 else None
    if loaded is None:
        failure, result = _request(fetch, _LMSTUDIO + "/v1/models")
        if failure:
            return _transport_failure(spec, "127.0.0.1:1234", observed, failure)
        if result and result[0] == 200:
            return _blank(spec["id"], "mac", "lmstudio", spec["label"], observed, health="ok",
                          reason="load_state_unreported", endpoint="127.0.0.1:1234", latency_ms=_ms(result[2]))
        return _blank(spec["id"], "mac", "lmstudio", spec["label"], observed, health="down",
                      reason="upstream_error", endpoint="127.0.0.1:1234")
    latency = _ms(result[2])
    if len(loaded) == 1:
        model_id, health, reason = _model_fields(loaded[0]["id"], spec["expectedModel"])
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


def collect_map(host, *, fetch=None, rates=None, now=None, targets=None, veritas_url=None, tunnel=None, vram=None):
    """The same node ids on the Mac and the PC. A machine only fills what it can probe."""
    if host not in ("mac", "pc"):
        raise ValueError(host)
    fetch = fetch or loopback_get
    rates = rates or RateMemory()
    now = time.time() if now is None else now
    observed = _iso(now)
    if host == "pc":
        mac_children = _mac_absent(observed)
        pc_children = [_read_pc(item, fetch, rates, now, observed) for item in (targets or direct_targets())]
        tunnel = None
    else:
        mac_children = [_read_lmstudio(fetch, observed),
                        _read_veritas(fetch, observed, os.environ.get("VERITAS_REVIEWER_URL") if veritas_url is None else veritas_url)]
        pc_children = [_read_pc(item, fetch, rates, now, observed) for item in (targets if targets is not None else closed_targets())]
    nodes = [machine_node("mac", "Mac", mac_children, observed_at=observed, vram=vram if host == "mac" else None),
             *mac_children,
             machine_node("pc", "PC", pc_children, observed_at=observed, vram=vram if host == "pc" else None),
             *pc_children]
    nodes.extend(client_nodes(nodes))
    return {"schema": MAP_SCHEMA, "observedAt": observed, "tunnel": tunnel, "nodes": nodes, "edges": edges_for(nodes)}


def attach(snapshot, host, **kwargs):
    """Add the map and copy measured tok/s onto the machine rows. A probe failure does not drop the sample."""
    if not isinstance(snapshot, dict):
        return None
    try:
        document = collect_map(host, **kwargs)
    except Exception:
        document = {"schema": MAP_SCHEMA, "nodes": [], "edges": [], "error": "collector_failed"}
    snapshot["inference"] = document
    if document.get("nodes"):
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
