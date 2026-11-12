"""Passive readiness uses cached Windows observations without a socket or owner."""
from contextlib import ExitStack
from email.message import Message
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import server


class PassiveReadinessTests(unittest.TestCase):
    def test_get_reads_only_cached_state_even_when_an_owner_is_available(self):
        owner = mock.Mock()
        store = mock.Mock()
        store.read.return_value = {"sampledAt": 100, "onlineCodeMode": {"setupState": "fresh"}}
        handler = server.Handler.__new__(server.Handler)
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 8767),
                                         store=store, online_code_repair=owner, l2_open=True)
        handler.path = "/api/online-code-mode/readiness"
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8767"
        handler.json_reply = mock.Mock()
        with ExitStack() as stack:
            blocked = [stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
                       for target in ("threading.Thread", "subprocess.Popen", "socket.create_connection",
                                      "os.open", "pathlib.Path.write_text", "pathlib.Path.write_bytes")]
            stack.enter_context(mock.patch.object(server.time, "time", return_value=100))
            handler.do_GET()
            for call in blocked:
                call.assert_not_called()
        code, result = handler.json_reply.call_args.args
        self.assertEqual(code, 200)
        self.assertTrue(result["readOnly"])
        self.assertEqual(result["reasonCode"], "cached_observations")
        self.assertEqual(result["inferenceReadiness"], "unknown")
        self.assertEqual(result["setupState"], "unknown")
        store.read.assert_called_once_with()
        self.assertEqual(owner.mock_calls, [])

    def test_missing_expired_clock_skew_and_invalid_cached_evidence_remain_unknown(self):
        cases = ((None, "not_observed"), ({"sampledAt": 95}, "stale"),
                 ({"sampledAt": 101}, "clock_skew"), ({"sampledAt": True}, "not_observed"),
                 ({"sampledAt": 10 ** 400}, "not_observed"),
                 ({"sampledAt": 100, "fullSampledAt": 90}, "stale"))
        for snapshot, reason in cases:
            with self.subTest(reason=reason):
                result = server.cached_readiness(snapshot, 100)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reasonCode"], reason)
                self.assertEqual(result["inferenceReadiness"], "unknown")

