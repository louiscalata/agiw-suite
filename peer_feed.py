"""Per-host Mac, Windows and Linux status: the LAN peer link first, share reports second.
The windows_feed entry point retains its existing single-PC contract for older consumers.
Also this Mac's side of the PC edition's share link: an opt-in presence file in llm-lab/status/agiw-peers/.

Reading is read-only. Nothing here starts, loads or controls anything on either machine, and the observer never
changes the peer-link state folder (its control token is read, never tightened, repaired or rotated). It contacts
a running peer link only when that peer link's own check on contact has nothing to repair, so the folder's mode,
ACL and contents stay as they are (that check may still re-stamp the folder's change time). The paired
LAN feed is shown as live only when its own observation time is fresh; an unsigned share report is only ever
recorded, never live; otherwise the Windows source says why there is neither. The only write is this Mac's own
presence file (and removing this host's own leftover temp files beside it), and only when publishStatus or
peerLink is turned on.
"""
from __future__ import annotations

import errno
import hmac
import json
import math
import os
import plistlib
import re
import secrets
import socket
import urllib.parse
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import peer_link

SHARE_STATUS = Path("/Volumes/SharedChami/llm-lab/status")
FRESH_SECONDS = 30.0
MAX_FILE = 65536
SHARE_READ_DEADLINE = 1.5
CONTROL_DEADLINE = 1.0

# The PC edition's presence names (windows/agiw_win/probes.py), so both editions meet in one folder.
PEER_DIR = ("llm-lab", "status", "agiw-peers")
PEER_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
PRESENCE_INTERVAL = 10.0   # the PC's publish_every
PRESENCE_DEADLINE = 5.0    # a write still running after this is reported stuck; nothing queues behind it
PRESENCE_READS = ["agiw-peers", "windows-status.json"]
# The PC links a Mac by this platform name (collect.py: platform == "macos"); the LAN link's host kind stays "mac".
SHARE_PLATFORM = "macos"
LOADED_STATES = ("loaded", "inactive", "unknown")
# One vocabulary on both editions: the PC's probes.memory_level (ok / watch / tight / critical, unknown when its
# memory status is unreadable) and this Mac's mem_guard.LEVELS (the same four, plus unknown). Published and read.
MEMORY_LEVELS = ("ok", "watch", "tight", "critical", "unknown")
# The PC reader's own folder budget (probes.PEER_SCAN_LIMIT): a folder holding more entries is not read at all.
PEER_SCAN_LIMIT = 256
PEER_MAX_FILES = 32
# The peer-link control exchange as peer_link.control_request makes it: a 1 KB hello, then the peer listing, the
# two replies together bounded by one aggregate limit.
CONTROL_HELLO_LIMIT = 1024
CONTROL_LIMIT = (peer_link.MAX_PEERS + 1) * peer_link.MAX_STATUS_BYTES
CONTROL_TOKEN = "control-token"
CONTROL_TOKEN_PURPOSE = "agiw-peer control-token"
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# Model-file names that make an identifier a path (D4); such an id is published as its bare final name.
MODEL_FILE_SUFFIXES = (".gguf", ".safetensors", ".bin", ".mlx", ".npz", ".pt", ".pth", ".ckpt", ".onnx",
                       ".mlmodel", ".mlpackage")
_REGISTRY_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:+-]{0,119}\Z")
# Leftover temp files of this host's own presence writes (a dropped share or a killed process) older than this
# are swept before the next write, so they never crowd the PC reader's bounded folder scan.
TEMP_SWEEP_AGE = 300.0
TEMP_SWEEP_LIMIT = 1024

# Nisi and Jev on this Mac as peers see them: the observer's own component states (telemetry._route_components),
# published with fixed words per state. Free text from the snapshot never leaves the Mac.
COMPONENTS = (
    ("nisi", "Nisi route adapter", {
        "ready": "Nisi Inference route ready on this Mac (pins verified, edit pair loaded)",
        "partial": "Nisi Inference pins verified; the Mac edit pair is not fully loaded",
        "in-use": "A Nisi call is in flight for a live route on this Mac",
        "unresolved": "A Nisi pending marker on this Mac needs owner recovery",
        "needs-action": "Nisi Inference host binding drifted since activation on this Mac",
        "unknown": "Nisi Inference state not known on this Mac",
    }),
    ("jev", "Jev", {
        "configured": "Jev opted in on this Mac (key never read or shared)",
        "unavailable": "No Jev opt-in on this Mac",
        "unknown": "Jev state not known on this Mac",
    }),
)


class InventoryUnknown(ValueError):
    """The Mac's model inventory is unavailable or incomplete, not an empty list."""


class LinkUnavailable(Exception):
    """The LAN feed cannot be read without changing anything (a missing, loose, foreign or malformed control token,
    or a listener that did not prove it is this user's peer link). Fixed words, never a path or a secret."""


class ShareRefused(ValueError):
    """The share's status folders cannot be read as bounded, link-free folders, so nothing from the share is used
    (never a partial read). Fixed words, never a path from the share."""

# The PC reads publishStatus / peerLink from config.json beside its observer; the Mac keeps it beside peer-link/.
CONFIG_PATH = Path.home() / "Library" / "Application Support" / "AGIW" / "config.json"
CONFIG_MAX_BYTES = 16384
CONFIG_KEYS = ("publishStatus", "peerLink")


def _number(value: Any, low: float, high: float) -> float | None:
    """A finite number in range, else None. JSON allows 1e999 (infinity), which would break serialization."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and low <= value <= high else None


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value.isprintable() else None


def _summary(doc: dict, via: str, now: float, platform: str = "windows") -> dict[str, Any]:
    if not isinstance(doc, dict) or type(doc.get("schemaVersion")) is not int or doc["schemaVersion"] != 1:
        raise ValueError("status document invalid")
    raw_models = doc.get("models")
    valid_models, seen = [], set()
    for model in (raw_models if isinstance(raw_models, list) else [])[:peer_link.MAX_MODELS]:
        if (not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"].strip()
                or model.get("loadedState") not in LOADED_STATES):
            continue
        model_id = model["id"][:256]
        if model_id in seen:
            continue
        seen.add(model_id)
        valid_models.append(model)
    complete = (doc.get("modelsComplete") is not False and isinstance(raw_models, list) and len(raw_models) <= peer_link.MAX_MODELS
                and len(valid_models) == len(raw_models))
    if doc.get("kind") == "agiw-peer":
        complete = complete and type(doc.get("modelsTotal")) is int and doc["modelsTotal"] == len(valid_models)
    # Keep the valid prefix of a partial inventory; the link's other status rules still apply.
    doc = peer_link.validate_status({**doc, "models": valid_models}, platform, now)
    observed = peer_link._parse_iso(doc["observedAt"])
    age = now - observed
    if not math.isfinite(age) or age < -2.0:
        # A clock mismatch is never turned into freshness.
        raise ValueError("PC observation is dated in the future (clock mismatch)")
    models = []
    for model in doc["models"][:peer_link.MAX_MODELS]:
        row = {"id": str(model["id"])[:256], "loadedState": model["loadedState"]}
        for key in ("activity", "lane", "servedModel"):
            if _text(model.get(key), 64) is not None:
                row[key] = _text(model[key], 64)
        for key in ("slotsBusy", "slotsTotal"):
            if _number(model.get(key), 0, 256) is not None and isinstance(model[key], int):
                row[key] = model[key]
        rate = _number(model.get("liveTokensPerSecond"), 0, 100000)
        if rate is not None:
            row["liveTokensPerSecond"] = round(rate, 1)
        models.append(row)
    gpus = []
    for g in (doc.get("gpus") if isinstance(doc.get("gpus"), list) else [])[:4]:
        if not isinstance(g, dict):
            continue
        gpu = {"name": _text(g.get("name"), 80)}
        for key, high in (("utilizationPercent", 100), ("memoryUsedMiB", 1 << 22), ("memoryTotalMiB", 1 << 22),
                          ("temperatureC", 150)):
            gpu[key] = _number(g.get(key), 0, high)
        gpus.append({k: v for k, v in gpu.items() if v is not None})
    memory = doc.get("memory") if isinstance(doc.get("memory"), dict) else {}
    # The PC's own levels (probes.memory_level): "tight" is the pressure case the inspector must keep showing.
    level = memory.get("level") if isinstance(memory.get("level"), str) and memory["level"] in MEMORY_LEVELS else None
    free = _number(memory.get("availablePercent"), 0, 100)
    return {"via": via, "host": _text(doc.get("host"), 63) or "", "health": doc.get("health") if doc.get("health") in
            ("ok", "degraded", "down") else None, "observedAt": doc["observedAt"],
            "ageSeconds": round(max(0.0, age), 1), "models": models, "modelsComplete": complete, "gpus": gpus,
            "memory": {k: v for k, v in (("level", level), ("availablePercent", free)) if v is not None} or None}


REASON_CODES = frozenset({"ok", "timeout", "refused", "non_json", "oversize", "schema_invalid", "stale",
                          "host_key_mismatch", "share_unmounted", "asleep", "clock_skew", "not_connected",
                          "unverified", "unavailable"})


class FeedUnavailable(Exception):
    """A transport's fixed reason code; never include remote text or private paths."""
    def __init__(self, reason_code: str):
        self.reason_code = reason_code if reason_code in REASON_CODES else "unavailable"
        super().__init__(self.reason_code)


