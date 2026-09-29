"""Tests for auto_unload (stdlib unittest; Python 3.9 and 3.14).

Every test drives AutoUnloader with a fake clock, fake snapshots and a fake unload path;
nothing here touches LM Studio, the real config, the real journal or the real lock.
"""
import errno
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock

import auto_unload as au

GEMMA = "google/gemma-4-26b-a4b-qat"
GEMMA3 = "google/gemma-3-4b"
QWEN = "qwen/qwen3.8-27b"
OTHER = "t/openai/gpt-oss-20b"
SECOND = "s/second-llm"
EMBED = "text-embedding-nomic-embed-text-v1.5"
MINUTE = 60.0


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_790_000_000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds


def row(key, *, state="idle", queued=0, instance=None, age=0.0, confirm=True, **extra):
    instance = instance or key
    value = {"id": instance, "name": key, "host": "mac", "state": state, "loaded": True,
             "queued": queued, "source": "lms-ps", "ageSeconds": age, "modelKey": key,
             "instanceId": instance, "loadedInstanceIds": [instance] if confirm else None,
             "metadata": {"type": "embedding" if "embed" in key.casefold() else "llm"}}
    value.update(extra)
    return value


def metadata(instance, *, ttl=None, kind="llm"):
    """telemetry's API metadata, which the same-id lms-ps row inherits in the merge."""
    return {"type": kind, "loadedInstances": [{"id": instance, "context": 8192, "parallel": 4,
                                               "remainingTtlSeconds": ttl}]}


def quiet_pipeline(**fields):
    value = {"status": "idle", "recoveryRequired": False, "pendingMarkerObserved": False,
             "pendingMarkerUnreadable": False, "pipelines": []}
    value.update(fields)
    return value


def quiet_mode(**fields):
    value = {"state": "inactive", "active": False,
             "runCounts": {"running": 0, "queued": 0, "unresolved": 0}}
    value.update(fields)
    return value


def snapshot(clock, rows, *, memory="ok", status="idle", age=0.2, lms="live", api="live", guard="live",
             **extra):
    value = {
        "sampledAt": clock.wall - age,
        "models": rows,
        "sources": [{"id": "lmstudio-api", "state": api}, {"id": "lms-ps", "state": lms},
                    {"id": "mem-guard", "state": guard}],
        "pipeline": quiet_pipeline(status=status),
        "onlineCodeMode": quiet_mode(),
        "activity": {"runs": []},
        "components": [{"id": "nisi", "state": "ready"}, {"id": "jev", "state": "configured"}],
        "memory": {"level": memory, "reasons": []},
    }
    value.update(extra)
    return value


class FakeControlError(Exception):
    """Shape of model_control.ControlError: status + message."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class FakeControl:
    """The ModelControl surface auto_unload uses: request('unload', id) and read()."""

    def __init__(self):
        self.calls = []
        self.status = {"status": "idle", "operationId": None, "message": "No model operation."}
        self.raise_next = None

    def unload(self, instance_id):
        if self.raise_next is not None:
            error, self.raise_next = self.raise_next, None
            raise error
        self.calls.append(instance_id)
        self.status = {"status": "running", "operationId": f"op{len(self.calls)}",
                       "action": "unload", "modelId": instance_id,
                       "message": "LM Studio unload requested; checking current state."}
        return dict(self.status)

    def read(self):
        return dict(self.status)

    def finish(self, result="succeeded"):
        self.status = dict(self.status, status=result, message=f"Model unload {result}.")


class Base(unittest.TestCase):
    def setUp(self):
        # subTest loops call setUp() again for a clean directory, clock and control.
        if getattr(self, "_tmp", None) is not None:
            self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = self.root / "config" / "agiw" / "auto-unload.json"
        self.journal = self.root / "state" / "inference-monitor" / "auto-unload.jsonl"
        self.clock = Clock()
        self.control = FakeControl()

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, *, default_enabled=True, **options):
        # Most behavior tests exercise an explicitly enabled policy. Keep the missing-file
        # default covered separately instead of letting it silently trigger model actions.
        if default_enabled and not os.path.lexists(self.config):
            self.write_config({"enabled": True})
        options.setdefault("unload_fn", self.control.unload)
        unloader = au.AutoUnloader(clock=self.clock.monotonic, now=self.clock.time,
                                   config_path=self.config, journal_path=self.journal, **options)
        self.addCleanup(unloader.close)
        return unloader

    def state_dir(self):
        self.journal.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.journal.parent, 0o700)

    def write_config(self, value, mode=0o600):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        data = value if isinstance(value, (str, bytes)) else json.dumps(value)
        if isinstance(data, str):
            data = data.encode()
        self.config.write_bytes(data)
        os.chmod(self.config, mode)

    def write_journal(self, rows):
        self.state_dir()
        self.journal.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows))
        os.chmod(self.journal, 0o600)

    def journal_row(self, key, ts, result="requested"):
        return {"ts": ts, "attemptTs": ts, "model": key, "instanceId": key, "idleSeconds": 1200,
                "reason": "idle", "memoryLevel": "ok", "result": result, "message": None, "operationId": None}

    def drive(self, unloader, seconds, make_snapshot, step=5.0):
        """Tick every `step` seconds for `seconds`, building each snapshot at the current time."""
        elapsed = 0.0
        state = None
        while elapsed < seconds:
            self.clock.advance(step)
            elapsed += step
            state = unloader.tick(make_snapshot())
        return state

    def journal_rows(self):
        if not self.journal.exists():
            return []
        return [json.loads(line) for line in self.journal.read_text().splitlines() if line]

    def pair_and(self, *extra):
        return [row(GEMMA), row(QWEN), *extra]

    def live(self, *extra):
        """The pair plus `extra`, minus every instance an unload was requested for (it landed)."""
        return [item for item in self.pair_and(*extra) if item["id"] not in self.control.calls]

    def until_call(self, unloader, make_snapshot, limit, step=5.0):
        """Tick until one more unload is requested; return the seconds it took."""
        before = len(self.control.calls)
        elapsed = 0.0
        while len(self.control.calls) == before:
            self.assertLess(elapsed, limit, "no unload within the limit")
            self.clock.advance(step)
            elapsed += step
            unloader.tick(make_snapshot())
        return elapsed

    def within(self, fn, seconds=5.0):
        """Run fn on a thread; fail (instead of hanging the suite) if it blocks."""
        result = {}

        def target():
            try:
                result["value"] = fn()
            except BaseException as error:  # re-raised on the test thread
                result["error"] = error

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(seconds)
        self.assertFalse(thread.is_alive(), "blocked (a FIFO open must never block)")
        if "error" in result:
            raise result["error"]
        return result.get("value")


class StartupGraceTests(Base):
    def test_first_sample_counts_as_active_even_under_critical_memory(self):
        unloader = self.make()
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)), memory="critical")
        state = unloader.tick(make())
        self.assertEqual(self.control.calls, [])
        # Critical has not held yet, so the normal threshold still applies.
        self.assertEqual([(c["model"], c["idleSeconds"], c["eta"]) for c in state["candidates"]],
                         [(OTHER, 0, 1200)])
        state = self.drive(unloader, 60, make)
        self.assertEqual((state["thresholdSeconds"], state["candidates"][0]["eta"]), (300, 240))
        self.assertEqual(self.control.calls, [])

    def test_unloads_exactly_when_twenty_observed_minutes_have_passed(self):
        unloader = self.make()
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)))
        unloader.tick(make())
        self.drive(unloader, 20 * MINUTE - 5, make)
        self.assertEqual(self.control.calls, [])
        self.drive(unloader, 5, make)
        self.assertEqual(self.control.calls, [OTHER])

    def test_newly_loaded_model_gets_its_own_grace(self):
        unloader = self.make()
        self.drive(unloader, 30 * MINUTE, lambda: snapshot(self.clock, self.pair_and()))
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)))
        self.drive(unloader, 19 * MINUTE, make)
        self.assertEqual(self.control.calls, [])
        self.drive(unloader, 1 * MINUTE + 5, make)
        self.assertEqual(self.control.calls, [OTHER])

    def test_generation_and_queued_work_restart_the_idle_clock(self):
        for busy in (row(OTHER, state="generating"), row(OTHER, state="busy"),
                     row(OTHER, state="processing"), row(OTHER, queued=2)):
            with self.subTest(busy=busy["state"], queued=busy["queued"]):
                self.setUp()
                unloader = self.make()
                idle = lambda: snapshot(self.clock, self.pair_and(row(OTHER)))
                unloader.tick(idle())
                self.drive(unloader, 15 * MINUTE, idle)
                self.clock.advance(5)
                unloader.tick(snapshot(self.clock, self.pair_and(busy)))
                self.drive(unloader, 19 * MINUTE, idle)
                self.assertEqual(self.control.calls, [])
                self.drive(unloader, 1 * MINUTE + 5, idle)
                self.assertEqual(self.control.calls, [OTHER])
                self.tearDown()


class RequestEvidenceTests(Base):
    """LM Studio's per-instance remainingTtlSeconds only goes up when a request arrives."""

    def minutes_until_unload(self, ttl_at, limit=40):
        unloader = self.make()
        elapsed = {"s": 0}

        def make():
            ttl = ttl_at(elapsed["s"])
            return snapshot(self.clock, self.pair_and(row(OTHER, metadata=metadata(OTHER, ttl=ttl))))
        unloader.tick(make())
        while elapsed["s"] < limit * MINUTE:
            self.clock.advance(5)
            elapsed["s"] += 5
            unloader.tick(make())
            if self.control.calls:
                return elapsed["s"] / MINUTE
        return None

    def test_a_ttl_refreshed_between_samples_counts_as_use(self):
        # A request every minute, each shorter than a sample: never seen busy, TTL reset each time.
        self.assertIsNone(self.minutes_until_unload(lambda s: 3600 - (s % 60)))

    def test_a_ttl_counting_down_or_absent_is_no_evidence(self):
        self.assertEqual(self.minutes_until_unload(lambda s: 3600 - s), 20)
        self.setUp()
        self.assertEqual(self.minutes_until_unload(lambda s: None), 20)

    def test_ttl_from_a_separate_api_row_counts_too(self):
        # An alias lms-ps row keeps no metadata; the API row for the key carries the TTL.
        unloader = self.make()
        elapsed = {"s": 0}

        def make():
            api = {"id": OTHER, "host": "mac", "source": "lmstudio-api", "loaded": True, "state": "loaded",
                   "modelKey": OTHER, "loadedInstanceIds": [OTHER],
                   "metadata": metadata(OTHER, ttl=600 - (elapsed["s"] % 120))}
            return snapshot(self.clock, self.pair_and(row(OTHER), api))
        unloader.tick(make())
        while elapsed["s"] < 40 * MINUTE:
            self.clock.advance(5)
            elapsed["s"] += 5
            state = unloader.tick(make())
        self.assertEqual(self.control.calls, [])
        self.assertLess(state["candidates"][0]["idleSeconds"], 120)


