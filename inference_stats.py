"""Per-model inference readings. Null measurements mean not measured.

Alerts, history and spend are later. They key off ``id`` and these fields;
this module does not store them.
"""
from __future__ import annotations

import math
import datetime as dt

MAP_SCHEMA = "agiw.inference-map.v1"
# Up to 32 configured remote roots + 64 lanes, with room for local nodes
# and their caller observations. Keep the CLI export bounded as the map grows.
MAX_MAP_NODES = 128
MAX_MAP_EDGES = 256
HEALTH = frozenset({"ok", "degraded", "down", "not_measured"})
ROLES = frozenset({"machine", "lmstudio", "veritas", "router", "llama", "client"})
_RATES = ("promptTokensPerSecond", "generationTokensPerSecond", "latencyMs")
_COUNTS = ("queueDepth", "slotsBusy", "slotsTotal", "contextUsed", "contextLimit",
           "vramUsedMiB", "vramTotalMiB")
_TEXT = ("id", "machine", "role", "label", "endpoint", "modelId", "upstream", "reason", "detail")


def expire_map(document, now):
    """Age a copied map at the read boundary; never renew an observation's timestamp."""
    if not isinstance(document, dict):
        return
    try:
        stamp = dt.datetime.fromisoformat(document.get("observedAt", "").replace("Z", "+00:00"))
        age = now - stamp.timestamp() if stamp.tzinfo is not None else math.inf
    except (TypeError, ValueError, AttributeError, OverflowError):
        age = math.inf
    if document.get("tunnel") and not -2 <= age <= 30:
        document["tunnel"] = {"state": "not_measured", "reason": "stale",
                              "detail": "PC tunnel observation stale."}
    expired = set()
    for node in document.get("nodes") or []:
        registry = node.get("observationSource") == "registry"
        stamp = node.get("observedAt") if registry else (node.get("observedAt") or document.get("observedAt"))
        try:
            observed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            age = now - observed.timestamp() if observed.tzinfo is not None else math.inf
        except (TypeError, ValueError, AttributeError, OverflowError):
            age = math.inf
        if registry:
            node["ageSeconds"] = max(0.0, age) if math.isfinite(age) else None
            interval = node.get("probeIntervalSeconds", 3)
            interval = interval if _finite(interval) and interval > 0 else 3
            max_age = 2 * interval
            # Pending probes have no completion time. Keep their explicit unknown
            # reason without borrowing a newer snapshot timestamp.
            if (node.get("observedAt") is None and node.get("health") == "not_measured"
                    and node.get("modelId") is None):
                continue
        else:
            max_age = 30 if node.get("machine") == "pc" else 3
        if not -2 <= age <= max_age:
            expired.add(node.get("id"))
            node.update(health="not_measured", reason="stale")
            for field in _RATES + _COUNTS + ("modelId", "upstream", "detail", "models", "callers"):
                node[field] = None
    for node in document.get("nodes") or []:
        if node.get("role") != "machine":
            continue
        children = [child for child in document["nodes"]
                    if child.get("machine") == node.get("machine")
                    and child.get("role") not in ("machine", "client")]
        rollup = machine_node(node["machine"], node["label"], children)
        if node.get("inventoryScope") == "configured-lanes" and any(child.get("health") != "ok" for child in children):
            rollup.update(health="not_measured", reason="lane_unknown")
        for field in ("health", "reason", "promptTokensPerSecond", "generationTokensPerSecond"):
            node[field] = rollup[field]
    # Upstream and caller links are observations too; retain only structural links
    # when either end has expired, without changing the node layout.
    if "edges" in document:
        document["edges"] = [edge for edge in document["edges"]
                             if edge.get("kind") not in ("upstream", "caller")
                             or not expired.intersection((edge.get("from"), edge.get("to")))]


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _text(value, limit):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ValueError("text")
    return value


