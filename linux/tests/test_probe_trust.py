"""Regression fixtures for private journals and process/listener attribution."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_linux import linsys, probes


def process(pid=101, **changes):
    return {"pid": pid, "name": "llama-server", "exe": probes.WRAPPER_EXE,
            "kind": "gpu-wrapper", "startTimeTicks": 1234, "port": 41000,
            "flags": {"host": "127.0.0.1", "alias": "fixture-model"}, **changes}


def network(owner, port=41000, **changes):
    return {"state": "up", "listeners": [{"state": "listen", "local": "127.0.0.1",
            "localPort": port, "pid": owner["pid"], "startTimeTicks": owner.get("startTimeTicks"),
            "ownershipVerified": True, **changes}]}


class ProbeTrustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def put(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
        return target

    def stat(self, start):
        return "101 (llama-server) S " + "0 " * 18 + f"{start} 0 0\n"

    def fixture_process(self):
        self.put("proc/101/status", f"Name:\tllama-server\nUid:\t{os.getuid()} 0 0 0\n")
        self.put("proc/101/stat", self.stat(1234))
        self.put("proc/101/cmdline", "\0".join((probes.WRAPPER_EXE, "--host", "127.0.0.1", "--port", "41000")) + "\0")
        (self.root / "proc/101/exe").symlink_to(probes.WRAPPER_EXE)

    def test_arbitrary_journal_strings_never_leave_reader(self):
        values = {key: "sk-ant-fixture-secret /private/path prompt text" for key in
                  ("reason", "runId", "epoch", "admission", "pause", "generation", "state", "terminalState")}
        path = self.put("codemode-router/install-fence.json", json.dumps(values))
        result = probes.read_codex_state(self.root, path.stat().st_mtime)
        text = json.dumps(result)
        for secret in ("sk-ant-fixture-secret", "/private/path", "prompt text"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(result["fence"]), {"observedUnix", "ageSeconds"})

    def test_wrapper_without_listener_evidence_makes_no_request(self):
        fetch = Mock()
        result = probes.probe_wrapper([process()], fetch)[0]
        fetch.assert_not_called()
        self.assertEqual(result["health"], "unknown")

    def test_pair_without_listener_evidence_makes_no_request(self):
        fetch = Mock(return_value={"status": "ok", "data": []})
        result = probes.probe_pair(fetch)
        fetch.assert_not_called()
        self.assertEqual(result["health"], "unknown")

    def test_wrong_or_ambiguous_wrapper_owner_makes_no_request(self):
        owner = process()
        for evidence in (network(process(pid=102)), network(owner, startTimeTicks=999),
                         network(owner, ownershipVerified=False),
                         {"state": "unknown", "listeners": network(owner)["listeners"]},
                         {"state": "up", "listeners": network(owner)["listeners"] * 2}):
            with self.subTest(evidence=evidence):
                fetch = Mock()
                result = probes.probe_wrapper([owner], fetch, network=evidence)[0]
                fetch.assert_not_called()
                self.assertEqual((result["health"], result["status"]), ("unknown", "unknown"))

    def test_wrong_pair_owner_makes_no_request(self):
        owner = process(kind="pair-router", port=8081)
        fetch = Mock()
        row = probes.probe_pair(fetch, processes=[owner], network=network(process(pid=102), 8081))
        fetch.assert_not_called()
        self.assertEqual(row["health"], "unknown")

    def test_unstable_process_loses_executable_flags_and_kind(self):
        self.fixture_process()
        original = linsys.read_text
        count = 0
        def read(path, *args):
            nonlocal count
            if Path(path).name == "stat":
                count += 1
                return self.stat(1234 if count == 1 else 9999)
            return original(path, *args)
        with patch.object(linsys, "read_text", side_effect=read):
            row = linsys.read_processes(self.root)[0]
        self.assertIsNone(row["exe"])
        self.assertEqual(row["kind"], "unknown")
        self.assertEqual(row["flags"], {})
        self.assertIsNone(row["port"])
        self.assertIsNone(row["startTimeTicks"])

    def test_pid_reused_before_fd_read_cannot_own_listener(self):
        self.fixture_process()
        self.put("proc/101/stat", self.stat(9999))
        (self.root / "proc/101/fd").mkdir()
        (self.root / "proc/101/fd/3").symlink_to("socket:[100]")
        header = "sl local remote state tx tr retr uid timeout inode\n"
        self.put("proc/net/tcp", header + "0: 0100007F:A028 00000000:0000 0A 0:0 00:0 0 1000 0 100\n")
        self.put("proc/net/tcp6", header)
        row = linsys.read_network(self.root, [process()])
        self.assertIsNone(row["listeners"][0]["pid"])
        self.assertEqual(row["callersState"], "unknown")

    def test_pid_reused_during_fd_read_cannot_own_listener(self):
        self.fixture_process()
        (self.root / "proc/101/fd").mkdir()
        (self.root / "proc/101/fd/3").symlink_to("socket:[100]")
        header = "sl local remote state tx tr retr uid timeout inode\n"
        self.put("proc/net/tcp", header + "0: 0100007F:A028 00000000:0000 0A 0:0 00:0 0 1000 0 100\n")
        self.put("proc/net/tcp6", header)
        original = os.readlink
        def readlink(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            self.put("proc/101/stat", self.stat(9999))
            return value
        with patch.object(linsys.os, "readlink", side_effect=readlink):
            row = linsys.read_network(self.root, [process()])
        self.assertIsNone(row["listeners"][0]["pid"])
        self.assertFalse(row["listeners"][0]["ownershipVerified"])
        self.assertEqual(row["callersState"], "unknown")

    def test_missing_process_start_cannot_retain_probe_authority(self):
        self.fixture_process()
        (self.root / "proc/101/stat").unlink()
        rows = linsys.read_processes(self.root)
        self.assertEqual(rows[0]["kind"], "unknown")
        self.assertIsNone(rows[0]["exe"])
        fetch = Mock()
        self.assertEqual(probes.probe_wrapper(rows, fetch, network=network(process())), [])
        fetch.assert_not_called()

    def test_unobserved_coordinator_installation_is_unknown(self):
        self.assertEqual(probes.coordinator_status([])["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
