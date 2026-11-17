"""Reproducible semantic mutation and generated evidence controls.

Run directly with an optional --baseline source file to classify behavioral
changes against an earlier revision. No owner calls or native probes run.
"""

import argparse
import copy
import io
import json
import math
import random
import string
import sys
import unittest
from pathlib import Path
from unittest import mock

import online_code_repair
import test_online_code_repair_security as regressions

MUTATIONS = (
    ("recovery-router-recheck", "                self._fix_nisi_route_idle()\n", "                pass\n"),
    ("inventory-positive", "            and inventory_ok is True\n", "            and True\n"),
    ("ready-pending-conflict", "            and pending is None\n", "            and True\n"),
    ("ready-pending-evidence", '            and "pending" in value\n', "            and True\n"),
    ("ready-exit-conflict", "            and code == 0\n", "            and True\n"),
    ("ready-owner-code", '            and "code" not in value\n', "            and True\n"),
    ("ready-inventory-reason", "            and inventory_reason is None\n", "            and True\n"),
    (
        "receipt-status",
        '            status == "success"\n            and job_id',
        "            True\n            and job_id",
    ),
    ("receipt-job-required", "            and job_id\n", "            and True\n"),
    ("receipt-model-required", "            and model\n", "            and True\n"),
    (
        "receipt-missing-elapsed-default",
        "elapsed = float(elapsed) if _finite(elapsed) and 0 <= elapsed <= 3600 else None",
        "elapsed = float(elapsed) if _finite(elapsed) and 0 <= elapsed <= 3600 else 0",
    ),
    ("receipt-answer-typed", '            and type(evidence.get("answered")) is bool\n', "            and True\n"),
    ("receipt-failure-code", '            and "code" not in evidence\n', "            and True\n"),
    ("receipt-job-grammar", "isinstance(job_id, str) and JOB_ID.fullmatch(job_id)", "isinstance(job_id, str)"),
    ("receipt-model-grammar", 're.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+()/-]{0,95}", model)', "True"),
    ("receipt-negative-elapsed", "_finite(elapsed) and 0 <= elapsed <= 3600", "_finite(elapsed) and elapsed <= 3600"),
    ("receipt-excessive-elapsed", "_finite(elapsed) and 0 <= elapsed <= 3600", "_finite(elapsed) and 0 <= elapsed"),
    (
        "receipt-boolean-elapsed",
        "_finite(elapsed) and 0 <= elapsed <= 3600",
        "isinstance(elapsed, (int, float)) and 0 <= elapsed <= 3600",
    ),
    ("probe-exit-coherence", 'if reply.returncode != (0 if value.get("status") == "success" else 3):', "if False:"),
    (
        "probe-duplicate-keys",
        'value = _strict_json(result.stdout or b"{}")',
        'value = json.loads(result.stdout or b"{}")',
    ),
    (
        "dispatch-intent",
        "                self._inference_started = True",
        "                self._inference_started = False",
    ),
    (
        "cancel-before-dispatch",
        "                _raise_if_cancelled(self._owner_children)\n                self._inference_started",
        "                pass\n                self._inference_started",
    ),
    (
        "cancel-transition",
        'if self._inference_started or self._state["action"] in ("fix-route", "fix-both")',
        "if False",
    ),
    ("cancel-worker", 'if self._inference_started or action in ("fix-route", "fix-both")', "if False"),
    (
        "journal-incomplete-tail",
        'if info.st_size and os.pread(fd, 1, info.st_size - 1) != b"\\n":',
        "if False:",
    ),
    ("journal-append-short-write", "                _write_all(fd, line)", "                os.write(fd, line)"),
    (
        "journal-rotation-short-write",
        "                _write_all(out, tail + line)",
        "                os.write(out, tail + line)",
    ),
    ("journal-zero-progress", 'raise OSError(errno.EIO, "journal write made no progress")', "return"),
    ("share-lock-path-identity", "yield _lock_matches_path(fd, path)", "yield True"),
    ("router-lock-path-identity", "if not _lock_matches_path(fd, path):", "if False:"),
    (
        "nisi-probe-path-identity",
        'if not _lock_matches_path(fd, self._nisi_pending_path.parent / "owner.lock"):',
        "if False:",
    ),
    ("lock-opened-regular", "stat.S_ISREG(opened.st_mode)", "True"),
    ("lock-current-regular", "stat.S_ISREG(current.st_mode)", "True"),
    ("lock-opened-single-link", "opened.st_nlink == current.st_nlink == 1", "current.st_nlink == 1"),
    ("lock-current-single-link", "opened.st_nlink == current.st_nlink == 1", "opened.st_nlink == 1"),
    (
        "lock-device-identity",
        "(opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)",
        "opened.st_ino == current.st_ino",
    ),
    (
        "lock-inode-identity",
        "(opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)",
        "opened.st_dev == current.st_dev",
    ),
    ("lock-no-follow", "os.stat(path, follow_symlinks=False)", "os.stat(path)"),
)


