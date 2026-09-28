"""Explicit, bounded Online Code Mode maintenance for Inference Monitor.

The monitor remains an observer until a user requests this controller's single
operation.  This module never submits a task, resends a Windows job, calls a
model directly, or writes, moves or deletes an owner state file itself.  The
installed launcher and its owners perform every state transition.  The public
Fix inference action runs the existing guarded local, Nisi and route repairs
once each.  Legacy scoped actions remain available to older clients.

Only the explicit Fix Route control goes further: it recovers a wedged or
missing SharedChami mount (bounded force unmount and registered-user remount with
an empty password, only when
no Windows job is being waited on) and proves the Windows route with one tiny
inference request through the Windows transport owner, which records it as
pending so it is never resent.

Only the explicit Nisi repair component (fix scopes "nisi" and "all") acknowledges
Nisi recovery, and only by running the launcher's own
``--nisi recover --confirm-server-idle`` at most once per click.  That flag is
the caller's attestation that the model server is idle, so the control first
measures it and stops, changing nothing, at the first failed precondition: the
router has no run and is not busy; the pending marker passes the launcher's
private-file checks and is at least ten minutes old; the Nisi owner lock is
free while the router owner lock is held across the recovery; and two samples
2 s apart show every LLM in ``lms ps`` idle with nothing queued and no client
other than LM Studio (or this monitor's own inventory reads) with an open or
half-closed connection to 127.0.0.1:1234.  Afterwards it confirms the launcher moved exactly that marker
to exactly one new recovered file, then reports, from status reads only,
whether two distinct LLMs are resident for the Mac route's author and reviewer
and whether Jev is opted in.  It never loads or unloads a model, sends work to
Nisi or Jev, or touches the PC.  Each Nisi or unified fix outcome is also appended
to a private, bounded monitor journal.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import copy
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
import threading
import time
from typing import Callable

import errno
import fcntl

from model_control import ControlError, _find_lms
from telemetry import (_bounded_command, _bounded_http, _bounded_lms, _finite, _parse_api,
                       _parse_lms, _windows_jobs, _windows_worker_reader_blocked,
                       _windows_worker_stuck_readers, _WINDOWS_WORKER_IO_LOCK,
                       hold_windows_worker_probe, monitor_router_hold, set_windows_worker_tolerance)
from nisi_v02 import probe_nisi_v02_bridge


LAUNCHER = Path.home() / "bin" / "initiate-online-code-mode"
ENTRY = Path.home() / "bin" / "online-code-entry"
MAX_OUTPUT = 262144
MAX_ENTRY_SOURCE = 65536
ENTRY_READINESS = ("readiness",)
ROUTE_STATUS = ("--route", "status")
ROUTE_PREFLIGHT = ("--route", "reconcile-preflight")
WINDOWS_STATUS = ("--windows", "status")
WINDOWS_RECONCILE = ("--windows", "reconcile")
NISI_STATUS = ("--nisi", "status")
# Fix Nisi Inference only: the launcher's own marker transition and Jev's file-only status.
NISI_RECOVER = ("--nisi", "recover", "--confirm-server-idle")
JEV_STATUS = ("--jev", "status")
READINESS = ()
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
JOB_ID = re.compile(r"mac-[A-Za-z0-9-]{12,80}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
STAGES = ("preflight", "prepare", "intake", "backend", "macReturn", "finalValidation")
LOCAL_API_HOST = "127.0.0.1"
LOCAL_API_PORT = 1234
FIX_SCOPES = ("local", "route", "both", "nisi", "all")
NISI_V02_HOST_FILES = frozenset({"nisi_auto_preflight.mjs", "nisi_bridge.mjs",
                                "nisi_local.ts", "nisi_validate.mjs", "pipeline_integrations.py"})
WINDOWS_STATUS_CODES = frozenset({"WINDOWS_EXACT_LANES_UNAVAILABLE", "WINDOWS_UNRESOLVED_JOB"})
DISPATCH = Path.home() / "bin" / "chami-dispatch"
SHARE_MOUNT = "/Volumes/SharedChami"
# The publishable source contains no PC address or account. Both values must
# be supplied by the app owner before Fix Route can inspect or change the mount.
SHARE_USER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
SHARE_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
SHARE_LAN_RANGES = tuple(ipaddress.IPv4Network(cidr) for cidr in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
SHARE_MOUNT_ROW_RE = re.compile(r"^//[^\n]+ on /Volumes/SharedChami \(smbfs[,)]", re.M | re.I)


@dataclass(frozen=True)
class ShareConfig:
    hosts: tuple[str, ...]
    username: str
    mounted_pattern: re.Pattern[str]


def _share_config_from_env() -> ShareConfig | None:
    """Read bounded owner config; absence or malformed input disables recovery."""
    username = os.environ.get("AGIW_SHARE_USERNAME", "")
    raw_hosts = os.environ.get("AGIW_SHARE_HOSTS", "")
    if not SHARE_USER_RE.fullmatch(username) or username.casefold() in {"guest", "anonymous"} or not raw_hosts:
        return None
    hosts = tuple(part.strip() for part in raw_hosts.split(","))
    if not 1 <= len(hosts) <= 3 or any(not host for host in hosts) or len(set(hosts)) != len(hosts):
        return None
    # The first two hosts are used for a bounded TCP reachability check. They
    # must be private LAN IPv4 addresses so DNS cannot extend the recovery
    # budget or redirect an explicit remount to a public SMB endpoint.
    for host in hosts[:2]:
        try:
            address = ipaddress.IPv4Address(host)
        except ipaddress.AddressValueError:
            return None
        if not any(address in subnet for subnet in SHARE_LAN_RANGES):
            return None
    if len(hosts) == 3 and not SHARE_NAME_RE.fullmatch(hosts[2]):
        return None
    mounted_pattern = re.compile(
        r"^//" + re.escape(username) + r":?@(?:" + "|".join(re.escape(host) for host in hosts)
        + r")/SharedChami on /Volumes/SharedChami \(smbfs[,)]", re.M | re.I)
    return ShareConfig(hosts, username, mounted_pattern)
WINDOWS_OWNER_STATE = Path.home() / ".local/state/codemode-windows"
PROBE_SCRIPT = Path(__file__).with_name("windows_probe.py")
PROBE_MODEL_SECONDS = 60
# Owner.request waits the model budget, then a 30 s settlement grace, plus I/O.
PROBE_TIMEOUT = PROBE_MODEL_SECONDS + 30 + 25
PROBE_CODES = frozenset({
    "OWNER_BUSY", "OWNER_UNSAFE", "PENDING_EXISTS", "PUBLICATION_UNCERTAIN",
    "PUBLICATION_INVALID", "RESULT_INVALID", "TIMEOUT", "PENDING_INVALID",
    "INVALID_TIMEOUT", "OWNER_UNAVAILABLE", "WORKER_NOT_READY", "PROBE_FAILED",
    "TRANSPORT_ERROR",
    # Router-concurrency transport (spec R2.9 rule 6): lane tokens, the job binding and the
    # Windows owner's lock and pending identity; none of them publishes a job.
    "LANE_BUSY", "LANE_UNKNOWN", "LANE_UNAVAILABLE", "LANE_UNSAFE",
    "PUBLICATION_INTERRUPTED", "PUBLICATION_BINDING_FAILED", "PUBLICATION_BUDGET_EXHAUSTED",
    "OWNER_LOCK_REPLACED", "PENDING_CHANGED",
    # A transport that refuses to load during a router install or rollback: a probe not-run.
    "ROUTER_INSTALL_IN_PROGRESS", "ROUTER_INSTALL_ROLLED_BACK", "ROUTER_INSTALL_FENCE_INVALID",
    "ROUTER_INSTALL_GENERATION_MISMATCH", "ROUTER_INSTALL_CHANGED", "ROUTER_INSTALL_FENCE_MISSING",
})
# Dispatcher answers that mean the SMB I/O itself failed. A stale heartbeat or a
# stopped worker is PC-side state that no share repair can change.
SHARE_IO_FAULTS = frozenset({
    "timeout", "other", "unmounted", "queue I/O unavailable or timed out",
    "trusted SharedChami mount unavailable",
    "queue parent is missing, linked or not a directory",
})
# Exactly the dispatcher's validate_state reasons: the share was read and the
# PC reported this state. Anything else (errno text, timeouts) is a share fault.
PC_STATE_REASONS = frozenset({
    "heartbeat stale or future-dated", "worker is not running", "worker status degraded",
    "invalid or empty model inventory", "invalid endpoint inventory",
    "unsupported worker schema", "invalid heartbeat timestamp",
})
# The share was read, but the PC wrote bad content; a remount cannot fix it.
PC_CONTENT_ERRORS = ("JSONDecodeError", "UnicodeDecodeError", "duplicate JSON evidence key",
                     "queue evidence exceeds bounded limit", "queue evidence is not a regular file")
ROUTER_OWNER_LOCK = Path.home() / ".local/state/codemode-router/owner.lock"
# What the owner can do on the PC for each worker-reported state.
WINDOWS_UNAVAILABLE_HINTS = {
    "worker status degraded": (": the PC worker is running, but none of its model servers answer. "
                               "Start the model servers on the PC (Qwen on port 1234, gpt-oss-20b on 1235), "
                               "then press Fix again."),
    "worker is not running": ": the PC worker reports it has stopped. Start the worker on the PC, then press Fix again.",
    "heartbeat stale or future-dated": (": the PC worker heartbeat is stale. Check that the PC is awake and its "
                                        "worker task is running, then press Fix again."),
}
# pc-llm is the headless switch's only writer; the monitor asks it, bounded.
# Turning on runs its own one-prompt probe of the PC (90 s) before writing.
PC_LLM = Path.home() / "bin" / "pc-llm"
HEADLESS_COMMANDS = {"on": (("on", "--granted-by", "inference-monitor", "--hours", "4"), 150),
                     "off": (("off",), 15)}
PATH_LIKE = re.compile(r"""(['"])[^'"]*[/\\][^'"]*\1|(?<![\w.])(?:~|[A-Za-z]:)?[/\\][^\s'"]+|[\w.~-]+(?:[/\\][\w.-]+)+""")
# Slash words in pc-llm and chami-dispatch messages ("queue I/O unavailable or timed out",
# "on/off") are not paths; every other path-like token is still redacted.
PATH_KEEP = frozenset({"i/o", "on/off"})
WINDOWS_INVENTORY_REASONS = frozenset({
    "trusted SharedChami mount unavailable", "queue I/O unavailable or timed out",
    "queue parent is missing, linked or not a directory", "heartbeat stale or future-dated",
    "worker is not running", "worker status degraded", "invalid or empty model inventory",
    "invalid endpoint inventory", "unsupported worker schema", "invalid heartbeat timestamp",
})
# Fix Nisi Inference. The launcher caps one Nisi call at 135 s (nisi_run _timeout);
# a marker younger than four such calls, or ten minutes, may still be settling.
NISI_CALL_CAP_SECONDS = 135
NISI_MARKER_MIN_AGE = max(600, 4 * NISI_CALL_CAP_SECONDS)
NISI_MARKER_MAX_BYTES = 4096
NISI_MARKER_KIND = "codemode.nisi.pending.v1"
# nisi_run writes the 3-key form; the router's conversation stages add runId/operation.
NISI_MARKER_KEYS = frozenset({"kind", "started_unix", "input_sha256"})
NISI_ROUTER_MARKER_KEYS = NISI_MARKER_KEYS | {"runId", "operation"}
NISI_MARKER_OPERATIONS = frozenset({"answer", "review"})
MARKER_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
RECOVERED_NAME = re.compile(r"recovered-[0-9a-f]{32}\.json\Z")
NISI_STATUS_KEYS = frozenset({"status", "root", "recoveryRequired", "ownership_scope",
                              "model_inference", "workflow_acceptance"})
NISI_RECOVERED = {"kind": "codemode.integrations.v1", "status": "RECOVERY_ACKNOWLEDGED",
                  "remoteInferenceStopped": "NOT_OBSERVED", "certification": "NOT_RUN"}
NISI_RECOVER_TIMEOUT = 15
SERVER_IDLE_SAMPLE_GAP = 2.0
LMS_TYPES = frozenset({"llm", "embedding"})
LMS_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}\Z")
# The Mac route's default author (pipeline_auto.DEFAULT_MODEL). Like
# select_edit_models, the reviewer is the lexically first other resident LLM.
ROUTE_DEFAULT_AUTHOR = "google/gemma-4-26b-a4b-qat"
LOCAL_API_ENDPOINT = f"{LOCAL_API_HOST}:{LOCAL_API_PORT}"
LSOF = "/usr/sbin/lsof"
LSOF_ARGS = ("-nP", f"-iTCP@{LOCAL_API_ENDPOINT}", "-F", "pcnT")
LSOF_NAME = re.compile(r"(127\.0\.0\.1:[0-9]{1,5})(?:->(127\.0\.0\.1:[0-9]{1,5}))?\Z")
TCP_STATES = frozenset({"CLOSED", "LISTEN", "SYN_SENT", "SYN_RCVD", "ESTABLISHED", "CLOSE_WAIT",
                        "FIN_WAIT_1", "CLOSING", "LAST_ACK", "FIN_WAIT_2", "TIME_WAIT"})
LMSTUDIO_COMMAND = "LM Studio"
FIX_JOURNAL = Path.home() / ".local/state/inference-monitor/fix-journal.jsonl"
FIX_JOURNAL_MAX_BYTES = 262144
FIX_JOURNAL_MAX_LINE = 16384
FIX_NISI_CANCELLED = ("Monitor stopped before Fix Nisi Inference completed. If its recover step had started, "
                      "check Nisi status before retrying. No model inference was run.")
FIX_ALL_CANCELLED = ("Monitor stopped before Fix inference completed. Inspect the recorded steps, "
                     "Nisi status and Windows owner state before retrying; a probe or recovery may have started.")
# `--route status` refusals (exit 3) that describe router state, not a broken router
# (spec R2.9 rule 6, review:compat 1): each stops the check with needs-action and changes
# nothing.  Any other exit-3 code, or an invalid reply, stays an error.
ROUTER_INSTALL_MESSAGE = ("Router install in progress: the router refuses every command until its install or "
                          "rollback finishes. Nothing was repaired; press the button again afterwards.")
# Only these two ROUTER_INSTALL_* codes are a transition that ends by itself (a fence that says
# installing / rolling-back, or a generation that changed under a running command).  The others
# (FENCE_MISSING, FENCE_INVALID, GENERATION_MISMATCH, ROLLED_BACK and any new one) stay until
# the owner acts, so "press the button again afterwards" would be wrong advice for them.
ROUTER_INSTALL_TRANSITIONS = frozenset({"ROUTER_INSTALL_IN_PROGRESS", "ROUTER_INSTALL_CHANGED"})
ROUTER_INSTALL_REFUSING_MESSAGE = ("The router refuses every command ({code}): its install fence is missing, invalid, "
                                   "rolled back or names another code generation. Nothing was repaired; its owner must "
                                   "finish the router install (re-run it with the same staging) or roll it back.")
ROUTE_STATUS_REFUSALS = {
    "ROUTER_OWNER_BUSY": ("busy", "The router owner is busy. Try again after its current work finishes."),
    "ROUTER_MULTIPLE_UNRESOLVED_RUNS": ("multiple-unresolved", "More than one router run needs owner review."),
    "ROUTER_OWNER_LOCK_MISSING": ("owner-action", "The router owner lock is missing, so the router admits no run. "
                                  "Its owner must prove with lsof that nothing holds the old lock, then re-run the "
                                  "router install with the same staging."),
    "ROUTER_OWNER_LOCK_REPLACED": ("owner-action", "The router owner lock was replaced, so the router admits no run. "
                                   "Its owner must prove with lsof that nothing holds the old lock, then re-run the "
                                   "router install with the same staging."),
    "ROUTER_POLICY_BUSY": ("owner-action", "The router's concurrency policy is being changed while runs hold "
                           "admission. Try again when they finish."),
    "ROUTER_BEGIN_UNCERTAIN": ("owner-action", "A router run's start record could not be completed or undone; "
                               "its owner must settle it with an exact reconcile gate."),
}


def _route_status_refusal(code: int | None, value: dict) -> tuple[str, str] | None:
    """(step result, message) for a known `--route status` refusal, else None."""
    refusal = value.get("code") if code == 3 and isinstance(value, dict) else None
    if not isinstance(refusal, str):
        return None
    if refusal.startswith("ROUTER_INSTALL_") and re.fullmatch(r"ROUTER_INSTALL_[A-Z_]{1,40}", refusal):
        if refusal in ROUTER_INSTALL_TRANSITIONS:
            return "install-in-progress", ROUTER_INSTALL_MESSAGE
        return "install-refusing", ROUTER_INSTALL_REFUSING_MESSAGE.format(code=refusal)
    return ROUTE_STATUS_REFUSALS.get(refusal)


ROUTE_ACTIVE_MESSAGE = (
    "An unresolved router run is open; Fix Nisi Inference never recovers Nisi under it, so Nisi recovery was not "
    "attempted. If that run stopped on its own Mac Nisi call, confirm the model server is idle, recover the "
    "marker yourself with initiate-online-code-mode --nisi recover --confirm-server-idle, then reconcile the run "
    "with --route reconcile-review-unavailable or --route reconcile-finish-refused using the recovered file. "
    "Otherwise resolve the run with its owner first.")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes


class _Cancelled(Exception):
    """The monitor is closing; no more owner actions may begin."""


def _kill_private_group(child: subprocess.Popen) -> bool:
    # Every tracked command creates a new session, with its PID as the group ID.
    try:
        os.killpg(child.pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        if child.poll() is not None:
            return True
    except PermissionError:
        # macOS returns EPERM for a group whose only member is the already
        # killed, unreaped leader. Reap it (bounded), then recheck the group.
        try:
            child.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        if child.poll() is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
                return True
            except ProcessLookupError:
                return True
            except OSError:
                pass
    except OSError:
        pass
    # An unexpected group-signal failure must not abort SIGTERM handling.
    # Try the direct child, but retain the uncertain descendant outcome.
    try:
        child.kill()
    except OSError:
        pass
    return False


class _OwnerChildren:
    """Let SIGTERM reach an owner child even while its worker is blocked."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._children: dict[int, subprocess.Popen] = {}
        self.signal_failed = False

    def register(self, child: subprocess.Popen) -> None:
        with self._lock:
            self._children[child.pid] = child
            # Cancellation can win between Popen returning and registration.
            # In that case the new child must be killed before its caller runs.
            if self.cancelled.is_set():
                if not _kill_private_group(child):
                    self.signal_failed = True

    def unregister(self, child: subprocess.Popen, *, cleanup_ok: bool = True) -> None:
        with self._lock:
            if not cleanup_ok:
                self.signal_failed = True
            self._children.pop(child.pid, None)

    def cancel(self) -> bool:
        self.cancelled.set()
        with self._lock:
            # Keep registration/unregistration ordered with each signal so a
            # completed child cannot leave the registry before killpg runs.
            for child in self._children.values():
                if not _kill_private_group(child):
                    self.signal_failed = True
            return not self.signal_failed


_OWNER_THREAD = threading.local()


def _current_owner() -> _OwnerChildren | None:
    return getattr(_OWNER_THREAD, "children", None)


def _raise_if_cancelled(owner: _OwnerChildren | None) -> None:
    if owner is not None and owner.cancelled.is_set():
        raise _Cancelled()


def _entry_source() -> bytes:
    """Snapshot the owner script through pinned, no-follow descriptors.

    The script is fed to the fixed system Python interpreter.  Executing the
    pathname after a separate lstat would allow a rename between check and use.
    """
    directory_fd = os.open(ENTRY.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory = os.fstat(directory_fd)
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or stat.S_IMODE(directory.st_mode) & 0o022):
            raise ValueError("universal entry directory is not owner controlled")
        fd = os.open(ENTRY.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) & 0o077
                    or not stat.S_IMODE(before.st_mode) & 0o100
                    or before.st_nlink != 1 or before.st_size > MAX_ENTRY_SOURCE):
                raise ValueError("universal entry is not an owner executable")
            source = bytearray()
            while len(source) <= MAX_ENTRY_SOURCE:
                chunk = os.read(fd, min(8192, MAX_ENTRY_SOURCE + 1 - len(source)))
                if not chunk:
                    break
                source.extend(chunk)
            after = os.fstat(fd)
            if (len(source) > MAX_ENTRY_SOURCE or len(source) != before.st_size
                    or (before.st_dev, before.st_ino, before.st_size,
                        before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size,
                        after.st_mtime_ns, after.st_ctime_ns)
                    or not source.startswith(b"#!/usr/bin/python3\n")):
                raise ValueError("universal entry source changed or is invalid")
            return bytes(source)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def launcher_runner(args: tuple[str, ...], data: bytes | None, timeout: float) -> CommandResult:
    """Run only fixed owner commands; suppress private diagnostics."""
    if args not in (ROUTE_STATUS, ROUTE_PREFLIGHT, WINDOWS_STATUS,
                    WINDOWS_RECONCILE, NISI_STATUS, NISI_RECOVER, JEV_STATUS,
                    READINESS, ENTRY_READINESS):
        raise ValueError("unsupported launcher operation")
    if args in (NISI_RECOVER, JEV_STATUS) and data is not None:
        raise ValueError("launcher operation accepts no input")
    entry = args == ENTRY_READINESS
    if entry and data is not None:
        raise ValueError("universal entry accepts no input")
    if entry:
        data = _entry_source()
        # The shebang is /usr/bin/python3.  Run exactly those snapshotted bytes
        # with isolated stdlib-only startup; argv contains no script content.
        command = ["/usr/bin/python3", "-I", "-S", "-c",
                   "import sys;exec(compile(sys.stdin.buffer.read(),'<online-code-entry>','exec'))",
                   *args]
    else:
        command = [str(LAUNCHER), *args]
    if data is not None and len(data) > (MAX_ENTRY_SOURCE if entry else 4096):
        raise ValueError("launcher input exceeded limit")
    return _run_bounded(command, data, timeout)


def _run_bounded(command: list[str], data: bytes | None, timeout: float) -> CommandResult:
    """Run one fixed command in its own process group with a hard deadline.

    The group is registered with the current owner so monitor shutdown can
    stop it, and it is killed on every exit path.
    """
    owner = _current_owner()
    _raise_if_cancelled(owner)
    child = subprocess.Popen(command,
                             stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        if owner is not None:
            owner.register(child)
        _raise_if_cancelled(owner)
        assert child.stdout is not None
        chunks = []
        total = 0
        written = 0
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            if data is not None:
                assert child.stdin is not None
                selector.register(child.stdin, selectors.EVENT_WRITE)
            while True:
                _raise_if_cancelled(owner)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                events = selector.select(min(remaining, 0.05) if owner is not None else remaining)
                if not events:
                    continue
                output_complete = False
                for key, _ in events:
                    if key.fileobj is child.stdin:
                        input_closed = False
                        try:
                            written += os.write(child.stdin.fileno(), data[written:written + 8192])
                        except BrokenPipeError:
                            input_closed = True
                        if input_closed or written == len(data) or child.poll() is not None:
                            selector.unregister(child.stdin)
                            child.stdin.close()
                    else:
                        chunk = os.read(child.stdout.fileno(), min(8192, MAX_OUTPUT + 1 - total))
                        if not chunk:
                            output_complete = True
                            break
                        total += len(chunk)
                        if total > MAX_OUTPUT:
                            raise ValueError("launcher output exceeded limit")
                        chunks.append(chunk)
                if output_complete:
                    break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        result = CommandResult(child.wait(timeout=remaining), b"".join(chunks))
        _raise_if_cancelled(owner)
        return result
    finally:
        # Owner commands can finish after an inner SMB timeout while their
        # reader remains blocked. The launcher has a private process group, so
        # clean that group even when its direct child has already exited.
        group_stopped = _kill_private_group(child)
        try:
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            if child.stdout is not None:
                child.stdout.close()
        finally:
            if owner is not None:
                owner.unregister(child, cleanup_ok=group_stopped)
        if not group_stopped:
            raise OSError("launcher process group could not be confirmed terminated")


def _pipe_json(result: CommandResult, limit: int = 65536) -> dict:
    if len(result.stdout) > limit:
        raise ValueError("owner output exceeded limit")
    value = json.loads(result.stdout.decode("utf-8") or "{}",
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("bad number")))
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


class ShareControl:
    """Production SharedChami checks and repairs; every call is bounded."""

    def __init__(self):
        self._config = _share_config_from_env()

    @property
    def configuration_ready(self) -> bool:
        return self._config is not None

    def mounted(self) -> bool | None:
        if self._config is None:
            return None
        owner = _current_owner()
        kwargs = {"limit": 65536, "timeout": 2.0}
        if owner is not None:
            kwargs["cancel"] = owner.cancelled
        try:
            listing = _bounded_command(["/sbin/mount", "-t", "smbfs"], **kwargs)
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            return None
        rows = SHARE_MOUNT_ROW_RE.findall(listing)
        if not rows:
            return False
        # One exact configured identity is required. A foreign or duplicate
        # row at this path is a conflict, even beside a trusted row.
        if len(rows) != 1 or not self._config.mounted_pattern.search(rows[0]):
            return None
        return True

    def stuck_readers(self) -> int | None:
        return _windows_worker_stuck_readers()

    def dispatch_status(self) -> str:
        """'ok', an I/O fault, 'pc-state', 'timeout' or 'other'. One bounded SMB read.

        Only the worker states the dispatcher validates (stale heartbeat,
        degraded or stopped worker, bad inventory) are PC-side. Errno text from
        a dead SMB session, timeouts and unknown reasons stay share faults.
        """
        try:
            reply = _run_bounded(["/usr/bin/python3", "-I", "-S", str(DISPATCH),
                                  "status", "--json", "--no-list"], None, 10)
            value = _pipe_json(reply)
        except subprocess.TimeoutExpired:
            return "timeout"
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            return "other"
        if value.get("ok") is True:
            return "ok"
        reason = value.get("reason")
        if isinstance(reason, str):
            # Errors raised inside the dispatcher's I/O child carry a type prefix.
            if reason.startswith(PC_CONTENT_ERRORS):
                return "pc-state"
            reason = re.sub(r"\A[A-Za-z_][A-Za-z0-9_]*: ", "", reason)
            if reason.startswith(PC_CONTENT_ERRORS):
                return "pc-state"
        if isinstance(reason, str) and reason in SHARE_IO_FAULTS:
            return reason
        return "pc-state" if reason in PC_STATE_REASONS else "other"

    @staticmethod
    @contextmanager
    def _hold_lock(path: Path):
        """Hold an existing owner flock without blocking; yield False if busy."""
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            # A missing owner lock cannot exclude an older or changing owner.
            yield False
            return
        except OSError:
            yield False
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @contextmanager
    def hold_router(self):
        """Hold the router's global owner lock (taken before any host lock).

        A router run that starts while Fix holds it stops at its own
        ROUTER_OWNER_BUSY before writing state, instead of being quarantined.
        The readers are told first (monitor_router_hold), so an EX holder that is
        this Monitor is never shown as a live legacy route (spec R2.9 (iii)); a
        failed attempt holds nothing, so that mark is dropped before the caller
        handles "busy" (a live legacy route is discounted for one flock call only).
        """
        with monitor_router_hold() as mark, self._hold_lock(ROUTER_OWNER_LOCK) as held:
            if not held:
                mark.release()
            yield held

    def hold_owner(self):
        """Hold the Windows transport owner lock; yield False if an owner is active.

        A retained pending record with no lock holder is not activity: its
        result survives a remount and the reconcile step settles it afterwards.
        """
        return self._hold_lock(WINDOWS_OWNER_STATE / "owner.lock")

    def open_jobs(self) -> int | None:
        jobs, _source = _windows_jobs(time.time())
        if jobs.get("journal") == "invalid":
            return None
        unsettled = jobs.get("unsettled")
        return unsettled if type(unsettled) is int else len(jobs.get("inFlight") or [])

    def reachable_host(self) -> str | None:
        if self._config is None:
            return None
        for host in self._config.hosts[:2]:
            try:
                with socket.create_connection((host, 445), timeout=2.0):
                    return host
            except OSError:
                continue
        return None

    def force_unmount(self) -> bool:
        if self._config is None:
            return False
        try:
            return _run_bounded(["/sbin/umount", "-f", SHARE_MOUNT], None, 20).returncode == 0
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def mount(self, host: str) -> bool:
        if self._config is None or host not in self._config.hosts[:2]:
            raise ValueError("untrusted share host")
        script = f'mount volume "smb://{self._config.username}:@{host}/SharedChami"'
        try:
            return _run_bounded(["/usr/bin/osascript", "-e", script], None, 45).returncode == 0
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def settle(self, seconds: float) -> None:
        owner = _current_owner()
        _raise_if_cancelled(owner)
        if owner is not None:
            owner.cancelled.wait(seconds)
        else:
            time.sleep(seconds)
        _raise_if_cancelled(owner)


def _redact_path(match: re.Match) -> str:
    """One PATH_LIKE match: a known slash word stays as written, anything else becomes [path]."""
    token = match.group(0)
    return token if token.strip("'\".").casefold() in PATH_KEEP else "[path]"


def _pc_llm_message(value: object) -> str:
    """pc-llm's own error text: printable, path-free and at most 200 characters."""
    text = "".join(char if char.isprintable() else " " for char in value) if isinstance(value, str) else ""
    text = " ".join(PATH_LIKE.sub(_redact_path, text).split())[:200].strip()
    return text or "pc-llm reported an error; the switch was not changed."


def windows_inference_probe() -> dict:
    """One tiny Windows request through the owner; returns sanitized evidence."""
    try:
        reply = _run_bounded(["/usr/bin/python3", "-I", "-S", str(PROBE_SCRIPT),
                              str(PROBE_MODEL_SECONDS)], None, PROBE_TIMEOUT)
        value = _pipe_json(reply, 4096)
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return {"status": "not-run", "code": "PROBE_FAILED"}
    return value


class _Stop(Exception):
    def __init__(self, status: str, message: str):
        self.status, self.message = status, message


def _strict_json(raw: bytes) -> object:
    """Bounded UTF-8 JSON with unique keys and finite numbers only."""
    if not isinstance(raw, bytes) or len(raw) > MAX_OUTPUT:
        raise ValueError("invalid command output size")

    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def _decode(raw: bytes) -> dict:
    value = _strict_json(raw)
    if not isinstance(value, dict):
        raise ValueError("command output is not an object")
    return value


def _new_readiness_receipt(path: Path, started_at: float) -> bool:
    """Verify a private inventory receipt observed after this invocation began."""
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.fstat(directory_fd)
            if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                    or stat.S_IMODE(directory.st_mode) & 0o077):
                return False
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1
                        or info.st_size > 4096):
                    return False
                raw = os.read(fd, 4097)
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
        if len(raw) > 4096:
            return False
        receipt = _decode(raw)
        if (set(receipt) != {"schemaVersion", "status", "observedAtUnix",
                             "source", "client", "chatId"}
                or type(receipt.get("schemaVersion")) is not int
                or receipt["schemaVersion"] != 1
                or receipt.get("status") != "PREFLIGHT_COMPLETED"
                or receipt.get("source") != "online-code-mode"
                or receipt.get("client") is not None or receipt.get("chatId") is not None):
            return False
        observed = receipt.get("observedAtUnix")
        # _finite never calls math.isfinite on an int (10**400 would raise OverflowError).
        return _finite(observed) and started_at <= observed <= time.time() + 1
    except (OSError, ValueError, TypeError, UnicodeError):
        return False


