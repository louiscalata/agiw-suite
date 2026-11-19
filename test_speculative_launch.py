"""Per-backend speculative decoding launch and load hook tests."""

import unittest

import speculative_launch as launch
from speculative_config import normalize_speculative


class LlamaCppDraftArgsTests(unittest.TestCase):
    def test_none_config_returns_empty(self):
        self.assertEqual(launch.llama_cpp_draft_args(None), [])

    def test_disabled_config_returns_empty(self):
        config = normalize_speculative({"enabled": False})
        self.assertEqual(launch.llama_cpp_draft_args(config), [])

    def test_lmstudio_backend_returns_empty(self):
        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "lmstudio-dflash",
                "modelKey": "draft-model",
            }
        )
        self.assertEqual(launch.llama_cpp_draft_args(config), [])

    def test_llama_cpp_with_model_path(self):
        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "llama.cpp",
                "modelPath": "/models/draft.gguf",
            }
        )
        args = launch.llama_cpp_draft_args(config)
        self.assertIn("--spec-draft-model", args)
        self.assertEqual(args[args.index("--spec-draft-model") + 1], "/models/draft.gguf")

    def test_llama_cpp_with_model_key(self):
        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "llama.cpp",
                "modelKey": "publisher/draft",
            }
        )
        args = launch.llama_cpp_draft_args(config)
        self.assertIn("--spec-draft-model", args)
        self.assertEqual(args[args.index("--spec-draft-model") + 1], "publisher/draft")

    def test_llama_cpp_full_config(self):
        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "llama.cpp",
                "modelPath": "/models/draft.gguf",
                "nMax": 16,
                "nMin": 2,
                "type": "draft-dflash",
                "deviceDraft": "cuda:0",
                "gpuLayersDraft": 32,
            }
        )
        args = launch.llama_cpp_draft_args(config)
        self.assertEqual(args[args.index("--spec-draft-model") + 1], "/models/draft.gguf")
        self.assertEqual(args[args.index("--spec-draft-n-max") + 1], "16")
        self.assertEqual(args[args.index("--spec-draft-n-min") + 1], "2")
        self.assertEqual(args[args.index("--spec-type") + 1], "draft-dflash")
        self.assertEqual(args[args.index("--spec-draft-device") + 1], "cuda:0")
        self.assertEqual(args[args.index("--spec-draft-ngl") + 1], "32")

    def test_llama_cpp_no_optional_fields(self):
        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "llama.cpp",
                "modelKey": "draft",
            }
        )
        args = launch.llama_cpp_draft_args(config)
        self.assertEqual(args, ["--spec-draft-model", "draft"])

    def test_missing_draft_fallback_returns_empty(self):
        from speculative_config import resolve_speculative_plan

        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "llama.cpp",
                "modelKey": "draft",
            }
        )
        mode, reason = resolve_speculative_plan(config, draft_present=False)
        self.assertEqual((mode, reason), ("plain", "missing_draft"))
        modified = dict(config)
        modified["_fallback_reason"] = reason
        args = launch.llama_cpp_draft_args(modified)
        self.assertIn("--spec-draft-model", args)

    def test_vocab_mismatch_returns_empty_when_plan_resolved(self):
        from speculative_config import resolve_speculative_plan

        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "llama.cpp",
                "modelKey": "draft",
            }
        )
        mode, reason = resolve_speculative_plan(config, vocab_compatible=False)
        self.assertEqual(reason, "vocab_mismatch")


class LmstudioDflashLoadFieldsTests(unittest.TestCase):
    def test_none_config_returns_empty(self):
        self.assertEqual(launch.lmstudio_dflash_load_fields(None), [])

    def test_disabled_config_returns_empty(self):
        config = normalize_speculative({"enabled": False})
        self.assertEqual(launch.lmstudio_dflash_load_fields(config), [])

    def test_llama_cpp_backend_returns_empty(self):
        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "llama.cpp",
                "modelKey": "draft-model",
            }
        )
        self.assertEqual(launch.lmstudio_dflash_load_fields(config), [])

    def test_dflash_default_type_uses_dflash_sidecar(self):
        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "lmstudio-dflash",
                "modelKey": "draft-model",
            }
        )
        fields = launch.lmstudio_dflash_load_fields(config)
        self.assertTrue(
            any(f["key"] == "llm.load.speculativeDecoding.draftDflashSidecar" and f["value"] is True for f in fields)
        )
        self.assertTrue(
            any(f["key"] == "llm.load.speculativeDecoding.draftModel" and f["value"] == "draft-model" for f in fields)
        )

    def test_dflash_explicit_dflash_type(self):
        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "lmstudio-dflash",
                "type": "draft-dflash",
                "modelKey": "draft-model",
            }
        )
        fields = launch.lmstudio_dflash_load_fields(config)
        self.assertTrue(
            any(f["key"] == "llm.load.speculativeDecoding.draftDflashSidecar" and f["value"] is True for f in fields)
        )

    def test_dflash_simple_type(self):
        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "lmstudio-dflash",
                "type": "draft-simple",
                "modelKey": "draft-model",
            }
        )
        fields = launch.lmstudio_dflash_load_fields(config)
        self.assertTrue(
            any(f["key"] == "llm.load.speculativeDecoding.draftSimple" and f["value"] is True for f in fields)
        )
        self.assertTrue(
            any(f["key"] == "llm.load.speculativeDecoding.draftDflashSidecar" and f["value"] is False for f in fields)
        )

    def test_dflash_with_model_path(self):
        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "lmstudio-dflash",
                "modelPath": "/models/draft.gguf",
            }
        )
        fields = launch.lmstudio_dflash_load_fields(config)
        self.assertTrue(
            any(
                f["key"] == "llm.load.speculativeDecoding.draftModel" and f["value"] == "/models/draft.gguf"
                for f in fields
            )
        )

    def test_dflash_disables_unused_draft_kinds(self):
        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "lmstudio-dflash",
                "modelKey": "draft-model",
            }
        )
        fields = launch.lmstudio_dflash_load_fields(config)
        disabled_kinds = ["draftMtp", "draftDspark", "draftDsparkSidecar", "draftMtpSidecar"]
        for kind in disabled_kinds:
            key = f"llm.load.speculativeDecoding.{kind}"
            self.assertTrue(
                any(f["key"] == key and f["value"] is False for f in fields),
                f"expected {key} to be False",
            )


