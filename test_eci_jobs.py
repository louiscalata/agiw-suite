"""Check-job engine tests: fake runner, fake clocks, no socket, no model call."""

import ast
import copy
import hashlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import eci_fast_check as fast
import eci_jobs as jobs
import eci_tuning as tuning
from remote_machines import PROTECTED_PORTS

ROOT = Path(__file__).resolve().parent
LANE = "registry:bench:lane-a"
OTHER = "registry:bench:lane-b"
MODEL = dict(name="synthetic candidate", file_sha256="a" * 64, quantization="Q4_K_M", file_size_bytes=100)
HARDWARE = dict(
    machine_label="placeholder-machine",
    cpu="placeholder-cpu",
    gpu="placeholder-gpu",
    memory={"bytes": 1000},
    runtime_build_id="placeholder-build",
    memory_bytes=1000,
)
SETTINGS = dict(
    scorer_id="parsed_exact_match-v1",
    prompt_template_id="synthetic-v1",
    temperature=0,
    max_tokens=128,
    backend="fake",
    backend_version="1",
    ctx=4096,
    seed=42,
    stop=[],
    system_message="",
    n_epochs=1,
)
STAMP = "2026-11-12T12:00:00+00:00"


def datasets(n=6):
    return {"arc": {"items": [dict(id=str(i), answer="A", question="synthetic") for i in range(n)], "revision": None}}


def observation():
    return dict(
        outcome="correct",
        parsed=True,
        finish_reason="stop",
        cut_off=False,
        answer_value="A",
        prompt_tokens=8,
        completion_tokens=4,
        seconds=2,
        error=None,
        prediction="A",
    )


def make_record(count=4, wall_budget_s="60", step=0, model=None, max_tokens=128, settings=True):
    """A real validated record from the offline core; ``step`` advances its fake clock."""
    data = datasets()
    plan = fast.make_plan(data, count=count, wall_budget_s=wall_budget_s)
    ticks = {"now": 0}

    def clock():
        ticks["now"] += step
        return ticks["now"]

    model = model or copy.deepcopy(MODEL)
    settings = dict(copy.deepcopy(SETTINGS), max_tokens=max_tokens) if settings else None
    return fast.run_check(
        plan,
        data,
        lambda item: observation(),
        model=model,
        hardware=copy.deepcopy(HARDWARE),
        runtime_settings=settings,
        pre=fast.pre_check(model["file_size_bytes"], model["quantization"], 800),
        clock=clock,
        created_at=STAMP,
    )


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value


class Now:
    """Wall clock that moves one second per reading, so stamps are deterministic."""

    def __init__(self):
        self.value = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.value += timedelta(seconds=1)
        return self.value


DEFAULT = object()


class Runner:
    """Fake runner: four items, asks ``should_stop`` between items, never opens a socket."""

    def __init__(self):
        self.calls = []
        self.between = None
        self.result = DEFAULT
        self.error = None
        self.honour_stop = True
        self.stops = []

    def __call__(self, spec, should_stop, progress):
        self.calls.append(copy.deepcopy(spec))
        if self.error is not None:
            raise self.error
        for index in range(4):
            stop = should_stop()
            self.stops.append(stop)
            if stop and self.honour_stop:
                raise jobs.Stopped()
            if self.between is not None:
                self.between(index)
            progress(index + 1, 4)
        if self.result is not DEFAULT:
            return self.result
        return record_for(spec)


def record_for(spec, **changes):
    """The record the adapter would return for ``spec``: its model name and its max_tokens."""
    options = dict(
        count=spec["count"],
        wall_budget_s=spec["wall_budget_s"],
        max_tokens=spec["max_tokens"],
        model=dict(MODEL, name=spec["facts"].get("model_label", spec["model_id"])),
    )
    options.update(changes)
    return make_record(**options)


NORMAL = {"model_id": "synthetic/candidate", "max_tokens": 16384, "count": 4, "wall_budget_s": "60", "facts": {}}


class Sink:
    def __init__(self):
        self.records = []
        self.error = None

    def __call__(self, record):
        if self.error is not None:
            raise self.error
        self.records.append(record)


def targets(port=18300):
    return [
        {
            "id": LANE,
            "machine": "registry:bench",
            "lane": "lane-a",
            "label": "Lane A",
            "base": "http://127.0.0.1:%d" % port,
            "timeoutSeconds": 1.5,
        },
        {
            "id": OTHER,
            "machine": "registry:bench",
            "lane": "lane-b",
            "label": "Lane B",
            "base": "http://127.0.0.1:18301",
            "timeoutSeconds": 1.5,
        },
    ]


def request(**changes):
    value = {
        "submission_id": "submission-0001",
        "target": LANE,
        "model_id": "synthetic/candidate",
        "sample_set": "screening",
        "count": 4,
    }
    value.update(changes)
    return value


class EngineCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.gate = {"open": True}
        self.lanes = targets()
        self.runner, self.sink, self.clock, self.now = Runner(), Sink(), Clock(), Now()
        self.policy = jobs.Policy(
            gate=lambda: self.gate["open"], allowed_targets={LANE}, max_wall_budget_s="600", default_wall_budget_s="60"
        )
        self.engines = []
        self.engine = self.open()

    def open(self, state=None, **changes):
        options = dict(
            policy=self.policy,
            targets=lambda: self.lanes,
            sample_sets={"screening": [str(self.root / "samples.json")]},
            runner=self.runner,
            sink=self.sink,
            now=self.now,
            clock=self.clock,
            plan_size=lambda files, count: count,
        )
        options.update(changes)
        engine = jobs.Engine(state or self.state, **options)
        self.engines.append(engine)
        self.addCleanup(engine.close)
        return engine

    def journal(self, state=None):
        path = (state or self.state) / "jobs.jsonl"
        return path.read_bytes() if path.exists() else b""

    def reviewed(self, value=None, engine=None):
        preview = (engine or self.engine).preview(value or request())
        return {"reviewed": True, "review_sha256": preview["review_sha256"]}

    def submit(self, value=None, engine=None):
        value = value or request()
        return (engine or self.engine).submit(value, self.reviewed(value, engine))

    def refused(self, code, call, *args, **kwargs):
        with self.assertRaises(jobs.Refused) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class RequestTests(EngineCase):
    def test_defaults_become_explicit_in_the_reviewed_request(self):
        normalized = jobs.normalize_request(request(), self.policy)
        self.assertEqual(
            normalized,
            {
                "submission_id": "submission-0001",
                "target": LANE,
                "model_id": "synthetic/candidate",
                "sample_set": "screening",
                "count": 4,
                "max_tokens": 16384,
                "wall_budget_s": "60",
                "request_timeout_s": 180,
                "disable_thinking": False,
                "facts": {},
            },
        )
        self.assertEqual(jobs.normalize_request(normalized, self.policy), normalized)

    def test_malformed_requests_refuse(self):
        cases = [
            None,
            [],
            request(extra=1),
            request(submission_id="short"),
            request(submission_id=7),
            request(target="lane-a"),
            request(target="registry:bench"),
            request(sample_set="../x"),
            request(model_id="/private/model.gguf"),
            request(model_id="http://x/y"),
            request(count=0),
            request(count=True),
            request(count=jobs.MAX_COUNT + 1),
            request(max_tokens=8191),
            request(max_tokens=jobs.MAX_MAX_TOKENS + 1),
            request(request_timeout_s=0),
            request(request_timeout_s=1.5),
            request(disable_thinking="no"),
            request(wall_budget_s=60),
            request(wall_budget_s="0"),
            request(wall_budget_s="1e3"),
            request(facts=[]),
            request(facts={"unknown": 1}),
            request(facts={"model_sha256": "A" * 64}),
            request(facts={"file_size_bytes": 0}),
            request(facts={"cpu": "/dev/cpu"}),
            request(facts={"quantization": "q4_k_m"}),
        ]
        for value in cases:
            with self.subTest(value=value):
                self.refused("invalid_request", jobs.normalize_request, value, self.policy)

    def test_accepted_facts_are_kept(self):
        facts = {
            "model_sha256": "b" * 64,
            "quantization": "Q4_K_M",
            "file_size_bytes": 100,
            "machine_label": "placeholder machine",
            "cpu": None,
        }
        normalized = jobs.normalize_request(request(facts=facts), self.policy)
        self.assertEqual(normalized["facts"], {key: value for key, value in facts.items() if value})

    def test_wall_budget_needs_a_decided_default_and_maximum(self):
        undecided = jobs.Policy(gate=lambda: True, allowed_targets={LANE})
        self.refused("budget_required", jobs.normalize_request, request(), undecided)
        self.refused("budget_refused", jobs.normalize_request, request(wall_budget_s="60"), undecided)
        self.refused("budget_refused", jobs.normalize_request, request(wall_budget_s="600.1"), self.policy)
        self.assertEqual(jobs.normalize_request(request(wall_budget_s="600"), self.policy)["wall_budget_s"], "600")

    def test_policy_rejects_undecidable_settings(self):
        for options in (
            dict(gate=True),
            dict(allowed_targets={"lane-a"}),
            dict(max_wall_budget_s=60),
            dict(default_wall_budget_s="60"),
            dict(max_wall_budget_s="60", default_wall_budget_s="61"),
            dict(max_wall_budget_s="1." + "0" * 31),
            dict(max_wall_budget_s="9" * 33),
            dict(max_wall_budget_s="60", default_wall_budget_s="1." + "0" * 5000),
        ):
            with self.subTest(options=str(options)[:80]), self.assertRaises(ValueError) as caught:
                jobs.Policy(**options)
            self.assertNotIn("digits", str(caught.exception))

    def test_a_wall_budget_is_at_most_32_characters(self):
        self.assertEqual(jobs.MAX_BUDGET_CHARS, 32)
        longest = "1." + "0" * 30
        self.assertEqual(jobs.normalize_request(request(wall_budget_s=longest), self.policy)["wall_budget_s"], longest)
        jobs.Policy(max_wall_budget_s="9" * 32, default_wall_budget_s=longest)
        # 5,000 digits is past the integer conversion limit of newer interpreters;
        # about a million is what the older one accepted and journalled.
        for digits in (31, 5000, 1040000):
            for value in ("1." + "0" * digits, "9" * (digits + 2)):
                with self.subTest(digits=digits, kind=value[:2]):
                    error = self.refused(
                        "invalid_request", jobs.normalize_request, request(wall_budget_s=value), self.policy
                    )
                    self.assertLess(len(str(error)), 200)
                    self.refused("invalid_request", self.engine.preview, request(wall_budget_s=value))
                    self.refused(
                        "invalid_request",
                        self.engine.submit,
                        request(wall_budget_s=value),
                        {"reviewed": True, "review_sha256": "0" * 64},
                    )
        self.assertEqual(self.journal(), b"")

    def test_a_policy_budget_changed_to_an_overlong_value_refuses(self):
        self.submit()
        for value in ("1." + "0" * 5000, "9" * 33, 600, None):
            with self.subTest(value=str(value)[:8]):
                self.policy.max_wall_budget_s = value
                self.refused("budget_refused", jobs.normalize_request, request(), self.policy)
                self.refused("budget_refused", self.engine.preview, request())
        self.policy.max_wall_budget_s = "600"
        self.policy.default_wall_budget_s = "1." + "0" * 5000
        self.refused("invalid_request", jobs.normalize_request, request(), self.policy)
        self.policy.default_wall_budget_s = "60"
        self.policy.max_wall_budget_s = "9" * 5000
        done = self.engine.run_next()
        self.assertEqual((done["state"], done["reason"]), ("failed", "Refused at start: budget_refused"))
        self.assertEqual(self.runner.calls, [])
        self.assertLess(len(self.journal()), 4096)

    def test_a_model_label_must_be_a_public_model_identifier(self):
        kept = jobs.normalize_request(request(facts={"model_label": "vendor/label-1"}), self.policy)
        self.assertEqual(kept["facts"], {"model_label": "vendor/label-1"})
        for label in ("synthetic candidate", "/private/model.gguf", "http://x/y", "../label", "", 7):
            with self.subTest(label=label):
                self.refused(
                    "invalid_request", jobs.normalize_request, request(facts={"model_label": label}), self.policy
                )


class GateTests(EngineCase):
    def test_the_engine_ships_closed(self):
        engine = self.open(state=self.root / "shipped", policy=jobs.CLOSED)
        self.assertEqual(engine.status()["gate"], "closed")
        self.assertIs(jobs.CLOSED.gate_open(), False)
        self.refused("budget_required", engine.preview, request())
        self.refused("budget_refused", engine.submit, request(wall_budget_s="60"), {"reviewed": True})
        default = jobs.Engine(self.root / "default")
        self.addCleanup(default.close)
        self.assertEqual(default.status()["gate"], "closed")
        self.assertEqual(self.runner.calls, [])

    def test_a_closed_gate_refuses_submit_resume_and_start(self):
        review = self.reviewed()
        self.gate["open"] = False
        self.refused("gate_closed", self.engine.submit, request(), review)
        self.assertEqual(self.journal(), b"")
        self.gate["open"] = True
        self.submit()
        self.gate["open"] = False
        self.refused("gate_closed", self.engine.run_next)
        self.assertEqual(self.engine.status()["jobs"][0]["state"], "queued")
        self.assertEqual(self.runner.calls, [])

    def test_only_exactly_true_opens_the_gate(self):
        def broken():
            raise RuntimeError("gate source failed")

        for gate in (lambda: 1, lambda: "yes", lambda: None, lambda: [True], broken):
            policy = jobs.Policy(gate=gate, allowed_targets={LANE}, max_wall_budget_s="600", default_wall_budget_s="60")
            self.assertIs(policy.gate_open(), False)

    def test_preview_names_every_reason_and_writes_nothing(self):
        self.gate["open"] = False
        self.lanes = []
        engine = self.open(state=self.root / "preview", runner=None)
        preview = engine.preview(request(sample_set="absent"))
        self.assertEqual(
            [item["code"] for item in preview["refusals"]],
            ["gate_closed", "unknown_target", "unknown_sample_set", "not_configured"],
        )
        self.assertIs(preview["effects"]["starts_model"], False)
        self.assertIsNone(preview["effects"]["planned_requests"])
        self.assertEqual((self.root / "preview" / "jobs.jsonl").read_bytes(), b"")
        self.gate["open"] = True
        self.lanes = targets()
        ready = self.engine.preview(request())
        self.assertEqual(ready["refusals"], [])
        self.assertEqual(ready["effects"]["planned_requests"], 4)
        self.assertEqual(ready["effects"]["max_requests"], 8)

    def test_an_unreadable_sample_set_is_reported_in_preview(self):
        engine = self.open(state=self.root / "plan", plan_size=jobs.default_plan_size)
        preview = engine.preview(request())
        self.assertEqual([item["code"] for item in preview["refusals"]], ["samples_unreadable"])
        (self.root / "samples.json").write_text(json.dumps(datasets()))
        self.assertEqual(engine.preview(request())["effects"]["planned_requests"], 4)


