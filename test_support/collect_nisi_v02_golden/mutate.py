"""Mutate only the collector in memory and require a failing new test witness.

The repository source is never written. Corpus comparison finds a witness,
then the exact new unittest method must also fail with that mutant installed.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]
CORPUS = Path(__file__).with_name("cases.json")
TEST_MODULES = ("test_collect_nisi_v02_golden", "test_collect_nisi_v02_generation_golden")


def evidence_raises(function_source):
    found = {}
    for node in ast.walk(ast.parse(function_source)):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        if not isinstance(node.exc.func, ast.Name) or node.exc.func.id != "_InvalidEvidence":
            continue
        label = node.exc.args[0].value
        found.setdefault(label, ast.get_source_segment(function_source, node))
    return found.items()


def catalog(function_source):
    """Keep mutation intent explicit, including proved source-level equivalents."""
    specifications = []

    def add(name, old, new, occurrence=0, equivalent_reason=None):
        starts = []
        position = 0
        while True:
            position = function_source.find(old, position)
            if position < 0:
                break
            starts.append(position)
            position += len(old)
        if not starts or occurrence is not None and occurrence >= len(starts):
            raise RuntimeError(f"Mutation target absent: {name}: {old!r}, occurrence {occurrence}")
        if occurrence is None:
            mutated = function_source.replace(old, new)
        else:
            start = starts[occurrence]
            mutated = function_source[:start] + new + function_source[start + len(old) :]
        tree = ast.parse(mutated, filename="<collector-mutant>")
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            raise RuntimeError(f"Mutation escaped function scope: {name}")
        specifications.append(
            {
                "name": name,
                "old": old,
                "new": new,
                "occurrence": occurrence,
                "equivalent_reason": equivalent_reason,
                "tree": tree,
            }
        )

    add("numeric_type_guard_flip", "not isinstance(now, (int, float))", "isinstance(now, (int, float))")
    add("finite_guard_flip", "not math.isfinite(now)", "math.isfinite(now)")
    add(
        "timestamp_exception_type",
        'raise ValueError("finite timestamp required")',
        'raise TypeError("finite timestamp required")',
    )
    add("home_default_branch_flip", "if home is None else Path(home)", "if home is not None else Path(home)")
    add("snapshot_schema_constant", '"schemaVersion": 1', '"schemaVersion": 2')
    add("initial_live_inference_constant", '"liveInference": "UNKNOWN"', '"liveInference": "IDLE"')
    add(
        "initial_release_acceptance_constant",
        '"releaseAcceptance": "NOT_ESTABLISHED"',
        '"releaseAcceptance": "VERIFIED"',
    )
    add("drop_home_ownership", "_owned(home, directory=True)", "pass")
    add("drop_local_ownership", '_owned(home / ".local", directory=True)', "pass")
    add("drop_share_ownership", '_owned(home / ".local/share", directory=True)', "pass")
    add(
        "swap_home_directory_check_order",
        '_owned(home / ".local", directory=True)\n        _owned(home / ".local/share", directory=True)',
        '_owned(home / ".local/share", directory=True)\n        _owned(home / ".local", directory=True)',
    )
    add("absent_receipt_branch_flip", "if receipt_path is None:", "if receipt_path is not None:")
    for delta in (-1, 1):
        add(f"receipt_limit_{delta:+d}", "_json(receipt_path, 65536)", f"_json(receipt_path, {65536 + delta})")
    add("generation_dispatch_flip", "if generation is not None:", "if generation is None:")
    add("generation_commit_guard_flip", "commit is not None", "commit is None")
    add(
        "generation_kind_constant",
        'receipt.get("kind") != "nisi.runtime.activation.v2"',
        'receipt.get("kind") != "nisi.runtime.activation.v1"',
    )
    add("generation_type_guard_flip", "not isinstance(generation, str)", "isinstance(generation, str)")
    add("generation_digest_guard_flip", "not _COMMIT.fullmatch(generation)", "_COMMIT.fullmatch(generation)")
    add("generation_rollback_prefix", '"rollback-" + generation[:8] + "-"', '"rollback-" + generation[:7] + "-"')
    add(
        "generation_rollback_prefix_plus_one",
        '"rollback-" + generation[:8] + "-"',
        '"rollback-" + generation[:9] + "-"',
    )
    add("generation_drop_runtime_ownership", "_owned(runtime, directory=True)", "pass", occurrence=0)
    add("commit_drop_runtime_ownership", "_owned(runtime, directory=True)", "pass", occurrence=1)
    for occurrence, mode in enumerate(("generation", "commit")):
        add(
            f"{mode}_root_comparison_flip",
            'receipt.get("runtimeRoot") != str(runtime)',
            'receipt.get("runtimeRoot") == str(runtime)',
            occurrence,
        )
    for delta in (-1, 1):
        add(
            f"generation_manifest_limit_{delta:+d}",
            '_bytes(runtime / "INSTALL-MANIFEST.json", 16384)',
            f'_bytes(runtime / "INSTALL-MANIFEST.json", {16384 + delta})',
        )
        add(
            f"commit_manifest_limit_{delta:+d}",
            '_json(runtime / "INSTALL-MANIFEST.json", 16384)',
            f'_json(runtime / "INSTALL-MANIFEST.json", {16384 + delta})',
        )
    add("manifest_digest_type_flip", "not isinstance(manifest_sha, str)", "isinstance(manifest_sha, str)")
    add("manifest_digest_format_flip", "not _SHA.fullmatch(manifest_sha)", "_SHA.fullmatch(manifest_sha)")
    add(
        "manifest_digest_comparison_flip",
        "hashlib.sha256(manifest_raw).hexdigest() != manifest_sha",
        "hashlib.sha256(manifest_raw).hexdigest() == manifest_sha",
    )
    add(
        "generation_manifest_identity_flip",
        'manifest.get("generation") != generation',
        'manifest.get("generation") == generation',
    )
    add(
        "generation_manifest_schema_constant",
        'manifest.get("schemaVersion") != 2',
        'manifest.get("schemaVersion") != 3',
    )
    add(
        "generation_manifest_schema_constant_minus_one",
        'manifest.get("schemaVersion") != 2',
        'manifest.get("schemaVersion") != 1',
    )
    add("commit_manifest_identity_flip", 'manifest.get("commit") != commit', 'manifest.get("commit") == commit')
    for occurrence, mode in enumerate(("generation", "commit")):
        add(f"{mode}_pins_type_flip", "not isinstance(pins, dict)", "isinstance(pins, dict)", occurrence)
        add(
            f"{mode}_required_subset_flip",
            "not _RUNTIME_FILES.issubset(pins)",
            "_RUNTIME_FILES.issubset(pins)",
            occurrence,
        )
        for delta in (-1, 1):
            add(
                f"{mode}_pin_upper_limit_{delta:+d}",
                "not 1 <= len(pins) <= 64",
                f"not 1 <= len(pins) <= {64 + delta}",
                occurrence,
            )
        add(f"{mode}_pin_lower_limit_five", "not 1 <= len(pins) <= 64", "not 5 <= len(pins) <= 64", occurrence)
        add(
            f"{mode}_lower_bound_removed_equivalent",
            "not 1 <= len(pins) <= 64",
            "not len(pins) <= 64",
            occurrence,
            "The preceding required-file subset guard guarantees at least four dictionary keys; the lower-bound rejection cannot be reached.",
        )
        add(
            f"{mode}_lower_bound_four_equivalent",
            "not 1 <= len(pins) <= 64",
            "not 4 <= len(pins) <= 64",
            occurrence,
            "The preceding required-file subset guard guarantees at least four dictionary keys, so this lower bound admits exactly the same reachable dictionaries.",
        )
        add(
            f"{mode}_lower_bound_zero_equivalent",
            "not 1 <= len(pins) <= 64",
            "not 0 <= len(pins) <= 64",
            occurrence,
            "The preceding required-file subset guard guarantees at least four dictionary keys; decreasing the lower bound by one cannot affect a reachable input.",
        )
        add(
            f"{mode}_lower_bound_two_equivalent",
            "not 1 <= len(pins) <= 64",
            "not 2 <= len(pins) <= 64",
            occurrence,
            "The preceding required-file subset guard guarantees at least four dictionary keys; increasing the lower bound by one cannot affect a reachable input.",
        )
    add("commit_type_guard_flip", "not isinstance(commit, str)", "isinstance(commit, str)")
    add("commit_digest_guard_flip", "not _COMMIT.fullmatch(commit)", "_COMMIT.fullmatch(commit)")
    add("commit_rollback_prefix", '"rollback-" + commit[:8] + "-"', '"rollback-" + commit[:7] + "-"')
    add("commit_rollback_prefix_plus_one", '"rollback-" + commit[:8] + "-"', '"rollback-" + commit[:9] + "-"')
    add("archive_sha_type_flip", "not isinstance(archive_sha, str)", "isinstance(archive_sha, str)")
    add("archive_sha_format_flip", "not _SHA.fullmatch(archive_sha)", "_SHA.fullmatch(archive_sha)")
    add(
        "archive_receipt_comparison_flip",
        'receipt.get("runtimeArchiveSha256") != archive_sha',
        'receipt.get("runtimeArchiveSha256") == archive_sha',
    )
    add(
        "archive_contents_comparison_flip",
        '_sha(base / f"{commit}.tar", 8 * 1024 * 1024) != archive_sha',
        '_sha(base / f"{commit}.tar", 8 * 1024 * 1024) == archive_sha',
    )
    for delta in (-1, 1):
        add(
            f"archive_read_limit_{delta:+d}",
            'f"{commit}.tar", 8 * 1024 * 1024',
            f'f"{{commit}}.tar", {8 * 1024 * 1024 + delta}',
        )
        add(
            f"runtime_file_read_limit_{delta:+d}",
            "_sha(_relative_file(runtime, name), 2 * 1024 * 1024)",
            f"_sha(_relative_file(runtime, name), {2 * 1024 * 1024 + delta})",
        )
        add(
            f"host_file_read_limit_{delta:+d}",
            "_sha(_relative_file(home, relative), 2 * 1024 * 1024)",
            f"_sha(_relative_file(home, relative), {2 * 1024 * 1024 + delta})",
        )
        add(
            f"package_read_limit_{delta:+d}",
            '_json(runtime / "package.json", 16384)',
            f'_json(runtime / "package.json", {16384 + delta})',
        )
    add("runtime_pin_type_flip", "not isinstance(expected, str)", "isinstance(expected, str)")
    add("runtime_pin_format_flip", "not _SHA.fullmatch(expected)", "_SHA.fullmatch(expected)")
    add(
        "runtime_contents_comparison_flip",
        "_sha(_relative_file(runtime, name), 2 * 1024 * 1024) != expected",
        "_sha(_relative_file(runtime, name), 2 * 1024 * 1024) == expected",
    )
    add(
        "reverse_runtime_pin_order",
        "for name, expected in pins.items():",
        "for name, expected in reversed(list(pins.items())):",
    )
    add("package_name_comparison_flip", 'package.get("name") != "nisi"', 'package.get("name") == "nisi"')
    add(
        "package_version_constant",
        'package.get("version") != "0.2.0-private.0"',
        'package.get("version") != "0.2.1-private.0"',
    )
    add("package_private_identity_flip", 'package.get("private") is not True', 'package.get("private") is True')
    add(
        "package_private_equal_instead_of_identity",
        'package.get("private") is not True',
        'package.get("private") != True',
    )
    add("swap_snapshot_commit", 'snapshot["commit"] = commit', 'snapshot["commit"] = generation')
    add("swap_snapshot_generation", 'snapshot["generation"] = generation', 'snapshot["generation"] = commit')
    add(
        "runtime_integrity_assignment",
        'snapshot["runtimeIntegrity"] = "VERIFIED"',
        'snapshot["runtimeIntegrity"] = "UNKNOWN"',
    )
    add(
        "activation_and_to_or",
        'receipt.get("status") == "VERIFIED" and stamp',
        'receipt.get("status") == "VERIFIED" or stamp',
    )
    add(
        "activation_drop_timestamp_requirement",
        'receipt.get("status") == "VERIFIED" and stamp',
        'receipt.get("status") == "VERIFIED"',
    )
    add("activation_drop_status_requirement", 'receipt.get("status") == "VERIFIED" and stamp', "stamp")
    add("drop_activation_probe", '_probe(receipt, now) if snapshot["activationStatus"] == "VERIFIED" else None', "None")
    add(
        "probe_even_without_verified_activation",
        '_probe(receipt, now) if snapshot["activationStatus"] == "VERIFIED" else None',
        "_probe(receipt, now)",
    )
    add("host_pins_type_flip", "not isinstance(files, dict)", "isinstance(files, dict)")
    add("host_pin_key_set_comparison_flip", "set(files) != set(_HOST_FILES)", "set(files) == set(_HOST_FILES)")
    add(
        "reverse_host_pin_iteration",
        "for name, relative in _HOST_FILES.items():",
        "for name, relative in reversed(list(_HOST_FILES.items())):",
    )
    add("host_pin_type_flip", "not isinstance(pin, dict)", "isinstance(pin, dict)")
    add(
        "host_installed_path_comparison_flip",
        'pin.get("installedPath") != str(expected_path)',
        'pin.get("installedPath") == str(expected_path)',
    )
    add("host_sha_type_flip", 'not isinstance(pin.get("afterSha256"), str)', 'isinstance(pin.get("afterSha256"), str)')
    add("host_sha_format_flip", 'not _SHA.fullmatch(pin["afterSha256"])', '_SHA.fullmatch(pin["afterSha256"])')
    add(
        "host_read_caught_exception_type",
        "except _InvalidEvidence:\n                current = None",
        "except ValueError:\n                current = None",
    )
    add("host_contents_comparison_flip", 'current != pin["afterSha256"]', 'current == pin["afterSha256"]')
    add("reverse_drift_accumulation", "drift.append(name)", "drift.insert(0, name)")
    add("drop_drift_result", 'snapshot["driftedHostFiles"] = drift', 'snapshot["driftedHostFiles"] = []')
    add("host_binding_branch_flip", '"DRIFT" if drift else "VERIFIED"', '"DRIFT" if not drift else "VERIFIED"')
    add(
        "activation_source_branch_flip",
        'if snapshot["activationStatus"] != "VERIFIED":',
        'if snapshot["activationStatus"] == "VERIFIED":',
    )
    add("drift_source_branch_flip", "elif drift:", "elif not drift:")
    add("recorded_source_state", 'state="recorded",', 'state="unavailable",')
    add("outer_caught_exception_type", "except _InvalidEvidence as exc:", "except ValueError as exc:")
    add("missing_evidence_state_comparison_flip", 'str(exc) != "MISSING"', 'str(exc) == "MISSING"')
    add("swap_final_result_order", "return snapshot, source", "return source, snapshot", occurrence=1)
    for label, old in evidence_raises(function_source):
        add(f"evidence_exception_type_{label.lower()}", old, old.replace("_InvalidEvidence", "ValueError", 1))
    add(
        "nonmissing_evidence_message_equivalent",
        'raise _InvalidEvidence("INVALID_GENERATION")',
        'raise _InvalidEvidence("DIFFERENT_INVALID_GENERATION")',
        equivalent_reason="The function catches either non-MISSING evidence message and exposes the same generic error state and detail; the direct raise message is not returned or recorded in helper calls.",
    )
    add("generation_dispatch_truthiness", "if generation is not None:", "if generation:")
    add("archive_sha_format_removed", "or not _SHA.fullmatch(archive_sha)", "")
    add(
        "runtime_pin_string_coercion",
        "not isinstance(expected, str) or not _SHA.fullmatch(expected)",
        "not _SHA.fullmatch(str(expected))",
    )
    add(
        "identity_match_equivalent",
        "_COMMIT.fullmatch(",
        "_COMMIT.match(",
        occurrence=None,
        equivalent_reason="The identity pattern ends with an absolute end anchor, so match and fullmatch accept the same strings in both receipt arms.",
    )
    add(
        "manifest_digest_format_removed_equivalent",
        "or not _SHA.fullmatch(manifest_sha)",
        "",
        equivalent_reason="The following equality against hashlib.sha256(...).hexdigest() already requires a lowercase 64-character hexadecimal string.",
    )
    for mode in ("generation", "commit"):
        add(
            f"{mode}_rollback_separator_removed_equivalent",
            f'"rollback-" + {mode}[:8] + "-"',
            f'"rollback-" + {mode}[:8]',
            equivalent_reason="The receipt selector admits only rollback folders with eight lowercase hexadecimal digits followed by a separator. The remaining prefix check still requires those same eight identity digits.",
        )
    return specifications


def expected(case):
    return case["expected"]


def all_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from all_tests(item)
        else:
            yield item


def witness_test(case, tests):
    module_name = TEST_MODULES[case["mode"] == "generation"]
    matches = [
        test
        for test in tests
        if test._testMethodName == "test_" + case["name"] and type(test).__module__ == module_name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one new test for {case['name']}, found {len(matches)}")
    return type(matches[0])("test_" + case["name"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="Validate and list mutation definitions without loading tests"
    )
    parser.add_argument("--output", type=Path, help="JSON report destination; required unless --list is used")
    options = parser.parse_args()
    if not options.list and options.output is None:
        parser.error("--output is required unless --list is used")
    for name in list(os.environ):
        if name.startswith("GIT_CONFIG_"):
            del os.environ[name]
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    document = json.loads(CORPUS.read_text(encoding="utf-8"))
    provenance = document["generated_from"]
    base = provenance["revision"]
    path = WORKTREE / "nisi_v02.py"
    source = path.read_bytes()
    base_source = subprocess.check_output(["git", "-C", str(WORKTREE), "show", f"{base}:nisi_v02.py"])
    if source != base_source or hashlib.sha256(source).hexdigest() != provenance["sha256"]:
        raise RuntimeError("Source differs from the pinned main blob")
    text = source.decode()
    target = next(
        node for node in ast.parse(text).body if isinstance(node, ast.FunctionDef) and node.name == "collect_nisi_v02"
    )
    function_source = ast.get_source_segment(text, target)
    specifications = catalog(function_source)
    if options.list:
        sys.stdout.write(
            json.dumps({"mutations": [spec["name"] for spec in specifications], "total": len(specifications)}, indent=2)
            + "\n"
        )
        return 0
    sys.path.insert(0, str(WORKTREE))
    module = importlib.import_module("nisi_v02")
    support = importlib.import_module("test_support.collect_nisi_v02_golden.support")
    cases = document["cases"]
    tests = list(all_tests(unittest.defaultTestLoader.loadTestsFromNames(TEST_MODULES)))
    original = module.collect_nisi_v02
    report = {
        "base_revision": base,
        "interpreter_version": sys.version,
        "source_sha256_before": hashlib.sha256(source).hexdigest(),
        "case_count": len(cases),
        "new_test_count": len(tests),
        "mutations": [],
    }
    # Small ordinary cases find most witnesses without repeatedly creating
    # maximum-sized byte-limit fixtures. The corpus itself stays boundary-first.
    ordered = sorted(cases, key=lambda case: (any(key.endswith("_size") for key in case), len(case.get("faults", []))))
    try:
        for case in ordered:
            if support.run_case(module, case) != expected(case):
                raise RuntimeError(f"Unmodified source does not match case {case['name']}")
        report["baseline_cases_matched"] = len(cases)
        for index, specification in enumerate(specifications, start=1):
            namespace = dict(module.__dict__)
            exec(compile(copy.deepcopy(specification["tree"]), "<collector-mutant>", "exec"), namespace)
            mutated = namespace["collect_nisi_v02"]
            # The replacement must resolve patched helper globals in the same
            # namespace used by run_case, rather than copied pre-patch helpers.
            import types

            module.collect_nisi_v02 = types.FunctionType(
                mutated.__code__, module.__dict__, mutated.__name__, mutated.__defaults__
            )
            module.collect_nisi_v02.__kwdefaults__ = mutated.__kwdefaults__
            witness = None
            for case in ordered:
                try:
                    actual = support.run_case(module, case)
                    mismatch = actual != expected(case)
                except (
                    AssertionError,
                    AttributeError,
                    KeyError,
                    OSError,
                    OverflowError,
                    TypeError,
                    ValueError,
                    module._InvalidEvidence,
                ):
                    mismatch = True
                if mismatch:
                    witness = case
                    break
            item = {key: value for key, value in specification.items() if key != "tree"}
            if witness is None:
                item["status"] = "equivalent" if specification["equivalent_reason"] else "survived"
            else:
                test = witness_test(witness, tests)
                result = unittest.TestResult()
                test.run(result)
                item["witness_case"] = witness["name"]
                item["witness_test"] = test.id()
                item["witness_failures"] = len(result.failures)
                item["witness_errors"] = len(result.errors)
                item["status"] = "killed" if not result.wasSuccessful() else "witness_not_reproduced"
                if specification["equivalent_reason"]:
                    item["status"] = "claimed_equivalent_changed"
            report["mutations"].append(item)
            module.collect_nisi_v02 = original
            if index % 10 == 0:
                progress = {
                    "mutation_progress": index,
                    "total": len(specifications),
                    "status_counts": {
                        status: sum(entry["status"] == status for entry in report["mutations"])
                        for status in (
                            "killed",
                            "equivalent",
                            "survived",
                            "witness_not_reproduced",
                            "claimed_equivalent_changed",
                        )
                    },
                }
                sys.stdout.write(json.dumps(progress, sort_keys=True) + "\n")
                sys.stdout.flush()
        report["counts"] = {
            status: sum(item["status"] == status for item in report["mutations"])
            for status in ("killed", "equivalent", "survived", "witness_not_reproduced", "claimed_equivalent_changed")
        }
    finally:
        module.collect_nisi_v02 = original
        after = path.read_bytes()
        report["source_sha256_after"] = hashlib.sha256(after).hexdigest()
        report["source_unchanged"] = after == source == base_source
        options.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(
        json.dumps(
            {
                "counts": report["counts"],
                "baseline_cases_matched": report["baseline_cases_matched"],
                "source_unchanged": report["source_unchanged"],
                "report": str(options.output),
            },
            indent=2,
        )
        + "\n"
    )
    counts = report["counts"]
    return (
        0
        if report["source_unchanged"]
        and counts["killed"] >= 40
        and not any(counts[key] for key in ("survived", "witness_not_reproduced", "claimed_equivalent_changed"))
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
