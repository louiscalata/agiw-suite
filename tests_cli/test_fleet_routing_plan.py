import ast
import unittest
from dataclasses import replace
from pathlib import Path

from agiw import fleet_routing_plan as routing

NOW = 1000.0
READY = routing.WorkerEvidence(NOW, True, True, True, False)
MAC, PC, MONTEREY = tuple(routing.WORKERS)


class FleetRoutingPlanTests(unittest.TestCase):
    def route(self, workers, *, mode="local", workload="routine", hosted=None, source="agent"):
        return routing.plan(mode, workload, workers, hosted or routing.HostedEvidence(), now=NOW, source=source)

    def test_inventory_matches_sketch_without_claiming_readiness(self):
        self.assertEqual(set(routing.WORKERS), {MAC, PC, MONTEREY})
        self.assertTrue(all("grok" in tools for tools in routing.WORKERS.values()))
        self.assertIn("opencode", routing.WORKERS[MAC])
        self.assertNotIn("claude", routing.WORKERS[MONTEREY])
        self.assertIsNone(self.route({}).worker)

    def test_mac_routine_pc_heavy(self):
        workers = {MAC: READY, PC: READY, MONTEREY: READY}
        self.assertEqual(self.route(workers).worker, MAC)
        self.assertEqual(self.route(workers, workload="gpu-heavy").worker, PC)

    def test_busy_pc_not_selected(self):
        workers = {MAC: READY, PC: replace(READY, busy=True)}
        self.assertEqual(self.route(workers, workload="gpu-heavy").worker, MAC)

    def test_monterey_selected_only_when_verified(self):
        self.assertIsNone(self.route({MONTEREY: replace(READY, authenticated=False)}).worker)
        self.assertEqual(self.route({MONTEREY: READY}).worker, MONTEREY)

    def test_unknown_stale_future_nonfinite_or_unleased_never_selected(self):
        candidates = [
            routing.WorkerEvidence(NOW),
            replace(READY, observed_at=NOW - 31),
            replace(READY, observed_at=NOW + 1),
            replace(READY, observed_at=float("nan")),
            replace(READY, lease_available=False),
            replace(READY, local_model_ready=False),
            replace(READY, busy=0),
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertIsNone(self.route({MAC: candidate, "not-in-sketch": READY}).worker)

    def test_local_never_uses_hosted_preanalysis(self):
        hosted = routing.HostedEvidence(True, True, True, True, True)
        self.assertEqual(self.route({MAC: READY}, hosted=hosted).preanalysis, "local")

    def test_jev_first_when_both_hosted_candidates_ready(self):
        hosted = routing.HostedEvidence(True, True, True, True, True)
        self.assertEqual(self.route({MAC: READY}, mode="online", hosted=hosted).preanalysis, "jev")

    def test_responses_only_with_all_required_gates(self):
        hosted = routing.HostedEvidence(True, False, True, True, True)
        route = self.route({MAC: READY}, mode="online", hosted=hosted)
        self.assertEqual(route.preanalysis, "responses/gpt-5.4-mini")
        self.assertEqual(route.worker, MAC)
        for gate in ("online_opt_in", "pool_key_verified", "budget_approved", "summary_redacted"):
            with self.subTest(gate=gate):
                altered = replace(hosted, **{gate: False})
                self.assertEqual(self.route({MAC: READY}, mode="online", hosted=altered).preanalysis, "local")

    def test_unknown_jev_status_does_not_call_pool_candidate(self):
        hosted = routing.HostedEvidence(True, None, True, True, True)
        self.assertEqual(self.route({MAC: READY}, mode="online", hosted=hosted).preanalysis, "local")

    def test_no_hosted_preanalysis_if_there_is_no_local_worker(self):
        hosted = routing.HostedEvidence(True, True, True, True, True)
        self.assertEqual(self.route({}, mode="online", hosted=hosted).preanalysis, "none")

    def test_notion_is_tracking_only_even_when_every_route_is_ready(self):
        hosted = routing.HostedEvidence(True, False, True, True, True)
        result = self.route({MAC: READY}, mode="online", hosted=hosted, source="notion")
        self.assertEqual(result, routing.RoutePlan(None, "none", "notion-documentation-only"))

    def test_invalid_inputs_refuse_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            self.route({MAC: READY}, mode="typo")
        with self.assertRaises(ValueError):
            self.route({MAC: READY}, workload="typo")
        with self.assertRaises(ValueError):
            self.route({MAC: READY}, source="untrusted")

    def test_policy_implementation_has_no_io_imports(self):
        tree = ast.parse(Path(routing.__file__).read_text(encoding="utf-8"))
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(imports, {"dataclasses", "math", "typing"})
        self.assertFalse(any(isinstance(n, ast.Import) for n in ast.walk(tree)))
