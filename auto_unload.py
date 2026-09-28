"""Auto-unload idle Mac LM Studio models for the AGIW monitor (Louis, 2026-09-27).

Policy
  * Automatic unloading starts disabled. A private config or the app-only API must explicitly
    enable it after the router admission hold is available; idle tracking still runs while off.
  * The Nisi Inference route pair (ROUTE_PAIR) is never auto-unloaded. The config's ``protect``
    list adds to the pair; it cannot remove it.
  * Any other loaded Mac LM Studio instance (LLM or embedding) is unloaded once the monitor
    has watched it idle for ``idleMinutes`` (default 20). An LLM instance waits only
    ``tightIdleMinutes`` (default 5) once mem-guard's level has stayed tight or critical for
    TIGHT_HOLD_SECONDS of consecutive fresh samples; one spiking sample never switches it.
    Embedding instances always wait the normal threshold: their requests are sub-second, so
    the 1 Hz sampler rarely sees them busy, and they free little memory.
  * Idle is monitor-observed: the time since the last full sample in which the instance was
    not provably idle (generating/busy/processing, queued > 0, unknown state or queue, a stale
    row, an observation gap longer than OBSERVATION_GAP_SECONDS, or LM Studio's per-instance
    remainingTtlSeconds going up, which only a request does). A newly seen instance,
    including every instance at startup, counts as active now, so nothing unloads on startup.
  * Nothing unloads while the feed is stale, a router run is live/queued/unresolved, a Nisi
    call or pending marker exists, recovery is required, any route field is missing or
    reshaped (the gate fails closed), an Online Code check or another model operation is
    running (``busy_fn``), the previous auto-unload is still unconfirmed, a journal row is
    still unwritten, or the hourly limit is reached. At most one unload per tick, never the
    same instance or model key twice within COOLDOWN_SECONDS. ``route_check_fn`` re-reads the
    router immediately before the unload call.
  * Only one AutoUnloader on the machine acts: it holds an exclusive flock on
    ``auto-unload.lock`` beside the journal for its whole life. Any other instance (a second
    app, a dev server) tracks and reports but never unloads, and takes over only after the
    owner exits, re-reading the journal first so the hourly budget and cooldowns carry over.
  * Every attempt and its confirmed outcome is a journal row (0600, fsync'd, bounded); an
    unload is never attempted when the journal cannot be opened first. Every open of the
    journal, the lock and the config is non-blocking, so a FIFO planted at one of those paths
    is refused instead of stalling the sampler.

The unloader never loads anything and never runs the CLI itself: ``unload_fn(instance_id)``
is injected (the monitor's ModelControl.request("unload", ...), which re-validates the target
against its own fresh snapshot). ``tick`` and ``state`` never raise.
"""
from __future__ import annotations

import collections
import errno
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Callable, Optional

try:
    import fcntl
except ImportError:  # no flock (not macOS/Linux): this unloader never owns the lock, never unloads
    fcntl = None

ROUTE_PAIR = ("google/gemma-4-26b-a4b-qat", "qwen/qwen3.8-27b")
DEFAULT_CONFIG_PATH = Path.home() / ".config/agiw/auto-unload.json"
DEFAULT_JOURNAL_PATH = Path.home() / ".local/state/inference-monitor/auto-unload.jsonl"
LOCK_NAME = "auto-unload.lock"

# Same grammar as model_control.MODEL_ID (checked by a test); duplicated so this module
# stays import-light and testable on its own.
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}\Z")
_INSTANCE_SUFFIX = re.compile(r":[0-9]{1,4}\Z")

# Every open of a path someone else could have replaced: no symlink, no fd leak into the
# `lms` child, and never a blocking open (a FIFO fails with ENXIO or is refused by fstat).
_SAFE_OPEN = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)

FRESH_SECONDS = 3.0                # model_control.MAX_SNAPSHOT_AGE
FUTURE_TOLERANCE_SECONDS = 0.5     # model_control._target's clock-skew allowance
OBSERVATION_GAP_SECONDS = 10.0     # a longer blind spot restarts every idle clock
TIGHT_HOLD_SECONDS = 60.0          # tight/critical must hold this long before the tight threshold
COOLDOWN_SECONDS = 120.0
OUTCOME_TIMEOUT_SECONDS = 60.0     # > ModelControl UNLOAD_TIMEOUT (30) + VERIFY_TIMEOUT (12)
LOCK_RETRY_SECONDS = 5.0
HOUR_SECONDS = 3600.0
JOURNAL_CAP_BYTES = 256 * 1024
JOURNAL_SEED_BYTES = 64 * 1024
UNWRITTEN_MAX = 16
RECENT_LIMIT = 10
CONFIG_MAX_BYTES = 4096
PROTECT_MAX = 32
MESSAGE_MAX = 300

