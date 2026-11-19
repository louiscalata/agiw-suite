"""Compare pinned and current pure fleet projections with and without spies."""

import argparse
import copy
import hashlib
import importlib
import importlib.util
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from random import Random
from types import SimpleNamespace
from unittest import mock

from test_support.exception_outcome import comparable, pinned_message

SEED = 1593557031
SYNTHETIC_COUNT = 2048
STAMP = "1970-01-01T00:16:40Z"
TIMESTAMPS = (
    STAMP,
    "1970-01-01T00:16:37Z",
    "1970-01-01T00:16:42Z",
    "1970-01-01T00:15:00Z",
    None,
    "",
    "invalid",
    "1970-01-01T00:16:40",
    1000,
    [],
)
COUNTS = (-1, 0, 1, 2, 17, False, True, None, "1", 0.0, 1.0, {"$float": "nan"})
IDENTITIES = ("mac", "pc", "remote", "other", "", None, 7, False)
REASONS = (
    None,
    "timeout",
    "refused",
    "unavailable",
    "upstream_error",
    "tunnel_down",
    "stale",
    "not_configured",
    "identity_unknown",
    "no_models",
    "custom",
)


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":"))


def choice(rng, values):
    return copy.deepcopy(rng.choice(values))


def synthetic_machine(rng, identity, local=False):
    row = {
        "id": identity,
        "host": identity,
        "label": choice(rng, ("Machine", "", None, "<host>&\u0000")),
        "state": choice(rng, ("up", "down", None, "unknown")),
        "platform": choice(rng, ("mac", "macos", "windows", "remote", None)),
        "observedAt": choice(rng, TIMESTAMPS),
        "modelsComplete": choice(rng, (True, False, None, 1)),
        "loadedCount": choice(rng, COUNTS),
        "busyCount": choice(rng, COUNTS),
    }
    if local:
        row.update(choice(rng, ({"trust": "local"}, {"role": "this-machine"}, {"source": "local"})))
    if rng.randrange(3) == 0:
        row["freshForSeconds"] = choice(rng, (0, 1, 3, 30, -1, None, False, "3"))
    if rng.randrange(4) == 0:
        row["reasonCode"] = choice(rng, ("reported", "not_observed", "stale", "timeout", None))
    if rng.randrange(5) == 0:
        row["observationSource"] = "registry"
    if rng.randrange(5) == 0:
        row["inventoryScope"] = "configured-lanes"
    return row


def synthetic_roots(rng):
    roots = []
    for identity in ("remote", "other", "pc")[: rng.randrange(4)]:
        root = {"id": identity, "role": "machine", "label": identity.upper()}
        if rng.randrange(2) == 0:
            root["replaces"] = "legacy-pc"
        if rng.randrange(3) == 0:
            root["observationSource"] = "registry"
        roots.append(root)
    return roots


def synthetic_lane(rng, index):
    lane = {
        "id": choice(rng, ("lane-a", "lane-b", "lane-c", "", None, 7)),
        "role": choice(rng, ("llama", "llama", "llama", "machine", "client", None)),
        "machine": choice(rng, ("mac", "pc", "remote", "other", None, 7)),
        "label": choice(rng, ("lane", "<lane>\n\u0000", None, "")),
        "health": choice(rng, ("ok", "degraded", "unknown", None)),
        "modelId": choice(rng, ("fixture/model", "", None, 17, [])),
        "observedAt": choice(rng, TIMESTAMPS),
        "reason": choice(rng, REASONS),
    }
    if index % 2 == 0:
        lane["freshForSeconds"] = choice(rng, (-1, 0, 1, 3, 30, True, None, "3", {"$float": "nan"}))
    if index % 3 == 0:
        lane["probeIntervalSeconds"] = choice(rng, (0, 1, 1.5, 3, None, "2"))
    if index % 5 == 0:
        lane["expiresAt"] = choice(rng, (STAMP, "1970-01-01T00:16:39Z", "1970-01-01T00:17:10Z", None, "bad"))
    if index % 7 == 0:
        lane["observationSource"] = "registry"
    return lane


def add_attribution(rng, rows):
    sources = []
    for row in rows:
        if rng.randrange(3) == 0:
            row[choice(rng, ("machineId", "registryMachineId", "attributedMachineId"))] = choice(
                rng, ("remote", "other", "unconfigured", "", None)
            )
        if rng.randrange(4) == 0:
            row["legacy"] = choice(rng, (True, False, 1))
        if rng.randrange(3) == 0:
            sources.append(
                {
                    "node": row["id"],
                    "machineId": choice(rng, ("remote", "other", None)),
                    "legacy": choice(rng, (True, False)),
                }
            )
        if rng.randrange(5) == 0:
            row["source"] = {"machineId": choice(rng, ("remote", "other", "")), "legacy": choice(rng, (True, False))}
    return sources


