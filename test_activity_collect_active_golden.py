"""Active collector fold outcomes recorded for projected-row fixtures."""

import json
import sys
import unittest
from pathlib import Path

import activity
from test_support.activity_collect_golden.support import run_case


class ActivityCollectActiveGoldenTests(unittest.TestCase):
    """Pin active priority, liveness, inventory reconciliation and failures."""


def _golden_test(case):
    def test(self):
        expected = (
            case.get("expected_before_py314", case["expected"]) if sys.version_info < (3, 14) else case["expected"]
        )
        self.assertEqual(run_case(activity, case), expected)

    return test


_CASES_PATH = Path(__file__).parent / "test_support" / "activity_collect_golden" / "cases.json"
_CASES = json.loads(_CASES_PATH.read_text(encoding="utf-8"))["cases"]
for _case in _CASES:
    if _case["group"] != "active":
        continue
    _name = "test_" + _case["name"]
    if hasattr(ActivityCollectActiveGoldenTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(ActivityCollectActiveGoldenTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