class ThresholdTests(Base):
    def minutes_until_unload(self, memory, config=None, limit=40, guard="live"):
        if config is not None:
            self.write_config(dict(config, enabled=True))
        unloader = self.make()
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)), memory=memory, guard=guard)
        unloader.tick(make())
        for second in range(5, int(limit * MINUTE) + 5, 5):
            self.clock.advance(5)
            unloader.tick(make())
            if self.control.calls:
                return second / MINUTE
        return None

    def test_levels_select_the_threshold(self):
        for memory, minutes in (("ok", 20), ("watch", 20), ("unknown", 20), ("bogus", 20),
                                ("tight", 5), ("critical", 5)):
            with self.subTest(memory=memory):
                self.setUp()
                self.assertEqual(self.minutes_until_unload(memory), minutes)
                self.tearDown()

    def test_configured_minutes_apply_and_tight_never_exceeds_normal(self):
        self.assertEqual(self.minutes_until_unload("tight", {"tightIdleMinutes": 3}), 3)
        self.setUp()
        self.assertEqual(self.minutes_until_unload("ok", {"idleMinutes": 7}), 7)
        self.setUp()
        self.assertEqual(self.minutes_until_unload("tight", {"idleMinutes": 6, "tightIdleMinutes": 30}), 6)
        self.setUp()
        # One minute: the tight threshold and the 60 s hold are met on the same tick.
        self.assertEqual(self.minutes_until_unload("critical", {"tightIdleMinutes": 1}), 1)

    def test_a_stale_or_unavailable_mem_guard_reads_as_unknown(self):
        for guard in ("unavailable", "error", "stale"):
            with self.subTest(guard=guard):
                self.setUp()
                self.assertEqual(self.minutes_until_unload("critical", guard=guard), 20)
                self.tearDown()
        self.setUp()
        unloader = self.make()
        state = unloader.tick(snapshot(self.clock, self.pair_and(), memory="tight", guard="unavailable"))
        self.assertEqual(state["memoryLevel"], "unknown")

    def test_memory_turning_tight_unloads_an_already_idle_model_once_it_holds(self):
        unloader = self.make()
        ok = lambda: snapshot(self.clock, self.pair_and(row(OTHER)), memory="ok")
        tight = lambda: snapshot(self.clock, self.pair_and(row(OTHER)), memory="tight")
        unloader.tick(ok())
        self.drive(unloader, 7 * MINUTE, ok)
        self.clock.advance(1)
        state = unloader.tick(tight())
        self.assertEqual(self.control.calls, [])  # one tight sample is not a trend
        self.assertEqual(state["thresholdSeconds"], 1200)
        state = self.drive(unloader, 55, tight)
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["thresholdSeconds"], 1200)
        state = self.drive(unloader, 5, tight)
        self.assertEqual(self.control.calls, [OTHER])
        self.assertEqual(state["thresholdSeconds"], 300)
        [entry] = self.journal_rows()
        self.assertEqual((entry["reason"], entry["memoryLevel"]), ("idle-tight-memory", "tight"))
        self.assertGreaterEqual(entry["idleSeconds"], 8 * 60)

    def test_flapping_tight_samples_never_switch_the_threshold(self):
        make = lambda memory: (lambda: snapshot(self.clock, self.pair_and(row(OTHER)), memory=memory))
        for name, spike, calm in (("every-30s", 5, 25), ("every-other-sample", 5, 5)):
            with self.subTest(pattern=name):
                self.setUp()
                unloader = self.make()
                unloader.tick(make("ok")())
                self.drive(unloader, 6 * MINUTE, make("ok"))
                for _ in range(int(10 * MINUTE / (spike + calm))):
                    self.drive(unloader, spike, make("critical"))
                    self.drive(unloader, calm, make("ok"))
                self.assertEqual(self.control.calls, [])
                self.tearDown()

    def test_a_gap_in_tight_samples_restarts_the_hold(self):
        make = lambda memory, **options: (
            lambda: snapshot(self.clock, self.pair_and(row(OTHER)), memory=memory, **options))
        for name, gap in (("mem-guard-unavailable", make("tight", guard="unavailable")),
                          ("stale-sample", make("tight", age=3.5))):
            with self.subTest(gap=name):
                self.setUp()
                unloader = self.make()
                unloader.tick(make("ok")())
                self.drive(unloader, 10 * MINUTE, make("ok"))
                self.drive(unloader, 50, make("tight"))
                self.drive(unloader, 5, gap)
                # The hold restarts at the first tight sample after the gap (5 s later).
                self.drive(unloader, 60, make("tight"))
                self.assertEqual(self.control.calls, [])
                self.drive(unloader, 5, make("tight"))
                self.assertEqual(self.control.calls, [OTHER])
                self.tearDown()

    def test_embedding_models_keep_the_normal_threshold_under_tight_memory(self):
        vectors = "lab/vectors"
        unloader = self.make()
        make = lambda: snapshot(self.clock, self.live(
            row(OTHER), row(EMBED), row(vectors, metadata=metadata(vectors, kind="embedding"))), memory="tight")
        unloader.tick(make())
        self.drive(unloader, 19 * MINUTE, make)
        self.assertEqual(self.control.calls, [OTHER])
        self.drive(unloader, 1 * MINUTE + 10, make)
        self.assertEqual(sorted(self.control.calls[1:]), sorted([EMBED, vectors]))
        reasons = {entry["instanceId"]: entry["reason"] for entry in self.journal_rows()}
        self.assertEqual(reasons, {OTHER: "idle-tight-memory", EMBED: "idle", vectors: "idle"})


