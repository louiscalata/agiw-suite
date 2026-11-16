"""Replay concrete fleet projection mutations through their unittest witnesses."""

import argparse
import ast
import copy
import hashlib
import importlib
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


class ReplaceNode(ast.NodeTransformer):
    def __init__(self, target, replacement):
        self.target = target
        self.replacement = replacement

    def visit(self, node):
        return self.replacement if node is self.target else self.generic_visit(node)


def mutated_source(tree, spec):
    tree = copy.deepcopy(tree)
    node = list(ast.walk(tree))[spec["index"]]
    if node.lineno != spec["line"]:
        raise ValueError("Mutation location differs from its recorded source")
    kind, arg = spec["kind"], spec["arg"]
    if kind == "booldrop":
        if len(node.values) == 2:
            tree = ReplaceNode(node, node.values[1 - arg]).visit(tree)
        else:
            del node.values[arg]
    elif kind == "boolop":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif kind == "const" and arg == "str":
        node.value += "~"
    elif kind == "const" and arg == "none":
        node.value = 0
    else:
        raise ValueError("Unsupported mutation specification")
    return ast.unparse(ast.fix_missing_locations(tree))


def run_witness(repo, adapter, protocol, tree, spec):
    source = mutated_source(tree, spec)
    module = types.ModuleType("fleet_mutation")
    module.__file__ = str(repo / "fleet_state.py")
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    name = "test_" + spec["case"]
    suite = unittest.TestSuite((adapter.FleetStateGoldenTests(name), protocol.FleetStateGoldenProtocolTests(name)))
    stream = io.StringIO()
    with mock.patch.object(adapter, "fleet_state", module), mock.patch.object(protocol, "fleet_state", module):
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    return {
        **spec,
        "mutantSourceSha256": hashlib.sha256(source.encode()).hexdigest(),
        "testsRun": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skips": len(result.skipped),
        "status": "survived" if result.wasSuccessful() else "killed",
        "output": stream.getvalue(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    source = (repo / "fleet_state.py").read_bytes()
    manifest = json.loads((repo / "test_support/fleet_state_golden/mutations.json").read_text(encoding="utf-8"))
    if hashlib.sha256(source).hexdigest() != manifest["sourceSha256"]:
        raise ValueError("Mutation locations require the recorded fleet source")
    sys.path.insert(0, str(repo))
    adapter = importlib.import_module("test_fleet_state_golden")
    protocol = importlib.import_module("test_fleet_state_golden_protocol")
    # A failing original must not be mistaken for mutation sensitivity.
    baseline = unittest.TestSuite(
        test
        for spec in manifest["mutations"]
        for test in (
            adapter.FleetStateGoldenTests("test_" + spec["case"]),
            protocol.FleetStateGoldenProtocolTests("test_" + spec["case"]),
        )
    )
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(baseline)
    if not result.wasSuccessful() or result.skipped:
        raise ValueError("Unmodified mutation witnesses must pass without skips")
    tree = ast.parse(source)
    records = [run_witness(repo, adapter, protocol, tree, spec) for spec in manifest["mutations"]]
    summary = {
        "mutations": len(records),
        "distinctInputs": len({item["case"] for item in records}),
        "baselineTests": result.testsRun,
        "killed": sum(item["status"] == "killed" for item in records),
        "survived": sum(item["status"] == "survived" for item in records),
        "testsRun": sum(item["testsRun"] for item in records),
        "failures": sum(item["failures"] for item in records),
        "errors": sum(item["errors"] for item in records),
        "skips": sum(item["skips"] for item in records),
    }
    report = {"python": sys.version, "sourceSha256": manifest["sourceSha256"], "summary": summary, "results": records}
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(summary, sort_keys=True) + "\n")
    return 1 if summary["survived"] or summary["skips"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
