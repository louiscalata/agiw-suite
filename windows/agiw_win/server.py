"""Loopback HTTP observer for the Windows edition: snapshot, live stream and the shared dashboard."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import SERVICE, VERSION
from . import probes
from .collect import Collector

DEFAULT_PORT = 8767
ACTIVE = {"generating", "busy"}
KNOWN_ACTIVITY = ACTIVE | {"idle"}
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/online-code-mode.mjs": ("online-code-mode.mjs", "text/javascript; charset=utf-8"),
          "/map-layout.mjs": ("map-layout.mjs", "text/javascript; charset=utf-8"),
          "/model-control-view.mjs": ("model-control-view.mjs", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def find_web_root(start: Path) -> Path:
    """web/ beside the install (installed layout) or at the repository root (source layout)."""
    for candidate in (start / "web", start.parent / "web", start.parent.parent / "web"):
        if (candidate / "index.html").is_file():
            return candidate
    raise FileNotFoundError("dashboard assets (web/index.html) not found")


def activity_is_known(rows: list[dict], source_status: dict[str, str]) -> bool:
    """Every loaded local lane row must carry fresh, exact slot activity."""
    if source_status.get("llama-slots") != "live":
        return False
    loaded = [row for row in rows if row.get("loaded") is True]
    return bool(loaded) and all(row.get("source") == "llama-slots" and row.get("state") in KNOWN_ACTIVITY
                                and isinstance(row.get("ageSeconds"), (int, float)) and row["ageSeconds"] <= 3
                                for row in loaded)


class SnapshotStore:
    """Atomic snapshot publication with history and observed transitions (mirrors the Mac store)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.snapshot = None
        self.history: list[dict] = []
        self.events: list[dict] = []
        self.previous: dict[str, dict] = {}
        self.sequence = 0

    def publish(self, data: dict) -> None:
        data = copy.deepcopy(data)
        now = data["sampledAt"]
        rows = data.get("models", [])
        status = {s["id"]: s.get("state") for s in data.get("sources", [])}
        known = activity_is_known(rows, status)
        data["activityKnown"] = known
        with self.lock:
            self.sequence += 1
            states = {}
            for row in rows:
                key = f"{row['host']}:{row['id']}"
                states[key] = {"state": row["state"], "source": row.get("source")}
                before = self.previous.get(key)
                if before is None or (before["state"] == row["state"] and before["source"] == row.get("source")):
                    continue
                was_known = before["source"] == "llama-slots" and before["state"] in KNOWN_ACTIVITY
                is_known = row.get("source") == "llama-slots" and row["state"] in KNOWN_ACTIVITY
                kind, label = (("evidence-loss", "Activity visibility lost; inventory only") if was_known and not is_known
                               and row["state"] != "unloaded" else
                               ("evidence-recovered", "Lane activity reporting resumed") if is_known and not was_known
                               and before["state"] != "unloaded" else
                               ("state-change", "Observed lane state changed"))
                self.events.insert(0, {"at": now, "model": row["name"], "host": row["host"],
                                       "before": before["state"], "state": row["state"], "kind": kind,
                                       "label": label, "sourceBefore": before["source"], "source": row.get("source")})
            self.previous = states
            self.events = self.events[:50]
            live = [r for r in rows if isinstance(r.get("ageSeconds"), (int, float)) and r["ageSeconds"] <= 3]
            self.history.append({"at": now,
                                 "active": sum(r["state"] in ACTIVE for r in live) if known else None,
                                 "loaded": sum(r.get("loaded") is True for r in live)})
            self.history = self.history[-90:]
            data.update(sequence=self.sequence, history=copy.deepcopy(self.history),
                        events=copy.deepcopy(self.events), intervalSeconds=1, fullSampledAt=now)
            self.snapshot = data
            self.changed.notify_all()

    def read(self):
        with self.lock:
            return copy.deepcopy(self.snapshot)

    def wait_for_change(self, last: int | None, timeout: float):
        with self.changed:
            if not self.changed.wait_for(lambda: self.snapshot is not None and self.sequence != last, timeout=timeout):
                return None, last
            return copy.deepcopy(self.snapshot), self.sequence


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8
    max_streams = 3

    def __init__(self, address, store: SnapshotStore, web_root: Path):
        if address[0] != "127.0.0.1":
            raise ValueError("Monitor must bind to IPv4 loopback")
        self.store, self.web_root = store, web_root
        self.stopping = threading.Event()
        self._streams = threading.BoundedSemaphore(self.max_streams)
        super().__init__(address, Handler)

    def server_bind(self):
        # Windows lets a second socket bind a port another process holds unless this is set.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, *_):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "AGIWWin/" + VERSION
    sys_version = ""
    timeout = 5

    def log_message(self, *_):
        pass

    def same_origin(self, require_origin: bool = False) -> bool:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host_headers = self.headers.get_all("Host", [])
        if len(host_headers) != 1 or host_headers[0] not in hosts:
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (require_origin and len(origins) != 1):
            return False
        return not origins or origins[0] == f"http://{host_headers[0]}"

    def reply(self, status: int, payload: bytes, mime: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def json_reply(self, status: int, value) -> None:
        self.reply(status, json.dumps(value, allow_nan=False).encode(), "application/json")

    def do_GET(self):
        if not self.same_origin():
            return self.reply(403, b"Invalid host or origin", "text/plain")
        path = urlsplit(self.path).path
        if path == "/api/health":
            return self.json_reply(200, {"service": SERVICE, "version": VERSION, "pid": os.getpid()})
        if path == "/api/snapshot":
            data = self.server.store.read()
            return self.json_reply(200, data) if data is not None else self.json_reply(503, {"status": "starting"})
        if path == "/api/stream":
            return self.stream()
        if path == "/api/models/control":
            return self.json_reply(200, {"status": "idle", "operationId": 0, "supported": False,
                                         "message": "Model load and unload are not offered on the PC lanes."})
        if path in ("/api/models/auto-unload", "/api/online-code-mode/repair", "/api/inference/fix",
                    "/api/online-code-mode/entry", "/api/online-code-mode/headless", "/api/components",
                    "/api/components/jev"):
            return self.json_reply(501, {"status": "error", "message": "Not available in the Windows edition yet."})
        if path == "/usage-format.mjs":
            return self.asset(self.server.web_root.parent / "usage-format.mjs", "text/javascript; charset=utf-8")
        asset = ASSETS.get(path)
        if asset is None:
            return self.reply(404, b"Not found", "text/plain")
        return self.asset(self.server.web_root / asset[0], asset[1])

    def asset(self, path: Path, mime: str) -> None:
        try:
            self.reply(200, path.read_bytes(), mime)
        except OSError:
            self.reply(503, b"Asset unavailable", "text/plain")

    def do_POST(self):
        if not self.same_origin(require_origin=True):
            return self.json_reply(403, {"status": "error", "message": "Exact same-origin Origin and Host are required."})
        return self.json_reply(501, {"status": "error", "message": "The Windows edition is read-only in this version."})

    def stream(self):
        if not self.server._streams.acquire(blocking=False):
            return self.json_reply(503, {"status": "busy", "message": "Too many live streams"})
        try:
            self.connection.settimeout(5)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.close_connection = True
            last = None
            while not self.server.stopping.is_set():
                data, sequence = self.server.store.wait_for_change(last, timeout=1.0)
                if data is None:
                    chunk = b": alive\n\n"
                else:
                    last = sequence
                    chunk = (f"id: {sequence}\nevent: snapshot\ndata: ".encode()
                             + json.dumps(data, allow_nan=False, separators=(",", ":")).encode() + b"\n\n")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ValueError):
            pass
        finally:
            self.server._streams.release()


