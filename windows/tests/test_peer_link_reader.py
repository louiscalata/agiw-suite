"""Dormant pairing-service reader: bounded local evidence, not a LAN or model action."""
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes, server  # noqa: E402

NOW = 1700000000.0
SELF = "11111111-1111-4111-8111-111111111111"
PEER = "22222222-2222-4222-8222-222222222222"


def iso(at):
    return dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def document():
    return {"schemaVersion": 1, "protocol": "agiw-peer/2", "observedAt": iso(NOW),
            "self": {"node": SELF, "name": "test-pc", "host": "windows"},
            "peers": [{"node": PEER, "name": "test-mac", "host": "mac", "paired": True,
                       "state": "connected", "status": {
                           "schemaVersion": 1, "host": "mac", "observedAt": iso(NOW),
                           "models": [{"id": "m", "loadedState": "loaded", "activity": "idle"}]}}]}


class PeerLinkFixedPath(unittest.TestCase):
    def test_only_the_default_local_service_path_is_used(self):
        base = Path("C:/test-appdata")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(base)}):
            self.assertEqual(probes.peer_link_status_path(), base / "AGIW" / "peer-link" / "peers.json")
        with mock.patch.dict(os.environ), mock.patch.object(Path, "home", return_value=Path("C:/test-home")):
            os.environ.pop("LOCALAPPDATA", None)
            self.assertEqual(probes.peer_link_status_path(),
                             Path("C:/test-home/AppData/Local/AGIW/peer-link/peers.json"))