def normalize_platform(value: Any) -> str | None:
    return {"macos": "mac", "mac": "mac", "windows": "windows", "linux": "linux"}.get(value) if isinstance(value, str) else None


def failure_reason(exc: Exception) -> str:
    if isinstance(exc, FeedUnavailable):
        return exc.reason_code
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, FileNotFoundError):
        return "share_unmounted"
    if isinstance(exc, (ShareRefused, PermissionError)):
        return "refused"
    if isinstance(exc, LinkUnavailable):
        return "unverified"
    if isinstance(exc, (json.JSONDecodeError, UnicodeError)):
        return "non_json"
    if isinstance(exc, ValueError):
        return "oversize" if "too large" in str(exc) else "schema_invalid"
    return "unavailable"


def _feed_failure(platform: str, host: str, reason: str, age: float | None = None) -> dict:
    return {"feed": None, "source": {"id": f"{platform}:{host}-telemetry", "label": host,
            "host": host, "platform": platform, "state": "unavailable", "trust": "unverified",
            "reasonCode": reason, "ageSeconds": age, "detail": reason.replace("_", " ")}}


def summarize_document(doc: Any, via: str, now: float, *, platform: str | None = None,
                       host: str | None = None) -> dict:
    """Project one host's report. Transport callers supply trusted identity and provenance, not document fields.

    Failure keeps attribution and age but exposes no stale model inventory. A share report can never acquire
    authenticated trust, including a relayed Linux document with a relayedBy field.
    """
    expected = normalize_platform(platform)
    identity = host or expected or "unknown"
    try:
        if isinstance(doc, (bytes, str)):
            raw = doc.encode("utf-8") if isinstance(doc, str) else doc
            if len(raw) > MAX_FILE:
                raise FeedUnavailable("oversize")
            try:
                doc = peer_link.strict_json(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise FeedUnavailable("non_json") from None
        if not isinstance(doc, dict):
            raise FeedUnavailable("schema_invalid")
        actual = normalize_platform(doc.get("platform") if doc.get("kind") == "agiw-peer" else doc.get("host"))
        if actual is None or (platform is not None and expected is None):
            raise FeedUnavailable("schema_invalid")
        if expected is not None and expected != actual:
            raise FeedUnavailable("host_key_mismatch")
        expected = actual
        reported_host = doc.get("host")
        if not isinstance(reported_host, str) or not PEER_HOST.fullmatch(reported_host):
            raise FeedUnavailable("schema_invalid")
        if host is not None and host != reported_host:
            raise FeedUnavailable("host_key_mismatch")
        identity = reported_host
        canonical = dict(doc)
        if canonical.get("kind") == "agiw-peer":
            canonical["platform"] = actual
        else:
            canonical["host"] = actual
        observed = peer_link._parse_iso(canonical["observedAt"])
        age = now - observed
        if not math.isfinite(age):
            raise FeedUnavailable("schema_invalid")
        if age < -2:
            raise FeedUnavailable("clock_skew")
        if age > FRESH_SECONDS:
            result = _feed_failure(actual, identity, "stale", round(age, 1))
            result["source"]["observedAt"] = canonical["observedAt"]
            return result
        if doc.get("health") not in ("ok", "degraded", "down"):
            raise FeedUnavailable("schema_invalid")
        summary = _summary(canonical, via, now, actual)
        # Only the PC has an accepted share-reported health source. Other platforms
        # retain the unsigned share observation as a diagnostic, never health evidence.
        if actual != "windows" and via == "share":
            result = _feed_failure(actual, identity, "unverified", round(max(0.0, age), 1))
            result["source"].update(via="share", observedAt=canonical["observedAt"],
                                     detail=f"{'Mac' if actual == 'mac' else 'Linux'} share hint; authenticated transport required")
            return result
        trust = "authenticated" if via in ("lan", "ssh") else "share-reported" if via == "share" else "unverified"
        summary.update(host=identity, platform=actual, trust=trust, reasonCode="ok")
        if type(doc.get("handsOff")) is bool:
            summary["handsOff"] = doc["handsOff"]
        source = {"id": f"{actual}:{identity}-telemetry", "label": identity, "host": identity,
                  "platform": actual, "state": "live" if trust == "authenticated" else "recorded",
                  "trust": trust, "reasonCode": "ok", "ageSeconds": summary["ageSeconds"],
                  "detail": "Authenticated peer report" if trust == "authenticated" else
                            "Unsigned report via share" if trust == "share-reported" else "Unverified report"}
        return {"feed": summary, "source": source}
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError, FeedUnavailable) as exc:
        return _feed_failure(expected or "unknown", identity, failure_reason(exc) if isinstance(exc, (ValueError, FeedUnavailable)) else "schema_invalid")


# Remember only each UUID's last reported label to detect a change within this process.
# Labels are not globally unique identities or a trusted cross-transport association.
# Never persist peer state. Bound by MAX_PEERS.
_paired_hosts: dict[str, tuple[str, str]] = {}
_paired_hosts_scope: str | None = None
_paired_hosts_lock = threading.Lock()


def from_peer_link_hosts(now: float, state_dir: Path | None = None, port: int | None = None) -> list[dict]:
    """All paired hosts from the existing authenticated local control channel; never repair its state."""
    state_dir = state_dir or peer_link.default_state_dir()
    try:
        os.lstat(state_dir / CONTROL_TOKEN)
    except FileNotFoundError:
        return []
    status, listing = _control_get(state_dir, peer_link.STATUS_PORT if port is None else port, "/control/peers")
    if status != 200:
        raise FeedUnavailable("refused")
    if not isinstance(listing, dict) or not isinstance(listing.get("peers"), list) or len(listing["peers"]) > peer_link.MAX_PEERS:
        raise FeedUnavailable("schema_invalid")
    global _paired_hosts_scope
    rows = []
    with _paired_hosts_lock:
        scope = str(state_dir)
        if _paired_hosts_scope != scope:
            _paired_hosts.clear()
            _paired_hosts_scope = scope
        paired = [row for row in listing["peers"] if isinstance(row, dict) and row.get("paired") is True]
        active = {row.get("node") for row in paired if isinstance(row.get("node"), str)}
        for node in list(_paired_hosts):
            if node not in active:
                del _paired_hosts[node]
        for row in paired:
            platform = normalize_platform(row.get("host"))
            node = row.get("node")
            if platform is None:
                continue
            if not isinstance(node, str) or not peer_link.UUID_RE.fullmatch(node):
                rows.append(_feed_failure(platform, platform, "schema_invalid"))
                continue
            known = _paired_hosts.get(node)
            host = known[1] if known and known[0] == platform else node
            if row.get("state") == "connected" and row.get("status") is not None:
                result = summarize_document(row["status"], "lan", now, platform=platform,
                                            host=known[1] if known else None)
                if result["feed"]:
                    claimed = result["feed"]["host"]
                    if known and known[0] != platform:
                        result = _feed_failure(platform, host, "host_key_mismatch")
                    else:
                        _paired_hosts[node] = (platform, claimed)
                        host = claimed
                else:
                    result["source"].update(host=host, label=host)
            else:
                result = _feed_failure(platform, host, "not_connected")
            # The paired UUID owns the row. A peer may report a label used by another
            # UUID; that cannot reserve the name or make the other peer fail validation.
            result["source"].update(node=node, host=node, id=f"{platform}:{node}-telemetry",
                                     label=f"{host} (reported)" if host != node else node)
            if host != node:
                result["source"]["reportedHost"] = host
            if result["feed"]:
                result["feed"].update(node=node, host=node, reportedHost=host)
            rows.append(result)
    return rows


