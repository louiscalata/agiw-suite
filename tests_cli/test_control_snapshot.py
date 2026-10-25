"""Control-only local status and packaging checks, without sockets or services."""
import ast
import copy
import hashlib
import hmac
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import ROOT, pl


NOW = 1700000000.0


class ControlSnapshot(unittest.TestCase):
    def setUp(self):
        # Exercise real snapshot/handler methods while avoiding state files and listeners.
        self.document = {
            "schemaVersion": 1, "kind": "agiw-peer", "host": "local-machine", "platform": "linux",
            "observedAt": pl._iso(NOW - 1), "health": "up", "producer": "agiw-linux-observer",
            "models": [{"id": "model", "loadedState": "loaded", "activity": "idle", "secret": "hidden"}],
            "loadedListComplete": True, "modelsTotal": 1, "private": "hidden",
            "selfStatus": {"private": "never sent"},
        }
        link = self.link = pl.PeerLink.__new__(pl.PeerLink)
        link.node, link.name, link.host = "local-node", "Local machine", "linux"
        link.clock = lambda: 7.0  # A monotonic clock must never be used to validate observedAt.
        link.status_provider = mock.Mock(side_effect=lambda: copy.deepcopy(self.document))
        link.trusted = lambda: {}
        link._lock = threading.RLock()
        link._seen, link._live, link._outbound, link._inbound = {}, {}, {}, {}
        link._control_challenges, link._nonces = {}, {}
        link.store = SimpleNamespace(quarantined=[])
        link.poll_error = None
        link.allow_loopback = False
        self.wall_clock = mock.patch.object(pl.time, "time", return_value=NOW)
        self.wall_clock.start()
        self.addCleanup(self.wall_clock.stop)

    def handler(self, path, address="127.0.0.1", headers=None):
        handler = pl.LinkHandler.__new__(pl.LinkHandler)
        handler.path, handler.command = path, "GET"
        handler.client_address = (address, 54321)
        handler.server = SimpleNamespace(link=self.link)
        handler.headers = headers or {}
        handler._send, handler._json = mock.Mock(), mock.Mock()
        return handler

    def control_handler(self):
        token, nc, ns = "a" * 64, "b" * 32, pl.secrets.token_hex(16)
        self.link.current_control_token = lambda: token
        self.link._control_challenges[ns] = (nc, float("inf"), pl.control_token_id(token))
        path = "/control/peers"
        return self.handler(path, headers={
            "X-Agiw-Control-Nonce": ns,
            "X-Agiw-Control-Proof": pl.control_client_proof(token, ns, nc, "GET", path, b""),
        })

    def test_snapshot_contains_projected_validated_self_status(self):
        expected = pl.project_status(pl.validate_status(self.document, "linux", NOW))
        snapshot = self.link.snapshot(include_self_status=True)
        self.assertEqual(snapshot["selfStatus"], expected)
        self.assertEqual(snapshot["self"], self.link.me())
        self.assertNotIn("private", snapshot["selfStatus"])
        self.assertNotIn("selfStatus", snapshot["selfStatus"])
        self.assertNotIn("secret", snapshot["selfStatus"]["models"][0])

    def test_provider_or_validation_failure_is_a_short_safe_error(self):
        for failure in (RuntimeError("secret path and token"), None):
            with self.subTest(failure=type(failure).__name__):
                if failure is not None:
                    self.link.status_provider.side_effect = failure
                else:
                    self.link.status_provider.side_effect = None
                    self.link.status_provider.return_value = {"schemaVersion": 1}
                answer = self.link.snapshot(include_self_status=True)["selfStatus"]
                self.assertEqual(answer, {"error": "local status unavailable or invalid"})
                self.assertEqual(set(answer), {"error"})
                self.assertTrue(0 < len(answer["error"]) <= 200)
                self.assertNotIn("secret", answer["error"])
                self.assertNotIn("token", answer["error"])

    def test_stale_provider_document_is_an_error(self):
        self.document["observedAt"] = pl._iso(NOW - pl.STATUS_MAX_AGE - 1)
        self.assertEqual(set(self.link.snapshot(include_self_status=True)["selfStatus"]), {"error"})

    def test_authenticated_control_answer_contains_self_status(self):
        handler = self.control_handler()
        handler.do_GET()
        self.assertEqual(handler._json.call_args[0][0], 200)
        self.assertEqual(handler._json.call_args[0][1]["selfStatus"],
                         pl.project_status(pl.validate_status(self.document, "linux", NOW)))

    def test_default_snapshot_never_reads_provider(self):
        self.assertNotIn("selfStatus", self.link.snapshot())
        self.link.status_provider.assert_not_called()

    def test_control_provider_failure_returns_safe_error(self):
        self.link.status_provider.side_effect = RuntimeError("private path and token")
        handler = self.control_handler()
        handler.do_GET()
        self.assertEqual(handler._json.call_args[0][0], 200)
        self.assertEqual(handler._json.call_args[0][1]["selfStatus"],
                         {"error": "local status unavailable or invalid"})

    def test_poll_once_publishes_peers_json_without_self_status(self):
        release = threading.Event()
        self.link.status_provider.side_effect = lambda: release.wait()
        with tempfile.TemporaryDirectory(prefix="agiw-m2-poll-") as folder:
            path = Path(folder) / "peers.json"
            published = []
            write = pl.write_snapshot(path)

            def on_change(snapshot):
                published.append(snapshot)
                write(snapshot)

            self.link.on_change = on_change
            poller = threading.Thread(target=self.link.poll_once, daemon=True)
            try:
                poller.start()
                poller.join(0.5)
                self.assertFalse(poller.is_alive(), "poll_once stalled on the status provider")
                self.link.status_provider.assert_not_called()
                self.assertEqual(len(published), 1)
                self.assertNotIn("selfStatus", published[0])
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), published[0])
            finally:
                release.set()
                poller.join(1)

    def test_control_timeout_is_bounded_single_flight_and_recovers(self):
        entered, release = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            release.wait()
            return copy.deepcopy(self.document)

        self.link.status_provider.side_effect = blocked
        handler = self.control_handler()
        request = threading.Thread(target=handler.do_GET, daemon=True)
        published = []
        self.link.on_change = published.append
        poller = threading.Thread(target=self.link.poll_once, daemon=True)
        timeout = {"error": "local status timed out"}
        with mock.patch.object(pl, "SELF_STATUS_DEADLINE", 0.05):
            try:
                started = time.monotonic()
                request.start()
                self.assertTrue(entered.wait(0.5), "provider never started")
                # A concurrent request must not launch a second blocked provider read.
                second = self.control_handler()
                second.do_GET()
                self.assertEqual(second._json.call_args[0][1]["selfStatus"], timeout)
                request.join(0.5)
                self.assertFalse(request.is_alive(), "control answer exceeded the bounded wait")
                self.assertLess(time.monotonic() - started, 0.75)
                self.assertEqual(handler._json.call_args[0][0], 200)
                self.assertEqual(handler._json.call_args[0][1]["selfStatus"], timeout)
                self.assertTrue(self.link._self_status_worker.daemon)
                # Polling and repeated requests must stay usable after the timeout, too.
                poller.start()
                poller.join(0.5)
                self.assertFalse(poller.is_alive(), "an in-flight provider blocked polling")
                self.assertEqual(len(published), 1)
                self.assertNotIn("selfStatus", published[0])
                third = self.control_handler()
                third.do_GET()
                self.assertEqual(third._json.call_args[0][1]["selfStatus"], timeout)
                self.link.status_provider.assert_called_once_with()
            finally:
                release.set()
                request.join(1)
                if poller.ident is not None:
                    poller.join(1)
                worker = getattr(self.link, "_self_status_worker", None)
                if worker is not None:
                    worker.join(1)
        recovered = self.control_handler()
        recovered.do_GET()
        self.assertEqual(recovered._json.call_args[0][1]["selfStatus"],
                         pl.project_status(pl.validate_status(self.document, "linux", NOW)))
        self.assertEqual(self.link.status_provider.call_count, 2)

    def test_unauthenticated_control_never_reads_provider(self):
        handler = self.handler("/control/peers")
        handler.do_GET()
        handler._send.assert_called_once_with(404)
        handler._json.assert_not_called()
        self.link.status_provider.assert_not_called()

    def test_lan_status_body_and_signature_remain_the_projected_document(self):
        peer, key, nonce, ts = "peer-node", b"k" * 32, "d" * 32, str(NOW)
        self.link.trusted = lambda: {peer: {"key": key}}
        path = pl.PREFIX + "/status"
        signed = "\n".join(("GET", path, peer, self.link.node, ts, nonce)).encode()
        keys = pl.Subkeys(key)
        handler = self.handler(path, address="203.0.113.2", headers={
            "X-Agiw-Node": peer, "X-Agiw-Ts": ts, "X-Agiw-Nonce": nonce,
            "X-Agiw-Sig": hmac.new(keys.request, signed, hashlib.sha256).hexdigest(),
        })
        expected = pl.canonical(pl.project_status(pl.validate_status(self.document, "linux", NOW)))
        # Obtain the new control document first, then exercise the existing LAN handler.
        self.assertIn("selfStatus", self.link.snapshot(include_self_status=True))
        handler.do_GET()
        signature = hmac.new(keys.reply, pl._reply_header(nonce, self.link.node, peer) + expected,
                             hashlib.sha256).hexdigest()
        handler._send.assert_called_once_with(200, expected,
                                              {"X-Agiw-Node": self.link.node, "X-Agiw-Sig": signature})
        self.assertNotIn("selfStatus", json.loads(handler._send.call_args[0][1]))
        handler._json.assert_not_called()


