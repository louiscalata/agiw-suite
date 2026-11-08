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
        machines = [{"platform": "windows", "host": "pc", "state": "up"}, {"platform": "mac", "host": "mac", "state": "up"}]
        apply_rates(machines, nodes)
        self.assertEqual(machines[0]["generationTokensPerSecond"], 10)
        self.assertEqual(machines[0]["promptTokensPerSecond"], 4)
        self.assertNotIn("generationTokensPerSecond", machines[1])

    def test_tunnel_probed_rates_go_only_on_a_row_that_is_up(self):
        nodes = [lane(generation_tokens_per_second=10, prompt_tokens_per_second=4)]
        for state in ("unknown", "down", None):
            with self.subTest(state=state):
                row = {"platform": "windows", "host": "pc", "state": state, "promptTokensPerSecond": None,
                       "generationTokensPerSecond": None}
                apply_rates([row], nodes)
                self.assertEqual((row["promptTokensPerSecond"], row["generationTokensPerSecond"]), (None, None))
        up = {"platform": "windows", "host": "pc", "state": "up"}
        apply_rates([up], nodes)
        self.assertEqual((up["promptTokensPerSecond"], up["generationTokensPerSecond"]), (4, 10))

    def test_a_busy_unmeasured_lane_makes_the_machine_rate_unknown_not_a_lower_bound(self):
        busy_measured = lane(generation_tokens_per_second=100, slots_busy=1)
        busy_unmeasured = lane(id="pc:qwen3.8-27b", generation_tokens_per_second=None, slots_busy=1)
        idle_unmeasured = lane(id="pc:idle", generation_tokens_per_second=None, slots_busy=0)
        self.assertIsNone(machine_node("pc", "PC", [busy_measured, busy_unmeasured])["generationTokensPerSecond"])
        # An idle lane with nothing to measure does not void the others.
        self.assertEqual(machine_node("pc", "PC", [busy_measured, idle_unmeasured])["generationTokensPerSecond"], 100)
        row = {"platform": "windows", "host": "pc", "state": "up"}
        apply_rates([row], [busy_measured, busy_unmeasured])
        self.assertNotIn("generationTokensPerSecond", row)

    def test_a_llama_lane_that_timed_out_makes_the_machine_rate_unknown(self):
        measured = lane(generation_tokens_per_second=50, slots_busy=1)
        timed_out = lane(id="pc:qwen3.8-27b", generation_tokens_per_second=None, reason="timeout")
        refused = lane(id="pc:down", generation_tokens_per_second=None, health="down", reason="refused")
        self.assertIsNone(machine_node("pc", "PC", [measured, timed_out])["generationTokensPerSecond"])
        self.assertEqual(machine_node("pc", "PC", [measured, refused])["generationTokensPerSecond"], 50)

    def test_public_map_drops_a_document_that_is_not_the_schema(self):
        self.assertIsNone(public_map({"schema": "other", "nodes": [], "edges": []}))
        node = lane()
        copied = public_map({"schema": MAP_SCHEMA, "nodes": [node], "edges": []})
        self.assertEqual(copied["nodes"][0]["modelId"], "gpt-oss-20b")
        self.assertIsNone(public_map({"schema": MAP_SCHEMA, "nodes": [{"id": "x"}], "edges": []}))


if __name__ == "__main__":
    unittest.main()