def _preflight_identity(active: object) -> tuple[str, str] | None:
    """Allow only the two-checkpoint, no-backend refusal shape.

    The router's reconcile-preflight command independently verifies every
    checkpoint and pending store under its owner locks before it archives.
    """
    if (not isinstance(active, dict) or type(active.get("schemaVersion")) is not int
            or active["schemaVersion"] != 1):
        return None
    run_id, digest = active.get("runId"), active.get("inputSha256")
    if (not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)
            or not isinstance(digest, str) or not SHA256.fullmatch(digest)
            or active.get("stage") != "incomplete" or type(active.get("sequence")) is not int
            or active["sequence"] != 2):
        return None
    checkpoint = active.get("checkpoint")
    if (not isinstance(checkpoint, dict) or checkpoint.get("runId") != run_id
            or checkpoint.get("inputSha256") != digest
            or checkpoint.get("stage") != "incomplete"
            or type(checkpoint.get("sequence")) is not int
            or checkpoint["sequence"] != 2):
        return None
    evidence = checkpoint.get("evidence")
    if not isinstance(evidence, dict):
        return None
    stages = evidence.get("stages")
    if (evidence.get("kind") != "codemode.router.v1"
            or evidence.get("status") != "NOT_RUN"
            or evidence.get("code") != "NISI_RECOVERY_REQUIRED"
            or evidence.get("runId") != run_id
            or evidence.get("requestSha256") != digest
            or evidence.get("recoveryRequired") is not True
            or evidence.get("candidate") is not None
            or evidence.get("selectedHost") is not None
            or not isinstance(stages, dict) or set(stages) != set(STAGES)
            or any(stages.get(stage) != {"status": "NOT_RUN"} for stage in STAGES)):
        return None
    return run_id, digest


