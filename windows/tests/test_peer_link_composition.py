"""Optional native pairing -> private file -> observer reader composition.

Set AGIW_PEER_LINK_PROPOSAL_DIR to a checkout of the published peer-link
proposal. Its native test guard is imported only in a fresh child process.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid


_TEST_KEY_PARENT = r"Software\AGIW-test"
_OWNED_KEY = re.compile(r"Software\\AGIW-test\\[0-9a-f]{32}\Z")


_CHILD_SOURCE = textwrap.dedent(r'''
    import json
    import os
    from pathlib import Path
    import sys
    import tempfile
    import time
    import unittest
    from unittest import mock

    # -I keeps inherited Python paths out; only the copied published proposal
    # and this checkout's Windows package are added explicitly.
    sys.path.insert(0, sys.argv[1])
    sys.path.insert(0, sys.argv[2])
    import test_peer_link as fixture
    import peer_link as pl
    from agiw_win import probes

    state_root = Path(sys.argv[3])
    state_root.mkdir()
    known_registry_root = sys.argv[4]
    mode = sys.argv[5]
    real_mkdtemp = tempfile.mkdtemp
    real_uuid4 = pl.uuidlib.uuid4
    first_uuid = True

    def owned_root_uuid():
        global first_uuid
        if first_uuid:
            first_uuid = False
            return pl.uuidlib.UUID(hex=known_registry_root.rsplit("\\", 1)[1])
        return real_uuid4()

    class PairedReaderComposition(fixture.PeerLinkCase):
        def register_node_directory(self, directory):
            # The published fixture normally gives each Node a sibling key.
            # Put those normally generated UUIDs below our parent-owned root,
            # so a killed process leaves nothing outside its known namespace.
            try:
                with mock.patch.object(fixture, "_TEST_REGISTRY_PARENT", self.registry_path):
                    return super().register_node_directory(directory)
            finally:
                # Its cleanup uses DeleteKey rather than recursive deletion;
                # preserve that cleanup and let it delete children before root.
                self._registry_paths.remove(self.registry_path)
                self._registry_paths.append(self.registry_path)

        def test_composition(self):
            self.assertEqual(pl._GENERATIONS, self.registry_path)
            self.assertEqual(self.registry_path, known_registry_root)
            model_state = {"loadedState": "loaded", "activity": "idle"}
            private_marker = "PRIVATE-PEER-COMPOSITION-DATA"

            def mac_status():
                return {
                    "schemaVersion": 1,
                    "host": "mac",
                    "observedAt": pl._iso(time.time()),
                    "models": [{"id": "mac-composition-model", **model_state,
                                "private": private_marker}],
                    "components": [{"id": "nisi", "label": "Nisi", "state": "ready",
                                    "detail": "composition ready", "private": private_marker}],
                    "private": private_marker,
                }

            # Node supplies ephemeral loopback HTTP/UDP ports, no broadcast,
            # native DPAPI, and one independently isolated generation namespace.
            # Keep even an incompletely constructed Node below the outer temp
            # directory; it is removed only after this entire process exits.
            with mock.patch.object(fixture.tempfile, "mkdtemp",
                                   side_effect=lambda: real_mkdtemp(dir=state_root)):
                mac = fixture.Node(self, "mac", "composition-mac", mac_status)
                pc = fixture.Node(self, "windows", "composition-pc")
            self.assertEqual(len(set(self._node_registry_paths.values())), 2)
            self.assertTrue(all(path.startswith(known_registry_root + "\\")
                                for path in self._node_registry_paths.values()))
            pin = fixture.pair(self, mac, pc)
            for me, other in ((mac, pc), (pc, mac)):
                me.link.poll_once()
                remote = me.link.snapshot()["peers"][0]
                self.assertTrue(remote["paired"])
                self.assertEqual(remote["state"], "connected")
                self.assertEqual(remote["status"]["host"], other.link.host)
                self.assertEqual(me.link.bind, "127.0.0.1")
                self.assertEqual(me.link.broadcast, ())
                self.assertNotIn(me.link.status_port, (8767, 8768))
                self.assertNotEqual(me.link.beacon_port, 8769)

            if mode == "abrupt-exit":
                # Crash only after native pairing has written both generation
                # namespaces. Bypass every fixture/finally cleanup deliberately.
                print("paired native generations below " + known_registry_root, flush=True)
                os._exit(23)
            self.assertEqual(mode, "composition")

            path = pc.dir / "peers.json"
            write = pl.write_snapshot(path)

            def publish():
                doc = pc.link.snapshot()
                # These adjacent fields must never cross the observer adapter,
                # even if a producer includes them in an otherwise real snapshot.
                doc["invitesShowingPin"] = [{"pin": "PIN-" + pin, "private": private_marker}]
                doc["pollError"] = private_marker
                doc["quarantined"] = [private_marker]
                doc["peers"][0]["error"] = private_marker
                write(doc)
                self.assertTrue(path.is_file(), "published private writer must create peers.json")
                return doc

            def read(now):
                with mock.patch.object(probes, "peer_link_status_path", return_value=path):
                    return probes.read_peer_link(now, enabled=True)

            def assert_private(rows, source):
                serialized = json.dumps({"rows": rows, "source": source}, allow_nan=False)
                for secret in (private_marker, "PIN-" + pin, "127.0.0.1"):
                    self.assertNotIn(secret, serialized)
                forbidden = {"address", "error", "inviteWaiting", "invitesShowingPin",
                             "pollError", "quarantined", "private", "pin"}

                def check(value):
                    if isinstance(value, dict):
                        self.assertFalse(forbidden.intersection(value))
                        for child in value.values():
                            check(child)
                    elif isinstance(value, list):
                        for child in value:
                            check(child)
                check(rows)
                check(source)

            first = publish()
            observed = pl._parse_iso(first["observedAt"])
            rows, source = read(observed)
            self.assertEqual(source["id"], "peer-link")
            self.assertEqual(source["state"], "live")
            self.assertIn("authenticated", source["detail"])
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["node"], rows[0]["name"], rows[0]["host"],
                              rows[0]["paired"], rows[0]["state"]),
                             (mac.link.node, "composition-mac", "mac", True, "connected"))
            initial_model = rows[0]["status"]["models"][0]
            self.assertEqual((initial_model["id"], initial_model["loadedState"],
                              initial_model["activity"]),
                             ("mac-composition-model", "loaded", "idle"))
            self.assertEqual(rows[0]["status"]["components"][0],
                             {"id": "nisi", "label": "Nisi", "state": "ready",
                              "detail": "composition ready"})
            assert_private(rows, source)
            original_bytes = path.read_bytes()

            # The actual authenticated request and published atomic writer run
            # again; the next observer read must reopen the replaced file.
            model_state.update(loadedState="inactive", activity="unknown")
            pc.link.poll_once()
            second = publish()
            self.assertNotEqual(path.read_bytes(), original_bytes)
            observed = pl._parse_iso(second["observedAt"])
            refreshed, source = read(observed)
            self.assertEqual(refreshed[0]["state"], "connected")
            self.assertEqual(refreshed[0]["status"]["models"][0]["loadedState"], "inactive")
            self.assertEqual(initial_model["loadedState"], "loaded")
            assert_private(refreshed, source)

            for now in (observed + 30.001, observed - 5.001):
                unknown, source = read(now)
                self.assertEqual(len(unknown), 1)
                self.assertEqual(unknown[0]["state"], "unknown")
                self.assertIsNone(unknown[0].get("status"))
                self.assertNotEqual(source["state"], "live")
                assert_private(unknown, source)

            # Expiry is evaluated per read, without rewriting cached row state.
            recovered, source = read(observed)
            self.assertEqual(recovered[0]["state"], "connected")
            self.assertEqual(recovered[0]["status"]["models"][0]["loadedState"], "inactive")
            self.assertEqual(source["state"], "live")
            assert_private(recovered, source)

    try:
        # Only the fixture's first UUID (case root) is parent-chosen. Node UUIDs,
        # identities, and all later generation randomness remain native.
        with mock.patch.object(pl.uuidlib, "uuid4", side_effect=owned_root_uuid):
            result = unittest.TextTestRunner(verbosity=2).run(
                unittest.TestSuite([PairedReaderComposition("test_composition")]))
    finally:
        # PeerLinkCase has already stopped and joined all of its tracked workers
        # and deleted only its unique native test keys. This removes an empty
        # AGIW-test parent, preserving another test run's children or values.
        fixture.tearDownModule()
    raise SystemExit(0 if result.wasSuccessful() else 1)
''')


def _real_registry_key_exists():
    """Parent-only, read-only production-key presence check."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\AGIW", 0, winreg.KEY_READ):
            return True
    except FileNotFoundError:
        return False


