"""Full runner outputs captured from the original source before a later split.

The corpus pins refusals as well as successful records. Only the public callback,
clock and named checkpoint seams are controlled; statistics and validation run
unchanged. These are synthetic offline checks, not runtime measurements.
"""

import builtins
import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eci_fast_check as f

CORPUS = Path(__file__).parent / "test_support" / "eci_fast_check_run_check_golden" / "cases.json"
CAPTURED_ERRORS = (
    ArithmeticError,
    AssertionError,
    AttributeError,
    LookupError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


class EvidenceIterationError(dict):
    """A dictionary whose measurements are readable but key iteration fails."""

    def __iter__(self):
        raise OSError("synthetic evidence iteration failure")


class EvidenceMembershipError(dict):
    """A dictionary whose optional-field membership checks fail."""

    def __contains__(self, key):
        raise OSError("synthetic evidence membership failure")


def decode(value):
    """Restore hostile Python values that ordinary JSON cannot express."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$iter_error_dict"}:
            return EvidenceIterationError(decode(value["$iter_error_dict"]))
        if set(value) == {"$contains_error_dict"}:
            return EvidenceMembershipError(decode(value["$contains_error_dict"]))
        if set(value) == {"$float"}:
            return float(value["$float"])
        if set(value) == {"$tuple"}:
            return tuple(decode(item) for item in value["$tuple"])
        return {key: decode(item) for key, item in value.items()}
    return value


def encode(value):
    """Keep non-finite values and tuple types visible in a JSON-safe receipt."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": str(value)}
    if isinstance(value, tuple):
        return {"$tuple": [encode(item) for item in value]}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    return value


def raise_action(action, module):
    kind = action["type"]
    error_class = module.ValidationError if kind == "ValidationError" else getattr(builtins, kind)
    error = error_class(action.get("message", "synthetic callback failure"))
    for key, value in action.get("attrs", {}).items():
        setattr(error, key, decode(value))
    raise error


def callback_result(action, module):
    if "raise" in action:
        raise_action(action["raise"], module)
    return decode(copy.deepcopy(action["return"]))


class ScriptedClock:
    def __init__(self, ticks, events):
        self.ticks = iter(ticks)
        self.last = 0
        self.events = events

    def __call__(self):
        self.last = next(self.ticks, self.last)
        self.events.append(["clock", self.last])
        return self.last


def answer_callback(config, module, events, inputs):
    actions = iter(config.get("answers", []))
    fallback = config.get("default_answer", {"raise": {"type": "AssertionError", "message": "unexpected answer call"}})

    def answer(row, *, benchmark, spec):
        events.append(["answer", copy.deepcopy(row), benchmark, copy.deepcopy(spec)])
        action = next(actions, fallback)
        if action.get("mutate_row_spec"):
            row["answer"] = "changed"
            spec["item_ids"].clear()
            events.append(["callback_mutated_copies", copy.deepcopy(row), copy.deepcopy(spec)])
        if "mutate_plan_spec" in action:
            inputs["plan"]["benchmarks"][0].update(copy.deepcopy(action["mutate_plan_spec"]))
        for key, value in action.get("mutate_inputs", {}).items():
            inputs[key].update(decode(copy.deepcopy(value)))
        return callback_result(action, module)

    def legacy(row):
        return answer(row, benchmark="legacy", spec={"item_ids": []})

    def incompatible(row, required_argument):
        return answer(row, benchmark=required_argument, spec={"item_ids": []})

    return {"context": answer, "legacy": legacy, "incompatible": incompatible}[config.get("signature", "context")]


def score_callback(config, module, events):
    values = iter(config["score_values"])

    def score(prediction, row, *, benchmark, spec):
        events.append(["score", copy.deepcopy(prediction), copy.deepcopy(row), benchmark, copy.deepcopy(spec)])
        value = next(values, config["score_values"][-1])
        if isinstance(value, dict) and "raise" in value:
            raise_action(value["raise"], module)
        return decode(value)

    return score