ACTIVE_STATES = frozenset({"generating", "busy", "processing"})
TIGHT_LEVELS = frozenset({"tight", "critical"})
MEMORY_LEVELS = frozenset({"ok", "watch", "tight", "critical", "unknown"})
# Allowlist: any other router status blocks every unload. 'stale' is a dead run's record that
# was never reconciled; telemetry also counts it in runCounts.unresolved, so it blocks too.
QUIET_PIPELINE = frozenset({"idle"})
RUN_COUNT_KEYS = ("running", "queued", "unresolved")
UNRESOLVED_ROUTE = "an unresolved route record exists; reconcile it"

CONFIG_KEYS = frozenset({"enabled", "idleMinutes", "tightIdleMinutes", "protect", "maxPerHour"})
CONFIG_RANGES = {"idleMinutes": (5, 240), "tightIdleMinutes": (1, 60), "maxPerHour": (1, 60)}
DEFAULT_CONFIG = {"enabled": False, "idleMinutes": 20, "tightIdleMinutes": 5,
                  "protect": list(ROUTE_PAIR), "maxPerHour": 6}

JOURNAL_KEYS = ("ts", "attemptTs", "model", "instanceId", "idleSeconds", "reason",
                "memoryLevel", "result", "message", "operationId")
ATTEMPT_RESULTS = frozenset({"requested", "refused", "error", "succeeded", "failed"})
OUTCOME_RESULTS = frozenset({"succeeded", "failed", "unconfirmed"})
REASONS = frozenset({"idle", "idle-tight-memory"})

STATE_KEYS = ("enabled", "idleMinutes", "tightIdleMinutes", "maxPerHour", "protect",
              "thresholdSeconds", "memoryLevel", "blocked", "candidates", "recent",
              "unloadsLastHour", "configError", "journalError", "tickedAt")
CANDIDATE_KEYS = ("model", "instanceId", "idleSeconds", "eta", "blocked")


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _clip(value: Any, limit: int = MESSAGE_MAX) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _strict_json(raw: bytes) -> Any:
    def distinct(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def refuse_constant(_name):
        raise ValueError("non-finite number")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=distinct,
                      parse_constant=refuse_constant)


def validate_config(value: Any) -> dict:
    """Closed reader: known keys only, exact types, bounded ranges. Missing keys take defaults.
    Raises ValueError with a short reason."""
    if not isinstance(value, dict):
        raise ValueError("config must be a JSON object")
    extra = sorted(str(key) for key in value if key not in CONFIG_KEYS)
    if extra:
        raise ValueError(f"unknown key {extra[0][:40]!r}")
    config = dict(DEFAULT_CONFIG, protect=list(DEFAULT_CONFIG["protect"]))
    if "enabled" in value:
        if type(value["enabled"]) is not bool:
            raise ValueError("enabled must be true or false")
        config["enabled"] = value["enabled"]
    for key, (low, high) in CONFIG_RANGES.items():
        if key in value:
            item = value[key]
            if type(item) is not int or not low <= item <= high:
                raise ValueError(f"{key} must be a whole number from {low} to {high}")
            config[key] = item
    if "protect" in value:
        items = value["protect"]
        if (not isinstance(items, list) or len(items) > PROTECT_MAX
                or not all(isinstance(item, str) and MODEL_ID.fullmatch(item) for item in items)):
            raise ValueError(f"protect must be a list of at most {PROTECT_MAX} exact model ids")
        config["protect"] = list(dict.fromkeys(items))
    return config