def regression_result(module):
    original = regressions.repair
    regressions.repair = module
    try:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(regressions.RepairSecurityTests)
        return unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    finally:
        regressions.repair = original


def mutation_report():
    source = Path(online_code_repair.__file__).read_text()
    control = regression_result(online_code_repair)
    if not control.wasSuccessful():
        raise AssertionError("unchanged source failed the positive regression control")
    results = []
    for index, (name, old, new) in enumerate(MUTATIONS):
        if source.count(old) != 1:
            raise AssertionError(f"mutation anchor is ambiguous: {name}")
        module = regressions.source_module(source.replace(old, new, 1), f"_repair_security_mutant_{index}")
        try:
            result = regression_result(module)
        finally:
            sys.modules.pop(module.__name__, None)
        if not result.failures or result.errors:
            raise AssertionError(f"mutation lacks a clean assertion witness: {name}; errors={len(result.errors)}")
        results.append({"name": name, "witness": result.failures[0][0].id()})
    return {"mutants": len(results), "killed": len(results), "invalid": 0, "witnesses": results}


def receipt_oracle(value):
    if not isinstance(value, dict) or value.get("status") != "success" or value.get("answered") is not True:
        return False
    if "code" in value:
        return False
    job = value.get("jobId")
    if not isinstance(job, str) or not job.startswith("mac-"):
        return False
    suffix = job[4:]
    if not 12 <= len(suffix) <= 80 or any(char not in string.ascii_letters + string.digits + "-" for char in suffix):
        return False
    model = value.get("model")
    if not isinstance(model, str) or not 1 <= len(model) <= 96:
        return False
    alphabet = string.ascii_letters + string.digits
    if model[0] not in alphabet or any(char not in alphabet + " ._+()/-" for char in model):
        return False
    elapsed = value.get("elapsedSeconds")
    if type(elapsed) not in (int, float) or not 0 <= elapsed <= 3600:
        return False
    return type(elapsed) is int or math.isfinite(elapsed)


def readiness_oracle(code, value):
    inventory = value.get("inventory")
    return (
        code == 0
        and value.get("kind") == "codemode.windows.status.v1"
        and "pending" in value
        and value["pending"] is None
        and value.get("readyForWork") is True
        and isinstance(inventory, dict)
        and inventory.get("ok") is True
        and inventory.get("reason") is None
        and "code" not in value
    )


def receipt_outcome(module, value):
    controller = module.OnlineCodeRepair(lambda *_: None, l2_open=True, inference_probe=lambda: value)
    try:
        controller._prove_windows_inference()
    except module._Stop:
        return False
    return any(step["result"] == "verified" for step in controller.read()["steps"])


def readiness_outcome(module, code, value):
    result = module.CommandResult(code, json.dumps(value).encode())
    controller = module.OnlineCodeRepair(lambda *_: result, l2_open=True)
    try:
        return controller._windows_status()[1]
    except module._Stop:
        return False


