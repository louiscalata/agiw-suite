"""Check-job host: one explicit config builds a real engine with the gate closed.

The registry here is a temporary copy shaped like the operator's lanes. Nothing
contacts a lane or a model. The observer runs in the test process on a loopback
port chosen by the system.
"""

import ast
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlencode

import eci_job_host as host
import eci_job_routes as routes
import eci_jobs as jobs
import remote_machines
import server
from server import MonitorServer, SnapshotStore
from test_eci_jobs import datasets

ROOT = Path(__file__).resolve().parent
FAST, REVIEW = "registry:pc:fast", "registry:pc:review"
GLIMMER, MACPRO = "registry:pc:glimmer", "registry:macpro:glimmer"
# Same shape and numbers as the operator's lane registry; a temporary copy, never the real file.
REGISTRY = {
    "schemaVersion": 1,
    "machines": [
        {
            "id": "pc",
            "label": "Windows PC",
            "lanes": [
                {"id": "fast", "label": "Fast", "localPort": 11247, "contextTokens": 32768},
                {"id": "review", "label": "Review", "localPort": 11237, "contextTokens": 16384},
                {"id": "glimmer", "label": "Glimmer - retiring", "localPort": 11236, "timeoutSeconds": 3},
            ],
        },
        {
            "id": "macpro",
            "label": "Mac Pro",
            "lanes": [
                {"id": "glimmer", "label": "Glimmer", "localPort": 11280, "timeoutSeconds": 5, "contextTokens": 4096}
            ],
        },
    ],
}


def preview_query(**fields):
    value = {
        "submission_id": "submission-0001",
        "target": FAST,
        "model_id": "google/gemma-4-12b-it",
        "sample_set": "screening",
        "count": 4,
    }
    value.update(fields)
    return urlencode([(key, str(item)) for key, item in value.items()])


class HostCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.samples = self.root / "samples.json"
        self.samples.write_text(json.dumps(datasets(6)), encoding="utf-8")
        self.registry = self.root / "remote-machines.json"
        self.write(self.registry, REGISTRY)
        self.config = self.root / "check-jobs.json"
        self.values = {
            "state_dir": str(self.root / "state"),
            "dataset_dir": str(self.root / "dataset"),
            "sample_sets": {"screening": [str(self.samples)]},
            "allowed_targets": [FAST, REVIEW],
            "max_wall_budget_s": "1500",
            "default_wall_budget_s": "1500",
        }
        self.write(self.config, self.values)

    def write(self, path, value):
        path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding="utf-8")
        os.chmod(path, 0o600)

    def open(self, path=None):
        opened = host.open_host(path or self.config, registry=lambda: remote_machines.load_config(path=self.registry))
        self.addCleanup(opened.close)
        return opened

    def preview(self, opened, **fields):
        status, body = opened.routes("/api/eci-jobs/preview", preview_query(**fields))
        self.assertEqual(status, 200, body)
        return body

    def codes(self, body):
        return [row["code"] for row in body["refusals"]]

    def journal(self):
        path = self.root / "state" / "jobs.jsonl"
        return path.read_bytes() if path.exists() else b""