class ReviewTests(EngineCase):
    def test_unreviewed_submissions_refuse(self):
        sha = self.reviewed()["review_sha256"]
        for review in (
            None,
            True,
            {},
            {"reviewed": True},
            {"reviewed": False, "review_sha256": sha},
            {"reviewed": 1, "review_sha256": sha},
            {"reviewed": "true", "review_sha256": sha},
            {"reviewed": True, "review_sha256": sha, "note": "x"},
        ):
            with self.subTest(review=review):
                self.refused("not_reviewed", self.engine.submit, request(), review)
        self.assertEqual(self.journal(), b"")

    def test_a_review_binds_one_exact_request(self):
        review = self.reviewed()
        for changed in (
            request(count=5),
            request(model_id="synthetic/other"),
            request(target=OTHER),
            request(wall_budget_s="61"),
            request(submission_id="submission-0002"),
            request(disable_thinking=True),
            request(facts={"quantization": "Q8_0"}),
        ):
            with self.subTest(changed=changed):
                self.refused("review_mismatch", self.engine.submit, changed, review)
        self.refused("review_mismatch", self.engine.submit, request(), {"reviewed": True, "review_sha256": "0" * 64})
        self.assertEqual(self.journal(), b"")


class TargetTests(EngineCase):
    def test_targets_outside_the_registry_or_policy_refuse(self):
        self.refused(
            "target_not_allowed", self.engine.submit, request(target=OTHER), self.reviewed(request(target=OTHER))
        )
        value = request(target="registry:bench:absent")
        self.refused("unknown_target", self.engine.submit, value, self.reviewed(value))
        review = self.reviewed()
        self.lanes = targets() + targets()
        self.refused("unknown_target", self.engine.submit, request(), review)
        self.lanes = None
        self.refused("unknown_target", self.engine.submit, request(), review)
        engine = self.open(state=self.root / "raising", targets=mock.Mock(side_effect=OSError("registry")))
        self.refused("unknown_target", engine.submit, request(), review)
        engine = self.open(state=self.root / "absent", targets=None)
        self.refused("unknown_target", engine.submit, request(), review)

    def test_every_protected_port_refuses(self):
        review = self.reviewed()
        self.assertEqual(len(PROTECTED_PORTS), 8)
        for port in sorted(PROTECTED_PORTS):
            with self.subTest(port=port):
                self.lanes = targets(port)
                self.refused("protected_port", self.engine.submit, request(), review)
        self.assertEqual(self.journal(), b"")

    def test_only_a_loopback_lane_address_is_a_target(self):
        review = self.reviewed()
        for base in (
            "http://localhost:18300",
            "https://127.0.0.1:18300",
            "http://127.0.0.1",
            "http://127.0.0.1:18300/v2",
            "http://127.0.0.1:99999",
            "http://192.0.2.1:18300",
            7,
        ):
            with self.subTest(base=base):
                self.lanes = targets()
                self.lanes[0]["base"] = base
                expected = "unknown_target" if base == 7 else "invalid_target"
                self.refused(expected, self.engine.submit, request(), review)
        self.assertEqual(self.journal(), b"")

    def test_unconfigured_parts_refuse(self):
        value = request(sample_set="absent")
        self.refused("unknown_sample_set", self.engine.submit, value, self.reviewed(value))
        for missing in ("runner", "sink"):
            engine = self.open(state=self.root / missing, **{missing: None})
            self.refused("not_configured", engine.submit, request(), self.reviewed(engine=engine))


class SubmitTests(EngineCase):
    def test_submit_queues_and_repeats_change_nothing(self):
        first = self.submit()
        self.assertEqual((first["state"], first["attempt"]), ("queued", 1))
        self.assertEqual(first["job_id"], self.engine.preview(request())["job_id"])
        before = self.journal()
        self.assertEqual(self.submit(), first)
        self.gate["open"] = False
        self.assertEqual(self.engine.submit(request(), self.reviewed()), first)
        self.assertEqual(self.journal(), before)
        self.assertEqual(len(before.splitlines()), 1)
        self.assertEqual(self.runner.calls, [])

    def test_the_queue_is_bounded(self):
        for index in range(jobs.MAX_QUEUED):
            self.submit(request(submission_id="submission-%04d" % index))
        value = request(submission_id="submission-9999")
        self.refused("queue_full", self.engine.submit, value, self.reviewed(value))

    def test_reading_status_starts_and_writes_nothing(self):
        job = self.submit()
        before = self.journal()
        for _ in range(3):
            status = self.engine.status()
            self.engine.job(job["job_id"])
            self.engine.preview(request())
            jobs.read_status(self.state)
        self.assertEqual((status["status"], status["gate"], status["active"]), ("ok", "open", None))
        self.assertEqual([item["state"] for item in status["jobs"]], ["queued"])
        self.assertEqual(self.journal(), before)
        self.assertEqual(self.runner.calls, [])
        self.assertFalse((self.state / "runs").exists())
        self.refused("unknown_job", self.engine.job, "0" * 32)
        self.refused("unknown_job", self.engine.job, None)


