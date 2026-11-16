"""Full outputs and I/O order recorded from the unmodified router projections."""

import copy
import json
import os
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import telemetry


def _run_case(module, case):
    """Drive only fixture reads, retaining module-global patch targets and call order."""
    router = copy.deepcopy(case["router"])
    original_router = copy.deepcopy(router)
    events = []
    pending = iter(case.get("pending", [{"error": "absent"}]))
    pending_path = Path("/golden/pending.json")
    readiness_path = Path("/golden/readiness.json")
    errors = {
        "absent": FileNotFoundError,
        "invalid": ValueError,
        "unreadable": OSError,
        "changed": module._RecordChanged,
    }

    def read_value(value):
        if "error" in value:
            raise errors[value["error"]]("fixture " + value["error"])
        return copy.deepcopy(value["value"])

    def safe_file(path, limit):
        events.append(["read", str(path), limit])
        if path == readiness_path:
            return read_value(case.get("readiness", {"error": "absent"}))
        if path == pending_path:
            return read_value(next(pending))
        raise AssertionError(f"Unexpected fixture path: {path}")

    launcher = mock.Mock()
    launcher_options = case.get("launcher", {})

    def launcher_stat():
        events.append(["launcher-stat"])
        if "error" in launcher_options:
            raise errors[launcher_options["error"]]("fixture launcher")
        return SimpleNamespace(
            st_mode=launcher_options.get("mode", stat.S_IFDIR | 0o700),
            st_uid=os.getuid() + launcher_options.get("owner_delta", 0),
        )

    launcher.lstat.side_effect = launcher_stat
    launcher.is_symlink.return_value = launcher_options.get("symlink", False)

    def initialized():
        events.append(["namespace"])
        return case.get("initialized", True)

    def observe(now):
        events.append(["observe", now])
        return router

    def sleep(seconds):
        events.append(["sleep", seconds])

    with (
        mock.patch.object(module, "_safe_file", side_effect=safe_file),
        mock.patch.object(module, "_PENDING_PATH", pending_path),
        mock.patch.object(module, "_READINESS_PATH", readiness_path),
        mock.patch.object(module, "_LAUNCHER_ROOT", launcher),
        mock.patch.object(module, "_router_namespace_initialized", side_effect=initialized),
        mock.patch.object(module, "_router_observation", side_effect=observe),
        mock.patch.object(module.time, "sleep", side_effect=sleep),
    ):
        try:
            if case["target"] == "pipeline":
                result = module._pipeline(case["now"], None if case.get("observe", False) else router)
            else:
                result = module._online_code_mode_state(case["now"], case["observed_at"], router)
            outcome = {"result": result}
        except (AttributeError, KeyError, OSError, StopIteration, TypeError, ValueError) as error:
            outcome = {"error": type(error).__name__, "message": str(error)}
    outcome.update(events=events, inputsUnchanged=router == original_router)
    # JSON also normalizes the pipeline's tuple without weakening any stored field.
    return json.loads(json.dumps(outcome, ensure_ascii=True))


class PipelineGoldenTests(unittest.TestCase):
    """Pin reports, caught/escaping errors, and side-effect order before extraction."""


def _golden_test(case):
    def test(self):
        self.assertEqual(_run_case(telemetry, case), case["expected"])

    return test


_CASES_PATH = Path(__file__).parent / "test_support" / "telemetry_pipeline_golden" / "cases.json"
_CASES = json.loads(_CASES_PATH.read_text(encoding="utf-8"))["cases"]
for _case in _CASES:
    _name = "test_" + _case["target"] + "_" + _case["name"]
    if hasattr(PipelineGoldenTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(PipelineGoldenTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