class ConfigTests(HostCase):
    def test_a_valid_config_is_read_exactly(self):
        self.assertEqual(host.read_config(self.config), self.values)

    def test_every_field_is_required_and_no_other_field_is_accepted(self):
        for name in self.values:
            with self.subTest(missing=name):
                value = dict(self.values)
                del value[name]
                self.write(self.config, value)
                with self.assertRaises(host.ConfigError):
                    host.read_config(self.config)
        for extra in ("gate", "gate_open", "runner", "registry", "Notes"):
            with self.subTest(extra=extra):
                self.write(self.config, dict(self.values, **{extra: True}))
                with self.assertRaises(host.ConfigError):
                    host.read_config(self.config)

    def test_fields_are_checked(self):
        bad = {
            "state_dir": ["relative/state", "/a/../b", "", 7, None, str(self.root / "dataset")],
            "dataset_dir": ["dataset", "/x/../y", [], str(self.root / "state")],
            "sample_sets": [
                {},
                {"screening": []},
                {"screening": ["samples.json"]},
                {"screening": "/a.json"},
                {"Bad Id": [str(self.samples)]},
                {"screening": [str(self.samples), str(self.samples)]},
                [],
            ],
            "allowed_targets": ["registry:pc:fast", [FAST, FAST], ["pc:fast"], [7], ["http://127.0.0.1:11247"]],
            "max_wall_budget_s": [1500, "-1", "0", "abc", "1" * 33, None],
            "default_wall_budget_s": [1500, "1501", "0", None],
        }
        for name, values in bad.items():
            for value in values:
                with self.subTest(field=name, value=value):
                    self.write(self.config, dict(self.values, **{name: value}))
                    with self.assertRaises(host.ConfigError):
                        host.read_config(self.config)

    def test_the_file_is_read_only_when_it_is_safe(self):
        self.assertRaises(host.ConfigError, host.read_config, "check-jobs.json")
        self.assertRaises(host.ConfigError, host.read_config, self.root / "missing.json")
        link = self.root / "link.json"
        link.symlink_to(self.config)
        self.assertRaises(host.ConfigError, host.read_config, link)
        self.assertRaises(host.ConfigError, host.read_config, self.root)
        os.chmod(self.config, 0o620)
        self.assertRaises(host.ConfigError, host.read_config, self.config)
        os.chmod(self.config, 0o600)
        self.write(self.config, json.dumps(self.values) + " " * host.MAX_CONFIG_BYTES)
        self.assertRaises(host.ConfigError, host.read_config, self.config)

    def test_json_must_be_strict(self):
        text = json.dumps(self.values)
        for broken in (
            text[:-1],
            text[:-1] + ', "state_dir": "/elsewhere"}',
            text[:-1] + ', "x": NaN}',
            "[]",
            "null",
            "﻿" + text,
        ):
            with self.subTest(text=broken[-40:]):
                self.write(self.config, broken)
                self.assertRaises(host.ConfigError, host.read_config, self.config)


