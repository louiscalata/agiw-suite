"""Read-only Linux lane observations; PAIR's autoload endpoints are never probed."""
from __future__ import annotations

import http.client
import json
import math
import os
from pathlib import Path
import re
import selectors
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from . import lanes
from .linsys import listener_owned

PAIR_BASE = "http://127.0.0.1:8081"
PAIR_URLS = frozenset(PAIR_BASE + path for path in ("/health", "/models"))
WRAPPER_EXE = "/srv/storage/work/claude-llama-vulkan/bin/llama-server"
HTTP_TIMEOUT = 0.35
SLOTS_TIMEOUT = 0.75  # Separate queue allowance; the outer sampling deadline still wins.
HTTP_MAX_BYTES = 65536
LOG_MAX_BYTES = 131072
STATE_SCAN_LIMIT = 128
UNIT_TIMEOUT = 0.35
UNIT_MAX_BYTES = 4096
# Intended system deployment from map-linux (2026-10-17), distinct from the
# retired v7 user unit. Digest verified against the local v8 release.json and
# command shape against its pinned agiw-coordinator-v8.service.in template.
COORDINATOR_RUNTIME = "linux-scheduler-authority-v8-jev114-qwen35-boot-v1"
COORDINATOR_RELEASE_SHA256 = "85adaf746dd8775029d9505eadca04eb0282d9af56d056f2fce9004a58c43e9d"
ROLE_MODELS = {"code": "Qwen3-8B-Q4_K_M", "extract": "Qwen3-4B-Instruct-2507-Q6_K"}


