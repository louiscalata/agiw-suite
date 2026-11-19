"""Speculative decoding config schema validation and fallback policy tests."""

import unittest

import speculative_config as spec


class ValidateSpeculativeTests(unittest.TestCase):
    def test_none_is_valid(self):
        self.assertIsNone(spec.validate_speculative(None, "pc"))

    def test_empty_dict_is_valid_and_disabled(self):
        result = spec.validate_speculative({}, "pc")
        self.assertEqual(result, {})

    def test_enabled_defaults_to_false(self):
        result = spec.validate_speculative({}, "pc")
        self.assertNotIn("enabled", result)
        normalized = spec.normalize_speculative(result)
        self.assertFalse(normalized["enabled"])

    def test_enabled_false_explicit(self):
        result = spec.validate_speculative({"enabled": False}, "pc")
        self.assertEqual(result["enabled"], False)

    def test_enabled_true_requires_backend(self):
        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative({"enabled": True, "modelKey": "key"}, "pc")
        self.assertIn("backend", str(caught.exception))
        self.assertIn("required when enabled", str(caught.exception))

    def test_enabled_true_requires_exactly_one_model_ref(self):
        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative({"enabled": True, "backend": "llama.cpp"}, "pc")
        self.assertIn("modelKey", str(caught.exception))
        self.assertIn("exactly one", str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative(
                {"enabled": True, "backend": "llama.cpp", "modelKey": "key", "modelPath": "/path"}, "pc"
            )
        self.assertIn("modelKey", str(caught.exception))
        self.assertIn("cannot specify both", str(caught.exception))

    def test_enabled_true_valid_with_model_key(self):
        result = spec.validate_speculative({"enabled": True, "backend": "llama.cpp", "modelKey": "my-draft"}, "pc")
        self.assertTrue(result["enabled"])
        self.assertEqual(result["backend"], "llama.cpp")
        self.assertEqual(result["modelKey"], "my-draft")

    def test_enabled_true_valid_with_model_path(self):
        result = spec.validate_speculative(
            {"enabled": True, "backend": "lmstudio-dflash", "modelPath": "/models/draft.gguf"}, "pc"
        )
        self.assertTrue(result["enabled"])
        self.assertEqual(result["backend"], "lmstudio-dflash")
        self.assertEqual(result["modelPath"], "/models/draft.gguf")

    def test_disabled_config_may_include_optional_fields_still_type_checked(self):
        result = spec.validate_speculative(
            {
                "enabled": False,
                "backend": "llama.cpp",
                "type": "draft-dflash",
                "modelKey": "key",
                "nMax": 16,
                "nMin": 2,
                "deviceDraft": "cpu",
                "threadsDraft": 4,
                "gpuLayersDraft": 0,
                "fallback": "plain",
            },
            "pc",
        )
        self.assertEqual(result["nMax"], 16)

        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative({"enabled": False, "nMax": "sixteen"}, "pc")
        self.assertIn("nMax", str(caught.exception))

    def test_unknown_fields_rejected(self):
        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative({"unknownField": "value"}, "pc")
        self.assertIn("unknownField", str(caught.exception))
        self.assertIn("not allowed", str(caught.exception))

    def test_bad_backend_rejected(self):
        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative({"backend": "invalid-backend"}, "pc")
        self.assertIn("backend", str(caught.exception))
        self.assertIn("invalid-backend", str(caught.exception))

    def test_bad_type_rejected(self):
        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative({"type": "invalid-type"}, "pc")
        self.assertIn("type", str(caught.exception))
        self.assertIn("invalid-type", str(caught.exception))

    def test_nmin_nmax_bounds(self):
        for bad in (0, 33, -1, 100):
            with self.subTest(nMax=bad), self.assertRaises(ValueError):
                spec.validate_speculative({"nMax": bad}, "pc")
            with self.subTest(nMin=bad), self.assertRaises(ValueError):
                spec.validate_speculative({"nMin": bad}, "pc")

        result = spec.validate_speculative({"nMin": 1, "nMax": 32}, "pc")
        self.assertEqual((result["nMin"], result["nMax"]), (1, 32))

    def test_nmin_exceeds_nmax_rejected(self):
        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative({"nMin": 10, "nMax": 5}, "pc")
        self.assertIn("nMin", str(caught.exception))
        self.assertIn("must not exceed", str(caught.exception))

    def test_empty_model_strings_rejected(self):
        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative({"modelKey": ""}, "pc")
        self.assertIn("modelKey", str(caught.exception))
        self.assertIn("non-empty string", str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative({"modelPath": ""}, "pc")
        self.assertIn("modelPath", str(caught.exception))

    def test_threads_draft_bounds(self):
        with self.assertRaises(ValueError):
            spec.validate_speculative({"threadsDraft": 0}, "pc")
        with self.assertRaises(ValueError):
            spec.validate_speculative({"threadsDraft": -1}, "pc")
        result = spec.validate_speculative({"threadsDraft": 1}, "pc")
        self.assertEqual(result["threadsDraft"], 1)

    def test_gpu_layers_draft_bounds(self):
        with self.assertRaises(ValueError):
            spec.validate_speculative({"gpuLayersDraft": -1}, "pc")
        result = spec.validate_speculative({"gpuLayersDraft": 0}, "pc")
        self.assertEqual(result["gpuLayersDraft"], 0)

    def test_device_draft_non_empty_string(self):
        with self.assertRaises(ValueError):
            spec.validate_speculative({"deviceDraft": ""}, "pc")
        result = spec.validate_speculative({"deviceDraft": "CUDA0"}, "pc")
        self.assertEqual(result["deviceDraft"], "CUDA0")

    def test_fallback_only_plain_allowed(self):
        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative({"fallback": "skip"}, "pc")
        self.assertIn("fallback", str(caught.exception))
        result = spec.validate_speculative({"fallback": "plain"}, "pc")
        self.assertEqual(result["fallback"], "plain")

    def test_enabled_must_be_boolean(self):
        for bad in ("true", 1, None, "yes"):
            with self.subTest(enabled=bad), self.assertRaises(ValueError):
                spec.validate_speculative({"enabled": bad}, "pc")

    def test_non_dict_speculative_rejected(self):
        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative("not a dict", "pc")
        self.assertIn("must be an object", str(caught.exception))

        with self.assertRaises(ValueError):
            spec.validate_speculative([], "pc")

    def test_error_includes_machine_and_lane_context(self):
        with self.assertRaises(ValueError) as caught:
            spec.validate_speculative({"unknownField": 1}, "worker-1", "fast-lane")
        message = str(caught.exception)
        self.assertIn("machine 'worker-1'", message)
        self.assertIn("lane 'fast-lane'", message)
        self.assertIn("speculative field", message)


class EffectiveSpeculativeTests(unittest.TestCase):
    def test_lane_overrides_machine(self):
        machine_spec = {"enabled": True, "backend": "llama.cpp", "modelKey": "machine-draft"}
        lane_spec = {"enabled": False}
        result = spec.effective_speculative(machine_spec, lane_spec)
        self.assertEqual(result, lane_spec)

    def test_machine_default_used_when_lane_omits(self):
        machine_spec = {"enabled": True, "backend": "llama.cpp", "modelKey": "machine-draft"}
        result = spec.effective_speculative(machine_spec, None)
        self.assertEqual(result, machine_spec)

    def test_both_none_returns_none(self):
        result = spec.effective_speculative(None, None)
        self.assertIsNone(result)


class NormalizeSpeculativeTests(unittest.TestCase):
    def test_none_normalizes_to_disabled(self):
        result = spec.normalize_speculative(None)
        self.assertEqual(result, {"enabled": False, "fallback": "plain"})

    def test_empty_dict_normalizes_to_disabled(self):
        result = spec.normalize_speculative({})
        self.assertEqual(result, {"enabled": False, "fallback": "plain"})

    def test_preserves_existing_fields(self):
        original = {"enabled": True, "backend": "llama.cpp", "modelKey": "draft"}
        result = spec.normalize_speculative(original)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["backend"], "llama.cpp")
        self.assertEqual(result["fallback"], "plain")

    def test_does_not_mutate_input(self):
        original = {"enabled": True, "backend": "llama.cpp", "modelKey": "draft"}
        original_copy = dict(original)
        spec.normalize_speculative(original)
        self.assertEqual(original, original_copy)


class ResolveLaneSpeculativeTests(unittest.TestCase):
    def test_lane_config_used_when_present(self):
        machine = {"speculative": {"enabled": True, "backend": "llama.cpp", "modelKey": "machine"}}
        lane = {"speculative": {"enabled": False}}
        result = spec.resolve_lane_speculative(machine, lane)
        self.assertFalse(result["enabled"])

    def test_machine_config_inherited_when_lane_omits(self):
        machine = {"speculative": {"enabled": True, "backend": "llama.cpp", "modelKey": "machine"}}
        lane = {}
        result = spec.resolve_lane_speculative(machine, lane)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["modelKey"], "machine")

    def test_defaults_applied_when_both_omit(self):
        result = spec.resolve_lane_speculative({}, {})
        self.assertFalse(result["enabled"])
        self.assertEqual(result["fallback"], "plain")


