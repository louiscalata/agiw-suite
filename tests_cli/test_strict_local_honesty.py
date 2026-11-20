"""Strict local mode stays honest: hosted TypeSafe Jev evidence never rides a strict-local route (LOU-41)."""

import itertools
import unittest

from agiw import fleet_jev
from agiw.fleet_routing_plan import POOL_MODELS, HostedEvidence, RoutePlan, WorkerEvidence, plan
from agiw.windows_worker_receipt_check import valid_route

W = "m5-macbook-pro"
PROGRAM = "nisi-smoke.mjs"
NOW = 1000.0
STRICT = {"worker": W, "preanalysis": "local", "reason": "strict-local"}


def jev_evidence():
    def judge(state, questions):
        return {
            "model": fleet_jev.JEV_MODEL,
            "answers": {"plan": {"choice": "direct", "confidence": 0.42}, "underspecified": {"noul": 0.1}},
        }

    evidence = fleet_jev.preanalyze(PROGRAM, judge)
    evidence.pop("status")
    return evidence


class StrictLocalHonestyTests(unittest.TestCase):
    def test_strict_local_and_labelled_jev_routes_are_both_accepted(self):
        self.assertTrue(valid_route(dict(STRICT), W, program=PROGRAM))
        jev_route = {"worker": W, "preanalysis": "jev", "reason": "jev-preanalyzed", "jev": jev_evidence()}
        self.assertTrue(valid_route(jev_route, W, program=PROGRAM))

    def test_hosted_jev_never_rides_a_strict_local_route(self):
        cases = [
            dict(STRICT, jev=jev_evidence()),
            dict(STRICT, jev={}),
            dict(STRICT, hosted=True),
            {"worker": W, "preanalysis": "jev", "reason": "strict-local"},
            {"worker": W, "preanalysis": "jev", "reason": "strict-local", "jev": jev_evidence()},
            {"worker": W, "preanalysis": "local", "reason": "jev-preanalyzed"},
            {"worker": W, "preanalysis": "local", "reason": "jev-first"},
            {"worker": W, "preanalysis": "local", "reason": "jev-not-applicable"},
        ]
        for route in cases:
            with self.subTest(route=route):
                self.assertFalse(valid_route(route, W, program=PROGRAM))

    def test_repo_patch_exception_stays_local_only(self):
        not_applicable = {"worker": W, "preanalysis": "local", "reason": "jev-not-applicable"}
        self.assertTrue(valid_route(dict(not_applicable), W, program="repo-patch"))
        for route in (dict(not_applicable, jev=jev_evidence()), dict(not_applicable, hosted=True)):
            with self.subTest(route=route):
                self.assertFalse(valid_route(route, W, program="repo-patch"))

    def test_route_plan_labels_jev_and_never_calls_it_strict_local(self):
        self.assertEqual(RoutePlan.for_local(W), RoutePlan(W, "local", "strict-local"))
        judged = RoutePlan.for_local(W, jev_preanalyzed=True)
        self.assertEqual((judged.preanalysis, judged.reason), ("jev", "jev-preanalyzed"))

    def test_local_plan_ignores_every_hosted_gate_combination(self):
        workers = {W: WorkerEvidence(NOW, True, True, True, False)}
        for gates in itertools.product((False, True), repeat=4):
            for jev_ready in (None, False, True):
                for tier in POOL_MODELS:
                    hosted = HostedEvidence(gates[0], jev_ready, gates[1], gates[2], gates[3], tier=tier)
                    with self.subTest(gates=gates, jev_ready=jev_ready, tier=tier):
                        self.assertEqual(
                            plan("local", "routine", workers, hosted, now=NOW), RoutePlan(W, "local", "strict-local")
                        )


if __name__ == "__main__":
    unittest.main()
