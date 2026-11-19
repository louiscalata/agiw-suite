"""telemetry imports and degrades safely where fcntl does not exist (LOU-97, Windows)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# Runs in a fresh interpreter so this module's own telemetry import is untouched.
_SCRIPT = """
import json, sys
from pathlib import Path
sys.modules["fcntl"] = None  # importing fcntl now raises ImportError, as on Windows
try:
    import fcntl
except ImportError:
    pass
else:
    raise SystemExit("fcntl import did not fail")
import telemetry
lock = Path(sys.argv[1])
print(json.dumps({
    "fcntl": telemetry.fcntl is None,
    "probe": telemetry._probe_lock(lock),
    "drain": telemetry._router_drain_probe(lock),
}))
"""


class TelemetryWithoutFcntlTests(unittest.TestCase):
    def test_import_succeeds_and_lock_probes_report_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "owner.lock"
            lock.write_text("")
            lock.chmod(0o600)
            result = subprocess.run(
                [sys.executable, "-B", "-c", _SCRIPT, str(lock)],
                cwd=_HERE,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(observed, {"fcntl": True, "probe": "unknown", "drain": "unknown"})


if __name__ == "__main__":
    unittest.main()
