"""This Mac's side of the PC edition's share link: presence publisher, config.json opt-ins, sampler wiring.

Every share here is a temp directory; nothing writes to /Volumes/SharedChami or reads the real AGIW config.
"""
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import peer_feed
import peer_link

HERE = Path(__file__).resolve().parent
WINDOWS = HERE / "windows"
HOST = "louis-m5-pro"


def pc_probes():
    """The PC edition's own reader (windows/agiw_win/probes.py), imported read-only for interop checks."""
    if not (WINDOWS / "agiw_win" / "probes.py").is_file():
        raise unittest.SkipTest("PC edition sources are not in this checkout")
    before, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    sys.path.insert(0, str(WINDOWS))
    try:
        from agiw_win import probes
    finally:
        sys.path.remove(str(WINDOWS))
        sys.dont_write_bytecode = before
    return probes


def snapshot(age=1.0, **over):
    sampled = time.time() - age
    base = {"schemaVersion": 1, "host": "mac", "sampledAt": sampled, "activityKnown": True,
            "sources": [{"id": "lmstudio-api", "state": "live"}, {"id": "lms-ps", "state": "live"}],
            "models": [{"id": "qwen/qwen3.8-27b", "host": "mac", "loaded": True, "state": "generating",
                        "parallel": 4, "modelKey": "qwen3.8-27b", "metadata": {"path": "/Users/x/models"}},
                       {"id": "google/gemma-4", "host": "mac", "loaded": False, "state": "unloaded", "parallel": 4},
                       {"id": "gpt-oss-20b", "host": "windows", "loaded": True, "state": "idle"}],
            "macGpu": {"model": "Apple M5 Pro", "cores": 20, "utilizationPercent": 37, "rendererPercent": 30,
                       "allocatedBytes": 9 << 30, "inUseBytes": 3 << 30, "ageSeconds": 0.0},
            "memory": {"level": "watch", "availablePercent": 41, "reasons": ["swap"], "swapUsedBytes": 5}}
    base.update(over)
    return base


