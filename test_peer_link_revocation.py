"""Pairing revocation with synthetic keys, an in-memory store and mocked transport."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import peer_link as pl
from test_peer_link_security import LOCAL, REMOTE, MemoryTrust

OTHER = "33333333-3333-4333-8333-333333333333"
INVITE = "b" * 32
KEYS = {"inviterConfirm": "c" * 64, "joinerConfirm": "d" * 64, "finishAck": "e" * 64, "link": b"n" * 32}


class BoundaryLock:
    """Expose the next outer critical section without relying on thread scheduling."""

    def __init__(self, before=None, after=None):
        self.lock = threading.RLock()
        self.depth = 0
        self.entries = 0
        self.before, self.after = before, after
        self.fired = False

    def __enter__(self):
        if self.depth == 0:
            self.entries += 1
            if self.entries == 2 and self.before and not self.fired:
                self.fired = True
                self.before()
        self.lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, *_):
        self.depth -= 1
        self.lock.release()
        if self.depth == 0 and self.entries == 2 and self.after and not self.fired:
            self.fired = True
            self.after()


class PendingRevocationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = MemoryTrust()
        with (
            mock.patch.object(pl, "load_identity", return_value=LOCAL),
            mock.patch.object(pl, "load_control_token", return_value="a" * 64),
            mock.patch.object(pl, "TrustStore", return_value=self.store),
        ):
            self.link = pl.PeerLink(Path(folder.name), "mac", "mac", lambda: {}, clock=lambda: 100.0)
        self.store.pin(REMOTE, "pc", "windows", b"o" * 32)
        self.link._seen[REMOTE] = {
            "address": "192.0.2.1",
            "lastHello": 100.0,
            "name": "pc",
            "host": "windows",
            "port": 8768,
        }

    def outbound(self, node=REMOTE, invite=INVITE):
        self.link._outbound[invite] = {
            "id": invite,
            "expires": 200.0,
            "attempt": "f" * 32,
            "attempts": 0,
            "pin": "123456",
            "spake": dict(KEYS),
            "peer": {"node": node, "name": "pc", "host": "windows"},
        }
        return {"invite": invite, "attempt": "f" * 32, "confirm": KEYS["joinerConfirm"]}

    def notify(self, node=REMOTE, invite=INVITE):
        self.link._seen.setdefault(node, {"address": "192.0.2.1", "lastHello": 100.0})
        result = self.link.on_notify(
            {"node": node, "host": "windows", "name": "pc", "invite": invite, "port": 8768}, "192.0.2.1"
        )
        self.assertEqual(result, (200, {"waiting": True}))
        return self.link._inbound[node]

    def accept(self, during=None, ack=None):
        spake = mock.Mock(public=7)
        spake.finish.return_value = dict(KEYS)

        def exchange(*args, **_kwargs):
            lock = self.link._lock
            if isinstance(lock, BoundaryLock):
                self.assertEqual(lock.depth, 0, "transport waited while holding the pairing lock")
            phase = args[3].rsplit("/", 1)[1]
            if during:
                during(phase)
            if phase == "start":
                return 200, {}, pl.canonical({"y": "mocked", "attempt": "f" * 32, "confirm": KEYS["inviterConfirm"]})
            self.assertEqual(phase, "finish")
            return 200, {}, pl.canonical({"ack": KEYS["finishAck"] if ack is None else ack})

        with mock.patch.object(pl, "Spake", return_value=spake), mock.patch.object(pl, "_http", side_effect=exchange):
            return self.link.accept(REMOTE, "123456")

    def assert_no_trust(self):
        self.assertNotIn(REMOTE, self.link.trusted())

    def test_pending_outbound_exact_revocation_witness(self):
        body = self.outbound()
        self.assertTrue(self.link.trusted())
        self.assertEqual(self.link.unpair(REMOTE), {"unpaired": REMOTE})
        self.assert_no_trust()
        self.assertNotIn(INVITE, self.link._outbound)
        code, reply = self.link.on_finish(body)
        self.assertNotEqual(code, 200)
        self.assertFalse(reply.get("paired", False))
        self.assert_no_trust()
        self.assertEqual(self.link._completed, {})

    def test_captured_accept_exact_revocation_witness(self):
        self.notify()

        def revoke(phase):
            if phase == "finish":
                self.link.unpair(REMOTE)
                self.assert_no_trust()

        with mock.patch.object(self.store, "pin", wraps=self.store.pin) as pin:
            with self.assertRaisesRegex(PermissionError, "revoked"):
                self.accept(revoke)
            pin.assert_not_called()
        self.assert_no_trust()
        self.assertNotIn(REMOTE, self.link._inbound)

    def test_accept_revoked_during_start_cannot_save_authentic_finish(self):
        self.notify()

        def revoke(phase):
            if phase == "start":
                self.link.unpair(REMOTE)

        with self.assertRaisesRegex(PermissionError, "revoked"):
            self.accept(revoke)
        self.assert_no_trust()

    def test_accept_checks_identity_at_locked_save_boundary(self):
        self.notify()
        lock = BoundaryLock(before=lambda: self.link.unpair(REMOTE))
        with mock.patch.object(self.link, "_lock", lock):
            with self.assertRaisesRegex(PermissionError, "revoked"):
                self.accept()
        self.assertTrue(lock.fired)
        self.assert_no_trust()

    def test_accept_save_before_overlapping_unpair_is_ordered(self):
        self.notify()
        events = []
        original_pin, original_remove = self.store.pin, self.store.remove
        lock = BoundaryLock(after=lambda: self.link.unpair(REMOTE))

        def pin(*args):
            self.assertGreater(lock.depth, 0, "trust save must share the unpair lock")
            events.append("pin")
            return original_pin(*args)

        def remove(*args):
            self.assertGreater(lock.depth, 0)
            events.append("remove")
            return original_remove(*args)

        with (
            mock.patch.object(self.link, "_lock", lock),
            mock.patch.object(self.store, "pin", side_effect=pin),
            mock.patch.object(self.store, "remove", side_effect=remove),
        ):
            self.assertEqual(self.accept(), {"paired": True, "peer": "pc"})
        self.assertTrue(lock.fired)
        self.assertEqual(events, ["pin", "remove"])
        self.assert_no_trust()

    def test_same_id_new_invitation_does_not_validate_captured_accept(self):
        pending = self.notify()

        def replace(phase):
            if phase == "finish":
                self.link.unpair(REMOTE)
                self.notify()

        with self.assertRaisesRegex(PermissionError, "revoked"):
            self.accept(replace)
        self.assertIsNot(self.link._inbound[REMOTE], pending)
        self.assert_no_trust()
        self.assertEqual(self.accept(), {"paired": True, "peer": "pc"})
        self.assertEqual(self.link.trusted()[REMOTE]["key"], KEYS["link"])

    def test_replaced_invitation_without_unpair_cancels_captured_accept(self):
        self.notify()

        def replace(phase):
            if phase == "finish":
                self.notify(invite="a" * 32)

        with self.assertRaisesRegex(PermissionError, "revoked"):
            self.accept(replace)
        self.assertEqual(self.link.trusted()[REMOTE]["key"], b"o" * 32)
        self.assertEqual(self.link._inbound[REMOTE]["invite"], "a" * 32)

    def test_expired_captured_accept_does_not_save(self):
        self.notify()

        def expire(phase):
            if phase == "finish":
                self.link.clock = lambda: 1000.0

        with self.assertRaisesRegex(PermissionError, "expired"):
            self.accept(expire)
        self.assertEqual(self.link.trusted()[REMOTE]["key"], b"o" * 32)

    def test_forged_ack_never_saves_trust(self):
        self.notify()
        with self.assertRaisesRegex(PermissionError, "not authentic"):
            self.accept(ack="0" * 64)
        self.assertEqual(self.link.trusted()[REMOTE]["key"], b"o" * 32)

    def test_fresh_inbound_pairing_after_unpair_succeeds(self):
        self.notify()
        self.link.unpair(REMOTE)
        self.notify(invite="a" * 32)
        self.assertEqual(self.accept(), {"paired": True, "peer": "pc"})
        self.assertEqual(self.link.trusted()[REMOTE]["key"], KEYS["link"])
        self.assertNotIn(REMOTE, self.link._inbound)

    def test_fresh_outbound_pairing_after_unpair_and_old_finish(self):
        old = self.outbound()
        self.link.unpair(REMOTE)
        with mock.patch.object(pl, "_http", return_value=(200, {}, b"")):
            self.link.invite(REMOTE)
        invite = next(iter(self.link._outbound))
        spake = mock.Mock(public=7)
        spake.finish.return_value = dict(KEYS)
        with mock.patch.object(pl, "Spake", return_value=spake):
            code, reply = self.link.on_start({**self.link._outbound[invite]["peer"], "invite": invite, "x": "mocked"})
        self.assertEqual(code, 200)
        body = {"invite": invite, "attempt": reply["attempt"], "confirm": KEYS["joinerConfirm"]}
        self.assertEqual(self.link.on_finish(body), (200, {"paired": True, "ack": KEYS["finishAck"]}))
        self.assertEqual(self.link.on_finish(body), (200, {"paired": True, "ack": KEYS["finishAck"]}))
        self.assertNotEqual(self.link.on_finish(old)[0], 200)
        self.assertEqual(self.link.trusted()[REMOTE]["key"], KEYS["link"])

    def test_unpair_preserves_other_peer_transactions_and_trust(self):
        self.store.pin(OTHER, "other", "windows", KEYS["link"])
        self.outbound()
        other_body = self.outbound(OTHER, "a" * 32)
        self.notify()
        other_inbound = self.notify(OTHER, "a" * 32)
        self.link.unpair(REMOTE)
        self.assertEqual(self.link.trusted()[OTHER]["key"], KEYS["link"])
        self.assertIs(self.link._inbound[OTHER], other_inbound)
        self.assertEqual(self.link.on_finish(other_body), (200, {"paired": True, "ack": KEYS["finishAck"]}))
        self.assert_no_trust()

    def test_unpair_frees_only_target_inbound_slot(self):
        pending = {REMOTE: self.notify()}
        for index in range(pl.MAX_INBOUND_INVITES - 1):
            node = f"{index + 3:08x}-3333-4333-8333-333333333333"
            pending[node] = self.notify(node, f"{index + 1:032x}")
        extra = "99999999-3333-4333-8333-333333333333"
        self.link._seen[extra] = {"address": "192.0.2.1", "lastHello": 100.0}
        body = {"node": extra, "name": "pc", "host": "windows", "invite": "a" * 32, "port": 8768}
        self.assertEqual(self.link.on_notify(body, "192.0.2.1")[0], 429)
        self.link.unpair(REMOTE)
        self.assertEqual(self.link.on_notify(body, "192.0.2.1")[0], 200)
        self.assertEqual(len(self.link._inbound), pl.MAX_INBOUND_INVITES)
        for node, transaction in pending.items():
            if node != REMOTE:
                self.assertIs(self.link._inbound[node], transaction)

    def test_unpair_preserves_other_peer_completed_retry(self):
        body = self.outbound(OTHER, "a" * 32)
        self.assertEqual(self.link.on_finish(body)[0], 200)
        done = self.link._completed[body["invite"]]
        self.link.unpair(REMOTE)
        self.assertIs(self.link._completed[body["invite"]], done)
        self.assertEqual(self.link.on_finish(body), (200, {"paired": True, "ack": KEYS["finishAck"]}))

    def test_failed_unpair_keeps_pending_transactions(self):
        self.outbound()
        pending = self.notify()
        with mock.patch.object(self.store, "remove", side_effect=PermissionError("fixture refusal")):
            with self.assertRaisesRegex(PermissionError, "fixture refusal"):
                self.link.unpair(REMOTE)
        self.assertIs(self.link._inbound[REMOTE], pending)
        self.assertIn(INVITE, self.link._outbound)
        self.assertEqual(self.link.trusted()[REMOTE]["key"], b"o" * 32)

    def test_start_cannot_publish_keys_after_unpair(self):
        self.outbound()
        spake = mock.Mock(public=7)

        def finish(_):
            self.link.unpair(REMOTE)
            return dict(KEYS)

        spake.finish.side_effect = finish
        with mock.patch.object(pl, "Spake", return_value=spake):
            code, reply = self.link.on_start({"node": REMOTE, "name": "pc", "host": "windows", "invite": INVITE})
        self.assertEqual(code, 409)
        self.assertNotIn("confirm", reply)
        self.assert_no_trust()

    def test_start_cannot_publish_keys_for_replaced_invitation(self):
        self.outbound()
        spake = mock.Mock(public=7)

        def finish(_):
            self.outbound()
            return dict(KEYS)

        spake.finish.side_effect = finish
        with mock.patch.object(pl, "Spake", return_value=spake):
            code, reply = self.link.on_start({"node": REMOTE, "name": "pc", "host": "windows", "invite": INVITE})
        self.assertEqual(code, 409)
        self.assertNotIn("confirm", reply)