def _runtime_sources() -> tuple[bool, bool]:
    """Sample the two fresh local feeds; neither proves generation occurred."""
    owner = _current_owner()
    _raise_if_cancelled(owner)
    api_live = False
    activity_live = False
    try:
        _parse_api(_bounded_http(), time.time())
        api_live = True
    except (OSError, ValueError, TimeoutError, TypeError, UnicodeError,
            subprocess.SubprocessError):
        pass
    _raise_if_cancelled(owner)
    try:
        _parse_lms(_bounded_lms(), time.time())
        activity_live = True
    except (OSError, ValueError, TimeoutError, TypeError, UnicodeError,
            subprocess.SubprocessError):
        pass
    _raise_if_cancelled(owner)
    return api_live, activity_live


def _loopback_port_free() -> bool:
    """A failed bind is never interpreted as permission to start a second server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            probe.bind((LOCAL_API_HOST, LOCAL_API_PORT))
            return True
        except OSError:
            return False


def _local_server_stopped() -> bool:
    """Require LM Studio's own bounded JSON status to say stopped."""
    try:
        owner = _current_owner()
        _raise_if_cancelled(owner)
        kwargs = {"limit": 4096, "timeout": 3.0}
        if owner is not None:
            kwargs["cancel"] = owner.cancelled
        output = _bounded_command([_find_lms(), "server", "status", "--json"],
                                  **kwargs)
        value = _decode(output.encode("utf-8"))
    except (ControlError, OSError, ValueError, TimeoutError, TypeError, UnicodeError,
            subprocess.SubprocessError):
        raise _Stop("needs-action", "LM Studio owner status is unavailable or invalid; no server was started.")
    port = value.get("port")
    if (not set(value) <= {"running", "port"}
            or type(value.get("running")) is not bool
            or ("port" in value and port is not None
                and (type(port) is not int or not 1 <= port <= 65535))):
        raise _Stop("needs-action", "LM Studio owner status is ambiguous; no server was started.")
    return value["running"] is False


def _start_local_api() -> bool:
    """Only start the fixed owner-controlled LM Studio loopback API."""
    try:
        executable = _find_lms()
    except ControlError:
        raise _Stop("needs-action", "The owner-controlled LM Studio CLI is unavailable; no server was started.")
    command = [executable, "server", "start", "--port", str(LOCAL_API_PORT),
               "--bind", LOCAL_API_HOST]
    owner = _current_owner()
    _raise_if_cancelled(owner)
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, start_new_session=True)
    group_stopped = True
    try:
        if owner is not None:
            owner.register(child)
        result = child.wait(timeout=20) == 0
        _raise_if_cancelled(owner)
        return result
    except subprocess.TimeoutExpired:
        group_stopped = _kill_private_group(child)
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        if not group_stopped:
            raise _Stop("needs-action", "LM Studio server start timed out, and its owner process group could not be confirmed terminated. Inspect local owner processes before retrying.")
        raise _Stop("needs-action", "LM Studio server start timed out; inspect the local owner state.")
    finally:
        if owner is not None:
            owner.unregister(child, cleanup_ok=group_stopped)


class _Unsafe(Exception):
    """A Nisi state file failed a private-file check; the reason is fixed text."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _private_dir_fd(path: Path) -> int:
    """Open the launcher's state directory with its own checks: no symlink, owner, 0700-class."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise _Unsafe("state directory is missing")
    except OSError:
        raise _Unsafe("state directory is linked or unreadable")
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        os.close(fd)
        raise _Unsafe("state directory is not private")
    return fd


def _read_private(dir_fd: int, name: str, limit: int = NISI_MARKER_MAX_BYTES,
                  subject: str = "marker") -> tuple[bytes, os.stat_result]:
    """One regular, owner-only (0600), singly linked file of at most `limit` bytes, read without following links."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise _Unsafe(f"{subject} is a symbolic link" if error.errno == errno.ELOOP
                      else f"{subject} cannot be opened")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise _Unsafe(f"{subject} is not a regular file")
        if before.st_uid != os.getuid():
            raise _Unsafe(f"{subject} is not owned by this user")
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise _Unsafe(f"{subject} mode is not 0600")
        if before.st_nlink != 1:
            raise _Unsafe(f"{subject} has another hard link")
        if before.st_size > limit:
            raise _Unsafe(f"{subject} is larger than 4 KiB")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(fd, limit + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        if (len(raw) > limit or len(raw) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise _Unsafe(f"{subject} changed while it was read")
        return bytes(raw), before
    finally:
        os.close(fd)


def _parse_nisi_marker(raw: bytes) -> dict:
    """The launcher's exact 3-key marker, or the router conversation's 5-key form."""
    try:
        value = _decode(raw)
    except (ValueError, UnicodeError):
        raise _Unsafe("marker is not valid JSON")
    started = value.get("started_unix")
    digest = value.get("input_sha256")
    if (set(value) not in (NISI_MARKER_KEYS, NISI_ROUTER_MARKER_KEYS)
            or value.get("kind") != NISI_MARKER_KIND
            or not _finite(started)             # never OverflowError on a huge int
            or not isinstance(digest, str) or not SHA256.fullmatch(digest)):
        raise _Unsafe("marker is not a recognized form")
    if set(value) == NISI_ROUTER_MARKER_KEYS:
        run_id = value.get("runId")
        if (not isinstance(run_id, str) or not MARKER_RUN_ID.fullmatch(run_id)
                or value.get("operation") not in NISI_MARKER_OPERATIONS):
            raise _Unsafe("marker is not a recognized form")
    return value


def _format_age(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    return f"{hours // 24} d {hours % 24} h"


def lms_ps_listing() -> object:
    """LM Studio's own loaded-model list (`lms ps --json`), bounded and cancellable."""
    owner = _current_owner()
    _raise_if_cancelled(owner)
    kwargs = {"limit": 524288, "timeout": 5.0}
    if owner is not None:
        kwargs["cancel"] = owner.cancelled
    output = _bounded_command([_find_lms(), "ps", "--json"], **kwargs)
    return _strict_json(output.encode("utf-8"))


