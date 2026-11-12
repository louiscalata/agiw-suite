"""Offline screening parser/template regressions; Python 3.9, stdlib only."""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import eci_fast_check as fast
import eci_fast_check_adapter as adapter
from test_eci_fast_check import SETTINGS, datasets, run
import test_eci_fast_check_adapter as fixtures
from test_eci_fast_check_adapter import Stub, completion, MODEL_ID


class NumericScreeningTests(unittest.TestCase):
    def test_equivalent_final_numbers_normalize_exactly_on_both_sides(self):
        cases = {
            "2,125": "2125",
            "1,000.50": "1000.5",
            "$12": "12", "£12": "12", "€12": "12", "¥12": "12",
            "12 dollars": "12", "12 meters.": "12", "12%": "12", "12 €": "12",
            "12.": "12", "-12": "-12", "-$12": "-12", "$-12": "-12",
            "-1,000.50 dollars.": "-1000.5",
            "+12.00": "12", "-0.000": "0", ".125": "0.125", "1.2e3": "1200",
            "Work: 2000 + 125.\n#### 2,125": "2125",
            "Work first.\nFinal answer: $12.": "12",
            "Work first.\nAnswer: 12 dollars": "12",
            r"\boxed{12}": "12", r"\boxed{-1,000.50}": "-1000.5",
            r"$\boxed{12}$": "12", r"\(\boxed{12}\)": "12",
            r"\[\boxed{12}\]": "12", r"#### \boxed{12}": "12",
            r"\boxed{\$12}": "12", r"\boxed{12.}": "12",
            "Work first.\n" + r"\boxed{12}.": "12",
        }
        row = {"id": "n", "question": "Compute."}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(adapter.number_answer(text), expected)
                self.assertEqual(adapter.reference(dict(row, answer=text), "gsm8k"), expected)
                self.assertEqual(adapter.extract_answer(text, row, "gsm8k"), expected)

    def test_numeric_substrings_malformed_groups_and_ambiguous_prose_are_unparsed(self):
        for text in (
            "abc12", "12abc", "word1,000.50word", "A12", "12e", "12eels",
            "12,34", "1,00.50", "1,,000", "1,000,00", "1.000,50", "12..",
            "12 or 13", "12 and 13 dollars", "12 is wrong", "12 dollars more",
            "There are 12 dollars", "The question says 12", "12/2", "1 + 2",
            "NaN", "Infinity", "1e101", "-1e-101", "++12", "$--12",
            r"\boxed{12", r"\boxed{12}\boxed{13}", r"\boxed{\boxed{12}}",
            r"$\boxed{12}\)", "#### 12\nBut perhaps 13", "Reasoning: 12\nMore reasoning.",
            "#### 12 or 13", "#### 12 dollars and cents",
        ):
            with self.subTest(text=text):
                self.assertIsNone(adapter.number_answer(text))

    def test_values_are_exact_no_tolerance_or_decimal_context_rounding(self):
        self.assertNotEqual(adapter.number_answer("1,000.50"), adapter.number_answer("1000.500000000000000000000000001"))
        self.assertNotEqual(adapter.number_answer("-12"), adapter.number_answer("12"))
        self.assertEqual(adapter.number_answer("1234567890123456789012345678901.00"), "1234567890123456789012345678901")

    def test_numeric_rule_identifies_the_final_answer_selection(self):
        row = {"id": "n", "question": "Calculate.", "answer": "12"}
        for content, rule in (("12 dollars", "numeric_complete"),
                              ("Work.\n#### $12", "numeric_marked_final"),
                              ("Work.\n" + r"\boxed{12}", "numeric_boxed_final")):
            self.assertEqual(adapter.parse_answer(content, row, "gsm8k"), ("12", rule))

    def test_answer_equal_to_question_number_is_still_scored(self):
        row = {"id": "same", "question": "There are 12 apples. How many apples are there?", "answer": "$12"}
        with Stub(replies=[completion("The count is unchanged.\n#### 12 apples")]) as stub:
            evidence = adapter.OpenAIAdapter(stub.url, MODEL_ID).answer(row, benchmark="gsm8k", spec={})
        self.assertEqual(evidence["outcome"], "correct")
        self.assertEqual(evidence["parse_rule"], "numeric_marked_final")


