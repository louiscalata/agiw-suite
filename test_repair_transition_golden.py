"""Complete transition observations recorded from the unmodified implementation."""

import test_support.posix_only  # noqa: F401  (skips this module on Windows)

# isort: split
import copy
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import online_code_repair as repair


class _Probe:
    """Expose comparison order and reject accidental hashing of actions/events."""

    __hash__ = None

    def __init__(self, spec, trace, observe):
        self.spec, self.trace, self.observe = spec, trace, observe

    def __eq__(self, other):
        self.trace.append(["equal", other, self.observe()])
        if other == self.spec.get("equal_error"):
            raise ValueError("fixture equality failed")
        return other in self.spec.get("matches", [])

    def startswith(self, prefix):
        self.trace.append(["prefix", prefix, self.observe()])
        if self.spec.get("prefix_error"):
            raise ValueError("fixture prefix failed")
        return self.spec.get("prefix", False)

    def __deepcopy__(self, memo):
        return self


def _freeze(value):
    if isinstance(value, _Probe):
        return value.spec
    if isinstance(value, bytes):
        return {"$kind": "bytes", "value": value.decode("utf-8")}
    if isinstance(value, dict):
        return {key: _freeze(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_freeze(item) for item in value]
    return value


def _decode(value, trace, observe):
    if isinstance(value, dict):
        if value.get("$kind") == "probe":
            return _Probe(value, trace, observe)
        if value.get("$kind") == "bytes":
            return value["value"].encode("utf-8")
        return {key: _decode(item, trace, observe) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item, trace, observe) for item in value]
    return value


class _Worker:
    def __init__(self, record, start):
        self.record, self._start = record, start

    def start(self):
        self._start(self)


def observe_case(module, case):
    """Run only transitions, with inert workers and no owner, model or socket I/O."""
    controller = module.OnlineCodeRepair.__new__(module.OnlineCodeRepair)
    trace, workers = [], []
    previous_worker = object()
    controller._worker = previous_worker
    controller._operation_id = case.get("counter", 7)
    # Mirrors the "initialize" transition: no inference probe has been dispatched yet.
    controller._inference_started = case.get("inference_started", False)

    def observe():
        return {"state": _freeze(controller._state), "counter": controller._operation_id}

    controller._state = _decode(
        copy.deepcopy(
            case.get(
                "initial",
                {"status": "idle", "message": "fixture before", "steps": [], "operationId": 7, "action": None},
            )
        ),
        trace,
        observe,
    )
    initial_state = controller._state
    initial_steps = controller._state.get("steps") if isinstance(controller._state, dict) else None
    stopped = case.get("stopped", False)

    def is_set():
        trace.append(["cancelled", stopped, observe()])
        return stopped

    controller._owner_children = SimpleNamespace(cancelled=SimpleNamespace(is_set=is_set))

    def clock():
        trace.append(["clock", observe()])
        if case.get("clock_error"):
            raise RuntimeError("fixture clock failed")
        return case.get("clock", 1000.0)

    controller._clock = clock
    original_diagnosis = module.read_only_diagnosis

    def diagnosis(action, snapshot, *, now):
        trace.append(["diagnosis", _freeze(action), _freeze(snapshot), now, observe()])
        if case.get("diagnosis_error"):
            raise RuntimeError("fixture diagnosis failed")
        return original_diagnosis(action, snapshot, now=now)

    def start(worker):
        trace.append(["start", controller._worker is worker, observe()])
        if case.get("thread_error") == "start":
            raise RuntimeError("fixture worker start failed")
        worker.record["started"] = True

    def thread(*, target, args, name, daemon):
        trace.append(
            ["thread", list(_freeze(args)), name, daemon, target.__name__, target.__self__ is controller, observe()]
        )
        if case.get("thread_error") == "construct":
            raise RuntimeError("fixture worker construction failed")
        worker = _Worker({"args": _freeze(args), "name": name, "daemon": daemon, "started": False}, start)
        workers.append(worker)
        return worker

    observations = []
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(module.time, "time", return_value=1000.0))
        stack.enter_context(mock.patch.object(module.threading, "Thread", side_effect=thread))
        stack.enter_context(mock.patch.object(module, "read_only_diagnosis", side_effect=diagnosis))
        for name, value in case.get("patches", {}).items():
            stack.enter_context(mock.patch.object(module, name, value))
        for call in case["calls"]:
            if "set_stopped" in call:
                stopped = call["set_stopped"]
                continue
            arguments = _decode(copy.deepcopy(call), trace, observe)
            event = arguments.pop("event")
            error = None
            result = None
            try:
                result = controller._transition(event, **arguments)
            except (AttributeError, KeyError, TypeError, ValueError, RuntimeError) as caught:
                error = {"type": type(caught).__name__, "message": str(caught)}
            state = controller._state
            worker = controller._worker
            observations.append(
                {
                    "return": _freeze(result),
                    "error": error,
                    "state_json": json.dumps(_freeze(state), ensure_ascii=True, separators=(",", ":")),
                    "counter": controller._operation_id,
                    "worker": "previous"
                    if worker is previous_worker
                    else None
                    if worker is None
                    else workers.index(worker),
                    "same_state": state is initial_state,
                    "same_steps": isinstance(state, dict) and state.get("steps") is initial_steps,
                    "action_is_argument": isinstance(state, dict)
                    and "action" in arguments
                    and state.get("action") is arguments["action"],
                    "step_is_last": isinstance(state, dict)
                    and isinstance(state.get("steps"), list)
                    and bool(state["steps"])
                    and "step" in arguments
                    and state["steps"][-1] is arguments["step"],
                }
            )
    return {"observations": observations, "trace": trace, "workers": [worker.record for worker in workers]}


class RepairTransitionGoldenTests(unittest.TestCase):
    """Preserve payload bytes, partial writes, aliases and ordered side effects."""


def _golden_test(case):
    def test(self):
        self.assertEqual(observe_case(repair, case), case["expected"])

    return test


_CASES_PATH = Path(__file__).parent / "test_support" / "repair_transition_golden" / "cases.json"
_CASES = json.loads(_CASES_PATH.read_text(encoding="utf-8"))["cases"]
for _case in _CASES:
    _name = "test_" + _case["name"]
    if hasattr(RepairTransitionGoldenTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(RepairTransitionGoldenTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