class ResolveSpeculativePlanTests(unittest.TestCase):
    def test_none_config_returns_disabled(self):
        mode, reason = spec.resolve_speculative_plan(None)
        self.assertEqual((mode, reason), ("plain", "disabled"))

    def test_disabled_config_returns_disabled(self):
        mode, reason = spec.resolve_speculative_plan({"enabled": False, "fallback": "plain"})
        self.assertEqual((mode, reason), ("plain", "disabled"))

    def test_enabled_config_happy_path(self):
        config = {"enabled": True, "backend": "llama.cpp", "modelKey": "draft", "fallback": "plain"}
        mode, reason = spec.resolve_speculative_plan(config)
        self.assertEqual((mode, reason), ("speculative", None))

    def test_enabled_with_unknown_checks_returns_speculative(self):
        config = {"enabled": True, "backend": "llama.cpp", "modelKey": "draft", "fallback": "plain"}
        mode, reason = spec.resolve_speculative_plan(config, draft_present=None, vocab_compatible=None)
        self.assertEqual((mode, reason), ("speculative", None))

    def test_missing_draft_falls_back_to_plain(self):
        config = {"enabled": True, "backend": "llama.cpp", "modelKey": "draft", "fallback": "plain"}
        mode, reason = spec.resolve_speculative_plan(config, draft_present=False)
        self.assertEqual((mode, reason), ("plain", "missing_draft"))

    def test_vocab_mismatch_falls_back_to_plain(self):
        config = {"enabled": True, "backend": "llama.cpp", "modelKey": "draft", "fallback": "plain"}
        mode, reason = spec.resolve_speculative_plan(config, vocab_compatible=False)
        self.assertEqual((mode, reason), ("plain", "vocab_mismatch"))

    def test_load_failure_falls_back_to_plain(self):
        config = {"enabled": True, "backend": "llama.cpp", "modelKey": "draft", "fallback": "plain"}
        mode, reason = spec.resolve_speculative_plan(config, load_error="failed to load")
        self.assertEqual((mode, reason), ("plain", "load_failure"))

    def test_draft_present_true_allows_speculative(self):
        config = {"enabled": True, "backend": "llama.cpp", "modelKey": "draft", "fallback": "plain"}
        mode, reason = spec.resolve_speculative_plan(config, draft_present=True, vocab_compatible=True)
        self.assertEqual((mode, reason), ("speculative", None))

    def test_fallback_priority_order(self):
        config = {"enabled": True, "backend": "llama.cpp", "modelKey": "draft", "fallback": "plain"}
        mode, reason = spec.resolve_speculative_plan(
            config, draft_present=False, vocab_compatible=False, load_error="error"
        )
        self.assertEqual(reason, "missing_draft")

        mode, reason = spec.resolve_speculative_plan(config, draft_present=True, vocab_compatible=False, load_error="e")
        self.assertEqual(reason, "vocab_mismatch")


if __name__ == "__main__":
    unittest.main()
