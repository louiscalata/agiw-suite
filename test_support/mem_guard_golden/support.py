"""Safe, shared input adapters for recorded mem-guard behaviour tests."""

import argparse
import contextlib
import copy
import io
import math
from pathlib import Path
from unittest import mock


def _input_value(value):
    if isinstance(value, dict):
        if set(value) == {"nonfinite"}:
            return {"nan": math.nan, "inf": math.inf, "negative_inf": -math.inf}[value["nonfinite"]]
        return {key: _input_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_input_value(item) for item in value]
    return value


def classify_result(module, case):
    """Preserve identity when constructing the real two-sample swap evidence."""
    state = _input_value(copy.deepcopy(case["state"]))
    cfg = _input_value(copy.deepcopy(case.get("cfg")))
    evidence_input = _input_value(case.get("evidence"))
    evidence = None
    if evidence_input is not None:
        second = state if evidence_input.get("same_identity", True) else copy.deepcopy(state)
        evidence = module.SwapEvidence(
            copy.deepcopy(evidence_input["first"]),
            second,
            evidence_input.get("windowSeconds", 5.1),
            evidence_input.get("ageSeconds", 0),
        )
    try:
        return list(module.classify(state, cfg, swap_evidence=evidence))
    except ArithmeticError as error:
        # An out-of-float-range reading raises OverflowError; the exception type is the recorded result.
        return {"exception": type(error).__name__}


class _TraceStream(io.StringIO):
    def __init__(self, events, name):
        super().__init__()
        self.events = events
        self.name = name

    def write(self, text):
        self.events.append([self.name, text])
        return super().write(text)


def main_result(module, case):
    """Call main with its existing injected Context and module-global patch targets."""
    events = []
    stdout = _TraceStream(events, "stdout")
    stderr = _TraceStream(events, "stderr")
    registry = mock.Mock(spec=module.Registry)

    def registry_call(name, default):
        def call(*args):
            events.append([name, *args])
            error = case.get(name + "_error")
            if error:
                kind = module.RegistryError if error["kind"] == "registry" else OSError
                raise kind(error["text"])
            return copy.deepcopy(case.get(name + "_result", default))

        return call

    entry = {"pid": 4321, "startTime": "fixed-start", "comm": "/fixture/job", "label": "fixture job"}
    registry.register.side_effect = registry_call("register", entry)
    registry.unregister.side_effect = registry_call("unregister", True)
    registry.resume.side_effect = registry_call("resume", [])
    registry.resume_overdue.side_effect = registry_call("resume_overdue", [])

    def journal_write(record):
        events.append(["journal", record])

    def dispatch(name):
        def call(_ctx, *args):
            events.append([name, *args])
            if _ctx is not ctx:
                # main must hand every dispatched command the one Context it built or was given.
                events.append([name, "ctx-other"])
            return case.get("dispatch_exit", 23)

        return call

    ctx = module.Context(
        state_root=Path("/fixture/state"),
        cfg=module.default_config(),
        registry=registry,
        probes=mock.Mock(),
        notifier=mock.Mock(),
        clock=lambda: 100.0,
        sleep=lambda _seconds: None,
        gpu=lambda: None,
        stdin=io.BytesIO(b""),
        stdout=stdout,
        stderr=stderr,
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(contextlib.redirect_stdout(stdout))
        stack.enter_context(contextlib.redirect_stderr(stderr))
        stack.enter_context(mock.patch.dict(module.os.environ, {"COLUMNS": "80", "TERM": "dumb", "NO_COLOR": "1"}))
        stack.enter_context(mock.patch.object(module.Journal, "write", side_effect=journal_write))
        for name in ("_cmd_status", "_cmd_admit", "_cmd_watch", "_cmd_hook"):
            stack.enter_context(mock.patch.object(module, name, side_effect=dispatch(name)))
        argv = case["argv"]
        if case.get("argv_none"):
            stack.enter_context(mock.patch.object(module.sys, "argv", ["mem-guard", *argv]))
            argv = None
        if case.get("default_context"):
            stack.enter_context(mock.patch.object(module, "Context", return_value=ctx))
        if case.get("fallback"):
            parser = mock.Mock()
            parser.parse_args.return_value = argparse.Namespace(command="future-command")
            stack.enter_context(mock.patch.object(module, "_parser", return_value=parser))
        try:
            result = module.main(argv, None if case.get("default_context") else ctx)
            system_exit = False
        except SystemExit as error:
            result = error.code
            system_exit = True
    return {
        "exit_code": result,
        "system_exit": system_exit,
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
        "events": events,
    }
