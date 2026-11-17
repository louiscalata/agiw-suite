"""Deterministic recovery/readiness differential and guard mutation witnesses.

Run from the repository root with:
    python3 -B -m test_support.telemetry_security_harness

The differential compares production projections with a small contract oracle.
Mutants execute in memory and never change a production file.
"""

import ast
import contextlib
import hashlib
import io
import itertools
import json
import random
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import telemetry
import test_telemetry_security

_ROOT = Path(__file__).resolve().parents[1]


def _readiness_oracle(pipeline, pins, loaded, inventory_complete, models_complete):
    if pipeline.get("pendingMarkerAttributedTo"):
        return "in-use"
    if any(pipeline.get(key) for key in ("pendingMarkerObserved", "pendingMarkerUnreadable")):
        return "unresolved"
    if pins == {"runtimeIntegrity": "VERIFIED", "hostBinding": "VERIFIED"}:
        if not (inventory_complete and models_complete):
            return "unknown"
        return "ready" if len(loaded) >= 2 else "partial"
    return "needs-action" if pins["hostBinding"] == "DRIFT" else "unknown"


def _readiness_differential(module):
    generator = random.Random(30731)
    states = ("VERIFIED", "DRIFT", "UNKNOWN")
    count = 0
    with (
        mock.patch.object(module, "_last_jev_judgment", return_value=(None, None)),
        mock.patch.object(module, "_JEV_ENV_PATH", _ROOT / "absent-opt-in-fixture"),
    ):
        for index in range(4096):
            pipeline = {
                "pendingMarkerObserved": generator.choice((False, True)),
                "pendingMarkerUnreadable": generator.choice((False, True)),
                "pendingMarkerAttributedTo": "route" if generator.randrange(7) == 0 else None,
            }
            pins = {"runtimeIntegrity": generator.choice(states), "hostBinding": generator.choice(states)}
            complete = (generator.choice((False, True)), generator.choice((False, True)))
            rows = {}
            for offset in range(generator.randrange(6)):
                identifier = f"model-{index}-{offset}"
                rows[identifier] = {"id": identifier, "loaded": generator.choice((False, True, None))}
            loaded = sorted(row["id"] for row in rows.values() if row["loaded"] is True)
            expected = _readiness_oracle(pipeline, pins, loaded, *complete)
            result = module._route_components(
                pipeline, pins, rows, 2000000000.0, inventory_complete=complete[0], models_complete=complete[1]
            )[0]
            if result["state"] != expected:
                raise AssertionError((index, pipeline, pins, rows, complete, expected, result))
            if expected == "ready" and (result["authorModel"], result["reviewerModel"]) != tuple(loaded[:2]):
                raise AssertionError((index, loaded, result))
            count += 1
    return count


def _row_oracle(stage, lock, valid, recovery, archived):
    held = lock == "held"
    bound = held and valid
    live = bound and stage != "incomplete"
    if live:
        state = "running"
    elif lock in ("missing", "replaced", "unsafe", "unknown"):
        state = "unverified"
    elif not valid:
        state = "unreadable"
    else:
        state = "archived-uncleared" if archived else "unresolved"
    return (valid, held, bound, live, state, recovery if valid else False)


def _router_record(index, stage, valid, recovery):
    record = test_telemetry_security._active_route(2000000000.0 + index)
    record.update(stage=stage, inputSha256=f"{index:064x}", runLock={"dev": index, "ino": index + 1})
    record["checkpoint"].update(stage=stage, inputSha256=record["inputSha256"], evidence={"recoveryRequired": recovery})
    if not valid:
        record["checkpoint"]["inputSha256"] = "invalid"
    return record


def _router_differential(module):
    stages = ("incomplete", "backend_draft", "backend_review", "started", "mac_return", "done", "intake", "repair")
    locks = ("held", "free", "missing", "replaced", "unsafe", "unknown")
    variants = list(itertools.product(stages, locks, (False, True), (False, True), (False, True)))
    count = 0
    install = {"state": "absent", "inProgress": False}
    policy = {"policy": None, "source": "fixture", "drainState": "unknown", "draining": False, "sharedHolders": []}
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(module, "_router_install_state", return_value=install))
        stack.enter_context(mock.patch.object(module, "_router_policy", return_value=policy))
        stack.enter_context(mock.patch.object(module, "_private_dir", return_value="ok"))
        stack.enter_context(mock.patch.object(module, "_observe_retry", return_value=("absent", None)))
        stack.enter_context(mock.patch.object(module, "_router_note", return_value=None))
        stack.enter_context(mock.patch.object(module, "_run_owners"))
        stack.enter_context(
            mock.patch.object(
                module,
                "_router_listing",
                side_effect=lambda directory, suffix: (
                    ["bound-route"] if directory.name == "active" else [],
                    False,
                    "ok",
                ),
            )
        )
        reader = stack.enter_context(mock.patch.object(module, "_router_listed_record"))
        probe = stack.enter_context(mock.patch.object(module, "_probe_lock"))
        exists = stack.enter_context(mock.patch.object(module.os.path, "lexists"))
        for index in range(3072):
            stage, lock, valid, recovery, archived = variants[index % len(variants)]
            reader.return_value = ("ok", _router_record(index, stage, valid, recovery), None)
            probe.return_value = lock
            exists.side_effect = lambda name, archived=archived: (
                archived if Path(name).parent.name == "archive" else True
            )
            result = module._router_observation(2000000000.0 + index)["rows"][0]
            keys = ("valid", "lockHeld", "lockBound", "live", "state", "recoveryRequired")
            actual = tuple(result[key] for key in keys)
            expected = _row_oracle(stage, lock, valid, recovery, archived)
            if actual != expected:
                raise AssertionError((index, variants[index % len(variants)], expected, actual))
            count += 1
    return count