def make_node(*, id, machine, role, label, health, endpoint=None, model_id=None,
              upstream=None, reason=None, observed_at=None, prompt_tokens_per_second=None,
              generation_tokens_per_second=None, latency_ms=None, queue_depth=None,
              slots_busy=None, slots_total=None, context_used=None, context_limit=None,
              vram_used_mib=None, vram_total_mib=None, detail=None, models=None, callers=None):
    """One map node. ``model_id`` is the id the server reported, unchanged."""
    node = {
        "id": _text(id, 80),
        "machine": _text(machine, 32),
        "role": role if role in ROLES else None,
        "label": _text(label, 80),
        "endpoint": _text(endpoint, 80) if endpoint is not None else None,
        "modelId": _text(model_id, 1024) if model_id is not None else None,
        "upstream": _text(upstream, 200) if upstream is not None else None,
        "health": health if health in HEALTH else None,
        "reason": _text(reason, 64) if reason is not None else None,
        "observedAt": _text(observed_at, 40) if observed_at is not None else None,
        "promptTokensPerSecond": prompt_tokens_per_second,
        "generationTokensPerSecond": generation_tokens_per_second,
        "latencyMs": latency_ms,
        "queueDepth": queue_depth,
        "slotsBusy": slots_busy,
        "slotsTotal": slots_total,
        "contextUsed": context_used,
        "contextLimit": context_limit,
        "vramUsedMiB": vram_used_mib,
        "vramTotalMiB": vram_total_mib,
        "detail": _text(detail, 240) if detail is not None else None,
    }
    if None in (node["id"], node["machine"], node["role"], node["label"], node["health"]):
        raise ValueError("node identity")
    for key in _RATES:
        value = node[key]
        if value is not None and (not _finite(value) or value < 0):
            raise ValueError(key)
    for key in _COUNTS:
        value = node[key]
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(key)
    if models is not None:
        node["models"] = _models(models)
    if callers is not None:
        node["callers"] = _callers(callers)
    return node


def _models(models):
    if not isinstance(models, list) or len(models) > 16:
        raise ValueError("models")
    rows = []
    for item in models:
        if not isinstance(item, dict):
            raise ValueError("models")
        rows.append({"id": _text(item.get("id"), 1024),
                     "upstream": _text(item["upstream"], 200) if item.get("upstream") is not None else None})
        if rows[-1]["id"] is None:
            raise ValueError("models")
    return rows


def _callers(callers):
    if not isinstance(callers, list) or len(callers) > 8:
        raise ValueError("callers")
    rows = []
    for item in callers:
        if not isinstance(item, dict):
            raise ValueError("callers")
        rows.append({"id": _text(item.get("id"), 80),
                     "label": _text(item["label"], 80) if item.get("label") is not None else None})
        if rows[-1]["id"] is None:
            raise ValueError("callers")
    return rows


def _url_port(url):
    if not isinstance(url, str) or "://" not in url:
        return None
    host = url.split("://", 1)[1].split("/", 1)[0]
    if host.endswith("]") or ":" not in host:
        return None
    port = host.rsplit(":", 1)[1]
    return int(port) if port.isdecimal() and 1 <= int(port) <= 65535 else None


def client_nodes(nodes):
    """A caller attached to a node becomes its own map node. Absent callers add nothing."""
    extra = []
    for node in nodes:
        for caller in node.get("callers") or []:
            label = caller.get("label") or caller["id"]
            extra.append(make_node(id=f"{node['id']}:caller:{caller['id']}", machine=node["machine"],
                                   role="client", label=label, health="ok", endpoint=node.get("endpoint"),
                                   reason="reported"))
    return extra


def edges_for(nodes):
    by_id = {node["id"]: node for node in nodes}
    edges = []
    for node in nodes:
        if node["role"] == "machine":
            continue
        if node["role"] == "client":
            owner = node["id"].rsplit(":caller:", 1)[0]
            if owner in by_id:
                edges.append({"from": owner, "to": node["id"], "kind": "caller"})
            continue
        parent = node["machine"]
        if parent in by_id:
            edges.append({"from": parent, "to": node["id"], "kind": "hosts"})
    router = by_id.get("pc:router")
    for model in (router or {}).get("models") or []:
        port = _url_port(model.get("upstream"))
        if port is None:
            continue
        for node in nodes:
            endpoint = node.get("upstream") or node.get("endpoint")
            if node["role"] == "llama" and isinstance(endpoint, str) and endpoint.endswith(f":{port}"):
                edges.append({"from": "pc:router", "to": node["id"], "kind": "upstream", "modelId": model["id"]})
    return edges


def _sum(nodes, machine, key):
    total, unmeasured_busy = None, False
    for node in nodes:
        if node.get("machine") != machine or node.get("role") in ("machine", "client"):
            continue
        value = node.get(key)
        if _finite(value) and value >= 0:
            total = value if total is None else total + value
        elif type(node.get("slotsBusy")) is int and node["slotsBusy"] > 0:
            unmeasured_busy = True  # a lane known to be working has no rate yet: the sum would be a lower bound
        elif node.get("role") == "llama" and node.get("slotsBusy") is None and node.get("reason") == "timeout":
            unmeasured_busy = True  # a lane that did not answer in time may be working: its rate is unknown
    return None if unmeasured_busy else total


