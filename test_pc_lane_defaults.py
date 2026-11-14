"""The legacy PC collector and tunnel have no built-in lane layout.

The earlier router and llama-server lanes are retired. Nothing built in probes
them or reports them as down. Lane membership on the Mac comes from the machine
registry; an unconfigured lane is not counted.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path

import inference_collect
import pc_tunnel
import remote_machines
from inference_collect import ProbeError, collect_map, closed_targets, direct_targets

ROOT = Path(__file__).resolve().parent
RETIRED_IDS = {"pc:router", "pc:gpt-oss-20b", "pc:qwen3.8-27b"}
CONFIGURED = {"user": "pc-user", "host": "pc.example", "identityFile": "/keys/restricted"}


def refuse(url):
    raise ProbeError("refused")


def legacy_lanes(document):
    return {item["id"]: item for item in document["nodes"]
            if item.get("machine") == "pc" and item.get("role") not in ("machine", "client")}


def node(document, node_id):
    return next(item for item in document["nodes"] if item["id"] == node_id)


class NoBuiltInLayoutTests(unittest.TestCase):
    def test_collector_and_tunnel_list_no_lane(self):
        self.assertEqual(inference_collect.PC_LANES, ())
        self.assertEqual(pc_tunnel.DEFAULT_FORWARDS, ())
        self.assertEqual(direct_targets(), [])
        self.assertEqual(closed_targets(), [])

    def test_pc_observer_probes_nothing_and_counts_nothing_as_down(self):
        seen = []

        def fetch(url):
            seen.append(url)
            raise ProbeError("refused")

        document = collect_map("pc", fetch=fetch, now=1000, veritas_url="")
        self.assertEqual(seen, [])
        self.assertEqual(legacy_lanes(document), {})
        self.assertFalse({item["id"] for item in document["nodes"]} & RETIRED_IDS)
        self.assertEqual(node(document, "pc")["health"], "not_measured")

    def test_mac_without_tunnel_or_registry_shows_no_pc_lane(self):
        config = pc_tunnel.load_config(environ={})
        self.assertNotIn("error", config)
        self.assertEqual(config["forwards"], [])
        status, rows = pc_tunnel.targets(config, lambda port: True)
        self.assertEqual((status["reason"], rows), ("host_unconfigured", []))
        document = collect_map("mac", fetch=refuse, now=1000, targets=rows, tunnel=status, veritas_url="")
        self.assertEqual(legacy_lanes(document), {})
        self.assertNotEqual(node(document, "pc")["health"], "down")


class TunnelWithoutForwardsTests(unittest.TestCase):
    def test_host_without_forwards_is_not_up_and_starts_nothing(self):
        config = pc_tunnel.load_config(environ={"AGIW_PC_SSH_USER": CONFIGURED["user"],
                                                "AGIW_PC_SSH_HOST": CONFIGURED["host"],
                                                "AGIW_PC_SSH_IDENTITY": CONFIGURED["identityFile"]})
        self.assertNotIn("error", config)
        status, rows = pc_tunnel.targets(config, lambda port: True)
        self.assertEqual((status["state"], status["reason"], rows), ("down", "forwards_unconfigured", []))
        self.assertFalse(pc_tunnel.runnable(config))
        with self.assertRaises(ValueError):
            pc_tunnel.ssh_command(config)

        def spawn(_argv):
            raise AssertionError("spawn")

        self.assertEqual(pc_tunnel.supervise(config, threading.Event(), spawn=spawn)["reason"],
                         "forwards_unconfigured")

    def test_a_file_must_still_list_at_least_one_forward(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pc-tunnel.json"
            path.write_text(json.dumps({**CONFIGURED, "forwards": []}), encoding="utf-8")
            self.assertEqual(pc_tunnel.load_config(path, environ={})["error"], "config_invalid")

    def test_configured_forwards_are_still_forwarded(self):
        forwards = [{"name": "lane", "localPort": 21011, "remotePort": 21001}]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pc-tunnel.json"
            path.write_text(json.dumps({**CONFIGURED, "forwards": forwards}), encoding="utf-8")
            config = pc_tunnel.load_config(path, environ={})
        self.assertTrue(pc_tunnel.runnable(config))
        self.assertIn("127.0.0.1:21011:127.0.0.1:21001", pc_tunnel.ssh_command(config))
        status, rows = pc_tunnel.targets(config, lambda port: port == 21011)
        self.assertEqual(status["state"], "up")
        self.assertEqual(rows, [{"id": "pc:lane", "name": "lane", "service": "127.0.0.1:21001",
                                 "base": "http://127.0.0.1:21011"}])


class RetiredLaneVisibilityTests(unittest.TestCase):
    def test_a_stale_forward_for_a_retired_lane_is_unknown_and_not_probed(self):
        targets = [{"id": "pc:router", "name": "router", "service": "127.0.0.1:1234", "base": "http://127.0.0.1:1334"},
                   {"id": "pc:qwen3.8-27b", "name": "qwen3.8-27b", "service": "127.0.0.1:1236",
                    "base": "http://127.0.0.1:1336"}]
        seen = []

        def fetch(url):
            seen.append(url)
            raise ProbeError("refused")

        document = collect_map("mac", fetch=fetch, now=1000, targets=targets, tunnel=None, veritas_url="")
        lanes = legacy_lanes(document)
        self.assertEqual(set(lanes), {"pc:router", "pc:qwen3.8-27b"})
        self.assertEqual({(item["health"], item["reason"]) for item in lanes.values()},
                         {("not_measured", "unknown_lane")})
        self.assertFalse([url for url in seen if ":1334/" in url or ":1336/" in url])
        self.assertNotEqual(node(document, "pc")["health"], "down")

    def test_registry_lanes_appear_whether_or_not_they_replace_the_legacy_pc(self):
        for replaces in (True, False):
            machine = {"id": "pc", "label": "PC", "lanes": [{"id": "fast", "label": "Fast lane", "localPort": 21001}]}
            if replaces:
                machine["replaces"] = "legacy-pc"
            config = {"schemaVersion": 1, "machines": [machine]}
            remote_machines.validate_config(config)

            class Probes:
                def snapshot(self, _config, now=None):
                    return remote_machines.unavailable_nodes(config, now=now)

            with self.subTest(replaces=replaces):
                document = collect_map("mac", fetch=refuse, now=1000, targets=None, veritas_url="",
                                       remote_config=config, remote_probes=Probes())
                ids = {item["id"] for item in document["nodes"]}
                self.assertEqual(legacy_lanes(document), {})
                self.assertFalse(ids & RETIRED_IDS)
                self.assertTrue(any(item.get("observationSource") == "registry" and item.get("role") != "machine"
                                    for item in document["nodes"]), ids)


class LaneDocsTests(unittest.TestCase):
    def test_docs_no_longer_describe_the_retired_defaults_as_current(self):
        monitor = (ROOT / "docs/inference-monitor.md").read_text(encoding="utf-8")
        ports = (ROOT / "docs/three-machine-ports.md").read_text(encoding="utf-8")
        readme = (ROOT / "windows/README.md").read_text(encoding="utf-8")
        for name, text in (("inference-monitor", monitor), ("three-machine-ports", ports)):
            for forward in ("1334", "1345", "1336"):
                self.assertNotIn(forward, text, name)
            self.assertIn("no built-in", text.casefold(), name)
        self.assertNotIn("`pc:gpt-oss-20b`", monitor)
        self.assertNotIn('permitopen="127.0.0.1:1234"', monitor)
        self.assertNotIn("default remote PC router port", ports)
        self.assertNotIn("serves the router on `:1234`", readme)


if __name__ == "__main__":
    unittest.main()
