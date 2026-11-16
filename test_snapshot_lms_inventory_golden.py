"""Full snapshots and call order recorded from the unmodified inventory collector."""

import copy
import json
import os
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import telemetry


def _error(kind, module):
    errors = {
        "timeout": TimeoutError("fixture timeout"),
        "refused": ConnectionRefusedError("fixture refused"),
        "non_json": json.JSONDecodeError("fixture invalid", "x", 0),
        "unicode": UnicodeError("fixture encoding"),
        "oversize": ValueError("status response too large"),
        "value": ValueError("fixture schema"),
        "os": OSError("fixture read"),
        "schema": TypeError("fixture schema type"),
        "incomplete": module._IncompleteInventoryError("fixture inventory incomplete"),
        "runtime": RuntimeError("fixture runtime"),
    }
    return errors[kind]


def _inventory_patches(stack, module, case, calls):
    def patch(name, value=None, error=None, delegate=None):
        def call(*args, **kwargs):
            calls.append(copy.deepcopy([name, args, kwargs]))
            if error:
                raise _error(error, module)
            return delegate(*args, **kwargs) if delegate else copy.deepcopy(value)

        stack.enter_context(mock.patch.object(module, name, side_effect=call))

    patch("_lms_server_status", case.get("status", {"port": 1234, "running": True}), case.get("status_error"))
    patch("_native_api_endpoint", error=case.get("endpoint_error"), delegate=module._native_api_endpoint)
    listeners = case.get("listeners", [[41, "lmstudio", "/fixture/LM Studio"]])
    listener_index = 0

    def listener(port):
        nonlocal listener_index
        value = listeners[min(listener_index, len(listeners) - 1)]
        listener_index += 1
        return tuple(value) if isinstance(value, list) else value

    patch("_inventory_listener", error=case.get("listener_error"), delegate=listener)
    if "route" in case:
        patch("_lmstudio_route", case["route"], case.get("route_error"))
    else:
        patch("_lmstudio_route", error=case.get("route_error"), delegate=module._lmstudio_route)
    patch("_bounded_http", case.get("payload", {"models": []}), case.get("http_error"))
    if "api_rows" in case:
        patch("_parse_api", case["api_rows"], case.get("parse_error"))
    else:
        patch("_parse_api", error=case.get("parse_error"), delegate=module._parse_api)
    patch("_bounded_lms", case.get("cli_payload", {"models": []}), case.get("cli_error"))
    patch("_afm_status", {"state": "missing"})
    patch("observe_router", {})
    patch("_pipeline", [{"state": "unknown"}, {"id": "router", "state": "unavailable"}])
    patch("_online_code_mode", {"state": "unknown"})
    patch("_mark_live_route")
    patch("_windows_worker", [{"state": "unavailable"}, {"id": "windows-worker", "state": "unavailable"}])
    patch("_windows_headless", {"state": "unknown"})
    patch("_windows_jobs", [{"journal": "absent"}, {"id": "windows-jobs", "state": "unavailable"}])
    patch("_canary", [{"state": "unavailable"}, {"id": "canary", "state": "unavailable"}])
    patch("collect_nisi_v02", [{"runtimeIntegrity": "UNKNOWN"}, {"id": "nisi-v02-runtime", "state": "unavailable"}])

    def components(pipeline, nisi, models, sampled, **kwargs):
        return [{"modelIds": list(models), "sampled": sampled, **kwargs}]

    stack.enter_context(mock.patch.object(module, "_route_components", side_effect=components))


def evaluate_case(case, module=telemetry):
    """Call the real snapshot entry point with all system I/O replaced by fixtures."""
    calls = []
    guards = []
    with ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ))
        os.environ.pop(module._API_PORT_ENV, None)
        if "env_port" in case:
            os.environ[module._API_PORT_ENV] = case["env_port"]
        stack.enter_context(mock.patch.object(module.time, "time", return_value=case.get("sampled", 10000.0)))
        _inventory_patches(stack, module, case, calls)
        stack.enter_context(mock.patch.object(module.peer_feed, "peer_feeds", return_value=[]))
        for target in ("_safe_file", "_bounded_command"):
            guards.append(
                stack.enter_context(
                    mock.patch.object(module, target, side_effect=AssertionError("unexpected system I/O"))
                )
            )
        guards.append(
            stack.enter_context(
                mock.patch.object(module.socket, "socket", side_effect=AssertionError("unexpected socket"))
            )
        )
        guards.append(
            stack.enter_context(
                mock.patch.object(module.subprocess, "Popen", side_effect=AssertionError("unexpected process"))
            )
        )
        try:
            outcome = {"snapshot": module.collect_snapshot()}
        except (AttributeError, IndexError, KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
            outcome = {"exception": {"type": type(error).__name__, "message": str(error)}}
        for guard in guards:
            guard.assert_not_called()
    return json.loads(json.dumps({**outcome, "calls": calls}, allow_nan=False))


class SnapshotLmsInventoryGoldenTests(unittest.TestCase):
    """Protect the collector through the unchanged public snapshot entry point."""


def _golden_test(case):
    def test(self):
        self.assertEqual(evaluate_case(case), case["expected"])

    return test


_CASES_PATH = Path(__file__).parent / "test_support" / "snapshot_lms_inventory_golden" / "cases.json"
for _case in json.loads(_CASES_PATH.read_text(encoding="utf-8"))["cases"]:
    _name = "test_" + _case["name"]
    if hasattr(SnapshotLmsInventoryGoldenTests, _name):
        raise ValueError(f"Duplicate golden case: {_name}")
    setattr(SnapshotLmsInventoryGoldenTests, _name, _golden_test(_case))


if __name__ == "__main__":
    unittest.main()