def configured_modules():
    """The literal array is also readable on Python 3.9, without tomllib."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[tool.setuptools]", 1)[1]
    match = re.search(r"^py-modules\s*=\s*(\[[^\]]*\])", section, re.MULTILINE)
    if match is None:
        raise AssertionError("py-modules setting missing")
    return ast.literal_eval(match.group(1))


class Packaging(unittest.TestCase):
    def test_pyproject_includes_machine_state(self):
        self.assertIn("machine_state", configured_modules())

    def test_built_module_list_and_artifact_include_machine_state(self):
        try:
            from setuptools import Distribution
            from setuptools.command.build_py import build_py
        except ImportError:
            self.skipTest("setuptools is not installed in this interpreter; no network install attempted")
        modules = configured_modules()
        distribution = Distribution({"py_modules": modules})
        command = build_py(distribution)
        command.finalize_options()
        # build_py resolves these top-level source names relative to the project root.
        with tempfile.TemporaryDirectory(prefix="agiw-m2-build-") as folder:
            with mock.patch.object(command, "get_package_dir", return_value=str(ROOT)):
                command.build_lib = folder
                command.compile = False
                self.assertIn("machine_state", [row[1] for row in command.find_modules()])
                command.run()
            built = Path(folder) / "machine_state.py"
            self.assertIn(str(built), command.get_outputs())
            self.assertEqual(built.read_bytes(), (ROOT / "machine_state.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