class ProtectionTests(Base):
    def test_route_pair_is_never_unloaded(self):
        unloader = self.make()
        rows = [row(GEMMA), row(QWEN)]
        state = self.drive(unloader, 3 * 3600, lambda: snapshot(self.clock, rows, memory="critical"), step=10)
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["candidates"], [])
        self.assertEqual(state["protect"], [GEMMA, QWEN])

    def test_selected_resident_pair_is_protected_and_unselected_qwen_can_unload(self):
        unloader = self.make()
        rows = [row(GEMMA), row(GEMMA3), row(QWEN)]
        make = lambda: snapshot(self.clock, [item for item in rows if item["id"] not in self.control.calls],
                                memory="critical")
        state = self.drive(unloader, 25 * MINUTE, make)
        self.assertEqual(self.control.calls, [QWEN])
        self.assertEqual(state["protect"], [GEMMA, GEMMA3])

    def test_uncertain_resident_pair_blocks_all_unloads(self):
        unloader = self.make()
        rows = [row(GEMMA), row(QWEN, instance=f"{QWEN}:3")]
        state = self.drive(unloader, 30 * MINUTE,
                           lambda: snapshot(self.clock, rows, memory="critical"))
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["candidates"], [])
        self.assertEqual(state["blocked"], "Nisi resident model pair is uncertain")
        self.assertEqual(state["protect"], [])

    def test_single_resident_model_blocks_all_unloads(self):
        unloader = self.make()
        state = self.drive(unloader, 30 * MINUTE,
                           lambda: snapshot(self.clock, [row(GEMMA)], memory="critical"))
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["candidates"], [])
        self.assertEqual(state["blocked"], "Nisi resident model pair is uncertain")

    def test_suffixed_pair_instance_without_a_model_key_is_still_protected(self):
        unloader = self.make()
        rows = self.pair_and(row(QWEN, instance=f"{QWEN}:3", modelKey=None))
        state = self.drive(unloader, 30 * MINUTE, lambda: snapshot(self.clock, rows, memory="critical"))
        self.assertEqual(state["candidates"], [])
        self.assertEqual(self.control.calls, [])

    def test_config_cannot_remove_the_pair_but_can_add(self):
        self.write_config({"protect": [OTHER], "enabled": True})
        unloader = self.make()
        make = lambda: snapshot(self.clock, self.live(row(OTHER), row(EMBED)), memory="tight")
        state = self.drive(unloader, 30 * MINUTE, make)
        self.assertEqual(self.control.calls, [EMBED])
        self.assertEqual(state["protect"], [GEMMA, QWEN, OTHER])
        self.write_config({"protect": [], "enabled": True})
        state = self.drive(unloader, 30 * MINUTE, make)
        self.assertEqual(state["protect"], [GEMMA, QWEN])
        self.assertEqual(self.control.calls, [EMBED, OTHER])


