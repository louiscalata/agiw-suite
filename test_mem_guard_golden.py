"""Full-output main/classify cases recorded from unmodified origin/main."""

import json
import re
import sys
import unittest
from pathlib import Path

import mem_guard
from test_support.mem_guard_golden.support import classify_result, main_result


class MemGuardGoldenTests(unittest.TestCase):
    """Pin complete results, streams, exit codes and ordered side effects."""


def _normalize_usage_wrap(value):
    if isinstance(value, str):
        value = re.sub(r"\}\n +\.\.\.\n", "} ...\n", value)
        return re.sub(
            r"\(choose from ([A-Za-z0-9_-]+(?:, [A-Za-z0-9_-]+)*)\)",
            lambda match: "(choose from " + ", ".join(f"'{choice}'" for choice in match[1].split(", ")) + ")",
            value,
        )
    if isinstance(value, dict):
        return {key: _normalize_usage_wrap(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_usage_wrap(item) for item in value]
    return value


def _golden_test(case):
    def test(self):
        actual = classify_result(mem_guard, case) if case["kind"] == "classify" else main_result(mem_guard, case)
        expected = case.get("expected_py39", case["expected"]) if sys.version_info[:2] == (3, 9) else case["expected"]
        if (3, 10) <= sys.version_info[:2] < (3, 13):
            actual = _normalize_usage_wrap(actual)
        self.assertEqual(actual, expected)

    return test


_CASES_PATH = Path(__file__).parent / "test_support" / "mem_guard_golden" / "cases.json"
_CASES = json.loads(_CASES_PATH.read_text(encoding="utf-8"))["cases"]
for _case in _CASES:
    _name = "test_" + _case["kind"] + "_" + _case["name"]
    if hasattr(MemGuardGoldenTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(MemGuardGoldenTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
