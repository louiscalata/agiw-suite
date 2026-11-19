"""Receipt uncertainty and Docker help roles, using only injected runtime boundaries."""

import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mem_guard
import online_code_repair as repair
from test_mem_guard_security import bash, hook_context
from test_online_code_repair_security import JOB, proof

RECEIPT_WITNESSES = (
    b"",
    b"{}",
    b'{"status":"unknown"}',
    b'{"status":"not-run","code":"UNRECOGNIZED"}',
    b'{"status":"not-run","code":"PROBE_FAILED"}',
)
PREPUBLICATION_CODES = (
    "INVALID_TIMEOUT",
    "OWNER_UNAVAILABLE",
    "WORKER_NOT_READY",
    "ROUTER_INSTALL_IN_PROGRESS",
    "ROUTER_INSTALL_ROLLED_BACK",
    "ROUTER_INSTALL_FENCE_INVALID",
    "ROUTER_INSTALL_GENERATION_MISMATCH",
    "ROUTER_INSTALL_CHANGED",
    "ROUTER_INSTALL_FENCE_MISSING",
)


class ReceiptUncertaintyTests(unittest.TestCase):
    def controller(self, probe, **options):
        return repair.OnlineCodeRepair(lambda *_: None, l2_open=True, inference_probe=probe, **options)

    def assert_uncertain(self, controller):
        with self.assertRaises(repair._Stop) as stopped:
            controller._prove_windows_inference()
        self.assertTrue(controller._inference_started)
        self.assertEqual(controller.read()["steps"][-1]["result"], "unresolved")
        for denial in ("was not sent", "no probe was sent", "No job was published", "No model inference was run"):
            self.assertNotIn(denial, stopped.exception.message)
        self.assertIn("uncertain", stopped.exception.message)

    def test_exact_unknown_receipts_preserve_attempt_and_uncertainty(self):
        for raw in RECEIPT_WITNESSES:
            with (
                self.subTest(raw=raw),
                mock.patch.object(repair, "_run_bounded", return_value=repair.CommandResult(3, raw)) as dispatch,
            ):
                self.assert_uncertain(self.controller(repair.windows_inference_probe))
                dispatch.assert_called_once()

    def test_transport_rejects_unknown_status_and_exit_contradictions(self):
        for raw in (b"", b"{}", b"[]", b"null", b"{", b"\xff"):
            with (
                self.subTest(raw=raw),
                mock.patch.object(repair, "_run_bounded", return_value=repair.CommandResult(3, raw)),
            ):
                self.assertEqual(repair.windows_inference_probe(), {"status": "unresolved", "code": "PROBE_FAILED"})
        for status in (None, "unknown", 3, True, [], {}):
            with (
                self.subTest(status=status),
                mock.patch.object(
                    repair,
                    "_run_bounded",
                    return_value=repair.CommandResult(3, json.dumps({"status": status}).encode()),
                ),
            ):
                self.assertEqual(repair.windows_inference_probe()["status"], "unresolved")
        for code, value in ((3, proof()), (0, {"status": "not-run", "code": "WORKER_NOT_READY"})):
            with (
                self.subTest(code=code),
                mock.patch.object(
                    repair, "_run_bounded", return_value=repair.CommandResult(code, json.dumps(value).encode())
                ),
            ):
                self.assert_uncertain(self.controller(repair.windows_inference_probe))

    def test_unrecognized_and_postpublication_errors_remain_uncertain(self):
        values = [None, [], {}, {"status": []}, {"status": "unknown"}, {"status": "error"}]
        values.extend(
            {"status": "not-run", "code": code, "jobId": None}
            for code in ("UNRECOGNIZED", "PROBE_FAILED", "TRANSPORT_ERROR", "RESULT_INVALID", "OWNER_BUSY")
        )
        values.extend(({"status": "timeout"}, {"status": "unresolved", "code": "TIMEOUT", "jobId": JOB}))
        for value in values:
            with self.subTest(value=value):
                probe = mock.Mock(return_value=value)
                controller = self.controller(probe)
                self.assert_uncertain(controller)
                if not isinstance(value, dict) or value.get("status") != "timeout":
                    self.assertNotEqual(controller.read()["steps"][-1]["evidence"], "code=outer-timeout")
                probe.assert_called_once_with()

    def test_safe_code_needs_not_run_status_and_no_contradictory_job_evidence(self):
        for code in PREPUBLICATION_CODES:
            values = [{"code": code}, {"status": "unknown", "code": code}]
            values.extend({"status": "not-run", "code": code, "jobId": job} for job in (JOB, "invalid", "", 3, []))
            values.extend(
                {"status": "not-run", "code": code, field: value}
                for field, value in (("model", "fixture"), ("answered", True))
            )
            for value in values:
                with self.subTest(value=value):
                    self.assert_uncertain(self.controller(lambda value=value: value))

    def test_contract_prepublication_refusals_and_coherent_success_are_preserved(self):
        for code in PREPUBLICATION_CODES:
            value = {"status": "not-run", "code": code}
            with (
                self.subTest(code=code),
                mock.patch.object(
                    repair, "_run_bounded", return_value=repair.CommandResult(3, json.dumps(value).encode())
                ) as dispatch,
            ):
                controller = self.controller(repair.windows_inference_probe)
                with self.assertRaises(repair._Stop) as stopped:
                    controller._prove_windows_inference()
                self.assertEqual(controller.read()["steps"][-1]["result"], "not-run")
                self.assertIn("No job was published", stopped.exception.message)
                dispatch.assert_called_once()
        with mock.patch.object(
            repair, "_run_bounded", return_value=repair.CommandResult(0, json.dumps(proof()).encode())
        ):
            controller = self.controller(repair.windows_inference_probe)
            self.assertIn("verified end to end", controller._prove_windows_inference())
            self.assertEqual(controller.read()["steps"][-1]["jobId"], JOB)
        controller = self.controller(lambda: {**proof(), "answered": False})
        with self.assertRaises(repair._Stop):
            controller._prove_windows_inference()
        self.assertEqual(controller.read()["steps"][-1]["result"], "returned")

    def test_locally_known_predispatch_refusals_never_call_probe(self):
        for open_jobs, router_free in ((1, True), (None, True), (0, False)):
            with self.subTest(open_jobs=open_jobs, router_free=router_free):
                probe = mock.Mock(return_value=proof())
                share = SimpleNamespace(
                    open_jobs=lambda open_jobs=open_jobs: open_jobs,
                    hold_router=lambda router_free=router_free: contextlib.nullcontext(router_free),
                )
                controller = self.controller(probe, share=share)
                with self.assertRaises(repair._Stop) as stopped:
                    controller._prove_windows_inference()
                probe.assert_not_called()
                self.assertFalse(controller._inference_started)
                self.assertEqual(controller.read()["steps"][-1]["result"], "deferred")
                self.assertIn("no probe was sent", stopped.exception.message)


