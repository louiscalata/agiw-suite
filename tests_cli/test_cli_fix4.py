"""Fourth-round CLI regressions: temporary state, fake services and registry only."""
import io
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import FakeRunner, forbid_real_commands, isolate_windows_registry, temp_dir
from agiw import cli
from agiw.state_lock import StateLockBusy


class CliFix4(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        isolate_windows_registry(self)
        self.home = temp_dir(self, "agiw-cli-fix4-").resolve()
        self.state = self.home / "state"
        self.state.mkdir()
        self.key = self.state / "identity.json"
        self.key.write_bytes(b"identity")
        self.manager = SimpleNamespace(home=self.home,
                                       uninstall=mock.Mock(return_value=["removed service"]))

    def invoke(self, *argv, probe=None):
        events = []
        error = io.StringIO()
        with mock.patch.object(cli, "make_manager", return_value=self.manager), \
                mock.patch("sys.stderr", error):
            code = cli.main(["--state-dir", str(self.state), *argv], out=events.append,
                            probe=probe or (lambda *_: "free"))
        return code, events, error.getvalue()

    def test_purge_lock_refusal_reports_no_service_was_installed(self):
        self.manager.uninstall.return_value = ["no agiw service was installed"]

        def acquire(_lock):
            self.manager.uninstall.assert_called_once_with()
            raise StateLockBusy("busy")

        with mock.patch.object(cli.StateLock, "acquire", acquire):
            code, output, error = self.invoke("uninstall", "--purge-keys", "--yes")
        self.assertEqual(code, 1, output)
        self.assertIn("no agiw service was installed; no key file was deleted", error)
        self.assertNotIn("the login service was removed", error)
        self.assertEqual(self.key.read_bytes(), b"identity")

    def test_purge_port_refusal_reports_no_service_was_installed(self):
        self.manager.uninstall.return_value = ["no agiw service was installed"]
        code, output, error = self.invoke("uninstall", "--purge-keys", "--yes",
                                          probe=lambda *_: "busy")
        self.assertEqual(code, 1, output)
        self.assertIn("no agiw service was installed; no key file was deleted", error)
        self.assertNotIn("the login service was removed", error)
        self.assertEqual(self.key.read_bytes(), b"identity")

    def test_purge_refusal_preserves_confirmation_when_no_removal_was_reported(self):
        self.manager = cli.ServiceManager("linux", home=self.home, env={},
                                          runner=FakeRunner({"is-active": (3, "inactive\n")}))
        with mock.patch.object(cli.StateLock, "acquire", side_effect=StateLockBusy("busy")):
            code, output, error = self.invoke("uninstall", "--purge-keys", "--yes")
        self.assertEqual(code, 1, output)
        self.assertIn("confirmed agiw-peer-link.service is not running; no key file was deleted", error)
        self.assertNotIn("the login service was removed", error)
        self.assertEqual(self.key.read_bytes(), b"identity")

    def test_windows_registry_failure_follows_file_summary_and_exits_one(self):
        for held_file in (False, True):
            with self.subTest(held_file=held_file):
                self.key.write_bytes(b"identity")
                peers = self.state / "peers.json"
                peers.write_bytes(b"peers")
                original_unlink = Path.unlink

                def unlink(path, *args, **kwargs):
                    if held_file and path == self.key:
                        raise PermissionError("scanner holds identity")
                    return original_unlink(path, *args, **kwargs)

                windows_os = mock.Mock(wraps=os)
                windows_os.name = "nt"
                with mock.patch.object(cli, "os", windows_os), \
                        mock.patch.object(Path, "unlink", unlink), \
                        mock.patch.object(cli.pl, "_gen_revoke_all", side_effect=PermissionError(
                            "Access is denied: " + str(self.home / "registry"))) as revoke:
                    code, output, error = self.invoke("uninstall", "--purge-keys", "--yes")
                self.assertEqual(code, 1, output)
                revoke.assert_called_once_with()
                self.assertFalse(peers.exists())
                self.assertEqual(self.key.exists(), held_file)
                summary = next(i for i, line in enumerate(output) if line.startswith("deleted "))
                failure = next(i for i, line in enumerate(output)
                               if "revoking the Windows pairing generations failed" in line)
                self.assertLess(summary, failure)
                self.assertIn("deleted 1 file(s); 1 file(s) could not be deleted" if held_file
                              else "deleted 2 file(s)", output[summary])
                self.assertIn("PermissionError", output[failure])
                self.assertIn("Access is denied", output[failure])
                self.assertNotIn(str(self.home), "\n".join(output) + error)
                self.assertNotIn("no key file was deleted", "\n".join(output) + error)
                self.assertNotIn("Traceback", error)
                # The next case creates its own files after this one's cleanup.
                self.state.mkdir(exist_ok=True)

    def test_windows_registry_failure_with_no_key_files_still_summarizes(self):
        self.key.unlink()
        windows_os = mock.Mock(wraps=os)
        windows_os.name = "nt"
        with mock.patch.object(cli, "os", windows_os), \
                mock.patch.object(cli.pl, "_gen_revoke_all", side_effect=OSError("registry unavailable")):
            code, output, error = self.invoke("uninstall", "--purge-keys", "--yes")
        self.assertEqual(code, 1, output)
        self.assertIn("deleted 0 file(s)", "\n".join(output))
        self.assertIn("revoking the Windows pairing generations failed", output[-1])
        self.assertIn("registry unavailable", output[-1])
        self.assertEqual(error, "")

    def test_service_lock_refusal_is_clean_on_every_host_kind(self):
        for kind in ("mac", "linux", "windows"):
            with self.subTest(kind=kind), \
                    mock.patch.object(cli.StateLock, "acquire", side_effect=StateLockBusy("busy")), \
                    mock.patch.object(cli.pl, "main") as peer:
                code, output, error = self.invoke("serve", "--service", "--host", kind,
                                                  "--status-file", str(self.home / "observer.json"))
                self.assertEqual(code, 0, output + [error])
                self.assertIn("stop it first", "\n".join(output) + error)
                peer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