def loopback_api_sockets() -> str:
    """lsof's field listing of TCP sockets on 127.0.0.1:1234 (exit 1 means none), bounded."""
    owner = _current_owner()
    _raise_if_cancelled(owner)
    kwargs = {"limit": 65536, "timeout": 5.0, "ok_codes": (0, 1)}
    if owner is not None:
        kwargs["cancel"] = owner.cancelled
    return _bounded_command([LSOF, *LSOF_ARGS], **kwargs)


def _lms_idle_verdict(listing: object) -> tuple[str, str]:
    """'idle' only when every loaded LLM reports status idle with nothing queued."""
    if not isinstance(listing, list) or len(listing) > 256:
        return "unknown", "lms=invalid"
    llms = 0
    for item in listing:
        if not isinstance(item, dict) or item.get("type") not in LMS_TYPES:
            return "unknown", "lms=invalid"
        if item["type"] != "llm":
            continue
        llms += 1
        status, queued = item.get("status"), item.get("queued")
        if not isinstance(status, str) or type(queued) is not int or queued < 0:
            return "unknown", "lms=invalid"
        if status != "idle" or queued != 0:
            label = status if re.fullmatch(r"[A-Za-z_-]{1,32}", status) else "other"
            return "busy", f"lms=llm-{label}; queued={queued}"
    return "idle", f"lms={llms}-llm-idle"


def _lsof_sockets(text: object) -> list[dict]:
    """Parse lsof -F pcnT output; any unexpected shape is an error, never idle."""
    if not isinstance(text, str) or len(text) > 65536:
        raise ValueError("invalid socket listing")
    sockets: list[dict] = []
    pid = None
    command = None
    current = None
    for line in text.split("\n"):
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            if not re.fullmatch(r"[0-9]{1,10}", value):
                raise ValueError("invalid pid")
            pid, command, current = int(value), None, None
        elif tag == "c" and pid is not None and command is None:
            command = value
        elif tag == "f" and pid is not None:
            current = {"pid": pid, "command": command, "name": None, "state": None}
            sockets.append(current)
        elif tag == "n" and current is not None and current["name"] is None:
            current["name"] = value
        elif tag == "T" and current is not None:
            if value.startswith("ST="):
                if current["state"] is not None:
                    raise ValueError("duplicate socket state")
                current["state"] = value[3:]
        else:
            raise ValueError("unexpected socket listing field")
    if any(item["name"] is None or item["state"] not in TCP_STATES for item in sockets):
        raise ValueError("incomplete socket listing")
    return sockets


def _server_client_verdict(sockets: list[dict], own_pid: int) -> tuple[str, str]:
    """'idle' only when LM Studio listens and every connected client end is LM Studio or this monitor.

    A server-side connection whose client end is not visible (another user's
    process, or a client that died) counts as a foreign client, as does a
    connection being opened. Half-closed connections (CLOSE_WAIT, FIN_WAIT_*,
    CLOSING, LAST_ACK) count like open ones: a server end in CLOSE_WAIT is the
    shape of an abandoned request LM Studio may still be generating for. Only
    LISTEN, TIME_WAIT and CLOSED sockets are ignored.
    """
    listeners = [item for item in sockets if item["state"] == "LISTEN"]
    if (not listeners or any(item["name"] != LOCAL_API_ENDPOINT
                             or not isinstance(item["command"], str)
                             or not item["command"].startswith(LMSTUDIO_COMMAND)
                             for item in listeners)):
        return "unknown", "listener=absent-or-not-lm-studio"
    server_pids = {item["pid"] for item in listeners}
    client_ends: list[tuple[str, int]] = []
    peers: list[str] = []
    half_closed = 0
    for item in sockets:
        if item["state"] in ("SYN_SENT", "SYN_RCVD"):
            return "busy", "clients=connecting"
        if item["state"] in ("LISTEN", "TIME_WAIT", "CLOSED"):
            continue
        if item["state"] != "ESTABLISHED":
            half_closed += 1
        match = LSOF_NAME.fullmatch(item["name"])
        if match is None or match.group(2) is None:
            return "unknown", "socket=unrecognized"
        local, remote = match.groups()
        if local == LOCAL_API_ENDPOINT and remote != LOCAL_API_ENDPOINT:
            if item["pid"] not in server_pids:
                return "unknown", "socket=unrecognized"
            peers.append(remote)
        elif remote == LOCAL_API_ENDPOINT and local != LOCAL_API_ENDPOINT:
            client_ends.append((local, item["pid"]))
        else:
            return "unknown", "socket=unrecognized"
    visible = {local for local, _ in client_ends}
    foreign = sum(1 for _, pid in client_ends if pid not in server_pids and pid != own_pid)
    unseen = sum(1 for peer in peers if peer not in visible)
    monitor = sum(1 for _, pid in client_ends if pid == own_pid)
    if foreign or unseen:
        return "busy", f"clients={foreign}; unseen-clients={unseen}; half-closed={half_closed}"
    return "idle", f"clients=0; monitor-reads={monitor}"


def _resident_pair(listing: object) -> tuple[str, str | None, str | None, int]:
    """The Mac route's author/reviewer from `lms ps`: two distinct exact resident LLMs."""
    if not isinstance(listing, list) or len(listing) > 256:
        return "unavailable", None, None, 0
    identifiers: list[str] = []
    llms: list[str] = []
    for item in listing:
        if not isinstance(item, dict) or item.get("type") not in LMS_TYPES:
            return "unavailable", None, None, 0
        identifier, key = item.get("identifier"), item.get("modelKey")
        if (not isinstance(identifier, str) or not LMS_MODEL_ID.fullmatch(identifier)
                or not isinstance(key, str) or not LMS_MODEL_ID.fullmatch(key)):
            return "unavailable", None, None, 0
        identifiers.append(identifier)
        if item["type"] == "llm":
            if identifier != key:
                return "ambiguous", None, None, 0
            llms.append(identifier)
    if len(set(identifiers)) != len(identifiers):
        return "ambiguous", None, None, 0
    ids = sorted(set(llms))
    if len(ids) < 2:
        return "missing", None, None, len(ids)
    author = ROUTE_DEFAULT_AUTHOR if ROUTE_DEFAULT_AUTHOR in ids else ids[0]
    reviewer = next(model for model in ids if model != author)
    return "resident", author, reviewer, len(ids)