def from_share_hosts(now: float, root: Path | None = None, anchor: Path | None = None) -> list[dict]:
    """Bounded, link-free read of every platform; freshness and provenance belong to each host."""
    return from_share(now, root, anchor, _all_hosts=True)


class _HostShareReader:
    """One bounded share read at a time; sleeping SMB never grows an unbounded thread queue."""
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None

    def read(self, now: float) -> list[dict]:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise FeedUnavailable("timeout")
            box = {}
            def run():
                try:
                    box["rows"] = from_share_hosts(now)
                except Exception as exc:
                    box["error"] = exc
            thread = threading.Thread(target=run, name="agiw-host-share-feed", daemon=True)
            self.thread = thread
            thread.start()
        thread.join(SHARE_READ_DEADLINE)
        if thread.is_alive():
            raise FeedUnavailable("timeout")
        if "error" in box:
            raise box["error"]
        return box["rows"]


_host_share_reader = _HostShareReader()


# Diagnostics are observations, never additional machine rows or health evidence. Keep
# only projected metadata, bounded independently of an injected reader's output size.
MAX_FEED_DIAGNOSTICS = PEER_MAX_FILES + 1


def _attach_diagnostic(target: dict, observation: dict, candidate_nodes=()) -> None:
    source = observation["source"]
    diagnostic = {key: source[key] for key in
                  ("host", "platform", "reasonCode", "ageSeconds", "observedAt", "legacy") if key in source}
    diagnostic.update(correlation="unverified", trust="share-reported",
                      detail="Uncorrelated share observation; not machine identity or health evidence")
    if candidate_nodes:
        diagnostic["candidateNodes"] = sorted(candidate_nodes)[:peer_link.MAX_PEERS]
    feed = observation.get("feed")
    if feed:
        diagnostic["observedAt"] = feed.get("observedAt")
        diagnostic["reportedHealth"] = feed.get("health")
    diagnostics = target["source"].setdefault("diagnostics", [])
    for item in [diagnostic, *source.get("diagnostics", [])[:MAX_FEED_DIAGNOSTICS]]:
        if len(diagnostics) >= MAX_FEED_DIAGNOSTICS:
            target["source"]["diagnosticsTruncated"] = True
            break
        diagnostics.append(dict(item))


def peer_feeds(now: float, *, peer_reader: Callable | None = None, share_reader: Callable | None = None) -> list[dict]:
    """Per-host feed/source pairs, authenticated before share, with explicit missing-platform failures.

    Readers are injectable for fixture-only callers. No transport inference equates a timeout with sleep.
    """
    merged = {}
    failures = []
    local_host = host_id()
    paired_platforms = {}
    for reader_index, reader in enumerate((peer_reader or from_peer_link_hosts, share_reader or _host_share_reader.read)):
        try:
            rows = reader(now)
        except Exception as exc:
            failures.append(failure_reason(exc))
            continue
        for row in rows:
            # Do not mutate reader-owned rows (a cached failure may be reused next poll).
            row = {**row, "source": dict(row["source"])}
            if "diagnostics" in row["source"]:
                row["source"]["diagnostics"] = list(row["source"]["diagnostics"])
            source = row["source"]
            host_key = (source["platform"], source["host"])
            if host_key == ("mac", local_host):
                continue  # our own share presence is already represented by local telemetry
            node = source.get("node") if reader_index == 0 else None
            key = (source["platform"], node or source["host"])
            if reader_index == 0 and node:
                paired_platforms.setdefault(source["platform"], []).append(key)
            if reader_index == 1 and source["platform"] in paired_platforms:
                # Neither first signed label nor friendly pairing name proves that an
                # unsigned share file is this UUID. Even after a warm disconnect, keep
                # it as an uncertain observation; never erase an authenticated failure.
                candidates = paired_platforms[source["platform"]]
                nodes = [candidate[1] for candidate in candidates]
                for candidate in candidates:
                    _attach_diagnostic(merged[candidate], row, nodes)
                continue
            previous = merged.get(key)
            if reader_index == 1 and previous and previous["source"].get("reasonCode") == "host_key_mismatch":
                _attach_diagnostic(previous, row)
                continue
            if reader_index == 1 and source["platform"] == "linux":
                # A share cannot mask a failed authenticated read, including a reader-wide failure.
                if previous is not None:
                    continue
                if failures:
                    row = _feed_failure("linux", source["host"], failures[0])
            if previous is None or (not previous["feed"] and row["feed"]):
                merged[key] = row
            elif previous["feed"] and row["feed"] and previous["source"]["trust"] == source["trust"]:
                if peer_link._parse_iso(row["feed"]["observedAt"]) > peer_link._parse_iso(previous["feed"]["observedAt"]):
                    merged[key] = row
    if any(row["feed"] and row["source"]["platform"] == "windows"
           and row["source"].get("legacy") is not True for row in merged.values()):
        merged = {key: row for key, row in merged.items()
                  if not (row["source"]["platform"] == "windows" and row["source"].get("legacy") is True)}
    represented = {key[0] for key in merged}
    unattributed = [row["source"]["reasonCode"] for key, row in merged.items()
                    if key[0] == "unknown" and row["feed"] is None]
    for platform in ("windows", "linux"):
        if platform not in represented:
            reason = unattributed[0] if unattributed else failures[-1] if failures else "not_connected"
            missing = _feed_failure(platform, platform, reason)
            if unattributed:
                missing["source"]["detail"] = "Peer document could not be attributed: " + reason.replace("_", " ")
            merged[(platform, platform)] = missing
    return [merged[key] for key in sorted(merged)]


_checked_dirs: dict[str, tuple] = {}  # state folder -> (inode, ctime_ns) whose access list was read clean


