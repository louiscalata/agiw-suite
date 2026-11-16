"""Recorded gate refusals and accepted records through the in-memory adapter.

The transport patch follows the existing already-loaded fixtures. No socket is
opened. Expectations were captured from the pinned original source; see the
corpus provenance for its revision and source hashes.
"""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eci_fast_check_adapter as adapter


class _Transport:
    def shutdown(self, how):
        pass

    def settimeout(self, timeout):
        pass


class _Lane:
    """Replay response bytes at the existing HTTPConnection patch target."""

    def __init__(self, case, module):
        self.case = case
        self.module = module
        self.requests = []

    def __enter__(self):
        lane = self

        class Connection:
            def __init__(self, host, port, timeout):
                self.sock = _Transport()

            def connect(self):
                pass

            def request(self, method, path, body=None, headers=None):
                lane.requests.append([method, path, None if body is None else json.loads(body)])
                self.status = lane.case.get("statuses", {}).get(path, 200)
                replies = {
                    "/props": lane.case["props"],
                    "/v1/models": lane.case["models"],
                    "/slots": lane.case["slots"],
                    "/v1/chat/completions": lane.case["completion"],
                }
                self.body = io.BytesIO(json.dumps(replies[path]).encode("utf-8"))

            def getresponse(self):
                return self

            def read1(self, size):
                return self.body.read(size)

            def close(self):
                pass

        self.patcher = mock.patch.object(self.module.http.client, "HTTPConnection", Connection)
        self.patcher.start()
        return self

    def __exit__(self, *args):
        self.patcher.stop()


def _run_frozen(module, original, args, kwargs):
    kwargs.update(clock=lambda: 0, created_at="2026-11-12T12:00:00+00:00")
    return original(*args, **kwargs)


def capture_case(case, module=adapter, directory=None):
    """Capture the complete result, refusal, requests and published file bytes."""
    original = module.fast.run_check
    facts = {"model_sha256": "a" * 64, "file_size_bytes": 100, "already_loaded": True}
    facts.update(copy.deepcopy(case.get("kwargs", {})))
    captured = {}
    with tempfile.TemporaryDirectory(prefix="gate-golden-", dir=directory) as temporary:
        root = Path(temporary)
        sample = root / "sample.json"
        sample.write_text(json.dumps(case["datasets"]), encoding="utf-8")
        output = root / "output"
        with (
            _Lane(case, module) as lane,
            mock.patch.object(module.time, "monotonic", return_value=0),
            mock.patch.object(
                module.fast,
                "run_check",
                side_effect=lambda *args, **kwargs: _run_frozen(module, original, args, kwargs),
            ),
        ):
            try:
                captured["return"] = module.run("http://127.0.0.1:1", "fixture-model", [sample], output, **facts)
            except (
                module.fast.ValidationError,
                module.ResponseError,
                TypeError,
                KeyError,
                OverflowError,
            ) as exc:
                captured["exception"] = {"type": type(exc).__name__, "message": str(exc)}
        captured["requests"] = lane.requests
        captured["files"] = {
            str(path.relative_to(output)): path.read_text(encoding="utf-8")
            for path in sorted(output.glob("*"))
            if path.is_file()
        }
    return captured


class RunCheckGateGoldenTests(unittest.TestCase):
    """Keep gate input ordering and complete output evidence observable."""


def _golden_test(case):
    def test(self):
        self.assertEqual(capture_case(case["input"]), case["expected"])

    return test


def load_tests(loader, tests, pattern):
    path = Path(__file__).parent / "test_support" / "eci_fast_check_run_check_golden" / "gates.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    suite = unittest.TestSuite()
    names = set()
    for case in cases:
        name = "test_" + case["name"]
        if name in names:
            raise ValueError(f"Duplicate golden case: {name}")
        names.add(name)
        setattr(RunCheckGateGoldenTests, name, _golden_test(case))
        suite.addTest(RunCheckGateGoldenTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
