"""Complete results and ordered helper calls captured from unmodified source."""

import json
import unittest
from pathlib import Path

import peer_feed
from test_support.exception_outcome import comparable
from test_support.mac_presence_golden.support import run_case


class MacPresenceGoldenTests(unittest.TestCase):
    """Pin refusals, grouping, activity, completeness, limits and envelope fields."""


def _golden_test(case):
    def test(self):
        actual = run_case(peer_feed, case)
        self.assertEqual(comparable(case["expected"], actual), actual)

    return test


_CASES_PATH = Path(__file__).parent / "test_support" / "mac_presence_golden" / "cases.json"
_CASES = json.loads(_CASES_PATH.read_text(encoding="utf-8"))["cases"]
for _case in _CASES:
    _name = "test_" + _case["name"]
    if hasattr(MacPresenceGoldenTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(MacPresenceGoldenTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
