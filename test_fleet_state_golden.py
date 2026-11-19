"""Full fleet projections recorded from an unmodified source revision."""

import copy
import json
import math
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fleet_state
from test_support.exception_outcome import comparable, explicit_raise, pinned_message

CASES_PATH = Path(__file__).parent / "test_support" / "fleet_state_golden" / "cases.json"
HELPERS = ("_number", "_iso", "_report_metadata", "_fact", "_machine_state", "_coverage")

# These pins identify the capture; later implementations may change independently.
CAPTURE_REVISION = "5694d16e8d77e8a0dddd5cf597ac1a91f2db1784"
CAPTURE_HASHES = {
    "fleet_state.py": "af208a566b53812eec46139d014f081e4e2fb8521385a24fe81a73031132f5d5",
    "machine_state.py": "58062f8da85f1a851d137da00615a53a10e15a5150edbcf3ac5d058abcdcfdf3",
}
_MODULE = sys.modules[__name__]


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"$float"}:
            return {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}[value["$float"]]
        if set(value) == {"$tuple"}:
            return tuple(_decode(item) for item in value["$tuple"])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _encode(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
    if isinstance(value, tuple):
        return {"$tuple": [_encode(item) for item in value]}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


def _mutable_paths(value, path=""):
    paths = {}
    if isinstance(value, (dict, list)):
        paths[id(value)] = path
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, item in items:
            paths.update(_mutable_paths(item, path + "/" + str(key)))
    elif isinstance(value, tuple):
        for index, item in enumerate(value):
            paths.update(_mutable_paths(item, path + "/" + str(index)))
    return paths


def _run_case(module, case):
    """Keep helpers real while recording their arguments at call time."""
    inputs = _decode(copy.deepcopy({key: case.get(key) for key in ("snapshot", "inference", "now")}))
    events = []

    def recording(name, original):
        def call(*args, **kwargs):
            events.append({"call": name, "args": _encode(args), "kwargs": _encode(kwargs)})
            return original(*args, **kwargs)

        return call

    def clock():
        events.append({"call": "time.time", "args": {"$tuple": []}, "kwargs": {}})
        return _decode(case.get("clock", 1000.0))

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(module, "time", SimpleNamespace(time=clock)))
        for name in HELPERS:
            stack.enter_context(mock.patch.object(module, name, side_effect=recording(name, getattr(module, name))))
        try:
            result = module.build_fleet_state(**inputs)
            outcome = {"result": _encode(result)}
            input_paths = _mutable_paths(inputs)
            output_paths = _mutable_paths(result)
            shared = sorted(
                [input_paths[identity], output_paths[identity]] for identity in input_paths.keys() & output_paths.keys()
            )
        except (ArithmeticError, AttributeError, KeyError, OSError, TypeError, ValueError) as error:
            outcome = {"error": type(error).__name__}
            message = pinned_message(error, module)
            if message is not None:
                outcome["message"] = message
            shared = []
    return {"outcome": outcome, "events": events, "inputsAfter": _encode(inputs), "sharedMutablePaths": shared}


def _canonical(value):
    """JSON comparison retains bool/int and int/float distinctions."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)


class FleetStateGoldenTests(unittest.TestCase):
    """Pin the complete projection or escaping exception for each input."""


class ExceptionOutcomeTests(unittest.TestCase):
    """Keep module messages observable across supported interpreters."""

    def test_explicit_errors_keep_their_messages(self):
        for error_type in (ValueError, TypeError, OverflowError):
            for message in ("module diagnostic", "changed module diagnostic"):
                with self.subTest(error=error_type.__name__, message=message):
                    try:
                        raise error_type(message)
                    except error_type as error:
                        self.assertTrue(explicit_raise(error, _MODULE))
                        self.assertEqual(pinned_message(error, _MODULE), message)

    def test_interpreter_errors_keep_only_their_types(self):
        operations = ((TypeError, lambda: hash([])), (ValueError, lambda: int("invalid")))
        for error_type, operation in operations:
            with self.subTest(error=error_type.__name__):
                try:
                    operation()
                except error_type as error:
                    self.assertFalse(explicit_raise(error, _MODULE))
                    self.assertIsNone(pinned_message(error, _MODULE))
                else:
                    self.fail("Operation did not raise its expected exception")

    def test_interpreter_overflow_keeps_only_its_type(self):
        try:
            float(10**1000)
        except OverflowError as error:
            self.assertIsNone(pinned_message(error, _MODULE))
        else:
            self.fail("Float conversion did not overflow")

    def test_reraised_errors_keep_the_original_message_policy(self):
        def explicit_error():
            raise ValueError("module diagnostic")

        def reraised(operation, named):
            try:
                operation()
            except (TypeError, ValueError) as error:
                if named:
                    raise error
                raise

        operations = ((lambda: hash([]), None), (explicit_error, "module diagnostic"))
        for operation, expected in operations:
            for named in (False, True):
                with self.subTest(expected=expected, named=named):
                    try:
                        reraised(operation, named)
                    except (TypeError, ValueError) as error:
                        self.assertEqual(pinned_message(error, _MODULE), expected)
                    else:
                        self.fail("Operation did not raise its expected exception")


def _golden_test(case):
    def test(self):
        actual = _run_case(fleet_state, case)["outcome"]
        self.assertEqual(_canonical(comparable(case["expected"]["outcome"], actual)), _canonical(actual))

    return test


def _load_cases():
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if payload["schemaVersion"] != 1:
        raise ValueError("Unsupported golden corpus schema")
    if payload["sourceRevision"] != CAPTURE_REVISION or payload["sourceSha256"] != CAPTURE_HASHES:
        raise ValueError("Golden corpus capture provenance changed")
    cases = payload["cases"]
    if not cases:
        raise ValueError("Golden corpus is empty")
    return cases


for _case in _load_cases():
    _name = "test_" + _case["name"]
    if hasattr(FleetStateGoldenTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(FleetStateGoldenTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
