import unittest
from unittest import mock

import speculative_telemetry as spec


class SpeculativeTelemetryTests(unittest.TestCase):
    def test_empty_timings(self):
        expected = dict.fromkeys(
            ("predictedPerSecond", "promptPerSecond", "draftN", "draftNAccepted", "draftAcceptanceRate")
        )
        for raw in (None, {}, [], "timings", False):
            with self.subTest(raw=raw):
                self.assertEqual(spec.parse_llama_timings(raw), expected)

    def test_snake_case_timings(self):
        self.assertEqual(
            spec.parse_llama_timings(
                {"predicted_per_second": 40, "prompt_per_second": 90, "draft_n": 100, "draft_n_accepted": 50}
            ),
            {
                "predictedPerSecond": 40.0,
                "promptPerSecond": 90.0,
                "draftN": 100,
                "draftNAccepted": 50,
                "draftAcceptanceRate": 0.5,
            },
        )

    def test_aliases(self):
        parsed = spec.parse_llama_timings(
            {"predictedPerSecond": 1e6, "promptPerSecond": 0, "n_draft": 8, "n_accepted": 2}
        )
        self.assertEqual((parsed["predictedPerSecond"], parsed["promptPerSecond"]), (1e6, 0.0))
        self.assertEqual((parsed["draftN"], parsed["draftNAccepted"], parsed["draftAcceptanceRate"]), (8, 2, 0.25))
        slot = spec.parse_slot_speculative({"n_drafted": 4, "n_draft_accepted": 3})
        self.assertEqual(slot, {"draftN": 4, "draftNAccepted": 3, "draftAcceptanceRate": 0.75})
        self.assertEqual(spec.parse_slot_speculative({"speculative": True}), dict.fromkeys(slot))

    def test_invalid_denominator_or_ratio(self):
        for drafted, accepted in ((0, 0), (3, 4), (None, 2)):
            with self.subTest(drafted=drafted, accepted=accepted):
                parsed = spec.parse_llama_timings({"draft_n": drafted, "draft_n_accepted": accepted})
                self.assertEqual((parsed["draftN"], parsed["draftNAccepted"]), (drafted, accepted))
                self.assertIsNone(parsed["draftAcceptanceRate"])
        self.assertIsNone(
            spec.receipt_speculative_fields(record={"draftN": 3, "draftNAccepted": 4, "draftAcceptanceRate": 1})[
                "draftAcceptanceRate"
            ]
        )

    def test_malformed_values(self):
        for value in (True, -1, 2**31, 10**400, float("nan"), float("inf"), -float("inf"), "1"):
            with self.subTest(value=value):
                parsed = spec.parse_llama_timings(
                    {
                        "predicted_per_second": value,
                        "prompt_per_second": value,
                        "draft_n": value,
                        "draft_n_accepted": value,
                    }
                )
                self.assertTrue(all(field is None for field in parsed.values()))
        self.assertIsNone(spec.parse_llama_timings({"draft_n": 1.0})["draftN"])
        self.assertIsNone(spec.receipt_speculative_fields(record={"draftAcceptanceRate": 1.1})["draftAcceptanceRate"])

    def test_fallback_reason_normalization(self):
        reasons = {"disabled", "missing_draft", "vocab_mismatch", "load_failure"}
        self.assertEqual(spec.FALLBACK_REASONS, frozenset(reasons))
        for value in reasons:
            self.assertEqual(spec.normalize_fallback_reason(value), value)
        for value in ("", "unknown", None, [], True):
            self.assertIsNone(spec.normalize_fallback_reason(value))

    def test_receipt_merge(self):
        fields = spec.receipt_speculative_fields(
            timings={"draft_n": 100},
            record={
                "draftN": 200,
                "draftNAccepted": 50,
                "fallbackReason": "disabled",
                "timings": {"predicted_per_second": 40},
            },
            fallback_reason="load_failure",
        )
        self.assertEqual(
            fields,
            {
                "predictedPerSecond": 40.0,
                "draftN": 100,
                "draftNAccepted": 50,
                "draftAcceptanceRate": 0.5,
                "fallbackReason": "load_failure",
            },
        )
        self.assertEqual(
            spec.receipt_speculative_fields(record={"draftAcceptanceRate": 0.25, "fallbackReason": "disabled"}),
            {
                "predictedPerSecond": None,
                "draftN": None,
                "draftNAccepted": None,
                "draftAcceptanceRate": 0.25,
                "fallbackReason": "disabled",
            },
        )

    def test_parsing_needs_no_external_state(self):
        forbidden = AssertionError("speculative parsing must not use external state")
        with (
            mock.patch("builtins.open", side_effect=forbidden),
            mock.patch("os.getenv", side_effect=forbidden),
            mock.patch("socket.socket", side_effect=forbidden),
        ):
            spec.parse_llama_timings({})
            spec.parse_slot_speculative({})
            spec.normalize_fallback_reason("disabled")
            spec.receipt_speculative_fields()
