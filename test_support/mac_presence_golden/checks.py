"""Reproduce presence expectations and run focused mutation/differential checks."""

import argparse
import copy
import hashlib
import importlib
import inspect
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_support.mac_presence_golden.support import _INTERPRETER_OPERATIONS, expected_outcome, run_case

_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = Path(__file__).with_name("cases.json")
_MODULE = "test_support.mac_presence_golden.checks"
_MUTATIONS = (
    (
        "unloaded_cli_activity",
        'cli = [item for item in loaded if item.get("source") == "lms-ps"]',
        'cli = [item for item in group if item.get("source") == "lms-ps"]',
        "unloaded_cli_does_not_override_api_idle",
    ),
    (
        "api_self_coverage",
        'instances = {item.get("instanceId") or item.get("id") for item in cli}',
        'instances = {item.get("instanceId") or item.get("id") for item in loaded}',
        "api_id_does_not_cover_cli_instance",
    ),
)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")


def _source(root):
    root = root.resolve()
    sys.path.insert(0, str(root))
    module = importlib.import_module("peer_feed")
    if Path(module.__file__).resolve() != root / "peer_feed.py":
        raise RuntimeError("Unexpected presence implementation")
    return module


def _capture(args):
    document = _read(_CORPUS)
    source = (args.source_root / "peer_feed.py").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest != document["generated_from"]["peer_feed_sha256"]:
        raise ValueError("Source differs from the recorded production bytes")
    module = _source(args.source_root)
    for case in document["cases"]:
        case.pop("expected_by_python", None)
        case["expected"] = run_case(module, case)
    document["generated_from"] = {
        "revision": args.revision,
        "peer_feed_sha256": digest,
        "generator": _MODULE + " capture",
        "python": sys.version.split()[0],
    }
    _write(args.output, document)
    return {"cases": len(document["cases"]), "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}


def _test_result(suite):
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    return {
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
    }


def _mutations():
    module = _source(_ROOT)
    golden = importlib.import_module("test_mac_presence_golden")
    baseline = _test_result(unittest.defaultTestLoader.loadTestsFromModule(golden))
    if baseline["failures"] or baseline["errors"] or baseline["skipped"]:
        raise AssertionError(baseline)
    source = inspect.getsource(module.mac_presence)
    records = []
    for name, old, new, witness in _MUTATIONS:
        if source.count(old) != 1:
            raise ValueError("Mutation site is not unique: " + name)
        namespace = {}
        exec(compile(source.replace(old, new), "<presence mutation>", "exec"), vars(module), namespace)
        with mock.patch.object(module, "mac_presence", namespace["mac_presence"]):
            result = _test_result(unittest.TestSuite([golden.MacPresenceGoldenTests("test_" + witness)]))
        records.append({"name": name, "witness": witness, **result})
        if result != {"tests": 1, "failures": 1, "errors": 0, "skipped": 0}:
            raise AssertionError(records[-1])
    controls = _comparison_controls(golden)
    return {"baseline": baseline, "killed": len(records), "mutations": records, "comparison_controls": controls}


def _comparison_controls(golden):
    cases = {case["name"]: case for case in golden._CASES}
    controls = []
    for name in _INTERPRETER_OPERATIONS:
        for field, value in (
            ("message", "different error message"),
            ("error", "ValueError"),
            ("events", [["unexpected call"]]),
            ("inputsUnchanged", False),
        ):
            observation = expected_outcome(cases[name])
            observation[field] = value
            with mock.patch.object(golden, "run_case", return_value=observation):
                result = _test_result(unittest.TestSuite([golden.MacPresenceGoldenTests("test_" + name)]))
            if result != {"tests": 1, "failures": 1, "errors": 0, "skipped": 0}:
                raise AssertionError((name, field, result))
            controls.append({"case": name, "changed": field, **result})
    for name in ("snapshot_none", "helper_public_id_TypeError"):
        case = copy.deepcopy(cases[name])
        case["expected"]["message"] = "different application message"
        result = _test_result(
            unittest.TestSuite(
                [unittest.FunctionTestCase(lambda case=case: golden._golden_test(case)(unittest.TestCase()))]
            )
        )
        if result != {"tests": 1, "failures": 1, "errors": 0, "skipped": 0}:
            raise AssertionError((name, result))
        controls.append({"case": name, "changed": "application message", **result})
    return {"rejected": len(controls), "records": controls}


def _fuzz_cases():
    rng = random.Random(71116)
    cases = []
    for number in range(1000):
        sources = [
            {
                "id": key,
                "state": rng.choice(["live", "error", None]),
                "modelsComplete": rng.choice([True, False, 1, None]),
                "route": rng.choice(["direct", "proxy", None]),
            }
            for key in rng.sample(["lmstudio-api", "lms-ps", "other"], rng.randrange(4))
        ]
        rows = []
        for index in range(rng.randrange(8)):
            item = {
                "id": f"instance-{index}",
                "modelKey": rng.choice([None, "", "group-a", "group-b", 0]),
                "host": rng.choice(["mac", "mac", "windows", None]),
                "loaded": rng.choice([True, False, None, 1, 0]),
                "state": rng.choice(["generating", "busy", "queued", "idle", "unknown", None]),
                "source": rng.choice(["lmstudio-api", "lms-ps", None]),
                "parallel": rng.choice([None, False, True, 0, 1, 2, 255, 256, 257]),
                "inventoryComplete": rng.choice([True, False, None, 1]),
                "representedByInstances": rng.choice([True, False, None]),
            }
            if rng.randrange(2):
                item["loadedInstanceIds"] = rng.choice([None, [], ["instance-0"], ["instance-0", "instance-1"]])
            rows.append(item)
        cut = rng.randrange(len(rows) + 1)
        cases.append(
            {
                "name": f"seeded-{number}",
                "snapshot": {
                    "sampledAt": rng.choice([0, -1, 10000, 10000.1234, 10000.1235, 10000.1236]),
                    "activityKnown": rng.choice([True, False, 1, None]),
                    "sources": sources,
                    "models": rows[:cut],
                    "localInstanceObservations": rows[cut:],
                    "memory": {
                        "level": rng.choice(["ok", "tight", "bad", None]),
                        "availablePercent": rng.choice([-1, 0, 1, 99, 100, 101, None, True]),
                    },
                },
                "host": "fixture-host" * rng.randrange(8),
                "version": rng.choice([None, "fixture", 1]),
                "dashboard_port": rng.choice([None, False, 0, 1, 65535, 65536, "1"]),
            }
        )
    return cases


def _differential(args):
    cases = _read(_CORPUS)["cases"]
    golden_count = len(cases)
    cases += _fuzz_cases()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(_ROOT), env.get("PYTHONPATH"))))
    with tempfile.TemporaryDirectory(prefix="presence-differential-") as directory:
        root = Path(directory)
        inputs = root / "inputs.json"
        _write(inputs, cases)
        outcomes = []
        for index, source_root in enumerate((args.baseline_root, _ROOT)):
            output = root / f"outcomes-{index}.json"
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    _MODULE,
                    "replay",
                    "--source-root",
                    str(source_root),
                    "--inputs",
                    str(inputs),
                    "--output",
                    str(output),
                ],
                check=True,
                env=env,
                timeout=60,
            )
            outcomes.append(_read(output))
        if len(outcomes[0]) != len(cases) or len(outcomes[1]) != len(cases):
            raise AssertionError("Incomplete differential replay")
        mismatches = [case["name"] for case, left, right in zip(cases, *outcomes) if left != right]
    if mismatches:
        raise AssertionError(mismatches)
    return {
        "golden": golden_count,
        "seeded": len(cases) - golden_count,
        "compared": len(cases),
        "mismatches": len(mismatches),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--source-root", type=Path, required=True)
    capture.add_argument("--revision", required=True)
    capture.add_argument("--output", type=Path, required=True)
    commands.add_parser("mutations")
    differential = commands.add_parser("differential")
    differential.add_argument("--baseline-root", type=Path, required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--source-root", type=Path, required=True)
    replay.add_argument("--inputs", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        report = _capture(args)
    elif args.command == "mutations":
        report = _mutations()
    elif args.command == "differential":
        report = _differential(args)
    else:
        module = _source(args.source_root)
        _write(args.output, [run_case(module, case) for case in _read(args.inputs)])
        return
    sys.stdout.write(json.dumps(report) + "\n")


if __name__ == "__main__":
    main()
