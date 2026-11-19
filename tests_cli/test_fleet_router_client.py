"""Router client tests: all HTTP stays on loopback and every job execute is injected."""

import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from agiw import fleet_router_client as rc
from agiw import windows_worker as ww

TOKEN = b"r" * 40


class RouterClientTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.token_file = self.root / "token"
        self.token_file.write_bytes(TOKEN)
        os.chmod(str(self.token_file), 0o600)

    def fake_execute(self, stored=True):
        def execute(mode, url, model, source, program="nisi-smoke.mjs", report_store=None, worker_id=None, **_):
            rid = str(uuid.uuid4())
            path = self.root / (rid + ".json")
            return {
                "reportStored": stored,
                "reportPath": str(path),
                "report": {
                    "outcome": "COMPLETED" if stored else "BLOCKED",
                    "runId": rid,
                    "reportStored": stored,
                    "storedReportSha256": "b" * 64,
                },
            }

        return execute

    def start_worker(self, name="m5-macbook-pro", stored=True):
        worker = ww.Worker(
            name,
            "fixture-model",
            "http://127.0.0.1:9/v1/chat/completions",
            str(self.root / "receipts"),
            execute=self.fake_execute(stored),
            probe=lambda: (["fixture-model"], "llama-server"),
            commit=lambda: "a" * 40,
        )
        server = ww.create_server(worker, TOKEN)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        return server.server_address[1]

    def config(self, port, allow_lan=False, token_file=None, host="127.0.0.1"):
        row = {"url": f"http://{host}:{port}", "token_file": str(token_file or self.token_file)}
        if allow_lan:
            row["allow_lan"] = True
        path = self.root / f"config-{len(list(self.root.glob('config-*.json')))}.json"
        path.write_text(json.dumps({"workers": {"m5-macbook-pro": row}}))
        return str(path)

    def client(self, path):
        workers, _ = rc.load_workers(path)
        return rc.FleetRouterClient(workers)

    def test_picks_forwarded_loopback_worker(self):
        port = self.start_worker()
        client = self.client(self.config(port))
        result = client.run("job-1")
        self.assertEqual(result["outcome"], "COMPLETED")
        self.assertEqual(result["worker"], "m5-macbook-pro")
        self.assertEqual(result["job_id"], "job-1")
        self.assertTrue(result["reportStored"])
        self.assertTrue(result["report_path"].endswith(".json"))
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        self.assertEqual(
            receipt["route"], {"worker": "m5-macbook-pro", "preanalysis": "local", "reason": "strict-local"}
        )
        self.assertEqual(receipt["workerCommit"], "a" * 40)

    def test_unreachable_worker_not_selected(self):
        # Reserve then release a port so nothing is listening there.
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        client = self.client(self.config(closed_port))
        result = client.run("job-1")
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["reason"], "no-verified-local-worker")

    def test_401_worker_not_selected(self):
        port = self.start_worker()
        other = self.root / "other-token"
        other.write_bytes(b"z" * 40)
        os.chmod(str(other), 0o600)
        client = self.client(self.config(port, token_file=other))
        result = client.run("job-1")
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["reason"], "no-verified-local-worker")

    def test_lan_url_rejected_without_allow_lan(self):
        with self.assertRaises(rc.Refused):
            rc.load_workers(self.config(8080, host="203.0.113.5"))
        path = self.config(8080, allow_lan=True, host="203.0.113.5")
        workers, _ = rc.load_workers(path)
        self.assertIn("m5-macbook-pro", workers)

    def test_missing_token_refused(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"workers": {"m5-macbook-pro": {"url": "http://127.0.0.1:8080"}}}))
        with self.assertRaises(rc.Refused):
            rc.load_workers(str(path))

    def test_credential_query_fragment_urls_refused(self):
        for value in (
            "http://u:p@127.0.0.1:8080",
            "http://127.0.0.1:8080?x=1",
            "http://127.0.0.1:8080#x",
            "https://127.0.0.1:8080",
        ):
            with self.subTest(value=value), self.assertRaises(rc.Refused):
                rc.worker_url(value)

    def test_receipt_without_report_stored_is_blocked(self):
        port = self.start_worker(stored=False)
        client = self.client(self.config(port))
        result = client.run("job-1")
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertIn("RECEIPT_INVALID", result["reason"])

    def test_no_hosted_route_ever(self):
        self.assertEqual(
            rc.routing.plan("local", "routine", {}, rc.routing.HostedEvidence(), now=time.time()).worker, None
        )


if __name__ == "__main__":
    unittest.main()
