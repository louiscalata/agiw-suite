"""Deterministic mutation controls and generated command-policy comparisons."""

import test_support.posix_only  # noqa: F401  (skips this module on Windows)

# isort: split
import contextlib
import io
import random
import shlex
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import mem_guard
import test_mem_guard_security as regression

COMMAND_CASES = 8192

# Each replacement removes or reverses one boundary and names its focused witness.
MUTATIONS = (
    (
        "shutdown_signal_callback_fence",
        "        with self._signal_lock:\n            pass\n",
        "        with contextlib.nullcontext():\n            pass\n",
        "test_shutdown_waits_for_signal_callback_already_started",
    ),
    (
        "signal_callback_uses_same_fence",
        "    def _stop_member(self, member: dict, root_pid: int, tree: dict[int, tuple[int, int]]) -> bool:\n"
        "        with self._signal_lock:",
        "    def _stop_member(self, member: dict, root_pid: int, tree: dict[int, tuple[int, int]]) -> bool:\n"
        "        with contextlib.nullcontext():",
        "test_shutdown_waits_for_signal_callback_already_started",
    ),
    (
        "shutdown_closing_flag",
        "        self._closing.set()\n        with self._signal_lock:",
        "        pass\n        with self._signal_lock:",
        "test_shutdown_fences_pause_after_disk_wait_and_lock_timeout",
    ),
    (
        "closing_recheck_after_identity",
        "                self._closing.is_set()\n                or current is None",
        "                False\n                or current is None",
        "test_shutdown_during_fresh_identity_probe_prevents_signal",
    ),
    (
        "identity_uid",
        '                or current is None\n                or current["uid"] != self._uid\n',
        "                or current is None\n",
        "test_replaced_root_identity_is_never_signalled",
    ),
    (
        "identity_start",
        '                or not same_start(member["startTime"], current["startTime"])\n',
        "",
        "test_replaced_root_identity_is_never_signalled",
    ),
    (
        "identity_executable",
        '                or current["comm"] != member["comm"]\n',
        "",
        "test_replaced_root_identity_is_never_signalled",
    ),
    (
        "identity_descendant_parent",
        '                or (member["pid"] != root_pid and current["ppid"] != tree[member["pid"]][0])\n',
        "",
        "test_descendant_parent_is_rechecked_after_previous_signal",
    ),
    (
        "recovery_record_precedes_signal",
        "            self._write_paused(record)\n            pruned = False",
        "            pruned = False",
        "test_identity_is_refreshed_after_write_before_every_signal",
    ),
    (
        "pruned_recovery_record_is_rewritten",
        '                self._write_paused([job for job in record if job["members"]])',
        "                pass",
        "test_descendant_parent_is_rechecked_after_previous_signal",
    ),
    (
        "uncertain_signal_retains_recovery",
        'return self._signal(member["pid"], signal.SIGSTOP) != "gone"',
        'return self._signal(member["pid"], signal.SIGSTOP) == "sent"',
        "test_signal_failure_keeps_recovery_intent_but_proven_exit_is_pruned",
    ),
    (
        "pause_uses_stop_signal",
        'return self._signal(member["pid"], signal.SIGSTOP) != "gone"',
        'return self._signal(member["pid"], signal.SIGCONT) != "gone"',
        "test_identity_is_refreshed_after_write_before_every_signal",
    ),
    (
        "compound_selects_larger_need",
        "HOOK_NEEDS[found[1]][0] > HOOK_NEEDS[strongest[1]][0]",
        "HOOK_NEEDS[found[1]][0] < HOOK_NEEDS[strongest[1]][0]",
        "test_compounds_choose_heaviest_later_command_including_nested_shell",
    ),
    (
        "compound_does_not_stop_at_first_match",
        "if found is not None and (strongest is None or HOOK_NEEDS[found[1]][0] > HOOK_NEEDS[strongest[1]][0]):",
        "if found is not None and strongest is None:",
        "test_compounds_choose_heaviest_later_command_including_nested_shell",
    ),
    (
        "nested_shell_is_classified",
        "return _match_command(args[index + 1], depth + 1)",
        "return None",
        "test_compounds_choose_heaviest_later_command_including_nested_shell",
    ),
    (
        "global_option_values_are_consumed",
        "        if option in value_options:\n            index += 1",
        "        if False:\n            index += 1",
        "test_docker_global_and_compose_option_values_do_not_hide_heavy_commands",
    ),
    (
        "compose_global_options_are_consumed",
        "remaining = _command_args(remaining[1:], _COMPOSE_VALUE_OPTIONS)",
        "remaining = remaining[1:]",
        "test_docker_global_and_compose_option_values_do_not_hide_heavy_commands",
    ),
    (
        "long_global_option_sequence_is_classified",
        'args = [arg.rstrip("`") for arg in tokens[i + 1 :]]',
        'args = [arg.rstrip("`") for arg in tokens[i + 1 : i + 24]]',
        "test_docker_global_and_compose_option_values_do_not_hide_heavy_commands",
    ),
    (
        "model_label_omits_credentials",
        'return ("lms load" if args[:1] == ["load"] else "lms server start"), "model"',
        'return " ".join([base] + args[:3]), "model"',
        "test_fixed_refusal_labels_and_outputs_never_copy_argument_credentials",
    ),
    (
        "build_label_omits_credentials",
        '    if base == "xcodebuild":\n        return "xcodebuild", "build"',
        '    if base == "xcodebuild":\n        return " ".join([base] + args[:3]), "build"',
        "test_fixed_refusal_labels_and_outputs_never_copy_argument_credentials",
    ),
    (
        "review_label_omits_credentials",
        'return "codex exec", "review"',
        'return " ".join([base] + args[:3]), "review"',
        "test_fixed_refusal_labels_and_outputs_never_copy_argument_credentials",
    ),
    (
        "journal_omits_command_arguments",
        '                "label": what,',
        '                "label": payload.get("tool_input", {}).get("command", what),',
        "test_fixed_refusal_labels_and_outputs_never_copy_argument_credentials",
    ),
    (
        "unknown_memory_refuses_guarded_work",
        'decision = dataclasses.replace(decision, allowed=False, reason="memory state unknown; guarded work refused")',
        "return None",
        "test_hook_probe_and_unknown_reading_refuse",
    ),
    (
        "oversized_hook_input_refuses",
        '            raise ValueError("hook input exceeds limit")',
        "            return 0",
        "test_hook_malformed_oversized_or_failed_input_refuses",
    ),
    (
        "hook_registry_failures_propagate",
        "_resume_overdue(ctx, strict=True)",
        "_resume_overdue(ctx, strict=False)",
        "test_hook_registry_or_refusal_output_failure_never_allows",
    ),
    (
        "hook_exception_boundary_refuses",
        '            ctx.stderr.write("mem-guard refused: hook check unavailable; check memory status and retry.\\n")\n'
        "        return 2",
        '            ctx.stderr.write("mem-guard refused: hook check unavailable; check memory status and retry.\\n")\n'
        "        return 0",
        "test_hook_probe_and_unknown_reading_refuse",
    ),
    (
        "hook_refusal_cannot_become_allow",
        "    if decision.allowed:\n        return None\n    gpu_bytes = None",
        "    if True:\n        return None\n    gpu_bytes = None",
        "test_fixed_refusal_labels_and_outputs_never_copy_argument_credentials",
    ),
    (
        "shell_depth_limit_refuses",
        '                    raise ValueError("hook command exceeds shell depth limit")',
        "                    return None",
        "test_nested_shell_depth_exhaustion_refuses_instead_of_hiding_work",
    ),
    (
        "unparseable_guarded_text_refuses",
        "            if may_be_guarded:\n                raise",
        "            if False:\n                raise",
        "test_unparseable_commands_fail_closed_only_when_they_look_guarded",
    ),
    (
        "unparseable_unrelated_text_allows",
        "            if may_be_guarded:\n                raise",
        "            if True:\n                raise",
        "test_unparseable_commands_fail_closed_only_when_they_look_guarded",
    ),
    (
        "eval_and_xargs_operands_are_classified",
        "        return _match_simple(nested, depth + 1)",
        "        return None",
        "test_eval_and_xargs_operands_are_classified_as_nested_commands",
    ),
    (
        "docker_container_is_normalised",
        '        remaining = remaining[1:]\n    if base == "docker" and remaining[:1]',
        '        return None\n    if base == "docker" and remaining[:1]',
        "test_docker_container_compose_run_and_start_forms_are_guarded",
    ),
    (
        "token_limit_does_not_analyze_a_prefix",
        '            raise ValueError("hook command exceeds token limit")',
        "            break",
        "test_hook_shell_parse_errors_and_complexity_limits_refuse",
    ),
    (
        "command_size_limit_refuses",
        '        raise ValueError("hook command exceeds size limit")',
        "        return None",
        "test_hook_shell_parse_errors_and_complexity_limits_refuse",
    ),
    (
        "invalid_payload_shape_refuses",
        '            raise ValueError("invalid hook payload")',
        "            return 0",
        "test_hook_invalid_payload_shapes_refuse_and_valid_unrelated_tools_allow",
    ),
    (
        "invalid_bash_command_shape_refuses",
        '                raise ValueError("invalid hook command")',
        "                return 0",
        "test_hook_invalid_payload_shapes_refuse_and_valid_unrelated_tools_allow",
    ),
    (
        "check_diagnostics_do_not_replace_refusal",
        "        with contextlib.suppress(Exception):\n"
        "            consumers = probes.consumers(models=models) or []\n"
        "            tips = suggestions(consumers, models)\n"
        "            if tips:\n"
        "                decision = dataclasses.replace(decision, suggestion=tips[0])\n"
        "    return decision",
        "        try:\n"
        "            consumers = probes.consumers(models=models) or []\n"
        "            tips = suggestions(consumers, models)\n"
        "            if tips:\n"
        "                decision = dataclasses.replace(decision, suggestion=tips[0])\n"
        "        except Exception:\n"
        "            return admit(need_bytes, kind, None, cfg)\n"
        "    return decision",
        "test_established_refusal_survives_consumer_and_suggestion_exceptions",
    ),
)


