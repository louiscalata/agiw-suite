"""Security boundaries exercised through in-memory transports and trust fixtures."""

import hashlib
import hmac
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import peer_link as pl


class StatusDeadlineSecurity(unittest.TestCase):
    def test_blocked_provider_releases_request_slots_and_recovers(self):
        link = pl.PeerLink.__new__(pl.PeerLink)
        link.node, link.host = "local", "mac"
        link._lock, link._nonces = threading.RLock(), {}
        link.allow_loopback_lan = False
        key = b"k" * 32
        link.trusted = lambda: {"paired": {"key": key}}
        entered, release = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            release.wait()
            return {"schemaVersion": 1, "host": "mac", "observedAt": pl._iso(time.time()), "models": []}

        link.status_provider = mock.Mock(side_effect=blocked)
        # Exercise real admission/release and request-thread cleanup without opening a socket.
        with mock.patch.object(pl.ThreadingHTTPServer, "__init__", return_value=None):
            server = pl.LinkServer(("unused", 0), link)
        server.shutdown_request = mock.Mock()
        server.handle_error = mock.Mock()
        address = ("192.168.1.2", 12345)

        def handler():
            result = pl.LinkHandler.__new__(pl.LinkHandler)
            result.server = server
            ts, nonce = str(time.time()), pl.secrets.token_hex(16)
            signed = "\n".join(("GET", pl.PREFIX + "/status", "paired", link.node, ts, nonce)).encode()
            result.headers = {
                "X-Agiw-Node": "paired",
                "X-Agiw-Ts": ts,
                "X-Agiw-Nonce": nonce,
                "X-Agiw-Sig": hmac.new(pl.Subkeys(key).request, signed, hashlib.sha256).hexdigest(),
            }
            result._send = mock.Mock()
            return result

        first = handler()
        server.finish_request = lambda *_: first._status()
        request = mock.Mock()
        worker = threading.Thread(target=server.process_request_thread, args=(request, address), daemon=True)
        with mock.patch.object(pl, "SELF_STATUS_DEADLINE", 0.02), mock.patch.object(pl, "REQUEST_DEADLINE", 0.05):
            try:
                self.assertTrue(server._admit(address))
                worker.start()
                self.assertTrue(entered.wait(1))
                worker.join(0.5)
                self.assertFalse(worker.is_alive(), "socket watchdog left the provider holding a LAN slot")
                self.assertEqual(first._send.call_args.args, (503,))
                self.assertNotIn(address[0], server.per_source)
                for _ in range(pl.MAX_CONNECTIONS + 1):
                    later = handler()
                    server.finish_request = lambda *_, current=later: current._status()
                    self.assertTrue(server._admit(address))
                    server.process_request_thread(mock.Mock(), address)
                    self.assertEqual(later._send.call_args.args, (503,))
                link.status_provider.assert_called_once_with()
                self.assertEqual(server.per_source, {})
            finally:
                release.set()
                worker.join(1)
                provider = getattr(link, "_self_status_worker", None)
                if provider is not None:
                    provider.join(1)
        recovered = handler()
        recovered._status()
        self.assertEqual(recovered._send.call_args.args[0], 200)
        body = recovered._send.call_args.args[1]
        self.assertEqual(pl.strict_json(body)["models"], [])
        self.assertEqual(link.status_provider.call_count, 2)


