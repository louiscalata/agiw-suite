"""In-memory HTTP fixtures: no sockets, ports, model processes or browsers."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import eci_fast_check as fast
import eci_fast_check_adapter as adapter
from remote_machines import PROTECTED_PORTS


ROOT = Path(__file__).resolve().parent
MODEL_ID = "fixture-model"
MODEL_SHA = "a" * 64


def completion(content="A", finish_reason="stop", **message_fields):
    return {
        "model": MODEL_ID,
        "choices": [{
            "message": dict(content=content, **message_fields),
            "finish_reason": finish_reason,
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    }


def props():
    return {
        "model": {
            "id": MODEL_ID,
            "sha256": MODEL_SHA,
            "quantization": "Q4_K_M",
            "file_size_bytes": 100,
        },
        "hardware": {
            "cpu": "fixture-cpu",
            "gpu": "fixture-gpu",
            "memory_bytes": 10000,
            "available_memory_bytes": 8000,
        },
        "build_info": {"version": "fixture-build"},
        "default_generation_settings": {"n_ctx": 32768},
    }


class Stub:
    """Exercise adapter HTTP/JSON handling without opening any network port."""
    def __init__(self, *, properties=None, get_status=200, replies=None,
                 blocked_phase=None, model_meta=None):
        self.properties = props() if properties is None else properties
        self.get_status = get_status
        self.replies = [completion()] if replies is None else replies
        self.blocked_phase = blocked_phase
        self.requests = []
        self.posts = []
        self.model_meta = model_meta
        self.transports = []

    def __enter__(self):
        fixture = self

        class Transport:
            def __init__(self):
                self.closed = threading.Event()
                self.shutdown_called = False

            def shutdown(self, how):
                self.shutdown_called = True
                self.closed.set()

            def settimeout(self, timeout):
                pass

        class Connection:
            def __init__(self, host, port, timeout):
                self.sock = Transport()
                fixture.transports.append(self.sock)
                self.transport = self.sock

            def connect(self):
                pass

            def request(self, method, path, body=None, headers=None):
                fixture.requests.append((method, path))
                self.status = 200
                if method == "GET" and path == "/props":
                    self.status, reply = fixture.get_status, fixture.properties
                elif method == "GET" and path == "/v1/models":
                    selected = {"id": MODEL_ID}
                    if fixture.model_meta is not None:
                        selected["meta"] = fixture.model_meta
                    reply = {"data": [selected]}
                elif method == "POST" and path == "/v1/chat/completions":
                    fixture.posts.append(json.loads(body))
                    reply = fixture.replies[min(len(fixture.posts) - 1, len(fixture.replies) - 1)]
                    if callable(reply):
                        reply = reply()
                    if isinstance(reply, tuple):
                        self.status, reply = reply[:2]
                else:
                    self.status, reply = 404, {"error": "fixture missing route"}
                self.body = io.BytesIO(json.dumps(reply).encode("utf-8"))

            def getresponse(self):
                if fixture.blocked_phase == "headers":
                    # Simulate progress that never completes the headers. Only
                    # the adapter's total-deadline shutdown interrupts it.
                    while not self.transport.closed.wait(0.02):
                        pass
                    raise OSError("fixture transport shut down")
                return self

            def read1(self, size):
                if fixture.blocked_phase == "body":
                    if self.transport.closed.wait(0.02):
                        raise OSError("fixture transport shut down")
                    size = 1
                return self.body.read(size)

            def close(self):
                self.transport.closed.set()

        self.connection_type = Connection
        self.patcher = patch.object(adapter.http.client, "HTTPConnection", Connection)
        self.patcher.start()
        self.url = "http://127.0.0.1:1"  # An inert parser input, never contacted.
        return self

    def __exit__(self, *args):
        self.patcher.stop()


class AdapterTests(unittest.TestCase):
    def tempdir(self):
        return tempfile.TemporaryDirectory(dir=ROOT.parent)

    def sample(self, directory, items=None, benchmark="arc"):
        path = Path(directory) / (benchmark + ".json")
        rows = items or [
            {"id": "one", "question": "Choose the fixture answer.",
             "choices": ["first", "second", "third", "fourth"], "answer": "A"},
            {"id": "two", "question": "Choose another fixture answer.",
             "choices": [{"label": "A", "text": "first"},
                         {"label": "B", "text": "second"},
                         {"label": "C", "text": "third"},
                         {"label": "D", "text": "fourth"}], "answer": "A"},
        ]
        path.write_text(json.dumps({benchmark: {"items": rows}}), encoding="utf-8")
        return path

    def run_fixture(self, stub, sample_files, output, **kwargs):
        return adapter.run(
            stub.url, MODEL_ID, sample_files, output,
            model_sha256=MODEL_SHA, file_size_bytes=100,
            quantization="Q4_K_M", available_memory_bytes=8000, **kwargs
        )

    def test_only_literal_loopback_and_unprotected_ports(self):
        self.assertEqual(adapter.validate_base_url("http://127.0.0.1:1"),
                         ("127.0.0.1", 1, ""))
        self.assertEqual(adapter.validate_base_url("http://127.0.0.1:65535/v1/"),
                         ("127.0.0.1", 65535, "/v1"))
        urls = [
            "http://localhost:9000", "https://127.0.0.1:9000",
            "http://127.0.0.1", "http://127.0.0.1:0",
            "http://127.0.0.1:65536", "http://127.0.0.1:9000/other",
            "http://127.0.0.1:9000/?x=1", "http://127.0.0.1:9000/#x",
            "http://user@127.0.0.1:9000", "http://127.1:9000",
            "http://127.0.0.1:9000/v1/chat/completions",
        ]
        urls.extend("http://127.0.0.1:%d" % port for port in PROTECTED_PORTS)
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                adapter.validate_base_url(url)

    def test_metadata_and_request_preserve_observed_facts(self):
        with Stub() as stub:
            client = adapter.OpenAIAdapter(stub.url + "/v1", MODEL_ID)
            model, hardware = client.discover()
            self.assertEqual(model["name"], MODEL_ID)
            self.assertEqual(model["file_sha256"], MODEL_SHA)
            self.assertEqual(hardware["cpu"], "fixture-cpu")
            self.assertEqual(hardware["gpu"], "fixture-gpu")
            row = {"id": "fixture", "question": "Choose.",
                   "choices": ["first", "second", "third", "fourth"], "answer": "A"}
            evidence = client.answer(row, benchmark="arc", spec={"answer_options": 4})
            self.assertEqual(evidence["outcome"], "correct")
            self.assertEqual(evidence["answer_value"], "A")
            self.assertEqual(evidence["prompt_tokens"], 12)
            self.assertEqual(evidence["completion_tokens"], 7)
            self.assertGreaterEqual(evidence["seconds"], 0)
            self.assertEqual(stub.posts[0]["model"], MODEL_ID)
            self.assertGreaterEqual(stub.posts[0]["max_tokens"], 8192)
            self.assertFalse(stub.posts[0].get("stream", False))
            self.assertTrue(all(method == "GET" or path == "/v1/chat/completions"
                                for method, path in stub.requests))

    def test_reasoning_only_is_truncated_even_with_stop_finish(self):
        for content in ("", "   ", None):
            with self.subTest(content=content), Stub(replies=[
                completion(content, reasoning_content="A is in the private reasoning.")
            ]) as stub:
                client = adapter.OpenAIAdapter(stub.url, MODEL_ID)
                evidence = client.answer(
                    {"id": "one", "question": "Choose.", "answer": "A"},
                    benchmark="arc", spec={"answer_options": 4},
                )
                self.assertEqual(evidence["outcome"], "truncated_empty")
                self.assertTrue(evidence["cut_off"])
                self.assertFalse(evidence["parsed"])
                self.assertIsNone(evidence["answer_value"])
                self.assertEqual(evidence["finish_reason"], "stop")

    def test_cutoff_signals_do_not_score_empty_content_as_wrong(self):
        replies = [completion("", finish_reason=reason)
                   for reason in fast.TOKEN_LIMIT_REASONS]
        limited = completion("")
        limited["sample"] = {"limit": 16384}
        replies.append(limited)
        with Stub(replies=replies) as stub:
            client = adapter.OpenAIAdapter(stub.url, MODEL_ID)
            for _ in replies:
                evidence = client.answer(
                    {"id": "one", "question": "Choose.", "answer": "A"},
                    benchmark="arc", spec={"answer_options": 4},
                )
                self.assertEqual(evidence["outcome"], "truncated_empty")
                self.assertTrue(evidence["cut_off"])

    def test_full_result_validates_and_completed_resume_reuses_items(self):
        with self.tempdir() as directory, Stub() as stub:
            sample = self.sample(directory)
            output = Path(directory) / "out"
            first = self.run_fixture(stub, [sample], output)
            fast.validate_record(first)
            self.assertEqual(first["protocol_id"], fast.PROTOCOL)
            self.assertEqual(len(stub.posts), 2)
            for filename in ("plan.json", "result.json", "checkpoint.jsonl"):
                self.assertTrue((output / filename).is_file(), filename)
            checkpoint = (output / "checkpoint.jsonl").read_text(encoding="utf-8")
            self.assertEqual(len(checkpoint.splitlines()), 2)
            second = self.run_fixture(stub, [sample], output)
            fast.validate_record(second)
            self.assertEqual(len(stub.posts), 2)
            self.assertEqual(first["plan"], second["plan"])
            self.assertEqual(first["benchmarks"][0]["observations"],
                             second["benchmarks"][0]["observations"])

    def test_interrupted_resume_reuses_fsynced_prefix(self):
        with self.tempdir() as directory, Stub() as stub:
            sample = self.sample(directory)
            output = Path(directory) / "out"
            append = fast.checkpoint_append

            def interrupt_after_persist(*args):
                append(*args)
                raise KeyboardInterrupt()

            with patch.object(fast, "checkpoint_append", interrupt_after_persist):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_fixture(stub, [sample], output)
            self.assertEqual(len(stub.posts), 1)
            result = self.run_fixture(stub, [sample], output)
            fast.validate_record(result)
            self.assertEqual(len(stub.posts), 2)
            self.assertEqual(len((output / "checkpoint.jsonl").read_text().splitlines()), 2)

    def test_models_only_fallback_keeps_unknown_hardware_null(self):
        with Stub(properties={}, get_status=404) as stub:
            model, hardware = adapter.OpenAIAdapter(stub.url, MODEL_ID).discover()
            self.assertEqual(model["name"], MODEL_ID)
            self.assertIsNone(model["file_sha256"])
            for field in ("cpu", "gpu", "memory", "runtime_build_id"):
                self.assertIsNone(hardware[field], field)

    def test_models_metadata_can_supply_model_hardware_and_server_facts(self):
        metadata = props()
        metadata.update(metadata.pop("model"))
        with self.tempdir() as directory, Stub(properties={}, get_status=404,
                                               model_meta=metadata) as stub:
            result = adapter.run(stub.url, MODEL_ID, [self.sample(directory)],
                                 Path(directory) / "out")
            fast.validate_record(result)
            self.assertEqual(result["model"]["file_sha256"], MODEL_SHA)
            self.assertEqual(result["hardware"]["cpu"], "fixture-cpu")
            self.assertEqual(result["runtime_settings"]["ctx"], 32768)
            self.assertEqual(result["runtime_settings"]["backend_version"], "fixture-build")
            self.assertEqual(len(stub.posts), 2)

    def test_http_and_malformed_completion_are_error_observations(self):
        replies = [(503, {"error": "fixture unavailable"}), {"choices": []}]
        with self.tempdir() as directory, Stub(replies=replies) as stub:
            result = self.run_fixture(stub, [self.sample(directory)], Path(directory) / "out")
            fast.validate_record(result)
            observations = result["benchmarks"][0]["observations"]
            self.assertEqual([o["outcome"] for o in observations], ["error", "error"])
            self.assertTrue(all(o["error"] for o in observations))
            self.assertEqual(len(stub.posts), 2)

    def test_mismatched_model_is_never_scored(self):
        reply = completion("A")
        reply["model"] = "another-fixture-model"
        with self.tempdir() as directory, Stub(replies=[reply]) as stub:
            result = self.run_fixture(stub, [self.sample(directory)], Path(directory) / "out")
            self.assertTrue(all(o["outcome"] == "error"
                                for o in result["benchmarks"][0]["observations"]))

    def test_timeout_is_checkpointed_and_resume_does_not_repeat_it(self):
        def delayed_reply():
            threading.Event().wait(0.1)
            return completion()

        with self.tempdir() as directory, Stub(replies=[delayed_reply]) as stub:
            sample = self.sample(directory, items=[{
                "id": "one", "question": "Choose.", "answer": "A",
            }])
            output = Path(directory) / "out"
            first = self.run_fixture(stub, [sample], output, timeout=0.02)
            observation = first["benchmarks"][0]["observations"][0]
            self.assertEqual(observation["outcome"], "error")
            self.assertEqual(observation["error"], "TimeoutError")
            self.assertGreaterEqual(observation["seconds"], 0)
            second = self.run_fixture(stub, [sample], output, timeout=0.02)
            self.assertEqual(len(stub.posts), 1)
            self.assertEqual(first["benchmarks"][0]["observations"],
                             second["benchmarks"][0]["observations"])

    def test_redirect_is_not_followed(self):
        with self.tempdir() as directory, Stub() as destination:
            reply = (302, {}, {"Location": destination.url + "/v1/chat/completions"})
            with Stub(replies=[reply]) as stub:
                result = self.run_fixture(stub, [self.sample(directory)], Path(directory) / "out")
                self.assertTrue(all(o["outcome"] == "error"
                                    for o in result["benchmarks"][0]["observations"]))
            self.assertEqual(destination.requests, [])

    def test_resume_refuses_changed_generation_budget_before_inference(self):
        with self.tempdir() as directory, Stub() as stub:
            sample = self.sample(directory)
            output = Path(directory) / "out"
            self.run_fixture(stub, [sample], output)
            posts = len(stub.posts)
            with self.assertRaises(fast.ValidationError):
                self.run_fixture(stub, [sample], output, max_tokens=8192)
            self.assertEqual(len(stub.posts), posts)

    def test_missing_usage_remains_unknown(self):
        reply = completion("Answer: A")
        del reply["usage"]
        with Stub(replies=[reply]) as stub:
            evidence = adapter.OpenAIAdapter(stub.url, MODEL_ID).answer(
                {"id": "one", "question": "Choose.", "answer": "A",
                 "choices": ["first", "second", "third", "fourth"]},
                benchmark="arc", spec={"answer_options": 4},
            )
            self.assertEqual(evidence["outcome"], "correct")
            self.assertIsNone(evidence["prompt_tokens"])
            self.assertIsNone(evidence["completion_tokens"])

    def test_multiple_sample_files_and_numeric_answer(self):
        with self.tempdir() as directory, Stub(replies=[
            completion("(A)"), completion("#### 42")
        ]) as stub:
            arc = self.sample(directory, items=[{
                "id": "arc-one", "question": "Choose.", "answer": "A",
                "choices": ["one", "two", "three", "four"],
            }])
            gsm = self.sample(directory, items=[{
                "id": "gsm-one", "question": "Calculate the fixture value.", "answer": "42",
            }], benchmark="gsm8k")
            result = self.run_fixture(stub, [arc, gsm], Path(directory) / "out")
            self.assertEqual([b["benchmark"] for b in result["benchmarks"]], ["arc", "gsm8k"])
            self.assertEqual([b["observations"][0]["outcome"] for b in result["benchmarks"]],
                             ["correct", "correct"])
            fast.validate_record(result)

    def test_cli_entry_writes_valid_result(self):
        with self.tempdir() as directory, Stub() as stub:
            sample = self.sample(directory)
            output = Path(directory) / "out"
            with contextlib.redirect_stdout(__import__("io").StringIO()):
                code = adapter.main([
                    "--base-url", stub.url, "--model", MODEL_ID,
                    "--samples", str(sample), "--output-dir", str(output),
                    "--model-sha256", MODEL_SHA, "--file-size-bytes", "100",
                    "--quantization", "Q4_K_M", "--available-memory-bytes", "8000",
                ])
            self.assertEqual(code, 0)
            fast.validate_record(fast.load(output / "result.json"))

    def test_choice_parentheses_must_be_balanced(self):
        row = {"id": "one", "question": "Choose.", "answer": "A",
               "choices": ["first", "second", "third", "fourth"]}
        for text in ("A", "Answer: A", "(A)", "Final answer: (A)"):
            with self.subTest(text=text):
                self.assertEqual(adapter.extract_answer(text, row, "arc"), "A")
        for text in ("(A", "A)", "Answer: (A", "Final answer: A)"):
            with self.subTest(text=text):
                self.assertIsNone(adapter.extract_answer(text, row, "arc"))

    def test_long_numeric_answers_are_not_rounded_into_equal_scores(self):
        expected = "1234567890123456789012345678901"
        different = "1234567890123456789012345678902"
        row = {"id": "one", "question": "Calculate.", "answer": expected}
        with Stub(replies=[completion("Final answer: " + different),
                           completion("#### " + expected)]) as stub:
            client = adapter.OpenAIAdapter(stub.url, MODEL_ID)
            wrong = client.answer(row, benchmark="gsm8k", spec={})
            correct = client.answer(row, benchmark="gsm8k", spec={})
            self.assertEqual(wrong["outcome"], "wrong")
            self.assertEqual(wrong["answer_value"], different)
            self.assertEqual(correct["outcome"], "correct")
            self.assertEqual(correct["answer_value"], expected)

    def test_missing_artifact_or_fit_facts_refuse_before_inference(self):
        for missing in ("file_sha256", "fit"):
            with self.subTest(missing=missing), self.tempdir() as directory:
                supplied = {"file_size_bytes": 100, "quantization": "Q4_K_M",
                            "available_memory_bytes": 8000}
                if missing == "fit":
                    supplied = {"model_sha256": MODEL_SHA}
                with Stub(properties={}, get_status=404) as stub:
                    with self.assertRaises(fast.ValidationError):
                        adapter.run(stub.url, MODEL_ID, [self.sample(directory)],
                                    Path(directory) / "out", **supplied)
                    self.assertEqual(stub.posts, [])

    def test_conflicting_explicit_model_facts_refuse_before_inference(self):
        variants = [
            {"model_sha256": "b" * 64},
            {"file_size_bytes": 101},
            {"quantization": "Q8_0"},
        ]
        for supplied in variants:
            with self.subTest(supplied=supplied), self.tempdir() as directory, Stub() as stub:
                with self.assertRaises(fast.ValidationError):
                    adapter.run(stub.url, MODEL_ID, [self.sample(directory)],
                                Path(directory) / "out", **supplied)
                self.assertEqual(stub.posts, [])

    def test_output_lock_refuses_second_writer_before_inference(self):
        with self.tempdir() as directory, Stub() as stub:
            output = Path(directory) / "out"
            output.mkdir()
            sample = self.sample(directory)
            with adapter.output_lock(output):
                with self.assertRaisesRegex(fast.ValidationError, "lock|writer"):
                    self.run_fixture(stub, [sample], output)
            self.assertEqual(stub.posts, [])
            self.assertFalse((output / "plan.json").exists())
            self.assertFalse((output / "result.json").exists())

    def test_raw_props_paths_and_machine_names_are_not_retained(self):
        properties = props()
        properties.update({"model_path": "/fixture/private-model.gguf",
                           "command_line": "fixture private launch arguments"})
        properties["hardware"].update({
            "machine_label": "fixture-hostname-value",
            "cpu": "hostname fixture", "gpu": "serial fixture",
        })
        with self.tempdir() as directory, Stub(properties=properties) as stub:
            result = self.run_fixture(stub, [self.sample(directory)], Path(directory) / "out")
            encoded = json.dumps(result)
            for raw in ("/fixture/private-model.gguf", "fixture private launch arguments",
                        "fixture-hostname-value", "hostname fixture", "serial fixture"):
                self.assertNotIn(raw, encoded)
            self.assertIsNone(result["hardware"]["machine_label"])
            self.assertIsNone(result["hardware"]["cpu"])
            self.assertIsNone(result["hardware"]["gpu"])

    def check_transport_deadline(self, phase):
        with Stub(blocked_phase=phase) as stub:
            client = adapter.OpenAIAdapter(stub.url, MODEL_ID, timeout=0.12)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                client.answer({"id": "one", "question": "Choose.", "answer": "A"},
                              benchmark="arc", spec={"answer_options": 4})
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.08)
            self.assertLess(elapsed, 1)
            self.assertTrue(stub.transports[-1].shutdown_called)

    def test_total_deadline_interrupts_slow_drip_headers(self):
        self.check_transport_deadline("headers")

    def test_total_deadline_interrupts_slow_drip_body(self):
        self.check_transport_deadline("body")

    def test_deadline_shuts_down_before_timer_callback(self):
        # A loaded machine can run the request thread past the deadline before
        # dispatching the timer. Advance a controlled clock at each boundary.
        for phase in ("connect", "getresponse", "read1"):
            for detached in (False, True):
                with self.subTest(phase=phase, detached=detached), Stub() as stub:
                    clock = [0.0]
                    connection_type = stub.connection_type
                    getresponse = connection_type.getresponse
                    close = connection_type.close
                    shutdown_at_close = []

                    def response(connection):
                        value = getresponse(connection)
                        if detached:
                            connection.sock = None
                        return value

                    def cleanup(connection):
                        shutdown_at_close.append(connection.transport.shutdown_called)
                        close(connection)

                    with patch.object(connection_type, "getresponse", response):
                        operation = getattr(connection_type, phase)

                        def reach_deadline(connection, *args, **kwargs):
                            value = operation(connection, *args, **kwargs)
                            clock[0] = 2.0
                            return value

                        with patch.object(connection_type, phase, reach_deadline), \
                                patch.object(connection_type, "close", cleanup), \
                                patch.object(adapter.time, "monotonic", side_effect=lambda: clock[0]), \
                                patch.object(adapter.threading, "Timer"):
                            client = adapter.OpenAIAdapter(stub.url, MODEL_ID, timeout=1)
                            with self.assertRaises(TimeoutError):
                                client.request("GET", "/props")
                    self.assertTrue(stub.transports[-1].shutdown_called)
                    self.assertEqual(shutdown_at_close, [True])

    def test_socket_timeouts_shut_down_before_cleanup(self):
        # Python 3.9 socket.timeout is distinct from TimeoutError. Connect can
        # fail before the adapter has captured the connection's transport.
        for phase in ("connect", "request", "getresponse", "read1"):
            with self.subTest(phase=phase), Stub() as stub:
                connection_type = stub.connection_type
                getresponse = connection_type.getresponse
                close = connection_type.close
                shutdown_at_close = []

                def detached_response(connection):
                    value = getresponse(connection)
                    connection.sock = None
                    return value

                def cleanup(connection):
                    shutdown_at_close.append(connection.transport.shutdown_called)
                    close(connection)

                def socket_timeout(*args, **kwargs):
                    raise adapter.socket.timeout("fixture inactivity timeout")

                with patch.object(connection_type, "getresponse", detached_response), \
                        patch.object(connection_type, phase, socket_timeout), \
                        patch.object(connection_type, "close", cleanup), \
                        patch.object(adapter.threading, "Timer"):
                    client = adapter.OpenAIAdapter(stub.url, MODEL_ID, timeout=1)
                    with self.assertRaises(TimeoutError):
                        client.request("GET", "/props")
                self.assertTrue(stub.transports[-1].shutdown_called)
                self.assertEqual(shutdown_at_close, [True])

    def test_resume_refuses_changed_explicit_available_memory(self):
        with self.tempdir() as directory, Stub() as stub:
            sample = self.sample(directory)
            output = Path(directory) / "out"
            self.run_fixture(stub, [sample], output)
            posts = len(stub.posts)
            with self.assertRaises(fast.ValidationError):
                adapter.run(stub.url, MODEL_ID, [sample], output,
                            model_sha256=MODEL_SHA, file_size_bytes=100,
                            quantization="Q4_K_M", available_memory_bytes=7000)
            self.assertEqual(len(stub.posts), posts)


if __name__ == "__main__":
    unittest.main()
