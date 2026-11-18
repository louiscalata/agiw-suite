import os
import time
import unittest

from support import NISI_AVAILABLE

from agiw.fleet_local_pilot import Refused, clean_env, endpoint, execute, readiness, route
from agiw.fleet_routing_plan import WorkerEvidence


class PilotTests(unittest.TestCase):
    def test_loopback_only(self):
        for u in [
            "https://127.0.0.1:8081/v1/chat/completions",
            "http://localhost:8081/v1/chat/completions",
            "http://example.com:80/v1/chat/completions",
            "http://u:p@127.0.0.1:80/v1/chat/completions",
            "http://127.0.0.1:80/v1/chat/completions?q=x",
            "http://127.0.0.1:80/v1/chat/completions#x",
            "http://127.0.0.1:80/other",
        ]:
            with self.subTest(u=u), self.assertRaises(Refused):
                endpoint(u)

    def getter(self, slots=None, models=None):
        def g(url):
            if url.endswith("/health"):
                return {"status": "ok"}
            if url.endswith("/v1/models"):
                return {"data": models if models is not None else [{"id": "local"}]}
            return slots

        return g

    def test_ready_explicit_idle(self):
        e = readiness("http://127.0.0.1:8081/v1/chat/completions", "local", self.getter([{"is_processing": False}]))
        self.assertEqual(route(e)["worker"], "m5-macbook-pro")

    def test_missing_or_busy_slots_refused(self):
        for s in [None, [], {}, [{}], [{"is_processing": True}], [{"is_processing": 0}]]:
            with self.subTest(s=s), self.assertRaises(Refused):
                readiness("http://127.0.0.1:8081/v1/chat/completions", "local", self.getter(s))

    def test_missing_model_refused(self):
        with self.assertRaises(Refused):
            readiness("http://127.0.0.1:8081/v1/chat/completions", "local", self.getter([{"is_processing": False}], []))

    def test_notion_refused(self):
        with self.assertRaises(Refused):
            route(WorkerEvidence(time.time(), True, True, True, False), "notion")

    def test_stale_refused(self):
        with self.assertRaises(Refused):
            route(WorkerEvidence(time.time() - 31, True, True, True, False))

    def test_environment_allowlist(self):
        expected = {"PATH", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE"}
        if os.name == "nt":
            expected |= {"SYSTEMROOT", "TEMP", "TMP", "USERPROFILE"}  # Windows processes need these to start
        self.assertEqual(set(clean_env()), expected)

    @unittest.skipUnless(NISI_AVAILABLE, "pinned Nisi vendor or node missing")
    def test_actual_nisi_fixture_integration(self):
        r = execute("fixture", "http://127.0.0.1:8081/v1/chat/completions", "fixture-only")
        self.assertEqual(r["report"]["outcome"], "COMPLETED")
        self.assertEqual(r["modelCalls"], 0)
        self.assertFalse(r["independentLLMReview"])
        self.assertEqual(len(r["authorReceipts"]), 1)
        self.assertTrue(r["reportStored"])
        self.assertTrue(r["reportPath"])

    def test_online_mode_refused(self):
        with self.assertRaises(Refused):
            execute("online", "http://127.0.0.1:8081/v1/chat/completions", "x")


if __name__ == "__main__":
    unittest.main()