def _http_get(url: str, timeout: float, max_bytes: int) -> Any:
    """An allowlist caller supplies a literal URL; no proxy, redirects or DNS."""
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/(?:health|models|v1/models|slots)", url):
        raise ValueError("probe URL not allowlisted")
    port, path = url.removeprefix("http://127.0.0.1:").split("/", 1)
    if not 1024 <= int(port) <= 65535 or int(port) == 8081 and url not in PAIR_URLS:
        raise ValueError("probe port/path not allowlisted")
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    expired = threading.Event()

    def expire() -> None:
        expired.set()
        if connection.sock is not None:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(timeout * 2, expire)
    timer.daemon = True
    timer.start()
    try:
        connection.request("GET", "/" + path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise OSError(f"HTTP status {response.status}")
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("oversized HTTP response")
        if expired.is_set():
            raise TimeoutError("HTTP total deadline")
        return lanes.loads_strict(payload.decode("utf-8"))
    finally:
        timer.cancel()
        connection.close()


def pair_get(url: str, timeout: float = HTTP_TIMEOUT, max_bytes: int = HTTP_MAX_BYTES,
             *, method: str = "GET", fetch: Callable | None = None) -> Any:
    """Reject every PAIR request except two exact URLs, before touching a transport."""
    if method != "GET" or url not in PAIR_URLS:
        raise ValueError("PAIR permits only GET /health and GET /models on 127.0.0.1:8081")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0 \
            or type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("positive finite probe bounds required")
    return (fetch or _http_get)(url, min(timeout, HTTP_TIMEOUT), min(max_bytes, HTTP_MAX_BYTES))


def _failure(error: Exception) -> str:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(error, ConnectionRefusedError):
        return "refused"
    if isinstance(error, PermissionError):
        return "permission-denied"
    if isinstance(error, (ValueError, TypeError, UnicodeError)):
        return "invalid-response"
    return "unavailable"


def require_complete(value, rows=None):
    """An explicit partial/page/cap signal overrides otherwise plausible values."""
    if not isinstance(value, dict):
        return
    if ("complete" in value and value["complete"] is not True) or any(
            key in value and value[key] is not False for key in ("truncated", "has_more")) \
            or value.get("next") is not None or value.get("next_cursor") is not None:
        raise ValueError("incomplete-response")
    if "total" in value and rows is not None and (type(value["total"]) is not int
            or not isinstance(rows, list) or value["total"] != len(rows)):
        raise ValueError("incomplete-response")


def probe_pair(fetch: Callable | None = None, *, processes: list[dict] | None = None,
               network: dict | None = None) -> dict[str, Any]:
    row = {"id": "pair-llamacpp", "health": "unknown", "status": "unknown", "reason": "unavailable",
           "models": [], "modelsComplete": False, "liveTokensPerSecond": None,
           "promptTokensPerSecond": None, "source": "PAIR local router /health and /models"}
    owners = [p for p in processes or [] if p.get("kind") == "pair-router"
              and listener_owned(p, network, 8081)]
    if len(owners) != 1:
        row["reason"] = "unverified-listener-owner"
        return row
    try:
        health = pair_get(PAIR_BASE + "/health", fetch=fetch)
        require_complete(health)
        if not isinstance(health, dict) or health.get("status") != "ok":
            row["reason"] = "invalid-health"
            return row
        payload = pair_get(PAIR_BASE + "/models", fetch=fetch)
        models = payload.get("data", payload.get("models")) if isinstance(payload, dict) else None
        require_complete(payload, models)
        if not isinstance(models, list) or len(models) > 32:
            row["reason"] = "invalid-models"
            return row
        result, seen = [], set()
        for model in models:
            name = model.get("id") if isinstance(model, dict) else None
            state = model.get("status") if isinstance(model, dict) else None
            state = state.get("value") if isinstance(state, dict) else None
            if not isinstance(name, str) or not name.strip() or not name.isprintable() or len(name) > 256 \
                    or name in seen or state not in ("loaded", "unloaded", "loading", "unloading"):
                row["reason"] = "invalid-models"
                return row
            seen.add(name)
            result.append({"id": name, "host": "linux", "lane": "pair-llamacpp",
                           "loadedState": "loaded" if state == "loaded" else "inactive" if state == "unloaded" else "unknown",
                           "lifecycle": state, "activity": "unknown", "liveTokensPerSecond": None,
                           "promptTokensPerSecond": None, "source": "PAIR local router /models"})
        row.update(health="up", status="serving", reason="observed", models=result, modelsComplete=True)
    except (OSError, ValueError, TypeError, http.client.HTTPException) as error:
        row["reason"] = _failure(error)
    return row


def _flag(process: dict, *names: str) -> str | None:
    flags = process.get("flags")
    if isinstance(flags, dict):
        for name in names:
            value = flags.get(name)
            if isinstance(value, str):
                return value
    return None


def probe_wrapper(processes: list[dict] | None, fetch: Callable | None = None,
                  *, network: dict | None = None) -> list[dict]:
    """Only the mapped wrapper executable can authorize health/model/slot reads."""
    results = []
    for process in processes or []:
        if not isinstance(process, dict) or process.get("exe") != WRAPPER_EXE:
            continue
        raw_port = _flag(process, "--port", "port")
        try:
            port = int(raw_port) if raw_port is not None else process.get("port")
        except ValueError:
            port = None
        declared_alias = _flag(process, "--alias", "-a", "alias")
        alias = declared_alias or lanes.public_id(_flag(process, "--model", "-m", "model"))
        if alias is not None and (not alias.isprintable() or not alias.strip() or len(alias) > 120):
            alias = None
        pid = process.get("pid")
        row = {"id": f"gpu-wrapper-{pid}", "pid": pid, "host": "linux", "health": "unknown",
               "status": "unknown", "reason": "unverified-endpoint", "liveTokensPerSecond": None,
               "slots": [], "slotsBusy": None, "slotsTotal": None, "servedModel": None,
               "expectedModel": alias, "phase": None}
        if type(port) is not int or not 1024 <= port <= 65535 or port == 8081 \
                or _flag(process, "--host", "host") != "127.0.0.1" or alias is None:
            results.append(row)
            continue
        if not listener_owned(process, network, port):
            row["reason"] = "unverified-listener-owner"
            results.append(row)
            continue
        spec = {"id": row["id"], "port": port, "alias": alias, "device": "Vulkan"}
        base = f"http://127.0.0.1:{port}"
        allowed = frozenset(base + path for path in ("/health", "/v1/models", "/slots"))
        failure = None

        def local_fetch(url: str, timeout: float, max_bytes: int) -> Any:
            nonlocal failure
            if url not in allowed:
                raise ValueError("wrapper endpoint not allowlisted")
            try:
                allowance = SLOTS_TIMEOUT if url.endswith("/slots") else HTTP_TIMEOUT
                value = (fetch or _http_get)(url, min(timeout, allowance), min(max_bytes, HTTP_MAX_BYTES))
                require_complete(value, value.get("data", value.get("slots")) if isinstance(value, dict) else None)
            except (OSError, ValueError, TypeError, http.client.HTTPException) as error:
                failure = _failure(error)
                raise
            if url.endswith("/health") and (not isinstance(value, dict) or value.get("status") != "ok"):
                raise ValueError("invalid health response")
            # Without an explicit alias, the server may report its absolute model
            # filename. Compare the public basename supplied by the process reader.
            if url.endswith("/v1/models") and declared_alias is None and isinstance(value, dict) \
                    and isinstance(value.get("data"), list):
                value = {**value, "data": [{**item, "id": lanes.public_id(item.get("id"))}
                                           if isinstance(item, dict) else item for item in value["data"]]}
            return value

        lane = lanes.probe_lane(spec, fetch=local_fetch, slots_timeout=SLOTS_TIMEOUT)
        good = lane["status"] in lanes.LANE_UP
        row.update(lane, health="up" if good else "unknown",
                   reason=failure or ("observed" if lane["status"] in ("idle", "busy") else lane["status"]), spec=spec)
        results.append(row)
    return results


def _open_directory(path: Path) -> int:
    """Walk the directory by held descriptors, including the final component."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute unlinked path required")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise


def _record(path: Path, max_bytes: int, *, tail: bool = False) -> tuple[bytes, float] | None:
    """Hold every directory descriptor: no symlink parent or final file is followed."""
    path = Path(path)
    fd = None
    try:
        fd = _open_directory(path.parent)
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        os.close(fd)
        fd = child
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not tail and info.st_size > max_bytes:
            return None
        if tail and info.st_size > max_bytes:
            os.lseek(fd, -max_bytes, os.SEEK_END)
        data = os.read(fd, max_bytes + 1)
        if len(data) > max_bytes:
            return None
        return data, info.st_mtime
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


_RATE = re.compile(r"(?:^|\s)(?P<prompt>prompt\s+)?eval time\s*=.*?\(.*?[, ](?P<rate>\d+(?:\.\d+)?) tokens per second\)")


def last_rates(path: Path, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    role = Path(path).stem.removeprefix("last-")
    row = {"kind": "unknown", "role": role if role in ROLE_MODELS else None,
           "model": None, "expectedModel": ROLE_MODELS.get(role), "modelLabelSource": "role-map-unverified",
           "generationTokensPerSecond": None, "promptTokensPerSecond": None,
           "observedUnix": None, "ageSeconds": None, "source": "wrapper log timings", "reason": "unavailable"}
    if role not in ROLE_MODELS or Path(path).name != f"last-{role}.log":
        row["reason"] = "unsupported-role"
        return row
    record = _record(Path(path), LOG_MAX_BYTES, tail=True)
    if record is None:
        return row
    payload, observed = record
    age = now - observed
    if not math.isfinite(age) or age < -5:
        row["reason"] = "clock-invalid"
        return row
    for line in payload.decode("utf-8", errors="replace").splitlines():
        match = _RATE.search(line)
        if match:
            rate = float(match.group("rate"))
            if math.isfinite(rate) and 0 <= rate <= 1e7:
                key = "promptTokensPerSecond" if match.group("prompt") else "generationTokensPerSecond"
                row[key] = rate
    if any(row[key] is not None for key in ("promptTokensPerSecond", "generationTokensPerSecond")):
        row.update(kind="last-measured", observedUnix=observed, ageSeconds=round(max(0.0, age), 1), reason="observed")
    else:
        row["reason"] = "no-timings"
    return row


def _unit_query(scope: str) -> bytes | None:
    """Observe only the fixed coordinator unit, with capped bytes and wall time."""
    if sys.platform != "linux" or scope not in ("user", "system"):
        return None
    process = selector = None
    deadline = time.monotonic() + UNIT_TIMEOUT
    try:
        process = subprocess.Popen(
            ["/usr/bin/systemctl", f"--{scope}", "--no-pager", "show", "agiw-coordinator.service", "--all",
             "--property=Id,LoadState,UnitFileState,ActiveState,SubState,ExecStart,MainPID"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={"LANG": "C", "LC_ALL": "C", "SYSTEMD_COLORS": "0", "SYSTEMD_PAGER": "",
                 "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}, bufsize=0)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        payload = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return None
            chunk = os.read(process.stdout.fileno(), UNIT_MAX_BYTES + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > UNIT_MAX_BYTES:
                return None
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            return None
        return bytes(payload)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=0.1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process.stdout is not None:
                process.stdout.close()


_UNIT_PROPERTIES = {
    "Id": {"agiw-coordinator.service"},
    "LoadState": {"loaded", "not-found", "error", "bad-setting", "masked"},
    "UnitFileState": {"", "enabled", "enabled-runtime", "linked", "linked-runtime", "alias", "masked",
                      "masked-runtime", "static", "disabled", "indirect", "generated", "transient", "bad"},
    "ActiveState": {"active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance"},
    "SubState": {"dead", "running", "exited", "failed", "start-pre", "start", "start-post", "stop", "stop-sigterm",
                 "stop-sigkill", "stop-post", "final-sigterm", "final-sigkill", "auto-restart", "reload", "cleaning"},
}


def _valid_unit(value: Any) -> bool:
    return isinstance(value, dict) and value.keys() == _UNIT_PROPERTIES.keys() | {"ExecStart", "MainPID"} \
        and all(isinstance(value[key], str) and value[key] in allowed for key, allowed in _UNIT_PROPERTIES.items()) \
        and isinstance(value["MainPID"], str) and re.fullmatch(r"0|[1-9][0-9]{0,9}", value["MainPID"]) is not None \
        and int(value["MainPID"]) <= 2 ** 31 - 1 \
        and isinstance(value["ExecStart"], str) and len(value["ExecStart"]) <= UNIT_MAX_BYTES


def coordinator_units() -> dict[str, dict | None]:
    result = {}
    for scope in ("user", "system"):
        result[scope] = None
        payload = _unit_query(scope)
        if not isinstance(payload, bytes) or len(payload) > UNIT_MAX_BYTES:
            continue
        try:
            pairs = [line.split("=", 1) for line in payload.decode("ascii").splitlines()]
            value = dict(pairs)
            if len(pairs) == len(value) and _valid_unit(value):
                result[scope] = value
        except (ValueError, UnicodeError):
            continue
    return result


def _unit_state(value: dict | None) -> str:
    if not _valid_unit(value):
        return "unknown"
    if value["LoadState"] == "loaded":
        if value["ActiveState"] == "active" and value["SubState"] == "running" and value["MainPID"] != "0":
            return "active"
        if value["UnitFileState"] == "disabled" and value["ActiveState"] == "inactive" \
                and value["SubState"] == "dead" and value["MainPID"] == "0":
            return "disabled"
    if value["LoadState"] == "not-found" and value["UnitFileState"] == "" \
            and value["ActiveState"] == "inactive" and value["SubState"] == "dead" \
            and value["MainPID"] == "0" and not value["ExecStart"]:
        return "absent"
    return "unknown"


def _coordinator_release(value: dict | None) -> dict[str, str | None]:
    """Extract only bounded identity fields, never publish unit command text.

    Accept the mapped python invocation with a single script and release flag.
    Extra commands/arguments and escaped or ambiguous paths remain unknown.
    This is configured-unit identity, not verification of installed file hashes.
    """
    identity = {"runtime": None, "releaseSha256": None}
    if not _valid_unit(value):
        return identity
    command = value["ExecStart"]
    match = re.fullmatch(
        r"\{ path=/usr/bin/python3 ; argv\[\]=/usr/bin/python3 -B "
        r"/home/[A-Za-z0-9_-]+/\.local/share/agiw-runtimes/"
        r"(?P<runtime>linux-scheduler-authority-v7|linux-scheduler-authority-v8-jev114-qwen35-boot-v1)"
        r"/(?P<script>coordinator(?:_v8)?\.py) serve --release-sha256=(?P<release>[0-9a-f]{64})"
        r"(?P<upgrade> --upgrade-directory=/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+"
        r" --upgrade-binding-sha256=[0-9a-f]{64})? ; "
        r"ignore_errors=(?:yes|no) ; [^{}\n]* \}", command)
    if match:
        v8 = match.group("runtime") == COORDINATOR_RUNTIME
        if match.group("script") == ("coordinator_v8.py" if v8 else "coordinator.py") \
                and bool(match.group("upgrade")) == v8:
            identity.update(runtime=match.group("runtime"), releaseSha256=match.group("release"))
    return identity


def _coordinator_process_matches(process: dict, unit: dict) -> bool:
    return unit["state"] == "active" \
        and type(process.get("pid")) is int and process["pid"] == unit["mainPid"] \
        and unit["runtime"] is not None and unit["releaseSha256"] is not None \
        and process.get("coordinatorRuntime") == unit["runtime"] \
        and process.get("coordinatorReleaseSha256") == unit["releaseSha256"]


def coordinator_status(processes: list[dict] | None, *, units: dict | None = None) -> dict[str, Any]:
    complete = isinstance(processes, list) and all(isinstance(p, dict) and p.get("identityComplete") is not False for p in processes)
    coordinators = [p for p in processes if p.get("name") == "agiw-coordinator" or p.get("coordinator") is True] if complete else []
    running = bool(coordinators) if complete else None
    observed = {}
    for scope in ("user", "system"):
        value = units.get(scope) if isinstance(units, dict) else None
        observed[scope] = {"state": _unit_state(value), **_coordinator_release(value),
                           "mainPid": int(value["MainPID"]) if _valid_unit(value) else None}
    row = {"id": "coordinator", "state": "unknown", "health": "unknown", "processPresent": running,
           "reason": "unit-state-unavailable" if complete else "process-inventory-unavailable", "units": observed,
           "intendedUnit": {"scope": "system", "id": "agiw-coordinator.service",
                            "runtime": COORDINATOR_RUNTIME, "releaseSha256": COORDINATOR_RELEASE_SHA256,
                            "source": "map-linux 2026-10-17"}}
    if not complete or any(value["state"] == "unknown" for value in observed.values()):
        return row
    intended = observed["system"]
    retired = observed["user"]
    # An active retired v7 runtime is separate evidence, not a contradiction of
    # the intended v8 unit's absence. Unidentified or mismatched processes still
    # conflict, including another process using the same v7 command or a v7
    # process whose configured user unit is inactive.
    intended_process = any(_coordinator_process_matches(process, intended) for process in coordinators)
    unaccounted = any(not (_coordinator_process_matches(process, intended)
                          or (retired["runtime"] == "linux-scheduler-authority-v7"
                              and _coordinator_process_matches(process, retired)))
                      for process in coordinators)
    if intended["state"] != "absent" and (intended["runtime"] != COORDINATOR_RUNTIME
            or intended["releaseSha256"] != COORDINATOR_RELEASE_SHA256):
        row["reason"] = "intended-unit-release-unconfirmed"
    elif unaccounted or (intended["state"] == "active" and not intended_process):
        row["reason"] = "process-unit-state-conflict"
    elif intended["state"] == "active":
        row.update(state="active", health="up", reason="intended-unit-active-observed")
    elif intended["state"] == "disabled":
        row.update(state="disabled", health="down", reason="intended-unit-disabled-observed")
    else:
        row.update(state="absent", health="down", reason="intended-system-unit-absent-observed")
    return row


def _journal(path: Path, now: float) -> dict | None:
    record = _record(path, HTTP_MAX_BYTES)
    if record is None:
        return None
    payload, observed = record
    try:
        value = lanes.loads_strict(payload.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    age = now - observed
    if not isinstance(value, dict) or not math.isfinite(age) or age < -5:
        return None
    result = {"observedUnix": observed, "ageSeconds": round(max(0.0, age), 1)}
    # Journal string schemas are unconfirmed. Do not export arbitrary text,
    # including through the full loopback snapshot; only bounded counters pass.
    for key in ("epoch", "admission", "pause", "generation"):
        item = value.get(key)
        if type(item) is int and 0 <= item <= 2 ** 53:
            result[key] = item
    return result


def _journal_names(directory: Path) -> list[str]:
    fd = None
    try:
        fd = _open_directory(directory)
        with os.scandir(fd) as scan:
            names = []
            for index, entry in enumerate(scan):
                if index >= STATE_SCAN_LIMIT:
                    return []
                if re.fullmatch(r"[0-9]{8}\.json", entry.name):
                    names.append(entry.name)
        return names
    except (OSError, ValueError):
        return []
    finally:
        if fd is not None:
            os.close(fd)


def read_codex_state(state_root: Path, now: float | None = None) -> dict:
    """Read only named journals; never run a coordinator CLI, lock, or write state."""
    now = time.time() if now is None else now
    state_root = Path(state_root)
    router = state_root if state_root.name == "codemode-router" else state_root / "codemode-router"
    result = {"state": "unknown", "authority": {}, "fence": _journal(router / "install-fence.json", now),
              "lastRun": {"state": "unknown", "reason": "terminal-record-schema-unconfirmed"},
              "idleAnalysis": {"state": "unknown", "reason": "no-passive-record-path-confirmed"},
              "qwen35": {"state": "staged", "serving": None, "source": "map-linux 2026-10-17; not promoted"}}
    for kind in ("epochs", "admission", "pauses"):
        directory = router / "scheduler-authority" / kind
        names = _journal_names(directory)
        result["authority"][kind] = _journal(directory / max(names), now) if names else None
    if result["fence"] is not None or any(result["authority"].values()):
        result["state"] = "recorded"
    return result


def read_pair_files(home: Path, now: float | None = None) -> dict:
    """PAIR's own membership claim, never proof that a peer engine is available."""
    now = time.time() if now is None else now
    result = {"state": "unknown", "reason": "unavailable", "attribution": "PAIR says",
              "observedUnix": None, "ageSeconds": None, "members": [], "membersComplete": False,
              "pathConfirmed": False}
    path = Path(home) / ".config/Nvidia Corporation/Personal AI Router/cluster/members.json"
    record = _record(path, HTTP_MAX_BYTES)
    if record is None:
        return result
    payload, observed = record
    age = now - observed
    if not math.isfinite(age) or age < -5:
        result["reason"] = "clock-invalid"
        return result
    try:
        value = lanes.loads_strict(payload.decode("utf-8"))
    except (ValueError, UnicodeError):
        result["reason"] = "invalid-record"
        return result
    members = value.get("members") if isinstance(value, dict) else value
    if not isinstance(members, list) or len(members) > 32:
        result["reason"] = "invalid-members"
        return result
    rows, seen = [], set()
    for member in members:
        if not isinstance(member, dict):
            result["reason"] = "invalid-members"
            return result
        identity = member.get("id", member.get("nodeId", member.get("node")))
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity) or identity in seen:
            result["reason"] = "invalid-members"
            return result
        seen.add(identity)
        row = {"id": identity}
        for target, item in (("name", member.get("name", member.get("hostname"))),
                             ("state", member.get("state")), ("lastSeen", member.get("lastSeen"))):
            if isinstance(item, str) and item.isprintable() and len(item) <= 128:
                row[target] = item
            elif target == "lastSeen" and lanes._number(item, low=0) is not None:
                row[target] = item
            else:
                row[target] = None
        rows.append(row)
    result.update(state="recorded", reason="vendor-record", observedUnix=observed,
                  ageSeconds=round(max(0.0, age), 1), members=rows, membersComplete=True)
    return result
