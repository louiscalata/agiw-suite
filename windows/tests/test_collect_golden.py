"""Replay full snapshots and ordered boundary calls recorded from unchanged source."""

import copy
import datetime as dt
import importlib
import json
import math
import socket
import subprocess
import sys
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
collect = importlib.import_module("agiw_win.collect")
CASES_PATH = Path(__file__).resolve().parent / "fixtures/windows_collect_golden/cases.json"


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"float"}:
            return {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}[value["float"]]
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _json_value(value):
    """Preserve exceptional numbers and make paths/sets portable across test hosts."""
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dt.tzinfo):
        return value.tzname(None)
    if isinstance(value, float) and not math.isfinite(value):
        return {"float": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_json_value(item) for item in value)
    return value


def canonical(value):
    """JSON equality also distinguishes booleans, integers and decimal readings."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False)


def _raise_fixture_error(data, name):
    errors = {
        "OSError": OSError,
        "ValueError": ValueError,
        "TypeError": TypeError,
        "RuntimeError": RuntimeError,
        "KeyError": KeyError,
        "AttributeError": AttributeError,
        "OverflowError": OverflowError,
        "SystemExit": SystemExit,
    }
    error = data["errors"].get(name)
    if error:
        raise errors[error]("fixture " + name)


def _callback(data, events, name, result=None, implementation=None):

    def call(*args, **kwargs):
        events.append([name, _json_value(args), _json_value(kwargs)])
        _raise_fixture_error(data, name)
        if name in data.get("returns", {}):
            return copy.deepcopy(data["returns"][name])
        return implementation(*args, **kwargs) if implementation is not None else copy.deepcopy(result)

    return call


def _patch_pure(stack, module, data, events):
    for name in (
        "_source",
        "unconfigured_lane",
        "ambiguous_lane_ids",
        "mark_foreign_lanes",
        "lane_model_row",
        "lanes_health",
        "lane_health",
        "lane_server_pids",
        "hands_off",
        "pair_component",
    ):
        original = getattr(module.probes, name)
        stack.enter_context(
            mock.patch.object(module.probes, name, side_effect=_callback(data, events, name, implementation=original))
        )
    for name in ("mac_from_presence", "merge_mac_lan_observation"):
        original = getattr(module, name)
        stack.enter_context(
            mock.patch.object(module, name, side_effect=_callback(data, events, name, implementation=original))
        )


def _patch_boundaries(stack, module, data, events, collector):
    def timestamp(value, timezone):
        # Avoid platform libc timestamp limits; the assembler still formats a real datetime.
        return dt.datetime(1970, 1, 1, tzinfo=timezone) + dt.timedelta(seconds=value)

    stack.enter_context(
        mock.patch.object(
            module,
            "dt",
            SimpleNamespace(
                datetime=SimpleNamespace(fromtimestamp=_callback(data, events, "timestamp", implementation=timestamp)),
                timezone=dt.timezone,
            ),
        )
    )
    values = {
        "lane_specs": (data["specs"], data["lane_source"]),
        "route_queue": (data["pipeline"], data["pipeline_source"]),
        "share_health": (data["share"], data["share_source"]),
        "configured_mac_identity": (data["mac_name"], data["config_reason"]),
        "read_peers": (data["peers"], data["peers_source"]),
        "nisi_component": data["nisi"],
        "read_peer_link": (data["paired"], data["peer_link_source"]),
        "machine_rows": data["machines"],
    }
    for name, value in values.items():
        stack.enter_context(mock.patch.object(module.probes, name, side_effect=_callback(data, events, name, value)))
    methods = {
        "_gpus": ("gpus", (data["gpus"], data["gpu_source"])),
        "_jobs": ("jobs", (data["jobs"], data["jobs_source"])),
        "_memory": ("memory", (data["memory"], data["memory_source"])),
        "_pair": ("pair", data["pair"]),
        "_adapter_list": ("adapters", data["adapters"]),
    }
    for method, (name, value) in methods.items():
        setattr(collector, method, _callback(data, events, name, value))


def _fixture_collector(module, data, events):
    """Skip constructor caches, worker threads and native API initialization entirely."""
    collector = module.Collector.__new__(module.Collector)
    collector.share_root = Path("fixture/share")
    collector.bin_root = Path("fixture/bin")
    collector.host = "fixture-pc"
    collector.clock = _callback(data, events, "clock", data["now"])
    collector.lane_interval = data["interval"]
    collector.build = data["build"]
    collector.mac_lan_observation = data["mac_lan_observation"]
    collector.peer_link_reader = data["peer_link_reader"]
    collector._lock = threading.Lock()
    collector._lane_ownership = {"retained": "unknown"}
    collector._lane_owners = {"retained": {"pid": 999}}
    collector._pollers = {}
    for key in data["pollers"]:

        def record_polling(allowed, key=key):
            events.append(["set_polling", _json_value(key), allowed])
            _raise_fixture_error(data, "set_polling")

        collector._pollers[tuple(key)] = SimpleNamespace(set_polling=record_polling)

    def sample(ports):
        return data["servers"], data["callers"], data["process_source"]

    collector.lane_processes = (
        SimpleNamespace(sample=_callback(data, events, "processes", implementation=sample))
        if data["processes_enabled"]
        else None
    )

    def lane_result(spec, _sampled):
        return copy.deepcopy(data["lanes"][spec["id"]])

    collector._lane = _callback(data, events, "lane", implementation=lane_result)
    collector.notification_fn = _callback(data, events, "notification", data["notification"])
    collector.clients_fn = (
        _callback(data, events, "clients", (data["clients"], data["client_sources"]))
        if data["clients_enabled"]
        else None
    )
    return collector


def _forbid_io(stack, module):
    """Fail immediately if fixture replay reaches a real host boundary on any OS."""
    for target, name in (
        (socket, "socket"),
        (socket, "create_connection"),
        (subprocess, "Popen"),
        (threading.Thread, "start"),
        (Path, "open"),
        (module.probes, "http_get_json"),
        (module.probes, "read_json"),
        (module.probes, "_memory_status"),
        (module.probes, "display_adapters"),
        (module.probes, "nvidia_gpus"),
        (module.winsys, "LaneProcesses"),
        (module.winsys, "PairCensus"),
        (module.winsys, "notification_state"),
    ):
        stack.enter_context(mock.patch.object(target, name, side_effect=AssertionError("unexpected host I/O")))
    stack.enter_context(mock.patch.object(module.winsys, "IS_WINDOWS", False))
    stack.enter_context(
        mock.patch.object(
            module.winsys, "GpuCounters", side_effect=AssertionError("unexpected native counters"), create=True
        )
    )


def run_case(module, case):
    """Execute the real assembler with copied fixture observations and ordered spies."""
    data = _decode(copy.deepcopy(case["input"]))
    events = []
    collector = _fixture_collector(module, data, events)
    with ExitStack() as stack:
        _forbid_io(stack, module)
        _patch_pure(stack, module, data, events)
        _patch_boundaries(stack, module, data, events, collector)
        try:
            outcome = {"result": collector.collect()}
        except (
            ArithmeticError,
            AssertionError,
            AttributeError,
            KeyError,
            OSError,
            RuntimeError,
            SystemExit,
            TypeError,
            ValueError,
        ) as error:
            outcome = {"error": type(error).__name__, "message": str(error)}
    outcome.update(
        events=events,
        state={
            "ownership": collector._lane_ownership,
            "owners": collector._lane_owners,
            "pollerKeys": list(collector._pollers),
            "processSource": data["process_source"],
            "servers": data["servers"],
            "inputLanes": data["lanes"],
        },
    )
    return _json_value(outcome)


class CollectGoldenTests(unittest.TestCase):
    """The complete recorded snapshot, calls and retained state form the contract."""


def golden_test(case):
    def test(self):
        self.assertEqual(canonical(run_case(collect, case)), canonical(case["expected"]))

    return test


CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
for _case in CASES:
    if "result" in _case["expected"]:
        _name = "test_" + _case["name"]
        if hasattr(CollectGoldenTests, _name):
            raise ValueError(f"Duplicate golden case: {_name}")
        setattr(CollectGoldenTests, _name, golden_test(_case))


if __name__ == "__main__":
    unittest.main()
