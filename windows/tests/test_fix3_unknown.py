"""Fix pass 3: missing evidence remains unknown in the PC snapshot."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes
from tests import test_collector_safety as safety


class LaneEvidence(unittest.TestCase):
    setUp = safety.CollectorSafety.setUp
    collector = safety.CollectorSafety.collector
    configure = safety.CollectorSafety.configure
    processes = staticmethod(safety.CollectorSafety.processes)

    def test_missing_config_and_unknown_owner_publish_unknown_worker_lanes(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                if configured:
                    self.configure()
                snapshot = self.collector(processes=self.processes(None)).collect()
                worker = snapshot["windowsWorker"]
                self.assertEqual(worker["state"], "unknown")
                self.assertIn("unknown", worker["detail"].lower())
                for lane in worker["lanes"].values():
                    self.assertIsNone(lane["up"])
                    self.assertEqual(lane["health"]["state"], "unknown")
                    self.assertTrue(lane["health"]["reason"])

    def test_pending_probe_publishes_unknown(self):
        self.configure()
        collector = self.collector(processes=self.processes())
        collector.lane_wait = 0
        with mock.patch("agiw_win.collect.LanePoller") as poller:
            poller.return_value.read.return_value = None
            snapshot = collector.collect()
        lane = snapshot["windowsWorker"]["lanes"]["fast"]
        self.assertIsNone(lane["up"])
        self.assertEqual(lane["health"], {"state": "unknown", "reason": "First lane probe pending"})

    def test_lane_health_requires_affirmative_non_serving_evidence(self):
        for status in ("unknown", "undeclared", "stale", "unreachable", "no_identity", "unexpected", None):
            with self.subTest(status=status):
                self.assertEqual(probes.lane_health({"status": status})["state"], "unknown")
        for status in ("loading", "identity_mismatch", "not_agiw"):
            with self.subTest(status=status):
                self.assertEqual(probes.lane_health({"status": status})["state"], "down")
        for status in ("idle", "busy", "slots_unknown"):
            with self.subTest(status=status):
                self.assertEqual(probes.lane_health({"status": status})["state"], "up")

    def test_worker_mixed_evidence_keeps_unknown_lane_unknown(self):
        self.configure()
        collector = self.collector(processes=self.processes(), fetch=safety.CollectorSafety.fetch)
        snapshot = collector.collect()
        worker = snapshot["windowsWorker"]
        self.assertEqual(worker["state"], "advertised")
        self.assertIs(worker["lanes"]["fast"]["up"], True)
        self.assertIsNone(worker["lanes"]["deep"]["up"])

    def test_nisi_unknown_is_not_no_answer(self):
        gate = self.root / "nisi-gate-v0.2" / "nisi_gate.mjs"
        gate.parent.mkdir()
        gate.write_text("")
        for status in ("unknown", "undeclared", "stale", "unreachable", "no_identity"):
            with self.subTest(status=status):
                lanes = [{"id": name, "status": status, "expectedModel": name} for name in ("fast", "deep")]
                component = probes.nisi_component(lanes, self.root)
                self.assertEqual(component["state"], "unknown")
                self.assertIn("unknown", component["detail"].lower())
                self.assertNotIn("no lane answers", component["detail"].lower())
                lanes[0]["status"] = "idle"
                component = probes.nisi_component(lanes, self.root)
                self.assertEqual(component["state"], "unknown")
                self.assertIn("unknown", component["detail"].lower())
                self.assertNotIn("only", component["detail"].lower())


class JobEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_unreadable_feed_is_unknown(self):
        with mock.patch.object(probes.os, "scandir", side_effect=PermissionError("denied")):
            jobs, source = probes.codemode_jobs(self.root, [], 1000)
        self.assertEqual(jobs["availability"], {"state": "unknown", "reason": "code-runs folder unreadable"})
        self.assertIs(jobs["liveAvailable"], False)
        self.assertEqual(source["state"], "unavailable")

    def test_readable_completed_history_never_claims_live_availability(self):
        jobs, source = probes.codemode_jobs(self.root, [], 1000)
        self.assertEqual(jobs["availability"]["state"], "available")
        self.assertIs(jobs["liveAvailable"], False)
        self.assertEqual(source["state"], "live")

    def test_unreadable_or_malformed_manifest_cannot_claim_empty_history(self):
        run = self.root / "20261017-000000-fixture"
        run.mkdir()
        manifest = run / "manifest.json"
        manifest.write_text("not json")
        for failure in (None, PermissionError("denied")):
            with self.subTest(failure=failure):
                if failure:
                    with mock.patch.object(probes, "read_json", side_effect=failure):
                        jobs, source = probes.codemode_jobs(self.root, [], 1000)
                else:
                    jobs, source = probes.codemode_jobs(self.root, [], 1000)
                self.assertEqual(jobs["availability"]["state"], "unknown")
                self.assertEqual(source["state"], "unavailable")

    def test_stat_failure_cannot_claim_empty_history(self):
        (self.root / "20261017-000000-fixture").mkdir()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(Path, "stat", side_effect=PermissionError("denied")).start()
        jobs, source = probes.codemode_jobs(self.root, [], 1000)
        self.assertEqual(jobs["availability"]["state"], "unknown")
        self.assertEqual(source["state"], "unavailable")

    def test_missing_metrics_or_malformed_metric_is_not_empty_history(self):
        run = self.root / "20261017-000000-fixture"
        run.mkdir()
        for payload in ({}, {"metrics": ["bad", 3]}):
            with self.subTest(payload=payload):
                (run / "manifest.json").write_text(json.dumps(payload))
                jobs, source = probes.codemode_jobs(self.root, [], 1000)
                self.assertEqual(jobs["availability"]["state"], "unknown")
                self.assertEqual(source["state"], "unavailable")

    def test_scan_limit_cannot_claim_complete_history(self):
        for name in ("one", "two"):
            (self.root / name).mkdir()
        with mock.patch.object(probes, "QUEUE_MAX_ENTRIES", 1):
            jobs, source = probes.codemode_jobs(self.root, [], 1000)
        self.assertEqual(jobs["availability"]["state"], "unknown")
        self.assertEqual(source["state"], "unavailable")

    def test_collector_probe_exception_keeps_feed_unknown(self):
        helper = safety.CollectorSafety()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        collector = helper.collector()
        with mock.patch.object(probes, "codemode_jobs", side_effect=OSError("denied")):
            snapshot = collector.collect()
        self.assertEqual(snapshot["windowsJobs"]["availability"]["state"], "unknown")
        self.assertIs(snapshot["windowsJobs"]["liveAvailable"], False)


class BuildWhitespace(unittest.TestCase):
    def test_buildinfo_has_one_final_newline(self):
        path = Path(__file__).resolve().parents[1] / "agiw_win" / "buildinfo.py"
        contents = path.read_bytes()
        self.assertTrue(contents.endswith(b"\n"))
        self.assertFalse(contents.endswith(b"\n\n"))
