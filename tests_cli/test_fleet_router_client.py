"""Router client tests: all HTTP stays on loopback and every job execute is injected."""

import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, call
from urllib.error import HTTPError

from agiw import fleet_jev as jev
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

    def start_worker(self, name="m5-macbook-pro", stored=True, execute=None):
        worker = ww.Worker(
            name,
            "fixture-model",
            "http://127.0.0.1:9/v1/chat/completions",
            str(self.root / "receipts"),
            execute=self.fake_execute(stored) if execute is None else execute,
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
        return rc.FleetRouterClient(workers, jev_preanalysis="off")

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

    def test_router_requires_known_idle_state_before_jev_or_dispatch(self):
        worker = "m5-macbook-pro"
        for fields in ({}, {"busy": None}, {"busy": 0}, {"busy": "false"}, {"busy": True}):
            with self.subTest(fields=fields):
                judge = Mock()
                client = rc.FleetRouterClient({worker: {}}, judge=judge)
                client.request = Mock(return_value=(200, {"worker_id": worker, "model_ready": True, **fields}))
                self.assertEqual(
                    client.run("job"),
                    {"outcome": "BLOCKED", "reason": "no-verified-local-worker", "worker": None, "job_id": "job"},
                )
                client.request.assert_called_once_with(worker, "GET", "/v1/ready")
                judge.assert_not_called()

    def test_required_jev_preanalysis_controls_dispatch_and_stored_receipt(self):
        worker = "m5-macbook-pro"
        program = "tiny-arithmetic.mjs"
        execute = Mock(wraps=self.fake_execute())
        judge = Mock(
            side_effect=[
                {"model": jev.JEV_MODEL, "answers": {"plan": {"choice": "direct"}, "underspecified": {"noul": value}}}
                for value in (0.5, 0.49)
            ]
        )
        workers, _ = rc.load_workers(self.config(self.start_worker(execute=execute)))
        client = rc.FleetRouterClient(workers, judge=judge)
        client.request = Mock(wraps=client.request)
        trace = Mock()
        trace.attach_mock(client.request, "request")
        trace.attach_mock(judge, "judge")
        state = jev.task_state(program)
        questions = jev.plan_questions()
        readiness = call.request(worker, "GET", "/v1/ready")
        preanalysis = call.judge(state, questions)

        self.assertEqual(
            client.run("blocked", program),
            {
                "outcome": "BLOCKED",
                "reason": "JEV_PREANALYSIS_FAILED:JEV_UNDERSPECIFIED",
                "worker": worker,
                "job_id": "blocked",
            },
        )
        self.assertEqual(trace.mock_calls, [readiness, preanalysis])
        execute.assert_not_called()
        self.assertEqual(list((self.root / "receipts").glob("*.json")), [])

        result = client.run("accepted", program)
        self.assertEqual((result["outcome"], result["worker"], result["job_id"]), ("COMPLETED", worker, "accepted"))
        self.assertIs(result["reportStored"], True)
        execute.assert_called_once()
        body = client.request.call_args.args[3]
        self.assertEqual(
            trace.mock_calls,
            [readiness, preanalysis, readiness, preanalysis, call.request(worker, "POST", "/v1/jobs", body)],
        )
        self.assertEqual((body["job_id"], body["program"]), ("accepted", program))
        self.assertEqual(body["route"]["jev"]["request"], {"state": state, "questions": questions})
        self.assertEqual(execute.call_args.kwargs["route"], body["route"])
        receipt_path = Path(result["receipt_path"])
        self.assertEqual(list((self.root / "receipts").glob("*.json")), [receipt_path])
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual((receipt["job_id"], receipt["worker_id"], receipt["program"]), ("accepted", worker, program))
        self.assertEqual(receipt["route"], body["route"])
        self.assertEqual(receipt["route"]["preanalysis"], "jev")

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

    def test_closes_http_error_bodies_at_router_refusal_boundaries(self):
        worker = "m5-macbook-pro"
        ready = (200, {"worker_id": worker, "model_ready": True, "busy": False})
        for method, path, status, reason in (
            ("GET", "/v1/ready", 401, "no-verified-local-worker"),
            ("POST", "/v1/jobs", 503, "DISPATCH_FAILED"),
        ):
            with self.subTest(method=method):
                body = BytesIO(b"response")
                error = HTTPError("http://127.0.0.1:9" + path, status, "fixture", None, body)
                client = rc.FleetRouterClient({worker: {}}, jev_preanalysis="off")
                client.request = Mock(side_effect=[error] if method == "GET" else [ready, error])
                self.assertEqual(
                    client.run("job"),
                    {
                        "outcome": "BLOCKED",
                        "reason": reason,
                        "worker": None if method == "GET" else worker,
                        "job_id": "job",
                    },
                )
                self.assertTrue(body.closed)
                self.assertEqual(client.request.call_args_list[0], call(worker, "GET", "/v1/ready"))
                self.assertEqual(client.request.call_count, 1 if method == "GET" else 2)
                self.assertEqual(client.request.call_args.args[:3], (worker, method, path))

    def test_direct_request_caller_can_read_the_http_error_body(self):
        worker = "m5-macbook-pro"
        url = "http://127.0.0.1:9"
        body = BytesIO(b"response")
        error = HTTPError(url + "/v1/ready", 401, "fixture", None, body)
        client = rc.FleetRouterClient({worker: {"url": url, "token": TOKEN}}, opener=Mock(open=Mock(side_effect=error)))
        with error:
            with self.assertRaises(HTTPError) as raised:
                client.request(worker, "GET", "/v1/ready")
            self.assertIs(raised.exception, error)
            self.assertEqual(raised.exception.read(), b"response")
        self.assertTrue(body.closed)

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
