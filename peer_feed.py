"""The PC's live state for the Mac edition: the LAN peer link first, the SharedChami status files second.
Also this Mac's side of the PC edition's share link: an opt-in presence file in llm-lab/status/agiw-peers/.

Reading is read-only. Nothing here starts, loads or controls anything on either machine. A feed is shown as
live only when its own observation time is fresh; otherwise the Windows source says why it is not. The only
write is this Mac's own presence file (and removing this host's own leftover temp files beside it), and only
when publishStatus or peerLink is turned on.
"""
from __future__ import annotations

import errno
import json
import math
import os
import plistlib
import re
import secrets
import socket
import stat
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
MEMORY_LEVELS = ("ok", "watch", "tight", "critical", "unknown")
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
    """Neither LM Studio source answered, so the Mac's model list is unknown, not empty."""

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


def _summary(doc: dict, via: str, now: float) -> dict[str, Any]:
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
    level = memory.get("level") if memory.get("level") in ("ok", "watch", "warn", "critical", "unknown") else None
    free = _number(memory.get("availablePercent"), 0, 100)
    return {"via": via, "host": _text(doc.get("host"), 63) or "", "health": doc.get("health") if doc.get("health") in
            ("ok", "degraded", "down") else None, "observedAt": doc["observedAt"],
            "ageSeconds": round(max(0.0, age), 1), "models": models, "gpus": gpus,
            "memory": {k: v for k, v in (("level", level), ("availablePercent", free)) if v is not None} or None}


def from_peer_link(now: float, state_dir: Path | None = None, port: int = peer_link.STATUS_PORT) -> dict | None:
    """The connected, paired Windows peer from the local peer-link daemon, or None."""
    state_dir = state_dir or peer_link.default_state_dir()
    if not (state_dir / "control-token").exists():
        return None  # no peer link set up on this Mac; never create one from the observer
    # Mutually authenticated: the listener proves it holds the token before anything is sent.
    status, listing = peer_link.control_request(state_dir, port, "GET", "/control/peers",
                                                deadline=CONTROL_DEADLINE)
    if status != 200 or not isinstance(listing, dict):
        return None
    for row in listing.get("peers", []):
        if row.get("paired") and row.get("host") == "windows" and row.get("state") == "connected" and row.get("status"):
            doc = peer_link.validate_status(row["status"], "windows", time.time())
            summary = _summary(doc, "lan", now)
            return summary if summary["ageSeconds"] <= FRESH_SECONDS else None
    return None


SHARE_ANCHOR = Path("/Volumes/SharedChami")