@contextlib.contextmanager
def source_module(source):
    """Compile a source variant in memory, restoring all module bindings afterwards."""
    name = "_memory_guard_contract_variant"
    module = types.ModuleType(name)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        exec(compile(source, "<memory-guard-contract>", "exec"), module.__dict__)
        with mock.patch.object(regression, "mem_guard", module):
            yield module
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def run_security(source, method=None):
    with source_module(source):
        suite = (
            unittest.TestSuite([regression.MemGuardSecurityTests(method)])
            if method
            else unittest.defaultTestLoader.loadTestsFromTestCase(regression.MemGuardSecurityTests)
        )
        return unittest.TextTestRunner(stream=io.StringIO()).run(suite)


def mutation_results(source):
    results = []
    for name, before, after, method in MUTATIONS:
        if source.count(before) != 1:
            raise ValueError(f"mutation anchor must be unique: {name}")
        result = run_security(source.replace(before, after, 1), method)
        results.append(
            {
                "name": name,
                "witness": method,
                "tests": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
                "killed": bool(result.failures) and not result.errors,
            }
        )
    return results


def generated_commands(count=COMMAND_CASES):
    """Generate annotated shell grammar; expected needs come from the workload contract."""
    rng = random.Random(672851)
    items = (
        ("lms load fixture", "model", 8),
        ("lms server start", "model", 8),
        ("ollama pull fixture", "model", 8),
        ("python3 -m mlx_lm.generate --model fixture", "model", 8),
        ("xcodebuild -scheme Fixture build", "build", 4),
        ("npm ci", "build", 4),
        ("simctl boot fixture", "simulator", 4),
        ("open -a Simulator", "simulator", 4),
        ("docker --context fixture run image", "docker", 4),
        ("docker --host=fixture compose --file fixture.yml up", "docker", 4),
        ("docker-compose --project-name fixture up", "docker", 4),
        ("codex exec fixture", "review", 1),
        ("astra-review fixture", "review", 1),
        ("printf 'lms load ignored'", None, 0),
        ("docker ps", None, 0),
        ("docker up", None, 0),
        ("xcodebuild --help", None, 0),
        ("npm --version", None, 0),
    )
    wrappers = ("", "env FIXTURE=1 ", "nice -n 5 ", "timeout -s TERM 20 ")
    for _ in range(count):
        chosen = [rng.choice(items) for _ in range(rng.randrange(1, 7))]
        command = ""
        for index, (text, _category, _need) in enumerate(chosen):
            if index:
                command += rng.choice(("; ", " && ", " || ", " | ", "\n"))
            command += rng.choice(wrappers) + text
        if rng.randrange(3) == 0:
            command = "sh -c " + shlex.quote(command)
        if rng.randrange(4) == 0:
            command = "# lms load never-runs\n" + command
        yield command, max(chosen, key=lambda item: item[2])[1]


