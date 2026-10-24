"""Third-round CLI regressions: fake service runners, locks and temporary state only."""
import io
import os
import plistlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from xml.etree import ElementTree as ET

from support import FakeRunner, forbid_real_commands, temp_dir
from agiw import cli
from agiw.state_lock import StateLockBusy


class CliFix3(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        self.home = temp_dir(self, "agiw-cli-fix3-").resolve()
        self.state = self.home / "state"
        self.state.mkdir()
        self.key = self.state / "identity.json"
        self.key.write_bytes(b"unchanged identity")
        self.manager = SimpleNamespace(home=self.home, uninstall=mock.Mock(return_value=["removed service"]))

    def invoke(self, *argv, **kwargs):
        lines, error = [], io.StringIO()
        with mock.patch.object(cli, "make_manager", return_value=self.manager), \
                mock.patch("sys.stderr", error):
            code = cli.main(["--state-dir", str(self.state), *argv], out=lines.append,
                            probe=kwargs.pop("probe", lambda *_: "free"), **kwargs)
        return code, "\n".join(lines), error.getvalue()

    def test_foreground_lock_refusal_exits_tempfail(self):
        with mock.patch.object(cli.StateLock, "acquire", side_effect=StateLockBusy("busy")), \
                mock.patch.object(cli.pl, "main") as peer:
            code, output, error = self.invoke("serve", "--host", "linux")
        self.assertEqual(code, 75, output + error)
        self.assertIn("state folder", error)
        self.assertIn("stop it first", error)
        peer.assert_not_called()

    def test_launchd_service_lock_refusal_exits_success_with_message(self):
        with mock.patch.object(cli.StateLock, "acquire", side_effect=StateLockBusy("busy")), \
                mock.patch.object(cli.pl, "main") as peer:
            code, output, error = self.invoke("serve", "--service", "--host", "mac")
        self.assertEqual(code, 0, output + error)
        self.assertIn("state folder", output + error)
        self.assertIn("stop it first", output + error)
        peer.assert_not_called()

    def test_service_flag_does_not_hide_other_lock_errors(self):
        with mock.patch.object(cli.StateLock, "acquire", side_effect=PermissionError("denied")), \
                mock.patch.object(cli.pl, "main") as peer:
            code, output, error = self.invoke("serve", "--service", "--host", "mac")
        self.assertEqual(code, 1, output + error)
        self.assertIn("PermissionError", error)
        peer.assert_not_called()

    def test_purge_lock_refusal_reports_removed_service_and_unchanged_keys(self):
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        probe = mock.Mock(return_value="free")

        def locked(_lock):
            self.manager.uninstall.assert_called_once_with()
            raise StateLockBusy("busy")

        with mock.patch.object(cli.StateLock, "acquire", locked):
            code, output, error = self.invoke("uninstall", "--purge-keys", "--yes", probe=probe)
        self.assertEqual(code, 1, output + error)
        self.assertIn("the login service was removed; no key file was deleted", error)
        self.assertIn("the peer link is running with this state folder", error)
        self.assertEqual({p.name: p.read_bytes() for p in self.state.iterdir()}, before)
        probe.assert_not_called()

    def test_purge_port_refusal_reports_removed_service_and_unchanged_keys(self):
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        code, output, error = self.invoke("uninstall", "--purge-keys", "--yes",
                                          probe=lambda *_: "busy")
        self.assertEqual(code, 1, output + error)
        self.manager.uninstall.assert_called_once_with()
        self.assertIn("the login service was removed; no key file was deleted", error)
        self.assertIn("tcp/8768 is busy", error)
        self.assertEqual({p.name: p.read_bytes() for p in self.state.iterdir()}, before)

    def test_uninstall_stops_service_before_taking_its_lock(self):
        events = []
        self.manager.uninstall.side_effect = lambda: events.append("stop") or []
        original = cli.StateLock.acquire

        def acquire(lock):
            events.append("lock")
            self.assertEqual(events, ["stop", "lock"])
            return original(lock)

        with mock.patch.object(cli.StateLock, "acquire", acquire):
            code, output, error = self.invoke("uninstall", "--purge-keys", "--yes")
        self.assertEqual(code, 0, output + error)
        self.assertFalse(self.key.exists())

    def test_default_xdg_state_is_pinned_into_each_service_definition(self):
        state_home = self.home / "xdg state"
        expected = str(state_home / "agiw" / "peer-link")
        for kind in ("mac", "linux", "windows"):
            with self.subTest(kind=kind):
                runner = FakeRunner({"/user": (0, '"user","S-1-5-21-42-1001"\n'),
                                     "is-active": (0, "active\n")})
                extra = ["--status-file", str(self.home / "observer.json")] if kind == "windows" else []
                lines = []
                with mock.patch.object(cli.pl.sys, "platform", "linux"), \
                        mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
                    code = cli.main(["service", "install", *extra], out=lines.append, runner=runner,
                                    kind=kind, home=self.home, env={"USERNAME": "tester"})
                self.assertEqual(code, 0, lines)
                manager = cli.ServiceManager(kind, home=self.home, env={})
                raw = manager.definition_path().read_bytes()
                if kind == "mac":
                    args = plistlib.loads(raw)["ProgramArguments"]
                    self.assertEqual(args[args.index("--state-dir") + 1], expected)
                elif kind == "linux":
                    self.assertIn('"--state-dir" "' + expected + '"', raw.decode())
                else:
                    root = ET.fromstring(raw)
                    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
                    self.assertIn('--state-dir "' + expected + '"',
                                  root.find("t:Actions/t:Exec/t:Arguments", ns).text)

    def test_windows_purge_continues_after_each_unlink_failure_and_revokes_registry(self):
        peers = self.state / "peers.json"
        peers.write_bytes(b"peers can be deleted")
        temporary = self.state / ".keys.tmp"
        temporary.write_bytes(b"also held")
        original = Path.unlink
        attempted = []

        def unlink(path, *args, **kwargs):
            attempted.append(path)
            if path in (self.key, temporary):
                raise PermissionError("scanner holds the file")
            return original(path, *args, **kwargs)

        windows_os = mock.Mock(wraps=os)
        windows_os.name = "nt"
        with mock.patch.object(cli, "os", windows_os), mock.patch.object(Path, "unlink", unlink), \
                mock.patch.object(cli.pl, "_gen_revoke_all") as revoke:
            code, output, error = self.invoke("uninstall", "--purge-keys", "--yes")
        self.assertEqual(code, 1, output + error)
        revoke.assert_called_once_with()
        self.assertIn(peers, attempted)
        self.assertFalse(peers.exists())
        self.assertEqual(self.key.read_bytes(), b"unchanged identity")
        self.assertEqual(temporary.read_bytes(), b"also held")
        self.assertIn("could not delete: ~/state/identity.json", output + error)
        self.assertIn("could not delete: ~/state/.keys.tmp", output + error)
        self.assertIn("PermissionError", output + error)
        self.assertIn("scanner holds the file", output + error)
        self.assertIn("deleted 1 file(s)", output)

    def test_purge_lock_file_unlink_error_is_reported_after_key_deletion(self):
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name.endswith(".agiw-lock"):
                raise PermissionError("lock file held")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", unlink):
            code, output, error = self.invoke("uninstall", "--purge-keys", "--yes")
        self.assertEqual(code, 1, output + error)
        self.assertFalse(self.key.exists())
        self.assertIn("could not remove the state lock file", output + error)
        self.assertIn("PermissionError", output + error)
        self.assertNotIn("no key file was deleted", output + error)

    def test_uninstall_documents_direct_peer_link_purge_limit(self):
        docs = (Path(__file__).resolve().parents[1] / "docs" / "cli.md").read_text()
        uninstall = docs.split("## Uninstall", 1)[1].split("## What leaves the machine", 1)[0]
        self.assertIn("Purge protects against `agiw serve` and the login service; "
                      "if you started peer_link.py directly, stop it before purging.", uninstall)


if __name__ == "__main__":
    unittest.main()
