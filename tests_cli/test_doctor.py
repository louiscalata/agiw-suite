"""`agiw doctor` and every diagnostic line: the home folder shows as ~, and no key, token, share path or
folder outside the home is ever printed. Ports are probed through a fake; nothing is changed."""
import contextlib
import os
import secrets
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

from support import FakeRunner, forbid_real_commands, pl, temp_dir

from agiw import cli, doctor
from agiw.redact import HIDDEN_PATH, redact, show_path

TOKEN = secrets.token_hex(32)
UNC = "\\\\nas-box\\team\\agiw"
MOUNTED = "/Volumes/TeamShare/agiw/state"


class Redaction(unittest.TestCase):
    def test_home_becomes_tilde_only_at_a_path_boundary(self):
        self.assertEqual(redact("/home/ann/.local/state/agiw", "/home/ann"), "~/.local/state/agiw")
        self.assertEqual(redact("/home/annex/x", "/home/ann"), "/home/annex/x")
        self.assertEqual(redact("C:\\Users\\Ann\\AppData\\Local\\AGIW", "C:\\Users\\ann"), "~\\AppData\\Local\\AGIW")
        self.assertEqual(redact("C:/Users/ann/x", "C:\\Users\\ann"), "~/x")

    def test_secrets_and_share_paths_are_replaced(self):
        text = redact(f"token {TOKEN} sealed dpapi1:QUJD+/== at {UNC} and {MOUNTED} or smb://nas-box/team", "/h")
        for secret in (TOKEN, "dpapi1:", "nas-box", "TeamShare", "/Volumes/"):
            self.assertNotIn(secret, text)
        self.assertIn("[redacted]", text)
        self.assertIn("[share path]", text)

    def test_node_ids_and_pins_survive(self):
        node = "0f8e2d4c-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
        self.assertEqual(redact(f"{node} PIN 123456", "/h"), f"{node} PIN 123456")

    def test_paths_outside_home_are_described_not_printed(self):
        self.assertEqual(show_path(Path("/srv/private/agiw"), "/home/ann"), HIDDEN_PATH)
        self.assertEqual(show_path(Path("/home/ann/.config/x"), "/home/ann"), str(Path("~/.config/x")))


