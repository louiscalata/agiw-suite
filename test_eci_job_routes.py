"""Read-only check-job routes: fake runner, fake lanes, no model call.

The route object is tested directly, then through an in-process observer on a
loopback port chosen by the system. Nothing here contacts a lane.
"""

import ast
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
import unittest
from urllib.parse import urlencode

import eci_job_routes as routes
import eci_jobs as jobs
from server import MonitorServer, SnapshotStore
from test_eci_jobs import EngineCase, request, targets

ROOT = Path(__file__).resolve().parent
UNCONFIGURED = {
    "status": "unknown",
    "reason": "Check jobs are not configured.",
    "owner": False,
    "gate": "closed",
    "jobs": [],
    "active": None,
}


def query(value=None, **facts):
    value = dict(request() if value is None else value)
    pairs = [(key, "true" if item is True else "false" if item is False else str(item)) for key, item in value.items()]
    pairs += [("facts." + key, str(item)) for key, item in facts.items()]
    return urlencode(pairs)


class RouteCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.routes = routes.ReadOnlyRoutes(self.engine)

    def assert_nothing_happened(self, journal):
        self.assertEqual(self.journal(), journal)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.sink.records, [])


class ReadOnlyRouteTests(RouteCase):
    def test_status_is_the_engine_status(self):
        job = self.submit()
        before = self.journal()
        status, body = self.routes("/api/eci-jobs", "")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.engine.status())
        self.assertEqual((body["status"], body["gate"], body["owner"]), ("ok", "open", True))
        self.assertEqual([row["job_id"] for row in body["jobs"]], [job["job_id"]])
        json.dumps(body, allow_nan=False)
        self.assert_nothing_happened(before)

    def test_job_detail_is_one_job_view(self):
        job = self.submit()
        before = self.journal()
        self.assertEqual(self.routes("/api/eci-jobs/jobs/" + job["job_id"], ""), (200, self.engine.job(job["job_id"])))
        self.assertEqual(
            self.routes("/api/eci-jobs/jobs/" + "0" * 32, ""),
            (404, {"status": "refused", "code": "unknown_job", "message": "No job has this id."}),
        )
        for bad in (
            "",
            "abc",
            "A" * 32,
            "0" * 31,
            "0" * 33,
            job["job_id"] + "/",
            job["job_id"] + "/x",
            "../" + job["job_id"],
        ):
            with self.subTest(job_id=bad):
                self.assertEqual(self.routes("/api/eci-jobs/jobs/" + bad, "")[0], 404)
        self.assert_nothing_happened(before)

    def test_preview_matches_the_engine_preview_and_queues_nothing(self):
        value = request(max_tokens=8192, wall_budget_s="30.5", request_timeout_s=20, disable_thinking=True)
        status, body = self.routes(
            "/api/eci-jobs/preview",
            query(
                value, model_sha256="a" * 64, quantization="Q4_K_M", file_size_bytes=100, model_label="synthetic/label"
            ),
        )
        expected = self.engine.preview(
            dict(
                value,
                facts={
                    "model_sha256": "a" * 64,
                    "quantization": "Q4_K_M",
                    "file_size_bytes": 100,
                    "model_label": "synthetic/label",
                },
            )
        )
        self.assertEqual((status, body), (200, expected))
        self.assertEqual(body["refusals"], [])
        self.assertEqual(body["request"]["max_tokens"], 8192)
        self.assertIs(body["request"]["disable_thinking"], True)
        self.assertIs(body["effects"]["starts_model"], False)
        self.assertEqual(self.engine.status()["jobs"], [])
        self.assert_nothing_happened(b"")

    def test_preview_defaults_match_a_request_without_optional_fields(self):
        status, body = self.routes("/api/eci-jobs/preview", query())
        self.assertEqual((status, body), (200, self.engine.preview(request())))

    def test_preview_shows_the_lane_context_refusal(self):
        self.lanes = targets()
        self.lanes[0]["contextTokens"] = 8192
        status, body = self.routes("/api/eci-jobs/preview", query(request(max_tokens=16384)))
        self.assertEqual(status, 200)
        self.assertEqual([row["code"] for row in body["refusals"]], ["context_exceeded"])
        self.assertEqual(body["effects"]["lane_context_tokens"], 8192)
        self.assert_nothing_happened(b"")

    def test_malformed_preview_queries_refuse_with_a_fixed_answer(self):
        good = query()
        cases = {
            "empty": "",
            "duplicate": good + "&count=4",
            "unknown field": good + "&base_url=x",
            "facts object": good + "&facts=%7B%7D",
            "unknown fact": good + "&facts.path=x",
            "no value": good + "&count",
            "leading zero": query(request(count="04")),
            "signed": query(request(count="+4")),
            "decimal": query(request(count="4.0")),
            "wide digit": good.replace("count=4", "count=%EF%BC%94"),
            "long integer": query(request(count="1" * 11)),
            "boolean spelling": good + "&disable_thinking=True",
            "integer fact": good + "&facts.memory_bytes=1e3",
            "too long": good + "&facts.cpu=" + "x" * routes.MAX_QUERY_BYTES,
            "too many fields": "&".join("f%d=1" % index for index in range(64)),
            "not text": None,
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                self.assertEqual(
                    self.routes("/api/eci-jobs/preview", value),
                    (
                        400,
                        {
                            "status": "refused",
                            "code": "invalid_request",
                            "message": "The preview query is not a valid check request.",
                        },
                    ),
                )
        self.assert_nothing_happened(b"")

    def test_engine_refusals_keep_their_code_and_text(self):
        status, body = self.routes("/api/eci-jobs/preview", query(request(target="lane")))
        self.assertEqual((status, body["code"]), (400, "invalid_request"))
        self.assertEqual(body["message"], "target must be a registry lane id.")
        status, body = self.routes("/api/eci-jobs/preview", query(request(wall_budget_s="601")))
        self.assertEqual((status, body["status"], body["code"]), (409, "refused", "budget_refused"))

    def test_unknown_paths_are_not_found(self):
        for path in (
            "/api/eci-jobs/",
            "/api/eci-jobs/jobs",
            "/api/eci-jobs/jobs/",
            "/api/eci-jobs/submit",
            "/api/eci-jobs/cancel",
            "/api/eci-jobs/resume",
            "/api/eci-jobs/run",
            "/api/eci-jobs/preview/",
            "/api/eci-tuning",
            "/",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.routes(path, ""),
                    (404, {"status": "refused", "code": "not_found", "message": "No such check-job route."}),
                )

    def test_a_second_engine_that_owns_nothing_answers_unknown_and_unavailable(self):
        job = self.submit()
        other = routes.ReadOnlyRoutes(self.open())
        status, body = other("/api/eci-jobs", "")
        self.assertEqual((status, body["status"], body["owner"], body["jobs"]), (200, "unknown", False, []))
        status, body = other("/api/eci-jobs/jobs/" + job["job_id"], "")
        self.assertEqual((status, body["code"]), (503, "not_owner"))
        status, body = other("/api/eci-jobs/preview", query())
        self.assertEqual(status, 200)
        self.assertIn("not_owner", [row["code"] for row in body["refusals"]])


class ClosedByDefaultTests(RouteCase):
    def test_an_engine_with_no_policy_shows_a_closed_gate_and_refuses_every_preview(self):
        engine = self.open(state=self.root / "closed", policy=jobs.CLOSED, runner=None, sink=None)
        closed = routes.ReadOnlyRoutes(engine)
        status, body = closed("/api/eci-jobs", "")
        self.assertEqual((status, body["status"], body["gate"], body["jobs"]), (200, "ok", "closed", []))
        self.assertEqual(closed("/api/eci-jobs/preview", query())[1]["code"], "budget_required")
        status, body = closed("/api/eci-jobs/preview", query(request(wall_budget_s="60")))
        self.assertEqual((status, body["code"]), (409, "budget_refused"))
        self.assertEqual(self.journal(self.root / "closed"), b"")

    def test_a_closed_gate_is_listed_in_the_preview(self):
        self.gate["open"] = False
        status, body = self.routes("/api/eci-jobs/preview", query())
        self.assertEqual(status, 200)
        self.assertEqual([row["code"] for row in body["refusals"]], ["gate_closed"])
        self.assertEqual(self.routes("/api/eci-jobs", "")[1]["gate"], "closed")

    def test_the_routes_need_an_engine(self):
        for bad in (None, object(), {"status": "ok"}):
            with self.assertRaises(ValueError):
                routes.ReadOnlyRoutes(bad)


class ObserverCase(RouteCase):
    check_jobs = "routes"

    def setUp(self):
        super().setUp()
        options = {} if self.check_jobs is None else {"check_jobs": getattr(self, self.check_jobs)}
        self.server = MonitorServer(("127.0.0.1", 0), SnapshotStore(), **options)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def call(self, path, method="GET", headers=None, body=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read(), response
        finally:
            connection.close()

    def get(self, path, headers=None):
        status, kind, body, _ = self.call(path, headers=headers)
        return status, kind, json.loads(body) if kind == "application/json" else body


class ObserverRouteTests(ObserverCase):
    def test_the_three_reads_are_served_as_json_and_write_nothing(self):
        job = self.submit()
        before = self.journal()
        self.assertEqual(self.get("/api/eci-jobs"), (200, "application/json", self.engine.status()))
        self.assertEqual(
            self.get("/api/eci-jobs/jobs/" + job["job_id"]), (200, "application/json", self.engine.job(job["job_id"]))
        )
        other = request(submission_id="submission-0002")
        self.assertEqual(
            self.get("/api/eci-jobs/preview?" + query(other)), (200, "application/json", self.engine.preview(other))
        )
        self.assertEqual(self.get("/api/eci-jobs/jobs/" + "f" * 32)[0], 404)
        self.assertEqual(self.get("/api/eci-jobs/preview?count=4")[0], 400)
        self.assert_nothing_happened(before)

    def test_answers_are_not_cached_and_not_sniffed(self):
        response = self.call("/api/eci-jobs")[3]
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")

    def test_another_host_or_origin_is_refused_before_the_engine_is_read(self):
        seen = []
        self.server.check_jobs = lambda path, text: seen.append(path) or (200, {})
        own = "127.0.0.1:%d" % self.port
        for headers in (
            {"Host": "example.invalid"},
            {"Host": own, "Origin": "http://example.invalid"},
            {"Host": own, "Origin": "null"},
            {"Host": "127.0.0.1:%d" % (self.port + 1)},
        ):
            for path in ("/api/eci-jobs", "/api/eci-jobs/preview?" + query(), "/api/eci-jobs/jobs/" + "0" * 32):
                with self.subTest(headers=headers, path=path):
                    self.assertEqual(self.call(path, headers=headers)[0], 403)
        self.assertEqual(seen, [])
        self.assertEqual(self.get("/api/eci-jobs", {"Host": own, "Origin": "http://" + own})[0], 200)
        self.assertEqual(seen, ["/api/eci-jobs"])

    def test_no_method_but_get_reaches_the_engine(self):
        job = self.submit()
        before = self.journal()
        own = "127.0.0.1:%d" % self.port
        headers = {
            "Host": own,
            "Origin": "http://" + own,
            "Content-Type": "application/json",
            "X-AGIW-Control": "anything",
        }
        payload = json.dumps({"job_id": job["job_id"]})
        for path in (
            "/api/eci-jobs",
            "/api/eci-jobs/submit",
            "/api/eci-jobs/cancel",
            "/api/eci-jobs/resume",
            "/api/eci-jobs/preview",
            "/api/eci-jobs/jobs/" + job["job_id"],
            "/api/eci-jobs/jobs/" + job["job_id"] + "/cancel",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, "POST", headers, payload)[0], 404)
                for method in ("PUT", "DELETE", "PATCH"):
                    self.assertEqual(self.call(path, method, headers, payload)[0], 501)
        self.assertEqual(self.engine.job(job["job_id"])["state"], "queued")
        self.assert_nothing_happened(before)

    def test_a_failing_route_object_answers_unknown(self):
        for broken in (
            lambda path, text: 1 / 0,
            lambda path, text: (200, {"value": float("nan")}),
            lambda path, text: (200, object()),
            lambda path, text: None,
            lambda path, text: ("200", {}),
            lambda path, text: (True, {}),
            lambda path, text: (99, {}),
            lambda path, text: (200, []),
        ):
            self.server.check_jobs = broken
            self.assertEqual(self.get("/api/eci-jobs"), (503, "application/json", {"status": "unknown"}))

    def test_other_paths_under_the_prefix_are_not_routed(self):
        seen = []
        self.server.check_jobs = lambda path, text: seen.append(path) or (200, {})
        for path in (
            "/api/eci-jobs/",
            "/api/eci-jobs/submit",
            "/api/eci-jobs/cancel",
            "/api/eci-jobs/jobs",
            "/api/eci-jobsx",
            "/api/eci-jobs/previewx",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.call(path)[0], 404)
        self.assertEqual(seen, [])


class UnconfiguredObserverTests(ObserverCase):
    check_jobs = None

    def test_the_observer_ships_with_no_engine_and_says_so(self):
        self.assertIsNone(self.server.check_jobs)
        self.assertEqual(self.get("/api/eci-jobs"), (200, "application/json", UNCONFIGURED))
        for path in ("/api/eci-jobs/preview?" + query(), "/api/eci-jobs/jobs/" + "0" * 32):
            with self.subTest(path=path):
                self.assertEqual(
                    self.get(path),
                    (503, "application/json", {"status": "unknown", "reason": "Check jobs are not configured."}),
                )
        self.assertEqual(self.call("/api/eci-jobs/submit")[0], 404)
        # The engine in this fixture was never handed to the observer.
        self.assert_nothing_happened(b"")

    def test_only_a_callable_or_nothing_is_accepted(self):
        for bad in ("routes", 1, self.engine, {"status": "ok"}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                MonitorServer(("127.0.0.1", 0), SnapshotStore(), check_jobs=bad)


class SourceTests(unittest.TestCase):
    def tree(self, name):
        return ast.parse((ROOT / name).read_text(encoding="utf-8"))

    def imported(self, name):
        found = set()
        for node in ast.walk(self.tree(name)):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add((node.module or "").split(".")[0])
        return found

    def test_the_routes_call_only_the_three_reads(self):
        tree = self.tree("eci_job_routes.py")
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse(
            names
            & {
                "submit",
                "resume",
                "cancel",
                "run_next",
                "close",
                "preview_resume",
                "Thread",
                "urlopen",
                "create_connection",
                "Popen",
                "system",
            }
        )
        self.assertTrue({"status", "job", "preview"} <= names)
        self.assertFalse(
            self.imported("eci_job_routes.py")
            & {
                "subprocess",
                "socket",
                "http",
                "threading",
                "asyncio",
                "server",
                "model_control",
                "eci_fast_check_adapter",
                "remote_machines",
                "multiprocessing",
            }
        )

    def test_the_observer_imports_the_engine_lazily_and_bundles_it(self):
        engine = {"eci_job_host", "eci_jobs", "eci_job_routes", "eci_fast_check_adapter"}
        top = set()
        for node in self.tree("server.py").body:
            if isinstance(node, ast.Import):
                top.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                top.add((node.module or "").split(".")[0])
        self.assertFalse(top & engine)
        self.assertEqual(self.imported("server.py") & engine, {"eci_job_host"})
        for script in ("build.sh", "package-release.sh"):
            text = (ROOT / script).read_text(encoding="utf-8")
            for name in sorted(engine):
                self.assertIn(name + ".py", text)

    def test_the_observer_adds_no_check_job_post_route(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        post = source[source.index("    def do_POST(self):") : source.index("    def owner_action(")]
        self.assertGreater(len(post), 2000)
        self.assertFalse(
            "eci-jobs" in post or "check_jobs" in post or "CHECK_JOBS" in post, "do_POST mentions the check-job routes"
        )
        tree = self.tree("server.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "OWNER_ACTION_ROUTES" for t in node.targets
            ):
                self.assertNotIn("eci-jobs", ast.dump(node))
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(calls & {"submit", "resume", "run_next", "preview_resume"})


if __name__ == "__main__":
    unittest.main()