def add_inventory(rng, snapshot, local_id):
    if rng.randrange(2) == 0:
        snapshot["inventoryComplete"] = choice(rng, (True, False, None, 0, 1))
    if rng.randrange(2) == 0:
        snapshot["activityKnown"] = choice(rng, (True, False, None, 0, 1))
    if rng.randrange(3) == 0:
        snapshot["models"] = choice(rng, (None, [], "odd", {}))
    else:
        snapshot["models"] = [
            {
                "host": choice(rng, (local_id, "mac", "pc", "windows", "remote", None)),
                "loaded": choice(rng, (True, False, None, 0, 1, "true")),
                "inventoryComplete": choice(rng, (True, False, None, 0)),
            }
            for _ in range(rng.randrange(4))
        ]


def add_oddities(rng, case, index):
    if index % 32 == 0:
        case["snapshot"] = choice(rng, (None, [], 7, "snapshot", False))
    elif index % 32 == 1:
        case["snapshot"]["machines"] = choice(rng, (None, "rows", 1, {"key": {}}, {"$tuple": [None, {}]}))
    elif index % 32 == 2:
        case["inference"]["nodes"] = choice(rng, (None, "nodes", 1, {"key": {}}, {"$tuple": [None, {}]}))
    elif index % 32 == 3:
        case["snapshot"]["sources"] = choice(rng, (None, "sources", 1, {"key": {}}))
    elif index % 32 == 4 and case["snapshot"]["machines"]:
        case["snapshot"]["machines"][0]["id"] = []
    elif index % 32 == 5:
        case["snapshot"]["host"] = []
    elif index % 32 == 6:
        case["inference"]["nodes"].append({"id": "hostile", "machine": [], "reason": []})
    elif index % 32 == 7:
        case["snapshot"]["inference"] = copy.deepcopy(case["inference"])
        case["inference"] = choice(rng, (None, [], {}, "odd"))


def synthetic_cases():
    rng = Random(SEED)
    cases = []
    for index in range(SYNTHETIC_COUNT):
        local_id = choice(rng, ("mac", "pc", "custom"))
        rows = [synthetic_machine(rng, local_id, True)]
        rows.extend(synthetic_machine(rng, choice(rng, IDENTITIES)) for _ in range(rng.randrange(4)))
        if index % 7 == 0:
            rows.append(copy.deepcopy(rows[0]))
            rows[-1]["loadedCount"] = 17
        sources = add_attribution(rng, rows)
        snapshot = {
            "host": local_id,
            "platform": choice(rng, ("mac", "windows", None)),
            "machines": rows,
            "sources": sources,
        }
        add_inventory(rng, snapshot, local_id)
        document = {
            "nodes": synthetic_roots(rng) + [synthetic_lane(rng, index + item) for item in range(rng.randrange(6))],
            "observedAt": choice(rng, TIMESTAMPS),
        }
        if index % 4 != 0:
            document["remoteMachines"] = {
                "state": choice(rng, ("configured", "invalid", "unknown", None)),
                "freshForSeconds": choice(rng, (0, 3, 30, None)),
            }
        case = {
            "name": f"seeded_{index:04d}",
            "snapshot": snapshot,
            "inference": document,
            "now": 1000.0 + choice(rng, (-2.001, -2.0, -1.999, 0.0, 2.999, 3.0, 3.001, 29.999, 30.0, 30.001)),
        }
        if index % 64 == 0:
            case["now"] = choice(rng, (None, False, True, "1000", {"$float": "nan"}, {"$float": "inf"}, 10**400, 1e20))
            case["clock"] = choice(rng, (1000.0, False, {"$float": "nan"}))
        add_oddities(rng, case, index)
        cases.append(case)
    return cases


def direct_record(module, adapter, case):
    inputs = adapter._decode(copy.deepcopy({key: case.get(key) for key in ("snapshot", "inference", "now")}))
    clock = SimpleNamespace(time=lambda: adapter._decode(case.get("clock", 1000.0)))
    with mock.patch.object(module, "time", clock):
        try:
            result = module.build_fleet_state(**inputs)
            outcome = {"result": adapter._encode(result)}
            input_paths = adapter._mutable_paths(inputs)
            output_paths = adapter._mutable_paths(result)
            shared = sorted(
                [input_paths[identity], output_paths[identity]] for identity in input_paths.keys() & output_paths.keys()
            )
        except (ArithmeticError, AttributeError, KeyError, OSError, TypeError, ValueError) as error:
            outcome = {"error": type(error).__name__}
            message = pinned_message(error, module)
            if message is not None:
                outcome["message"] = message
            shared = []
    return {"outcome": outcome, "inputsAfter": adapter._encode(inputs), "sharedMutablePaths": shared}


