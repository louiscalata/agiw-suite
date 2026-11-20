"""Recorded helper order and ownership checks for the fleet projection."""

import test_support.posix_only  # noqa: F401  (skips this module on Windows)

# isort: split
import unittest

import fleet_state
from test_fleet_state_golden import _canonical, _load_cases, _run_case
from test_support.exception_outcome import comparable


class FleetStateGoldenProtocolTests(unittest.TestCase):
    """Freeze call order, caller inputs, and mutable output ownership."""


def _protocol_test(case):
    def test(self):
        actual = _run_case(fleet_state, case)
        for key in ("events", "inputsAfter", "sharedMutablePaths"):
            with self.subTest(field=key):
                self.assertEqual(_canonical(comparable(case["expected"][key], actual[key])), _canonical(actual[key]))

    return test


for _case in _load_cases():
    _name = "test_" + _case["name"]
    if hasattr(FleetStateGoldenProtocolTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(FleetStateGoldenProtocolTests, _name, _protocol_test(_case))


if __name__ == "__main__":
    unittest.main()
