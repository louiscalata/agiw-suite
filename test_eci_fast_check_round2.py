"""Round-two offline regressions; evidence is synthetic and no runtime is called."""

from fractions import Fraction
import math
import unittest
import eci_fast_check as f


class RoundTwoTests(unittest.TestCase):
    def run_fixture(self, **kw):
        from test_eci_fast_check import run

        return run(**kw)

    def data(self, chance="0.5", options=2):
        from test_eci_fast_check import datasets

        return {
            "fixture": dict(datasets()["arc"], chance=chance, answer_options=options)
        }

    def obs(self, outcome="correct", **kw):
        from test_eci_fast_check import observation

        return observation(outcome, **kw)

    def test_harness_cutoffs_and_sample_limit_count_unparsed_items(self):
        for reason in (
            "length",
            "max_tokens",
            "model_length",
            "token_limit",
            "sample.limit",
            "stop",
        ):
            calls = []

            def answer(item):
                calls.append(item)
                return (
                    self.obs("unparsed", cut_off=True, finish_reason=reason)
                    if len(calls) <= 20
                    else self.obs()
                )

            record = self.run_fixture(data=self.data(), answer=answer)
            self.assertEqual(record["benchmarks"][0]["truncations"], 20)
            self.assertEqual(
                record["benchmarks"][0]["outcome_counts"]["truncated_empty"], 20
            )
            self.assertEqual(record["post"]["decision"], "REJECT")
            self.assertIn(
                "answers cut off at the token limit: raise the limit and re-run",
                record["post"]["reasons"],
            )
            self.assertEqual(f.rank([record])[0]["rows"], [])

    def test_inconsistent_cutoff_and_answer_evidence_is_validation_error(self):
        for update in (
            {"cut_off": False, "finish_reason": "model_length"},
            {"cut_off": None},
            {"answer_value": None},
            {"answer_value": " A "},
            {"answer_value": "x" * 129},
        ):
            with self.subTest(update=update), self.assertRaises(f.ValidationError):
                self.run_fixture(answer=lambda item: self.obs(**update))

    def test_near_total_parse_failure_and_zero_parse_chance_zero(self):
        for parsed in (0, 1):
            calls = []

            def answer(item):
                calls.append(item)
                return self.obs("wrong" if len(calls) <= parsed else "unparsed")

            record = self.run_fixture(data=self.data("0", None), answer=answer)
            self.assertEqual(record["post"]["decision"], "REJECT")
            self.assertIn(
                "runtime/model incompatibility suspected: answers could not be parsed",
                record["post"]["reasons"],
            )
            if not parsed:
                self.assertIn(
                    "runtime/model incompatibility suspected", record["post"]["reasons"]
                )
            self.assertEqual(record["post"]["inputs"]["max_unparsed_rate"], "0.10")
            self.assertEqual(f.rank([record])[0]["rows"], [])

    def test_exact_unparsed_boundary(self):
        for missing, expected in ((20, "ADMIT"), (21, "REJECT")):
            calls = []

            def answer(item):
                calls.append(item)
                return self.obs("unparsed" if len(calls) <= missing else "correct")

            record = self.run_fixture(data=self.data("0", None), answer=answer)
            self.assertEqual(record["post"]["decision"], expected)

    def test_perfect_high_chance_is_admitted(self):
        record = self.run_fixture(data=self.data("0.9", 10))
        self.assertEqual(record["post"]["decision"], "ADMIT")
        self.assertEqual(len(f.rank([record])[0]["rows"]), 1)

    def test_near_chance_clean_models_are_unknown_never_incompatible(self):
        evidence = dict(
            planned=200,
            attempted=200,
            parsed=200,
            errors=0,
            truncations=0,
            chance="0.5",
            answer_options=2,
            top_answer_count=100,
            unknown_finish=0,
        )
        unknown_probability = Fraction(0)
        for k in range(201):
            post = f.post_check([dict(evidence, correct=k)])
            self.assertNotEqual(post["decision"], "REJECT")
            self.assertNotIn("runtime/model incompatibility suspected", post["reasons"])
            if k <= 112:
                self.assertEqual(post["decision"], "UNKNOWN")
                self.assertIn(
                    "not distinguishable from chance on this sample: weak model or incompatible runtime; re-run with a larger sample or check the runtime",
                    post["reasons"],
                )
                unknown_probability += Fraction(
                    math.comb(200, k) * 11**k * 9 ** (200 - k), 20**200
                )
            else:
                self.assertEqual(post["decision"], "ADMIT")
        self.assertAlmostEqual(
            float(unknown_probability), 0.6380034604943883, places=15
        )
        calls = []

        def answer(item):
            calls.append(item)
            return self.obs(
                "correct" if len(calls) <= 110 else "wrong",
                answer_value="A" if len(calls) % 2 else "B",
            )

        record = self.run_fixture(data=self.data(), answer=answer)
        self.assertEqual(record["post"]["decision"], "UNKNOWN")
        self.assertEqual(f.rank([record])[0]["rows"], [])

    def test_chance_tail_uses_exact_record_alpha(self):
        p = sum(Fraction(math.comb(200, k), 2**200) for k in range(113, 201))
        evidence = dict(
            planned=200,
            attempted=200,
            correct=113,
            parsed=200,
            errors=0,
            truncations=0,
            chance="0.5",
            answer_options=2,
            top_answer_count=100,
            unknown_finish=0,
        )
        # Exact binary-denominator probability has a finite decimal representation.
        # Compare immediately neighboring decimal thresholds, not displayed p floats.
        from decimal import Decimal, localcontext

        with localcontext() as ctx:
            ctx.prec = 250
            exact = Decimal(p.numerator) / Decimal(p.denominator)
            self.assertEqual(
                f.post_check([evidence], alpha=format(exact, "f"))["decision"], "ADMIT"
            )
            self.assertEqual(
                f.post_check([evidence], alpha=format(exact - Decimal("1e-210"), "f"))[
                    "decision"
                ],
                "UNKNOWN",
            )

    def test_degenerate_answers_at_chance_and_zero_chance_reject(self):
        for chance in ("0.5", "0"):
            calls = []

            def answer(item):
                calls.append(item)
                return self.obs(
                    "correct" if len(calls) <= 100 else "wrong", answer_value="A"
                )

            record = self.run_fixture(data=self.data(chance), answer=answer)
            self.assertEqual(record["benchmarks"][0]["top_answer_share"], 1)
            self.assertEqual(record["post"]["decision"], "REJECT")
            self.assertIn(
                "runtime/model incompatibility suspected", record["post"]["reasons"]
            )

    def test_relaxed_missing_evidence_never_ranks(self):
        calls = []

        def answer(item):
            calls.append(item)
            return self.obs("error" if len(calls) % 3 != 0 else "correct")

        for options, expected in (
            ({"max_error_rate": "1"}, "UNKNOWN"),
            ({"max_error_rate": "1", "max_unparsed_rate": "1"}, "UNKNOWN"),
        ):
            calls.clear()
            record = self.run_fixture(
                data=self.data("0", None), answer=answer, post_options=options
            )
            self.assertEqual(record["post"]["decision"], expected)
            self.assertEqual(f.rank([record])[0]["rows"], [])

    def test_zero_parse_rule_is_independent_of_parseability_bound(self):
        record = self.run_fixture(
            data=self.data("0", None),
            answer=lambda item: self.obs("unparsed"),
            post_options={"max_unparsed_rate": "1"},
        )
        self.assertEqual(record["post"]["decision"], "REJECT")
        self.assertIn(
            "runtime/model incompatibility suspected", record["post"]["reasons"]
        )

    def test_degeneracy_boundary_is_inclusive(self):
        for top, expected in ((179, "ADMIT"), (180, "REJECT")):
            calls = []

            def answer(item):
                calls.append(item)
                return self.obs(
                    "correct" if len(calls) <= 100 else "wrong",
                    answer_value="A" if len(calls) <= top else "B",
                )

            record = self.run_fixture(data=self.data("0"), answer=answer)
            self.assertEqual(record["post"]["decision"], expected)
            self.assertEqual(record["benchmarks"][0]["top_answer_share"], top / 200)

    def test_ranking_separates_admission_policies(self):
        one = self.run_fixture()
        two = self.run_fixture(post_options={"max_error_rate": "0.005"})
        groups = f.rank([one, two])
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            {g["admission_policy"]["max_error_rate"] for g in groups}, {"0.01", "0.005"}
        )
        self.assertTrue(all(len(g["rows"]) == 1 for g in groups))

    def test_record_alpha_cannot_be_overridden_by_post_options(self):
        with self.assertRaisesRegex(f.ValidationError, "alpha"):
            self.run_fixture(
                answer=lambda item: self.fail("invalid options called adapter"),
                alpha="0.01",
                post_options={"alpha": "0.05"},
            )

    def test_parsed_cutoff_answer_is_not_empty_truncation(self):
        record = self.run_fixture(
            answer=lambda item: self.obs(cut_off=True, finish_reason="model_length")
        )
        self.assertEqual(record["benchmarks"][0]["truncations"], 0)
        self.assertEqual(record["post"]["decision"], "ADMIT")

    def test_unknown_finish_evidence_is_retained_and_pending(self):
        record = self.run_fixture(answer=lambda item: self.obs(finish_reason=None))
        self.assertEqual(
            record["benchmarks"][0]["observations"][0]["finish_reason"], None
        )
        self.assertEqual(record["post"]["decision"], "UNKNOWN")
        self.assertEqual(f.rank([record])[0]["rows"], [])