def _require_owned_namespace(namespace):
    if not isinstance(namespace, str) or _OWNED_KEY.fullmatch(namespace) is None:
        raise ValueError("cleanup requires an exact Software\\AGIW-test\\<uuid> namespace")


def _remove_owned_registry_tree(namespace):
    """Delete only one exact parent-chosen test UUID, including after child death."""
    _require_owned_namespace(namespace)
    import winreg

    def remove(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
                children = [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
        except FileNotFoundError:
            return
        for child in children:
            remove(path + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)

    # Never remove the shared AGIW-test parent or compare/enumerate its siblings.
    remove(namespace)


def _run_proposal_child(proposal, windows, namespace, *, mode="composition", timeout=90):
    _require_owned_namespace(namespace)
    try:
        with tempfile.TemporaryDirectory(prefix="agiw-peer-reader-composition-") as temporary:
            root = Path(temporary)
            copied = root / "proposal"
            copied.mkdir()
            for name in ("peer_link.py", "test_peer_link.py"):
                shutil.copyfile(proposal / name, copied / name)
            script = root / "composition.py"
            script.write_text(_CHILD_SOURCE, encoding="utf-8", newline="\n")
            return subprocess.run(
                [sys.executable, "-I", "-B", str(script), str(copied), str(windows),
                 str(root / "state"), namespace, mode],
                cwd=root, capture_output=True, text=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    finally:
        # run() kills and waits for its child before raising TimeoutExpired.
        # No live child worker can race this exact-namespace cleanup, on either
        # success, deliberate abrupt exit, or timeout.
        _remove_owned_registry_tree(namespace)


class RegistryCleanupBounds(unittest.TestCase):
    def test_refuses_production_parent_sibling_and_non_uuid_cleanup_roots(self):
        for namespace in (r"Software\AGIW", r"Software\AGIW\PeerLink",
                          _TEST_KEY_PARENT, _TEST_KEY_PARENT + "\\not-a-uuid",
                          _TEST_KEY_PARENT + "\\" + "a" * 32 + "\\child",
                          "Software\\Other\\" + "a" * 32, None):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                _remove_owned_registry_tree(namespace)


@unittest.skipUnless(os.name == "nt" and os.environ.get("AGIW_PEER_LINK_PROPOSAL_DIR"),
                     "set AGIW_PEER_LINK_PROPOSAL_DIR for native published-proposal composition")
class PeerLinkReaderComposition(unittest.TestCase):
    def setUp(self):
        self.assertFalse(_real_registry_key_exists(),
                         "composition requires real HKCU\\Software\\AGIW to be absent")
        self.addCleanup(self.assert_production_absent)
        self.proposal = Path(os.environ["AGIW_PEER_LINK_PROPOSAL_DIR"]).resolve()
        for name in ("peer_link.py", "test_peer_link.py"):
            self.assertTrue((self.proposal / name).is_file(), "proposal source missing: " + name)
        self.windows = Path(__file__).resolve().parents[1]
        self.namespace = _TEST_KEY_PARENT + "\\" + uuid.uuid4().hex
        self.assert_namespace_absent(self.namespace)

    def assert_production_absent(self):
        self.assertFalse(_real_registry_key_exists(),
                         "composition must leave real HKCU\\Software\\AGIW absent")

    def assert_namespace_absent(self, namespace):
        import winreg
        with self.assertRaises(FileNotFoundError):
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, namespace, 0, winreg.KEY_READ):
                pass

    def test_abrupt_child_exit_cleans_owned_root_and_preserves_other_test_data(self):
        import winreg
        sibling = _TEST_KEY_PARENT + "\\" + uuid.uuid4().hex
        sentinel = "composition-" + uuid.uuid4().hex
        self.assert_namespace_absent(sibling)
        parent_value_set = False
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sibling, 0,
                                    winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, sentinel, 0, winreg.REG_SZ, "sibling owned by this regression")
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _TEST_KEY_PARENT, 0,
                                    winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, sentinel, 0, winreg.REG_SZ, "parent value owned by this regression")
                parent_value_set = True
            child = _run_proposal_child(self.proposal, self.windows, self.namespace,
                                       mode="abrupt-exit")
            self.assertEqual(child.returncode, 23, child.stdout + child.stderr)
            self.assertIn("paired native generations below " + self.namespace, child.stdout)
            self.assert_namespace_absent(self.namespace)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sibling, 0, winreg.KEY_READ) as key:
                self.assertEqual(winreg.QueryValueEx(key, sentinel)[0], "sibling owned by this regression")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _TEST_KEY_PARENT, 0, winreg.KEY_READ) as key:
                self.assertEqual(winreg.QueryValueEx(key, sentinel)[0], "parent value owned by this regression")
        finally:
            try:
                _remove_owned_registry_tree(sibling)
            finally:
                if parent_value_set:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _TEST_KEY_PARENT, 0,
                                        winreg.KEY_SET_VALUE) as key:
                        winreg.DeleteValue(key, sentinel)

    def test_real_pairing_atomic_snapshot_reopen_and_expiry(self):
        child = _run_proposal_child(self.proposal, self.windows, self.namespace)
        self.assertEqual(child.returncode, 0,
                         "isolated native composition failed:\n" + child.stdout + child.stderr)
        self.assertNotIn("Exception in thread", child.stderr)
        self.assertNotIn("ResourceWarning", child.stderr)
        self.assert_namespace_absent(self.namespace)


if __name__ == "__main__":
    unittest.main()
