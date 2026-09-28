"""Exercise the login-agent plist writer without touching launchd or an app."""

import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("login-agent.sh")


def plist_writer():
    source = SCRIPT.read_text()
    return source.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


class LoginAgentConfigTests(unittest.TestCase):
    def run_writer(self, *, hosts=None, username=None, existing_mode=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "InferenceMonitor"
            binary.write_bytes(b"fake binary")
            destination = root / "agent.plist"
            state = root / "state"
            state.mkdir()
            env = dict(os.environ)
            env.pop("AGIW_SHARE_HOSTS", None)
            env.pop("AGIW_SHARE_USERNAME", None)
            if hosts is not None:
                env["AGIW_SHARE_HOSTS"] = hosts
            if username is not None:
                env["AGIW_SHARE_USERNAME"] = username
            result = subprocess.run([sys.executable, "-c", plist_writer(),
                                     str(binary), str(destination), str(state)],
                                    env=env, capture_output=True, text=True, timeout=5)
            if existing_mode is not None and result.returncode == 0:
                destination.chmod(existing_mode)
                result = subprocess.run([sys.executable, "-c", plist_writer(),
                                         str(binary), str(destination), str(state)],
                                        env=env, capture_output=True, text=True, timeout=5)
            payload = plistlib.loads(destination.read_bytes()) if destination.exists() else None
            mode = destination.stat().st_mode & 0o777 if destination.exists() else None
            return result, payload, mode

    def test_explicit_share_settings_survive_in_private_plist(self):
        result, payload, mode = self.run_writer(hosts="10.222.33.10,10.222.33.20,pc.example.invalid",
                                                username="suiteuser")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["EnvironmentVariables"], {
            "AGIW_SHARE_HOSTS": "10.222.33.10,10.222.33.20,pc.example.invalid",
            "AGIW_SHARE_USERNAME": "suiteuser",
        })
        self.assertEqual(mode, 0o600)

    def test_absent_settings_do_not_create_environment_fields(self):
        result, payload, mode = self.run_writer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("EnvironmentVariables", payload)
        self.assertEqual(mode, 0o600)

    def test_identical_existing_plist_is_tightened_to_owner_only(self):
        result, payload, mode = self.run_writer(hosts="10.222.33.10", username="suiteuser",
                                                existing_mode=0o644)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["EnvironmentVariables"]["AGIW_SHARE_USERNAME"], "suiteuser")
        self.assertEqual(mode, 0o600)

    def test_fifo_at_existing_plist_path_refuses_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "InferenceMonitor"
            binary.write_bytes(b"fake binary")
            destination = root / "agent.plist"
            os.mkfifo(destination)
            state = root / "state"
            state.mkdir()
            result = subprocess.run([sys.executable, "-c", plist_writer(),
                                     str(binary), str(destination), str(state)],
                                    capture_output=True, text=True, timeout=2)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe login agent", result.stderr)

    def test_multiply_linked_existing_plist_refuses_without_changing_other_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "InferenceMonitor"
            binary.write_bytes(b"fake binary")
            destination = root / "agent.plist"
            other = root / "unrelated-hardlink"
            state = root / "state"
            state.mkdir()
            args = [sys.executable, "-c", plist_writer(), str(binary), str(destination), str(state)]
            first = subprocess.run(args, capture_output=True, text=True, timeout=2)
            self.assertEqual(first.returncode, 0, first.stderr)
            os.link(destination, other)
            destination.chmod(0o644)
            second = subprocess.run(args, capture_output=True, text=True, timeout=2)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("unsafe login agent", second.stderr)
            self.assertEqual(other.stat().st_mode & 0o777, 0o644)

    def test_partial_settings_refuse_before_writing_plist(self):
        for hosts, username in (("10.222.33.10", None), (None, "suiteuser")):
            result, payload, mode = self.run_writer(hosts=hosts, username=username)
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(payload)
            self.assertIsNone(mode)


if __name__ == "__main__":
    unittest.main()
