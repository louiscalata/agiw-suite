import unittest
from unittest import mock

from inference_stats import apply_rates, edges_for, expire_map, make_node, machine_node, public_map, MAP_SCHEMA


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

    def test_additional_machine_roots_and_lanes_keep_distinct_identities(self):
        pc_lane = lane()
        remote = lane(id="worker-b:deep", machine="worker-b", label="Deep", model_id="server/model")
        roots = [machine_node("pc", "PC", [pc_lane]), machine_node("worker-b", "Worker B", [remote])]
        document = public_map({"schema": MAP_SCHEMA, "nodes": [*roots, pc_lane, remote], "edges": []})
        self.assertEqual([node["id"] for node in roots], ["pc", "worker-b"])
        self.assertEqual(document["edges"], [
            {"from": "pc", "to": "pc:gpt-oss-20b", "kind": "hosts"},
            {"from": "worker-b", "to": "worker-b:deep", "kind": "hosts"},
        ])
        self.assertEqual(document["nodes"][-1]["modelId"], "server/model")

    def test_expiring_one_remote_lane_cannot_leave_its_machine_ok(self):
        fresh = lane(id="worker-b:fast", machine="worker-b", observed_at="1970-01-01T00:16:40Z")
        old = lane(id="worker-b:deep", machine="worker-b", observed_at="1970-01-01T00:16:35Z")
        root = machine_node("worker-b", "Worker B", [fresh, old], observed_at="1970-01-01T00:16:40Z")
        document = {"schema": MAP_SCHEMA, "observedAt": "1970-01-01T00:16:40Z",
                    "nodes": [root, fresh, old], "edges": edges_for([root, fresh, old])}
        expire_map(document, 1000)
        self.assertEqual(fresh["health"], "ok")
        self.assertEqual(old["health"], "not_measured")
        self.assertIsNone(old["modelId"])
        self.assertEqual((root["health"], root["reason"]), ("not_measured", "lanes_unknown"))
        self.assertEqual(len(document["edges"]), 2)  # Structural ownership remains visible.

    def test_configured_lane_scope_stays_unknown_without_changing_legacy_pc_rollup(self):
        fresh = lane(observed_at="1970-01-01T00:16:40Z")
        failed = lane(id="pc:deep", health="not_measured", model_id=None,
                      reason="timeout", observed_at="1970-01-01T00:16:40Z")
        for marked in (False, True):
            root = machine_node("pc", "PC", [fresh, failed], observed_at="1970-01-01T00:16:40Z")
            if marked:
                root["inventoryScope"] = "configured-lanes"
            document = {"schema": MAP_SCHEMA, "nodes": [root, fresh, failed], "edges": []}
            expire_map(document, 1000)
            self.assertEqual(root["health"], "not_measured" if marked else "ok")
            if marked:
                self.assertEqual(root["reason"], "lane_unknown")


class RegistryFreshnessTests(unittest.TestCase):
    def registry_lane(self, **extra):
        node = lane(id="registry:pc:deep", observed_at="1970-01-01T00:16:40Z")
        node.update(observationSource="registry", probeIntervalSeconds=4, ageSeconds=0)
        node.update(extra)
        return node

    def test_namespaced_machine_ids_allow_the_full_registry_token_length(self):
        node = lane(id="registry:abcdefghijklmnop:deep", machine="registry:abcdefghijklmnop")
        self.assertEqual(node["machine"], "registry:abcdefghijklmnop")

    def test_registry_snapshot_boundary_uses_its_interval_even_for_pc_and_rerolls_root(self):
        from server import SnapshotStore
        node = self.registry_lane()
        root = machine_node("pc", "Registry PC", [node], observed_at=node["observedAt"])
        root.update(observationSource="registry", probeIntervalSeconds=4, inventoryScope="configured-lanes")
        document = {"schema": MAP_SCHEMA, "observedAt": node["observedAt"],
                    "nodes": [root, node], "edges": edges_for([root, node])}
        store = SnapshotStore()
        store.snapshot = {"sampledAt": 1000, "inference": document}
        with mock.patch("server.time.time", return_value=1007.5):
            fresh = store.read()["inference"]["nodes"]
        self.assertEqual((fresh[1]["health"], fresh[1]["ageSeconds"]), ("ok", 7.5))
        with mock.patch("server.time.time", return_value=1008.01):
            stale = store.read()["inference"]["nodes"]
        self.assertEqual((stale[1]["health"], stale[1]["reason"]), ("not_measured", "stale"))
        self.assertIsNone(stale[1]["modelId"])
        self.assertAlmostEqual(stale[1]["ageSeconds"], 8.01)
        self.assertEqual((stale[0]["health"], stale[0]["reason"]), ("not_measured", "lane_unknown"))
        self.assertEqual(document["nodes"][1]["modelId"], "gpt-oss-20b")  # Read ages a copy.
        legacy = lane(observed_at="1970-01-01T00:16:40Z")
        expire_map({"nodes": [legacy]}, 1008.01)
        self.assertEqual(legacy["health"], "ok")

    def test_registry_default_interval_and_missing_observation_never_borrow_snapshot_time(self):
        old = self.registry_lane()
        del old["probeIntervalSeconds"]  # Absent interval uses the registry default of 3 s.
        pending = self.registry_lane(id="registry:pc:pending", observedAt=None, ageSeconds=None,
                                     health="not_measured", modelId=None, reason="pending")
        document = {"observedAt": "1970-01-01T00:16:47Z", "nodes": [old, pending]}
        expire_map(document, 1007)
        self.assertEqual((old["health"], old["reason"], old["ageSeconds"]), ("not_measured", "stale", 7))
        self.assertIsNone(pending["ageSeconds"])
        self.assertIsNone(pending["observedAt"])
        self.assertEqual(pending["health"], "not_measured")

    def test_public_map_keeps_registry_freshness_and_optional_capability_metadata(self):
        node = self.registry_lane(contextTokens=8192, thinking="unknown")
        copied = public_map({"schema": MAP_SCHEMA, "nodes": [node], "edges": []})["nodes"][0]
        for key in ("observationSource", "probeIntervalSeconds", "ageSeconds", "contextTokens", "thinking"):
            self.assertEqual(copied[key], node[key])
        for field, bad in (("contextTokens", True), ("contextTokens", 1.5), ("thinking", "maybe"),
                           ("probeIntervalSeconds", 0), ("ageSeconds", float("nan"))):
            with self.subTest(field=field, bad=bad):
                invalid = {**node, field: bad}
                self.assertIsNone(public_map({"schema": MAP_SCHEMA, "nodes": [invalid], "edges": []}))


if __name__ == "__main__":
    unittest.main()
