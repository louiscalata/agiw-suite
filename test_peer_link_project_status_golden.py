"""Full projection outputs and call order captured from the unmodified peer link."""

import copy
import json
import math
import re
import unittest
from pathlib import Path
from unittest import mock

import peer_link


def encode(value):
    """Keep non-JSON values and container kinds observable in the stored corpus."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"$golden": "float", "value": repr(value)}
    if isinstance(value, bytes):
        return {"$golden": "bytes", "hex": value.hex()}
    if isinstance(value, slice):
        return {"$golden": "slice", "items": [encode(value.start), encode(value.stop), encode(value.step)]}
    if isinstance(value, (tuple, set, frozenset)):
        items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
        return {"$golden": type(value).__name__, "items": [encode(item) for item in items]}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("$golden")
    if kind == "float":
        return float(value["value"])
    if kind == "bytes":
        return bytes.fromhex(value["hex"])
    if kind == "slice":
        return slice(*(decode(item) for item in value["items"]))
    if kind in ("tuple", "set", "frozenset"):
        constructors = {"tuple": tuple, "set": set, "frozenset": frozenset}
        return constructors[kind](decode(item) for item in value["items"])
    return {key: decode(item) for key, item in value.items()}


def observe(module, case):
    """Exercise only the public caller, with its original globals patched by name."""
    doc = decode(copy.deepcopy(case["doc"]))
    trace = []
    original_scalar = module._scalar
    original_hands_off = module.hands_off_flag

    def scalar(value, kind, limit=128):
        trace.append(["_scalar", encode(value), kind.__name__, limit])
        answer = original_scalar(value, kind, limit)
        if case.get("scalar_mode") == "reject":
            return None
        if case.get("scalar_mode") == "normalize" and answer is not None:
            return "normalized" if kind is str else 0
        return answer

    def hands_off(value):
        trace.append(["hands_off_flag", encode(value)])
        return False if case.get("hands_mode") == "false" else original_hands_off(value)

    patches = {"_scalar": scalar, "hands_off_flag": hands_off}
    if "max_models" in case:
        patches["MAX_MODELS"] = case["max_models"]
    if "component_words" in case:
        patches["_COMPONENT_WORDS"] = decode(case["component_words"])
    if "state_pattern" in case:
        patches["_STATE_WORD"] = re.compile(case["state_pattern"])
    with mock.patch.multiple(module, **patches):
        try:
            outcome = {"returned": encode(module.project_status(doc))}
        except (AttributeError, IndexError, KeyError, OverflowError, TypeError, ValueError) as exc:
            outcome = {"raised": {"type": type(exc).__name__, "args": encode(exc.args)}}
    return {"outcome": outcome, "trace": trace, "input_after": encode(doc)}


class ProjectStatusGoldenTests(unittest.TestCase):
    """One named test per recorded input, including malformed-input exceptions."""


def _golden_test(case):
    def test(self):
        actual = observe(peer_link, case)
        # JSON text comparison also checks key insertion order and int versus float.
        expected = [case["expected"], *case.get("expected_variants", [])]
        self.assertIn(
            json.dumps(actual, ensure_ascii=True, allow_nan=False),
            [json.dumps(item, ensure_ascii=True, allow_nan=False) for item in expected],
        )

    return test


def load_tests(loader, tests, pattern):
    path = Path(__file__).parent / "test_support" / "peer_link_project_status_golden" / "cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    names = [case["name"] for case in cases]
    if not cases or len(names) != len(set(names)):
        raise ValueError("Golden cases must be nonempty and uniquely named")
    for case in cases:
        setattr(ProjectStatusGoldenTests, "test_" + case["name"], _golden_test(case))
    return loader.loadTestsFromTestCase(ProjectStatusGoldenTests)


if __name__ == "__main__":
    unittest.main()