class DockerHelpRoleTests(unittest.TestCase):
    def test_container_help_and_option_values_still_require_memory_admission(self):
        for command in (
            "docker run image --help",
            'docker run image sh -c "echo --help"',
            "docker run -- image --help",
            "docker run -- --help",
            "docker run --env --help image",
            "docker run --env=--help image",
            "docker run -h --help image",
            "docker run -it --hostname --help image",
            "docker --context --help run image",
            "docker -DH --help run image",
            "docker --host --help run image",
            "docker compose --file --help up",
            "docker-compose --env-file --help up",
            "docker compose up --attach --help",
            "docker compose up fixture --attach --help",
            "docker compose up fixture -- --help",
            "docker compose up -- --help",
        ):
            with self.subTest(command=command):
                ctx = hook_context(bash(command))
                match = mem_guard.hook_match(bash(command))
                self.assertIsNotNone(match)
                self.assertEqual(match[1], "docker")
                self.assertEqual(mem_guard._cmd_hook(ctx), 2)
                ctx.probes.read.assert_called_once_with()

    def test_actual_client_help_and_unrelated_docker_commands_do_not_probe(self):
        for command in (
            "docker --help",
            "docker --debug --help",
            "docker --context fixture --help",
            "docker run --help",
            "docker run --rm --help",
            "docker run --rm=false -it --help",
            "docker run --hostname fixture --help",
            "docker --context fixture run --help",
            "docker run --context fixture --help",
            "docker -DH fixture run --help",
            "docker compose --help",
            "docker compose --file fixture.yml --help",
            "docker compose up --help",
            "docker compose up --detach --help",
            "docker compose up fixture --help",
            "docker-compose --help",
            "docker-compose up --help",
            "docker ps",
            "docker --context fixture ps --help",
            "lms load --help",
        ):
            with self.subTest(command=command):
                ctx = hook_context(bash(command))
                self.assertIsNone(mem_guard.hook_match(bash(command)))
                self.assertEqual(mem_guard._cmd_hook(ctx), 0)
                ctx.probes.read.assert_not_called()
                self.assertEqual(ctx.stderr.getvalue(), "")

    def test_unknown_or_missing_option_roles_fail_closed_without_leaking_values(self):
        secret = "fixture-credential-2718"
        for command in (
            f"docker --unknown {secret} run --help",
            f"docker run --unknown {secret} --help",
            f"docker compose --unknown {secret} up --help",
            f"docker compose up --unknown {secret} --help",
            "docker --context",
            "docker run --env",
            "docker compose --file",
        ):
            with self.subTest(command=command):
                ctx = hook_context(bash(command))
                self.assertEqual(mem_guard._cmd_hook(ctx), 2)
                self.assertNotIn(secret, ctx.stderr.getvalue())
        command = f"docker --context {secret} run image --help"
        ctx = hook_context(bash(command))
        self.assertEqual(mem_guard._cmd_hook(ctx), 2)
        ctx.probes.read.assert_called_once_with()
        self.assertNotIn(secret, ctx.stderr.getvalue())
        self.assertNotIn(secret, json.dumps(ctx.journal.write.call_args_list, default=str))
        self.assertEqual(mem_guard.hook_match(bash(command)), ("docker run", "docker"))

    def test_compound_help_keeps_heaviest_workload_and_probe_errors_refuse(self):
        for command in (
            "docker run --help; lms load fixture",
            "docker run image --help; lms load fixture",
            "codex exec fixture; docker run image --help",
            "sh -c 'docker run --help; lms load fixture'",
        ):
            with self.subTest(command=command):
                expected = "model" if "lms" in command else "docker"
                self.assertEqual(mem_guard.hook_match(bash(command))[1], expected)
                ctx = hook_context(bash(command))
                self.assertEqual(mem_guard._cmd_hook(ctx), 2)
                ctx.probes.read.assert_called_once_with()
        ctx = hook_context(bash("docker run image --help"))
        ctx.probes.read.side_effect = RuntimeError("fixture-credential-2718")
        self.assertEqual(mem_guard._cmd_hook(ctx), 2)
        ctx.probes.read.assert_called_once_with()
        self.assertNotIn("fixture-credential-2718", ctx.stderr.getvalue())