class RunTests(EngineCase):
    def test_a_complete_valid_record_is_ingested_and_the_job_finishes(self):
        job = self.submit(request(facts={"model_sha256": "a" * 64}))
        seen = []
        self.runner.between = lambda index: seen.append(self.engine.job(job["job_id"]))
        done = self.engine.run_next()
        self.assertEqual(done["state"], "finished")
        self.assertEqual([item["state"] for item in seen], ["running"] * 4)
        self.assertEqual(seen[1]["progress"], {"completed": 1, "planned": 4})
        self.assertIsNone(done["progress"])
        self.assertEqual(len(self.sink.records), 1)
        self.assertEqual(done["result"]["record_id"], self.sink.records[0]["record_id"])
        self.assertEqual(
            done["result"]["benchmarks"],
            [{"benchmark": "arc", "planned": 4, "attempted": 4, "stop_reason": "complete"}],
        )
        spec = self.runner.calls[0]
        self.assertEqual(spec["base_url"], "http://127.0.0.1:18300")
        self.assertEqual(spec["output_dir"], str(self.state / "runs" / job["job_id"]))
        self.assertEqual(spec["sample_files"], [str(self.root / "samples.json")])
        self.assertEqual((spec["count"], spec["wall_budget_s"], spec["attempt"]), (4, "60", 1))
        self.assertIsNone(self.engine.run_next())
        self.assertEqual(len(self.runner.calls), 1)
        self.assertFalse(done["resumable"])

    def test_registry_or_policy_changes_before_start_fail_the_job(self):
        self.submit()
        self.lanes = []
        done = self.engine.run_next()
        self.assertEqual((done["state"], done["reason"]), ("failed", "Refused at start: unknown_target"))
        self.assertEqual(self.runner.calls, [])
        self.lanes = targets()
        self.submit(request(submission_id="submission-0002", wall_budget_s="500"))
        self.policy.max_wall_budget_s = "100"
        done = self.engine.run_next()
        self.assertEqual(done["reason"], "Refused at start: budget_refused")
        self.assertEqual(self.runner.calls, [])

    def test_runner_errors_fail_the_job_without_copying_untrusted_text(self):
        self.submit()
        self.runner.error = RuntimeError("secret server body\n<html>")
        done = self.engine.run_next()
        self.assertEqual((done["state"], done["reason"]), ("failed", "Runner failed: RuntimeError"))
        self.submit(request(submission_id="submission-0002"))
        self.runner.error = fast.ValidationError("protected server port")
        self.assertEqual(self.engine.run_next()["reason"], "Runner failed: protected server port")
        self.assertEqual(self.sink.records, [])
        self.assertNotIn(b"secret", self.journal())

    def test_an_invalid_record_is_never_ingested(self):
        tampered = make_record()
        tampered["benchmarks"][0]["correct"] = 0
        for index, result in enumerate((tampered, {"kind": "agiw_fast_check"}, None, "record", [])):
            with self.subTest(index=index):
                self.submit(request(submission_id="submission-%04d" % index))
                self.runner.result = result
                done = self.engine.run_next()
                self.assertEqual((done["state"], done["reason"]), ("failed", "The runner returned an invalid record."))
        self.assertEqual(self.sink.records, [])

    def test_an_incomplete_record_is_kept_out_of_the_dataset(self):
        self.submit(request(wall_budget_s="10"))
        self.runner.result = record_for(dict(NORMAL, wall_budget_s="10"), step=3)
        done = self.engine.run_next()
        self.assertEqual(
            (done["state"], done["reason"]), ("failed", "The check is incomplete; the checkpoint is kept.")
        )
        bench = done["result"]["benchmarks"][0]
        self.assertEqual(bench["stop_reason"], "budget")
        self.assertLess(bench["attempted"], bench["planned"])
        self.assertEqual(self.sink.records, [])
        self.assertTrue(done["resumable"])

    def test_a_record_for_another_request_is_refused(self):
        hashed = {"facts": {"model_sha256": "a" * 64}}
        labelled = {"facts": {"model_label": "vendor/label"}}
        named = dict(MODEL, name="synthetic/candidate")
        cases = {
            "count": ({}, record_for(NORMAL, count=3)),
            "wall budget": ({}, record_for(NORMAL, wall_budget_s="61")),
            "declared hash": (hashed, record_for(NORMAL, model=dict(named, file_sha256="c" * 64))),
            # No model hash is declared in the cases below: the binding does not depend on one.
            "model name": ({}, record_for(NORMAL, model=dict(MODEL, name="synthetic candidate"))),
            "another model id": ({}, record_for(NORMAL, model=dict(MODEL, name="vendor/one"))),
            "model name with a hash": (hashed, record_for(NORMAL, model=dict(MODEL, name="vendor/one"))),
            "label ignored": (labelled, record_for(NORMAL)),
            "max_tokens lowered": ({}, record_for(NORMAL, max_tokens=8192)),
            "max_tokens raised": ({"max_tokens": 8192}, record_for(NORMAL)),
            "max_tokens unset": ({}, record_for(NORMAL, max_tokens=None)),
            "settings null": ({}, record_for(NORMAL, settings=False)),
        }
        for index, (name, (changes, result)) in enumerate(cases.items()):
            with self.subTest(name=name):
                fast.validate_record(copy.deepcopy(result))
                self.submit(request(submission_id="submission-%04d" % index, **changes))
                self.runner.result = result
                done = self.engine.run_next()
                self.assertEqual(
                    (done["state"], done["reason"]), ("failed", "The record does not match the reviewed request.")
                )
                self.assertEqual(done["result"]["record_id"], result["record_id"])
        self.assertEqual(self.sink.records, [])
        self.assertEqual(len(self.runner.calls), len(cases))

    def test_a_record_is_bound_to_the_reviewed_label_and_max_tokens(self):
        self.submit(request(max_tokens=8192, facts={"model_label": "vendor/label"}))
        done = self.engine.run_next()
        self.assertEqual(done["state"], "finished")
        self.assertEqual(self.sink.records[0]["model"]["name"], "vendor/label")
        self.assertEqual(self.sink.records[0]["runtime_settings"]["max_tokens"], 8192)

    def test_a_failed_ingest_fails_the_job(self):
        self.submit()
        self.sink.error = OSError("disk")
        done = self.engine.run_next()
        self.assertEqual(
            (done["state"], done["reason"]), ("failed", "The record could not be ingested; the dataset is unchanged.")
        )
        self.assertTrue(done["resumable"])

    def test_one_job_at_a_time(self):
        first = self.submit()
        second = self.submit(request(submission_id="submission-0002"))
        codes = []

        def nested(index):
            if index == 0:
                codes.append(self.refused("busy", self.engine.run_next).code)
                # Another thread can read and submit while the runner works.
                reader = threading.Thread(target=lambda: codes.append(self.engine.status()["active"]))
                reader.start()
                reader.join(10)
                self.assertFalse(reader.is_alive())

        self.runner.between = nested
        self.assertEqual(self.engine.run_next()["job_id"], first["job_id"])
        self.assertEqual(codes, ["busy", first["job_id"]])
        self.assertEqual(self.engine.job(second["job_id"])["state"], "queued")
        self.assertEqual(len(self.runner.calls), 1)
        self.runner.between = None
        self.assertEqual(self.engine.run_next()["job_id"], second["job_id"])
        self.assertEqual(len(self.sink.records), 2)

    def test_a_second_engine_on_the_same_directory_owns_nothing(self):
        review = self.reviewed()
        self.submit()
        second = self.open()
        status = second.status()
        self.assertEqual((status["status"], status["owner"], status["jobs"]), ("unknown", False, []))
        self.refused("not_owner", second.submit, request(submission_id="submission-0002"), review)
        self.refused("not_owner", second.run_next)
        self.refused("not_owner", second.cancel, "0" * 32)
        self.assertEqual(jobs.read_status(self.state)["jobs"][0]["state"], "queued")
        self.assertEqual(self.runner.calls, [])