class ChoiceScreeningTests(unittest.TestCase):
    row = {"id": "c", "question": "Choose.", "answer": "B", "choices": ["red", "blue", "green", "yellow"]}

    def test_final_rule_is_deterministic_and_case_insensitive(self):
        cases = {
            "A and C look plausible, but B fits.\nAnswer: b": ("B", "mc_explicit_label"),
            "A, B, C and D were considered.\nFinal answer: (b).": ("B", "mc_explicit_label"),
            "A and C are wrong.\nb": ("B", "mc_last_standalone_label"),
            "A\nC\n(b)": ("B", "mc_last_standalone_label"),
            "blue": ("B", "mc_exact_choice_text"),
            "A and C are wrong.\nBLUE": ("B", "mc_exact_choice_text"),
            "Reasoning.\nAnswer: blue": ("B", "mc_explicit_choice_text"),
            "Answer: B\nFinal answer: b": ("B", "mc_explicit_label"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(adapter.parse_answer(text, self.row, "arc"), expected)

    def test_ambiguous_or_embedded_choices_are_unparsed(self):
        for text in (
            "A or B", "The answer might be B", "Answer: A or B", "(B", "B)",
            "Answer: B) ", "Answer: A\nAnswer: B", "Answer: unknown\nAnswer: B",
            "Answer: B\nC", "Answer: B\nMore prose", "A, B, C and D", "B2",
            "blue or green", "Answer: blue and green", "Answer: blue.", "B. blue",
        ):
            with self.subTest(text=text):
                self.assertEqual(adapter.parse_answer(text, self.row, "arc"), (None, None))
        row = dict(self.row, choices=["blue", "BLUE"])
        self.assertIsNone(adapter.extract_answer("blue", row, "arc"))
        self.assertIsNone(adapter.extract_answer("Answer: blue", row, "arc"))

    def test_label_format_precedes_text_fallback_when_choice_text_is_a_label(self):
        row = dict(self.row, choices=["C", "NO_{2}", "O_{2}", "CO"], answer="C")
        for text in ("C", "Answer: C", "Reasoning.\n(c)"):
            self.assertEqual(adapter.extract_answer(text, row, "arc"), "C")

    def test_numeric_choice_labels_and_exact_numeric_choice_text(self):
        row = dict(self.row, choices=[{"label": "1", "text": "red"}, {"label": "2", "text": "blue"}])
        self.assertEqual(adapter.parse_answer("Reasoning.\nAnswer: 2", row, "arc"), ("2", "mc_explicit_label"))
        row = dict(self.row, choices=["12", "13"])
        self.assertEqual(adapter.parse_answer("Answer: 12", row, "arc"), ("A", "mc_explicit_choice_text"))

    def test_wrong_and_unparsed_replies_keep_their_rule_evidence(self):
        with Stub(replies=[completion("Answer: A"), completion("Answer: A or B")]) as stub:
            client = adapter.OpenAIAdapter(stub.url, MODEL_ID)
            wrong = client.answer(self.row, benchmark="arc", spec={})
            unparsed = client.answer(self.row, benchmark="arc", spec={})
        self.assertEqual((wrong["outcome"], wrong["parse_rule"]), ("wrong", "mc_explicit_label"))
        self.assertEqual((unparsed["outcome"], unparsed["parse_rule"]), ("unparsed", None))

    def test_parse_rule_survives_result_checkpoint_and_resume(self):
        fixture = fixtures.AdapterTests()
        with fixture.tempdir() as directory, Stub(replies=[completion("Consider A and C.\nAnswer: B")]) as stub:
            path = fixture.sample(directory, items=[self.row])
            output = Path(directory) / "out"
            first = fixture.run_fixture(stub, [path], output)
            second = fixture.run_fixture(stub, [path], output)
            for record in (first, second):
                fast.validate_record(record)
                observation = record["benchmarks"][0]["observations"][0]
                self.assertEqual(observation["parse_rule"], "mc_explicit_label")
                self.assertEqual(observation["outcome"], "correct")
            checkpoint = json.loads((output / "checkpoint.jsonl").read_text().splitlines()[0])
            self.assertEqual(checkpoint["observation"]["parse_rule"], "mc_explicit_label")
            self.assertEqual(len(stub.posts), 1)


class TemplateScreeningTests(unittest.TestCase):
    def data(self):
        return {"gsm8k": {"items": [{"id": "test", "question": "Test population question?", "answer": "12"}],
                          "demonstrations": [{"id": "train-" + str(i), "question": "Training question " + str(i) + "?",
                                              "answer": "Add 1 and 2.\n#### 3"} for i in range(5)]}}

    def test_five_descriptor_demonstrations_render_with_fixed_final_lines(self):
        data = self.data()
        fast.validate_plan(fast.make_plan(data))
        demos = adapter.demonstrations_for(data)
        prompt = adapter.prompt_for(data["gsm8k"]["items"][0], "gsm8k", demos["gsm8k"])
        self.assertEqual(prompt.count("Add 1 and 2.\n#### 3"), 5)
        self.assertNotIn("#### 12", prompt)
        self.assertTrue(prompt.endswith("End with a single final-answer line in this format:\n#### <number>"))
        self.assertIn("Answer: <choice label>", adapter.prompt_for(ChoiceScreeningTests.row, "arc"))
        self.assertIn("Answer: <short answer>", adapter.prompt_for({"question": "Short?"}, "other"))
        self.assertNotIn("Worked demonstrations", adapter.prompt_for(data["gsm8k"]["items"][0], "gsm8k"))

    def test_training_examples_cannot_be_sourced_from_test_items(self):
        invalid = [
            [{"question": " TEST   population question? ", "answer": "12"}],
            [{"id": "test", "question": "Other?", "answer": "12"}],
            [{"question": "Train?", "answer": "1"}] * 2,
            [{"question": "Train?"}], "not a list", ["not a row"],
        ]
        for demos in invalid:
            with self.subTest(demos=demos):
                data = self.data()
                data["gsm8k"]["demonstrations"] = demos
                with self.assertRaises(fast.ValidationError):
                    fast.make_plan(data)
        with self.assertRaises(fast.ValidationError):
            fast.make_plan({"arc": self.data()["gsm8k"]})

    def test_rationale_is_descriptor_supplied_and_order_changes_template_hash(self):
        data = self.data()
        data["gsm8k"]["demonstrations"][0].update(answer="$1,000.50", rationale="Training worked solution.")
        demos = adapter.demonstrations_for(data)
        self.assertEqual(demos["gsm8k"][0]["answer"], "Training worked solution.\n#### 1000.5")
        original = fast.digest(adapter.template_manifest(demos))
        demos["gsm8k"].reverse()
        self.assertNotEqual(fast.digest(adapter.template_manifest(demos)), original)

    def test_invalid_demo_reference_refused_before_server_contact(self):
        fixture = fixtures.AdapterTests()
        with fixture.tempdir() as directory, patch.object(adapter.OpenAIAdapter, "discover") as discover:
            data = self.data()
            data["gsm8k"]["demonstrations"][0]["answer"] = "12 or 13"
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(fast.ValidationError):
                adapter.run("http://127.0.0.1:1", MODEL_ID, [path], Path(directory) / "out")
            discover.assert_not_called()

    def test_template_hash_versions_and_demonstrations_are_recorded(self):
        fixture = fixtures.AdapterTests()
        with fixture.tempdir() as directory, Stub(replies=[completion("#### 12")]) as stub:
            data = self.data()
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(data))
            result = fixture.run_fixture(stub, [path], Path(directory) / "out")
            settings = result["runtime_settings"]
            self.assertEqual(settings["parse_version"], adapter.PARSE_VERSION)
            self.assertEqual(settings["prompt_template_id"], adapter.PROMPT_ID)
            self.assertEqual(settings["prompt_template_version"], adapter.TEMPLATE_VERSION)
            self.assertEqual(settings["prompt_template_sha256"], fast.digest(settings["prompt_template"]))
            self.assertIn("screening", settings["evaluation_scope"])
            demos = settings["prompt_template"]["demonstrations"]
            self.assertEqual(len(demos["gsm8k"]), 5)
            self.assertEqual(stub.posts[0]["messages"][1]["content"], adapter.prompt_for(data["gsm8k"]["items"][0], "gsm8k", demos["gsm8k"]))
            changed = copy.deepcopy(settings)
            changed["prompt_template"]["demonstrations"]["gsm8k"][0]["question"] += "Changed"
            with self.assertRaises(fast.ValidationError):
                fast.identity(result["model"], result["hardware"], changed)
            path.write_text(json.dumps({"gsm8k": {"items": data["gsm8k"]["items"]}}))
            posts = len(stub.posts)
            with self.assertRaises(fast.ValidationError):
                fixture.run_fixture(stub, [path], Path(directory) / "out")
            self.assertEqual(len(stub.posts), posts)

    def test_different_parse_template_ids_versions_and_hashes_cannot_compare(self):
        versioned = dict(SETTINGS, parse_version=adapter.PARSE_VERSION,
                         prompt_template_version=adapter.TEMPLATE_VERSION,
                         prompt_template_sha256="b" * 64)
        baseline = run(datasets(1), runtime_settings=versioned)
        for key in ("scorer_id", "parse_version", "prompt_template_id", "prompt_template_version", "prompt_template_sha256"):
            settings = dict(versioned, **{key: "c" * 64 if key.endswith("sha256") else "different-v1"})
            with self.subTest(key=key):
                candidate = run(datasets(1), runtime_settings=settings)
                with self.assertRaisesRegex(fast.ValidationError, "settings mismatch"):
                    fast.pair_verdict(candidate, baseline)
                self.assertEqual(len(fast.rank([baseline, candidate])), 2)
        legacy = run(datasets(1))
        with self.assertRaisesRegex(fast.ValidationError, "settings mismatch"):
            fast.pair_verdict(legacy, baseline)
        # Historical paired evidence must still validate within its own cohort.
        fast.validate_record(run(datasets(1), baseline=legacy))


if __name__ == "__main__":
    unittest.main()
