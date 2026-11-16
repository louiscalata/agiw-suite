"""Full-output Windows journal cases recorded before the in-place extraction."""

import base64
import json
import unittest
from pathlib import Path
from unittest import mock

import telemetry


class WindowsJobsGoldenTests(unittest.TestCase):
    """Keep the original journal fold observable through its public patch targets."""


def _golden_test(case):
    def test(self):
        raw = base64.b64decode(case["journal_base64"], validate=True)
        reader_kind = case["reader_kind"]
        if reader_kind == "absent-tuple":
            journal = (None, False)
        elif reader_kind == "absent-none":
            journal = None
        elif reader_kind == "tail":
            journal = telemetry._JournalTail(raw)
        elif reader_kind == "truncated-tuple":
            journal = (raw, True)
        elif reader_kind == "complete-tuple":
            journal = (raw, False)
        elif reader_kind == "tail-in-complete-tuple":
            journal = (telemetry._JournalTail(raw), False)
        else:
            journal = raw
        errors = {
            "os-error": OSError("synthetic unreadable journal"),
            "value-error": ValueError("synthetic unsafe file"),
        }
        with (
            mock.patch.object(telemetry, "_read_windows_journal", return_value=journal) as reader,
            mock.patch.object(
                telemetry, "_pid_alive", side_effect=lambda pid, alive=case["pid_alive"]: pid is None or alive
            ),
        ):
            reader.side_effect = errors.get(reader_kind)
            actual = telemetry._windows_jobs(case["now"])
        reader.assert_called_once_with()
        self.assertEqual(actual, tuple(case["expected"]))

    return test


_CASES_PATH = Path(__file__).parent / "test_support" / "windows_jobs_golden" / "cases.json"
_CASES = json.loads(_CASES_PATH.read_text(encoding="utf-8"))["cases"]
for _case in _CASES:
    _name = "test_" + _case["name"]
    if hasattr(WindowsJobsGoldenTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(WindowsJobsGoldenTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
