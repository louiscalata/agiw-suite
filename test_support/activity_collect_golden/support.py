"""Deterministic projected-record adapters for the activity collector goldens."""

import contextlib
import copy
import math
from pathlib import Path
from unittest import mock

from test_support.exception_outcome import pinned_message


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"$float"}:
            return {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}[value["$float"]]
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _snapshot(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, list) and hasattr(value, "read_failed"):
        return {
            "paths": [_snapshot(item) for item in value],
            "scan_cut": value.scan_cut,
            "cut_mtime": _snapshot(value.cut_mtime),
            "read_failed": value.read_failed,
        }
    if isinstance(value, dict):
        return {key: _snapshot(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot(item) for item in value]
    return value


def _listing(module, inputs, kind, count):
    options = inputs.get(kind + "_listing", {})
    paths = [Path("fixture") / kind / f"record-{index}.json" for index in range(count)]
    if options.get("plain"):
        return paths
    result = module._Listing(paths)
    for field in ("scan_cut", "cut_mtime", "read_failed"):
        if field in options:
            setattr(result, field, options[field])
    return result


def run_case(module, case):
    """Run the unchanged fold with no filesystem, service or device reads.

    Enumeration, receipt/archive projection and active observation are the I/O
    seams. History selection, evidence validation and bounds remain real. The
    ordered call snapshots and post-call inputs also pin mutations and merges.
    """
    inputs = _decode(
        copy.deepcopy({key: value for key, value in case.items() if key not in {"expected", "expected_before_py314"}})
    )
    events = []
    receipts = inputs.get("receipts", [])
    archives = inputs.get("archives", [])
    active = inputs.get("active", [])

    receipt_paths = _listing(module, inputs, "receipts", inputs.get("receipt_path_count", len(receipts)))
    archive_paths = _listing(module, inputs, "archives", len(archives))
    errors = {
        "OSError": OSError,
        "ValueError": ValueError,
        "TypeError": TypeError,
        "RuntimeError": RuntimeError,
        "RecursionError": RecursionError,
        "KeyError": KeyError,
        "SystemExit": SystemExit,
    }

    def read_archive(path):
        index = archive_paths.index(path)
        value = archives[index]
        if isinstance(value, dict) and "$read_error" in value:
            raise errors[value["$read_error"]]("fixture archive read")
        return value

    def project_archive(value, _sampled):
        if isinstance(value, dict) and "$project_error" in value:
            raise errors[value["$project_error"]]("fixture archive projection")
        return value

    replacements = {
        "_receipt_files": lambda: receipt_paths,
        "_archive_files": lambda: archive_paths,
        "_receipt_runs": lambda _now, _paths: receipts,
        "_read_archive": read_archive,
        "_project_archive": project_archive,
        "_active_rows": lambda _now: (active, inputs.get("problem", False), inputs.get("listing", "ok")),
    }

    injected = []

    def traced(name, implementation):
        def call(*args, **kwargs):
            events.append([name, _snapshot(args), _snapshot(kwargs)])
            fault = inputs.get("errors", {}).get(name)
            if fault is not None:
                error = errors[fault]("fixture " + name)
                injected.append(error)
                raise error
            return implementation(*args, **kwargs)

        return call

    with contextlib.ExitStack() as stack:
        for name, implementation in replacements.items():
            stack.enter_context(mock.patch.object(module, name, side_effect=traced(name, implementation)))
        for name in ("_history_source", "_bounded_history", "_history_bound", "current_run_evidence"):
            implementation = getattr(module, name)
            stack.enter_context(mock.patch.object(module, name, side_effect=traced(name, implementation)))
        stack.enter_context(mock.patch.object(module.time, "time", side_effect=traced("time", lambda: inputs["clock"])))
        # An accidental new I/O call must fail locally, including in a mutant.
        for target in (
            "builtins.open",
            "io.open",
            "pathlib.Path.open",
            "os.open",
            "os.scandir",
            "socket.socket",
            "subprocess.Popen",
            "os.listdir",
            "os.stat",
            "os.lstat",
        ):
            stack.enter_context(mock.patch(target, side_effect=AssertionError("unexpected external I/O")))
        try:
            if "now" in inputs:
                result = module.collect_activity(inputs["now"], current_evidence=inputs.get("current_evidence"))
            else:
                result = module.collect_activity(current_evidence=inputs.get("current_evidence"))
            outcome = {"result": result}
        except (
            ArithmeticError,
            AttributeError,
            IndexError,
            KeyError,
            OSError,
            RuntimeError,
            SystemExit,
            TypeError,
            ValueError,
        ) as error:
            outcome = {"exception": type(error).__name__}
            message = pinned_message(error, module, injected=injected)
            if message is not None:
                outcome["message"] = message
    outcome.update(
        events=events,
        inputsAfter={
            "receipts": receipts,
            "archives": archives,
            "active": active,
            "current_evidence": inputs.get("current_evidence"),
            "receipt_paths": receipt_paths,
            "archive_paths": archive_paths,
        },
    )
    return _snapshot(outcome)
