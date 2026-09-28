import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

import nisi_v02


COMMIT = "a1e3ca5aa42ddf65c8314b3a1de5f22ff069a71f"
NOW = dt.datetime(2026, 9, 25, 12, tzinfo=dt.timezone.utc).timestamp()


def write(path: Path, contents: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    path.chmod(0o600)
    return hashlib.sha256(contents).hexdigest()


def fixture(home: Path) -> tuple[Path, Path, Path]:
    base = home / ".local/share/nisi-runtime"
    runtime = base / COMMIT
    archive_hash = write(base / f"{COMMIT}.tar", b"private test archive")
    runtime_files = {
        "package.json": json.dumps({"name": "nisi", "version": "0.2.0-private.0", "private": True}).encode(),
        "workflow/contracts.mjs": b"contracts fixture",
        "adapters/local-chat.mjs": b"local fixture",
        "hosts/local-models/opencode-orchestration-v1.mjs": b"host fixture",
    }
    pins = {name: write(runtime / name, content) for name, content in runtime_files.items()}
    write(runtime / "INSTALL-MANIFEST.json", json.dumps({"commit": COMMIT, "archiveSha256": archive_hash,
                                                        "files": pins}).encode())
    hosts = {}
    for name, relative in nisi_v02._HOST_FILES.items():
        path = home / relative
        hosts[name] = {"installedPath": str(path), "afterSha256": write(path, name.encode())}
    receipt = base / f"rollback-{COMMIT[:8]}-fixture/receipt.json"
    write(receipt, json.dumps({"commit": COMMIT, "runtimeRoot": str(runtime),
                               "runtimeArchiveSha256": archive_hash, "status": "VERIFIED",
                               "verifiedAt": "2026-09-24T12:00:00Z", "routedAt": "2026-09-24T11:55:00Z",
                               "files": hosts, "verification": {
                                   "work": {"status": "RESPONSE_VALIDATED", "prompt": "SECRET"},
                                   "validation": {"contract": "PASS", "syntax": "PASS", "tests": "NOT_RUN",
                                                  "certification": "NOT_RUN", "accepted": False,
                                                  "candidate": "SECRET"}}}).encode())
    return runtime, receipt, base


def listed_model(runtime_id="opencode.local", model_id="m"):
    return {"runtimeId": runtime_id, "modelId": model_id, "displayName": "Model",
            "type": "llm", "sizeBytes": 123, "reportedDigest": None,
            "presence": "PROVIDER_LISTED", "loadedState": "REPORTED_NOT_LOADED",
            "instanceIds": [], "locality": "NOT_VERIFIED", "inference": "NOT_TESTED",
            "license": "NOT_REVIEWED", "providerFee": "UNKNOWN", "useAuthorization": "NONE"}


def runtime_row(runtime_id="opencode.local", status="LISTED", models=None):
    return {"runtimeId": runtime_id, "provider": "lmstudio-v1",
            "endpoint": "http://127.0.0.1:1234/api/v1/models", "status": status,
            "code": None if status == "LISTED" else "INVENTORY_UNAVAILABLE",
            "responseSha256": "a" * 64 if status == "LISTED" else None,
            "models": [listed_model(runtime_id)] if models is None and status == "LISTED"
            else [] if models is None else models}


def bridge_envelope(rows, status="LISTED"):
    return {"kind": "codemode.nisi.bridge.v1", "operation": "status", "status": "RETURNED",
            "recoveryRequired": False,
            "result": {"schemaVersion": 1, "scope": "REGISTERED_RUNTIME_INVENTORY_ONLY",
                       "status": status, "runtimes": rows}}


class NisiV02ProjectionTests(unittest.TestCase):
    def test_valid_integrity_and_historical_probe_stay_separate_from_live_status(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fixture(home)
            item, source = nisi_v02.collect_nisi_v02(NOW, home=home)
            self.assertEqual(item["commit"], COMMIT)
            self.assertEqual(item["version"], "0.2.0-private.0")
            self.assertEqual(item["runtimeIntegrity"], "VERIFIED")
            self.assertEqual(item["hostBinding"], "VERIFIED")
            self.assertEqual(item["activationProbe"]["workStatus"], "RESPONSE_VALIDATED")
            self.assertFalse(item["activationProbe"]["accepted"])
            self.assertEqual(item["activationProbe"]["tests"], "NOT_RUN")
            self.assertEqual(item["liveInference"], "UNKNOWN")
            self.assertEqual(item["workflowAcceptance"], "UNKNOWN")
            self.assertEqual(item["releaseAcceptance"], "NOT_ESTABLISHED")
            self.assertEqual(source["state"], "recorded")
            self.assertGreater(item["activationAgeSeconds"], 0)
            self.assertNotIn("SECRET", json.dumps([item, source]))
            self.assertNotIn(str(home), json.dumps([item, source]))

    def test_changed_host_bridge_is_not_called_current_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fixture(home)
            target = home / nisi_v02._HOST_FILES["pipeline_integrations.py"]
            target.write_bytes(b"a later owner change")
            item, source = nisi_v02.collect_nisi_v02(NOW, home=home)
            self.assertEqual(item["runtimeIntegrity"], "VERIFIED")
            self.assertEqual(item["hostBinding"], "DRIFT")
            self.assertEqual(item["driftedHostFiles"], ["pipeline_integrations.py"])
            self.assertEqual(source["state"], "error")

    def test_runtime_pin_drift_refuses_runtime_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            runtime, _, _ = fixture(home)
            (runtime / "workflow/contracts.mjs").write_bytes(b"tampered")
            item, source = nisi_v02.collect_nisi_v02(NOW, home=home)
            self.assertEqual(item["runtimeIntegrity"], "UNKNOWN")
            self.assertEqual(item["hostBinding"], "UNKNOWN")
            self.assertEqual(source["state"], "error")

    def test_symlinked_runtime_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            runtime, _, _ = fixture(home)
            target = runtime / "workflow/contracts.mjs"
            replacement = runtime / "other.mjs"
            write(replacement, target.read_bytes())
            target.unlink()
            target.symlink_to(replacement)
            item, source = nisi_v02.collect_nisi_v02(NOW, home=home)
            self.assertEqual(item["runtimeIntegrity"], "UNKNOWN")
            self.assertEqual(source["state"], "error")

    def test_missing_or_malformed_activation_never_claims_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            item, source = nisi_v02.collect_nisi_v02(NOW, home=home)
            self.assertEqual(item["hostBinding"], "UNKNOWN")
            self.assertEqual(source["state"], "unavailable")
            _, receipt, _ = fixture(home)
            receipt.write_text('{"status":"VERIFIED","status":"VERIFIED"}')
            item, source = nisi_v02.collect_nisi_v02(NOW, home=home)
            self.assertEqual(item["runtimeIntegrity"], "UNKNOWN")
            self.assertEqual(source["state"], "error")

    def test_a_host_parent_symlink_cannot_satisfy_a_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fixture(home)
            directory = home / ".config/opencode"
            moved = home / "moved-opencode"
            directory.rename(moved)
            directory.symlink_to(moved)
            item, source = nisi_v02.collect_nisi_v02(NOW, home=home)
            self.assertEqual(item["hostBinding"], "DRIFT")
            self.assertEqual(item["driftedHostFiles"], ["nisi_local.ts"])
            self.assertEqual(source["state"], "error")

    def test_explicit_bridge_status_projects_only_inventory_and_no_inference(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fixture(home)
            node = home / "node"
            write(node, b"synthetic node executable")
            node.chmod(0o700)
            envelope = bridge_envelope([runtime_row()])
            with mock.patch.object(nisi_v02, "_bounded_process", return_value=(0, json.dumps(envelope).encode())) as run:
                result = nisi_v02.probe_nisi_v02_bridge(NOW, home=home, node_binary=node)
            self.assertEqual(result["status"], "RETURNED")
            self.assertEqual(result["inventoryStatus"], "LISTED")
            self.assertEqual(result["runtimeCount"], 1)
            self.assertEqual(result["modelCount"], 1)
            self.assertEqual(result["modelInference"], "NOT_RUN")
            self.assertEqual(result["workflowAcceptance"], "NOT_RUN")
            self.assertEqual(result["releaseAcceptance"], "NOT_ESTABLISHED")
            self.assertEqual(run.call_args.args[0][-1], "status")
            self.assertEqual(run.call_args.args[0][1], str(home / nisi_v02._HOST_FILES["nisi_bridge.mjs"]))

    def test_listed_aggregate_with_unavailable_or_not_run_row_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fixture(home)
            node = home / "node"
            write(node, b"synthetic node executable")
            node.chmod(0o700)
            for state in ("UNAVAILABLE", "NOT_RUN"):
                rows = [runtime_row(), runtime_row("other.runtime", state)]
                with mock.patch.object(nisi_v02, "_bounded_process",
                                       return_value=(0, json.dumps(bridge_envelope(rows)).encode())):
                    result = nisi_v02.probe_nisi_v02_bridge(NOW, home=home, node_binary=node)
                self.assertEqual(result["status"], "UNAVAILABLE")
                self.assertEqual(result["inventoryStatus"], "UNKNOWN")
                self.assertIsNone(result["modelCount"])

    def test_malformed_or_cross_runtime_model_entries_are_never_counted(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fixture(home)
            node = home / "node"
            write(node, b"synthetic node executable")
            node.chmod(0o700)
            wrong_runtime = listed_model("other.runtime")
            invalid_size = listed_model()
            invalid_size["sizeBytes"] = True
            duplicate = [listed_model(), listed_model()]
            for models in ([{}], [wrong_runtime], [invalid_size], duplicate):
                with mock.patch.object(nisi_v02, "_bounded_process",
                                       return_value=(0, json.dumps(bridge_envelope([runtime_row(models=models)])).encode())):
                    result = nisi_v02.probe_nisi_v02_bridge(NOW, home=home, node_binary=node)
                self.assertEqual(result["status"], "UNAVAILABLE")
                self.assertIsNone(result["modelCount"])

    def test_explicit_probe_refuses_drifted_bridge_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fixture(home)
            (home / nisi_v02._HOST_FILES["nisi_bridge.mjs"]).write_bytes(b"changed")
            with mock.patch.object(nisi_v02, "_bounded_process") as run:
                result = nisi_v02.probe_nisi_v02_bridge(NOW, home=home)
            self.assertEqual(result["status"], "REFUSED")
            self.assertEqual(result["activationHostBinding"], "DRIFT")
            self.assertEqual(result["driftedHostFiles"], ["nisi_bridge.mjs"])
            run.assert_not_called()

    def test_explicit_probe_rejects_invalid_or_duplicate_status_envelopes(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fixture(home)
            node = home / "node"
            write(node, b"synthetic node executable")
            node.chmod(0o700)
            for raw in (b'{"kind":"codemode.nisi.bridge.v1","operation":"work"}',
                        b'{"kind":"codemode.nisi.bridge.v1","kind":"codemode.nisi.bridge.v1"}'):
                with mock.patch.object(nisi_v02, "_bounded_process", return_value=(0, raw)):
                    result = nisi_v02.probe_nisi_v02_bridge(NOW, home=home, node_binary=node)
                self.assertEqual(result["status"], "UNAVAILABLE")
                self.assertEqual(result["modelInference"], "NOT_RUN")

    def test_status_child_has_time_and_output_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with self.assertRaisesRegex(nisi_v02._InvalidEvidence, "PROBE_TIMEOUT"):
                nisi_v02._bounded_process([sys.executable, "-c", "import time; time.sleep(2)"],
                                          home, 0.1, 1024)
            with self.assertRaisesRegex(nisi_v02._InvalidEvidence, "PROBE_OUTPUT_LIMIT"):
                nisi_v02._bounded_process([sys.executable, "-c", "print('x'*4096)"],
                                          home, 1, 128)

    def test_status_timeout_kills_descendant_after_leader_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            marker, pid_file = home / "heartbeat", home / "child-pid"
            child = ("from pathlib import Path\nimport sys,time\nn=0\n"
                     "while True:\n Path(sys.argv[1]).write_text(str(n)); n+=1; time.sleep(.02)\n")
            parent = ("from pathlib import Path\nimport subprocess,sys,time\n"
                      f"child={child!r}\n"
                      "p=subprocess.Popen([sys.executable,'-c',child,sys.argv[1]],"
                      "stdout=sys.stdout,stderr=subprocess.DEVNULL)\n"
                      "Path(sys.argv[2]).write_text(str(p.pid))\n"
                      "deadline=time.monotonic()+.3\n"
                      "while not Path(sys.argv[1]).exists() and time.monotonic()<deadline: time.sleep(.01)\n")
            child_pid = None
            try:
                with self.assertRaisesRegex(nisi_v02._InvalidEvidence, "PROBE_TIMEOUT"):
                    nisi_v02._bounded_process([sys.executable, "-c", parent, str(marker), str(pid_file)],
                                              home, .5, 1024)
                self.assertTrue(marker.exists())
                child_pid = int(pid_file.read_text())
                before = marker.read_text()
                time.sleep(.15)
                self.assertEqual(marker.read_text(), before)
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
