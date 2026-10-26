import json
import unittest

from inference_collect import ProbeError, RateMemory, collect_map, direct_targets, parse_metrics, parse_slots
from inference_stats import MAP_SCHEMA


METRICS = """\
# HELP llamacpp:prompt_tokens_total counted
llamacpp:prompt_tokens_total 100
llamacpp:prompt_seconds_total 2
llamacpp:tokens_predicted_total 50
llamacpp:predicted_seconds_total 1
llamacpp:requests_deferred 3
llamacpp:n_busy_slots 1
ignored_metric 9
"""

METRICS_LATER = """\
llamacpp:prompt_tokens_total 180
llamacpp:prompt_seconds_total 3
llamacpp:tokens_predicted_total 130
llamacpp:predicted_seconds_total 2
llamacpp:requests_deferred 0
"""

SLOTS = json.dumps([
    {"id": 0, "is_processing": True, "n_ctx": 65536, "n_past": 1200, "prompt": "SECRET-PROMPT"},
    {"id": 1, "is_processing": False, "n_ctx": 65536, "n_past": 0, "prompt": "OTHER"},
])

PROPS = json.dumps({"model_alias": "gpt-oss-20b", "model_path": "/models/secret.gguf",
                    "default_generation_settings": {"n_ctx": 65536}})


def route(table):
    def fetch(url):
        for suffix, value in table.items():
            if url.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                status, body = value
                return status, body, 12.5
        raise ProbeError("refused")
    return fetch