def share(test):
    """A temp share root with llm-lab/status, like a mounted SharedChami."""
    root = Path(tempfile.mkdtemp())
    test.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
    (root / "llm-lab" / "status").mkdir(parents=True)
    return root


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class MacPresenceShape(unittest.TestCase):
    def test_carries_every_field_the_pc_presence_carries(self):
        doc = peer_feed.mac_presence(snapshot(), HOST, version="1.0.0", dashboard_port=51234)
        self.assertEqual((doc["platform"], doc["edition"], doc["version"], doc["health"]), ("mac", "mac", "1.0.0", "ok"))
        self.assertEqual(doc["dashboard"], {"bind": "loopback", "port": 51234})
        self.assertEqual(doc["reads"], ["agiw-peers", "windows-status.json"])
        self.assertEqual(doc["memory"], {"level": "watch", "availablePercent": 41.0})
        self.assertEqual(doc["gpus"], [{"index": 0, "name": "Apple M5 Pro", "utilizationPercent": 37.0,
                                        "memoryUsedMiB": 3072, "memoryTotalMiB": None, "temperatureC": None}])
        self.assertEqual(doc["models"], [
            {"id": "qwen/qwen3.8-27b", "aliases": ["qwen3.8-27b"], "host": "mac", "loadedState": "loaded",
             "activity": "generating", "slotsBusy": None, "slotsTotal": 4},
            {"id": "google/gemma-4", "aliases": [], "host": "mac", "loadedState": "inactive",
             "activity": "unknown", "slotsBusy": None, "slotsTotal": None}])
        self.assertAlmostEqual(doc["observedUnix"], peer_link._parse_iso(doc["observedAt"]), delta=0.002)
        json.dumps(doc, allow_nan=False)
        self.assertNotIn("/Users", json.dumps(doc))

    def test_key_sets_match_the_pc_editions_own_presence(self):
        probes = pc_probes()
        now = time.time()
        pc = probes.peer_presence({"lanes": [{"id": "fast", "status": "idle", "phase": "idle", "port": 1235,
                                              "expectedModel": "openai/gpt-oss-20b", "servedModel": "gpt-oss-20b",
                                              "slotsBusy": 0, "slotsTotal": 2}],
                                   "pcGpus": [{"index": 0, "name": "RTX", "utilizationPercent": 1.0, "memoryUsedMiB": 1,
                                               "memoryTotalMiB": 2, "temperatureC": 30.0}],
                                   "memory": {"level": "ok", "availablePercent": 50}, "observerVersion": "0.1.0",
                                   "observedAt": peer_feed._iso(now), "sampledAt": now, "dashboardPort": 8767},
                                  "louisaurorar12")
        mac = peer_feed.mac_presence(snapshot(), HOST, version="1.0.0", dashboard_port=1)
        # Everything the PC publishes, plus this Mac's Nisi and Jev states (the PC's reader ignores unknown keys).
        self.assertEqual(set(mac) - {"components"}, set(pc))
        self.assertIn("components", mac)
        self.assertLessEqual(set(pc["models"][0]), set(mac["models"][0]))
        self.assertEqual(set(mac["gpus"][0]), set(pc["gpus"][0]))
        self.assertEqual((set(mac["memory"]), set(mac["dashboard"])), (set(pc["memory"]), set(pc["dashboard"])))

    def test_encoded_and_uri_forms_never_leak_folders(self):
        # Daybreak: single-part percent-encoded paths / URIs slipped past the two-part rule.
        cases = {
            "file:%2FUsers%2Falice%2Fsecret.gguf": "secret",
            "file:%2FUsers%2Falice%2Fsecret": "secret",
            "file:%2FUsers%2Falice": None,
            "%2Fhome%2Fbob%2Fmodels%2Fm.safetensors": "m",
            "file:Users": "Users",
            "name%ZZ": None,  # a malformed escape is never a registry id, and '%' itself never publishes
            "hf:org/model": "model",
        }
        for raw, expected in cases.items():
            self.assertEqual(peer_feed.public_id(raw), expected, raw)
        for kept in ("qwen/qwen3.8-27b", "gpt-oss:20b", "qwen3:8b", "google/gemma-4-26b-a4b-qat"):
            self.assertEqual(peer_feed.public_id(kept), kept)
        for raw in cases:
            out = peer_feed.public_id(raw) or ""
            self.assertNotIn("alice", out)
            self.assertNotIn("%", out)

    def test_path_shaped_model_ids_never_leak_folders(self):
        # Review finding D4: a relative path reported as a model id publishes only its bare final name.
        for value, published in (("Users/alice/private/models/secret.gguf", "secret"),
                                 ("lmstudio-community/Qwen3-8B-GGUF/Qwen3-8B-Q4_K_M.gguf", "Qwen3-8B-Q4_K_M"),
                                 ("publisher/model.safetensors", "model"), ("models/weights.bin", "weights"),
                                 ("mlx-community/thing.mlx", "thing"), ("../../alice/x", "x"), ("~/models/m", "m"),
                                 ("/Users/alice/m.gguf", "m"), ("C:\\Users\\alice\\Q.gguf", "Q"),
                                 ("file:///Users/alice/q", "q"), ("a b/c", "c"), ("secret.GGUF", "secret"),
                                 (".gguf", None), ("..", None), ("/", None), ("", None), ("  ", None), (7, None),
                                 # Registry-style ids, one name or publisher/model, are published as they are.
                                 ("qwen/qwen3.8-27b", "qwen/qwen3.8-27b"), ("google/gemma-4", "google/gemma-4"),
                                 ("qwen/qwen3-8b:2", "qwen/qwen3-8b:2"), ("gpt-oss-20b", "gpt-oss-20b"),
                                 ("Qwen3.8-27B Q4_K_M", "Qwen3.8-27B Q4_K_M"), ("qwen3:8b", "qwen3:8b")):
            self.assertEqual(peer_feed.public_id(value), published, value)
        rows = [{"id": "Users/alice/private/models/secret.gguf", "host": "mac", "loaded": True, "state": "idle",
                 "modelKey": "Users/alice/private/models/secret-key.gguf"},
                {"id": "qwen/qwen3.8-27b", "host": "mac", "loaded": False, "state": "unloaded",
                 "modelKey": "work/clients/acme/qwen.safetensors"}]
        doc = peer_feed.share_presence(snapshot(models=rows), HOST)
        self.assertEqual([(m["id"], m["aliases"]) for m in doc["models"]],
                         [("secret", ["secret-key"]), ("qwen/qwen3.8-27b", ["qwen"])])
        text = json.dumps(doc)
        for leak in ("Users", "alice", "private", "models/", ".gguf", ".safetensors", "clients", "acme"):
            self.assertNotIn(leak, text)

    def test_two_part_paths_and_mid_name_extensions_are_path_shaped(self):
        # Review follow-up to D4: two-part relative paths under a home or system folder, and model-file extensions
        # anywhere in a name (a .part download), are path-shaped too. The name right after a home folder is an
        # account name, so such a value is dropped rather than published as its final name.
        for value, published in (("Users/alice", None), ("home/alice", None), ("HOME/alice", None),
                                 ("Users/alice/", None), ("/Users/alice", None), ("/home/alice/", None),
                                 ("C:\\Users\\alice", None), ("~alice", None), ("~", None),
                                 ("private/models", "models"), ("var/weights", "weights"), ("tmp/q", "q"),
                                 ("alice/secret.gguf.part", "secret"), ("secret.gguf.part", "secret"),
                                 ("models/q.safetensors.download", "q"), ("Users/alice/q.gguf.part", "q"),
                                 # Extensions count only as whole dot-separated pieces.
                                 ("mlx-community/Qwen3-8B-4bit", "mlx-community/Qwen3-8B-4bit"),
                                 ("acme/model.bin-v2", "acme/model.bin-v2"), ("qwen/qwen3.8-27b", "qwen/qwen3.8-27b"),
                                 # The documented limit: a two-part relative path shaped like publisher/model.
                                 ("alice/private-models", "alice/private-models")):
            self.assertEqual(peer_feed.public_id(value), published, value)
        rows = [{"id": "Users/alice", "host": "mac", "loaded": True, "state": "idle", "modelKey": "home/alice"},
                {"id": "qwen/qwen3.8-27b", "host": "mac", "loaded": True, "state": "idle",
                 "modelKey": "alice/qwen.gguf.part"}]
        doc = peer_feed.share_presence(snapshot(models=rows), HOST)
        self.assertEqual([(m["id"], m["aliases"]) for m in doc["models"]], [("qwen/qwen3.8-27b", ["qwen"])])
        self.assertNotIn("alice", json.dumps(doc))

    def test_unverified_activity_and_missing_inventory_are_not_claimed(self):
        doc = peer_feed.mac_presence(snapshot(activityKnown=False), HOST)
        self.assertEqual([m["activity"] for m in doc["models"]], ["unknown", "unknown"])
        self.assertEqual(doc["health"], "degraded")
        one = peer_feed.mac_presence(snapshot(activityKnown=False, sources=[{"id": "lmstudio-api", "state": "live"},
                                                                             {"id": "lms-ps", "state": "error"}]), HOST)
        self.assertEqual((one["health"], len(one["models"])), ("degraded", 2))

    def test_unknown_inventory_is_refused_not_published_as_zero_models(self):
        # Both LM Studio probes timed out while models are loaded: the PC would count 0 loaded from models: [].
        for sources in ([{"id": "lmstudio-api", "state": "unavailable"}, {"id": "lms-ps", "state": "unavailable"}],
                        [{"id": "lms-ps", "state": "error"}], []):
            snap = snapshot(activityKnown=False, sources=sources, models=[])
            with self.assertRaises(peer_feed.InventoryUnknown, msg=sources):
                peer_feed.mac_presence(snap, HOST)
            with self.assertRaises(ValueError, msg=sources):  # the LAN link answers 503 for a provider ValueError
                peer_feed.share_presence(snap, HOST)

    def test_nisi_and_jev_states_are_published_in_fixed_words(self):
        components = [{"id": "nisi", "label": "Nisi route adapter", "state": "ready",
                       "detail": "v0.2 pins verified; edit pair /Users/x/secret (author)", "authorModel": "/Users/x"},
                      {"id": "jev", "label": "Jev", "state": "configured", "lastJudgedAgeSeconds": 754.4,
                       "detail": "Opted in (key file present, not read)"}]
        doc = peer_feed.mac_presence(snapshot(components=components), HOST)
        self.assertEqual(doc["components"], [
            {"id": "nisi", "label": "Nisi route adapter", "state": "ready",
             "detail": "Nisi Inference route ready on this Mac (pins verified, edit pair loaded)"},
            {"id": "jev", "label": "Jev", "state": "configured",
             "detail": "Jev opted in on this Mac (key never read or shared)", "lastJudgedAgeSeconds": 754}])
        self.assertNotIn("/Users", json.dumps(doc))
        self.assertNotIn("key file", json.dumps(doc))
        for _, _, details in peer_feed.COMPONENTS:
            for state, detail in details.items():
                self.assertTrue(detail.isprintable() and len(detail) <= 120, (state, detail))

    def test_missing_or_odd_component_states_are_unknown_never_omitted(self):
        for components in (None, [], "junk", [{"id": "nisi", "state": "exploded"}, {"id": "jev", "state": 7,
                                                                                   "lastJudgedAgeSeconds": float("inf")}]):
            doc = peer_feed.mac_presence(snapshot(components=components), HOST)
            self.assertEqual([(c["id"], c["state"]) for c in doc["components"]], [("nisi", "unknown"), ("jev", "unknown")])
            self.assertIsNone(doc["components"][1]["lastJudgedAgeSeconds"])
            json.dumps(doc, allow_nan=False)
        drift = peer_feed.mac_presence(snapshot(components=[{"id": "nisi", "state": "needs-action"},
                                                            {"id": "jev", "state": "unavailable"}]), HOST)
        self.assertEqual([c["state"] for c in drift["components"]], ["needs-action", "unavailable"])

    def test_components_reach_the_share_file_and_the_pc_reader_still_accepts_it(self):
        doc = peer_feed.share_presence(snapshot(components=[{"id": "nisi", "state": "partial"}]), HOST)
        self.assertEqual([(c["id"], c["state"]) for c in doc["components"]], [("nisi", "partial"), ("jev", "unknown")])
        self.assertTrue(peer_feed.pc_reader_accepts(doc, HOST, time.time()))
        # The LAN link's allowlist carries them too, word for word, so both transports agree.
        # (a null age is simply left out: absent and unknown mean the same thing)
        self.assertEqual(peer_link.project_status(doc)["components"],
                         [{k: v for k, v in c.items() if v is not None} for c in doc["components"]])

    def test_odd_inputs_stay_null_or_are_dropped(self):
        snap = snapshot(macGpu=None, memory={"level": "melting", "availablePercent": float("inf")},
                        models=[{"id": "/Users/x/models/secret.gguf", "host": "mac", "loaded": True, "state": "idle",
                                 "parallel": True},
                                {"id": "", "host": "mac", "loaded": True}, {"id": 7, "host": "mac"}, "junk"])
        doc = peer_feed.mac_presence(snap, HOST, dashboard_port=True)
        self.assertEqual(doc["gpus"], [])
        self.assertEqual(doc["memory"], {"level": None, "availablePercent": None})
        self.assertEqual(doc["dashboard"]["port"], None)
        # A path keeps only its final name, without the model-file extension (review finding D4).
        self.assertEqual([(m["id"], m["slotsTotal"]) for m in doc["models"]], [("secret", None)])

    def test_rows_are_capped(self):
        many = [{"id": f"m{i}", "host": "mac", "loaded": False} for i in range(50)]
        self.assertEqual(len(peer_feed.mac_presence(snapshot(models=many), HOST)["models"]), peer_link.MAX_MODELS)

    def test_the_lan_link_still_accepts_it(self):
        peer_link.validate_status(peer_feed.mac_presence(snapshot(), HOST), "mac", time.time())


