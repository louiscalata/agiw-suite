import contextlib
import hmac
import json
import os
import secrets
import socket
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import peer_link as pl


def status(host, state="loaded"):
    return {"schemaVersion": 1, "host": host, "observedAt": pl._iso(time.time()),
            "models": [{"id": "gpt-oss-20b", "host": host, "loadedState": state}]}


ME = "S-1-5-21-1111111111-2222222222-3333333333-1001"
OTHER = "S-1-5-21-1111111111-2222222222-3333333333-1002"
EVERYONE = "S-1-1-0"
SYSTEM32 = "C:\\Windows\\System32"
ICACLS = "C:\\Windows\\System32\\icacls.exe"
PEER = "88888888-8888-4888-8888-888888888888"
PEER_RECORD_PLAIN = {"nodeUuid": PEER, "name": "pc", "host": "windows", "linkKey": "ab" * 32, "pinnedAt": 1}
PEER_RECORD = json.dumps(PEER_RECORD_PLAIN)  # replaced by a sealed record in WindowsSecurity, below


ACCOUNT_KEY = b"this windows account's dpapi master key"
FAKE_REGISTRY: dict = {}  # stands in for HKCU\\Software\\AGIW\\PeerLink\\TrustGenerations


def fake_protect(data, entropy, key=ACCOUNT_KEY):
    """Stands in for CryptProtectData: only the same account (key) and purpose (entropy) can open it."""
    return b"BLOB" + pl.hmac.new(key, entropy + b"\0" + data, pl.hashlib.sha256).digest()[:16] + data


def fake_unprotect(blob, entropy, key=ACCOUNT_KEY):
    data = blob[20:]
    if blob[:4] != b"BLOB" or not pl.hmac.compare_digest(
            blob[4:20], pl.hmac.new(key, entropy + b"\0" + data, pl.hashlib.sha256).digest()[:16]):
        raise pl.LooseSecret("not sealed by this account")
    return data


def sealed(value, purpose, key=ACCOUNT_KEY):
    return pl.SEAL_PREFIX + pl.base64.b64encode(fake_protect(value.encode(), purpose.encode(), key)).decode()


def unsealed(text, purpose="agiw-peer control-token"):
    assert text.startswith(pl.SEAL_PREFIX), "Windows secrets are stored sealed"
    return fake_unprotect(pl.base64.b64decode(text[len(pl.SEAL_PREFIX):]), purpose.encode()).decode()


@contextlib.contextmanager
def as_windows(security, me=ME):
    """Run the Windows branches on any OS. `security` stands in for _win_owner_and_allow_sids (the only
    ctypes call that reads a security descriptor); icacls is mocked and returned for inspection. Each
    entry starts with nothing tightened, like a fresh process. Build every Path before entering: pathlib
    refuses to construct one while os.name says 'nt'."""
    with mock.patch.object(pl.os, "name", "nt"), mock.patch.object(pl.sys, "platform", "win32"), \
            mock.patch.object(pl, "_win_current_sid", return_value=me), \
            mock.patch.object(pl, "_win_owner_and_allow_sids", security), \
            mock.patch.object(pl, "_win_system_dir", return_value=SYSTEM32), \
            mock.patch.object(pl, "_win_tightened", set()), \
            mock.patch.object(pl, "_win_protect", fake_protect), \
            mock.patch.object(pl, "_gen_get", FAKE_REGISTRY.get), \
            mock.patch.object(pl, "_gen_set", FAKE_REGISTRY.__setitem__), \
            mock.patch.object(pl, "_gen_delete", lambda node: FAKE_REGISTRY.pop(node, None)), \
            mock.patch.object(pl, "_gen_revoke_all", FAKE_REGISTRY.clear), \
            mock.patch.object(pl, "_win_unprotect", fake_unprotect), \
            mock.patch.object(pl.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
        yield run


class Node:
    """A real link on loopback with its own state directory."""

    def __init__(self, case, host, name, provider=None, clock=None):
        self.dir = Path(tempfile.mkdtemp())
        kw = {"clock": clock} if clock else {}
        self.link = pl.PeerLink(self.dir, host, name, provider or (lambda: status(host)), status_port=0,
                                beacon_port=0, bind="127.0.0.1", broadcast=(), allow_loopback=True, **kw).start()
        case.addCleanup(self.link.stop)

    def hello_to(self, other):
        other.link.handle_datagram(pl.make_hello(self.link.node, self.link.host, self.link.name,
                                                 self.link.status_port, time.time()), "127.0.0.1")

    def control(self, method, path, body=None):
        status, doc = pl.control_request(self.dir, self.link.status_port, method, path, body)
        if status != 200:
            raise urllib.error.HTTPError(path, status, str(doc), None, None)
        return doc


def pair(case, a, b):
    a.hello_to(b)
    b.hello_to(a)
    pin = a.link.invite(b.link.name)["pin"]
    b.link.accept(a.link.name, pin)
    return pin


class Spake2(unittest.TestCase):
    A = {"node": "11111111-1111-4111-8111-111111111111", "name": "mac", "host": "mac"}
    B = {"node": "22222222-2222-4222-8222-222222222222", "name": "pc", "host": "windows"}

    def run_pair(self, pin_a, pin_b, invite_a="i" * 32, invite_b="i" * 32):
        inviter = pl.Spake("inviter", pin_a, self.A, self.B, invite_a)
        joiner = pl.Spake("joiner", pin_b, self.A, self.B, invite_b)
        return inviter.finish(pl._hex(joiner.public)), joiner.finish(pl._hex(inviter.public))

    def test_same_pin_agrees_on_link_key_and_confirmations(self):
        a, b = self.run_pair("402199", "402199")
        self.assertEqual(a, b)
        self.assertEqual(len(a["link"]), 32)

    def test_wrong_pin_or_other_invite_disagrees(self):
        for args in (("402199", "402198"), ("402199", "402199", "i" * 32, "j" * 32)):
            a, b = self.run_pair(*args)
            self.assertNotEqual(a["link"], b["link"])
            self.assertNotEqual(a["inviterConfirm"], b["inviterConfirm"])

    def test_fresh_randomness_each_run(self):
        self.assertNotEqual(self.run_pair("123456", "123456")[0]["link"], self.run_pair("123456", "123456")[0]["link"])

    def test_generators_are_in_the_subgroup_and_distinct(self):
        for value in (pl.G, pl.M, pl.N):
            self.assertTrue(pl._in_group(value))
        self.assertEqual(len({pl.G, pl.M, pl.N}), 3)

    def test_rejects_identity_small_order_and_out_of_range_elements(self):
        joiner = pl.Spake("joiner", "000000", self.A, self.B, "i" * 32)
        non_residue = next(v for v in range(2, 50) if pow(v, pl.Q, pl.P) != 1)
        for bad in (1, 0, pl.P - 1, pl.P, pl.P + 4, non_residue):
            with self.assertRaises(ValueError):
                joiner.finish(format(bad % (1 << 2048), "0512x"))
        for bad in ("zz", "0" * 511, 7, None):
            with self.assertRaises(ValueError):
                joiner.finish(bad)


class Hello(unittest.TestCase):
    def test_valid_hello_and_junk(self):
        node = "33333333-3333-4333-8333-333333333333"
        self.assertEqual(pl.read_hello(pl.make_hello(node, "windows", "pc", 8768, 1.0))["name"], "pc")
        for data in (b"\xff", b"[]", b"{}", b"x" * 2000,
                     pl.make_hello("not-a-uuid", "windows", "pc", 8768, 1.0),
                     pl.make_hello(node, "toaster", "pc", 8768, 1.0),
                     pl.make_hello(node, "windows", "pc", 80, 1.0),
                     pl.make_hello(node, "windows", "bad\x1bname", 8768, 1.0)):
            self.assertIsNone(pl.read_hello(data), data[:50])

    def test_only_private_lan_sources(self):
        for ok in ("<pc-lan-ip>", "192.168.1.5", "172.16.0.9", "169.254.3.4", "fe80::1%en0", "::ffff:<lan-ip>"):
            self.assertTrue(pl.lan_source(ok), ok)
        for bad in ("8.8.8.8", "127.0.0.1", "224.0.0.1", "0.0.0.0", "nonsense", "2001:4860::8888"):
            self.assertFalse(pl.lan_source(bad), bad)


class Pairing(unittest.TestCase):
    def setUp(self):
        self.mac = Node(self, "mac", "louis-mac")
        self.pc = Node(self, "windows", "louisaurorar12")

    def test_discovered_machine_is_available_not_trusted(self):
        self.pc.hello_to(self.mac)
        row = self.mac.link.snapshot()["peers"][0]
        self.assertEqual((row["state"], row["paired"], row["name"]), ("available", False, "louisaurorar12"))
        self.mac.link.poll_once()
        self.assertIsNone(self.mac.link.snapshot()["peers"][0]["status"])

    def test_pin_pairing_then_auto_connect_both_ways(self):
        pair(self, self.mac, self.pc)
        for me, other in ((self.mac, self.pc), (self.pc, self.mac)):
            me.link.poll_once()
            row = me.link.snapshot()["peers"][0]
            self.assertEqual((row["state"], row["paired"], row["error"]), ("connected", True, None), me.link.name)
            self.assertEqual(row["status"]["host"], other.link.host)
        files = list((self.mac.dir / "trusted").glob("*.json"))
        self.assertEqual([f.stem for f in files], [self.pc.link.node])
        if os.name != "nt":
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(pl.TrustStore(self.mac.dir).load()[self.pc.link.node]["key"],
                         pl.TrustStore(self.pc.dir).load()[self.mac.link.node]["key"])

    def test_inviter_shows_pin_and_joiner_sees_waiting_invite(self):
        self.pc.hello_to(self.mac)
        self.mac.hello_to(self.pc)
        result = self.mac.link.invite("louisaurorar12")
        self.assertRegex(result["pin"], r"^\d{6}$")
        self.assertTrue(self.pc.link.snapshot()["peers"][0]["inviteWaiting"])

    def test_wrong_pin_pins_nothing_and_three_failures_end_the_invite(self):
        self.pc.hello_to(self.mac)
        self.mac.hello_to(self.pc)
        pin = self.mac.link.invite("louisaurorar12")["pin"]
        wrong = f"{(int(pin) + 1) % 1000000:06d}"
        for _ in range(3):
            with self.assertRaises(PermissionError):
                self.pc.link.accept("", wrong)
        with self.assertRaises(ConnectionError):
            self.pc.link.accept("", pin)  # the right PIN, too late: the invite is gone
        self.assertEqual(self.mac.link.trusted(), {})
        self.assertEqual(self.pc.link.trusted(), {})

    def test_pin_from_another_invite_is_useless(self):
        self.pc.hello_to(self.mac)
        self.mac.hello_to(self.pc)
        old = self.mac.link.invite("louisaurorar12")["pin"]
        new = self.mac.link.invite("louisaurorar12")["pin"]
        if old != new:
            with self.assertRaises((PermissionError, ConnectionError)):
                self.pc.link.accept("", old)

    def test_expired_invite_cannot_be_used(self):
        clock = [time.time()]
        mac = Node(self, "mac", "clocked-mac", clock=lambda: clock[0])
        self.pc.hello_to(mac)
        mac.hello_to(self.pc)
        pin = mac.link.invite("louisaurorar12")["pin"]
        clock[0] += pl.INVITE_TTL + 1
        with self.assertRaises(ConnectionError):
            self.pc.link.accept("clocked-mac", pin)

    def test_unpair_cuts_status_access_on_the_other_side(self):
        pair(self, self.mac, self.pc)
        self.mac.link.poll_once()
        self.assertEqual(self.mac.link.snapshot()["peers"][0]["state"], "connected")
        self.pc.link.unpair("louis-mac")
        self.mac.link.poll_once()
        row = self.mac.link.snapshot()["peers"][0]
        self.assertNotEqual(row["state"], "connected")
        self.assertIsNone(row["status"])
        self.assertIn("403", row["error"])

    def test_repair_with_a_different_key_is_refused_until_unpaired(self):
        pair(self, self.mac, self.pc)
        with self.assertRaises(ValueError):
            self.mac.link.invite("louisaurorar12")
        with self.assertRaises(PermissionError):
            self.mac.link.store.pin(self.pc.link.node, "x", "windows", b"\x00" * 32)


class StatusRoute(unittest.TestCase):
    def setUp(self):
        self.mac = Node(self, "mac", "louis-mac")
        self.pc = Node(self, "windows", "louisaurorar12")
        pair(self, self.mac, self.pc)
        self.key = self.mac.link.trusted()[self.pc.link.node]["key"]

    def fetch(self, key=None, me=None, peer=None):
        return pl.fetch_status(key or self.key, me or self.mac.link.node, "127.0.0.1", self.pc.link.status_port,
                               peer or self.pc.link.node, "windows", time.time())

    def test_paired_fetch_works(self):
        self.assertEqual(self.fetch()["host"], "windows")

    def test_unknown_requester_wrong_key_and_impostor_reply(self):
        with self.assertRaises(ConnectionError):
            self.fetch(me="44444444-4444-4444-8444-444444444444")
        with self.assertRaises(ConnectionError):
            self.fetch(key=b"\x01" * 32)
        # The request signature names the target machine, so asking the wrong one is refused outright.
        with self.assertRaises(ConnectionError):
            self.fetch(peer="55555555-5555-4555-8555-555555555555")

    def test_unsigned_wrong_route_and_methods(self):
        base = f"http://127.0.0.1:{self.pc.link.status_port}"
        for path, code in ((pl.PREFIX + "/status", 403), ("/", 404), ("/api/snapshot", 404), ("/control/peers", 404)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + path, timeout=2)
            self.assertEqual(caught.exception.code, code, path)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(base + pl.PREFIX + "/status", method="PUT"), timeout=2)
        self.assertEqual(caught.exception.code, 405)

    def test_request_replay_is_refused(self):
        sent = {}
        real = pl.http.client.HTTPConnection.request

        def capture(conn, method, url, body=None, headers=None, **kw):
            sent.update(headers)
            return real(conn, method, url, body=body, headers=headers, **kw)

        with mock.patch.object(pl.http.client.HTTPConnection, "request", capture):
            self.fetch()
        sent.pop("Connection", None)
        replay = urllib.request.Request(f"http://127.0.0.1:{self.pc.link.status_port}{pl.PREFIX}/status", headers=sent)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(replay, timeout=2)
        self.assertEqual(caught.exception.code, 403)

    def test_provider_failure_is_an_error_not_stale_data(self):
        self.mac.link.poll_once()
        self.assertEqual(self.mac.link.snapshot()["peers"][0]["state"], "connected")
        self.pc.link.status_provider = lambda: (_ for _ in ()).throw(OSError("lanes down"))
        self.mac.link.poll_once()
        row = self.mac.link.snapshot()["peers"][0]
        self.assertNotEqual(row["state"], "connected")
        self.assertIn("503", row["error"])
        self.assertIsNone(row["status"])