class EngineTests(HostCase):
    def test_the_engine_is_real_and_its_gate_is_closed(self):
        opened = self.open()
        self.assertIsInstance(opened.engine, jobs.Engine)
        self.assertIsInstance(opened.routes, routes.ReadOnlyRoutes)
        policy = opened.engine._policy
        self.assertIs(policy.gate(), False)
        self.assertFalse(policy.gate_open())
        self.assertEqual(policy.allowed_targets, frozenset({FAST, REVIEW}))
        self.assertEqual((policy.max_wall_budget_s, policy.default_wall_budget_s), ("1500", "1500"))
        self.assertIs(opened.engine._runner, jobs.adapter_runner)
        self.assertIsInstance(opened.engine._sink, jobs.DatasetSink)
        self.assertEqual(opened.engine._sink._directory, self.root / "dataset")
        self.assertEqual(
            opened.routes("/api/eci-jobs", ""),
            (200, {"status": "ok", "reason": None, "owner": True, "gate": "closed", "jobs": [], "active": None}),
        )

    def test_submit_resume_and_start_are_impossible(self):
        opened = self.open()
        body = self.preview(opened)
        review = {"reviewed": True, "review_sha256": body["review_sha256"]}
        with self.assertRaises(jobs.Refused) as caught:
            opened.engine.submit(
                {
                    "submission_id": "submission-0001",
                    "target": FAST,
                    "model_id": "google/gemma-4-12b-it",
                    "sample_set": "screening",
                    "count": 4,
                },
                review,
            )
        self.assertEqual(caught.exception.code, "gate_closed")
        self.assertIsNone(opened.engine.run_next())
        self.assertEqual(opened.engine.status()["jobs"], [])
        self.assertEqual(self.journal(), b"")
        self.assertFalse((self.root / "dataset").exists())

    def test_an_allowed_lane_is_refused_only_by_the_closed_gate(self):
        opened = self.open()
        for target, context in ((FAST, 32768), (REVIEW, 16384)):
            with self.subTest(target=target):
                body = self.preview(opened, target=target)
                self.assertEqual(self.codes(body), ["gate_closed"])
                self.assertEqual(body["request"]["wall_budget_s"], "1500")
                self.assertEqual(body["request"]["max_tokens"], 16384)
                self.assertEqual(body["effects"]["lane_context_tokens"], context)
                self.assertEqual(body["effects"]["planned_requests"], 4)
                self.assertIs(body["effects"]["starts_model"], False)
        self.assertEqual(self.journal(), b"")

    def test_previews_show_the_refusals_of_the_live_registry(self):
        opened = self.open()
        self.assertEqual(self.codes(self.preview(opened, target=GLIMMER)), ["gate_closed", "target_not_allowed"])
        self.assertEqual(
            self.codes(self.preview(opened, target=MACPRO)), ["gate_closed", "target_not_allowed", "context_exceeded"]
        )
        self.assertEqual(
            self.codes(self.preview(opened, target=REVIEW, max_tokens=32768)), ["gate_closed", "context_exceeded"]
        )
        self.assertEqual(self.codes(self.preview(opened, target=FAST, max_tokens=32768)), ["gate_closed"])
        self.assertEqual(
            self.codes(self.preview(opened, target="registry:pc:missing")), ["gate_closed", "unknown_target"]
        )
        self.assertEqual(self.codes(self.preview(opened, sample_set="other")), ["gate_closed", "unknown_sample_set"])
        status, body = opened.routes("/api/eci-jobs/preview", preview_query(wall_budget_s="1501"))
        self.assertEqual((status, body["code"]), (409, "budget_refused"))
        self.assertEqual(self.journal(), b"")

    def test_the_registry_is_read_again_for_every_preview(self):
        opened = self.open()
        self.assertEqual(self.preview(opened, target=REVIEW)["effects"]["lane_context_tokens"], 16384)
        changed = json.loads(json.dumps(REGISTRY))
        changed["machines"][0]["lanes"][1]["contextTokens"] = 8192
        self.write(self.registry, changed)
        body = self.preview(opened, target=REVIEW)
        self.assertEqual(body["effects"]["lane_context_tokens"], 8192)
        self.assertEqual(self.codes(body), ["gate_closed", "context_exceeded"])
        self.registry.unlink()
        self.assertEqual(self.codes(self.preview(opened, target=REVIEW)), ["gate_closed", "unknown_target"])

    def test_the_default_registry_is_the_monitor_registry(self):
        with mock.patch.object(
            remote_machines, "load_config", return_value=remote_machines.load_config(path=self.registry)
        ) as load:
            opened = host.open_host(self.config)
            self.addCleanup(opened.close)
            self.assertEqual(self.preview(opened)["effects"]["lane_context_tokens"], 32768)
        load.assert_called_with()

    def test_close_releases_the_state_directory(self):
        first = self.open()
        second = self.open()
        self.assertEqual(second.routes("/api/eci-jobs", "")[1]["status"], "unknown")
        first.close()
        first.close()
        third = self.open()
        self.assertEqual(third.routes("/api/eci-jobs", "")[1]["status"], "ok")


class UnavailableTests(HostCase):
    def test_an_unusable_config_answers_unknown_and_builds_no_engine(self):
        self.write(self.config, dict(self.values, gate=True))
        opened = self.open()
        self.assertIsNone(opened.engine)
        self.assertEqual(
            opened.routes("/api/eci-jobs", ""),
            (
                200,
                {
                    "status": "unknown",
                    "reason": "The check-job configuration is not valid.",
                    "owner": False,
                    "gate": "closed",
                    "jobs": [],
                    "active": None,
                },
            ),
        )
        for path in ("/api/eci-jobs/preview", "/api/eci-jobs/jobs/" + "0" * 32):
            self.assertEqual(
                opened.routes(path, preview_query()),
                (503, {"status": "unknown", "reason": "The check-job configuration is not valid."}),
            )
        self.assertFalse((self.root / "state").exists())
        opened.close()

    def test_an_unreadable_config_has_its_own_fixed_reason(self):
        opened = self.open(self.root / "missing.json")
        self.assertEqual(
            opened.routes("/api/eci-jobs", "")[1]["reason"], "The check-job configuration cannot be read safely."
        )

    def test_an_engine_that_cannot_be_built_answers_unknown(self):
        with mock.patch.object(jobs, "Engine", side_effect=OSError("disk detail")):
            opened = self.open()
        body = opened.routes("/api/eci-jobs", "")[1]
        self.assertEqual((body["status"], body["reason"]), ("unknown", "The check-job engine cannot be opened."))
        self.assertNotIn("disk detail", json.dumps(body))


