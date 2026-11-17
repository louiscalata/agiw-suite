"""Observe program naming and banner flush order beyond the CLI byte corpus."""

import io
import unittest

import peer_link
from test_peer_link_main_golden import observe


class RecordingStream(io.StringIO):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def flush(self):
        self.trace.append(["stdout.flush"])
        super().flush()


class MainObserverTests(unittest.TestCase):
    def test_help_uses_protocol_program_name_for_other_executable(self):
        for argv_none in (False, True):
            with self.subTest(argv_none=argv_none):
                result = observe(peer_link, {"argv": ["--help"], "argv_none": argv_none}, argv0="different-entry.py")
                self.assertEqual(result["exit_code"], 0)
                self.assertTrue(result["stdout"].startswith("usage: peer_link "))
                self.assertNotIn("different-entry.py", result["stdout"])
                self.assertEqual(result["stderr"], "")

    def test_run_flushes_banner_before_sleep(self):
        result = observe(peer_link, {"argv": ["run", "--host", "mac"]}, stdout_factory=RecordingStream)
        self.assertEqual(result["outcome"], {"returned": 0})
        self.assertEqual(result["trace"].count(["stdout.flush"]), 1)
        self.assertLess(result["trace"].index(["stdout.flush"]), result["trace"].index(["sleep", 3600]))
        self.assertTrue(result["stdout"].endswith("2 paired\n"))
