"""The vendored pure functions must retain the PC edition's behavior."""
import ast
import copy
import inspect
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "linux"))
sys.path.insert(0, str(ROOT / "windows"))
from agiw_linux import lanes
from agiw_win import probes as pc
from agiw_win import jsonutil as pc_json


class LaneParityTests(unittest.TestCase):
    def test_vendored_function_bodies_match(self):
        names = ("_string", "_number", "_source", "_reject_constant", "loads_strict", "_model_ids", "_slot",
                 "probe_lane", "memory_level", "public_id", "pc_status", "validate_status",
                 "peer_presence", "_peer_models", "_validate_peer")
        for name in names:
            with self.subTest(name=name):
                pc_module = pc_json if name in ('_reject_constant', 'loads_strict') else pc
                linux_body = ast.dump(ast.parse(inspect.getsource(getattr(lanes, name))), include_attributes=False)
                pc_body = ast.dump(ast.parse(inspect.getsource(getattr(pc_module, name))), include_attributes=False)
                self.assertEqual(linux_body, pc_body)

    def test_probe_lane_fixtures_match(self):
        spec = {"id": "fixture", "port": 49152, "alias": "fixture-model", "device": "Vulkan"}
        cases = [
            [ConnectionRefusedError(), {}, []],
            [{"status": "loading"}, {}, []],
            [{"status": "ok"}, {"data": []}, []],
            [{"status": "ok"}, {"data": [{"id": "wrong"}]}, []],
            [{"status": "ok"}, {"data": [{"id": "fixture-model"}]}, []],
            [{"status": "ok"}, {"data": [{"id": "fixture-model"}]}, [{"id": 0, "is_processing": False}]],
            [{"status": "ok"}, {"data": [{"id": "fixture-model"}]},
             [{"id": 0, "id_task": 4, "is_processing": True, "next_token": [{"n_decoded": 30}]}]],
            [{"status": "ok"}, {"data": [{"id": "fixture-model"}]},
             [{"id": 0, "is_processing": True}, {"id": 0, "is_processing": False}]],
        ]
        for case in cases:
            with self.subTest(case=case):
                def run(module):
                    values = iter(copy.deepcopy(case))
                    def fetch(*args):
                        value = next(values)
                        if isinstance(value, Exception):
                            raise value
                        return value
                    return module.probe_lane(spec, fetch=fetch)
                self.assertEqual(run(lanes), run(pc))

    def test_memory_level_fixtures_match(self):
        for available in (0, 5.9, 6, 11.9, 12, 19.9, 20, 100):
            for commit in (None, 0, 94.9, 95, 100):
                with self.subTest(available=available, commit=commit):
                    self.assertEqual(lanes.memory_level(available, commit), pc.memory_level(available, commit))

    def test_live_rate_fixtures_match(self):
        for old_task, new_task, old_count, new_count, gap in ((1, 1, 10, 40, 1), (1, 2, 10, 40, 1),
                                                            (1, 1, 40, 10, 1), (1, 1, 10, 40, 11),
                                                            (1, 1, 10, 40, .1)):
            previous = {(0, old_task): (old_count, 100)}
            lane = {"slots": [{"id": 0, "taskId": new_task, "decodedTokens": new_count, "isProcessing": True}]}
            self.assertEqual(lanes.live_decode_rate(previous, lane, 100 + gap), pc.live_decode_rate(previous, lane, 100 + gap))

    def test_live_rate_requires_same_task_two_polls(self):
        lane = {"slots": [{"id": 0, "taskId": 5, "decodedTokens": 10, "isProcessing": True}]}
        rate, previous = lanes.live_decode_rate({}, lane, 100)
        self.assertIsNone(rate)
        lane["slots"][0]["decodedTokens"] = 35
        self.assertEqual(lanes.live_decode_rate(previous, lane, 101)[0], 25)
        lane["slots"][0]["taskId"] = 6
        self.assertIsNone(lanes.live_decode_rate(previous, lane, 101)[0])

    def test_status_and_presence_fixtures_match(self):
        for status in ("idle", "busy", "slots_unknown", "unreachable", "identity_mismatch"):
            snapshot = {"observerVersion": "fixture", "observedAt": "2026-10-17T00:00:00Z", "sampledAt": 100,
                        "lanes": [{"id": "fast", "status": status, "phase": "idle", "expectedModel": "openai/gpt-oss-20b",
                                   "servedModel": "openai/gpt-oss-20b", "liveTokensPerSecond": 25}],
                        "pcGpus": [{"name": "fixture GPU", "memoryUsedMiB": 100}], "memory": {"level": "ok"}}
            self.assertEqual(lanes.pc_status(snapshot), pc.pc_status(snapshot))
            self.assertEqual(lanes.peer_presence(snapshot, "fixture"), pc.peer_presence(snapshot, "fixture"))
            self.assertEqual(lanes.validate_status(lanes.pc_status(snapshot)), pc.validate_status(pc.pc_status(snapshot)))

    def test_peer_validation_fixtures_match(self):
        base = {"schemaVersion": 1, "kind": "agiw-peer", "host": "fixture", "platform": "linux",
                "observedUnix": 100, "modelsTotal": 1, "models": [{"id": "m", "loadedState": "loaded"}]}
        for change in ({}, {"models": None}, {"observedUnix": 200}, {"modelsTotal": 2}, {"schemaVersion": True},
                       {"models": [{"id": "m", "loadedState": "mystery"}]}):
            value = {**base, **change}
            self.assertEqual(lanes._validate_peer(value, 101), pc._validate_peer(value, 101))

    def test_portable_layer_needs_explicit_transport(self):
        with self.assertRaisesRegex(ValueError, "explicit allowlisted"):
            lanes.http_get_json("http://127.0.0.1:8081/slots")


if __name__ == "__main__":
    unittest.main()