def machine_node(machine, label, children, *, observed_at=None, vram=None, reason=None):
    healths = [node["health"] for node in children]
    # Additional machines describe only configured lanes. One answering lane
    # cannot establish health for another lane whose probe failed.
    if machine not in ("mac", "pc") and any(item == "not_measured" for item in healths):
        health = "not_measured"
        if reason is None:
            reason = "lanes_unknown"
    elif any(item == "down" for item in healths):
        health = "down"
    elif any(item == "degraded" for item in healths):
        health = "degraded"
    elif any(item == "ok" for item in healths):
        health = "ok"
    else:
        health = "not_measured"
    if reason is None and children and len({node.get("reason") for node in children}) == 1:
        reason = children[0].get("reason") if health != "ok" else None
    used = total = None
    if isinstance(vram, dict):
        used, total = vram.get("usedMiB"), vram.get("totalMiB")
        if type(used) is not int or type(total) is not int or used < 0 or total < used:
            used = total = None
    return make_node(id=machine, machine=machine, role="machine",
                     label=label, health=health, reason=reason, observed_at=observed_at,
                     prompt_tokens_per_second=_sum(children, machine, "promptTokensPerSecond"),
                     generation_tokens_per_second=_sum(children, machine, "generationTokensPerSecond"),
                     vram_used_mib=used, vram_total_mib=total)


def apply_rates(machines, nodes):
    """Fill the machine rows' tok/s from measured nodes. Unmeasured stays null, never zero."""
    if not isinstance(machines, list):
        return

    def assign(rows, machine):
        prompt = _sum(nodes, machine, "promptTokensPerSecond")
        generation = _sum(nodes, machine, "generationTokensPerSecond")
        for row in rows:
            # These rates are probed through the tunnel, not reported by the machine: they go only on a row that is
            # up, never on one that is unknown or never observed.
            if row.get("state") != "up":
                continue
            if prompt is not None:
                row["promptTokensPerSecond"] = prompt
            if generation is not None:
                row["generationTokensPerSecond"] = generation

    windows = [row for row in machines if isinstance(row, dict) and row.get("platform") == "windows"]
    macs = [row for row in machines if isinstance(row, dict) and row.get("platform") in ("mac", "macos")]
    local_windows = [row for row in windows if row.get("role") == "this-machine" or row.get("trust") == "local"
                     or row.get("source") == "local"]
    assign(local_windows or (windows if len(windows) == 1 else []), "pc")
    local_macs = [row for row in macs if row.get("host") == "mac" or row.get("trust") == "local"
                  or row.get("role") == "this-machine"]
    assign(local_macs or (macs if len(macs) == 1 else []), "mac")


def public_map(document):
    """A bounded copy for agiw-status. None when the document is not the map schema."""
    if not isinstance(document, dict) or document.get("schema") != MAP_SCHEMA:
        return None
    nodes = document.get("nodes")
    edges = document.get("edges")
    if (not isinstance(nodes, list) or len(nodes) > MAX_MAP_NODES
            or not isinstance(edges, list) or len(edges) > MAX_MAP_EDGES):
        return None
    try:
        copied = [make_node(id=node["id"], machine=node["machine"], role=node["role"], label=node["label"],
                            health=node["health"], endpoint=node.get("endpoint"), model_id=node.get("modelId"),
                            upstream=node.get("upstream"), reason=node.get("reason"),
                            observed_at=node.get("observedAt"),
                            prompt_tokens_per_second=node.get("promptTokensPerSecond"),
                            generation_tokens_per_second=node.get("generationTokensPerSecond"),
                            latency_ms=node.get("latencyMs"), queue_depth=node.get("queueDepth"),
                            slots_busy=node.get("slotsBusy"), slots_total=node.get("slotsTotal"),
                            context_used=node.get("contextUsed"), context_limit=node.get("contextLimit"),
                            vram_used_mib=node.get("vramUsedMiB"), vram_total_mib=node.get("vramTotalMiB"),
                            detail=node.get("detail"), models=node.get("models"), callers=node.get("callers"))
                  for node in nodes]
        for source, node in zip(nodes, copied):
            if source.get("observationSource") == "registry":
                interval = source.get("probeIntervalSeconds", 3)
                age = source.get("ageSeconds")
                if not _finite(interval) or interval <= 0 or (age is not None and (not _finite(age) or age < 0)):
                    raise ValueError("registry freshness")
                node.update(observationSource="registry", probeIntervalSeconds=interval, ageSeconds=age)
                if source.get("inventoryScope") == "configured-lanes":
                    node["inventoryScope"] = "configured-lanes"
            if "contextTokens" in source:
                value = source["contextTokens"]
                if type(value) is not int or value < 0:
                    raise ValueError("contextTokens")
                node["contextTokens"] = value
            if "thinking" in source:
                value = source["thinking"]
                if value not in ("on", "off", "unknown"):
                    raise ValueError("thinking")
                node["thinking"] = value
    except (KeyError, TypeError, ValueError):
        return None
    return {"schema": MAP_SCHEMA, "nodes": copied, "edges": edges_for(copied)}
