"""Regression coverage for purge safety and uninstall command reporting."""
import errno
import io
import os
import socket
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import FakeRunner, forbid_real_commands, isolate_windows_registry, pl, temp_dir

from agiw import cli, doctor

NODE = "0f8e2d4c-1a2b-4c3d-8e9f-0a1b2c3d4e5f"


class CliReview(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        isolate_windows_registry(self)
        legacy = mock.patch.object(pl, "control_request", side_effect=AssertionError("purge must not repair tokens"))
        legacy.start()
        self.addCleanup(legacy.stop)
        # These plaintext fixtures exercise purge policy, not native DPAPI or
        # ACL tooling. Read-only token validation itself has separate coverage.
        token = mock.patch.object(cli, "_purge_token", return_value="a" * 64)
        self.check_token = token.start()
        self.addCleanup(token.stop)
        self.home = temp_dir(self, "agiw-cli-review-")
        self.state = self.home / "state"
        self.state.mkdir()
        self.key = self.state / "identity.json"
        self.key.write_text("identity retained")
        self.token = self.state / "control-token"
        self.token.write_text("a" * 64)
        if os.name != "nt":
            os.chmod(self.state, 0o700)
            os.chmod(self.token, 0o600)
        self.manager = SimpleNamespace(home=self.home, uninstall=mock.Mock(return_value=["login service removed"]))

    def run_cli(self, *args, probe=None):
        lines, err = [], io.StringIO()
        kwargs = {} if probe is None else {"probe": probe}
        with mock.patch.object(cli, "make_manager", return_value=self.manager), mock.patch("sys.stderr", err):
            code = cli.main(["--state-dir", str(self.state), *args], out=lines.append,
                            runner=FakeRunner(), kind="linux", home=self.home, env={}, **kwargs)
        return code, "\n".join(lines), err.getvalue()

    def assert_refused(self, probe, port=pl.STATUS_PORT):
        code, output, err = self.run_cli("--status-port", str(port), "uninstall", "--purge-keys", "--yes",
                                         probe=probe)
        self.assertEqual(code, 1, output + err)
        self.assertTrue(self.key.exists())
        self.assertIn("stop", err.casefold())
        self.assertIn("nothing was deleted", err.casefold())
        return err

    def _junction(self, method):
        trusted = self.state / "trusted"
        trusted.mkdir()
        hidden = trusted / (NODE + ".json")
        hidden.write_text("outside key")
        real_lstat, real_listdir = os.lstat, os.listdir

        def fake_lstat(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if Path(path) == trusted and method == "attributes":
                fields = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
                fields["st_file_attributes"] = 0x400
                return SimpleNamespace(**fields)
            return result

        def checked_listdir(path):
            if Path(path) == trusted:
                self.fail("purge descended into a junction")
            return real_listdir(path)

        with mock.patch("os.lstat", side_effect=fake_lstat), \
                mock.patch.object(Path, "is_junction", lambda path: path == trusted and method == "junction",
                                  create=True), \
                mock.patch.object(Path, "is_symlink", return_value=False), \
                mock.patch("os.listdir", side_effect=checked_listdir) as listing, \
                mock.patch.object(cli, "peer_link_answers", return_value=False):
            delete, folders, keep = cli.purge_plan(self.state)
            self.assertIn(trusted, keep)
            self.assertNotIn(trusted, folders)
            self.assertNotIn(hidden, delete + keep)
            code, output, err = self.run_cli("uninstall", "--purge-keys", "--yes")
            self.assertEqual(code, 0, output + err)
            self.assertIn("left in place", output)
            self.assertNotIn(hidden.name, output)
            self.assertNotIn(trusted, [Path(call.args[0]) for call in listing.call_args_list])
        self.assertEqual(hidden.read_text(), "outside key")

    def test_purge_never_descends_into_reparse_attribute_directory(self):
        self._junction("attributes")

    def test_purge_never_descends_into_is_junction_directory(self):
        self._junction("junction")

    def test_purge_keeps_reparse_attribute_file_including_tmp(self):
        record = self.state / ".secret.tmp"
        record.write_text("outside file")
        real_lstat = os.lstat

        def flagged(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if Path(path) == record:
                fields = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
                fields["st_file_attributes"] = 0x400
                return SimpleNamespace(**fields)
            return result

        with mock.patch("os.lstat", side_effect=flagged):
            delete, folders, keep = cli.purge_plan(self.state)
        self.assertIn(record, keep)
        self.assertNotIn(record, delete)

    def test_purge_keeps_trusted_reparse_record_and_symlink_tmp(self):
        trusted = self.state / "trusted"
        trusted.mkdir()
        reparse = trusted / (NODE + ".json")
        linked_tmp = trusted / ".linked.tmp"
        reparse.write_text("outside record")
        linked_tmp.write_text("outside tmp")
        real_lstat, real_symlink = os.lstat, Path.is_symlink

        def flagged(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if Path(path) == reparse:
                fields = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
                fields["st_file_attributes"] = 0x400
                return SimpleNamespace(**fields)
            return result

        with mock.patch("os.lstat", side_effect=flagged), \
                mock.patch.object(Path, "is_symlink", lambda path: path == linked_tmp or real_symlink(path)), \
                mock.patch.object(cli, "peer_link_answers", return_value=False):
            delete, folders, keep = cli.purge_plan(self.state)
            self.assertIn(reparse, keep)
            self.assertIn(linked_tmp, keep)
            self.assertNotIn(reparse, delete)
            self.assertNotIn(linked_tmp, delete)
            code, output, err = self.run_cli("uninstall", "--purge-keys", "--yes")
            self.assertEqual(code, 0, output + err)
        self.assertEqual(reparse.read_text(), "outside record")
        self.assertEqual(linked_tmp.read_text(), "outside tmp")

    def test_purge_refuses_token_privacy_error_without_treating_it_as_stopped(self):
        with mock.patch.object(cli, "_purge_token", side_effect=PermissionError("loose token")):
            with self.assertRaises(cli.CliError):
                cli.peer_link_answers(self.state, pl.STATUS_PORT)

    def test_purge_refuses_uncertain_token_reads(self):
        for error in (PermissionError("loose token"), TimeoutError("late"), OSError(errno.EIO, "I/O"),
                      ValueError("bad token")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(cli, "_purge_token", side_effect=error):
                self.assert_refused(lambda port, proto: "free")

    def test_purge_refuses_token_acl_command_timeout(self):
        with mock.patch.object(cli, "_purge_token", side_effect=subprocess.TimeoutExpired("ls", 10)):
            self.assert_refused(lambda port, proto: "free")

    def test_purge_with_token_requires_selected_default_and_beacon_ports_free(self):
        probe = mock.Mock(return_value="free")
        selected = pl.STATUS_PORT + 10
        code, output, err = self.run_cli("--status-port", str(selected), "uninstall", "--purge-keys", "--yes",
                                         probe=probe)
        self.assertEqual(code, 0, output + err)
        self.assertFalse(self.key.exists())
        self.assertEqual(set(call.args for call in probe.call_args_list),
                         {(selected, "tcp"), (pl.STATUS_PORT, "tcp"), (pl.BEACON_PORT, "udp")})
        self.check_token.assert_called_once_with(self.state)

    def test_purge_with_token_refuses_busy_or_unknown_ports(self):
        for result in ("in use", "unknown"):
            with self.subTest(result=result), \
                    mock.patch.object(cli, "_purge_token", return_value="a" * 64):
                self.assert_refused(lambda port, proto: result)

    def test_purge_without_token_requires_selected_default_and_beacon_ports_free(self):
        self.token.unlink()
        selected = pl.STATUS_PORT + 10
        needed = {(selected, "tcp"), (pl.STATUS_PORT, "tcp"), (pl.BEACON_PORT, "udp")}
        probe = mock.Mock(return_value="free")
        with mock.patch.object(cli, "_purge_token", side_effect=AssertionError("must not read missing token")):
            code, output, err = self.run_cli("--status-port", str(selected), "uninstall", "--purge-keys", "--yes",
                                             probe=probe)
        self.assertEqual(code, 0, output + err)
        self.assertEqual(set(call.args for call in probe.call_args_list), needed)
        self.assertFalse(self.key.exists())

    def test_purge_without_token_refuses_each_busy_or_unknown_port(self):
        self.token.unlink()
        selected = pl.STATUS_PORT + 10
        for checked in ((selected, "tcp"), (pl.STATUS_PORT, "tcp"), (pl.BEACON_PORT, "udp")):
            for outcome in ("in use", "unknown"):
                with self.subTest(port=checked, outcome=outcome):
                    self.assert_refused(lambda port, proto: outcome if (port, proto) == checked else "free",
                                        port=selected)

    def test_purge_real_probe_refuses_a_listening_loopback_status_port_without_token(self):
        self.token.unlink()
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
            except BaseException:
                listener.close()
                raise
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES) or getattr(exc, "winerror", None) == 10013:
                self.skipTest(f"creating a loopback listener was denied: {exc}")
            raise
        with listener:
            port = listener.getsockname()[1]
            # Only the bind address is constrained; use the production probe and real socket behavior.
            probe = lambda port, proto: doctor.probe_port(port, proto, address="127.0.0.1")
            observed = probe(port, "tcp")
            self.assertEqual(observed, "in use")
            self.assert_refused(probe, port=port)

    def test_uninstall_reports_service_removed_and_keys_only_dry_run(self):
        code, output, err = self.run_cli("uninstall", "--purge-keys")
        self.assertEqual(code, 0, output + err)
        self.manager.uninstall.assert_called_once_with()
        self.assertIn("login service removed", output)
        self.assertIn("keys: dry run", output)
        self.assertIn("no key files were deleted", output)
        self.assertNotIn("dry run: nothing was deleted", output)
        self.assertTrue(self.key.exists())

    def test_yes_without_purge_is_usage_error_before_service_or_key_changes(self):
        code, output, err = self.run_cli("uninstall", "--yes")
        self.assertEqual(code, 2, output + err)
        self.manager.uninstall.assert_not_called()
        self.assertTrue(self.key.exists())
        self.assertTrue(self.token.exists())
        self.assertIn("--purge-keys", err)

    def test_uninstall_without_keys_still_reports_keys_only_dry_run(self):
        self.key.unlink()
        self.token.unlink()
        code, output, err = self.run_cli("uninstall", "--purge-keys")
        self.assertEqual(code, 0, output + err)
        self.assertIn("login service removed", output)
        self.assertIn("keys: dry run", output)
        self.assertIn("no key files were deleted", output)


class PurgeTokenChecks(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        self.home = temp_dir(self, "agiw-purge-token-")
        self.state = self.home / "state"
        self.state.mkdir()
        self.token = self.state / "control-token"
        self.token.write_text("a" * 64 + "\n")
        if os.name != "nt":
            os.chmod(self.state, 0o700)
            os.chmod(self.token, 0o600)
        # These are read-only policy tests with plaintext data; native Windows
        # DPAPI and OS ACL queries are explicit mocked boundaries.
        for name, value in (("_only_me", lambda *a, **k: None),
                            ("_win_check_private", lambda *a, **k: None),
                            ("_unseal", lambda stored, purpose: stored)):
            patcher = mock.patch.object(pl, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_purge_token_validation_is_read_only_and_makes_no_exchange(self):
        before = self.token.read_bytes()
        with mock.patch.object(pl, "_restrict_dir", side_effect=AssertionError("state repair")), \
                mock.patch.object(pl, "_http", side_effect=AssertionError("HTTP exchange")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("TCP connection")), \
                mock.patch("http.client.HTTPConnection", side_effect=AssertionError("HTTP connection")):
            self.assertEqual(cli._purge_token(self.state), "a" * 64)
            probe = mock.Mock(return_value="free")
            self.assertFalse(cli.peer_link_answers(self.state, 9000, probe=probe))
        self.assertEqual(self.token.read_bytes(), before)
        self.assertEqual(set(call.args for call in probe.call_args_list),
                         {(9000, "tcp"), (pl.STATUS_PORT, "tcp"), (pl.BEACON_PORT, "udp")})

    def test_purge_token_rejects_malformed_contents(self):
        self.token.write_text("invalid")
        with self.assertRaisesRegex(ValueError, "invalid control token"):
            cli._purge_token(self.state)

    def test_purge_token_rejects_case_variant(self):
        # Inject the listing so this behaves identically on case-sensitive and
        # case-insensitive test volumes.
        with mock.patch.object(cli.os, "listdir", return_value=["control-token", "Control-Token"]):
            with self.assertRaisesRegex(PermissionError, "case variant"):
                cli._purge_token(self.state)

    def test_purge_token_privacy_failure_prevents_unsealing(self):
        with mock.patch.object(pl, "_read_private", side_effect=PermissionError("not private")), \
                mock.patch.object(pl, "_unseal", side_effect=AssertionError("unsealed unsafe token")) as unseal:
            with self.assertRaises(PermissionError):
                cli._purge_token(self.state)
        unseal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
