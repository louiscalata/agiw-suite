"""Audit regressions using in-memory transport fixtures only."""

import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import eci_fast_check as fast
import eci_fast_check_adapter as adapter
import test_eci_fast_check_adapter as fixtures


class AdapterCorrectnessTests(unittest.TestCase):
    fixture = fixtures.AdapterTests()

    def test_different_models_share_comparison_settings_and_rank_group(self):
        records = []
        with self.fixture.tempdir() as directory:
            rows = [dict(id=str(i), question="Choose.", choices=["red", "blue", "green", "yellow"],
                         answer="ABCD"[i % 4]) for i in range(200)]
            sample = self.fixture.sample(directory, items=rows)
            selected = fast.make_plan({"arc": {"items": rows}})["benchmarks"][0]["item_ids"]
            answers = {row["id"]: row["answer"] for row in rows}
            for index, name in enumerate(("fixture-first", "fixture-second")):
                with patch.object(fixtures, "MODEL_ID", name):
                    properties = fixtures.props()
                    properties["model"].update(sha256=str(index + 1) * 64, file_size_bytes=100 + index)
                    with fixtures.Stub(properties=properties, replies=[
                        fixtures.completion(answers[item_id]) for item_id in selected
                    ]) as stub:
                        records.append(adapter.run(stub.url, name, [sample], Path(directory) / name,
                                                   machine_label="fixture-system", file_size_bytes=100+index))
            self.assertEqual(records[0]["runtime_settings"], records[1]["runtime_settings"])
            self.assertTrue(fast.hardware_known(records[0]["hardware"]))
            self.assertEqual(fast.pair_verdict(*records)[0], "INCONCLUSIVE")
            groups = fast.rank(records)
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0]["rows"]), 2)
            self.assertIn("server_facts", records[0]["provenance"])
            self.assertNotIn("pre_inputs", records[0]["runtime_settings"])

    def test_explicit_hardware_facts_are_recorded_and_safe(self):
        with self.fixture.tempdir() as directory, fixtures.Stub(properties={}, get_status=404) as stub:
            result = self.fixture.run_fixture(stub, [self.fixture.sample(directory)], Path(directory) / "out",
                                              machine_label="fixture-system", cpu="fixture-cpu", gpu="fixture-gpu",
                                              memory_bytes=10000, runtime_build_id="fixture-build")
            self.assertTrue(fast.hardware_known(result["hardware"]))
            self.assertEqual(result["provenance"]["explicit_hardware_facts"],
                             ["cpu", "gpu", "machine_label", "memory", "runtime_build_id"])
            self.assertEqual(result["provenance"]["hash_attestation"], "caller-supplied; unverified")
        with self.fixture.tempdir() as directory, fixtures.Stub() as stub:
            with self.assertRaises(fast.ValidationError):
                self.fixture.run_fixture(stub, [self.fixture.sample(directory)], Path(directory) / "out",
                                         machine_label="unsafe/path")
            self.assertEqual(stub.posts, [])

    def test_transient_request_is_retried_once_and_permanent_error_is_not(self):
        row = dict(id="one", question="Choose.", answer="A")
        for replies, expected, count in (([(503, {}), fixtures.completion()], "correct", 2),
                                         ([(503, {})], None, 2), ((400, {}), None, 1)):
            values = replies if isinstance(replies, list) else [replies]
            with self.subTest(replies=replies), fixtures.Stub(replies=values) as stub:
                client = adapter.OpenAIAdapter(stub.url, fixtures.MODEL_ID)
                if expected:
                    self.assertEqual(client.answer(row, benchmark="arc", spec={})["outcome"], expected)
                else:
                    with self.assertRaises(adapter.ResponseError):
                        client.answer(row, benchmark="arc", spec={})
                self.assertEqual(len(stub.posts), count)

    def test_thinking_default_lane_setting_is_opt_in_and_comparison_bound(self):
        with self.fixture.tempdir() as directory, fixtures.Stub() as stub:
            sample = self.fixture.sample(directory)
            records = []
            for enabled in (False, True):
                records.append(self.fixture.run_fixture(stub, [sample], Path(directory) / str(enabled),
                                                        disable_thinking=enabled))
                if enabled:
                    self.assertEqual(stub.posts[-1]["chat_template_kwargs"], {"enable_thinking": False})
                else:
                    self.assertNotIn("chat_template_kwargs", stub.posts[-1])
            self.assertNotEqual(records[0]["runtime_settings"], records[1]["runtime_settings"])

    def test_llama_metadata_aliases_cannot_be_overridden_by_caller(self):
        for meta, supplied in (({"ftype": "Q8_0"}, {"quantization": "Q4_K_M"}),):
            with self.subTest(meta=meta), self.fixture.tempdir() as directory, fixtures.Stub(
                    properties={}, get_status=404, model_meta=meta) as stub:
                facts = dict(model_sha256=fixtures.MODEL_SHA, file_size_bytes=100,
                             quantization="Q4_K_M", available_memory_bytes=8000)
                facts.update(supplied)
                with self.assertRaisesRegex(fast.ValidationError, "disagrees"):
                    adapter.run(stub.url, fixtures.MODEL_ID, [self.fixture.sample(directory)],
                                Path(directory) / "out", **facts)
                self.assertEqual(stub.posts, [])
        with fixtures.Stub(properties={}, get_status=404, model_meta={"size": 100, "ftype": "q4_k_m"}) as stub:
            model, _ = adapter.OpenAIAdapter(stub.url, fixtures.MODEL_ID).discover()
            self.assertIsNone(model["file_size_bytes"])
            self.assertEqual(model["quantization"], "Q4_K_M")

    def test_path_model_uses_explicit_public_label_and_context_caps_output(self):
        private_model = str(Path("/models") / "fixture.gguf")
        with patch.object(fixtures, "MODEL_ID", private_model):
            properties = fixtures.props()
            properties["default_generation_settings"]["n_ctx"] = 4096
            with self.fixture.tempdir() as directory, fixtures.Stub(properties=properties) as stub:
                record = adapter.run(stub.url, private_model, [self.fixture.sample(directory)],
                                     Path(directory) / "out", model_label="fixture-model", file_size_bytes=100)
                self.assertEqual(record["model"]["name"], "fixture-model")
                self.assertNotIn(private_model, json.dumps(record))
                self.assertEqual(stub.posts[0]["model"], private_model)
                self.assertLess(stub.posts[0]["max_tokens"], 4096 - adapter.prompt_size_estimate(stub.posts[0]["messages"]))
                self.assertEqual(record["runtime_settings"]["max_tokens"], 4096)

    def test_cli_incomplete_result_reports_stop_reason_and_nonzero(self):
        record = {"post": {"decision": "UNKNOWN"}, "benchmarks": [
            {"benchmark": "arc", "stop_reason": "budget", "attempted": 0, "planned": 200}]}
        output = io.StringIO()
        with patch.object(adapter, "run", return_value=record), contextlib.redirect_stdout(output):
            code = adapter.main(["--base-url", "fixture", "--model", "fixture-model",
                                 "--samples", "fixture.json", "--output-dir", "fixture-output"])
        self.assertEqual(code, 3)
        self.assertIn("budget", output.getvalue())
        self.assertIn("UNKNOWN", output.getvalue())

    def test_numeric_unit_evidence_survives_checkpoint_and_resume(self):
        row = dict(id="one", question="Calculate.", answer="12 cents")
        with self.fixture.tempdir() as directory, fixtures.Stub(replies=[fixtures.completion("12 cents")]) as stub:
            sample = self.fixture.sample(directory, items=[row], benchmark="gsm8k")
            output = Path(directory) / "out"
            for _ in range(2):
                record = self.fixture.run_fixture(stub, [sample], output)
                fast.validate_record(record)
                self.assertEqual(record["benchmarks"][0]["observations"][0]["parsed_unit"], "cents")
            self.assertEqual(len(stub.posts), 1)
            checkpoint = json.loads((output / "checkpoint.jsonl").read_text().splitlines()[0])
            self.assertEqual(checkpoint["observation"]["parsed_unit"], "cents")

    def test_endpoint_change_cannot_reuse_checkpoint(self):
        with self.fixture.tempdir() as directory, fixtures.Stub() as stub:
            sample = self.fixture.sample(directory)
            output = Path(directory) / "out"
            first = self.fixture.run_fixture(stub, [sample], output)
            posts = len(stub.posts)
            # The stub intercepts the connection; these remain inert parser inputs.
            _, port, _ = adapter.validate_base_url(stub.url)
            stub.url = stub.url.rsplit(":", 1)[0] + ":" + str(port + 1)
            with self.assertRaisesRegex(fast.ValidationError, "checkpoint"):
                self.fixture.run_fixture(stub, [sample], output)
            self.assertEqual(len(stub.posts), posts)
            second = self.fixture.run_fixture(stub, [sample], Path(directory) / "other")
            self.assertEqual(first["runtime_settings"], second["runtime_settings"])
            self.assertNotEqual(first["provenance"]["endpoint_sha256"],
                                second["provenance"]["endpoint_sha256"])
            self.assertEqual(len(first["provenance"]["endpoint_sha256"]), 64)
            self.assertNotIn(stub.url, json.dumps(second))


