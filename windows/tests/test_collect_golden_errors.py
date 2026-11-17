"""Replay escaping failures and check that fixture execution remains isolated."""

import copy
import unittest

import test_collect_golden as golden


class CollectErrorGoldenTests(unittest.TestCase):
    """An escaping failure includes its type, message, preceding calls and state."""


class CollectFixtureIsolationTests(unittest.TestCase):
    def test_corpus_has_unique_names_and_both_outcome_kinds(self):
        self.assertTrue(golden.CASES)
        names = [case["name"] for case in golden.CASES]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(any("error" in case["expected"] for case in golden.CASES))
        self.assertTrue(any("result" in case["expected"] for case in golden.CASES))

    def test_replay_does_not_mutate_inputs_or_leave_patches(self):
        original = copy.deepcopy(golden.CASES)
        source_function = golden.collect.probes._source
        for case in golden.CASES:
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    golden.canonical(golden.run_case(golden.collect, case)), golden.canonical(case["expected"])
                )
        self.assertEqual(golden.CASES, original)
        self.assertIs(golden.collect.probes._source, source_function)

    def test_reversed_replay_is_independent_of_case_order(self):
        for case in reversed(golden.CASES):
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    golden.canonical(golden.run_case(golden.collect, case)), golden.canonical(case["expected"])
                )


for _case in golden.CASES:
    if "error" in _case["expected"]:
        _name = "test_" + _case["name"]
        if hasattr(CollectErrorGoldenTests, _name):
            raise ValueError(f"Duplicate golden case: {_name}")
        setattr(CollectErrorGoldenTests, _name, golden.golden_test(_case))


if __name__ == "__main__":
    unittest.main()
