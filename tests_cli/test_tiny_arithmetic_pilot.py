"""Synthetic HTTP fixtures only: never invokes a model."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from support import NISI_AVAILABLE

from agiw.fleet_local_pilot import execute

SCHEMA_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "arithmetic_answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
}


@unittest.skipUnless(NISI_AVAILABLE, "pinned Nisi vendor or node missing")
class TinyTests(unittest.TestCase):
    def run_case(
        self,
        content,
        finish="stop",
        served="fixture-model",
        worker="m5-macbook-pro",
        want_tokens=64,
        want_format=None,
        reject_schema=None,
    ):
        want_format = want_format or {"type": "json_object"}
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, d):
                b = json.dumps(d).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self.send(
                    {"status": "ok"}
                    if self.path == "/health"
                    else {"data": [{"id": "fixture-model"}]}
                    if self.path == "/v1/models"
                    else [{"is_processing": False}]
                )

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(req["response_format"])
                assert req["max_tokens"] == want_tokens
                if reject_schema and req["response_format"]["type"] == "json_schema":
                    b = json.dumps({"error": {"code": reject_schema, "message": "failed to parse grammar"}}).encode()
                    self.send_response(reject_schema)
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                    return
                assert req["response_format"] == want_format, req["response_format"]
                self.send(
                    {
                        "model": served,
                        "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}],
                        "usage": {"prompt_tokens": 42, "completion_tokens": 10, "total_tokens": 52},
                    }
                )

        server = HTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            return execute(
                "live",
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                "fixture-model",
                program="tiny-arithmetic.mjs",
                worker_id=worker,
            )
        finally:
            server.shutdown()
            server.server_close()
            t.join()

    def test_correct_fixture_passes(self):
        self.assertEqual(self.run_case('{"answer":42}')["report"]["outcome"], "COMPLETED")

    def test_wrong_value_is_not_corrected(self):
        self.assertEqual(self.run_case('{"answer":34}')["report"]["outcome"], "FAILED")

    def test_wrong_key_refused(self):
        self.assertEqual(self.run_case('{"result":34}')["report"]["code"], "STATIC_CHECKS_FAILED")

    def test_length_refused_and_preserved(self):
        r = self.run_case('{"answer":42}', "length")
        self.assertEqual(r["report"]["outcome"], "BLOCKED")
        self.assertEqual(r["authorReceipts"][0]["finishReason"], "length")

    def test_model_mismatch_refused(self):
        self.assertEqual(self.run_case('{"answer":42}', served="wrong")["report"]["outcome"], "BLOCKED")

    def test_length_records_sanitized_output_sample(self):
        r = self.run_case(
            "<|channel|>analysis<|message|>We need to compute 6*7 for user token=abcdefabcdefabcdefabcdefabcd", "length"
        )
        ev = r["authorEvidence"][0]
        self.assertEqual(r["report"]["outcome"], "BLOCKED")
        self.assertIn("<|channel|>analysis", ev["outputSample"])
        self.assertNotIn("abcdefabcdefabcdefabcdefabcd", ev["outputSample"])
        self.assertLessEqual(len(ev["outputSample"]), 300)

    def test_harmony_preamble_final_json_extracted_unchanged(self):
        c = '<|channel|>analysis<|message|>Multiply.<|end|><|start|>assistant<|channel|>final<|message|>{"answer":42}<|return|>'
        r = self.run_case(c)
        self.assertEqual(r["report"]["outcome"], "COMPLETED")
        self.assertEqual(r["authorEvidence"][0]["preambleStripped"], "harmony")

    def test_harmony_final_with_wrong_answer_not_corrected(self):
        c = '<|channel|>analysis<|message|>x<|end|><|start|>assistant<|channel|>final<|message|>{"answer":41}'
        self.assertEqual(self.run_case(c)["report"]["outcome"], "FAILED")

    def test_unknown_preamble_refused_with_sample(self):
        r = self.run_case('Sure! Here it is: {"answer":42}')
        self.assertEqual(r["report"]["outcome"], "BLOCKED")
        self.assertIn("Sure!", r["authorEvidence"][0]["outputSample"])

    def test_monterey_lane_uses_512_tokens_and_strict_schema(self):
        r = self.run_case('{"answer":42}', worker="monterey-mac-pro", want_tokens=512, want_format=SCHEMA_FORMAT)
        self.assertEqual(r["report"]["outcome"], "COMPLETED")
        self.assertEqual(r["responseFormat"], "json-schema")
        self.assertEqual(self.seen, [SCHEMA_FORMAT])
        ev = r["authorEvidence"][0]
        self.assertEqual((ev["responseFormat"], ev["attempts"]), ("json_schema", 1))
        self.assertNotIn("responseFormatFallback", ev)

    def test_schema_rejected_falls_back_to_json_object_and_is_recorded(self):
        r = self.run_case('{"answer":42}', worker="monterey-mac-pro", want_tokens=512, reject_schema=400)
        self.assertEqual(r["report"]["outcome"], "COMPLETED")
        self.assertEqual(self.seen, [SCHEMA_FORMAT, {"type": "json_object"}])
        ev = r["authorEvidence"][0]
        self.assertEqual(ev["responseFormatFallback"], "json_schema->json_object")
        self.assertEqual((ev["fallbackHttpStatus"], ev["attempts"], ev["responseFormat"]), (400, 2, "json_object"))
        self.assertEqual(ev["fallbackServerMessage"], "failed to parse grammar")
        self.assertEqual(r["modelCalls"], 1)

    def test_schema_length_still_fails_closed(self):
        # Even with a grammar, a truncated answer is never accepted, though the JSON inside parses.
        r = self.run_case(
            '{"answer":42}', "length", worker="monterey-mac-pro", want_tokens=512, want_format=SCHEMA_FORMAT
        )
        self.assertEqual(r["report"]["outcome"], "BLOCKED")
        self.assertEqual(r["authorEvidence"][0]["finishReason"], "length")

    def test_echoed_reasoning_with_answers_is_not_salvaged(self):
        c = 'What is 6 multiplied by 7? Let me think. {"answer":42} so the answer is {"answer":42} and'
        r = self.run_case(c, "length", worker="monterey-mac-pro", want_tokens=512, want_format=SCHEMA_FORMAT)
        self.assertEqual(r["report"]["outcome"], "BLOCKED")
        self.assertIn("6 multiplied by 7", r["authorEvidence"][0]["outputSample"])

    def test_no_fallback_without_opt_in(self):
        # Default lanes keep json_object; a server error is a failure, never retried.
        r = self.run_case('{"answer":42}', reject_schema=400)
        self.assertEqual(r["report"]["outcome"], "COMPLETED")
        self.assertEqual(self.seen, [{"type": "json_object"}])
        self.assertEqual(r["authorEvidence"][0]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
