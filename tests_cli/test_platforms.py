"""agiw runs on the Mac and the Windows PC only; on Linux it refuses before touching anything."""
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from support import FakeRunner, forbid_real_commands

from agiw import cli, service


class LinuxIsRefused(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        self.home = Path(tempfile.mkdtemp())
        self.state = self.home / "state"

    def run_cli(self, *argv, kind="linux"):
        runner, err = FakeRunner(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["--state-dir", str(self.state), *argv], out=lambda _: None, runner=runner,
                            kind=kind, home=self.home, env={})
        return code, err.getvalue(), runner

    def test_every_command_refuses_on_linux_and_changes_nothing(self):
        for argv in (("serve",), ("status",), ("peers",), ("doctor",), ("service", "install"),
                     ("service", "status"), ("uninstall",), ("uninstall", "--purge-keys", "--yes")):
            with self.subTest(argv=argv):
                code, err, runner = self.run_cli(*argv)
                self.assertEqual(code, cli.EXIT_USAGE)
                self.assertIn("macOS and Windows only", err)
                self.assertEqual(runner.calls, [])
                self.assertFalse(self.state.exists())
                self.assertEqual(list(self.home.iterdir()), [])

    def test_detected_linux_is_refused_too(self):
        with mock.patch.object(cli.pl, "detect_host_kind", return_value="linux"):
            code, err, _ = self.run_cli("status", kind=None)
        self.assertEqual((code, "macOS and Windows only" in err), (cli.EXIT_USAGE, True))

    def test_linux_is_not_a_choice_for_host_or_the_service(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["serve", "--host", "linux"])
        with self.assertRaisesRegex(ValueError, "mac or windows"):
            service.ServiceManager("linux", home=self.home, env={})


if __name__ == "__main__":
    unittest.main()