class StatusInputsSecurity(unittest.TestCase):
    def lmstudio(self, payload):
        with mock.patch.object(pl.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            return pl.lmstudio_status()()

    def test_lmstudio_rejects_missing_inventory(self):
        with self.assertRaises(ValueError):
            self.lmstudio({})

    def test_lmstudio_rejects_error_responses(self):
        for payload in ({"error": "backend unavailable"}, {"error": "backend unavailable", "data": []}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.lmstudio(payload)

    def test_lmstudio_rejects_non_list_inventory(self):
        for inventory in (None, False, "", "bad", {}, 0):
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                self.lmstudio({"data": inventory})

    def test_lmstudio_rejects_invalid_inventory_entries(self):
        for entry in (None, "model", {}, {"id": 42}, {"id": ""}):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.lmstudio({"data": [entry]})

    def test_lmstudio_rejects_blank_or_control_character_model_ids(self):
        for model_id in ("   ", "\t", "\n", "model\n", "model\x00", "model\x1b", "\u200b"):
            with self.subTest(model_id=repr(model_id)):
                with self.assertRaises(ValueError):
                    self.lmstudio({"data": [{"id": model_id}]})

    def test_lmstudio_preserves_valid_empty_inventory_and_model_states(self):
        empty = self.lmstudio({"data": []})
        self.assertEqual(empty["health"], "ok")
        self.assertEqual(empty["models"], [])
        doc = self.lmstudio(
            {
                "data": [
                    {"id": "loaded-model", "type": "llm", "state": "loaded"},
                    {"id": "inactive-model", "type": "vlm", "state": "not-loaded"},
                    {"id": "unknown-model", "state": "other"},
                    {"id": "embedding-model", "type": "embeddings", "state": "loaded"},
                ]
            }
        )
        self.assertEqual(
            doc["models"],
            [
                {"id": "loaded-model", "host": "mac", "loadedState": "loaded"},
                {"id": "inactive-model", "host": "mac", "loadedState": "inactive"},
                {"id": "unknown-model", "host": "mac", "loadedState": "unknown"},
            ],
        )

    def write_status(self, raw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "status.json"
        path.write_bytes(raw)
        return path

    def status_bytes(self):
        return json.dumps(
            {"schemaVersion": 1, "host": "windows", "observedAt": pl._iso(time.time()), "health": "ok", "models": []}
        ).encode()

    def test_lmstudio_rejects_duplicate_inventory_keys(self):
        raw = b'{"data": [{"id": "old"}], "data": []}'
        with mock.patch.object(pl.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
            with self.assertRaises(ValueError):
                pl.lmstudio_status()()

    def test_lmstudio_rejects_oversized_reply_with_valid_prefix(self):
        raw = b'{"data": []}'.ljust(pl.MAX_STATUS_BYTES, b" ") + b"INVALID-TAIL"
        with mock.patch.object(pl.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
            with self.assertRaises(ValueError):
                pl.lmstudio_status()()

    def test_lmstudio_rejects_oversized_json_whitespace(self):
        raw = b'{"data": []}'.ljust(pl.MAX_STATUS_BYTES + 1, b" ")
        with mock.patch.object(pl.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
            with self.assertRaisesRegex(ValueError, "too large"):
                pl.lmstudio_status()()

    def test_file_status_rejects_oversized_json_whitespace(self):
        path = self.write_status(self.status_bytes().ljust(pl.MAX_STATUS_BYTES + 1, b" "))
        with self.assertRaisesRegex(ValueError, "too large"):
            pl.file_status(path)()

    def test_lmstudio_checks_entries_beyond_published_slice(self):
        entries = [{"id": "valid"}] * pl.MAX_MODELS + [None]
        with self.assertRaises(ValueError):
            self.lmstudio({"data": entries})

    def test_file_status_rejects_duplicate_health_keys(self):
        raw = self.status_bytes().replace(b'"health": "ok"', b'"health": "down", "health": "ok"')
        path = self.write_status(raw)
        with self.assertRaises(ValueError):
            pl.validate_status(pl.file_status(path)(), "windows", time.time())

    def test_file_status_rejects_oversized_file_with_valid_prefix(self):
        raw = self.status_bytes().ljust(pl.MAX_STATUS_BYTES, b" ") + b"INVALID-TAIL"
        path = self.write_status(raw)
        with self.assertRaises(ValueError):
            pl.validate_status(pl.file_status(path)(), "windows", time.time())

    def test_file_status_accepts_exact_byte_limit(self):
        path = self.write_status(self.status_bytes().ljust(pl.MAX_STATUS_BYTES, b" "))
        self.assertEqual(pl.validate_status(pl.file_status(path)(), "windows", time.time())["health"], "ok")


LOCAL = "11111111-1111-4111-8111-111111111111"
REMOTE = "22222222-2222-4222-8222-222222222222"


class MemoryTrust:
    def __init__(self):
        self.pins = {}

    def load(self):
        return dict(self.pins)

    def pin(self, node, name, host, key):
        self.pins[node] = {"nodeUuid": node, "name": name, "host": host, "key": key}

    def remove(self, node):
        self.pins.pop(node)


class PairingRevocationSecurity(unittest.TestCase):
    def setUp(self):
        self.store = MemoryTrust()
        with (
            mock.patch.object(pl, "load_identity", return_value=LOCAL),
            mock.patch.object(pl, "load_control_token", return_value="a" * 64),
            mock.patch.object(pl, "TrustStore", return_value=self.store),
        ):
            self.link = pl.PeerLink(
                Path(__file__).parent / "unused-test-state", "mac", "mac", lambda: {}, clock=lambda: 100.0
            )

    def completed_pairing(self):
        invite = "b" * 32
        keys = {"joinerConfirm": "c" * 64, "finishAck": "d" * 64, "link": b"e" * 32}
        self.link._outbound[invite] = {
            "id": invite,
            "expires": 200.0,
            "attempt": "f" * 32,
            "spake": keys,
            "peer": {"node": REMOTE, "name": "pc", "host": "windows"},
        }
        body = {"invite": invite, "attempt": "f" * 32, "confirm": keys["joinerConfirm"]}
        expected = (200, {"paired": True, "ack": keys["finishAck"]})
        self.assertEqual(self.link.on_finish(body), expected)
        self.assertEqual(self.link.on_finish(body), expected, "a still-trusted retry remains idempotent")
        return body

    def test_finish_retry_after_unpair_is_refused(self):
        body = self.completed_pairing()
        self.assertEqual(self.link.unpair(REMOTE), {"unpaired": REMOTE})
        self.assertEqual(self.link.trusted(), {})
        code, reply = self.link.on_finish(body)
        self.assertNotEqual(code, 200)
        self.assertFalse(reply.get("paired", False))
        self.assertEqual(self.link.trusted(), {}, "a revoked retry must not repin the peer")

    def test_old_finish_retry_after_repair_is_refused(self):
        body = self.completed_pairing()
        self.link.unpair(REMOTE)
        self.store.pin(REMOTE, "pc", "windows", b"n" * 32)
        self.link._invalidate()
        code, reply = self.link.on_finish(body)
        self.assertNotEqual(code, 200)
        self.assertFalse(reply.get("paired", False))
        self.assertEqual(self.link.trusted()[REMOTE]["key"], b"n" * 32)

    def test_finish_retry_requires_the_original_link_key(self):
        body = self.completed_pairing()
        self.store.pin(REMOTE, "pc", "windows", b"n" * 32)
        self.link._invalidate()
        code, reply = self.link.on_finish(body)
        self.assertNotEqual(code, 200)
        self.assertFalse(reply.get("paired", False))
        self.assertEqual(self.link.trusted()[REMOTE]["key"], b"n" * 32)

    def test_finish_retry_revoked_while_waiting_is_refused(self):
        body = self.completed_pairing()
        done = self.link._completed[body["invite"]]
        with mock.patch.object(done["saved"], "wait", side_effect=lambda _: self.link.unpair(REMOTE)):
            code, reply = self.link.on_finish(body)
        self.assertNotEqual(code, 200)
        self.assertFalse(reply.get("paired", False))
        self.assertEqual(self.link.trusted(), {})

    def test_unpair_between_finish_transition_and_pin_cannot_restore_trust(self):
        self.store.pin(REMOTE, "pc", "windows", b"e" * 32)
        invite = "b" * 32
        keys = {"joinerConfirm": "c" * 64, "finishAck": "d" * 64, "link": b"e" * 32}
        self.link._outbound[invite] = {
            "id": invite,
            "expires": 200.0,
            "attempt": "f" * 32,
            "spake": keys,
            "peer": {"node": REMOTE, "name": "pc", "host": "windows"},
        }
        body = {"invite": invite, "attempt": "f" * 32, "confirm": keys["joinerConfirm"]}
        original_lock, link = self.link._lock, self.link

        class UnpairBeforePin:
            entries = 0

            def __enter__(self):
                self.entries += 1
                if self.entries == 2:
                    link.unpair(REMOTE)
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        with mock.patch.object(self.link, "_lock", UnpairBeforePin()):
            code, reply = self.link.on_finish(body)
        self.assertNotEqual(code, 200)
        self.assertFalse(reply.get("paired", False))
        self.assertEqual(self.link.trusted(), {}, "the pending finish restored revoked trust")

    def test_first_finish_revoked_while_notifying_waiters_is_refused(self):
        invite = "b" * 32
        keys = {"joinerConfirm": "c" * 64, "finishAck": "d" * 64, "link": b"e" * 32}
        self.link._outbound[invite] = {
            "id": invite,
            "expires": 200.0,
            "attempt": "f" * 32,
            "spake": keys,
            "peer": {"node": REMOTE, "name": "pc", "host": "windows"},
        }
        body = {"invite": invite, "attempt": "f" * 32, "confirm": keys["joinerConfirm"]}
        saved = pl.threading.Event()
        real_set = saved.set

        def revoke_before_return():
            real_set()
            self.link.unpair(REMOTE)

        with mock.patch.object(pl.threading, "Event", return_value=saved):
            with mock.patch.object(saved, "set", side_effect=revoke_before_return):
                code, reply = self.link.on_finish(body)
        self.assertNotEqual(code, 200)
        self.assertFalse(reply.get("paired", False))
        self.assertEqual(self.link.trusted(), {})

    def test_finish_publishes_trust_before_waking_retry(self):
        self.assertEqual(self.link.trusted(), {})  # Cache the pre-pairing state.
        invite = "b" * 32
        keys = {"joinerConfirm": "c" * 64, "finishAck": "d" * 64, "link": b"e" * 32}
        self.link._outbound[invite] = {
            "id": invite,
            "expires": 200.0,
            "attempt": "f" * 32,
            "spake": keys,
            "peer": {"node": REMOTE, "name": "pc", "host": "windows"},
        }
        body = {"invite": invite, "attempt": "f" * 32, "confirm": keys["joinerConfirm"]}
        saved = pl.threading.Event()
        real_set, replies = saved.set, []

        def wake_retry():
            real_set()
            replies.append(self.link.on_finish(body))

        with (
            mock.patch.object(pl.threading, "Event", return_value=saved),
            mock.patch.object(saved, "set", side_effect=wake_retry),
        ):
            initial = self.link.on_finish(body)
        expected = (200, {"paired": True, "ack": keys["finishAck"]})
        self.assertEqual(initial, expected)
        self.assertEqual(replies, [expected])

    def test_hello_rejects_uuid_with_trailing_newline(self):
        valid = pl.make_hello(REMOTE, "windows", "pc", 8768, 100.0)
        self.assertEqual(pl.read_hello(valid)["node"], REMOTE)
        malformed = pl.make_hello(REMOTE + "\n", "windows", "pc", 8768, 100.0)
        self.assertIsNone(pl.read_hello(malformed))
        self.link.handle_datagram(malformed, "192.168.1.2")
        self.assertEqual(self.link._seen, {})

    def test_invite_rejects_uuid_with_trailing_newline(self):
        malformed = REMOTE + "\n"
        self.link._seen[malformed] = {"address": "192.168.1.2", "lastHello": 100.0}
        code, reply = self.link.on_notify(
            {"node": malformed, "host": "windows", "name": "pc", "invite": "b" * 32, "port": 8768}, "192.168.1.2"
        )
        self.assertEqual(code, 400)
        self.assertEqual(self.link._inbound, {})


TOKEN = "a" * 64
CLIENT_NONCE = "b" * 32
SERVER_NONCE = "c" * 32


def response(document, version="HTTP/1.1", connection="keep-alive"):
    body = pl.canonical(document)
    return (f"{version} 200 OK\r\nContent-Length: {len(body)}\r\nConnection: {connection}\r\n\r\n").encode() + body


class MemorySocket:
    """Each makefile is one response; every outbound byte remains observable."""

    def __init__(self, responses, die_after_first=False):
        self.responses = iter(responses)
        self.sent = []
        self.closed = False
        self.die_after_first = die_after_first
        self.reads = 0

    def setsockopt(self, *_):
        pass

    def settimeout(self, *_):
        pass

    def sendall(self, data):
        if self.closed:
            raise BrokenPipeError("authenticated daemon stopped")
        self.sent.append(bytes(data))

    def makefile(self, *_):
        self.reads += 1
        owner = self

        class ResponseFile(io.BytesIO):
            def close(self):
                if owner.die_after_first and owner.reads == 1:
                    owner.closed = True
                super().close()

        return ResponseFile(next(self.responses))

    def shutdown(self, *_):
        self.closed = True

    def close(self):
        self.closed = True


class ControlConnectionSecurity(unittest.TestCase):
    def hello(self, **kwargs):
        return response(
            {"ns": SERVER_NONCE, "proof": pl.control_server_proof(TOKEN, CLIENT_NONCE, SERVER_NONCE)}, **kwargs
        )

    def request(self, genuine, replacement, method="POST", path="/control/accept", body=None):
        with (
            mock.patch.object(pl, "load_control_token", return_value=TOKEN),
            mock.patch.object(pl.secrets, "token_hex", return_value=CLIENT_NONCE),
            mock.patch.object(pl.socket, "create_connection", side_effect=[genuine, replacement]) as connect,
        ):
            try:
                result = pl.control_request(Path("unused-control-fixture"), 8768, method, path, body)
            except (OSError, pl.http.client.HTTPException) as exc:
                result = exc
            return result, connect.call_count

    def test_closed_hello_never_sends_pin_to_replacement_listener(self):
        for version, connection in (("HTTP/1.0", "close"), ("HTTP/1.1", "close")):
            with self.subTest(version=version):
                genuine = MemorySocket([self.hello(version=version, connection=connection)])
                replacement = MemorySocket([response({"paired": True}, connection="close")])
                result, connections = self.request(genuine, replacement, body={"peer": "pc", "pin": "246810"})
                self.assertNotIn(b"246810", b"".join(replacement.sent), "PIN reached an unauthenticated listener")
                self.assertIsInstance(result, Exception, "forged success from replacement listener was accepted")
                self.assertEqual(connections, 1, "control must never reconnect after proving the daemon")

    def test_daemon_exit_after_hello_rejects_forged_status_without_reconnect(self):
        genuine = MemorySocket([self.hello()], die_after_first=True)
        replacement = MemorySocket([response({"peers": [{"state": "connected"}]}, connection="close")])
        result, connections = self.request(genuine, replacement, method="GET", path="/control/status")
        self.assertIsInstance(result, Exception, "a replacement listener supplied an accepted status")
        self.assertEqual(connections, 1)
        self.assertEqual(replacement.sent, [])

    def test_valid_control_exchange_uses_one_authenticated_connection(self):
        document = {"peer": "pc"}
        genuine = MemorySocket([self.hello(), response(document, connection="close")])
        replacement = MemorySocket([response(document, connection="close")])
        result, connections = self.request(genuine, replacement, body={"peer": "pc", "pin": "246810"})
        self.assertEqual(result, (200, document))
        self.assertEqual(connections, 1, "the request and reply must use the authenticated hello connection")
        sent = b"".join(genuine.sent)
        self.assertIn(b"GET /control/hello", sent)
        self.assertIn(b"POST /control/accept", sent)
        self.assertIn(b"246810", sent)
        self.assertNotIn(TOKEN.encode(), sent)
        self.assertEqual(replacement.sent, [])

    def test_daemon_answers_authenticated_operation_on_hello_connection(self):
        path = "/control/peers"
        proof = pl.control_client_proof(TOKEN, SERVER_NONCE, CLIENT_NONCE, "GET", path, b"")
        incoming = (
            f"GET /control/hello HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n"
            f"X-Agiw-Client-Nonce: {CLIENT_NONCE}\r\n\r\n"
            f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
            f"X-Agiw-Control-Nonce: {SERVER_NONCE}\r\nX-Agiw-Control-Proof: {proof}\r\n\r\n"
        ).encode()
        connection = MemorySocket([incoming])
        snapshot = mock.Mock(return_value={"peers": []})
        link = SimpleNamespace(
            current_control_token=lambda: TOKEN,
            new_control_challenge=lambda nc, token: SERVER_NONCE,
            take_control_challenge=lambda ns: (CLIENT_NONCE, pl.control_token_id(TOKEN)),
            snapshot=snapshot,
        )
        pl.LinkHandler(connection, ("127.0.0.1", 43210), SimpleNamespace(link=link))
        snapshot.assert_called_once_with()
        self.assertIn(b'{"peers":[]}', b"".join(connection.sent))


class SilentCatchFailClosed(unittest.TestCase):
    def _link(self):
        link = pl.PeerLink.__new__(pl.PeerLink)
        link.state_dir = Path("unused")
        return link

    def test_unreadable_control_token_yields_none_and_logs_type_only(self):
        for exc in (PermissionError("secret-detail"), ValueError("secret-detail")):
            with mock.patch.object(pl, "load_control_token", side_effect=exc):
                with self.assertLogs(pl._LOG, level="DEBUG") as logs:
                    self.assertIsNone(self._link().current_control_token())
            self.assertIn(type(exc).__name__, logs.output[0])
            self.assertNotIn("secret-detail", logs.output[0])

    def test_unexpected_token_error_is_not_swallowed(self):
        with mock.patch.object(pl, "load_control_token", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                self._link().current_control_token()
