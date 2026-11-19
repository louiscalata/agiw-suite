"""Historical collector fold outcomes recorded for projected-row fixtures."""

import json
import sys
import unittest
from pathlib import Path

import activity
from test_support.activity_collect_golden.support import run_case
from test_support.exception_outcome import comparable


class ActivityCollectGoldenTests(unittest.TestCase):
    """Pin history merging, bounds, errors, ordering and global calls."""


def _golden_test(case):
    def test(self):
        expected = (
            case.get("expected_before_py314", case["expected"]) if sys.version_info < (3, 14) else case["expected"]
        )
        actual = run_case(activity, case)
        self.assertEqual(comparable(expected, actual), actual)

    return test


_CASES_PATH = Path(__file__).parent / "test_support" / "activity_collect_golden" / "cases.json"
_CASES = json.loads(_CASES_PATH.read_text(encoding="utf-8"))["cases"]
for _case in _CASES:
    if _case["group"] != "history":
        continue
    _name = "test_" + _case["name"]
    if hasattr(ActivityCollectGoldenTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(ActivityCollectGoldenTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
