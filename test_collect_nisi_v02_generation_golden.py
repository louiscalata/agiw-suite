"""Generation collector outputs recorded from the same unmodified source."""

import unittest

import nisi_v02
from test_support.collect_nisi_v02_golden.support import load_cases, run_case


class CollectNisiV02GenerationGoldenTests(unittest.TestCase):
    """Preserve manifest pinning, host ordering and activation boundaries."""


def _golden_test(case):
    def test(self):
        expected = case["expected"]
        self.assertEqual(run_case(nisi_v02, case), expected)

    return test


for _case in load_cases("generation"):
    _name = "test_" + _case["name"]
    if hasattr(CollectNisiV02GenerationGoldenTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(CollectNisiV02GenerationGoldenTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