class LoadWithOptionalDraftTests(unittest.TestCase):
    def test_unload_always_uses_plain(self):
        calls = []

        def plain(action, model_id):
            calls.append(("plain", action, model_id))

        def dflash(action, model_id, fields):
            calls.append(("dflash", action, model_id, fields))

        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "lmstudio-dflash",
                "modelKey": "draft",
            }
        )
        mode, reason = launch.load_with_optional_draft(
            "unload", "model-123", config, run_plain=plain, run_dflash=dflash
        )
        self.assertEqual((mode, reason), ("plain", None))
        self.assertEqual(calls, [("plain", "unload", "model-123")])

    def test_none_config_uses_plain(self):
        calls = []

        def plain(action, model_id):
            calls.append(("plain", action, model_id))

        mode, reason = launch.load_with_optional_draft("load", "model-123", None, run_plain=plain)
        self.assertEqual((mode, reason), ("plain", None))
        self.assertEqual(calls, [("plain", "load", "model-123")])

    def test_disabled_config_uses_plain(self):
        calls = []

        def plain(action, model_id):
            calls.append(("plain", action, model_id))

        config = normalize_speculative({"enabled": False})
        mode, reason = launch.load_with_optional_draft("load", "model-123", config, run_plain=plain)
        self.assertEqual((mode, reason), ("plain", None))
        self.assertEqual(calls, [("plain", "load", "model-123")])

    def test_llama_cpp_backend_uses_plain(self):
        calls = []

        def plain(action, model_id):
            calls.append(("plain", action, model_id))

        def dflash(action, model_id, fields):
            calls.append(("dflash", action, model_id, fields))

        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "llama.cpp",
                "modelKey": "draft",
            }
        )
        mode, reason = launch.load_with_optional_draft("load", "model-123", config, run_plain=plain, run_dflash=dflash)
        self.assertEqual((mode, reason), ("plain", None))
        self.assertEqual(calls, [("plain", "load", "model-123")])

    def test_dflash_success_does_not_call_plain(self):
        calls = []

        def plain(action, model_id):
            calls.append(("plain", action, model_id))

        def dflash(action, model_id, fields):
            calls.append(("dflash", action, model_id, fields))

        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "lmstudio-dflash",
                "modelKey": "draft",
            }
        )
        mode, reason = launch.load_with_optional_draft("load", "model-123", config, run_plain=plain, run_dflash=dflash)
        self.assertEqual((mode, reason), ("dflash", None))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "dflash")
        self.assertEqual(calls[0][1], "load")
        self.assertEqual(calls[0][2], "model-123")
        self.assertTrue(len(calls[0][3]) > 0)

    def test_dflash_failure_falls_back_to_plain(self):
        calls = []

        def plain(action, model_id):
            calls.append(("plain", action, model_id))

        def dflash(action, model_id, fields):
            calls.append(("dflash", action, model_id, fields))
            raise RuntimeError("DFlash load failed")

        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "lmstudio-dflash",
                "modelKey": "draft",
            }
        )
        mode, reason = launch.load_with_optional_draft("load", "model-123", config, run_plain=plain, run_dflash=dflash)
        self.assertEqual((mode, reason), ("plain", "load_failure"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "dflash")
        self.assertEqual(calls[1], ("plain", "load", "model-123"))

    def test_missing_dflash_runner_falls_back_to_plain(self):
        calls = []

        def plain(action, model_id):
            calls.append(("plain", action, model_id))

        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "lmstudio-dflash",
                "modelKey": "draft",
            }
        )
        mode, reason = launch.load_with_optional_draft("load", "model-123", config, run_plain=plain, run_dflash=None)
        self.assertEqual((mode, reason), ("plain", "load_failure"))
        self.assertEqual(calls, [("plain", "load", "model-123")])

    def test_dflash_fields_passed_to_runner(self):
        received_fields = []

        def plain(action, model_id):
            pass

        def dflash(action, model_id, fields):
            received_fields.extend(fields)

        config = normalize_speculative(
            {
                "enabled": True,
                "backend": "lmstudio-dflash",
                "modelKey": "my-draft-model",
            }
        )
        launch.load_with_optional_draft("load", "model-123", config, run_plain=plain, run_dflash=dflash)
        self.assertTrue(
            any(
                f["key"] == "llm.load.speculativeDecoding.draftModel" and f["value"] == "my-draft-model"
                for f in received_fields
            )
        )


if __name__ == "__main__":
    unittest.main()
