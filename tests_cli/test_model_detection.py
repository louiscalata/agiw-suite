"""Model inventory detection: llama-server first, LM Studio only on 404/unavailable."""

import unittest

from agiw.fleet_local_pilot import Refused, detect_models

BASE = "http://127.0.0.1:19999"


class ModelDetectionTests(unittest.TestCase):
    def test_llama_server_first(self):
        seen = []

        def getter(url):
            seen.append(url)
            return {"data": [{"id": "a"}, {"id": "b"}]}

        self.assertEqual(detect_models(BASE, getter), (["a", "b"], "llama-server"))
        self.assertEqual(seen, [BASE + "/v1/models"])

    def test_404_falls_back_to_lm_studio_loaded_only(self):
        def getter(url):
            if url.endswith("/v1/models"):
                raise Refused("HEALTH_HTTP")  # 404
            return {
                "data": [
                    {"id": "loaded-one", "state": "loaded"},
                    {"id": "not-loaded", "state": "not_loaded"},
                    {"id": "no-state"},
                ]
            }

        self.assertEqual(
            detect_models(BASE, getter),
            (["loaded-one", "no-state"], "lm-studio"),
        )

    def test_both_fail_is_refused(self):
        def getter(url):
            raise Refused("HEALTH_HTTP")

        with self.assertRaises(Refused):
            detect_models(BASE, getter)

    def test_malformed_200_is_not_fallback(self):
        def getter(url):
            return {"unexpected": True}

        with self.assertRaises(Refused):
            detect_models(BASE, getter)

    def test_cap(self):
        def getter(url):
            return {"data": [{"id": str(i)} for i in range(65)]}

        with self.assertRaises(Refused):
            detect_models(BASE, getter)


if __name__ == "__main__":
    unittest.main()