class CollectorTests(unittest.TestCase):
    def test_llama_metrics_slots_and_a_second_sample_rate(self):
        rates = RateMemory()

        def fetch(url):
            if ":1245/" not in url:
                raise ProbeError("refused")
            suffix = "/" + url.rsplit("/", 1)[1]
            return 200, {"/metrics": METRICS, "/slots": SLOTS, "/props": PROPS}[suffix], 12.5

        def fetch_later(url):
            if url.endswith(":1245/metrics"):
                return 200, METRICS_LATER, 12.5
            return fetch(url)

        first = collect_map("pc", fetch=fetch, rates=rates, now=1000, veritas_url="")
        node = next(item for item in first["nodes"] if item["id"] == "pc:gpt-oss-20b")
        self.assertEqual(node["health"], "ok")
        self.assertEqual(node["modelId"], "gpt-oss-20b")
        self.assertIsNone(node["generationTokensPerSecond"])
        self.assertEqual((node["slotsBusy"], node["slotsTotal"], node["queueDepth"]), (1, 2, 3))
        self.assertEqual((node["contextUsed"], node["contextLimit"]), (1200, 65536))
        self.assertNotIn("SECRET-PROMPT", json.dumps(node))
        self.assertNotIn("secret.gguf", json.dumps(node))
        second = collect_map("pc", fetch=fetch_later, rates=rates, now=1002, veritas_url="")
        node = next(item for item in second["nodes"] if item["id"] == "pc:gpt-oss-20b")
        self.assertEqual(node["promptTokensPerSecond"], 80)
        self.assertEqual(node["generationTokensPerSecond"], 80)
        pc = next(item for item in second["nodes"] if item["id"] == "pc")
        self.assertEqual(pc["generationTokensPerSecond"], 80)
        mac = next(item for item in second["nodes"] if item["id"] == "mac:lmstudio")
        self.assertEqual((mac["health"], mac["reason"]), ("not_measured", "not_observed"))

    def test_refused_port_is_down_and_does_not_try_the_other_paths(self):
        seen = []

        def fetch(url):
            seen.append(url)
            raise ProbeError("refused")

        document = collect_map("pc", fetch=fetch, now=1000, veritas_url="")
        node = next(item for item in document["nodes"] if item["id"] == "pc:gpt-oss-20b")
        self.assertEqual((node["health"], node["reason"]), ("down", "refused"))
        self.assertEqual([url for url in seen if url.endswith(":1245/metrics") or ":1245/" in url],
                         ["http://127.0.0.1:1245/metrics"])

    def test_timeout_is_not_measured(self):
        def fetch(url):
            if url.endswith(":1236/metrics"):
                raise ProbeError("timeout")
            raise ProbeError("refused")

        document = collect_map("pc", fetch=fetch, now=1000, veritas_url="")
        node = next(item for item in document["nodes"] if item["id"] == "pc:qwen3.8-27b")
        self.assertEqual((node["health"], node["reason"]), ("not_measured", "timeout"))

    def test_health_probe_when_metrics_slots_and_props_are_absent(self):
        def fetch(url):
            if url.endswith("/health"):
                return 200, "ok", 4
            return 404, "", 1

        document = collect_map("pc", fetch=fetch, now=1000, veritas_url="")
        node = next(item for item in document["nodes"] if item["id"] == "pc:gpt-oss-20b")
        self.assertEqual(node["health"], "ok")
        self.assertEqual(node["detail"], "Timed health probe")
        self.assertIsNone(node["generationTokensPerSecond"])
        self.assertEqual(node["latencyMs"], 4)

    def test_router_reports_upstream_and_a_model_mismatch_is_degraded(self):
        def fetch(url):
            if url.endswith(":1234/v1/models"):
                return 200, json.dumps({"data": [
                    {"id": "gpt-oss-20b", "upstream": "http://127.0.0.1:1245"},
                    {"id": "qwen3.8-27b", "upstream": "http://upstream.invalid/v1"},
                ]}), 3
            if url.endswith("/props"):
                return 200, json.dumps({"model_alias": "other-model"}), 2
            if url.endswith("/metrics"):
                return 404, "", 1
            if url.endswith("/slots"):
                return 200, json.dumps([{"id": 0, "is_processing": False, "n_ctx": 8192}]), 2
            raise ProbeError("refused")

        document = collect_map("pc", fetch=fetch, now=1000, veritas_url="")
        router = next(item for item in document["nodes"] if item["id"] == "pc:router")
        self.assertEqual(router["models"][0]["upstream"], "http://127.0.0.1:1245")
        self.assertIsNone(router["models"][1]["upstream"])
        self.assertNotIn("upstream.invalid", json.dumps(router))
        mismatched = next(item for item in document["nodes"] if item["id"] == "pc:gpt-oss-20b")
        self.assertEqual((mismatched["health"], mismatched["reason"], mismatched["modelId"]),
                         ("degraded", "model_mismatch", "other-model"))
        upstreams = [edge for edge in document["edges"] if edge["kind"] == "upstream"]
        self.assertEqual(upstreams[0]["to"], "pc:gpt-oss-20b")

    def test_lmstudio_veritas_and_a_down_tunnel(self):
        seen = []

        def fetch(url):
            seen.append(url)
            if url.endswith("/api/v0/models"):
                return 200, json.dumps({"data": [{
                    "id": "gpt-oss-20b", "state": "loaded", "max_context_length": 131072,
                    "note": "SECRET-NOTE"}]}), 6
            if url.endswith(":9/health"):
                return 200, json.dumps({"status": "ok", "model": "qwen2.5-vl"}), 8
            raise AssertionError(url)

        targets = [{"id": lane["id"], "service": f"127.0.0.1:{lane['port']}", "base": None}
                   for lane in ({"id": "pc:router", "port": 1234},
                                {"id": "pc:gpt-oss-20b", "port": 1245},
                                {"id": "pc:qwen3.8-27b", "port": 1236})]
        tunnel = {"state": "down", "reason": "tunnel_down", "detail": "SSH tunnel is down."}
        document = collect_map("mac", fetch=fetch, now=1000, targets=targets, tunnel=tunnel,
                               veritas_url="http://127.0.0.1:9/health",
                               vram={"usedMiB": 1024, "totalMiB": 8192})
        self.assertEqual(document["schema"], MAP_SCHEMA)
        studio = next(item for item in document["nodes"] if item["id"] == "mac:lmstudio")
        self.assertEqual((studio["health"], studio["modelId"], studio["contextLimit"]), ("ok", "gpt-oss-20b", 131072))
        self.assertNotIn("SECRET-NOTE", json.dumps(studio))
        veritas = next(item for item in document["nodes"] if item["id"] == "mac:veritas")
        self.assertEqual((veritas["health"], veritas["modelId"]), ("ok", "qwen2.5-vl"))
        router = next(item for item in document["nodes"] if item["id"] == "pc:router")
        self.assertEqual((router["health"], router["reason"]), ("not_measured", "tunnel_down"))
        mac = next(item for item in document["nodes"] if item["id"] == "mac")
        self.assertEqual((mac["vramUsedMiB"], mac["vramTotalMiB"]), (1024, 8192))
        self.assertNotIn("http://127.0.0.1:1245", seen)
        self.assertNotIn("http://127.0.0.1:1236", seen)
        self.assertNotIn("http://127.0.0.1:1334", "".join(seen))

    def test_unconfigured_and_non_loopback_veritas_make_no_request(self):
        calls = []

        def fetch(url):
            calls.append(url)
            raise ProbeError("refused")

        for url in ("", "http://pc.example/health", "http://127.0.0.1:9/health?x=1"):
            document = collect_map("mac", fetch=fetch, now=1000, targets=[], veritas_url=url)
            node = next(item for item in document["nodes"] if item["id"] == "mac:veritas")
            self.assertEqual(node["health"], "not_measured")
            self.assertIn(node["reason"], ("not_configured", "url_not_loopback"))
        self.assertTrue(all("/health" not in url for url in calls))

    def test_metric_names_and_slot_shape(self):
        parsed = parse_metrics(METRICS)
        self.assertEqual(parsed["prompt_tokens_total"], 100)
        self.assertNotIn("ignored_metric", parsed)
        self.assertIsNone(parse_slots("{"))
        self.assertIsNone(parse_slots(json.dumps({"prompt": "SECRET"})))

    def test_direct_pc_targets_are_the_service_ports(self):
        self.assertEqual([item["base"] for item in direct_targets()],
                         ["http://127.0.0.1:1234", "http://127.0.0.1:1245", "http://127.0.0.1:1236"])


if __name__ == "__main__":
    unittest.main()
