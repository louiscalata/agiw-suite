"""Check pairing cancellation omissions and interleavings with in-memory mutations.

Run with ``python -B -m test_support.peer_link_revocation_harness``.
No mutation writes source, contacts a peer, or accesses a persistent trust store.
"""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import peer_link
import test_peer_link as existing
import test_peer_link_revocation as regression
from test_peer_link_security import MemoryTrust


def _mutations():
    check = 'if self._inbound.get(invite["peer"]["node"]) is not pending or self.clock() > invite["expires"]:'
    save = (
        """        with self._lock:
            # A captured invitation may have been revoked or replaced while the network replied.
            # Validate its identity and save under the same lock that orders unpair.
            """
        + check
        + """
                raise PermissionError("pairing was revoked or expired before it could be saved")
"""
    )
    return [
        (
            "pending outbound cancellation omitted",
            """            for invite, pending in list(self._outbound.items()):
                if pending["peer"]["node"] == matches[0]:
                    del self._outbound[invite]
""",
            "",
            "test_pending_outbound_exact_revocation_witness",
        ),
        (
            "pending inbound cancellation omitted",
            "            self._inbound.pop(matches[0], None)\n",
            "",
            "test_captured_accept_exact_revocation_witness",
        ),
        ("accept transaction check omitted", check, "if False:", "test_captured_accept_exact_revocation_witness"),
        (
            "accept checks node membership instead of transaction identity",
            check,
            'if invite["peer"]["node"] not in self._inbound or self.clock() > invite["expires"]:',
            "test_same_id_new_invitation_does_not_validate_captured_accept",
        ),
        (
            "accept check moved before save lock",
            save,
            "        "
            + check
            + """
            raise PermissionError("pairing was revoked or expired before it could be saved")
        with self._lock:
""",
            "test_accept_checks_identity_at_locked_save_boundary",
        ),
        (
            "accept save lock omitted",
            save,
            save.replace("with self._lock:", "if True:", 1),
            "test_accept_save_before_overlapping_unpair_is_ordered",
        ),
        (
            "cancelled start snapshot accepted",
            'self._outbound.get(record["id"]) is not record',
            "False",
            "test_start_cannot_publish_keys_after_unpair",
        ),
    ]


def mutation_witnesses():
    path = Path(peer_link.__file__)
    original = path.read_text(encoding="utf-8")
    rows = []
    for label, before, after, witness in _mutations():
        if original.count(before) != 1:
            raise AssertionError(("mutation anchor must be unique", label))
        module = types.ModuleType("peer_revocation_mutation")
        module.__file__ = str(path)
        exec(compile(original.replace(before, after, 1), str(path), "exec"), module.__dict__)
        result = unittest.TestResult()
        suite = unittest.defaultTestLoader.loadTestsFromName("PendingRevocationTests." + witness, regression)
        with mock.patch.object(regression, "pl", module):
            suite.run(result)
        if result.testsRun != 1 or result.errors or not result.failures:
            raise AssertionError(("mutation lacks assertion witness", label, result.errors, result.failures))
        rows.append({"mutation": label, "test": witness, "assertion_failures": len(result.failures), "errors": 0})
    if path.read_text(encoding="utf-8") != original:
        raise AssertionError("mutation changed source bytes")
    return rows


def existing_race_controls():
    """Run the existing save/retry races with memory nodes instead of listeners."""
    endpoints = {}

    class MemoryNode:
        def __init__(self, case, host, name):
            index = len(endpoints) + 1
            node = f"{index:08x}-1111-4111-8111-111111111111"
            folder = tempfile.TemporaryDirectory()
            case.addCleanup(folder.cleanup)
            self.dir = Path(folder.name)
            with (
                mock.patch.object(peer_link, "load_identity", return_value=node),
                mock.patch.object(peer_link, "load_control_token", return_value="a" * 64),
                mock.patch.object(peer_link, "TrustStore", return_value=MemoryTrust()),
            ):
                self.link = peer_link.PeerLink(self.dir, host, name, lambda: {}, status_port=10000 + index)
            endpoints[self.link.status_port] = self.link

        def hello_to(self, other):
            other.link.handle_datagram(
                peer_link.make_hello(
                    self.link.node, self.link.host, self.link.name, self.link.status_port, peer_link.time.time()
                ),
                "192.168.1.2",
            )

    def transport(_address, port, _method, path, _headers, body, _limit, _deadline=None):
        link = endpoints[port]
        document = peer_link.strict_json(body)
        if path.endswith("/notify"):
            code, reply = link.on_notify(document, "192.168.1.2")
        elif path.endswith("/start"):
            code, reply = link.on_start(document)
        elif path.endswith("/finish"):
            code, reply = link.on_finish(document)
        else:
            raise AssertionError("unexpected fixture route")
        return code, {}, peer_link.canonical(reply)

    names = [
        "FinishRace.test_concurrent_identical_finishes_both_get_the_ack_and_pin_once",
        "Hardening3.test_retry_during_the_inviters_save_waits_and_gets_the_ack",
    ]
    result = unittest.TestResult()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name, existing) for name in names)
    with mock.patch.object(existing, "Node", MemoryNode), mock.patch.object(peer_link, "_http", transport):
        suite.run(result)
    if result.testsRun != len(names) or result.errors or result.failures or result.skipped:
        raise AssertionError(
            ("existing memory-transport race controls", result.errors, result.failures, result.skipped)
        )
    return {"tests_run": result.testsRun, "failures": 0, "errors": 0, "skips": 0, "tests": names}


if __name__ == "__main__":
    sys.stdout.write(
        json.dumps({"mutations": mutation_witnesses(), "existing_race_controls": existing_race_controls()}, indent=2)
        + "\n"
    )
