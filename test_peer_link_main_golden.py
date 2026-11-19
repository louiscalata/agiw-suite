"""CLI bytes, exits and dependency calls recorded before the in-place extraction."""

import contextlib
import copy
import getpass
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import peer_link
from test_support.exception_outcome import comparable, pinned_exception


def _plain(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _record(trace, name, result):
    def call(*args, **kwargs):
        trace.append([name, _plain(args), _plain(kwargs)])
        return copy.deepcopy(result)

    return call


def _link_type(trace, case):
    class Link:
        def __init__(self, state_dir, host, name, provider, **kwargs):
            trace.append(["PeerLink", _plain([state_dir, host, name, provider]), _plain(kwargs)])
            self.name = name
            self.node = "node-fixture"
            self.status_port = kwargs["status_port"]
            self.beacon_port = kwargs["beacon_port"]

        @property
        def on_change(self):
            return None

        @on_change.setter
        def on_change(self, callback):
            trace.append(["on_change", callback])

        def start(self):
            trace.append(["start"])

        def trusted(self):
            trace.append(["trusted"])
            return ["paired"] * case.get("trusted", 2)

        def stop(self):
            trace.append(["stop"])

    return Link


def _control_reply(path, case):
    if "control_status" in case:
        return case["control_status"], copy.deepcopy(case.get("control_doc", {}))
    docs = {
        "/control/peers": {
            "peers": case.get(
                "peers",
                [
                    {"state": "paired", "name": "Desktop", "host": "mac", "node": "node-a", "inviteWaiting": False},
                    {"state": "available", "name": "", "host": None, "node": "node-b", "inviteWaiting": True},
                    {"state": "seen", "name": "機械 café", "host": "windows", "node": "node-c", "inviteWaiting": False},
                ],
            )
        },
        "/control/invite": {"pin": "001234", "say": "read this PIN aloud"},
        "/control/accept": {"peer": "accepted-fixture"},
        "/control/unpair": {"unpaired": "forgotten-fixture"},
    }
    return 200, copy.deepcopy(docs[path])


def _input_pin(trace, case, injected):
    def read(prompt):
        trace.append(["getpass", prompt])
        if case.get("pin_interrupt"):
            error = KeyboardInterrupt()
            injected.append(error)
            raise error
        if sys.stdin is None:
            error = EOFError("stdin absent")
            injected.append(error)
            raise error
        line = sys.stdin.readline()
        if not line:
            error = EOFError("stdin empty")
            injected.append(error)
            raise error
        return line.rstrip("\n")

    return read


def observe(module, case, *, argv0="peer_link", stdout_factory=None):
    """Call main with the same module-global seams used by existing CLI tests."""
    trace = []
    injected = []
    stdout = io.StringIO() if stdout_factory is None else stdout_factory(trace)
    stderr = io.StringIO()

    def control(state_dir, port, method, path, body=None):
        trace.append(["control_request", _plain([state_dir, port, method, path, body])])
        if case.get("control_error"):
            error = OSError("control unavailable")
            injected.append(error)
            raise error
        return _control_reply(path, case)

    def sleep(seconds):
        trace.append(["sleep", seconds])
        error = KeyboardInterrupt()
        injected.append(error)
        raise error

    patches = {
        "default_state_dir": _record(trace, "default_state_dir", Path("state-default")),
        "detect_host_kind": _record(trace, "detect_host_kind", case.get("default_host", "mac")),
        "file_status": _record(trace, "file_status", "file-provider"),
        "lmstudio_status": _record(trace, "lmstudio_status", "lmstudio-provider"),
        "linux_status": _record(trace, "linux_status", "linux-provider"),
        "directed_broadcasts": _record(trace, "directed_broadcasts", ["192.0.2.255"]),
        "write_snapshot": _record(trace, "write_snapshot", "snapshot-callback"),
        "PeerLink": _link_type(trace, case),
        "control_request": control,
    }
    stdin = None if case.get("stdin_absent") else io.StringIO(case.get("stdin", "  001234  \n"))
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.multiple(module, **patches))
        stack.enter_context(mock.patch.dict(os.environ, {"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"}))
        stack.enter_context(
            mock.patch.object(module.socket, "gethostname", _record(trace, "gethostname", "machine.test"))
        )
        stack.enter_context(mock.patch.object(module.time, "sleep", sleep))
        stack.enter_context(mock.patch.object(getpass, "getpass", _input_pin(trace, case, injected)))
        stack.enter_context(mock.patch.multiple(sys, stdout=stdout, stderr=stderr, stdin=stdin))
        stack.enter_context(mock.patch.object(sys, "argv", [argv0, *case["argv"]]))
        try:
            result = module.main(None if case.get("argv_none") else case["argv"])
            outcome, exit_code = {"returned": result}, result
        except SystemExit as exc:
            outcome = {"raised": "SystemExit", "code": exc.code}
            exit_code = exc.code if isinstance(exc.code, int) else 0 if exc.code is None else 1
            if isinstance(exc.code, str):
                stderr.write(exc.code + "\n")
        except (EOFError, KeyboardInterrupt, OSError, KeyError, TypeError, ValueError) as exc:
            outcome = {"raised": type(exc).__name__}
            if pinned_exception(exc, module, injected=injected):
                outcome["args"] = _plain(exc.args)
            exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    return {
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
        "exit_code": exit_code,
        "outcome": outcome,
        "trace": trace,
    }


class MainGoldenTests(unittest.TestCase):
    """Each case compares complete streams as UTF-8 bytes, exits and call order."""


def _golden_test(case):
    def test(self):
        actual = observe(peer_link, case)
        expected = [case["expected"], *case.get("expected_variants", [])]
        for stream in ("stdout", "stderr"):
            self.assertIn(actual[stream].encode("utf-8"), [item[stream].encode("utf-8") for item in expected])
        self.assertIn(actual, [comparable(item, actual) for item in expected])

    return test


def load_tests(loader, tests, pattern):
    path = Path(__file__).parent / "test_support" / "peer_link_main_golden" / "cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    names = [case["name"] for case in cases]
    if not cases or len(names) != len(set(names)):
        raise ValueError("Golden cases must be nonempty and uniquely named")
    for case in cases:
        setattr(MainGoldenTests, "test_" + case["name"], _golden_test(case))
    return loader.loadTestsFromTestCase(MainGoldenTests)


if __name__ == "__main__":
    unittest.main()
