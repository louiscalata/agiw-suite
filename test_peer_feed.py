import hmac
import json
import os
import pwd
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import peer_feed
import peer_link


def presence(age=2.0, platform="windows", **extra):
    now = time.time() - age
    return {"schemaVersion": 1, "kind": "agiw-peer", "host": "louisaurorar12", "platform": platform,
            "observedAt": peer_link._iso(now), "observedUnix": now, "health": "ok",
            "models": [{"id": "gpt-oss-20b", "loadedState": "loaded", "activity": "idle", "slotsBusy": 0,
                        "slotsTotal": 2, "liveTokensPerSecond": 104.27, "secretish": "/C:/Users/x"}],
            "gpus": [{"name": "RTX 3080 Ti", "utilizationPercent": 4.0, "path": "C:\\nope"}],
            "memory": {"level": "ok", "availablePercent": 55.6, "commit": 1}, **extra}


class Share(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "llm-lab" / "status"
        (self.root / "agiw-peers").mkdir(parents=True)

    def put(self, name, doc):
        (self.root / name).write_text(json.dumps(doc))

    def test_fresh_presence_is_live_and_only_known_fields_pass(self):
        self.put("agiw-peers/louisaurorar12.json", presence())
        feed = peer_feed.from_share(time.time(), self.root, self.base)
        self.assertEqual((feed["via"], feed["host"], feed["models"][0]["slotsTotal"]), ("share", "louisaurorar12", 2))
        self.assertNotIn("secretish", feed["models"][0])
        self.assertNotIn("path", feed["gpus"][0])
        self.assertNotIn("commit", feed["memory"])
        self.assertEqual(feed["models"][0]["liveTokensPerSecond"], 104.3)

    def test_stale_or_mac_or_broken_files_are_not_live(self):
        self.put("agiw-peers/old.json", presence(age=45))
        self.put("agiw-peers/mac.json", presence(platform="mac"))
        self.put("agiw-peers/bad.json", {"schemaVersion": 1})
        (self.root / "agiw-peers" / "nan.json").write_text('{"schemaVersion": NaN}')
        (self.root / "agiw-peers" / "big.json").write_text(" " * 70000)
        self.assertIsNone(peer_feed.from_share(time.time(), self.root, self.base))

    def test_windows_status_json_is_the_fallback(self):
        doc = presence()
        doc.pop("kind")
        doc["host"] = "windows"
        self.put("windows-status.json", doc)
        self.assertEqual(peer_feed.from_share(time.time(), self.root, self.base)["models"][0]["id"], "gpt-oss-20b")

    def test_symlinked_record_or_folder_is_refused(self):
        real = self.root.parent / "elsewhere.json"
        real.write_text(json.dumps(presence()))
        os.symlink(real, self.root / "agiw-peers" / "link.json")
        self.assertIsNone(peer_feed.from_share(time.time(), self.root, self.base))

    def test_symlinked_ancestor_folder_is_refused(self):
        self.put("agiw-peers/louisaurorar12.json", presence())
        moved = self.base / "real-llm-lab"
        (self.base / "llm-lab").rename(moved)
        os.symlink(moved, self.base / "llm-lab")
        with self.assertRaises(peer_feed.ShareRefused):
            peer_feed.from_share(time.time(), self.root, self.base)

    def test_infinite_numbers_and_odd_memory_values_are_dropped(self):
        doc = presence()
        self.put("agiw-peers/louisaurorar12.json", doc)
        raw = (self.root / "agiw-peers" / "louisaurorar12.json").read_text()
        raw = raw.replace('"utilizationPercent": 4.0', '"utilizationPercent": 1e999')
        raw = raw.replace('"availablePercent": 55.6', '"availablePercent": {"nested": 1}')
        (self.root / "agiw-peers" / "louisaurorar12.json").write_text(raw)
        feed = peer_feed.from_share(time.time(), self.root, self.base)
        self.assertNotIn("utilizationPercent", feed["gpus"][0])
        self.assertEqual(feed["memory"], {"level": "ok"})
        json.dumps(feed, allow_nan=False)  # the snapshot must stay serializable

    def test_future_dated_record_is_a_clock_mismatch_not_fresh(self):
        self.put("agiw-peers/louisaurorar12.json", presence(age=-45))
        self.assertIsNone(peer_feed.from_share(time.time(), self.root, self.base))

    def test_missing_share_is_none(self):
        self.assertIsNone(peer_feed.from_share(time.time(), Path("/nonexistent/llm-lab/status")))


class Feed(unittest.TestCase):
    def test_stale_lan_result_falls_back_to_a_fresh_share(self):
        token_dir = Path(tempfile.mkdtemp())
        (token_dir / "control-token").write_text("a" * 64)
        stale = presence(age=45)
        listing = {"peers": [{"paired": True, "host": "windows", "state": "connected", "status": stale}]}
        with mock.patch.object(peer_link, "default_state_dir", return_value=token_dir), \
                mock.patch.object(peer_feed, "_control_get", return_value=(200, listing)):
            self.assertIsNone(peer_feed.from_peer_link(time.time()))
            listing["peers"][0]["status"] = presence(age=2)
            self.assertEqual(peer_feed.from_peer_link(time.time())["via"], "lan")

    def test_no_peer_link_setup_means_no_lan_attempt_and_nothing_created(self):
        empty = Path(tempfile.mkdtemp()) / "peer-link"
        with mock.patch.object(peer_link, "default_state_dir", return_value=empty), \
                mock.patch.object(peer_feed, "_control_get", side_effect=AssertionError("must not be called")), \
                mock.patch.object(peer_link, "control_request", side_effect=AssertionError("must not be called")):
            self.assertIsNone(peer_feed.from_peer_link(time.time()))
        self.assertFalse(empty.exists())

    def test_lan_beats_share(self):
        lan = peer_feed._summary(peer_link.validate_status(presence(), "windows"), "lan", time.time())
        with mock.patch.object(peer_feed, "from_peer_link", return_value=lan), \
                mock.patch.object(peer_feed, "from_share", side_effect=AssertionError("share read not needed")):
            feed, source = peer_feed.windows_feed(time.time())
        self.assertEqual((feed["via"], source["state"]), ("lan", "live"))
        self.assertIn("LAN", source["detail"])

    def test_share_used_when_lan_fails_and_says_unsigned(self):
        share = peer_feed._summary(peer_link.validate_status(presence(), "windows"), "share", time.time())
        with mock.patch.object(peer_feed, "from_peer_link", side_effect=ConnectionRefusedError()), \
                mock.patch.object(peer_feed, "from_share", return_value=share):
            feed, source = peer_feed.windows_feed(time.time())
        self.assertEqual(feed["via"], "share")
        # The map's words (REPORTED VIA SHARE) and the PC's: an unsigned share file is reported, never "linked",
        # and its source is recorded, never live (review finding D2).
        self.assertEqual(source["state"], "recorded")
        self.assertEqual(source["detail"], "Unsigned report from louisaurorar12 in SharedChami status files "
                                           "(not verified; LAN link not connected: ConnectionRefusedError)")
        self.assertNotIn("Linked", source["detail"])

    def test_hung_share_is_reported_not_waited_on(self):
        release = threading.Event()
        reader = peer_feed._ShareReader()
        with mock.patch.object(peer_feed, "from_share", side_effect=lambda now: release.wait(5)), \
                mock.patch.object(peer_feed, "SHARE_READ_DEADLINE", 0.2):
            started = time.monotonic()
            self.assertEqual(reader.read(time.time())[0], "stuck")
            self.assertEqual(reader.read(time.time())[0], "stuck")  # no second thread piles up
            self.assertLess(time.monotonic() - started, 1.0)
        release.set()

    def test_nothing_fresh_is_unavailable_with_reason(self):
        with mock.patch.object(peer_feed, "from_peer_link", return_value=None), \
                mock.patch.object(peer_feed, "from_share", return_value=None):
            feed, source = peer_feed.windows_feed(time.time())
        self.assertIsNone(feed)
        self.assertEqual(source["state"], "unavailable")
        self.assertIn("no fresh PC status", source["detail"])


class MacPresence(unittest.TestCase):
    def test_maps_snapshot_rows_and_validates(self):
        snap = {"sampledAt": time.time(), "activityKnown": True, "memory": {"level": "watch", "availablePercent": 57},
                "models": [{"id": "qwen", "host": "mac", "loaded": True, "state": "generating"},
                           {"id": "gemma", "host": "mac", "loaded": False, "state": "unloaded"},
                           {"id": "pc-model", "host": "windows", "loaded": True, "state": "idle"}]}
        doc = peer_feed.mac_presence(snap, "louis-mac")
        peer_link.validate_status(doc, "mac", time.time())
        self.assertEqual([(m["id"], m["loadedState"], m["activity"]) for m in doc["models"]],
                         [("qwen", "loaded", "generating"), ("gemma", "inactive", "unknown")])

    def test_no_snapshot_is_an_error_not_an_empty_status(self):
        with self.assertRaises(ValueError):
            peer_feed.mac_presence(None, "louis-mac")


HERE = Path(__file__).resolve().parent
WINDOWS = HERE / "windows"


def pc_probes():
    """The PC edition's own producer (windows/agiw_win/probes.py), imported read-only for interop checks."""
    if not (WINDOWS / "agiw_win" / "probes.py").is_file():
        raise unittest.SkipTest("PC edition sources are not in this checkout")
    before, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    sys.path.insert(0, str(WINDOWS))
    try:
        from agiw_win import probes
    finally:
        sys.path.remove(str(WINDOWS))
        sys.dont_write_bytecode = before
    return probes


class PcMemoryLevels(unittest.TestCase):
    """Review finding A1: every level the PC's producer emits, "tight" above all, reaches the Mac's reader."""

    def test_pc_tight_memory_survives_producer_to_reader(self):
        probes = pc_probes()
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        root = base / "llm-lab" / "status"
        (root / "agiw-peers").mkdir(parents=True)
        gib = 1 << 30
        # (available %, commit %, level) through the PC's own probes.memory_level thresholds.
        for available, commit, expected in ((50, None, "ok"), (15, None, "watch"), (10, None, "tight"),
                                            (3, None, "critical"), (50, 97, "tight")):
            reading = {"totalPhys": 100 * gib, "availPhys": available * gib}
            if commit is not None:
                reading.update(commitLimit=100 * gib, commitAvail=(100 - commit) * gib)
            block, _ = probes.memory_block(lambda: reading, lambda: [])
            self.assertEqual(block["level"], expected)
            now = time.time()
            doc = probes.peer_presence({"lanes": [{"id": "fast", "status": "idle", "phase": "idle", "port": 1235,
                                                   "expectedModel": "openai/gpt-oss-20b", "servedModel": "gpt-oss-20b",
                                                   "slotsBusy": 0, "slotsTotal": 2}],
                                        "pcGpus": [], "memory": block, "observerVersion": "0.1.0",
                                        "observedAt": peer_feed._iso(now), "sampledAt": now, "dashboardPort": 8767},
                                       "louisaurorar12")
            (root / "agiw-peers" / "louisaurorar12.json").write_text(json.dumps(doc))
            want = {"level": expected, "availablePercent": float(available)}
            # The share file, read as the Mac reads it.
            self.assertEqual(peer_feed.from_share(time.time(), root, base)["memory"], want, expected)
            # The paired LAN link: what leaves the PC (project_status), then the same reader.
            lan = peer_feed._summary(peer_link.validate_status(peer_link.project_status(doc), "windows", time.time()),
                                     "lan", time.time())
            self.assertEqual(lan["memory"], want, expected)

    def test_reader_and_publisher_share_one_level_vocabulary(self):
        import mem_guard
        self.assertEqual(set(peer_feed.MEMORY_LEVELS), set(mem_guard.LEVELS) | {"unknown"})
        doc = presence(memory={"level": "warn", "availablePercent": 9})
        self.assertEqual(peer_feed._summary(peer_link.validate_status(doc, "windows"), "share", time.time())["memory"],
                         {"availablePercent": 9.0})  # a word no producer emits is dropped, the number kept


class ShareBounds(unittest.TestCase):
    """Review finding D1: agiw-peers is enumerated from its own descriptor within a fixed budget, never followed."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / "llm-lab" / "status"
        self.peers = self.root / "agiw-peers"
        self.peers.mkdir(parents=True)
        (self.peers / "louisaurorar12.json").write_text(json.dumps(presence()))

    def fill(self, count):
        for index in range(count):
            (self.peers / f".junk-{index:04d}.tmp").write_bytes(b"")

    def test_normal_folder_reads_as_before(self):
        self.fill(peer_feed.PEER_SCAN_LIMIT - 1)  # exactly the budget, the record included
        feed = peer_feed.from_share(time.time(), self.root, self.base)
        self.assertEqual((feed["via"], feed["host"]), ("share", "louisaurorar12"))

    def test_over_budget_folder_is_unavailable_not_partial(self):
        self.fill(peer_feed.PEER_SCAN_LIMIT)  # one entry over the budget
        doc = presence()
        doc.pop("kind")
        doc["host"] = "windows"
        (self.root / "windows-status.json").write_text(json.dumps(doc))
        with self.assertRaises(peer_feed.ShareRefused) as caught:
            peer_feed.from_share(time.time(), self.root, self.base)
        self.assertIn("too many entries", str(caught.exception))
        self.assertNotIn(str(self.base), str(caught.exception))
        # The refusal reaches the Windows source row through the share reader, with nothing shown as the PC.
        with mock.patch.object(peer_feed, "from_peer_link", return_value=None), \
                mock.patch.object(peer_feed, "SHARE_STATUS", self.root), \
                mock.patch.object(peer_feed, "SHARE_ANCHOR", self.base):
            feed, source = peer_feed.windows_feed(time.time())
        self.assertIsNone(feed)
        self.assertEqual(source["state"], "unavailable")
        self.assertIn("too many entries", source["detail"])

    def test_symlinked_agiw_peers_is_refused_whole(self):
        elsewhere = self.base / "elsewhere"
        self.peers.rename(elsewhere)
        os.symlink(elsewhere, self.peers)
        # Even a fresh windows-status.json beside it is not used: a tampered folder means no share answer at all.
        doc = presence()
        doc.pop("kind")
        doc["host"] = "windows"
        (self.root / "windows-status.json").write_text(json.dumps(doc))
        with self.assertRaises(peer_feed.ShareRefused) as caught:
            peer_feed.from_share(time.time(), self.root, self.base)
        self.assertIn("symbolic link", str(caught.exception))
        self.assertEqual(sorted(p.name for p in elsewhere.iterdir()), ["louisaurorar12.json"])

    def test_folder_swapped_while_read_is_refused(self):
        real_read = peer_feed._read_at
        swapped = []

        def read_then_swap(directory, name):
            data = real_read(directory, name)
            if not swapped and name.endswith(".json") and name != "windows-status.json":
                self.peers.rename(self.root / "agiw-peers-old")
                self.peers.mkdir()
                swapped.append(name)
            return data

        with mock.patch.object(peer_feed, "_read_at", side_effect=read_then_swap):
            with self.assertRaises(peer_feed.ShareRefused) as caught:
                peer_feed.from_share(time.time(), self.root, self.base)
        self.assertEqual(swapped, ["louisaurorar12.json"])
        self.assertIn("replaced while it was read", str(caught.exception))

    def test_scan_stops_at_the_budget(self):
        self.fill(peer_feed.PEER_SCAN_LIMIT * 4)
        listed = []
        real_scandir = os.scandir

        class Counting:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.inner.close()

            def __iter__(self):
                for entry in self.inner:
                    listed.append(entry.name)
                    yield entry

        with mock.patch.object(peer_feed.os, "scandir", side_effect=lambda target: Counting(real_scandir(target))):
            with self.assertRaises(peer_feed.ShareRefused):
                peer_feed.from_share(time.time(), self.root, self.base)
        self.assertEqual(len(listed), peer_feed.PEER_SCAN_LIMIT + 1)


class ShareNeverLive(unittest.TestCase):
    """Review finding D2: an unsigned share report is recorded, never live; the paired LAN feed may stay live."""

    def test_share_source_is_recorded_and_lan_source_is_the_peers_report(self):
        share = peer_feed._summary(peer_link.validate_status(presence(age=0), "windows"), "share", time.time())
        for lan in (None, peer_feed.LinkUnavailable("control token malformed (the peer link replaces it)")):
            effect = {"return_value": None} if lan is None else {"side_effect": lan}
            with mock.patch.object(peer_feed, "from_peer_link", **effect), \
                    mock.patch.object(peer_feed, "from_share", return_value=share):
                feed, source = peer_feed.windows_feed(time.time())
            self.assertEqual((feed["via"], source["state"]), ("share", "recorded"))
            self.assertNotIn("live", source["detail"].lower())
            self.assertTrue(source["detail"].startswith("Unsigned report from louisaurorar12"), source["detail"])
        self.assertIn("LAN link not connected: control token malformed", source["detail"])
        lan_feed = peer_feed._summary(peer_link.validate_status(presence(), "windows"), "lan", time.time())
        with mock.patch.object(peer_feed, "from_peer_link", return_value=lan_feed):
            _feed, source = peer_feed.windows_feed(time.time())
        self.assertEqual(source["state"], "live")
        self.assertEqual(source["detail"], "Reported by louisaurorar12 over the paired LAN link "
                                           "(signed by the paired peer; its own report of its state)")


VALID_TOKEN = "0123456789abcdef" * 4


class Listener:
    """A loopback listener on the peer-link control paths that records every request it receives. With a token it
    answers as the real peer link does (server proof first, then checks the client proof); without one it is a
    squatter whose hello proof is wrong."""

    def __init__(self, token=None, listing=None):
        self.token, self.listing, self.requests, self.issued = token, listing, [], None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, code, doc=None):
                body = json.dumps(doc).encode() if doc is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append((self.command, self.path, dict(self.headers.items())))
                if self.path == "/control/hello":
                    nc, ns = self.headers.get("X-Agiw-Client-Nonce", ""), secrets.token_hex(16)
                    outer.issued = (nc, ns)
                    proof = peer_link.control_server_proof(outer.token, nc, ns) if outer.token else "0" * 64
                    return self.reply(200, {"ns": ns, "proof": proof})
                if self.path == "/control/peers" and outer.token and outer.issued:
                    nc, ns = outer.issued
                    expected = peer_link.control_client_proof(outer.token, ns, nc, "GET", "/control/peers", b"")
                    if (self.headers.get("X-Agiw-Control-Nonce") == ns
                            and hmac.compare_digest(self.headers.get("X-Agiw-Control-Proof", ""), expected)):
                        return self.reply(200, outer.listing)
                self.reply(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def tree(path):
    """Everything a status read could change in a state folder: names, modes, owners, inodes, change times,
    contents (or link targets) and, on macOS, extended access lists."""
    paths = [str(path)]
    for dirpath, dirnames, filenames in os.walk(path):
        paths += [os.path.join(dirpath, name) for name in dirnames + filenames]
    out = {}
    for item in paths:
        info = os.lstat(item)
        if stat.S_ISREG(info.st_mode):
            with open(item, "rb") as handle:
                content = handle.read()
        elif stat.S_ISLNK(info.st_mode):
            content = os.readlink(item)
        else:
            content = None
        acl = ""
        if sys.platform == "darwin":
            acl = "\n".join(subprocess.run(["/bin/ls", "-led", item], capture_output=True, text=True,
                                           timeout=10).stdout.splitlines()[1:])
        out[os.path.relpath(item, path)] = (info.st_mode, info.st_uid, info.st_ino, info.st_ctime_ns,
                                            info.st_mtime_ns, content, acl)
    return out


ACL_TOOL = "/bin/" + "chmod"  # macOS chmod +a / -N: adds the test's folder ACL and removes it in cleanup


class ObserverToken(unittest.TestCase):
    """Review finding A2: observing the paired PC never changes the peer-link state folder, and nothing derived from
    the control token is sent before the listener proves it holds that token."""

    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        self.dir = base / "peer-link"
        self.dir.mkdir()
        os.chmod(self.dir, 0o700)
        (self.dir / "trusted").mkdir(mode=0o700)
        self.put("identity.json", b'{"nodeUuid": "6f1c3a52-6a0e-4c43-8f55-2b1e0c9a7d10"}')
        self.http_calls = []
        real_http = peer_link._http

        def recording_http(*args, **kwargs):
            self.http_calls.append(args[:5])
            return real_http(*args, **kwargs)

        never = AssertionError("the observer must never repair or rotate peer-link state")
        for name in ("load_control_token", "_restrict_dir", "_write_private", "control_request",
                     "_win_restrict_dir", "_win_retire_exposed"):
            patcher = mock.patch.object(peer_link, name, side_effect=never)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(peer_link, "_http", side_effect=recording_http)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, name, data, mode=0o600):
        path = self.dir / name
        path.write_bytes(data)
        os.chmod(path, mode)
        return path

    def listener(self, **kwargs):
        listener = Listener(**kwargs)
        self.addCleanup(listener.close)
        return listener

    def status_read(self, listener):
        """One snapshot's Windows read (LAN then share), then a direct LAN read against the fake listener."""
        share = peer_feed._summary(peer_link.validate_status(presence(), "windows"), "share", time.time())
        with mock.patch.object(peer_link, "default_state_dir", return_value=self.dir), \
                mock.patch.object(peer_feed, "from_share", return_value=share):
            feed, source = peer_feed.windows_feed(time.time())
        direct = None
        try:
            peer_feed.from_peer_link(time.time(), self.dir, listener.port)
        except peer_feed.LinkUnavailable as exc:
            direct = str(exc)
        return feed, source, direct

    def test_token_problems_change_nothing_and_send_nothing(self):
        real_fstat = os.fstat

        def foreign(target):
            def fstat(fd):
                info = real_fstat(fd)
                if info.st_ino != target:
                    return info
                return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid + 1,
                                       info.st_gid, info.st_size, int(info.st_atime), int(info.st_mtime),
                                       int(info.st_ctime)))
            return mock.patch.object(peer_feed.os, "fstat", side_effect=fstat)

        def open_folder():
            self.put("control-token", VALID_TOKEN.encode())
            os.chmod(self.dir, 0o757)

        def group_folder():  # not writable by others, but a running peer link would still chmod it to 0700
            self.put("control-token", VALID_TOKEN.encode())
            os.chmod(self.dir, 0o750)

        cases = [
            ("malformed", lambda: self.put("control-token", b"not-a-token\n"), None, "malformed"),
            ("upper-case hex", lambda: self.put("control-token", VALID_TOKEN.upper().encode()), None, "malformed"),
            ("loose", lambda: self.put("control-token", VALID_TOKEN.encode(), 0o644), None, "another user"),
            ("not text", lambda: self.put("control-token", b"\xff\xfe" * 40), None, "unreadable"),
            ("linked", lambda: os.symlink(self.put("real-token", VALID_TOKEN.encode()).name, self.dir / "control-token"),
             None, "unreadable"),
            ("foreign", lambda: self.put("control-token", VALID_TOKEN.encode()), foreign, "another user"),
            ("open folder", open_folder, None, "writable by other users"),
            ("group-readable folder", group_folder, None, "owner-only"),
        ]
        if sys.platform == "darwin":
            me = pwd.getpwuid(os.getuid()).pw_name

            def with_acl():
                self.put("control-token", VALID_TOKEN.encode())
                if subprocess.run([ACL_TOOL, "+a", f"user:{me} allow list", str(self.dir)],
                                  capture_output=True, timeout=10).returncode != 0:
                    self.skipTest("this volume does not take an extended ACL")
                self.addCleanup(subprocess.run, [ACL_TOOL, "-N", str(self.dir)], capture_output=True, timeout=10)
            cases.append(("folder ACL", with_acl, None, "access list"))
        for name, arrange, patch, reason in cases:
            with self.subTest(name):
                for entry in list(self.dir.iterdir()):
                    if entry.name not in ("trusted", "identity.json"):
                        entry.unlink()
                os.chmod(self.dir, 0o700)
                arrange()
                listener = self.listener()
                before = tree(self.dir)
                self.http_calls.clear()
                if patch is not None:
                    with patch(os.lstat(self.dir / "control-token").st_ino):
                        feed, source, direct = self.status_read(listener)
                else:
                    feed, source, direct = self.status_read(listener)
                self.assertEqual(tree(self.dir), before, "the state folder changed")
                self.assertEqual(listener.requests, [], "something was sent")
                self.assertEqual(self.http_calls, [], "a control exchange was attempted")
                # The share fallback still answers, and the source row says why the LAN feed did not.
                self.assertEqual((feed["via"], source["state"]), ("share", "recorded"))
                self.assertIn(reason, source["detail"])
                self.assertIn(reason, direct or "")
                self.assertNotIn(str(self.dir), source["detail"])
                self.assertNotIn(VALID_TOKEN, json.dumps(source))

    def test_case_variant_token_is_left_alone(self):
        self.put("Control-Token", VALID_TOKEN.encode())
        listener = self.listener()
        before = tree(self.dir)
        _feed, source, direct = self.status_read(listener)
        self.assertEqual(tree(self.dir), before)
        self.assertEqual((listener.requests, self.http_calls), ([], []))
        if (self.dir / "control-token").exists():  # a case-insensitive volume opens the variant for the real name
            self.assertIn("case-variant", direct)
            self.assertIn("case-variant", source["detail"])

    def test_squatter_without_the_proof_gets_nothing_token_derived(self):
        self.put("control-token", VALID_TOKEN.encode())
        squatter = self.listener()
        before = tree(self.dir)
        with self.assertRaises(peer_feed.LinkUnavailable) as caught:
            peer_feed.from_peer_link(time.time(), self.dir, squatter.port)
        self.assertIn("did not prove", str(caught.exception))
        self.assertEqual(tree(self.dir), before)
        self.assertEqual([(method, path) for method, path, _ in squatter.requests], [("GET", "/control/hello")])
        headers = {key.lower(): value for key, value in squatter.requests[0][2].items()}
        self.assertNotIn("x-agiw-control-proof", headers)
        self.assertNotIn("x-agiw-control-nonce", headers)
        self.assertRegex(headers["x-agiw-client-nonce"], r"^[0-9a-f]{32}$")
        self.assertNotIn(VALID_TOKEN, json.dumps(squatter.requests))

    def test_genuine_peer_link_answers_through_the_mutual_handshake(self):
        self.put("control-token", VALID_TOKEN.encode() + b"\n")
        listing = {"peers": [{"paired": True, "host": "windows", "state": "connected", "status": presence(age=1)}]}
        link = self.listener(token=VALID_TOKEN, listing=listing)
        before = tree(self.dir)
        feed = peer_feed.from_peer_link(time.time(), self.dir, link.port)
        self.assertEqual((feed["via"], feed["host"]), ("lan", "louisaurorar12"))
        self.assertEqual(tree(self.dir), before)
        self.assertEqual([path for _, path, _ in link.requests], ["/control/hello", "/control/peers"])
        self.assertNotIn(VALID_TOKEN, json.dumps(link.requests))