class SharePresence(unittest.TestCase):
    def test_uses_the_platform_name_the_pc_links_a_mac_by(self):
        doc = peer_feed.share_presence(snapshot(), HOST, version="1.0.0", dashboard_port=8000)
        self.assertEqual((doc["platform"], doc["edition"], doc["host"]), ("macos", "mac", HOST))
        self.assertTrue(peer_feed.pc_reader_accepts(doc, HOST, time.time()))

    def test_stale_missing_or_misnamed_input_is_refused(self):
        for snap, host in ((snapshot(age=40), HOST), (snapshot(age=90), HOST), (None, HOST), ({}, HOST),
                           (snapshot(age=-30), HOST), (snapshot(), "bad host"), (snapshot(), "../x"), (snapshot(), "")):
            with self.assertRaises(ValueError, msg=f"{host} {snap and snap.get('sampledAt')}"):
                peer_feed.share_presence(snap, host)

    def test_projection_rules_refuse_what_the_lan_link_would_cut(self):
        doc = peer_feed.mac_presence(snapshot(), HOST)
        doc["models"][0]["activity"] = "x" * 70
        with self.assertRaises(ValueError):
            peer_feed._projection_keeps(doc)
        doc = peer_feed.mac_presence(snapshot(), HOST)
        doc["health"] = "ok\n"
        with self.assertRaises(ValueError):
            peer_feed._projection_keeps(doc)
        peer_feed._projection_keeps(peer_feed.mac_presence(snapshot(), HOST))

    def test_the_lan_links_own_rules_are_consulted_before_writing(self):
        with mock.patch.object(peer_link, "validate_status", side_effect=ValueError("status document invalid")):
            with self.assertRaises(ValueError):
                peer_feed.share_presence(snapshot(), HOST)
        real = peer_link.project_status

        def cut(doc):
            out = real(doc)
            out["models"] = out["models"][:1]
            return out

        with mock.patch.object(peer_link, "project_status", cut):
            with self.assertRaises(ValueError):
                peer_feed.share_presence(snapshot(), HOST)

    def test_pc_reader_rules(self):
        now = time.time()
        good = peer_feed.share_presence(snapshot(), HOST)
        self.assertTrue(peer_feed.pc_reader_accepts(good, HOST, now))
        for change in ({"platform": "mac"}, {"host": "other"}, {"observedUnix": now + 30}, {"observedUnix": True},
                       {"observedUnix": now - 31}, {"kind": "status"}, {"models": [{"id": " ", "loadedState": "loaded"}]},
                       {"models": [{"id": "a", "loadedState": "resident"}]}):
            self.assertFalse(peer_feed.pc_reader_accepts({**good, **change}, HOST, now), change)