def differential_results(before_source, after_source):
    counts = {"cases": 0, "before_mismatches": 0, "after_mismatches": 0, "repaired": 0, "regressions": 0}
    with source_module(before_source) as before, source_module(after_source) as after:
        for command, expected in generated_commands():
            old_match = before.hook_match(regression.bash(command))
            new_match = after.hook_match(regression.bash(command))
            old_category = old_match[1] if old_match else None
            new_category = new_match[1] if new_match else None
            old_ok = old_category == expected
            new_ok = new_category == expected
            counts["cases"] += 1
            counts["before_mismatches"] += not old_ok
            counts["after_mismatches"] += not new_ok
            counts["repaired"] += not old_ok and new_ok
            counts["regressions"] += old_ok and not new_ok
    return counts


class MemGuardSecurityControls(unittest.TestCase):
    def test_mutations_are_killed_by_assertions(self):
        source = Path(mem_guard.__file__).read_text()
        baseline = run_security(source)
        self.assertTrue(baseline.wasSuccessful(), "mutation witnesses must first pass unchanged source")
        for result in mutation_results(source):
            with self.subTest(mutation=result["name"], witness=result["witness"]):
                self.assertEqual(result["errors"], 0, "setup/import/runtime errors are not mutation kills")
                self.assertTrue(result["killed"], "the focused witness must reject the variant")

    def test_generated_command_policy_matches_independent_workload_oracle(self):
        cases = 0
        for command, expected in generated_commands():
            actual = mem_guard.hook_match(regression.bash(command))
            with self.subTest(case=cases, command=command):
                self.assertEqual(actual[1] if actual else None, expected)
            cases += 1
        self.assertEqual(cases, COMMAND_CASES)


if __name__ == "__main__":
    unittest.main()