class RouteGateTests(Base):
    def assert_blocked(self, make_blocked, expect=None, **options):
        unloader = self.make(**options)
        state = self.drive(unloader, 25 * MINUTE, make_blocked)
        self.assertEqual(self.control.calls, [])
        self.assertIsNotNone(state["blocked"])
        if expect:
            self.assertIn(expect, state["blocked"])
        # Idle kept accruing while blocked: the first clean sample unloads.
        self.clock.advance(1)
        unloader.tick(snapshot(self.clock, self.pair_and(row(OTHER))))
        return unloader

    def test_router_statuses_other_than_idle_block(self):
        for status in ("running", "queued", "unresolved", "recovery-required", "installing",
                       "unknown", "added-later", None):
            with self.subTest(status=status):
                self.setUp()
                self.assert_blocked(lambda: snapshot(self.clock, self.pair_and(row(OTHER)), status=status),
                                    "router status")
                self.assertEqual(self.control.calls, [OTHER])
                self.tearDown()

    def test_stale_router_record_blocks_until_it_is_reconciled(self):
        def real_shape():
            # telemetry counts every dead, non-changing run row as unresolved.
            value = snapshot(self.clock, self.pair_and(row(OTHER)), status="stale")
            value["onlineCodeMode"]["runCounts"]["unresolved"] = 1
            return value

        def unresolved_only():
            value = snapshot(self.clock, self.pair_and(row(OTHER)))
            value["onlineCodeMode"]["runCounts"]["unresolved"] = 2
            return value

        for name, make in (("stale", real_shape), ("stale-without-count",
                                                   lambda: snapshot(self.clock, self.pair_and(row(OTHER)), status="stale")),
                           ("unresolved-count", unresolved_only)):
            with self.subTest(case=name):
                self.setUp()
                self.assert_blocked(make, au.UNRESOLVED_ROUTE)
                self.assertEqual(self.control.calls, [OTHER])
                self.tearDown()

    def test_nisi_marker_recovery_live_runs_and_reshaped_fields_block(self):
        def with_pipeline(**fields):
            def make():
                value = snapshot(self.clock, self.pair_and(row(OTHER)))
                value["pipeline"].update(fields)
                return value
            return make

        def without(key):
            def make():
                value = snapshot(self.clock, self.pair_and(row(OTHER)))
                del value["pipeline"][key]
                return value
            return make

        def with_mode(**fields):
            return lambda: snapshot(self.clock, self.pair_and(row(OTHER)), onlineCodeMode=quiet_mode(**fields))

        def with_counts(counts):
            return with_mode(runCounts=counts)

        def top(**fields):
            return lambda: snapshot(self.clock, self.pair_and(row(OTHER)), **fields)

        cases = {
            "marker": (with_pipeline(pendingMarkerObserved=True), "pending marker"),
            "marker-missing": (without("pendingMarkerObserved"), "pending marker"),
            "marker-unreadable": (with_pipeline(pendingMarkerUnreadable=True), "unreadable"),
            "marker-unreadable-missing": (without("pendingMarkerUnreadable"), "unreadable"),
            "recovery": (with_pipeline(recoveryRequired=True), "recovery"),
            "recovery-missing": (without("recoveryRequired"), "recovery"),
            "live-run": (with_pipeline(pipelines=[{"runId": "r1", "status": "unresolved", "live": True}]), "live"),
            "listed-running": (with_pipeline(pipelines=[{"runId": "r1", "status": "running", "live": False}]), "live"),
            "run-list-garbage": (with_pipeline(pipelines="x"), "run list"),
            "run-list-missing": (without("pipelines"), "run list"),
            "no-pipeline": (top(pipeline=None), "router state"),
            "ocm-processing": (with_mode(state="processing", active=True), "processing"),
            "ocm-missing": (top(onlineCodeMode=None), "Online Code Mode state"),
            "ocm-activity-unknown": (with_mode(state="unknown", active=None), "activity is unknown"),
            "ocm-queued": (with_counts({"running": 0, "queued": 1, "unresolved": 0}), "running or queued"),
            "ocm-running": (with_counts({"running": 2, "queued": 0, "unresolved": 0}), "running or queued"),
            "counts-missing": (with_mode(runCounts=None), "run counts"),
            "counts-renamed": (with_counts({"running": 0, "queued": 0, "stale": 0}), "run counts"),
            "counts-float": (with_counts({"running": 0.0, "queued": 0, "unresolved": 0}), "run counts"),
            "counts-bool": (with_counts({"running": False, "queued": 0, "unresolved": 0}), "run counts"),
            "counts-negative": (with_counts({"running": -1, "queued": 0, "unresolved": 0}), "run counts"),
            "activity-running": (top(activity={"runs": [{"runId": "r", "activity": "running"}]}), "activity feed"),
            "activity-missing": (top(activity=None), "route activity"),
            "activity-runs-garbage": (top(activity={"runs": None}), "route activity"),
            "activity-row-garbage": (top(activity={"runs": [5]}), "route activity"),
            "nisi-in-use": (top(components=[{"id": "nisi", "state": "in-use"}]), "Nisi"),
            "components-missing": (top(components=None), "components"),
        }
        for name, (make, expect) in cases.items():
            with self.subTest(case=name):
                self.setUp()
                self.assert_blocked(make, expect)
                self.assertEqual(self.control.calls, [OTHER])
                self.tearDown()

    def test_online_code_check_or_model_operation_blocks_through_busy_fn(self):
        busy = {"value": "Online Code check running"}
        unloader = self.assert_blocked(lambda: snapshot(self.clock, self.pair_and(row(OTHER))),
                                       "Online Code check running", busy_fn=lambda: busy["value"])
        self.assertEqual(self.control.calls, [])
        busy["value"] = None
        self.clock.advance(1)
        unloader.tick(snapshot(self.clock, self.pair_and(row(OTHER))))
        self.assertEqual(self.control.calls, [OTHER])

    def test_busy_check_failure_blocks(self):
        def broken():
            raise RuntimeError("probe failed")
        unloader = self.make(busy_fn=broken)
        state = self.drive(unloader, 25 * MINUTE, lambda: snapshot(self.clock, self.pair_and(row(OTHER))))
        self.assertEqual(self.control.calls, [])
        self.assertIn("busy check failed", state["blocked"])


class FreshRouteCheckTests(Base):
    """route_check_fn re-reads the router right before the unload call."""

    def test_a_route_admitted_after_the_sample_stops_the_unload(self):
        fresh = {"value": {"pipeline": quiet_pipeline(status="queued"), "onlineCodeMode": quiet_mode()}}
        consulted = []

        def check():
            consulted.append(self.clock.mono)
            return fresh["value"]
        unloader = self.make(route_check_fn=check)
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)))
        unloader.tick(make())
        self.drive(unloader, 20 * MINUTE - 5, make)
        self.assertEqual(consulted, [])  # only consulted when an unload is about to happen
        state = self.drive(unloader, 5 * MINUTE, make)
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["blocked"], "fresh router check: router status is queued")
        self.assertEqual((state["unloadsLastHour"], self.journal_rows()), (0, []))
        fresh["value"] = {"pipeline": quiet_pipeline(), "onlineCodeMode": quiet_mode(
            runCounts={"running": 1, "queued": 0, "unresolved": 0})}
        state = self.drive(unloader, 5, make)
        self.assertEqual(state["blocked"], "fresh router check: Online Code Mode has running or queued routes")
        fresh["value"] = {"pipeline": quiet_pipeline(), "onlineCodeMode": quiet_mode()}
        self.drive(unloader, 5, make)
        self.assertEqual(self.control.calls, [OTHER])

    def test_a_failing_or_garbled_fresh_check_blocks(self):
        def broken():
            raise OSError("router journal unreadable")
        for name, check, expect in (("raises", broken, "failed"), ("none", lambda: None, "nothing usable"),
                                    ("reshaped", lambda: {"pipeline": quiet_pipeline()}, "Online Code Mode state")):
            with self.subTest(case=name):
                self.setUp()
                unloader = self.make(route_check_fn=check)
                state = self.drive(unloader, 25 * MINUTE, lambda: snapshot(self.clock, self.pair_and(row(OTHER))))
                self.assertEqual(self.control.calls, [])
                self.assertTrue(state["blocked"].startswith("fresh router check:"))
                self.assertIn(expect, state["blocked"])
                self.tearDown()


