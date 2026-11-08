"""PUBCLI round 5 regressions; no listeners, native services or real account data."""
import ctypes
import json
import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import ROOT, pl, temp_dir
from agiw import cli, contract, doctor, service
from test_contract_adapter import LOCAL_NODE, NODE, NOW, document, peer, snapshot
import test_loopback_pairing as pairing


class Fix5(unittest.TestCase):
    def cli_status(self, source):
        lines = []
        with mock.patch.object(cli, "control", return_value=source), \
                mock.patch.object(contract.time, "time", return_value=NOW):
            code = cli.main(["--state-dir", "unused", "status", "--json"], out=lines.append)
        self.assertEqual(code, 0)
        return json.loads("\n".join(lines))

    def test_c1_schema_version_requires_exact_int_in_both_shapes(self):
        for presence in (True, False):
            doc = document()
            if not presence:
                doc.pop("kind")
                doc.pop("platform")
                doc["host"] = "windows"
            for value in (True, 1.0, "1"):
                with self.subTest(presence=presence, value=repr(value)):
                    with self.assertRaises(ValueError):
                        pl.validate_status(dict(doc, schemaVersion=value), "windows")
            self.assertIs(pl.validate_status(doc, "windows"), doc)

    def test_c1_boolean_peer_schema_is_unknown_through_cli(self):
        output = self.cli_status(snapshot([peer(document(schemaVersion=True))]))
        row = next(row for row in output["machines"] if row["id"] == NODE)
        self.assertEqual((row["state"], row["reasonCode"]), ("unknown", "unavailable"))

    def test_the_row_format_name_is_the_same_in_code_help_and_docs(self):
        self.assertEqual(contract.CONTRACT, "machine-state/1")
        self.assertEqual(contract.status_document(snapshot(), now=NOW)["contract"], contract.CONTRACT)
        subcommands = cli.build_parser()._subparsers._group_actions[0]._choices_actions
        status_help = next(action.help for action in subcommands if action.dest == "status")
        self.assertIn(contract.CONTRACT, status_help)
        docs = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
        self.assertGreaterEqual(docs.count(contract.CONTRACT), 2)

    def test_c4_envelope_names_the_row_format(self):
        self.assertEqual(contract.status_document(snapshot(), now=NOW)["contract"], "machine-state/1")
        self.assertEqual(self.cli_status(snapshot())["contract"], "machine-state/1")

    def test_c5_duplicate_peer_node_keeps_first_row_and_document(self):
        first = peer(document(host="first", health="down"), name="first")
        repeat = peer(document(host="repeat", health="ok"), name="repeat")
        source = snapshot([first, repeat])
        output = contract.status_document(source, now=NOW, full=True)
        rows = [row for row in output["machines"] if row["id"] == NODE]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["host"], rows[0]["label"], rows[0]["state"]), ("first", "first", "down"))
        self.assertEqual(output["documents"][NODE], pl.project_status(first["status"]))
        # An unavailable first occurrence also wins over a later usable report.
        output = contract.status_document(snapshot([peer(None), repeat]), now=NOW, full=True)
        rows = [row for row in output["machines"] if row["id"] == NODE]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reasonCode"], "unavailable")
        self.assertNotIn(NODE, output["documents"])

    def test_c6_self_error_code_controls_reason_in_adapter_and_cli(self):
        for error, reason in (
            ({"error": "local status timed out", "code": "timeout"}, "timeout"),
            ({"error": "different text", "code": "timeout"}, "timeout"),
            ({"error": "local status unavailable or invalid", "code": "unavailable"}, "unavailable"),
            ({"error": "local status timed out", "code": "unavailable"}, "unavailable"),
            ({"error": "local status timed out"}, "unavailable"),
            ({"error": "other failure", "code": "other"}, "unavailable"),
            ({"error": "other failure"}, "unavailable"),
        ):
            source = snapshot(self_status=error)
            for run in (lambda: contract.status_document(source, now=NOW), lambda: self.cli_status(source)):
                with self.subTest(error=error, run=run):
                    output = run()
                    row = next(row for row in output["machines"] if row["id"] == LOCAL_NODE)
                    self.assertEqual(output["linkState"], "running")
                    self.assertEqual((row["state"], row["reasonCode"]), ("unknown", reason))

    def test_c6_bounded_provider_errors_include_machine_readable_code(self):
        link = pl.PeerLink.__new__(pl.PeerLink)
        link.host, link._lock = "mac", threading.RLock()
        entered, release = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            release.wait()
            return document("mac")

        link.status_provider = blocked
        try:
            with mock.patch.object(pl, "SELF_STATUS_DEADLINE", 0.05):
                first = link._bounded_self_status()
                self.assertTrue(entered.is_set())
                self.assertEqual(first, {"error": "local status timed out", "code": "timeout"})
                self.assertEqual(link._bounded_self_status(), first)
        finally:
            release.set()
            worker = getattr(link, "_self_status_worker", None)
            if worker is not None:
                worker.join(1)
        for provider in (mock.Mock(side_effect=RuntimeError("private fake path")), lambda: {}):
            link.status_provider = provider
            self.assertEqual(link._bounded_self_status(),
                             {"error": "local status unavailable or invalid", "code": "unavailable"})

    def test_c7_pairing_owner_patch_is_only_active_on_elevated_windows(self):
        for os_name, elevated in (("posix", True), ("nt", False), ("nt", True)):
            with self.subTest(os_name=os_name, elevated=elevated):
                case = pairing.LoopbackPairing("test_pair_exchange_one_signed_status_and_unpair")
                case.node = mock.Mock(side_effect=[object(), object()])  # no state or sockets
                native = mock.Mock(return_value=(pl._ADMINS_SID, ["test-account", pl._SYSTEM_SID]))
                shell = mock.Mock()
                shell.IsUserAnAdmin.return_value = elevated
                with mock.patch.object(pairing, "os", SimpleNamespace(name=os_name)), \
                        mock.patch.object(ctypes, "windll", SimpleNamespace(shell32=shell), create=True), \
                        mock.patch.object(pl, "_win_current_sid", return_value="test-account"), \
                        mock.patch.object(pl, "_win_owner_and_allow_sids", native):
                    try:
                        try:
                            case.setUp()
                        except unittest.SkipTest as exc:
                            self.fail("pairing must run on elevated Windows: " + str(exc))
                        patched = os_name == "nt" and elevated
                        self.assertEqual(pl._win_owner_and_allow_sids is not native, patched)
                        owner, sids = pl._win_owner_and_allow_sids("fake-state")
                        self.assertEqual(owner, "test-account" if patched else pl._ADMINS_SID)
                        self.assertEqual(sids, ["test-account", pl._SYSTEM_SID])
                        native.return_value = ("foreign-account", ["test-account"])
                        self.assertEqual(pl._win_owner_and_allow_sids("fake-state"), native.return_value)
                    finally:
                        case.doCleanups()
                    self.assertIs(pl._win_owner_and_allow_sids, native)
                if os_name == "nt":
                    shell.IsUserAnAdmin.assert_called_once_with()
                else:
                    shell.IsUserAnAdmin.assert_not_called()

    def test_c9_windows_state_and_service_agree_for_relative_and_absolute_appdata(self):
        home = temp_dir(self)
        for value in (None, "", "relative/appdata", str(home / "custom")):
            env = {} if value is None else {"LOCALAPPDATA": value}
            expected = home / "custom" if value and Path(value).is_absolute() else home / "AppData" / "Local"
            with self.subTest(value=value), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(pl.os, "name", "nt"), \
                    mock.patch.object(pl, "Path", type(home)), \
                    mock.patch.object(type(home), "home", return_value=home):
                state = pl.default_state_dir()
            manager = service.ServiceManager("windows", home=home, python="fake-python", env=env)
            self.assertEqual(state, expected / "AGIW" / "peer-link")
            self.assertEqual(manager.definition_path(), expected / "AGIW" / "service" / "agiw-peer-link.xml")

    def test_c8_strict_json_rejects_duplicate_keys_at_every_depth(self):
        for raw in ('{"health":"down","health":"ok"}',
                    '{"components":[{"health":"down","health":"ok"}]}',
                    '{"health":"down","healt\\u0068":"ok"}'):
            for value in (raw, raw.encode()):
                with self.subTest(raw=value), self.assertRaises(ValueError):
                    pl.strict_json(value)

    def test_c8_signed_peer_body_with_duplicate_health_is_rejected_by_fetch_status(self):
        # Grok's delta review: the unit test above calls strict_json directly; this one pins that the real
        # peer fetch uses it, so a correctly signed body that says "down" then "ok" is never read as healthy.
        import hashlib
        import hmac
        link_key, me, peer_node = b"k" * 32, "client-node", "server-node"
        fields = ('"schemaVersion":1,"kind":"agiw-peer","host":"pc","platform":"windows",'
                  '"edition":"peer-link","observedAt":"%s","producer":"fake","models":[]' % pl._iso(NOW))

        def fetch(body):
            def fake_http(address, port, method, path, headers, payload, limit, **kwargs):
                nonce = headers["X-Agiw-Nonce"]
                sig = hmac.new(pl.Subkeys(link_key).reply, pl._reply_header(nonce, peer_node, me) + body,
                               hashlib.sha256).hexdigest()
                return 200, {"x-agiw-node": peer_node, "x-agiw-sig": sig}, body
            with mock.patch.object(pl, "_http", fake_http), mock.patch.object(pl.time, "time", return_value=NOW):
                return pl.fetch_status(link_key, me, "127.0.0.1", 8768, peer_node, "windows", NOW)

        duplicate = ('{%s,"health":"down","health":"ok"}' % fields).encode()
        self.assertEqual(json.loads(duplicate)["health"], "ok")  # what a plain json.loads would have believed
        with self.assertRaises(ValueError):
            fetch(duplicate)
        self.assertEqual(fetch(('{%s,"health":"down"}' % fields).encode())["health"], "down")  # control

    def test_c8_normal_json_and_nonfinite_rejection_are_preserved(self):
        doc = {"health": "ok", "components": [{"id": "nisi"}, {"id": "jev"}]}
        self.assertEqual(pl.strict_json(pl.canonical(doc)), doc)
        for word in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(word=word), self.assertRaises(ValueError):
                pl.strict_json('{"value":' + word + '}')

    def test_10_windows_hint_has_distinct_complete_tcp_and_udp_rules(self):
        hint = "\n".join(text for _, text in doctor.firewall_hints("windows", 9000, 9001))
        for proto, port in (("TCP", 9000), ("UDP", 9001)):
            self.assertIn("New-NetFirewallRule -DisplayName 'AGIW peer link " + proto +
                          "' -Direction Inbound -Protocol " + proto + " -LocalPort " + str(port) +
                          " -Profile Private -Action Allow", hint)


class Fix5Docs(unittest.TestCase):
    def test_9_cli_guide_windows_source_and_pairing_order(self):
        text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
        self.assertIn("IPv4 LAN", text)
        self.assertIn('--status-file "<observer folder>\\state\\peer-link-status.json"', text)
        self.assertIn("--lmstudio", text)
        steps = text.split("3. **Pair.**", 1)[1]
        self.assertLess(steps.index("agiw peers"), steps.index("agiw pair studio-pc"))
        self.assertLess(steps.index("agiw pair studio-pc"), steps.index("agiw accept"))

    def test_11_cli_guide_records_current_sources_tests_and_service_limits(self):
        text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
        for phrase in ("machine-state/1", "duplicate", "schemaVersion", "3 s", "detail",
                       "python -B -m unittest discover -s tests_cli -v",
                       "python -B -m unittest test_peer_link", "elevated", "unredacted",
                       "peer-link.log", "Task Scheduler", "Linux is not"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