class WritePresence(unittest.TestCase):
    def setUp(self):
        self.root = share(self)
        self.doc = peer_feed.share_presence(snapshot(), HOST)
        self.target = self.root / "llm-lab" / "status" / "agiw-peers" / f"{HOST}.json"

    def leftovers(self):
        return sorted(p.name for p in self.target.parent.iterdir() if p.name != self.target.name)

    def test_atomic_write_creates_only_agiw_peers_and_the_host_file(self):
        peer_feed.write_presence(self.doc, self.root)
        self.assertEqual(json.loads(self.target.read_text()), self.doc)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(sorted(p.name for p in (self.root / "llm-lab" / "status").iterdir()), ["agiw-peers"])
        self.assertFalse((self.root / "llm-lab" / "status" / "mac-status.json").exists())

    def test_missing_status_folder_is_never_created(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(os.rmdir, empty)
        with self.assertRaises(FileNotFoundError):
            peer_feed.write_presence(self.doc, empty)
        self.assertEqual(list(empty.iterdir()), [])

    def test_unmounted_default_share_is_refused(self):
        with mock.patch.object(peer_feed, "SHARE_ANCHOR", self.root):
            with self.assertRaises(FileNotFoundError):
                peer_feed.write_presence(self.doc, self.root)
        self.assertFalse(self.target.exists())
        with self.assertRaises(FileNotFoundError):
            peer_feed.write_presence(self.doc, self.root, require_mount=True)

    def test_symlinked_folders_and_target_are_refused(self):
        elsewhere = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(elsewhere, ignore_errors=True))
        os.symlink(elsewhere, self.root / "llm-lab" / "status" / "agiw-peers")
        with self.assertRaises(OSError) as caught:
            peer_feed.write_presence(self.doc, self.root)
        self.assertIn(caught.exception.errno, (errno.ELOOP, errno.ENOTDIR))
        self.assertEqual(list(elsewhere.iterdir()), [])
        os.unlink(self.root / "llm-lab" / "status" / "agiw-peers")
        # An ancestor swapped for a link is refused too.
        moved = self.root / "real-llm-lab"
        (self.root / "llm-lab").rename(moved)
        os.symlink(moved, self.root / "llm-lab")
        with self.assertRaises(OSError):
            peer_feed.write_presence(self.doc, self.root)
        self.assertFalse((moved / "status" / "agiw-peers").exists())
        os.unlink(self.root / "llm-lab")
        moved.rename(self.root / "llm-lab")
        # A link planted at the target name is not replaced, and what it points at is untouched.
        victim = elsewhere / "victim.json"
        victim.write_text("keep")
        self.target.parent.mkdir()
        os.symlink(victim, self.target)
        with self.assertRaises(ValueError):
            peer_feed.write_presence(self.doc, self.root)
        self.assertEqual(victim.read_text(), "keep")
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(self.leftovers(), [])

    def test_a_held_file_is_retried_and_a_failed_rename_keeps_the_old_file(self):
        peer_feed.write_presence(self.doc, self.root)
        good = self.target.read_bytes()
        real = os.rename
        calls = []

        def busy_twice(*args, **kwargs):
            calls.append(1)
            if len(calls) <= 2:
                raise OSError(errno.EBUSY, "busy")
            return real(*args, **kwargs)

        newer = {**self.doc, "observedUnix": self.doc["observedUnix"] + 1}
        with mock.patch.object(peer_feed.os, "rename", busy_twice), mock.patch.object(peer_feed.time, "sleep"):
            peer_feed.write_presence(newer, self.root)
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(self.target.read_text())["observedUnix"], newer["observedUnix"])
        good = self.target.read_bytes()
        with mock.patch.object(peer_feed.os, "rename", side_effect=OSError(errno.EACCES, "denied")), \
                mock.patch.object(peer_feed.time, "sleep"):
            with self.assertRaises(PermissionError):
                peer_feed.write_presence(self.doc, self.root)
        self.assertEqual(self.target.read_bytes(), good)
        self.assertEqual(self.leftovers(), [])

    def test_old_own_temp_leftovers_are_swept_and_nothing_else_is_touched(self):
        folder = self.target.parent
        folder.mkdir()
        old = time.time() - peer_feed.TEMP_SWEEP_AGE - 60

        def plant(name, *, age=old, kind="file"):
            path = folder / name
            if kind == "file":
                path.write_text("partial")
            elif kind == "dir":
                path.mkdir()
            else:
                os.symlink(folder.parent, path)
            os.utime(path, (age, age), follow_symlinks=False)
            return path

        stale = [plant(f".{HOST}.json.{i:016x}.tmp") for i in range(3)]
        keep = [plant(f".{HOST}.json.{'a' * 16}.tmp", age=time.time()),        # a write in progress elsewhere
                plant(f".other-mac.json.{'b' * 16}.tmp"),                       # another host's leftover
                plant(f".{HOST}.json.tmp-123"),                                 # not this writer's name pattern
                plant(f".{HOST}.json.{'c' * 16}.tmp.json"),
                plant(f".{HOST}.json.{'d' * 16}.tmp", kind="dir"),
                plant(f".{HOST}.json.{'e' * 16}.tmp", kind="link")]
        peer_feed.write_presence(self.doc, self.root)
        self.assertEqual(json.loads(self.target.read_text()), self.doc)
        self.assertFalse(any(p.exists() for p in stale))
        self.assertTrue(all(os.path.lexists(p) for p in keep))
        self.assertTrue(folder.parent.is_dir(), "a planted link's target is never touched")

    def test_sweep_is_bounded_and_never_blocks_the_write(self):
        folder = self.target.parent
        folder.mkdir()
        with mock.patch.object(peer_feed.os, "scandir", side_effect=OSError(errno.EIO, "share dropped")):
            peer_feed.write_presence(self.doc, self.root)
        self.assertTrue(self.target.exists())
        old = time.time() - peer_feed.TEMP_SWEEP_AGE - 60
        for i in range(12):
            path = folder / f".{HOST}.json.{i:016x}.tmp"
            path.write_text("x")
            os.utime(path, (old, old))
        with mock.patch.object(peer_feed, "TEMP_SWEEP_LIMIT", 5):
            fd = os.open(folder, os.O_RDONLY)
            try:
                removed = peer_feed._sweep_own_temps(fd, HOST)
            finally:
                os.close(fd)
        self.assertLessEqual(removed, 5)
        self.assertGreaterEqual(len(self.leftovers()), 7)

    def test_unfit_documents_never_touch_the_share(self):
        for doc in ({**self.doc, "host": "../../etc"}, {**self.doc, "observedUnix": float("nan")},
                    {**self.doc, "models": [{"id": "x" * 70000, "loadedState": "loaded"}]}):
            with self.assertRaises(ValueError):
                peer_feed.write_presence(doc, self.root)
        self.assertFalse(self.target.parent.exists())


