from __future__ import annotations

import io
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agiw import orchestrator_mcp as om


class OrchestratorMcpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "root"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def config(self, **kwargs: object) -> om.Config:
        values = {"root": self.root}
        values.update(kwargs)
        return om.Config(**values)

    def write_free_pool_script(self, payer: str = "openai", budget_report: dict | None = None) -> Path:
        directory = Path(self.temporary_directory.name)
        script = directory / "free_pool_call.py"
        payer_path = directory / "payer.txt"
        payer_path.write_text(payer, encoding="utf-8")
        if budget_report is None:
            budget_report = {
                "precheck": "budget-only",
                "budget": {
                    "utc_day": "2026-01-01",
                    "developer_billed": 7,
                    "possibly_billed": 0,
                    "totals": {"official_10M": 50},
                    "ceiling": 100,
                    "published_cap": 200,
                },
            }
        script.write_text(
            "\n".join(
                [
                    "import json",
                    "import os",
                    "import sys",
                    "",
                    "directory = os.path.dirname(__file__)",
                    "if '--budget-only' in sys.argv:",
                    f"    print({json.dumps(budget_report)!r})",
                    "    sys.exit(0)",
                    "with open(os.path.join(directory, 'argv.json'), 'w', encoding='utf-8') as handle:",
                    "    json.dump(sys.argv, handle)",
                    "with open(os.path.join(directory, 'payer.txt'), encoding='utf-8') as handle:",
                    "    payer = handle.read().strip()",
                    "data = {",
                    "    'output_text': json.dumps({",
                    "        'title': 'Test task',",
                    "        'goal': 'Verify orchestrator planning',",
                    "        'steps': ['write tests'],",
                    "        'acceptance': ['tests pass'],",
                    "        'risks': [],",
                    "        'lane_hint': 'human',",
                    "    }),",
                    "    'model': 'gpt-5.6-terra',",
                    "    'response_id': 'resp_test1',",
                    "    'usage': {'input_tokens': 12, 'output_tokens': 34, 'total_tokens': 46},",
                    "    'billing': {'payer': payer} if payer else {},",
                    "}",
                    "print(json.dumps(data))",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.chmod(script, 0o700)
        return script

    def test_stdio_dispatches_initialize_and_tools_list(self) -> None:
        orchestrator = om.Orchestrator(self.config())
        input_stream = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                }
            )
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n"
        )
        output_stream = io.StringIO()

        om.run_stdio(orchestrator, input_stream, output_stream)

        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(responses[1]["result"]["tools"][0]["name"], "status")
        self.assertEqual(len(responses[1]["result"]["tools"]), 4)

    def test_plan_invokes_free_pool_and_writes_private_receipt(self) -> None:
        script = self.write_free_pool_script()
        orchestrator = om.Orchestrator(self.config(free_pool_call=str(script)))

        result = orchestrator.plan({"request": "Draft a small test task.", "project": "agiw-suite"})

        self.assertEqual(result["response_id"], "resp_test1")
        self.assertEqual(result["payer"], "openai")
        self.assertEqual(result["usage"]["total_tokens"], 46)
        argv = json.loads((script.parent / "argv.json").read_text(encoding="utf-8"))
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5.6-terra")
        self.assertIn("--lane", argv)
        self.assertEqual(argv[argv.index("--lane") + 1], om.LANE)
        self.assertIn("--no-reroute", argv)
        self.assertIn("--max-output", argv)
        self.assertEqual(argv[argv.index("--max-output") + 1], "600")

        receipt_path = orchestrator.receipt_path
        receipt = json.loads(receipt_path.read_text(encoding="utf-8").strip())
        self.assertEqual(receipt["lane"], om.LANE)
        self.assertEqual(receipt["tool"], "plan")
        self.assertNotIn("Draft a small test task.", receipt_path.read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)

    def test_plan_rejects_unapproved_models_without_subprocess(self) -> None:
        script = self.write_free_pool_script()
        orchestrator = om.Orchestrator(self.config(free_pool_call=str(script)))

        request = {"request": "Make a plan.", "project": "agiw-suite"}
        sol = orchestrator.call_tool("plan", {**request, "model": "sol"})
        gpt_sol = orchestrator.call_tool("plan", {**request, "model": "gpt-6.2-sol"})

        self.assertTrue(sol["isError"])
        self.assertEqual(sol["structuredContent"]["code"], "MODEL_REFUSED")
        self.assertTrue(gpt_sol["isError"])
        self.assertEqual(gpt_sol["structuredContent"]["code"], "MODEL_REFUSED")
        self.assertFalse((script.parent / "argv.json").exists())

    def test_plan_refused_marker_does_not_call_or_write_receipt(self) -> None:
        script = self.write_free_pool_script()
        orchestrator = om.Orchestrator(self.config(free_pool_call=str(script)))
        request = "Please inspect " + om.REFUSED_MARKERS[0] + " and make a plan."

        response = orchestrator.call_tool("plan", {"request": request, "project": "agiw-suite"})

        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"]["code"], "SOURCE_REFUSED")
        self.assertFalse((script.parent / "argv.json").exists())
        self.assertFalse(orchestrator.receipt_path.exists())

    def test_plan_requires_an_approved_project_before_calling_the_model(self) -> None:
        script = self.write_free_pool_script()
        orchestrator = om.Orchestrator(self.config(free_pool_call=str(script)))
        for project in ({}, {"project": "none"}, {"project": "other"}):
            with self.subTest(project=project):
                result = orchestrator.call_tool("plan", {"request": "Draft a task.", **project})
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"]["code"], "PROJECT_INVALID")
        self.assertFalse((script.parent / "argv.json").exists())

    def test_plan_pauses_after_non_openai_payer(self) -> None:
        for payer in ("developer", ""):
            with self.subTest(payer=payer), tempfile.TemporaryDirectory() as root:
                script = self.write_free_pool_script(payer=payer)
                orchestrator = om.Orchestrator(self.config(free_pool_call=str(script), root=Path(root)))

                request = {"request": "Draft a task.", "project": "agiw-suite"}
                refused = orchestrator.call_tool("plan", request)

                self.assertTrue(refused["isError"])
                self.assertEqual(refused["structuredContent"]["code"], "PAYER_REFUSED")
                self.assertNotIn("brief", refused["structuredContent"])
                self.assertTrue(orchestrator.paused_path.is_file())
                response = orchestrator.call_tool("plan", request)
                self.assertTrue(response["isError"])
                self.assertEqual(response["structuredContent"]["code"], "PAYER_PAUSED")

    def test_plan_pauses_on_guard_payer_refusal_without_reading_model_output(self) -> None:
        script = self.write_free_pool_script()
        script.write_text("print('Untrusted guard output')\nraise SystemExit(7)\n", encoding="utf-8")
        orchestrator = om.Orchestrator(self.config(free_pool_call=str(script)))

        request = {"request": "Draft a task.", "project": "agiw-suite"}
        refused = orchestrator.call_tool("plan", request)

        self.assertTrue(refused["isError"])
        self.assertEqual(refused["structuredContent"]["code"], "PAYER_REFUSED")
        self.assertTrue(orchestrator.paused_path.is_file())
        receipt_text = orchestrator.receipt_path.read_text(encoding="utf-8")
        receipt = json.loads(receipt_text)
        self.assertEqual(receipt["outcome"], "BLOCKED")
        self.assertEqual(receipt["code"], "PAYER_REFUSED")
        self.assertNotIn("Untrusted guard output", receipt_text)
        response = orchestrator.call_tool("plan", {**request, "model": "luna"})
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"]["code"], "PAYER_PAUSED")

    def test_queue_task_writes_private_record_and_rejects_secret(self) -> None:
        orchestrator = om.Orchestrator(self.config())

        result = orchestrator.queue_task(
            {
                "title": "Add queue coverage",
                "brief": {"goal": "Verify queue files"},
                "project": "agiw-suite",
                "lane_hint": "human",
            }
        )

        path = Path(result["path"])
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["id"], result["id"])
        self.assertEqual(record["title"], "Add queue coverage")
        self.assertEqual(record["project"], "agiw-suite")
        self.assertEqual(record["lane"], om.LANE)
        self.assertEqual(record["status"], "queued")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        prefix = "s" + "k-"
        secret = prefix + ("a" * 40)
        response = orchestrator.call_tool(
            "queue_task",
            {"title": "Secret task", "brief": secret, "project": "agiw-suite"},
        )
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"]["code"], "SECRET_VALUE_REFUSED")

    def test_task_result_rejects_traversal_and_round_trips_result(self) -> None:
        orchestrator = om.Orchestrator(self.config())
        queued = orchestrator.queue_task({"title": "Result task", "brief": "Wait for result.", "project": "none"})
        identifier = queued["id"]
        om._mkdir_private(orchestrator.results)
        result_path = orchestrator.results / (identifier + ".json")
        result_path.write_text('{"status":"complete"}', encoding="utf-8")

        with self.assertRaises(om.PoolRefused) as context:
            orchestrator.task_result({"id": "../x"})
        self.assertEqual(context.exception.code, "ID_INVALID")

        result = orchestrator.task_result({"id": identifier})
        self.assertEqual(result["id"], identifier)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["result"], '{"status":"complete"}')

    @unittest.skipIf(os.name == "nt", "symlink behavior is platform-specific on Windows")
    def test_task_result_rejects_symlink(self) -> None:
        orchestrator = om.Orchestrator(self.config())
        identifier = "20240101T000000Z-task-abcdef"
        om._mkdir_private(orchestrator.results)
        outside = Path(self.temporary_directory.name) / "outside.json"
        outside.write_text("not a result", encoding="utf-8")
        link = orchestrator.results / (identifier + ".json")
        link.symlink_to(outside)

        result = orchestrator.task_result({"id": identifier})

        self.assertEqual(result, {"id": identifier, "status": "unknown"})

    def test_status_reports_ci_and_only_today_totals(self) -> None:
        directory = Path(self.temporary_directory.name)
        gh = directory / "fake_gh.py"
        gh.write_text(
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "",
                    "repo = sys.argv[sys.argv.index('-R') + 1]",
                    "rollups = {",
                    "    'success': [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}],",
                    "    'failure': [{'status': 'COMPLETED', 'conclusion': 'FAILURE'}],",
                    "    'pending': [{'status': 'IN_PROGRESS', 'conclusion': None}],",
                    "    'none': [],",
                    "}",
                    "key = repo.rsplit('/', 1)[-1]",
                    "print(json.dumps([{",
                    "    'number': 1,",
                    "    'title': key,",
                    "    'url': 'https://example.invalid/' + key,",
                    "    'isDraft': False,",
                    "    'headRefName': 'test',",
                    "    'statusCheckRollup': rollups[key],",
                    "}]))",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.chmod(gh, 0o700)
        repos = [
            "example/success",
            "example/failure",
            "example/pending",
            "example/none",
        ]
        script = self.write_free_pool_script()
        orchestrator = om.Orchestrator(
            self.config(gh=str(gh), repos=repos, fleet_configs=[], free_pool_call=str(script))
        )
        today = om._utc_now().strftime("%Y-%m-%d")
        orchestrator._append_receipt(
            {
                "at": today + "T01:00:00Z",
                "payer": "openai",
                "usage": {"total_tokens": 12},
            }
        )
        orchestrator._append_receipt(
            {
                "at": "2000-01-01T00:00:00Z",
                "payer": "developer",
                "usage": {"total_tokens": 99},
            }
        )

        status = orchestrator.status({"include_github": True})

        self.assertEqual(status["machines"], [])
        self.assertEqual(status["free_pool"]["today"]["calls"], 1)
        self.assertEqual(status["free_pool"]["today"]["total_tokens"], 12)
        self.assertEqual(status["free_pool"]["today"]["billed_calls"], 0)
        self.assertEqual(status["free_pool"]["pool_today"]["developer_billed"], 7)
        self.assertEqual(status["free_pool"]["pool_today"]["totals"], {"official_10M": 50})
        self.assertFalse((script.parent / "argv.json").exists())
        self.assertEqual(status["github"]["example/success"][0]["ci"], "success")
        self.assertEqual(status["github"]["example/failure"][0]["ci"], "failure")
        self.assertEqual(status["github"]["example/pending"][0]["ci"], "pending")
        self.assertEqual(status["github"]["example/none"][0]["ci"], "none")

    def test_status_reports_strict_pool_budget_without_extra_fields(self) -> None:
        expected = {
            "utc_day": "2026-01-01",
            "machine_id": "m5",
            "share": 0.6,
            "groups": {"official_10M": {"used": 50, "allowed": 100, "billed_models_today": ["gpt-5.4-mini"]}},
        }
        strict_report = {
            **expected,
            "receipts_dir": "internal",
            "groups": {
                "official_10M": {**expected["groups"]["official_10M"], "ledger_file": "internal"},
            },
        }
        script = self.write_free_pool_script(budget_report=strict_report)
        orchestrator = om.Orchestrator(self.config(free_pool_call=str(script)))

        status = orchestrator.status({"include_github": False})

        self.assertEqual(status["free_pool"]["pool_today"], expected)
        self.assertFalse((script.parent / "argv.json").exists())


class HttpServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "root"
        self.token_file = Path(self.temporary_directory.name) / "token"
        self.token_file.write_text("test-token\n", encoding="utf-8")
        os.chmod(self.token_file, 0o600)
        self.config = om.Config(
            root=self.root,
            host="127.0.0.1",
            port=0,
            token_file=self.token_file,
            token=om._read_token(self.token_file),
        )
        self.orchestrator = om.Orchestrator(self.config)
        self.server = om.make_http_server(self.config, self.orchestrator)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        deadline = time.monotonic() + 5.0
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    self.fail("HTTP server did not become ready in time")
                time.sleep(0.01)
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.health_url = f"http://127.0.0.1:{port}/healthz"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.temporary_directory.cleanup()

    def request(
        self,
        url: str,
        method: str = "POST",
        body: object | None = None,
        token: str | None = None,
        origin: str | None = None,
    ) -> tuple[int, bytes]:
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if origin is not None:
            headers["Origin"] = origin
        headers["Connection"] = "close"
        last_error = None
        for _ in range(3):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                response = self.opener.open(request, timeout=2)
            except urllib.error.HTTPError as error:
                try:
                    return error.code, error.read()
                finally:
                    error.close()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as error:
                last_error = error
                continue
            except urllib.error.URLError as error:
                if isinstance(
                    error.reason,
                    (ConnectionAbortedError, ConnectionResetError, BrokenPipeError),
                ):
                    last_error = error
                    continue
                raise
            with response:
                return response.status, response.read()
        if last_error is not None:
            raise last_error
        raise AssertionError("request failed without a response")

    def test_http_auth_routes_origin_and_notifications(self) -> None:
        status, _body = self.request(
            self.url,
            body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        self.assertEqual(status, 401)

        status, _body = self.request(
            self.url,
            body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            token="wrong-token",
        )
        self.assertEqual(status, 401)

        status, body = self.request(
            self.url,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            token="test-token",
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["protocolVersion"], "2025-06-18")

        status, body = self.request(
            self.url,
            body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            token="test-token",
        )
        self.assertEqual(status, 200)
        names = [tool["name"] for tool in json.loads(body)["result"]["tools"]]
        self.assertEqual(names, ["status", "plan", "queue_task", "task_result"])

        status, _body = self.request(
            self.url,
            body={"jsonrpc": "2.0", "id": 3, "method": "ping"},
            token="test-token",
            origin="https://evil.example",
        )
        self.assertEqual(status, 403)

        status, _body = self.request(self.url, method="GET")
        self.assertEqual(status, 405)

        status, _body = self.request(self.url + "/other", method="GET")
        self.assertEqual(status, 404)

        status, body = self.request(self.health_url, method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

        status, _body = self.request(
            self.url,
            body={"jsonrpc": "2.0", "method": "notifications/initialized"},
            token="test-token",
        )
        self.assertEqual(status, 202)

    def test_non_loopback_host_is_refused(self) -> None:
        config = om.Config(root=self.root, host="0.0.0.0", port=0, token="test-token")

        with self.assertRaises(ValueError):
            om.make_http_server(config)


class MainValidationTestCase(unittest.TestCase):
    def test_no_auth_without_stdio_is_refused(self) -> None:
        self.assertEqual(om.main(["--no-auth"]), 2)

    def test_non_loopback_host_is_refused_by_main(self) -> None:
        self.assertEqual(om.main(["--host", "0.0.0.0"]), 2)

    @unittest.skipIf(os.name == "nt", "POSIX token permission checks do not apply on Windows")
    def test_broad_token_file_permissions_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text("test-token\n", encoding="utf-8")
            os.chmod(token_file, 0o644)

            self.assertEqual(om.main(["--token-file", str(token_file)]), 2)


if __name__ == "__main__":
    unittest.main()