@unittest.skipIf(os.name == "nt", "the state folder rules checked here are POSIX modes")
class ObserverWithRunningPeerLink(unittest.TestCase):
    """Review finding A2 against the real peer link, not a fake listener. A running peer link checks its folder and
    token on every control request, the observer's hello included (load_control_token -> _restrict_dir sets the
    folder to 0700, strips its ACL and rotates a bad token). The observer must not reach it while that check would
    change anything; when it does reach it, the folder's mode, access list and files stay as they are."""

    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        self.dir = base / "peer-link"
        self.link = peer_link.PeerLink(self.dir, "mac", "m", lambda: presence(platform="mac"), status_port=0,
                                       beacon_port=0, bind="127.0.0.1", broadcast=(), allow_loopback=True).start()
        self.addCleanup(self.link.stop)
        self.addCleanup(self.reset)  # cleanups run last-in first-out: the folder is writable again before rmtree
        self.token = (self.dir / "control-token").read_bytes()
        # Every check the running peer link makes, counted from here on; the observer never calls it itself.
        patcher = mock.patch.object(peer_link, "load_control_token", wraps=peer_link.load_control_token)
        self.checks = patcher.start()
        self.addCleanup(patcher.stop)
        self.http_calls = []
        real_http = peer_link._http

        def recording_http(*args, **kwargs):
            self.http_calls.append(args[3])
            return real_http(*args, **kwargs)

        patcher = mock.patch.object(peer_link, "_http", side_effect=recording_http)
        patcher.start()
        self.addCleanup(patcher.stop)

    def reset(self):
        os.chmod(self.dir, 0o700)
        if sys.platform == "darwin":
            subprocess.run([ACL_TOOL, "-N", str(self.dir)], capture_output=True, timeout=10)
        token = self.dir / "control-token"
        token.write_bytes(self.token)
        os.chmod(token, 0o600)

    def read(self):
        return peer_feed.from_peer_link(time.time(), self.dir, self.link.status_port)

    def test_state_the_peer_link_would_repair_is_never_contacted(self):
        token = self.dir / "control-token"
        cases = [(oct(mode), lambda mode=mode: os.chmod(self.dir, mode), "owner-only")
                 for mode in (0o750, 0o740, 0o710, 0o500, 0o1700)]
        cases += [("0757", lambda: os.chmod(self.dir, 0o757), "writable by other users"),
                  ("loose token", lambda: os.chmod(token, 0o640), "another user"),
                  ("malformed token", lambda: token.write_bytes(b"not-a-token\n"), "malformed")]
        if sys.platform == "darwin":
            me = pwd.getpwuid(os.getuid()).pw_name

            def with_acl():
                if subprocess.run([ACL_TOOL, "+a", f"user:{me} allow list", str(self.dir)],
                                  capture_output=True, timeout=10).returncode != 0:
                    self.skipTest("this volume does not take an extended ACL")
            cases.append(("folder ACL", with_acl, "access list"))
        for name, arrange, reason in cases:
            with self.subTest(name):
                self.reset()
                arrange()
                before = tree(self.dir)
                self.checks.reset_mock()
                self.http_calls.clear()
                with self.assertRaises(peer_feed.LinkUnavailable) as caught:
                    self.read()
                self.assertIn(reason, str(caught.exception))
                self.assertEqual(tree(self.dir), before, "the state folder changed")
                self.assertEqual((self.checks.call_count, self.http_calls), (0, []), "the peer link was contacted")

    def test_contact_leaves_mode_access_list_and_files_as_they_are(self):
        def without_change_times(snapshot):
            return {name: row[:3] + row[4:] for name, row in snapshot.items()}

        before = tree(self.dir)
        self.assertIsNone(self.read())  # nothing paired: the full handshake ran and the listing was empty
        self.assertEqual(self.http_calls, ["/control/hello", "/control/peers"])
        self.assertGreaterEqual(self.checks.call_count, 1, "the real peer link's own check did not run")
        after = tree(self.dir)
        # The peer link's re-check may re-stamp the folder's change time (chmod 0700 on a folder already 0700);
        # mode, owner, inodes, modification times, contents and access lists stay exactly as they were.
        self.assertEqual(without_change_times(after), without_change_times(before))
        self.assertEqual(stat.S_IMODE(os.lstat(self.dir).st_mode), 0o700)
        # Once the peer link has re-checked the folder, a further read changes nothing at all.
        self.assertIsNone(self.read())
        self.assertEqual(tree(self.dir), after)


if __name__ == "__main__":
    unittest.main()
