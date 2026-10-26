"""The native watchdog tests require Linux's /usr/bin/timeout; never emulate it."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

LINUX = Path(__file__).resolve().parents[1]
WRAPPER = LINUX / "agiw-linux-once"
NATIVE = sys.platform == "linux" and os.access("/usr/bin/timeout", os.X_OK)


class OuterDeadline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="outer-deadline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def copy_wrapper(self):
        entry = self.root / "release with spaces" / WRAPPER.name
        entry.parent.mkdir()
        shutil.copyfile(WRAPPER, entry)
        entry.chmod(0o700)
        return entry

    def test_launcher_uses_fixed_native_paths_and_no_caller_command(self):
        source = WRAPPER.read_text()
        self.assertTrue(source.startswith("#!/bin/sh\n"))
        self.assertIn('exec /usr/bin/timeout --signal=KILL 3.8s /usr/bin/python3 -I -B', source)
        self.assertNotIn("SSH_ORIGINAL_COMMAND", source)
        self.assertNotIn('eval ', source)
        self.assertNotIn('/usr/bin/env', source)
        self.assertNotIn('--foreground', '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('#')))

    def test_arguments_are_refused_before_any_python_startup(self):
        wrapper = self.copy_wrapper()
        # Would fail loudly if any argument reaches the interpreter.
        (wrapper.parent / "agiw-linux-status").write_text("raise AssertionError('executed')\n")
        for args in (["--once"], ["--serve"], ["; touch sentinel"], ["--port", "1234"]):
            with self.subTest(args=args):
                result = subprocess.run([str(wrapper), *args], capture_output=True, timeout=2)
                self.assertEqual(result.returncode, 64)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"accepts no arguments", result.stderr)
                self.assertNotIn(b"AssertionError", result.stderr)

    def test_relative_invocation_is_refused(self):
        wrapper = self.copy_wrapper()
        result = subprocess.run(["./" + wrapper.name], cwd=wrapper.parent,
                                capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 64)
        self.assertIn(b"absolute path", result.stderr)

    @unittest.skipIf(os.access("/usr/bin/timeout", os.X_OK), "native watchdog is present")
    def test_missing_watchdog_refuses_without_running_python_or_writing(self):
        wrapper = self.copy_wrapper()
        (wrapper.parent / "agiw-linux-status").write_text("raise AssertionError('executed')\n")
        before = set(self.root.rglob("*"))
        result = subprocess.run([str(wrapper)], capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 69)
        self.assertIn(b"native deadline unavailable", result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(before, set(self.root.rglob("*")))

    @unittest.skipUnless(NATIVE, "requires real Linux /usr/bin/timeout")
    def test_delayed_imports_are_killed_before_four_seconds(self):
        wrapper = self.copy_wrapper()
        # Actual installed Python entry, with delay before the server's timer exists.
        shutil.copyfile(LINUX / "agiw-linux-status", wrapper.parent / "agiw-linux-status")
        package = wrapper.parent / "agiw_linux"
        package.mkdir()
        (package / "__init__.py").write_text("import time\ntime.sleep(10)\n")
        (package / "server.py").write_text("def main(): return 0\n")
        before = set(self.root.rglob("*"))
        self.assert_deadline(wrapper)
        self.assertEqual(before, set(self.root.rglob("*")))

    @unittest.skipUnless(NATIVE, "requires real Linux /usr/bin/timeout")
    def test_delayed_interpreter_boundary_is_killed_before_four_seconds(self):
        wrapper = self.copy_wrapper()
        # Inject only the interpreter path into a fixture copy. The watchdog is
        # the real, absolute native executable, and must start before this stub.
        interpreter = self.root / "slow-interpreter"
        interpreter.write_text("#!/bin/sh\nexec /bin/sleep 10\n")
        interpreter.chmod(0o700)
        wrapper.write_text(wrapper.read_text().replace("/usr/bin/python3", '"' + str(interpreter) + '"'))
        self.assert_deadline(wrapper)

    @unittest.skipUnless(NATIVE, "requires real Linux /usr/bin/timeout")
    def test_deadline_kills_descendants_that_ignore_term(self):
        wrapper = self.copy_wrapper()
        pid_path = self.root / "fixture-child.pid"
        (wrapper.parent / "agiw-linux-status").write_text(
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    while True: time.sleep(10)\n"
            f"with open({str(pid_path)!r}, 'w') as stream: stream.write(str(pid))\n"
            "while True: time.sleep(10)\n")
        try:
            self.assert_deadline(wrapper)
            pid = int(pid_path.read_text())
            # A killed child may briefly remain a zombie pending init's reap;
            # no running descendant may survive the watchdog's process-group kill.
            status = Path(f"/proc/{pid}/stat")
            self.assertTrue(not status.exists() or status.read_text().rsplit(")", 1)[1].split()[0] == "Z")
        finally:
            if pid_path.exists():
                try:
                    os.kill(int(pid_path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(NATIVE, "requires real Linux /usr/bin/timeout")
    def test_fresh_cache_with_hostile_environment_is_read_only(self):
        wrapper = self.copy_wrapper()
        shutil.copyfile(LINUX / "agiw-linux-status", wrapper.parent / "agiw-linux-status")
        shutil.copytree(LINUX / "agiw_linux", wrapper.parent / "agiw_linux",
                        ignore=shutil.ignore_patterns("__pycache__"))
        sys.path.insert(0, str(LINUX))
        self.addCleanup(sys.path.remove, str(LINUX))
        from agiw_linux.collect import project_status, unknown_snapshot
        from agiw_linux.server import write_status
        state = self.root / "state"
        value = project_status(unknown_snapshot(now=time.time(), host="fixture"))
        write_status(state / "agiw", value)
        hostile = self.root / "hostile"
        hostile.mkdir()
        for name in ("python3", "timeout"):
            (hostile / name).write_text("#!/bin/sh\nexit 98\n")
            (hostile / name).chmod(0o700)
        (hostile / "sitecustomize.py").write_text("raise AssertionError('injected')\n")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        env = dict(os.environ, PATH=str(hostile), PYTHONPATH=str(hostile),
                   XDG_STATE_HOME=str(state), SSH_ORIGINAL_COMMAND="--serve; touch sentinel")
        started = time.monotonic()
        result = subprocess.run([str(wrapper)], env=env, cwd=hostile, capture_output=True, timeout=5)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(json.loads(result.stdout)["observedUnix"], value["observedUnix"])
        self.assertLessEqual(len(result.stdout), 65536)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def assert_deadline(self, wrapper):
        started = time.monotonic()
        result = subprocess.run([str(wrapper)], capture_output=True, timeout=5)
        self.assertLess(time.monotonic() - started, 4)
        self.assertIn(result.returncode, (-signal.SIGKILL, 128 + signal.SIGKILL))
        self.assertEqual(result.stdout, b"")  # Refusal, never an invented healthy document.


if __name__ == "__main__":
    unittest.main()
