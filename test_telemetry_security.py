"""Recovery and archive regressions driven only by private local fixtures."""

import test_support.posix_only  # noqa: F401  (skips this module on Windows)

# isort: split
import contextlib
import fcntl
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import telemetry


def _private_write(path, text, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)


def _active_route(now):
    return {
        "schemaVersion": 1,
        "runId": "bound-route",
        "inputSha256": "a" * 64,
        "stage": "backend_draft",
        "sequence": 1,
        "startedUnix": now - 15,
        "checkpoint": {
            "schemaVersion": 1,
            "runId": "bound-route",
            "inputSha256": "a" * 64,
            "sequence": 1,
            "stage": "backend_draft",
            "recordedUnix": now - 5,
            "evidence": {},
        },
    }


class TelemetrySafetyRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "router"
        self.root.mkdir(mode=0o700)
        for name in ("active", "archive", "checkpoints", "locks", "notes"):
            (self.root / name).mkdir(mode=0o700)
        _private_write(self.root / "owner.lock", "")
        self.pending = self.base / "pending.json"
        for name, value in (
            ("_ROUTER_ROOT", self.root),
            ("_ACTIVE_PATH", self.root / "active.json"),
            ("_ROUTER_SCRIPT", self.base / "pipeline_router.py"),
            ("_PENDING_PATH", self.pending),
            ("_JEV_ENV_PATH", self.base / "jev.env"),
            ("_LAUNCHER_ROOT", self.base / "launcher"),
            ("_READINESS_PATH", self.base / "launcher/readiness.json"),
        ):
            patcher = mock.patch.object(telemetry, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Process attribution is optional; flock still probes the real bound fixture inode.
        patcher = mock.patch.object(telemetry, "_run_owners")
        patcher.start()
        self.addCleanup(patcher.stop)
        telemetry._JEV_CACHE.update(key=None, when=None, model=None)
        with telemetry._ROUTER_RECORD_CACHE_LOCK:
            telemetry._ROUTER_RECORD_CACHE.clear()
        self.addCleanup(telemetry._LAST_ROUTER_OBSERVATION.update, sampledAt=None, value=None)

    def observe(self):
        now = time.time()
        router = telemetry._router_observation(now)
        mode = telemetry._online_code_mode(now, "now", router)
        pipeline, source = telemetry._pipeline(now, router)
        telemetry._mark_live_route(pipeline, source, mode)
        return router, mode, pipeline, source

    def test_incomplete_per_run_with_held_bound_lock_preserves_recovery(self):
        lock_path = self.root / "locks" / "bound-route.lock"
        _private_write(lock_path, "")
        with lock_path.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(lock.fileno())
            for recovery in (True, False):
                with self.subTest(recovery=recovery):
                    record = _active_route(time.time())
                    record.update(stage="incomplete", runLock={"dev": info.st_dev, "ino": info.st_ino})
                    record["checkpoint"].update(
                        stage="incomplete", evidence={"recoveryRequired": recovery, "code": "OWNER_BUSY"}
                    )
                    _private_write(self.root / "active" / "bound-route.json", json.dumps(record))
                    router, mode, pipeline, _ = self.observe()
                    row = router["rows"][0]
                    self.assertEqual((row["lockHeld"], row["lockBound"], row["valid"]), (True, True, True))
                    self.assertEqual((row["live"], row["state"]), (False, "unresolved"))
                    self.assertEqual(
                        (mode["state"], mode["blinking"], mode["taskState"]), ("unknown", False, "unfinished")
                    )
                    self.assertEqual(mode["runCounts"], {"running": 0, "queued": 0, "unresolved": 1})
                    self.assertIn("run lock is held", mode["evidence"])
                    self.assertEqual(
                        (pipeline["status"], pipeline["recoveryRequired"]),
                        ("recovery-required" if recovery else "unresolved", recovery),
                    )
                    self.assertEqual(
                        (pipeline["pipelines"][0]["status"], pipeline["pipelines"][0]["recoveryRequired"]),
                        ("unresolved", recovery),
                    )

    def test_ordinary_bound_route_still_runs_and_blinks(self):
        lock_path = self.root / "locks" / "bound-route.lock"
        _private_write(lock_path, "")
        with lock_path.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(lock.fileno())
            record = _active_route(time.time())
            record["runLock"] = {"dev": info.st_dev, "ino": info.st_ino}
            _private_write(self.root / "active" / "bound-route.json", json.dumps(record))
            router, mode, pipeline, _ = self.observe()
        self.assertTrue(router["rows"][0]["live"])
        self.assertTrue(router["rows"][0]["lockBound"])
        self.assertEqual((mode["state"], mode["blinking"]), ("processing", True))
        self.assertEqual(pipeline["status"], "running")
        self.assertEqual(mode["runCounts"], {"running": 1, "queued": 0, "unresolved": 0})

    def test_unreadable_pending_marker_blocks_ready_with_verified_pair(self):
        _private_write(self.base / "target.json", "{}")
        for kind in ("malformed", "unsafe-mode", "symlink", "unreadable"):
            with self.subTest(kind=kind):
                if os.path.lexists(self.pending):
                    self.pending.unlink()
                if kind == "symlink":
                    self.pending.symlink_to(self.base / "target.json")
                else:
                    _private_write(
                        self.pending,
                        "{not json" if kind == "malformed" else "{}",
                        mode=0o644 if kind == "unsafe-mode" else 0o600,
                    )
                with contextlib.ExitStack() as stack:
                    if kind == "unreadable":
                        stack.enter_context(
                            mock.patch.object(telemetry, "_safe_file", side_effect=PermissionError("fixture"))
                        )
                    _, _, pipeline, source = self.observe()
                self.assertTrue(pipeline["pendingMarkerUnreadable"])
                self.assertIsNone(pipeline["pendingMarkerAttributedTo"])
                self.assertEqual(source["state"], "error")
                components = telemetry._route_components(
                    pipeline,
                    {"runtimeIntegrity": "VERIFIED", "hostBinding": "VERIFIED"},
                    {name: {"id": name, "loaded": True} for name in ("a", "b")},
                    time.time(),
                    inventory_complete=True,
                )
                self.assertEqual(components[0]["state"], "unresolved")

    def snapshot(self):
        worker = {"state": "unavailable", "modelsAdvertised": [], "modelCount": 0}
        with contextlib.ExitStack() as stack:
            for name, value in (
                ("_collect_lmstudio_inventory", None),
                ("_bounded_lms", {"models": []}),
                ("_afm_status", {"state": "missing"}),
                ("_windows_worker", (worker, {"id": "windows-worker", "state": "unavailable"})),
                ("_windows_headless", {"state": "unknown"}),
                ("_windows_jobs", ({}, {"id": "windows-jobs", "state": "unavailable"})),
                ("_canary", ({}, {"id": "canary-ledger", "state": "unavailable"})),
                ("collect_nisi_v02", ({}, {"id": "nisi-v02-runtime", "state": "unavailable"})),
            ):
                stack.enter_context(mock.patch.object(telemetry, name, return_value=value))
            stack.enter_context(mock.patch.object(telemetry.peer_feed, "peer_feeds", return_value=[]))
            return telemetry.collect_snapshot()

    def test_snapshot_skips_archive_with_2000_nested_arrays(self):
        raw = '{"x":' + "[" * 2000 + "0" + "]" * 2000 + "}"
        _private_write(self.root / "archive" / "deep.json", raw)
        snapshot = self.snapshot()
        jev = next(row for row in snapshot["components"] if row["id"] == "jev")
        self.assertIsNone(jev["lastJudgedAgeSeconds"])
        self.assertIsInstance(snapshot["sampledAt"], float)

    def test_snapshot_skips_large_deep_archive_and_keeps_valid_judgment(self):
        now = time.time()
        valid = self.root / "archive" / "valid.json"
        _private_write(
            valid,
            json.dumps(
                {
                    "finishedUnix": now - 120,
                    "result": {
                        "stages": {
                            "intake": {"kind": "chami.intake.typesafe.v1", "status": "JUDGED", "model": "fixture-model"}
                        }
                    },
                }
            ),
        )
        os.utime(valid, (now - 10, now - 10))
        raw = '{"x":' + "[" * 200000 + "0" + "]" * 200000 + "}"
        self.assertLess(len(raw), telemetry._MAX_STATE)
        _private_write(self.root / "archive" / "deep.json", raw)
        snapshot = self.snapshot()
        jev = next(row for row in snapshot["components"] if row["id"] == "jev")
        self.assertGreaterEqual(jev["lastJudgedAgeSeconds"], 120)
        self.assertIn("fixture-model", jev["detail"])
        cached = next(row for row in self.snapshot()["components"] if row["id"] == "jev")
        self.assertGreaterEqual(cached["lastJudgedAgeSeconds"], jev["lastJudgedAgeSeconds"])
        self.assertEqual(cached["detail"], jev["detail"])


if __name__ == "__main__":
    unittest.main()
