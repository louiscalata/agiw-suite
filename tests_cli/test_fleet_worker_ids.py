"""Worker ids come from the router config; every known id routes, unknown ids are refused."""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agiw import fleet_local_pilot as pilot
from agiw import fleet_router_client as rc
from agiw import fleet_routing_plan as routing
from agiw import windows_worker as ww
from agiw.fleet_routing_plan import WorkerEvidence

ENDPOINT = "http://127.0.0.1:9/v1/chat/completions"
NISI_AVAILABLE = (pilot.ROOT / "preview-pilot/vendor/nisi-0.3.1/workflow/engine.mjs").is_file() and shutil.which(
    "node"
) is not None


def ready():
    return WorkerEvidence(time.time(), True, True, True, False)


class WorkerIdTests(unittest.TestCase):
    def test_router_config_matches_routing_plan(self):
        self.assertEqual(set(pilot.known_workers()), set(routing.WORKERS))
        self.assertEqual(set(pilot.known_workers()), {"m5-macbook-pro", "dual-gpu-pc", "monterey-mac-pro"})

    def test_every_known_worker_routes_to_itself(self):
        for worker in pilot.known_workers():
            with self.subTest(worker=worker):
                self.assertEqual(pilot.route(ready(), worker_id=worker)["worker"], worker)

    def test_unknown_worker_id_refused_by_route_and_execute(self):
        for bad in ("unknown-box", "", None, "M5-MACBOOK-PRO"):
            with self.subTest(bad=bad):
                with self.assertRaises(pilot.Refused) as cm:
                    pilot.route(ready(), worker_id=bad)
                self.assertEqual(str(cm.exception), "UNKNOWN_WORKER_ID")
                with self.assertRaises(pilot.Refused):
                    pilot.execute("fixture", ENDPOINT, "fixture-only", worker_id=bad)

    def test_worker_refuses_unknown_id_and_accepts_known(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ww.Refused):
                ww.Worker("unknown-box", "m", ENDPOINT, td)
            for worker in pilot.known_workers():
                self.assertEqual(ww.Worker(worker, "m", ENDPOINT, td).worker_id, worker)

    def test_worker_passes_its_id_to_execute(self):
        seen = {}

        def execute(mode, url, model, source, program="nisi-smoke.mjs", report_store=None, worker_id=None, **_):
            seen["worker_id"] = worker_id
            return {
                "reportStored": True,
                "reportPath": "/x",
                "report": {"outcome": "COMPLETED", "reportStored": True, "runId": "r", "storedReportSha256": "a" * 64},
            }

        with tempfile.TemporaryDirectory() as td:
            worker = ww.Worker("monterey-mac-pro", "m", ENDPOINT, td, execute=execute)
            receipt = worker.run_job(
                {
                    "job_id": "j1",
                    "program": "nisi-smoke.mjs",
                    "route": {"worker": "monterey-mac-pro", "preanalysis": "local", "reason": "strict-local"},
                }
            )
        self.assertEqual(seen["worker_id"], "monterey-mac-pro")
        self.assertEqual(receipt["outcome"], "COMPLETED")

    def test_router_config_rejects_unknown_worker_name(self):
        with tempfile.TemporaryDirectory() as td:
            token = Path(td) / "token"
            token.write_bytes(b"t" * 40)
            os.chmod(str(token), 0o600)
            cfg = Path(td) / "workers.json"
            cfg.write_text(
                json.dumps({"workers": {"unknown-box": {"url": "http://127.0.0.1:18791", "token_file": str(token)}}})
            )
            with self.assertRaises(rc.Refused) as cm:
                rc.load_workers(str(cfg))
            self.assertEqual(str(cm.exception), "UNKNOWN_WORKER_ID")
            cfg.write_text(
                json.dumps(
                    {"workers": {"monterey-mac-pro": {"url": "http://127.0.0.1:18791", "token_file": str(token)}}}
                )
            )
            workers, _ = rc.load_workers(str(cfg))
            self.assertEqual(list(workers), ["monterey-mac-pro"])

    def test_invalid_or_mismatched_router_config_refused(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fleet-workers.json"
            for body, code in (
                ({"schemaVersion": 1, "workers": []}, "ROUTER_CONFIG_INVALID"),
                ({"schemaVersion": 2, "workers": ["m5-macbook-pro"]}, "ROUTER_CONFIG_INVALID"),
                ({"schemaVersion": 1, "workers": ["m5-macbook-pro", "m5-macbook-pro"]}, "ROUTER_CONFIG_INVALID"),
                ({"schemaVersion": 1, "workers": ["m5-macbook-pro", "evil-box"]}, "ROUTER_CONFIG_MISMATCH"),
            ):
                path.write_text(json.dumps(body))
                with self.subTest(code=code, body=body):
                    with self.assertRaises(pilot.Refused) as cm:
                        pilot.known_workers(path)
                    self.assertEqual(str(cm.exception), code)

    @unittest.skipUnless(NISI_AVAILABLE, "pinned Nisi vendor or node missing")
    def test_fixture_job_runs_as_each_known_worker_through_node(self):
        with tempfile.TemporaryDirectory() as td:
            for worker in pilot.known_workers():
                with self.subTest(worker=worker):
                    result = pilot.execute(
                        "fixture", ENDPOINT, "fixture-only", report_store=str(Path(td) / "store"), worker_id=worker
                    )
                    self.assertEqual(result["route"]["worker"], worker)
                    self.assertEqual(result["report"]["outcome"], "COMPLETED")
                    self.assertTrue(result["reportStored"])


if __name__ == "__main__":
    unittest.main()