def differential(module):
    return {"router_inputs": _router_differential(module), "readiness_inputs": _readiness_differential(module)}


def _replace_function(source, name, old, new):
    function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == name)
    lines = source.splitlines(keepends=True)
    selected = "".join(lines[function.lineno - 1 : function.end_lineno])
    if selected.count(old) != 1:
        raise ValueError(f"Mutation anchor is not unique in {name}")
    lines[function.lineno - 1 : function.end_lineno] = [selected.replace(old, new)]
    return "".join(lines)


def _mutant(source, name, old, new):
    changed = _replace_function(source, name, old, new)
    module = types.ModuleType("telemetry_security_mutant")
    module.__file__ = str(_ROOT / "telemetry.py")
    exec(compile(changed, module.__file__, "exec"), module.__dict__)
    return module


def _regressions(module):
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_telemetry_security.TelemetrySafetyRegressionTests)
    with mock.patch.object(test_telemetry_security, "telemetry", module):
        return unittest.TextTestRunner(stream=io.StringIO()).run(suite)


def _mutations():
    live = 'row["lockBound"] and row["stage"] != "incomplete"'
    marker = 'pipeline.get("pendingMarkerObserved") or pipeline.get("pendingMarkerUnreadable")'
    return [
        ("allow-incomplete-live", "_router_observation", live, 'row["lockBound"]'),
        ("only-incomplete-live", "_router_observation", live, 'row["lockBound"] and row["stage"] == "incomplete"'),
        ("unbound-complete-live", "_router_observation", live, 'row["lockBound"] or row["stage"] != "incomplete"'),
        (
            "incomplete-without-recovery-live",
            "_router_observation",
            live,
            'row["lockBound"] and not row["recoveryRequired"]',
        ),
        (
            "discard-bound-proof",
            "_router_observation",
            'row["lockBound"] = row["lockHeld"] and row["valid"] and row["runLock"] is not None',
            'row["lockBound"] = False',
        ),
        (
            "drop-envelope-proof",
            "_router_observation",
            'row["lockHeld"] and row["valid"] and row["runLock"] is not None',
            'row["lockHeld"]',
        ),
        ("allow-unheld-complete", "_router_observation", live, 'row["stage"] != "incomplete"'),
        (
            "incomplete-unverified",
            "_router_observation",
            'elif row["lockHeld"] and not row["lockBound"]:',
            'elif row["lockHeld"]:',
        ),
        (
            "unresolved-running",
            "_router_observation",
            '"archived-uncleared" if row["archived"] else "unresolved"',
            '"archived-uncleared" if row["archived"] else "running"',
        ),
        ("held-message-free", "_online_code_mode_primary", "{'held' if held else 'free'}", "{'free'}"),
        ("ignore-unreadable-marker", "_route_components", marker, 'pipeline.get("pendingMarkerObserved")'),
        (
            "require-both-marker-flags",
            "_route_components",
            marker,
            'pipeline.get("pendingMarkerObserved") and pipeline.get("pendingMarkerUnreadable")',
        ),
        ("ignore-observed-marker", "_route_components", marker, 'pipeline.get("pendingMarkerUnreadable")'),
        (
            "invert-unreadable-marker",
            "_route_components",
            marker,
            'pipeline.get("pendingMarkerObserved") or not pipeline.get("pendingMarkerUnreadable")',
        ),
        (
            "erase-unreadable-marker",
            "_pipeline",
            'pipeline["pendingMarkerUnreadable"] = True',
            'pipeline["pendingMarkerUnreadable"] = False',
        ),
        ("archive-recursion-escapes", "_scan_jev_judgment", "except Exception:", "except (OSError, ValueError):"),
        (
            "archive-error-hides-valid-judgment",
            "_scan_jev_judgment",
            "except Exception:\n            continue",
            "except Exception:\n            return None, None",
        ),
    ]


def mutations(source):
    results = []
    for identifier, function, old, new in _mutations():
        candidate = _mutant(source, function, old, new)
        regression = _regressions(candidate)
        witness = None
        if regression.failures or regression.errors:
            witness = "regression"
        else:
            try:
                differential(candidate)
            except AssertionError:
                witness = "differential"
        results.append(
            {
                "id": identifier,
                "killed": witness is not None,
                "witness": witness,
                "witness_tests": [test.id() for test, _ in regression.failures + regression.errors],
                "failure_count": len(regression.failures),
                "error_count": len(regression.errors),
            }
        )
    return results


def complete_flag_witness(source):
    candidate = _mutant(
        source,
        "_windows_jobs",
        "read_complete = read_complete if lines_complete else False",
        "read_complete = read_complete",
    )
    with mock.patch.object(candidate, "_read_windows_journal", return_value=b"{invalid\n"):
        report, _ = candidate._windows_jobs(2000000000.0)
    if report["complete"] is not True:
        raise AssertionError("The preservation mutant did not restore the old complete flag defect")
    with mock.patch.object(telemetry, "_read_windows_journal", return_value=b"{invalid\n"):
        report, _ = telemetry._windows_jobs(2000000000.0)
    if report["complete"] is not False:
        raise AssertionError("Incomplete journal lines were published as complete")
    return {"id": "preserve-incomplete-lines-flag", "killed": True, "witness": "incomplete-journal"}


def main():
    source = (_ROOT / "telemetry.py").read_text(encoding="utf-8")
    generated = differential(telemetry)
    results = mutations(source)
    results.append(complete_flag_witness(source))
    receipt = {
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "differential": generated,
        "mutants": results,
    }
    sys.stdout.write(json.dumps(receipt, indent=2) + "\n")
    return 0 if all(result["killed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