class ParserCorrectnessTests(unittest.TestCase):
    row = dict(id="one", question="Choose.", choices=["red", "blue", "green", "yellow"], answer="B")

    def test_common_markdown_and_matching_label_text_are_parsed(self):
        for content in ("**Answer:** **B**", "`Answer: B`", '"Answer: B"', "B. blue", "B) blue.",
                        "Answer: blue.", "Reasoning.\n**Final answer: B. blue**"):
            with self.subTest(content=content):
                self.assertEqual(adapter.extract_answer(content, self.row, "arc"), "B")
        for content in ("B. green", "B) red", "**Answer: A**\n**Answer: B**", "B. blue or green"):
            with self.subTest(content=content):
                self.assertIsNone(adapter.extract_answer(content, self.row, "arc"))
        self.assertEqual(adapter.number_answer(r"$$\boxed{12}$$"), "12")
        self.assertEqual(adapter.number_answer("**Answer:** **12**"), "12")

    def test_scaled_or_ambiguous_units_are_not_scored_as_bare_numbers(self):
        row = dict(id="one", question="Calculate.", answer="12")
        for content in ("12 cents", "12 M", "Answer: 12 millions"):
            with self.subTest(content=content), fixtures.Stub(replies=[fixtures.completion(content)]) as stub:
                result = adapter.OpenAIAdapter(stub.url, fixtures.MODEL_ID).answer(row, benchmark="gsm8k", spec={})
                self.assertEqual(result["outcome"], "unparsed")
        for unit in ("%", "cents"):
            content = "12" + (" " if unit == "cents" else "") + unit
            with self.subTest(unit=unit), fixtures.Stub(replies=[fixtures.completion(content)]) as stub:
                result = adapter.OpenAIAdapter(stub.url, fixtures.MODEL_ID).answer(
                    dict(row, answer=content), benchmark="gsm8k", spec={})
                self.assertEqual(result["outcome"], "correct")
                self.assertEqual(result["parsed_unit"], unit)

    def test_terminal_punctuation_does_not_break_exact_choice_text(self):
        row = dict(self.row, choices=["red.", "blue.", "green.", "yellow."])
        for content in ("blue.", "B. blue.", "Answer: blue."):
            self.assertEqual(adapter.extract_answer(content, row, "arc"), "B")


if __name__ == "__main__":
    unittest.main()