def sample(store: SnapshotStore, collector: Collector, stop: threading.Event, interval: float = 1.0) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            store.publish(collector.collect())
        except Exception as error:  # noqa: BLE001 - a failed sample ages out; it is never re-stamped
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} sample failed: {error!r}", file=sys.stderr)
        stop.wait(max(0.05, interval - (time.monotonic() - started)))


def bind(store: SnapshotStore, web_root: Path, port: int, retry_seconds: float = 12.0) -> MonitorServer:
    """The preferred port, retried briefly (a restart can find it still held by closing sockets), else ephemeral."""
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            return MonitorServer(("127.0.0.1", port), store, web_root)
        except OSError:
            if port == 0:
                raise
            if time.monotonic() >= deadline:
                return MonitorServer(("127.0.0.1", 0), store, web_root)
            time.sleep(0.5)


def write_state(path: Path | None, port: int) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"service": SERVICE, "version": VERSION, "port": port, "pid": os.getpid(),
                                   "url": f"http://127.0.0.1:{port}/", "startedUnix": round(time.time(), 3)}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def parent_watch(pid: int):
    """A callable reporting whether the parent still runs; on Windows it holds one SYNCHRONIZE handle opened at
    startup, so a later reuse of the PID by another process cannot keep the observer alive."""
    if os.name != "nt":
        def alive() -> bool:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
        return alive
    import ctypes  # noqa: PLC0415
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return None  # cannot watch (already gone or not permitted): do not guess
    return lambda: kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AGIW Inference Monitor · Windows observer")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--share-root", type=Path, default=Path(os.environ.get("AGIW_SHARE_ROOT", probes.DEFAULT_SHARE_ROOT)))
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--once", action="store_true", help="print one snapshot as JSON and exit")
    parser.add_argument("--no-lan", action="store_true", help="skip the Mac LAN probe")
    parser.add_argument("--log-file", type=Path, help="append stderr here (the tray shell does not read stderr)")
    args = parser.parse_args(argv)
    if args.log_file:
        try:
            args.log_file.parent.mkdir(parents=True, exist_ok=True)
            sys.stderr = open(args.log_file, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives for the process
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} observer {VERSION} starting (pid {os.getpid()})", file=sys.stderr)
        except OSError:
            pass

    here = Path(__file__).resolve().parent
    clients_fn = None
    for root in (here.parent, here.parent.parent):
        if (root / "client_models.py").is_file():
            sys.path.insert(0, str(root))
            try:
                from client_models import collect_clients  # noqa: PLC0415
                clients_fn = collect_clients
            except Exception:  # noqa: BLE001
                clients_fn = None
            break
    mac_peer = None if args.no_lan else probes.MacPeer(args.share_root)
    collector = Collector(args.share_root, clients_fn=clients_fn, mac_peer=mac_peer, lane_wait=6.0 if args.once else 0.0)
    if args.once:
        if mac_peer:
            mac_peer.poll_once()
        print(json.dumps(collector.collect(), indent=1, default=str))
        collector.close()
        return 0

    store = SnapshotStore()
    server = bind(store, find_web_root(here.parent), args.port)
    port = server.server_address[1]
    stop = threading.Event()
    if mac_peer:
        mac_peer.start()
    threading.Thread(target=sample, args=(store, collector, stop), name="sampler", daemon=True).start()

    def shutdown(*_):
        stop.set()
        server.stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown)
    # Under the tray, exit when the observer's own source changes so an update applies without a manual
    # restart; the tray starts the new code within its backoff (exit code 3 marks it as an update).
    reload_code = {"value": 0}
    if args.parent_pid:
        sources = sorted(here.glob("*.py"))
        def fingerprint():
            try:
                return tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in sources)
            except OSError:
                return None
        baseline = fingerprint()
        def watch_source():
            while not stop.wait(2):
                now = fingerprint()
                if now is not None and baseline is not None and now != baseline:
                    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} source changed; exiting for reload", file=sys.stderr)
                    reload_code["value"] = 3
                    shutdown()
                    return
        threading.Thread(target=watch_source, name="source-watch", daemon=True).start()
    alive = parent_watch(args.parent_pid) if args.parent_pid else None
    if alive is not None:
        def watch_parent():
            while not stop.wait(2):
                if not alive():
                    shutdown()
                    return
        threading.Thread(target=watch_parent, name="parent-watch", daemon=True).start()
    print(json.dumps({"port": port, "pid": os.getpid(), "service": SERVICE}), flush=True)
    write_state(args.state_file, port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        collector.close()
        server.server_close()
    return reload_code["value"]


if __name__ == "__main__":
    raise SystemExit(main())
