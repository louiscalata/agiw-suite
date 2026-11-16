"""Operator-stated already-loaded fit gate: in-memory HTTP fixtures only.

No sockets, ports, model processes or browsers. The fixture imitates the
llama-server shapes measured on 2026-11-15: /props names the loaded model in
model_alias, reports is_sleeping false and carries no hardware memory; /slots
lists slot state. ReplayedCaptureTests uses the fields the adapter reads from
the saved R5 Ornith /props, /v1/models and /slots captures.
"""

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import eci_fast_check as fast
import eci_fast_check_adapter as adapter
import eci_tuning


ROOT = Path(__file__).resolve().parent
MODEL_ID = "fixture-model"
MODEL_SHA = "a" * 64


def completion(content="A", model=MODEL_ID):
    return {
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    }


def served_props(**changes):
    value = {
        "model_alias": MODEL_ID,
        "model_ftype": "Q4_0",
        "total_slots": 1,
        "is_sleeping": False,
        "default_generation_settings": {"n_ctx": 32768},
    }
    value.update(changes)
    return value


def served_models(*ids, meta=None):
    return {"data": [dict(id=i, meta=dict(meta or {"n_ctx": 32768, "size": 90, "ftype": "Q4_0"}))
                     for i in (ids or (MODEL_ID,))]}


class LoadedLane:
    """A fake loaded lane. Records every request; never opens a socket."""

    def __init__(self, *, props=None, models=None, slots=None, slots_status=200, missing=()):
        self.missing = missing  # Endpoints that answer 404.
        self.props = served_props() if props is None else props
        self.models = served_models() if models is None else models
        self.slots = [{"id": 0, "n_ctx": 32768, "is_processing": False}] if slots is None else slots
        self.slots_status = slots_status
        self.model = MODEL_ID
        self.requests = []
        self.posts = []

    def __enter__(self):
        lane = self

        class Transport:
            def shutdown(self, how):
                pass

            def settimeout(self, timeout):
                pass

        class Connection:
            def __init__(self, host, port, timeout):
                self.sock = Transport()

            def connect(self):
                pass

            def request(self, method, path, body=None, headers=None):
                lane.requests.append((method, path))
                self.status = 200
                if path in lane.missing:
                    self.status, reply = 404, {"error": "fixture missing route"}
                elif method == "GET" and path == "/props":
                    reply = lane.props
                elif method == "GET" and path == "/v1/models":
                    reply = lane.models
                elif method == "GET" and path == "/slots":
                    self.status, reply = lane.slots_status, lane.slots
                elif method == "POST" and path == "/v1/chat/completions":
                    lane.posts.append(json.loads(body))
                    reply = completion(model=lane.model)
                else:
                    self.status, reply = 404, {"error": "fixture missing route"}
                self.body = io.BytesIO(json.dumps(reply).encode("utf-8"))

            def getresponse(self):
                return self

            def read1(self, size):
                return self.body.read(size)

            def close(self):
                pass

        self.patcher = patch.object(adapter.http.client, "HTTPConnection", Connection)
        self.patcher.start()
        self.url = "http://127.0.0.1:1"  # An inert parser input, never contacted.
        return self

    def __exit__(self, *args):
        self.patcher.stop()


def redigest(record):
    """Recompute only the record id, so a test isolates one forged claim."""
    record = copy.deepcopy(record)
    record.pop("record_id")
    record["record_id"] = fast.digest(record)
    return record


class AlreadyLoadedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.samples = [self.path / "arc.json"]
        self.samples[0].write_text(json.dumps({"arc": {"items": [
            {"id": "one", "question": "Choose the fixture answer.",
             "choices": ["first", "second", "third", "fourth"], "answer": "A"},
            {"id": "two", "question": "Choose another fixture answer.",
             "choices": ["first", "second", "third", "fourth"], "answer": "A"},
        ]}}), encoding="utf-8")

    def run_lane(self, lane, output="out", **kwargs):
        facts = dict(model_sha256=MODEL_SHA, file_size_bytes=100)
        facts.update(kwargs)
        return adapter.run(lane.url, MODEL_ID, self.samples, self.path / output, **facts)

    def loaded_record(self, output="out"):
        with LoadedLane() as lane:
            return self.run_lane(lane, output, already_loaded=True)

    # Default stays fail-closed.

    def test_unknown_memory_still_refuses_without_the_operator_statement(self):
        with LoadedLane() as lane:
            with self.assertRaisesRegex(fast.ValidationError, "fit gate"):
                self.run_lane(lane)
            self.assertEqual(lane.posts, [])
            self.assertNotIn(("GET", "/slots"), lane.requests)

    # Accepted only with matching served model and idle slots.

    def test_matching_idle_lane_runs_and_records_the_mode(self):
        with LoadedLane() as lane:
            record = self.run_lane(lane, already_loaded=True)
            self.assertIn(("GET", "/slots"), lane.requests)
            self.assertEqual(len(lane.posts), 2)
        self.assertEqual(fast.validate_record(record), record)
        self.assertEqual(record["pre"]["decision"], "UNKNOWN")
        self.assertIsNone(record["pre"]["inputs"]["available_memory_bytes"])
        self.assertEqual(record["pre"]["inputs"]["file_size_bytes"], 100)
        self.assertEqual(record["pre"]["inputs"]["quantization"], "Q4_0")
        provenance = record["provenance"]
        self.assertEqual(provenance["fit_gate"], "already_loaded")
        self.assertEqual(provenance["available_memory_source"], "unknown")
        self.assertEqual(provenance["already_loaded"], {
            "statement": "operator-stated: the requested model is already loaded and serving; "
                         "available memory was not measured",
            "served_model": MODEL_ID,
            "served_model_source": "/props model_alias",
            "advertised_models": 1,
            "n_ctx": 32768,
            "total_slots": 1,
            "slots_idle": True,
            "is_sleeping": False,
            "evidence_sources": ["/props", "/slots", "/v1/models"],
            "file_size_bytes": 100,
            "file_size_source": "operator-stated",
            "file_sha256_source": "operator-stated",
            "quantization_source": "server-reported",
        })
        self.assertTrue(all(b["stop_reason"] == "complete" for b in record["benchmarks"]))
        self.assertIsNone(record["hardware"]["memory"])
        self.assertIsNone(record["hardware"]["memory_bytes"])

    def test_resume_reuses_the_already_loaded_checkpoint(self):
        first = self.loaded_record()
        with LoadedLane() as lane:
            second = self.run_lane(lane, already_loaded=True)
            self.assertEqual(lane.posts, [])
        outcomes = [[(o["item_id"], o["outcome"]) for o in b["observations"]] for b in first["benchmarks"]]
        self.assertEqual(outcomes, [[(o["item_id"], o["outcome"]) for o in b["observations"]]
                                    for b in second["benchmarks"]])
        self.assertEqual(first["provenance"], second["provenance"])

    # Refusals: mismatch, busy, contradicting facts, mixed modes.

    def assert_refused(self, lane, pattern, **kwargs):
        kwargs.setdefault("already_loaded", True)
        with lane:
            with self.assertRaisesRegex((fast.ValidationError, adapter.ResponseError), pattern):
                self.run_lane(lane, **kwargs)
            self.assertEqual(lane.posts, [])
        self.assertFalse((self.path / "out" / "result.json").exists())

    def test_refused_when_the_served_model_differs(self):
        self.assert_refused(LoadedLane(props=served_props(model_alias="other-model")), "served model")

    def test_refused_when_the_server_does_not_name_the_loaded_model(self):
        props = served_props()
        del props["model_alias"]
        self.assert_refused(LoadedLane(props=props), "served model")

    def test_refused_when_several_models_are_advertised(self):
        self.assert_refused(LoadedLane(models=served_models(MODEL_ID, "other-model")), "one advertised model")

    def test_refused_when_a_slot_is_busy(self):
        slots = [{"id": 0, "n_ctx": 16384, "is_processing": False},
                 {"id": 1, "n_ctx": 16384, "is_processing": True}]
        self.assert_refused(LoadedLane(slots=slots, props=served_props(total_slots=2)), "busy")

    def test_refused_when_slot_state_is_unknown(self):
        for slots, status in (([], 200), ([{"id": 0}], 200), ([{"id": 0, "is_processing": "no"}], 200),
                              ([{"id": 0, "is_processing": False}], 404)):
            with self.subTest(slots=slots, status=status):
                self.assert_refused(LoadedLane(slots=slots, slots_status=status), "slot")

    def test_refused_when_slot_count_disagrees_or_is_missing(self):
        self.assert_refused(LoadedLane(props=served_props(total_slots=2)), "slot count")
        for total in ("1", True, 1.0):
            with self.subTest(total=total):
                self.assert_refused(LoadedLane(props=served_props(total_slots=total)), "slot count")
        props = served_props()
        del props["total_slots"]
        self.assert_refused(LoadedLane(props=props), "slot count")

    def test_refused_when_context_is_unknown_or_disagrees(self):
        props = served_props()
        del props["default_generation_settings"]
        self.assert_refused(LoadedLane(props=props, models=served_models(meta={"ftype": "Q4_0"})), "context")
        self.assert_refused(LoadedLane(models=served_models(meta={"n_ctx": 4096, "ftype": "Q4_0"})), "context")

    def test_refused_when_either_context_size_is_missing(self):
        # Both /props and the model metadata must report the context size.
        missing = "needs the context size"
        props = served_props()
        del props["default_generation_settings"]
        self.assert_refused(LoadedLane(props=props), missing)
        self.assert_refused(LoadedLane(props=served_props(default_generation_settings={})), missing)
        self.assert_refused(LoadedLane(models=served_models(meta={"size": 90, "ftype": "Q4_0"})), missing)
        self.assert_refused(LoadedLane(models=served_models(meta={"n_ctx": "32768", "ftype": "Q4_0"})), missing)

    def test_refused_when_the_lane_is_sleeping(self):
        # A sleeping llama-server has unloaded its weights; only absent or false passes.
        for value in (True, None, 0, "false", {}):
            with self.subTest(is_sleeping=value):
                lane = LoadedLane(props=served_props(is_sleeping=value))
                self.assert_refused(lane, "sleep")
                self.assertNotIn(("GET", "/slots"), lane.requests)

    def test_absent_sleep_state_is_accepted_and_recorded_as_null(self):
        props = served_props()
        del props["is_sleeping"]
        with LoadedLane(props=props) as lane:
            record = self.run_lane(lane, already_loaded=True)
            self.assertEqual(len(lane.posts), 2)
        self.assertIsNone(record["provenance"]["already_loaded"]["is_sleeping"])
        self.assertEqual(fast.validate_record(record), record)

    def test_refused_when_an_explicit_model_fact_disagrees(self):
        self.assert_refused(LoadedLane(), "disagrees", quantization="Q8_0")

    def test_refused_without_operator_stated_file_size_or_quantization(self):
        self.assert_refused(LoadedLane(), "file size", file_size_bytes=None)
        self.assert_refused(LoadedLane(models=served_models(meta={"n_ctx": 32768})), "quantization")

    def test_refused_when_available_memory_is_known(self):
        self.assert_refused(LoadedLane(), "fit gate", available_memory_bytes=8000)
        props = served_props(hardware={"available_memory_bytes": 8000})
        self.assert_refused(LoadedLane(props=props), "fit gate")

    def test_flag_must_be_a_boolean(self):
        self.assert_refused(LoadedLane(), "boolean", already_loaded="yes")

    # A known memory reading, zero included, is never treated as unknown.

    def fresh_output(self):
        """Give each subtest its own output directory so one cannot mask another."""
        self.path = Path(tempfile.mkdtemp(dir=self.directory.name))

    def run_capturing_pre(self, lane, **kwargs):
        decisions, original = [], fast.pre_check

        def capture(*args, **kw):
            decisions.append(original(*args, **kw))
            return decisions[-1]

        with patch.object(adapter.fast, "pre_check", side_effect=capture):
            with lane, self.assertRaisesRegex(fast.ValidationError, "fit gate"):
                self.run_lane(lane, **kwargs)
            self.assertEqual(lane.posts, [])
        return decisions

    def test_server_reported_zero_memory_refuses_already_loaded(self):
        lane = LoadedLane(props=served_props(hardware={"available_memory_bytes": 0}))
        self.assert_refused(lane, "available memory is known")
        self.assertNotIn(("GET", "/slots"), lane.requests)

    def test_server_reported_zero_memory_is_a_skip_on_the_default_path(self):
        self.assertEqual(fast.pre_check(100, "Q4_0", 0)["local_decision"], "SKIP")
        lane = LoadedLane(props=served_props(hardware={"available_memory_bytes": 0}))
        [pre] = self.run_capturing_pre(lane)
        self.assertEqual((pre["decision"], pre["inputs"]["available_memory_bytes"]), ("SKIP", 0))

    def test_absent_or_null_server_memory_stays_unknown(self):
        for hardware in ({}, {"available_memory_bytes": None, "memory_bytes": None}):
            with self.subTest(hardware=hardware):
                output = "absent" if not hardware else "null"
                with LoadedLane(props=served_props(hardware=hardware)) as lane:
                    record = self.run_lane(lane, output, already_loaded=True)
                    self.assertEqual(len(lane.posts), 2)
                self.assertEqual(record["pre"]["decision"], "UNKNOWN")
                self.assertIsNone(record["pre"]["inputs"]["available_memory_bytes"])
                self.assertEqual(fast.validate_record(record), record)
                [pre] = self.run_capturing_pre(LoadedLane(props=served_props(hardware=hardware)))
                self.assertEqual(pre["decision"], "UNKNOWN")

    def test_invalid_server_reported_memory_refuses_on_both_paths(self):
        invalid = [("available_memory_bytes", value) for value in
                   (-1, True, False, "0", "8000", 8000.0, 0.0, float("nan"), float("inf"), [], {})]
        invalid += [("memory_bytes", value) for value in (0, -1, True, "8000", 8000.0, float("nan"))]
        for key, value in invalid:
            for loaded in (True, False):
                with self.subTest(key=key, value=value, already_loaded=loaded):
                    self.fresh_output()
                    lane = LoadedLane(props=served_props(hardware={key: value}))
                    self.assert_refused(lane, "invalid server-reported memory|invalid JSON", already_loaded=loaded)
                    self.assertNotIn(("GET", "/slots"), lane.requests)

    def test_huge_server_reported_memory_is_known_and_refuses_already_loaded(self):
        for value in (2**64, 10**300, 10**400):
            with self.subTest(value=value):
                self.fresh_output()
                lane = LoadedLane(props=served_props(hardware={"available_memory_bytes": value}))
                self.assert_refused(lane, "available memory is known|invalid JSON")
                self.assertNotIn(("GET", "/slots"), lane.requests)

    def test_explicit_zero_memory_refuses_already_loaded_and_skips_by_default(self):
        self.assert_refused(LoadedLane(), "available memory is known", available_memory_bytes=0)
        [pre] = self.run_capturing_pre(LoadedLane(), available_memory_bytes=0)
        self.assertEqual((pre["decision"], pre["inputs"]["available_memory_bytes"]), ("SKIP", 0))

    def test_invalid_explicit_memory_refuses_before_any_request(self):
        for value in (-1, True, False, "0", "8000", 0.0, 8000.0, float("nan"), float("inf")):
            for loaded in (True, False):
                with self.subTest(value=value, already_loaded=loaded):
                    self.fresh_output()
                    lane = LoadedLane()
                    self.assert_refused(lane, "expected a number|number outside", already_loaded=loaded,
                                        available_memory_bytes=value)
                    self.assertEqual(lane.requests, [])

    def test_cli_explicit_zero_memory_with_the_flag_refuses(self):
        with LoadedLane() as lane, contextlib.redirect_stderr(io.StringIO()) as stderr:
            status = adapter.main(["--base-url", lane.url, "--model", MODEL_ID,
                                   "--samples", str(self.samples[0]), "--output-dir", str(self.path / "out"),
                                   "--model-sha256", MODEL_SHA, "--file-size-bytes", "100",
                                   "--available-memory-bytes", "0", "--already-loaded"])
            self.assertEqual(lane.posts, [])
        self.assertEqual(status, 2)
        self.assertIn("available memory is known", stderr.getvalue())

    # Validation: the mode is bound and cannot be forged or dropped.

    def test_record_without_the_mode_cannot_carry_observations(self):
        record = self.loaded_record()
        for drop in ("fit_gate", "already_loaded"):
            with self.subTest(drop=drop):
                forged = copy.deepcopy(record)
                del forged["provenance"][drop]
                with self.assertRaises(fast.ValidationError):
                    fast.validate_record(redigest(forged))

    def test_record_with_weakened_evidence_is_refused(self):
        record = self.loaded_record()
        changes = [
            ("slots_idle", False), ("slots_idle", 1), ("advertised_models", 2), ("advertised_models", True),
            ("file_size_source", "server-reported"), ("served_model", "other-model"),
            ("file_size_bytes", 99), ("file_size_bytes", 100.0), ("n_ctx", 0), ("total_slots", True),
            ("evidence_sources", ["/props", "/v1/models"]),
            ("served_model_source", "/v1/models id"), ("file_sha256_source", "unknown"),
            ("quantization_source", "unknown"), ("statement", "loaded"), ("statement", 7),
            ("is_sleeping", True), ("is_sleeping", 0), ("is_sleeping", "false"),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                forged = copy.deepcopy(record)
                forged["provenance"]["already_loaded"][key] = value
                with self.assertRaises(fast.ValidationError):
                    fast.validate_record(redigest(forged))
        for key, value in (("is_sleeping", None), ("quantization_source", "operator-stated"),
                           ("file_sha256_source", "server-reported"), ("served_model_source", "/props model.id")):
            with self.subTest(accepted=key, value=value):
                kept = copy.deepcopy(record)
                kept["provenance"]["already_loaded"][key] = value
                kept = redigest(kept)
                self.assertEqual(fast.validate_record(kept), kept)
        forged = copy.deepcopy(record)
        forged["provenance"]["fit_gate"] = "memory_estimate"
        with self.assertRaises(fast.ValidationError):
            fast.validate_record(redigest(forged))

    def test_mode_is_refused_when_memory_was_known(self):
        record = self.loaded_record()
        forged = copy.deepcopy(record)
        forged["pre"] = fast.pre_check(100, "Q4_0", 8000)
        with self.assertRaises(fast.ValidationError):
            fast.validate_record(redigest(forged))

    def test_mode_is_refused_when_memory_was_known_as_zero(self):
        record = self.loaded_record()
        skip = fast.pre_check(100, "Q4_0", 0)
        self.assertEqual(skip["decision"], "SKIP")
        forged = copy.deepcopy(record)
        forged["pre"] = skip
        with self.assertRaisesRegex(fast.ValidationError, "available memory is unknown"):
            fast.validate_record(redigest(forged))
        # Relabelling a known zero as UNKNOWN fails the recomputation of pre.
        forged = copy.deepcopy(record)
        forged["pre"]["inputs"]["available_memory_bytes"] = 0
        with self.assertRaisesRegex(fast.ValidationError, "pre decision"):
            fast.validate_record(redigest(forged))

    def test_run_check_refuses_a_stated_gate_beside_a_check(self):
        # The gate is validated even when pre alone would authorize the run.
        evidence = self.loaded_record()["provenance"]["already_loaded"]
        original = fast.run_check

        def with_stated_gate(*args, provenance, **kwargs):
            provenance = dict(provenance, fit_gate="already_loaded", already_loaded=evidence)
            return original(*args, provenance=provenance, **kwargs)

        with LoadedLane() as lane, patch.object(adapter.fast, "run_check", with_stated_gate):
            with self.assertRaisesRegex(fast.ValidationError, "available memory is unknown"):
                self.run_lane(lane, "gated", available_memory_bytes=8000)
            self.assertEqual(lane.posts, [])

    # Ranking keeps the two kinds apart; ingest accepts the record.

    def gated_record(self):
        with LoadedLane() as lane:
            return self.run_lane(lane, "gated", available_memory_bytes=8000)

    def test_rank_lists_already_loaded_as_screening_only(self):
        loaded, gated = self.loaded_record(), self.gated_record()
        self.assertNotIn("fit_gate", gated.get("provenance", {}))
        views = fast.rank([loaded, gated])
        ranked = [row["record_id"] for view in views for row in view["rows"]]
        pending = [row for view in views for row in view["pending"]]
        self.assertNotIn(loaded["record_id"], ranked)
        (row,) = [row for row in pending if row["record_id"] == loaded["record_id"]]
        self.assertEqual(row["fit_gate"], "already_loaded")
        self.assertIn("already loaded", row["reason"])
        def gate_differs(first, second):
            return any(r.startswith("fit gate differs") for r in fast.screening_mismatches(first, second))
        self.assertTrue(gate_differs(loaded, gated))
        self.assertFalse(gate_differs(gated, gated))
        self.assertFalse(gate_differs(loaded, loaded))
        for view in views:
            for other in view["pending"]:
                if other["record_id"] != loaded["record_id"]:
                    self.assertNotIn("fit_gate", other)

    def test_ingest_accepts_and_ranks_the_record_as_pending(self):
        record = self.loaded_record()
        path = self.path / "dataset.json"
        path.write_text(json.dumps({"schema_version": 1, "runs": [], "items": [],
                                    "fast_checks": [record]}), encoding="utf-8")
        data = eci_tuning.ingest([path])
        self.assertEqual(data["fast_checks"], [record])
        self.assertEqual(data["rejected_fast_checks"], [])
        views = eci_tuning.rank(data)["fast_check"]["hardware_views"]
        self.assertEqual([row["record_id"] for view in views for row in view["pending"]], [record["record_id"]])

    # Interfaces: CLI flag and schema.

    def test_cli_flag_reaches_run(self):
        with patch.object(adapter, "run", side_effect=fast.ValidationError("stop")) as run, \
                contextlib.redirect_stderr(io.StringIO()):
            adapter.main(["--base-url", "http://127.0.0.1:1", "--model", MODEL_ID,
                          "--samples", str(self.samples[0]), "--output-dir", str(self.path / "out"),
                          "--already-loaded"])
            self.assertIs(run.call_args.kwargs["already_loaded"], True)
            adapter.main(["--base-url", "http://127.0.0.1:1", "--model", MODEL_ID,
                          "--samples", str(self.samples[0]), "--output-dir", str(self.path / "out")])
            self.assertIs(run.call_args.kwargs["already_loaded"], False)

    def test_schema_describes_the_mode(self):
        schema = fast.load(ROOT / "eci_fast_check.schema.json")
        provenance = schema["$defs"]["record"]["properties"]["provenance"]
        self.assertEqual(provenance["properties"]["fit_gate"], {"enum": ["already_loaded"]})
        evidence = schema["$defs"]["alreadyLoaded"]
        self.assertEqual(provenance["properties"]["already_loaded"], {"$ref": "#/$defs/alreadyLoaded"})
        self.assertEqual(set(evidence["required"]), set(fast.ALREADY_LOADED_FIELDS.split()))
        self.assertFalse(evidence["additionalProperties"])
        from test_eci_measured_data import schema_errors
        record = self.loaded_record()
        self.assertEqual(schema_errors(record, schema["$defs"]["record"], schema), [])
        for drop in ("fit_gate", "already_loaded"):
            with self.subTest(drop=drop):
                unpaired = copy.deepcopy(record)
                del unpaired["provenance"][drop]
                self.assertNotEqual(schema_errors(unpaired, schema["$defs"]["record"], schema), [])
        for key, value in (("slots_idle", False), ("is_sleeping", True), ("is_sleeping", 0),
                           ("statement", "loaded")):
            with self.subTest(key=key, value=value):
                forged = copy.deepcopy(record)
                forged["provenance"]["already_loaded"][key] = value
                self.assertNotEqual(schema_errors(forged, schema["$defs"]["record"], schema), [])
        record["provenance"]["already_loaded"]["is_sleeping"] = None
        self.assertEqual(schema_errors(record, schema["$defs"]["record"], schema), [])


ABSENT = object()  # The source carries no hardware object at all.
NAN, INF = float("nan"), float("inf")
INVALID_AVAILABLE = (-1, True, False, "0", "8000", 8000.0, 0.0, NAN, INF, [], {}, [8000], {"bytes": 8000})
INVALID_TOTAL = INVALID_AVAILABLE + (0,)
BAD_HARDWARE = (None, [], [{"available_memory_bytes": 0}], "8000", 8000, 0, True)
REFUSALS = {
    "conflict": "conflicting server-reported memory",
    "invalid": "invalid server-reported memory|invalid JSON",
    "hardware": "invalid hardware metadata",
}


def source_cases(key, invalid):
    """Every /props.hardware and metadata hardware combination for one memory key.

    Each row is (name, props hardware, metadata hardware, expected): None for
    unknown, an integer for a known reading, or a REFUSALS name.
    """
    rows = [
        ("neither source has hardware", ABSENT, ABSENT, None),
        ("both hardware objects are empty", {}, {}, None),
        ("both report null", {key: None}, {key: None}, None),
        ("props only", {key: 8000}, ABSENT, 8000),
        ("metadata only", ABSENT, {key: 8000}, 8000),
        ("props empty, metadata reports", {}, {key: 8000}, 8000),
        ("props has another key, metadata reports", {"cpu": "fixture-cpu"}, {key: 8000}, 8000),
        ("props null, metadata reports", {key: None}, {key: 8000}, 8000),
        ("props reports, metadata null", {key: 8000}, {key: None}, 8000),
        ("both agree", {key: 8000}, {key: 8000}, 8000),
        ("both disagree", {key: 8000}, {key: 4000}, "conflict"),
    ]
    if key == "available_memory_bytes":
        rows += [
            ("props zero only", {key: 0}, ABSENT, 0),
            ("props absent, metadata zero", ABSENT, {key: 0}, 0),
            ("props empty, metadata zero", {}, {key: 0}, 0),
            ("props has another key, metadata zero", {"cpu": "fixture-cpu"}, {key: 0}, 0),
            ("both zero", {key: 0}, {key: 0}, 0),
            ("props zero, metadata positive", {key: 0}, {key: 8000}, "conflict"),
            ("props positive, metadata zero", {key: 8000}, {key: 0}, "conflict"),
        ]
    for value in invalid:
        rows += [
            (f"props invalid {value!r}, metadata valid", {key: value}, {key: 8000}, "invalid"),
            (f"props valid, metadata invalid {value!r}", {key: 8000}, {key: value}, "invalid"),
            (f"props invalid {value!r} only", {key: value}, ABSENT, "invalid"),
            (f"metadata invalid {value!r} only", ABSENT, {key: value}, "invalid"),
            (f"props empty, metadata invalid {value!r}", {}, {key: value}, "invalid"),
        ]
    for value in BAD_HARDWARE:
        rows += [
            (f"props hardware {value!r}, metadata valid", value, {key: 8000}, "hardware"),
            (f"props valid, metadata hardware {value!r}", {key: 8000}, value, "hardware"),
            (f"props hardware {value!r} only", value, ABSENT, "hardware"),
            (f"props empty, metadata hardware {value!r}", {}, value, "hardware"),
        ]
    return rows


def source_lane(props_hardware=ABSENT, meta_hardware=ABSENT, **kwargs):
    props = served_props()
    meta = {"n_ctx": 32768, "size": 90, "ftype": "Q4_0"}
    if props_hardware is not ABSENT:
        props["hardware"] = props_hardware
    if meta_hardware is not ABSENT:
        meta["hardware"] = meta_hardware
    return LoadedLane(props=props, models=served_models(meta=meta), **kwargs)


class MemorySourceTests(unittest.TestCase):
    """Each memory source is read on its own: one fixture per source combination."""

    setUp = AlreadyLoadedTests.setUp
    run_lane = AlreadyLoadedTests.run_lane
    fresh_output = AlreadyLoadedTests.fresh_output
    run_capturing_pre = AlreadyLoadedTests.run_capturing_pre

    def refused(self, lane, pattern, *, slots=False, **kwargs):
        """Refused with no prompt and no result; /slots is read only where designed."""
        self.fresh_output()
        with lane:
            with self.assertRaisesRegex((fast.ValidationError, adapter.ResponseError), pattern):
                self.run_lane(lane, **kwargs)
            self.assertEqual(lane.posts, [])
            self.assertEqual(("GET", "/slots") in lane.requests, slots)
        self.assertFalse((self.path / "out" / "result.json").exists())

    def accepted(self, lane, **kwargs):
        self.fresh_output()
        with lane:
            record = self.run_lane(lane, **kwargs)
            self.assertEqual(len(lane.posts), 2)
        self.assertEqual(fast.validate_record(record), record)
        return record

    # available_memory_bytes: every source combination, both paths.

    def test_available_memory_with_the_flag_for_every_source_combination(self):
        for name, props_hardware, meta_hardware, expected in source_cases(
                "available_memory_bytes", INVALID_AVAILABLE):
            with self.subTest(name):
                lane = source_lane(props_hardware, meta_hardware)
                if expected is None:
                    record = self.accepted(lane, already_loaded=True)
                    self.assertEqual(record["pre"]["decision"], "UNKNOWN")
                    self.assertIsNone(record["pre"]["inputs"]["available_memory_bytes"])
                    self.assertEqual(record["provenance"]["available_memory_source"], "unknown")
                else:
                    self.refused(lane, REFUSALS.get(expected, "available memory is known"), already_loaded=True)

    def test_available_memory_by_default_for_every_source_combination(self):
        for name, props_hardware, meta_hardware, expected in source_cases(
                "available_memory_bytes", INVALID_AVAILABLE):
            with self.subTest(name):
                lane = source_lane(props_hardware, meta_hardware)
                if expected == 8000:
                    record = self.accepted(lane)
                    self.assertEqual(record["pre"]["decision"], "CHECK")
                    self.assertEqual(record["pre"]["inputs"]["available_memory_bytes"], 8000)
                    self.assertEqual(record["provenance"]["available_memory_source"], "server-reported")
                    self.assertNotIn("fit_gate", record["provenance"])
                    self.assertNotIn(("GET", "/slots"), lane.requests)
                elif expected in REFUSALS:
                    self.refused(lane, REFUSALS[expected])
                else:
                    self.fresh_output()
                    [pre] = self.run_capturing_pre(lane)
                    self.assertEqual((pre["decision"], pre["inputs"]["available_memory_bytes"]),
                                     ("UNKNOWN" if expected is None else "SKIP", expected))
                    self.assertNotIn(("GET", "/slots"), lane.requests)

    def test_codex_round_58_metadata_zero_is_not_hidden_by_props_hardware(self):
        for props_hardware in ({}, {"cpu": "fixture-cpu"}):
            with self.subTest(props_hardware=props_hardware):
                self.refused(source_lane(props_hardware, {"available_memory_bytes": 0}),
                             "available memory is known", already_loaded=True)
                self.fresh_output()
                [pre] = self.run_capturing_pre(source_lane(props_hardware, {"available_memory_bytes": 0}))
                self.assertEqual((pre["decision"], pre["inputs"]["available_memory_bytes"]), ("SKIP", 0))

    # memory_bytes (total): the same combinations, both paths.

    def test_total_memory_for_every_source_combination_on_both_paths(self):
        for name, props_hardware, meta_hardware, expected in source_cases("memory_bytes", INVALID_TOTAL):
            for kwargs in (dict(already_loaded=True), dict(available_memory_bytes=8000)):
                with self.subTest(name, **kwargs):
                    lane = source_lane(props_hardware, meta_hardware)
                    if expected in REFUSALS:
                        self.refused(lane, REFUSALS[expected], **kwargs)
                        continue
                    record = self.accepted(lane, **kwargs)
                    self.assertEqual(record["hardware"]["memory_bytes"], expected)
                    self.assertEqual(record["hardware"]["memory"], expected and {"bytes": expected})

    # Each fact falls back per key, never per hardware object.

    def test_each_hardware_fact_falls_back_per_key(self):
        record = self.accepted(source_lane(
            {"cpu": "fixture-cpu"},
            {"cpu": "other-cpu", "gpu": "fixture-gpu", "memory_bytes": 16000, "available_memory_bytes": 8000}))
        self.assertEqual(
            [record["hardware"][key] for key in ("cpu", "gpu", "memory_bytes")],
            ["fixture-cpu", "fixture-gpu", 16000])
        self.assertEqual(record["pre"]["inputs"]["available_memory_bytes"], 8000)
        # A /props descriptor the allowlist drops stays unknown; metadata never replaces it.
        record = self.accepted(source_lane(
            {"cpu": "/private/fixture", "gpu": None, "available_memory_bytes": 8000},
            {"cpu": "other-cpu", "gpu": "fixture-gpu"}))
        self.assertEqual((record["hardware"]["cpu"], record["hardware"]["gpu"]), (None, "fixture-gpu"))

    def test_one_endpoint_alone_supplies_the_facts(self):
        hardware = {"memory_bytes": 16000, "available_memory_bytes": 8000}
        lane = source_lane(ABSENT, hardware, missing=("/props",))
        record = self.accepted(lane)
        self.assertEqual(record["pre"]["inputs"]["available_memory_bytes"], 8000)
        props = served_props(model={"id": MODEL_ID, "ftype": "Q4_0"}, hardware=hardware)
        record = self.accepted(LoadedLane(props=props, missing=("/v1/models",)))
        self.assertEqual(record["hardware"]["memory_bytes"], 16000)
        for missing, zero in ((("/props",), source_lane(ABSENT, {"available_memory_bytes": 0})),
                              (("/v1/models",), LoadedLane(props=served_props(
                                  model={"id": MODEL_ID, "ftype": "Q4_0"},
                                  hardware={"available_memory_bytes": 0})))):
            with self.subTest(missing=missing):
                zero.missing = missing
                self.refused(zero, "available memory is known", already_loaded=True)

    # Memory outside the recognized hardware facts is never used and never unlocks the mode.

    def stray_lanes(self, value):
        """A figure only where the adapter does not read memory facts."""
        figure = {"available_memory_bytes": value}
        nested = {"hardware": figure}
        meta = {"n_ctx": 32768, "size": 90, "ftype": "Q4_0"}
        entry = dict(id=MODEL_ID, meta=meta)
        other = dict(id="other-model", meta=dict(meta, **nested))
        return {
            "props top level": LoadedLane(props=served_props(**figure)),
            "props hardware, nested deeper": LoadedLane(props=served_props(hardware={"memory": figure})),
            "props hardware list entry": LoadedLane(props=served_props(hardware={"devices": [figure]})),
            "props model object": LoadedLane(props=served_props(model=dict(nested, id=MODEL_ID))),
            "props generation settings": LoadedLane(props=served_props(
                default_generation_settings=dict(figure, n_ctx=32768))),
            "metadata top level": LoadedLane(models=served_models(meta=dict(meta, **figure))),
            "selected entry outside meta": LoadedLane(models={"data": [dict(entry, **nested)]}),
            "models document top level": LoadedLane(models=dict(nested, data=[entry])),
            "only a non-selected entry": LoadedLane(models={"data": [entry, other]}),
            "only a non-selected entry, listed first": LoadedLane(models={"data": [other, entry]}),
        }

    def test_memory_outside_the_recognized_facts_refuses_already_loaded(self):
        for value in (0, 8000, -1, "8000", True, 8000.5, [], {}):
            for name, lane in self.stray_lanes(value).items():
                with self.subTest(name, value=value):
                    self.refused(lane, "outside the recognized hardware facts", already_loaded=True)

    def test_memory_outside_the_recognized_facts_is_never_used_by_default(self):
        for value in (0, 8000, "8000"):
            for name, lane in self.stray_lanes(value).items():
                with self.subTest(name, value=value):
                    self.fresh_output()
                    [pre] = self.run_capturing_pre(lane)
                    self.assertEqual((pre["decision"], pre["inputs"]["available_memory_bytes"]), ("UNKNOWN", None))
                    self.assertNotIn(("GET", "/slots"), lane.requests)
        # The operator's own figure still decides, and is recorded as explicit.
        lane = self.stray_lanes(0)["only a non-selected entry"]
        record = self.accepted(lane, available_memory_bytes=8000)
        self.assertEqual(record["provenance"]["available_memory_source"], "explicit")

    def test_null_memory_outside_the_recognized_facts_is_still_unknown(self):
        for name, lane in self.stray_lanes(None).items():
            if "non-selected" not in name:
                with self.subTest(name):
                    self.accepted(lane, already_loaded=True)

    def test_slots_reporting_available_memory_refuse_after_the_slot_read(self):
        for value in (0, 8000, "8000"):
            for slot in ({"available_memory_bytes": value}, {"hardware": {"available_memory_bytes": value}}):
                with self.subTest(slot=slot):
                    slots = [dict(slot, id=0, n_ctx=32768, is_processing=False)]
                    self.refused(LoadedLane(slots=slots), "outside the recognized hardware facts",
                                 slots=True, already_loaded=True)
        slots = [{"id": 0, "n_ctx": 32768, "is_processing": False, "available_memory_bytes": None}]
        self.accepted(LoadedLane(slots=slots), already_loaded=True)

    # Explicit arguments beside each server source.

    def test_explicit_memory_is_validated_before_any_request(self):
        invalid = [("available_memory_bytes", value) for value in
                   (-1, True, False, "0", "8000", 0.0, 8000.0, NAN, INF, [], {}, 10**400)]
        invalid += [("memory_bytes", value) for value in
                    (0, -1, True, False, "8000", 8000.0, NAN, INF, [], {}, 10**400)]
        for key, value in invalid:
            for loaded in (True, False):
                with self.subTest(key=key, value=value, already_loaded=loaded):
                    lane = LoadedLane()
                    self.refused(lane, "expected a number|number outside", already_loaded=loaded, **{key: value})
                    self.assertEqual(lane.requests, [])

    def test_cli_refuses_invalid_memory_arguments_with_no_request(self):
        for flag in ("--available-memory-bytes", "--memory-bytes"):
            for value in ("-1", "8000.0", "nan", "true", ""):
                with self.subTest(flag=flag, value=value), LoadedLane() as lane, \
                        contextlib.redirect_stderr(io.StringIO()):
                    try:
                        status = adapter.main([
                            "--base-url", lane.url, "--model", MODEL_ID, "--samples", str(self.samples[0]),
                            "--output-dir", str(self.path / "out"), "--model-sha256", MODEL_SHA,
                            "--file-size-bytes", "100", "--already-loaded", flag + "=" + value])
                    except SystemExit as stop:  # argparse refuses a non-integer.
                        status = stop.code
                    self.assertEqual(status, 2)
                    self.assertEqual(lane.requests, [])

    def test_explicit_memory_beside_each_server_source(self):
        for source in ("props", "metadata"):
            def lane(hardware):
                return source_lane(hardware, ABSENT) if source == "props" else source_lane(ABSENT, hardware)
            with self.subTest(source=source):
                # Known from either argument or either source: the mode is refused.
                self.refused(lane({"available_memory_bytes": 0}), "available memory is known",
                             already_loaded=True, available_memory_bytes=8000)
                self.refused(lane({}), "available memory is known", already_loaded=True, available_memory_bytes=0)
                # An invalid server value refuses even beside a valid argument.
                self.refused(lane({"available_memory_bytes": -1}), "invalid server-reported memory",
                             available_memory_bytes=8000)
                # The stated total must match a server total and bound available memory.
                self.refused(lane({"memory_bytes": 16000}), "disagrees with server", memory_bytes=32000,
                             available_memory_bytes=8000)
                self.refused(lane({"memory_bytes": 4000}), "exceeds hardware memory", available_memory_bytes=8000)
                self.refused(lane({"available_memory_bytes": 8000}), "exceeds hardware memory", memory_bytes=4000)
                record = self.accepted(lane({"memory_bytes": 16000}), memory_bytes=16000, already_loaded=True)
                self.assertEqual(record["hardware"]["memory_bytes"], 16000)
                # Documented and unchanged: a stated available memory decides the default path.
                record = self.accepted(lane({"available_memory_bytes": 4000}), available_memory_bytes=8000)
                self.assertEqual(record["pre"]["inputs"]["available_memory_bytes"], 8000)
                self.assertEqual(record["provenance"]["available_memory_source"], "explicit")

    # Saved records and the schema.

    def test_record_naming_a_memory_source_beside_the_gate_is_refused(self):
        record = self.accepted(LoadedLane(), already_loaded=True)
        schema = fast.load(ROOT / "eci_fast_check.schema.json")
        from test_eci_measured_data import schema_errors
        for source in ("server-reported", "explicit", None, 0):
            with self.subTest(source=source):
                forged = copy.deepcopy(record)
                forged["provenance"]["available_memory_source"] = source
                with self.assertRaisesRegex(fast.ValidationError, "available memory is unknown"):
                    fast.validate_record(redigest(forged))
                self.assertNotEqual(schema_errors(forged, schema["$defs"]["record"], schema), [])
        # A receipt without the field keeps validating, as do memory-estimate records.
        kept = copy.deepcopy(record)
        del kept["provenance"]["available_memory_source"]
        kept = redigest(kept)
        self.assertEqual(fast.validate_record(kept), kept)
        self.assertEqual(schema_errors(kept, schema["$defs"]["record"], schema), [])
        gated = self.accepted(LoadedLane(), available_memory_bytes=8000)
        self.assertEqual(schema_errors(gated, schema["$defs"]["record"], schema), [])

    def test_saved_record_memory_fields_keep_zero_apart_from_unknown(self):
        record = self.accepted(source_lane({"memory_bytes": 16000, "available_memory_bytes": 8000}))
        for path, value in ((("pre", "inputs", "available_memory_bytes"), 0),
                            (("pre", "inputs", "available_memory_bytes"), None),
                            (("pre", "inputs", "available_memory_bytes"), -1),
                            (("pre", "inputs", "available_memory_bytes"), True),
                            (("pre", "inputs", "available_memory_bytes"), "8000"),
                            (("pre", "inputs", "available_memory_bytes"), 8000.0),
                            (("pre", "inputs", "available_memory_bytes"), 16001),
                            (("hardware", "memory_bytes"), 0), (("hardware", "memory_bytes"), -1),
                            (("hardware", "memory_bytes"), True), (("hardware", "memory_bytes"), "16000"),
                            (("hardware", "memory_bytes"), 16000.0), (("hardware", "memory_bytes"), 4000)):
            with self.subTest(path=path, value=value):
                forged = copy.deepcopy(record)
                target = forged
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaises(fast.ValidationError):
                    fast.validate_record(redigest(forged))
        for available, decision in ((None, "UNKNOWN"), (0, "SKIP"), (8000, "CHECK")):
            self.assertEqual(fast.pre_check(100, "Q4_0", available)["decision"], decision)
        for value in (-1, True, False, "8000", 8000.0, 0.0, NAN, INF, [], {}, 10**400):
            with self.subTest(pre_check=value), self.assertRaises(fast.ValidationError):
                fast.pre_check(100, "Q4_0", value)

    def test_ingest_rejects_a_forged_gate_and_keeps_the_valid_records(self):
        loaded = self.accepted(LoadedLane(), already_loaded=True)
        gated = self.accepted(source_lane(ABSENT, {"available_memory_bytes": 8000}))
        forged = copy.deepcopy(loaded)
        forged["pre"]["inputs"]["available_memory_bytes"] = 0
        forged = redigest(forged)
        path = self.path / "dataset.json"
        path.write_text(json.dumps({"schema_version": 1, "runs": [], "items": [],
                                    "fast_checks": [loaded, gated, forged]}), encoding="utf-8")
        data = eci_tuning.ingest([path])
        self.assertEqual(data["fast_checks"], [loaded, gated])
        self.assertEqual([entry["record_id"] for entry in data["rejected_fast_checks"]], [forged["record_id"]])


class JobMemoryFactTests(unittest.TestCase):
    """A reviewed job states memory facts only as positive integers and cannot state the mode."""

    def test_job_facts_refuse_zero_and_invalid_memory_and_drop_null(self):
        import eci_jobs
        from test_eci_jobs import request
        policy = eci_jobs.Policy(gate=lambda: True, allowed_targets={request()["target"]},
                                 default_wall_budget_s="60", max_wall_budget_s="600")
        for key in ("available_memory_bytes", "memory_bytes"):
            for value in (0, -1, True, False, "8000", 8000.0, NAN, [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(eci_jobs.Refused) as caught:
                    eci_jobs.normalize_request(request(facts={key: value}), policy)
                self.assertEqual(caught.exception.code, "invalid_request")
            self.assertEqual(eci_jobs.normalize_request(request(facts={key: None}), policy)["facts"], {})
            self.assertEqual(eci_jobs.normalize_request(request(facts={key: 8000}), policy)["facts"], {key: 8000})
        for forged in (request(already_loaded=True), request(facts={"already_loaded": True})):
            with self.assertRaises(eci_jobs.Refused):
                eci_jobs.normalize_request(forged, policy)


# The fields the adapter reads from the saved 2026-11-15 R5 Ornith captures
# (eci-run-R5-ornith-prerun-props.json, -models.json and -slots.json). Raw
# payloads such as chat templates and the model path are left out.
R5_MODEL = "ornith-ai/ornith-1.5-9b"
R5_PROPS = {
    "model_alias": R5_MODEL, "model_ftype": "Q4_K - Medium", "total_slots": 1, "is_sleeping": False,
    "build_info": "b12254-45c8ea837", "default_generation_settings": {"n_ctx": 16384},
}
R5_MODELS = {"object": "list", "data": [{
    "id": R5_MODEL, "aliases": [R5_MODEL], "object": "model", "owned_by": "llamacpp",
    "meta": {"vocab_type": 2, "n_vocab": 248320, "n_ctx": 16384, "n_ctx_train": 262144, "n_embd": 4096,
             "n_params": 9197093888, "size": 5769121792, "ftype": "Q4_K - Medium"},
}]}
R5_SLOTS = [{"id": 0, "n_ctx": 16384, "speculative": False, "is_processing": False}]


class ReplayedCaptureTests(unittest.TestCase):
    """The R5 Ornith capture shape, accepted as captured and refused when asleep."""

    setUp = AlreadyLoadedTests.setUp

    def replay(self, **props):
        lane = LoadedLane(props=dict(copy.deepcopy(R5_PROPS), **props), models=copy.deepcopy(R5_MODELS),
                          slots=copy.deepcopy(R5_SLOTS))
        lane.model = R5_MODEL
        return lane

    def run_r5(self, lane):
        return adapter.run(lane.url, R5_MODEL, self.samples, self.path / "out", model_sha256=MODEL_SHA,
                           file_size_bytes=5769121792, already_loaded=True)

    def test_captured_lane_is_accepted(self):
        with self.replay() as lane:
            record = self.run_r5(lane)
            self.assertEqual(len(lane.posts), 2)
        evidence = record["provenance"]["already_loaded"]
        self.assertEqual((evidence["n_ctx"], evidence["total_slots"], evidence["is_sleeping"]), (16384, 1, False))
        self.assertEqual(fast.validate_record(record), record)

    def test_captured_lane_reported_asleep_sends_no_prompt(self):
        with self.replay(is_sleeping=True) as lane:
            with self.assertRaisesRegex(adapter.ResponseError, "sleeping"):
                self.run_r5(lane)
            self.assertEqual(lane.posts, [])
            self.assertNotIn(("GET", "/slots"), lane.requests)
        self.assertFalse((self.path / "out" / "result.json").exists())

    def test_captured_lane_without_a_context_or_slot_field_is_refused(self):
        for field, pattern in (("total_slots", "slot count"), ("default_generation_settings", "context")):
            with self.subTest(field=field):
                lane = self.replay()
                del lane.props[field]
                with lane:
                    with self.assertRaisesRegex(adapter.ResponseError, pattern):
                        self.run_r5(lane)
                    self.assertEqual(lane.posts, [])


if __name__ == "__main__":
    unittest.main()
