"""The read-only fleet contract. Endpoint identity is not host or hardware health.

Joins use exact declared IDs and the owner's explicit legacy-PC replacement.
The replacement joins an unambiguous legacy Windows report to its configured
machine. No function in this module reads a file or probes a host.
"""
from __future__ import annotations

import copy
import datetime as dt
import math
import time

# Only explicit probe failures establish unavailable endpoint evidence. New or
# unrecognised producer reasons fail closed to unknown.
ENDPOINT_FAILURES = frozenset({"refused", "timeout", "unavailable", "upstream_error", "tunnel_down"})
ANSWERED_WITHOUT_IDENTITY = frozenset({
    "load_state_unreported", "identity_unknown", "multiple_loaded", "model_unreported",
    "model_id_too_long", "models_unreadable", "model_not_loaded", "no_models",
})


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _iso(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _clock(row, now, default):
    observed = _stamp(row.get("observedAt"))
    window = row.get("freshForSeconds")
    if not _number(window) or window <= 0:
        interval = row.get("probeIntervalSeconds")
        window = 2 * interval if _number(interval) and interval > 0 else default
    expires = _stamp(row.get("expiresAt"))
    invalid_expiry = expires is not None and observed is not None and expires < observed
    if expires is None and observed is not None:
        expires = observed + window
    age = now - observed if observed is not None else None
    return {"observedAt": _iso(observed) if observed is not None else None,
            "expiresAt": _iso(expires) if expires is not None else None,
            "ageSeconds": max(0.0, age) if age is not None else None}, (
                "invalid_expiry" if invalid_expiry else "unknown" if age is None or age < -2 else
                "stale" if expires is not None and now > expires else "fresh")


def _fact(identity, scope, label, source, row, now, default, parent=None):
    clock, freshness = _clock(row, now, default)
    return {"id": identity, "scope": scope, "label": label or identity,
            "parentId": parent, "source": source, **clock,
            "completeness": "unknown", "reason": row.get("reason") or row.get("reasonCode"),
            "state": "unknown", "evidence": copy.deepcopy(row)}, freshness



def _identity_only(row):
    return row.get("observationSource") == "registry" or row.get("inventoryScope") == "configured-lanes"


def _machine_state(fact, row, freshness):
    """Apply the same producer-clock rules to host and extra-source facts."""
    if not row or _identity_only(row):
        # Registry rows carry endpoint reachability and identity only; even a
        # legacy producer's "up" on such a row is not a host report.
        fact.update(reason="host_not_measured")
    elif freshness == "invalid_expiry":
        fact.update(reason="invalid_expiry")
    elif freshness == "unknown":
        fact.update(reason="clock_skew" if fact["observedAt"] else "not_observed")
    elif freshness == "stale" or row.get("reasonCode") == "stale":
        fact.update(state="stale", reason="stale")
    elif row.get("state") in ("up", "down"):
        fact.update(state="reported", completeness="complete" if row.get("modelsComplete") is True else "partial",
                    reason=row.get("reasonCode") or "reported")
    else:
        fact.update(reason=row.get("reasonCode") or "host_not_measured")


def _report_metadata(row, sources):
    """Resolve only exact producer identities; a label supplies no attribution."""
    identity = row.get("id") or row.get("host")
    related = [source for source in sources
               if (source.get("node") == identity or
                   source.get("host") and source.get("host") == row.get("host") or
                   source.get("id") and source.get("id") == row.get("sourceId"))]
    carriers = [row] + ([row["source"]] if isinstance(row.get("source"), dict) else [])
    # Explicit report attribution wins over source hints. Conflicting source
    # attribution blocks the legacy fallback rather than choosing one by order.
    def targets(items):
        return {item[key] for item in items for key in ("attributedMachineId", "registryMachineId", "machineId")
                if isinstance(item.get(key), str) and item[key]}
    attributed = targets(carriers) or targets(related)
    declared = next(iter(attributed)) if len(attributed) == 1 else None
    marked = any(item.get("legacy") is True or item.get("id") == "legacy-pc"
                 or item.get("sourceId") == "legacy-pc" or item.get("source") == "legacy-pc"
                 for item in carriers + related)
    return declared, bool(attributed), marked


def _coverage(lanes):
    counts = {"lanes": len(lanes),
            "answering": sum(item["state"] == "endpoint answering" for item in lanes),
            "unavailable": sum(item["state"] == "endpoint unavailable" for item in lanes),
            "stale": sum(item["state"] == "stale" for item in lanes),
            "unknown": sum(item["state"] == "unknown" for item in lanes)}
    if not counts["answering"] and (not lanes or counts["unknown"] or counts["stale"]):
        counts["answering"] = None
    return counts


def build_fleet_state(snapshot, inference=None, now=None):
    """Join declared machines, lane facts, and exact-ID/unmatched host reports.

    ``now`` is epoch seconds, making expiry deterministic in fixture tests. The
    returned facts retain their producers' timestamps, even when a new enclosing
    snapshot arrives. Missing timestamps do not borrow the snapshot's clock.
    Counts of loaded/busy models require fresh complete evidence from every
    machine. Endpoint coverage never supplies model activity or host health.
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    document = inference if isinstance(inference, dict) else snapshot.get("inference")
    document = document if isinstance(document, dict) else {}
    now = time.time() if now is None else now
    if not _number(now):
        raise ValueError("now must be finite epoch seconds")
    rows = [row for row in (snapshot.get("machines") or []) if isinstance(row, dict)]
    nodes = [node for node in (document.get("nodes") or []) if isinstance(node, dict)]
    roots = {node["id"]: node for node in nodes
             if isinstance(node.get("id"), str) and node.get("role") == "machine"}
    # Include a root even when a producer emitted only its configured child.
    for node in nodes:
        parent = node.get("machine")
        if node.get("role") not in ("machine", "client") and isinstance(parent, str):
            roots.setdefault(parent, {"id": parent, "label": parent, "role": "machine"})
    local_rows = [row for row in rows if row.get("trust") == "local"
                  or row.get("role") == "this-machine" or row.get("source") == "local"]
    default_local = "pc" if snapshot.get("host") in ("pc", "windows") or snapshot.get("platform") == "windows" else "mac"
    local_id = str(local_rows[0].get("id") or local_rows[0].get("host") or default_local) if local_rows else default_local
    # This observer remains visible when no endpoint or report has answered.
    roots.setdefault(local_id, {"id": local_id, "label": "This Mac" if local_id == "mac" else "This machine"})
    by_id, peer_reports = {}, {}
    replacement_ids = [identity for identity, root in roots.items()
                       if root.get("replaces") == "legacy-pc"]
    replacement_id = replacement_ids[0] if len(replacement_ids) == 1 else None
    sources = [source for source in (snapshot.get("sources") or []) if isinstance(source, dict)]
    metadata = {id(row): _report_metadata(row, sources) for row in rows}
    eligible = [row for row in rows
                if row.get("platform") == "windows" and (row.get("id") or row.get("host")) not in roots
                and row.get("observationSource") != "registry" and not metadata[id(row)][1]]
    marked = [row for row in eligible if metadata[id(row)][2]]
    candidates = marked or eligible
    candidate_ids = {row.get("id") or row.get("host") for row in candidates}
    legacy_peer_id = next(iter(candidate_ids)) if len(candidate_ids) == 1 else None
    for row in rows:
        identity = row.get("id") or row.get("host")
        if not isinstance(identity, str) or not identity:
            continue
        report_id = identity
        # The registry's replacement declaration names the legacy Windows peer
        # source. Labels never establish identity; an explicit per-report ID
        # takes precedence over the platform-level legacy declaration.
        attributed, has_attribution, _marked = metadata[id(row)]
        if attributed in roots:
            identity = attributed
        elif (replacement_id and not has_attribution and identity not in roots
              and identity == legacy_peer_id):
            identity = replacement_id
        if row.get("observationSource") == "registry":
            roots.setdefault(identity, {"id": identity, "label": row.get("label"),
                                       "observationSource": "registry"})
            continue
        # A never-observed synthetic placeholder supplies no peer evidence.
        if (replacement_id and row.get("platform") == "windows" and report_id != local_id
                and report_id in ("pc", "windows") and row.get("observedAt") is None
                and row.get("trust", "unverified") == "unverified"
                and row.get("reasonCode", "not_observed") == "not_observed"):
            continue
        by_id.setdefault(identity, row)
        if identity != report_id:
            peer_reports.setdefault(identity, []).append((report_id, row))
        roots.setdefault(identity, {"id": identity, "label": row.get("label") or row.get("host") or identity})
    facts, lanes = [], []
    for node in nodes:
        if node.get("role") in ("machine", "client") or not isinstance(node.get("id"), str):
            continue
        if node.get("reason") == "not_configured":
            continue  # An optional, unconfigured service is not a fleet lane.
        identity, parent = node["id"], node.get("machine")
        if any(item["id"] == identity for item in lanes):
            continue
        source = "registry /v1/models" if node.get("observationSource") == "registry" else "inference endpoint"
        fact, freshness = _fact(identity, "lane", node.get("label"), source, node, now,
                                30.0 if parent == "pc" else 3.0, parent)
        model = node.get("modelId")
        if freshness == "stale":
            fact.update(state="stale", reason="stale", completeness="unknown")
        elif freshness in ("unknown", "invalid_expiry"):
            fact.update(reason="invalid_expiry" if freshness == "invalid_expiry" else
                        "clock_skew" if fact["observedAt"] else node.get("reason") or "not_observed")
        elif node.get("health") in ("ok", "degraded") and isinstance(model, str) and model:
            fact.update(state="endpoint answering", completeness="complete", reason=node.get("reason"))
        elif node.get("reason") == "stale":
            fact.update(state="stale", reason="stale")
        elif node.get("reason") in ENDPOINT_FAILURES:
            fact.update(state="endpoint unavailable", completeness="complete")
        else:
            fact.update(reason=node.get("reason") or "identity_not_measured")
            if node.get("reason") in ANSWERED_WITHOUT_IDENTITY:
                fact["evidence"]["endpointAnswered"] = True
        fact["evidence"]["healthScope"] = "endpoint identity only; host, hardware and inference activity unknown"
        lanes.append(fact)
    for identity, root in roots.items():
        row = by_id.get(identity)
        if row and identity == local_id:
            row = copy.deepcopy(row)
            # A publisher can invalidate a source after constructing its host
            # summary. Retained summary counts cannot override that later loss
            # of evidence. Alias counts intentionally need not equal row counts.
            local_hosts = {local_id, snapshot.get("host"), row.get("host")}
            if row.get("platform") in ("mac", "macos"):
                local_hosts.add("mac")
            if row.get("platform") == "windows":
                local_hosts.update(("windows", "pc"))
            raw_models = snapshot.get("models")
            local_models = [model for model in raw_models
                            if isinstance(model, dict) and model.get("host") in local_hosts] if isinstance(raw_models, list) else None
            contradiction = (snapshot.get("inventoryComplete") is False
                             or local_models is not None and (
                                 any(type(model.get("loaded")) is not bool or model.get("inventoryComplete") is False
                                     for model in local_models)
                                 or not local_models and type(row.get("loadedCount")) is int
                                 and row["loadedCount"] > 0))
            if contradiction:
                row.update(modelsComplete=False, loadedCount=None, busyCount=None)
            if snapshot.get("activityKnown") is False:
                row["busyCount"] = None
        children = [lane for lane in lanes if lane["parentId"] == identity]
        source = ("local report" if identity == local_id else "peer report") if row else "configured membership"
        label = (root.get("label") if root.get("observationSource") == "registry" else None) or (row or {}).get("label") or root.get("label")
        fact, freshness = _fact(identity, "machine", label,
                                source, row or {}, now,
                                3.0 if identity == local_id else 30.0)
        _machine_state(fact, row, freshness)
        fact["evidence"].update(endpointCounts=_coverage(children),
                                configured=copy.deepcopy(root),
                                unmatchedPeer=bool(row and identity != local_id
                                                   and root.get("observationSource") != "registry"
                                                   and not children))
        if peer_reports.get(identity):
            reports = []
            for report_id, report in peer_reports[identity]:
                evidence, report_freshness = _fact(report_id, "machine", report.get("label") or report.get("host"),
                                                  "peer report", report, now, 30.0)
                _machine_state(evidence, report, report_freshness)
                reports.append(evidence)
            fact["evidence"]["peerReports"] = reports
        facts.append(fact)
    facts.extend(lanes)
    counts = _coverage(lanes)
    registry = None
    status = document.get("remoteMachines")
    if isinstance(status, dict):
        registry, freshness = _fact("remote-machines", "registry", "Remote machine registry",
                                    "registry configuration", {**status, "observedAt": document.get("observedAt")},
                                    now, 3.0)
        registry.update(reason="registry_invalid" if status.get("state") == "invalid" else
                        "registry_configured" if status.get("state") == "configured" and freshness == "fresh" else
                        "registry_unknown")
        if registry["reason"] == "registry_configured":
            registry.update(state="reported", completeness="complete")
    registry_partial = registry is not None and registry["completeness"] != "complete"
    if registry_partial and not counts["answering"]:
        counts["answering"] = None
    counts["machines"] = len(roots)
    machine_facts = [fact for fact in facts if fact["scope"] == "machine"]
    for output, field in (("loaded", "loadedCount"), ("busy", "busyCount")):
        measured = all(fact["state"] == "reported" and fact["completeness"] == "complete"
                       and fact["evidence"].get("state") == "up"
                       and type(fact["evidence"].get(field)) is int and fact["evidence"][field] >= 0
                       for fact in machine_facts)
        counts[output] = sum(fact["evidence"][field] for fact in machine_facts) if measured and not registry_partial else None
    summary = ("Answering count unknown; {} lanes configured".format(counts["lanes"])
               if lanes and counts["answering"] is None else
               "{} of {} lanes answering".format(counts["answering"], counts["lanes"])
               if lanes else "Endpoint coverage unknown; no lane evidence")
    for state, key in (("endpoint unavailable", "unavailable"), ("stale", "stale"), ("unknown", "unknown")):
        affected = [fact for fact in lanes if fact["state"] == state]
        if affected:
            names = ", ".join("{} ({}){}{}".format(fact["label"], fact["id"],
                              ", {} s ago".format(math.floor(fact["ageSeconds"] + .5))
                              if state == "stale" and fact["ageSeconds"] is not None else "",
                              ", answered, identity unknown" if state == "unknown"
                              and fact["evidence"].get("endpointAnswered") is True else "")
                              for fact in affected)
            summary += "; {} {}: {}".format(len(affected), key, names)
    if registry_partial:
        summary = "Partial coverage; {}; ".format(registry["reason"].replace("_", " ")) + summary
    complete = bool(lanes) and not counts["unknown"] and not counts["stale"] and not registry_partial
    return {"schemaVersion": 1, "observedAt": _iso(now), "facts": facts, "counts": counts,
            **({"registry": registry} if registry is not None else {}),
            "completeness": "complete" if complete else "partial",
            "state": "coverage" if lanes else "unknown", "summary": summary}
