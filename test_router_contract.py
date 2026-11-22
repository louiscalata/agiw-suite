"""The public router state contract (docs/router-state-contract.md), exercised without the router.

``RouterContractFixtureTests`` and ``RouterContractActivityFixtureTests`` run every R2.9
dual-read case of test_telemetry and test_activity against the contract fixture writers in
test_support/router_contract/ instead of the separately managed router project's writers.
A pass says the Monitor reads a journal shaped as the contract describes.  It is not router
acceptance: that still needs the router's own writers, which
test_telemetry.RouterDualReadTests and test_activity.DualReadActivityTests keep requiring.

``RouterContractDocumentTests`` pins the machine-readable contract to the Monitor's readers,
to the fixture writers' output and to the prose document, so none of the four can drift alone.
"""

import test_support.posix_only  # noqa: F401  (skips this module on Windows)

# isort: split
import ast
import contextlib
import fcntl
import json
import os
import runpy
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import activity
import telemetry
import test_activity as dual_read_activity  # module attributes, so their cases are not collected twice
import test_telemetry as dual_read

_REPOSITORY = Path(__file__).resolve().parent
_FIXTURES = _REPOSITORY / "test_support/router_contract"
_WRITERS = {
    "per-run": _FIXTURES / "per_run/pipeline_router_state.py",
    "legacy": _FIXTURES / "legacy/pipeline_router_state.py",
}
_CONTRACT_PATH = _FIXTURES / "contract.json"
_DOCUMENT_PATH = _REPOSITORY / "docs/router-state-contract.md"


def _contract():
    return json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


def _work_interpreter_in_contract(interpreter, contract):
    """The contract's own rule for a router work process's argv[0], restated here so a skip
    below never depends on the reader under test."""
    rule = contract["workProcess"]
    path = Path(interpreter)
    if path.name in rule["interpreterNames"]:
        return True
    return interpreter.endswith(rule["frameworkInterpreterSuffix"]) and any(
        part in rule["frameworkDirectories"] for part in path.parts
    )


def _process_argv0(pid):
    listing = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10
    ).stdout.decode("utf-8", "replace")
    return listing.split()[0] if listing.split() else ""


@unittest.skipUnless(
    sys.platform == "darwin" and os.path.exists("/usr/sbin/lsof"),
    "NOT_RUN: the journal readers attribute processes with macOS lsof and ps",
)
class RouterContractFixtureTests(dual_read.RouterDualReadTests):
    """Every dual-read case, with the journal written by the public contract fixtures."""

    def writer(self, kind):
        path = _WRITERS[kind]
        return runpy.run_path(str(path)), path

    @contextlib.contextmanager
    def holder(self, kind, run_id, digest, **config):
        with super().holder(kind, run_id, digest, **config) as pid:
            interpreter = _process_argv0(pid)
            if not _work_interpreter_in_contract(interpreter, _contract()):
                self.skipTest(
                    "NOT_RUN: this Python runs as %r, which the contract does not name as a "
                    "router work interpreter" % interpreter
                )
            yield pid

    def test_fixture_writers_are_the_public_ones(self):
        """These cases never fall back to an installed router: both writers come from this
        repository, and the per-run one carries its fence writer."""
        for kind in ("per-run", "legacy"):
            _module, path = self.writer(kind)
            self.assertEqual(path.parent.parent, _FIXTURES)
            self.assertEqual("def claim(" in path.read_text(encoding="utf-8"), kind == "per-run")
        self.assertTrue(_WRITERS["per-run"].with_name("router_fence.py").is_file())
        self.assertFalse(_WRITERS["legacy"].with_name("router_fence.py").exists())


class RouterContractActivityFixtureTests(dual_read_activity.DualReadActivityTests):
    """Every dual-read activity case, with the journal written by the public contract fixtures.
    These cases attribute no process, so they need neither lsof nor ps."""

    def setUp(self):
        # The inherited setUp asks test_telemetry for the router's writers by name.
        patcher = mock.patch.object(dual_read, "_require_router_writer", lambda _test, kind: _WRITERS[kind])
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_fixture_writers_are_the_public_ones(self):
        for module, kind in ((self.state, "per-run"), (self.legacy, "legacy")):
            self.assertEqual(module["__doc__"], runpy.run_path(str(_WRITERS[kind]))["__doc__"])
            self.assertIn("Contract fixture", module["__doc__"])


