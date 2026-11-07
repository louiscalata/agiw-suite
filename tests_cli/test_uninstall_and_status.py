"""`agiw uninstall --purge-keys` is a dry run unless --yes; `agiw status` maps peer rows to up/down/unknown."""
import os
import unittest
from unittest import mock

from support import FakeRunner, forbid_real_commands, isolate_windows_registry, temp_dir

from agiw import cli

NODE = "0f8e2d4c-1a2b-4c3d-8e9f-0a1b2c3d4e5f"


class Purge(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        self.registry = isolate_windows_registry(self)
        self.registry["kept"] = "x"
        self.home = temp_dir(self, "agiw-purge-home-")
        self.state = self.home / "state"
        (self.state / "trusted").mkdir(parents=True)
        self.ours = [self.state / "identity.json", self.state / "control-token", self.state / "peers.json",
                     self.state / "trusted" / f"{NODE}.json", self.state / "trusted" / f"{NODE}.json.quarantined-17"]
        for path in self.ours:
            path.write_text("{}")
        running = mock.patch.object(cli, "peer_link_answers", return_value=False)
        self.answers = running.start()
        self.addCleanup(running.stop)

    def run_cli(self, *argv):
        lines = []
        code = cli.main(["--state-dir", str(self.state), "uninstall", *argv], out=lines.append,
                        runner=FakeRunner({"bootout": (113, ""), "print": (113, "")}),
                        kind="mac", home=self.home, env={})
        return code, "\n".join(lines)

    def test_without_purge_keys_nothing_is_listed_or_deleted(self):
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("pairing keys kept", text)
        self.assertTrue(all(p.exists() for p in self.ours))

    def test_purge_keys_alone_is_a_dry_run(self):
        code, text = self.run_cli("--purge-keys")
        self.assertEqual(code, 0)
        self.assertIn("keys: dry run, no key files were deleted", text)
        self.assertEqual(text.count("would delete: ~"), len(self.ours))
        self.assertTrue(all(p.exists() for p in self.ours))
        self.assertIn("kept", self.registry)
        self.assertNotIn(str(self.home), text)

    def test_yes_deletes_only_what_agiw_created(self):
        stranger = self.state / "notes.txt"
        stranger.write_text("mine")
        code, text = self.run_cli("--purge-keys", "--yes")
        self.assertEqual(code, 0, text)
        self.assertFalse(any(p.exists() for p in self.ours))
        self.assertTrue(stranger.exists())
        self.assertIn("left in place (not created by agiw)", text)
        self.assertFalse((self.state / "trusted").exists())
        self.assertTrue(self.state.exists())  # not empty, so kept
        if os.name == "nt":
            self.assertEqual(self.registry, {})

    def test_yes_with_an_empty_folder_removes_it(self):
        self.run_cli("--purge-keys", "--yes")
        self.assertFalse(self.state.exists())

    def test_refuses_while_the_peer_link_answers(self):
        self.answers.return_value = True
        with mock.patch("sys.stderr"):
            code, _ = self.run_cli("--purge-keys", "--yes")
        self.assertEqual(code, 1)
        self.assertTrue(all(p.exists() for p in self.ours))

    def test_a_linked_state_folder_is_refused(self):
        link = self.home / "linked"
        try:
            os.symlink(self.state, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available")
        lines = []
        with mock.patch("sys.stderr"):
            code = cli.main(["--state-dir", str(link), "uninstall", "--purge-keys", "--yes"], out=lines.append,
                            runner=FakeRunner({"bootout": (113, ""), "print": (113, "")}),
                            kind="mac", home=self.home, env={})
        self.assertEqual(code, 1)
        self.assertTrue(all(p.exists() for p in self.ours))


class StatusRows(unittest.TestCase):
    def test_up_down_unknown(self):
        status = {"schemaVersion": 1, "kind": "agiw-peer", "host": "up", "platform": "windows",
                  "observedAt": "2023-11-14T22:13:19+00:00", "health": "ok",
                  "models": [{"id": "a", "loadedState": "loaded"}, {"id": "b", "loadedState": "inactive"}]}
        peers = [
            {"paired": True, "name": "up", "state": "connected", "status": status, "lastHello": 1.0},
            {"paired": True, "name": "down", "state": "offline", "lastHello": 1.0, "error": "TimeoutError"},
            {"paired": True, "name": "never", "state": "offline", "lastHello": None, "error": "no known endpoint"},
            {"paired": True, "name": "stale", "state": "stale", "lastHello": 2.0,
             "error": "ConnectionError: peer answered 503"},
            {"paired": True, "name": "connecting", "state": "connecting", "lastHello": 2.0, "error": None},
            {"paired": False, "name": "guest", "state": "available"}]
        for peer in peers:
            peer.update(node=peer["name"], host="windows")
        rows = cli.machine_rows({"self": {"node": "local", "host": "mac"}, "peers": peers},
                                now=1700000000.0)
        states = {r.get("label", r["id"]): (r["state"], r["reasonCode"]) for r in rows}
        self.assertEqual(states["up"], ("up", "observed"))
        self.assertEqual(states["down"], ("unknown", "unavailable"))
        self.assertEqual(states["never"], ("unknown", "unavailable"))
        self.assertEqual(states["stale"], ("unknown", "unavailable"))
        self.assertEqual(states["connecting"], ("unknown", "not_observed"))
        self.assertNotIn("guest", states)
        up = next(r for r in rows if r["id"] == "up")
        self.assertEqual((up["modelsComplete"], up["loadedCount"], up["busyCount"]), (False, None, None))

    def test_commands_report_a_stopped_peer_link_clearly(self):
        lines = []
        with mock.patch.object(cli.pl, "control_request", side_effect=ConnectionRefusedError()), \
                mock.patch("sys.stderr") as err:
            code = cli.main(["--state-dir", "unused", "status"], out=lines.append)
        self.assertEqual(code, cli.EXIT_NOT_RUNNING)
        self.assertIn("not running", "".join(str(c) for c in err.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
