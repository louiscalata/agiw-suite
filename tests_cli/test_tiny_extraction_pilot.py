"""Synthetic HTTP fixtures only: never invokes a model."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from support import NISI_AVAILABLE

from agiw.fleet_local_pilot import execute


@unittest.skipUnless(NISI_AVAILABLE, "pinned Nisi vendor or node missing")
class TinyExtractionTests(unittest.TestCase):
    def run_case(self, content, finish="stop", served="fixture-model"):
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
                assert req["max_tokens"] == 64 and req["response_format"]["json_schema"]["schema"] == {
                    "type": "object",
                    "properties": {"color": {"type": "string"}},
                    "required": ["color"],
                    "additionalProperties": False,
                }
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
                program="tiny-extraction.mjs",
            )
        finally:
            server.shutdown()
            server.server_close()
            t.join()

    def test_correct_fixture_passes(self):
        self.assertEqual(self.run_case('{"color":"red"}')["report"]["outcome"], "COMPLETED")

    def test_wrong_value_is_not_corrected(self):
        self.assertEqual(self.run_case('{"color":"blue"}')["report"]["outcome"], "FAILED")

    def test_wrong_key_refused(self):
        self.assertEqual(self.run_case('{"result":"red"}')["report"]["code"], "STATIC_CHECKS_FAILED")

    def test_length_refused_and_preserved(self):
        r = self.run_case('{"color":"red"}', "length")
        self.assertEqual(r["report"]["outcome"], "BLOCKED")
        self.assertEqual(r["authorReceipts"][0]["finishReason"], "length")

    def test_model_mismatch_refused(self):
        self.assertEqual(self.run_case('{"color":"red"}', served="wrong")["report"]["outcome"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