def generated_receipts(count):
    randomizer = random.Random(75321)
    faults = {
        "status": [None, "error", "unresolved", True],
        "jobId": [None, "mac-short", "../job", regressions.JOB + "\n", 1],
        "model": [None, "", "name\nsecret", "x" * 97, True],
        "elapsedSeconds": [None, -0.25, 3601, True, float("nan"), float("inf"), "1", 10**400],
        "answered": [None, False, 1, "true", []],
        "code": [None, "TIMEOUT", "PUBLICATION_UNCERTAIN", "unknown"],
    }
    for index in range(count):
        value = {
            **regressions.proof(),
            "jobId": f"mac-generated-{index:012d}",
            "model": f"model-{index:04d}",
            "elapsedSeconds": randomizer.choice([0, 3600, index % 3600, index / 100]),
        }
        if index % 4:
            for _ in range(1 + index % 3):
                field = randomizer.choice(list(faults))
                if field != "code" and randomizer.randrange(5) == 0:
                    value.pop(field, None)
                else:
                    value[field] = randomizer.choice(faults[field])
        yield value


def generated_readiness(count):
    randomizer = random.Random(98273)
    changes = (
        ("inventory", None),
        ("inventory", {}),
        ("inventory", {"ok": False}),
        ("inventory", {"ok": 1}),
        ("inventory", {"ok": True, "reason": "worker is not running"}),
        ("code", "WINDOWS_UNRESOLVED_JOB"),
        ("code", None),
        ("readyForWork", False),
        ("readyForWork", 1),
        ("pending", {"id": regressions.JOB}),
        ("kind", "unknown"),
    )
    for index in range(count):
        value = regressions.windows()
        code = 0
        if index % 4:
            for _ in range(1 + index % 3):
                field, invalid = randomizer.choice(changes)
                value[field] = copy.deepcopy(invalid)
            if randomizer.randrange(5) == 0:
                code = 3
            if randomizer.randrange(7) == 0:
                value.pop("pending", None)
        yield code, value


def differential_report(baseline=None, count=4096):
    report = {
        "receipt_inputs": 0,
        "readiness_inputs": 0,
        "oracle_mismatches": 0,
        "positive_receipts": 0,
        "positive_readiness": 0,
    }
    if baseline is not None:
        report.update(baseline_receipt_changes=0, baseline_readiness_changes=0, unexpected_changes=0)
    for value in generated_receipts(count):
        expected = receipt_oracle(value)
        observed = receipt_outcome(online_code_repair, value)
        if observed != expected:
            raise AssertionError(f"generated receipt oracle disagreement at {report['receipt_inputs']}")
        report["receipt_inputs"] += 1
        report["positive_receipts"] += int(observed)
        if baseline is not None:
            previous = receipt_outcome(baseline, value)
            report["baseline_receipt_changes"] += int(previous != observed)
            report["unexpected_changes"] += int(observed and not previous)
    for code, value in generated_readiness(count):
        expected = readiness_oracle(code, value)
        observed = readiness_outcome(online_code_repair, code, value)
        if observed != expected:
            raise AssertionError(f"generated readiness oracle disagreement at {report['readiness_inputs']}")
        report["readiness_inputs"] += 1
        report["positive_readiness"] += int(observed)
        if baseline is not None:
            previous = readiness_outcome(baseline, code, value)
            report["baseline_readiness_changes"] += int(previous != observed)
            report["unexpected_changes"] += int(observed and not previous)
    return report


def run_controls(baseline_path=None):
    baseline = (
        regressions.source_module(baseline_path.read_text(), "_repair_security_baseline") if baseline_path else None
    )
    with (
        mock.patch.object(online_code_repair.subprocess, "Popen", side_effect=AssertionError("unexpected process")),
        mock.patch.object(online_code_repair.os, "kill", side_effect=AssertionError("unexpected signal")),
        mock.patch.object(online_code_repair.os, "killpg", side_effect=AssertionError("unexpected signal")),
    ):
        return {"mutation": mutation_report(), "differential": differential_report(baseline)}


class RepairSecurityControlTests(unittest.TestCase):
    def test_semantic_mutants_have_assertion_witnesses(self):
        report = mutation_report()
        self.assertEqual(report["killed"], len(MUTATIONS))
        self.assertEqual(report["invalid"], 0)

    def test_generated_evidence_matches_independent_oracles(self):
        report = differential_report()
        self.assertEqual(report["oracle_mismatches"], 0)
        self.assertGreater(report["positive_receipts"], 0)
        self.assertGreater(report["positive_readiness"], 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    arguments = parser.parse_args()
    sys.stdout.write(json.dumps(run_controls(arguments.baseline), indent=2) + "\n")