@contextlib.contextmanager
def source_variant(source, target):
    """Load a temporary in-memory source variant and restore its module binding."""
    name = "_receipt_help_source_control"
    module = types.ModuleType(name)
    module.__file__ = target.__file__
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        with mock.patch.dict(globals(), {"mem_guard" if target is mem_guard else "repair": module}):
            yield module
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def mutation_results():
    """Assertion failures count as kills; errors and passing variants are separate."""
    variants = (
        (
            mem_guard,
            DockerHelpRoleTests,
            "test_container_help_and_option_values_still_require_memory_admission",
            "anywhere-help-exemption",
            '    if base in ("docker", "docker-compose"):\n        return _docker_match(base, args)',
            '    if base in ("docker", "docker-compose"):\n'
            '        return None if "--help" in args else _docker_match(base, args)',
        ),
        (
            mem_guard,
            DockerHelpRoleTests,
            "test_container_help_and_option_values_still_require_memory_admission",
            "option-value-as-help",
            '        if name in value_options:\n            if "=" not in option:',
            "        if name in value_options:\n            if False:",
        ),
        (
            mem_guard,
            DockerHelpRoleTests,
            "test_container_help_and_option_values_still_require_memory_admission",
            "help-after-image-or-separator",
            '        if option == "--":\n            return False',
            '        if option == "--":\n            return "--help" in args[index + 1 :]',
        ),
        (
            mem_guard,
            DockerHelpRoleTests,
            "test_container_help_and_option_values_still_require_memory_admission",
            "help-after-image",
            "            if not interspersed:\n                return False",
            '            if not interspersed:\n                return "--help" in args[index + 1 :]',
        ),
        (
            mem_guard,
            DockerHelpRoleTests,
            "test_actual_client_help_and_unrelated_docker_commands_do_not_probe",
            "client-help-refused",
            '        if option == "--help":\n            return True',
            '        if option == "--help":\n            return False',
        ),
        (
            mem_guard,
            DockerHelpRoleTests,
            "test_actual_client_help_and_unrelated_docker_commands_do_not_probe",
            "compose-service-hides-client-help",
            '    "up": (_COMPOSE_UP_VALUE_OPTIONS, _COMPOSE_UP_FLAG_OPTIONS, True),',
            '    "up": (_COMPOSE_UP_VALUE_OPTIONS, _COMPOSE_UP_FLAG_OPTIONS, False),',
        ),
        (
            mem_guard,
            DockerHelpRoleTests,
            "test_unknown_or_missing_option_roles_fail_closed_without_leaking_values",
            "unknown-option-help-allowed",
            "            # Unknown arity cannot establish that a later token is client help.\n"
            '            raise ValueError("unknown Docker option")',
            "            return True",
        ),
        (
            repair,
            ReceiptUncertaintyTests,
            "test_transport_rejects_unknown_status_and_exit_contradictions",
            "unknown-exit-three-status-accepted",
            '        if value.get("status") not in ("success", "error", "not-run", "unresolved"):',
            "        if False:",
        ),
        (
            repair,
            ReceiptUncertaintyTests,
            "test_exact_unknown_receipts_preserve_attempt_and_uncertainty",
            "unknown-not-run-denies-publication",
            "        if not prepublication:",
            '        if not prepublication and status != "not-run":',
        ),
        (
            repair,
            ReceiptUncertaintyTests,
            "test_safe_code_needs_not_run_status_and_no_contradictory_job_evidence",
            "safe-code-without-status",
            '            status == "not-run"\n            and code in PROBE_PREPUBLICATION_CODES',
            "            True\n            and code in PROBE_PREPUBLICATION_CODES",
        ),
        (
            repair,
            ReceiptUncertaintyTests,
            "test_safe_code_needs_not_run_status_and_no_contradictory_job_evidence",
            "malformed-job-discarded",
            '            and evidence.get("jobId") is None',
            "            and job_id is None",
        ),
    )
    results = []
    for target, case, method, name, before, after in variants:
        source = Path(target.__file__).read_text()
        if source.count(before) != 1:
            raise AssertionError(f"source control anchor is not unique: {name}")
        with source_variant(source.replace(before, after, 1), target):
            result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([case(method)]))
        results.append(
            {
                "name": name,
                "failures": len(result.failures),
                "errors": len(result.errors),
                "equivalent": result.wasSuccessful(),
            }
        )
    return results


def differential_results(before_guard_source, before_repair_source):
    """Compare the focused behavioral assertions against two supplied earlier sources."""
    results = {}
    for label, target, case, source in (
        ("guard", mem_guard, DockerHelpRoleTests, before_guard_source),
        ("repair", repair, ReceiptUncertaintyTests, before_repair_source),
    ):
        row = {}
        for revision, candidate in (("before", source), ("after", Path(target.__file__).read_text())):
            with source_variant(candidate, target):
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
                result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
            row[revision] = {
                "tests": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
                "skips": len(result.skipped),
            }
        results[label] = row
    return results


class RepairedBranchControlTests(unittest.TestCase):
    def test_negative_source_controls_fail_assertions_without_errors(self):
        for row in mutation_results():
            with self.subTest(control=row["name"]):
                self.assertEqual(row["errors"], 0)
                self.assertGreater(row["failures"], 0)
                self.assertFalse(row["equivalent"])


if __name__ == "__main__":
    unittest.main()
