"""completion-grammar request mode end to end with synthetic llama-server routes; never invokes a model."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from support import NISI_AVAILABLE

from agiw import fleet_local_pilot as pilot
from agiw import fleet_router_client as rc
from agiw import windows_worker as ww

RENDERED = "<|start|>system<|message|>S<|eot|><|start|>user<|message|>Q<|eot|><|start|>assistant"
PREFILL = " to=self<|message|><|eom|><|start|>assistant to=user<|message|>"


def serve(template, completion, seen):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, d):
            b = json.dumps(d).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/health":
                return self.send(200, {"status": "ok"})
            if self.path == "/v1/models":
                return self.send(200, {"data": [{"id": "fixture-model"}]})
            return self.send(200, [{"is_processing": False}])

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, req))
            if self.path == "/apply-template":
                return self.send(*template)
            if self.path == "/completion":
                return self.send(*completion)
            return self.send(404, {"error": "chat route must not be used"})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def done(content, stop_type="eos", model="fixture-model"):
    return 200, {
        "content": content,
        "stop_type": stop_type,
        "tokens_predicted": 6,
        "tokens_evaluated": 60,
        "model": model,
    }


@unittest.skipUnless(NISI_AVAILABLE, "pinned Nisi vendor or node missing")
class CompletionModeTests(unittest.TestCase):
    def run_case(self, program, completion, template=(200, {"prompt": RENDERED}), worker="monterey-mac-pro", **kw):
        seen = self.seen = []
        server = serve(template, completion, seen)
        try:
            return pilot.execute(
                "live",
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                "fixture-model",
                program=program,
                worker_id=worker,
                **kw,
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_monterey_lane_defaults_to_completion_grammar(self):
        r = self.run_case("tiny-arithmetic.mjs", done('{"answer":42}'))
        self.assertEqual(r["report"]["outcome"], "COMPLETED")
        self.assertEqual(r["requestMode"], "completion-grammar")
        self.assertEqual([p for p, _ in self.seen], ["/apply-template", "/completion"])
        body = self.seen[1][1]
        self.assertEqual(body["prompt"], RENDERED + PREFILL)
        self.assertEqual((body["n_predict"], body["temperature"]), (512, 0))
        self.assertIn('"\\"answer\\"" ws ":" ws integer', body["grammar"])
        self.assertNotIn("json_schema", body)
        ev = r["authorEvidence"][0]
        self.assertEqual(
            {k: ev[k] for k in ("requestMode", "templateRender", "stopType", "tokensPredicted", "finishReason")},
            {
                "requestMode": "completion-grammar",
                "templateRender": "OK",
                "stopType": "eos",
                "tokensPredicted": 6,
                "finishReason": "stop",
            },
        )
        self.assertNotIn("6 multiplied", json.dumps(r["authorEvidence"]))

    def test_word_stop_is_stop_and_limit_fails_closed(self):
        self.assertEqual(
            self.run_case("tiny-arithmetic.mjs", done('{"answer":42}', "word"))["report"]["outcome"], "COMPLETED"
        )
        r = self.run_case("tiny-arithmetic.mjs", done('{"answer":42}', "limit"))
        self.assertEqual(r["report"]["outcome"], "BLOCKED")
        ev = r["authorEvidence"][0]
        self.assertEqual((ev["stopType"], ev["finishReason"]), ("limit", "length"))
        self.assertIn('{"answer":42}', ev["outputSample"])

    def test_wrong_answer_not_corrected(self):
        self.assertEqual(self.run_case("tiny-arithmetic.mjs", done('{"answer":41}'))["report"]["outcome"], "FAILED")

    def test_apply_template_failure_blocks_without_completion(self):
        r = self.run_case("tiny-arithmetic.mjs", done('{"answer":42}'), template=(500, {"error": {"message": "boom"}}))
        self.assertEqual(r["report"]["outcome"], "BLOCKED")
        self.assertEqual([p for p, _ in self.seen], ["/apply-template"])
        ev = r["authorEvidence"][0]
        self.assertEqual(
            (ev["templateRender"], ev["errorCode"], ev["httpStatus"]), ("FAILED", "TEMPLATE_RENDER_FAILED", 500)
        )
        self.assertEqual(ev["serverMessage"], "boom")

    def test_served_model_mismatch_refused(self):
        r = self.run_case("tiny-arithmetic.mjs", done('{"answer":42}', model="other"))
        self.assertEqual(r["report"]["outcome"], "BLOCKED")

    def test_extraction_grammar_from_its_schema(self):
        r = self.run_case("tiny-extraction.mjs", done('{"color":"red"}'))
        self.assertEqual(r["report"]["outcome"], "COMPLETED")
        self.assertIn('"\\"color\\"" ws ":" ws string', self.seen[1][1]["grammar"])

    def test_chat_stays_default_elsewhere(self):
        r = self.run_case("tiny-arithmetic.mjs", done('{"answer":42}'), worker="m5-macbook-pro")
        self.assertEqual(r["requestMode"], "chat")
        self.assertEqual([p for p, _ in self.seen], ["/v1/chat/completions"])


class CompletionConfigTests(unittest.TestCase):
    def lanes(self, lane):
        return mock.patch.object(pilot, "lane_defaults", lambda worker_id, path=None: lane)

    def test_lane_defaults(self):
        self.assertEqual(pilot.request_mode("monterey-mac-pro"), "completion-grammar")
        self.assertEqual(pilot.request_mode("m5-macbook-pro"), "chat")
        self.assertEqual(pilot.request_mode("m5-macbook-pro", "completion-grammar"), "completion-grammar")
        self.assertEqual(
            pilot.completion_settings("monterey-mac-pro"),
            {"completionPrefill": PREFILL, "promptSuffix": "<|start|>assistant", "completionConstraint": "grammar"},
        )
        self.assertEqual(pilot.completion_settings("m5-macbook-pro"), {})

    def test_invalid_values_refused(self):
        for bad in ("completion", "", True, 1):
            with self.subTest(bad=bad), self.assertRaises(pilot.Refused):
                pilot.request_mode("m5-macbook-pro", bad)
        for lane, code in (
            ({"completion_prefill": "a\x00"}, "COMPLETION_PREFILL_INVALID"),
            ({"completion_prefill": "x" * 161}, "COMPLETION_PREFILL_INVALID"),
            ({"completion_prefill": 3}, "COMPLETION_PREFILL_INVALID"),
            ({"prompt_suffix": "\t"}, "PROMPT_SUFFIX_INVALID"),
            ({"completion_constraint": "regex"}, "COMPLETION_CONSTRAINT_INVALID"),
            ({"request_mode": "raw"}, "REQUEST_MODE_INVALID"),
        ):
            with self.subTest(code=code), self.lanes(lane), self.assertRaises(pilot.Refused) as cm:
                pilot.request_mode("m5-macbook-pro")
                pilot.completion_settings("m5-macbook-pro")
            self.assertEqual(str(cm.exception), code)

    def test_worker_job_and_router_forwarding(self):
        import os
        import tempfile
        from pathlib import Path

        seen = []

        def execute(*a, request_mode_value=None, **_):
            seen.append(request_mode_value)
            raise RuntimeError("fixture")

        job = {
            "job_id": "j",
            "program": "tiny-arithmetic.mjs",
            "route": {"worker": "monterey-mac-pro", "preanalysis": "local", "reason": "strict-local"},
        }
        with tempfile.TemporaryDirectory() as td:
            worker = ww.Worker("monterey-mac-pro", "m", "http://127.0.0.1:9/v1/chat/completions", td, execute=execute)
            receipt = worker.run_job(dict(job))
            worker.run_job(dict(job, request_mode="chat"))
            with self.assertRaises(ww.Refused):
                worker.run_job(dict(job, request_mode="raw"))
            self.assertEqual(receipt["requestMode"], "completion-grammar")
            self.assertEqual(seen, ["completion-grammar", "chat"])
            with self.assertRaises(ww.Refused):
                ww.Worker("m5-macbook-pro", "m", "http://127.0.0.1:9/v1/chat/completions", td, request_mode="raw")

            token = Path(td) / "t"
            token.write_bytes(b"x" * 40)
            os.chmod(str(token), 0o600)
            cfg = Path(td) / "c.json"
            rows = {
                "monterey-mac-pro": {"url": "http://127.0.0.1:18791", "token_file": str(token)},
                "m5-macbook-pro": {"url": "http://127.0.0.1:18790", "token_file": str(token)},
            }
            cfg.write_text(json.dumps({"workers": rows}))
            workers = rc.load_workers(str(cfg))[0]
            self.assertEqual(workers["monterey-mac-pro"]["request_mode"], "completion-grammar")
            self.assertEqual(workers["m5-macbook-pro"]["request_mode"], "chat")
            client = rc.FleetRouterClient({"monterey-mac-pro": workers["monterey-mac-pro"]}, jev_preanalysis="off")
            now = 1000.0
            evidence = {"monterey-mac-pro": rc.routing.WorkerEvidence(now, True, True, True, False)}
            bodies = []
            client.probe = lambda: (evidence, now)
            client.request = lambda name, method, path, body=None: bodies.append(body) or (409, {})
            client.run("job-1", "tiny-arithmetic.mjs")
            self.assertEqual(bodies[0]["request_mode"], "completion-grammar")
            self.assertEqual(bodies[0]["route"]["worker"], "monterey-mac-pro")
            rows["m5-macbook-pro"]["request_mode"] = "raw"
            cfg.write_text(json.dumps({"workers": rows}))
            with self.assertRaises(rc.Refused):
                rc.load_workers(str(cfg))


if __name__ == "__main__":
    unittest.main()