class Publisher(unittest.TestCase):
    def setUp(self):
        self.root = share(self)
        self.clock = FakeClock()
        self.target = self.root / "llm-lab" / "status" / "agiw-peers" / f"{HOST}.json"

    def publisher(self, **kw):
        options = {"share_root": self.root, "version": "1.0.0", "dashboard_port": 8000, "clock": self.clock,
                   "enabled_by": "--publish-status", **kw}
        pub = peer_feed.PresencePublisher(HOST, **options)
        self.addCleanup(pub.close)
        return pub

    def test_off_by_default_shape_writes_nothing_and_reports_nothing(self):
        pub = self.publisher(enabled=False)
        self.assertFalse(pub.offer(lambda: snapshot()))
        self.assertEqual(pub.sources(), [])
        self.assertIsNone(pub._thread)
        self.assertFalse((self.root / "llm-lab" / "status" / "agiw-peers").exists())

    def test_writes_every_interval_from_the_offered_snapshot(self):
        pub = self.publisher()
        self.assertEqual(pub.sources()[0]["state"], "unavailable")  # first write pending
        self.assertTrue(pub.offer(lambda: snapshot()))
        self.assertTrue(pub.wait_idle(5))
        written = json.loads(self.target.read_text())
        self.assertEqual((written["platform"], written["version"], written["dashboard"]["port"]), ("macos", "1.0.0", 8000))
        row = pub.sources()[0]
        self.assertEqual((row["id"], row["state"], row["ageSeconds"]), ("agiw-presence", "live", 0.0))
        self.assertIn("agiw-peers/louis-m5-pro.json", row["detail"])
        self.assertIn("--publish-status", row["detail"])
        self.clock.now += 9.9
        self.assertFalse(pub.offer(lambda: self.fail("not due yet")))
        self.clock.now += 0.2
        self.assertTrue(pub.offer(lambda: snapshot()))
        self.assertTrue(pub.wait_idle(5))

    def test_hung_share_is_reported_stuck_and_nothing_queues_behind_it(self):
        release, started = threading.Event(), threading.Event()
        calls = []

        def hang(doc, root, require_mount=None):
            calls.append(doc)
            started.set()
            release.wait(10)

        pub = self.publisher()
        self.addCleanup(release.set)
        with mock.patch.object(peer_feed, "write_presence", hang):
            began = time.monotonic()
            self.assertTrue(pub.offer(lambda: snapshot()))
            self.assertTrue(started.wait(5))
            self.assertLess(time.monotonic() - began, 1.0, "the sampler never waits on the share")
            self.clock.now += peer_feed.PRESENCE_DEADLINE + 1
            row = pub.sources()[0]
            self.assertEqual(row["state"], "unavailable")
            self.assertTrue(row["detail"].startswith("Stuck:"))
            self.clock.now += 60
            self.assertFalse(pub.offer(lambda: snapshot()))
            threads = [t for t in threading.enumerate() if t.name == "agiw-presence"]
            self.assertEqual(len(threads), 1)
            release.set()
            self.assertTrue(pub.wait_idle(5))
        self.assertEqual(len(calls), 1)

    def test_failures_keep_the_previous_file_and_say_why(self):
        pub = self.publisher()
        self.assertTrue(pub.offer(lambda: snapshot()))
        self.assertTrue(pub.wait_idle(5))
        good = self.target.read_bytes()
        self.clock.now += 10
        self.assertTrue(pub.offer(lambda: snapshot(age=45)))  # stale: refused before any file operation
        self.assertTrue(pub.wait_idle(5))
        self.assertEqual(self.target.read_bytes(), good)
        row = pub.sources()[0]
        self.assertEqual(row["state"], "error")
        self.assertIn("left as it was", row["detail"])
        self.clock.now += 10
        missing = self.publisher(share_root=self.root / "nope")
        self.assertTrue(missing.offer(lambda: snapshot()))
        self.assertTrue(missing.wait_idle(5))
        self.assertEqual(missing.sources()[0]["state"], "unavailable")
        self.assertIn("not mounted", missing.sources()[0]["detail"])
        self.assertFalse(missing.offer(lambda: snapshot()))  # not due again yet

    def test_linked_share_folder_is_an_error_row(self):
        elsewhere = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(elsewhere, ignore_errors=True))
        os.symlink(elsewhere, self.root / "llm-lab" / "status" / "agiw-peers")
        pub = self.publisher()
        self.assertTrue(pub.offer(lambda: snapshot()))
        self.assertTrue(pub.wait_idle(5))
        self.assertEqual(pub.sources()[0]["state"], "error")
        self.assertIn("symbolic link", pub.sources()[0]["detail"])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_no_snapshot_yet_is_reported_without_starting_a_write(self):
        pub = self.publisher()
        self.assertFalse(pub.offer(lambda: None))
        self.assertIsNone(pub._thread)
        self.assertIn("no published snapshot", pub.sources()[0]["detail"])

    def test_unknown_inventory_keeps_the_previous_file_to_age_out(self):
        pub = self.publisher()
        self.assertTrue(pub.offer(lambda: snapshot()))
        self.assertTrue(pub.wait_idle(5))
        good = self.target.read_bytes()
        self.clock.now += 10
        blind = snapshot(activityKnown=False, models=[], sources=[{"id": "lmstudio-api", "state": "unavailable"},
                                                                  {"id": "lms-ps", "state": "unavailable"}])
        self.assertTrue(pub.offer(lambda: blind))
        self.assertTrue(pub.wait_idle(5))
        self.assertEqual(self.target.read_bytes(), good, "no models: [] is written over the last known list")
        row = pub.sources()[0]
        self.assertEqual(row["state"], "unavailable")
        self.assertIn("inventory unknown", row["detail"])
        self.assertIn("ages out", row["detail"])

    def test_link_failure_row_shows_beside_the_presence_row(self):
        pub = self.publisher()
        pub.link_failed(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"))
        rows = pub.sources()
        self.assertEqual([r["id"] for r in rows], ["agiw-peer-link", "agiw-presence"])
        self.assertEqual(rows[0]["state"], "error")
        self.assertIn("UnicodeDecodeError", rows[0]["detail"])
        off = self.publisher(enabled=False)
        off.link_failed(PermissionError(errno.EACCES, "denied", "/Users/x/Library/Application Support/AGIW/peer-link"))
        self.assertEqual([r["id"] for r in off.sources()], ["agiw-peer-link"])
        self.assertIn("PermissionError EACCES", off.sources()[0]["detail"])
        self.assertNotIn("/Users", off.sources()[0]["detail"])

    def test_config_error_row_shows_even_when_publishing_is_off(self):
        row = {"id": "agiw-config", "label": "AGIW config", "state": "error", "ageSeconds": None, "detail": "x"}
        self.assertEqual(self.publisher(enabled=False, config_source=row).sources(), [row])


class PcReadsTheMac(unittest.TestCase):
    """Cross-edition: the PC edition's own reader links this Mac from the file the publisher wrote."""

    def test_pc_read_peers_links_the_mac_via_the_share(self):
        probes = pc_probes()
        root = share(self)
        pub = peer_feed.PresencePublisher(HOST, share_root=root, version="1.0.0", dashboard_port=8000)
        self.addCleanup(pub.close)
        self.assertTrue(pub.offer(lambda: snapshot()))
        self.assertTrue(pub.wait_idle(5))
        # Run here, the PC reader's host_id() would be this Mac and skip it as itself; answer as the PC would.
        with mock.patch.object(probes.socket, "gethostname", return_value="LOUISAURORAR12"):
            peers, source = probes.read_peers(root, "louisaurorar12", time.time())
        mac = next((p for p in peers if p["platform"] == "macos" and p["fresh"]), None)  # collect.py's Mac link rule
        self.assertIsNotNone(mac, peers)
        self.assertEqual((mac["host"], mac["edition"], mac["version"], mac["source"]), (HOST, "mac", "1.0.0", "agiw-peers"))
        self.assertEqual([(m["id"], m["loadedState"]) for m in mac["models"]],
                         [("qwen/qwen3.8-27b", "loaded"), ("google/gemma-4", "inactive")])
        self.assertEqual(source["state"], "live")

    def test_the_mac_never_reads_its_own_presence_as_the_pc(self):
        root = share(self)
        peer_feed.write_presence(peer_feed.share_presence(snapshot(), HOST), root)
        self.assertIsNone(peer_feed.from_share(time.time(), root / "llm-lab" / "status", root))


class Config(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.path = self.dir / "config.json"

    def test_missing_file_is_off_and_silent(self):
        self.assertEqual(peer_feed.load_config(self.path), ({"publishStatus": False, "peerLink": False}, None))

    def test_pc_keys_turn_options_on(self):
        self.path.write_text('{"publishStatus": true, "peerLink": false, "futureKey": 3}')
        self.assertEqual(peer_feed.load_config(self.path), ({"publishStatus": True, "peerLink": False}, None))
        options = peer_feed.link_options(config_path=self.path)
        self.assertEqual((options["presence"], options["lanLink"], options["enabledBy"]),
                         (True, False, "config.json publishStatus"))
        self.path.write_text('{"peerLink": true}')
        options = peer_feed.link_options(config_path=self.path)
        self.assertEqual((options["publishStatus"], options["peerLink"], options["presence"], options["lanLink"]),
                         (False, True, True, True))
        # A UTF-8 BOM (PowerShell 5.1 Set-Content -Encoding UTF8, or a file copied from the PC) reads as the PC reads it.
        self.path.write_bytes(b'\xef\xbb\xbf{"publishStatus": true, "peerLink": true}')
        self.assertEqual(peer_feed.load_config(self.path), ({"publishStatus": True, "peerLink": True}, None))
        self.path.write_bytes(b'\xef\xbb\xbf\xef\xbb\xbf{"publishStatus": true}')  # only one BOM is a BOM
        self.assertEqual(peer_feed.load_config(self.path)[1]["state"], "error")

    def test_bad_files_mean_off_plus_a_visible_error_never_a_crash(self):
        cases = {"not json": "{publishStatus: true}", "nan": '{"publishStatus": NaN}', "dup": '{"peerLink": false, "peerLink": true}',
                 "list": "[true]", "null": "null", "string bool": '{"publishStatus": "true"}', "number": '{"peerLink": 1}',
                 "huge": '{"publishStatus": true, "pad": "' + "x" * 20000 + '"}', "bom-less binary": b"\xff\xfe{}",
                 "empty": ""}
        for name, text in cases.items():
            self.path.write_bytes(text if isinstance(text, bytes) else text.encode())
            options, row = peer_feed.load_config(self.path)
            self.assertEqual(options, {"publishStatus": False, "peerLink": False}, name)
            self.assertEqual((row["id"], row["state"]), ("agiw-config", "error"), name)
            self.assertIn("stay off", row["detail"], name)
            self.assertNotIn(str(self.dir), row["detail"], name)

    def test_links_and_folders_are_refused(self):
        real = self.dir / "real.json"
        real.write_text('{"publishStatus": true}')
        os.symlink(real, self.path)
        options, row = peer_feed.load_config(self.path)
        self.assertEqual((options["publishStatus"], row["state"]), (False, "error"))
        self.assertIn("symbolic link", row["detail"])
        os.unlink(self.path)
        self.path.mkdir()
        self.assertEqual(peer_feed.load_config(self.path)[1]["state"], "error")

    def test_flags_win_over_a_bad_or_off_config(self):
        self.path.write_text("[]")
        options = peer_feed.link_options(publish_status=True, config_path=self.path)
        self.assertEqual((options["publishStatus"], options["presence"], options["lanLink"]), (True, True, False))
        self.assertEqual(options["configSource"]["state"], "error")
        self.path.write_text('{"publishStatus": false, "peerLink": false}')
        options = peer_feed.link_options(peer_link_flag=True, config_path=self.path)
        self.assertEqual((options["peerLink"], options["lanLink"], options["enabledBy"]), (True, True, "--peer-link"))

    def test_foreign_owned_config_is_ignored_and_flags_still_win(self):
        # Review finding D3: only a file this user owns may turn the opt-ins on.
        self.path.write_text('{"publishStatus": true, "peerLink": true}')
        os.chmod(self.path, 0o600)
        target, real_fstat = os.stat(self.path).st_ino, os.fstat

        def fstat(fd):
            info = real_fstat(fd)
            if info.st_ino != target:
                return info
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid + 1, info.st_gid,
                                   info.st_size, int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))

        with mock.patch.object(peer_feed.os, "fstat", side_effect=fstat):
            options, row = peer_feed.load_config(self.path)
            linked = peer_feed.link_options(peer_link_flag=True, config_path=self.path)
        self.assertEqual(options, {"publishStatus": False, "peerLink": False})
        self.assertEqual((row["id"], row["state"]), ("agiw-config", "error"))
        self.assertIn("not owned by you", row["detail"])
        self.assertNotIn(str(self.dir), row["detail"])
        self.assertEqual((linked["publishStatus"], linked["peerLink"], linked["lanLink"], linked["enabledBy"]),
                         (False, True, True, "--peer-link"))
        self.assertIn("not owned by you", linked["configSource"]["detail"])
        # The same file, owned by this user, turns both on.
        self.assertEqual(peer_feed.load_config(self.path), ({"publishStatus": True, "peerLink": True}, None))

    def test_group_or_world_writable_config_is_ignored(self):
        self.path.write_text('{"publishStatus": true, "peerLink": true}')
        for mode in (0o666, 0o620, 0o602, 0o622):
            os.chmod(self.path, mode)
            options, row = peer_feed.load_config(self.path)
            self.assertEqual(options, {"publishStatus": False, "peerLink": False}, oct(mode))
            self.assertEqual(row["state"], "error", oct(mode))
            self.assertIn("writable by other users", row["detail"], oct(mode))
            options = peer_feed.link_options(publish_status=True, config_path=self.path)
            self.assertEqual((options["publishStatus"], options["peerLink"], options["enabledBy"]),
                             (True, False, "--publish-status"), oct(mode))
        for mode in (0o600, 0o644, 0o400):  # readable by others is fine; only writing could change the opt-ins
            os.chmod(self.path, mode)
            self.assertEqual(peer_feed.load_config(self.path), ({"publishStatus": True, "peerLink": True}, None), oct(mode))

    @unittest.skipUnless(sys.platform == "darwin", "macOS extended ACLs")
    def test_extended_acl_on_the_config_is_ignored(self):
        # Daybreak: mode 0600 can still be writable by another account through an ACL.
        self.path.write_text('{"publishStatus": true, "peerLink": true}')
        os.chmod(self.path, 0o600)
        subprocess.run(["/bin/chmod", "+a", "everyone allow write", str(self.path)], check=True)
        options, row = peer_feed.load_config(self.path)
        self.assertEqual(options, {"publishStatus": False, "peerLink": False})
        self.assertIn("extended ACL", row["detail"])
        subprocess.run(["/bin/chmod", "-N", str(self.path)], check=True)
        self.assertEqual(peer_feed.load_config(self.path), ({"publishStatus": True, "peerLink": True}, None))

    def test_config_acl_is_read_from_the_open_descriptor_and_fails_closed(self):
        # Astra: the ACL must be the opened file's, not whatever the path names a moment later.
        self.path.write_text('{"publishStatus": true}')
        os.chmod(self.path, 0o600)
        seen = []

        def by_fd(fd):
            seen.append(fd)
            raise OSError(13, "denied")

        with mock.patch.object(peer_feed.sys, "platform", "darwin"), \
                mock.patch.object(peer_feed, "_fd_has_extended_acl", by_fd), \
                mock.patch.object(peer_feed.subprocess, "run", side_effect=AssertionError("no pathname tool")):
            options, row = peer_feed.load_config(self.path)
        self.assertEqual(options, {"publishStatus": False, "peerLink": False})
        self.assertIn("access list unreadable", row["detail"])
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], int)

    def test_default_path_sits_beside_the_peer_link_state(self):
        self.assertEqual(peer_feed.CONFIG_PATH.parent, Path.home() / "Library" / "Application Support" / "AGIW")
        self.assertEqual(peer_feed.CONFIG_PATH.name, "config.json")


