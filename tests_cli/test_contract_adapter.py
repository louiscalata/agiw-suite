"""M2 adapter checks using frozen clocks and in-memory control snapshots only.

All 12 M0 producer fixtures for the CLI, Linux and Windows are expressible.
Their core host becomes status.host; only id changes to the paired node UUID
(or local node UUID). Display names are omitted to compare the whole row.
"""
import copy
import datetime as dt
import json
import unittest
from unittest import mock

from support import ROOT, forbid_real_commands

import peer_link as pl
from agiw import cli, contract
from test_machine_status_contract import SCHEMA, validate_schema


NOW = 1700000000.0
NODE = "11111111-1111-4111-8111-111111111111"
LOCAL_NODE = "22222222-2222-4222-8222-222222222222"
OTHER_NODE = "33333333-3333-4333-8333-333333333333"
CONTRACT = ROOT / "contracts" / "machine-status"
PRODUCERS = {"cli_peer_link": "agiw-peer-link lmstudio",
             "linux_observer": "agiw-linux-observer",
             "windows_observer": "agiw-win-observer 0.2.0"}


def iso(at):
    return dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat()


def document(platform="windows", health="ok", age=1, **fields):
    return dict({"schemaVersion": 1, "kind": "agiw-peer", "host": "reported-machine",
                 "platform": platform, "health": health, "observedAt": iso(NOW - age),
                 "producer": "agiw-peer-link lmstudio", "models": []}, **fields)


def peer(status=None, state="connected", platform="windows", node=NODE, **fields):
    return dict({"node": node, "host": platform, "paired": True, "state": state,
                 "lastHello": NOW, "error": None, "status": status}, **fields)


def snapshot(peers=(), self_status=None, platform="mac", **fields):
    return dict({"protocol": pl.PROTOCOL, "observedAt": iso(NOW),
                 "self": {"node": LOCAL_NODE, "host": platform},
                 "selfStatus": self_status if self_status is not None else {"error": "not available"},
                 "peers": list(peers), "pollError": None}, **fields)


