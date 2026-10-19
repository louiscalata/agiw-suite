"""Bounded one-shot output, private heartbeats and an optional loopback JSON API."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import stat
import sys
import threading
import time

from .collect import Collector, number, project_status, unknown_snapshot

STATUS_MAX_BYTES = 65536
ONCE_SECONDS = 3.6  # Leave startup/JSON/output room within the four-second forced-command budget.
HEARTBEAT_SECONDS = 10.0
# Resolve once at process startup: an old process must not adopt a repointed link.
RELEASE_COMMIT = Path(__file__).resolve().parent.parent.name
if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", RELEASE_COMMIT):
    RELEASE_COMMIT = None


def default_state_dir():
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    if not base.is_absolute():
        base = Path.home() / ".local/state"
    return base / "agiw"


def encode(value, cap=STATUS_MAX_BYTES):
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(payload) > cap:
        raise ValueError("snapshot-too-large")
    return payload


class SampleDeadline(BaseException):
    # Probe-level OSError/Exception guards must not swallow the overall deadline.
    pass


@contextmanager
def deadline(seconds=ONCE_SECONDS):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("bounded sampling must run on the main thread")
    def expired(*_):
        raise SampleDeadline("sample-deadline")
    handler = signal.getsignal(signal.SIGALRM)
    previous = signal.setitimer(signal.ITIMER_REAL, 0)
    started = time.monotonic()
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)
        if previous[0]:
            signal.setitimer(signal.ITIMER_REAL, max(.001, previous[0] - (time.monotonic() - started)), previous[1])


def cached_status(path, now):
    try:
        with private_directory(path.parent, create=False) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 \
                        or info.st_nlink != 1 or info.st_size > STATUS_MAX_BYTES:
                    return None
                payload = stream.read(STATUS_MAX_BYTES + 1)
        if len(payload) > STATUS_MAX_BYTES:
            return None
        value = json.loads(payload)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or type(value.get("schemaVersion")) is not int or value.get("schemaVersion") != 1 or value.get("kind") != "agiw-peer" \
            or value.get("platform") != "linux" or value.get("edition") != "linux":
        return None
    if not isinstance(value.get("host"), str) or not isinstance(value.get("observedAt"), str) \
            or not isinstance(value.get("memory"), dict) \
            or not (isinstance(value.get("gpus"), list) or value.get("gpus") is None
                    and value.get("gpusComplete") is False and value.get("gpusState") == "unknown") \
            or not isinstance(value.get("components"), list) or value.get("health") not in ("up", "down", "unknown"):
        return None
    at = number(value.get("observedUnix"))
    if at is None or not 0 <= now - at <= HEARTBEAT_SECONDS:
        return None
    try:
        iso_at = dt.datetime.fromisoformat(value.get("observedAt", "").replace("Z", "+00:00"))
        if iso_at.tzinfo is None or abs(iso_at.timestamp() - at) > .01:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    if not isinstance(value.get("models"), list) or len(value["models"]) > 32:
        return None
    if "historicalRates" in value and (not isinstance(value["historicalRates"], list)
            or len(value["historicalRates"]) > 2 or any(not isinstance(row, dict) for row in value["historicalRates"])):
        return None
    if "network" in value:
        network = value["network"]
        if not isinstance(network, dict) or any(not isinstance(network.get(key), list)
                or len(network[key]) > 32 or any(not isinstance(row, dict) for row in network[key])
                for key in ("listeners", "callers")):
            return None
    seen = set()
    for row in value["models"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] \
                or row.get("loadedState") not in ("loaded", "inactive", "unknown"):
            return None
        if row["id"] in seen:
            return None
        seen.add(row["id"])
    # Reproject rather than echoing arbitrary fields from a file written by another process.
    snapshot = {**value, "sampledAt": at, "hostId": value.get("host"),
                "modelsComplete": value.get("modelsComplete") is True and type(value.get("modelsTotal")) is int
                and value.get("modelsTotal") == len(value["models"])}
    result = project_status(snapshot)
    encode(result)
    return result


def sample(collector, *, twice=False, sleep=time.sleep):
    with deadline():
        first = collector.collect()
        if not twice:
            return first
        sleep(1.0)
        return collector.collect()


def once(collector=None, *, state_dir=None, clock=time.time, sleep=time.sleep):
    state_dir = default_state_dir() if state_dir is None else Path(state_dir)
    try:
        # The same deadline covers cache reads, fallback probes and projection.
        with deadline():
            cached = cached_status(state_dir / "linux-status.json", clock())
            if cached is not None:
                return encode(cached)
            collector = collector or Collector(clock=clock)
            collector.collect()
            sleep(1.0)
            snapshot = collector.collect()
            if not isinstance(snapshot.get("components"), list):
                snapshot.update(components=[], componentsComplete=False)
            snapshot["components"].append({"id": "heartbeatUnit", "health": "unknown",
                "reason": "heartbeat-not-fresh-live-fallback"})
            return encode(project_status(snapshot))
    except SampleDeadline:
        return encode(project_status(unknown_snapshot("sample-deadline", clock())))
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return encode(project_status(unknown_snapshot("sample-unavailable", clock())))


@contextmanager
def private_directory(path, *, create=True):
    """Hold the destination by fd across rename; never follow a parent symlink."""
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("state directory must be private to the observer user")
        yield fd
    finally:
        os.close(fd)


def write_status(state_dir, value):
    payload = encode(value)
    with private_directory(state_dir) as parent:
        try:
            existing = os.stat("linux-status.json", dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid():
                raise PermissionError("unsafe heartbeat target")
        except FileNotFoundError:
            pass
        temporary = f".linux-status-{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, "linux-status.json", src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


class SnapshotStore:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.latest = None
        self.lock = threading.Lock()

    def publish(self, snapshot):
        with self.lock:
            self.latest = snapshot

    def read(self):
        with self.lock:
            value = self.latest
        if value is None:
            return None
        at = number(value.get("sampledAt"))
        if at is None or not 0 <= self.clock() - at <= HEARTBEAT_SECONDS:
            return unknown_snapshot("snapshot-stale-or-future", self.clock())
        return value


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def do_GET(self):
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}") \
                or self.headers.get("Origin") not in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            self.send_error(403)
            return
        if self.path not in ("/api/snapshot", "/api/status"):
            self.send_error(404)
            return
        value = self.server.store.read()
        status = 200 if value is not None else 503
        value = value or unknown_snapshot()
        if self.path == "/api/status":
            value = project_status(value)
        try:
            payload = encode(value, 512_000)
        except (ValueError, TypeError):
            status, payload = 503, encode(unknown_snapshot("snapshot-invalid"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.send_error(405, "Read-only observer")

    do_PUT = do_DELETE = do_PATCH = do_POST


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8

    def __init__(self, address, store):
        if address[0] != "127.0.0.1":
            raise ValueError("dashboard must bind IPv4 loopback")
        self.store = store
        super().__init__(address, Handler)


def serve(collector=None, *, state_dir=None, port=None, stop=None, clock=time.time):
    collector, stop = collector or Collector(clock=clock), stop or threading.Event()
    state_dir = default_state_dir() if state_dir is None else Path(state_dir)
    store, http = SnapshotStore(clock), None
    if port is not None:
        http = MonitorServer(("127.0.0.1", port), store)
        threading.Thread(target=http.serve_forever, kwargs={"poll_interval": .2}, daemon=True).start()
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                snapshot = sample(collector)
            except SampleDeadline:
                snapshot = unknown_snapshot("sample-deadline", clock())
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                snapshot = unknown_snapshot("sample-unavailable", clock())
            if not isinstance(snapshot.get("components"), list):
                snapshot.update(components=[], componentsComplete=False)
            snapshot["components"].append({"id": "heartbeatUnit", "health": "up", "reason": "publisher-running"})
            snapshot["releaseCommit"] = RELEASE_COMMIT
            write_status(state_dir, project_status(snapshot))
            store.publish(snapshot)
            stop.wait(max(.05, 5.0 - (time.monotonic() - started)))
    finally:
        if http is not None:
            http.shutdown()
            http.server_close()


def write_output(payload, fd, expires):
    """A stalled SSH pipe must not outlive the one-shot deadline."""
    blocking = os.get_blocking(fd)
    os.set_blocking(fd, False)
    try:
        offset = 0
        while offset < len(payload):
            remaining = expires - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                return False
            try:
                offset += os.write(fd, payload[offset:])
            except BlockingIOError:
                continue
        return True
    except OSError:
        return False
    finally:
        os.set_blocking(fd, blocking)


def main(argv=None):
    expires = time.monotonic() + 3.85
    parser = argparse.ArgumentParser(description="Read-only Linux inference observations")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, help="Optional loopback-only JSON API port (serve mode)")
    args = parser.parse_args(argv)
    if args.port is not None and (args.once or not 1 <= args.port <= 65535):
        parser.error("--port requires --serve and a port from 1 to 65535")
    if args.once:
        return 0 if write_output(once(), sys.stdout.fileno(), expires) else 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    serve(port=args.port, stop=stop)
    return 0