def _observer_token(state_dir: Path) -> str:
    """The peer link's control token, read without changing anything. Repair belongs to the peer link itself:
    a running peer link calls peer_link.load_control_token for every control exchange, including the hello the
    observer sends, and that call sets the folder to 0700, strips its ACL, removes case-variant names and rotates
    a bad token. So the observer contacts the peer link only when nothing there is left to repair: a folder that
    is a link, not this user's, any mode other than 0700 (0750 and 0500 too, not only group- or world-writable)
    or carrying an extended ACL, a case-variant token name, or a token that is loose, foreign, linked, unreadable
    or not 64 lowercase hex makes the LAN feed unavailable before anything is sent. The peer link's own re-check
    of a folder already at 0700 can still re-stamp the folder's change time; mode, ACL and contents stay as they
    are. (The folder rules are POSIX; this feed runs in the Mac edition.)"""
    try:
        info = os.lstat(state_dir)
    except OSError:
        raise LinkUnavailable("peer-link folder unreadable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LinkUnavailable("peer-link folder is a link or not a folder")
    if os.name != "nt":
        if info.st_uid != os.getuid():
            raise LinkUnavailable("peer-link folder belongs to another user")
        if info.st_mode & 0o022:
            raise LinkUnavailable("peer-link folder is writable by other users")
        if stat.S_IMODE(info.st_mode) != 0o700:
            # peer_link._restrict_dir would chmod it to 0700 when the hello reaches a running peer link.
            raise LinkUnavailable("peer-link folder mode is not owner-only 0700 (the peer link repairs it)")
        key = str(state_dir)
        if sys.platform == "darwin" and _checked_dirs.get(key) != (info.st_ino, info.st_ctime_ns):
            try:
                peer_link._only_me(state_dir, True)  # reads the access list (ls -led); changes nothing
            except (OSError, subprocess.SubprocessError):
                raise LinkUnavailable("peer-link folder access list is not owner-only (extended ACL)") from None
            _checked_dirs[key] = (info.st_ino, info.st_ctime_ns)
    try:
        variants = peer_link._case_variants(state_dir, CONTROL_TOKEN)
    except OSError:
        raise LinkUnavailable("peer-link folder unreadable") from None
    if variants:
        raise LinkUnavailable("a case-variant control token is present (the peer link removes it)")
    with peer_link._token_lock:  # never mid-rotation by an in-process peer link
        try:
            stored = peer_link._read_private(state_dir / CONTROL_TOKEN)
        except peer_link.LooseSecret:
            raise LinkUnavailable("control token could have been read or written by another user "
                                  "(the peer link replaces it)") from None
        except (OSError, ValueError):
            raise LinkUnavailable("control token unreadable") from None
    try:
        token = peer_link._unseal(stored.strip(), CONTROL_TOKEN_PURPOSE)
    except (OSError, ValueError):
        raise LinkUnavailable("control token is not sealed to this account") from None
    if not isinstance(token, str) or not _TOKEN.match(token):
        raise LinkUnavailable("control token malformed (the peer link replaces it)")
    return token


def _control_get(state_dir: Path, port: int, path: str, deadline: float = CONTROL_DEADLINE) -> tuple[int, Any]:
    """GET one control path from this user's own peer link on 127.0.0.1, as peer_link.control_request does it but
    with the read-only token above. The listener must first prove it holds the token (hello: our nonce in, its
    nonce and proof out); only then is a token-derived proof sent. The token itself is never sent."""
    token = _observer_token(state_dir)
    nc = secrets.token_hex(16)
    status, _h, raw = peer_link._http("127.0.0.1", port, "GET", "/control/hello", {"X-Agiw-Client-Nonce": nc}, None,
                                      CONTROL_HELLO_LIMIT, deadline=min(deadline, 3.0))
    hello = peer_link.strict_json(raw) if status == 200 and raw else {}
    ns = hello.get("ns") if isinstance(hello, dict) else None
    if (not isinstance(ns, str) or not _NONCE.match(ns)
            or not hmac.compare_digest(str(hello.get("proof", "")), peer_link.control_server_proof(token, nc, ns))):
        raise LinkUnavailable("the process on the peer-link port did not prove it is your peer link; nothing was sent")
    status, _h, body = peer_link._http("127.0.0.1", port, "GET", path, {
        "X-Agiw-Control-Nonce": ns, "Content-Type": "application/json",
        "X-Agiw-Control-Proof": peer_link.control_client_proof(token, ns, nc, "GET", path, b"")},
        None, max(0, CONTROL_LIMIT - len(raw)), deadline=deadline)
    return status, (peer_link.strict_json(body) if body else {})


def from_peer_link(now: float, state_dir: Path | None = None, port: int | None = None) -> dict | None:
    """The connected, paired Windows peer from the local peer-link daemon, or None. Raises LinkUnavailable when the
    control token or its folder cannot be used as they are; the observer never creates, repairs or rotates
    peer-link state, and it contacts a running peer link only when that peer link's own check has nothing to
    repair (see _observer_token). No control token means no peer link is set up: nothing is attempted."""
    state_dir = state_dir or peer_link.default_state_dir()
    port = peer_link.STATUS_PORT if port is None else port
    try:
        os.lstat(state_dir / CONTROL_TOKEN)
    except FileNotFoundError:
        return None  # no peer link set up on this Mac; never create one from the observer
    except OSError:
        raise LinkUnavailable("peer-link folder unreadable") from None
    status, listing = _control_get(state_dir, port, "/control/peers")
    if status != 200 or not isinstance(listing, dict):
        return None
    for row in listing.get("peers", []):
        if row.get("paired") and row.get("host") == "windows" and row.get("state") == "connected" and row.get("status"):
            summary = _summary(row["status"], "lan", now)
            age = now - peer_link._parse_iso(summary["observedAt"])
            return summary if age <= FRESH_SECONDS else None
    return None


SHARE_ANCHOR = Path("/Volumes/SharedChami")


def _share_anchor(root: Path, anchor: Path | None) -> Path:
    if anchor is not None:
        return anchor
    return SHARE_ANCHOR if SHARE_ANCHOR in root.parents else root.parents[len(root.parents) - 2]


def _open_below(anchor: Path, parts: tuple[str, ...]) -> int:
    """A descriptor for anchor/parts: every component below the anchor opened as a folder with O_NOFOLLOW relative
    to its parent's descriptor, so neither a symlinked folder nor one swapped between check and open redirects it."""
    fd = os.open(str(anchor), _DIR_FLAGS)
    try:
        for part in parts:
            child = os.open(part, _DIR_FLAGS | _NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_at(directory: int, name: str) -> bytes:
    """One status record in an already-opened folder: not a link, a regular file, at most 64 KB."""
    fd = os.open(name, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("status record is not a file")
        data = b""
        while len(data) <= MAX_FILE:
            part = os.read(fd, MAX_FILE + 1 - len(data))
            if not part:
                break
            data += part
        if len(data) > MAX_FILE:
            raise ValueError("status record too large")
        return data
    finally:
        os.close(fd)


def _peer_names(directory: int) -> list[str]:
    """agiw-peers/<host>.json candidates, newest first, from one bounded pass over the opened folder (the PC reader's
    rules: a host-shaped name, a regular file, never followed). More than PEER_SCAN_LIMIT entries of any kind means
    the folder is not read at all: a flooded folder is unavailable, never a partial answer."""
    seen, dated = 0, []
    with os.scandir(directory) as entries:
        for entry in entries:
            seen += 1
            if seen > PEER_SCAN_LIMIT:
                raise ShareRefused(f"agiw-peers holds more than {PEER_SCAN_LIMIT} entries (too many entries); "
                                   "share status not read")
            name = entry.name
            if not name.endswith(".json") or name.startswith(".") or not PEER_HOST.match(name[:-5]):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue  # removed between listing and stat
            if stat.S_ISREG(info.st_mode):
                dated.append((info.st_mtime, name))
    return [name for _, name in sorted(dated, reverse=True)[:PEER_MAX_FILES]]


def _refused_folder(exc: OSError) -> bool:
    return exc.errno in (errno.ELOOP, errno.ENOTDIR)


def from_share(now: float, root: Path | None = None, anchor: Path | None = None,
               *, _all_hosts: bool = False) -> dict | list | None:
    """The freshest Windows presence file in agiw-peers/, then windows-status.json; None when nothing is fresh.

    Every folder from the share root down is opened descriptor-relatively without following links, agiw-peers is
    enumerated once from its own descriptor within PEER_SCAN_LIMIT entries, and its records are read through that
    same descriptor. ShareRefused (nothing from the share is used) when a folder on the way is a symbolic link or not
    a folder, when agiw-peers holds too many entries, or when agiw-peers was replaced while it was read."""
    root = SHARE_STATUS if root is None else root
    anchor = _share_anchor(root, anchor)
    try:
        parts = root.relative_to(anchor).parts
    except ValueError:
        raise ShareRefused("status folder is not below the share root") from None
    try:
        status_dir = _open_below(anchor, parts)
    except FileNotFoundError:
        if _all_hosts:
            raise FeedUnavailable("share_unmounted") from None
        return None  # share not mounted, or no llm-lab/status on it
    except OSError as exc:
        if _refused_folder(exc):
            raise ShareRefused("a share status folder is a symbolic link or not a folder; refused") from None
        raise
    records: list[tuple[str | None, bytes | Exception]] = []
    try:
        try:
            peers = os.open("agiw-peers", _DIR_FLAGS | _NOFOLLOW, dir_fd=status_dir)
        except FileNotFoundError:
            peers = None
        except OSError as exc:
            if _refused_folder(exc):
                raise ShareRefused("agiw-peers is a symbolic link or not a folder; refused") from None
            raise
        if peers is not None:
            try:
                opened = os.fstat(peers)
                for name in _peer_names(peers):
                    try:
                        records.append((name[:-5], _read_at(peers, name)))
                    except (OSError, ValueError) as exc:
                        if _all_hosts:
                            records.append((name[:-5], exc))
                        continue
                try:
                    still = os.stat("agiw-peers", dir_fd=status_dir, follow_symlinks=False)
                except OSError:
                    still = None
                if still is None or (still.st_dev, still.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ShareRefused("agiw-peers was replaced while it was read; refused")
            finally:
                os.close(peers)
        try:
            records.append((None, _read_at(status_dir, "windows-status.json")))
        except (OSError, ValueError) as exc:
            if _all_hosts and not isinstance(exc, FileNotFoundError):
                records.append((None, exc))
    finally:
        os.close(status_dir)
    if _all_hosts:
        rows = []
        legacy = []
        for host, raw in records:
            row = (_feed_failure("unknown" if host else "windows", host or "windows", failure_reason(raw))
                   if isinstance(raw, Exception) else
                   summarize_document(raw, "share", now, host=host, platform="windows" if host is None else None))
            if host is None:
                row["source"]["legacy"] = True  # local read-path metadata, never trusted from document content
            (rows if host is not None else legacy).append(row)
        # The PC writes both formats; the legacy platform-named report is a fallback, not a second machine.
        if not any(row["feed"] and row["source"]["platform"] == "windows" for row in rows):
            if legacy and legacy[0]["feed"]:
                # Failed modern observations do not establish an additional PC. Keep
                # their named errors on the selected legacy report without claiming
                # that its platform-only name proves which modern file it belongs to.
                uncertain = [row for row in rows if not row["feed"]
                             and row["source"]["platform"] in ("windows", "unknown")]
                for row in uncertain:
                    _attach_diagnostic(legacy[0], row)
                rows = [row for row in rows if row not in uncertain]
            rows.extend(legacy)
        return rows
    best, best_observed = None, None
    for _host, raw in records:
        try:
            summary = _summary(peer_link.strict_json(raw), "share", now)
            observed = peer_link._parse_iso(summary["observedAt"])
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
            continue
        if now - observed <= FRESH_SECONDS and (best_observed is None or observed > best_observed):
            best, best_observed = summary, observed
    return best


class _ShareReader:
    """SMB reads can hang when the PC sleeps. One reader thread at a time, with a deadline; a stuck
    reader is reported instead of piling up new threads behind it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.result: Any = None

    def read(self, now: float) -> tuple[str, dict | None, str | None]:
        """(outcome, feed, note): outcome ok, stuck or error; note is a refusal's fixed words (ShareRefused)."""
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return "stuck", None, None
            box: dict[str, Any] = {}

            def run():
                try:
                    box["value"] = from_share(now)
                except Exception as exc:  # noqa: BLE001
                    box["error"] = exc

            self.thread = threading.Thread(target=run, name="agiw-share-feed", daemon=True)
            self.thread.start()
        self.thread.join(SHARE_READ_DEADLINE)
        if self.thread.is_alive():
            return "stuck", None, None
        if "error" in box:
            return "error", None, str(box["error"]) if isinstance(box["error"], ShareRefused) else None
        return "ok", box.get("value"), None


_share_reader = _ShareReader()


def windows_feed(now: float) -> tuple[dict | None, dict]:
    """(feed, source row). LAN beats share; both must be fresh. The paired LAN feed is live (the paired PC's own,
    signed report); an unsigned share report is only recorded, never live, whatever its timestamp says."""
    lan_note = None
    try:
        feed = from_peer_link(now)
        if feed:
            return feed, {"id": "windows-telemetry", "label": "Windows", "state": "live", "ageSeconds": feed["ageSeconds"],
                          "detail": f"Reported by {feed['host'] or 'the PC'} over the paired LAN link "
                                    "(signed by the paired peer; its own report of its state)"}
    except LinkUnavailable as exc:  # fixed words: why the token or the listener could not be used as they are
        lan_note = str(exc)
    except Exception as exc:  # noqa: BLE001 - the share fallback still runs
        lan_note = type(exc).__name__
    outcome, feed, note = _share_reader.read(now)
    lan = f"LAN link not connected: {lan_note}" if lan_note else "LAN link not connected"
    if feed:
        return feed, {"id": "windows-telemetry", "label": "Windows", "state": "recorded", "ageSeconds": feed["ageSeconds"],
                      "detail": f"Unsigned report from {feed['host'] or 'the PC'} in SharedChami status files "
                                f"(not verified; {lan})"}
    why = note or {"stuck": "share read did not finish (PC asleep or share dropped)", "error": "share status unreadable",
                   "ok": "no fresh PC status on the share"}[outcome]
    return None, {"id": "windows-telemetry", "label": "Windows", "state": "unavailable", "ageSeconds": None,
                  "detail": f"Not linked: peer link not connected{f' ({lan_note})' if lan_note else ''}; {why}"}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z"


def _strip_model_suffix(name: str) -> str:
    """The name before its first model-file extension, wherever it sits: "q.gguf" and "q.gguf.part" both give "q"
    (a download in progress keeps the extension in the middle). Extensions are matched as whole dot-separated
    pieces, so "qwen3.8-27b" or "model.bin-v2" are left as they are."""
    pieces = name.split(".")
    for index in range(1, len(pieces)):
        if "." + pieces[index].lower() in MODEL_FILE_SUFFIXES:
            return ".".join(pieces[:index])
    return name


# First folders of a path, never of a registry id. Under a home folder the next name is an account name.
_HOME_ROOTS = ("users", "home")
_SYSTEM_ROOTS = _HOME_ROOTS + ("private", "var", "tmp", "volumes", "opt", "usr", "etc", "library", "system",
                               "applications", "root", "mnt")


_TAG_ID = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]*$")
_URI_SCHEMES = frozenset({"file", "http", "https", "ftp", "sftp", "s3", "gs", "hf", "smb", "afp", "nfs", "data"})


def _final_name(text: str) -> str | None:
    """The last usable part of a path-shaped value, cut before a model-file extension; None for account names."""
    parts = re.split(r"[/\\:]", text)
    index = next((i for i in range(len(parts) - 1, -1, -1) if parts[i] not in ("", ".", "..")), None)
    if index is None or (index > 0 and parts[index - 1].lower() in _HOME_ROOTS) or parts[index].startswith("~"):
        return None
    name = _strip_model_suffix(parts[index]).strip()
    return name[:120] if name and name not in (".", "..") and "%" not in name else None


def public_id(value: Any) -> str | None:
    """A model identifier fit to publish on the share. A registry-style id (one name, or publisher/model with at
    most one "/", safe characters, no model-file extension in any part, a first part that is not a home or system
    folder such as Users, home, private or var) is published as it is. Anything path-shaped (absolute, Windows or
    URI-like, more than one "/", an empty, "." or ".." part, a part holding a model-file extension such as .gguf,
    even mid-name as in .gguf.part, or a home or system first folder) is published only as its final name cut
    before that extension; a final name that is an account name (the one right after Users or home, or ~name)
    or no usable final name drops the value. A two-part relative path shaped like publisher/model (alice/models)
    cannot be told from a registry id and is published as it is."""
    if not isinstance(value, str) or not value.isprintable():
        return None
    text = value.strip()
    if not text:
        return None
    if "%" in text:
        # Percent-encoded separators ('file:%2FUsers%2Falice%2F...') are judged by what they decode to, and an id
        # with any '%' (even a malformed escape) is never a registry id: only a safe final name is published.
        decoded = urllib.parse.unquote(text)
        return _final_name(decoded) if decoded.isprintable() else None
    if ":" in text and (not _TAG_ID.match(text) or text.split(":", 1)[0].split("/")[-1].lower() in _URI_SCHEMES):
        # URI schemes and other colon forms; only (publisher/)name:tag ids pass on to the registry rules below
        return _final_name(text)
    unified = text.replace("\\", "/")
    parts = unified.split("/")
    path_shaped = ("\\" in text or ":/" in unified or unified.startswith(("/", "~")) or len(parts) > 2
                   or any(not part or part in (".", "..") or _strip_model_suffix(part) != part for part in parts)
                   or (len(parts) == 2 and (parts[0].lower() in _SYSTEM_ROOTS
                                            or not all(_REGISTRY_PART.match(part) for part in parts))))
    if not path_shaped:
        return text[:120]
    index = next((i for i in range(len(parts) - 1, -1, -1) if parts[i] not in ("", ".", "..")), None)
    if index is None or (index > 0 and parts[index - 1].lower() in _HOME_ROOTS) or parts[index].startswith("~"):
        return None
    name = _strip_model_suffix(parts[index]).strip()
    return name[:120] if name and name not in (".", "..") else None


def host_id() -> str:
    """This Mac's presence name: the short host name in lower case (the PC's probes.host_id rule)."""
    name = socket.gethostname().split(".")[0].lower()
    return name if PEER_HOST.match(name) else "mac"


def app_version(here: Path | None = None) -> str | None:
    """CFBundleShortVersionString from Info.plist beside the code (checkout) or in Contents/ (app bundle)."""
    here = here or Path(__file__).resolve().parent
    for path in (here / "Info.plist", here.parent / "Info.plist"):
        try:
            with open(path, "rb") as handle:
                value = plistlib.loads(handle.read(MAX_FILE)).get("CFBundleShortVersionString")
        except Exception:  # noqa: BLE001 - a missing or odd plist only leaves the version unknown
            continue
        if isinstance(value, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,24}", value):
            return value
    return None


def _mac_gpus(gpu: Any) -> list[dict[str, Any]]:
    """snapshot.macGpu in the PC's gpu shape. Apple GPUs share system memory, so there is no VRAM total and
    no temperature; those stay null rather than guessed."""
    if not isinstance(gpu, dict):
        return []
    in_use = _number(gpu.get("inUseBytes"), 0, 1 << 50)
    return [{"index": 0, "name": _text(gpu.get("model"), 80),
             "utilizationPercent": _number(gpu.get("utilizationPercent"), 0, 100),
             "memoryUsedMiB": round(in_use / 1048576) if in_use is not None else None,
             "memoryTotalMiB": None, "temperatureC": None}]


def mac_components(snapshot: dict) -> list[dict[str, Any]]:
    """Nisi and Jev, always both and in that order: id, label, state and a fixed detail per state. A component the
    snapshot lacks, or a state this table does not know, is published as unknown, never left out."""
    rows = {c.get("id"): c for c in snapshot.get("components") or [] if isinstance(c, dict)}
    out = []
    for component_id, label, details in COMPONENTS:
        row = rows.get(component_id) or {}
        state = row.get("state") if isinstance(row.get("state"), str) and row.get("state") in details else "unknown"
        item = {"id": component_id, "label": label, "state": state, "detail": details[state]}
        if component_id == "jev":
            age = _number(row.get("lastJudgedAgeSeconds"), 0, 10 * 365 * 86400)
            item["lastJudgedAgeSeconds"] = round(age) if age is not None else None
        out.append(item)
    return out


def mac_presence(snapshot: dict | None, host_name: str, *, version: str | None = None,
                 dashboard_port: int | None = None) -> dict:
    """This Mac's status for peers, in the PC edition's presence shape (probes.peer_presence), built only from
    the observer's latest snapshot, plus this Mac's Nisi and Jev states. Model ids, states, versions and counts
    only; no paths, prompts or keys. An unavailable or incomplete inventory is refused (InventoryUnknown)
    rather than published as a complete count."""
    if not snapshot:
        raise ValueError("no snapshot yet")
    sampled = float(snapshot["sampledAt"])
    if not math.isfinite(sampled):
        raise ValueError("snapshot time is not a number")
    sources = {s.get("id"): s for s in snapshot.get("sources") or [] if isinstance(s, dict)}
    live = [sources[key] for key in ("lmstudio-api", "lms-ps") if sources.get(key, {}).get("state") == "live"]
    known = snapshot.get("activityKnown") is True
    machine = next((row for row in snapshot.get('machines', [])
                    if row.get('platform') == 'mac' and row.get('host') == 'mac'), None)
    if machine is not None and (machine.get('state') != 'up' or machine.get('modelsComplete') is not True):
        raise InventoryUnknown("Mac model inventory incomplete")
    if not known and not live:
        # The PC counts loaded models from this list whatever the health says; an empty list would read as 0 loaded.
        raise InventoryUnknown("Mac model inventory unknown (LM Studio inventory and activity both unavailable)")
    if any(source.get("modelsComplete", True) is not True for source in live):
        raise InventoryUnknown("Mac model inventory incomplete")
    rows = [row for key in ("models", "localInstanceObservations") for row in snapshot.get(key) or []
            if isinstance(row, dict) and row.get("host") == "mac"]
    groups = {}
    for row in rows:
        metadata = row.get("metadata")
        if (isinstance(metadata, dict) and isinstance(metadata.get("loadedInstances"), list)
                and metadata.get("loadedInstanceCount") != len(metadata["loadedInstances"])):
            raise InventoryUnknown("Mac model instance inventory incomplete")
        if public_id(row.get("id")) is None:
            raise InventoryUnknown("Mac model inventory contains an unpublishable identifier")
        key = row.get("modelKey")
        groups.setdefault(key if isinstance(key, str) and key else row["id"], []).append(row)
    models = []
    for key, group in groups.items():
        row = next((item for item in group if item.get("id") == key or item.get("source") == "lmstudio-api"), group[0])
        model_id = (public_id(key) if all(item.get("source") == "lms-ps" for item in group) else None) or public_id(row["id"])
        loaded = [item for item in group if item.get("loaded") is True]
        state = "loaded" if loaded else "inactive" if all(item.get("loaded") is False for item in group) else "unknown"
        if state == "unknown":
            raise InventoryUnknown("Mac model loaded state unknown")
        # Activity is claimed only from a snapshot whose activity the observer verified.
        cli = [item for item in loaded if item.get("source") == "lms-ps"]
        activity = "unknown"
        if known and loaded:
            observed = cli or loaded
            activity = next((word for word in ("generating", "busy", "queued")
                             if any(item.get("state") == word for item in observed)), "unknown")
            if activity == "unknown" and all(item.get("state") == "idle" for item in observed):
                instances = {item.get("instanceId") or item.get("id") for item in cli}
                covered = not cli or all((item.get("source") == "lms-ps" and item.get("loadedInstanceIds") is None)
                    or (isinstance(item.get("loadedInstanceIds"), list) and bool(item["loadedInstanceIds"])
                        and set(item["loadedInstanceIds"]) <= instances) for item in loaded)
                if covered:
                    activity = "idle"
        parallel = row.get("parallel")
        slots = parallel if loaded and isinstance(parallel, int) and not isinstance(parallel, bool) \
            and 1 <= parallel <= 256 else None
        aliases = []
        for item in group:
            for value in (item.get("id"), item.get("modelKey"), item.get("instanceId"), *(item.get("loadedInstanceIds") or [])):
                alias = public_id(value)
                if alias and alias != model_id and alias not in aliases:
                    aliases.append(alias)
        models.append({"id": model_id, "aliases": aliases, "host": "mac",
                       "loadedState": state, "activity": activity, "slotsBusy": None, "slotsTotal": slots})
    models_total = len(models)
    models.sort(key=lambda model: model["loadedState"] != "loaded")
    mac_rows = [row for row in rows if not row.get('representedByInstances')]
    states = {key: value.get('state') for key, value in sources.items()}
    inventory_complete = (all(row.get('inventoryComplete') is not False for row in mac_rows)
                          and any(s.get('id') == 'lmstudio-api' and s.get('state') == 'live'
                                  and s.get('route') == 'direct' for s in snapshot.get('sources') or []))
    loaded_complete = (len(models) == len(groups)
                       and all(type(row.get('loaded')) is bool and row.get('inventoryComplete') is not False
                               for row in mac_rows)
                       and (states.get('lms-ps') == 'live' or inventory_complete)
                       and sum(model['loadedState'] == 'loaded' for model in models) <= peer_link.MAX_MODELS)
    models = models[:peer_link.MAX_MODELS]
    memory = snapshot.get("memory") if isinstance(snapshot.get("memory"), dict) else {}
    port = dashboard_port if isinstance(dashboard_port, int) and not isinstance(dashboard_port, bool) \
        and 1 <= dashboard_port <= 65535 else None
    return {"schemaVersion": 1, "kind": "agiw-peer", "host": host_name[:63], "platform": "mac", "edition": "mac",
            "version": version if isinstance(version, str) else None,
            "observedAt": _iso(sampled), "observedUnix": round(sampled, 3),
            # Inventory is known here; only unverified activity lowers health.
            "health": "ok" if known else "degraded", "models": models,
            **({"modelsTotal": models_total} if inventory_complete else {}),
            "loadedListComplete": loaded_complete,
            "gpus": _mac_gpus(snapshot.get("macGpu")),
            "memory": {"level": memory.get("level") if memory.get("level") in MEMORY_LEVELS else None,
                       "availablePercent": _number(memory.get("availablePercent"), 0, 100)},
            "dashboard": {"bind": "loopback", "port": port}, "reads": list(PRESENCE_READS),
            "components": mac_components(snapshot)}


def _projection_keeps(doc: dict) -> None:
    """peer_link.project_status is the allowlist of what may leave a machine. Every value it covers must come
    through it unchanged, so the share file never contradicts the LAN link. The link now also carries version and the
    nisi/jev components; dashboard and reads reach the share only."""
    projected = peer_link.project_status(doc)

    def same(source: dict, target: dict, keys: tuple[str, ...], where: str) -> None:
        for key in keys:
            if source.get(key) is not None and target.get(key) != source[key]:
                raise ValueError(f"presence {where}{key} fails the link projection")

    same(doc, projected, ("schemaVersion", "observedAt", "kind", "host", "platform", "edition", "health",
                          "observedUnix"), "")
    if len(projected["models"]) != len(doc["models"]) or len(projected["gpus"]) != len(doc["gpus"]):
        raise ValueError("presence rows fail the link projection")
    for source, target in zip(doc["models"], projected["models"]):
        same(source, target, ("id", "loadedState", "host", "activity", "slotsBusy", "slotsTotal"), "model ")
    for source, target in zip(doc["gpus"], projected["gpus"]):
        same(source, target, ("name", "utilizationPercent", "memoryUsedMiB", "memoryTotalMiB", "temperatureC"), "gpu ")
    same(doc["memory"], projected["memory"], ("level", "availablePercent"), "memory ")


def pc_reader_accepts(doc: Any, host: str, now: float) -> bool:
    """The PC edition's own acceptance rules for a Mac presence file (probes._validate_peer + read_peers +
    collect.py's Mac link): the file name is the host, the host is new and fresh, and the platform is macos."""
    if (not isinstance(doc, dict) or type(doc.get("schemaVersion")) is not int or doc["schemaVersion"] != 1
            or doc.get("kind") != "agiw-peer"):
        return False
    observed = doc.get("observedUnix")
    if (doc.get("host") != host or not isinstance(host, str) or not PEER_HOST.match(host)
            or isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isfinite(observed)
            or observed > now + 5 or now - observed > FRESH_SECONDS or doc.get("platform") != SHARE_PLATFORM
            or not isinstance(doc.get("models"), list)):
        return False
    return all(isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"].strip()
               and m.get("loadedState") in LOADED_STATES for m in doc["models"][:32])


def share_presence(snapshot: dict | None, host: str, *, version: str | None = None,
                   dashboard_port: int | None = None, now: float | None = None) -> dict:
    """The presence file for llm-lab/status/agiw-peers/<host>.json. It must pass the LAN link's status rules
    (validate_status as host kind "mac", project_status) and the PC reader's rules, or nothing is written."""
    if not isinstance(host, str) or not PEER_HOST.match(host):
        raise ValueError("host name is not fit for a presence file")
    now = time.time() if now is None else now
    doc = mac_presence(snapshot, host, version=version, dashboard_port=dashboard_port)
    peer_link.validate_status(doc, "mac", now)
    _projection_keeps(doc)
    doc["platform"] = SHARE_PLATFORM
    if not pc_reader_accepts(doc, host, now):
        raise ValueError("presence would not pass the PC reader")
    if len(json.dumps(doc, separators=(",", ":"), allow_nan=False)) > MAX_FILE:
        raise ValueError("presence larger than 64 KB")
    return doc


def _open_dirs(root: Path, parts: tuple[str, ...], create_last: bool = False) -> int:
    """A descriptor for root/parts, each component opened with O_NOFOLLOW relative to its parent's descriptor,
    so a symlinked folder (or one swapped between check and open) is refused instead of followed."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(root), flags)
    try:
        for index, part in enumerate(parts):
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create_last or index != len(parts) - 1:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _sweep_own_temps(directory: int, host: str, now: float | None = None) -> int:
    """Remove this host's own leftover temp files (.<host>.json.<16 hex>.tmp, regular files only, older than
    TEMP_SWEEP_AGE) from agiw-peers. A dropped share or a killed process can leave them, and the PC reader counts
    every folder entry toward its scan limit. Bounded, best effort, never follows a link; returns how many went."""
    pattern = re.compile(re.escape(f".{host}.json.") + r"[0-9a-f]{16}\.tmp\Z")
    cutoff = (time.time() if now is None else now) - TEMP_SWEEP_AGE
    removed = 0
    try:
        with os.scandir(directory) as entries:
            names = [entry.name for _, entry in zip(range(TEMP_SWEEP_LIMIT), entries) if pattern.match(entry.name)]
    except OSError:
        return 0
    for name in names:
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                os.unlink(name, dir_fd=directory)
                removed += 1
        except OSError:
            continue
    return removed


def write_presence(doc: dict, share_root: Path = SHARE_ANCHOR, *, require_mount: bool | None = None) -> None:
    """Atomically replace <share>/llm-lab/status/agiw-peers/<host>.json: an exclusively created temp file in the
    same folder, then rename. llm-lab/status must already exist (a dropped mount never gets a local copy);
    only agiw-peers/ may be created. A failure leaves the previous file as it was. This host's own temp files
    left by an earlier interrupted write are swept first (_sweep_own_temps)."""
    host = doc.get("host")
    if not isinstance(host, str) or not PEER_HOST.match(host):
        raise ValueError("host name is not fit for a presence file")
    payload = json.dumps(doc, separators=(",", ":"), allow_nan=False).encode("ascii")
    if len(payload) > MAX_FILE:
        raise ValueError("presence larger than 64 KB")
    if (share_root == SHARE_ANCHOR if require_mount is None else require_mount) and not os.path.ismount(share_root):
        raise FileNotFoundError(errno.ENOENT, "SharedChami is not mounted")
    directory = _open_dirs(share_root, PEER_DIR, create_last=True)
    name = f"{host}.json"
    try:
        try:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise ValueError("presence target is not a regular file")
        _sweep_own_temps(directory, host)
        tmp = f".{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
        try:
            try:
                view = memoryview(payload)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
            # A Windows share refuses to replace a file the PC holds open at that instant; its reader closes
            # within milliseconds, so a few short retries ride it out (the PC's write_json_atomic does the same).
            for attempt in range(6):
                try:
                    os.rename(tmp, name, src_dir_fd=directory, dst_dir_fd=directory)
                    break
                except OSError as exc:
                    if attempt == 5 or exc.errno not in (errno.EACCES, errno.EPERM, errno.EBUSY):
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=directory)
            except OSError:
                pass
            raise
    finally:
        os.close(directory)


def _failure(exc: BaseException) -> tuple[str, str]:
    """(source state, detail) for a failed presence write; fixed words, never a path from the share."""
    if isinstance(exc, OSError) and exc.errno in (errno.ELOOP, errno.ENOTDIR):
        return "error", "a share folder is a symbolic link or not a folder; refused"
    if isinstance(exc, FileNotFoundError):
        return "unavailable", "SharedChami or its llm-lab/status folder is missing (share not mounted)"
    if isinstance(exc, OSError):
        return "unavailable", f"share write failed ({errno.errorcode.get(exc.errno or 0, 'I/O error')})"
    if isinstance(exc, InventoryUnknown):
        causes = {"Mac model inventory unknown (LM Studio inventory and activity both unavailable)",
                  "Mac model inventory incomplete", "Mac model instance inventory incomplete",
                  "Mac model loaded state unknown", "Mac model inventory contains an unpublishable identifier"}
        cause = str(exc) if str(exc) in causes else "Mac model inventory unknown"
        return "unavailable", cause + ", so no model count is published; the last file ages out after 30 s"
    if isinstance(exc, ValueError):
        return "error", f"presence refused: {str(exc)[:120]}"
    return "error", f"presence write failed ({type(exc).__name__})"


def link_failure_source(exc: BaseException) -> dict[str, Any]:
    """The 'agiw-peer-link' source row for a LAN peer link that did not start; fixed words, never a path or key."""
    kind = type(exc).__name__[:40]
    if isinstance(exc, OSError) and exc.errno:
        kind += f" {errno.errorcode.get(exc.errno, 'I/O error')}"
    return {"id": "agiw-peer-link", "label": "AGIW peer link (LAN)", "state": "error", "ageSeconds": None,
            "detail": f"LAN peer link not started ({kind}); the observer and the share presence keep running. "
                      "Check the peer-link state folder, then restart the monitor"}


class PresencePublisher:
    """This Mac's presence on SharedChami every PRESENCE_INTERVAL seconds, written by one worker thread.

    The sampler only hands over the latest published snapshot and never waits on the share. A write still
    running after PRESENCE_DEADLINE seconds (a hung SMB mount) is reported as stuck in the snapshot sources,
    and no second write is queued behind it. Off unless publishStatus or peerLink is on."""

    def __init__(self, host: str, *, enabled: bool = True, share_root: Path = SHARE_ANCHOR,
                 version: str | None = None, dashboard_port: int | None = None, enabled_by: str = "",
                 config_source: dict | None = None, interval: float = PRESENCE_INTERVAL,
                 deadline: float = PRESENCE_DEADLINE, require_mount: bool | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.host, self.enabled, self.share_root = host, bool(enabled), Path(share_root)
        self.version, self.dashboard_port, self.enabled_by = version, dashboard_port, enabled_by
        self.config_source, self.interval, self.deadline = config_source, interval, deadline
        self.require_mount, self.clock = require_mount, clock
        self._cond = threading.Condition()
        self._pending: dict | None = None
        self._busy_since: float | None = None
        self._next_due = -math.inf
        self._last_ok: float | None = None
        self._failure: tuple[str, str] | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._link_source: dict[str, Any] | None = None

    def link_failed(self, exc: BaseException) -> None:
        """Called when the LAN peer link could not start; its error row then shows under Sources."""
        row = link_failure_source(exc)
        with self._cond:
            self._link_source = row

    def offer(self, read_snapshot: Callable[[], dict | None]) -> bool:
        """Hand the latest snapshot to the worker when a write is due and none is running. Never blocks on I/O."""
        if not self.enabled:
            return False
        now = self.clock()
        with self._cond:
            if self._closed or now < self._next_due or self._busy_since is not None or self._pending is not None:
                return False
            self._next_due = now + self.interval
        try:
            snapshot = read_snapshot()
        except Exception:  # noqa: BLE001
            snapshot = None
        with self._cond:
            if not snapshot:
                self._failure = ("unavailable", "no published snapshot yet")
                return False
            if self._closed:
                return False
            self._pending = snapshot
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="agiw-presence", daemon=True)
                self._thread.start()
            self._cond.notify_all()
        return True

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._pending is None and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                snapshot, self._pending = self._pending, None
                self._busy_since = self.clock()
            failure = None
            try:
                doc = share_presence(snapshot, self.host, version=self.version, dashboard_port=self.dashboard_port)
                write_presence(doc, self.share_root, require_mount=self.require_mount)
            except Exception as exc:  # noqa: BLE001 - reported in sources; the sampler never sees it
                failure = _failure(exc)
            with self._cond:
                self._busy_since = None
                if failure is None:
                    self._last_ok, self._failure = self.clock(), None
                else:
                    self._failure = failure
                self._cond.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """For tests and shutdown: True once no write is pending or running."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending is None and self._busy_since is None, timeout)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def sources(self) -> list[dict[str, Any]]:
        """Source rows for the snapshot: a config error and a LAN link error (if any), then this publisher's state
        when it is on."""
        rows = [dict(self.config_source)] if self.config_source else []
        with self._cond:
            if self._link_source is not None:
                rows.append(dict(self._link_source))
        if not self.enabled:
            return rows
        now = self.clock()
        with self._cond:
            busy, last_ok, failure = self._busy_since, self._last_ok, self._failure
        target = f"agiw-peers/{self.host}.json"
        row = {"id": "agiw-presence", "label": "AGIW presence (share)"}
        if busy is not None and now - busy > self.deadline:
            row.update(state="unavailable", ageSeconds=None,
                       detail=f"Stuck: the write of {target} has not finished in {int(now - busy)} s "
                              "(PC asleep or share dropped); no new write is queued behind it")
        elif failure is not None:
            row.update(state=failure[0], ageSeconds=None,
                       detail=f"Not written: {failure[1]}; any earlier {target} is left as it was")
        elif last_ok is not None:
            row.update(state="live", ageSeconds=round(max(0.0, now - last_ok), 1),
                       detail=f"Writes {target} every {self.interval:g} s for AGIW peers (platform {SHARE_PLATFORM}, "
                              f"unsigned share file){f'; on by {self.enabled_by}' if self.enabled_by else ''}")
        else:
            row.update(state="unavailable", ageSeconds=None, detail=f"First write of {target} pending")
        return rows + [row]


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen[key] = value
    return seen


def _reject_constant(name: str):
    raise ValueError("non-finite number " + name)


_acl_api: list = []


def _fd_has_extended_acl(fd: int) -> bool:
    """macOS acl_get_fd_np on the open descriptor itself: no pathname lookup, so nothing can be swapped in
    between. True when the file carries any extended ACL entry; raises OSError when it cannot be read."""
    if not _acl_api:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libSystem.dylib", use_errno=True)
        get_fd = libc.acl_get_fd_np
        get_fd.argtypes, get_fd.restype = [ctypes.c_int, ctypes.c_int], ctypes.c_void_p
        get_entry = libc.acl_get_entry
        get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        get_entry.restype = ctypes.c_int
        free = libc.acl_free
        free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_int
        _acl_api.append((ctypes, get_fd, get_entry, free))
    ctypes, get_fd, get_entry, free = _acl_api[0]
    ctypes.set_errno(0)
    acl = get_fd(fd, 0x00000100)  # ACL_TYPE_EXTENDED
    if not acl:
        err = ctypes.get_errno()
        if err == errno.ENOENT:
            return False  # no extended ACL at all
        raise OSError(err, "acl_get_fd_np failed")
    try:
        entry = ctypes.c_void_p()
        return get_entry(acl, 0, ctypes.byref(entry)) == 0  # ACL_FIRST_ENTRY
    finally:
        free(acl)


def _config_acl_problem(fd: int) -> str | None:
    """macOS: an extended ACL can let another account write a file whose mode still reads 0600."""
    try:
        return "has an extended ACL; remove it with chmod -N" if _fd_has_extended_acl(fd) else None
    except (OSError, AttributeError):
        return "access list unreadable"


def load_config(path: Path | str | None = None) -> tuple[dict[str, bool], dict | None]:
    """({publishStatus, peerLink}, source row or None) from the AGIW config.json, as the PC reads its own.

    A regular file (never a link) owned by this user and not writable by group or others, holding a strict JSON
    object of at most 16 KB, booleans only. A missing file means both off, silently. Anything else wrong means
    both off and an 'agiw-config' error row naming the reason in the snapshot sources; the observer still starts,
    and the --publish-status / --peer-link flags still turn their options on."""
    path = CONFIG_PATH if path is None else Path(path)
    off = dict.fromkeys(CONFIG_KEYS, False)

    def bad(why: str) -> tuple[dict[str, bool], dict]:
        return dict(off), {"id": "agiw-config", "label": "AGIW config", "state": "error", "ageSeconds": None,
                           "detail": f"{path.name} ignored ({why}); publishStatus and peerLink stay off"}

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return dict(off), None
    except OSError as exc:
        return bad("symbolic link refused" if exc.errno == errno.ELOOP else "unreadable")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return bad("not a regular file")
        # The opt-ins start a listener and publish to the share, so only a file this user owns and no one else can
        # write may turn them on (the descriptor read is the file checked: no swap between check and read).
        if os.name != "nt" and info.st_uid != os.getuid():
            return bad("not owned by you")
        if os.name != "nt" and info.st_mode & 0o022:
            return bad("writable by other users; chmod 600 it")
        if sys.platform == "darwin":
            acl = _config_acl_problem(fd)
            if acl:
                return bad(acl)
        raw = os.read(fd, CONFIG_MAX_BYTES + 1)
    except OSError:
        return bad("unreadable")
    finally:
        os.close(fd)
    if len(raw) > CONFIG_MAX_BYTES:
        return bad("larger than 16 KB")
    try:
        # utf-8-sig, as the PC's probes.read_json: a file saved with a BOM (PowerShell 5.1, or copied from the PC)
        # means the same options on both editions.
        value = json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_constant,
                           object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return bad("not strict JSON")
    if not isinstance(value, dict):
        return bad("root is not an object")
    wrong = [key for key in CONFIG_KEYS if key in value and not isinstance(value[key], bool)]
    if wrong:
        return bad(f"{' and '.join(wrong)} must be true or false")
    return {key: value.get(key) is True for key in CONFIG_KEYS}, None


def link_options(publish_status: bool = False, peer_link_flag: bool = False,
                 config_path: Path | str | None = None) -> dict[str, Any]:
    """The PC's opt-in rule: a flag turns its option on whatever config.json says; config.json can only turn
    options on. On the Mac both options publish presence (mac-status.json belongs to the Mac status script);
    peerLink also starts the LAN peer link."""
    config, source = load_config(config_path)
    on = {"publishStatus": bool(publish_status) or config["publishStatus"],
          "peerLink": bool(peer_link_flag) or config["peerLink"]}
    by = [label for label, flag in (("--publish-status", publish_status), ("--peer-link", peer_link_flag),
                                    ("config.json publishStatus", config["publishStatus"]),
                                    ("config.json peerLink", config["peerLink"])) if flag]
    return {**on, "presence": on["publishStatus"] or on["peerLink"], "lanLink": on["peerLink"],
            "enabledBy": ", ".join(by), "configSource": source}
