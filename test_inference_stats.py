import unittest

from inference_stats import apply_rates, edges_for, make_node, machine_node, public_map, MAP_SCHEMA


def lane(**extra):
    fields = dict(id="pc:gpt-oss-20b", machine="pc", role="llama", label="gpt-oss-20b", health="ok",
                  endpoint="127.0.0.1:1245", model_id="gpt-oss-20b")
    fields.update(extra)
    return make_node(**fields)


class StatsModelTests(unittest.TestCase):
    def test_null_measurements_are_kept_and_bad_numbers_are_rejected(self):
        node = lane()
        self.assertIsNone(node["promptTokensPerSecond"])
        self.assertIsNone(node["generationTokensPerSecond"])
        self.assertIsNone(node["vramUsedMiB"])
        for bad in (-1, float("nan"), float("inf"), True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    lane(generation_tokens_per_second=bad)
        with self.assertRaises(ValueError):
            lane(slots_busy=1.5)
        with self.assertRaises(ValueError):
            lane(health="up")

    def test_router_edge_uses_the_reported_upstream_port(self):
        router = make_node(id="pc:router", machine="pc", role="router", label="Router", health="ok",
                           endpoint="127.0.0.1:1234",
                           models=[{"id": "gpt-oss-20b", "upstream": "http://127.0.0.1:1245/v1"}])
        other = lane()
        qwen = lane(id="pc:qwen3.8-27b", label="qwen3.8-27b", endpoint="127.0.0.1:1236", model_id="qwen3.8-27b")
        edges = edges_for([machine_node("pc", "PC", [router, other, qwen]), router, other, qwen])
        upstreams = [edge for edge in edges if edge["kind"] == "upstream"]
        self.assertEqual(upstreams, [{"from": "pc:router", "to": "pc:gpt-oss-20b", "kind": "upstream",
                                      "modelId": "gpt-oss-20b"}])

    def test_a_caller_becomes_a_node_only_when_one_is_reported(self):
        node = lane(callers=[{"id": "codex", "label": "Codex"}])
        from inference_stats import client_nodes
        clients = client_nodes([node])
        self.assertEqual(clients[0]["id"], "pc:gpt-oss-20b:caller:codex")
        self.assertEqual(clients[0]["role"], "client")
        self.assertEqual(client_nodes([lane()]), [])

    def test_machine_rates_sum_measurements_and_leave_gaps_null(self):
        nodes = [lane(generation_tokens_per_second=10),
                 lane(id="pc:qwen3.8-27b", prompt_tokens_per_second=4, generation_tokens_per_second=None)]
        machines = [{"platform": "windows", "host": "pc"}, {"platform": "mac", "host": "mac"}]
        apply_rates(machines, nodes)
        self.assertEqual(machines[0]["generationTokensPerSecond"], 10)
        self.assertEqual(machines[0]["promptTokensPerSecond"], 4)
        self.assertNotIn("generationTokensPerSecond", machines[1])

    def test_public_map_drops_a_document_that_is_not_the_schema(self):
        self.assertIsNone(public_map({"schema": "other", "nodes": [], "edges": []}))
        node = lane()
        copied = public_map({"schema": MAP_SCHEMA, "nodes": [node], "edges": []})
        self.assertEqual(copied["nodes"][0]["modelId"], "gpt-oss-20b")
        self.assertIsNone(public_map({"schema": MAP_SCHEMA, "nodes": [{"id": "x"}], "edges": []}))


if __name__ == "__main__":
    unittest.main()
