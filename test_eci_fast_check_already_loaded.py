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

    def __init__(self, *, props=None, models=None, slots=None, slots_status=200):
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
                if method == "GET" and path == "/props":
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