def checkpoint_patches(module, config, events, streams):
    original_open = module.checkpoint_open
    original_append = module.checkpoint_append

    def open_checkpoint(path, binding, plan):
        events.append(["checkpoint_open", copy.deepcopy(binding), copy.deepcopy(plan)])
        if "open_error" in config:
            raise_action(config["open_error"], module)
        stream, prior = original_open(path, binding, plan)
        streams.append(stream)
        events.append(
            [
                "checkpoint_prior",
                copy.deepcopy(prior),
                [[list(key), value] for key, value in stream._eci_supersedes.items()],
            ]
        )
        return stream, prior

    def append_checkpoint(stream, binding, benchmark, observation):
        events.append(["checkpoint_append", copy.deepcopy(binding), benchmark, copy.deepcopy(observation)])
        if "append_error" in config:
            raise_action(config["append_error"], module)
        return original_append(stream, binding, benchmark, observation)

    return (
        mock.patch.object(module, "checkpoint_open", side_effect=open_checkpoint),
        mock.patch.object(module, "checkpoint_append", side_effect=append_checkpoint),
    )


def capture_run(case, config, module, path):
    inputs = decode(copy.deepcopy(case["input"]))
    inputs.update(decode(copy.deepcopy(config.get("overrides", {}))))
    events, streams = [], []
    answer = answer_callback(config, module, events, inputs)
    inputs["clock"] = ScriptedClock(config.get("clock", [0]), events)
    if "score_values" in config:
        inputs["score"] = score_callback(config, module, events)
    if config.get("checkpoint"):
        inputs["checkpoint"] = path
    open_patch, append_patch = checkpoint_patches(module, config, events, streams)
    fixed_date = mock.Mock()
    fixed_date.now.return_value.isoformat.return_value = "2026-11-12T12:00:00+00:00"
    with open_patch, append_patch, mock.patch.object(module, "datetime", fixed_date):
        try:
            result = {"return": module.run_check(answer=answer, **inputs)}
        except CAPTURED_ERRORS as error:
            result = {"exception": {"type": type(error).__name__, "message": str(error)}}
    result["events"] = events
    result["streams_closed"] = [stream.closed for stream in streams]
    result["checkpoint_lines"] = path.read_text(encoding="utf-8").splitlines() if path.exists() else None
    result["datasets_after"] = inputs["datasets"]
    return result


def replay_case(case, module=f):
    """Run a saved input without using any saved expected values."""
    with tempfile.TemporaryDirectory(prefix="eci-run-check-golden-") as folder:
        path = Path(folder) / "checkpoint.jsonl"
        if "initial_checkpoint" in case:
            path.write_text(case["initial_checkpoint"], encoding="utf-8")
        return encode([capture_run(case, run, module, path) for run in case["runs"]])


class RunCheckGoldenTests(unittest.TestCase):
    """One discoverable test per immutable corpus input."""


def expected_for_python(case, runtime=None):
    """Treat expected_by_python keys as minimum Python versions."""
    runtime = sys.version_info[:2] if runtime is None else runtime
    alternatives = [
        (tuple(map(int, minimum.split("."))), expected)
        for minimum, expected in case.get("expected_by_python", {}).items()
    ]
    for minimum, expected in sorted(alternatives, key=lambda item: item[0], reverse=True):
        if runtime >= minimum:
            return expected
    return case["expected"]


def _golden_test(case):
    def test(self):
        self.assertEqual(replay_case(case), expected_for_python(case))

    return test


def load_tests(loader, tests, pattern):
    cases = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
    suite = unittest.TestSuite()
    names = set()
    for case in cases:
        name = "test_" + case["name"]
        if name in names:
            raise ValueError(f"Duplicate golden case: {name}")
        names.add(name)
        setattr(RunCheckGoldenTests, name, _golden_test(case))
        suite.addTest(RunCheckGoldenTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
