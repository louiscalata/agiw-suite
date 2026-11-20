"""Keep the projection's text bounds independent of downstream label patterns."""

import test_support.posix_only  # noqa: F401  (skips this module on Windows)

# isort: split
import re
import unittest
from unittest import mock

import telemetry
from test_telemetry_pipeline_golden import _CASES, _run_case


class PipelineTextBoundsTests(unittest.TestCase):
    """A permissive label pattern must not bypass the projection's size checks."""


def _golden_test(case):
    def test(self):
        patterns = case["text_bound_patterns"]
        with (
            mock.patch.object(telemetry, "_RUN_ID", re.compile(patterns["run_id"])),
            mock.patch.object(telemetry, "_STAGE", re.compile(patterns["stage"])),
        ):
            self.assertEqual(_run_case(telemetry, case), case["expected"])

    return test


for _case in _CASES:
    if "text_bound_patterns" not in _case:
        continue
    _name = "test_" + _case["name"]
    if hasattr(PipelineTextBoundsTests, _name):
        raise ValueError(f"Duplicate text-bound case: {_name}")
    setattr(PipelineTextBoundsTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