class CancelTests(EngineCase):
    def test_a_queued_job_is_cancelled_without_running(self):
        job = self.submit()
        done = self.engine.cancel(job["job_id"])
        self.assertEqual((done["state"], done["reason"]), ("cancelled", "Cancelled before the job started."))
        before = self.journal()
        self.assertEqual(self.engine.cancel(job["job_id"])["state"], "cancelled")
        self.assertEqual(self.journal(), before)
        self.assertIsNone(self.engine.run_next())
        self.assertEqual(self.runner.calls, [])

    def test_a_running_job_stops_between_items(self):
        job = self.submit()
        views = []

        def between(index):
            if index == 1:
                views.append(self.engine.cancel(job["job_id"]))
                views.append(self.engine.cancel(job["job_id"]))

        self.runner.between = between
        done = self.engine.run_next()
        self.assertEqual(self.runner.stops, [None, None, "cancel"])
        self.assertEqual(
            [(view["state"], view["cancel_requested"]) for view in views], [("running", True), ("running", True)]
        )
        self.assertEqual((done["state"], done["reason"]), ("cancelled", "Cancelled while running."))
        self.assertEqual(self.sink.records, [])
        states = [json.loads(line)["state"] for line in self.journal().splitlines()]
        self.assertEqual(states, ["queued", "running", "running", "cancelled"])

    def test_a_cancelled_job_is_not_ingested_even_with_a_complete_record(self):
        job = self.submit()
        self.runner.honour_stop = False
        self.runner.between = lambda index: self.engine.cancel(job["job_id"])
        self.assertEqual(self.engine.run_next()["state"], "cancelled")
        self.assertEqual(self.sink.records, [])

    def test_cancel_works_with_the_gate_closed_and_refuses_ended_jobs(self):
        job = self.submit()
        self.gate["open"] = False
        self.assertEqual(self.engine.cancel(job["job_id"])["state"], "cancelled")
        self.gate["open"] = True
        other = self.submit(request(submission_id="submission-0002"))
        self.engine.run_next()
        self.refused("not_cancellable", self.engine.cancel, other["job_id"])
        self.refused("unknown_job", self.engine.cancel, "f" * 32)

    def test_the_wall_budget_stops_the_runner_between_items(self):
        self.submit(request(wall_budget_s="10"))

        def between(index):
            self.clock.value += 4

        self.runner.between = between
        done = self.engine.run_next()
        self.assertEqual(self.runner.stops, [None, None, None, "budget"])
        self.assertEqual((done["state"], done["reason"]), ("failed", "The wall budget ran out."))
        self.assertEqual(self.sink.records, [])

    def test_a_runner_that_stops_unasked_fails(self):
        self.submit()
        self.runner.error = jobs.Stopped()
        self.assertEqual(self.engine.run_next()["reason"], "The runner stopped without a request.")

    def test_an_interrupt_marks_the_job_and_propagates(self):
        job = self.submit()
        self.runner.error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.engine.run_next()
        view = self.engine.job(job["job_id"])
        self.assertEqual((view["state"], view["reason"]), ("interrupted", "The runner was interrupted."))
        self.assertIsNone(self.engine.run_next())


class RestartTests(EngineCase):
    def crashed(self, name):
        """Copy the journal as it stands mid-run: what a killed engine leaves behind."""
        job = self.submit()
        self.submit(request(submission_id="submission-0002"))
        target = self.root / name
        target.mkdir()

        def between(index):
            if index == 2:
                (target / "jobs.jsonl").write_bytes(self.journal())

        self.runner.between = between
        self.engine.run_next()
        self.runner.between = None
        self.runner.calls.clear()
        self.sink.records.clear()
        return target, job

    def test_a_restart_interrupts_and_never_resumes_by_itself(self):
        target, job = self.crashed("restarted")
        before = (target / "jobs.jsonl").read_bytes()
        self.assertEqual(jobs.read_status(target)["active"], job["job_id"])
        engine = self.open(state=target)
        status = engine.status()
        self.assertEqual(
            [(item["state"], item["reason"]) for item in status["jobs"]],
            [
                ("interrupted", "The engine stopped while the job was running."),
                ("interrupted", "The engine stopped before the job started."),
            ],
        )
        self.assertIsNone(status["active"])
        self.assertIsNone(engine.run_next())
        self.assertEqual(self.runner.calls, [])
        after = (target / "jobs.jsonl").read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(len(after.splitlines()), len(before.splitlines()) + 2)
        engine.close()
        again = self.open(state=target)
        self.assertEqual((target / "jobs.jsonl").read_bytes(), after)
        self.assertEqual(again.status()["jobs"], status["jobs"])

    def test_a_reviewed_resume_appends_a_new_attempt_in_the_same_output_directory(self):
        target, job = self.crashed("resumed")
        engine = self.open(state=target)
        job_id = job["job_id"]
        first_review = self.reviewed(engine=engine)
        for review in (None, {"reviewed": True}, {"reviewed": False, "review_sha256": "0" * 64}):
            self.refused("not_reviewed", engine.resume, job_id, review)
        self.refused("review_mismatch", engine.resume, job_id, first_review)
        preview = engine.preview_resume(job_id)
        self.assertEqual((preview["attempt"], preview["refusals"]), (2, []))
        review = {"reviewed": True, "review_sha256": preview["review_sha256"]}
        self.gate["open"] = False
        self.refused("gate_closed", engine.resume, job_id, review)
        self.gate["open"] = True
        queued = engine.resume(job_id, review)
        self.assertEqual((queued["state"], queued["attempt"], queued["reason"]), ("queued", 2, None))
        before = (target / "jobs.jsonl").read_bytes()
        self.assertEqual(engine.resume(job_id, review), queued)
        self.assertEqual((target / "jobs.jsonl").read_bytes(), before)
        done = engine.run_next()
        self.assertEqual((done["state"], done["attempt"]), ("finished", 2))
        self.assertEqual(self.runner.calls[0]["output_dir"], str(target / "runs" / job_id))
        self.assertEqual(self.runner.calls[0]["attempt"], 2)
        self.assertEqual(engine.resume(job_id, review), done)
        self.refused("not_resumable", engine.preview_resume, job_id)
        self.refused("review_mismatch", engine.resume, job_id, {"reviewed": True, "review_sha256": "0" * 64})
        states = [
            (entry["attempt"], entry["state"])
            for entry in map(json.loads, (target / "jobs.jsonl").read_bytes().splitlines())
            if entry.get("job_id") == job_id
        ]
        self.assertEqual(
            states, [(1, "queued"), (1, "running"), (1, "interrupted"), (2, "queued"), (2, "running"), (2, "finished")]
        )

    def test_each_new_stop_needs_a_new_review(self):
        job = self.submit()
        self.runner.error = RuntimeError("first")
        self.engine.run_next()
        second = self.engine.preview_resume(job["job_id"])
        review = {"reviewed": True, "review_sha256": second["review_sha256"]}
        self.engine.resume(job["job_id"], review)
        self.engine.run_next()
        self.refused("review_mismatch", self.engine.resume, job["job_id"], review)
        self.assertEqual(self.engine.preview_resume(job["job_id"])["attempt"], 3)

    def test_first_attempts_and_finished_jobs_do_not_resume(self):
        job = self.submit()
        self.refused("not_resumable", self.engine.resume, job["job_id"], self.reviewed())
        self.refused("not_resumable", self.engine.preview_resume, job["job_id"])
        self.engine.run_next()
        self.refused("not_resumable", self.engine.resume, job["job_id"], self.reviewed())
        self.refused("unknown_job", self.engine.resume, "0" * 32, self.reviewed())

    def test_attempts_are_bounded(self):
        job = self.submit()
        self.runner.error = RuntimeError("again")
        self.engine.run_next()
        for _ in range(jobs.MAX_ATTEMPTS - 1):
            preview = self.engine.preview_resume(job["job_id"])
            self.engine.resume(job["job_id"], {"reviewed": True, "review_sha256": preview["review_sha256"]})
            self.engine.run_next()
        view = self.engine.job(job["job_id"])
        self.assertEqual((view["attempt"], view["resumable"]), (jobs.MAX_ATTEMPTS, False))
        self.refused("not_resumable", self.engine.preview_resume, job["job_id"])
        # resume() has its own bound: a correct review of the next attempt queues nothing.
        before = self.journal()
        calls = len(self.runner.calls)
        beyond = jobs.review_digest(job["job_id"], jobs.MAX_ATTEMPTS + 1, view["request"])
        with self.assertRaises(jobs.Refused):
            self.engine.resume(job["job_id"], {"reviewed": True, "review_sha256": beyond})
        self.assertEqual(self.journal(), before)
        self.assertEqual(self.engine.job(job["job_id"]), view)
        self.assertIsNone(self.engine.run_next())
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(max(json.loads(line)["attempt"] for line in before.splitlines()), jobs.MAX_ATTEMPTS)