class PeerLinkReader(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "peers.json"
        patch = mock.patch.object(probes, "peer_link_status_path", return_value=self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def read(self, doc=None, now=NOW):
        self.path.write_text(json.dumps(document() if doc is None else doc), encoding="utf-8")
        return probes.read_peer_link(now, enabled=True)

    def test_off_never_reads_the_filesystem_or_adds_a_source(self):
        with mock.patch.object(probes, "_read_peer_link_file") as read:
            for enabled in (False, None, 1, "true"):
                self.assertEqual(probes.read_peer_link(NOW, enabled=enabled), ([], None))
            read.assert_not_called()

    def test_absent_service_is_unavailable_without_exposing_a_path(self):
        rows, source = probes.read_peer_link(NOW, enabled=True)
        self.assertEqual(rows, [])
        self.assertEqual(source["state"], "unavailable")
        self.assertEqual(source["detail"], "Pairing service status unavailable (no peers.json)")
        self.assertNotIn(self.temp.name, json.dumps(source))

    def test_connected_status_is_attributed_and_filtered_at_every_depth(self):
        doc = document()
        doc.update(invitesShowingPin=[{"pin": "123456"}], quarantined=["secret"], pollError="secret")
        peer = doc["peers"][0]
        peer.update(address="secret", error="secret", inviteWaiting=True, lastHello=123456)
        status = peer["status"]
        status.update(secret="secret", gpus=["secret"])
        status["models"][0].update(prompt="secret", host="mac", path="secret", activity="busy")
        status["components"] = [{"id": "nisi", "label": "Nisi", "state": "ready", "detail": "ok",
                                 "pin": "123456", "lastJudgedAgeSeconds": 9},
                                {"id": "secrets", "detail": "secret"}]
        rows, source = self.read(doc)
        self.assertEqual(rows, [{"node": PEER, "name": "test-mac", "host": "mac", "paired": True,
                                 "state": "connected", "status": {
                                     "schemaVersion": 1, "host": "mac", "observedAt": iso(NOW),
                                     "models": [{"id": "m", "loadedState": "loaded", "activity": "busy"}],
                                     "components": [{"id": "nisi", "label": "Nisi", "state": "ready", "detail": "ok"}]}}])
        self.assertEqual(source["state"], "live")
        self.assertIn("Pairing service reports 1 paired, 1 connected (authenticated)", source["detail"])
        self.assertNotIn("secret", json.dumps((rows, source)))
        self.assertNotIn("123456", json.dumps((rows, source)))

    def test_bad_header_identity_or_root_is_unknown(self):
        for changes in ({"schemaVersion": True}, {"schemaVersion": 2}, {"protocol": "agiw-peer/3"},
                        {"peers": {}}, {"self": None}, {"self": {"host": "mac", "node": SELF}},
                        {"self": {"host": "windows", "node": "invalid"}}):
            with self.subTest(changes=changes):
                rows, source = self.read({**document(), **changes})
                self.assertEqual(rows, [])
                self.assertEqual(source["state"], "unavailable")
        for root in ([], True, "file", 1):
            with self.subTest(root=root):
                self.assertEqual(self.read(root)[0], [])

    def test_oversize_strict_json_and_deep_input_cannot_lose_a_sample(self):
        valid = json.dumps(document())
        for raw in (valid + " " * probes.PEER_LINK_MAX_BYTES, "{", "[NaN]", "[Infinity]",
                    '{"schemaVersion":1,"schemaVersion":2}', "[" * 2000 + "0" + "]" * 2000):
            with self.subTest(size=len(raw)):
                self.path.write_text(raw, encoding="utf-8")
                rows, source = probes.read_peer_link(NOW, enabled=True)
                self.assertEqual(rows, [])
                self.assertEqual(source["state"], "unavailable")

    def test_outer_clock_boundaries_expire_before_rounding_and_clear_all_claims(self):
        for age in (-5, 0, 30, -5.001, 30.001):
            doc = document()
            doc["observedAt"] = iso(NOW - age)
            with self.subTest(age=age):
                rows, source = self.read(doc)
                fresh = -5 <= age <= 30
                self.assertEqual(rows[0]["state"], "connected" if fresh else "unknown")
                self.assertEqual(source["state"], "live" if fresh else "unavailable")
                self.assertEqual(rows[0]["status"] is not None, fresh)
        for clock in (None, True, 1, "", "2023-11-14T22:13:20", "99999-01-01T00:00:00Z"):
            doc = document()
            doc["observedAt"] = clock
            with self.subTest(clock=clock):
                rows, source = self.read(doc)
                self.assertEqual((rows[0]["state"], rows[0]["status"], source["state"]),
                                 ("unknown", None, "unavailable"))

    def test_nested_clock_must_be_fresh_independently_of_the_outer_report(self):
        for age in (-5, 0, 30, -5.001, 30.001, 60):
            doc = document()
            doc["peers"][0]["status"]["observedAt"] = iso(NOW - age)
            with self.subTest(age=age):
                rows, source = self.read(doc)
                self.assertEqual(rows[0]["state"], "connected" if -5 <= age <= 30 else "unknown")
                self.assertEqual(rows[0]["status"] is not None, -5 <= age <= 30)
                self.assertEqual(source["state"], "live")  # the service report itself is fresh
        doc = document()
        doc["peers"][0]["status"]["observedAt"] = "2023-11-14T22:13:20"
        self.assertIsNone(self.read(doc)[0][0]["status"])

    def test_pairing_flag_requires_exact_true_and_canonical_host(self):
        for paired in (False, 1, "true", None, [], {}):
            doc = document()
            doc["peers"][0]["paired"] = paired
            with self.subTest(paired=paired):
                rows, source = self.read(doc)
                self.assertEqual((rows[0]["state"], rows[0]["status"]), ("unknown", None))
                self.assertIn("0 connected", source["detail"])
        for host in (None, "bogus", "unknown", [], {}):
            doc = document()
            doc["peers"][0]["host"] = host
            doc["peers"][0]["status"]["host"] = "unknown"
            with self.subTest(host=host):
                rows, source = self.read(doc)
                self.assertEqual((rows[0]["state"], rows[0]["status"]), ("unknown", None))
                self.assertIn("0 connected", source["detail"])

    def test_nonconnected_or_unknown_states_never_keep_residency_or_activity(self):
        for state in ("available", "connecting", "stale", "offline", "CONNECTED", "connected\n", [], {}):
            doc = document()
            doc["peers"][0]["state"] = state
            with self.subTest(state=state):
                rows, _source = self.read(doc)
                self.assertIsNone(rows[0]["status"])
                self.assertEqual(rows[0]["state"], state if state in probes.PEER_LINK_STATES else "unknown")
        doc = document()
        doc["observedAt"] = iso(NOW - 31)
        doc["peers"][0]["state"] = "offline"
        self.assertEqual(self.read(doc)[0][0]["state"], "unknown")

    def test_invalid_nested_models_or_header_void_only_the_peer_status(self):
        for missing_or_invalid in (None, True, [], {}):
            doc = document()
            doc["peers"][0]["status"] = missing_or_invalid
            with self.subTest(status=missing_or_invalid):
                rows, source = self.read(doc)
                self.assertEqual((rows[0]["state"], rows[0]["status"]), ("unknown", None))
                self.assertIn("0 connected", source["detail"])
        for changes in ({"schemaVersion": True}, {"host": "windows"}, {"models": {}},
                        {"models": [{"id": "m", "loadedState": "busy"}]},
                        {"models": [{"id": " ", "loadedState": "loaded"}]},
                        {"models": [{"id": "m", "host": "windows", "loadedState": "loaded"}]},
                        {"models": [{"id": "m", "loadedState": "loaded"}] * 33}):
            doc = document()
            doc["peers"][0]["status"].update(changes)
            with self.subTest(changes=changes):
                rows, source = self.read(doc)
                self.assertEqual((len(rows), rows[0]["state"], rows[0]["status"]), (1, "unknown", None))
                self.assertEqual(source["state"], "live")
        doc = document()
        status = doc["peers"][0]["status"]
        status.update(kind="agiw-peer", host="test-mac", platform="mac")
        self.assertEqual(self.read(doc)[0][0]["status"]["host"], "mac")

    def test_text_and_row_budgets_and_duplicate_identities(self):
        doc = document()
        peer = doc["peers"][0]
        peer["name"] = "x\x00" * 100
        peer["status"]["models"][0]["id"] = "m" * 400
        peer["status"]["models"][0]["activity"] = "busy\n"
        peer["status"]["components"] = [{"id": "jev", "detail": "x\t" * 200}] * 6
        rows, _source = self.read(doc)
        self.assertEqual(rows[0]["name"], "x" * 64)
        self.assertEqual(rows[0]["status"]["models"][0], {"id": "m" * 256, "loadedState": "loaded"})
        self.assertEqual(len(rows[0]["status"]["components"]), 4)
        self.assertEqual(rows[0]["status"]["components"][0]["detail"], "x" * 128)
        doc = document()
        doc["peers"] = [{**doc["peers"][0], "node": f"{i:08x}-0000-4000-8000-000000000000"} for i in range(33)]
        self.assertEqual(len(self.read(doc)[0]), 32)
        doc = document()
        doc["peers"] += [None, {"node": "bad"}, copy.deepcopy(doc["peers"][0])]
        rows, source = self.read(doc)
        self.assertEqual((len(rows), rows[0]["state"], rows[0]["status"]), (1, "unknown", None))
        self.assertIsNone(rows[0]["paired"])
        self.assertIn("0 paired", source["detail"])
        self.assertIn("0 connected", source["detail"])
        for flags in ((True, False), (False, True)):
            doc = document()
            first = doc["peers"][0]
            first["paired"] = flags[0]
            doc["peers"].append({**copy.deepcopy(first), "paired": flags[1]})
            with self.subTest(flags=flags):
                rows, source = self.read(doc)
                self.assertIsNone(rows[0]["paired"])
                self.assertEqual((rows[0]["state"], rows[0]["status"]), ("unknown", None))
                self.assertIn("0 paired, 0 connected", source["detail"])

    def test_atomic_replacement_is_reopened_then_expired_and_recovered(self):
        self.read()
        doc = document()
        doc["peers"][0]["status"]["models"][0]["loadedState"] = "inactive"
        replacement = self.path.with_suffix(".new")
        replacement.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(replacement, self.path)
        self.assertEqual(probes.read_peer_link(NOW, enabled=True)[0][0]["status"]["models"][0]["loadedState"], "inactive")
        rows, _source = probes.read_peer_link(NOW + 30.001, enabled=True)
        self.assertEqual((rows[0]["state"], rows[0]["status"]), ("unknown", None))
        self.assertEqual(self.read()[0][0]["state"], "connected")

    def test_a_platform_directory_symlink_does_not_void_the_report(self):
        ancestor = self.path.parent.parent
        def is_link(candidate):
            return Path(candidate) == ancestor
        with mock.patch.object(probes, "is_link_or_junction", side_effect=is_link), \
                mock.patch.object(probes, "_platform_directory_symlink", side_effect=lambda p: Path(p) == ancestor):
            rows, source = self.read()
        self.assertEqual(rows[0]["state"], "connected")
        self.assertEqual(source["state"], "live")

    def test_a_non_platform_ancestor_symlink_refuses_the_report(self):
        ancestor = self.path.parent.parent
        def is_link(candidate):
            return Path(candidate) == ancestor
        with mock.patch.object(probes, "is_link_or_junction", side_effect=is_link), \
                mock.patch.object(Path, "open") as opened:
            rows, source = probes.read_peer_link(NOW, enabled=True)
        self.assertEqual(rows, [])
        self.assertEqual(source["state"], "unavailable")
        opened.assert_not_called()

    def test_reparse_final_and_ancestor_or_redirected_handle_is_refused(self):
        self.read()
        for linked in (self.path, self.path.parent):
            with self.subTest(linked=linked), mock.patch.object(probes, "is_link_or_junction", side_effect=lambda p: p == linked), \
                    mock.patch.object(Path, "open") as opened:
                rows, source = probes.read_peer_link(NOW, enabled=True)
                self.assertEqual(rows, [])
                self.assertEqual(source["state"], "unavailable")
                opened.assert_not_called()
        with mock.patch.object(probes, "_peer_link_opened_path", side_effect=ValueError("redirected")):
            self.assertEqual(probes.read_peer_link(NOW, enabled=True)[0], [])

    def test_server_config_is_exact_opt_in_and_default_is_off(self):
        config = self.path.with_name("config.json")
        for value in (None, False, 1, "true", True):
            config.write_text(json.dumps({"peerLinkReader": value}), encoding="utf-8")
            with self.subTest(value=value), mock.patch.object(server, "Collector") as collector, \
                    mock.patch("sys.stdout", new=io.StringIO()):
                collector.return_value.collect.return_value = {"host": "windows"}
                server.main(["--once", "--no-lan", "--config", str(config)])
                self.assertEqual(collector.call_args.kwargs["peer_link_reader"], value is True)


if __name__ == "__main__":
    unittest.main()