def without_events(record):
    return {key: value for key, value in record.items() if key != "events"}


def mismatch_fields(left, right):
    return [key for key in sorted(set(left) | set(right)) if canonical(left.get(key)) != canonical(right.get(key))]


def load_generator(path):
    spec = importlib.util.spec_from_file_location("fleet_differential_generator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def current_modules(repo):
    sys.path.insert(0, str(repo))
    dependency = importlib.import_module("machine_state")
    module = importlib.import_module("fleet_state")
    for loaded in (dependency, module):
        if Path(loaded.__file__).parent != repo:
            raise ValueError("Current module imported from wrong directory")
    adapter = importlib.import_module("test_fleet_state_golden")
    return module, dependency, adapter


def compare_cases(group, cases, baseline, current, adapter, report):
    for case in cases:
        pinned = adapter._run_case(baseline, case)
        candidate = adapter._run_case(current, case)
        comparisons = [("pinned_vs_current", pinned, candidate)]
        if group == "corpus":
            comparisons.append(("pinned_vs_recorded", comparable(case["expected"], pinned), pinned))
        comparisons.extend(
            (
                ("pinned_direct_vs_spied", without_events(pinned), direct_record(baseline, adapter, case)),
                ("current_direct_vs_spied", without_events(candidate), direct_record(current, adapter, case)),
            )
        )
        for kind, left, right in comparisons:
            report["comparison_counts"][kind] += 1
            if canonical(left) != canonical(right):
                report["mismatch_counts"][kind] += 1
                if len(report["witnesses"]) < 20:
                    report["witnesses"].append(
                        {
                            "group": group,
                            "case": case["name"],
                            "kind": kind,
                            "different_fields": mismatch_fields(left, right),
                            "input": {key: value for key, value in case.items() if key != "expected"},
                            "left_outcome": left["outcome"],
                            "right_outcome": right["outcome"],
                        }
                    )
        report["outcomes"][group]["exception" if "error" in candidate["outcome"] else "result"] += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--generator", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    started = time.perf_counter()
    repo = args.repo.resolve()
    generator = load_generator(args.generator)
    if any(name in sys.modules for name in ("fleet_state", "machine_state")):
        raise ValueError("Production modules already imported before isolated baseline")
    corpus_bytes = (repo / "test_support/fleet_state_golden/cases.json").read_bytes()
    corpus = json.loads(corpus_bytes)
    synthetic = synthetic_cases()
    report = {
        "source_revision": generator.BASE,
        "seed": SEED,
        "corpus_case_count": len(corpus["cases"]),
        "synthetic_case_count": len(synthetic),
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "synthetic_inputs_sha256": hashlib.sha256(canonical(synthetic).encode()).hexdigest(),
        "comparison_counts": Counter(),
        "mismatch_counts": Counter(),
        "witnesses": [],
        "outcomes": {"corpus": Counter(), "synthetic": Counter()},
        "python": sys.version.split()[0],
    }
    with tempfile.TemporaryDirectory(prefix="fleet-differential-") as temp:
        baseline, pinned_hashes = generator.baseline(repo, Path(temp))
        pinned_dependency = sys.modules.pop("machine_state")
        sys.path.remove(temp)
        current, dependency, adapter = current_modules(repo)
        if baseline._number.__globals__ is not pinned_dependency.__dict__:
            raise ValueError("Pinned function dependency ownership differs")
        if current._number.__globals__ is not dependency.__dict__:
            raise ValueError("Current function dependency ownership differs")
        if pinned_dependency is dependency:
            raise ValueError("Pinned and current dependencies share module identity")
        current_hashes = {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in pinned_hashes}
        report.update(
            pinned_hashes=pinned_hashes,
            current_hashes=current_hashes,
            source_bytes_identical=pinned_hashes == current_hashes,
            corpus_pins_identical=corpus["sourceSha256"] == pinned_hashes
            and corpus["sourceRevision"] == generator.BASE,
            dependencies_isolated=True,
        )
        compare_cases("corpus", corpus["cases"], baseline, current, adapter, report)
        compare_cases("synthetic", synthetic, baseline, current, adapter, report)
    report["elapsed_seconds"] = round(time.perf_counter() - started, 6)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    summary = {
        key: report[key]
        for key in (
            "corpus_case_count",
            "synthetic_case_count",
            "comparison_counts",
            "mismatch_counts",
            "source_bytes_identical",
            "corpus_pins_identical",
            "dependencies_isolated",
            "elapsed_seconds",
        )
    }
    sys.stdout.write(json.dumps(summary, sort_keys=True) + "\n")
    return (
        0
        if not report["mismatch_counts"] and report["source_bytes_identical"] and report["corpus_pins_identical"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