class JournalTests(EngineCase):
    def finished(self):
        self.submit()
        self.engine.run_next()
        self.submit(request(submission_id="submission-0002"))
        self.engine.close()
        return self.journal()

    def reopened(self, name, raw):
        target = self.root / name
        target.mkdir()
        (target / "jobs.jsonl").write_bytes(raw)
        return target, self.open(state=target)

    def test_every_action_only_appends(self):
        seen = [b""]

        def grew():
            now = self.journal()
            self.assertTrue(now.startswith(seen[-1]) and len(now) > len(seen[-1]))
            seen.append(now)

        job = self.submit()
        grew()
        self.engine.cancel(job["job_id"])
        grew()
        preview = self.engine.preview_resume(job["job_id"])
        self.engine.resume(job["job_id"], {"reviewed": True, "review_sha256": preview["review_sha256"]})
        grew()
        self.engine.run_next()
        grew()
        entries = [json.loads(line) for line in seen[-1].splitlines()]
        self.assertEqual([entry["seq"] for entry in entries], list(range(1, len(entries) + 1)))
        self.assertEqual([entry["prev"] for entry in entries], [None] + [entry["sha256"] for entry in entries[:-1]])
        self.assertEqual(jobs.read_status(self.state)["jobs"], self.engine.status()["jobs"])

    def test_a_torn_tail_is_superseded_and_the_rest_is_kept(self):
        raw = self.finished()
        lines = raw.splitlines(keepends=True)
        for name, torn in (("cut", raw[:-40]), ("garbage", raw + b'{"v":1,"seq"'), ("two", raw + b'not json\n{"v":')):
            with self.subTest(name=name):
                expected = jobs.replay(b"".join(lines[:-1]) if name == "cut" else raw)["jobs"]
                self.assertEqual(len(jobs.read_status(self.root / "state")["jobs"]), 2)
                target, engine = self.reopened(name, torn)
                after = (target / "jobs.jsonl").read_bytes()
                self.assertTrue(after.startswith(torn))
                status = engine.status()
                self.assertEqual(status["status"], "ok")
                self.assertEqual([job["job_id"] for job in status["jobs"]], list(expected))
                self.assertIn(b'"kind":"repair"', after)
                self.assertIsNone(jobs.replay(after)["problem"])
                self.assertEqual(jobs.replay(after)["tail"], b"")
                engine.close()
                again = self.open(state=target)
                self.assertEqual((target / "jobs.jsonl").read_bytes(), after)
                self.assertEqual(again.status()["jobs"], status["jobs"])

    def test_a_complete_entry_that_lost_only_its_newline_is_kept(self):
        raw = self.finished()
        target, engine = self.reopened("newline", raw[:-1])
        after = (target / "jobs.jsonl").read_bytes()
        self.assertTrue(after.startswith(raw))
        self.assertNotIn(b'"kind":"repair"', after)
        self.assertEqual([job["state"] for job in engine.status()["jobs"]], ["finished", "interrupted"])

    def test_damage_before_the_tail_makes_every_state_unknown(self):
        raw = self.finished()
        lines = raw.splitlines(keepends=True)
        edited = json.loads(lines[1])
        edited["state"] = "finished"
        swapped = json.loads(lines[0])
        swapped["detail"]["request"]["count"] = 5
        cases = {
            "dropped": lines[0] + b"".join(lines[2:]),
            "reordered": lines[1] + lines[0] + b"".join(lines[2:]),
            "edited": lines[0] + json.dumps(edited).encode() + b"\n" + b"".join(lines[2:]),
            "noise": lines[0] + b"noise\n" + b"".join(lines[1:]),
            "rebound": json.dumps(swapped).encode() + b"\n" + b"".join(lines[1:]),
        }
        for name, damaged in cases.items():
            with self.subTest(name=name):
                target, engine = self.reopened(name, damaged)
                status = engine.status()
                self.assertEqual((status["status"], status["jobs"], status["gate"]), ("unknown", [], "closed"))
                self.assertEqual(jobs.read_status(target)["status"], "unknown")
                self.refused(
                    "journal_unavailable",
                    engine.submit,
                    request(submission_id="submission-0009"),
                    self.reviewed(request(submission_id="submission-0009"), engine),
                )
                self.refused("journal_unavailable", engine.run_next)
                self.refused("journal_unavailable", engine.cancel, "0" * 32)
                self.assertEqual((target / "jobs.jsonl").read_bytes(), damaged)
        self.assertEqual(len(self.runner.calls), 1)

    def test_an_edit_that_keeps_a_legal_move_is_detected_by_the_entry_hash(self):
        raw = self.finished()
        lines = raw.splitlines(keepends=True)
        self.assertEqual([json.loads(line)["state"] for line in lines], ["queued", "running", "finished", "queued"])

        def edited(index, change):
            entry = json.loads(lines[index])
            change(entry)
            return fast.canonical(entry).encode() + b"\n"

        def retime(entry):
            entry["at"] = "2026-11-14T12:00:59Z"

        def rename(entry):
            entry["detail"]["result"]["record_id"] = "f" * 64

        def redecide(entry):
            entry["detail"]["result"]["post_decision"] = "edited decision"

        def recount(entry):
            entry["detail"]["result"]["benchmarks"][0]["attempted"] = 3

        for name, index, change in (
            ("timestamp", 1, retime),
            ("record id", 2, rename),
            ("post decision", 2, redecide),
            ("counts", 2, recount),
        ):
            with self.subTest(name=name):
                line = edited(index, change)
                self.assertNotEqual(line, lines[index])
                # Every field a state change is checked against is unchanged; only the hash differs.
                damaged = b"".join(lines[:index]) + line + b"".join(lines[index + 1 :])
                state = jobs.replay(damaged)
                self.assertIsNotNone(state["problem"])
                self.assertEqual(len(state["jobs"]), 0)
                target, engine = self.reopened("hash-%d-%s" % (index, name.replace(" ", "-")), damaged)
                self.assertEqual(engine.status()["status"], "unknown")
                self.assertEqual(jobs.read_status(target)["status"], "unknown")
                self.refused("journal_unavailable", engine.run_next)
                self.assertEqual((target / "jobs.jsonl").read_bytes(), damaged)
        # The same edit in the last entry is an invalid tail: it is never applied.
        last = edited(3, retime)
        state = jobs.replay(b"".join(lines[:3]) + last)
        self.assertEqual((state["problem"], state["tail"], len(state["jobs"])), (None, last, 1))
        ended = jobs.replay(b"".join(lines[:2]) + edited(2, rename))
        self.assertEqual([job["state"] for job in ended["jobs"].values()], ["running"])

    def test_a_repair_entry_must_name_exactly_the_skipped_bytes(self):
        raw = self.finished()
        lines = raw.splitlines(keepends=True)
        second = json.loads(lines[1])
        noise = b'noise\n{"v":1,\n'

        def repaired(skipped, bad):
            body = {
                "v": 1,
                "seq": second["seq"] + 1,
                "prev": second["sha256"],
                "at": second["at"],
                "kind": "repair",
                "skipped_sha256": skipped,
            }
            return lines[0] + lines[1] + bad + (fast.canonical(dict(body, sha256=fast.digest(body))) + "\n").encode()

        exact = repaired(hashlib.sha256(noise).hexdigest(), noise)
        state = jobs.replay(exact)
        self.assertEqual((state["problem"], state["tail"], state["seq"]), (None, b"", 3))
        self.assertEqual([job["state"] for job in state["jobs"].values()], ["running"])
        cases = {
            "another hash": repaired(hashlib.sha256(b"other\n").hexdigest(), noise),
            "part of the bytes": repaired(hashlib.sha256(noise[:6]).hexdigest(), noise),
            "more bytes": repaired(hashlib.sha256(noise).hexdigest(), noise + b"more\n"),
            "not a hash": repaired(None, noise),
            "nothing skipped": repaired(hashlib.sha256(b"").hexdigest(), b""),
        }
        for name, damaged in cases.items():
            with self.subTest(name=name):
                self.assertIsNotNone(jobs.replay(damaged)["problem"])
                target, engine = self.reopened("repair-" + name.replace(" ", "-"), damaged)
                self.assertEqual(engine.status()["status"], "unknown")
                self.refused("journal_unavailable", engine.cancel, second["job_id"])
                self.assertEqual((target / "jobs.jsonl").read_bytes(), damaged)

    def test_a_validly_hashed_illegal_move_is_damage(self):
        raw = self.finished()
        last = json.loads(raw.splitlines()[-1])
        first = json.loads(raw.splitlines()[0])

        def forged(state, detail, attempt=1, job_id=first["job_id"]):
            body = {
                "v": 1,
                "seq": last["seq"] + 1,
                "prev": last["sha256"],
                "at": last["at"],
                "kind": "job",
                "job_id": job_id,
                "attempt": attempt,
                "state": state,
                "detail": detail,
            }
            return raw + (fast.canonical(dict(body, sha256=fast.digest(body))) + "\n").encode()

        cases = [
            forged("running", {"cancel_requested": False}),
            forged("queued", {"review_sha256": "0" * 64}, attempt=2),
            forged("finished", {"result": None}),
            forged("paused", {}),
            forged("cancelled", {"reason": "x"}, job_id="0" * 32),
            forged("failed", {"reason": "bad\ntext", "result": None}),
            forged("cancelled", {"reason": "x", "extra": 1}),
        ]
        for index, damaged in enumerate(cases):
            with self.subTest(index=index):
                self.assertIsNotNone(jobs.replay(damaged)["problem"])

    def test_a_linked_or_oversized_journal_refuses(self):
        raw = self.finished()
        target = self.root / "linked"
        target.mkdir()
        (self.root / "elsewhere.jsonl").write_bytes(raw)
        os.symlink(self.root / "elsewhere.jsonl", target / "jobs.jsonl")
        engine = self.open(state=target)
        self.assertEqual(engine.status()["status"], "unknown")
        self.assertEqual(jobs.read_status(target)["status"], "unknown")
        self.assertEqual((self.root / "elsewhere.jsonl").read_bytes(), raw)
        with mock.patch.object(jobs, "MAX_JOURNAL_BYTES", len(raw) - 1):
            self.assertEqual(jobs.read_status(self.state)["status"], "unknown")

    def test_a_write_failure_makes_state_unknown(self):
        job = self.submit()
        with mock.patch.object(jobs.os, "write", side_effect=OSError("full")):
            self.refused("journal_unavailable", self.engine.cancel, job["job_id"])
        self.assertEqual(self.engine.status()["status"], "unknown")
        self.refused("journal_unavailable", self.engine.run_next)
        self.assertEqual(jobs.read_status(self.state)["jobs"][0]["state"], "queued")

    def test_a_nearly_full_journal_refuses_new_work(self):
        review = self.reviewed()
        with mock.patch.object(jobs, "JOURNAL_RESERVE_BYTES", jobs.MAX_JOURNAL_BYTES + 1):
            self.refused("journal_full", self.engine.submit, request(), review)
        with mock.patch.object(jobs, "MAX_JOBS", 0):
            self.refused("journal_full", self.engine.submit, request(), review)

    def test_the_state_directory_must_be_absolute(self):
        for value in ("relative/state", str(self.root / ".." / "state")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                jobs.Engine(value)
        with self.assertRaises(ValueError):
            jobs.Engine(self.root / "s", sample_sets={"screening": ["relative.json"]})


class DatasetSinkTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.folder = self.root / "dataset"
        self.folder.mkdir()
        self.path = self.folder / "dataset.json"
        self.store = jobs.DatasetSink(self.folder, now=self.now)

    def test_a_valid_record_creates_a_dataset_the_tuning_reader_accepts(self):
        record = make_record()
        self.assertEqual(self.store(record), "added")
        data = tuning.ingest([self.path], require_normalized=True)
        self.assertEqual([row["record_id"] for row in data["fast_checks"]], [record["record_id"]])
        self.assertEqual(data["rejected_fast_checks"], [])
        self.assertEqual(data["ingested_at"], "2026-11-14T12:00:01Z")
        before = self.path.read_bytes()
        self.assertEqual(self.store(record), "present")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["dataset.json"])

    def test_existing_content_is_kept_and_a_second_record_is_added(self):
        self.store(make_record())
        second = make_record(model=dict(MODEL, file_sha256="d" * 64, name="synthetic second"))
        self.assertEqual(self.store(second), "added")
        data = tuning.ingest([self.path], require_normalized=True)
        self.assertEqual(len(data["fast_checks"]), 2)

    def test_an_invalid_record_leaves_the_dataset_untouched(self):
        self.store(make_record())
        before = self.path.read_bytes()
        tampered = make_record(model=dict(MODEL, file_sha256="e" * 64))
        tampered["benchmarks"][0]["correct"] = 0
        for value in (tampered, {}, None):
            with self.subTest(value=type(value)), self.assertRaises(Exception):
                self.store(value)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["dataset.json"])

    def test_an_unusable_existing_dataset_is_left_alone(self):
        for index, content in enumerate(
            (
                b"not json",
                b'{"schema_version":2,"runs":[],"items":[]}',
                b'{"schema_version":1,"runs":[{"status":"COMPUTED"}],"items":[]}',
            )
        ):
            with self.subTest(index=index):
                self.path.write_bytes(content)
                with self.assertRaises(Exception):
                    self.store(make_record())
                self.assertEqual(self.path.read_bytes(), content)
                self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["dataset.json"])
        self.path.unlink()
        os.symlink(self.root / "elsewhere.json", self.path)
        with self.assertRaises(Exception):
            self.store(make_record())
        self.assertFalse((self.root / "elsewhere.json").exists())
        with self.assertRaises(Exception):
            jobs.DatasetSink(self.root / "absent")(make_record())
        with self.assertRaises(ValueError):
            jobs.DatasetSink("relative")

    def test_the_comparison_limit_is_never_exceeded(self):
        self.store(make_record())
        before = self.path.read_bytes()
        second = make_record(model=dict(MODEL, file_sha256="d" * 64))
        with mock.patch.object(tuning, "MAX_DATASET_COMPARISON_PAIRS", 0), self.assertRaises(tuning.ValidationError):
            self.store(second)
        with mock.patch.object(tuning, "MAX_DATASET_RUNS", 1), self.assertRaises(tuning.ValidationError):
            self.store(second)
        with mock.patch.object(tuning, "MAX_DATASET_BYTES", len(before)), self.assertRaises(tuning.ValidationError):
            self.store(second)
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_dataset_changed_during_ingestion_is_not_replaced(self):
        self.store(make_record())
        real = tuning.ingest
        other = b'{"schema_version":1,"runs":[],"items":[],"fast_checks":[]}\n'

        def racing(paths, **options):
            data = real(paths, **options)
            self.path.write_bytes(other)
            return data

        with mock.patch.object(tuning, "ingest", racing), self.assertRaises(tuning.ValidationError):
            self.store(make_record(model=dict(MODEL, file_sha256="d" * 64)))
        self.assertEqual(self.path.read_bytes(), other)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["dataset.json"])

    def test_the_engine_ingests_through_the_sink_once(self):
        engine = self.open(state=self.root / "with-sink", sink=self.store)
        self.submit(engine=engine)
        done = engine.run_next()
        self.assertEqual(done["state"], "finished")
        data = tuning.ingest([self.path], require_normalized=True)
        self.assertEqual([row["record_id"] for row in data["fast_checks"]], [done["result"]["record_id"]])
        self.path.write_bytes(b"not json")
        self.submit(request(submission_id="submission-0002"), engine=engine)
        failed = engine.run_next()
        self.assertEqual(failed["reason"], "The record could not be ingested; the dataset is unchanged.")
        self.assertEqual(self.path.read_bytes(), b"not json")


