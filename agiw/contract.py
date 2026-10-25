"""Adapt authenticated peer-link snapshots to frozen machine-status/v1 rows.

The wheel deliberately ships no contracts/ directory. Keep this embedded table
in parity with contracts/machine-status/health-words.json (checked by tests).
Transport acceptance belongs to peer_link; health and freshness belong to the
frozen machine_state core. Neither connection time nor receipt time is evidence
of a new observation.
"""
from __future__ import annotations

import datetime as dt
import time

import machine_state
import peer_link as pl


HEALTH_WORDS = {
    "mac_local": {"ok": {"health": "ok"}},
    "windows_observer": {
        "ok": {"health": "ok"}, "degraded": {"health": "degraded"},
        "down": {"health": "down"}, "unknown": {"reason": "unavailable"},
    },
    "peer_feed": {
        "ok": {"health": "ok"}, "degraded": {"health": "degraded"},
        "down": {"health": "down"},
    },
    "linux_observer": {
        "up": {"health": "ok"}, "down": {"health": "down"},
        "unknown": {"reason": "unavailable"},
    },
    "cli_peer_link": {
        "ok": {"health": "ok"}, "degraded": {"health": "degraded"},
        "down": {"health": "down"}, "unknown": {"reason": "unavailable"},
    },
}
PLATFORMS = ("mac", "windows", "linux")


def _text(value, fallback):
    return value if isinstance(value, str) and value else fallback


def producer_table(producer):
    if producer == "agiw-linux-observer":
        return "linux_observer"
    # buildinfo.producer() appends a version and optional build provenance.
    if isinstance(producer, str) and producer.startswith("agiw-win-observer "):
        return "windows_observer"
    return "cli_peer_link"


def _platform(document):
    return document.get("platform") if document.get("kind") == "agiw-peer" else document.get("host")


def _accepted(document):
    """Recheck the control input; leave observation age to the core.

    The peer link has already authenticated peer reports. Revalidating with a
    receipt clock here would discard their original freshness/skew evidence.
    """
    if not isinstance(document, dict) or "error" in document:
        return None
    try:
        return pl.validate_status(document, _platform(document))
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def _report(document):
    models = document.get("models")
    complete = (document.get("loadedListComplete") is True and isinstance(models, list)
                and all(isinstance(model, dict) and model.get("loadedState") in ("loaded", "inactive")
                        for model in models)
                and ("modelsTotal" not in document
                     or (type(document["modelsTotal"]) is int and document["modelsTotal"] == len(models))))
    report = {"observedAt": document.get("observedAt"), "health": document.get("health"),
              "models": models, "modelsComplete": complete, "handsOff": document.get("handsOff")}
    word = report["health"]
    table = HEALTH_WORDS[producer_table(document.get("producer"))]
    rule = table.get(word, {"reason": "schema_invalid"}) if isinstance(word, str) else {"reason": "schema_invalid"}
    if "health" in rule:
        report["health"] = rule["health"]
    return report, rule.get("reason")


def _row(node, name, platform, document, *, now, trust, reason=None):
    # Legacy status.host is a platform kind, not a machine identity.
    host = (_text(document.get("host"), node)
            if document is not None and document.get("kind") == "agiw-peer" else node)
    report = None
    if document is not None:
        report, reason = _report(document)
    row = machine_state.machine_summary(host, platform, report, now=now, trust=trust, reason=reason)
    row["id"] = node
    if isinstance(name, str) and name:
        row["label"] = name
    return row


def status_document(snapshot, *, now=None, full=False, local=None, protocol=pl.PROTOCOL, link_error=None):
    """Schema-2 CLI envelope. None means no usable local control snapshot.

    With no snapshot, link_error (timeout or read_error) means unreachable;
    without an error the local peer link is not running.

    No previous signed documents are retained by this adapter. In particular,
    a stale connection is unknown/not_connected rather than a stale report.
    """
    now = time.time() if now is None else now
    running = snapshot is not None
    snapshot = snapshot or {}
    me = snapshot.get("self") or local or {}
    kind = machine_state.platform_name(me.get("host"))
    node = _text(me.get("node"), kind or "local")
    identity = {"node": node, "name": _text(me.get("name"), node), "host": me.get("host")}
    documents = {}
    local_doc = _accepted(snapshot.get("selfStatus")) if running else None
    machines = [_row(node, me.get("name"), kind, local_doc, now=now, trust="local",
                     reason=(link_error or "unavailable") if local_doc is None else None)]
    if local_doc is not None:
        documents[node] = pl.project_status(local_doc)
    for peer in snapshot.get("peers", []):
        if not peer.get("paired"):
            continue
        peer_node = _text(peer.get("node"), _text(peer.get("name"), "peer"))
        document = _accepted(peer.get("status")) if peer.get("state") == "connected" else None
        platform = _platform(document) if document is not None else peer.get("host")
        reason = None
        if document is None:
            if peer.get("error") or snapshot.get("pollError") or peer.get("state") in ("error", "connected"):
                reason = "unavailable"
            elif peer.get("state") in ("stale", "connecting") or peer.get("lastHello") is not None:
                reason = "not_connected"
            else:
                reason = "not_observed"
        machines.append(_row(peer_node, peer.get("name"), platform, document, now=now,
                             trust="authenticated", reason=reason))
        if document is not None:
            documents[peer_node] = pl.project_status(document)
    present = {row["platform"] for row in machines}
    for platform in PLATFORMS:
        if platform not in present:
            machines.append(machine_state.machine_summary(platform, platform, now=now))
    result = {"schemaVersion": 2, "contract": "machine-status/v1", "sampledAt": now,
              "observedAt": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
              "protocol": snapshot.get("protocol", protocol),
              "linkState": "running" if running else "unreachable" if link_error else "not_running",
              "self": identity,
              "machines": machines, "signed": True, "encrypted": False}
    if full:
        result["documents"] = documents
    return result
