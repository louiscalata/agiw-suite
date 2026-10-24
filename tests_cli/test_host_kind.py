"""peer_link.py's host-kind default: Linux is no longer served as a Mac; Mac and Windows are unchanged."""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from support import pl, temp_dir


class _Stop(Exception):
    pass


class Detection(unittest.TestCase):
    def test_each_platform_maps_to_its_kind(self):
        self.assertEqual(pl.detect_host_kind("nt", "win32"), "windows")
        self.assertEqual(pl.detect_host_kind("posix", "linux"), "linux")
        self.assertEqual(pl.detect_host_kind("posix", "darwin"), "mac")

    def test_mac_and_windows_match_the_previous_default(self):
        # The previous default was `"windows" if os.name == "nt" else "mac"` for every platform but Linux.
        for os_name, platform in (("nt", "win32"), ("nt", "cygwin"), ("posix", "darwin"), ("posix", "freebsd14")):
            previous = "windows" if os_name == "nt" else "mac"
            self.assertEqual(pl.detect_host_kind(os_name, platform), previous, (os_name, platform))

    def test_monkeypatched_platform_is_read_at_call_time(self):
        with mock.patch.object(pl.sys, "platform", "linux"):
            self.assertEqual(pl.detect_host_kind(os_name="posix"), "linux")
        with mock.patch.object(pl.sys, "platform", "darwin"):
            self.assertEqual(pl.detect_host_kind(os_name="posix"), "mac")
        with mock.patch.object(pl.sys, "platform", "win32"):
            self.assertEqual(pl.detect_host_kind(os_name="nt"), "windows")

    def test_this_runner_is_detected_natively(self):
        expected = "windows" if os.name == "nt" else "linux" if sys.platform.startswith("linux") else "mac"
        self.assertEqual(pl.detect_host_kind(), expected)


class RunDefaults(unittest.TestCase):
    """`peer_link.py run` with no --host: the detected kind picks the host and the status source."""

    def setUp(self):
        self.state = temp_dir(self) / "state"
        self.created = []

        def recorder(state_dir, host, name, provider, **kw):
            self.created.append({"host": host, "provider": provider, **kw})
            raise _Stop()

        for target, value in (("PeerLink", recorder), ("directed_broadcasts", lambda: ())):
            patcher = mock.patch.object(pl, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, kind, *extra):
        with mock.patch.object(pl, "detect_host_kind", return_value=kind):
            with self.assertRaises(_Stop):
                pl.main(["--state-dir", str(self.state), "run", *extra])
        return self.created[-1]

    def provider_name(self, provider):
        return provider.__qualname__.split(".")[0]

    def test_mac_default_still_serves_lm_studio(self):
        made = self.run_main("mac")
        self.assertEqual(made["host"], "mac")
        self.assertEqual(self.provider_name(made["provider"]), "lmstudio_status")

    def test_windows_default_still_requires_an_explicit_status_source(self):
        with mock.patch.object(pl, "detect_host_kind", return_value="windows"), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as stop:
                pl.main(["--state-dir", str(self.state), "run"])
        self.assertEqual(stop.exception.code, 2)
        self.assertEqual(self.created, [])

    def test_windows_with_a_status_file_is_unchanged(self):
        made = self.run_main("windows", "--status-file", str(self.state / "s.json"))
        self.assertEqual((made["host"], self.provider_name(made["provider"])), ("windows", "file_status"))

    def test_linux_default_is_linux_with_the_linux_source(self):
        made = self.run_main("linux")
        self.assertEqual(made["host"], "linux")
        self.assertEqual(self.provider_name(made["provider"]), "linux_status")

    def test_explicit_flags_keep_their_meaning(self):
        made = self.run_main("linux", "--host", "mac")
        self.assertEqual((made["host"], self.provider_name(made["provider"])), ("mac", "lmstudio_status"))
        made = self.run_main("linux", "--lmstudio", "http://127.0.0.1:1234")
        self.assertEqual((made["host"], self.provider_name(made["provider"])), ("linux", "lmstudio_status"))
        made = self.run_main("mac", "--host", "linux")
        self.assertEqual((made["host"], self.provider_name(made["provider"])), ("linux", "linux_status"))


class LinuxStatusSource(unittest.TestCase):
    def setUp(self):
        self.base = temp_dir(self)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_observer_file_location_follows_xdg_state_home(self):
        self.assertEqual(pl.linux_status_file(), self.base / "agiw" / "linux-status.json")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative/ignored"}):
            self.assertEqual(pl.linux_status_file(), Path.home() / ".local" / "state" / "agiw" / "linux-status.json")

    def test_without_an_observer_the_minimal_status_is_valid_and_says_unknown(self):
        doc = pl.linux_status()()
        checked = pl.project_status(pl.validate_status(doc, "linux", time.time()))
        self.assertEqual((checked["platform"], checked["health"], checked["models"]), ("linux", "unknown", []))
        self.assertIs(checked["loadedListComplete"], False)
        with self.assertRaises(ValueError):
            pl.validate_status(doc, "mac", time.time())  # never passes as another kind

    def test_a_fresh_observer_file_is_served_and_a_stale_one_refused(self):
        path = self.base / "agiw" / "linux-status.json"
        path.parent.mkdir(parents=True)
        observed = {"schemaVersion": 1, "kind": "agiw-peer", "host": "node", "platform": "linux", "edition": "linux",
                    "observedAt": pl._iso(time.time()), "health": "ok", "producer": "agiw-linux-status",
                    "models": [{"id": "m", "loadedState": "loaded", "host": "linux"}]}
        path.write_text(json.dumps(observed))
        provider = pl.linux_status()
        self.assertEqual(provider()["producer"], "agiw-linux-status")
        old = time.time() - 120
        os.utime(path, (old, old))
        with self.assertRaises(ValueError):
            provider()  # a stopped observer answers 503, never with old data
        path.unlink()
        self.assertEqual(provider()["producer"], "agiw-peer-link minimal")


class ServeWrapper(unittest.TestCase):
    """`agiw serve` is `peer_link.py run` with the same options."""

    def test_serve_hands_its_options_to_peer_link_run(self):
        from agiw import cli
        state = temp_dir(self) / "S"
        with mock.patch.object(pl, "main", return_value=0) as run:
            code = cli.main(["--state-dir", str(state), "--status-port", "9000", "serve", "--host", "linux", "--name", "lab"],
                            out=lambda line: None)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args[0][0], ["--state-dir", str(state), "--status-port", "9000", "run", "--beacon-port",
                                               "8769", "--host", "linux", "--name", "lab"])

    def test_windows_without_a_status_file_is_refused_in_agiw_words(self):
        from agiw import cli
        with mock.patch.object(pl, "main") as run, mock.patch.object(pl, "detect_host_kind", return_value="windows"), \
                mock.patch("sys.stderr"):
            code = cli.main(["--state-dir", "S", "serve"], out=lambda line: None)
        self.assertEqual(code, 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