class AdapterRunnerTests(unittest.TestCase):
    SPEC = {
        "base_url": "http://127.0.0.1:18300",
        "model_id": "synthetic/candidate",
        "sample_files": ["/samples/a.json"],
        "output_dir": "/state/runs/x",
        "count": 4,
        "max_tokens": 16384,
        "request_timeout_s": 180,
        "wall_budget_s": "60",
        "disable_thinking": True,
        "facts": {"quantization": "Q4_K_M", "memory_bytes": 9},
    }

    def test_it_only_calls_the_public_run_with_the_reviewed_values(self):
        with mock.patch.object(jobs.adapter, "run", return_value={"record": 1}) as run:
            result = jobs.adapter_runner(self.SPEC, lambda: None, lambda done, planned: None)
        self.assertEqual(result, {"record": 1})
        args, kwargs = run.call_args
        self.assertEqual(
            args, ("http://127.0.0.1:18300", "synthetic/candidate", [Path("/samples/a.json")], Path("/state/runs/x"))
        )
        self.assertEqual(
            kwargs,
            dict(
                count=4,
                max_tokens=16384,
                timeout=180,
                wall_budget_s="60",
                disable_thinking=True,
                model_sha256=None,
                quantization="Q4_K_M",
                model_label=None,
                machine_label=None,
                cpu=None,
                gpu=None,
                runtime_build_id=None,
                file_size_bytes=None,
                available_memory_bytes=None,
                memory_bytes=9,
            ),
        )

    def test_a_stop_request_before_the_first_call_contacts_nothing(self):
        with mock.patch.object(jobs.adapter, "run") as run, self.assertRaises(jobs.Stopped):
            jobs.adapter_runner(self.SPEC, lambda: "cancel", lambda done, planned: None)
        run.assert_not_called()


class SourceTests(unittest.TestCase):
    def test_the_engine_imports_nothing_that_starts_a_model_or_opens_a_socket(self):
        tree = ast.parse((ROOT / "eci_jobs.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertFalse(
            imported
            & {
                "subprocess",
                "socket",
                "http",
                "urllib3",
                "requests",
                "asyncio",
                "model_control",
                "server",
                "online_code_repair",
                "multiprocessing",
            }
        )
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse(names & {"system", "popen", "Popen", "urlopen", "create_connection", "Thread"})
        self.assertIs(jobs.Engine.__init__.__kwdefaults__["policy"], jobs.CLOSED)
        self.assertIsNone(jobs.Engine.__init__.__kwdefaults__["runner"])


if __name__ == "__main__":
    unittest.main()
