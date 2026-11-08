"""Transport failures through the real CLI control wrapper, without listeners."""
import errno
import http.client
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from support import forbid_real_commands, pl
from agiw import cli
from support import validate_row


class ControlFailures(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)

    def invoke(self, error, mode):
        lines, stderr = [], io.StringIO()
        argv = ["--state-dir", "unused", "status"]
        if mode != "human":
            argv += ["--json"] + (["--full"] if mode == "full" else [])
        with mock.patch.object(pl, "control_request", side_effect=error) as request, \
                mock.patch.object(pl, "detect_host_kind", return_value="mac"), \
                mock.patch.object(cli.socket, "gethostname", return_value="test-host"), \
                mock.patch("sys.stderr", stderr):
            code = cli.main(argv, out=lines.append)
        request.assert_called_once_with(Path("unused"), pl.STATUS_PORT, "GET", "/control/status", None)
        return code, lines, stderr.getvalue()

    def check_failure(self, error, state, reason):
        for mode in ("human", "json", "full"):
            with self.subTest(error=type(error).__name__, mode=mode):
                code, lines, stderr = self.invoke(error, mode)
                if mode == "human":
                    self.assertEqual(lines, [])
                    if state == "not_running":
                        self.assertEqual(code, cli.EXIT_NOT_RUNNING)
                        self.assertEqual(stderr, "agiw: " + cli.NOT_RUNNING + "\n")
                    else:
                        self.assertEqual(code, cli.EXIT_FAILED)
                        detail = "timeout" if reason == "timeout" else "read error"
                        self.assertEqual(stderr, "agiw: the peer link is running but did not answer: "
                                         + detail + "\n")
                    continue
                self.assertEqual((code, stderr), (0, ""))
                doc = json.loads("\n".join(lines))
                self.assertEqual(doc["schemaVersion"], 2)
                self.assertEqual(doc["contract"], "machine-state/1")
                self.assertEqual(doc["linkState"], state)
                self.assertEqual(doc["self"], {"node": "mac", "name": "test-host", "host": "mac"})
                self.assertEqual(len(doc["machines"]), 2)
                self.assertEqual({row["platform"] for row in doc["machines"]}, {"mac", "windows"})
                for row in doc["machines"]:
                    validate_row(row)
                    self.assertEqual(row["state"], "unknown")
                    self.assertIsNone(row["observedAt"])
                    self.assertIsNone(row["ageSeconds"])
                    self.assertIsNone(row["loadedCount"])
                    self.assertIsNone(row["busyCount"])
                    self.assertEqual(row["reasonCode"], reason if row["platform"] == "mac" else "not_observed")
                    self.assertEqual(row["trust"], "local" if row["platform"] == "mac" else "unverified")
                if mode == "full":
                    self.assertEqual(doc["documents"], {})
                else:
                    self.assertNotIn("documents", doc)

    def test_control_refused_or_missing_state_is_not_running(self):
        for error in (ConnectionRefusedError(), FileNotFoundError("missing control-token"),
                      FileNotFoundError("missing state directory")):
            self.check_failure(error, "not_running", "unavailable")

    def test_control_timeout_is_unreachable(self):
        for error in (TimeoutError("deadline exceeded"), pl.socket.timeout("timed out")):
            self.check_failure(error, "unreachable", "timeout")

    def test_control_reset_is_unreachable(self):
        self.check_failure(ConnectionResetError("connection reset"), "unreachable", "read_error")

    def test_control_invalid_json_is_unreachable(self):
        for error in (ValueError("non-finite number NaN"), json.JSONDecodeError("invalid JSON", "<html>", 0)):
            self.check_failure(error, "unreachable", "read_error")

    def test_control_other_read_errors_are_unreachable(self):
        for error in (OSError(errno.EIO, "read failed"), ConnectionError("disconnected"),
                      http.client.BadStatusLine("bad status"), http.client.IncompleteRead(b"partial")):
            self.check_failure(error, "unreachable", "read_error")

    def test_control_non_object_response_is_unreachable(self):
        for mode in ("human", "json"):
            with self.subTest(mode=mode), mock.patch.object(pl, "control_request", return_value=(200, [])):
                lines, stderr = [], io.StringIO()
                argv = ["--state-dir", "unused", "status"] + (["--json"] if mode == "json" else [])
                with mock.patch("sys.stderr", stderr):
                    code = cli.main(argv, out=lines.append)
                if mode == "json":
                    self.assertEqual(code, 0)
                    self.assertEqual(json.loads("\n".join(lines))["linkState"], "unreachable")
                    self.assertEqual(stderr.getvalue(), "")
                else:
                    self.assertEqual((code, lines), (cli.EXIT_FAILED, []))
                    self.assertIn("the peer link is running but did not answer: read error", stderr.getvalue())

    def test_control_permission_error_is_unchanged(self):
        for mode in ("human", "json", "full"):
            with self.subTest(mode=mode):
                code, lines, stderr = self.invoke(PermissionError("control authentication failed"), mode)
                self.assertEqual((code, lines), (cli.EXIT_FAILED, []))
                self.assertEqual(stderr, "agiw: control authentication failed\n")


if __name__ == "__main__":
    unittest.main()


class WriteOutcomeUnknown(unittest.TestCase):
    """A write with no readable answer may still have been applied; the CLI must not say it failed."""

    def setUp(self):
        forbid_real_commands(self)

    def run_cli(self, argv, **patch):
        lines, stderr = [], io.StringIO()
        with mock.patch.object(pl, "control_request", **patch), mock.patch("sys.stderr", stderr):
            code = cli.main(["--state-dir", "unused", *argv], out=lines.append)
        return code, stderr.getvalue()

    def test_a_write_without_a_readable_answer_says_it_may_have_taken_effect(self):
        for argv in (["pair", "pc"], ["unpair", "pc"]):
            for error in (TimeoutError("deadline exceeded"), ConnectionResetError("reset")):
                with self.subTest(argv=argv, error=type(error).__name__):
                    code, err = self.run_cli(argv, side_effect=error)
                    self.assertNotEqual(code, 0)
                    self.assertIn("may or may not have taken effect", err)
                    self.assertIn("agiw peers", err)

    def test_a_write_with_a_non_object_answer_says_it_may_have_taken_effect(self):
        code, err = self.run_cli(["unpair", "pc"], return_value=(200, []))
        self.assertNotEqual(code, 0)
        self.assertIn("may or may not have taken effect", err)

    def test_a_read_or_a_refused_write_does_not_claim_an_unknown_outcome(self):
        code, err = self.run_cli(["peers"], side_effect=TimeoutError("deadline exceeded"))
        self.assertNotEqual(code, 0)
        self.assertNotIn("may or may not", err)
        code, err = self.run_cli(["unpair", "pc"], side_effect=ConnectionRefusedError())
        self.assertNotEqual(code, 0)
        self.assertNotIn("may or may not", err)

    def test_an_answered_error_is_a_definite_failure(self):
        code, err = self.run_cli(["unpair", "pc"], return_value=(404, {"error": "no such peer"}))
        self.assertNotEqual(code, 0)
        self.assertIn("no such peer", err)
        self.assertNotIn("may or may not", err)
