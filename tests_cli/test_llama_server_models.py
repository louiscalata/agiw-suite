"""lmstudio_status falls back from LM Studio's /api/v0/models to llama-server's OpenAI-style /v1/models.

Every case talks to a mock server on 127.0.0.1; nothing leaves the machine."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from support import pl


def serve(case, routes):
    """Start a loopback server answering each path in routes with (status, body); other paths 404."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            status, body = routes.get(self.path, (404, {"error": "not found"}))
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop():
        server.shutdown()
        server.server_close()
        thread.join(5)

    case.addCleanup(stop)
    return f"http://127.0.0.1:{server.server_address[1]}", hits


def states(doc):
    return [(m["id"], m["host"], m["loadedState"]) for m in doc["models"]]


class LlamaServerModels(unittest.TestCase):
    def test_lm_studio_listing_is_used_first(self):
        base, hits = serve(
            self,
            {
                "/api/v0/models": (
                    200,
                    {
                        "data": [
                            {"id": "qwen", "type": "llm", "state": "loaded"},
                            {"id": "gemma", "type": "vlm", "state": "not-loaded"},
                            {"id": "nomic", "type": "embeddings", "state": "loaded"},
                        ]
                    },
                ),
                "/v1/models": (200, {"data": [{"id": "should-not-be-read"}]}),
            },
        )
        doc = pl.lmstudio_status(base, "mac")()
        self.assertEqual(states(doc), [("qwen", "mac", "loaded"), ("gemma", "mac", "inactive")])
        self.assertEqual(hits, ["/api/v0/models"])
        self.assertEqual(doc["producer"], "agiw-peer-link lmstudio")

    def test_llama_server_v1_models_is_detected_when_lm_studio_endpoint_is_absent(self):
        base, hits = serve(
            self,
            {
                "/v1/models": (
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": "muse-glimmer-30b",
                                "object": "model",
                                "created": 1763400000,
                                "owned_by": "llamacpp",
                            },
                        ],
                    },
                ),
            },
        )
        doc = pl.lmstudio_status(base, "mac")()
        self.assertEqual(states(doc), [("muse-glimmer-30b", "mac", "loaded")])
        self.assertEqual(hits, ["/api/v0/models", "/v1/models"])
        self.assertEqual(doc["health"], "ok")

    def test_llama_server_router_status_values_map_to_loaded_states(self):
        base, _ = serve(
            self,
            {
                "/v1/models": (
                    200,
                    {
                        "object": "list",
                        "data": [
                            {"id": "a", "status": {"value": "loaded"}},
                            {"id": "b", "status": {"value": "unloaded"}},
                            {"id": "c", "status": {"value": "loading"}},
                            {"no-id": True},
                        ],
                    },
                ),
            },
        )
        doc = pl.lmstudio_status(base, "mac")()
        self.assertEqual(states(doc), [("a", "mac", "loaded"), ("b", "mac", "inactive"), ("c", "mac", "unknown")])

    def test_lm_studio_payload_without_a_model_list_falls_back(self):
        base, _ = serve(
            self,
            {
                "/api/v0/models": (200, {"error": "unexpected"}),
                "/v1/models": (200, {"object": "list", "data": [{"id": "glimmer"}]}),
            },
        )
        self.assertEqual(states(pl.lmstudio_status(base, "mac")()), [("glimmer", "mac", "loaded")])

    def test_both_endpoints_missing_raises_instead_of_reporting_no_models(self):
        base, hits = serve(self, {})
        with self.assertRaises(OSError) as caught:
            pl.lmstudio_status(base, "mac")()
        caught.exception.close()
        self.assertEqual(hits, ["/api/v0/models", "/v1/models"])


if __name__ == "__main__":
    unittest.main()