class ContractAdapter(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        blocker = mock.patch("socket.socket", side_effect=AssertionError("No sockets in adapter tests"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def adapt(self, source, **kwargs):
        output = contract.status_document(source, now=NOW, **kwargs)
        self.assertEqual(output["schemaVersion"], 2)
        self.assertEqual(output["contract"], "machine-status/v1")
        self.assertEqual(output["sampledAt"], NOW)
        self.assertIs(output["signed"], True)
        self.assertIs(output["encrypted"], False)
        self.assertIsInstance(output["observedAt"], str)
        self.assertEqual({"mac", "windows", "linux"},
                         {row["platform"] for row in output["machines"] if row["platform"]})
        for row in output["machines"]:
            validate_schema(row, SCHEMA)
        return output

    def row(self, source, node=NODE, **kwargs):
        output = self.adapt(source, **kwargs)
        return next(row for row in output["machines"] if row["id"] == node)

    def test_all_relevant_m0_fixtures_have_exact_parity(self):
        covered = []
        for path in sorted((CONTRACT / "fixtures").glob("*.json")):
            case = json.loads(path.read_text(encoding="utf-8"))
            inputs = case["input"]
            producer = inputs.get("producer")
            if producer not in PRODUCERS:
                continue
            with self.subTest(fixture=case["name"]):
                self.assertEqual(case["kind"], "summary")
                report = copy.deepcopy(inputs["report"])
                complete = report.pop("modelsComplete", False)
                doc = document(inputs["platform"], **report)
                doc.update(host=inputs["host"], producer=PRODUCERS[producer],
                           loadedListComplete=complete)
                if complete:
                    doc["modelsTotal"] = len(doc["models"])
                if inputs["trust"] == "local":
                    source = snapshot(self_status=doc, platform=inputs["platform"])
                    row_id = LOCAL_NODE
                else:
                    source = snapshot([peer(doc, platform=inputs["platform"])])
                    row_id = NODE
                row = self.row(source, row_id)
                self.assertEqual(row, dict(case["expected"], id=row_id))
                covered.append(case["name"])
        self.assertEqual(set(covered), {
            "cli-file-degraded", "cli-file-down", "cli-file-unlisted", "cli-lmstudio-ok",
            "cli-minimal-unknown", "linux-observer-down", "linux-observer-unknown",
            "linux-observer-up", "windows-observer-degraded", "windows-observer-down",
            "windows-observer-ok", "windows-observer-unknown"})

    def test_embedded_health_table_matches_frozen_file(self):
        expected = json.loads((CONTRACT / "health-words.json").read_text(encoding="utf-8"))
        self.assertEqual(contract.HEALTH_WORDS, expected)

    def test_every_snapshot_state(self):
        for state, last_hello, reason in [
            ("stale", NOW, "not_connected"),
            ("connecting", NOW, "not_connected"),
            ("offline", NOW, "not_connected"),
            ("offline", 0, "not_connected"),
            ("offline", None, "not_observed"),
        ]:
            with self.subTest(state=state, lastHello=last_hello):
                row = self.row(snapshot([peer(None, state, lastHello=last_hello)]))
                self.assertEqual((row["state"], row["reasonCode"]), ("unknown", reason))
                self.assertEqual(row["platform"], "windows")
                self.assertIsNone(row["loadedCount"])
                self.assertIsNone(row["observedAt"])
        output = self.adapt(snapshot([peer(document(), "available", paired=False)]))
        self.assertNotIn(NODE, {row["id"] for row in output["machines"]})
        row = self.row(snapshot([peer(document())]))
        self.assertEqual((row["state"], row["trust"]), ("up", "authenticated"))

    def test_stale_snapshot_does_not_keep_a_previous_document(self):
        source = snapshot([peer(document(), "stale")])
        row = self.row(source)
        self.assertEqual((row["state"], row["reasonCode"]), ("unknown", "not_connected"))
        self.assertIsNone(row["observedAt"])
        self.assertNotIn(NODE, self.adapt(source, full=True)["documents"])

    def test_peer_errors_and_poll_errors_fail_unavailable(self):
        for state in ("connected", "stale", "connecting", "offline"):
            for fields in ({"error": "read failed"}, {"error": None}):
                source = snapshot([peer(None, state, **fields)], pollError="poll failed")
                with self.subTest(state=state, fields=fields):
                    row = self.row(source)
                    self.assertEqual((row["state"], row["reasonCode"]), ("unknown", "unavailable"))
        for bad in (None, {"error": "provider failed"}, [], "bad"):
            with self.subTest(status=bad):
                row = self.row(snapshot([peer(bad)]))
                self.assertEqual(row["reasonCode"], "unavailable")

    def test_transport_diagnostics_do_not_replace_usable_documents(self):
        source = snapshot([peer(document(), error="old transport diagnostic")], pollError="old poll failure")
        row = self.row(source)
        self.assertEqual((row["state"], row["reasonCode"]), ("up", "observed"))
        self.assertIn(NODE, self.adapt(source, full=True)["documents"])

    def test_completeness_is_explicit_and_total_must_match(self):
        models = [{"id": "loaded", "loadedState": "loaded", "activity": "idle"}]
        for fields in ({}, {"modelsComplete": True}, {"loadedListComplete": False},
                       {"loadedListComplete": 1},
                       {"loadedListComplete": True, "modelsTotal": 2},
                       {"loadedListComplete": True, "modelsTotal": None},
                       {"loadedListComplete": True, "modelsTotal": True},
                       {"loadedListComplete": True, "modelsTotal": 1.0}):
            with self.subTest(fields=fields):
                row = self.row(snapshot([peer(document(models=models, **fields))]))
                self.assertEqual(row["state"], "up")
                self.assertFalse(row["modelsComplete"])
                self.assertIsNone(row["loadedCount"])
                self.assertIsNone(row["busyCount"])
        for fields in ({"loadedListComplete": True}, {"loadedListComplete": True, "modelsTotal": 1}):
            with self.subTest(fields=fields):
                row = self.row(snapshot([peer(document(models=models, **fields))]))
                self.assertTrue(row["modelsComplete"])
                self.assertEqual((row["loadedCount"], row["busyCount"]), (1, 0))
        row = self.row(snapshot([peer(document(loadedListComplete=True, modelsTotal=0))]))
        self.assertEqual((row["loadedCount"], row["busyCount"]), (0, 0))

    def test_unknown_residency_and_unknown_activity_are_distinct(self):
        for residency, activity, loaded in (("unknown", "idle", None), ("loaded", "unknown", 1)):
            with self.subTest(residency=residency, activity=activity):
                doc = document(loadedListComplete=True, modelsTotal=1,
                               models=[{"id": "a", "loadedState": residency, "activity": activity}])
                row = self.row(snapshot([peer(doc)]))
                self.assertEqual(row["state"], "up")
                self.assertEqual(row["loadedCount"], loaded)
                self.assertIsNone(row["busyCount"])
                self.assertEqual(row["modelsComplete"], loaded is not None)

    def test_producer_routing_and_exact_linux_name(self):
        for producer, health, state, reason in (
            ("agiw-linux-observer", "up", "up", "observed"),
            ("unknown-observer", "up", "unknown", "schema_invalid"),
            ("agiw-linux-observer-extra", "up", "unknown", "schema_invalid"),
            ("agiw-peer-link minimal", "unknown", "unknown", "unavailable"),
            ("agiw-win-observer 0.2.0", "degraded", "up", "reported_degraded"),
            ("agiw-win-observer 0.2.0+abcdef123456.dirty.unverified", "unknown", "unknown", "unavailable"),
            ("agiw-linux-observer", "ok", "unknown", "schema_invalid"),
            ("unrecognized", None, "unknown", "schema_invalid"),
        ):
            with self.subTest(producer=producer, health=health):
                row = self.row(snapshot([peer(document("linux", health, producer=producer), platform="linux")]))
                self.assertEqual((row["state"], row["reasonCode"]), (state, reason))

    def test_local_mac_keeps_the_three_second_window(self):
        # Contract v1.1: only the local row keeps the platform cadence (Mac 3 s); remote reports get 30 s.
        for age, reason in ((3, "observed"), (3.001, "stale")):
            with self.subTest(age=age):
                row = self.row(snapshot(self_status=document("mac", age=age)), node=LOCAL_NODE)
                self.assertEqual((row["trust"], row["reasonCode"], row["freshForSeconds"]), ("local", reason, 3.0))

    def test_original_timestamp_and_core_freshness_windows(self):
        for platform, age, reason in (("mac", 30, "observed"), ("mac", 30.001, "stale"),
                                      ("windows", 30, "observed"), ("windows", 30.001, "stale"),
                                      ("linux", 30, "observed"), ("linux", 30.001, "stale"),
                                      ("windows", -2, "observed"), ("windows", -2.001, "clock_skew")):
            with self.subTest(platform=platform, age=age):
                doc = document(platform, age=age)
                row = self.row(snapshot([peer(doc, platform=platform, lastHello=NOW)]))
                self.assertEqual(row["reasonCode"], reason)
                self.assertEqual(row["observedAt"], doc["observedAt"])
                self.assertAlmostEqual(row["ageSeconds"], max(0, age), places=5)

    def test_health_unknown_and_unlisted_keep_timestamp_and_null_counts(self):
        for health, reason in (("unknown", "unavailable"), ("UP", "schema_invalid"),
                               ("serving", "schema_invalid")):
            with self.subTest(health=health):
                doc = document(health=health, loadedListComplete=True, modelsTotal=0)
                row = self.row(snapshot([peer(doc)]))
                self.assertEqual((row["state"], row["reasonCode"]), ("unknown", reason))
                self.assertEqual(row["observedAt"], doc["observedAt"])
                self.assertIsNone(row["loadedCount"])

    def test_local_status_and_errors(self):
        doc = document("mac", host="my-local-machine", handsOff=True)
        row = self.row(snapshot(self_status=doc), LOCAL_NODE)
        self.assertEqual((row["host"], row["trust"], row["state"]), ("my-local-machine", "local", "up"))
        self.assertTrue(row["handsOff"])
        for error in ({"error": "provider failed"}, None):
            with self.subTest(error=error):
                row = self.row(snapshot(self_status=error), LOCAL_NODE)
                self.assertEqual((row["trust"], row["state"], row["reasonCode"]),
                                 ("local", "unknown", "unavailable"))
        source = snapshot(self_status=document("macos"), platform="macos")
        self.assertEqual(self.row(source, LOCAL_NODE)["platform"], "mac")

    def test_missing_platforms_and_two_nodes_on_one_platform(self):
        source = snapshot([peer(document(), node=NODE), peer(document(host="second"), node=OTHER_NODE)])
        rows = self.adapt(source)["machines"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(len([row for row in rows if row["platform"] == "windows"]), 2)
        missing = next(row for row in rows if row["id"] == "linux")
        self.assertEqual((missing["host"], missing["state"], missing["reasonCode"], missing["trust"]),
                         ("linux", "unknown", "not_observed", "unverified"))

    def test_labels_and_legacy_identity(self):
        source = snapshot([peer(document(host="reported-host"), name="Friendly")])
        row = self.row(source)
        self.assertEqual((row["id"], row["host"], row["label"]), (NODE, "reported-host", "Friendly"))
        legacy = document()
        legacy.pop("kind")
        legacy.pop("platform")
        legacy["host"] = "windows"
        row = self.row(snapshot([peer(legacy)]))
        self.assertEqual((row["id"], row["host"], row["platform"]), (NODE, NODE, "windows"))

    def test_full_has_projected_documents_outside_rows_and_no_input_mutation(self):
        local_doc = document("mac", privatePath="do not publish")
        peer_doc = document(privatePath="do not publish", health="down")
        source = snapshot([peer(peer_doc), peer(None, "offline", node=OTHER_NODE)], self_status=local_doc)
        before = copy.deepcopy(source)
        plain = self.adapt(source)
        self.assertNotIn("documents", plain)
        output = self.adapt(source, full=True)
        self.assertEqual(set(output["documents"]), {NODE, LOCAL_NODE})
        self.assertEqual(output["documents"][NODE], pl.project_status(peer_doc))
        self.assertEqual(output["documents"][LOCAL_NODE], pl.project_status(local_doc))
        self.assertNotIn("privatePath", json.dumps(output))
        self.assertEqual(source, before)
        for row in output["machines"]:
            self.assertNotIn("document", row)
            self.assertNotIn("status", row)
        # A fresh verified document may still describe a stale health observation.
        peer_doc["observedAt"] = iso(NOW - 45)
        row = self.row(source)
        self.assertEqual(row["reasonCode"], "stale")
        self.assertIn(NODE, self.adapt(source, full=True)["documents"])

    def test_cli_json_not_running_exits_zero_and_other_failures_stay_errors(self):
        for full in (False, True):
            lines = []
            argv = ["--state-dir", "unused", "status", "--json"] + (["--full"] if full else [])
            with mock.patch.object(cli, "control", side_effect=cli.CliError(cli.NOT_RUNNING, cli.EXIT_NOT_RUNNING)), \
                    mock.patch.object(cli.pl, "detect_host_kind", return_value="mac"), \
                    mock.patch("sys.stderr") as err:
                code = cli.main(argv, out=lines.append)
            self.assertEqual(code, 0)
            err.write.assert_not_called()
            output = json.loads("\n".join(lines))
            self.assertEqual(output["linkState"], "not_running")
            self.assertEqual(len(output["machines"]), 3)
            for row in output["machines"]:
                validate_schema(row, SCHEMA)
                self.assertEqual(row["state"], "unknown")
            self.assertEqual("documents" in output, full)
        for code in (cli.EXIT_FAILED, cli.EXIT_USAGE):
            with self.subTest(code=code), \
                    mock.patch.object(cli, "control", side_effect=cli.CliError("control failure", code)), \
                    mock.patch("sys.stderr"):
                self.assertEqual(cli.main(["--state-dir", "unused", "status", "--json"], out=lambda line: None), code)

    def test_not_running_returns_three_unknown_rows(self):
        local = {"node": LOCAL_NODE, "name": "this-machine", "host": "macos"}
        output = self.adapt(None, local=local, full=True)
        self.assertEqual(output["linkState"], "not_running")
        self.assertEqual(output["self"], local)
        self.assertEqual(len(output["machines"]), 3)
        self.assertEqual(output["documents"], {})
        for row in output["machines"]:
            self.assertEqual(row["state"], "unknown")
            self.assertIsNone(row["loadedCount"])
        local_row = next(row for row in output["machines"] if row["id"] == LOCAL_NODE)
        self.assertEqual((local_row["platform"], local_row["trust"], local_row["reasonCode"]),
                         ("mac", "local", "unavailable"))
        self.assertTrue(all(row["reasonCode"] == "not_observed" for row in output["machines"]
                            if row["id"] != LOCAL_NODE))


if __name__ == "__main__":
    unittest.main()
