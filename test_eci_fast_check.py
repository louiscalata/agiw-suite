"""Offline synthetic tests only: no harness imports, models, network or browsers."""

import contextlib
import copy
import io
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import eci_fast_check as f
import eci_tuning as e

ROOT = Path(__file__).resolve().parent
MODEL = dict(
    name="synthetic candidate",
    file_sha256="a" * 64,
    quantization="Q4_K_M",
    file_size_bytes=100,
)
HARDWARE = dict(
    machine_label="fixture-machine-a",
    cpu="fixture-cpu",
    gpu="fixture-gpu",
    memory={"bytes": 1000},
    runtime_build_id="fixture-build",
    memory_bytes=1000,
)
SETTINGS = dict(
    scorer_id="parsed_exact_match-v1",
    prompt_template_id="fixture-v1",
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


def datasets(n=200):
    return {
        "arc": {
            "items": [
                dict(id=str(i), answer="A", question="fixture") for i in range(n)
            ],
            "revision": None,
        }
    }


def rehash(record):
    record["record_id"] = f.digest(
        {k: v for k, v in record.items() if k != "record_id"}
    )
    return record


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


def run(data=None, plan=None, answer=None, **kwargs):
    data = data or datasets()
    plan = plan or f.make_plan(data)
    model = kwargs.pop("model", copy.deepcopy(MODEL))
    adapter = answer or (lambda item: ("A", 4, 2))

    def classified(item):
        supplied = adapter(item)
        if not isinstance(supplied, tuple):
            return supplied
        prediction, tokens, seconds = supplied
        if "answer" not in item:
            raise ValueError("fixture reference answer absent")
        outcome = (
            "unparsed"
            if prediction is None
            else "correct"
            if prediction == item["answer"]
            else "wrong"
        )
        return dict(
            observation(
                outcome,
                completion_tokens=tokens,
                seconds=seconds,
                answer_value=prediction,
            ),
            prediction=prediction,
        )

    return f.run_check(
        plan,
        data,
        classified,
        model=model,
        hardware=kwargs.pop("hardware", copy.deepcopy(HARDWARE)),
        runtime_settings=kwargs.pop("runtime_settings", copy.deepcopy(SETTINGS)),
        pre=kwargs.pop(
            "pre", f.pre_check(model["file_size_bytes"], model["quantization"], 800)
        ),
        clock=kwargs.pop("clock", FakeClock()),
        created_at=kwargs.pop("created_at", STAMP),
        **kwargs,
    )


class PlanTests(unittest.TestCase):
    def test_default_same_ids_across_input_order_and_models(self):
        data = datasets(600)
        plan = f.make_plan(data)
        data["arc"]["items"].reverse()
        self.assertEqual(f.make_plan(data), plan)
        self.assertEqual(len(plan["benchmarks"][0]["item_ids"]), 200)
        self.assertEqual(
            plan["benchmarks"][0]["seed"], f.digest(["arc", f.PLAN_VERSION])
        )
        self.assertNotEqual(
            f.make_plan(data, version="next")["plan_id"], plan["plan_id"]
        )
        self.assertNotEqual(f.make_plan(data, count=199)["plan_id"], plan["plan_id"])
        self.assertEqual(plan["benchmarks"][0]["dataset_revision"], None)
        self.assertEqual(len(plan["benchmarks"][0]["dataset_sha256"]), 64)

    def test_strata_counts_are_descriptive_with_no_minimum(self):
        data = datasets(100)
        for i, row in enumerate(data["arc"]["items"]):
            row["subject"] = "rare" if i == 0 else "common"
        spec = f.make_plan(data, count=10)["benchmarks"][0]
        self.assertEqual(spec["stratum_key"], "subject")
        self.assertEqual(sum(s["selected"] for s in spec["strata"]), 10)
        self.assertEqual(
            len(f.make_plan(data, count=1)["benchmarks"][0]["item_ids"]), 1
        )

    def test_task_strata_missing_label_and_small_dataset(self):
        data = datasets(3)
        data["arc"]["items"][0]["task"] = "one"
        plan = f.make_plan(data)
        self.assertEqual(plan["benchmarks"][0]["stratum_key"], "task")
        self.assertEqual(len(plan["benchmarks"][0]["item_ids"]), 3)
        self.assertEqual(len(plan["benchmarks"][0]["strata"]), 2)

    def test_bad_input_and_unknown_chance(self):
        for count in (0, True, 1.5):
            with self.subTest(count=count), self.assertRaises(f.ValidationError):
                f.make_plan(datasets(), count=count)
        data = datasets(2)
        data["arc"]["items"][1]["id"] = "0"
        with self.assertRaises(f.ValidationError):
            f.make_plan(data)
        for chance in (1, float("nan"), True, 0.5):
            with self.subTest(chance=chance), self.assertRaises(f.ValidationError):
                f.make_plan({"arc": dict(datasets(2)["arc"], chance=chance)})
        other = {"new-benchmark": datasets(2)["arc"]}
        self.assertIsNone(f.make_plan(other)["benchmarks"][0]["chance"])

    def test_plan_hash_and_seed_tampering(self):
        plan = f.make_plan(datasets())
        plan["benchmarks"][0]["seed"] = "0" * 64
        plan["plan_id"] = f.digest({k: v for k, v in plan.items() if k != "plan_id"})
        with self.assertRaises(f.ValidationError):
            f.validate_plan(plan)


class StatisticsTests(unittest.TestCase):
    def test_wilson_known_values_and_extremes(self):
        middle = f.statistics(100, 200, 0.25)
        self.assertAlmostEqual(middle["interval95"][0], 0.4313608596038919)
        self.assertAlmostEqual(middle["interval95"][1], 0.5686391403961081)
        for n in (1, 2, 200, 10000):
            for correct in (0, n):
                result = f.statistics(correct, n, 0.5)
                lo, hi = result["interval95"]
                self.assertTrue(0 <= lo <= result["accuracy"] <= hi <= 1)
                self.assertEqual(lo if correct == 0 else hi, 0 if correct == 0 else 1)
                self.assertTrue(
                    all(math.isfinite(v) for v in result["normalized_interval95"])
                )
        self.assertTrue(all(v is None for v in f.statistics(0, 0, 0.25).values()))

    def test_admit_formula_decimal_boundaries_and_interval_transform(self):
        for raw, chance, expected in (
            (0.325, 0.25, 0.1),
            (0.925, 0.25, 0.9),
            (0.55, 0.5, 0.1),
            (0.95, 0.5, 0.9),
            (0.1, 0, 0.1),
            (0, 0.5, 0),
            (1, 0.5, 1),
        ):
            self.assertEqual(f.normalized(raw, chance), expected)
        result = f.statistics(55, 100, 0.5)
        self.assertEqual(
            result["normalized_interval95"],
            [f.normalized(v, 0.5) for v in result["interval95"]],
        )
        self.assertIsNone(f.normalized(0.5, None))
        for args in ((True, 1, 0), (2, 1, 0), (0, -1, 0)):
            with self.assertRaises(f.ValidationError):
                f.statistics(*args)

    def test_exact_mcnemar_known_p_and_directions(self):
        a = {str(i): True for i in range(6)}
        b = {str(i): False for i in range(6)}
        test = f.mcnemar(a, b)
        self.assertEqual(test["p_exact"], ["1", "32"])
        self.assertEqual(test["p_value"], 0.03125)
        self.assertEqual(test["verdict"], "BETTER")
        self.assertEqual(f.mcnemar(b, a)["verdict"], "WORSE")
        self.assertEqual(f.mcnemar(a, b, alpha="0.03")["verdict"], "INCONCLUSIVE")
        self.assertEqual(f.mcnemar(a, b, alpha="0.03125")["verdict"], "INCONCLUSIVE")
        self.assertEqual(test["n_discordant"], 6)

    def test_no_discordance_tiny_n_missing_pairs_and_large_exact_test(self):
        self.assertEqual(f.mcnemar({}, {})["p_value"], 1)
        self.assertEqual(f.mcnemar({"x": True}, {"x": True})["verdict"], "INCONCLUSIVE")
        self.assertEqual(f.mcnemar({"x": True}, {"x": False})["p_value"], 1)
        test = f.mcnemar({"x": None, "y": False, "z": True}, {"x": False, "y": None})
        self.assertEqual(test["n_shared"], 0)
        for b, c, exact in (
            (5, 0, ["1", "16"]),
            (8, 2, ["7", "64"]),
            (9, 1, ["11", "512"]),
            (3, 3, ["1", "1"]),
        ):
            a = {str(i): i < b for i in range(b + c)}
            result = f.mcnemar(a, {k: not v for k, v in a.items()})
            self.assertEqual(result["p_exact"], exact)
        result = f.mcnemar(
            {str(i): True for i in range(2000)}, {str(i): False for i in range(2000)}
        )
        self.assertEqual(result["verdict"], "BETTER")
        self.assertEqual(result["p_exact"][0], "1")


class DecisionTests(unittest.TestCase):
    def test_pre_fit_boundary_unknown_and_no_advisory_relaxation(self):
        self.assertEqual(f.pre_check(75, "Q4", 100)["decision"], "CHECK")
        self.assertEqual(f.pre_check(76, "Q4", 100)["decision"], "SKIP")
        self.assertEqual(f.pre_check(1, "Q4", 0)["decision"], "SKIP")
        for size, quant, memory in (
            (None, "Q4", 100),
            (50, None, 100),
            (50, "Q4", None),
        ):
            self.assertEqual(f.pre_check(size, quant, memory)["decision"], "UNKNOWN")
        for local, size in (("SKIP", 80), ("CHECK", 50)):
            for advisory in ("CHECK", "UNKNOWN", "SKIP"):
                result = f.pre_check(
                    size,
                    "Q4",
                    100,
                    advisory=dict(decision=advisory, reasons=["fixture"]),
                )
                self.assertGreaterEqual(
                    ["CHECK", "UNKNOWN", "SKIP"].index(result["decision"]),
                    ["CHECK", "UNKNOWN", "SKIP"].index(local),
                )

    def test_post_thresholds_unknown_and_advisory_order(self):
        evidence = dict(
            planned=200,
            attempted=200,
            correct=180,
            parsed=200,
            errors=0,
            truncations=0,
            chance="0.25",
            answer_options=4,
            top_answer_count=1,
            unknown_finish=0,
        )
        self.assertEqual(f.post_check([evidence])["decision"], "ADMIT")
        self.assertEqual(
            f.post_check([dict(evidence, attempted=199, parsed=199)])["decision"],
            "REJECT",
        )
        self.assertEqual(
            f.post_check([evidence], max_interval_width="0.001")["decision"], "REJECT"
        )
        self.assertEqual(f.post_check([dict(evidence, errors=3)])["decision"], "REJECT")
        self.assertEqual(f.post_check([])["decision"], "UNKNOWN")
        for errors, local in ((0, "ADMIT"), (3, "REJECT")):
            for advice in ("ADMIT", "UNKNOWN", "REJECT"):
                result = f.post_check(
                    [dict(evidence, errors=errors)],
                    advisory=dict(decision=advice, reasons=[]),
                )
                self.assertGreaterEqual(
                    ["ADMIT", "UNKNOWN", "REJECT"].index(result["decision"]),
                    ["ADMIT", "UNKNOWN", "REJECT"].index(local),
                )
        with self.assertRaises(f.ValidationError):
            f.post_check([{}])


class RunnerTests(unittest.TestCase):
    def test_fake_callable_record_counts_statistics_and_journal(self):
        record = run(journal="synthetic-nisi-journal")
        bench = record["benchmarks"][0]
        self.assertEqual(
            (bench["planned"], bench["completed"], bench["correct"]), (200, 200, 200)
        )
        self.assertEqual(bench["tokens"], 800)
        self.assertEqual(bench["tokens_per_s"], 2)
        self.assertEqual(record["pre"]["decision"], "CHECK")
        self.assertEqual(record["post"]["decision"], "ADMIT")
        self.assertEqual(record["journal"], "synthetic-nisi-journal")
        self.assertIsNone(record["comparison"]["arc"]["n_shared"])
        self.assertEqual(record["model"]["file_sha256"], "a" * 64)
        self.assertEqual(f.validate_record(record), record)

    def test_error_timeout_and_invalid_measurement_remain_unknown(self):
        calls = []

        def answer(item):
            calls.append(item["id"])
            if len(calls) == 1:
                raise TimeoutError("PRIVATE SENTINEL")
            if len(calls) == 2:
                raise RuntimeError("PRIVATE SENTINEL")
            if len(calls) == 3:
                raise RuntimeError("PRIVATE SENTINEL")
            return "B", None, None

        record = run(answer=answer)
        bench = record["benchmarks"][0]
        self.assertEqual(
            (bench["completed"], bench["errors"], bench["timeouts"], bench["correct"]),
            (197, 3, 1, 0),
        )
        self.assertIsNone(bench["tokens_per_s"])
        self.assertIsNone(bench["tokens"])
        self.assertIsNone(bench["answer_seconds"])
        self.assertEqual(record["post"]["decision"], "REJECT")
        self.assertNotIn("PRIVATE SENTINEL", json.dumps(record))

    def test_cooperative_budget_stops_before_next_call_across_benchmarks(self):
        data = datasets(5)
        data["winogrande"] = copy.deepcopy(data["arc"])
        plan = f.make_plan(data, wall_budget_s="3")
        clock = FakeClock()

        def answer(item):
            clock.now += 2
            return "A", 4, 2

        record = run(data, plan, answer, clock=clock)
        first, second = record["benchmarks"]
        self.assertEqual((first["completed"], second["completed"]), (2, 0))
        self.assertEqual(first["stop_reason"], "budget")
        self.assertEqual(second["stop_reason"], "budget")
        self.assertEqual(record["wall_time_s"], 4)
        self.assertEqual(record["post"]["decision"], "REJECT")
        self.assertEqual(second["accuracy"], 0)

    def test_unknown_or_skip_pre_never_calls_answer(self):
        def forbidden(item):
            self.fail("answer must not be called")

        for pre in (f.pre_check(100, "Q4_K_M", None), f.pre_check(100, "Q4_K_M", 1)):
            record = run(answer=forbidden, pre=pre)
            self.assertEqual(record["benchmarks"][0]["stop_reason"], "pre")
            self.assertEqual(record["benchmarks"][0]["completed"], 0)

    def test_dataset_drift_and_baseline_conflict_fail_before_calls(self):
        data = datasets()
        plan = f.make_plan(data)
        data["arc"]["items"][0]["question"] = "changed"
        with self.assertRaises(f.ValidationError):
            run(data, plan, lambda item: self.fail("must not call"))
        baseline = run()
        with self.assertRaises(f.ValidationError):
            run(
                baseline=baseline,
                hardware=dict(HARDWARE, machine_label="fixture-machine-b"),
                answer=lambda item: self.fail("must not call"),
            )

    def test_paired_baseline_and_custom_parsed_scorer(self):
        baseline = run(
            answer=lambda item: ("B", 1, 1), model=dict(MODEL, name="baseline")
        )
        candidate = run(
            baseline=baseline,
            score=lambda prediction, item: prediction.lower() == item["answer"].lower(),
        )
        comparison = candidate["comparison"]["arc"]
        self.assertEqual(comparison["verdict"], "BETTER")
        self.assertEqual(comparison["model_only"], 200)
        self.assertEqual(comparison["baseline_only"], 0)
        self.assertEqual(candidate["baseline"]["record_id"], baseline["record_id"])

    def test_missing_prediction_gold_and_boolean_scorer(self):
        record = run(datasets(1), answer=lambda item: (None, 1, 1))
        self.assertEqual(record["benchmarks"][0]["completed"], 0)
        self.assertEqual(record["benchmarks"][0]["errors"], 0)
        with self.assertRaises(f.ValidationError):
            run(datasets(1), answer=lambda item: ("A", 1, 1), score=lambda p, i: 1)
        with self.assertRaises(f.ValidationError):
            run(datasets(1), answer=lambda item: ("A", float("nan"), 0))
        data = datasets(1)
        del data["arc"]["items"][0]["answer"]
        self.assertEqual(run(data)["benchmarks"][0]["completed"], 0)

    def test_bad_post_options_refuse_before_answer(self):
        calls = []
        with self.assertRaises(f.ValidationError):
            run(
                answer=lambda item: calls.append(item), post_options={"min_coverage": 2}
            )
        self.assertEqual(calls, [])

    def test_rehashed_false_claims_and_wrong_types_are_rejected(self):
        original = run()

        def mutations(record):
            yield dict(record, eci_posterior_mean=200)
            yield dict(record, journal=0)
            yield dict(record, created_at="2026-11-12T12:00:00")
            r = copy.deepcopy(record)
            r["benchmarks"][0]["completed"] = 0
            yield r
            r = copy.deepcopy(record)
            r["benchmarks"][0]["accuracy"] = 0
            yield r
            r = copy.deepcopy(record)
            r["benchmarks"][0]["normalized_score"] = 0.5
            yield r
            r = copy.deepcopy(record)
            r["benchmarks"][0]["observations"][0]["correct"] = 1
            yield r
            r = copy.deepcopy(record)
            r["benchmarks"][0]["observations"][0]["item_id"] = "missing"
            yield r
            r = copy.deepcopy(record)
            r["pre"]["decision"] = "SKIP"
            yield r
            r = copy.deepcopy(record)
            r["post"]["decision"] = "REJECT"
            yield r
            r = copy.deepcopy(record)
            r["comparison"]["arc"]["verdict"] = "BETTER"
            yield r
            r = copy.deepcopy(record)
            del r["post"]
            yield r

        for index, bad in enumerate(mutations(original)):
            with self.subTest(index=index), self.assertRaises(f.ValidationError):
                f.validate_record(rehash(bad))
        bad = copy.deepcopy(original)
        bad["benchmarks"][0]["wall_time_s"] = float("nan")
        with self.assertRaises(ValueError):
            f.validate_record(bad)

    def test_boolean_cannot_impersonate_a_paired_integer_count(self):
        data = datasets(1)
        record = run(data, baseline=run(data, model=dict(MODEL, name="baseline")))
        self.assertEqual(record["comparison"]["arc"]["n_shared"], 1)
        record["comparison"]["arc"]["n_shared"] = True
        with self.assertRaises(f.ValidationError):
            f.validate_record(rehash(record))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        override = patch.dict(
            os.environ, {"AGIW_ECI_TUNING_CONFIG": str(self.path / "policy.json")}
        )
        override.start()
        self.addCleanup(override.stop)

    def file(self, name, value):
        p = self.path / name
        p.write_text(json.dumps(value), encoding="utf-8")
        return p

    def test_ingest_round_trip_separation_no_policy_promotion_and_status(self):
        path = self.file(
            "fast.json", dict(schema_version=1, runs=[], items=[], fast_checks=[run()])
        )
        data = e.ingest([path])
        self.assertEqual(data["runs"], [])
        self.assertEqual(data["items"], [])
        self.assertEqual(e.ingest([self.file("normalized.json", data)]), data)
        for group in e.CLASSES:
            self.assertEqual(e.rank(data)[group]["eci"], [])
            self.assertEqual(e.rank(data)[group]["components"], {})
        self.assertIsNone(e.recommend(data)["routes"]["general"]["model"])
        self.assertEqual(
            len(e.status(data)["rankings"]["fast_check"]["hardware_views"]), 1
        )
        with self.assertRaises(e.ValidationError):
            e.apply_policy(data, self.path / "policy.json", write=True)
        with self.assertRaises(e.ValidationError):
            e.normalize_run(
                dict(
                    run_id="bad", model="bad", protocol_id=f.PROTOCOL, status="COMPUTED"
                )
            )
        with self.assertRaises(e.ValidationError):
            e.ingest([path, path])

    def test_rank_ties_dominance_finalists_and_as_of(self):
        baseline = run(
            answer=lambda item: ("A" if int(item["id"]) < 120 else "B", 1, 1),
            model=dict(MODEL, name="loser"),
        )
        candidate = run(created_at="2026-11-12T14:00:00+00:00")
        tied = run(model=dict(MODEL, name="tied"))
        view = f.rank([baseline, candidate, tied])[0]
        self.assertEqual(view["as_of"], candidate["created_at"])
        self.assertEqual(
            [r["normalized_score"] for r in view["rows"]],
            [1, 1, f.normalized(0.6, 0.25)],
        )
        self.assertEqual(len(view["best_record_ids"]), 2)
        for row in view["rows"][:2]:
            self.assertEqual(row["verdict"], "INCONCLUSIVE")
            self.assertEqual(len(row["tie_with"]), 1)
            self.assertTrue(row["finalist"])
            self.assertEqual(row["full_run"], "NOT_RUN")
        self.assertFalse(view["rows"][2]["finalist"])

    def test_hardware_plan_settings_and_alpha_are_separate(self):
        records = [
            run(),
            run(hardware=dict(HARDWARE, machine_label="fixture-machine-b")),
            run(datasets(199)),
            run(runtime_settings=dict(SETTINGS, temperature=1)),
            run(alpha="0.01"),
        ]
        self.assertEqual(len(f.rank(records)), 5)
        for group in f.rank(records):
            self.assertEqual(len(group["rows"]), 1)
            self.assertEqual(group["rows"][0]["verdict"], "INCONCLUSIVE")

    def test_unknown_settings_hardware_and_chance_stay_pending(self):
        records = [
            run(runtime_settings=None),
            run(hardware=dict(HARDWARE, machine_label=None)),
            run(runtime_settings={"seed": 42}),
            run(runtime_settings=dict(SETTINGS, ctx=None)),
            run({"unknown": datasets()["arc"]}),
            run(pre=f.pre_check(100, "Q4_K_M", None)),
        ]
        for group in f.rank(records):
            self.assertEqual(group["rows"], [])
            self.assertEqual(len(group["pending"]), 1)
            self.assertEqual(group["best_record_ids"], [])

    def test_incomplete_settings_cannot_support_a_paired_claim(self):
        baseline = run(runtime_settings={"seed": 42})
        result = run(runtime_settings={"seed": 42}, baseline=baseline)
        self.assertEqual(result["comparison"]["arc"]["verification"], "UNVERIFIED")

    def test_relaxed_post_does_not_promote_incomplete_sample(self):
        data = datasets(5)
        plan = f.make_plan(data, wall_budget_s="1")
        clock = FakeClock()

        def answer(item):
            clock.now += 1
            return "A", 1, 1

        record = run(
            data,
            plan,
            answer,
            clock=clock,
            post_options={"min_coverage": "0.1", "max_interval_width": "1"},
        )
        self.assertIn(record["post"]["decision"], ("REJECT", "UNKNOWN"))
        self.assertEqual(f.rank([record])[0]["rows"], [])

    def test_conflicting_benchmark_wins_remain_inconclusive(self):
        data = datasets()
        data["winogrande"] = copy.deepcopy(data["arc"])
        for row in data["winogrande"]["items"]:
            row["question"] = "other benchmark"
        candidate = run(
            data,
            answer=lambda item: (
                "A" if item["question"] == "fixture" or int(item["id"]) < 120 else "B",
                1,
                1,
            ),
        )
        baseline = run(
            data,
            model=dict(MODEL, name="baseline"),
            answer=lambda item: (
                "A" if item["question"] != "fixture" or int(item["id"]) < 120 else "B",
                1,
                1,
            ),
        )
        verdict, details = f.pair_verdict(candidate, baseline)
        self.assertEqual(verdict, "INCONCLUSIVE")
        self.assertEqual(details["arc"]["verdict"], "BETTER")
        self.assertEqual(details["winogrande"]["verdict"], "WORSE")
        self.assertTrue(
            all(row["tie_with"] for row in f.rank([candidate, baseline])[0]["rows"])
        )

    def test_cli_plan_validate_ingest_rank_and_exclusive_output(self):
        data = self.file("datasets.json", datasets(5))
        plan = self.path / "plan.json"
        self.assertEqual(
            f.main(["plan", str(data), "--count", "3", "--output", str(plan)]), 0
        )
        self.assertEqual(len(f.load(plan)["benchmarks"][0]["item_ids"]), 3)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(f.main(["plan", str(data), "--output", str(plan)]), 2)
        path = self.file("record.json", run())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(f.main(["validate", str(path)]), 0)
        self.assertIn("full ECI NOT_RUN", out.getvalue())
        output = self.path / "normalized.json"
        envelope = self.file(
            "envelope.json",
            dict(schema_version=1, runs=[], items=[], fast_checks=[f.load(path)]),
        )
        self.assertEqual(e.main(["ingest", str(envelope), "--output", str(output)]), 0)
        for operation in ("validate", "rank", "status"):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(e.main([operation, str(output)]), 0)
            self.assertIsInstance(json.loads(out.getvalue()), dict)

    def test_duplicate_json_key_and_invalid_fast_record_are_not_silently_dropped(self):
        path = self.path / "duplicate.json"
        path.write_text('{"kind":"agiw_fast_check", "kind":"other"}')
        with self.assertRaises(ValueError):
            e.ingest([path])
        path = self.file("bad.json", dict(kind="agiw_fast_check", schema_version=1))
        with self.assertRaises(e.ValidationError):
            e.ingest([path])
        bad = run()
        del bad["kind"]
        with self.assertRaises(e.ValidationError):
            e.ingest([self.file("missing-kind.json", bad)])
        with self.assertRaises(e.ValidationError):
            e.ingest([self.file("plan-is-not-a-record.json", f.make_plan(datasets()))])

    def test_published_schema_contains_the_record_contract(self):
        schema = f.load(ROOT / "eci_fast_check.schema.json")
        record = run(baseline=run(model=dict(MODEL, name="fixture-baseline")))
        b = record["benchmarks"][0]
        cases = {
            "record": record,
            "benchmark": b,
            "observation": b["observations"][0],
            "plan": record["plan"],
            "sample": record["plan"]["benchmarks"][0],
            "baseline": record["baseline"],
            "comparison": record["comparison"]["arc"],
        }
        for definition, value in cases.items():
            with self.subTest(definition=definition):
                self.assertEqual(
                    set(schema["$defs"][definition]["required"]), set(value)
                )
        for name in ("pre", "post"):
            self.assertEqual(
                set(schema["$defs"][name]["properties"]["inputs"]["required"]),
                set(record[name]["inputs"]),
            )
        budget_schema = schema["$defs"]["plan"]["properties"]["wall_budget_s"]
        self.assertIn({"$ref": "#/$defs/decimal"}, budget_schema["anyOf"])
        self.assertTrue(schema["$defs"]["hardware"]["additionalProperties"])
        self.assertEqual(schema["$defs"]["decimal"]["type"], "string")
        self.assertEqual(
            schema["$defs"]["record"]["properties"]["alpha"]["$ref"], "#/$defs/decimal"
        )
        outer = f.load(ROOT / "eci_tuning.schema.json")
        self.assertEqual(
            outer["properties"]["fast_checks"]["items"]["$ref"],
            "eci_fast_check.schema.json#/$defs/record",
        )


class FixRoundRegressions(unittest.TestCase):
    def test_whole_population_hash_sample_unequal_strata(self):
        data = datasets(10099)
        for i, row in enumerate(data["arc"]["items"]):
            row["subject"] = "large" if i < 10000 else "singleton-" + str(i)
            row["answer"] = "A" if i < 10000 else "B"
        plan = f.make_plan(data)
        spec = plan["benchmarks"][0]
        priority = lambda item_id: f.digest([spec["seed"], item_id])
        expected = sorted((r["id"] for r in data["arc"]["items"]), key=priority)[:200]
        self.assertEqual(spec["item_ids"], expected)
        self.assertEqual(spec["sampling"], "simple_random_without_replacement")
        result = run(data, plan)["benchmarks"][0]
        self.assertGreater(result["accuracy"], 0.95)
        self.assertTrue(any(s["selected"] == 0 for s in spec["strata"]))
        self.assertTrue(f.make_plan(datasets(3))["benchmarks"][0]["whole_dataset"])

    def test_exact_alpha_boundary_is_conservative(self):
        a = {str(i): True for i in range(6)}
        b = {k: False for k in a}
        for alpha in ("0.031249999999999999", "0.03125"):
            self.assertEqual(f.mcnemar(a, b, alpha)["verdict"], "INCONCLUSIVE")
        self.assertEqual(f.mcnemar(a, b, "0.031250000000000001")["verdict"], "BETTER")

    def test_adapter_quality_classes_and_planned_denominator(self):
        values = ["correct", "wrong", "truncated_empty", "unparsed", "error"]
        calls = []

        def answer(item):
            outcome = values[len(calls)]
            calls.append(item["id"])
            return observation(outcome)

        record = run(datasets(5), answer=answer)
        b = record["benchmarks"][0]
        self.assertEqual(b["accuracy"], 0.2)
        self.assertEqual(b["accuracy_over_parsed_items"], 0.5)
        self.assertEqual(b["outcome_counts"], dict.fromkeys(values, 1))
        self.assertEqual(b["outcome_rates"], dict.fromkeys(values, 0.2))
        self.assertEqual(record["post"]["decision"], "REJECT")
        self.assertIn(
            "answers cut off at the token limit: raise the limit and re-run",
            record["post"]["reasons"],
        )

    def test_empty_garbage_and_chance_outputs_never_rank(self):
        data = {"winogrande": datasets()["arc"]}
        for outcome in ("truncated_empty", "unparsed", "wrong"):
            record = run(
                data,
                answer=lambda item: observation(
                    outcome, completion_tokens=70000, seconds=1
                ),
            )
            self.assertEqual(record["post"]["decision"], "REJECT")
            self.assertIn(
                "runtime/model incompatibility suspected", record["post"]["reasons"]
            )
            self.assertEqual(f.rank([record])[0]["rows"], [])
        calls = []

        def chance(item):
            calls.append(item["id"])
            return observation("correct" if len(calls) % 2 else "wrong")

        record = run(data, answer=chance)
        self.assertEqual(record["post"]["decision"], "UNKNOWN")
        self.assertIn(
            "not distinguishable from chance on this sample: weak model or incompatible runtime; re-run with a larger sample or check the runtime",
            record["post"]["reasons"],
        )
        self.assertEqual(f.rank([record])[0]["rows"], [])

    def test_baseline_outcomes_bound_to_full_record(self):
        baseline = run()
        candidate = run(baseline=baseline)
        binding = candidate["baseline"]
        self.assertEqual(
            binding["outcomes_sha256"], f.digest(f.baseline_outcomes(baseline))
        )
        self.assertEqual(binding["record"], baseline)
        bad = copy.deepcopy(candidate)
        bad["baseline"]["outcomes_sha256"] = "0" * 64
        with self.assertRaises(f.ValidationError):
            f.validate_record(rehash(bad))
        bad = copy.deepcopy(candidate)
        bad["baseline"]["record"]["benchmarks"][0]["observations"][0]["correct"] = False
        with self.assertRaises(f.ValidationError):
            f.validate_record(rehash(bad))
        unresolved = copy.deepcopy(candidate)
        unresolved["baseline"]["record"] = None
        unresolved["comparison"] = f.comparisons(
            unresolved, unresolved["baseline"], unresolved["alpha"]
        )
        f.validate_record(rehash(unresolved))
        self.assertEqual(unresolved["comparison"]["arc"]["verification"], "UNVERIFIED")
        self.assertEqual(unresolved["comparison"]["arc"]["verdict"], "INCONCLUSIVE")

    def test_incomplete_pairs_report_counts(self):
        data = datasets(20)
        plan = f.make_plan(data, wall_budget_s="3")
        clock = FakeClock()

        def answer(item):
            clock.now += 1
            return observation("wrong")

        baseline = run(data, plan, answer, clock=clock)
        candidate = run(data, plan, baseline=baseline)
        test = candidate["comparison"]["arc"]
        self.assertEqual(test["verdict"], "INCONCLUSIVE")
        self.assertEqual(test["reason"], "incomplete pairs")
        self.assertEqual(
            (
                test["n_expected"],
                test["n_candidate"],
                test["n_baseline"],
                test["n_shared"],
            ),
            (20, 20, 3, 3),
        )

    def test_checkpoint_crash_resume_fsync_and_binding(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "checkpoint.jsonl"
            calls = []

            def crash(item):
                if len(calls) == 3:
                    raise KeyboardInterrupt()
                calls.append(item["id"])
                return observation("correct")

            with patch("eci_fast_check.os.fsync", wraps=os.fsync) as sync:
                with self.assertRaises(KeyboardInterrupt):
                    run(datasets(8), answer=crash, checkpoint=path)
                self.assertGreaterEqual(sync.call_count, 3)
            self.assertEqual(len(path.read_text().splitlines()), 3)
            rest = []
            resumed = run(
                datasets(8),
                answer=lambda item: rest.append(item["id"]) or observation("correct"),
                checkpoint=path,
            )
            self.assertEqual(len(rest), 5)
            self.assertEqual(len(set(calls + rest)), 8)
            self.assertEqual(resumed["benchmarks"][0]["correct"], 8)
            self.assertEqual(
                run(
                    datasets(8),
                    answer=lambda item: self.fail("repeated"),
                    checkpoint=path,
                )["benchmarks"][0]["correct"],
                8,
            )
            for options in (
                {"model": dict(MODEL, name="other")},
                {"hardware": dict(HARDWARE, cpu="other")},
                {"runtime_settings": dict(SETTINGS, ctx=8000)},
            ):
                with self.assertRaises(f.ValidationError):
                    run(
                        datasets(8),
                        checkpoint=path,
                        answer=lambda item: self.fail("mixed"),
                        **options,
                    )
            with self.assertRaises(f.ValidationError):
                run(
                    datasets(9),
                    checkpoint=path,
                    answer=lambda item: self.fail("mixed plan"),
                )

    def test_failure_preserves_returned_and_exception_measurements(self):
        def broken_score(prediction, item):
            raise RuntimeError("private text")

        record = run(
            datasets(1),
            answer=lambda item: dict(observation("correct"), prediction="A"),
            score=broken_score,
        )
        item = record["benchmarks"][0]["observations"][0]
        self.assertEqual(
            (
                item["outcome"],
                item["prompt_tokens"],
                item["completion_tokens"],
                item["seconds"],
            ),
            ("error", 8, 4, 2),
        )

        def timeout(item):
            exc = TimeoutError("private text")
            exc.seconds = 3
            exc.prompt_tokens = 10
            exc.completion_tokens = 2
            raise exc

        item = run(datasets(1), answer=timeout)["benchmarks"][0]["observations"][0]
        self.assertEqual(
            (item["seconds"], item["prompt_tokens"], item["completion_tokens"]),
            (3, 10, 2),
        )

    def test_hardware_is_validated_freeform_and_grouped(self):
        hardware = dict(
            machine_label="fixture-machine-a",
            cpu="fixture-cpu",
            gpu={"label": "fixture-gpu"},
            memory={"bytes": 1000},
            runtime_build_id="fixture-build",
            extension={"version": 1},
        )
        one = run(hardware=hardware)
        two = run(hardware=dict(hardware, runtime_build_id="fixture-build-2"))
        self.assertEqual(len(f.rank([one, two])), 2)
        for value in ("", [], {"machine_label": "fixture"}):
            with self.assertRaises(f.ValidationError):
                run(hardware=value)

    def test_thresholds_stay_exact_and_are_recorded(self):
        b = dict(
            planned=200,
            attempted=200,
            correct=190,
            parsed=190,
            errors=0,
            truncations=10,
            chance="0.25",
            answer_options=4,
            top_answer_count=1,
            unknown_finish=0,
        )
        self.assertEqual(f.post_check([b])["decision"], "ADMIT")
        self.assertEqual(
            f.post_check([b], max_truncation_rate="0.049999999999999999")["decision"],
            "REJECT",
        )
        b.update(correct=198, parsed=198, errors=2, truncations=0)
        self.assertEqual(f.post_check([b])["decision"], "ADMIT")
        self.assertEqual(
            f.post_check([b], max_error_rate="0.009999999999999999")["decision"],
            "REJECT",
        )
        b.update(attempted=199, correct=199, parsed=199, errors=0)
        self.assertEqual(f.post_check([b], min_coverage="0.995")["decision"], "UNKNOWN")
        self.assertEqual(
            f.post_check([b], min_coverage="0.995000000000000001")["decision"], "REJECT"
        )
        self.assertEqual(
            f.pre_check(75, "Q4", 100, safety_margin="0.25")["decision"], "CHECK"
        )
        self.assertEqual(
            f.pre_check(75, "Q4", 100, safety_margin="0.250000000000000001")[
                "decision"
            ],
            "SKIP",
        )
        result = run(
            alpha="0.050000000000000001",
            post_options={"max_unparsed_rate": "0.100000000000000001"},
        )
        self.assertEqual(result["alpha"], "0.050000000000000001")
        self.assertEqual(
            result["post"]["inputs"]["max_unparsed_rate"], "0.100000000000000001"
        )
        for options in (
            {"min_coverage": 1.0},
            {"max_interval_width": 0.2},
            {"max_error_rate": 0.01},
            {"max_truncation_rate": 0.05},
            {"max_unparsed_rate": 0.1},
        ):
            with self.assertRaises(f.ValidationError):
                f.post_check([b], **options)
        for alpha in (0.05, "NaN", "1e-2", "0"):
            with self.assertRaises(f.ValidationError):
                f.mcnemar({}, {}, alpha)

    def test_wilson_gate_boundaries_use_exact_counts_not_display_floats(self):
        # Independent 90-digit Decimal calculation gives width
        # 0.13727828079221626630555189846528198781823169838544...
        evidence = dict(
            planned=200,
            attempted=200,
            correct=100,
            parsed=200,
            errors=0,
            truncations=0,
            chance="0",
            answer_options=None,
            top_answer_count=1,
            unknown_finish=0,
        )
        lower = "0.13727828079221626630"
        upper = "0.13727828079221626631"
        self.assertEqual(float(lower), float(upper))
        self.assertEqual(
            f.post_check([evidence], max_interval_width=lower)["decision"], "REJECT"
        )
        self.assertEqual(
            f.post_check([evidence], max_interval_width=upper)["decision"], "ADMIT"
        )

    def test_unscored_items_cannot_support_an_otherwise_significant_pair(self):
        baseline = run(
            answer=lambda item: ("A" if int(item["id"]) < 130 else "B", 1, 1)
        )
        for quality in ("truncated_empty", "unparsed", "error"):
            calls = []

            def answer(item):
                calls.append(item["id"])
                return observation(quality if len(calls) == 1 else "correct")

            result = run(answer=answer, baseline=baseline)
            test = result["comparison"]["arc"]
            self.assertEqual(test["reason"], "incomplete pairs")
            self.assertEqual(test["verdict"], "INCONCLUSIVE")
            self.assertEqual(
                (
                    test["n_expected"],
                    test["n_candidate"],
                    test["n_baseline"],
                    test["n_shared"],
                ),
                (200, 199, 200, 199),
            )

    def test_budget_is_an_exact_decimal_threshold(self):
        data = datasets(3)
        with self.assertRaises(f.ValidationError):
            f.make_plan(data, wall_budget_s=0.1)
        plan = f.make_plan(data, wall_budget_s="0.100000000000000001")
        clock = FakeClock()

        def answer(item):
            clock.now += 0.1
            return observation()

        result = run(data, plan, answer, clock=clock)
        self.assertEqual(result["benchmarks"][0]["attempted"], 2)
        self.assertEqual(result["plan"]["wall_budget_s"], "0.100000000000000001")

    def test_checkpoint_budget_resume_and_corrupt_prefix_refusal(self):
        data = datasets(5)
        plan = f.make_plan(data, wall_budget_s="2")
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "observations.jsonl"
            clock = FakeClock()

            def first(item):
                clock.now += 1
                return observation()

            partial = run(data, plan, first, clock=clock, checkpoint=path)
            self.assertEqual(partial["benchmarks"][0]["attempted"], 2)
            result = run(data, plan, checkpoint=path)
            self.assertEqual(result["benchmarks"][0]["attempted"], 5)
            rows = path.read_text().splitlines()
            rows[0], rows[1] = rows[1], rows[0]
            path.write_text("\n".join(rows) + "\n")
            with self.assertRaises(f.ValidationError):
                run(
                    data,
                    plan,
                    checkpoint=path,
                    answer=lambda i: self.fail("must not call"),
                )
            path.write_text("{incomplete")
            with self.assertRaises(f.ValidationError):
                run(
                    data,
                    plan,
                    checkpoint=path,
                    answer=lambda i: self.fail("must not call"),
                )

    def test_checkpoint_and_pairs_do_not_conflate_json_boolean_identity(self):
        one = dict(HARDWARE, extension={"revision": 1})
        other = dict(HARDWARE, extension={"revision": True})
        baseline = run(datasets(3), hardware=one)
        with self.assertRaises(f.ValidationError):
            run(
                datasets(3),
                baseline=baseline,
                hardware=other,
                answer=lambda i: self.fail("mixed baseline"),
            )
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "checkpoint.jsonl"
            run(datasets(3), hardware=one, checkpoint=path)
            with self.assertRaises(f.ValidationError):
                run(
                    datasets(3),
                    hardware=other,
                    checkpoint=path,
                    answer=lambda i: self.fail("mixed checkpoint"),
                )

    def test_wall_time_validation_tolerance_uses_an_exact_decimal(self):
        record = run(datasets(3))
        record["wall_time_s"] = 0.0004
        record["benchmarks"][0]["wall_time_s"] = 0.000401
        f.validate_record(rehash(record))
        record["benchmarks"][0]["wall_time_s"] = 0.00040100000000000004
        with self.assertRaises(f.ValidationError):
            f.validate_record(rehash(record))

    def test_resumed_wall_time_does_not_invent_prior_elapsed_time(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "checkpoint.jsonl"
            first = run(datasets(3), checkpoint=path)
            resumed = run(
                datasets(3), checkpoint=path, answer=lambda i: self.fail("must reuse")
            )
            self.assertEqual(first["wall_time_s"], 0)
            self.assertEqual(resumed["wall_time_s"], 0)
            self.assertEqual(resumed["benchmarks"][0]["wall_time_s"], 0)
            self.assertEqual(resumed["benchmarks"][0]["answer_seconds"], 6)

    def test_plan_rejects_chance_one_in_every_decimal_spelling(self):
        plan = f.make_plan({"fixture-unknown": datasets(3)["arc"]})
        for chance in ("1", "1.0", "1.00"):
            bad = copy.deepcopy(plan)
            bad["benchmarks"][0]["chance"] = chance
            bad["plan_id"] = f.digest({k: v for k, v in bad.items() if k != "plan_id"})
            with self.assertRaises(f.ValidationError):
                f.validate_plan(bad)

    def test_invalid_adapter_classification_is_not_a_scored_answer(self):
        bad = [
            dict(observation("correct"), parsed=False),
            dict(observation("truncated_empty"), finish_reason="stop", cut_off=False),
            dict(observation("unparsed"), parsed=True),
            dict(observation("correct"), outcome="unknown"),
            ("A", 4, 2),
        ]
        for supplied in bad:
            with self.assertRaises(f.ValidationError):
                f.run_check(
                    f.make_plan(datasets(1)),
                    datasets(1),
                    lambda i: supplied,
                    model=MODEL,
                    hardware=HARDWARE,
                    runtime_settings=SETTINGS,
                    pre=f.pre_check(100, "Q4_K_M", 800),
                    clock=FakeClock(),
                    created_at=STAMP,
                )


def observation(outcome="correct", **updates):
    value = dict(
        outcome=outcome,
        parsed=outcome in ("correct", "wrong"),
        finish_reason="length" if outcome == "truncated_empty" else "stop",
        cut_off=outcome == "truncated_empty",
        answer_value="A"
        if outcome == "correct"
        else "B"
        if outcome == "wrong"
        else None,
        prompt_tokens=8,
        completion_tokens=4,
        seconds=2,
        error="AdapterError" if outcome == "error" else None,
    )
    value.update(updates)
    return value


# Include round-two regression modules in the explicit user-requested suite.
from test_eci_fast_check_round2 import RoundTwoTests
from test_eci_fast_check_runtime_round2 import RuntimeRound2Tests

if __name__ == "__main__":
    unittest.main()
