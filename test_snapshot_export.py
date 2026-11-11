"""GET /api/snapshot from a real observer process on a free port."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DOCUMENTED_KEYS = {
    "schemaVersion", "host", "observedAt", "sampledAt", "models",
    "localInstanceObservations", "afm", "sources", "pipeline", "onlineCodeMode",
    "windowsWorker", "windowsLive", "windowsJobs", "canary", "nisiV02", "pair",
    "machines", "components",
}

# Field names only. modelKey and similar readings are not credentials.
_FORBIDDEN = {
    "hostname", "token", "apikey", "password", "secret", "authorization",
    "accesstoken", "refreshtoken",
}


def _forbidden(name):
    compact = str(name).lower().replace("_", "").replace("-", "")
    return compact in _FORBIDDEN or compact.endswith("password") or compact.endswith("secret")


def _walk(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if _forbidden(key):
                raise AssertionError(f"snapshot field {key!r} is not part of the export")
            _walk(item)
    elif isinstance(value, list):
        for item in value:
            _walk(item)


class SnapshotExportTest(unittest.TestCase):
    def test_observer_snapshot_is_schema_1_without_secret_fields(self):
        home = tempfile.mkdtemp()
        env = os.environ.copy()
        env["HOME"] = home
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "server.py"), "--port", "0"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            line = process.stdout.readline()
            port = json.loads(line)["port"]
            self.assertIsInstance(port, int)
            self.assertGreater(port, 0)
            url = f"http://127.0.0.1:{port}/api/snapshot"
            deadline = time.monotonic() + 25
            body = None
            while time.monotonic() < deadline:
                request = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{port}"})
                try:
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertEqual(response.status, 200)
                        body = json.loads(response.read().decode())
                        break
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 503)
                    self.assertEqual(json.loads(error.read().decode()), {"status": "starting"})
                    time.sleep(0.2)
            self.assertIsInstance(body, dict, "observer did not publish a snapshot")
            self.assertEqual(body["schemaVersion"], 1)
            self.assertTrue(DOCUMENTED_KEYS <= set(body))
            self.assertNotIn("hostname", body)
            _walk(body)
        finally:
            if process.stdout is not None:
                process.stdout.close()
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
