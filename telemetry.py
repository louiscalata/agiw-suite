"""Bounded, passive status sampling for the real-time inference visualizer."""
from __future__ import annotations

import datetime as _dt
import errno
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from nisi_v02 import collect_nisi_v02

# Router-concurrency P2 (spec 6.12, R2.9): these readers dual-read the legacy single-run
# journal (active.json + owner.lock held EX for the whole run) and the per-run layout
# (active/<runId>.json with runLock, locks/<runId>.lock, notes/, policy.json, the install
# fence).  install.py refuses a router generation until this assignment and the R2.9
# contract tests are present and the owner accepted exactly this build.
ROUTER_READER_CONTRACT = "codemode.router.readers.v2"

_API_HOST = "127.0.0.1"
_API_PORT = 1234
_API_PATH = "/api/v1/models"
_MAX_HTTP = 2 * 1024 * 1024
_MAX_CLI = 512 * 1024
# Router checkpoints may include a bounded candidate envelope. Match the
# router's 3 MiB active-record ceiling so an in-flight route remains observable.
_MAX_STATE = 3 * 1024 * 1024
_MAX_AGE = 300.0
_HOME = Path.home()
_AFM_EXECUTABLE = _HOME / "bin/afm"
_ACTIVE_PATH = _HOME / ".local/state/codemode-router/active.json"
_ROUTER_ROOT = _HOME / ".local/state/codemode-router"
_ROUTER_SCRIPT = _HOME / ".codex/skills/local-llm-orchestrator/scripts/pipeline_router.py"
_LAUNCHER_ROOT = _HOME / ".local/state/codemode-launcher"
_READINESS_PATH = _LAUNCHER_ROOT / "readiness.json"
_READINESS_MAX_AGE = 600.0
_PENDING_PATH = _HOME / ".local/state/codemode-nisi/pending.json"
_JEV_ENV_PATH = _HOME / ".config/chami-intake/typesafe.env"
_DEFAULT_ROUTE_MODEL = "google/gemma-4-26b-a4b-qat"
_CANARY_ROOT = _HOME / ".local/state/nisi-canary"
_CANARY_PATH = _CANARY_ROOT / "state.json"
_WINDOWS_WORKER_CMD = _HOME / "bin/chami-dispatch"
_SHAREDCHAMI_ENSURE = _HOME / "bin/chami-ensure"
_WINDOWS_WORKER_SCOPE = "heartbeat_inventory_only_not_inference_or_native_parity"
_WINDOWS_WORKER_MAX_AGE = 60.0
_WINDOWS_WORKER_POLL_SECONDS = 10.0
_WINDOWS_WORKER_FAILURE_BACKOFF_SECONDS = 60.0
# The dispatcher's own SMB reader has a three-second limit. Give it time to
# return while keeping this optional probe off the one-second sampling path.
_WINDOWS_WORKER_COMMAND_TIMEOUT = 4.5
# -inf, not 0.0: Python 3.9 on macOS starts time.monotonic() near zero per
# process, which delayed the first heartbeat probe by a full poll interval.
_WINDOWS_WORKER_CACHE: dict[str, Any] = {"checkedMonotonic": float("-inf"), "heartbeatUnix": None,
                                         "modelsAdvertised": [], "modelCount": 0,
                                         "probeRunning": False, "probeError": False,
                                         "probePaused": False, "probeBusy": False,
                                         "retryAfterMonotonic": 0.0}
_WINDOWS_WORKER_LOCK = threading.Lock()
# Serialize every in-process SharedChami owner read. The process-state guard
# below detects a reader only after it has entered macOS U state; this gate
# closes the window where passive sampling and an explicit button check could
# start overlapping SMB reads first.
_WINDOWS_WORKER_IO_LOCK = threading.Lock()
_WINDOWS_WORKER_CANCEL = threading.Event()
# Set while an explicit Fix owns SharedChami: passive heartbeat reads must not
# contend with its queue I/O, its share recovery or its end-to-end probe.
_WINDOWS_WORKER_HOLD = threading.Event()
_WINDOWS_WORKER_LAST_STUCK: int | None = None
# Fresh heartbeat, but the worker itself reports it cannot serve a model.
_WINDOWS_WORKER_CONDITIONS = {"worker status degraded": "degraded", "worker is not running": "stopped"}
# Readers already stuck when a Fix verified a successful queue read. They do not
# hold the queue, so they stop pausing reads; any new stuck reader, or an
# unknown count, still does. The baseline only ever lowers as they exit.
_WINDOWS_WORKER_TOLERATED = 0
_WINDOWS_WORKER_TOLERANCE_LOCK = threading.Lock()
# The dispatcher's local, content-free job journal on the Mac disk. Reading it
# never touches SMB, so Windows job activity stays visible while the share is
# wedged and the heartbeat probe is paused.
_WINDOWS_JOBS_PATH = _HOME / ".local/state/chami-dispatch/jobs.jsonl"
_WINDOWS_JOBS_MAX = 256 * 1024
_WINDOWS_JOBS_RECENT_SECONDS = 1800.0
_WINDOWS_JOBS_EVENTS = frozenset({"enqueued", "result", "unresolved",
                                  "publish-uncertain", "invalid-result", "cancel-requested"})
# Result statuses that settle a job. 'cancelled' (worker 1.2) means the worker stopped, or never
# started, the job on a cancel request; the dispatcher relays nothing else from such a result.
_WINDOWS_JOB_RESULTS = ("success", "error", "cancelled")
_WINDOWS_JOB_ID = re.compile(r"mac-[A-Za-z0-9-]{12,80}\Z")
_WINDOWS_JOB_EVIDENCE = ("client", "lane", "predictedPerSecond", "completionTokens",
                         "promptPerSecond", "promptTokens", "flags")
# Result flags the dispatcher journals (reply_flags); anything else is dropped.
_WINDOWS_JOB_FLAGS = ("hit-token-limit",)
# Published as windowsWorker.lanesError when a fresh heartbeat's lane detail is rejected.
_WINDOWS_LANES_MALFORMED = "malformed lane detail"
# Worker 1.2 heartbeat detail that chami-dispatch relays (validate_state): the worker's version and its
# NVIDIA GPUs. Checked again here with the dispatcher's own bounds (GPU_LIMITS / MAX_GPUS); any fault
# drops the whole GPU list, and a worker without a GPU sample sends null.
_WINDOWS_WORKER_VERSION = re.compile(r"[0-9A-Za-z._+-]{1,16}\Z")
_WINDOWS_GPU_MAX = 4
_WINDOWS_GPU_NAME = re.compile(r"[\x20-\x7e]{1,64}\Z")
_WINDOWS_GPU_LIMITS = (("utilizationPercent", 0, 100), ("memoryUsedMiB", 0, 1048576),
                       ("memoryTotalMiB", 1, 1048576), ("temperatureC", 0, 150), ("powerW", 0, 2000))
# Fallback for journals written before the dispatcher recorded the lane at enqueue.
_WINDOWS_MODEL_LANES = {"gpt-oss-20b": "fast", "openai/gpt-oss-20b": "fast",
                        "Qwen3.8-27B Q4_K_M": "deep", "qwen3.8-27b": "deep"}
# The dispatcher's worker lanes under the monitor's names, and the calling
# agent's label as the dispatcher validates it (never trusted, checked again).
_WINDOWS_LANE_NAMES = {"amd": "fast", "bionic": "deep"}
_WINDOWS_CLIENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}\Z", re.I)
# pc-llm's headless switch; only pc-llm writes it (the monitor asks it to).
_WINDOWS_HEADLESS_PATH = _HOME / ".local/state/codemode-online/mode.json"
_WINDOWS_HEADLESS_KIND = "codemode.online-mode.v1"
_WINDOWS_WORKER_THREAD: threading.Thread | None = None
_MAX_CANARY_STATE = 16 * 1024 * 1024  # Matches the durable ledger's file bound.
_CANARY_STATES = ("READY", "CLAIMED", "SENT", "RECEIVED", "VERIFIED", "HELD")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
_STAGE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_STEPS = (
    ("intake", "Intake"), ("preflight", "Preflight"),
    ("backend_draft", "Author"), ("backend_answer", "Author"),
    ("backend_review", "Review"), ("validation", "Validation"),
    ("checks", "Checks"), ("mac_return", "Return"),
    ("final_validation", "Final validation"),
)


_MAX_EXACT_INT = (1 << 53) - 1


def _afm_status(path: Path = _AFM_EXECUTABLE) -> dict[str, Any]:
    """Observe this Mac's AFM adapter file; never execute or infer from it."""
    result = {"schemaVersion": 1, "host": "mac", "state": "unknown",
              "callability": "unknown", "inference": "NOT_TESTED",
              "role": "optional-advisory-intake", "source": "local-executable-metadata"}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {**result, "state": "missing", "callability": "not-installed"}
    except OSError:
        return result
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return {**result, "state": "untrusted", "callability": "not-accepted"}
    if not os.access(path, os.X_OK):
        return {**result, "state": "not-executable", "callability": "permission-denied"}
    return {**result, "state": "executable", "callability": "permission-granted"}


def _finite(value: Any) -> bool:
    """A JSON number that is a finite float, or an int within a float's exact range.

    JSON allows integers of any length, and ``math.isfinite(10**400)`` raises OverflowError
    instead of answering, so one oversized number in a record must never reach it: such a
    value is simply not a usable number (the row or file that carries it is invalid)."""
    if type(value) is float:
        return math.isfinite(value)
    return type(value) is int and -_MAX_EXACT_INT <= value <= _MAX_EXACT_INT


def _age(value: Any, now: float) -> float | None:
    if not _finite(value):
        return None
    result = max(0.0, now - float(value))
    return round(result, 3)


def _json_object(raw: bytes, limit: int) -> dict[str, Any]:
    if len(raw) > limit:
        raise ValueError("record too large")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("bad number")))
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


class _RecordChanged(ValueError):
    """A record was replaced, relinked or rewritten while it was read (retry, never trust)."""


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _safe_file(path: Path, limit: int) -> dict[str, Any]:
    """Read a fixed local record with no symlink following or lock acquisition.

    One observation (router invariant I9): lstat, open without following links, fstat
    equal, read, then fstat and lstat again.  A record at ``nlink`` 0 or 2 (being
    replaced, or published by link) or any identity change is ``_RecordChanged``.
    """
    before = path.lstat()
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsafe file")
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino) or info.st_nlink in (0, 2):
            raise _RecordChanged("file changed")
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise ValueError("not a regular file")
        chunks: list[bytes] = []
        size = 0
        while size <= limit:
            part = os.read(fd, min(65536, limit + 1 - size))
            if not part:
                break
            chunks.append(part)
            size += len(part)
        after = os.fstat(fd)
        try:
            current = path.lstat()
        except FileNotFoundError:
            raise _RecordChanged("file removed during observation") from None
        if not (_file_identity(before) == _file_identity(after) == _file_identity(current)):
            raise _RecordChanged("file changed during observation")
        return _json_object(b"".join(chunks), limit)
    finally:
        os.close(fd)