class Identity(unittest.TestCase):
    def test_host_id_follows_the_pc_rule(self):
        with mock.patch.object(peer_feed.socket, "gethostname", return_value="Louis-M5-Pro-MacBook-Pro.local"):
            self.assertEqual(peer_feed.host_id(), "louis-m5-pro-macbook-pro")
        with mock.patch.object(peer_feed.socket, "gethostname", return_value="bad host!.local"):
            self.assertEqual(peer_feed.host_id(), "mac")

    def test_version_from_checkout_or_bundle_and_never_garbage(self):
        self.assertRegex(peer_feed.app_version(HERE) or "", r"^\d+\.\d+\.\d+$")
        bundle = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(bundle, ignore_errors=True))
        (bundle / "Resources").mkdir()
        (bundle / "Info.plist").write_bytes((HERE / "Info.plist").read_bytes())
        self.assertEqual(peer_feed.app_version(bundle / "Resources"), peer_feed.app_version(HERE))
        (bundle / "Info.plist").write_text("not a plist")
        self.assertIsNone(peer_feed.app_version(bundle / "Resources"))


class SamplerWiring(unittest.TestCase):
    """server.sample adds the publisher's rows to each full sample and hands it the published snapshot."""

    class Stop:
        def __init__(self, rounds):
            self.rounds, self.checks = rounds, 0

        def is_set(self):
            self.checks += 1
            return self.checks > self.rounds

        def wait(self, _):
            return True

    class Feed:
        def fingerprints(self):
            return {}

        def changed(self):
            return [], {}

        def set_baseline(self, _prints):
            pass

        def overlay(self, snap, _groups):
            return snap

    class Callers:
        def sample(self, _models):
            return None, {"id": "local-callers", "state": "idle"}

    class Guard:
        def sample(self, gpu_alloc_bytes=None, models=None):
            return {"level": "ok", "availablePercent": 50}, {"id": "mem-guard", "state": "live"}

    def run_sampler(self, publisher):
        import server
        store = server.SnapshotStore()
        row = {"id": "model-a", "name": "Model A", "host": "mac", "state": "idle", "loaded": True, "ageSeconds": 0,
               "source": "lms-ps"}
        with mock.patch("server.collect_snapshot", side_effect=lambda: {"schemaVersion": 1, "sampledAt": time.time(),
                                                                         "models": [dict(row)], "sources": [{"id": "lms-ps", "state": "live"}]}), \
                mock.patch("server.collect_activity", return_value={"runs": [], "sources": []}), \
                mock.patch("server.collect_clients", return_value=([], [])), \
                mock.patch("server.mac_gpu", return_value=(None, {"id": "mac-gpu", "state": "unavailable"})), \
                mock.patch("server.LocalCallers", return_value=self.Callers()):
            server.sample(store, self.Stop(1), feed=self.Feed(), guard=self.Guard(), publisher=publisher)
        return store.read()

    def test_rows_are_published_and_the_offer_reads_the_published_snapshot(self):
        offered = []

        class Fake:
            def sources(self):
                return [{"id": "agiw-presence", "state": "live", "detail": "ok"}]

            def offer(self, read):
                offered.append(read())

        published = self.run_sampler(Fake())
        self.assertIn({"id": "agiw-presence", "state": "live", "detail": "ok"}, published["sources"])
        self.assertEqual(len(offered), 1)
        self.assertEqual(offered[0]["sampledAt"], published["sampledAt"])
        self.assertTrue(offered[0]["activityKnown"], "built from the store's snapshot, not the raw sample")

    def test_a_broken_publisher_never_stops_the_sample(self):
        class Broken:
            def sources(self):
                raise RuntimeError("boom")

            def offer(self, read):
                raise RuntimeError("boom")

        published = self.run_sampler(Broken())
        self.assertEqual([s["state"] for s in published["sources"] if s["id"] == "agiw-presence"], ["error"])
        self.assertEqual(published["memory"]["level"], "ok")

    def test_real_publisher_end_to_end_into_a_temp_share(self):
        root = share(self)
        pub = peer_feed.PresencePublisher(HOST, share_root=root, version="1.0.0", dashboard_port=8000)
        self.addCleanup(pub.close)
        self.run_sampler(pub)
        self.assertTrue(pub.wait_idle(5))
        written = json.loads((root / "llm-lab" / "status" / "agiw-peers" / f"{HOST}.json").read_text())
        self.assertEqual([(m["id"], m["activity"]) for m in written["models"]], [("model-a", "idle")])
        self.assertEqual(written["health"], "ok")

    def test_peer_options_follow_config_and_flags_without_writing(self):
        import server
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        config = directory / "config.json"
        options, pub = server.peer_options(config=config, port=8000)
        self.addCleanup(pub.close)
        self.assertEqual((options["presence"], options["lanLink"], pub.enabled, pub.sources()), (False, False, False, []))
        config.write_text('{"peerLink": true}')
        options, pub = server.peer_options(config=config, port=8000)
        self.addCleanup(pub.close)
        self.assertEqual((options["lanLink"], pub.enabled, pub.dashboard_port, pub.share_root),
                         (True, True, 8000, Path("/Volumes/SharedChami")))
        self.assertIsNone(pub._thread, "nothing is written until the sampler offers a snapshot")
        config.write_text("{bad")
        options, pub = server.peer_options(publish_status=True, config=config, port=8000)
        self.addCleanup(pub.close)
        self.assertTrue(pub.enabled)
        self.assertEqual([r["id"] for r in pub.sources()], ["agiw-config", "agiw-presence"])


