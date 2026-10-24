"""Shared test helpers. The repository root goes first on sys.path, so these tests exercise the checkout
(agiw/ and peer_link.py) whether or not the package is installed."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import peer_link as pl  # noqa: E402


def temp_dir(case: unittest.TestCase, prefix: str = "agiw-") -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    case.addCleanup(shutil.rmtree, path, True)
    return path


class FakeRunner:
    """Records service commands instead of running them. results maps a word in the command (such as
    'print' or 'is-active') to (returncode, stdout)."""

    def __init__(self, results=None, default=(0, "")):
        self.calls, self.results, self.default = [], dict(results or {}), default

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        code, stdout = next((v for k, v in self.results.items() if k in argv), self.default)
        return subprocess.CompletedProcess(argv, code, stdout, "")


def forbid_real_commands(case: unittest.TestCase):
    """Any attempt to start a real process (launchctl, systemctl, schtasks...) fails the test."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"a real command was started: {args[:1]}")

    for target in ("subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
                   "subprocess.check_output", "os.system"):
        patcher = mock.patch(target, side_effect=refuse)
        patcher.start()
        case.addCleanup(patcher.stop)
    import agiw.service
    real = mock.patch.object(agiw.service, "real_runner", side_effect=refuse)
    started = real.start()
    case.addCleanup(real.stop)
    return started


def isolate_windows_registry(case: unittest.TestCase) -> dict:
    """Pairing generations live in HKCU on Windows; tests keep them in memory and never touch the real key."""
    store: dict = {}
    for name, fake in (("_gen_get", store.get), ("_gen_set", store.__setitem__),
                       ("_gen_delete", lambda node: store.pop(node, None)), ("_gen_revoke_all", store.clear)):
        patcher = mock.patch.object(pl, name, fake)
        patcher.start()
        case.addCleanup(patcher.stop)
    patcher = mock.patch.object(pl, "_GENERATIONS", r"Software\AGIW-cli-test\never-used")
    patcher.start()
    case.addCleanup(patcher.stop)
    return store


def status_doc(host: str, name: str, health: str = "ok") -> dict:
    now = time.time()
    return {"schemaVersion": 1, "kind": "agiw-peer", "host": name, "platform": host, "edition": "test",
            "observedAt": pl._iso(now), "observedUnix": round(now, 3), "health": health,
            "models": [{"id": "test-model", "loadedState": "loaded"}]}


def env_without(*names: str) -> dict:
    return {k: v for k, v in os.environ.items() if k not in names}
