"""Pin incomplete-journal flags to the literal values returned before extraction."""

import base64
import json
import unittest
from pathlib import Path
from unittest import mock

import telemetry

_COMPLETE_VALUES = {
    "none": None,
    "integer-zero": 0,
    "float-zero": 0.0,
    "empty-text": "",
    "empty-bytes": b"",
    "empty-list": [],
    "false": False,
}


class _JournalBytes(bytes):
    complete = None


def _run_case(module, case):
    raw = _JournalBytes(base64.b64decode(case["journal_base64"], validate=True))
    raw.complete = _COMPLETE_VALUES[case["complete_value"]]
    with (
        mock.patch.object(module, "_read_windows_journal", return_value=(raw, False)) as reader,
        mock.patch.object(module, "_pid_alive", return_value=case["pid_alive"]),
    ):
        actual = module._windows_jobs(case["now"])
    reader.assert_called_once_with()
    return actual


class WindowsJobsCompleteTests(unittest.TestCase):
    def test_audit_reproduction_assigns_literal_false(self):
        raw = _JournalBytes(b"not json\n")
        with mock.patch.object(telemetry, "_read_windows_journal", return_value=(raw, False)):
            summary, source = telemetry._windows_jobs(10000.0)
        self.assertIs(summary["complete"], False)
        self.assertIs(summary["liveAvailable"], False)
        self.assertIs(source["complete"], False)
        self.assertEqual(summary["journal"], "invalid")

    def test_valid_records_preserve_the_original_falsy_value(self):
        # The old fold overwrites this value only when a record is invalid.
        journals = [
            b" \n",
            b'{"v":1,"id":"mac-valid-000000000001","event":"enqueued","unix":9995}\n',
            b'{"v":1,"id":"mac-valid-000000000001","event":"result","unix":9995,"status":"success"}\n',
        ]
        for name, complete in _COMPLETE_VALUES.items():
            for journal in journals:
                with self.subTest(complete=name, journal=journal):
                    raw = _JournalBytes(journal)
                    raw.complete = complete
                    with mock.patch.object(telemetry, "_read_windows_journal", return_value=(raw, False)):
                        summary, source = telemetry._windows_jobs(10000.0)
                    self.assertIs(summary["complete"], complete)
                    self.assertIs(summary["liveAvailable"], complete)
                    self.assertIs(source["complete"], complete)


def _golden_test(case):
    def test(self):
        actual = _run_case(telemetry, case)
        self.assertEqual(actual, tuple(case["expected"]))
        summary, source = actual
        # Equality alone would accept 0 in place of False.
        self.assertIs(summary["complete"], False)
        self.assertIs(summary["liveAvailable"], False)
        self.assertIs(source["complete"], False)

    return test


_CASES_PATH = Path(__file__).parent / "test_support" / "windows_jobs_golden" / "cases.json"
_CASES = json.loads(_CASES_PATH.read_text(encoding="utf-8"))["cases"]
for _case in _CASES:
    if "complete_value" not in _case:
        continue
    _name = "test_" + _case["name"]
    if hasattr(WindowsJobsCompleteTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(WindowsJobsCompleteTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