class Spoofing(unittest.TestCase):
    def setUp(self):
        self.mac = Node(self, "mac", "louis-mac")
        self.pc = Node(self, "windows", "louisaurorar12")
        pair(self, self.mac, self.pc)
        self.mac.link.poll_once()

    def test_spoofed_hello_cannot_rename_or_move_a_paired_peer(self):
        fake = pl.make_hello(self.pc.link.node, "mac", "evil", 9999, time.time())
        self.mac.link.handle_datagram(fake, "10.9.9.9")
        self.mac.link.poll_once()
        row = self.mac.link.snapshot()["peers"][0]
        self.assertEqual((row["name"], row["host"], row["state"], row["address"]),
                         ("louisaurorar12", "windows", "connected", "127.0.0.1"))

    def test_table_flood_evicts_unpaired_not_paired(self):
        for i in range(pl.MAX_PEERS + 10):
            node = f"{i:08x}-0000-4000-8000-000000000000"
            self.mac.link.handle_datagram(pl.make_hello(node, "windows", f"f{i}", 9000, time.time()), "127.0.0.1")
        rows = self.mac.link.snapshot()["peers"]
        self.assertLessEqual(len(rows), pl.MAX_PEERS)
        self.assertIn(self.pc.link.node, [r["node"] for r in rows])

    def test_paired_peer_goes_stale_then_offline_but_is_remembered(self):
        clock = [time.time()]
        mac = Node(self, "mac", "clocked", clock=lambda: clock[0])
        pc = Node(self, "windows", "pc2")
        pc.hello_to(mac)
        mac.hello_to(pc)
        pin = mac.link.invite("pc2")["pin"]
        pc.link.accept("clocked", pin)
        mac.link.poll_once()
        self.assertEqual(mac.link.snapshot()["peers"][0]["state"], "connected")
        clock[0] += pl.CONNECTED_WITHIN + 1
        self.assertEqual(mac.link.snapshot()["peers"][0]["state"], "stale")
        pc.link.stop()  # the machine really goes away
        clock[0] += pl.FORGET_AVAILABLE_AFTER
        mac.link.poll_once()
        self.assertEqual([(r["state"], r["paired"]) for r in mac.link.snapshot()["peers"]], [("offline", True)])


