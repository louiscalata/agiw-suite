"""State-folder exclusion uses only temporary homes and a tiny Python child."""
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import ROOT, isolate_windows_registry, pl, temp_dir

from agiw import cli
from agiw import state_lock
from agiw.state_lock import StateLock, StateLockBusy


class StateLocks(unittest.TestCase):
    def setUp(self):
        isolate_windows_registry(self)
        self.home = temp_dir(self, "agiw-state-lock-").resolve()
        self.state = self.home / "state"
        self.state.mkdir()
        self.key = self.state / "identity.json"
        self.key.write_text("keep this identity")
        self.manager = SimpleNamespace(home=self.home,
                                       uninstall=mock.Mock(return_value=["login service removed"]))

    def hold_in_child(self):
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from agiw.state_lock import StateLock\n"
            "with StateLock(Path(sys.argv[1])):\n"
            "    print('locked', flush=True)\n"
            "    sys.stdin.readline()\n"
        )
        child = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.state)], cwd=ROOT,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

        def finish():
            try:
                child.communicate("\n", timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=10)
                self.fail("lock-holder child did not exit")

        self.addCleanup(finish)
        self.assertEqual(child.stdout.readline().strip(), "locked",
                         "child could not acquire the state lock")
        return child

    def invoke(self, *args, probe=None):
        output, error = [], io.StringIO()
        with mock.patch.object(cli, "make_manager", return_value=self.manager), \
                mock.patch("sys.stderr", error):
            code = cli.main(["--state-dir", str(self.state), *args], out=output.append,
                            probe=probe or mock.Mock(return_value="free"))
        return code, "\n".join(output), error.getvalue()

    def test_helper_excludes_a_second_process_and_uses_a_sibling_file(self):
        self.hold_in_child()
        lock = StateLock(self.state)
        self.assertEqual(lock.path, self.home / "state.agiw-lock")
        self.assertNotIn(lock.path, list(self.state.iterdir()))
        with self.assertRaises(StateLockBusy):
            lock.acquire()

    def test_purge_refuses_a_lock_held_by_another_process_before_any_deletion(self):
        self.hold_in_child()
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        probe = mock.Mock(return_value="free")
        code, output, error = self.invoke("--status-port", "9000", "uninstall", "--purge-keys", "--yes",
                                          probe=probe)
        self.assertEqual(code, 1, output + error)
        self.assertIn("the peer link is running with this state folder; stop it first; nothing was deleted",
                      error)
        self.assertEqual({p.name: p.read_bytes() for p in self.state.iterdir()}, before)
        self.manager.uninstall.assert_called_once_with()
        self.assertIn("the login service was removed; no key file was deleted", error)
        probe.assert_not_called()

    def test_serve_refuses_a_lock_held_by_another_process(self):
        self.hold_in_child()
        with mock.patch.object(pl, "main", return_value=0) as run:
            code, output, error = self.invoke("--status-port", "9000", "serve", "--host", "linux",
                                              "--beacon-port", "9001")
        self.assertEqual(code, 75, output + error)
        self.assertIn("stop it first", error)
        run.assert_not_called()

    def test_serve_holds_the_lock_for_the_entire_run_and_releases_on_return(self):
        def running(_argv):
            with self.assertRaises(StateLockBusy):
                with StateLock(self.state):
                    pass
            return 17

        with mock.patch.object(pl, "main", side_effect=running):
            code, output, error = self.invoke("serve", "--host", "linux")
        self.assertEqual(code, 17, output + error)
        with StateLock(self.state):
            pass

    def test_serve_releases_the_lock_on_exception(self):
        def interrupted(_argv):
            with self.assertRaises(StateLockBusy):
                with StateLock(self.state):
                    pass
            raise KeyboardInterrupt()

        with mock.patch.object(pl, "main", side_effect=interrupted):
            code, _, _ = self.invoke("serve", "--host", "linux")
        self.assertEqual(code, 130)
        with StateLock(self.state):
            pass

    def test_helper_rejects_an_opened_lock_file_replaced_before_acquisition(self):
        original_open = state_lock._open_lock
        lock = StateLock(self.state)

        def replaced(path):
            fd = original_open(path)
            path.rename(path.with_name(path.name + ".old"))
            path.write_bytes(b"replacement")
            return fd

        with mock.patch.object(state_lock, "_open_lock", side_effect=replaced):
            with self.assertRaisesRegex(OSError, "state lock file changed"):
                lock.acquire()
        self.assertIsNone(lock.fd)
        self.assertEqual(lock.path.read_bytes(), b"replacement")
        with StateLock(self.state):
            pass

    def test_helper_rejects_reparse_attributes_before_writing_a_seed_byte(self):
        original_stat = os.fstat

        def reparse(fd):
            info = original_stat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
                                   st_size=info.st_size, st_file_attributes=0x400)

        with mock.patch.object(state_lock.os, "fstat", side_effect=reparse), \
                mock.patch.object(state_lock.os, "write", side_effect=AssertionError("wrote through reparse")):
            with self.assertRaisesRegex(OSError, "not a regular file"):
                StateLock(self.state).acquire()

    def test_purge_holds_lock_through_deletion_and_removes_it_after_state_folder(self):
        original = Path.unlink
        saw = []

        def checked_unlink(path, *args, **kwargs):
            if path == self.key:
                with self.assertRaises(StateLockBusy):
                    with StateLock(self.state):
                        pass
                saw.append("key")
            if path == StateLock(self.state).path:
                self.assertFalse(self.state.exists())
                saw.append("lock")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", checked_unlink):
            code, output, error = self.invoke("uninstall", "--purge-keys", "--yes")
        self.assertEqual(code, 0, output + error)
        self.assertEqual(saw, ["key", "lock"])
        self.assertFalse(StateLock(self.state).path.exists())

    def test_purge_keeps_lock_when_unrelated_files_keep_state_folder(self):
        note = self.state / "notes.txt"
        note.write_text("mine")
        code, output, error = self.invoke("uninstall", "--purge-keys", "--yes")
        self.assertEqual(code, 0, output + error)
        self.assertTrue(StateLock(self.state).path.exists())
        self.assertEqual(note.read_text(), "mine")

    def test_purge_removes_an_empty_state_folder_and_its_lock(self):
        self.key.unlink()
        code, output, error = self.invoke("uninstall", "--purge-keys", "--yes")
        self.assertEqual(code, 0, output + error)
        self.assertFalse(self.state.exists())
        self.assertFalse(StateLock(self.state).path.exists())


if __name__ == "__main__":
    unittest.main()
