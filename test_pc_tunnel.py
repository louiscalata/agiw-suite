import json
from pathlib import Path
import tempfile
import threading
import unittest

import pc_tunnel


class _Proc:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


class TunnelTests(unittest.TestCase):
    def test_missing_host_is_down_and_does_not_build_a_command(self):
        config = pc_tunnel.load_config(environ={})
        status, rows = pc_tunnel.targets(config, lambda _port: False)
        self.assertEqual(status["reason"], "host_unconfigured")
        self.assertTrue(all(row["base"] is None for row in rows))

        def spawn(_argv):
            raise AssertionError("spawn")

        self.assertEqual(pc_tunnel.supervise(config, threading.Event(), spawn=spawn)["reason"],
                         "host_unconfigured")
        with self.assertRaises(ValueError):
            pc_tunnel.ssh_command(config)

    def test_closed_forwards_are_tunnel_down_and_open_forwards_are_probed(self):
        config = {"user": "pc-user", "host": "pc.example", "identityFile": "/keys/restricted",
                  "forwards": [dict(item) for item in pc_tunnel.DEFAULT_FORWARDS]}
        status, rows = pc_tunnel.targets(config, lambda port: port == 1345)
        self.assertEqual(status["state"], "down")
        self.assertEqual(status["reason"], "tunnel_down")
        self.assertIn("1334", status["detail"])
        self.assertNotIn("/keys/restricted", status["detail"])
        by_id = {row["id"]: row["base"] for row in rows}
        self.assertIsNone(by_id["pc:router"])
        self.assertEqual(by_id["pc:gpt-oss-20b"], "http://127.0.0.1:1345")
        argv = pc_tunnel.ssh_command(config)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-N", argv)
        self.assertIn("127.0.0.1:1334:127.0.0.1:1234", argv)
        self.assertEqual(argv[-1], "pc-user@pc.example")
        self.assertTrue(all("@" not in arg for arg in argv[:-1]))

    def test_invalid_file_and_relative_identity_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pc-tunnel.json"
            path.write_text("{", encoding="utf-8")
            broken = pc_tunnel.load_config(path, environ={})
            self.assertEqual(broken["error"], "config_invalid")
            path.write_text(json.dumps({"user": "pc-user", "host": "pc.example",
                                        "identityFile": "keys/restricted"}), encoding="utf-8")
            relative = pc_tunnel.load_config(path, environ={})
            self.assertEqual(relative["error"], "config_invalid")

    def test_supervisor_stops_the_forward_it_started(self):
        config = {"user": "pc-user", "host": "pc.example", "identityFile": "/keys/restricted",
                  "forwards": [dict(item) for item in pc_tunnel.DEFAULT_FORWARDS]}
        stop = threading.Event()
        started = []

        def spawn(argv):
            started.append(argv)
            stop.set()
            return _Proc()

        result = pc_tunnel.supervise(config, stop, spawn=spawn, pause=0)
        self.assertEqual(result["reason"], "supervisor_stopped")
        self.assertEqual(len(started), 1)
        self.assertTrue(started[0][-1].endswith("@pc.example"))


if __name__ == "__main__":
    unittest.main()