class FeedTests(Base):
    def test_stale_or_future_samples_and_dead_sources_block(self):
        cases = {"old": dict(age=3.5), "future": dict(age=-1.0), "lms-error": dict(lms="error"),
                 "api-unavailable": dict(api="unavailable")}
        for name, options in cases.items():
            with self.subTest(case=name):
                self.setUp()
                unloader = self.make()
                state = self.drive(unloader, 40 * MINUTE,
                                   lambda: snapshot(self.clock, self.pair_and(row(OTHER)), **options))
                self.assertEqual(self.control.calls, [])
                self.assertIsNotNone(state["blocked"])
                self.assertEqual(state["candidates"], [])
                self.tearDown()

    def test_stale_row_counts_as_unobserved(self):
        unloader = self.make()
        state = self.drive(unloader, 40 * MINUTE,
                           lambda: snapshot(self.clock, self.pair_and(row(OTHER, age=3.5))))
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["candidates"][0]["idleSeconds"], 0)

    def test_observation_gap_restarts_the_idle_clock(self):
        unloader = self.make()
        fresh = lambda: snapshot(self.clock, self.pair_and(row(OTHER)))
        unloader.tick(fresh())
        self.drive(unloader, 19 * MINUTE, fresh)
        self.drive(unloader, 30, lambda: snapshot(self.clock, self.pair_and(row(OTHER)), lms="error"))
        self.drive(unloader, 19 * MINUTE, fresh)
        self.assertEqual(self.control.calls, [])
        self.drive(unloader, 1 * MINUTE + 5, fresh)
        self.assertEqual(self.control.calls, [OTHER])

    def test_one_missed_sample_inside_the_gap_allowance_keeps_idle(self):
        unloader = self.make()
        fresh = lambda: snapshot(self.clock, self.pair_and(row(OTHER)))
        unloader.tick(fresh())
        self.drive(unloader, 19 * MINUTE, fresh, step=4)
        self.drive(unloader, 4, lambda: snapshot(self.clock, self.pair_and(row(OTHER)), lms="error"), step=4)
        self.drive(unloader, 1 * MINUTE, fresh, step=4)
        self.assertEqual(self.control.calls, [OTHER])


class RateLimitTests(Base):
    def test_at_most_one_unload_per_tick_most_idle_first(self):
        unloader = self.make()
        unloader.tick(snapshot(self.clock, self.live(row("z/early"))))
        make_three = lambda: snapshot(self.clock, self.live(row(OTHER), row(EMBED), row("z/early")))
        self.until_call(unloader, make_three, 21 * MINUTE)
        self.assertEqual(self.control.calls, ["z/early"])  # seen 5 s before the others
        self.clock.advance(5)
        unloader.tick(make_three())
        self.assertEqual(self.control.calls, ["z/early", EMBED])  # equal idle: still one per tick
        self.clock.advance(1)
        unloader.tick(make_three())
        self.assertEqual(self.control.calls, ["z/early", EMBED, OTHER])

    def test_same_model_waits_two_minutes_after_an_attempt(self):
        unloader = self.make()
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)))  # the unload never lands
        self.until_call(unloader, make, 21 * MINUTE)
        state = self.drive(unloader, 115, make)
        self.assertEqual(self.control.calls, [OTHER])
        self.assertEqual(state["candidates"][0]["blocked"], "auto-unloaded less than 2 min ago")
        self.drive(unloader, 5, make)
        self.assertEqual(self.control.calls, [OTHER, OTHER])

    def test_cooldown_covers_every_instance_of_the_model_key(self):
        unloader = self.make()
        make = lambda: snapshot(self.clock, self.live(
            row(OTHER, loadedInstanceIds=[OTHER, f"{OTHER}:2"]), row(OTHER, instance=f"{OTHER}:2", confirm=False)))
        state = self.drive(unloader, 21 * MINUTE, make)
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["blocked"], "Nisi resident model pair is uncertain")
        self.assertEqual(state["candidates"], [])

    def test_hourly_limit(self):
        models = [row(f"lab/model-{index}") for index in range(8)]
        make = lambda: snapshot(self.clock, self.live(*models), memory="tight")
        unloader = self.make()
        state = self.drive(unloader, 10 * MINUTE, make)
        self.assertEqual(len(self.control.calls), 6)
        self.assertEqual(state["unloadsLastHour"], 6)
        self.assertEqual(state["blocked"], "hourly limit reached (6/6)")
        self.drive(unloader, 45 * MINUTE, make)
        self.assertEqual(len(self.control.calls), 6)
        self.drive(unloader, 15 * MINUTE, make)
        self.assertEqual(len(self.control.calls), 8)

    def test_hourly_count_survives_a_restart(self):
        self.write_config({"maxPerHour": 2, "enabled": True})
        models = [row(f"lab/model-{index}") for index in range(4)]
        make = lambda: snapshot(self.clock, self.live(*models), memory="tight")
        first = self.make()
        self.drive(first, 10 * MINUTE, make)
        self.assertEqual(len(self.control.calls), 2)
        first.close()
        restarted = self.make()
        self.assertEqual(restarted.state()["unloadsLastHour"], 2)
        state = self.drive(restarted, 30 * MINUTE, make)
        self.assertEqual(len(self.control.calls), 2)
        self.assertEqual(state["blocked"], "hourly limit reached (2/2)")
        self.drive(restarted, 30 * MINUTE, make)
        self.assertEqual(len(self.control.calls), 4)

    def test_cooldown_survives_a_restart(self):
        self.write_config({"tightIdleMinutes": 1, "enabled": True})
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)), memory="tight")  # never lands
        first = self.make()
        self.until_call(first, make, 5 * MINUTE)
        first.close()
        restarted = self.make()
        # Eligible again after 60 s of grace and hold, but its cooldown runs 120 s from the attempt.
        self.drive(restarted, 115, make)
        self.assertEqual(self.control.calls, [OTHER])
        self.drive(restarted, 5, make)
        self.assertEqual(self.control.calls, [OTHER, OTHER])

    def test_rows_dated_in_the_future_count_as_now_after_a_clock_step_back(self):
        self.write_config({"tightIdleMinutes": 1, "enabled": True})
        self.write_journal([self.journal_row(OTHER, self.clock.wall + 600)])
        unloader = self.make()
        state = unloader.state()
        self.assertEqual(state["unloadsLastHour"], 1)
        self.assertEqual(state["recent"][0]["ageSeconds"], 0.0)
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)), memory="tight")
        self.drive(unloader, 115, make)
        self.assertEqual(self.control.calls, [])  # cooldown from now, not from 10 min ahead
        self.drive(unloader, 10, make)
        self.assertEqual(self.control.calls, [OTHER])

    def test_a_future_dated_row_leaves_the_budget_one_monotonic_hour_later(self):
        self.write_config({"maxPerHour": 1, "enabled": True})
        self.write_journal([self.journal_row(SECOND, self.clock.wall + 600)])
        unloader = self.make()
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)))
        state = self.drive(unloader, 59 * MINUTE, make)
        self.assertEqual(state["blocked"], "hourly limit reached (1/1)")
        self.drive(unloader, 1 * MINUTE + 5, make)
        self.assertEqual(self.control.calls, [OTHER])

    def test_waits_for_the_previous_unload_to_confirm(self):
        unloader = self.make(status_fn=self.control.read)
        make = lambda: snapshot(self.clock, self.live(row(OTHER), row(SECOND)), memory="tight")
        state = self.drive(unloader, 5.5 * MINUTE, make)
        self.assertEqual(self.control.calls, [OTHER])
        self.assertEqual(state["blocked"], "waiting for the previous auto-unload to confirm")
        self.control.finish("succeeded")
        self.clock.advance(1)
        unloader.tick(make())
        self.assertEqual(self.control.calls, [OTHER, SECOND])
        rows = self.journal_rows()
        self.assertEqual([r["result"] for r in rows], ["requested", "succeeded", "requested"])
        self.assertEqual(rows[1]["attemptTs"], rows[0]["ts"])
        self.assertEqual((rows[1]["operationId"], rows[1]["message"]), ("op1", "Model unload succeeded."))
        self.drive(unloader, 55, make)
        self.assertEqual(len(self.journal_rows()), 3)
        self.drive(unloader, 10, make)
        self.assertEqual([r["result"] for r in self.journal_rows()][-1], "unconfirmed")
        self.assertEqual(self.control.calls, [OTHER, SECOND])

    def test_an_operation_that_replaced_ours_is_unconfirmed_at_once(self):
        unloader = self.make(status_fn=self.control.read)
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)))
        self.until_call(unloader, make, 21 * MINUTE)
        self.control.status = {"status": "running", "operationId": "someone-else", "message": "load"}
        self.clock.advance(1)
        unloader.tick(make())
        rows = self.journal_rows()
        self.assertEqual([r["result"] for r in rows], ["requested", "unconfirmed"])
        self.assertEqual(rows[1]["message"], "another model operation replaced it before its result was read")


