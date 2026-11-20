"""Generation collector outputs recorded from the same unmodified source."""

import test_support.posix_only  # noqa: F401  (skips this module on Windows)

# isort: split
import unittest

import nisi_v02
from test_support.collect_nisi_v02_golden.support import load_cases, run_case
from test_support.exception_outcome import comparable


class CollectNisiV02GenerationGoldenTests(unittest.TestCase):
    """Preserve manifest pinning, host ordering and activation boundaries."""


def _golden_test(case):
    def test(self):
        actual = run_case(nisi_v02, case)
        self.assertEqual(comparable(case["expected"], actual), actual)

    return test


for _case in load_cases("generation"):
    _name = "test_" + _case["name"]
    if hasattr(CollectNisiV02GenerationGoldenTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(CollectNisiV02GenerationGoldenTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