class ObserverTests(HostCase):
    def serve(self, check_jobs):
        observer = MonitorServer(("127.0.0.1", 0), SnapshotStore(), check_jobs=check_jobs)
        thread = threading.Thread(target=observer.serve_forever, daemon=True)
        thread.start()

        def stop():
            observer.shutdown()
            observer.server_close()
            thread.join(2)

        self.addCleanup(stop)
        return observer.server_address[1]

    def get(self, port, path):
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", path, headers={"Host": "127.0.0.1:%d" % port})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_the_observer_serves_the_real_engine_reads(self):
        opened = self.open()
        port = self.serve(opened.routes)
        status, body = self.get(port, "/api/eci-jobs")
        self.assertEqual((status, body["status"], body["gate"], body["owner"]), (200, "ok", "closed", True))
        status, body = self.get(port, "/api/eci-jobs/preview?" + preview_query(target=MACPRO))
        self.assertEqual(status, 200)
        self.assertEqual(self.codes(body), ["gate_closed", "target_not_allowed", "context_exceeded"])
        self.assertEqual(self.get(port, "/api/eci-jobs/jobs/" + "0" * 32)[1]["code"], "unknown_job")
        self.assertEqual(self.journal(), b"")

    def test_open_check_jobs_builds_a_host_only_when_a_config_is_named(self):
        self.assertIsNone(server.open_check_jobs(None))
        with mock.patch.object(
            remote_machines, "load_config", return_value=remote_machines.load_config(path=self.registry)
        ):
            opened = server.open_check_jobs(self.config)
        self.addCleanup(opened.close)
        self.assertIsInstance(opened, host.Host)
        self.assertEqual(opened.routes("/api/eci-jobs", "")[1]["gate"], "closed")

    def test_the_flag_is_parsed_and_the_engine_is_imported_only_with_it(self):
        code = (
            "import sys, server\n"
            "loaded = sorted(n for n in ('eci_job_host', 'eci_jobs', 'eci_job_routes',"
            " 'eci_fast_check_adapter') if n in sys.modules)\n"
            "print(loaded)\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "server.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--check-jobs-config", result.stdout)

    def test_server_imports_the_host_only_inside_open_check_jobs(self):
        tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
        engine = {"eci_job_host", "eci_jobs", "eci_job_routes", "eci_fast_check_adapter"}
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                top.add((node.module or "").split(".")[0])
        self.assertFalse(top & engine)
        lazy = [
            (function.name, alias.name)
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef)
            for node in ast.walk(function)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(node.module)])
            if alias.name.split(".")[0] in engine
        ]
        self.assertEqual(lazy, [("open_check_jobs", "eci_job_host")])

    def test_the_host_opens_no_socket_and_starts_nothing(self):
        tree = ast.parse((ROOT / "eci_job_host.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertFalse(
            imported
            & {
                "socket",
                "subprocess",
                "threading",
                "http",
                "urllib",
                "server",
                "model_control",
                "multiprocessing",
                "asyncio",
            }
        )
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse(names & {"submit", "resume", "run_next", "cancel", "preview_resume", "Thread"})


class BundleTests(unittest.TestCase):
    def test_both_packagers_bundle_the_host_and_its_engine(self):
        from test_package_resources import resources

        for script in ("build.sh", "package-release.sh"):
            for name in ("eci_jobs.py", "eci_job_routes.py", "eci_job_host.py", "eci_fast_check_adapter.py"):
                with self.subTest(script=script, name=name):
                    self.assertIn(name, resources(script))


if __name__ == "__main__":
    unittest.main()
