"""Golden harnesses pin only exception messages the module under test raised."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_support.exception_outcome import (
    comparable,
    explicit_raise,
    pinned_exception,
    pinned_message,
)

SOURCE = """
import json


def explicit():
    raise ValueError("module diagnostic")


def builtin_type_error():
    return len(5)


def standard_library_error():
    return json.loads("{")
"""


class GoldenExceptionOutcomeTests(unittest.TestCase):
    """Pin module raises and leave interpreter wording out of comparisons."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="golden-exception-")
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "golden_module.py"
        path.write_text(SOURCE, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("golden_exception_module", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def _raised(self, operation):
        try:
            operation()
        except (TypeError, ValueError) as error:
            return error
        self.fail("Operation did not raise its expected exception")

    def test_explicit_raise_keeps_its_message(self):
        error = self._raised(self.module.explicit)
        self.assertTrue(explicit_raise(error, self.module))
        self.assertEqual(pinned_message(error, self.module), "module diagnostic")

    def test_builtin_operation_keeps_only_its_type(self):
        error = self._raised(self.module.builtin_type_error)
        self.assertIsInstance(error, TypeError)
        self.assertFalse(explicit_raise(error, self.module))
        self.assertIsNone(pinned_message(error, self.module))

    def test_standard_library_raise_keeps_only_its_type(self):
        error = self._raised(self.module.standard_library_error)
        self.assertIsInstance(error, ValueError)
        self.assertFalse(explicit_raise(error, self.module))
        self.assertIsNone(pinned_message(error, self.module))

    def test_raise_in_another_module_is_not_pinned(self):
        try:
            raise ValueError("test module diagnostic")
        except ValueError as error:
            self.assertFalse(explicit_raise(error, self.module))
            self.assertIsNone(pinned_message(error, self.module))
        else:
            self.fail("Test module did not raise its expected exception")

    def test_comparable_drops_unpinned_fields_when_types_match(self):
        expected = {"error": "TypeError", "message": "interpreter wording", "args": ["detail"]}
        actual = {"error": "TypeError"}
        self.assertEqual(comparable(expected, actual), {"error": "TypeError"})
        self.assertEqual(expected["message"], "interpreter wording")

    def test_comparable_keeps_fields_when_types_differ(self):
        expected = {"error": "ValueError", "message": "module diagnostic"}
        actual = {"error": "TypeError"}
        self.assertEqual(comparable(expected, actual), expected)

    def test_comparable_keeps_pinned_fields_and_other_keys(self):
        expected = {"exception": "OSError", "message": "pinned", "events": [["read"]]}
        actual = {"exception": "OSError", "message": "pinned", "events": [["read"]]}
        self.assertEqual(comparable(expected, actual), expected)

    def test_comparable_descends_into_nested_outcomes(self):
        expected = {"outcome": {"raised": "EOFError", "args": ["stdin absent"]}, "trace": [["getpass"]]}
        actual = {"outcome": {"raised": "EOFError"}, "trace": [["getpass"]]}
        self.assertEqual(comparable(expected, actual), {"outcome": {"raised": "EOFError"}, "trace": [["getpass"]]})

    def test_nested_type_change_keeps_the_message(self):
        expected = {"outcome": {"error": "ValueError", "message": "module diagnostic"}}
        actual = {"outcome": {"error": "TypeError"}}
        self.assertEqual(comparable(expected, actual), expected)

    def _raised_injected(self, fault):
        with mock.patch.object(self.module, "explicit", side_effect=fault):
            return self._raised(self.module.explicit)

    def test_injected_helper_fault_keeps_its_message(self):
        fault = TypeError("fixture helper failure")
        error = self._raised_injected(fault)
        self.assertIs(error, fault)
        self.assertFalse(explicit_raise(error, self.module))
        self.assertIsNone(pinned_message(error, self.module))
        self.assertTrue(pinned_exception(error, self.module, injected=fault))
        self.assertEqual(pinned_message(error, self.module, injected=fault), "fixture helper failure")

    def test_only_the_exact_injected_object_is_pinned(self):
        fault = TypeError("fixture helper failure")
        other = TypeError("fixture helper failure")
        self.assertIsNone(pinned_message(other, self.module, injected=fault))
        self.assertEqual(
            pinned_message(other, self.module, injected="fixture helper failure"),
            "fixture helper failure",
        )

    def test_injected_fault_comparison_keeps_the_message(self):
        fault = TypeError("fixture helper failure")
        error = self._raised_injected(fault)
        actual = {"error": type(error).__name__}
        message = pinned_message(error, self.module, injected=fault)
        if message is not None:
            actual["message"] = message
        expected = {"error": "TypeError", "message": "fixture helper failure"}
        self.assertEqual(comparable(expected, actual), expected)
        self.assertEqual(actual["message"], "fixture helper failure")
        changed = {"error": "TypeError", "message": "different helper failure"}
        self.assertEqual(comparable(changed, actual), changed)
        self.assertNotEqual(comparable(changed, actual), actual)


if __name__ == "__main__":
    unittest.main()
