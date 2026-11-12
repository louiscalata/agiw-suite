"""Offline regression tests for checkpoint custody and benchmark callback context."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

import eci_fast_check as f


ROOT = Path(__file__).resolve().parent
MODEL = dict(
    name="checkpoint fixture",
    file_sha256="a" * 64,
    quantization="Q4_K_M",
    file_size_bytes=100,
)
HARDWARE = dict(
    machine_label="fixture",
    cpu="fixture",
    gpu="fixture",
    memory={"bytes": 1000},
    runtime_build_id="fixture",
    memory_bytes=1000,
)
SETTINGS = dict(
    scorer_id="fixture",
    prompt_template_id="fixture",
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


def evidence():
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


def dataset():
    return {
        "arc": {
            "items": [dict(id="shared", answer="A", question="fixture")],
            "revision": None,
        }
    }


def run(data=None, answer=None, **kwargs):
    data = data or dataset()
    model = copy.deepcopy(kwargs.pop("model", MODEL))
    return f.run_check(
        f.make_plan(data),
        data,
        answer or (lambda row: evidence()),
        model=model,
        hardware=copy.deepcopy(HARDWARE),
        runtime_settings=copy.deepcopy(SETTINGS),
        pre=f.pre_check(100, "Q4_K_M", 800),
        clock=lambda: 0,
        created_at="2026-11-12T12:00:00+00:00",
        **kwargs,
    )


class RuntimeRound2Tests(unittest.TestCase):
    def test_second_synchronized_checkpoint_writer_refused_before_adapter(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "checkpoint.jsonl"
            entered, release = threading.Event(), threading.Event()
            failures = []

            def first_adapter(row):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("writer synchronization timed out")
                return evidence()

            def first():
                try:
                    run(answer=first_adapter, checkpoint=path)
                except BaseException as exc:
                    failures.append(exc)

            worker = threading.Thread(target=first)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(
                    f.ValidationError, "checkpoint.*writer|checkpoint.*lock"
                ):
                    run(
                        answer=lambda row: self.fail("second writer called adapter"),
                        checkpoint=path,
                    )
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(path.read_text().splitlines()), 1)
            resumed = run(
                answer=lambda row: self.fail("completed item repeated"), checkpoint=path
            )
            self.assertEqual(resumed["benchmarks"][0]["correct"], 1)

    def test_resume_requires_stable_artifact_identity(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "checkpoint.jsonl"
            unknown = dict(MODEL, file_sha256=None)
            run(checkpoint=path, model=unknown)
            with self.assertRaisesRegex(
                f.ValidationError, "artifact.*hash|file_sha256"
            ):
                run(
                    checkpoint=path,
                    model=unknown,
                    answer=lambda row: self.fail("unverified artifact resumed"),
                )

    def test_valid_looking_checkpoint_measurement_edit_is_refused(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "checkpoint.jsonl"
            run(checkpoint=path)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[0]["observation"]["seconds"] = 9
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            with self.assertRaisesRegex(f.ValidationError, "checksum|integrity|chain"):
                run(
                    checkpoint=path,
                    answer=lambda row: self.fail("corrupt checkpoint used"),
                )

    def test_benchmark_context_reaches_answer_and_score_including_resume(self):
        data = dataset()
        data["gsm8k"] = copy.deepcopy(data["arc"])
        seen = []

        def answer(row, *, benchmark, spec):
            seen.append(("answer", benchmark, spec["benchmark"], row["id"]))
            return evidence()

        def score(prediction, row, *, benchmark, spec):
            seen.append(("score", benchmark, spec["benchmark"], row["id"]))
            return prediction == "A"

        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "checkpoint.jsonl"
            calls = []

            def interrupted(row, *, benchmark, spec):
                if calls:
                    raise KeyboardInterrupt()
                calls.append(benchmark)
                return answer(row, benchmark=benchmark, spec=spec)

            with self.assertRaises(KeyboardInterrupt):
                run(data=data, answer=interrupted, score=score, checkpoint=path)
            result = run(data=data, answer=answer, score=score, checkpoint=path)
            self.assertEqual([b["correct"] for b in result["benchmarks"]], [1, 1])
            self.assertEqual(
                seen,
                [
                    (stage, benchmark, benchmark, "shared")
                    for benchmark in ("arc", "gsm8k")
                    for stage in ("answer", "score")
                ],
            )

    def test_callback_typeerror_is_not_retried_as_legacy_signature(self):
        calls = []

        def broken(row, *, benchmark, spec):
            calls.append(benchmark)
            raise TypeError("inside adapter")

        result = run(answer=broken)
        self.assertEqual(calls, ["arc"])
        self.assertEqual(
            result["benchmarks"][0]["observations"][0]["error"], "TypeError"
        )

    def test_legacy_callback_signature_is_preserved(self):
        result = run(answer=lambda row: evidence(), score=lambda prediction, row: True)
        self.assertEqual(result["benchmarks"][0]["correct"], 1)


if __name__ == "__main__":
    unittest.main()