class StartPeerLink(unittest.TestCase):
    """server.start_peer_link never takes the observer down: any failure is a source row, not a crash loop."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp()) / "peer-link"
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state.parent, ignore_errors=True))
        self.state.mkdir(mode=0o700)
        self.binds = []

        def no_bind(link):
            self.binds.append(link)
            raise AssertionError("the test link must never bind")

        patches =[mock.patch.object(peer_link, "default_state_dir", return_value=self.state),
                   mock.patch.object(peer_link, "directed_broadcasts", return_value=("127.255.255.255",)),
                   mock.patch.object(peer_link.PeerLink, "start", no_bind)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def start(self):
        import server
        reported = []
        store = server.SnapshotStore()
        with redirect_stderr(io.StringIO()) as err:
            link = server.start_peer_link(store, report=reported.append)
        return link, reported, err.getvalue()

    def test_corrupt_control_token_is_rotated_and_never_used(self):
        # peer_link now treats an unreadable token like an exposed one: it is replaced, never used, and the link
        # goes on to start (here the start stub refuses to bind, which is the reported failure).
        token = self.state / "control-token"
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        bad = b"\xff\xfe" + b"z" * 62 + b"\n"
        with os.fdopen(fd, "wb") as handle:
            handle.write(bad)
        link, reported, err = self.start()
        self.assertIsNone(link)
        self.assertEqual([type(e) for e in reported], [AssertionError], "only the no-bind stub failed")
        self.assertEqual(len(self.binds), 1, "construction succeeded with a fresh token")
        fresh = token.read_bytes()
        self.assertNotEqual(fresh, bad)
        self.assertRegex(fresh.decode().strip(), r"^[0-9a-f]{64}$")
        self.assertNotIn(str(self.state), err)

    def test_slow_acl_check_timeout_is_caught_too(self):
        with mock.patch.object(peer_link.subprocess, "run", side_effect=subprocess.TimeoutExpired(["/bin/ls"], 10)):
            link, reported, _ = self.start()
        self.assertIsNone(link)
        self.assertEqual([type(e) for e in reported], [subprocess.TimeoutExpired])

    def test_a_broken_reporter_is_still_no_crash(self):
        import server

        def boom(_exc):
            raise RuntimeError("reporter broke")

        with mock.patch.object(peer_link, "PeerLink", side_effect=OSError(errno.EADDRINUSE, "in use")), \
                redirect_stderr(io.StringIO()):
            self.assertIsNone(server.start_peer_link(server.SnapshotStore(), report=boom))
            self.assertIsNone(server.start_peer_link(server.SnapshotStore()))

    def test_the_failure_shows_as_an_agiw_peer_link_source_row(self):
        token = self.state / "control-token"
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"\x80" * 64)
        import server
        root = share(self)
        pub = peer_feed.PresencePublisher(HOST, share_root=root)
        self.addCleanup(pub.close)
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(server.start_peer_link(server.SnapshotStore(), report=pub.link_failed))
        rows = {r["id"]: r for r in pub.sources()}
        self.assertEqual(rows["agiw-peer-link"]["state"], "error")
        self.assertIn("observer and the share presence keep running", rows["agiw-peer-link"]["detail"])


class MainWiring(unittest.TestCase):
    """server.main(): the flags and config.json reach peer_options, the sampler receives the publisher, and the LAN
    link starts exactly when peerLink is on (flag or config), off the startup path, and stops at shutdown."""

    class FakeServer:
        server_address = ("127.0.0.1", 12345)
        online_code_repair = None
        model_control = None
        auto_unloader = None

        def serve_forever(self, **_):
            pass

        def server_close(self):
            pass

    class FakeLink:
        def __init__(self):
            self.stopped = 0

        def stop(self):
            self.stopped += 1

    def run_main(self, argv, default_config=None, defer_link=False, after=None):
        """after(seen) runs once main() has returned, still inside every patch (so nothing real can start)."""
        import server
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        config_path = directory / "default" / "config.json"
        if default_config is not None:
            config_path.parent.mkdir()
            config_path.write_text(default_config)
        seen = {"threads": [], "options": [], "starts": [], "links": [], "published": [], "deferred": []}

        class FakeThread:
            def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, daemon=None):
                self.target, self.name, self.args, self.kwargs = target, name, args, kwargs or {}
                seen["threads"].append(self)

            def start(self):
                if self.name == "agiw-peer-link-start":
                    (seen["deferred"].append if defer_link else lambda run: run())(self.target)

        def fake_start(store, report=None):
            link = self.FakeLink()
            seen["starts"].append((store, report))
            seen["links"].append(link)
            return link

        real_options = server.peer_options

        def recording_options(*args, **kwargs):
            seen["options"].append(args)
            options, publisher = real_options(*args, **kwargs)
            seen["published"].append(publisher)
            self.addCleanup(publisher.close)
            return options, publisher

        with mock.patch.object(peer_feed, "CONFIG_PATH", config_path), \
                mock.patch.object(server, "MonitorServer", return_value=self.FakeServer()), \
                mock.patch.object(server, "memory_guard", return_value=None), \
                mock.patch.object(server.threading, "Thread", FakeThread), \
                mock.patch.object(server, "start_peer_link", side_effect=fake_start), \
                mock.patch.object(server, "peer_options", side_effect=recording_options), \
                mock.patch.object(server, "write_endpoint"), \
                mock.patch.object(server, "stop_windows_worker_probe"), \
                mock.patch.object(server.signal, "signal"), \
                mock.patch.object(sys, "argv", ["server.py", *argv]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            server.main()
            if after is not None:
                after(seen)
        samplers =[t for t in seen["threads"] if t.target is server.sample]
        self.assertEqual(len(samplers), 1)
        seen["sampler"] = samplers[0]
        self.assertEqual(len(seen["published"]), 1)
        seen["publisher"] = seen["published"][0]
        return seen

    def test_default_launch_publishes_nothing_and_starts_no_link(self):
        seen = self.run_main([])
        self.assertEqual(seen["options"], [(False, False, None, 12345)])
        self.assertIsInstance(seen["sampler"].kwargs["publisher"], peer_feed.PresencePublisher)
        self.assertIs(seen["sampler"].kwargs["publisher"], seen["publisher"])
        self.assertFalse(seen["publisher"].enabled)
        self.assertEqual(seen["starts"], [])

    def test_publish_status_flag_turns_presence_on_without_a_link(self):
        seen = self.run_main(["--publish-status"])
        self.assertEqual(seen["options"], [(True, False, None, 12345)])
        publisher = seen["sampler"].kwargs["publisher"]
        self.assertTrue(publisher.enabled)
        self.assertEqual((publisher.enabled_by, publisher.dashboard_port), ("--publish-status", 12345))
        self.assertEqual(seen["starts"], [])

    def test_peer_link_flag_starts_the_link_with_the_store_and_stops_it_at_shutdown(self):
        seen = self.run_main(["--peer-link"])
        self.assertEqual(seen["options"], [(False, True, None, 12345)])
        self.assertTrue(seen["publisher"].enabled)
        self.assertEqual(len(seen["starts"]), 1)
        store, report = seen["starts"][0]
        self.assertIs(store, seen["sampler"].args[0], "the link serves the store the sampler publishes into")
        self.assertEqual(report, seen["publisher"].link_failed)
        self.assertEqual(seen["links"][0].stopped, 1)

    def test_config_flag_file_and_default_config_reach_the_options(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        chosen = directory / "chosen.json"
        chosen.write_text('{"peerLink": true}')
        seen = self.run_main(["--config", str(chosen)])
        self.assertEqual(seen["options"], [(False, False, chosen, 12345)])
        self.assertEqual(len(seen["starts"]), 1)
        self.assertEqual(seen["publisher"].enabled_by, "config.json peerLink")
        # The app's own launch (no flags) follows the default config.json.
        seen = self.run_main([], default_config='{"peerLink": true}')
        self.assertEqual(seen["options"], [(False, False, None, 12345)])
        self.assertEqual(len(seen["starts"]), 1)
        self.assertEqual(seen["links"][0].stopped, 1)
        seen = self.run_main([], default_config='{"publishStatus": true}')
        self.assertTrue(seen["publisher"].enabled)
        self.assertEqual(seen["starts"], [])

    def test_a_link_that_finishes_starting_after_shutdown_is_stopped(self):
        def finish_late(seen):
            self.assertEqual(seen["starts"], [], "the port line and serving never wait on the link")
            self.assertEqual(len(seen["deferred"]), 1)
            seen["deferred"][0]()  # the start thread finishes after main() has shut down

        seen = self.run_main(["--peer-link"], defer_link=True, after=finish_late)
        self.assertEqual(len(seen["links"]), 1)
        self.assertEqual(seen["links"][0].stopped, 1)


if __name__ == "__main__":
    unittest.main()
