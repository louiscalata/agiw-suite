"""Production main -> real Collector -> real LaneProcesses; only OS and HTTP boundaries are faked."""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes, server, winsys


class ProductionEnrollment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.install = self.root / "install"
        (self.install / "agiw_win").mkdir(parents=True)
        self.share = self.root / "share"
        (self.share / "windows-llm-pipeline").mkdir(parents=True)
        self.specs = {name: {"port": 1235 + i, "alias": name + "-model"}
                      for i, name in enumerate(("fast", "deep"))}
        (self.share / "windows-llm-pipeline" / "config.json").write_text(
            json.dumps({"runtime": {"lanes": self.specs}}))
        self.pins = {name: {"pid": 77 + i, "created": 1000 + i,
                            "image": r"C:\Local\AGIW\llama-server.exe"} for i, name in enumerate(self.specs)}
        self.processes = {pin["pid"]: {**pin, "name": "llama-server.exe", "owner": None,
                                      "cpuTime": 0, "workingSetBytes": 1024} for pin in self.pins.values()}
        self.requests = []
        self.after_request = lambda _port, _path: None

    def run_main(self, *, trusted=True, enrolled=True, extra=()):
        test = self

        class Connection:
            sock = None

            def __init__(self, host, port, **_kwargs):
                test.assertEqual(host, "127.0.0.1")
                self.port = port

            def request(self, method, path, **_kwargs):
                test.assertEqual(method, "GET")
                self.path = path
                test.requests.append((self.port, path))
                test.after_request(self.port, path)

            def getresponse(self):
                lane = next(name for name, spec in test.specs.items() if spec["port"] == self.port)
                payload = {"/health": {"status": "ok"},
                           "/v1/models": {"data": [{"id": test.specs[lane]["alias"]}]},
                           "/slots": [{"id": 0, "is_processing": False}]}[self.path]
                return mock.Mock(status=200, read=lambda _limit: json.dumps(payload).encode())

            def close(self):
                pass

        table = [{"state": winsys.MIB_TCP_STATE_LISTEN, "local": "127.0.0.1", "localPort": spec["port"],
                  "remote": "0.0.0.0", "remotePort": 0, "pid": self.pins[name]["pid"]}
                 for name, spec in self.specs.items()]
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(server, "__file__", str(self.install / "agiw_win" / "server.py")))
            stack.enter_context(mock.patch.object(Path, "home", return_value=self.root))
            stack.enter_context(mock.patch.object(winsys, "IS_WINDOWS", False))
            stack.enter_context(mock.patch.object(probes, "IS_WINDOWS", False))
            reader = stack.enter_context(mock.patch.object(winsys, "_read_local_enrollment",
                return_value=json.dumps({"schema": 1, "lanes": self.pins if enrolled else {}}),
                side_effect=None if trusted else PermissionError("untrusted ACL"), create=True))
            stack.enter_context(mock.patch.object(winsys, "tcp_table", return_value=table))
            stack.enter_context(mock.patch.object(winsys, "process_info", side_effect=lambda pid: self.processes.get(pid)))
            stack.enter_context(mock.patch.object(probes.http.client, "HTTPConnection", Connection))
            stack.enter_context(mock.patch.object(probes.subprocess, "run", side_effect=OSError("OS mocked")))
            stack.enter_context(mock.patch.object(probes, "_memory_status", side_effect=OSError("OS mocked")))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(server.main(["--once", "--share-root", str(self.share), *extra]), 0)
            reader.assert_called_once_with(self.install / "lane-processes.json")
        snapshot = json.loads(output.getvalue())
        self.assertNotIn(self.pins["fast"]["image"], output.getvalue())
        return snapshot

    def test_default_main_wires_private_enrollment_and_probes_both_healthy_lanes(self):
        snapshot = self.run_main()
        self.assertEqual([lane["status"] for lane in snapshot["lanes"]], ["idle", "idle"])
        self.assertEqual(set(self.requests), {(port, endpoint) for port in (1235, 1236)
                                              for endpoint in ("/health", "/v1/models", "/slots")})

    def test_unknown_and_untrusted_enrollment_never_probe(self):
        for options in ({"enrolled": False}, {"trusted": False}):
            with self.subTest(options=options):
                snapshot = self.run_main(**options)
                self.assertEqual([lane["status"] for lane in snapshot["lanes"]], ["unknown", "unknown"])
                self.assertEqual(self.requests, [])

    def test_shared_and_arbitrary_config_cannot_supply_process_trust(self):
        forged = self.share / "observer.json"
        forged.write_text(json.dumps({"laneProcessIdentities": self.pins}))
        self.run_main(enrolled=False, extra=("--config", str(forged)))
        self.assertEqual(self.requests, [])

    def test_pair_wrong_image_reused_pid_and_unreadable_process_fail_closed(self):
        original = dict(self.processes[77])
        for change in ({"owner": "pair"}, {"image": r"C:\Program Files\PAIR\llama-server.exe"},
                       {"image": r"C:\Other\llama-server.exe"}, {"created": 9999}, None):
            with self.subTest(change=change):
                self.processes[77] = {**original, **change} if change is not None else None
                self.requests.clear()
                snapshot = self.run_main()
                self.assertNotIn(snapshot["lanes"][0]["status"], probes.LANE_UP)
                self.assertFalse(any(port == 1235 for port, _ in self.requests))

    def test_replacement_between_endpoints_is_not_probed_until_explicit_reenrollment(self):
        def replace(port, path):
            if port == 1235 and path == "/health":
                self.processes[77]["created"] = 9999
        self.after_request = replace
        snapshot = self.run_main()
        self.assertEqual([path for port, path in self.requests if port == 1235], ["/health"])
        self.assertNotIn(snapshot["lanes"][0]["status"], probes.LANE_UP)
        self.after_request = lambda *_: None
        self.requests.clear()
        self.run_main()
        self.assertFalse(any(port == 1235 for port, _ in self.requests))
        self.pins["fast"]["created"] = 9999
        self.requests.clear()
        self.assertEqual(self.run_main()["lanes"][0]["status"], "idle")


class EnrollmentSchema(unittest.TestCase):
    def test_bad_or_ambiguous_private_file_is_rejected(self):
        pin = {"pid": 77, "created": 1000, "image": r"C:\Local\llama-server.exe"}
        invalid = ["{", '{"schema":1,"schema":1,"lanes":{}}',
                   json.dumps({"schema": True, "lanes": {"fast": pin}}),
                   json.dumps({"schema": 1, "lanes": {"fast": {**pin, "pid": True}}}),
                   json.dumps({"schema": 1, "lanes": {"fast": {**pin, "image": r"\\host\share\server.exe"}}}),
                   json.dumps({"schema": 1, "lanes": {"fast": {**pin, "image": r"C:\Program Files\PAIR\server.exe"}}})]
        for raw in invalid:
            with self.subTest(raw=raw), mock.patch.object(winsys, "_read_local_enrollment", return_value=raw):
                self.assertEqual(winsys.local_lane_identities(Path("unused")), {})


if __name__ == "__main__":
    unittest.main()
