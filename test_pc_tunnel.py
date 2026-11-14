import json
from pathlib import Path
import tempfile
import threading
import unittest

import pc_tunnel

# There are no built-in forwards. This isolated fixture is the retired layout.
FIXTURE_FORWARDS = (
    {"name": "router", "localPort": 1334, "remotePort": 1234, "id": "pc:router"},
    {"name": "gpt-oss-20b", "localPort": 1345, "remotePort": 1245, "id": "pc:gpt-oss-20b"},
    {"name": "qwen3.8-27b", "localPort": 1336, "remotePort": 1236, "id": "pc:qwen3.8-27b"},
)


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
                  "forwards": [dict(item) for item in FIXTURE_FORWARDS]}
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

    def test_environment_overrides_file_and_file_overrides_defaults(self):
        forwards = [{"name": "only", "localPort": 2001, "remotePort": 3001}]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pc-tunnel.json"
            path.write_text(json.dumps({"user": "file-user", "host": "file.example",
                                        "identityFile": "/keys/file", "forwards": forwards}),
                            encoding="utf-8")
            # File beats defaults: nothing in the environment.
            from_file = pc_tunnel.load_config(path, environ={})
            self.assertNotIn("error", from_file)
            self.assertEqual((from_file["user"], from_file["host"], from_file["identityFile"]),
                             ("file-user", "file.example", "/keys/file"))
            self.assertEqual(from_file["forwards"],
                             [{"name": "only", "localPort": 2001, "remotePort": 3001, "id": "pc:only"}])
            # Empty environment values are ignored, so they do not erase the file.
            blank = pc_tunnel.load_config(path, environ={"AGIW_PC_SSH_USER": "", "AGIW_PC_SSH_HOST": "",
                                                          "AGIW_PC_SSH_IDENTITY": ""})
            self.assertEqual(blank, from_file)
            # Environment beats file for every overridable field.
            both = pc_tunnel.load_config(path, environ={"AGIW_PC_SSH_USER": "env-user",
                                                        "AGIW_PC_SSH_HOST": "env.example",
                                                        "AGIW_PC_SSH_IDENTITY": "/keys/env"})
            self.assertNotIn("error", both)
            self.assertEqual((both["user"], both["host"], both["identityFile"]),
                             ("env-user", "env.example", "/keys/env"))
            self.assertEqual(both["forwards"], from_file["forwards"])
            self.assertEqual(pc_tunnel.ssh_command(both)[-1], "env-user@env.example")
            # Each field overrides on its own and leaves the other file values alone.
            for key, field, value in (("AGIW_PC_SSH_USER", "user", "env-user"),
                                      ("AGIW_PC_SSH_HOST", "host", "env.example"),
                                      ("AGIW_PC_SSH_IDENTITY", "identityFile", "/keys/env")):
                one = pc_tunnel.load_config(path, environ={key: value})
                self.assertNotIn("error", one)
                for name in ("user", "host", "identityFile"):
                    self.assertEqual(one[name], value if name == field else from_file[name])
            # The file path itself comes from the environment and still loses to env values.
            via_env = pc_tunnel.load_config(environ={"AGIW_PC_SSH_CONFIG": str(path),
                                                     "AGIW_PC_SSH_HOST": "env.example"})
            self.assertEqual((via_env["user"], via_env["host"]), ("file-user", "env.example"))
        # Without a file the environment still fills the fields; there are no default forwards.
        env_only = pc_tunnel.load_config(environ={"AGIW_PC_SSH_USER": "env-user",
                                                  "AGIW_PC_SSH_HOST": "env.example",
                                                  "AGIW_PC_SSH_IDENTITY": "/keys/env"})
        self.assertNotIn("error", env_only)
        self.assertEqual(env_only["forwards"], [])

    def test_invalid_environment_value_is_rejected_like_an_invalid_file_value(self):
        bad = {"AGIW_PC_SSH_USER": "bad user", "AGIW_PC_SSH_HOST": "bad host/x",
               "AGIW_PC_SSH_IDENTITY": "keys/relative"}
        field_for = {"AGIW_PC_SSH_USER": "user", "AGIW_PC_SSH_HOST": "host",
                     "AGIW_PC_SSH_IDENTITY": "identityFile"}
        good = {"user": "file-user", "host": "file.example", "identityFile": "/keys/file"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pc-tunnel.json"
            for key, value in bad.items():
                # The same value in the file is flagged.
                path.write_text(json.dumps({**good, field_for[key]: value}), encoding="utf-8")
                self.assertEqual(pc_tunnel.load_config(path, environ={})["error"], "config_invalid")
                # In the environment it is flagged with no file, and a valid file value
                # cannot hide it because the environment is applied last.
                self.assertEqual(pc_tunnel.load_config(environ={key: value})["error"], "config_invalid")
                path.write_text(json.dumps(good), encoding="utf-8")
                invalid = pc_tunnel.load_config(path, environ={key: value})
                self.assertEqual(invalid["error"], "config_invalid")
                self.assertFalse(pc_tunnel.runnable(invalid))
                with self.assertRaises(ValueError):
                    pc_tunnel.ssh_command(invalid)

    def test_supervisor_stops_the_forward_it_started(self):
        config = {"user": "pc-user", "host": "pc.example", "identityFile": "/keys/restricted",
                  "forwards": [dict(item) for item in FIXTURE_FORWARDS]}
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