class RouterContractDocumentTests(unittest.TestCase):
    """contract.json is the contract's machine-readable half."""

    @classmethod
    def setUpClass(cls):
        cls.contract = _contract()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(os.path.realpath(temp.name)) / "router"

    # -- the contract against the readers -------------------------------------------------------

    def test_contract_names_the_readers_own_identifier(self):
        self.assertEqual(self.contract["schema"], "agiw.router-state-contract.v1")
        self.assertRegex(self.contract["version"], r"\A1\.\d+\.\d+\Z")
        self.assertEqual(self.contract["readerContract"], telemetry.ROUTER_READER_CONTRACT)
        self.assertEqual(self.contract["readerContract"], activity.ROUTER_READER_CONTRACT)

    def test_contract_paths_are_the_paths_the_readers_open(self):
        home = Path.home()
        paths = self.contract["paths"]
        self.assertEqual(home / paths["routerRoot"], telemetry._ROUTER_ROOT)
        self.assertEqual(home / paths["routerRoot"], activity._ROOT)
        self.assertEqual(home / paths["routerRoot"] / "active.json", telemetry._ACTIVE_PATH)
        self.assertEqual(home / paths["routerRoot"] / "active.json", activity._ACTIVE)
        self.assertEqual(home / paths["routerScript"], telemetry._ROUTER_SCRIPT)
        self.assertEqual(home / paths["nisiPendingMarker"], telemetry._PENDING_PATH)
        self.assertEqual(home / paths["launcherReadiness"], telemetry._READINESS_PATH)
        self.assertEqual(self.contract["installBarrierName"], telemetry._ROUTER_BARRIER)

    def test_contract_limits_are_the_readers_limits(self):
        limits = self.contract["limits"]
        expected = {
            "recordBytes": telemetry._MAX_STATE,
            "noteBytes": telemetry._ROUTER_NOTE_MAX,
            "policyBytes": telemetry._ROUTER_SMALL_MAX,
            "fenceBytes": telemetry._ROUTER_SMALL_MAX,
            "markerBytes": 4096,
            "readinessBytes": 4096,
            "archiveBytes": activity._MAX_ARCHIVE_BYTES,
            "directoryEntriesScanned": telemetry._ROUTER_SCAN,
            "namesListed": telemetry._ROUTER_LISTED,
            "fullRecordReads": telemetry._ROUTER_FULL_READS,
            "extraLiveRecordReads": telemetry._ROUTER_ATTRIBUTED,
            "rowsProjected": telemetry._ROUTER_PROJECTED,
            "readAttempts": telemetry._ROUTER_READ_RETRIES,
            "readRetrySeconds": telemetry._ROUTER_READ_RETRY_S,
            "checkpointFreshSeconds": telemetry._MAX_AGE,
            "readinessFreshSeconds": telemetry._READINESS_MAX_AGE,
            "processStartWindowSeconds": telemetry._ROUTER_START_WINDOW,
            "markerFutureSeconds": 5,
            "largestExactInteger": telemetry._MAX_EXACT_INT,
            "archiveNamesScanned": activity._MAX_ARCHIVE_SCAN,
            "archiveRunsShown": activity._MAX_RUNS,
            "judgmentArchivesRead": 12,
        }
        self.assertEqual(limits, expected)
        self.assertEqual(
            (
                activity._MAX_BYTES,
                activity._MAX_ACTIVE_READS,
                activity._MAX_ACTIVE_LISTED,
                activity._READ_RETRIES,
                activity._READ_RETRY_S,
                activity._MAX_AGE,
            ),
            (
                limits["recordBytes"],
                limits["fullRecordReads"],
                limits["namesListed"],
                limits["readAttempts"],
                limits["readRetrySeconds"],
                limits["checkpointFreshSeconds"],
            ),
        )

    def test_contract_schemas_are_the_readers_closed_schemas(self):
        files = self.contract["files"]
        self.assertEqual(files["fence"]["kind"], telemetry._ROUTER_FENCE_KIND)
        self.assertEqual(set(files["fence"]["keys"]), telemetry._ROUTER_FENCE_KEYS)
        self.assertEqual(tuple(files["fence"]["states"]), telemetry._ROUTER_FENCE_STATES)
        self.assertEqual(files["fence"]["stampPattern"], telemetry._ROUTER_STAMP.pattern)
        self.assertEqual(files["policy"]["kind"], telemetry._ROUTER_POLICY_KIND)
        self.assertEqual(files["note"]["kind"], telemetry._ROUTER_NOTE_KIND)
        self.assertEqual(set(files["note"]["keys"]), telemetry._ROUTER_NOTE_KEYS)
        self.assertEqual(tuple(files["note"]["phases"]), telemetry._ROUTER_NOTE_PHASES)
        self.assertEqual(tuple(files["note"]["resources"]), telemetry._ROUTER_NOTE_RESOURCES)
        self.assertEqual(
            {key: tuple(values) for key, values in files["note"]["enums"].items()}, telemetry._ROUTER_NOTE_ENUMS
        )
        self.assertEqual({name: tuple(lane) for name, lane in files["note"]["lanes"].items()}, telemetry._ROUTER_LANES)
        self.assertEqual(files["marker"]["kind"], telemetry._PENDING_KIND)
        self.assertEqual(set(files["marker"]["anonymousKeys"]), telemetry._PENDING_KEYS)
        self.assertEqual(set(files["marker"]["routerKeys"]), telemetry._PENDING_ROUTER_KEYS)
        self.assertEqual(tuple(files["marker"]["operations"]), telemetry._PENDING_OPERATIONS)
        self.assertEqual(tuple(files["marker"]["runIdSuffixes"]), telemetry._ROUTER_MARKER_SUFFIXES)
        self.assertEqual(files["marker"]["runIdPattern"], telemetry._PENDING_RUN_ID.pattern)
        self.assertEqual(files["record"]["runIdPattern"], telemetry._RUN_ID.pattern)
        self.assertEqual(files["record"]["stagePattern"], telemetry._STAGE.pattern)
        self.assertEqual(files["record"]["sha256Pattern"], telemetry._SHA256.pattern)
        self.assertEqual(files["record"]["stageHosts"], telemetry._ROUTER_STAGE_HOST)

    def test_contract_record_key_sets_are_what_the_envelope_check_accepts(self):
        """Each key set the contract lists passes the reader's closed envelope, and the same
        record with one key more or one key fewer does not."""
        record = self.contract["files"]["record"]
        digest, lock = "a" * 64, {"dev": 1, "ino": 2}
        checkpoint = {
            "schemaVersion": 1,
            "runId": "run-a",
            "inputSha256": digest,
            "sequence": 1,
            "stage": "backend_draft",
            "evidence": {},
            "recordedUnix": 20.0,
        }
        self.assertEqual(set(checkpoint), set(record["checkpointKeys"]))
        started = {
            "schemaVersion": 1,
            "runId": "run-a",
            "inputSha256": digest,
            "stage": "started",
            "checkpoint": None,
            "startedUnix": 10.0,
        }
        advanced = dict(started, stage="backend_draft", sequence=1, checkpoint=checkpoint)
        for value, keys, per_run in (
            (started, record["startedKeys"], False),
            (advanced, record["checkpointedKeys"], False),
            (dict(started, runLock=lock), record["startedKeys"] + ["runLock"], True),
            (dict(advanced, runLock=lock), record["checkpointedKeys"] + ["runLock"], True),
        ):
            with self.subTest(keys=sorted(keys), per_run=per_run):
                self.assertEqual(set(value), set(keys))
                self.assertIsNotNone(telemetry._valid_active_route(value, per_run=per_run))
                self.assertIsNone(telemetry._valid_active_route(dict(value, extra=1), per_run=per_run))
                for key in value:
                    self.assertIsNone(
                        telemetry._valid_active_route(
                            {name: item for name, item in value.items() if name != key}, per_run=per_run
                        ),
                        key,
                    )
        # A legacy record never carries runLock, and a per-run record always does.
        self.assertIsNone(telemetry._valid_active_route(dict(started, runLock=lock)))
        self.assertIsNone(telemetry._valid_active_route(started, per_run=True))

    def test_contract_work_process_rule_is_the_readers_rule(self):
        rule = self.contract["workProcess"]
        script = str(telemetry._ROUTER_SCRIPT)
        for name in rule["interpreterNames"]:
            for operation in rule["operations"]:
                self.assertTrue(telemetry._router_work_argv(["/usr/bin/" + name] + rule["flags"] + [script, operation]))
        framework = (
            "/Library/Frameworks/"
            + rule["frameworkDirectories"][0]
            + "/Versions/3.12/Resources"
            + rule["frameworkInterpreterSuffix"]
        )
        self.assertTrue(telemetry._router_work_argv([framework] + rule["flags"] + [script, "work"]))
        self.assertTrue(_work_interpreter_in_contract(framework, self.contract))
        for argv in (
            ["/usr/bin/python3.9"] + rule["flags"] + [script, "work"],
            ["/usr/bin/python3", "-B", "-I", script, "work"],
            ["/usr/bin/python3"] + rule["flags"] + [script, "status"],
            ["/usr/bin/python3"] + rule["flags"] + [script + ".bak", "work"],
            ["/usr/bin/python3"] + rule["flags"] + [script, "work", "extra"],
            ["/tmp/Python.app/Contents/MacOS/Python"] + rule["flags"] + [script, "work"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(telemetry._router_work_argv(argv))
        self.assertFalse(_work_interpreter_in_contract("/usr/bin/python3.9", self.contract))

    # -- every closed key set: the contract's keys are read, one more or one fewer is not ----------

    def journal(self):
        """An empty private journal root, with the readers pointed at it."""
        base = self.root.parent
        os.mkdir(self.root, 0o700)
        os.mkdir(self.root / "notes", 0o700)
        os.mkdir(base / "launcher", 0o700)
        dual_read._private_write(self.root / "owner.lock", "")
        for name, value in (
            ("_ROUTER_ROOT", self.root),
            ("_ACTIVE_PATH", self.root / "active.json"),
            ("_ROUTER_SCRIPT", base / "pipeline_router.py"),
            ("_PENDING_PATH", base / "pending.json"),
            ("_LAUNCHER_ROOT", base / "launcher"),
            ("_READINESS_PATH", base / "launcher/readiness.json"),
        ):
            patcher = mock.patch.object(telemetry, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return base

    def variants(self, value):
        """(label, changed value) for one key more and for each key missing."""
        yield "extra key", dict(value, extra=1)
        for key in value:
            yield "without " + key, {name: item for name, item in value.items() if name != key}

    def publish(self, path, value):
        if os.path.lexists(path):
            os.unlink(path)
        dual_read._private_write(path, json.dumps(value))

    def test_contract_fence_keys_are_read_closed(self):
        self.journal()
        spec = self.contract["files"]["fence"]
        lock = os.stat(self.root / "owner.lock")
        path = self.root / "install-fence.json"
        for state in spec["states"]:
            for bound in (None, {"dev": lock.st_dev, "ino": lock.st_ino}):
                fence = {
                    "schemaVersion": 1,
                    "kind": spec["kind"],
                    "state": state,
                    "generation": "c" * 64,
                    "stamp": "20260927T120000Z",
                    "sinceUnix": 1.0,
                    "ownerLock": bound,
                }
                self.assertEqual(set(fence), set(spec["keys"]))
                self.publish(path, fence)
                install = telemetry._router_install_state(self.root)
                self.assertEqual(
                    (install["state"], install["ownerLockBound"]), (state, None if bound is None else True)
                )
        self.assertEqual(set(bound), set(spec["ownerLockKeys"]))
        changed = list(self.variants(fence)) + [
            ("ownerLock extra key", dict(fence, ownerLock=dict(bound, extra=1))),
            ("unknown state", dict(fence, state="done")),
            ("another kind", dict(fence, kind=spec["kind"] + "x")),
            ("schemaVersion true", dict(fence, schemaVersion=True)),
        ]
        for label, value in changed:
            with self.subTest(fence=label):
                self.publish(path, value)
                self.assertEqual(telemetry._router_install_state(self.root)["state"], "invalid")

    def test_contract_policy_keys_are_read_closed(self):
        self.journal()
        spec = self.contract["files"]["policy"]
        install = {"state": "installed", "inProgress": False, "ownerLockBound": True}
        path = self.root / "policy.json"
        self.assertEqual(telemetry._router_policy(self.root, install)["source"], "default")
        for concurrency in spec["concurrency"]:
            policy = {"schemaVersion": 1, "kind": spec["kind"], "concurrency": concurrency}
            self.assertEqual(set(policy), set(spec["keys"]))
            self.publish(path, policy)
            read = telemetry._router_policy(self.root, install)
            self.assertEqual((read["policy"], read["source"]), (concurrency, "file"))
        for label, value in list(self.variants(policy)) + [
            ("unknown concurrency", dict(policy, concurrency="many")),
            ("another kind", dict(policy, kind=spec["kind"] + "x")),
        ]:
            with self.subTest(policy=label):
                self.publish(path, value)
                read = telemetry._router_policy(self.root, install)
                self.assertEqual((read["policy"], read["source"]), (None, "invalid"))

    def test_contract_note_keys_are_read_closed(self):
        self.journal()
        spec = self.contract["files"]["note"]
        path = self.root / "notes/run-a.json"
        good = {
            "schemaVersion": 1,
            "kind": spec["kind"],
            "runId": "run-a",
            "inputSha256": "a" * 64,
            "operation": "work",
            "client": "claude",
            "host": "mac",
            "admission": "shared",
            "phase": "running",
            "resource": "mac-pair",
            "step": "backend",
            "sinceUnix": 1.0,
            "untilUnix": None,
            "pid": 4242,
        }
        self.assertEqual(set(good), set(spec["keys"]))
        accepted = [("admission", value) for value in spec["admissions"]]
        accepted += [("phase", value) for value in spec["phases"]] + [
            ("resource", value) for value in spec["resources"]
        ]
        accepted += [(key, value) for key, values in spec["enums"].items() for value in values]
        for key, value in accepted:
            with self.subTest(key=key, value=value):
                self.publish(path, dict(good, **{key: value}))
                self.assertEqual(telemetry._router_note(self.root, "run-a")[key], value)
        refused = list(self.variants(good))
        refused += [
            ("unknown " + key, dict(good, **{key: "other"}))
            for key in ("admission", "phase", "resource", "operation", "client", "host", "step")
        ]
        refused += [
            ("another kind", dict(good, kind=spec["kind"] + "x")),
            ("pid zero", dict(good, pid=0)),
            ("pid true", dict(good, pid=True)),
            ("null admission", dict(good, admission=None)),
            ("null phase", dict(good, phase=None)),
        ]
        for label, value in refused:
            with self.subTest(note=label):
                self.publish(path, value)
                self.assertIsNone(telemetry._router_note(self.root, "run-a"))
        self.publish(path, good)
        self.assertIsNone(telemetry._router_note(self.root, "run-b"))  # a note names its own run only

    def test_contract_marker_and_readiness_keys_are_read_closed(self):
        base = self.journal()
        now = 1_800_000_000.0
        spec = self.contract["files"]["marker"]
        anonymous = {"kind": spec["kind"], "started_unix": now - 5, "input_sha256": "f" * 64}
        router = dict(anonymous, runId="run-a" + spec["runIdSuffixes"][0], operation=spec["operations"][0])
        self.assertEqual((set(anonymous), set(router)), (set(spec["anonymousKeys"]), set(spec["routerKeys"])))
        self.assertEqual(telemetry._pending_marker_facts(anonymous, now)["pendingMarkerOwner"], "anonymous (legacy)")
        self.assertEqual(telemetry._pending_marker_facts(router, now)["pendingMarkerOwner"], router["runId"])
        for suffix in spec["runIdSuffixes"]:
            for operation in spec["operations"]:
                marker = dict(router, runId="run-a" + suffix, operation=operation)
                self.assertEqual(telemetry._marker_attribution(marker, {"run-a"}, now), "run-a")
        self.assertIsNone(telemetry._marker_attribution(anonymous, {"run-a"}, now))
        for label, value in self.variants(router):
            with self.subTest(marker=label):
                self.assertIsNone(telemetry._marker_attribution(value, {"run-a"}, now))
                self.assertIsNone(telemetry._pending_marker_facts(value, now)["pendingMarkerOwner"])
        future = now + self.contract["limits"]["markerFutureSeconds"]
        self.assertEqual(telemetry._marker_attribution(dict(router, started_unix=future), {"run-a"}, now), "run-a")
        self.assertIsNone(telemetry._marker_attribution(dict(router, started_unix=future + 1), {"run-a"}, now))

        spec = self.contract["files"]["readiness"]
        path = base / "launcher/readiness.json"
        receipt = {
            "schemaVersion": 1,
            "status": spec["status"],
            "observedAtUnix": now - 30,
            "source": spec["source"],
            "client": None,
            "chatId": None,
        }
        self.assertEqual(set(receipt), set(spec["keys"]))
        self.assertEqual(telemetry._launcher_readiness(now), ("absent", None))
        self.publish(path, receipt)
        self.assertEqual(telemetry._launcher_readiness(now), ("fresh", 30.0))
        fresh = self.contract["limits"]["readinessFreshSeconds"]
        self.assertEqual(telemetry._launcher_readiness(now - 30 + fresh)[0], "fresh")
        self.assertEqual(telemetry._launcher_readiness(now - 30 + fresh + 1)[0], "expired")
        for label, value in list(self.variants(receipt)) + [
            ("another status", dict(receipt, status="DONE")),
            ("a client", dict(receipt, client="codex")),
            ("future", dict(receipt, observedAtUnix=now + 2)),
        ]:
            with self.subTest(readiness=label):
                self.publish(path, value)
                self.assertEqual(telemetry._launcher_readiness(now), ("invalid", None))

    def test_contract_archive_keys_are_the_completion_envelope(self):
        spec = self.contract["files"]["archive"]
        digest = "a" * 64
        result = {
            "kind": spec["resultKind"],
            "schemaVersion": 1,
            "runId": "run-a",
            "requestSha256": digest,
            "status": "RESPONSE_VALIDATED",
            "advisoryOnly": True,
            "accepted": False,
            "authorizing": False,
            "filesApplied": False,
            "recoveryRequired": False,
            "tests": "NOT_RUN",
            "certification": "NOT_RUN",
        }
        record = {
            "schemaVersion": 1,
            "runId": "run-a",
            "inputSha256": digest,
            "result": result,
            "exitCode": 0,
            "stage": "final_validation_intent",
            "checkpoint": None,
            "finishedUnix": 1_800_000_000.0,
        }
        self.assertEqual(set(record), set(spec["keys"]))
        self.assertTrue(activity._completion_owner_bound(record, result, "run-a", result["status"]))
        for label, value in self.variants(record):
            with self.subTest(archive=label):
                self.assertFalse(activity._completion_owner_bound(value, result, "run-a", result["status"]))
        shown = activity._project_archive(record, 1_800_000_060.0)
        self.assertEqual((shown["runId"], shown["source"], shown["ageSeconds"]), ("run-a", "router-archive", 60.0))
        self.assertIsNone(activity._project_archive(dict(record, result=dict(result, kind="other")), 1_800_000_060.0))

    # -- the fixture writers against the contract (no reader involved) ---------------------------

    def assert_private(self, path, *, directory=False):
        info = os.lstat(path)
        self.assertTrue(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), path)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700 if directory else 0o600, path)
        self.assertEqual(info.st_uid, os.getuid())
        if not directory:
            self.assertEqual(info.st_nlink, 1, path)

    def test_per_run_fixture_writes_the_contract_files(self):
        files = self.contract["files"]
        state = runpy.run_path(str(_WRITERS["per-run"]))
        fence = runpy.run_path(str(_WRITERS["per-run"].with_name("router_fence.py")))
        self.assertEqual(state["CONTRACT_VERSION"], self.contract["version"])
        with state["RouterOwner"](self.root) as owner:
            fence["write"](self.root, "installed", "c" * 64, "20260927T120000Z")
            state["_atomic"](
                self.root / "policy.json",
                {"schemaVersion": 1, "kind": files["policy"]["kind"], "concurrency": "multi"},
                1024,
            )
            owner.claim(
                "run-a",
                "a" * 64,
                until=time.monotonic() + 2,
                meta={"client": "claude", "host": "mac", "operation": "work"},
            )
            self.assertFalse((self.root / "active/run-a.json").exists())  # claimed, not begun
            note = json.loads((self.root / "notes/run-a.json").read_text())
            self.assertEqual(
                (note["phase"], note["step"], note["client"], note["host"], note["operation"]),
                ("admitting", "pre-begin", "claude", "mac", "work"),
            )
            owner.begin("run-a", "a" * 64)
            self.assertEqual(json.loads((self.root / "notes/run-a.json").read_text())["phase"], "running")
            owner.note("waiting", "mac-pair", "backend", time.monotonic() + 30)
            started = json.loads((self.root / "active/run-a.json").read_text())
            self.assertEqual(set(started), set(files["record"]["startedKeys"]) | {"runLock"})
            self.assertEqual((started["stage"], started["checkpoint"]), ("started", None))
            owner.checkpoint("backend_draft", {"selectedHost": "mac"})
            owner.checkpoint("backend_review", {"selectedHost": "mac"})
            record = json.loads((self.root / "active/run-a.json").read_text())
            self.assertEqual(set(record), set(files["record"]["checkpointedKeys"]) | {"runLock"})
            self.assertEqual(set(record["checkpoint"]), set(files["record"]["checkpointKeys"]))
            self.assertEqual(
                (record["sequence"], record["checkpoint"]["sequence"], record["stage"]), (2, 2, "backend_review")
            )
            lock = os.stat(self.root / "locks/run-a.lock")
            self.assertEqual(record["runLock"], {"dev": lock.st_dev, "ino": lock.st_ino})
            note = json.loads((self.root / "notes/run-a.json").read_text())
            self.assertEqual(set(note), set(files["note"]["keys"]))
            self.assertEqual(
                (note["kind"], note["admission"], note["pid"]), (files["note"]["kind"], "shared", os.getpid())
            )
            self.assertEqual((note["phase"], note["resource"]), ("waiting", "mac-pair"))
            self.assertGreater(note["untilUnix"], note["sinceUnix"] + 20)
            written = json.loads((self.root / "install-fence.json").read_text())
            self.assertEqual(set(written), set(files["fence"]["keys"]))
            owner_lock = os.stat(self.root / "owner.lock")
            self.assertEqual(written["ownerLock"], {"dev": owner_lock.st_dev, "ino": owner_lock.st_ino})
            for name in self.contract["layouts"]["per-run"]["directories"]:
                self.assert_private(self.root / name, directory=True)
            for name in (
                "owner.lock",
                "locks/run-a.lock",
                "active/run-a.json",
                "notes/run-a.json",
                "install-fence.json",
                "policy.json",
            ):
                self.assert_private(self.root / name)
            # The locks: L0 shared under multi (another shared holder fits, an exclusive one
            # does not), L1 exclusive.
            for name, shared_fits in (("owner.lock", True), ("locks/run-a.lock", False)):
                fd = os.open(str(self.root / name), os.O_RDONLY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if shared_fits:
                        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    else:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
            owner.finish(
                {
                    "kind": "codemode.router.v1",
                    "status": "NOT_RUN",
                    "runId": "run-a",
                    "requestSha256": "a" * 64,
                    "accepted": False,
                    "advisoryOnly": True,
                },
                3,
            )
            archive = json.loads((self.root / "archive/run-a.json").read_text())
            self.assertEqual(set(archive), set(files["archive"]["keys"]))
            self.assert_private(self.root / "archive/run-a.json")
            self.assertFalse((self.root / "active/run-a.json").exists())
            self.assertFalse((self.root / "notes/run-a.json").exists())
        self.assertEqual(sorted(name for name in os.listdir(self.root / "active")), [])  # no staged file left
        # Without a multi policy the run is admitted exclusive.
        os.unlink(self.root / "policy.json")
        with state["RouterOwner"](self.root) as owner:
            owner.claim("run-b", "b" * 64, until=time.monotonic() + 2)
            note = json.loads((self.root / "notes/run-b.json").read_text())
            self.assertEqual((note["admission"], note["untilUnix"], note["client"]), ("exclusive", None, None))
            other = state["RouterOwner"](self.root)
            with other:
                with self.assertRaises(RuntimeError):
                    other.claim("run-c", "c" * 64, until=time.monotonic() + 0.05)

    def test_legacy_fixture_writes_the_contract_files(self):
        files = self.contract["files"]
        state = runpy.run_path(str(_WRITERS["legacy"]))
        self.assertEqual(state["CONTRACT_VERSION"], self.contract["version"])
        with state["RouterOwner"](self.root) as owner:
            owner.begin("legacy-a", "a" * 64)
            started = json.loads((self.root / "active.json").read_text())
            self.assertEqual(set(started), set(files["record"]["startedKeys"]))
            owner.checkpoint("backend_draft", {"selectedHost": "mac"})
            record = json.loads((self.root / "active.json").read_text())
            self.assertEqual(set(record), set(files["record"]["checkpointedKeys"]))
            self.assertEqual(set(record["checkpoint"]), set(files["record"]["checkpointKeys"]))
            self.assertEqual(
                sorted(os.listdir(self.root)),
                sorted(["active.json", "owner.lock"] + self.contract["layouts"]["legacy"]["directories"]),
            )
            for name in self.contract["layouts"]["legacy"]["directories"]:
                self.assert_private(self.root / name, directory=True)
            for name in ("owner.lock", "active.json"):
                self.assert_private(self.root / name)
            fd = os.open(str(self.root / "owner.lock"), os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            with self.assertRaises(RuntimeError):
                state["RouterOwner"](self.root).__enter__()
            owner.finish(
                {
                    "kind": "codemode.router.v1",
                    "status": "NOT_RUN",
                    "runId": "legacy-a",
                    "requestSha256": "a" * 64,
                    "accepted": False,
                    "advisoryOnly": True,
                },
                3,
            )
            self.assertEqual(
                set(json.loads((self.root / "archive/legacy-a.json").read_text())), set(files["archive"]["keys"])
            )
            self.assertFalse((self.root / "active.json").exists())

    def test_fixture_writers_stand_alone(self):
        """The fixtures import the standard library only (they run under `python -I -B`), and
        say what they are."""
        for path in sorted(_FIXTURES.glob("*/*.py")):
            with self.subTest(fixture=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("Contract fixture", text)
                imported = set()
                for node in ast.walk(ast.parse(text)):
                    if isinstance(node, ast.Import):
                        imported.update(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        imported.add((node.module or ".").split(".")[0])
                self.assertLessEqual(imported, {"fcntl", "itertools", "json", "os", "re", "time", "pathlib"})
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-W", "error", "-c",
                     "import runpy, sys; runpy.run_path(sys.argv[1])", str(path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                )
                self.assertEqual((result.returncode, result.stderr), (0, b""))

    # -- the prose document against the machine-readable contract -------------------------------

    def test_document_states_the_contract(self):
        text = _DOCUMENT_PATH.read_text(encoding="utf-8")
        contract = self.contract
        self.assertIn("Contract version: **%s**" % contract["version"], text)
        files = contract["files"]
        needles = [
            contract["readerContract"],
            contract["schema"],
            contract["installBarrierName"],
            "test_support/router_contract/contract.json",
            "test_router_contract.py",
            files["fence"]["kind"],
            files["policy"]["kind"],
            files["note"]["kind"],
            files["marker"]["kind"],
            files["archive"]["resultKind"],
        ]
        needles += ["`%s`" % value for value in contract["paths"].values()]
        needles += ["`%s`" % key for group in ("fence", "note") for key in files[group]["keys"]]
        needles += [
            "`%s`" % key
            for key in files["record"]["checkpointedKeys"]
            + files["record"]["checkpointKeys"]
            + files["archive"]["keys"]
            + files["marker"]["routerKeys"]
            + files["readiness"]["keys"]
        ]
        needles += ["`%s`" % state for state in files["fence"]["states"] + contract["rowStates"]]
        for needle in needles:
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        # Every limit appears in the limits table with its value.
        for name, value in contract["limits"].items():
            with self.subTest(limit=name):
                self.assertRegex(text, r"\| `%s` \| %s \|" % (name, format(value, ",")))

    def test_document_is_linked_and_carries_no_private_detail(self):
        text = _DOCUMENT_PATH.read_text(encoding="utf-8")
        self.assertIn("router-state-contract.md", (_REPOSITORY / "docs/README.md").read_text(encoding="utf-8"))
        self.assertIn("router-state-contract.md", (_REPOSITORY / "docs/development.md").read_text(encoding="utf-8"))
        self.assertNotIn(str(Path.home()), text)
        self.assertNotRegex(text, r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}")


class ArchiveJudgmentScanTests(unittest.TestCase):
    """Contract section 8: one archive file the reader cannot use is skipped; it never fails
    the sample."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(os.path.realpath(temp.name)) / "router"
        os.makedirs(self.root / "archive", mode=0o700)
        os.chmod(self.root, 0o700)
        patcher = mock.patch.object(telemetry, "_ROUTER_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        telemetry._JEV_CACHE.update(key=None, when=None, model=None)
        self.addCleanup(telemetry._JEV_CACHE.update, key=None, when=None, model=None)

    def archive(self, name, text, when):
        path = self.root / "archive" / name
        dual_read._private_write(path, text)
        os.utime(path, (when, when))
        return path

    def judged(self, name, when, **record):
        intake = {"status": "JUDGED", "kind": "chami.intake.typesafe.v1", "model": "jev-1"}
        return self.archive(name, json.dumps(dict({"result": {"stages": {"intake": intake}}}, **record)), when)

    def test_a_deeply_nested_archive_is_skipped_not_fatal(self):
        """A 2 MB archive nested a million deep is under the size limit and makes the JSON
        parser raise RecursionError.  It was not caught, so it failed every snapshot while the
        file stayed among the newest twelve.  It is one skipped file: the older judged archive
        behind it is still found."""
        now = 1_800_000_000.0
        self.judged("run-judged.json", now - 120, finishedUnix=now - 120)
        self.archive("run-deep.json", '{"x":' + "[" * 1000000 + "]" * 1000000 + "}", now - 60)
        self.assertEqual(telemetry._last_jev_judgment(now), (120.0, "jev-1"))
        components = {
            row["id"]: row
            for row in telemetry._route_components(
                {}, {"runtimeIntegrity": "VERIFIED", "hostBinding": "VERIFIED"}, {}, now
            )
        }
        self.assertEqual(components["jev"]["lastJudgedAgeSeconds"], 120.0)

    def test_an_archive_removed_during_the_scan_is_skipped_not_fatal(self):
        """A judged archive with no usable finish time is dated by its mtime; if it is removed
        between the read and that stat, it is one skipped file."""
        now = 1_800_000_000.0
        self.judged("run-older.json", now - 300, finishedUnix=now - 300)
        newest = self.judged("run-newest.json", now - 30)
        real_stat, real_read, read = Path.stat, telemetry._safe_file, []

        def safe_file(path, limit):
            value = real_read(path, limit)
            read.append(path)
            return value

        def gone(path, *args, **kwargs):
            if path == newest and newest in read:  # only once its content has been read
                raise FileNotFoundError(2, "removed", str(path))
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(telemetry, "_safe_file", safe_file), mock.patch.object(Path, "stat", gone):
            self.assertEqual(telemetry._last_jev_judgment(now), (300.0, "jev-1"))
        self.assertEqual(read[0], newest)
        telemetry._JEV_CACHE.update(key=None)
        self.assertEqual(telemetry._last_jev_judgment(now), (30.0, "jev-1"))

    def test_an_archive_removed_between_listing_and_stat_is_skipped_not_fatal(self):
        """The newest twelve are chosen by each entry's mtime.  If one file is removed after the
        directory is listed and before its entry is stat'ed, only that entry is dropped: the
        judged archive beside it is still found."""
        now = 1_800_000_000.0
        self.judged("run-judged.json", now - 120, finishedUnix=now - 120)
        self.archive("run-gone.json", "{}", now - 60)
        real_scandir = os.scandir

        class Gone:
            def __init__(self, entry):
                self.name, self.path, self._entry = entry.name, entry.path, entry

            def is_file(self, *args, **kwargs):
                return self._entry.is_file(*args, **kwargs)

            def stat(self, *args, **kwargs):
                raise FileNotFoundError(2, "removed", self.path)

        def scandir(path):
            return [Gone(entry) if entry.name == "run-gone.json" else entry for entry in real_scandir(path)]

        with mock.patch.object(telemetry.os, "scandir", scandir):
            self.assertEqual(telemetry._scan_jev_judgment(self.root / "archive"), (now - 120, "jev-1"))


if __name__ == "__main__":
    unittest.main()