class SingleOwnerTests(Base):
    def test_only_one_unloader_on_the_machine_acts(self):
        first_control, second_control = FakeControl(), FakeControl()
        first = self.make(unload_fn=first_control.unload)
        second = self.make(unload_fn=second_control.unload)
        models = [row(f"lab/model-{index}") for index in range(12)]
        gone = set()
        for _ in range(int(3600 / 5)):
            self.clock.advance(5)
            current = snapshot(self.clock, [item for item in self.pair_and(*models) if item["id"] not in gone],
                               memory="tight")
            first.tick(current)
            state = second.tick(current)
            gone |= set(first_control.calls) | set(second_control.calls)
        self.assertEqual((len(first_control.calls), second_control.calls), (6, []))
        self.assertEqual(state["blocked"], "another Monitor owns auto-unload")
        self.assertEqual(len(self.journal_rows()), 6)
        # The owner exits: the other takes over and inherits the budget from the journal.
        first.close()
        live = lambda: snapshot(self.clock, [item for item in self.pair_and(*models)
                                             if item["id"] not in set(first_control.calls) | set(second_control.calls)],
                                memory="tight")
        state = self.drive(second, 5, live)
        self.assertEqual(state["unloadsLastHour"], 6)
        self.assertEqual(state["blocked"], "hourly limit reached (6/6)")
        self.until_call_on(second, second_control, live, 10 * MINUTE)
        oldest = min(entry["attemptTs"] for entry in self.journal_rows()[:6])
        self.assertGreaterEqual(self.clock.wall - oldest, 3600)

    def until_call_on(self, unloader, control, make, limit):
        elapsed = 0.0
        while not control.calls:
            self.assertLess(elapsed, limit, "no unload within the limit")
            self.clock.advance(5)
            elapsed += 5
            unloader.tick(make())

    def test_a_closed_unloader_never_acts_again(self):
        unloader = self.make()
        unloader.close()
        state = self.drive(unloader, 25 * MINUTE, lambda: snapshot(self.clock, self.pair_and(row(OTHER))))
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["blocked"], "auto-unload is closed")

    def test_unsafe_lock_files_block(self):
        for name in ("fifo", "symlink", "hardlink"):
            with self.subTest(case=name):
                self.setUp()
                self.state_dir()
                lock = self.journal.parent / au.LOCK_NAME
                if name == "fifo":
                    os.mkfifo(lock, 0o600)
                elif name == "symlink":
                    (self.root / "elsewhere.lock").write_text("")
                    os.symlink(self.root / "elsewhere.lock", lock)
                else:
                    lock.write_text("")
                    os.chmod(lock, 0o600)
                    os.link(lock, self.root / "second-name.lock")
                unloader = self.within(self.make)
                state = self.within(lambda: self.drive(
                    unloader, 25 * MINUTE, lambda: snapshot(self.clock, self.pair_and(row(OTHER)))))
                self.assertEqual(self.control.calls, [])
                self.assertIn("auto-unload lock", state["blocked"])
                self.tearDown()


class UnknownStateTests(Base):
    def test_unknown_busy_or_unconfirmed_instances_are_never_unloaded(self):
        cases = {
            "loaded": row(OTHER, state="loaded"),
            "unknown": row(OTHER, state="unknown"),
            "queue-unknown": row(OTHER, queued=None),
            "queue-bool": row(OTHER, queued=False),
            "negative-row-age": row(OTHER, age=-1.0),
            "no-instance-id": row(OTHER, instanceId=None),
            "instance-mismatch": row(OTHER, instanceId="something-else"),
            "no-model-key": row(OTHER, modelKey=None),
            "unconfirmed": row(OTHER, confirm=False),
            "api-row": row(OTHER, source="lmstudio-api"),
            "windows": row(OTHER, host="windows"),
            "not-loaded": row(OTHER, loaded=None),
        }
        for name, candidate in cases.items():
            with self.subTest(case=name):
                self.setUp()
                unloader = self.make()
                self.drive(unloader, 90 * MINUTE,
                           lambda: snapshot(self.clock, self.pair_and(candidate), memory="critical"), step=10)
                self.assertEqual(self.control.calls, [])
                self.tearDown()

    def test_two_instance_lists_for_one_key_is_ambiguous(self):
        unloader = self.make()
        rows = self.pair_and(row(OTHER), {"id": "dup", "host": "mac", "source": "lmstudio-api", "loaded": True,
                                          "modelKey": OTHER, "loadedInstanceIds": [OTHER]})
        state = self.drive(unloader, 30 * MINUTE, lambda: snapshot(self.clock, rows, memory="tight"))
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["candidates"][0]["blocked"], "instance not confirmed by LM Studio inventory")

    def test_refusals_and_errors_are_journaled_and_never_raise(self):
        unloader = self.make()
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)))
        self.control.raise_next = FakeControlError(409, "Selected model is busy or its activity is unknown.")
        self.drive(unloader, 21 * MINUTE, make)
        self.control.raise_next = RuntimeError("boom")
        self.drive(unloader, 2 * MINUTE + 5, make)
        results = [(r["result"], r["message"]) for r in self.journal_rows()]
        self.assertEqual(results, [("refused", "409: Selected model is busy or its activity is unknown."),
                                   ("error", "unload call failed (RuntimeError)")])

    def test_garbage_never_raises(self):
        def broken_status():
            raise RuntimeError("status")
        unloader = self.make(status_fn=broken_status)
        for value in (None, "x", 5, [], {"models": "x"}, {"sampledAt": float("nan")},
                      snapshot(self.clock, [1, None, {"id": 5}, {"host": "mac", "source": "lms-ps", "loaded": True},
                                            row(OTHER, metadata={"type": 5, "loadedInstances": [None, {"id": 3}]})]),
                      snapshot(self.clock, self.pair_and(row(OTHER)), memory=None, pipeline=[])):
            state = unloader.tick(value)
            json.dumps(state, allow_nan=False)
            self.assertEqual(tuple(state), au.STATE_KEYS)
        self.drive(unloader, 25 * MINUTE, lambda: snapshot(self.clock, self.live(row(OTHER))))
        self.assertEqual([r["result"] for r in self.journal_rows()], ["requested", "unconfirmed"])

    def test_module_has_no_load_or_process_path(self):
        source = Path(au.__file__).read_text()
        for needle in ("subprocess", "Popen", "os.system", "os.exec", '"load"', "'load'", "urllib", "socket"):
            self.assertNotIn(needle, source)