def read_config(path: Path) -> tuple:
    """(config, error). A missing file is the defaults; anything unsafe or invalid is an error
    (the caller then unloads nothing). Never follows a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | _SAFE_OPEN)
    except FileNotFoundError:
        return dict(DEFAULT_CONFIG, protect=list(DEFAULT_CONFIG["protect"])), None
    except OSError:
        return dict(DEFAULT_CONFIG, protect=list(DEFAULT_CONFIG["protect"])), \
            "config unreadable or a symlink"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("config is not a regular file")
        if info.st_uid != os.getuid():
            raise ValueError("config is not owned by this user")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError("config is writable by other users")
        if info.st_size > CONFIG_MAX_BYTES:
            raise ValueError(f"config is larger than {CONFIG_MAX_BYTES} bytes")
        raw = os.read(fd, CONFIG_MAX_BYTES + 1)
        if len(raw) > CONFIG_MAX_BYTES:
            raise ValueError(f"config is larger than {CONFIG_MAX_BYTES} bytes")
        try:
            value = _strict_json(raw)
        except (UnicodeDecodeError, ValueError):
            raise ValueError("config is not valid JSON with unique keys")
        return validate_config(value), None
    except (OSError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) else "config unreadable"
        return dict(DEFAULT_CONFIG, protect=list(DEFAULT_CONFIG["protect"])), message
    finally:
        os.close(fd)


def _config_signature(path: Path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return ("error",)
    return (info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode, info.st_uid)


def _private_dir(directory: Path) -> Optional[str]:
    """Create the state directory 0700 if missing. An existing one is never re-moded; it must be
    a real directory owned by this user and not writable by group or others."""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(directory)
    except OSError as error:
        return f"state directory unavailable ({type(error).__name__})"
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022):
        return "state directory is not a private directory owned by this user"
    return None


def _valid_journal_row(row: Any) -> bool:
    if not isinstance(row, dict) or tuple(sorted(row)) != tuple(sorted(JOURNAL_KEYS)):
        return False
    return (_finite(row["ts"]) and _finite(row["attemptTs"]) and row["attemptTs"] <= row["ts"]
            and isinstance(row["model"], str) and MODEL_ID.fullmatch(row["model"]) is not None
            and isinstance(row["instanceId"], str) and MODEL_ID.fullmatch(row["instanceId"]) is not None
            and type(row["idleSeconds"]) is int and row["idleSeconds"] >= 0
            and row["reason"] in REASONS and row["memoryLevel"] in MEMORY_LEVELS
            and row["result"] in (ATTEMPT_RESULTS | OUTCOME_RESULTS)
            and (row["message"] is None or isinstance(row["message"], str))
            and (row["operationId"] is None or isinstance(row["operationId"], str)))


def _names_embedding(value: Any) -> bool:
    return isinstance(value, str) and "embed" in value.casefold()


def _source_live(snapshot: dict, source_id: str) -> bool:
    sources = snapshot.get("sources")
    return isinstance(sources, list) and any(
        isinstance(source, dict) and source.get("id") == source_id and source.get("state") == "live"
        for source in sources)


def _memory_level(snapshot: Any, wall: float) -> str:
    """mem-guard's level from a fresh sample whose mem-guard source is live; else 'unknown'."""
    if not isinstance(snapshot, dict):
        return "unknown"
    sampled = snapshot.get("sampledAt")
    if not _finite(sampled) or not -FUTURE_TOLERANCE_SECONDS <= wall - sampled <= FRESH_SECONDS:
        return "unknown"
    if not _source_live(snapshot, "mem-guard"):
        return "unknown"
    memory = snapshot.get("memory")
    level = memory.get("level") if isinstance(memory, dict) else None
    return level if level in MEMORY_LEVELS else "unknown"


def feed_problem(snapshot: Any, wall: float) -> Optional[str]:
    """Why this snapshot cannot support an unload decision, or None when it is fresh."""
    if not isinstance(snapshot, dict):
        return "no snapshot yet"
    sampled = snapshot.get("sampledAt")
    if not _finite(sampled):
        return "snapshot has no valid sample time"
    age = wall - sampled
    if age > FRESH_SECONDS or age < -FUTURE_TOLERANCE_SECONDS:
        return "model feed is stale"
    if not _source_live(snapshot, "lms-ps"):
        return "LM Studio activity (lms ps) is not live"
    if not _source_live(snapshot, "lmstudio-api"):
        return "LM Studio inventory is not live"
    if not isinstance(snapshot.get("models"), list):
        return "model inventory is unavailable"
    return None


def router_problem(pipeline: Any, mode: Any) -> Optional[str]:
    """Why the router or a Nisi call may be using local models, or None when provably quiet.
    ``pipeline`` is telemetry's snapshot['pipeline'], ``mode`` its snapshot['onlineCodeMode'].
    Fails closed: a missing, mistyped or renamed field blocks."""
    if not isinstance(pipeline, dict):
        return "router state is unavailable"
    if pipeline.get("pendingMarkerObserved") is not False:
        return "a Nisi call is in flight or its pending marker is present"
    if pipeline.get("pendingMarkerUnreadable") is not False:
        return "the Nisi pending marker is unreadable"
    if pipeline.get("recoveryRequired") is not False:
        return "router recovery is required"
    status = pipeline.get("status")
    if status == "stale":
        return UNRESOLVED_ROUTE
    if status not in QUIET_PIPELINE:
        return f"router status is {_clip(status, 32) or 'unknown'}"
    runs = pipeline.get("pipelines")
    if not isinstance(runs, list):
        return "router run list is unreadable"
    for run in runs:
        if not isinstance(run, dict) or run.get("live") is not False or run.get("status") in ("running", "queued"):
            return "a router run is listed live"
    if not isinstance(mode, dict):
        return "Online Code Mode state is unavailable"
    if mode.get("active") is True or mode.get("state") == "processing":
        return "an Online Code Mode route is processing"
    if mode.get("active") is not False:
        return "Online Code Mode activity is unknown"
    counts = mode.get("runCounts")
    values = [counts.get(key) for key in RUN_COUNT_KEYS] if isinstance(counts, dict) else []
    if len(values) != len(RUN_COUNT_KEYS) or not all(type(value) is int and value >= 0 for value in values):
        return "Online Code Mode run counts are unavailable"
    running, queued, unresolved = values
    if running or queued:
        return "Online Code Mode has running or queued routes"
    if unresolved:
        return UNRESOLVED_ROUTE
    return None