class Doctor(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        acl = mock.patch.object(pl, "_only_me", lambda path, is_dir: None)  # macOS ACL listing runs /bin/ls
        acl.start()
        self.addCleanup(acl.stop)
        self.home = temp_dir(self, "agiw-doctor-home-")
        self.state = self.home / "state" / "peer-link"
        self.state.mkdir(parents=True)
        if os.name != "nt":
            os.chmod(self.state, 0o700)
        token = self.state / "control-token"
        # Metadata-only fixture: doctor must never decode this, including through Windows DPAPI.
        token.write_text(TOKEN + "\n")
        if os.name != "nt":
            os.chmod(token, 0o600)
        self.lines = []

    def run_doctor(self, probe=lambda port, proto: "free", **kw):
        kw.setdefault("kind", "linux")
        state = kw.pop("state", self.state)
        return doctor.run(state, 8768, 8769, probe=probe, write=self.lines.append,
                          home=str(self.home), **kw)

    def assert_clean(self):
        text = "\n".join(self.lines)
        for leak in (str(self.home), TOKEN, "nas-box", "TeamShare", "/Volumes/", os.path.realpath(self.home)):
            self.assertNotIn(leak, text)
        return text

    def test_busy_ports_report_occupancy_without_claiming_peer_identity(self):
        code = self.run_doctor(probe=lambda port, proto: "in use")
        text = self.assert_clean()
        self.assertEqual(code, 0, text)
        self.assertIn("~", text)
        self.assertIn("tcp/8768: in use", text)
        self.assertIn("udp/8769: in use", text)
        self.assertIn("port in use (probably the peer link; run `agiw status` to check)", text)
        self.assertNotIn("clock", text)

    def test_free_ports_report_not_running_without_a_token(self):
        (self.state / "control-token").unlink()
        code = self.run_doctor()
        text = self.assert_clean()
        self.assertEqual(code, 0)
        self.assertIn("peer link: not running", text)
        self.assertIn("tcp/8768: free", text)
        self.assertIn("udp/8769: free", text)

    def test_unknown_ports_do_not_claim_running_or_not_running(self):
        code = self.run_doctor(probe=lambda port, proto: "unknown")
        text = self.assert_clean()
        self.assertEqual(code, 0)
        self.assertIn("peer link: could not be checked; run `agiw status` to check", text)
        self.assertNotIn("peer link: not running", text)
        self.assertIn("udp/8769: could not be checked", text)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_loose_folder_and_key_files_are_reported_not_fixed(self):
        os.chmod(self.state, 0o755)
        os.chmod(self.state / "control-token", 0o644)
        self.run_doctor()
        text = self.assert_clean()
        self.assertIn("open to other users", text)
        self.assertIn("1 key file(s) could have been read by others", text)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)  # the doctor changed nothing

    def test_state_folder_outside_home_is_not_printed(self):
        outside = temp_dir(self, "agiw-outside-") / "peer-link"
        self.run_doctor(state=outside)
        text = "\n".join(self.lines)
        self.assertIn(HIDDEN_PATH, text)
        self.assertNotIn(str(outside), text)

    def test_firewall_hints_are_text_for_each_os(self):
        for kind, word in (("mac", "System Settings"), ("linux", "ufw"), ("windows", "New-NetFirewallRule")):
            self.lines = []
            self.run_doctor(kind=kind)
            text = self.assert_clean()
            self.assertIn(word, text)
            self.assertIn("8768", text)
            self.assertIn("8769", text)

    def test_old_python_fails(self):
        self.assertEqual(self.run_doctor(version=(3, 8, 18)), 1)

    def test_through_the_cli_without_a_running_peer_link(self):
        lines = []
        with mock.patch.object(doctor, "probe_port", lambda port, proto: "free"):
            code = cli.main(["--state-dir", str(self.home / "fresh"), "doctor"], out=lines.append,
                            runner=FakeRunner(), kind="mac", home=self.home, env={})
        text = "\n".join(lines)
        self.assertEqual(code, 0, text)
        self.assertIn("not created yet", text)
        self.assertIn("login service: not installed", text)
        self.assertNotIn(str(self.home), text)
        self.assertFalse((self.home / "fresh").exists(), "doctor must not create the state folder")

    def cli_doctor(self):
        return cli.main(["--state-dir", str(self.state), "doctor"], out=self.lines.append,
                        runner=FakeRunner(), kind="linux", home=self.home, env={})

    def test_cli_doctor_opens_no_tcp_connection(self):
        with mock.patch.object(doctor, "probe_port", return_value="in use"), \
                mock.patch("socket.create_connection", side_effect=AssertionError("TCP connection")) as tcp, \
                mock.patch("http.client.HTTPConnection", side_effect=AssertionError("HTTP connection")) as http, \
                mock.patch.object(pl, "_read_private", return_value=TOKEN), \
                mock.patch.object(pl, "_unseal", return_value=TOKEN):
            # Force any regressed exchange path past token loading without requiring a real DPAPI blob.
            code = self.cli_doctor()
        self.assertEqual(code, 0)
        tcp.assert_not_called()
        http.assert_not_called()
        self.assertIn("port in use (probably the peer link; run `agiw status` to check)",
                      "\n".join(self.lines))

    def test_cli_doctor_never_reads_or_unseals_token_contents(self):
        with mock.patch.object(doctor, "probe_port", return_value="free"), \
                mock.patch.object(pl, "_read_private", side_effect=AssertionError("read token")) as read, \
                mock.patch.object(pl, "_unseal", side_effect=AssertionError("unseal token")) as unseal:
            code = self.cli_doctor()
        self.assertEqual(code, 0)
        read.assert_not_called()
        unseal.assert_not_called()

    def test_windows_token_privacy_is_checked_without_unsealing(self):
        windows_os = mock.Mock(wraps=os)
        windows_os.name = "nt"
        with mock.patch.object(doctor, "os", windows_os), \
                mock.patch.object(pl, "_win_check_private", side_effect=[None, PermissionError()]) as acl, \
                mock.patch.object(pl, "_unseal", side_effect=AssertionError("unseal token")) as unseal:
            rows = doctor.check_state_dir(self.state, str(self.home))
        self.assertIn("1 key file(s) could have been read by others", "\n".join(text for _, text in rows))
        self.assertEqual(acl.call_args_list, [mock.call(str(self.state), self.state.name),
                                            mock.call(str(self.state / "control-token"), "control-token")])
        unseal.assert_not_called()

    def test_mac_token_acl_privacy_is_checked_without_reading_contents(self):
        mac_sys = mock.Mock(wraps=sys)
        mac_sys.platform = "darwin"
        posix_os = mock.Mock(wraps=os)
        posix_os.name = "posix"
        posix_os.getuid = mock.Mock(return_value=123)
        posix_os.lstat = lambda path: mock.Mock(st_mode=(stat.S_IFDIR | 0o700 if path == self.state else
                                                        stat.S_IFREG | 0o600), st_uid=123)
        with mock.patch.object(doctor, "sys", mac_sys), mock.patch.object(doctor, "os", posix_os), \
                mock.patch.object(pl, "_only_me", side_effect=[None, PermissionError()]) as acl, \
                mock.patch.object(pl, "_read_private", side_effect=AssertionError("read token")) as read:
            rows = doctor.check_state_dir(self.state, str(self.home))
        self.assertIn("1 key file(s) could have been read by others", "\n".join(text for _, text in rows))
        self.assertEqual(acl.call_args_list, [mock.call(self.state, True),
                                            mock.call(self.state / "control-token", False)])
        read.assert_not_called()

    def state_snapshot(self):
        return {str(path.relative_to(self.state)): (stat.S_IMODE(path.lstat().st_mode),
                path.read_bytes() if path.is_file() else None)
                for path in [self.state, *self.state.rglob("*")]}

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_cli_doctor_preserves_loose_state_modes_contents_and_listing(self):
        (self.state / "notes.txt").write_bytes(b"keep exactly\n")
        os.chmod(self.state, 0o755)
        os.chmod(self.state / "control-token", 0o644)
        before = self.state_snapshot()
        with mock.patch.object(doctor, "probe_port", return_value="free"):
            code = self.cli_doctor()
        self.assertEqual(self.state_snapshot(), before)
        self.assertEqual(code, 0)
        self.assertIn("open to other users", "\n".join(self.lines))

    def test_cli_doctor_never_calls_filesystem_mutators(self):
        with contextlib.ExitStack() as stack:
            forbidden = [stack.enter_context(mock.patch("os." + name,
                         side_effect=AssertionError("doctor attempted " + name)))
                         for name in ("chmod", "unlink", "remove", "replace", "rename", "mkdir")]
            stack.enter_context(mock.patch.object(doctor, "probe_port", return_value="free"))
            code = self.cli_doctor()
        self.assertEqual(code, 0)
        self.assertIn("peer link: not running", "\n".join(self.lines))
        for method in forbidden:
            method.assert_not_called()

    def test_case_variant_token_is_preserved_without_reading_it(self):
        variant = self.state / "Control-Token"
        (self.state / "control-token").rename(variant)
        before = self.state_snapshot()
        with mock.patch.object(pl, "_read_private", side_effect=AssertionError("read token")):
            self.run_doctor()
        self.assertEqual(self.state_snapshot(), before)

if __name__ == "__main__":
    unittest.main()