class JournalTests(Base):
    def unload_once(self, **options):
        unloader = self.make(**options)
        self.drive(unloader, 21 * MINUTE, lambda: snapshot(self.clock, self.pair_and(row(OTHER))))
        return unloader

    def test_rows_are_closed_and_private(self):
        self.unload_once()
        self.assertEqual(stat.S_IMODE(self.journal.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.journal.parent.stat().st_mode), 0o700)
        [entry] = self.journal_rows()
        self.assertEqual(sorted(entry), sorted(au.JOURNAL_KEYS))
        self.assertEqual((entry["model"], entry["instanceId"], entry["reason"], entry["memoryLevel"],
                          entry["result"], entry["operationId"]), (OTHER, OTHER, "idle", "ok", "requested", "op1"))
        self.assertEqual(entry["idleSeconds"], 20 * 60)
        self.assertEqual(entry["ts"], entry["attemptTs"])

    def test_existing_journal_is_tightened_to_0600(self):
        self.state_dir()
        self.journal.write_text("")
        os.chmod(self.journal, 0o644)
        self.unload_once()
        self.assertEqual(stat.S_IMODE(self.journal.stat().st_mode), 0o600)

    def test_symlinked_journal_blocks_every_unload(self):
        self.state_dir()
        target = self.root / "elsewhere.jsonl"
        target.write_text("")
        os.symlink(target, self.journal)
        unloader = self.unload_once()
        state = unloader.state()
        self.assertEqual(self.control.calls, [])
        self.assertIn("journal unavailable", state["blocked"])
        self.assertIsNotNone(state["journalError"])
        self.assertEqual(target.read_text(), "")

    def test_hardlinked_journal_blocks_every_unload(self):
        self.write_journal([])
        os.link(self.journal, self.root / "second-name.jsonl")
        unloader = self.unload_once()
        self.assertEqual(self.control.calls, [])
        self.assertIn("journal unavailable", unloader.state()["blocked"])
        self.assertEqual(unloader.state()["journalError"], "journal is not a private regular file")

    def test_fifo_journal_never_blocks_the_constructor_or_the_attempt(self):
        self.state_dir()
        os.mkfifo(self.journal, 0o600)
        unloader = self.within(self.make)
        self.assertEqual(unloader.state()["journalError"], "journal is not a regular file")
        state = self.within(lambda: self.drive(
            unloader, 21 * MINUTE, lambda: snapshot(self.clock, self.pair_and(row(OTHER)))))
        self.assertEqual(self.control.calls, [])
        self.assertIn("journal unavailable", state["blocked"])

    def test_fifo_planted_after_startup_never_blocks_the_attempt(self):
        unloader = self.make()
        os.mkfifo(self.journal, 0o600)
        state = self.within(lambda: self.drive(
            unloader, 21 * MINUTE, lambda: snapshot(self.clock, self.pair_and(row(OTHER)))))
        self.assertEqual(self.control.calls, [])
        self.assertIn("journal unavailable", state["blocked"])
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.journal).st_mode))

    def test_compaction_refuses_a_fifo_swapped_in(self):
        unloader = self.make()
        os.mkfifo(self.journal, 0o600)
        self.within(unloader._compact)
        self.assertTrue(unloader.state()["journalError"].startswith("journal compaction failed"))
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.journal).st_mode))

    def test_a_shared_state_directory_is_checked_not_remoded(self):
        self.state_dir()
        os.chmod(self.journal.parent, 0o777)
        # With the lock beside the journal, the lock refuses first.
        unloader = self.unload_once()
        self.assertEqual(unloader.state()["blocked"], "state directory is not a private directory owned by this user")
        self.setUp()
        self.state_dir()
        os.chmod(self.journal.parent, 0o777)
        unloader = self.unload_once(lock_path=self.root / "lock" / au.LOCK_NAME)
        state = unloader.state()
        self.assertEqual(self.control.calls, [])
        self.assertIn("journal unavailable", state["blocked"])
        self.assertEqual(state["journalError"], "state directory is not a private directory owned by this user")
        self.assertEqual(stat.S_IMODE(self.journal.parent.stat().st_mode), 0o777)

    def test_an_unwritten_row_blocks_unloads_until_it_is_journaled(self):
        unloader = self.make()
        make = lambda: snapshot(self.clock, self.live(row(OTHER), row(SECOND)))
        unloader.tick(make())
        self.drive(unloader, 20 * MINUTE - 5, make)
        with mock.patch.object(au.os, "write", side_effect=OSError(errno.ENOSPC, "disk full")):
            self.drive(unloader, 5, make)
            self.assertEqual(self.control.calls, [OTHER])
            state = self.drive(unloader, 30, make)
        self.assertEqual(self.control.calls, [OTHER])
        self.assertEqual(state["blocked"], "journal write failed; retrying it before any further unload")
        self.assertEqual(state["journalError"], "journal write failed (OSError)")
        self.assertEqual(state["recent"][0]["instanceId"], OTHER)
        self.drive(unloader, 5, make)
        self.assertEqual(self.control.calls, [OTHER, SECOND])
        self.assertEqual([entry["instanceId"] for entry in self.journal_rows()], [OTHER, SECOND])
        unloader.close()
        self.assertEqual(self.make().state()["unloadsLastHour"], 2)

    def test_journal_is_bounded_and_keeps_whole_newest_rows(self):
        old = self.journal_row(EMBED, self.clock.wall - 7200)
        line = json.dumps(old) + "\n"
        self.write_journal([old] * (6000 // len(line) + 1))
        self.unload_once(journal_cap=4096)
        data = self.journal.read_text()
        self.assertLessEqual(len(data.encode()), 4096)
        rows = [json.loads(item) for item in data.splitlines()]
        self.assertEqual(rows[-1]["instanceId"], OTHER)
        self.assertTrue(all(sorted(r) == sorted(au.JOURNAL_KEYS) for r in rows))
        self.assertEqual(stat.S_IMODE(self.journal.stat().st_mode), 0o600)

    def test_seed_ignores_malformed_rows_and_recent_is_newest_first(self):
        rows = [self.journal_row(f"lab/m{index}", self.clock.wall - 7200 + index) for index in range(12)]
        rows.insert(3, '{"ts": 1, "extra": true}')
        rows.insert(5, "not json")
        rows.append(dict(self.journal_row("x", self.clock.wall), idleSeconds=-1))
        self.write_journal(rows)
        state = self.make().state()
        self.assertEqual(len(state["recent"]), 10)
        self.assertEqual(state["recent"][0]["model"], "lab/m11")
        self.assertEqual(state["recent"][-1]["model"], "lab/m2")
        self.assertEqual(state["recent"][0]["ageSeconds"], 7189.0)
        self.assertEqual(state["unloadsLastHour"], 0)


class ConfigTests(Base):
    def assert_invalid(self, value, mode=0o600):
        self.write_config(value, mode)
        unloader = self.make()
        state = self.drive(unloader, 25 * MINUTE, lambda: snapshot(self.clock, self.pair_and(row(OTHER))))
        self.assertEqual(self.control.calls, [])
        self.assertFalse(state["enabled"])
        self.assertIsNotNone(state["configError"])
        self.assertTrue(state["blocked"].startswith("config invalid"))
        self.assertEqual(state["protect"], [GEMMA, QWEN])

    def test_missing_file_is_the_defaults(self):
        state = self.make(default_enabled=False).state()
        self.assertEqual((state["enabled"], state["idleMinutes"], state["tightIdleMinutes"], state["maxPerHour"],
                          state["protect"], state["configError"]), (False, 20, 5, 6, [], None))

    def test_missing_config_tracks_idle_but_requires_explicit_opt_in(self):
        unloader = self.make(default_enabled=False)
        make = lambda: snapshot(self.clock, self.pair_and(row(OTHER)))
        state = self.drive(unloader, 25 * MINUTE, make)
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["blocked"], "auto-unload is off")
        self.assertEqual(state["candidates"][0]["eta"], 0)
        self.assertTrue(unloader.set_enabled(True)["enabled"])
        unloader.tick(make())
        self.assertEqual(self.control.calls, [OTHER])

    def test_closed_reader_rejects_everything_outside_the_contract(self):
        cases = [{"enabled": True, "extra": 1}, {"enabled": "yes"}, {"enabled": 1},
                 {"idleMinutes": 4}, {"idleMinutes": 241}, {"idleMinutes": 20.0}, {"idleMinutes": True},
                 {"tightIdleMinutes": 0}, {"tightIdleMinutes": 61}, {"maxPerHour": 0}, {"maxPerHour": 61},
                 {"protect": OTHER}, {"protect": [1]}, {"protect": ["bad id!"]},
                 {"protect": [f"lab/m{index}" for index in range(33)]}, [], "not json {",
                 '{"enabled": true, "enabled": false}', '{"idleMinutes": NaN}', b"\xff\xfe",
                 json.dumps({"protect": ["a" * 150] * 30})]  # valid ids, but over 4096 bytes
        for value in cases:
            with self.subTest(value=value if not isinstance(value, str) else value[:40]):
                self.setUp()
                self.assert_invalid(value)
                self.tearDown()

    def test_unsafe_files_are_invalid(self):
        self.assert_invalid({"enabled": True}, mode=0o664)
        self.setUp()
        target = self.root / "real.json"
        target.write_text("{}")
        self.config.parent.mkdir(parents=True)
        os.symlink(target, self.config)
        state = self.make().state()
        self.assertFalse(state["enabled"])
        self.assertIsNotNone(state["configError"])
        self.setUp()
        self.config.parent.mkdir(parents=True)
        os.mkfifo(self.config, 0o600)
        state = self.within(self.make).state()
        self.assertEqual(state["configError"], "config is not a regular file")

    def test_disabled_tracks_but_never_unloads_and_reloads_on_change(self):
        self.write_config({"enabled": False})
        unloader = self.make()
        state = self.drive(unloader, 25 * MINUTE, lambda: snapshot(self.clock, self.pair_and(row(OTHER))))
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["blocked"], "auto-unload is off")
        self.assertEqual(state["candidates"][0]["eta"], 0)
        self.write_config({"enabled": True, "idleMinutes": 30})
        self.clock.advance(1)
        state = unloader.tick(snapshot(self.clock, self.pair_and(row(OTHER))))
        self.assertEqual(self.control.calls, [])
        self.assertEqual(state["idleMinutes"], 30)
        self.write_config({"enabled": True})
        self.clock.advance(1)
        unloader.tick(snapshot(self.clock, self.pair_and(row(OTHER))))
        self.assertEqual(self.control.calls, [OTHER])

    def test_set_enabled_persists_privately_and_keeps_other_fields(self):
        unloader = self.make()
        state = unloader.set_enabled(False)
        self.assertFalse(state["enabled"])
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.config.read_text()),
                         {"enabled": False, "idleMinutes": 20, "maxPerHour": 6,
                          "protect": [], "tightIdleMinutes": 5})
        self.write_config({"idleMinutes": 45, "protect": [OTHER]})
        state = unloader.set_enabled(True)
        self.assertTrue(state["enabled"])
        saved = json.loads(self.config.read_text())
        self.assertEqual((saved["idleMinutes"], saved["protect"], saved["enabled"]), (45, [OTHER], True))
        with self.assertRaises(ValueError):
            unloader.set_enabled("yes")
        self.write_config('{"enabled": true, "surprise": 1}')
        with self.assertRaises(ValueError):
            unloader.set_enabled(False)
        self.assertEqual(self.config.read_text(), '{"enabled": true, "surprise": 1}')


