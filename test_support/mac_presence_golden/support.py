"""Deterministic snapshot replay and ordered observations for presence goldens."""

import copy
import math
from contextlib import ExitStack
from unittest import mock

from test_support.exception_outcome import pinned_message

_HELPERS = ("public_id", "_iso", "_mac_gpus", "_number", "mac_components")
_ERRORS = {"ValueError": ValueError, "TypeError": TypeError, "OSError": OSError, "RuntimeError": RuntimeError}


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"$float"}:
            return float(value["$float"])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _encode(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": str(value)}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    return value


def run_case(module, case):
    """Call the supplied implementation with fresh inputs and fail closed on I/O."""
    snapshot = _decode(copy.deepcopy(case["snapshot"]))
    original = _encode(copy.deepcopy(snapshot))
    events = []
    counts = dict.fromkeys(_HELPERS, 0)
    fault = case.get("fault", {})
    originals = {name: getattr(module, name) for name in _HELPERS}
    injected = None
    if fault.get("helper"):
        injected = _ERRORS[fault["error"]]("fixture helper failure")

    def observe(name):
        def call(*args, **kwargs):
            events.append([name, _encode(copy.deepcopy(args)), _encode(copy.deepcopy(kwargs))])
            counts[name] += 1
            if fault.get("helper") == name and counts[name] == fault.get("at", 1):
                raise injected
            return originals[name](*args, **kwargs)

        return call

    with ExitStack() as stack:
        for target in (
            "builtins.open",
            "pathlib.Path.open",
            "os.open",
            "socket.socket",
            "socket.create_connection",
            "subprocess.Popen",
            "subprocess.run",
            "time.time",
            "time.monotonic",
            "time.sleep",
        ):
            stack.enter_context(mock.patch(target, side_effect=AssertionError("Unexpected external operation")))
        for name in _HELPERS:
            stack.enter_context(mock.patch.object(module, name, side_effect=observe(name)))
        try:
            result = module.mac_presence(
                snapshot,
                copy.deepcopy(case["host"]),
                version=copy.deepcopy(case.get("version")),
                dashboard_port=copy.deepcopy(case.get("dashboard_port")),
            )
            outcome = {"result": _encode(result)}
        except (
            AttributeError,
            IndexError,
            KeyError,
            OSError,
            OverflowError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            outcome = {"error": type(error).__name__}
            message = pinned_message(error, module, injected=injected)
            if message is not None:
                outcome["message"] = message
    outcome.update(events=events, inputsUnchanged=_encode(snapshot) == original)
    return outcome