def route_problem(snapshot: dict) -> Optional[str]:
    """router_problem plus the snapshot's activity feed and Nisi component; fails closed."""
    problem = router_problem(snapshot.get("pipeline"), snapshot.get("onlineCodeMode"))
    if problem is not None:
        return problem
    activity = snapshot.get("activity")
    runs = activity.get("runs") if isinstance(activity, dict) else None
    if not isinstance(runs, list):
        return "route activity is unavailable"
    for run in runs:
        if not isinstance(run, dict):
            return "route activity is unreadable"
        if run.get("activity") in ("running", "queued"):
            return "a route in the activity feed is running or queued"
    components = snapshot.get("components")
    if not isinstance(components, list):
        return "route components are unavailable"
    for component in components:
        if isinstance(component, dict) and component.get("id") == "nisi" and component.get("state") == "in-use":
            return "Nisi reports a call in use"
    return None


class AutoUnloader:
    """Idle tracking and bounded auto-unload; call tick(snapshot) once per full sample."""

    def __init__(self, *, unload_fn: Callable[[str], Any],
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], float] = time.time,
                 config_path: Optional[Path] = None,
                 journal_path: Optional[Path] = None,
                 lock_path: Optional[Path] = None,
                 status_fn: Optional[Callable[[], Any]] = None,
                 busy_fn: Optional[Callable[[], Any]] = None,
                 route_check_fn: Optional[Callable[[], Any]] = None,
                 journal_cap: int = JOURNAL_CAP_BYTES):
        self._unload_fn = unload_fn
        self._clock = clock
        self._now = now
        self._config_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
        self._journal_path = Path(journal_path) if journal_path is not None else DEFAULT_JOURNAL_PATH
        self._lock_path = Path(lock_path) if lock_path is not None else self._journal_path.parent / LOCK_NAME
        self._status_fn = status_fn
        self._busy_fn = busy_fn
        self._route_check_fn = route_check_fn
        self._journal_cap = max(1024, int(journal_cap))
        self._lock = threading.RLock()
        self._lock_fd: Optional[int] = None
        self._lock_tried: Optional[float] = None
        self._closed = False
        self._owner_problem: Optional[str] = "auto-unload lock not taken yet"
        self._tracks: dict = {}
        self._attempts: collections.deque = collections.deque()
        self._cooldown: dict = {}
        self._pending: Optional[dict] = None
        self._unwritten: collections.deque = collections.deque(maxlen=UNWRITTEN_MAX)
        self._recent: collections.deque = collections.deque(maxlen=RECENT_LIMIT)
        self._config = dict(DEFAULT_CONFIG, protect=list(DEFAULT_CONFIG["protect"]))
        self._config_error: Optional[str] = None
        self._config_sig: Any = ("unread",)
        self._journal_error: Optional[str] = None
        self._candidates: list = []
        self._tight_targets: set = set()
        self._tight_since: Optional[float] = None
        self._tight_seen: Optional[float] = None
        self._blocked: Optional[str] = "waiting for the first sample"
        self._threshold: Optional[int] = None
        self._memory: Optional[str] = None
        self._ticked_at: Optional[float] = None
        try:
            with self._lock:
                self._reload_config()
                # Lock first: an owner seeds from a journal no other unloader is writing.
                self._ensure_owner(reseed=False)
                self._seed_from_journal()
        except Exception as error:  # construction must not stop the monitor either
            self._journal_error = f"journal seed failed ({type(error).__name__})"

    # ------------------------------------------------------------------ public

    def tick(self, snapshot: Any) -> dict:
        """One decision per full sample. Never raises; returns state()."""
        try:
            with self._lock:
                self._tick(snapshot)
        except Exception as error:
            try:
                with self._lock:
                    self._blocked = f"internal error ({type(error).__name__}); nothing was unloaded"
            except Exception:
                pass
        return self.state()

    def state(self) -> dict:
        """The snapshot's 'autoUnload' block. Never raises."""
        try:
            with self._lock:
                wall = self._now()
                config = self._config
                recent = []
                for row in reversed(self._recent):
                    item = dict(row)
                    item["ageSeconds"] = round(max(0.0, wall - row["ts"]), 1) if _finite(wall) else None
                    recent.append(item)
                return {
                    "enabled": bool(config["enabled"]) and self._config_error is None,
                    "idleMinutes": config["idleMinutes"],
                    "tightIdleMinutes": config["tightIdleMinutes"],
                    "maxPerHour": config["maxPerHour"],
                    "protect": self._protect_list(),
                    "thresholdSeconds": self._threshold,
                    "memoryLevel": self._memory,
                    "blocked": self._blocked,
                    "candidates": [dict(item) for item in self._candidates],
                    "recent": recent,
                    "unloadsLastHour": len(self._attempts),
                    "configError": self._config_error,
                    "journalError": self._journal_error,
                    "tickedAt": self._ticked_at,
                }
        except Exception as error:
            return {"enabled": False, "idleMinutes": DEFAULT_CONFIG["idleMinutes"],
                    "tightIdleMinutes": DEFAULT_CONFIG["tightIdleMinutes"],
                    "maxPerHour": DEFAULT_CONFIG["maxPerHour"], "protect": list(ROUTE_PAIR),
                    "thresholdSeconds": None, "memoryLevel": None,
                    "blocked": f"state unavailable ({type(error).__name__})", "candidates": [],
                    "recent": [], "unloadsLastHour": 0, "configError": None,
                    "journalError": None, "tickedAt": None}

    def set_enabled(self, enabled: bool) -> dict:
        """The UI toggle: persist enabled in the config file (0600, atomic), keeping every other
        field. Refuses (ValueError) to overwrite a config that is present but invalid."""
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        with self._lock:
            self._config_sig = ("unread",)
            self._reload_config()
            if self._config_error is not None:
                raise ValueError(f"auto-unload config is invalid ({self._config_error}); fix or remove it first")
            config = dict(self._config, enabled=enabled)
            self._write_config(config)
            self._config_sig = ("unread",)
            self._reload_config()
        return self.state()

    def close(self) -> None:
        """Release the single-owner lock (monitor shutdown). This unloader never acts again.
        Waits at most a second for a tick in progress, so a stuck tick cannot stall shutdown."""
        locked = self._lock.acquire(timeout=1.0)
        try:
            self._closed = True
            fd, self._lock_fd = self._lock_fd, None
            self._owner_problem = "auto-unload is closed"
        finally:
            if locked:
                self._lock.release()
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    # ------------------------------------------------------------ single owner

    def _ensure_owner(self, reseed: bool = True) -> bool:
        """True while this unloader holds the machine-wide lock; retries every few seconds."""
        if self._lock_fd is not None:
            return True
        if self._closed:
            self._owner_problem = "auto-unload is closed"
            return False
        mono = self._clock()
        if self._lock_tried is not None and 0 <= mono - self._lock_tried < LOCK_RETRY_SECONDS:
            return False
        self._lock_tried = mono
        fd, problem = self._take_lock()
        if fd is None:
            self._owner_problem = problem
            return False
        self._lock_fd, self._owner_problem = fd, None
        if reseed:
            # The previous owner spent budget and started cooldowns this unloader never saw.
            self._attempts.clear()
            self._cooldown.clear()
            self._recent.clear()
            self._seed_from_journal()
        return True

    def _take_lock(self) -> tuple:
        if fcntl is None:
            return None, "auto-unload needs file locking (macOS or Linux)"
        problem = _private_dir(self._lock_path.parent)
        if problem is not None:
            return None, problem
        try:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | _SAFE_OPEN, 0o600)
        except OSError as error:
            return None, f"auto-unload lock cannot be opened ({type(error).__name__})"
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError("not a private regular file")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
        except (OSError, ValueError):
            os.close(fd)
            return None, "auto-unload lock is not a private regular file"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return None, "another Monitor owns auto-unload"
            return None, f"auto-unload lock failed ({type(error).__name__})"
        return fd, None

    # ---------------------------------------------------------------- config

    def _reload_config(self) -> None:
        signature = _config_signature(self._config_path)
        if signature == self._config_sig:
            return
        self._config, self._config_error = read_config(self._config_path)
        self._config_sig = signature

    def _write_config(self, config: dict) -> None:
        path = self._config_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        payload = (json.dumps({key: config[key] for key in sorted(CONFIG_KEYS)}, indent=2,
                              allow_nan=False) + "\n").encode()
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def _protect_list(self) -> list:
        return list(dict.fromkeys(list(ROUTE_PAIR) + list(self._config["protect"])))

    def _protected(self, row: dict, protect: frozenset) -> bool:
        for name in (row.get("modelKey"), row.get("id"), row.get("instanceId")):
            if isinstance(name, str):
                folded = name.casefold()
                if folded in protect or _INSTANCE_SUFFIX.sub("", folded) in protect:
                    return True
        return False

    # --------------------------------------------------------------- journal

    def _open_journal(self) -> Optional[int]:
        path = self._journal_path
        problem = _private_dir(path.parent)
        if problem is not None:
            self._journal_error = problem
            return None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _SAFE_OPEN, 0o600)
        except OSError as error:
            self._journal_error = f"journal cannot be opened ({type(error).__name__})"
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise OSError("journal is not a private regular file")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            self._journal_error = "journal is not a private regular file"
            return None
        self._journal_error = None
        return fd

    def _append(self, fd: int, rows: list, prefix: bytes = b"") -> bool:
        """Write rows to an already-open journal, fsync, close it, then bound the file."""
        written_ok = False
        try:
            data = prefix + b"".join(
                (json.dumps(row, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
                for row in rows)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            size = os.fstat(fd).st_size
            written_ok = True
        except (OSError, ValueError) as error:
            self._journal_error = f"journal write failed ({type(error).__name__})"
        finally:
            os.close(fd)
        if written_ok and size > self._journal_cap:
            self._compact()
        return written_ok

    def _record(self, row: dict, fd: Optional[int] = None) -> None:
        """Journal one row now, or keep it for a retry (unloads wait until it is written)."""
        if fd is None:
            fd = self._open_journal()
        if fd is None or not self._append(fd, [row]):
            self._unwritten.append(row)
        self._recent.append(row)

    def _flush_unwritten(self) -> None:
        if not self._unwritten:
            return
        fd = self._open_journal()
        # The leading newline ends any partial line a failed write left behind.
        if fd is not None and self._append(fd, list(self._unwritten), prefix=b"\n"):
            self._unwritten.clear()

    def _compact(self) -> None:
        """Keep the newest whole rows within half the cap (atomic replace, 0600)."""
        path = self._journal_path
        temp = path.with_name(f".{path.name}.{os.getpid()}.compact.tmp")
        try:
            fd = os.open(path, os.O_RDONLY | _SAFE_OPEN)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("journal is not a regular file")
                data = b""
                while True:
                    part = os.read(fd, 65536)
                    if not part:
                        break
                    data += part
                    if len(data) > self._journal_cap * 4:
                        data = data[-self._journal_cap * 2:]
            finally:
                os.close(fd)
            keep, budget = [], self._journal_cap // 2
            for line in reversed(data.split(b"\n")):
                if not line:
                    continue
                if len(line) + 1 > budget:
                    break
                keep.append(line)
                budget -= len(line) + 1
            payload = b"".join(line + b"\n" for line in reversed(keep))
            out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                          | getattr(os, "O_CLOEXEC", 0), 0o600)
            with os.fdopen(out, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError as error:
            self._journal_error = f"journal compaction failed ({type(error).__name__})"
            try:
                os.unlink(temp)
            except OSError:
                pass

    def _seed_from_journal(self) -> None:
        """Recent rows, the hourly count and cooldowns survive a monitor restart."""
        try:
            fd = os.open(self._journal_path, os.O_RDONLY | _SAFE_OPEN)
        except FileNotFoundError:
            return
        except OSError:
            self._journal_error = "journal is unreadable or a symlink"
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                self._journal_error = "journal is not a regular file"
                return
            os.lseek(fd, max(0, info.st_size - JOURNAL_SEED_BYTES), os.SEEK_SET)
            data = os.read(fd, JOURNAL_SEED_BYTES)
        finally:
            os.close(fd)
        lines = data.split(b"\n")
        if len(data) == JOURNAL_SEED_BYTES and len(lines) > 1:
            lines = lines[1:]  # the first line may be cut by the seek
        mono, wall = self._clock(), self._now()
        for line in lines:
            try:
                row = _strict_json(line) if line.strip() else None
            except (UnicodeDecodeError, ValueError):
                continue
            if not _valid_journal_row(row):
                continue
            self._recent.append(row)
            age = wall - row["attemptTs"]
            if row["ts"] == row["attemptTs"] and row["result"] in ATTEMPT_RESULTS and age < HOUR_SECONDS:
                # A row dated in the future (the wall clock stepped back) happened at an unknown
                # earlier moment: it counts as now, so it can only make the limits stricter.
                at = mono - max(0.0, age)
                self._attempts.append(at)
                for key in (row["instanceId"], "key:" + row["model"]):
                    self._cooldown[key] = max(self._cooldown.get(key, at), at)
        ordered = sorted(self._attempts)
        self._attempts = collections.deque(ordered)

    # ------------------------------------------------------------------ tick

    def _tick(self, snapshot: Any) -> None:
        mono, wall = self._clock(), self._now()
        self._ticked_at = round(wall, 3) if _finite(wall) else None
        self._reload_config()
        owner = self._ensure_owner()
        if owner:
            self._follow_pending(mono, wall)
            self._flush_unwritten()
        while self._attempts and self._attempts[0] <= mono - HOUR_SECONDS:
            self._attempts.popleft()
        config = self._config
        memory = _memory_level(snapshot, wall)
        self._memory = memory
        normal = config["idleMinutes"] * 60
        threshold = (min(config["tightIdleMinutes"] * 60, normal) if self._tight_held(memory, mono)
                     else normal)
        self._threshold = threshold
        problem = feed_problem(snapshot, wall)
        if problem is None:
            self._candidates = self._observe(snapshot, mono, threshold, normal)
        else:
            self._candidates = []
        blocked = (f"config invalid: {self._config_error}" if self._config_error is not None
                   else "auto-unload is off" if not config["enabled"]
                   else self._owner_problem if not owner
                   else problem or route_problem(snapshot)
                   or ("waiting for the previous auto-unload to confirm" if self._pending is not None else None)
                   or ("journal write failed; retrying it before any further unload" if self._unwritten else None)
                   or self._busy()
                   or (f"hourly limit reached ({len(self._attempts)}/{config['maxPerHour']})"
                       if len(self._attempts) >= config["maxPerHour"] else None))
        self._blocked = blocked
        if blocked is not None:
            return
        ready = [item for item in self._candidates if item["blocked"] is None and item["eta"] == 0]
        if not ready:
            return
        target = max(ready, key=lambda item: (item["idleSeconds"], item["instanceId"]))
        self._attempt(target, mono, wall, memory)

    def _tight_held(self, memory: str, mono: float) -> bool:
        """True once tight/critical has held over consecutive fresh samples for TIGHT_HOLD_SECONDS.
        mem-guard's level is a raw per-sample classification; its hysteresis only drives pausing."""
        if memory not in TIGHT_LEVELS:
            self._tight_since = self._tight_seen = None
            return False
        if self._tight_since is None or mono - self._tight_seen > OBSERVATION_GAP_SECONDS:
            self._tight_since = mono
        self._tight_seen = mono
        return mono - self._tight_since >= TIGHT_HOLD_SECONDS

    def _busy(self) -> Optional[str]:
        if self._busy_fn is None:
            return None
        try:
            busy = self._busy_fn()
        except Exception:
            return "the monitor's busy check failed"
        if not busy:
            return None
        return _clip(busy, 120) if isinstance(busy, str) else "another monitor operation is running"

    @staticmethod
    def _inventory(rows: list) -> tuple:
        """Per model key: the Mac loadedInstanceIds lists, LM Studio's per-instance
        remainingTtlSeconds, and whether the API calls the model an embedding model.
        telemetry merges the same-id lms-ps row over the API row and copies the API metadata
        onto it, so the metadata is read from any Mac row that carries the key."""
        lists: dict = collections.defaultdict(list)
        ttl: dict = {}
        embedding: set = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("host") != "mac":
                continue
            key = row.get("modelKey")
            if not isinstance(key, str):
                continue
            if isinstance(row.get("loadedInstanceIds"), list):
                lists[key].append(row["loadedInstanceIds"])
            metadata = row.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if _names_embedding(metadata.get("type")):  # the UI's rule (map-layout.mjs /embed/)
                embedding.add(key)
            instances = metadata.get("loadedInstances")
            for item in instances if isinstance(instances, list) else []:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    value = item.get("remainingTtlSeconds")
                    ttl.setdefault((key, item["id"]), value if type(value) is int and value >= 0 else None)
        return lists, ttl, embedding

    def _observe(self, snapshot: dict, mono: float, threshold: int, normal: int) -> list:
        """Update idle clocks from one fresh sample; return the unprotected candidates."""
        rows = snapshot["models"]
        protect = frozenset(name.casefold() for name in self._protect_list())
        lists, ttls, embedding_keys = self._inventory(rows)
        seen = set()
        candidates = []
        tight_targets = set()
        for row in rows:
            if not (isinstance(row, dict) and row.get("host") == "mac"
                    and row.get("source") == "lms-ps" and row.get("loaded") is True):
                continue
            instance = row.get("id")
            if not isinstance(instance, str) or not MODEL_ID.fullmatch(instance) or instance in seen:
                continue
            seen.add(instance)
            key = row.get("modelKey")
            key_ok = isinstance(key, str) and MODEL_ID.fullmatch(key) is not None
            age = row.get("ageSeconds")
            state = row.get("state")
            queued = row.get("queued")
            known_idle = (_finite(age) and 0 <= age <= FRESH_SECONDS and state == "idle"
                          and type(queued) is int and queued == 0)
            track = self._tracks.get(instance)
            if track is None:
                # Startup and newly loaded instances: active now, never unloaded on sight.
                track = {"lastActive": mono, "lastSeen": mono, "ttl": None}
                self._tracks[instance] = track
            # A request resets LM Studio's idle TTL: evidence of use the 1 Hz sampler can miss.
            ttl = ttls.get((key, instance)) if key_ok else None
            refreshed = type(ttl) is int and type(track["ttl"]) is int and ttl > track["ttl"]
            track["ttl"] = ttl
            if not known_idle or refreshed or mono - track["lastSeen"] > OBSERVATION_GAP_SECONDS:
                track["lastActive"] = mono
            track["lastSeen"] = mono
            if self._protected(row, protect):
                continue
            embedding = (key_ok and (key in embedding_keys or _names_embedding(key))) or _names_embedding(instance)
            limit = normal if embedding else threshold
            if limit < normal:
                tight_targets.add(instance)
            idle = max(0.0, mono - track["lastActive"])
            if known_idle:
                reason = None
            elif state in ACTIVE_STATES or (type(queued) is int and queued > 0):
                reason = "busy"
            else:
                reason = "activity or queue state unknown"
            if reason is None and row.get("instanceId") != instance:
                reason = "loaded-instance id unknown"
            if reason is None and not key_ok:
                reason = "model key unknown"
            if reason is None and not self._instance_confirmed(lists, key, instance):
                reason = "instance not confirmed by LM Studio inventory"
            if reason is None and any(
                    mono - self._cooldown.get(name, -math.inf) < COOLDOWN_SECONDS
                    for name in (instance, "key:" + key)):
                reason = "auto-unloaded less than 2 min ago"
            candidates.append({"model": key if key_ok else instance, "instanceId": instance,
                               "idleSeconds": int(idle), "eta": int(math.ceil(max(0.0, limit - idle))),
                               "blocked": reason})
        for instance in list(self._tracks):
            if instance not in seen:  # lms ps is live, so absence means not loaded
                del self._tracks[instance]
        self._tight_targets = tight_targets
        candidates.sort(key=lambda item: (-item["idleSeconds"], item["instanceId"]))
        return candidates

    @staticmethod
    def _instance_confirmed(lists: dict, key: str, instance: str) -> bool:
        """Mirror model_control._target: exactly one Mac instance list for the key contains it."""
        found = lists.get(key, [])
        return len(found) == 1 and instance in found[0]

    def _fresh_route_problem(self) -> Optional[str]:
        """Re-read the router right before the unload call (the sample's view is up to ~1 s old)."""
        if self._route_check_fn is None:
            return None
        try:
            fresh = self._route_check_fn()
            if not isinstance(fresh, dict):
                return "the fresh router check returned nothing usable"
            return router_problem(fresh.get("pipeline"), fresh.get("onlineCodeMode"))
        except Exception:
            return "the fresh router check failed"

    def _attempt(self, target: dict, mono: float, wall: float, memory: str) -> None:
        fd = self._open_journal()
        if fd is None:
            self._blocked = "journal unavailable; nothing is unloaded without a journal row"
            return
        problem = self._fresh_route_problem()
        if problem is not None:
            os.close(fd)
            self._blocked = f"fresh router check: {problem}"
            return
        stamp = round(wall, 3)
        row = {"ts": stamp, "attemptTs": stamp, "model": target["model"],
               "instanceId": target["instanceId"], "idleSeconds": target["idleSeconds"],
               "reason": "idle-tight-memory" if target["instanceId"] in self._tight_targets else "idle",
               "memoryLevel": memory, "result": "error", "message": None, "operationId": None}
        # Counted before the call: a failing unload still spends the hourly budget and cooldown.
        self._attempts.append(mono)
        self._cooldown[target["instanceId"]] = mono
        self._cooldown["key:" + target["model"]] = mono
        try:
            try:
                outcome = self._unload_fn(target["instanceId"])
            except Exception as error:
                status, message = getattr(error, "status", None), getattr(error, "message", None)
                if type(status) is int and isinstance(message, str):
                    row.update(result="refused", message=_clip(f"{status}: {message}"))
                else:
                    row.update(result="error", message=f"unload call failed ({type(error).__name__})")
            else:
                row.update(self._accepted(outcome))
        finally:
            self._record(row, fd)
        if row["result"] == "requested" and row["operationId"] and self._status_fn is not None:
            self._pending = {"row": dict(row), "since": mono}

    @staticmethod
    def _accepted(outcome: Any) -> dict:
        if not isinstance(outcome, dict):
            return {"result": "requested", "message": None, "operationId": None}
        op = outcome.get("operationId")
        op = op if isinstance(op, str) and 0 < len(op) <= 64 else None
        status = outcome.get("status")
        return {"result": status if status in ("succeeded", "failed") else "requested",
                "message": _clip(outcome.get("message")), "operationId": op}

    def _follow_pending(self, mono: float, wall: float) -> None:
        pending = self._pending
        if pending is None:
            return
        try:
            status = self._status_fn() if self._status_fn is not None else None
        except Exception:
            status = None
        op = pending["row"]["operationId"]
        current = status.get("operationId") if isinstance(status, dict) else None
        if current == op and status.get("status") in ("succeeded", "failed"):
            result, message = status["status"], _clip(status.get("message"))
        elif isinstance(current, str) and current != op:
            result, message = "unconfirmed", "another model operation replaced it before its result was read"
        elif mono - pending["since"] > OUTCOME_TIMEOUT_SECONDS:
            result, message = "unconfirmed", "no confirmed result within 60 s"
        else:
            return
        self._pending = None
        row = dict(pending["row"], ts=max(round(wall, 3), pending["row"]["attemptTs"]),
                   result=result, message=message)
        self._record(row)