class StateShapeTests(Base):
    def test_state_shape_is_closed_and_json_safe(self):
        unloader = self.make(status_fn=self.control.read)
        state = self.drive(unloader, 21 * MINUTE,
                           lambda: snapshot(self.clock, self.pair_and(row(OTHER), row(EMBED, state="loaded"))))
        text = json.dumps(state, allow_nan=False)
        self.assertEqual(tuple(json.loads(text)), au.STATE_KEYS)
        self.assertEqual(state["thresholdSeconds"], 1200)
        self.assertEqual(state["memoryLevel"], "ok")
        for candidate in state["candidates"]:
            self.assertEqual(tuple(candidate), au.CANDIDATE_KEYS)
        self.assertEqual({c["model"]: c["blocked"] for c in state["candidates"]},
                         {OTHER: "auto-unloaded less than 2 min ago", EMBED: "activity or queue state unknown"})
        [recent] = state["recent"]
        self.assertEqual(sorted(recent), sorted(au.JOURNAL_KEYS + ("ageSeconds",)))
        self.assertEqual(state["unloadsLastHour"], 1)
        self.assertIsInstance(state["tickedAt"], float)

    def test_model_id_grammar_matches_model_control(self):
        try:
            import model_control
        except Exception as error:  # the Monitor's module is edited by another workflow
            self.skipTest(f"model_control not importable: {type(error).__name__}")
        self.assertEqual(au.MODEL_ID.pattern, model_control.MODEL_ID.pattern)
        self.assertEqual(au.FRESH_SECONDS, model_control.MAX_SNAPSHOT_AGE)


if __name__ == "__main__":
    unittest.main()