def _read_bounded(path: Path, anchor: Path | None = None) -> bytes:
    """Open every component below the share root with O_NOFOLLOW relative to its parent's descriptor,
    so neither a symlinked folder nor a folder swapped between check and open can redirect the read."""
    anchor = anchor or (SHARE_ANCHOR if SHARE_ANCHOR in path.parents else path.parents[len(path.parents) - 2])
    parts = path.relative_to(anchor).parts
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(str(anchor), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0), dir_fd=directory)
    finally:
        os.close(directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("status record is not a file")
        data = os.read(fd, MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError("status record too large")
        return data
    finally:
        os.close(fd)


def from_share(now: float, root: Path = SHARE_STATUS, anchor: Path | None = None) -> dict | None:
    """The freshest Windows presence file, then windows-status.json; None when nothing is fresh."""
    candidates = []
    peers_dir = root / "agiw-peers"
    try:
        names = sorted(os.listdir(peers_dir))[:32]
    except OSError:
        names = []
    candidates += [peers_dir / n for n in names if n.endswith(".json") and not n.startswith(".")]
    candidates.append(root / "windows-status.json")
    best = None
    for path in candidates:
        try:
            doc = peer_link.validate_status(peer_link.strict_json(_read_bounded(path, anchor)), "windows", time.time())
            summary = _summary(doc, "share", now)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if summary["ageSeconds"] <= FRESH_SECONDS and (best is None or summary["ageSeconds"] < best["ageSeconds"]):
            best = summary
    return best


class _ShareReader:
    """SMB reads can hang when the PC sleeps. One reader thread at a time, with a deadline; a stuck
    reader is reported instead of piling up new threads behind it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.result: Any = None

    def read(self, now: float) -> tuple[str, dict | None]:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return "stuck", None
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
            return "stuck", None
        return ("ok", box.get("value")) if "error" not in box else ("error", None)


_share_reader = _ShareReader()


def windows_feed(now: float) -> tuple[dict | None, dict]:
    """(feed, source row). LAN beats share; both must be fresh."""
    lan_note = None
    try:
        feed = from_peer_link(now)
        if feed:
            return feed, {"id": "windows-telemetry", "label": "Windows", "state": "live", "ageSeconds": feed["ageSeconds"],
                          "detail": f"Linked over the LAN to {feed['host'] or 'the PC'} (paired, signed)"}
    except Exception as exc:  # noqa: BLE001 - the share fallback still runs
        lan_note = type(exc).__name__
    outcome, feed = _share_reader.read(now)
    if feed:
        return feed, {"id": "windows-telemetry", "label": "Windows", "state": "live", "ageSeconds": feed["ageSeconds"],
                      "detail": f"Reported via SharedChami status files from {feed['host'] or 'the PC'} "
                                "(unsigned, not verified; LAN link not connected)"}
    why = {"stuck": "share read did not finish (PC asleep or share dropped)", "error": "share status unreadable",
           "ok": "no fresh PC status on the share"}[outcome]
    return None, {"id": "windows-telemetry", "label": "Windows", "state": "unavailable", "ageSeconds": None,
                  "detail": f"Not linked: peer link not connected{f' ({lan_note})' if lan_note else ''}; {why}"}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z"


def public_id(value: Any) -> str | None:
    """A model identifier fit to publish on the share (the PC's probes.public_id): path-shaped values keep
    only their final name."""
    if not isinstance(value, str) or not value or not value.isprintable():
        return None
    if "\\" in value or ":/" in value or ":\\" in value or value.startswith("/"):
        value = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return value[:120] or None


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
    only; no paths, prompts or keys. When neither LM Studio source answered, the model list is unknown and the
    document is refused (InventoryUnknown) rather than published as zero models."""
    if not snapshot:
        raise ValueError("no snapshot yet")
    sampled = float(snapshot["sampledAt"])
    if not math.isfinite(sampled):
        raise ValueError("snapshot time is not a number")
    states = {s.get("id"): s.get("state") for s in snapshot.get("sources") or [] if isinstance(s, dict)}
    known = snapshot.get("activityKnown") is True
    if not known and states.get("lmstudio-api") != "live" and states.get("lms-ps") != "live":
        # The PC counts loaded models from this list whatever the health says; an empty list would read as 0 loaded.
        raise InventoryUnknown("Mac model inventory unknown (LM Studio inventory and activity both unavailable)")
    models = []
    for row in snapshot.get("models") or []:
        model_id = public_id(row.get("id")) if isinstance(row, dict) and row.get("host") == "mac" else None
        if model_id is None:
            continue
        loaded = row.get("loaded")
        state = "loaded" if loaded is True else "inactive" if loaded is False else "unknown"
        # Activity is claimed only from a snapshot whose activity the observer verified.
        activity = row.get("state") if known and row.get("state") in ("idle", "generating", "busy", "queued") else "unknown"
        parallel = row.get("parallel")
        slots = parallel if loaded is True and isinstance(parallel, int) and not isinstance(parallel, bool) \
            and 1 <= parallel <= 256 else None
        alias = public_id(row.get("modelKey"))
        models.append({"id": model_id, "aliases": [alias] if alias and alias != model_id else [], "host": "mac",
                       "loadedState": state, "activity": activity, "slotsBusy": None, "slotsTotal": slots})
        if len(models) >= peer_link.MAX_MODELS:
            break
    memory = snapshot.get("memory") if isinstance(snapshot.get("memory"), dict) else {}
    port = dashboard_port if isinstance(dashboard_port, int) and not isinstance(dashboard_port, bool) \
        and 1 <= dashboard_port <= 65535 else None
    return {"schemaVersion": 1, "kind": "agiw-peer", "host": host_name[:63], "platform": "mac", "edition": "mac",
            "version": version if isinstance(version, str) else None,
            "observedAt": _iso(sampled), "observedUnix": round(sampled, 3),
            # Inventory is known here; only unverified activity lowers health.
            "health": "ok" if known else "degraded", "models": models,
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
    if not isinstance(doc, dict) or doc.get("schemaVersion") != 1 or doc.get("kind") != "agiw-peer":
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
        return "unavailable", ("Mac model inventory unknown (LM Studio inventory and activity both unavailable), "
                               "so no model count is published; the last file ages out after 30 s")
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


def load_config(path: Path | str | None = None) -> tuple[dict[str, bool], dict | None]:
    """({publishStatus, peerLink}, source row or None) from the AGIW config.json, as the PC reads its own.

    Strict JSON object, at most 16 KB, booleans only. A missing file means both off, silently. Anything else
    wrong means both off and an 'agiw-config' error row in the snapshot sources; the observer still starts."""
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
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return bad("not a regular file")
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