class Control(unittest.TestCase):
    def setUp(self):
        self.mac = Node(self, "mac", "louis-mac")
        self.pc = Node(self, "windows", "louisaurorar12")

    def raw(self, path, headers, method="GET", data=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.mac.link.status_port}{path}", method=method,
                                     headers=headers, data=data)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        return caught.exception.code

    def test_control_refuses_the_old_bare_token_header_and_browser_origins(self):
        token = self.mac.link.control_token
        self.assertEqual(self.raw("/control/peers", {"X-Agiw-Control": token}), 404)
        nc = "b" * 32
        hello = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{self.mac.link.status_port}/control/hello", headers={"X-Agiw-Client-Nonce": nc}),
            timeout=5).read())
        good = {"X-Agiw-Control-Nonce": hello["ns"],
                "X-Agiw-Control-Proof": pl.control_client_proof(token, hello["ns"], nc, "GET", "/control/peers", b"")}
        self.assertEqual(self.raw("/control/peers", {**good, "Origin": "http://evil.example"}), 404)
        self.assertEqual(self.raw("/control/hello", {"X-Agiw-Client-Nonce": nc, "Origin": "http://evil.example"}), 404)

    def test_wrong_token_and_replayed_proof_are_refused(self):
        token = self.mac.link.control_token
        nc = "c" * 32
        hello = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{self.mac.link.status_port}/control/hello", headers={"X-Agiw-Client-Nonce": nc}),
            timeout=5).read())
        bad = {"X-Agiw-Control-Nonce": hello["ns"],
               "X-Agiw-Control-Proof": pl.control_client_proof("0" * 64, hello["ns"], nc, "GET", "/control/peers", b"")}
        self.assertEqual(self.raw("/control/peers", bad), 404)
        good = {"X-Agiw-Control-Nonce": hello["ns"],
                "X-Agiw-Control-Proof": pl.control_client_proof(token, hello["ns"], nc, "GET", "/control/peers", b"")}
        self.assertEqual(self.raw("/control/peers", good), 404, "server nonce is single use, even after a failure")

    def test_a_listener_squatting_the_port_never_sees_the_token(self):
        seen = []

        class Squatter(pl.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                seen.append(dict(self.headers))
                body = json.dumps({"ns": "d" * 32, "proof": "e" * 64}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

        squatter = pl.ThreadingHTTPServer(("127.0.0.1", 0), Squatter)
        pl.threading.Thread(target=squatter.serve_forever, daemon=True).start()
        self.addCleanup(squatter.server_close)
        self.addCleanup(squatter.shutdown)
        with self.assertRaises(PermissionError):
            pl.control_request(self.mac.dir, squatter.server_address[1], "POST", "/control/unpair", {"peer": "x"})
        token = self.mac.link.control_token
        self.assertEqual(len(seen), 1, "only the hello was sent")
        self.assertFalse(any(token in v for h in seen for v in h.values()))
        self.assertFalse(any(k.lower().startswith("x-agiw-control-proof") for h in seen for k in h))

    def test_full_flow_through_control_endpoints(self):
        self.pc.hello_to(self.mac)
        self.mac.hello_to(self.pc)
        pin = self.mac.control("POST", "/control/invite", {"peer": "louisaurorar12"})["pin"]
        self.assertEqual(self.pc.control("POST", "/control/accept", {"peer": "", "pin": pin})["peer"], "louis-mac")
        self.mac.link.poll_once()
        self.assertEqual(self.mac.control("GET", "/control/peers")["peers"][0]["state"], "connected")
        self.assertEqual(self.mac.control("POST", "/control/unpair", {"peer": "louisaurorar12"})["unpaired"],
                         self.pc.link.node)

    def test_identity_is_stable_and_private(self):
        again = pl.load_identity(self.mac.dir)
        self.assertEqual(again, self.mac.link.node)
        self.assertRegex(again, pl.UUID_RE)
        if os.name != "nt":
            self.assertEqual((self.mac.dir / "control-token").stat().st_mode & 0o777, 0o600)


class RealUdp(unittest.TestCase):
    def test_hello_over_a_real_socket_reaches_the_listener(self):
        mac = Node(self, "mac", "louis-mac")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        data = pl.make_hello("66666666-6666-4666-8666-666666666666", "windows", "pc", 8768, time.time())
        deadline = time.time() + 3
        while not mac.link.snapshot()["peers"] and time.time() < deadline:
            sock.sendto(data, ("127.0.0.1", mac.link.beacon_port))
            time.sleep(0.05)
        self.assertEqual(mac.link.snapshot()["peers"][0]["name"], "pc")


class Hardening(unittest.TestCase):
    def setUp(self):
        self.mac = Node(self, "mac", "louis-mac")
        self.pc = Node(self, "windows", "louisaurorar12")
        pair(self, self.mac, self.pc)

    def signed_headers(self, ts):
        key = pl.Subkeys(self.mac.link.trusted()[self.pc.link.node]["key"])
        nonce = "a" * 32
        signed = "\n".join(("GET", pl.PREFIX + "/status", self.mac.link.node, self.pc.link.node, ts, nonce)).encode()
        return {"X-Agiw-Node": self.mac.link.node, "X-Agiw-Ts": ts, "X-Agiw-Nonce": nonce,
                "X-Agiw-Sig": pl.hmac.new(key.request, signed, pl.hashlib.sha256).hexdigest()}

    def test_non_finite_request_timestamp_is_refused(self):
        for ts in ("nan", "inf", "-inf"):
            req = urllib.request.Request(f"http://127.0.0.1:{self.pc.link.status_port}{pl.PREFIX}/status",
                                         headers=self.signed_headers(ts))
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=2)
            self.assertEqual(caught.exception.code, 403, ts)

    def test_old_observation_is_not_served_as_live(self):
        old = status("windows")
        old["observedAt"] = pl._iso(time.time() - 3600)
        self.pc.link.status_provider = lambda: old
        self.mac.link.poll_once()
        self.assertIn("503", self.mac.link.snapshot()["peers"][0]["error"])
        for bad in ("not-a-time", "2026-10-10T18:00:00"):
            with self.assertRaises(ValueError):
                pl.validate_status({**status("windows"), "observedAt": bad}, "windows", time.time())

    def test_strict_json_rejects_nan(self):
        with self.assertRaises(ValueError):
            pl.strict_json('{"ts": NaN}')

    def test_drip_fed_headers_lose_their_slot(self):
        with mock.patch.object(pl, "REQUEST_DEADLINE", 0.5):
            server = Node(self, "windows", "slow-target")
            sock = socket.create_connection(("127.0.0.1", server.link.status_port))
            self.addCleanup(sock.close)
            sock.settimeout(3)
            started = time.monotonic()
            sock.sendall(b"GET / HTTP/1.0\r\n")
            try:
                while sock.recv(1024):
                    pass
            except OSError:
                pass
            self.assertLess(time.monotonic() - started, 2.5)

    def test_trickling_peer_cannot_freeze_the_poller(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def trickle():
            conn, _ = listener.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.0 200 OK\r\n")
            for _ in range(40):
                time.sleep(0.2)
                try:
                    conn.sendall(b"X-Pad: 1\r\n")
                except OSError:
                    break
            conn.close()

        pl.threading.Thread(target=trickle, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises((TimeoutError, OSError, pl.http.client.HTTPException)):
            pl._http("127.0.0.1", listener.getsockname()[1], "GET", "/", {}, None, 1024, deadline=1.0)
        self.assertLess(time.monotonic() - started, 2.5)

    def test_windows_run_requires_an_explicit_status_source(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                pl.main(["--state-dir", str(self.mac.dir), "run", "--host", "windows"])


class Regressions(unittest.TestCase):
    def setUp(self):
        # Explicit polls below own the ordering; keep the real HTTP/UDP threads.
        poller = mock.patch.object(pl.PeerLink, "_poll", lambda self: None)
        poller.start()
        self.addCleanup(poller.stop)

    def nodes(self, paired=True):
        mac = Node(self, "mac", "louis-mac")
        pc = Node(self, "windows", "louisaurorar12")
        if paired:
            pair(self, mac, pc)
        return mac, pc

    def test_http_deadline_interrupts_a_trickling_response_body(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(1)
        stopped = pl.threading.Event()
        drips, errors = [], []

        def trickle():
            try:
                with listener.accept()[0] as conn:
                    conn.settimeout(1)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        chunk = conn.recv(4096)
                        if not chunk:
                            raise ConnectionError("client closed before sending headers")
                        request += chunk
                    conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 10000\r\n\r\n")
                    # Bound the fixture too, so a broken watchdog fails instead of hanging the suite.
                    until = time.monotonic() + 1.8
                    while not stopped.is_set() and time.monotonic() < until:
                        try:
                            conn.sendall(b"x")
                        except OSError:
                            break  # the deadline closed the client's response socket
                        drips.append(time.monotonic())
                        stopped.wait(0.1)
            except OSError as exc:
                errors.append(exc)

        thread = pl.threading.Thread(target=trickle, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaises((TimeoutError, OSError, pl.http.client.HTTPException)):
                pl._http("127.0.0.1", listener.getsockname()[1], "GET", "/", {}, None,
                         10000, deadline=0.5)
            elapsed = time.monotonic() - started
        finally:
            stopped.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(drips), 2, "the client must reach the trickling body")
        self.assertLess(elapsed, 1.5)

    def test_hanging_peer_does_not_delay_a_healthy_peer(self):
        mac, pc = self.nodes()
        slow = Node(self, "linux", "slow-peer")
        pair(self, mac, slow)
        pins = mac.link.trusted()
        # Force the slow peer first: a serial poller must fail the ordering assertion.
        ordered = {slow.link.node: pins[slow.link.node], pc.link.node: pins[pc.link.node]}
        entered, finished, healthy, release = (pl.threading.Event() for _ in range(4))
        real_fetch, real_poll = pl.fetch_status, mac.link._poll_peer
        errors = []

        def fetch(key, me, address, port, node, host, now):
            if node == slow.link.node:
                entered.set()
                time.sleep(pl.FETCH_DEADLINE)
                release.wait(1)
                finished.set()
                raise TimeoutError("hung peer")
            return real_fetch(key, me, address, port, node, host, now)

        def poll_peer(node, pin, seen):
            real_poll(node, pin, seen)
            if node == pc.link.node:
                healthy.set()

        def poll():
            try:
                mac.link.poll_once()
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(pl, "FETCH_DEADLINE", 0.2), \
                mock.patch.object(pl, "fetch_status", fetch), \
                mock.patch.object(mac.link, "trusted", return_value=ordered), \
                mock.patch.object(mac.link, "_poll_peer", poll_peer):
            thread = pl.threading.Thread(target=poll, daemon=True)
            started = time.monotonic()
            thread.start()
            try:
                self.assertTrue(entered.wait(0.5))
                self.assertTrue(healthy.wait(0.5), "healthy peer waited for the hung peer")
                self.assertFalse(finished.is_set())
                rows = {r["node"]: r for r in mac.link.snapshot()["peers"]}
                self.assertEqual(rows[pc.link.node]["state"], "connected")
            finally:
                release.set()
                thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 2 * pl.FETCH_DEADLINE + 1)
        self.assertEqual(errors, [])
        self.assertTrue(finished.is_set())
        rows = {r["node"]: r for r in mac.link.snapshot()["peers"]}
        self.assertEqual(rows[pc.link.node]["state"], "connected")
        self.assertEqual(rows[pc.link.node]["status"]["host"], "windows")
        self.assertNotEqual(rows[slow.link.node]["state"], "connected")
        self.assertIsNone(rows[slow.link.node]["status"])

    def late_poll_after_unpair(self, repair):
        mac, pc = self.nodes()
        pc.link.status_provider = lambda: status("windows", "inactive")
        old_key = mac.link.trusted()[pc.link.node]["key"]
        fetched, release, delivered = (pl.threading.Event() for _ in range(3))
        real_fetch = pl.fetch_status
        errors = []

        def delayed_fetch(*args):
            doc = real_fetch(*args)  # hold a real, authenticated old-key reply
            fetched.set()
            if not release.wait(1.5):
                raise TimeoutError("test did not release the old reply")
            delivered.set()
            return doc

        def poll():
            try:
                mac.link.poll_once()
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(pl, "fetch_status", delayed_fetch):
            thread = pl.threading.Thread(target=poll, daemon=True)
            thread.start()
            try:
                self.assertTrue(fetched.wait(0.5))
                mac.link.unpair(pc.link.node)
                if repair:
                    pc.link.unpair(mac.link.node)
                    pair(self, mac, pc)
                    self.assertNotEqual(mac.link.trusted()[pc.link.node]["key"], old_key)
                self.assertNotIn(pc.link.node, mac.link._live)
                self.assertIsNone(mac.link.snapshot()["peers"][0]["status"])
            finally:
                release.set()
                thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(delivered.is_set(), "the old successful reply must actually reach the poller")
        self.assertNotIn(pc.link.node, mac.link._live)
        row = mac.link.snapshot()["peers"][0]
        self.assertEqual(row["paired"], repair)
        self.assertNotEqual(row["state"], "connected")
        self.assertIsNone(row["status"])
        if repair:
            pc.link.status_provider = lambda: status("windows")
            mac.link.poll_once()
            row = mac.link.snapshot()["peers"][0]
            self.assertEqual(row["state"], "connected")
            self.assertEqual(row["status"]["models"][0]["loadedState"], "loaded")

    def test_unpair_discards_an_in_flight_poll(self):
        self.late_poll_after_unpair(repair=False)

    def test_repair_with_a_new_key_discards_an_old_in_flight_poll(self):
        self.late_poll_after_unpair(repair=True)

    def test_same_ip_new_port_is_adopted_only_after_authenticated_status(self):
        mac, pc = self.nodes()
        mac.link.poll_once()
        old_port, node = pc.link.status_port, pc.link.node
        pc.link.stop()
        # Reserve the old port without listening so restart must choose another one.
        reserved = socket.socket()
        self.addCleanup(reserved.close)
        if os.name != "nt":
            reserved.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        reserved.bind(("127.0.0.1", old_port))
        pc.link = pl.PeerLink(pc.dir, "windows", "louisaurorar12", lambda: status("windows"),
                              status_port=0, beacon_port=0, bind="127.0.0.1", broadcast=(),
                              allow_loopback=True).start()
        self.addCleanup(pc.link.stop)
        self.assertEqual(pc.link.node, node)
        new_port = pc.link.status_port
        self.assertNotEqual(new_port, old_port)
        pc.hello_to(mac)
        self.assertEqual(mac.link._seen[node]["port"], old_port)
        self.assertIn(("127.0.0.1", new_port), mac.link._seen[node]["candidates"])
        pc.link.status_provider = lambda: (_ for _ in ()).throw(OSError("not ready"))
        mac.link.poll_once()
        self.assertEqual(mac.link._seen[node]["port"], old_port)
        self.assertIsNone(mac.link.snapshot()["peers"][0]["status"])
        pc.link.status_provider = lambda: status("windows")
        with mock.patch.object(pl, "fetch_status", wraps=pl.fetch_status) as fetch:
            mac.link.poll_once()
            self.assertEqual([call.args[3] for call in fetch.call_args_list], [old_port, new_port])
            self.assertEqual(mac.link._seen[node]["port"], new_port)
            self.assertNotIn(("127.0.0.1", new_port), mac.link._seen[node]["candidates"])
            self.assertEqual(mac.link.snapshot()["peers"][0]["state"], "connected")
            fetch.reset_mock()
            mac.link.poll_once()
            self.assertEqual([call.args[3] for call in fetch.call_args_list], [new_port])

    def test_overlapping_pair_starts_reject_the_older_finish(self):
        mac, pc = self.nodes(paired=False)
        pc.hello_to(mac)
        mac.hello_to(pc)
        pin = mac.link.invite(pc.link.name)["pin"]
        ready = {name: pl.threading.Event() for name in ("older", "newer")}
        release = {name: pl.threading.Event() for name in ready}
        replies, results, errors = {}, {}, {}
        real_http = pl._http

        def http(*args, **kwargs):
            reply = real_http(*args, **kwargs)
            name = pl.threading.current_thread().name
            if args[3] == pl.PREFIX + "/pair/start":
                ready[name].set()
                if not release[name].wait(1.5):
                    raise TimeoutError("test did not release the start reply")
            elif args[3] == pl.PREFIX + "/pair/finish":
                replies[name] = reply[0]
            return reply

        def accept(name):
            try:
                results[name] = pc.link.accept(mac.link.name, pin)
            except Exception as exc:
                errors[name] = exc

        threads = {name: pl.threading.Thread(target=accept, args=(name,), name=name, daemon=True)
                   for name in ready}
        with mock.patch.object(pl, "_http", http):
            threads["older"].start()
            try:
                self.assertTrue(ready["older"].wait(0.7))
                threads["newer"].start()
                self.assertTrue(ready["newer"].wait(0.7))
                # Both starts are outstanding; finish the superseded attempt first.
                release["older"].set()
                threads["older"].join(0.5)
                self.assertFalse(threads["older"].is_alive())
                self.assertIn(replies.get("older"), (404, 409))
                self.assertIsInstance(errors.get("older"), ConnectionError)
                self.assertEqual(mac.link.trusted(), {})
                self.assertEqual(pc.link.trusted(), {})
            finally:
                for event in release.values():
                    event.set()
                for thread in threads.values():
                    if thread.ident is not None:
                        thread.join(0.5)
        self.assertTrue(all(not thread.is_alive() for thread in threads.values()))
        self.assertNotIn("newer", errors)
        self.assertEqual(replies["newer"], 200)
        self.assertTrue(results["newer"]["paired"])
        self.assertEqual(mac.link.trusted()[pc.link.node]["key"], pc.link.trusted()[mac.link.node]["key"])

    def test_lost_final_reply_is_retried_idempotently(self):
        mac, pc = self.nodes(paired=False)
        finishes, pinned_before_drop = [], []
        real_http = pl._http

        def drop_first_finish(*args, **kwargs):
            reply = real_http(*args, **kwargs)
            if args[3] == pl.PREFIX + "/pair/finish":
                finishes.append((args[5], reply[0], reply[2]))
                if len(finishes) == 1:
                    pinned_before_drop.append(pc.link.node in mac.link.trusted())
                    raise ConnectionResetError("lost final reply after inviter processed it")
            return reply

        with mock.patch.object(pl, "_http", drop_first_finish), \
                mock.patch.object(mac.link.store, "pin", wraps=mac.link.store.pin) as pin:
            pair(self, mac, pc)
        self.assertEqual(pinned_before_drop, [True])
        self.assertEqual(len(finishes), 2)
        self.assertEqual([reply[1] for reply in finishes], [200, 200])
        self.assertEqual(finishes[0], finishes[1], "retry must use the same exchange and get the same answer")
        self.assertEqual(pin.call_count, 1, "an idempotent finish must not pin a second time")
        self.assertEqual(mac.link.trusted()[pc.link.node]["key"], pc.link.trusted()[mac.link.node]["key"])
        for me in (mac, pc):
            me.link.poll_once()
            self.assertEqual(me.link.snapshot()["peers"][0]["state"], "connected")

    def test_control_reads_multiple_statuses_larger_than_64_kib_total(self):
        # Outgoing statuses are projected (ids capped at 256 chars), so one peer can no longer exceed
        # ~14 KiB; a full table of MAX_PEERS peers can. Simulate that table on the control route.
        mac, pc = self.nodes()
        mac.link.poll_once()
        real = mac.link.snapshot
        big_rows = [{"node": f"{i:08x}-0000-4000-8000-000000000000", "status": {"models": [
            {"id": "m" * 256, "loadedState": "loaded"}] * pl.MAX_MODELS}} for i in range(8)]

        def big():
            doc = real()
            doc["peers"] = doc["peers"] + big_rows
            return doc

        with mock.patch.object(mac.link, "snapshot", big):
            self.assertGreater(len(pl.canonical(big())), 65536)
            doc = pl._control(mac.dir, mac.link.status_port, "GET", "/control/peers")
        self.assertEqual(len(doc["peers"]), 9)
        self.assertEqual(doc["peers"][0]["state"], "connected")

    @unittest.skipIf(os.name == "nt", "POSIX permissions; Windows uses icacls")
    def test_restrict_dir_sets_0700_on_a_preexisting_state_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "state"
            directory.mkdir()
            directory.chmod(0o755)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
            clean = mock.Mock(returncode=0, stdout="drwx------  2 me  staff  64 Oct 10 12:00 state\n")
            with mock.patch.object(pl.subprocess, "run", return_value=clean) as run:
                pl._restrict_dir(directory)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            if sys.platform == "darwin":  # extended ACLs are stripped and the result read back
                self.assertTrue(any("-N" in call.args[0] for call in run.call_args_list))


class StartupFailure(unittest.TestCase):
    def test_udp_bind_failure_releases_the_tcp_listener(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", 0))
        self.addCleanup(blocker.close)
        link = pl.PeerLink(Path(tempfile.mkdtemp()), "mac", "m", lambda: status("mac"), status_port=0,
                           beacon_port=blocker.getsockname()[1], bind="127.0.0.1", broadcast=(), allow_loopback=True)
        with mock.patch.object(socket.socket, "setsockopt"):  # no SO_REUSEADDR, so the bind really fails
            with self.assertRaises(OSError):
                link.start()
        self.assertIsNone(link.server)
        self.assertIsNone(link.udp)


class FinishAck(unittest.TestCase):
    def test_forged_bare_200_on_finish_pins_nothing_on_the_joiner(self):
        mac = Node(self, "mac", "louis-mac")
        pc = Node(self, "windows", "louisaurorar12")
        pc.hello_to(mac)
        mac.hello_to(pc)
        pin = mac.link.invite("louisaurorar12")["pin"]
        real = pl._http

        def forge(address, port, method, path, headers, body, limit, deadline=pl.FETCH_DEADLINE):
            if path.endswith("/pair/finish"):
                return 200, {}, b'{"paired":true}'  # an attacker's reply, without the PIN-derived ack
            return real(address, port, method, path, headers, body, limit, deadline)

        with mock.patch.object(pl, "_http", forge):
            with self.assertRaises(PermissionError):
                pc.link.accept("", pin)
        self.assertEqual(pc.link.trusted(), {})

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_exposed_control_token_is_rotated_not_reused(self):
        state = Path(tempfile.mkdtemp())
        token = pl.load_control_token(state)
        os.chmod(state / "control-token", 0o644)
        fresh = pl.load_control_token(state)
        self.assertNotEqual(fresh, token)
        self.assertEqual((state / "control-token").stat().st_mode & 0o777, 0o600)
        with mock.patch.object(pl.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(pl.LooseSecret):
                pl._read_private(state / "control-token")

    def test_undecodable_or_malformed_control_token_is_rotated(self):
        state = Path(tempfile.mkdtemp())
        pl.load_control_token(state)
        path = state / "control-token"
        for bad in (b"\xff" * 64 + b"\n",                              # not UTF-8 (a ValueError, not OSError)
                    b"Z" * 64 + b"\n",                                 # 64 characters, but not hex
                    (secrets.token_hex(32).upper() + "\n").encode(),   # not what token_hex writes
                    b"", b"ab" * 31 + b"\n"):
            path.write_bytes(bad)  # same file, still private
            fresh = pl.load_control_token(state)
            self.assertRegex(fresh, r"\A[0-9a-f]{64}\Z", msg=repr(bad))
            self.assertEqual(pl._unseal(path.read_text().strip(), "agiw-peer control-token"), fresh, msg=repr(bad))
            self.assertEqual(pl.load_control_token(state), fresh, "a well-formed token is kept")

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_exposed_trust_record_is_quarantined_and_needs_re_pairing(self):
        mac = Node(self, "mac", "louis-mac")
        pc = Node(self, "windows", "louisaurorar12")
        pair(self, mac, pc)
        record = mac.dir / "trusted" / f"{pc.link.node}.json"
        os.chmod(record, 0o640)
        self.assertEqual(mac.link.store.load(), {})
        self.assertFalse(record.exists())
        self.assertEqual(len(list((mac.dir / "trusted").glob("*.quarantined-*"))), 1)
        self.assertIn(pc.link.node, mac.link.store.quarantined)

    def test_windows_acl_read_back_fails_closed(self):
        # Judged by SID from the security descriptor, never by parsing icacls account names.
        path = Path(tempfile.mkdtemp())
        cases = [
            (PermissionError("could not read the access list"), PermissionError),  # read-back failed
            ((ME, None), pl.LooseSecret),                                           # NULL DACL: anyone
            ((ME, [ME, OTHER]), pl.LooseSecret),                                    # another account
            ((ME, [ME, EVERYONE]), pl.LooseSecret),
            ((ME, [ME, "S-1-5-32-545"]), pl.LooseSecret),                           # BUILTIN\Users
            ((OTHER, [ME]), pl.LooseSecret),                                        # someone else's file
            ((pl._ADMINS_SID, [ME]), pl.LooseSecret),                               # Administrators-owned
        ]
        for answer, expected in cases:
            security = mock.Mock(side_effect=answer) if isinstance(answer, Exception) else mock.Mock(
                return_value=answer)
            with as_windows(security):
                with self.assertRaises(expected, msg=str(answer)):
                    pl._only_me(path, True)
        for fine in ([ME, pl._SYSTEM_SID, pl._ADMINS_SID], [ME], []):
            with as_windows(mock.Mock(return_value=(ME, fine))) as run:
                pl._only_me(path, True)
            run.assert_not_called()  # no icacls, no whoami: reading needs no subprocess at all

    @unittest.skipUnless(sys.platform == "darwin", "macOS extended ACLs")
    def test_macos_extended_acl_on_a_secret_is_refused(self):
        listing = mock.Mock(returncode=0, stdout="-rw-------+ 1 me staff 65 Oct 10 token\n 0: user:other allow read\n")
        with mock.patch.object(pl.subprocess, "run", return_value=listing):
            with self.assertRaises(pl.LooseSecret):
                pl._only_me(Path("/nonexistent/token"), False)


class Hardening3(unittest.TestCase):
    def test_outbound_status_is_projected_to_the_allowlist(self):
        doc = {**status("windows"), "secretPath": "C:/Users/x/.ssh", "models": [
            {"id": "m", "host": "windows", "loadedState": "loaded", "prompt": "do not leak", "slotsTotal": 2}],
            "memory": {"level": "ok", "availablePercent": 50, "commitCharge": {"x": 1}}}
        out = pl.project_status(pl.validate_status(doc, "windows"))
        self.assertNotIn("secretPath", out)
        self.assertNotIn("prompt", out["models"][0])
        self.assertEqual(out["models"][0]["slotsTotal"], 2)
        self.assertEqual(out["memory"], {"level": "ok", "availablePercent": 50})

    def test_models_total_crosses_the_link_only_as_a_bounded_integer(self):
        doc = {**status("windows"), "modelsTotal": 1,
               "models": [{"id": "m", "host": "windows", "loadedState": "loaded"}]}
        self.assertEqual(pl.project_status(pl.validate_status(doc, "windows"))["modelsTotal"], 1)
        for bad in (True, -1, 1.0, "1", 1 << 17, None):
            out = pl.project_status(pl.validate_status({**doc, "modelsTotal": bad}, "windows"))
            self.assertNotIn("modelsTotal", out, repr(bad))

    def test_route_components_cross_the_link_but_only_known_ones_and_short_words(self):
        doc = {**status("mac"), "version": "0.1.0", "components": [
            {"id": "nisi", "label": "Nisi route adapter", "state": "ready", "detail": "pins verified", "extra": "x"},
            {"id": "jev", "label": "Jev", "state": "configured", "detail": "opted in", "lastJudgedAgeSeconds": 12},
            {"id": "secrets", "state": "leak"}, {"id": "jev", "state": "y" * 500}]}
        out = pl.project_status(pl.validate_status(doc, "mac"))
        self.assertEqual(out["version"], "0.1.0")
        self.assertEqual([c["id"] for c in out["components"]], ["nisi", "jev", "jev"])
        self.assertNotIn("extra", out["components"][0])
        self.assertEqual(out["components"][1]["lastJudgedAgeSeconds"], 12)
        self.assertEqual(len(out["components"][2]["state"]), 128)

    def test_invite_notice_from_an_undiscovered_machine_is_refused(self):
        pc = Node(self, "windows", "louisaurorar12")
        stranger = {"node": "77777777-7777-4777-8777-777777777777", "name": "x", "host": "mac",
                    "invite": "a" * 32, "port": 9000}
        self.assertEqual(pc.link.on_notify(stranger, "127.0.0.1")[0], 403)

    def test_retry_during_the_inviters_save_waits_and_gets_the_ack(self):
        mac = Node(self, "mac", "louis-mac")
        pc = Node(self, "windows", "louisaurorar12")
        pc.hello_to(mac)
        mac.hello_to(pc)
        pin = mac.link.invite("louisaurorar12")["pin"]
        release = pl.threading.Event()
        real_pin = mac.link.store.pin

        def slow_pin(*args):
            release.wait(5)
            return real_pin(*args)

        real_http = pl._http
        calls = []

        def flaky(address, port, method, path, headers, body, limit, deadline=pl.FETCH_DEADLINE):
            if path.endswith("/pair/finish") and not calls:
                calls.append(1)
                # the first finish reaches the inviter, which starts its slow save; the reply is "lost"
                pl.threading.Thread(target=real_http, args=(address, port, method, path, headers, body, limit, 10),
                                    daemon=True).start()
                time.sleep(0.3)
                pl.threading.Timer(0.5, release.set).start()
                raise ConnectionResetError("reply lost")
            return real_http(address, port, method, path, headers, body, limit, deadline)

        with mock.patch.object(mac.link.store, "pin", slow_pin), mock.patch.object(pl, "_http", flaky):
            self.assertTrue(pc.link.accept("", pin)["paired"])
        self.assertIn(pc.link.node, mac.link.trusted())
        self.assertIn(mac.link.node, pc.link.trusted())


class ControlTokenRotation(unittest.TestCase):
    """The running daemon follows the token file: an exposed token stops working the moment the file is
    replaced, without a restart, and challenges issued under it die with it."""

    def setUp(self):
        self.mac = Node(self, "mac", "louis-mac")
        self.base = f"http://127.0.0.1:{self.mac.link.status_port}"
        self.file = self.mac.dir / "control-token"

    def on_disk(self):
        return pl._unseal(self.file.read_text().strip(), "agiw-peer control-token")

    def proves(self, token, nc):
        """One /control/hello: the server nonce, and whether the daemon proved `token`."""
        req = urllib.request.Request(self.base + "/control/hello", headers={"X-Agiw-Client-Nonce": nc})
        doc = json.loads(urllib.request.urlopen(req, timeout=5).read())
        return doc["ns"], hmac.compare_digest(doc["proof"], pl.control_server_proof(token, nc, doc["ns"]))

    def peers(self, token, ns, nc):
        proof = pl.control_client_proof(token, ns, nc, "GET", "/control/peers", b"")
        req = urllib.request.Request(self.base + "/control/peers",
                                     headers={"X-Agiw-Control-Nonce": ns, "X-Agiw-Control-Proof": proof})
        try:
            with urllib.request.urlopen(req, timeout=5) as reply:
                json.loads(reply.read())
                return reply.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_rotated_control_token_revokes_the_old_one_without_restart(self):
        a = self.on_disk()
        ns, proved = self.proves(a, "1" * 32)
        self.assertTrue(proved)
        self.assertEqual(self.peers(a, ns, "1" * 32), 200)
        b = secrets.token_hex(32)
        pl._write_private(self.file, pl._seal(b, "agiw-peer control-token") + "\n")  # replaced while running
        ns, proved = self.proves(a, "2" * 32)
        self.assertFalse(proved, "the daemon must stop proving the old token")
        self.assertEqual(self.peers(a, ns, "2" * 32), 404, "an old-token proof is refused")
        ns, proved = self.proves(b, "3" * 32)
        self.assertTrue(proved)
        self.assertEqual(self.peers(b, ns, "3" * 32), 200, "the new token works without a restart")
        self.assertEqual(self.mac.link.control_token, b)
        self.assertEqual(pl.control_request(self.mac.dir, self.mac.link.status_port, "GET", "/control/peers")[0],
                         200)

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_exposed_token_rotation_reaches_the_running_daemon(self):
        a = self.on_disk()
        os.chmod(self.file, 0o644)               # exposed
        b = pl.load_control_token(self.mac.dir)  # the next legitimate client notices and rotates it
        self.assertNotEqual(a, b)
        ns, proved = self.proves(a, "4" * 32)
        self.assertFalse(proved)
        self.assertEqual(self.peers(a, ns, "4" * 32), 404)
        self.assertEqual(pl.control_request(self.mac.dir, self.mac.link.status_port, "GET", "/control/peers")[0],
                         200)
        # The daemon rotates an exposed token itself before using it, and never proves the exposed one.
        os.chmod(self.file, 0o604)
        ns, proved = self.proves(b, "5" * 32)
        self.assertFalse(proved)
        c = self.on_disk()
        self.assertNotEqual(c, b)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.peers(c, ns, "5" * 32), 200)

    def test_challenge_issued_before_rotation_is_refused_after_it(self):
        a = self.on_disk()
        ns1, proved1 = self.proves(a, "6" * 32)
        ns2, proved2 = self.proves(a, "7" * 32)
        self.assertTrue(proved1 and proved2)
        b = secrets.token_hex(32)
        pl._write_private(self.file, pl._seal(b, "agiw-peer control-token") + "\n")
        self.assertEqual(self.peers(a, ns1, "6" * 32), 404, "old token, old challenge")
        self.assertEqual(self.peers(b, ns2, "7" * 32), 404, "the new token cannot redeem a pre-rotation challenge")
        ns3, proved3 = self.proves(b, "8" * 32)
        self.assertTrue(proved3)
        self.assertEqual(self.peers(b, ns3, "8" * 32), 200)

    def test_a_token_that_cannot_be_vouched_for_serves_no_control(self):
        token = self.on_disk()
        ns, proved = self.proves(token, "9" * 32)
        self.assertTrue(proved)
        with mock.patch.object(pl, "load_control_token", side_effect=PermissionError("folder is not private")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.proves(token, "a" * 32)
            self.assertEqual(caught.exception.code, 503)
            self.assertEqual(self.peers(token, ns, "9" * 32), 404)
        # Whatever loading raises, hello answers 503 instead of dropping the connection.
        for failure in (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), ValueError("bad"),
                        RuntimeError("unexpected")):
            with mock.patch.object(pl, "load_control_token", side_effect=failure):
                with self.assertRaises(urllib.error.HTTPError, msg=repr(failure)) as caught:
                    self.proves(token, "c" * 32)
                self.assertEqual(caught.exception.code, 503, msg=repr(failure))

    def test_undecodable_token_on_disk_is_rotated_by_the_daemon_and_the_cli(self):
        self.file.write_bytes(b"\xff" * 64 + b"\n")  # still a private file, but not text
        nc = "d" * 32
        req = urllib.request.Request(self.base + "/control/hello", headers={"X-Agiw-Client-Nonce": nc})
        doc = json.loads(urllib.request.urlopen(req, timeout=5).read())  # answered, not a dropped connection
        fresh = self.on_disk()
        self.assertRegex(fresh, r"\A[0-9a-f]{64}\Z")
        self.assertTrue(hmac.compare_digest(doc["proof"], pl.control_server_proof(fresh, nc, doc["ns"])))
        self.assertEqual(self.peers(fresh, doc["ns"], nc), 200)
        self.file.write_bytes(b"\xfe" * 64 + b"\n")
        self.assertEqual(pl.control_request(self.mac.dir, self.mac.link.status_port, "GET", "/control/peers")[0],
                         200)  # the CLI rotates it too, instead of raising until the file is deleted by hand
        self.assertRegex(self.on_disk(), r"\A[0-9a-f]{64}\Z")


class WindowsSecurity(unittest.TestCase):
    """Windows owner and DACL checks through mocks of the ctypes readers (_win_current_sid and
    _win_owner_and_allow_sids). On a Mac these are the only Windows coverage; a native Windows run of this
    file also drives the real ctypes path, because every Node above creates and reads secrets."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.secret = self.folder / "control-token"
        FAKE_REGISTRY.clear()
        self.token = secrets.token_hex(32)  # well formed, so only an exposure can make it rotate
        self.secret.write_text(sealed(self.token, "agiw-peer control-token") + "\n")

    def trust_record(self):
        trusted = self.folder / "trusted"
        trusted.mkdir()
        record = trusted / f"{PEER}.json"
        FAKE_REGISTRY[PEER] = "e" * 32
        record.write_text(json.dumps({**PEER_RECORD_PLAIN,
                                      "linkKey": sealed("ab" * 32 + ":" + "e" * 32, "agiw-peer link " + PEER)}))
        return trusted, record

    def test_windows_rechecks_owner_and_dacl_on_every_secret_read(self):
        answers = [(ME, [ME, pl._SYSTEM_SID]), (ME, [ME, EVERYONE])]
        targets = []

        def security(target):
            targets.append(target)
            return answers[min(len(targets), len(answers)) - 1]

        with as_windows(security):
            self.assertEqual(unsealed(pl._read_private(self.secret).strip()), self.token)
            # Same file, inode and (creation-time) ctime; only the DACL changed, and it must be seen.
            with self.assertRaises(pl.LooseSecret):
                pl._read_private(self.secret)
        self.assertEqual(len(targets), 2)
        self.assertTrue(all(isinstance(t, int) for t in targets), "checked through the open handle, not the path")
        self.assertNotIn(str(self.secret), pl._verified)

    def test_windows_folder_is_rechecked_on_every_restrict_dir_call(self):
        acl = {"sids": [ME, pl._SYSTEM_SID, pl._ADMINS_SID]}
        targets = []

        def security(target):
            targets.append(target)
            return ME, list(acl["sids"])

        with as_windows(security) as run:
            pl._restrict_dir(self.folder)  # verified, secrets below checked, made exact once with icacls, read back
            pl._restrict_dir(self.folder)  # verified again: no cache and no second icacls
            self.assertEqual(run.call_count, 1)
            # By full path from the system folder, never a bare name that PATH or the working folder resolves.
            self.assertEqual(run.call_args.args[0], [ICACLS, str(self.folder), "/inheritance:r", "/grant:r",
                                                     f"*{ME}:(OI)(CI)F", "/Q"])
            self.assertEqual(run.call_args.kwargs["cwd"], SYSTEM32)
            self.assertEqual(targets, [str(self.folder), str(self.secret), str(self.folder), str(self.folder)])
            acl["sids"] = [ME, EVERYONE]  # widened; on Windows the inode and creation time stay the same
            with self.assertRaises(pl.LooseSecret):
                pl._restrict_dir(self.folder)  # icacls ran again, but the read-back still lists Everyone
            self.assertEqual(run.call_count, 2)
            self.assertFalse(self.secret.exists(), "the token was readable by Everyone, so it was deleted")

            def tighten(*_args, **_kwargs):
                acl["sids"] = [ME]
                return mock.Mock(returncode=0)

            run.side_effect = tighten
            pl._restrict_dir(self.folder)
            run.side_effect, run.return_value = None, mock.Mock(returncode=5)
            acl["sids"] = [ME, OTHER]
            with self.assertRaises(PermissionError):
                pl._restrict_dir(self.folder)  # a failing icacls is never taken for success
            calls = run.call_count
            with mock.patch.object(pl, "_win_system_dir", side_effect=PermissionError("no system folder")):
                with self.assertRaises(PermissionError):
                    pl._restrict_dir(self.folder)  # icacls is never looked up any other way
            self.assertEqual(run.call_count, calls)
        self.assertNotIn(str(self.folder), pl._verified)

    def test_windows_foreign_or_administrators_owner_is_refused(self):
        for owner in (OTHER, pl._ADMINS_SID, pl._SYSTEM_SID):
            with as_windows(mock.Mock(return_value=(owner, [ME]))) as run:
                with self.assertRaises(pl.LooseSecret, msg=owner):
                    pl._read_private(self.secret)
                with self.assertRaises(pl.LooseSecret, msg=owner):
                    pl._read_private(self.secret, secret=False)  # an owner can rewrite even the identity
                with self.assertRaises(PermissionError, msg=owner):
                    pl._restrict_dir(self.folder)
                with self.assertRaises(PermissionError, msg=owner):
                    pl.load_control_token(self.folder)
                run.assert_not_called()  # a foreign-owned folder is refused, never "fixed" with icacls
        self.assertEqual(unsealed(self.secret.read_text().strip()), self.token)
        # Our folder, but a token file someone else owns: rotated, never reused.
        with as_windows(lambda target: (ME, [ME]) if isinstance(target, str) else (OTHER, [ME])):
            fresh = pl.load_control_token(self.folder)
        self.assertNotEqual(fresh, self.token)
        self.assertEqual(unsealed(self.secret.read_text().strip()), fresh)

    def test_windows_null_dacl_and_reparse_points_are_refused(self):
        with as_windows(mock.Mock(return_value=(ME, None))) as run:
            with self.assertRaises(pl.LooseSecret):
                pl._read_private(self.secret)
            with self.assertRaises(pl.LooseSecret):
                pl._restrict_dir(self.folder)  # tightening was tried, but a NULL DACL read back is refused
            self.assertEqual(run.call_count, 1)
        reparse = mock.Mock(st_file_attributes=0x400, st_mode=stat.S_IFDIR | 0o700, st_ino=1, st_ctime_ns=1)
        with as_windows(mock.Mock(return_value=(ME, [ME]))) as run, \
                mock.patch.object(pl.os, "lstat", return_value=reparse):
            for attempt in (lambda: pl._restrict_dir(self.folder), lambda: pl._read_private(self.secret),
                            lambda: pl._only_me(self.secret, False)):
                with self.assertRaises(PermissionError) as caught:
                    attempt()
                self.assertIn("reparse point", str(caught.exception))
            run.assert_not_called()

    def test_windows_tightening_a_widened_folder_does_not_launder_its_secrets(self):
        acl = {"tight": False}

        def security(target):
            return (ME, [ME]) if acl["tight"] else (ME, [ME, EVERYONE])  # the file inherited Everyone too

        def icacls(*_args, **_kwargs):
            acl["tight"] = True  # re-inheritance strips Everyone from the files as well
            return mock.Mock(returncode=0)

        with as_windows(security) as run:
            run.side_effect = icacls
            pl._restrict_dir(self.folder)
            # It would look private now, but it was readable: it is gone from disk, not merely remembered.
            self.assertFalse(self.secret.exists())
            fresh = pl.load_control_token(self.folder)
            self.assertNotEqual(fresh, self.token)
            self.assertEqual(unsealed(pl._read_private(self.secret).strip()), fresh)

    def test_windows_widened_folder_retires_secrets_that_only_look_private(self):
        # Daybreak round 4: someone with write + WRITE_DAC on the folder writes their own token / trust record
        # into the victim-owned files, then sets each file's DACL to the victim only. Leaf metadata now looks
        # private, but the folder is still open, so nothing below it can be trusted.
        trusted, record = self.trust_record()
        attacker_token = "a" * 64
        self.secret.write_text(attacker_token + "\n")
        folders = {str(self.folder), str(trusted)}
        acl = {"tight": False}

        def security(target):
            if str(target) in folders and not acl["tight"]:
                return (ME, [ME, EVERYONE])  # the folder is still widened
            return (ME, [ME])  # every leaf was made to look private

        def icacls(*_args, **_kwargs):
            acl["tight"] = True
            return mock.Mock(returncode=0)

        with as_windows(security) as run:
            run.side_effect = icacls
            pl._restrict_dir(self.folder)
            fresh = pl.load_control_token(self.folder)
        self.assertNotEqual(fresh, attacker_token)
        self.assertFalse(record.exists())
        self.assertEqual(len(list(trusted.glob(f"{PEER}.json.quarantined-*"))), 1)

    def test_windows_case_variant_secrets_are_retired_from_a_widened_folder(self):
        # Daybreak round 5: Windows opens 'Control-Token' and 'trusted/<UUID>.JSON' for the canonical names.
        trusted, record = self.trust_record()
        os.rename(self.secret, self.folder / "Control-Token.tmp")  # two steps: a case-only rename
        os.rename(self.folder / "Control-Token.tmp", self.folder / "Control-Token")
        upper = trusted / f"{PEER.upper()}.JSON"
        os.rename(record, trusted / "x.tmp")
        os.rename(trusted / "x.tmp", upper)
        acl = {"tight": False}

        def security(target):
            if not acl["tight"] and os.path.basename(str(target)).casefold() in ("", self.folder.name.casefold(),
                                                                                  "trusted"):
                return (ME, [ME, EVERYONE])
            return (ME, [ME])

        def icacls(*_args, **_kwargs):
            acl["tight"] = True
            return mock.Mock(returncode=0)

        with as_windows(security) as run:
            run.side_effect = icacls
            pl._restrict_dir(self.folder)
        names = {n.casefold() for n in os.listdir(self.folder)}
        self.assertNotIn("control-token", names, "the renamed token was retired")
        self.assertFalse(upper.exists())
        self.assertEqual(len([n for n in os.listdir(trusted) if ".quarantined-" in n]), 1)

    def test_windows_records_in_a_renamed_trust_folder_are_retired_too(self):
        # Daybreak round 6: 'trusted' renamed to 'trust-container' (then given the 8.3 alias TRUSTED) must not
        # shelter its records from retirement.
        trusted, record = self.trust_record()
        moved = self.folder / "trust-container"
        os.rename(trusted, moved)
        acl = {"tight": False}

        def security(_target):
            return (ME, [ME]) if acl["tight"] else (ME, [ME, EVERYONE])

        def icacls(*_args, **_kwargs):
            acl["tight"] = True
            return mock.Mock(returncode=0)

        with as_windows(security) as run:
            run.side_effect = icacls
            pl._restrict_dir(self.folder)
        self.assertFalse((moved / f"{PEER}.json").exists())
        self.assertEqual(len([n for n in os.listdir(moved) if ".quarantined-" in n]), 1)

    def test_windows_secret_planted_before_the_folder_closes_is_retired_too(self):
        acl = {"tight": False}

        def security(target):
            return (ME, [ME]) if acl["tight"] else (ME, [ME, EVERYONE])

        def icacls(*_args, **_kwargs):
            # the other account drops a fresh token in between the first pass and the tightening
            self.secret.write_text("b" * 64 + "\n")
            acl["tight"] = True
            return mock.Mock(returncode=0)

        with as_windows(security) as run:
            run.side_effect = icacls
            pl._restrict_dir(self.folder)
        self.assertFalse(self.secret.exists(), "the planted token was retired by the second pass")

    def test_windows_private_folder_keeps_private_secrets(self):
        trusted, record = self.trust_record()

        def security(_target):
            return (ME, [ME])

        with as_windows(security):
            pl._restrict_dir(self.folder)
            self.assertEqual(pl.load_control_token(self.folder), self.token)
        self.assertTrue(record.exists(), "nothing was open, so nothing is retired")

    def test_windows_exposure_is_retired_on_disk_and_survives_a_restart(self):
        trusted, record = self.trust_record()
        acl = {"tight": False}

        def security(_target):
            # trusted/ inherits from the state folder, so tightening the state folder makes all of it look private
            return (ME, [ME]) if acl["tight"] else (ME, [ME, EVERYONE])

        def icacls(*_args, **_kwargs):
            acl["tight"] = True
            return mock.Mock(returncode=0)

        peers = self.folder / "peers.json"
        # Process 1: only the poll thread's snapshot write tightens the folder; nothing reads the token.
        with as_windows(security) as run:
            run.side_effect = icacls
            pl.write_snapshot(peers)({"peers": []})
            self.assertEqual(run.call_count, 1)
        self.assertTrue(acl["tight"] and peers.exists())
        self.assertFalse(self.secret.exists(), "the exposed token was deleted, not only noted in memory")
        self.assertFalse(record.exists())
        self.assertEqual(len(list(trusted.glob(f"{PEER}.json.quarantined-*"))), 1)
        self.assertEqual(pl.TrustStore(self.folder).quarantined, [PEER])
        # Process 2: a restart with nothing in memory (as_windows starts empty) and everything private now.
        with as_windows(security):
            fresh = pl.load_control_token(self.folder)
            self.assertEqual(pl.TrustStore(self.folder).load(), {})
        self.assertNotEqual(fresh, self.token)
        self.assertEqual(unsealed(self.secret.read_text().strip()), fresh)

    def test_windows_tightening_the_state_folder_retires_exposed_records_below_it(self):
        trusted, record = self.trust_record()
        outside = Path(tempfile.mkdtemp())
        (outside / "trusted").mkdir()
        elsewhere = outside / "trusted" / f"{PEER}.json"
        elsewhere.write_text(PEER_RECORD)
        try:
            (self.folder / "linked").symlink_to(outside, target_is_directory=True)
        except OSError:  # native Windows without the symlink privilege: the rest still runs
            pass
        acl = {"tight": False}

        def security(_target):
            return (ME, [ME]) if acl["tight"] else (ME, [ME, EVERYONE])

        def icacls(*_args, **_kwargs):
            acl["tight"] = True  # icacls on the state folder also re-inherits into trusted/ and its records
            return mock.Mock(returncode=0)

        store = pl.TrustStore(self.folder)
        with as_windows(security) as run:
            run.side_effect = icacls
            pl.load_control_token(self.folder)  # PeerLink.__init__ tightens the state folder before any trust read
            self.assertEqual(run.call_args_list[0].args[0][1], str(self.folder))
            self.assertEqual(store.load(), {})  # the record looks private now, but it was readable
        self.assertFalse(record.exists())
        self.assertEqual(store.quarantined, [PEER])
        self.assertTrue(elsewhere.exists(), "a link inside the state folder is never followed")
        self.assertEqual(list((outside / "trusted").glob("*.quarantined-*")), [])

    def test_windows_secret_that_cannot_be_retired_blocks_the_tightening(self):
        acl = {"tight": False}

        def security(_target):
            return (ME, [ME]) if acl["tight"] else (ME, [ME, EVERYONE])

        def icacls(*_args, **_kwargs):
            acl["tight"] = True
            return mock.Mock(returncode=0)

        with as_windows(security) as run:
            run.side_effect = icacls
            with mock.patch.object(pl.os, "unlink", side_effect=PermissionError("in use")):
                with self.assertRaises(PermissionError):
                    pl._restrict_dir(self.folder)
                with self.assertRaises(PermissionError):
                    pl.load_control_token(self.folder)
            run.assert_not_called()  # not tightened, so the exposure is still visible to the next attempt
            self.assertFalse(acl["tight"])
            self.assertEqual(unsealed(self.secret.read_text().strip()), self.token)
            fresh = pl.load_control_token(self.folder)  # retiring works now: deleted, tightened, replaced
            self.assertEqual(run.call_count, 1)
        self.assertNotEqual(fresh, self.token)

    def test_windows_exposed_trust_record_is_quarantined(self):
        trusted, record = self.trust_record()
        store = pl.TrustStore(self.folder)
        with as_windows(lambda target: (ME, [ME]) if isinstance(target, str) else (ME, [ME, OTHER])):
            self.assertEqual(store.load(), {})
        self.assertFalse(record.exists())
        self.assertEqual(store.quarantined, [PEER])
        FAKE_REGISTRY[PEER] = "f" * 32  # quarantine revoked the old generation; this is a fresh pairing
        record.write_text(json.dumps({**PEER_RECORD_PLAIN,
                                      "linkKey": sealed("ab" * 32 + ":" + "f" * 32, "agiw-peer link " + PEER)}))
        with as_windows(mock.Mock(return_value=(ME, [ME]))):
            self.assertIn(PEER, store.load())

    def test_windows_unsealed_or_foreign_sealed_token_is_never_used(self):
        # Daybreak round 7: a handle kept from before tightening can put back a file; it must not carry a
        # token the other account knows. Plain text, or a blob sealed by another account, is rotated away.
        private = mock.Mock(return_value=(ME, [ME]))
        for planted in ("a" * 64, sealed("b" * 64, "agiw-peer control-token", key=b"another account")):
            self.secret.write_text(planted + "\n")
            with as_windows(private):
                token = pl.load_control_token(self.folder)
            self.assertNotIn(token, ("a" * 64, "b" * 64))
            self.assertEqual(unsealed(self.secret.read_text().strip()), token)

    def test_windows_link_keys_are_sealed_and_bound_to_their_peer(self):
        private = mock.Mock(return_value=(ME, [ME]))
        store = pl.TrustStore(self.folder)
        with as_windows(private):
            store.pin(PEER, "pc", "windows", b"k" * 32)
            raw = json.loads((self.folder / "trusted" / f"{PEER}.json").read_text())
            self.assertTrue(raw["linkKey"].startswith(pl.SEAL_PREFIX))
            self.assertEqual(store.load()[PEER]["key"], b"k" * 32)
        other = "99999999-9999-4999-8999-999999999999"
        moved = {**raw, "nodeUuid": other}  # a valid blob, but sealed for PEER's record
        (self.folder / "trusted" / f"{other}.json").write_text(json.dumps(moved))
        plain = {**raw, "linkKey": "cd" * 32}
        (self.folder / "trusted" / f"{PEER}.json").write_text(json.dumps(plain))
        with as_windows(private):
            self.assertEqual(store.load(), {})
        names = os.listdir(self.folder / "trusted")
        self.assertEqual(len([n for n in names if ".quarantined-" in n]), 2)

    def test_windows_restored_record_after_unpair_or_quarantine_is_never_trusted(self):
        # Daybreak round 8: a genuine sealed record, put back unchanged after unpairing or quarantine, must not
        # bring that trust back (its former peer still knows the link key).
        private = mock.Mock(return_value=(ME, [ME]))
        store = pl.TrustStore(self.folder)
        record = self.folder / "trusted" / f"{PEER}.json"
        with as_windows(private):
            store.pin(PEER, "pc", "windows", b"k" * 32)
            original = record.read_bytes()
            self.assertIn(PEER, store.load())
            store.remove(PEER)                       # unpair
        record.write_bytes(original)                 # the exact old file, restored through an old handle
        with as_windows(private):
            self.assertEqual(store.load(), {}, "revoked in the registry, so the restored record is refused")
            store.pin(PEER, "pc", "windows", b"k" * 32)
            fresh = record.read_bytes()
            self.assertIn(PEER, store.load())
        record.write_bytes(original)                 # an older genuine record over a newer pairing
        with as_windows(private):
            self.assertEqual(store.load(), {}, "superseded generation")
        self.assertTrue(any(".quarantined-" in n for n in os.listdir(self.folder / "trusted")))
        self.assertNotIn(PEER, FAKE_REGISTRY, "quarantine revoked the generation too")
        self.assertNotEqual(fresh, original)

    def test_windows_record_hidden_under_another_name_is_revoked_by_a_widened_folder(self):
        # Daybreak round 9: rename trusted/<uuid>.json to stash.bin before the cleanup, put it back after.
        private = mock.Mock(return_value=(ME, [ME]))
        store = pl.TrustStore(self.folder)
        trusted = self.folder / "trusted"
        with as_windows(private):
            store.pin(PEER, "pc", "windows", b"k" * 32)
        os.rename(trusted / f"{PEER}.json", trusted / "stash.bin")
        acl = {"tight": False}

        def security(_target):
            return (ME, [ME]) if acl["tight"] else (ME, [ME, EVERYONE])

        def icacls(*_args, **_kwargs):
            acl["tight"] = True
            return mock.Mock(returncode=0)

        with as_windows(security) as run:
            run.side_effect = icacls
            pl._restrict_dir(self.folder)
        self.assertEqual(FAKE_REGISTRY, {}, "every pairing was revoked")
        os.rename(trusted / "stash.bin", trusted / f"{PEER}.json")  # restored through an old handle
        with as_windows(private):
            self.assertEqual(store.load(), {})

    def test_windows_failed_global_revocation_blocks_the_tightening(self):
        acl = {"tight": False}

        def security(_target):
            return (ME, [ME]) if acl["tight"] else (ME, [ME, EVERYONE])

        with as_windows(security) as run:
            with mock.patch.object(pl, "_gen_revoke_all", side_effect=PermissionError("registry denied")):
                with self.assertRaises(PermissionError):
                    pl._restrict_dir(self.folder)
            run.assert_not_called()

    def test_windows_unreadable_registry_gives_no_trust_but_keeps_the_record(self):
        private = mock.Mock(return_value=(ME, [ME]))
        store = pl.TrustStore(self.folder)
        with as_windows(private):
            store.pin(PEER, "pc", "windows", b"k" * 32)
            with mock.patch.object(pl, "_gen_get", side_effect=PermissionError("registry denied")):
                self.assertEqual(store.load(), {})
                self.assertEqual(store.registry_error, "PermissionError")
            self.assertTrue((self.folder / "trusted" / f"{PEER}.json").exists(), "not quarantined")
            self.assertIn(PEER, FAKE_REGISTRY, "not revoked")
            self.assertIn(PEER, store.load(), "trusted again once the registry answers")

    def test_windows_stale_load_cannot_revoke_a_newer_pairing(self):
        # Astra round 9: load reads the old record, someone unpairs and re-pairs, load must not quarantine the new one.
        private = mock.Mock(return_value=(ME, [ME]))
        store = pl.TrustStore(self.folder)
        with as_windows(private):
            store.pin(PEER, "pc", "windows", b"o" * 32)
            reading, proceed = pl.threading.Event(), pl.threading.Event()
            real_unseal = pl._unseal

            def slow_unseal(stored, purpose):
                reading.set()
                proceed.wait(2)
                return real_unseal(stored, purpose)

            def repair():
                reading.wait(2)
                store.remove(PEER)
                store.pin(PEER, "pc", "windows", b"n" * 32)

            other = pl.threading.Thread(target=repair)
            with mock.patch.object(pl, "_unseal", slow_unseal):
                other.start()
                loader = pl.threading.Thread(target=store.load)
                loader.start()
                time.sleep(0.3)
                proceed.set()
                loader.join(5)
            other.join(5)
            self.assertEqual(store.load()[PEER]["key"], b"n" * 32, "the newer pairing survived")

    def fake_winreg(self, values, fail_after=None, error=None):
        """A winreg stand-in: EnumValue(key, 0) returns the first remaining value, or raises OSError
        winerror 259 when none are left, or `error` once `fail_after` values were deleted."""
        deleted = []

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def enum_value(_key, _index):
            if fail_after is not None and len(deleted) >= fail_after:
                raise error
            if not values:
                exc = OSError("no more data")
                exc.winerror = 259
                raise exc
            return (next(iter(values)), "x" * 32, 1)

        def delete_value(_key, name):
            values.pop(name)
            deleted.append(name)

        return mock.Mock(HKEY_CURRENT_USER=0, KEY_QUERY_VALUE=1, KEY_SET_VALUE=2, OpenKey=lambda *a: Key(),
                         EnumValue=enum_value, DeleteValue=delete_value), deleted

    def test_real_revoke_all_drains_every_value_and_stops_only_on_no_more_items(self):
        values = {PEER: "a" * 32, "99999999-9999-4999-8999-999999999999": "b" * 32}
        fake, deleted = self.fake_winreg(values)
        with mock.patch.object(pl.os, "name", "nt"), mock.patch.dict(sys.modules, {"winreg": fake}):
            pl._gen_revoke_all()
        self.assertEqual(values, {})
        self.assertEqual(len(deleted), 2)

    def test_real_revoke_all_propagates_other_errors_even_after_partial_progress(self):
        for fail_after in (0, 1):
            values = {PEER: "a" * 32, "99999999-9999-4999-8999-999999999999": "b" * 32}
            fake, _ = self.fake_winreg(values, fail_after=fail_after, error=PermissionError("access denied"))
            with mock.patch.object(pl.os, "name", "nt"), mock.patch.dict(sys.modules, {"winreg": fake}):
                with self.assertRaises(PermissionError):
                    pl._gen_revoke_all()
            self.assertEqual(len(values), 2 - fail_after, "the remaining generations were not claimed revoked")

    def test_windows_failed_enumeration_blocks_the_tightening(self):
        values = {PEER: "a" * 32}
        fake, _ = self.fake_winreg(values, fail_after=0, error=PermissionError("access denied"))

        def security(_target):
            return (ME, [ME, EVERYONE])

        real_revoke = pl._gen_revoke_all
        with as_windows(security) as run, mock.patch.object(pl, "_gen_revoke_all", real_revoke), \
                mock.patch.dict(sys.modules, {"winreg": fake}):
            with self.assertRaises(PermissionError):
                pl._restrict_dir(self.folder)
            run.assert_not_called()
        self.assertEqual(values, {PEER: "a" * 32})

    def test_windows_decision_to_revoke_is_taken_under_the_lock(self):
        # Astra round 10: the folder's access list must be read after the lock is held.
        seen_locked = []

        def security(_target):
            seen_locked.append(pl._token_lock._is_owned())
            return (ME, [ME])

        with as_windows(security):
            pl._restrict_dir(self.folder)
        self.assertTrue(seen_locked and all(seen_locked))

    def test_windows_ace_parser_reads_allow_object_and_deny_entries(self):
        sid = bytes([1, 2, 0, 0, 0, 0, 0, 5]) + (32).to_bytes(4, "little") + (544).to_bytes(4, "little")
        mask, guid = (0x1F01FF).to_bytes(4, "little"), b"g" * 16

        def ace(kind, body):
            return bytes([kind, 0]) + (4 + len(body)).to_bytes(2, "little") + body

        def flags(value):
            return value.to_bytes(4, "little")

        self.assertEqual(pl._ace_allow_sid(ace(0x0, mask + sid)), sid)
        self.assertEqual(pl._ace_allow_sid(ace(0x9, mask + sid + b"condition")), sid)  # callback data follows
        self.assertEqual(pl._ace_allow_sid(ace(0x5, mask + flags(3) + guid + guid + sid)), sid)
        self.assertEqual(pl._ace_allow_sid(ace(0x5, mask + flags(2) + guid + sid)), sid)
        self.assertEqual(pl._ace_allow_sid(ace(0xB, mask + flags(0) + sid)), sid)
        for deny in (0x1, 0x6, 0xA, 0xC):
            self.assertIsNone(pl._ace_allow_sid(ace(deny, mask + sid)))
        for bad in (ace(0x4, mask + sid), ace(0x11, mask + sid), ace(0x0, mask + sid[:-1]), ace(0x0, mask),
                    ace(0x5, mask + flags(1) + sid), ace(0x0, mask + b"\x02" + sid[1:]), b"\x00\x00", b""):
            with self.assertRaises(pl.LooseSecret, msg=bad.hex()):
                pl._ace_allow_sid(bad)

    @unittest.skipUnless(sys.platform == "darwin", "macOS keeps the (inode, ctime) cache")
    def test_macos_keeps_the_cache_until_permissions_change(self):
        state = Path(tempfile.mkdtemp())
        pl.load_control_token(state)
        path = state / "control-token"

        def listings(run):
            return sum(1 for call in run.call_args_list if call.args[0][:2] == ["/bin/ls", "-le"])

        with mock.patch.object(pl.subprocess, "run", wraps=pl.subprocess.run) as run:
            pl._read_private(path)
            pl._read_private(path)
            self.assertEqual(listings(run), 1)
            os.chmod(path, 0o600)  # a permission edit, even to the same mode, moves ctime: checked again
            pl._read_private(path)
            self.assertEqual(listings(run), 2)


class TrustFolder(unittest.TestCase):
    def test_trust_folder_is_validated_before_it_is_listed(self):
        state = Path(tempfile.mkdtemp())
        (state / "trusted").mkdir()
        store = pl.TrustStore(state)
        order = []
        real_listdir = os.listdir
        trusted = str(state / "trusted")

        def listdir(path="."):
            if str(path) == trusted:
                order.append("list")  # only listings of the trust folder itself count
            return real_listdir(path)

        def refuse(_directory):
            order.append("validate")
            raise PermissionError("not a private folder")

        with mock.patch.object(pl, "_restrict_dir", refuse), mock.patch.object(pl.os, "listdir", listdir):
            self.assertEqual(store.load(), {})
        self.assertEqual(order, ["validate"], "a folder that fails validation is never listed")
        order.clear()
        with mock.patch.object(pl, "_restrict_dir", lambda _d: order.append("validate")), \
                mock.patch.object(pl.os, "listdir", listdir):
            store.load()
        self.assertEqual(order, ["validate", "list"])
        fresh = Path(tempfile.mkdtemp())
        self.assertEqual(pl.TrustStore(fresh).load(), {})
        self.assertFalse((fresh / "trusted").exists(), "loading creates nothing")

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_symlinked_trust_folder_yields_no_trust_and_is_left_alone(self):
        elsewhere = Path(tempfile.mkdtemp())
        node = "99999999-9999-4999-8999-999999999999"
        record = elsewhere / f"{node}.json"
        pl._write_private(record, json.dumps({"nodeUuid": node, "name": "pc", "host": "windows",
                                              "linkKey": "cd" * 32, "pinnedAt": 1}))
        state = Path(tempfile.mkdtemp())
        os.symlink(elsewhere, state / "trusted")
        with mock.patch.object(pl.Path, "glob", side_effect=AssertionError("listed before validation")):
            self.assertEqual(pl.TrustStore(state).load(), {})
        self.assertTrue(record.exists())


class FinishRace(unittest.TestCase):
    def test_concurrent_identical_finishes_both_get_the_ack_and_pin_once(self):
        mac = Node(self, "mac", "louis-mac")
        pc = Node(self, "windows", "louisaurorar12")
        pc.hello_to(mac)
        mac.hello_to(pc)
        pin = mac.link.invite("louisaurorar12")["pin"]
        invite_id = next(iter(mac.link._outbound))
        joiner = pl.Spake("joiner", pin, mac.link.me(), pc.link.me(), invite_id)
        code, reply = mac.link.on_start({**pc.link.me(), "invite": invite_id, "x": pl._hex(joiner.public)})
        self.assertEqual(code, 200)
        keys = joiner.finish(reply["y"])
        body = {"invite": invite_id, "attempt": reply["attempt"], "confirm": keys["joinerConfirm"]}

        inner = mac.link._lock
        first_out, second_out = pl.threading.Event(), pl.threading.Event()
        entries = {"first": 0, "second": 0}

        class Scheduled:
            """The link's own lock, with one rule: 'first' may not take it a second time until 'second'
            has held it once. A finish that looks up and transitions under two separate acquisitions
            lets 'second' miss the completion and answer 404; a single one cannot."""

            def __enter__(self):
                name = pl.threading.current_thread().name
                if name in entries:
                    entries[name] += 1
                    if name == "first" and entries[name] == 2:
                        second_out.wait(2)
                return inner.__enter__()

            def __exit__(self, *exc):
                result = inner.__exit__(*exc)
                name = pl.threading.current_thread().name
                if name in entries and entries[name] == 1:
                    (first_out if name == "first" else second_out).set()
                return result

        results = {}

        def finish(name):
            results[name] = mac.link.on_finish(dict(body))

        with mock.patch.object(mac.link, "_lock", Scheduled()), \
                mock.patch.object(mac.link.store, "pin", wraps=mac.link.store.pin) as pinned:
            first = pl.threading.Thread(target=finish, args=("first",), name="first", daemon=True)
            first.start()
            self.assertTrue(first_out.wait(2))
            second = pl.threading.Thread(target=finish, args=("second",), name="second", daemon=True)
            second.start()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        authentic = (200, {"paired": True, "ack": keys["finishAck"]})
        self.assertEqual(results, {"first": authentic, "second": authentic})
        self.assertEqual(pinned.call_count, 1)
        self.assertIn(pc.link.node, mac.link.trusted())


class CaseVariants(unittest.TestCase):
    """A default macOS volume and Windows are case-insensitive: a planted case variant opens for the canonical
    name. Variants are never written by this code, so they are never used."""

    def test_case_variant_control_token_is_not_used(self):
        state = Path(tempfile.mkdtemp())
        planted = "c" * 64
        variant = state / "Control-Token"
        fd = os.open(variant, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(planted + "\n")
        token = pl.load_control_token(state)
        self.assertNotEqual(token, planted)
        self.assertEqual([n for n in os.listdir(state) if n.casefold() == "control-token"], ["control-token"])

    def test_case_variant_trust_folder_or_record_yields_no_trust(self):
        mac = Node(self, "mac", "louis-mac")
        pc = Node(self, "windows", "louisaurorar12")
        pair(self, mac, pc)
        trusted = mac.dir / "trusted"
        record = trusted / f"{pc.link.node}.json"
        os.rename(record, trusted / "x.tmp")
        os.rename(trusted / "x.tmp", trusted / f"{pc.link.node.upper()}.JSON")
        self.assertEqual(pl.TrustStore(mac.dir).load(), {})
        self.assertTrue(any(".quarantined-" in n for n in os.listdir(trusted)))
        other = Node(self, "mac", "m2")
        pair(self, other, pc)
        os.rename(other.dir / "trusted", other.dir / "t.tmp")
        os.rename(other.dir / "t.tmp", other.dir / "TRUSTED")
        self.assertEqual(pl.TrustStore(other.dir).load(), {}, "a case-variant trust folder is never read")

    def test_trust_folder_whose_stored_name_differs_yields_no_trust_and_refuses_pins(self):
        # Simulates Windows resolving 'trusted' through an 8.3 alias to a folder stored under another name.
        mac = Node(self, "mac", "louis-mac")
        pc = Node(self, "windows", "louisaurorar12")
        pair(self, mac, pc)
        store = pl.TrustStore(mac.dir)
        real = pl.os.path.realpath
        alias = str(mac.dir / "trusted")

        def realpath(path, *a, **k):
            resolved = real(path, *a, **k)
            return os.path.join(os.path.dirname(resolved), "trust-container") if resolved == real(alias) else resolved

        with mock.patch.object(pl.os.path, "realpath", realpath):
            self.assertEqual(store.load(), {})
            with self.assertRaises(PermissionError):
                store.pin("12345678-1234-4234-8234-123456789abc", "x", "windows", b"k" * 32)
        self.assertFalse((mac.dir / "trusted" / "12345678-1234-4234-8234-123456789abc.json").exists())

    def test_pin_never_touches_a_linked_trust_folder(self):
        state = Path(tempfile.mkdtemp())
        outside = Path(tempfile.mkdtemp())
        node = "12345678-1234-4234-8234-123456789abc"
        victim = outside / f"{node.upper()}.JSON"
        victim.write_text("someone else's file")
        os.symlink(outside, state / "trusted")
        with self.assertRaises(PermissionError):
            pl.TrustStore(state).pin(node, "pc", "windows", b"k" * 32)
        self.assertTrue(victim.exists(), "nothing outside the trust store was listed or deleted")
        self.assertEqual(os.listdir(outside), [victim.name])

    def test_pairing_into_a_case_variant_trust_folder_fails_loudly(self):
        state = Path(tempfile.mkdtemp())
        (state / "TRUSTED").mkdir(mode=0o700)
        with self.assertRaises(PermissionError):
            pl.TrustStore(state).pin("12345678-1234-4234-8234-123456789abc", "pc", "windows", b"k" * 32)
        self.assertEqual(sorted(os.listdir(state / "TRUSTED")), [], "nothing was written that load() would refuse")

    def test_pin_clears_a_planted_case_variant_first(self):
        state = Path(tempfile.mkdtemp())
        store = pl.TrustStore(state)
        node = "12345678-1234-4234-8234-123456789abc"
        (state / "trusted").mkdir(mode=0o700)
        planted = state / "trusted" / f"{node.upper()}.JSON"
        fd = os.open(planted, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("{}")
        store.pin(node, "pc", "windows", b"k" * 32)
        self.assertEqual(sorted(os.listdir(state / "trusted")), [f"{node}.json"])
        self.assertIn(node, store.load())


class StatusDocs(unittest.TestCase):
    def test_both_shapes_and_rejections(self):
        self.assertEqual(pl.validate_status(status("windows"), "windows")["host"], "windows")
        presence = {**status("x"), "kind": "agiw-peer", "host": "louisaurorar12", "platform": "windows"}
        self.assertEqual(pl.validate_status(presence, "windows")["host"], "louisaurorar12")
        for doc in (status("mac"), status("windows", "running"), {**status("windows"), "schemaVersion": 2},
                    {**status("windows"), "models": [{}] * 33}, [], {**presence, "platform": "mac"}):
            with self.assertRaises(ValueError):
                pl.validate_status(doc, "windows")

    def test_stale_status_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps(status("windows")))
            self.assertEqual(pl.file_status(path)()["host"], "windows")
            old = time.time() - 60
            os.utime(path, (old, old))
            with self.assertRaises(ValueError):
                pl.file_status(path)()


if __name__ == "__main__":
    unittest.main()