@contextmanager
def _hold_router_owner(path: Path | None):
    """Hold the router's owner flock for Fix Nisi Inference and yield what the hold actually did.

    'held' (exclusive flock taken until exit), 'busy' (another owner holds it,
    or it is linked or unopenable), 'absent' (no lock file, so nothing is held)
    or 'not-configured' (a scripted test runner without a router lock path).
    Unlike ShareControl._hold_lock, a missing lock file is never reported as held.
    """
    if path is None:
        yield "not-configured"
        return
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        yield "absent"
        return
    except OSError:
        yield "busy"
        return
    try:
        # Readers first learn that this Monitor may hold owner.lock (spec R2.9 (iii)); a failed
        # attempt holds nothing, so the mark is dropped before "busy" is handled.
        with monitor_router_hold() as mark:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                mark.release()
                yield "busy"
                return
            try:
                yield "held"
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _append_journal(path: Path, record: dict, max_bytes: int = FIX_JOURNAL_MAX_BYTES) -> bool:
    """Append one JSON line to a private (0600) journal kept under max_bytes."""
    try:
        line = (json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("ascii")
        if len(line) > FIX_JOURNAL_MAX_LINE:
            record = {key: record.get(key) for key in ("kind", "action", "operationId", "finishedUnix", "status")}
            record["truncated"] = True
            line = (json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
                    + "\n").encode("ascii")
    except (TypeError, ValueError):
        return False
    temporary = None
    dir_fd = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(dir_fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            return False
        fd = os.open(path.name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=dir_fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                return False
            if info.st_size + len(line) <= max_bytes:
                os.write(fd, line)
                return True
            # Keep the newest whole lines within half the cap, then append.
            keep = max_bytes // 2
            tail = os.pread(fd, keep, max(0, info.st_size - keep))
            if info.st_size > keep:
                cut = tail.find(b"\n")
                tail = tail[cut + 1:] if cut >= 0 else b""
            temporary = f".{path.name}.{os.getpid()}.tmp"
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                          dir_fd=dir_fd)
            try:
                os.write(out, tail + line)
                os.fsync(out)
            finally:
                os.close(out)
            os.replace(temporary, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            temporary = None
            return True
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return False
    finally:
        if dir_fd is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=dir_fd)
                except OSError:
                    pass
            os.close(dir_fd)


class OnlineCodeRepair:
    """One explicit background check/repair at a time, with sanitized progress."""

    def __init__(self, runner: Callable[[tuple[str, ...], bytes | None, float], CommandResult] | None = None,
                 *, nisi_pending_path: Path | None = None, readiness_path: Path | None = None,
                 sleep: Callable[[float], None] | None = None,
                 monotonic: Callable[[], float] | None = None,
                 bridge_probe: Callable[[float], dict] | None = None,
                 share: ShareControl | None = None,
                 inference_probe: Callable[[], dict] | None = None,
                 pc_llm: Path | None = None,
                 clock: Callable[[], float] | None = None,
                 lms_ps: Callable[[], object] | None = None,
                 loopback_sockets: Callable[[], str] | None = None,
                 router_lock_path: Path | None = None,
                 fix_journal_path: Path | None = None):
        self._runner = runner or launcher_runner
        # Production owner calls can open the SharedChami queue. Scripted test
        # runners have no queue I/O and remain injectable without host state.
        self._guard_windows_reader = runner is None
        # Share recovery and the inference probe touch real host state, so a
        # scripted runner gets neither unless a test injects its own fakes.
        self._share = share if share is not None else (ShareControl() if runner is None else None)
        self._last_windows_reason: str | None = None
        self._inference_probe = (inference_probe if inference_probe is not None
                                 else (windows_inference_probe if runner is None else None))
        # Likewise the headless switch: production uses PC_LLM, a scripted
        # runner only a test-injected fake.
        self._pc_llm = pc_llm
        self._headless_enabled = pc_llm is not None or runner is None
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._bridge_probe = bridge_probe or probe_nisi_v02_bridge
        self._nisi_pending_path = nisi_pending_path or Path.home() / ".local/state/codemode-nisi/pending.json"
        self._readiness_path = readiness_path or Path.home() / ".local/state/codemode-launcher/readiness.json"
        # Fix Nisi Inference reads host state (lms ps, lsof, owner locks, journal).
        # A scripted runner gets none of it unless a test injects each piece,
        # so a missing probe fails closed as "unknown" instead of reading the host.
        production = runner is None
        self._clock = clock or time.time
        self._lms_ps = lms_ps if lms_ps is not None else (lms_ps_listing if production else None)
        self._loopback_sockets = (loopback_sockets if loopback_sockets is not None
                                  else (loopback_api_sockets if production else None))
        self._router_lock_path = (router_lock_path if router_lock_path is not None
                                  else (ROUTER_OWNER_LOCK if production else None))
        self._fix_journal_path = (fix_journal_path if fix_journal_path is not None
                                  else (FIX_JOURNAL if production else None))
        self._lock = threading.Lock()
        self._owner_children = _OwnerChildren()
        self._worker: threading.Thread | None = None
        self._operation_id = 0
        self._state = {"status": "idle", "message": "No check requested yet.",
                       "steps": [], "operationId": 0, "action": None}

    def read(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def request(self) -> dict:
        return self._request("check-and-repair")

    def request_entry(self) -> dict:
        return self._request("readiness")

    def request_fix(self, scope: str) -> dict:
        if scope not in FIX_SCOPES:
            raise ValueError("invalid inference repair scope")
        return self._request(f"fix-{scope}")

    def request_headless(self, action: str) -> dict:
        if action not in HEADLESS_COMMANDS:
            raise ValueError("invalid headless action")
        return self._request(f"headless-{action}")

    def _request(self, action: str) -> dict:
        with self._lock:
            if self._owner_children.cancelled.is_set():
                return copy.deepcopy(self._state)
            if self._state["status"] == "running":
                return copy.deepcopy(self._state)
            self._operation_id += 1
            operation_id = self._operation_id
            self._state = {"status": "running", "message": "Checking router and host owner state.",
                           "steps": [], "operationId": operation_id, "action": action}
            if action == "readiness":
                self._state["message"] = "Invoking the computer-wide readiness entry."
            elif action == "fix-all":
                self._state["message"] = "Checking local runtime, Nisi Inference and the Windows route under their owner guards."
            elif action == "fix-nisi":
                self._state["message"] = "Checking the router, Nisi owner and model server before any Nisi recovery."
            elif action.startswith("fix-"):
                self._state["message"] = "Checking the selected inference scope."
            elif action == "headless-on":
                self._state["message"] = "Probing the PC before turning headless on."
            elif action == "headless-off":
                self._state["message"] = "Turning PC headless off."
            worker = threading.Thread(target=self._work, args=(operation_id, action),
                                      name="online-code-repair", daemon=True)
            self._worker = worker
            worker.start()
            return copy.deepcopy(self._state)

    def cancel(self) -> None:
        """Stop private owner commands immediately when the monitor exits."""
        groups_stopped = self._owner_children.cancel()
        with self._lock:
            if self._state["status"] == "running":
                self._state["status"] = "needs-action"
                self._state["message"] = (
                    "Monitor stopped while pc-llm was probing the PC; check the headless switch before retrying."
                    + ("" if groups_stopped else " An owner process group could not be confirmed terminated.")
                    if self._state["action"] == "headless-on" else
                    FIX_NISI_CANCELLED
                    + ("" if groups_stopped else " An owner process group could not be confirmed terminated.")
                    if self._state["action"] == "fix-nisi" else
                    FIX_ALL_CANCELLED
                    + ("" if groups_stopped else " An owner process group could not be confirmed terminated.")
                    if self._state["action"] == "fix-all" else
                    "Monitor stopped before this check completed. No model inference was run."
                    if groups_stopped else
                    "Monitor stopped; an owner process group could not be confirmed terminated. Inspect owner processes before retrying. No model inference was run.")

    def join(self, timeout: float) -> bool:
        """Bound shutdown waiting; the Swift parent has a 1.5-second grace."""
        with self._lock:
            worker = self._worker
        if worker is None or worker is threading.current_thread():
            return True
        worker.join(timeout=timeout)
        return not worker.is_alive()

    def _pause(self, seconds: float) -> None:
        _raise_if_cancelled(self._owner_children)
        if self._sleep is time.sleep:
            self._owner_children.cancelled.wait(seconds)
        else:
            self._sleep(seconds)
        _raise_if_cancelled(self._owner_children)

    def _step(self, name: str, result: str, **identities: str) -> None:
        with self._lock:
            self._state["steps"].append({"name": name, "result": result, **identities})

    def _call(self, args: tuple[str, ...], *, data: bytes | None = None,
              timeout: float = 12) -> tuple[int, dict]:
        _raise_if_cancelled(self._owner_children)
        windows_io = (self._guard_windows_reader
                      and args in (WINDOWS_STATUS, WINDOWS_RECONCILE,
                                   READINESS, ENTRY_READINESS))
        acquired_windows_gate = False
        if windows_io:
            # The passive heartbeat sampler shares this process but runs on a
            # separate thread. Refuse overlapping SMB owner reads immediately;
            # never queue behind a potentially stalled network operation.
            if not _WINDOWS_WORKER_IO_LOCK.acquire(blocking=False):
                self._step("windows-preflight", "paused")
                raise _Stop("needs-action", "SharedChami worker status is already being read; "
                            "wait for that bounded check before retrying.")
            acquired_windows_gate = True
        try:
            if windows_io and _windows_worker_reader_blocked():
                self._step("windows-preflight", "paused")
                raise _Stop("needs-action", "SharedChami worker status could not be read; "
                            "check the mounted connection before retrying.")
            reply = self._runner(args, data, timeout)
        finally:
            if acquired_windows_gate:
                _WINDOWS_WORKER_IO_LOCK.release()
        if (not isinstance(reply, CommandResult) or type(reply.returncode) is not int
                or not isinstance(reply.stdout, bytes)):
            raise ValueError("invalid runner response")
        if args == READINESS:
            return reply.returncode, {}
        return reply.returncode, _decode(reply.stdout)

    def _route_status(self) -> object:
        code, value = self._call(ROUTE_STATUS)
        refusal = _route_status_refusal(code, value)
        if refusal is not None:
            # Router state the owner must act on (or wait out), not a broken router.
            self._step("router-status", refusal[0])
            raise _Stop("needs-action", refusal[1])
        if (code != 0 or type(value.get("schemaVersion")) is not int
                or value["schemaVersion"] != 1
                or "active" not in value or not isinstance(value["active"], (dict, type(None)))):
            self._step("router-status", "unavailable")
            raise _Stop("error", "Router status is unavailable or invalid; no repair was attempted.")
        return value["active"]

    def _nisi_clear(self) -> None:
        code, value = self._call(NISI_STATUS)
        nisi = value.get("nisi")
        if (code != 0 or value.get("kind") != "codemode.integrations.v1"
                or not isinstance(nisi, dict)
                or type(nisi.get("recoveryRequired")) is not bool):
            self._step("nisi-status", "unavailable")
            raise _Stop("error", "Nisi owner status is unavailable; no recovery was attempted.")
        # The launcher inventory reports ordinary files. The router's exact
        # reconcile command also rejects symlinked pending markers under lock.
        if nisi["recoveryRequired"] or os.path.lexists(self._nisi_pending_path):
            self._step("nisi-status", "pending")
            raise _Stop("needs-action", "Nisi recovery remains pending. This control does not acknowledge it.")
        self._step("nisi-status", "clear")

    def _nisi_v02_bridge(self) -> None:
        """Require an explicit current private bridge inventory, not a model run."""
        evidence = self._bridge_probe(time.time())
        if (not isinstance(evidence, dict) or evidence.get("schemaVersion") != 1
                or evidence.get("kind") != "agiw.nisi-v02.bridge-check.v1"
                or evidence.get("modelInference") != "NOT_RUN"
                or evidence.get("workflowAcceptance") != "NOT_RUN"
                or evidence.get("releaseAcceptance") != "NOT_ESTABLISHED"
                or evidence.get("status") not in {"RETURNED", "REFUSED", "UNAVAILABLE", "TIMED_OUT"}
                or evidence.get("inventoryStatus") not in {"LISTED", "PARTIAL", "UNAVAILABLE", "NOT_CONFIGURED", "UNKNOWN"}
                or evidence.get("activationHostBinding") not in {"VERIFIED", "DRIFT", "UNKNOWN"}
                or type(evidence.get("recoveryRequired")) not in (bool, type(None))):
            self._step("nisi-v02-bridge", "invalid")
            raise _Stop("error", "Private Nisi Inference bridge returned invalid status; no model inference was run.")
        listed = (evidence["status"] == "RETURNED"
                  and evidence["inventoryStatus"] == "LISTED"
                  and evidence["recoveryRequired"] is False
                  and type(evidence.get("runtimeCount")) is int
                  and 1 <= evidence["runtimeCount"] <= 8
                  and type(evidence.get("modelCount")) is int
                  and evidence["modelCount"] >= 1)
        binding = evidence["activationHostBinding"]
        drifted = evidence.get("driftedHostFiles", [])
        if (not isinstance(drifted, list) or len(drifted) > len(NISI_V02_HOST_FILES)
                or any(type(name) is not str or name not in NISI_V02_HOST_FILES for name in drifted)
                or len(set(drifted)) != len(drifted)):
            self._step("nisi-v02-bridge", "invalid")
            raise _Stop("error", "Private Nisi Inference bridge returned invalid host-file evidence; no model inference was run.")
        self._step("nisi-v02-bridge", "inventory-verified" if listed and binding == "VERIFIED" else "needs-action",
                   evidence=f"status={evidence['status']}; inventory={evidence['inventoryStatus']}; "
                            f"recovery={'yes' if evidence['recoveryRequired'] is True else 'no' if evidence['recoveryRequired'] is False else 'unknown'}; "
                            f"activationPin={binding}; drifted={','.join(drifted) if drifted else ('unknown' if binding == 'DRIFT' else 'none')}")
        if not listed:
            raise _Stop("needs-action", "Private Nisi Inference bridge inventory is unavailable or recovery is pending. No model inference was run.")
        if binding == "DRIFT":
            files = ", ".join(drifted) if drifted else "an unidentified host file"
            raise _Stop("needs-action", f"Private Nisi Inference inventory is listed, but the activation pin changed for {files}. Review the changed file against the activation receipt and reverify through the Nisi activation owner. The Monitor did not replace the trusted pin. No model inference was run.")
        if binding == "UNKNOWN":
            raise _Stop("needs-action", "Private Nisi Inference bridge inventory is listed, but its activation host binding could not be verified. Inspect the activation receipt and host files, then reverify with the Nisi activation owner. No model inference was run.")

    def _windows_status(self, *, timeout: float = 15) -> tuple[dict | None, bool]:
        code, value = self._call(WINDOWS_STATUS, timeout=timeout)
        pending = value.get("pending")
        inventory = value.get("inventory")
        inventory_ok = inventory.get("ok") if isinstance(inventory, dict) else None
        inventory_reason = inventory.get("reason") if isinstance(inventory, dict) else None
        self._last_windows_reason = inventory_reason if inventory_reason in WINDOWS_INVENTORY_REASONS else None
        owner_code = value.get("code")
        evidence = (f"exit={'0' if code == 0 else '3' if code == 3 else 'other'}; "
                    f"kind={'valid' if value.get('kind') == 'codemode.windows.status.v1' else 'invalid'}; "
                    f"pending={'valid' if isinstance(pending, (dict, type(None))) else 'invalid'}; "
                    f"ready={'true' if value.get('readyForWork') is True else 'false' if value.get('readyForWork') is False else 'invalid'}; "
                    f"inventory={'true' if inventory_ok is True else 'false' if inventory_ok is False else 'unknown'}; "
                    f"reason={inventory_reason if isinstance(inventory_reason, str) and inventory_reason in WINDOWS_INVENTORY_REASONS else 'none-or-other'}; "
                    f"code={owner_code if isinstance(owner_code, str) and owner_code in WINDOWS_STATUS_CODES else 'none-or-other'}")
        if (code not in (0, 3) or value.get("kind") != "codemode.windows.status.v1"
                or not isinstance(pending, (dict, type(None)))
                or type(value.get("readyForWork")) is not bool):
            self._step("windows-status", "unavailable", evidence=evidence)
            raise _Stop("error", "Windows owner status is unavailable; no job was reconciled.")
        if pending is not None:
            job_id = pending.get("id")
            if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
                self._step("windows-status", "invalid")
                raise _Stop("error", "Windows pending identity is invalid; no job was reconciled.")
            self._step("windows-status", "pending", jobId=job_id)
        elif value["readyForWork"]:
            self._step("windows-status", "clear")
        else:
            self._step("windows-status", "unavailable", evidence=evidence)
        return pending, value["readyForWork"]

    def _windows_heartbeat_grace(self) -> bool:
        """Two read-only status checks; no inventory repeat or job replay."""
        deadline = time.monotonic() + 8.0
        for delay in (2.0, 2.0):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._pause(min(delay, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                pending, ready = self._windows_status(timeout=min(2.0, remaining))
            except subprocess.TimeoutExpired:
                self._step("windows-status", "unavailable")
                continue
            if pending is not None:
                raise _Stop("needs-action", "A Windows job appeared during the heartbeat recheck; no job was reconciled.")
            if ready:
                return True
        return False

    def _reconcile_preflight(self, run_id: str, digest: str) -> None:
        data = json.dumps({"runId": run_id, "inputSha256": digest,
                           "confirmPreflightOnly": True}, separators=(",", ":")).encode("utf-8")
        code, value = self._call(ROUTE_PREFLIGHT, data=data, timeout=20)
        reconciliation = value.get("reconciliation")
        if (code != 3 or value.get("kind") != "codemode.router.v1"
                or value.get("status") != "NOT_RUN"
                or value.get("code") != "NISI_RECOVERY_REQUIRED"
                or value.get("runId") != run_id
                or value.get("requestSha256") != digest
                or value.get("recoveryRequired") is not False
                or not isinstance(reconciliation, dict)
                or reconciliation.get("kind") != "codemode.router.preflight-reconcile.v1"):
            self._step("preflight-reconcile", "not-settled", runId=run_id)
            raise _Stop("needs-action", "The exact preflight refusal was not archived; router recovery remains unresolved.")
        self._step("preflight-reconcile", "archived", runId=run_id)

    def _reconcile_windows(self, job_id: str) -> bool:
        data = json.dumps({"expectedJobId": job_id}, separators=(",", ":")).encode("utf-8")
        code, value = self._call(WINDOWS_RECONCILE, data=data, timeout=20)
        if code == 3 and value.get("code") == "EXPECTED_JOB_ID_MISMATCH":
            self._step("windows-reconcile", "job-changed", jobId=job_id)
            raise _Stop("needs-action", "The pending Windows job changed before reconciliation; no job was polled by this check.")
        if code == 3 and value.get("status") == "pending" and value.get("id") == job_id:
            self._step("windows-reconcile", "still-pending", jobId=job_id)
            raise _Stop("needs-action", "The exact Windows job is still pending; no request was resent.")
        if (code != 0 or value.get("schema_version") != 1
                or value.get("id") != job_id or value.get("status") not in ("success", "error")):
            self._step("windows-reconcile", "not-settled", jobId=job_id)
            raise _Stop("needs-action", "The Windows reconcile result did not match the retained job; inspect its owner state.")
        terminal_error = value["status"] == "error"
        self._step("windows-reconcile", "terminal-error" if terminal_error else "terminal-success", jobId=job_id)
        return terminal_error

    def _recover_share(self) -> None:
        """Make SharedChami usable before any guarded Windows owner call."""
        share = self._share
        if share is None:
            return
        if isinstance(share, ShareControl) and not share.configuration_ready:
            self._step("share", "unhealthy", evidence="configuration-required")
            raise _Stop("needs-action", "SharedChami recovery needs valid AGIW_SHARE_HOSTS and "
                                        "AGIW_SHARE_USERNAME settings. The share was not probed or changed.")
        mounted = share.mounted()
        stuck = share.stuck_readers()
        dispatch = share.dispatch_status() if mounted else "unmounted"
        # A wedge fails on every read; one busy lock or slow sample does not.
        for _ in range(2):
            if not mounted or dispatch not in SHARE_IO_FAULTS:
                break
            share.settle(1.5)
            mounted = share.mounted()
            dispatch = share.dispatch_status() if mounted else "unmounted"
        stuck_text = str(stuck) if stuck is not None else "unknown"
        if mounted and dispatch not in SHARE_IO_FAULTS:
            # A read just succeeded, so readers already stuck are not holding
            # the queue; later owner calls tolerate them but not new ones.
            set_windows_worker_tolerance(stuck)
            self._step("share", "healthy", evidence=f"stuck-readers={stuck_text}")
            return
        self._step("share", "unhealthy",
                   evidence="unmounted" if mounted is False else "unknown" if mounted is None
                   else f"io-failed; stuck-readers={stuck_text}")
        if mounted is None:
            raise _Stop("needs-action", "The SharedChami mount table could not be read, or a different SMB account "
                                        "or host owns this mount. The share was not changed. Check the settings "
                                        "and eject an existing Guest mount in Finder before pressing Fix again.")
        host = share.reachable_host()
        if host is None:
            self._step("pc-reachability", "unreachable")
            raise _Stop("needs-action", "The Windows PC does not answer on the LAN (SMB port 445). Wake or power it on, "
                                        "then press Fix again. The share was not changed.")
        self._step("pc-reachability", "reachable", evidence=f"host={host}")
        self._nisi_clear()
        # Hold the router lock, then the Windows owner lock (the router's own
        # order) across the gates and the whole remount, so no router run or
        # probe can start between the check and the unmount.
        with share.hold_router() as router_free:
            if not router_free:
                # Never take the Windows owner lock while a router run holds its
                # own: the router would hit OWNER_BUSY mid-run and quarantine.
                self._step("share-recovery", "deferred", evidence="router=busy")
                raise _Stop("needs-action", "SharedChami needs recovery, but a route task is running. Nothing was "
                                            "unmounted; press Fix again when it finishes.")
            self._recover_share_locked(share, mounted, host)

    def _recover_share_locked(self, share: ShareControl, mounted: bool, host: str) -> None:
        with share.hold_owner() as held:
            open_jobs = share.open_jobs()
            if not held or open_jobs is None or open_jobs > 0:
                self._step("share-recovery", "deferred",
                           evidence=f"router=free; owner={'free' if held else 'busy'}; "
                                    f"openJobs={open_jobs if open_jobs is not None else 'unknown'}")
                raise _Stop("needs-action", "SharedChami needs recovery, but a route task or Windows job is still running. "
                                            "Nothing was unmounted; press Fix again when it finishes. If the share stays stuck, eject "
                                            "SharedChami in Finder and reconnect it (Go > Connect to Server).")
            if mounted:
                if not share.force_unmount():
                    self._step("share-unmount", "failed")
                    raise _Stop("needs-action", "The stuck SharedChami mount could not be force-unmounted. "
                                                "Eject it in Finder, then press Fix again.")
                self._step("share-unmount", "forced")
                for _ in range(8):
                    if share.mounted() is False:
                        break
                    share.settle(1.0)
                else:
                    self._step("share-unmount", "unverified")
                    raise _Stop("needs-action", "The SharedChami unmount could not be confirmed. No new mount "
                                                "was attempted; eject the existing volume in Finder and press Fix again.")
            if not share.mount(host):
                self._step("share-mount", "failed", evidence=f"host={host}")
                raise _Stop("needs-action", "SharedChami could not be remounted as the configured registered user with an empty password. Check the PC share account, then connect in Finder (Go > Connect to Server). Nothing was reauthenticated by this check.")
            self._step("share-mount", "mounted", evidence=f"host={host}")
            for _ in range(3):
                if share.mounted() and share.dispatch_status() not in SHARE_IO_FAULTS:
                    stuck = share.stuck_readers()
                    set_windows_worker_tolerance(stuck)
                    self._step("share-verify", "healthy",
                               evidence=f"stuck-readers={stuck if stuck is not None else 'unknown'}")
                    return
                share.settle(2.0)
        self._step("share-verify", "failed")
        raise _Stop("needs-action", "SharedChami was remounted, but the Windows queue still cannot be read. "
                                    "Check the PC worker, then press Fix again.")

    def _prove_windows_inference(self) -> str | None:
        """Run the explicit end-to-end probe; returns a summary or raises _Stop."""
        if self._inference_probe is None:
            return None
        if self._share is not None:
            open_jobs = self._share.open_jobs()
            if open_jobs is None or open_jobs > 0:
                self._step("windows-inference", "deferred", evidence=f"openJobs={open_jobs if open_jobs is not None else 'unknown'}")
                raise _Stop("needs-action", "Route checks passed, but a Windows job is already open, so no probe was sent. "
                                            "Its result will appear on the map when it settles.")
        router_hold = self._share.hold_router() if self._share is not None else nullcontext(True)
        with router_hold as router_free:
            if not router_free:
                self._step("windows-inference", "deferred", evidence="router=busy")
                raise _Stop("needs-action", "Route checks passed, but a route task started, so no probe was sent. "
                                            "Press Fix again when it finishes.")
            evidence = self._inference_probe()
        status = evidence.get("status") if isinstance(evidence, dict) else None
        job_id = evidence.get("jobId") if isinstance(evidence, dict) else None
        job_id = job_id if isinstance(job_id, str) and JOB_ID.fullmatch(job_id) else None
        model = evidence.get("model") if isinstance(evidence, dict) else None
        model = model if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+()/-]{0,95}", model) else None
        elapsed = evidence.get("elapsedSeconds") if isinstance(evidence, dict) else None
        # _finite bounds an int before any float use: math.isfinite(10**400) raises OverflowError.
        elapsed = float(elapsed) if _finite(elapsed) and 0 <= elapsed <= 3600 else None
        code = evidence.get("code") if isinstance(evidence, dict) else None
        code = code if isinstance(code, str) and code in PROBE_CODES else None
        ids = {"jobId": job_id} if job_id else {}
        if status == "success" and model and elapsed is not None:
            answered = evidence.get("answered") is True
            self._step("windows-inference", "verified" if answered else "returned",
                       evidence=f"model={model}; elapsed={elapsed:.1f}s; answer={'expected' if answered else 'unexpected'}", **ids)
            if not answered:
                raise _Stop("needs-action", f"Route checks passed and {model} returned in {elapsed:.1f} s, but its reply "
                                            "to the probe was not the expected READY. Check the model on the PC.")
            return f"Windows inference verified end to end: {model} answered in {elapsed:.1f} s."
        if status == "error":
            self._step("windows-inference", "worker-error", evidence=f"model={model or 'unknown'}", **ids)
            raise _Stop("needs-action", "Route checks passed, but the Windows worker returned an error for the probe. "
                                        "Check the model runtime on the PC.")
        if status == "timeout" or code in ("TIMEOUT", "PUBLICATION_UNCERTAIN") or job_id:
            self._step("windows-inference", "unresolved", evidence=f"code={code or 'outer-timeout'}", **ids)
            raise _Stop("needs-action", "The Windows probe did not return a result. It was not resent; if the owner "
                                        "recorded it as pending, the next Fix reconciles it.")
        self._step("windows-inference", "not-run", evidence=f"code={code or 'invalid'}")
        if code in ROUTER_INSTALL_TRANSITIONS:
            raise _Stop("needs-action", "Route checks passed, but the Windows inference probe was not sent: "
                                        "router install in progress. No job was published.")
        if code is not None and code.startswith("ROUTER_INSTALL_"):
            raise _Stop("needs-action", "Route checks passed, but the Windows inference probe was not sent: the router "
                                        f"transport refused to load ({code}); its owner must finish or roll back the "
                                        "router install. No job was published.")
        raise _Stop("needs-action", "Route checks passed, but the Windows inference probe was not sent "
                                    f"({code or 'invalid response'}). No job was published.")

    def _check_and_repair(self, started_at: float, *, fix: bool = False) -> tuple[str, str]:
        # A production Fix pauses passive heartbeat reads so they cannot
        # contend with its share checks, owner calls or end-to-end probe.
        hold = (hold_windows_worker_probe() if self._guard_windows_reader or (fix and self._share is not None)
                else nullcontext(True))
        with hold:
            return self._check_and_repair_held(started_at, fix=fix)

    def _check_and_repair_held(self, started_at: float, *, fix: bool) -> tuple[str, str]:
        active = self._route_status()
        terminal_windows_error = False
        if active is not None:
            identity = _preflight_identity(active)
            if identity is None:
                self._step("router-status", "unresolved")
                raise _Stop("needs-action", "An unresolved router run needs owner review; this control cannot clear it.")
            run_id, digest = identity
            self._step("router-status", "preflight-only", runId=run_id)
            self._nisi_clear()
            if self._windows_status()[0] is not None:
                raise _Stop("needs-action", "A Windows job remains pending; the router preflight refusal was preserved.")
            self._reconcile_preflight(run_id, digest)
            if self._route_status() is not None:
                self._step("router-status", "unresolved")
                raise _Stop("needs-action", "Router state remains unresolved after preflight reconciliation.")
            self._step("router-status", "idle")
        else:
            self._step("router-status", "idle")

        if fix:
            self._recover_share()
        pending, _windows_ready = self._windows_status()
        if pending is not None:
            terminal_windows_error = self._reconcile_windows(pending["id"])
            if self._windows_status()[0] is not None:
                raise _Stop("needs-action", "The Windows job remains pending after one exact reconciliation.")

        self._nisi_clear()
        readiness_code, _ = self._call(READINESS, timeout=75)
        if readiness_code not in (0, 3):
            self._step("readiness", "unavailable")
            raise _Stop("error", "Capability inventory failed; no task or model request was run.")
        receipt_verified = readiness_code == 0 and _new_readiness_receipt(self._readiness_path, started_at)
        self._step("readiness", "receipt-verified" if receipt_verified else "degraded")

        # The final observation is separate from the inventory receipt. Neither
        # a zero exit nor a cleared job establishes model inference.
        self._nisi_clear()
        final_pending, final_windows_ready = self._windows_status()
        if final_pending is not None:
            raise _Stop("needs-action", "A Windows job remains pending after the capability inventory.")
        if self._route_status() is not None:
            self._step("router-status", "unresolved")
            raise _Stop("needs-action", "A router run remains unresolved after the capability inventory.")
        self._step("router-status", "idle")
        if readiness_code != 0:
            return "needs-action", "Capability inventory is degraded; no task or model request was run."
        if not receipt_verified:
            return "needs-action", "Capability inventory exited, but this check produced no verified readiness receipt."
        if terminal_windows_error:
            return "needs-action", "The retained Windows job ended in an error. Capability inventory completed; no request was resent."
        if not final_windows_ready:
            if not self._windows_heartbeat_grace():
                return "needs-action", ("Mac capability inventory completed, but the Windows route is unavailable"
                                        + WINDOWS_UNAVAILABLE_HINTS.get(self._last_windows_reason, ".")
                                        + " No model inference was run.")
            self._nisi_clear()
            if self._route_status() is not None:
                self._step("router-status", "unresolved")
                raise _Stop("needs-action", "A router run appeared during the Windows heartbeat recheck.")
            self._step("router-status", "idle")
        proof = self._prove_windows_inference() if fix else None
        try:
            self._nisi_v02_bridge()
        except _Stop as stop:
            if proof:
                raise _Stop(stop.status, proof + " " + stop.message.replace(" No model inference was run.", ""))
            raise
        if proof:
            return "ready", proof + " Router idle, capability inventory complete and Nisi Inference inventory listed."
        return "ready", "Capability inventory completed; router idle and no pending host job observed. No model inference was run."

    def _check_entry(self, started_at: float) -> tuple[str, str]:
        code, value = self._call(ENTRY_READINESS, timeout=100)
        if (set(value) != {"schemaVersion", "operation", "status", "evidence"}
                or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
                or value["operation"] != "readiness"
                or not isinstance(value["status"], str)
                or not isinstance(value["evidence"], str)):
            self._step("universal-entry", "invalid")
            raise _Stop("error", "The computer-wide entry returned an invalid readiness result.")
        status, evidence = value["status"], value["evidence"]
        expected = {"ready": (0, "fresh-launcher-receipt"),
                    "degraded": (3, "launcher-preflight-exit")}
        unavailable = {"launcher-timeout", "launcher-error", "readiness-unverified"}
        if status in expected:
            valid = (code, evidence) == expected[status]
        else:
            valid = status == "unavailable" and code == 3 and evidence in unavailable
        if not valid:
            self._step("universal-entry", "invalid")
            raise _Stop("error", "The computer-wide entry returned an inconsistent readiness result.")
        if status == "ready":
            if not _new_readiness_receipt(self._readiness_path, started_at):
                self._step("universal-entry", "receipt-unverified")
                return "needs-action", "The entry reported ready, but this click produced no verified private receipt."
            self._step("universal-entry", "receipt-verified")
            return "ready", "Computer-wide Online Code Mode readiness verified. No task or model inference was run."
        self._step("universal-entry", status)
        if status == "degraded":
            return "needs-action", "Computer-wide capability inventory is degraded. No task or model inference was run."
        return "error", "Computer-wide readiness is unavailable. No task or model inference was run."

    def _fix_local_runtime(self) -> tuple[str, str]:
        """Repair only a stopped local API; preserve loaded models and work."""
        api_live, activity_live = _runtime_sources()
        self._step("local-api-before", "live" if api_live else "unavailable")
        self._step("local-activity-before", "live" if activity_live else "unavailable")
        if api_live:
            if activity_live:
                return "ready", "LM Studio inventory and activity feeds are responsive. No model inference was run."
            raise _Stop("needs-action", "LM Studio inventory responds, but its activity feed is unavailable. No server was restarted or model run.")

        if not _local_server_stopped():
            self._step("local-owner-status", "running")
            raise _Stop("needs-action", "LM Studio reports a running server, but this monitor cannot reach its fixed loopback API. No second server was started.")
        self._step("local-owner-status", "stopped")
        if not _loopback_port_free():
            self._step("local-port", "occupied-or-unavailable")
            raise _Stop("needs-action", "The local API is unavailable and loopback port 1234 is occupied or cannot be checked. No server was started.")
        self._step("local-port", "free")
        # An external owner could start between observations. Recheck the API,
        # LM Studio owner status, and the exact port just before the start.
        api_live, activity_live = _runtime_sources()
        if api_live:
            self._step("local-api-before-start", "live")
            if activity_live:
                return "ready", "LM Studio inventory and activity feeds resumed before repair. No model inference was run."
            raise _Stop("needs-action", "LM Studio inventory resumed, but its activity feed is unavailable. No server was started.")
        self._step("local-api-before-start", "unavailable")
        if not _local_server_stopped() or not _loopback_port_free():
            self._step("local-owner-recheck", "changed")
            raise _Stop("needs-action", "LM Studio owner status or port changed before the start. No server was started.")
        self._step("local-owner-recheck", "stopped-and-free")
        if not _start_local_api():
            self._step("local-server-start", "failed")
            raise _Stop("needs-action", "LM Studio did not confirm a local server start. No model inference was run.")
        self._step("local-server-start", "accepted")

        # The CLI can return before the API is listening. Validate both feeds
        # independently after the command, with a finite grace period.
        deadline = self._monotonic() + 8.0
        while True:
            api_live, activity_live = _runtime_sources()
            if api_live and activity_live:
                self._step("local-api-after", "live")
                self._step("local-activity-after", "live")
                return "ready", "LM Studio loopback API started; fresh inventory and activity feeds responded. No model inference was run."
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._pause(min(0.5, remaining))
        self._step("local-api-after", "live" if api_live else "unavailable")
        self._step("local-activity-after", "live" if activity_live else "unavailable")
        raise _Stop("needs-action", "LM Studio start returned, but both fresh monitor feeds were not verified. No model inference was run.")

    def _fix_both(self, started_at: float) -> tuple[str, str]:
        try:
            local_status, local_message = self._fix_local_runtime()
        except _Cancelled:
            raise
        except _Stop as stop:
            local_status, local_message = stop.status, stop.message
        except Exception:
            local_status, local_message = "error", "The local runtime check failed unexpectedly; inspect LM Studio owner state."
        try:
            route_status, route_message = self._check_and_repair(started_at, fix=True)
        except _Cancelled:
            raise
        except _Stop as stop:
            route_status, route_message = stop.status, stop.message
        except Exception:
            route_status, route_message = "error", "The route check failed unexpectedly; inspect the recorded steps and host owner state."
        status = ("error" if "error" in (local_status, route_status) else
                  "needs-action" if "needs-action" in (local_status, route_status) else "ready")
        return status, f"Local runtime: {local_message} Route: {route_message}"

    def _fix_all(self, started_at: float) -> tuple[str, str]:
        """Run each existing guarded repair once and retain every component outcome."""
        components = (
            ("Local runtime", self._fix_local_runtime,
             "The local runtime check failed unexpectedly; inspect LM Studio owner state."),
            ("Nisi Inference", self._fix_nisi,
             "The Nisi check failed unexpectedly; inspect the recorded steps and Nisi owner state."),
            ("Route", lambda: self._check_and_repair(started_at, fix=True),
             "The route check failed unexpectedly; inspect the recorded steps and host owner state."),
        )
        outcomes = []
        for name, run, unexpected in components:
            _raise_if_cancelled(self._owner_children)
            try:
                component_status, component_message = run()
            except _Cancelled:
                raise
            except _Stop as stop:
                component_status, component_message = stop.status, stop.message
            except Exception:
                component_status, component_message = "error", unexpected
            outcomes.append((name, component_status, component_message))
            self._step("fix-component", component_status, component=name)
        status = ("error" if any(value == "error" for _, value, _ in outcomes) else
                  "needs-action" if any(value != "ready" for _, value, _ in outcomes) else "ready")
        message = " ".join(f"{name}: {detail}" for name, _, detail in outcomes)
        return status, message

    # Fix Nisi Inference -------------------------------------------------------

    def _fix_nisi(self) -> tuple[str, str]:
        """Recover a stale Nisi marker only through the launcher, after measuring every precondition.

        Steps 1-6 stop at the first failed precondition and change nothing;
        steps 7-9 are status reads whose gaps are all reported together.
        """
        self._fix_nisi_route_idle()                                   # 1 route-status
        recovery_required, _adapter = self._fix_nisi_owner_status()    # 2 nisi-status
        marker_present = recovery_required or os.path.lexists(self._nisi_pending_path)
        if not marker_present:
            self._step("nisi-status", "clear")
            done = "No Nisi marker needed recovery."
        else:
            self._step("nisi-status", "recovery-required")
            marker = self._fix_nisi_marker()                           # 3 marker
            # Hold the router owner lock across 4-6: a route run that starts
            # now stops at ROUTER_OWNER_BUSY before writing state, instead of
            # meeting a Nisi lock taken by this check or by the launcher's recover.
            with _hold_router_owner(self._router_lock_path) as router:
                if router == "busy":
                    self._step("owner-lock", "router-busy")
                    raise _Stop("needs-action", "A route task started (the router owner lock is held or could not "
                                                "be checked); Nisi recovery was not attempted. Press Fix Nisi Inference "
                                                "again when it finishes.")
                if router == "absent":
                    # Step 1's status read needs this lock file; its loss since then is not idle.
                    self._step("owner-lock", "router-absent")
                    raise _Stop("needs-action", "The router owner lock is missing, so it could not be held across "
                                                "the recovery; Nisi recovery was not attempted. Check the router "
                                                "state with --route status.")
                self._fix_nisi_owner_lock(router=router)               # 4 owner-lock
                self._fix_nisi_server_idle()                           # 5 server-idle
                self._fix_nisi_recover(marker)                         # 6 recover
            done = (f"Recovered the Nisi marker through the launcher (age {_format_age(marker['age'])}, "
                    f"owner {marker['owner']}, input {marker['digest'][:12]}).")
        gaps = [gap for gap in (self._fix_nisi_pair(),                 # 7 pair
                                self._fix_nisi_jev(),                  # 8 jev
                                self._fix_nisi_verify())               # 9 verify
                if gap]
        if gaps:
            return "needs-action", f"{done} Nisi Inference not ready: {'; '.join(gaps)}. No model inference was run."
        return "ready", f"Nisi Inference ready. {done} No model inference was run."

    def _fix_nisi_route_idle(self) -> None:
        try:
            code, value = self._call(ROUTE_STATUS)
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            # Unreadable output, a timeout or a runner failure is recorded like an invalid reply.
            code, value = None, {}
        if code == 3 and value.get("code") == "ROUTER_OWNER_BUSY":
            self._step("route-status", "busy")
            raise _Stop("needs-action", "A route task holds the router owner; Nisi recovery was not attempted. "
                                        "Press Fix Nisi Inference again when it finishes.")
        refusal = _route_status_refusal(code, value)
        if refusal is not None:
            self._step("route-status", refusal[0])
            raise _Stop("needs-action", refusal[1] + " Nisi recovery was not attempted.")
        active = value.get("active")
        if (code != 0 or set(value) != {"schemaVersion", "active"}
                or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
                or not isinstance(active, (dict, type(None)))):
            self._step("route-status", "unavailable")
            raise _Stop("error", "Router status is unavailable or invalid; Nisi recovery was not attempted.")
        if active is not None:
            run_id = active.get("runId")
            ids = {"runId": run_id} if isinstance(run_id, str) and RUN_ID.fullmatch(run_id) else {}
            self._step("route-status", "active", **ids)
            # The router's reconcile gates refuse while pending.json exists and need
            # the recovered marker, so "resolve the run first" alone would be a loop.
            raise _Stop("needs-action", ROUTE_ACTIVE_MESSAGE)
        self._step("route-status", "idle")

    def _fix_nisi_owner_status(self, step: str = "nisi-status") -> tuple[bool, bool]:
        """Closed read of `--nisi status`: (recoveryRequired, adapter present)."""
        try:
            code, value = self._call(NISI_STATUS)
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            code, value = None, {}
        nisi = value.get("nisi")
        if (code != 0 or set(value) != {"kind", "nisi"}
                or value["kind"] != "codemode.integrations.v1"
                or not isinstance(nisi, dict) or set(nisi) != NISI_STATUS_KEYS
                or nisi["status"] not in ("ADAPTER_PRESENT", "NOT_RUN")
                or type(nisi["recoveryRequired"]) is not bool
                or not isinstance(nisi["root"], str) or not isinstance(nisi["ownership_scope"], str)
                or nisi["model_inference"] != "NOT_RUN" or nisi["workflow_acceptance"] != "NOT_RUN"):
            self._step(step, "unavailable")
            raise _Stop("error", "Nisi owner status is unavailable or invalid; Nisi recovery was not attempted.")
        return nisi["recoveryRequired"], nisi["status"] == "ADAPTER_PRESENT"

    def _read_nisi_marker(self) -> dict:
        """Read pending.json with the launcher's private-file checks; raises _Unsafe or FileNotFoundError."""
        dir_fd = _private_dir_fd(self._nisi_pending_path.parent)
        try:
            raw, info = _read_private(dir_fd, self._nisi_pending_path.name)
        finally:
            os.close(dir_fd)
        value = _parse_nisi_marker(raw)
        run_id = value.get("runId")
        return {"raw": raw, "identity": (info.st_dev, info.st_ino), "kind": value["kind"],
                "started": float(value["started_unix"]), "digest": value["input_sha256"],
                "runId": run_id, "operation": value.get("operation"),
                "owner": run_id if run_id is not None else "anonymous (legacy)"}

    def _fix_nisi_marker(self) -> dict:
        try:
            marker = self._read_nisi_marker()
        except FileNotFoundError:
            self._step("marker", "missing")
            raise _Stop("needs-action", "The Nisi pending marker disappeared during the check (another owner may "
                                        "have recovered it). Press Fix Nisi Inference again.")
        except _Unsafe as unsafe:
            self._step("marker", "unsafe", evidence=unsafe.reason)
            raise _Stop("needs-action", f"The Nisi pending marker failed its private-file checks ({unsafe.reason}); "
                                        "it was not recovered. Inspect it with its owner.")
        now = self._clock()
        age = now - marker["started"]
        marker["age"] = age
        evidence = (f"kind={marker['kind']}; age={_format_age(age)}; input={marker['digest'][:12]}; "
                    f"owner={marker['owner']}")
        ids = {"ageSeconds": str(int(age)) if math.isfinite(age) else "unknown",
               "inputSha256": marker["digest"][:12], "owner": marker["owner"]}
        if marker["runId"] is not None:
            ids.update(runId=marker["runId"], operation=marker["operation"])
        if not math.isfinite(age) or age < -5:
            self._step("marker", "future-dated", evidence=evidence, **ids)
            raise _Stop("needs-action", "The Nisi pending marker is dated in the future; it was not recovered. "
                                        "Check the Mac clock and inspect the marker with its owner.")
        if age < NISI_MARKER_MIN_AGE:
            self._step("marker", "too-young", evidence=evidence, **ids)
            raise _Stop("needs-action", f"The Nisi pending marker is only {_format_age(age)} old; recovery waits until "
                                        f"it is at least {NISI_MARKER_MIN_AGE // 60} min old so a slow call can settle. "
                                        "Nothing was changed.")
        self._step("marker", "stale", evidence=evidence, **ids)
        return marker

    def _fix_nisi_owner_lock(self, *, router: str) -> None:
        """Probe the launcher's Nisi owner lock without blocking, then release it at once."""
        try:
            dir_fd = _private_dir_fd(self._nisi_pending_path.parent)
            try:
                fd = os.open("owner.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
            finally:
                os.close(dir_fd)
        except FileNotFoundError:
            self._step("owner-lock", "missing")
            raise _Stop("needs-action", "The Nisi owner lock is missing; Nisi recovery was not attempted.")
        except (OSError, _Unsafe):
            self._step("owner-lock", "unsafe")
            raise _Stop("needs-action", "The Nisi owner lock is linked or unsafe; Nisi recovery was not attempted.")
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                self._step("owner-lock", "unsafe")
                raise _Stop("needs-action", "The Nisi owner lock is not private; Nisi recovery was not attempted.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._step("owner-lock", "busy")
                raise _Stop("needs-action", "A Nisi call is still running; not recovering.")
            except OSError:
                self._step("owner-lock", "unavailable")
                raise _Stop("needs-action", "The Nisi owner lock could not be checked; Nisi recovery was not attempted.")
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        self._step("owner-lock", "free", evidence=f"nisi=free; router={router}")

    def _server_sample(self) -> tuple[str, str]:
        _raise_if_cancelled(self._owner_children)
        try:
            listing = self._lms_ps() if self._lms_ps is not None else None
        except (ControlError, OSError, ValueError, TimeoutError, TypeError, UnicodeError,
                subprocess.SubprocessError):
            listing = None
        lms, lms_evidence = _lms_idle_verdict(listing) if listing is not None else ("unknown", "lms=unavailable")
        _raise_if_cancelled(self._owner_children)
        if lms != "idle":
            return lms, lms_evidence
        try:
            if self._loopback_sockets is None:
                raise ValueError("socket listing unavailable")
            sockets = _lsof_sockets(self._loopback_sockets())
        except (OSError, ValueError, TimeoutError, TypeError, UnicodeError, subprocess.SubprocessError):
            # A probe killed by monitor shutdown is a cancellation, not an unknown server.
            _raise_if_cancelled(self._owner_children)
            return "unknown", f"{lms_evidence}; sockets=unavailable"
        _raise_if_cancelled(self._owner_children)
        verdict, evidence = _server_client_verdict(sockets, os.getpid())
        return verdict, f"{lms_evidence}; {evidence}"

    def _fix_nisi_server_idle(self) -> None:
        """Two samples 2 s apart: every LLM idle and no foreign client on the loopback API."""
        evidence = []
        for index in range(2):
            if index:
                self._pause(SERVER_IDLE_SAMPLE_GAP)
            verdict, sample = self._server_sample()
            evidence.append(f"sample{index + 1}: {sample}")
            if verdict != "idle":
                self._step("server-idle", verdict, evidence="; ".join(evidence))
                raise _Stop("needs-action", "The local model server is busy or its state is unknown; Nisi recovery "
                                            "was not attempted. Try again when it is idle.")
        self._step("server-idle", "idle", evidence="; ".join(evidence))

    def _recovered_names(self) -> set[str]:
        dir_fd = _private_dir_fd(self._nisi_pending_path.parent)
        try:
            return {name for name in os.listdir(dir_fd) if name.startswith("recovered-")}
        finally:
            os.close(dir_fd)

    def _fix_nisi_recover(self, marker: dict) -> None:
        """Run the launcher's recover once, then confirm it moved exactly the checked marker."""
        try:
            current = self._read_nisi_marker()
        except (FileNotFoundError, _Unsafe):
            current = None
        if current is None or current["raw"] != marker["raw"] or current["identity"] != marker["identity"]:
            self._step("recover", "marker-changed")
            raise _Stop("needs-action", "The Nisi pending marker changed before recovery; nothing was recovered. "
                                        "Press Fix Nisi Inference again.")
        try:
            before = self._recovered_names()
        except (OSError, _Unsafe):
            self._step("recover", "not-run", evidence="state listing unavailable")
            raise _Stop("needs-action", "The Nisi state directory could not be listed; nothing was recovered.")
        try:
            code, value = self._call(NISI_RECOVER, timeout=NISI_RECOVER_TIMEOUT)
        except _Cancelled:
            # The launcher child was killed mid-call; its rename may or may not have happened.
            self._step("recover", "interrupted")
            raise
        except subprocess.TimeoutExpired:
            self._step("recover", "no-answer")
            raise _Stop("needs-action", f"The launcher's recover did not answer within {NISI_RECOVER_TIMEOUT} s; its "
                                        "outcome is unknown and it was not retried. Check Nisi status before retrying.")
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            self._step("recover", "invalid")
            raise _Stop("needs-action", "The launcher's recover returned no readable result; it was not retried. "
                                        "Check Nisi status before retrying.")
        if code == 3 and value.get("code") == "NISI_OWNER_BUSY":
            self._step("recover", "owner-busy")
            raise _Stop("needs-action", "A Nisi call took the owner lock; the launcher did not recover. "
                                        "Try again when it finishes.")
        if code != 0 or value != NISI_RECOVERED:
            self._step("recover", "invalid")
            raise _Stop("needs-action", "The launcher's recover returned an unexpected result; it was not retried. "
                                        "Check Nisi status before retrying.")
        if os.path.lexists(self._nisi_pending_path):
            self._step("recover", "marker-still-present")
            raise _Stop("needs-action", "The launcher acknowledged recovery, but the Nisi pending marker is still "
                                        "present. Inspect the Nisi state with its owner.")
        try:
            new = self._recovered_names() - before
            name = next(iter(new)) if len(new) == 1 else None
            if name is None or not RECOVERED_NAME.fullmatch(name):
                raise _Unsafe("unexpected recovered files")
            dir_fd = _private_dir_fd(self._nisi_pending_path.parent)
            try:
                raw, info = _read_private(dir_fd, name, subject="recovered marker")
            finally:
                os.close(dir_fd)
        except (OSError, _Unsafe):
            self._step("recover", "unconfirmed")
            raise _Stop("needs-action", "The launcher acknowledged recovery, but exactly one new private recovered "
                                        "marker was not found. Inspect the Nisi state with its owner.")
        if raw != marker["raw"] or (info.st_dev, info.st_ino) != marker["identity"]:
            self._step("recover", "mismatch", evidence=f"file={name}")
            raise _Stop("needs-action", "The launcher acknowledged recovery, but the recovered marker is not the one "
                                        "this check measured. Inspect the Nisi state with its owner.")
        self._step("recover", "acknowledged", evidence=f"file={name}; remoteInferenceStopped=NOT_OBSERVED")

    def _fix_nisi_pair(self) -> str | None:
        try:
            listing = self._lms_ps() if self._lms_ps is not None else None
        except (ControlError, OSError, ValueError, TimeoutError, TypeError, UnicodeError,
                subprocess.SubprocessError):
            _raise_if_cancelled(self._owner_children)
            listing = None
        verdict, author, reviewer, count = _resident_pair(listing)
        if verdict == "resident":
            self._step("pair", "resident", author=author, reviewer=reviewer)
            return None
        if verdict == "missing":
            self._step("pair", "missing", evidence=f"resident-llms={count}")
            return "Nisi needs a second resident model"
        if verdict == "ambiguous":
            self._step("pair", "ambiguous")
            return "Nisi's resident models are ambiguous (a duplicate or renamed instance is loaded)"
        self._step("pair", "unavailable")
        return "LM Studio's loaded-model list is unavailable, so the Nisi pair could not be checked"

    def _fix_nisi_jev(self) -> str | None:
        """Jev's file-only opt-in status; never classification or any network call."""
        try:
            code, value = self._call(JEV_STATUS, timeout=15)
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            code, value = None, {}
        if (code != 0 or value.get("kind") != "chami.intake.typesafe.status.v1"
                or type(value.get("enabled")) is not bool
                or value.get("network_contacted") is not False):
            self._step("jev", "unavailable")
            return "Jev status is unavailable"
        if not value["enabled"]:
            self._step("jev", "not-opted-in")
            return "Jev is not opted in"
        self._step("jev", "opted-in")
        return None

    def _fix_nisi_verify(self) -> str | None:
        try:
            recovery_required, adapter = self._fix_nisi_owner_status("verify")
        except _Stop:
            return "Nisi status is unavailable"
        if recovery_required or os.path.lexists(self._nisi_pending_path):
            self._step("verify", "recovery-required")
            return "Nisi still reports recovery required"
        if not adapter:
            self._step("verify", "adapter-missing")
            return "the Nisi adapter is not installed"
        self._step("verify", "clear")
        return None

    def _journal_fix(self, operation_id: int, action: str, status: str, message: str) -> None:
        """Append this outcome to the private monitor fix journal (best effort, recorded as a step)."""
        if self._fix_journal_path is None:
            return
        with self._lock:
            steps = copy.deepcopy(self._state["steps"]) if self._state["operationId"] == operation_id else []
        # The worker must always reach its final state update: nothing here may raise.
        try:
            finished = round(float(self._clock()), 3)
            finished = finished if math.isfinite(finished) else None
        except Exception:
            finished = None
        try:
            written = _append_journal(self._fix_journal_path, {
                "kind": "inference-monitor.fix-journal.v1", "action": action, "operationId": operation_id,
                "finishedUnix": finished, "status": status, "message": message, "steps": steps})
        except Exception:
            written = False
        self._step("journal", "written" if written else "failed")

    def _headless(self, action: str) -> tuple[str, str]:
        """Ask pc-llm to turn the switch on (after its own probe) or off; parse its one JSON line."""
        command = self._pc_llm or PC_LLM
        try:
            info = command.lstat() if self._headless_enabled else None
        except OSError:
            info = None
        if (info is None or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022 or not os.access(command, os.X_OK)):
            self._step("pc-headless", "error")
            raise _Stop("needs-action", "pc-llm is not installed as an owner executable; the switch was not changed.")
        arguments, timeout = HEADLESS_COMMANDS[action]
        # Only turning on touches SharedChami (the probe): pause passive heartbeat
        # reads and never start beside an uninterruptible reader, like _call.
        guard = action == "on" and self._guard_windows_reader
        acquired = False
        try:
            with (hold_windows_worker_probe() if guard else nullcontext(True)):
                if guard:
                    acquired = _WINDOWS_WORKER_IO_LOCK.acquire(blocking=False)
                    if not acquired or _windows_worker_reader_blocked():
                        self._step("windows-preflight", "paused")
                        raise _Stop("needs-action", "SharedChami is busy or a reader is stuck; "
                                                    "the PC was not probed and the switch was not changed.")
                reply = _run_bounded([str(command), *arguments], None, timeout)
            lines = reply.stdout.splitlines()
            value = _decode(lines[0]) if len(lines) == 1 else None
        except subprocess.TimeoutExpired:
            self._step("pc-headless", "error")
            raise _Stop("needs-action", f"pc-llm did not answer within {timeout} s; check the headless switch before retrying.")
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            value = None
        finally:
            if acquired:
                _WINDOWS_WORKER_IO_LOCK.release()
        if value is None:
            self._step("pc-headless", "error")
            raise _Stop("needs-action", "pc-llm returned no readable result; check the headless switch before retrying.")
        if reply.returncode == 0 and value.get("state") == action == "off":
            self._step("pc-headless", "off")
            return "ready", "PC headless off."
        if reply.returncode == 0 and value.get("state") == action == "on":
            self._step("pc-headless", "on")
            expires, probe = value.get("expiresAtUnix"), value.get("probe")
            elapsed = probe.get("elapsedSeconds") if isinstance(probe, dict) else None
            until = (f" until {time.strftime('%H:%M', time.localtime(expires))}"
                     if type(expires) in (int, float) and math.isfinite(expires)
                     and time.time() < expires <= time.time() + 13 * 3600 else "")
            answered = (f" (probe answered in {elapsed:.1f} s)"
                        if type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 3600 else "")
            return "ready", f"PC headless on{until}{answered}."
        if reply.returncode != 0 and value.get("status") == "error":
            self._step("pc-headless", "probe-failed" if value.get("code") == "PC_PROBE_FAILED" else "error")
            raise _Stop("needs-action", _pc_llm_message(value.get("message")))
        self._step("pc-headless", "error")
        raise _Stop("needs-action", "pc-llm returned an unexpected result; check the headless switch before retrying.")

    def _work(self, operation_id: int, action: str) -> None:
        _OWNER_THREAD.children = self._owner_children
        try:
            _raise_if_cancelled(self._owner_children)
            started_at = time.time()
            if action == "readiness":
                with (hold_windows_worker_probe() if self._guard_windows_reader else nullcontext(True)):
                    status, message = self._check_entry(started_at)
            elif action == "fix-local":
                status, message = self._fix_local_runtime()
            elif action == "fix-all":
                status, message = self._fix_all(started_at)
            elif action == "fix-both":
                status, message = self._fix_both(started_at)
            elif action == "fix-route":
                status, message = self._check_and_repair(started_at, fix=True)
            elif action == "fix-nisi":
                status, message = self._fix_nisi()
            elif action == "check-and-repair":
                status, message = self._check_and_repair(started_at)
            elif action in ("headless-on", "headless-off"):
                status, message = self._headless(action[len("headless-"):])
            else:
                raise ValueError("unsupported maintenance action")
        except _Cancelled:
            status, message = "needs-action", (FIX_NISI_CANCELLED if action == "fix-nisi" else
                                               FIX_ALL_CANCELLED if action == "fix-all" else
                                               "Monitor stopped before this check completed. No model inference was run.")
        except _Stop as stop:
            status, message = stop.status, stop.message
        except (OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError):
            if action in ("fix-local", "fix-both"):
                status, message = "error", "A bounded inference check failed; no further action was attempted."
            elif action == "fix-all":
                status, message = "error", ("A bounded Fix inference check failed; inspect the recorded steps, "
                                            "Nisi status and Windows owner state before retrying.")
            elif action == "fix-nisi":
                status, message = "error", ("A bounded Fix Nisi Inference check failed; no further action was attempted. "
                                            "The recorded steps show whether the launcher's recover ran.")
            else:
                status, message = "error", "A bounded launcher check failed; no further action was attempted."
        except Exception:
            status, message = "error", "The repair check failed; inspect owner state before retrying."
        finally:
            del _OWNER_THREAD.children
        if action in ("fix-nisi", "fix-all"):
            self._journal_fix(operation_id, action, status, message)
        with self._lock:
            if self._state["operationId"] == operation_id and not self._owner_children.cancelled.is_set():
                self._state["status"] = status
                self._state["message"] = message