def _router_namespace_initialized() -> bool:
    """Recognize only an initialized private router journal; never create/lock it."""
    try:
        for directory in (_ROUTER_ROOT, _ROUTER_ROOT / "archive", _ROUTER_ROOT / "checkpoints"):
            info = directory.lstat()
            if (directory.is_symlink() or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                return False
        lock_info = (_ROUTER_ROOT / "owner.lock").lstat()
        return (stat.S_ISREG(lock_info.st_mode) and lock_info.st_uid == os.getuid()
                and not stat.S_IMODE(lock_info.st_mode) & 0o077 and lock_info.st_nlink == 1
                and not (_ROUTER_ROOT / "owner.lock").is_symlink())
    except OSError:
        return False


def _bounded_command(arguments: list[str], *, limit: int = 8192,
                     timeout: float = 0.5,
                     cancel: threading.Event | None = None,
                     ok_codes: tuple[int, ...] = (0,)) -> str:
    """Read only bounded process metadata; never invoke a shell or router command."""
    if cancel is not None and cancel.is_set():
        raise TimeoutError("process metadata cancelled")
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               close_fds=True, start_new_session=True)
    output = bytearray()
    selector = None
    deadline = time.monotonic() + timeout
    completed = False
    try:
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            if cancel is not None and cancel.is_set():
                raise TimeoutError("process metadata cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("process metadata timed out")
            events = selector.select(min(remaining, 0.05))
            if not events:
                continue
            part = os.read(process.stdout.fileno(), min(4096, limit + 1 - len(output)))
            if not part:
                break
            output.extend(part)
            if len(output) > limit:
                raise ValueError("process metadata too large")
        while True:
            if cancel is not None and cancel.is_set():
                raise TimeoutError("process metadata cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("process metadata timed out")
            try:
                exit_code = process.wait(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if exit_code not in ok_codes:
            raise OSError("process metadata unavailable")
        result = output.decode("utf-8")
        completed = True
        return result
    finally:
        if not completed:
            # The dispatcher starts a nested SMB reader. Killing only its CLI
            # parent on a timeout leaves that reader behind on a stuck mount.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        if selector is not None:
            selector.close()
        if process.stdout is not None:
            process.stdout.close()


# --- Router run readers: both journal layouts (router-concurrency spec 6.12, R2.9) ---------
#
# Legacy layout (the router installed today): one ``active.json`` and ``owner.lock`` held EX
# by the one work process for its whole life.  Per-run layout (router P3): ``active/<id>.json``
# with ``runLock: {dev, ino}``, ``locks/<id>.lock`` (L1) held EX by that run, advisory
# ``notes/<id>.json``, ``policy.json`` and ``install-fence.json``.  Everything below reads
# closed, bounded records and never creates, locks for writing or changes anything.  The only
# locks it takes are momentary ``LOCK_SH|LOCK_NB`` probes on a fresh open file description,
# released at once (and, under an installed per-run router with policy single, one
# ``LOCK_EX|LOCK_NB`` drain probe of owner.lock; it probes SH first where the router's own
# ``_l0_probe`` probes EX first, and classifies the three outcomes the same way).  A probe
# never waits and is released immediately after its non-blocking attempt (one measurement,
# 2026-09-27: 0.7 us median between flock and LOCK_UN; not a guarantee).  The per-run router
# polls every lock it takes, so a colliding claimant retries on its next poll.  The single-run
# router's one EX|NB claim can collide only with the owner.lock probe, which runs only while a
# valid legacy active.json exists, and that record already refuses any new run
# (ROUTER_UNRESOLVED_RUN).
_ROUTER_LISTED = 64          # names kept per directory (active/, notes/), newest first
_ROUTER_SCAN = 4096          # directory entries examined at most
# Full per-run records read per sample (newest by mtime; cached by identity).  A held run
# lock past this budget has its record read too, up to _ROUTER_ATTRIBUTED more, so its
# runLock inode is still checked.
_ROUTER_FULL_READS = 4
_ROUTER_PROJECTED = 8        # rows in activeRuns / queuedRuns / pipelines
_ROUTER_ATTRIBUTED = 8       # live per-run lock files attributed to a process per sample
_ROUTER_START_WINDOW = 660.0  # a run's record starts at most this long after its process
_ROUTER_READ_RETRIES = 3
_ROUTER_READ_RETRY_S = 0.02
_ROUTER_NOTE_MAX = 4096
_ROUTER_SMALL_MAX = 1024     # policy.json and install-fence.json
_ROUTER_LSOF_MAX = 65536
_ROUTER_FENCE_KIND = "codemode.router.install-fence.v1"
_ROUTER_FENCE_KEYS = frozenset({"schemaVersion", "kind", "state", "generation", "stamp", "sinceUnix", "ownerLock"})
_ROUTER_FENCE_STATES = ("installing", "installed", "rolling-back", "rolled-back")
_ROUTER_BARRIER = "owner.lock.install-barrier"
_ROUTER_STAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z\Z")     # router_fence.STAMP
_ROUTER_POLICY_KIND = "codemode.router.policy.v1"
_ROUTER_NOTE_KIND = "codemode.router.note.v1"
_ROUTER_NOTE_KEYS = frozenset({"schemaVersion", "kind", "runId", "inputSha256", "operation", "client", "host",
                               "admission", "phase", "resource", "step", "sinceUnix", "untilUnix", "pid"})
_ROUTER_NOTE_PHASES = ("admitting", "waiting", "running")
_ROUTER_NOTE_RESOURCES = (None, "mac-pair", "pc-route", "pc-lane-deep", "pc-lane-fast")
# The note's other fields, as spec 2.1 enumerates them (None = not recorded).  ``host`` also
# takes 'auto': the router records the request's host, and a request may say auto.
_ROUTER_NOTE_ENUMS = {"operation": (None, "work", "feedback"), "client": (None, "codex", "claude", "opencode"),
                      "host": (None, "mac", "windows", "auto"),
                      "step": (None, "pre-begin", "backend", "mac-return", "draft", "review")}
# note resource -> (lane name in runs.v1 / the UI, Mac-wide capacity)
_ROUTER_LANES = {"mac-pair": ("mac-pair", 1), "pc-route": ("pc-route", 1),
                 "pc-lane-deep": ("pc-deep", 1), "pc-lane-fast": ("pc-fast", 2)}
_ROUTER_MARKER_SUFFIXES = (".draft", ".review", ".mac-return", ".answer")
_ROUTER_STAGE_HOST = {"intake_intent": "mac", "intake_response": "mac", "mac_return_intent": "mac",
                      "mac_return_response": "mac", "final_validation_intent": "mac"}
_ROUTER_RECORD_CACHE: dict[str, tuple[tuple[int, ...], dict[str, Any] | None]] = {}
_ROUTER_RECORD_CACHE_LOCK = threading.Lock()
# Set while this Monitor process itself may hold router owner.lock (Fix's hold_router and the
# Fix Nisi Inference hold, online_code_repair).  R2.9 legacy rule (iii): an EX holder that is
# this Monitor is not a legacy route.  Counted before the flock is attempted, so there is no
# window in which the Monitor holds L0 while the readers believe it does not; a failed attempt
# releases the mark at once (``_MonitorRouterHold.release``), before its caller handles "busy",
# so a live legacy route is discounted for one flock call at most.
_MONITOR_ROUTER_HOLDS = 0
_MONITOR_ROUTER_HOLDS_LOCK = threading.Lock()
# The newest observation, for activity.py (same sampler thread, right after collect_snapshot).
_LAST_ROUTER_OBSERVATION: dict[str, Any] = {"sampledAt": None, "value": None}
_LAST_ROUTER_OBSERVATION_LOCK = threading.Lock()


class _MonitorRouterHold:
    """One mark from ``monitor_router_hold``; ``release()`` drops it early (idempotent)."""

    def __init__(self) -> None:
        self._marked = True

    def release(self) -> None:
        global _MONITOR_ROUTER_HOLDS
        with _MONITOR_ROUTER_HOLDS_LOCK:
            if self._marked:
                self._marked = False
                _MONITOR_ROUTER_HOLDS -= 1


@contextmanager
def monitor_router_hold() -> Iterator[_MonitorRouterHold]:
    """Mark that this Monitor process may hold router owner.lock (spec R2.9 rule 3 (iii)).

    Raised before the caller's EX|NB attempt; a caller whose attempt fails calls
    ``release()`` on the yielded mark before it goes on, so the readers stop discounting
    owner.lock at once instead of when the caller's "busy" handling ends."""
    global _MONITOR_ROUTER_HOLDS
    with _MONITOR_ROUTER_HOLDS_LOCK:
        _MONITOR_ROUTER_HOLDS += 1
    mark = _MonitorRouterHold()
    try:
        yield mark
    finally:
        mark.release()


def _monitor_holds_router_owner() -> bool:
    with _MONITOR_ROUTER_HOLDS_LOCK:
        return _MONITOR_ROUTER_HOLDS > 0


def last_router_observation(max_age: float = 5.0) -> dict[str, Any] | None:
    """The observation the last snapshot computed, while it is at most ``max_age`` old."""
    with _LAST_ROUTER_OBSERVATION_LOCK:
        sampled, value = _LAST_ROUTER_OBSERVATION["sampledAt"], _LAST_ROUTER_OBSERVATION["value"]
    if sampled is None or value is None or not 0 <= time.time() - sampled <= max_age:
        return None
    return value


def _router_root() -> Path:
    """The journal root is the legacy record's directory (one path to patch in tests)."""
    return _ACTIVE_PATH.parent


def _observe_retry(path: Path, limit: int) -> tuple[str, dict[str, Any] | None]:
    """('ok', value), ('absent', None) or ('unreadable', None).  A record being replaced,
    or published at ``nlink == 2``, is read at most 3 times, 20 ms apart (spec R2.9:
    ≤3 × 20 ms); never raises."""
    for attempt in range(_ROUTER_READ_RETRIES):
        try:
            return "ok", _safe_file(path, limit)
        except FileNotFoundError:
            return "absent", None
        except _RecordChanged:
            if attempt + 1 < _ROUTER_READ_RETRIES:
                time.sleep(_ROUTER_READ_RETRY_S)
        except Exception:
            return "unreadable", None
    return "unreadable", None


def _router_listing(directory: Path, suffix: str) -> tuple[list[str], bool, str]:
    """(run ids named ``<runId><suffix>``, truncated, 'ok'|'absent'|'unsafe') in a private
    directory, examining at most _ROUTER_SCAN entries and opening none of them."""
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return [], False, "absent"
    except OSError:
        return [], False, "unsafe"
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        return [], False, "unsafe"
    names: list[str] = []
    truncated = False
    try:
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= _ROUTER_SCAN:
                    truncated = True
                    break
                name = entry.name
                if name.endswith(suffix) and _RUN_ID.fullmatch(name[:-len(suffix)]):
                    names.append(name[:-len(suffix)])
    except FileNotFoundError:
        return [], False, "absent"
    except OSError:
        return [], False, "unsafe"
    return sorted(names), truncated, "ok"


def _private_dir(directory: Path) -> str:
    """'ok' (a private directory of this uid, not a link), 'absent' or 'unsafe'."""
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unsafe"
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        return "unsafe"
    return "ok"


def _probe_lock(path: Path, *, bound_ino: int | None = None, links: int = 1) -> str:
    """One momentary liveness probe of a router lock file.

    Opens without O_CREAT, read-only, with O_NOFOLLOW on the final path component only (the
    caller validates the parent directory: ``_private_dir``), requires a private regular file
    with ``links`` links, optionally the inode ``bound_ino`` (a per-run record's runLock),
    then ``LOCK_SH|LOCK_NB``, released at once.  Returns 'held' (someone holds it EX),
    'free', 'missing', 'replaced' (another inode than the record bound), 'unsafe' or
    'unknown' (any other error, or the path changed during the probe).
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return "unsafe" if exc.errno == errno.ELOOP else "unknown"
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != links):
            return "unsafe"
        if bound_ino is not None and info.st_ino != bound_ino:
            return "replaced"
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            state = "held"
        except OSError:
            return "unknown"
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            state = "free"
        try:
            current = path.lstat()
        except OSError:
            return "unknown"
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            return "unknown"
        return state
    finally:
        os.close(fd)


def _any_run_lock_live(lock_dir: Path) -> bool:
    """True when any ``locks/*.lock`` is held, or when that cannot be ruled out (listing
    too long or unreadable, an unsafe lock file, a probe error).  R2.9 rule 3 (ii), round 3:
    every lock file, not only runs with records (a pre-begin claimant holds L0 EX and its L1
    with no record for its whole admission wait)."""
    try:
        info = lock_dir.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        return True
    try:
        with os.scandir(lock_dir) as entries:
            names = []
            for count, entry in enumerate(entries):
                if count >= _ROUTER_SCAN:
                    return True
                names.append(entry.name)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    for name in names:
        if not name.endswith(".lock"):
            continue
        if _probe_lock(lock_dir / name) not in ("free", "missing"):
            return True
    return False


def _router_install_state(root: Path) -> dict[str, Any]:
    """The install fence and barrier (spec R2.1, R2.9 rules 3 (iii) and 7).

    ``state`` is 'absent', one of the fence states, 'invalid' (the router's own fence reader
    would refuse it: ROUTER_INSTALL_FENCE_INVALID) or 'missing' (no fence while the installed
    router code carries ``router_fence.py``: every entrypoint refuses
    ROUTER_INSTALL_FENCE_MISSING).  ``inProgress`` is true while the fence says installing /
    rolling-back, or while owner.lock carries the installer's second link
    (``owner.lock.install-barrier``): "router install in progress", never "unsafe".
    ``ownerLockBound`` is False when the fence binds another owner.lock inode than the file
    at that path (an owner repair; R2.4.5).
    """
    install: dict[str, Any] = {"state": "absent", "generation": None, "barrier": False,
                               "inProgress": False, "ownerLockBound": None}
    barrier = os.path.lexists(str(root / _ROUTER_BARRIER))
    try:
        lock: os.stat_result | None = (root / "owner.lock").lstat()
    except OSError:
        lock = None
    # The installer's second link: owner.lock at nlink 2 AND the barrier name.  A second
    # link without the barrier name stays an unsafe journal (never "install in progress").
    install["barrier"] = bool(barrier and lock is not None and stat.S_ISREG(lock.st_mode)
                              and lock.st_nlink == 2)
    status, fence = _observe_retry(root / "install-fence.json", _ROUTER_SMALL_MAX)
    if status == "ok":
        bound = fence.get("ownerLock") if isinstance(fence, dict) else None
        if (set(fence) != _ROUTER_FENCE_KEYS or type(fence.get("schemaVersion")) is not int
                or fence["schemaVersion"] != 1 or fence.get("kind") != _ROUTER_FENCE_KIND
                or fence.get("state") not in _ROUTER_FENCE_STATES
                or not isinstance(fence.get("generation"), str) or not _SHA256.fullmatch(fence["generation"])
                or not isinstance(fence.get("stamp"), str) or not _ROUTER_STAMP.fullmatch(fence["stamp"])
                or not _finite(fence.get("sinceUnix"))
                or not (bound is None or (isinstance(bound, dict) and set(bound) == {"dev", "ino"}
                                          and all(type(bound[k]) is int and bound[k] >= 0 for k in ("dev", "ino"))))):
            install["state"] = "invalid"
        else:
            install.update(state=fence["state"], generation=fence["generation"])
            if bound is not None and lock is not None:
                install["ownerLockBound"] = lock.st_ino == bound["ino"]
    elif status == "unreadable":
        install["state"] = "invalid"
    elif os.path.lexists(str(_ROUTER_SCRIPT.with_name("router_fence.py"))):
        # A per-run generation is installed (P3 put router_fence.py first, a rollback to the
        # single-run code moves it aside and leaves a rolled-back fence), so an absent fence
        # is not the pre-P3 router: the router refuses every command until it is restored.
        install["state"] = "missing"
    install["inProgress"] = install["state"] in ("installing", "rolling-back") or install["barrier"]
    return install


def _router_policy(root: Path, install: dict[str, Any]) -> dict[str, Any]:
    """Admission policy as the router would read it, plus the R2.2 drain state.

    Without an installed per-run generation (no fence, or a rollback to the old router)
    the router is the single-run router whatever policy.json says.  Under an installed
    fence: policy.json (absent = single), and while it says single the drain state comes
    from momentary probes of owner.lock alone: SH|NB fails (an EX holder excludes every SH
    holder) or EX|NB succeeds (nobody) = drained; SH|NB succeeds and EX|NB fails = draining.
    The probes are skipped while this Monitor may hold owner.lock or the lock is not the
    inode the install bound ('unknown').  Notes only name the shared holders.
    """
    if install["state"] in ("absent", "rolled-back"):
        return {"policy": "single", "source": "legacy-router", "drainState": "not-applicable",
                "draining": False, "sharedHolders": []}
    if install["state"] in ("missing", "invalid"):
        # Every router entrypoint refuses (ROUTER_INSTALL_FENCE_MISSING / _INVALID): no run
        # is admitted whatever policy.json says, until the owner finishes or rolls back.
        return {"policy": None, "source": "fence-" + install["state"], "drainState": "unknown",
                "draining": False, "sharedHolders": []}
    status, value = _observe_retry(root / "policy.json", _ROUTER_SMALL_MAX)
    if status == "absent":
        policy, source = "single", "default"
    elif (status == "ok" and set(value) == {"schemaVersion", "kind", "concurrency"}
          and type(value.get("schemaVersion")) is int and value["schemaVersion"] == 1
          and value.get("kind") == _ROUTER_POLICY_KIND and value.get("concurrency") in ("single", "multi")):
        policy, source = value["concurrency"], "file"
    else:
        # The router fails closed on an invalid policy: no new run is admitted.
        return {"policy": None, "source": "invalid", "drainState": "unknown", "draining": False, "sharedHolders": []}
    if policy != "single":
        return {"policy": policy, "source": source, "drainState": "not-applicable",
                "draining": False, "sharedHolders": []}
    drain = "unknown"
    if (install["state"] == "installed" and not install["inProgress"]
            and install["ownerLockBound"] is not False and not _monitor_holds_router_owner()):
        drain = _router_drain_probe(root / "owner.lock")
    return {"policy": policy, "source": source, "drainState": drain,
            "draining": drain != "drained", "sharedHolders": []}


def _router_drain_probe(path: Path) -> str:
    """'drained', 'draining' or 'unknown' from momentary NB probes of owner.lock."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return "unknown"
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            return "unknown"
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "drained"            # an EX holder: no SH holder exists at this instant
        except OSError:
            return "unknown"
        fcntl.flock(fd, fcntl.LOCK_UN)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "draining"           # SH holders (runs admitted shared) are still running
        except OSError:
            return "unknown"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "drained"                # nobody holds it
    finally:
        os.close(fd)


def _valid_active_route(active: dict[str, Any], *, per_run: bool = False) -> tuple[str, float, float] | None:
    """Validate only the router's fixed active-record envelope, not task text.

    A per-run record (``active/<id>.json``) is the legacy envelope plus ``runLock``
    (the L1 inode its run held at begin); a legacy record never carries it."""
    base = {"schemaVersion", "runId", "inputSha256", "stage", "checkpoint", "startedUnix"}
    if per_run:
        lock = active.get("runLock")
        if (not isinstance(lock, dict) or set(lock) != {"dev", "ino"}
                or any(type(lock[key]) is not int or lock[key] < 0 for key in ("dev", "ino"))):
            return None
        base = base | {"runLock"}
    if (active.get("schemaVersion") != 1 or type(active.get("schemaVersion")) is not int
            or not isinstance(active.get("runId"), str) or not _RUN_ID.fullmatch(active["runId"])
            or not isinstance(active.get("inputSha256"), str)
            or not _SHA256.fullmatch(active["inputSha256"])
            or not isinstance(active.get("stage"), str) or not _STAGE.fullmatch(active["stage"])):
        return None
    started = active.get("startedUnix")
    if not _finite(started):
        return None
    checkpoint = active.get("checkpoint")
    if checkpoint is None:
        if set(active) != base or active["stage"] != "started":
            return None
        return active["runId"], float(started), float(started)
    if (not isinstance(checkpoint, dict) or set(active) != base | {"sequence"}
            or type(active["sequence"]) is not int or active["sequence"] < 1
            or set(checkpoint) != {"schemaVersion", "runId", "inputSha256", "sequence",
                                   "stage", "evidence", "recordedUnix"}
            or checkpoint.get("schemaVersion") != 1
            or type(checkpoint.get("schemaVersion")) is not int
            or checkpoint.get("runId") != active["runId"]
            or checkpoint.get("inputSha256") != active["inputSha256"]
            or checkpoint.get("stage") != active["stage"]
            or type(checkpoint.get("sequence")) is not int
            or checkpoint["sequence"] != active["sequence"]
            or not isinstance(checkpoint.get("evidence"), dict)):
        return None
    recorded = checkpoint.get("recordedUnix")
    if not _finite(recorded):
        return None
    return active["runId"], float(started), float(recorded)


def _router_note(root: Path, run_id: str) -> dict[str, Any] | None:
    """A live run's advisory note (closed schema, every field enumerated as spec 2.1 lists
    it), else None.  Never a safety input."""
    status, note = _observe_retry(root / "notes" / (run_id + ".json"), _ROUTER_NOTE_MAX)
    if status != "ok" or set(note) != _ROUTER_NOTE_KEYS:
        return None
    finite = _finite
    if (type(note["schemaVersion"]) is not int or note["schemaVersion"] != 1
            or note["kind"] != _ROUTER_NOTE_KIND or note["runId"] != run_id
            or not isinstance(note["inputSha256"], str) or not _SHA256.fullmatch(note["inputSha256"])
            or note["admission"] not in ("shared", "exclusive")
            or any(not isinstance(note[key], (str, type(None))) or note[key] not in allowed
                   for key, allowed in _ROUTER_NOTE_ENUMS.items())
            or note["phase"] not in _ROUTER_NOTE_PHASES or note["resource"] not in _ROUTER_NOTE_RESOURCES
            or not finite(note["sinceUnix"]) or (note["untilUnix"] is not None and not finite(note["untilUnix"]))
            or type(note["pid"]) is not int or note["pid"] < 1):
        return None
    return note


def _router_record(path: Path, info: os.stat_result) -> tuple[str, dict[str, Any] | None]:
    """A per-run record, re-read only when its (dev, ino, size, mtime, ctime) changed.

    ``info`` is the listing's lstat taken before the read; the value is cached only when
    the file still has exactly that identity after the read, so a cached value always
    belongs to the identity it is stored under."""
    key = path.name
    identity = _file_identity(info)
    with _ROUTER_RECORD_CACHE_LOCK:
        cached = _ROUTER_RECORD_CACHE.get(key)
    if cached is not None and cached[0] == identity and info.st_nlink == 1:
        return "ok", cached[1]
    status, value = _observe_retry(path, _MAX_STATE)
    if status == "ok":
        try:
            current = path.lstat()
        except OSError:
            current = None
        if current is not None and _file_identity(current) == identity and current.st_nlink == 1:
            with _ROUTER_RECORD_CACHE_LOCK:
                _ROUTER_RECORD_CACHE[key] = (identity, value)
    return status, value


def _record_host(record: dict[str, Any] | None) -> str | None:
    checkpoint = record.get("checkpoint") if isinstance(record, dict) else None
    evidence = checkpoint.get("evidence") if isinstance(checkpoint, dict) else None
    if not isinstance(evidence, dict):
        return None
    if checkpoint.get("stage") in _ROUTER_STAGE_HOST:
        return _ROUTER_STAGE_HOST[checkpoint["stage"]]
    for key in ("selectedHost", "host", "requestedHost"):
        if evidence.get(key) in ("mac", "windows", "auto"):
            return evidence[key]
    return None


def _router_row(run_id: str | None, layout: str, record: dict[str, Any] | None, *, readable: bool,
                per_run: bool) -> dict[str, Any]:
    """The bounded, content-free facts of one run (no evidence body is ever copied)."""
    validated = _valid_active_route(record, per_run=per_run) if isinstance(record, dict) else None
    checkpoint = record.get("checkpoint") if validated and isinstance(record.get("checkpoint"), dict) else None
    evidence = checkpoint.get("evidence") if checkpoint else None
    code = evidence.get("code") if isinstance(evidence, dict) and record.get("stage") == "incomplete" else None
    return {"runId": validated[0] if validated else run_id, "layout": layout,
            "inputSha256": record["inputSha256"] if validated else None,
            "stage": record["stage"] if validated else None,
            "sequence": record.get("sequence") if validated else None,
            "startedUnix": validated[1] if validated else None,
            "recordedUnix": validated[2] if validated else None,
            "recoveryRequired": bool(isinstance(evidence, dict) and evidence.get("recoveryRequired") is True),
            "code": code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else None,
            "host": _record_host(record) if validated else None,
            "readable": readable, "valid": validated is not None, "read": True,
            "runLock": record.get("runLock") if validated and per_run else None,
            "live": False, "lock": None, "lockHeld": False, "lockBound": False, "stalled": False, "state": None,
            "note": None, "client": None, "collision": False,
            "operation": None, "pid": None, "archived": False, "evidence": None, "_record": record}


def _per_run_row(run_id: str, record: dict[str, Any] | None, readable: bool) -> dict[str, Any]:
    """The row of ``active/<run_id>.json``: a record that fails the closed envelope or names
    another run is unreadable (its runLock binds nothing)."""
    row = _router_row(run_id, "per-run", record, readable=readable, per_run=True)
    if record is not None and not row["valid"]:
        row["readable"] = False
    if row["runId"] != run_id:
        # A record naming another run: nothing in it describes this run.
        row = _router_row(run_id, "per-run", None, readable=False, per_run=True)
    return row


def _note_projection(note: dict[str, Any] | None) -> dict[str, Any] | None:
    if note is None:
        return None
    return {key: note[key] for key in ("phase", "resource", "step", "sinceUnix", "untilUnix", "admission")}


def _apply_note(row: dict[str, Any], note: dict[str, Any] | None) -> None:
    row["note"] = _note_projection(note)
    if note is not None:
        row["client"] = note["client"] if note["client"] in ("codex", "claude", "opencode") else None
        row["operation"] = note["operation"]
        if note["host"] in ("mac", "windows", "auto"):
            row["host"] = note["host"]
        if row["inputSha256"] is None:
            row["inputSha256"] = note["inputSha256"]


def _parse_lsof(listing: str) -> list[tuple[int, str, str]]:
    """(pid, access, name) for every file lsof -F pfan printed."""
    rows: list[tuple[int, str, str]] = []
    pid: int | None = None
    access: str | None = None
    for line in listing.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
            access = None
        elif line.startswith("f"):
            access = None
        elif line.startswith("a"):
            access = line[1:]
        elif line.startswith("n") and pid is not None:
            rows.append((pid, access or "", line[1:]))
    return rows


def _process_table(pids: list[int]) -> dict[int, tuple[float, list[str]]]:
    """{pid: (start, argv)} from one bounded ps for at most 16 pids."""
    wanted = sorted(set(pid for pid in pids if type(pid) is int and pid > 0))[:16]
    if not wanted:
        return {}
    listing = _bounded_command(["/bin/ps", "-p", ",".join(str(pid) for pid in wanted),
                                "-o", "pid=", "-o", "lstart=", "-o", "command="],
                               limit=4096 * len(wanted), ok_codes=(0, 1))
    table: dict[int, tuple[float, list[str]]] = {}
    for line in listing.splitlines():
        match = re.match(r"\s*(\d+)\s+(\w{3} \w{3} [ \d]\d \d{2}:\d{2}:\d{2} \d{4})\s+(.*)\Z", line)
        if not match:
            continue
        try:
            start = time.mktime(time.strptime(match.group(2), "%a %b %d %H:%M:%S %Y"))
            table[int(match.group(1))] = (start, shlex.split(match.group(3).strip()))
        except (ValueError, OverflowError):
            continue
    return table


def _router_work_argv(args: list[str]) -> bool:
    """argv is exactly [python, -I, -B, <_ROUTER_SCRIPT>, work|feedback]."""
    # macOS shows framework interpreters (Xcode's /usr/bin/python3 shim,
    # Homebrew) by their real binary: .../Python.app/Contents/MacOS/Python.
    interpreter = args[0] if args else ""
    framework = (Path(interpreter).name == "Python"
                 and interpreter.endswith("/Python.app/Contents/MacOS/Python")
                 and any(part in ("Python.framework", "Python3.framework")
                         for part in Path(interpreter).parts))
    return (len(args) == 5 and (framework or Path(interpreter).name in (
                "python3", "python3.14", "python3.13", "python3.12", "python3.11", "python3.10"))
            and args[1:3] == ["-I", "-B"] and args[3] == str(_ROUTER_SCRIPT)
            and args[4] in ("work", "feedback"))


def _real_root(root: Path) -> Path:
    """The journal root as lsof names it (resolved; the journal itself never has links)."""
    return Path(os.path.realpath(str(root)))


def _router_openers(root: Path) -> list[tuple[int, str, str]] | None:
    """One bounded lsof of owner.lock and every open file under locks/; None when unavailable.
    Names are compared under ``_real_root(root)``."""
    root = _real_root(root)
    lock_dir = root / "locks"
    args = ["/usr/sbin/lsof", "-nP", "-F", "pfan"]
    if lock_dir.is_dir() and not lock_dir.is_symlink():
        args += ["+d", str(lock_dir)]
    args += ["--", str(root / "owner.lock")]
    try:
        # lsof exits 1 when a named file is open nowhere; its output is still complete.
        return _parse_lsof(_bounded_command(args, limit=_ROUTER_LSOF_MAX, ok_codes=(0, 1)))
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        return None


def _router_owner_process(started: float | None = None) -> tuple[int, float] | None:
    """Legacy rule (iv): among the processes with a read-write owner.lock descriptor,
    exactly one exact work (or feedback) process that has no file under ``locks/`` open
    (lsof reports open descriptors, not flock holds, so this is stricter than "holds no
    run lock") and, when ``started`` is given, whose start is at most 660 s before it and
    never after it.

    The legacy router holds owner.lock EX for its whole run.  New-code waiters also keep
    owner.lock open while they poll, so "exactly one opener" is not a test; the flock
    probes (rules (i)-(iii)) decide who holds it and this only names the process.
    """
    # lsof enumerates process file descriptors and can itself enter U state
    # while an unrelated SharedChami SMB reader is stalled. Without a safe
    # local process inspection, leave route ownership unverified.
    if _windows_worker_reader_blocked():
        return None
    root = _router_root()
    lock_path = str(_real_root(root) / "owner.lock")
    lock_prefix = str(_real_root(root) / "locks") + "/"
    try:
        openers = _router_openers(root)
        if openers is None:
            return None
        run_lock_openers = {pid for pid, _, name in openers if name.startswith(lock_prefix)}
        candidates = sorted({pid for pid, access, name in openers
                             if name == lock_path and access == "u" and pid not in run_lock_openers})
        if not candidates or len(candidates) > 16:
            return None
        # lsof can show an open descriptor without reporting flock on macOS.
        # Require the exact canonical work process and check its start time.
        table = _process_table(candidates)
        matches = [(pid, table[pid][0]) for pid in candidates
                   if pid in table and _router_work_argv(table[pid][1])
                   and (started is None or 0 <= started - table[pid][0] <= _ROUTER_START_WINDOW)]
        return matches[0] if len(matches) == 1 else None
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        return None


def _run_owners(root: Path, rows: list[dict[str, Any]]) -> None:
    """Best-effort attribution of live per-run rows (R2.9 rule 3, per-run): among the
    openers of each run's lock file, the one exact work process whose start fits the record
    and that equals the note's pid when there is a note.  Ambiguous stays None ("live,
    process unknown"); liveness itself is the lock probe, never this."""
    live = [row for row in rows if row["live"] and row["layout"] != "legacy" and row["runId"]][:_ROUTER_ATTRIBUTED]
    if not live or _windows_worker_reader_blocked():
        return
    openers = _router_openers(root)
    if not openers:
        return
    by_path: dict[str, set[int]] = {}
    for pid, _access, name in openers:
        by_path.setdefault(name, set()).add(pid)
    lock_dir = _real_root(root) / "locks"
    wanted = {row["runId"]: by_path.get(str(lock_dir / (row["runId"] + ".lock")), set()) for row in live}
    try:
        table = _process_table([pid for pids in wanted.values() for pid in pids])
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        return
    for row in live:
        note_pid = row.get("_notePid")
        reference = row["startedUnix"] if row["startedUnix"] is not None else (
            row["note"]["sinceUnix"] if row["note"] else None)
        matches = [pid for pid in sorted(wanted[row["runId"]])
                   if pid in table and _router_work_argv(table[pid][1])
                   and (note_pid is None or pid == note_pid)
                   and (reference is None or 0 <= reference - table[pid][0] + 1 <= _ROUTER_START_WINDOW + 1)]
        row["pid"] = matches[0] if len(matches) == 1 else None


def _legacy_liveness(row: dict[str, Any], root: Path, install: dict[str, Any],
                     now: float) -> tuple[str, str]:
    """Legacy record liveness (spec R2.9 rule 3): (verdict, evidence), verdict one of
    'processing', 'unfinished' (unresolved: live work unverified), 'unknown' (the record
    or its owner changed during the observation) or 'invalid'.

    Live iff (i) owner.lock is held EX (a fresh SH|NB probe fails), (ii) no ``locks/*.lock``
    is held, (iii) the fence is absent, installed or rolled-back, no install transition is in
    progress and this Monitor does not hold owner.lock, and (iv) lsof/ps name exactly one
    exact work process that has no ``locks/`` file open and started at most 660 s before the
    record (never after it).
    An incomplete checkpoint, invalid timing or an invalid record never reaches the probes.
    The checkpoint's age is not a liveness condition: a route verified live whose last
    checkpoint is older than the freshness limit is live but stalled (``row["stalled"]``:
    it stops blinking), never shown as a dead, unresolved run.
    """
    active = row["_record"]
    validated = _valid_active_route(active) if isinstance(active, dict) else None
    if validated is None:
        return "invalid", "Router active record is not a valid bound route"
    run_id, started, recorded = validated
    if not 0 <= started <= recorded <= now + 1:
        return "unfinished", "Router route record is stale or has invalid timing; live work unverified"
    quiet = now - recorded
    stale = quiet > _MAX_AGE
    record = f"Router route record is stale (last checkpoint {quiet:.0f}s ago)" if stale else "Fresh route record exists"
    # The router writes `incomplete` as its terminal quarantine checkpoint.
    # A process may still hold the owner lock while it returns that result;
    # the lock cannot turn a terminal checkpoint back into live inference.
    if active["stage"] == "incomplete":
        return "unfinished", (("Router route record is stale; it" if stale else "Router")
                              + " recorded an incomplete checkpoint; recovery is unresolved and live work is unverified")
    if (install["state"] not in ("absent", "installed", "rolled-back") or install["inProgress"]
            or _monitor_holds_router_owner()):
        return "unfinished", (f"{record}, but the router owner lock may be held by an install transition, by this "
                              "monitor, or the install fence is missing or invalid; live work unverified")
    if _probe_lock(root / "owner.lock") != "held":
        return "unfinished", f"{record}; no process holds the router owner lock; live work unverified"
    if _any_run_lock_live(root / "locks"):
        return "unfinished", (f"{record}, but a per-run router lock is held; the owner lock holder is not the "
                              "legacy route")
    owner = _router_owner_process(started)
    if owner is None or not (0 <= started - owner[1] <= _ROUTER_START_WINDOW):
        return "unfinished", f"{record}; matching live owner process unverified"
    # A route may finish while process metadata is sampled. Re-read its exact
    # fixed record before claiming activity; do not take the owner's lock.
    try:
        if _safe_file(_ACTIVE_PATH, _MAX_STATE) != active:
            return "unknown", "Router route changed during observation"
    except Exception:
        return "unknown", "Router route changed during observation"
    if _router_owner_process(started) != owner:
        return "unknown", "Router owner process changed during observation"
    if _probe_lock(root / "owner.lock") != "held":
        return "unknown", "Router owner lock was released during observation"
    row["pid"] = owner[0]
    row["stalled"] = stale
    if stale:
        return "processing", (f"Bound router record and matching live work process holding the owner lock, but no "
                              f"checkpoint for {quiet:.0f}s; the route may be stalled; client and chat binding "
                              "unavailable")
    return "processing", ("Fresh bound router record and matching live work process holding the owner lock; "
                          "client and chat binding unavailable")


def _router_lanes(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Router holders and waiters per Mac-wide lane, from live runs' notes (spec 2.5).
    PC occupancy by pc-llm stays in the worker-heartbeat view."""
    lanes = {lane: {"capacity": capacity, "holders": [], "waiting": []}
             for lane, capacity in _ROUTER_LANES.values()}
    for row in rows:
        note = row["note"]
        if not row["live"] or note is None or note["resource"] is None:
            continue
        lane = _ROUTER_LANES[note["resource"]][0]
        if note["phase"] == "running":
            lanes[lane]["holders"].append(row["runId"])
        elif note["phase"] == "waiting":
            lanes[lane]["waiting"].append(row["runId"])
        if note["resource"].startswith("pc-lane-") and row["runId"] not in lanes["pc-route"]["holders"]:
            lanes["pc-route"]["holders"].append(row["runId"])  # lane tokens are taken under the PC route
    for lane in lanes.values():
        lane["holders"] = lane["holders"][:_ROUTER_PROJECTED]
        lane["waiting"] = lane["waiting"][:_ROUTER_PROJECTED]
    return lanes


def _router_listed_record(path: Path, archive: Path, *, read: bool) -> tuple[str, dict[str, Any] | None,
                                                                            os.stat_result | None]:
    """(status, record, lstat) of one listed ``active/<id>.json``.

    status: 'ok', 'unreadable', 'unread' (listed, not read: past the read budget) or
    'finished' (the record is gone and the run's archive exists: it finished cleanly, so it
    gets no row).  A listed record gone without its archive is looked for again, up to
    3 x 20 ms, then reported 'unreadable' (spec R2.9 rule 2): never silently dropped."""
    for attempt in range(_ROUTER_READ_RETRIES):
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        except OSError:
            return "unreadable", None, None
        if info is not None:
            if not read:
                return "unread", None, info
            status, record = _router_record(path, info)
            if status != "absent":
                return status, record, info
        if os.path.lexists(str(archive)):
            return "finished", None, None
        if attempt + 1 < _ROUTER_READ_RETRIES:
            time.sleep(_ROUTER_READ_RETRY_S)
    return "unreadable", None, None


def _router_observation(now: float) -> dict[str, Any]:
    """Read both journal layouts once: every listed run with its liveness and state.

    Row ``state``: a live run shows its note phase ('running', 'waiting', 'admitting';
    'running' without a readable note); a dead run with a record is 'unresolved', or
    'archived-uncleared' when its archive also exists; 'unverified' when a per-run run lock
    is missing, replaced, unsafe or could not be probed, or is held while the record that
    binds it was not read (liveness cannot be proven, so never idle); 'unreadable' when the
    record could not be read whole or fails the closed envelope, even while its run lock is
    held (``lockHeld``: only a valid record's bound inode makes a held lock this run's
    liveness).  A note whose run lock is free is ignored.
    """
    root = _router_root()
    install = _router_install_state(root)
    observation: dict[str, Any] = {"root": str(root), "install": install, "admission": None, "rows": [],
                                   "primary": None, "truncated": False, "error": None,
                                   "legacyPresent": False, "layout": "absent", "collisions": []}
    rows: list[dict[str, Any]] = []
    active_ids, active_cut, active_dir = _router_listing(root / "active", ".json")
    note_ids, note_cut, note_dir = _router_listing(root / "notes", ".json")
    # O_NOFOLLOW guards only a lock file's own name: locks/ itself must be a private directory,
    # or no per-run lock under it proves anything.
    locks_dir = _private_dir(root / "locks")
    if "unsafe" in (active_dir, note_dir, locks_dir):
        observation["error"] = "Router run directories are unsafe or unreadable"
    observation["truncated"] = active_cut or note_cut or len(active_ids) > _ROUTER_LISTED or len(note_ids) > _ROUTER_LISTED

    def run_lock(run_id: str, bound: dict[str, Any] | None = None) -> str:
        if locks_dir == "unsafe":
            return "unsafe"
        return _probe_lock(root / "locks" / (run_id + ".lock"), bound_ino=bound["ino"] if bound else None)

    # Per-run records, newest first; full reads for the newest few only.  A name that vanished
    # before its lstat sorts last and is looked for again below.
    listed: list[tuple[int, str]] = []
    for run_id in active_ids:
        try:
            listed.append(((root / "active" / (run_id + ".json")).lstat().st_mtime_ns, run_id))
        except OSError:
            listed.append((0, run_id))
    listed.sort(reverse=True)
    listed = listed[:_ROUTER_LISTED]
    reads = extra = 0
    seen: set[str] = set()
    cached: set[str] = set()
    for _order, run_id in listed:
        path = root / "active" / (run_id + ".json")
        archive = root / "archive" / (run_id + ".json")
        budget = reads < _ROUTER_FULL_READS
        reads += budget
        status, record, info = _router_listed_record(path, archive, read=budget)
        if status == "finished":
            continue
        if budget:
            cached.add(path.name)
        row = _per_run_row(run_id, record, status != "unreadable")
        row["read"] = status != "unread"
        row["lock"] = run_lock(run_id, row["runLock"])
        if row["lock"] == "held" and status == "unread" and extra < _ROUTER_ATTRIBUTED:
            # A held run lock past the read budget: read that record too (bounded and cached),
            # so the inode its run bound is checked for every live row, not only the newest.
            extra += 1
            status, record, info = _router_listed_record(path, archive, read=True)
            if status == "finished":
                continue                        # the run archived and cleared its record: it finished
            cached.add(path.name)
            row = _per_run_row(run_id, record, status == "ok")
            row["lock"] = run_lock(run_id, row["runLock"])
        row["lockHeld"] = row["lock"] == "held"
        # A held lock proves this run live only on the inode its valid record bound (the probe
        # checked it).  A held lock beside a record that is unreadable, fails the envelope or
        # was not read proves nothing about this run: never live, never running, never idle.
        row["live"] = row["lockHeld"] and row["valid"] and row["runLock"] is not None
        row["lockBound"] = row["live"]
        if not row["live"] and status in ("ok", "unread") and not os.path.lexists(str(path)):
            if os.path.lexists(str(archive)):
                # The run finished (archive, clear the record, release L1) between the listing
                # and the probe: nothing is left to report, never "unresolved" or
                # "archived-uncleared" for one sample.
                continue
            # Gone without an archive: what it said no longer holds (R2.9 rule 2).
            lock = row["lock"]
            row = _per_run_row(run_id, None, False)
            row.update(lock=lock, lockHeld=lock == "held")
        row["archived"] = os.path.lexists(str(archive))
        row["_mtime"] = info.st_mtime if info is not None else 0.0
        rows.append(row)
        seen.add(run_id)
    with _ROUTER_RECORD_CACHE_LOCK:
        for key in [key for key in _ROUTER_RECORD_CACHE if key not in cached]:
            del _ROUTER_RECORD_CACHE[key]
    # The legacy single-run record.  Always read and, when valid, verified by the legacy rules,
    # even when a per-run record names the same run (see the collision rule below).
    status, legacy = _observe_retry(_ACTIVE_PATH, _MAX_STATE)
    if status != "absent":
        observation["legacyPresent"] = True
        row = _router_row(None, "legacy", legacy, readable=status == "ok", per_run=False)
        if status == "ok":
            run_id = legacy.get("runId")
            row["runId"] = run_id if isinstance(run_id, str) and _RUN_ID.fullmatch(run_id) else None
        try:
            row["_mtime"] = _ACTIVE_PATH.lstat().st_mtime
        except OSError:
            row["_mtime"] = 0.0
        if row["runId"]:
            row["archived"] = os.path.lexists(str(root / "archive" / (row["runId"] + ".json")))
            seen.add(row["runId"])
        rows.append(row)
    # Notes of runs with no record: live pre-begin claimants (queued or admitting).
    for run_id in note_ids[:_ROUTER_LISTED]:
        if run_id in seen:
            continue
        lock = run_lock(run_id)
        if lock != "held":
            continue                            # a stale note of a dead attempt: no row
        row = _router_row(run_id, "note", None, readable=True, per_run=True)
        row.update(lock=lock, lockHeld=True, live=True, _mtime=0.0)
        rows.append(row)
    # Liveness and state.
    for row in rows:
        if row["layout"] == "legacy":
            continue
        note = _router_note(root, row["runId"]) if row["live"] else None
        # The pathname and runId are not enough to bind a note to a begun run. A stale
        # note for the same ID must not supply its phase, client or lane to another input.
        if (note is not None and row["layout"] == "per-run"
                and note["inputSha256"] != row["inputSha256"]):
            note = None
        _apply_note(row, note)
        row["_notePid"] = note["pid"] if note else None
        if row["live"]:
            # No valid note: a begun run (it has a record) is running; a record-less one has not
            # begun (begin writes the record first), so it is still in admission.
            row["state"] = note["phase"] if note else ("running" if row["layout"] == "per-run" else "admitting")
        elif row["lock"] in ("missing", "replaced", "unsafe", "unknown"):
            row["state"] = "unverified"
        elif not row["readable"]:
            row["state"] = "unreadable"
        elif row["lockHeld"]:
            row["state"] = "unverified"         # held, but the record binding it was not read
        else:
            row["state"] = "archived-uncleared" if row["archived"] else "unresolved"
        if row["layout"] == "note" and note:
            row["_mtime"] = note["sinceUnix"]
    for row in [row for row in rows if row["layout"] == "legacy"]:
        if not row["readable"] or row["runId"] is None:
            row.update(state="unreadable", evidence="Router active record invalid or unreadable")
            continue
        verdict, evidence = _legacy_liveness(row, root, install, now)
        row["evidence"] = evidence
        row["_verdict"] = verdict
        row["live"] = verdict == "processing"
        # 'unknown' means the record or its owner changed while it was observed (usually a
        # live route writing a checkpoint): 'changing', re-checked next sample, never counted
        # or shown as an unresolved run.
        row["state"] = ("running" if row["live"] else "unreadable" if verdict == "invalid"
                        else "changing" if verdict == "unknown"
                        else "archived-uncleared" if row["archived"] else "unresolved")
    # One run ID in both layouts.  It is one run (one row, the per-run one) only when the
    # per-run record is valid, describes the same input, and the legacy route is not live.
    # Otherwise both rows stay and the collision is reported: a live legacy route is never
    # hidden behind an unreadable or foreign per-run record.
    per_run = {row["runId"]: row for row in rows if row["layout"] == "per-run"}
    for row in [row for row in rows if row["layout"] == "legacy" and row["runId"] in per_run]:
        twin = per_run[row["runId"]]
        if not row["live"] and twin["valid"] and row["inputSha256"] == twin["inputSha256"]:
            rows.remove(row)
        else:
            row["collision"] = twin["collision"] = True
            observation["collisions"].append(row["runId"])
    legacy_rows = [row for row in rows if row["layout"] == "legacy"]
    _run_owners(root, rows)
    rows.sort(key=lambda row: (row["startedUnix"] if row["startedUnix"] is not None else row.get("_mtime") or 0.0),
              reverse=True)
    observation["rows"] = rows
    observation["collisions"] = observation["collisions"][:_ROUTER_PROJECTED]
    observation["layout"] = ("mixed" if legacy_rows and len(rows) > len(legacy_rows)
                             else "legacy" if legacy_rows else "per-run" if rows else "absent")
    live = [row for row in rows if row["live"]]
    # The newest running verified-live run, else the newest queued one, else the newest
    # unresolved one: the single-object projection older consumers read (spec 6.12).
    # Deliberate reading of R2.9 item 4 / 6.12 ("the newest verified-live run"), recorded for
    # the owner's acceptance: 6.12 lists queued runs apart (queuedRuns), and a newer queued run
    # as the primary would make the node read "queued" while an older route is running.
    running = [row for row in live if not _router_queued(row)]
    observation["primary"] = (running or live or [row for row in rows if row["runId"] is not None] or rows or [None])[0]
    admission = _router_policy(root, install)
    if admission["policy"] == "single":
        admission["sharedHolders"] = [row["runId"] for row in live if row["layout"] != "legacy" and (
            row["note"] is None or row["note"]["admission"] == "shared")][:_ROUTER_PROJECTED]
    # Spec 2.1: CODEMODE_ROUTER_CONCURRENCY=off|single in a caller's own environment forces that
    # caller single (it can never force multi).  That is each router process's environment,
    # which this Monitor (started by its login agent) cannot see, and the Monitor's own
    # environment says nothing about it; under multi the file policy is shown with that caveat.
    admission["callerOverride"] = "not-observable" if admission["policy"] == "multi" else None
    observation["admission"] = admission
    observation["lanes"] = _router_lanes(rows)
    return observation


def observe_router(now: float) -> dict[str, Any]:
    """One observation for a sample, remembered for activity.py; never raises."""
    try:
        router = _router_observation(now)
    except Exception:
        router = {"root": str(_router_root()),
                  "install": {"state": "invalid", "generation": None, "barrier": False,
                              "inProgress": False, "ownerLockBound": None},
                  "admission": None, "rows": [], "primary": None, "truncated": False,
                  "error": "Router run observation failed", "legacyPresent": False,
                  "layout": "absent", "lanes": _router_lanes([]), "collisions": []}
    with _LAST_ROUTER_OBSERVATION_LOCK:
        _LAST_ROUTER_OBSERVATION.update(sampledAt=now, value=router)
    return router


def _router_queued(row: dict[str, Any]) -> bool:
    return row["live"] and row["state"] in ("waiting", "admitting")


def _router_projection(observation: dict[str, Any], now: float) -> dict[str, Any]:
    """activeRuns, queuedRuns, lanes, counts and admission for the online-code snapshot."""
    rows = observation["rows"]
    active, queued = [], []
    for row in rows:
        if _router_queued(row):
            note = row["note"] or {}
            until = note.get("untilUnix")
            queued.append({"runId": row["runId"], "phase": row["state"], "resource": note.get("resource"),
                           "step": note.get("step"), "sinceUnix": note.get("sinceUnix"), "untilUnix": until,
                           "secondsLeft": round(max(0.0, until - now), 1) if until is not None else None,
                           "client": row["client"], "host": row["host"], "stage": row["stage"]})
        else:
            active.append({"runId": row["runId"], "state": row["state"], "stage": row["stage"],
                           "host": row["host"], "client": row["client"], "live": row["live"],
                           "layout": row["layout"], "processVerified": row["pid"] is not None,
                           "recoveryRequired": row["recoveryRequired"],
                           "lockHeld": row["lockHeld"] and not row["live"], "collision": row["collision"]})
    counts = {"running": sum(1 for row in rows if row["live"] and not _router_queued(row)),
              "queued": sum(1 for row in rows if _router_queued(row)),
              # A row changing under observation is re-checked next sample, not an unresolved run.
              "unresolved": sum(1 for row in rows if not row["live"] and row["state"] != "changing")}
    return {"activeRuns": active[:_ROUTER_PROJECTED], "queuedRuns": queued[:_ROUTER_PROJECTED],
            "lanes": observation["lanes"], "runCounts": counts,
            "admission": observation["admission"], "install": observation["install"],
            "runIdCollisions": list(observation.get("collisions") or []),
            "runsTruncated": observation["truncated"] or len(active) > _ROUTER_PROJECTED
            or len(queued) > _ROUTER_PROJECTED}


def _launcher_readiness(now: float) -> tuple[str, float | None]:
    """Read only the launcher's recent capability-inventory receipt.

    This is never proof of a model call, completed route, or client/chat owner.
    """
    try:
        directory = _LAUNCHER_ROOT.lstat()
        if (_LAUNCHER_ROOT.is_symlink() or not stat.S_ISDIR(directory.st_mode)
                or directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o077):
            return "invalid", None
        receipt = _safe_file(_READINESS_PATH, 4096)
    except FileNotFoundError:
        return "absent", None
    except Exception:
        return "invalid", None
    if (set(receipt) != {"schemaVersion", "status", "observedAtUnix", "source", "client", "chatId"}
            or type(receipt.get("schemaVersion")) is not int or receipt["schemaVersion"] != 1
            or receipt.get("status") != "PREFLIGHT_COMPLETED"
            or receipt.get("source") != "online-code-mode"
            or receipt.get("client") is not None or receipt.get("chatId") is not None
            or not _finite(receipt.get("observedAtUnix"))):
        return "invalid", None
    age = now - float(receipt["observedAtUnix"])
    if age < -1:
        return "invalid", None
    if age > _READINESS_MAX_AGE:
        return "expired", round(age, 3)
    return "fresh", round(max(0.0, age), 3)


def _online_code_mode(now: float, observed_at: str,
                      router: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project verified route activity without calling or changing the router.

    The single-object contract is kept (the newest verified-live run, else the newest
    unresolved one); ``activeRuns``, ``queuedRuns``, ``lanes``, ``runCounts``,
    ``admission`` and ``install`` describe every listed run (spec 6.12).  A run ID recorded
    in both journal layouts is named in the evidence (``runIdCollisions``).
    """
    if router is None:
        router = _router_observation(now)
    mode = _online_code_mode_state(now, observed_at, router)
    collisions = router.get("collisions") or []
    if collisions and not router["install"]["inProgress"]:
        mode["evidence"] += (f"; run ID {', '.join(collisions)} has both a legacy active.json and an active/ record "
                             "(one run ID in both journal layouts); settle it with an exact reconcile gate")
    return mode


def _online_code_mode_state(now: float, observed_at: str, router: dict[str, Any]) -> dict[str, Any]:
    readiness, readiness_age = _launcher_readiness(now)
    mode: dict[str, Any] = {
        "state": "unknown", "active": None, "blinking": False,
        "client": None, "chatId": None, "routeId": None,
        "taskState": "unknown", "setupState": readiness,
        "setupAgeSeconds": readiness_age,
        "evidence": "Router journal unavailable; online code mode activity unknown",
        "observedAt": observed_at,
        "activeRuns": [], "queuedRuns": [], "lanes": _router_lanes([]),
        "runCounts": {"running": 0, "queued": 0, "unresolved": 0},
        "admission": None, "install": None, "runIdCollisions": [], "runsTruncated": False,
    }
    mode.update(_router_projection(router, now))
    if router["install"]["inProgress"]:
        # R2.9: a transition (fence installing / rolling-back, or the installer's second link
        # on owner.lock) is "router install in progress", never an unsafe journal.
        mode.update(taskState="install-in-progress",
                    evidence="Router install in progress; route state is verified again when it finishes")
        return mode
    if not _router_namespace_initialized():
        return mode
    if router["error"]:
        mode["evidence"] = router["error"]
        return mode
    primary = router["primary"]
    if primary is None:
        mode["taskState"] = "idle"
        if router["install"]["state"] in ("missing", "invalid"):
            mode.update(state="inactive", active=False,
                        evidence=f"No active route; the router install fence is {router['install']['state']}, so the "
                                 "router refuses every command until its owner finishes or rolls back the install")
        elif readiness == "fresh":
            mode.update(state="ready", active=False,
                        evidence=f"Recent capability inventory completed {readiness_age:.0f}s ago; no active route or client/chat binding")
        elif readiness == "absent":
            mode.update(state="inactive", active=False,
                        evidence="Initialized router journal has no active route or recorded recent readiness check")
        elif readiness == "expired":
            mode.update(state="inactive", active=False,
                        evidence="Task idle; last capability inventory is older than ten minutes, so current setup readiness is unverified")
        else:
            mode.update(state="inactive", active=False,
                        evidence="Task idle; launcher readiness receipt is invalid or unreadable, so current setup readiness is unverified")
        return mode
    counts = mode["runCounts"]
    several = counts["running"] + counts["queued"] > 1
    if primary["layout"] == "legacy":
        if primary["runId"] is None or primary["state"] == "unreadable" and primary.get("_verdict") is None:
            mode["evidence"] = "Router active record invalid or unreadable"
            return mode
        verdict = primary.get("_verdict")
        if verdict == "invalid":
            mode["evidence"] = "Router active record is not a valid bound route"
            return mode
        mode["routeId"] = primary["runId"]
        if verdict == "processing":
            # A stalled route (no checkpoint within the freshness limit) is live but stops blinking.
            mode.update(state="processing", active=True, blinking=not primary["stalled"], taskState="processing",
                        evidence=primary["evidence"])
        elif verdict == "unfinished":
            mode.update(taskState="unfinished", evidence=primary["evidence"])
        else:
            mode["evidence"] = primary["evidence"]
        return mode
    mode["routeId"] = primary["runId"]
    if primary["live"]:
        # lsof names the openers of the lock file, not its flock holder: the probe proves the
        # lock is held, and a matching work process is only the likely holder.
        process = (f"its run lock is held; matching work process pid {primary['pid']} has it open"
                   if primary["pid"] is not None
                   else "its run lock is held; the holding process could not be attributed")
        # The primary is queued only when no run is running (a running run is chosen first).
        waiting = primary["state"] in ("waiting", "admitting")
        what = (f"Queued router run {primary['runId']} is waiting"
                + (f" for {primary['note']['resource']}" if primary["note"] and primary["note"]["resource"] else "")
                if waiting else f"Router run {primary['runId']} is live")
        # A queued-only primary is listed and active, but it is not running work: it never
        # reads or blinks as processing (taskState 'queued').
        mode.update(state="processing", active=True, blinking=not waiting,
                    taskState="queued" if waiting else "processing",
                    client=primary["client"],
                    evidence=(f"{counts['running']} routed task(s) running · {counts['queued']} queued; " if several else "")
                    + f"{what}; {process}; chat binding unavailable")
        return mode
    mode["taskState"] = "unfinished"
    held = primary["lockHeld"]
    mode["evidence"] = {
        "archived-uncleared": f"Router run {primary['runId']} was archived but its active record was not cleared; run reconcile-archived",
        "unverified": (f"Router run {primary['runId']} has an active record, but its run lock is held while that "
                       "record was not read in this sample, so the lock's identity cannot be checked; liveness "
                       "cannot be proven" if held and primary["lock"] == "held" else
                       f"Router run {primary['runId']} has an active record, but its run lock file is "
                       f"{primary['lock']}; liveness cannot be proven"),
        "unreadable": f"Router run {primary['runId']} has an active record that could not be read whole"
                      + ("; its run lock is held, so it may still be live" if held else ""),
    }.get(primary["state"], f"Router run {primary['runId']} is unresolved (its run lock is free); settle it with an exact reconcile gate")
    return mode


def _bounded_http() -> dict[str, Any]:
    connection = http.client.HTTPConnection(_API_HOST, _API_PORT, timeout=1.0)
    try:
        connection.request("GET", _API_PATH, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise OSError("status endpoint unavailable")
        raw = response.read(_MAX_HTTP + 1)
        if len(raw) > _MAX_HTTP:
            raise ValueError("status response too large")
        return _json_object(raw, _MAX_HTTP)
    finally:
        connection.close()


def _find_lms() -> str | None:
    executable = shutil.which("lms")
    if not executable:
        fixed = _HOME / ".lmstudio/bin/lms"
        if fixed.is_file() and os.access(fixed, os.X_OK):
            executable = str(fixed)
    return executable


def _bounded_lms() -> dict[str, Any]:
    executable = _find_lms()
    if not executable:
        raise FileNotFoundError("lms unavailable")
    process = subprocess.Popen([executable, "ps", "--json"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               close_fds=True, start_new_session=True)
    assert process.stdout is not None
    output = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 1.0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("lms status timed out")
            events = selector.select(min(remaining, 0.1))
            if events:
                part = os.read(process.stdout.fileno(), min(65536, _MAX_CLI + 1 - len(output)))
                if not part:
                    break
                output.extend(part)
                if len(output) > _MAX_CLI:
                    raise ValueError("lms output too large")
            elif process.poll() is not None:
                # Drain remaining bytes after process exit.
                part = os.read(process.stdout.fileno(), min(65536, _MAX_CLI + 1 - len(output)))
                if not part:
                    break
                output.extend(part)
                if len(output) > _MAX_CLI:
                    raise ValueError("lms output too large")
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise OSError("lms status failed")
        value = json.loads(output.decode("utf-8"))
        if not isinstance(value, list):
            raise ValueError("lms response is not a list")
        return {"models": value}
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def _text(value: Any, limit: int = 160) -> str | None:
    if (isinstance(value, str) and value and len(value) <= limit and value.isascii()
            and all(ord(char) >= 32 and ord(char) != 127 for char in value)):
        return value
    return None


def _integer(value: Any, minimum: int = 0, maximum: int = 2**53 - 1) -> int | None:
    if type(value) is not int or value < minimum or value > maximum:
        return None
    return value


def _metadata_label(value: Any, limit: int = 96) -> str | None:
    """Project short model labels without arbitrary local metadata or task text."""
    candidate = _text(value, limit)
    if candidate and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+()/\-]*", candidate):
        return candidate
    return None


def _api_model_metadata(item: dict[str, Any]) -> dict[str, Any]:
    quantization = item.get("quantization")
    quantization = quantization if isinstance(quantization, dict) else {}
    capabilities = item.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    reasoning = capabilities.get("reasoning")
    options: list[str] | None = None
    if isinstance(reasoning, dict) and isinstance(reasoning.get("allowed_options"), list):
        options = [label for raw in reasoning["allowed_options"][:16]
                   if (label := _metadata_label(raw, 32)) is not None]
    instances = item.get("loaded_instances")
    loaded_instances: list[dict[str, Any]] | None = None
    if isinstance(instances, list):
        loaded_instances = []
        for raw in instances[:16]:
            if not isinstance(raw, dict):
                continue
            instance_id = _text(raw.get("id")) or _text(raw.get("identifier"))
            if not instance_id or not _MODEL_ID.fullmatch(instance_id):
                continue
            config = raw.get("config")
            config = config if isinstance(config, dict) else {}
            loaded_instances.append({
                "id": instance_id,
                "context": _integer(config.get("context_length"), 1),
                "parallel": _integer(config.get("parallel"), 1, 10000),
                "remainingTtlSeconds": _integer(raw.get("remaining_ttl_seconds")),
            })
    return {
        "type": _metadata_label(item.get("type")),
        "publisher": _metadata_label(item.get("publisher")),
        "architecture": _metadata_label(item.get("architecture")),
        "quantization": _metadata_label(quantization.get("name")),
        "bitsPerWeight": _integer(quantization.get("bits_per_weight"), 1, 128),
        "parameters": _metadata_label(item.get("params_string")),
        "format": _metadata_label(item.get("format")),
        "capabilities": {
            "vision": capabilities.get("vision") if type(capabilities.get("vision")) is bool else None,
            "toolUse": (capabilities.get("trained_for_tool_use")
                        if type(capabilities.get("trained_for_tool_use")) is bool else None),
            "reasoning": (True if isinstance(reasoning, dict) else reasoning
                          if type(reasoning) is bool else None),
            "reasoningOptions": options,
        },
        "loadedInstances": loaded_instances,
        "loadedInstanceCount": len(instances) if isinstance(instances, list) else None,
    }


def _model_row(model_id: str, name: str, *, state: str, loaded: bool | None,
               source: str, age: float | None, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = raw or {}
    # Only exact status strings documented/observed by LM Studio are actionable.
    raw_status = raw.get("status")
    if raw_status == "generating":
        state = "generating"
    elif raw_status in ("busy", "processingPrompt"):
        # LM Studio reports this exact phase while evaluating the prompt,
        # before it transitions to generating output tokens.
        state = "busy"
    elif raw_status == "idle":
        state = "idle"
    elif raw_status is not None:
        state = "loaded" if loaded is True else "unknown"
    queued = _integer(raw.get("queued"))
    return {
        "id": model_id,
        "name": name,
        "host": "mac",
        "state": state,
        "loaded": loaded,
        "queued": queued,
        "parallel": _integer(raw.get("parallel"), 1, 10000),
        "context": _integer(raw.get("contextLength"), 1),
        "sizeBytes": _integer(raw.get("sizeBytes"), 1),
        "source": source,
        "ageSeconds": age,
        "role": None,
        "metadata": None,
        "modelKey": None,
        "loadedInstanceIds": None,
        "instanceId": None,
    }


def _parse_api(payload: dict[str, Any], now: float) -> list[dict[str, Any]]:
    entries = payload.get("models")
    if not isinstance(entries, list):
        raise ValueError("model list missing")
    rows = []
    for item in entries[:256]:
        if not isinstance(item, dict):
            continue
        candidate = _text(item.get("key")) or _text(item.get("id"))
        if not candidate or not _MODEL_ID.fullmatch(candidate):
            continue
        model_id = candidate
        name = _metadata_label(item.get("display_name")) or model_id
        instances = item.get("loaded_instances")
        loaded = (bool(instances) if isinstance(instances, list) else None)
        # API v1 is inventory evidence, not evidence that generation is happening.
        row = _model_row(model_id, name,
                               state=("loaded" if loaded is True else
                                      "unloaded" if loaded is False else "unknown"),
                               loaded=loaded, source="lmstudio-api", age=0.0,
                               raw={"contextLength": item.get("max_context_length"),
                                    "sizeBytes": item.get("size_bytes")})
        row["metadata"] = _api_model_metadata(item)
        key = _text(item.get("key"))
        row["modelKey"] = key if key and _MODEL_ID.fullmatch(key) else None
        instances_detail = row["metadata"]["loadedInstances"]
        raw_instances = item.get("loaded_instances")
        row["loadedInstanceIds"] = (
            [instance["id"] for instance in instances_detail]
            if (isinstance(raw_instances, list) and len(raw_instances) <= 16
                and instances_detail is not None and len(instances_detail) == len(raw_instances))
            else None)
        rows.append(row)
    return rows


def _parse_lms(payload: dict[str, Any], now: float) -> list[dict[str, Any]]:
    entries = payload.get("models")
    if not isinstance(entries, list):
        raise ValueError("model list missing")
    rows = []
    for item in entries[:256]:
        if not isinstance(item, dict):
            continue
        candidate = (_text(item.get("identifier")) or _text(item.get("modelKey"))
                     or _text(item.get("key")) or _text(item.get("id")))
        if not candidate or not _MODEL_ID.fullmatch(candidate):
            continue
        model_id = candidate
        name = _metadata_label(item.get("displayName")) or model_id
        # Restrict generation claims to exact reported values. Unknown enums stay unknown.
        raw = {key: item.get(key) for key in
               ("status", "queued", "parallel", "contextLength", "sizeBytes")}
        raw["max_context_length"] = item.get("max_context_length")
        row = _model_row(model_id, name, state="loaded", loaded=True,
                         source="lms-ps", age=0.0, raw=raw)
        key = _text(item.get("modelKey")) or _text(item.get("key"))
        row["modelKey"] = key if key and _MODEL_ID.fullmatch(key) else None
        identifier = _text(item.get("identifier"))
        row["instanceId"] = (identifier if identifier and _MODEL_ID.fullmatch(identifier)
                             else None)
        rows.append(row)
    return rows


def _merge_cli_rows(models_by_id: dict[str, dict[str, Any]],
                    cli_rows: list[dict[str, Any]]) -> None:
    api_by_key = {row["modelKey"]: row for row in models_by_id.values()
                  if row.get("source") == "lmstudio-api" and row.get("modelKey")}
    for row in cli_rows:
        previous = models_by_id.get(row["id"]) or api_by_key.get(row["modelKey"])
        if previous:
            if row["context"] is None:
                row["context"] = previous["context"]
            if row["sizeBytes"] is None:
                row["sizeBytes"] = previous["sizeBytes"]
            if row["metadata"] is None:
                row["metadata"] = previous["metadata"]
            if row["name"] == row["id"]:
                row["name"] = previous["name"]
            if row["modelKey"] is None:
                row["modelKey"] = previous["modelKey"]
            # Only the same-ID row may inherit the API's complete instance
            # list. An alias remains a separate CLI row; duplicating the list
            # would make independent API confirmation appear twice.
            if row["loadedInstanceIds"] is None and row["id"] == previous["id"]:
                row["loadedInstanceIds"] = previous["loadedInstanceIds"]
        models_by_id[row["id"]] = row


def _pipeline_rows(observation: dict[str, Any], now: float) -> list[dict[str, Any]]:
    """One bounded row per listed run (spec 6.12 ``pipelines``), newest first."""
    rows = []
    for row in observation["rows"][:_ROUTER_PROJECTED]:
        note = row["note"] or {}
        recorded = row["recordedUnix"] if row["recordedUnix"] is not None else row["startedUnix"]
        until = note.get("untilUnix")
        rows.append({"runId": row["runId"], "status": row["state"], "stage": row["stage"],
                     "host": row["host"], "client": row["client"], "live": row["live"],
                     "layout": row["layout"], "recoveryRequired": row["recoveryRequired"],
                     "ageSeconds": _age(recorded, now), "resource": note.get("resource"),
                     "secondsLeft": round(max(0.0, until - now), 1) if until is not None else None})
    return rows


def _marker_attribution(marker: dict[str, Any], live_ids: set[str], now: float) -> str | None:
    """The live run a 5-key Nisi marker belongs to, by exact runId only (spec R2.9 rule 5):
    strip exactly one suffix among .draft/.review/.mac-return/.answer (``<src>.jev.review``
    gives ``<src>.jev``) and compare for equality; never by prefix.  Only a whole, well-formed
    marker (the form pipeline_integrations writes: operation draft/review/answer, a sha256
    digest, a finite start not in the future) is attributed; anything else stays recovery."""
    started = marker.get("started_unix")
    if (marker.get("kind") != _PENDING_KIND or set(marker) != _PENDING_ROUTER_KEYS
            or not isinstance(marker.get("runId"), str) or not _PENDING_RUN_ID.fullmatch(marker["runId"])
            or marker.get("operation") not in _PENDING_OPERATIONS
            or not isinstance(marker.get("input_sha256"), str) or not _SHA256.fullmatch(marker["input_sha256"])
            or not _finite(started) or started > now + 5):
        return None
    child = marker["runId"]
    for suffix in _ROUTER_MARKER_SUFFIXES:
        if child.endswith(suffix) and child[:-len(suffix)] in live_ids:
            return child[:-len(suffix)]
    return None


def _pipeline(now: float, router: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    pipeline: dict[str, Any] = {
        "runId": None, "status": "unknown", "stage": None,
        "recoveryRequired": False, "pendingMarkerObserved": False, "ageSeconds": None,
        "pendingMarkerAgeSeconds": None, "pendingMarkerOwner": None,
        "pendingMarkerAttributedTo": None, "pendingMarkerForeign": False, "pendingMarkerUnreadable": False,
        "queuePhase": None, "queueResource": None,
        "steps": [{"id": key, "label": label, "state": "pending"} for key, label in _STEPS],
        "usage": {"inputTokens": None, "outputTokens": None, "totalTokens": None},
        "authorModel": None, "reviewerModel": None, "pipelines": [],
    }
    source = {"id": "pipeline-router", "label": "Pipeline", "state": "unavailable",
              "ageSeconds": None, "detail": "Router state unavailable"}
    if router is None:
        router = _router_observation(now)
    primary = router["primary"]
    try:
        if router["install"]["inProgress"]:
            pipeline["status"] = "installing"
            raise _RouterInstalling()
        if router["error"]:
            raise ValueError(router["error"])
        if primary is None:
            raise FileNotFoundError("no router run record")
        if _router_queued(primary):
            # A live run queued for admission or a lane (its note says waiting / admitting):
            # listed, never a running route and never promoted to one.
            # queuePhase / queueResource let the UI say "queued for a lane" only for a run whose
            # note says it waits for one ('admitting' waits for admission, not a lane).
            pipeline.update(runId=primary["runId"], status="queued", stage=primary["stage"],
                            queuePhase=primary["state"],
                            queueResource=(primary["note"] or {}).get("resource"))
            source.update(state="live", detail=f"Router run {primary['runId']} is queued; its run lock is held")
            raise _RouterListed()
        active = primary["_record"]
        if isinstance(active, dict) and not primary["valid"]:
            # A readable JSON object that fails the closed record envelope describes nothing.
            raise ValueError("router run record invalid")
        if not isinstance(active, dict):
            if primary["runId"] is None or primary["layout"] == "legacy" or not primary["readable"]:
                raise ValueError("router run record unreadable")
            # A run listed without a record read in this sample: past the read budget (never
            # live then), or a live pre-begin run whose note says running (no record yet).
            pipeline.update(runId=primary["runId"], status="unresolved", stage=primary["stage"])
            source.update(state="live" if primary["live"] else "stale",
                          detail="Router run listed; its checkpoint was not read in this sample")
            raise _RouterListed()
        run_id = _text(active.get("runId"), 80)
        stage = _text(active.get("stage"), 64)
        if not run_id or not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid router run id")
        if stage and not _STAGE.fullmatch(stage):
            stage = None
        checkpoint = active.get("checkpoint") if isinstance(active.get("checkpoint"), dict) else {}
        recorded = checkpoint.get("recordedUnix")
        # A verified-live route (a per-run run lock, or R2.9 legacy rules (i)-(iv)) may sit at
        # 'started', wait at mac_return or stall past the freshness limit; its liveness is the
        # lock, not the age.
        live = primary["live"]
        age = _age(recorded if recorded is not None or not live else active.get("startedUnix"), now)
        evidence = checkpoint.get("evidence") if isinstance(checkpoint.get("evidence"), dict) else {}
        recovery = evidence.get("recoveryRequired") is True
        pipeline.update(runId=run_id, status="unresolved", stage=stage,
                        recoveryRequired=recovery, ageSeconds=age)
        # Checkpoints describe recorded progress, not a live generation lock.
        source.update(state="live" if live or age is not None and age <= _MAX_AGE else "stale",
                      ageSeconds=age,
                      detail="Recorded router checkpoint; live execution is unverified")
        if not live and (age is None or age > _MAX_AGE):
            pipeline["status"] = "stale"
            for step in pipeline["steps"]:
                step["state"] = "unknown"
        elif stage:
            # A stage label is a checkpoint label, not proof that earlier stages
            # completed or that this stage is executing now.
            current = next((step for step in pipeline["steps"] if step["id"] == stage), None)
            if current and recovery:
                current["state"] = "blocked"
            if recovery:
                pipeline["status"] = "recovery-required"
    except _RouterInstalling:
        source.update(state="unavailable",
                      detail="Router install in progress; route state is verified again when it finishes")
    except _RouterListed:
        pass
    except FileNotFoundError:
        if _router_namespace_initialized():
            pipeline["status"] = "idle"
            source.update(state="live", detail="No unresolved route record in initialized router journal")
        else:
            pipeline["status"] = "unknown"
            source["detail"] = "Router journal absent or uninitialized; current route state cannot be verified"
    except Exception:
        source["state"] = "error"
        source["detail"] = "Router state invalid or unreadable"
        pipeline["status"] = "unknown"
    live_ids = {row["runId"] for row in router["rows"] if row["live"] and row["runId"]}
    try:
        try:
            marker = _safe_file(_PENDING_PATH, 4096)
        except _RecordChanged:
            # Spec 6.12: 4 KiB, one retry; a marker still changing after it is unreadable.
            time.sleep(_ROUTER_READ_RETRY_S)
            marker = _safe_file(_PENDING_PATH, 4096)
        pipeline["pendingMarkerObserved"] = True
        pipeline.update(_pending_marker_facts(marker, now))
        attributed = _marker_attribution(marker, live_ids, now)
        if attributed is not None:
            # R2.9 rule 5: the in-flight call of a verified-live run, not a recovery.
            pipeline["pendingMarkerAttributedTo"] = attributed
            source["detail"] = f"Nisi call in flight for live route {attributed}"
        else:
            pipeline["recoveryRequired"] = True
            # Only the single-run router's anonymous 3-key marker may be the live primary's own
            # call (one route at a time).  A 5-key marker that names no live run, or any marker
            # beside a per-run primary, belongs to no live route: it stays recovery-required.
            pipeline["pendingMarkerForeign"] = (set(marker) == _PENDING_ROUTER_KEYS or primary is None
                                                or primary["layout"] != "legacy")
            if pipeline["status"] not in ("unknown", "stale", "installing", "queued"):
                pipeline["status"] = "recovery-required"
            source["detail"] = "Nisi pending marker exists; recovery state is unresolved"
    except FileNotFoundError:
        pass
    except Exception:
        source["state"] = "error"
        source["detail"] = "Nisi marker invalid or unreadable"
        pipeline["pendingMarkerUnreadable"] = True
    pipeline["pipelines"] = _pipeline_rows(router, now)
    return pipeline, source


class _RouterInstalling(Exception):
    """The router install fence or barrier says a transition is in progress."""


class _RouterListed(Exception):
    """The primary run is listed without a record read in this sample."""


_PENDING_KIND = "codemode.nisi.pending.v1"
_PENDING_KEYS = frozenset({"kind", "started_unix", "input_sha256"})
_PENDING_ROUTER_KEYS = _PENDING_KEYS | {"runId", "operation"}
_PENDING_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PENDING_OPERATIONS = ("draft", "review", "answer")     # pipeline_integrations.IDENTITY_MODES


def _pending_marker_facts(marker: dict[str, Any], now: float) -> dict[str, Any]:
    """The Nisi pending marker's age and owner, for the monitor's Fix Nisi Inference summary.

    Only the launcher's exact 3-key form (owner "anonymous (legacy)") or the router's
    5-key form (owner = its runId) is described; any other shape, a future start time
    or a malformed runId leaves both unknown. The input digest is never copied.
    """
    unknown = {"pendingMarkerAgeSeconds": None, "pendingMarkerOwner": None}
    keys = set(marker)
    started = marker.get("started_unix")
    if (marker.get("kind") != _PENDING_KIND or keys not in (_PENDING_KEYS, _PENDING_ROUTER_KEYS)
            or not _finite(started) or started > now + 5):
        return unknown
    if keys == _PENDING_ROUTER_KEYS:
        run_id = marker.get("runId")
        if not isinstance(run_id, str) or not _PENDING_RUN_ID.fullmatch(run_id):
            return unknown
        owner = run_id
    else:
        owner = "anonymous (legacy)"
    return {"pendingMarkerAgeSeconds": int(max(0.0, now - started)), "pendingMarkerOwner": owner}


def _mark_live_route(pipeline: dict[str, Any], source: dict[str, Any], mode: dict[str, Any]) -> None:
    """A verified live owner turns an in-flight record into a running route.

    Every in-flight model call leaves a Nisi pending marker for crash safety.
    Only when Online Code Mode verified the live work process that owns this
    exact run is that marker expected; otherwise it still means recovery.
    """
    if (mode.get("state") == "processing" and pipeline.get("runId")
            and mode.get("routeId") == pipeline["runId"]
            and pipeline.get("status") in ("recovery-required", "unresolved")):
        # A marker that belongs to no live run (pendingMarkerForeign) is not this route's call:
        # the route runs, and that marker still needs recovery.
        foreign = pipeline.get("pendingMarkerForeign") is True
        pipeline.update(status="running", recoveryRequired=foreign, liveOwnerVerified=True)
        for step in pipeline["steps"]:
            if step["state"] == "blocked" and not foreign:
                step["state"] = "active"
        attributed = pipeline.get("pendingMarkerAttributedTo")
        detail = ("Live route; its run lock (legacy: owner lock and work process) was verified"
                  + (f"; Nisi call in flight for live route {attributed}" if attributed else "")
                  + ("; a Nisi pending marker belongs to no live route" if foreign else ""))
        if pipeline.get("pendingMarkerUnreadable"):
            # The route is live, but a marker that could not be read stays an error.
            source.update(state="error", detail=detail + "; Nisi marker invalid or unreadable")
        else:
            source.update(state="live", detail=detail)


def _canary(now: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project the private durable ledger, never dispatch or inspect task text.

    The ledger timestamps only creation, claim and hold transitions. The age
    below is the newest *timestamped* transition, not a scheduler heartbeat or
    evidence that a target or model is running.
    """
    summary: dict[str, Any] = {
        "enabled": None, "stateCounts": None, "outstanding": None,
        "latestRecordedTransitionAgeSeconds": None,
    }
    source = {"id": "canary-ledger", "label": "Canary", "state": "unavailable",
              "ageSeconds": None, "detail": "Durable Canary ledger unavailable; scheduler status unknown"}
    try:
        directory = _CANARY_ROOT.lstat()
        if (_CANARY_ROOT.is_symlink() or not stat.S_ISDIR(directory.st_mode)
                or directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o077):
            raise ValueError("unsafe ledger directory")
        state = _safe_file(_CANARY_PATH, _MAX_CANARY_STATE)
        if (set(state) != {"schema", "config", "configHash", "enabled", "events", "integrityHash"}
                or type(state["schema"]) is not int or state["schema"] != 1
                or type(state["enabled"]) is not bool or not isinstance(state["config"], dict)
                or not isinstance(state["events"], dict)):
            raise ValueError("invalid ledger schema")

        def digest(value: Any) -> str:
            raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
            return hashlib.sha256(raw).hexdigest()

        if (state["configHash"] != digest(state["config"])
                or state["integrityHash"] != digest({key: value for key, value in state.items()
                                                       if key != "integrityHash"})):
            raise ValueError("ledger integrity mismatch")

        counts = {key: 0 for key in _CANARY_STATES}
        outstanding = 0
        latest = None
        for event_id, event in state["events"].items():
            if (not isinstance(event_id, str) or not isinstance(event, dict)
                    or event.get("eventId") != event_id or event.get("state") not in counts):
                raise ValueError("invalid ledger event")
            phase = event["state"]
            created = event.get("createdAt")
            if type(created) is not int or created <= 0:
                raise ValueError("invalid ledger timestamp")
            stamps = [created]
            if phase in ("CLAIMED", "SENT", "RECEIVED", "VERIFIED"):
                claim = event.get("claimAt")
                if type(claim) is not int or claim <= 0:
                    raise ValueError("invalid claim timestamp")
                stamps.append(claim)
            if phase == "HELD":
                held = event.get("heldAt")
                if (type(held) is not int or held <= 0
                        or type(event.get("heldBlocksTarget")) is not bool):
                    raise ValueError("invalid hold metadata")
                stamps.append(held)
            latest = max([latest, *stamps]) if latest is not None else max(stamps)
            counts[phase] += 1
            # Match ledger.claim()'s limit rule. READY is queued, not claimed;
            # resolved HELD records do not block a target.
            if phase in ("CLAIMED", "SENT", "RECEIVED") or (phase == "HELD" and event["heldBlocksTarget"]):
                outstanding += 1
        age = _age(latest, now) if latest is not None and latest <= now + 1 else None
        summary.update(enabled=state["enabled"], stateCounts=counts,
                       outstanding=outstanding, latestRecordedTransitionAgeSeconds=age)
        source.update(state="recorded", ageSeconds=age,
                      detail=(f"Durable ledger {'enabled' if state['enabled'] else 'disabled'}; "
                              f"{len(state['events'])} recorded events, {outstanding} outstanding. "
                              "Scheduler and target activity unverified"))
    except FileNotFoundError:
        pass
    except Exception:
        source.update(state="error", detail="Canary ledger invalid or unreadable; scheduler status unknown")
    return summary, source


class _WindowsWorkerPaused(Exception):
    """A passive queue read was withheld because a reader may be unsafe."""


class _WindowsWorkerCondition(Exception):
    """The worker heartbeat is fresh but it reports no usable model lane."""


class _WindowsWorkerBusy(Exception):
    """The passive queue read was deferred while another owner read is active."""


def _windows_worker_stuck_readers() -> int | None:
    """Count this user's uninterruptible SharedChami owner readers; None if unknown.

    A nested reader in macOS U state cannot be reaped by process-group kill.
    Inspect only the local process table before another queue or readiness read.
    Match only the dispatcher's nested I/O child and chami-ensure's exact
    bounded probe child; do not infer from arbitrary SMB or Python processes.
    """
    global _WINDOWS_WORKER_LAST_STUCK
    count: int | None = None
    try:
        listing = _bounded_command(["/bin/ps", "-axo", "uid=,state=,command=", "-ww"],
                                   limit=_MAX_CLI, timeout=1.0,
                                   cancel=_WINDOWS_WORKER_CANCEL)
        if listing:
            suffixes = (f"{_WINDOWS_WORKER_CMD} _io-child",
                        f"{_SHAREDCHAMI_ENSURE} --probe",
                        f"{_SHAREDCHAMI_ENSURE} --probe-read-only")
            parsed = stuck = 0
            for line in listing.splitlines():
                fields = line.split(None, 2)
                if len(fields) != 3:
                    continue
                uid, state, command = fields
                if not uid.isdigit() or not state:
                    continue
                parsed += 1
                if (uid == str(os.getuid()) and state.startswith("U")
                        and any(command.endswith(suffix) for suffix in suffixes)):
                    stuck += 1
            count = stuck if parsed else None
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        count = None
    _WINDOWS_WORKER_LAST_STUCK = count
    return count


def set_windows_worker_tolerance(count: int | None) -> None:
    """Record the stuck readers present at a verified successful queue read."""
    global _WINDOWS_WORKER_TOLERATED
    with _WINDOWS_WORKER_TOLERANCE_LOCK:
        _WINDOWS_WORKER_TOLERATED = count if type(count) is int and 0 <= count <= 10000 else 0


def _windows_worker_reader_blocked() -> bool:
    """Fail closed if a new SharedChami owner reader is uninterruptible."""
    global _WINDOWS_WORKER_TOLERATED
    count = _windows_worker_stuck_readers()
    if count is None:
        return True
    with _WINDOWS_WORKER_TOLERANCE_LOCK:
        if count < _WINDOWS_WORKER_TOLERATED:
            _WINDOWS_WORKER_TOLERATED = count
        return count > _WINDOWS_WORKER_TOLERATED


def _windows_lanes(value: Any) -> dict[str, Any] | None:
    """Revalidate the dispatcher's optional per-lane detail; any fault drops it all."""
    if not isinstance(value, dict) or set(value) != set(_WINDOWS_LANE_NAMES):
        return None
    lanes: dict[str, Any] = {}
    for worker_lane, name in _WINDOWS_LANE_NAMES.items():
        row = value[worker_lane]
        if not isinstance(row, dict):
            return None
        slots, model = row.get("slots"), _metadata_label(row.get("alias"))
        if (type(row.get("up")) is not bool or model is None or row.get("kind") not in ("qwen", "gpt-oss")
                or not (slots is None or (isinstance(slots, dict) and type(slots.get("busy")) is int
                                          and type(slots.get("total")) is int
                                          and 0 <= slots["busy"] <= slots["total"] <= 64))):
            return None
        lanes[name] = {"up": row["up"], "model": model, "kind": row["kind"],
                       "slotsBusy": None if slots is None else slots["busy"],
                       "slotsTotal": None if slots is None else slots["total"]}
    return lanes


def _windows_lane_detail(record: dict[str, Any]) -> dict[str, Any] | None:
    """One heartbeat's lane detail: the revalidated lanes, None when the dispatcher sent none, or an
    empty dict when it reported lanesError or sent detail this observer rejects (a valid detail always
    names both lanes, so {} is unambiguous). _windows_worker publishes {} as lanesError."""
    if record.get("lanesError") is not None:
        return {}
    if record.get("lanes") is None:
        return None
    lanes = _windows_lanes(record["lanes"])
    return lanes if lanes is not None else {}


def _windows_gpus(value: Any) -> list[dict[str, Any]] | None:
    """Revalidate the relayed GPU rows: 1-4 rows, unique indexes, only the known keys, every number
    finite and in bounds, used memory at most total. Any fault drops them all (None)."""
    if not isinstance(value, list) or not 1 <= len(value) <= _WINDOWS_GPU_MAX:
        return None
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in value:
        if not isinstance(row, dict):
            return None
        index, name = row.get("index"), row.get("name")
        if (type(index) is not int or not 0 <= index <= 63 or index in seen or not isinstance(name, str)
                or not _WINDOWS_GPU_NAME.fullmatch(name) or name.strip() != name):
            return None
        seen.add(index)
        clean: dict[str, Any] = {"index": index, "name": name}
        for key, low, high in _WINDOWS_GPU_LIMITS:
            if _finite_number(row.get(key), low, high) is None:
                return None
            clean[key] = row[key]
        if clean["memoryUsedMiB"] > clean["memoryTotalMiB"]:
            return None
        rows.append(clean)
    return sorted(rows, key=lambda row: row["index"])


def _windows_hardware(record: dict[str, Any]) -> dict[str, Any]:
    """One heartbeat's worker version and GPU rows, each None when absent or rejected."""
    version = record.get("worker_version")
    return {"workerVersion": version if isinstance(version, str) and _WINDOWS_WORKER_VERSION.fullmatch(version) else None,
            "gpus": _windows_gpus(record.get("gpus"))}


def _windows_headless(now: float) -> dict[str, Any]:
    """Project pc-llm's switch with its read_mode rules: anything unsafe, malformed,
    future-dated, expired or off reads as off. Numbers must be real, finite numbers."""
    def off(reason: str) -> dict[str, Any]:
        return {"state": "off", "reason": reason, "expiresInSeconds": None, "grantedBy": None}
    try:
        info = _WINDOWS_HEADLESS_PATH.lstat()
    except FileNotFoundError:
        return off("never turned on")
    except OSError:
        return off("mode file unreadable")
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        return off("mode file unsafe")
    try:
        mode = _safe_file(_WINDOWS_HEADLESS_PATH, 4096)
    except (OSError, ValueError):
        return off("mode file unreadable")
    if (mode.get("kind") != _WINDOWS_HEADLESS_KIND or type(mode.get("schemaVersion")) is not int
            or mode["schemaVersion"] != 1):
        return off("mode file malformed")
    if mode.get("state") != "on":
        return off("turned off")
    granted, expires = _finite_number(mode.get("grantedAtUnix")), _finite_number(mode.get("expiresAtUnix"))
    if granted is None or expires is None:
        return off("mode file malformed")
    if granted > now + 60:
        return off("mode file future-dated")
    if expires <= now:
        return off("expired")
    if expires - granted > 12 * 3600 + 60:  # pc-llm never grants more than 12 hours
        return off("mode file malformed")
    by = mode.get("grantedBy")
    return {"state": "on", "reason": None, "expiresInSeconds": round(expires - now),
            "grantedBy": by if isinstance(by, str) and _WINDOWS_CLIENT.fullmatch(by) else None}


def _windows_worker_probe() -> tuple[float, list[str], int, dict[str, Any] | None, dict[str, Any]] | None:
    """Read only the registered PC heartbeat inventory through its bounded client.

    This is not a Windows inference feed and cannot establish a native monitor
    install, model activity, or a completed remote task.
    """
    if not _WINDOWS_WORKER_IO_LOCK.acquire(blocking=False):
        raise _WindowsWorkerBusy()
    try:
        return _windows_worker_probe_locked()
    finally:
        _WINDOWS_WORKER_IO_LOCK.release()


def _windows_worker_probe_locked() -> tuple[float, list[str], int, dict[str, Any] | None, dict[str, Any]] | None:
    """Perform one heartbeat read while holding the shared SMB I/O gate."""
    if _windows_worker_reader_blocked():
        raise _WindowsWorkerPaused(_WINDOWS_WORKER_LAST_STUCK)
    try:
        info = _WINDOWS_WORKER_CMD.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022
                or not os.access(_WINDOWS_WORKER_CMD, os.X_OK)):
            return None
        # The dispatcher exits 2 when the worker is not ready but still prints
        # the validated heartbeat reason, which is what this observer needs.
        output = _bounded_command([str(_WINDOWS_WORKER_CMD), "status", "--json", "--no-list"],
                                  limit=4096, timeout=_WINDOWS_WORKER_COMMAND_TIMEOUT,
                                  cancel=_WINDOWS_WORKER_CANCEL, ok_codes=(0, 2))
        record = _json_object(output.encode("utf-8"), 4096)
        age = record.get("age")
        raw_models = record.get("models")
        if (record.get("ok") is False and record.get("evidence_scope") == _WINDOWS_WORKER_SCOPE
                and record.get("reason") in _WINDOWS_WORKER_CONDITIONS
                and _finite(age) and 0 <= age <= 300):
            raise _WindowsWorkerCondition(_WINDOWS_WORKER_CONDITIONS[record["reason"]], float(age),
                                          _windows_lane_detail(record), _windows_hardware(record))
        if (record.get("ok") is not True or record.get("evidence_scope") != _WINDOWS_WORKER_SCOPE
                or not _finite(age)
                or age < 0 or age > 300
                or not isinstance(raw_models, list) or not 1 <= len(raw_models) <= 128):
            return None
        models = [_metadata_label(value) for value in raw_models]
        if any(value is None for value in models):
            return None
        return (time.time() - float(age), models[:8], len(models), _windows_lane_detail(record),
                _windows_hardware(record))
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        return None


def _windows_worker_refresh(cache: dict[str, Any]) -> None:
    """Complete one optional read without holding the snapshot sampling lock."""
    global _WINDOWS_WORKER_THREAD
    try:
        observed = _windows_worker_probe()
        with _WINDOWS_WORKER_LOCK:
            cache["probeError"] = observed is None
            cache["probePaused"] = False
            cache["probeBusy"] = False
            cache["stuckReaders"] = 0
            cache["retryAfterMonotonic"] = (time.monotonic() + _WINDOWS_WORKER_FAILURE_BACKOFF_SECONDS
                                            if observed is None else 0.0)
            if observed is not None:
                cache["heartbeatUnix"], cache["modelsAdvertised"], cache["modelCount"], cache["lanes"] = observed[:4]
                cache["hardware"] = observed[4] if len(observed) > 4 else None
                cache["workerCondition"] = None
    except _WindowsWorkerCondition as condition:
        kind, age, lanes, *extra = condition.args
        with _WINDOWS_WORKER_LOCK:
            cache.update(probeError=False, probePaused=False, probeBusy=False, stuckReaders=0,
                         retryAfterMonotonic=0.0, heartbeatUnix=time.time() - age,
                         modelsAdvertised=[], modelCount=0, workerCondition=kind, lanes=lanes,
                         hardware=extra[0] if extra else None)
    except _WindowsWorkerPaused as paused:
        reason = paused.args[0] if paused.args else None
        with _WINDOWS_WORKER_LOCK:
            cache["probeError"] = False
            cache["probePaused"] = True
            cache["probeBusy"] = False
            cache["stuckReaders"] = reason if type(reason) is int else None
    except _WindowsWorkerBusy:
        with _WINDOWS_WORKER_LOCK:
            cache["probeBusy"] = True
    except Exception:
        # No command output or local path is allowed into the public snapshot.
        with _WINDOWS_WORKER_LOCK:
            cache["probeError"] = True
            cache["probePaused"] = False
            cache["probeBusy"] = False
            cache["retryAfterMonotonic"] = time.monotonic() + _WINDOWS_WORKER_FAILURE_BACKOFF_SECONDS
    finally:
        with _WINDOWS_WORKER_LOCK:
            cache["probeRunning"] = False
            if _WINDOWS_WORKER_THREAD is threading.current_thread():
                _WINDOWS_WORKER_THREAD = None


@contextmanager
def hold_windows_worker_probe(timeout: float = _WINDOWS_WORKER_COMMAND_TIMEOUT + 1.5) -> Iterator[bool]:
    """Pause passive heartbeat reads; yield whether the last one has finished."""
    _WINDOWS_WORKER_HOLD.set()
    try:
        with _WINDOWS_WORKER_LOCK:
            thread = _WINDOWS_WORKER_THREAD
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        yield thread is None or not thread.is_alive()
    finally:
        _WINDOWS_WORKER_HOLD.clear()


def cancel_windows_worker_probe() -> None:
    """Request prompt cancellation from the server's SIGTERM handler."""
    _WINDOWS_WORKER_CANCEL.set()


def stop_windows_worker_probe(timeout: float = 0.8) -> bool:
    """Stop optional PC I/O before server exit, including its nested reader."""
    cancel_windows_worker_probe()
    with _WINDOWS_WORKER_LOCK:
        thread = _WINDOWS_WORKER_THREAD
    if thread is None or thread is threading.current_thread():
        return True
    thread.join(timeout=timeout)
    return not thread.is_alive()


def _windows_worker(now: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project optional PC availability without coupling monitor startup to it."""
    global _WINDOWS_WORKER_THREAD
    with _WINDOWS_WORKER_LOCK:
        cache = _WINDOWS_WORKER_CACHE
        checked = time.monotonic()
        if (not _WINDOWS_WORKER_CANCEL.is_set() and not _WINDOWS_WORKER_HOLD.is_set()
                and not cache.get("probeRunning")
                and checked - cache["checkedMonotonic"] >= _WINDOWS_WORKER_POLL_SECONDS
                and checked >= cache.get("retryAfterMonotonic", 0.0)):
            cache["checkedMonotonic"] = checked
            cache["probeRunning"] = True
            try:
                thread = threading.Thread(target=_windows_worker_refresh, args=(cache,),
                                          name="windows-worker-heartbeat", daemon=False)
                _WINDOWS_WORKER_THREAD = thread
                thread.start()
            except RuntimeError:
                cache["probeRunning"] = False
                cache["probeError"] = True
                _WINDOWS_WORKER_THREAD = None
        heartbeat = cache["heartbeatUnix"]
        age = _age(heartbeat, now)
        available = age is not None and age <= _WINDOWS_WORKER_MAX_AGE
        probe_error = cache.get("probeError") is True
        probe_paused = cache.get("probePaused") is True
        # A Fix or Online Code check holding SharedChami defers passive reads.
        probe_busy = cache.get("probeBusy") is True or _WINDOWS_WORKER_HOLD.is_set()
        stuck = cache.get("stuckReaders")
        stuck = stuck if type(stuck) is int and 0 <= stuck <= 10000 else None
        # Only a counted stuck reader recommends the share recovery in Fix.
        hint = (f"; {stuck} stuck SharedChami reader{'s' if stuck != 1 else ''}: open Fix inference > "
                "Route pipeline to recover the share") if probe_paused and stuck else ""
        if available:
            detail = "Recent registered Windows worker heartbeat; inventory only, inference unknown"
            if probe_paused:
                detail += "; current refresh paused because a SharedChami reader is uninterruptible or cannot be ruled out" + hint
            elif probe_busy:
                detail += "; current heartbeat refresh deferred while another monitor owner check is reading SharedChami"
            elif probe_error:
                detail += "; current heartbeat probe inconclusive"
        elif probe_paused:
            detail = ("Passive queue reads paused because a SharedChami reader is uninterruptible or cannot be ruled out"
                      + hint + "; heartbeat availability unknown")
        elif probe_busy:
            detail = "Heartbeat check deferred while another monitor owner check is reading SharedChami; heartbeat availability unknown"
        elif probe_error:
            detail = "Windows worker probe inconclusive; heartbeat availability unknown"
        elif cache.get("probeRunning"):
            detail = "Checking registered Windows worker heartbeat; availability unknown"
        else:
            detail = "Windows worker heartbeat not yet verified"
        condition = cache.get("workerCondition") if available else None
        hardware = cache.get("hardware") if available and isinstance(cache.get("hardware"), dict) else None
        if condition == "degraded":
            detail = ("Windows worker heartbeat is fresh, but no model lane answers (worker reports degraded); "
                      "start the model servers on the PC")
        elif condition == "stopped":
            detail = "Windows worker reports it has stopped; start the worker on the PC"
        worker = {
            "state": condition if condition in ("degraded", "stopped") else "advertised" if available else "unknown",
            "ageSeconds": age,
            "modelsAdvertised": list(cache["modelsAdvertised"]) if available else [],
            "modelCount": cache["modelCount"] if available else 0,
            "detail": detail,
            "stuckReaders": stuck if probe_paused else 0,
            # Advisory lane detail from the same fresh heartbeat; never shown once the
            # heartbeat is stale. chami-dispatch attaches lanes only to a ready heartbeat
            # today, so a degraded one arrives without them and the map says the heartbeat
            # has no lane detail. Lanes on a degraded heartbeat pass through unchanged once
            # the dispatcher sends them; a stopped worker writes none.
            "lanes": ({name: dict(row) for name, row in cache["lanes"].items()}
                      if available and cache.get("lanes") else None),
            # Detail that was sent but rejected (by the dispatcher or here) is not "no detail".
            "lanesError": _WINDOWS_LANES_MALFORMED if available and cache.get("lanes") == {} else None,
            # Worker 1.2: its version and GPU sample from the same fresh heartbeat (None when not sent,
            # rejected or stale). A GPU row is a heartbeat reading, not proof that a job is running.
            "workerVersion": hardware.get("workerVersion") if hardware else None,
            "gpus": [dict(row) for row in hardware["gpus"]] if hardware and hardware.get("gpus") else None,
        }
    source = {"id": "windows-worker", "label": "Windows worker",
              "state": "error" if probe_paused or probe_error or worker["state"] in ("degraded", "stopped")
              else ("recorded" if available else "unavailable"),
              "ageSeconds": age, "detail": worker["detail"]}
    return worker, source


def _finite_number(value: Any, minimum: float = 0.0, maximum: float = 1e12) -> float | None:
    if not _finite(value) or not minimum <= value <= maximum:
        return None
    return float(value)


def _pid_alive(pid: int | None) -> bool:
    """True unless the journaled waiting client has certainly exited."""
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _read_windows_journal() -> bytes | None:
    """Read the tail of the local journal without following links; None if absent."""
    try:
        before = _WINDOWS_JOBS_PATH.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("journal is not a regular file")
    fd = os.open(str(_WINDOWS_JOBS_PATH), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError("journal is not an owner-private regular file")
        start = max(0, info.st_size - _WINDOWS_JOBS_MAX)
        os.lseek(fd, start, os.SEEK_SET)
        chunks: list[bytes] = []
        size = 0
        while size < _WINDOWS_JOBS_MAX:
            part = os.read(fd, min(65536, _WINDOWS_JOBS_MAX - size))
            if not part:
                break
            chunks.append(part)
            size += len(part)
        raw = b"".join(chunks)
        # A mid-file start can split one record; drop that partial first line.
        return raw.split(b"\n", 1)[1] if start and b"\n" in raw else raw
    finally:
        os.close(fd)


def _windows_job_evidence(job: dict[str, Any]) -> dict[str, Any]:
    """The validated evidence fields of one job row; the flags list is copied, never shared."""
    return {key: list(job[key]) if key == "flags" else job[key] for key in _WINDOWS_JOB_EVIDENCE}


def _windows_jobs(now: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fold the dispatcher's local job journal into in-flight and recent jobs.

    The journal holds no prompt or output text. A recorded success means the
    dispatcher validated a returned remote result for that job at that time; it
    is not a certification and does not prove the worker is generating now.
    """
    summary: dict[str, Any] = {"schemaVersion": 1, "journal": "absent", "inFlight": [],
                               "recent": [], "lastSuccess": None, "unsettled": 0, "clientsRecent": {}}
    source = {"id": "windows-jobs", "label": "Windows jobs", "state": "unavailable",
              "ageSeconds": None, "detail": "No Windows job has been journaled on this Mac yet"}
    try:
        raw = _read_windows_journal()
    except (OSError, ValueError):
        summary["journal"] = "invalid"
        source.update(state="error", detail="Windows job journal is unreadable or not owner-private")
        return summary, source
    if raw is None:
        return summary, source
    jobs: dict[str, dict[str, Any]] = {}
    latest: float | None = None
    for line in raw.splitlines()[-2000:]:
        try:
            record = _json_object(line, 4096)
        except (ValueError, UnicodeError):
            continue
        job_id, event = record.get("id"), record.get("event")
        unix = _finite_number(record.get("unix"), 0.0, now + 5)
        if (record.get("v") != 1 or event not in _WINDOWS_JOBS_EVENTS or unix is None
                or not isinstance(job_id, str) or not _WINDOWS_JOB_ID.fullmatch(job_id)):
            continue
        latest = unix if latest is None else max(latest, unix)
        job = jobs.setdefault(job_id, {"id": job_id, "state": None, "model": None,
                                       "enqueuedUnix": None, "settledUnix": None,
                                       "timeout": None, "elapsedSeconds": None, "pid": None,
                                       "client": None, "lane": None, "predictedPerSecond": None,
                                       "completionTokens": None, "promptPerSecond": None,
                                       "promptTokens": None, "flags": [], "cancelRequested": False})
        model = _metadata_label(record.get("model"))
        if model:
            job["model"] = model
        client = record.get("client")
        if isinstance(client, str) and _WINDOWS_CLIENT.fullmatch(client):
            job["client"] = client.lower()
        lane = record.get("lane")
        if isinstance(lane, str) and lane in _WINDOWS_LANE_NAMES:
            job["lane"] = _WINDOWS_LANE_NAMES[lane]
        elif job.get("lane") is None and model in _WINDOWS_MODEL_LANES:
            job["lane"] = _WINDOWS_MODEL_LANES[model]
        if event == "enqueued":
            timeout, pid = record.get("timeout"), record.get("pid")
            job["enqueuedUnix"] = unix
            job["timeout"] = timeout if type(timeout) is int and 1 <= timeout <= 600 else 600
            job["pid"] = pid if type(pid) is int and 1 < pid < 2**31 else None
            if job["state"] is None:
                job["state"] = "in-flight"
        elif event == "result":
            status = record.get("status")
            if status not in _WINDOWS_JOB_RESULTS:
                continue
            if status == "cancelled":
                # Settled: the worker stopped or skipped the job. There is no answer, so no speed,
                # size or flag is kept, even if an earlier record carried some.
                job.update(state="cancelled", settledUnix=unix,
                           elapsedSeconds=_finite_number(record.get("elapsedSeconds"), 0.0, 86400.0),
                           predictedPerSecond=None, completionTokens=None, promptPerSecond=None,
                           promptTokens=None, flags=[])
                continue
            # A validated result is terminal and wins over any earlier timeout. Prompt (read) speed
            # and size sit beside generation speed; flags keep only the known ones, in a fixed order.
            flags = record.get("flags")
            job.update(state=status, settledUnix=unix,
                       elapsedSeconds=_finite_number(record.get("elapsedSeconds"), 0.0, 86400.0),
                       predictedPerSecond=_finite_number(record.get("predictedPerSecond"), 0.0, 1e6),
                       completionTokens=_integer(record.get("completionTokens"), 0, 2**31 - 1),
                       promptPerSecond=_finite_number(record.get("promptPerSecond"), 0.0, 1e6),
                       promptTokens=_integer(record.get("promptTokens"), 0, 2**31 - 1),
                       flags=[flag for flag in _WINDOWS_JOB_FLAGS
                              if isinstance(flags, list) and flag in flags[:16]])
        elif event == "cancel-requested":
            # Informational: the dispatcher asked the worker to stop this job. It stays open (and
            # counts as unsettled) until the worker's 'cancelled' result, or any other result, arrives.
            job["cancelRequested"] = True
        elif job["state"] not in _WINDOWS_JOB_RESULTS:
            job.update(state="uncertain" if event == "publish-uncertain" else
                       "invalid" if event == "invalid-result" else "unresolved",
                       settledUnix=unix)
    in_flight: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    last_success: dict[str, Any] | None = None
    unsettled = 0
    for job in jobs.values():
        if job["state"] == "in-flight":
            started, budget = job["enqueuedUnix"], job["timeout"] + 30
            # Safety gates count every job without a result inside its window,
            # even if its waiting client died: the PC may still be running it.
            if now - started <= budget:
                unsettled += 1
            if now - started <= budget and _pid_alive(job["pid"]):
                in_flight.append({"id": job["id"], "model": job["model"],
                                  "ageSeconds": _age(started, now), "timeoutSeconds": job["timeout"],
                                  "cancelRequested": job["cancelRequested"], **_windows_job_evidence(job)})
                continue
            # The dispatcher stops waiting at timeout + 30 s, and a client that
            # died journals nothing; either way nobody is waiting any more. With
            # no terminal record, settle at the last evidence (the enqueue) so the
            # row ages normally and never outranks a later real result.
            job.update(state="unresolved", settledUnix=started)
        settled = job["settledUnix"]
        if settled is None:
            continue
        row = {"id": job["id"], "state": job["state"], "model": job["model"],
               "elapsedSeconds": job["elapsedSeconds"], "ageSeconds": _age(settled, now),
               **_windows_job_evidence(job)}
        if now - settled <= _WINDOWS_JOBS_RECENT_SECONDS:
            recent.append(row)
        if (job["state"] == "success" and job["model"]
                and (last_success is None or row["ageSeconds"] < last_success["ageSeconds"])):
            last_success = row
    in_flight.sort(key=lambda row: row["ageSeconds"])
    recent.sort(key=lambda row: row["ageSeconds"])
    clients: dict[str, int] = {}
    for row in recent[:10]:
        clients[row["client"] or "unknown"] = clients.get(row["client"] or "unknown", 0) + 1
    summary.update(journal="recorded", inFlight=in_flight[:8], recent=recent[:10],
                   lastSuccess=last_success, unsettled=unsettled, clientsRecent=clients)
    age = _age(latest, now) if latest is not None else None
    if in_flight:
        detail = f"{len(in_flight)} Windows job{'s' if len(in_flight) != 1 else ''} in flight"
        state = "live"
    elif last_success is not None:
        detail = "Last journaled Windows result validated; no job in flight"
        state = "recorded"
    else:
        detail = "Windows jobs journaled; no validated result yet"
        state = "recorded"
    source.update(state=state, ageSeconds=age, detail=detail)
    return summary, source


_JEV_CACHE: dict[str, Any] = {"key": None, "when": None, "model": None}


def _last_jev_judgment(now: float) -> tuple[float | None, str | None]:
    """Newest archived route whose shared intake was judged by Jev (file-only).

    Rescanned only when the archive directory changes; ages are recomputed.
    """
    archive = _ROUTER_ROOT / "archive"
    try:
        info = archive.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            return None, None
    except OSError:
        return None, None
    key = (info.st_ino, info.st_mtime_ns)
    if _JEV_CACHE["key"] != key:
        when, model = _scan_jev_judgment(archive)
        _JEV_CACHE.update(key=key, when=when, model=model)
    return _age(_JEV_CACHE["when"], now), _JEV_CACHE["model"]


def _scan_jev_judgment(archive: Path) -> tuple[float | None, str | None]:
    try:
        entries = sorted(((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path))
                          for entry in os.scandir(archive)
                          if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False)),
                         reverse=True)[:12]
    except OSError:
        return None, None
    for _, path in entries:
        try:
            record = _safe_file(path, _MAX_STATE)
        except (OSError, ValueError):
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else record
        intake = (result.get("stages") or {}).get("intake") if isinstance(result, dict) else None
        if (isinstance(intake, dict) and intake.get("status") == "JUDGED"
                and intake.get("kind") == "chami.intake.typesafe.v1"):
            finished = record.get("finishedUnix") or record.get("recordedUnix")
            when = finished if _finite(finished) else path.stat().st_mtime
            return when, _metadata_label(intake.get("model"))
    return None, None


def _route_components(pipeline: dict[str, Any], nisi_v02: dict[str, Any],
                      models_by_id: dict[str, dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """Project Nisi and Jev readiness from local evidence; neither is invoked."""
    loaded = sorted(row["id"] for row in models_by_id.values()
                    if row.get("loaded") is True and "embed" not in str(row.get("id")).lower())
    if pipeline.get("pendingMarkerAttributedTo"):
        # A 5-key marker whose exact runId belongs to a verified-live route: its call is in flight.
        nisi = {"state": "in-use",
                "detail": f"Nisi call in flight for live route {pipeline['pendingMarkerAttributedTo']}"}
    elif pipeline.get("pendingMarkerObserved"):
        nisi = {"state": "unresolved", "detail": "Nisi pending marker present; the owner must recover it"}
    elif nisi_v02.get("runtimeIntegrity") == "VERIFIED" and nisi_v02.get("hostBinding") == "VERIFIED":
        if len(loaded) >= 2:
            # Mirrors the router: the default model authors (and answers chat)
            # when resident; the review is the lexically first other resident.
            if _DEFAULT_ROUTE_MODEL in loaded:
                author = _DEFAULT_ROUTE_MODEL
                reviewer = next(model for model in loaded if model != author)
            else:
                author, reviewer = loaded[0], loaded[1]
            nisi = {"state": "ready",
                    "detail": f"v0.2 pins verified; edit pair {author} (author) + {reviewer} (reviewer); chat {author}",
                    "authorModel": author, "reviewerModel": reviewer}
        elif loaded:
            nisi = {"state": "partial",
                    "detail": f"v0.2 pins verified; only {loaded[0]} is loaded, so Mac edit routes need a second model"}
        else:
            nisi = {"state": "partial", "detail": "v0.2 pins verified; no local model is loaded"}
    elif nisi_v02.get("hostBinding") == "DRIFT":
        nisi = {"state": "needs-action", "detail": "Nisi Inference host binding drifted since activation"}
    else:
        nisi = {"state": "unknown", "detail": "Nisi Inference runtime evidence unavailable"}
    try:
        env = _JEV_ENV_PATH.lstat()
        configured = (stat.S_ISREG(env.st_mode) and env.st_uid == os.getuid()
                      and not stat.S_IMODE(env.st_mode) & 0o077)
    except OSError:
        configured = False
    age, model = _last_jev_judgment(now)
    last = (f"; last judged a route {int(age // 60)} min ago" + (f" ({model})" if model else "")
            if age is not None else "; no archived Jev judgment yet")
    jev = {"state": "configured" if configured else "unavailable",
           "detail": ("Opted in (key file present, not read)" if configured else "No private Jev opt-in file") + last,
           "lastJudgedAgeSeconds": age}
    return [{"id": "nisi", "label": "Nisi route adapter", **nisi},
            {"id": "jev", "label": "Jev", **jev}]


def collect_snapshot() -> dict[str, Any]:
    """Collect bounded local status without touching inference or queue state."""
    sampled = time.time()
    observed = _dt.datetime.fromtimestamp(sampled, _dt.timezone.utc).isoformat().replace("+00:00", "Z")
    sources: list[dict[str, Any]] = []
    models_by_id: dict[str, dict[str, Any]] = {}

    try:
        api_rows = _parse_api(_bounded_http(), sampled)
        sources.append({"id": "lmstudio-api", "label": "LM Studio inventory", "state": "live",
                        "ageSeconds": 0.0, "detail": "Local model inventory sampled"})
        models_by_id.update((row["id"], row) for row in api_rows)
    except TimeoutError:
        sources.append({"id": "lmstudio-api", "label": "LM Studio inventory", "state": "unavailable",
                        "ageSeconds": None, "detail": "Local inventory timed out or unavailable"})
    except Exception:
        sources.append({"id": "lmstudio-api", "label": "LM Studio inventory", "state": "error",
                        "ageSeconds": None, "detail": "Local inventory invalid or unavailable"})

    try:
        cli_rows = _parse_lms(_bounded_lms(), sampled)
        sources.append({"id": "lms-ps", "label": "LM Studio activity", "state": "live",
                        "ageSeconds": 0.0, "detail": "Loaded models and reported activity sampled"})
        _merge_cli_rows(models_by_id, cli_rows)
    except TimeoutError:
        sources.append({"id": "lms-ps", "label": "LM Studio activity", "state": "unavailable",
                        "ageSeconds": None, "detail": "lms status timed out"})
    except Exception:
        sources.append({"id": "lms-ps", "label": "LM Studio activity", "state": "error",
                        "ageSeconds": None, "detail": "lms status invalid or unavailable"})
        # Keep inventory state, but never carry forward activity from an old sample.
        for row in models_by_id.values():
            if row["loaded"] is True:
                row["state"] = "loaded"
            elif row["loaded"] is False:
                row["state"] = "unloaded"

    afm = _afm_status()
    sources.append({"id": "afm-executable", "label": "Apple Foundation Models adapter",
                    "state": "live" if afm["state"] == "executable" else "unavailable",
                    "ageSeconds": 0.0, "detail": "Adapter executable metadata sampled on this Mac; inference not tested"
                    if afm["state"] == "executable" else
                    f"Adapter {afm['state']}; inference not tested"})

    # One observation of both router journal layouts feeds the pipeline, Online Code Mode
    # and (through last_router_observation) the activity rows of this sample.
    router = observe_router(sampled)
    pipeline, router_source = _pipeline(sampled, router)
    sources.append(router_source)
    online_code_mode = _online_code_mode(sampled, observed, router)
    _mark_live_route(pipeline, router_source, online_code_mode)
    windows_worker, worker_source = _windows_worker(sampled)
    try:
        windows_worker["headless"] = _windows_headless(sampled)
    except Exception:
        windows_worker["headless"] = {"state": "off", "reason": "mode file unreadable",
                                      "expiresInSeconds": None, "grantedBy": None}
    sources.append(worker_source)
    sources.append({"id": "windows-telemetry", "label": "Windows", "state": "unavailable",
                    "ageSeconds": None, "detail": "No bounded live Windows feed connected"})
    try:
        windows_jobs, jobs_source = _windows_jobs(sampled)
    except Exception:
        windows_jobs = {"schemaVersion": 1, "journal": "invalid", "inFlight": [],
                        "recent": [], "lastSuccess": None, "clientsRecent": {}}
        jobs_source = {"id": "windows-jobs", "label": "Windows jobs", "state": "error",
                       "ageSeconds": None, "detail": "Windows job journal reader failed"}
    canary, canary_source = _canary(sampled)
    sources.append(canary_source)
    try:
        nisi_v02, nisi_v02_source = collect_nisi_v02(sampled)
    except Exception:
        # An optional private-runtime reader must not interrupt local model
        # activity or turn an older activation receipt into a live signal.
        nisi_v02 = {"schemaVersion": 1, "observedAt": observed,
                    "integration": "private-local-orchestration-route",
                    "version": None, "commit": None, "runtimeIntegrity": "UNKNOWN",
                    "hostBinding": "UNKNOWN", "driftedHostFiles": [],
                    "activationStatus": "UNKNOWN", "activationVerifiedAt": None,
                    "activationAgeSeconds": None, "activationProbe": None,
                    "liveInference": "UNKNOWN", "workflowAcceptance": "UNKNOWN",
                    "releaseAcceptance": "NOT_ESTABLISHED"}
        nisi_v02_source = {"id": "nisi-v02-runtime", "label": "Nisi Inference private runtime",
                           "state": "error", "ageSeconds": None,
                           "detail": "Private Nisi Inference evidence reader unavailable"}
    sources.append(nisi_v02_source)
    sources.append(jobs_source)
    components = _route_components(pipeline, nisi_v02, models_by_id, sampled)
    return {
        "schemaVersion": 1,
        "host": "mac",
        "observedAt": observed,
        "sampledAt": sampled,
        "models": list(models_by_id.values())[:256],
        "afm": afm,
        "sources": sources,
        "pipeline": pipeline,
        "onlineCodeMode": online_code_mode,
        "windowsWorker": windows_worker,
        "windowsJobs": windows_jobs,
        "canary": canary,
        "nisiV02": nisi_v02,
        "components": components,
    }
