"""Trace the original collector and compare it to the current source with fixtures.

The source trace runs the byte-identical pinned function. Branch diagnostics use
a separate in-memory AST clone and are checked against the same original outcome
for every corpus input. No source file is rewritten or imported from a user home.
"""

import argparse
import ast
import copy
import dis
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from test_support.exception_outcome import comparable

WORKTREE = Path(__file__).resolve().parents[2]
CORPUS = Path(__file__).with_name("cases.json")
TARGET = "collect_nisi_v02"
ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}


def load(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class BranchProbes(ast.NodeTransformer):
    """Probe decisions independently of the original-source line trace."""

    def __init__(self, source):
        self.source = source
        self.points = {}

    def point(self, node, kind, detail=None):
        label = f"{kind}:{node.lineno}:{node.col_offset}:{len(self.points)}"
        self.points[label] = {
            "kind": kind,
            "line": node.lineno,
            "column": node.col_offset,
            "expression": detail if detail is not None else ast.get_source_segment(self.source, node),
        }
        return label

    def call(self, node, kind, detail=None):
        label = self.point(node, kind, detail)
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="_audit_decision", ctx=ast.Load()), args=[ast.Constant(label), node], keywords=[]
            ),
            node,
        )

    def visit_If(self, node):
        detail = ast.get_source_segment(self.source, node.test)
        self.generic_visit(node)
        node.test = self.call(node.test, "if", detail)
        return node

    def visit_IfExp(self, node):
        detail = ast.get_source_segment(self.source, node.test)
        self.generic_visit(node)
        node.test = self.call(node.test, "conditional-expression", detail)
        return node

    def visit_BoolOp(self, node):
        details = [ast.get_source_segment(self.source, value) for value in node.values]
        self.generic_visit(node)
        kind = "and-operand" if isinstance(node.op, ast.And) else "or-operand"
        node.values = [self.call(value, kind, detail) for value, detail in zip(node.values, details)]
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1:
            return node
        # The collector's only chains are 1 <= len(pins) <= 64, where pins is
        # already a real dict. Re-evaluating builtin len in this diagnostic clone
        # cannot change the fixture outcome; clone parity is checked per case.
        operands = [node.left, *node.comparators]
        links = []
        for index, operator in enumerate(node.ops):
            comparison = ast.copy_location(
                ast.Compare(
                    left=copy.deepcopy(operands[index]),
                    ops=[operator],
                    comparators=[copy.deepcopy(operands[index + 1])],
                ),
                node,
            )
            detail = ast.unparse(comparison)
            links.append(self.call(comparison, "comparison-link", detail))
        return ast.copy_location(ast.BoolOp(op=ast.And(), values=links), node)

    def visit_For(self, node):
        detail = ast.get_source_segment(self.source, node.iter)
        self.generic_visit(node)
        label = self.point(node, "loop", detail)
        node.iter = ast.copy_location(
            ast.Call(
                func=ast.Name(id="_audit_loop", ctx=ast.Load()), args=[ast.Constant(label), node.iter], keywords=[]
            ),
            node.iter,
        )
        return node

    def visit_Try(self, node):
        self.generic_visit(node)
        for handler in node.handlers:
            label = self.point(handler, "exception-handler", ast.unparse(handler.type))
            probe = ast.copy_location(
                ast.Expr(
                    value=ast.Call(
                        func=ast.Name(id="_audit_mark", ctx=ast.Load()), args=[ast.Constant(label)], keywords=[]
                    )
                ),
                handler,
            )
            handler.body.insert(0, probe)
        return node


def instrument(module, source):
    function = next(
        node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == TARGET
    )
    probes = BranchProbes(source)
    function = probes.visit(copy.deepcopy(function))
    tree = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    counts = defaultdict(Counter)

    def decision(label, value):
        counts[label]["true" if bool(value) else "false"] += 1
        return value

    def loop(label, values):
        entered = False
        for value in values:
            if not entered:
                counts[label]["true"] += 1
                entered = True
            yield value
        if not entered:
            counts[label]["false"] += 1
        counts[label]["exhausted"] += 1

    def mark(label):
        counts[label]["entered"] += 1

    module.__dict__.update(_audit_decision=decision, _audit_loop=loop, _audit_mark=mark)
    exec(compile(tree, "<collector-branch-probes>", "exec"), module.__dict__)
    return probes.points, counts


def unreachable(point, outcome):
    expression = point["expression"]
    if point["kind"] == "comparison-link" and expression == "1 <= len(pins)" and outcome == "false":
        return "The preceding required-file subset check admits only real dicts containing all required runtime files; their length is at least four."
    if point["kind"] == "loop" and outcome == "false":
        if expression == "pins.items()":
            return "Every manifest reaching this loop contains all required runtime files, so at least four pins are present."
        if expression == "_HOST_FILES.items()":
            return "The unchanged canonical host-file mapping is nonempty and is never replaced by these inputs."
    return None


def classify_lines(source, function, executed):
    lines = source.splitlines()
    bytecode_lines = {
        line
        for _, line in dis.findlinestarts(function.__code__)
        if line is not None and line != function.__code__.co_firstlineno
    }
    tree_function = next(
        node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == TARGET
    )
    statement_lines = {
        node.lineno
        for node in ast.walk(tree_function)
        if isinstance(node, ast.stmt)
        and not isinstance(node, ast.FunctionDef)
        and not (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        )
    }
    records = []
    for line in range(tree_function.lineno, tree_function.end_lineno + 1):
        text = lines[line - 1].strip()
        if line in executed:
            status, reason = "executed", None
        elif line in bytecode_lines:
            status, reason = "uncovered_executable", None
        elif line == tree_function.lineno:
            status, reason = (
                "non_executable",
                "Function definition creates the callable at import, outside a call to the target.",
            )
        elif line == tree_function.body[0].lineno:
            status, reason = "non_executable", "The function docstring is a code-object constant."
        elif not text:
            status, reason = "non_executable", "Blank line."
        elif text.startswith("#"):
            status, reason = "non_executable", "Comment."
        else:
            status, reason = (
                "non_executable",
                "Multiline syntax or continuation without a distinct executable line-table entry.",
            )
        records.append({"line": line, "source": lines[line - 1], "status": status, "reason": reason})
    return {
        "first_line": tree_function.lineno,
        "last_line": tree_function.end_lineno,
        "physical_lines": tree_function.end_lineno - tree_function.lineno + 1,
        "bytecode_executable_lines": len(bytecode_lines),
        "bytecode_executable_lines_hit": len(bytecode_lines & executed),
        "uncovered_executable_lines": sorted(bytecode_lines - executed),
        "ast_statement_lines": len(statement_lines),
        "ast_statement_lines_hit": len(statement_lines & executed),
        "untraced_statement_headers": sorted(statement_lines - executed),
        "line_records": records,
    }


def make_trace(original, executed, exceptions, exception_branches, case_outer_exceptions):
    target_code = original.collect_nisi_v02.__code__

    def trace(frame, event, argument):
        if frame.f_code is not target_code:
            return None
        if event == "line":
            executed.add(frame.f_lineno)
            if frame.f_lineno == 375:
                exception_branches["inner_try_attempts"] += 1
        elif event == "exception":
            exceptions[(frame.f_lineno, argument[0].__name__)] += 1
            matched = issubclass(argument[0], original._InvalidEvidence)
            if frame.f_lineno == 375:
                exception_branches["inner_try_matched_exception" if matched else "inner_try_unmatched_exception"] += 1
                if not matched:
                    exception_branches["outer_try_unmatched_exception"] += 1
                    case_outer_exceptions.append("unmatched")
            elif 266 <= frame.f_lineno <= 392:
                exception_branches["outer_try_matched_exception" if matched else "outer_try_unmatched_exception"] += 1
                case_outer_exceptions.append("matched" if matched else "unmatched")
        return trace

    return trace


def branch_records_for(points, branch_counts):
    records = []
    unexplained = []
    for label, point in points.items():
        counts = dict(branch_counts[label])
        expected_outcomes = ("entered",) if point["kind"] == "exception-handler" else ("true", "false")
        omissions = []
        for outcome in expected_outcomes:
            if counts.get(outcome, 0):
                continue
            reason = unreachable(point, outcome)
            omissions.append({"outcome": outcome, "unreachable_reason": reason})
            if reason is None:
                unexplained.append({"label": label, "outcome": outcome, **point})
        records.append({"label": label, **point, "counts": counts, "unobserved": omissions})
    return records, unexplained


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-only", action="store_true")
    parser.add_argument("--differential-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True, help="JSON report destination")
    args = parser.parse_args()
    document = json.loads(CORPUS.read_text(encoding="utf-8"))
    provenance = document["generated_from"]
    base = provenance["revision"]
    source_bytes = subprocess.check_output(["git", "-C", str(WORKTREE), "show", f"{base}:nisi_v02.py"], env=ENV)
    if hashlib.sha256(source_bytes).hexdigest() != provenance["sha256"]:
        raise RuntimeError("Pinned source digest differs from corpus provenance")
    source = source_bytes.decode()
    current_path = WORKTREE / "nisi_v02.py"
    before = hashlib.sha256(current_path.read_bytes()).hexdigest()
    if current_path.read_bytes() != source_bytes:
        raise RuntimeError("Worktree source differs from pinned original")
    support = load("collector_audit_support", WORKTREE / "test_support/collect_nisi_v02_golden/support.py")
    cases = document["cases"]
    original_executed = set()
    exceptions = Counter()
    exception_branches = Counter()
    case_outer_exceptions = []
    original_matches = 0
    instrumented_matches = 0
    current_matches = 0
    with tempfile.TemporaryDirectory(prefix="collector-audit-") as temporary:
        original_path = Path(temporary) / "nisi_v02.py"
        original_path.write_bytes(source_bytes)
        original = load("collector_audit_original", original_path)
        current = load("collector_audit_current", current_path)
        instrumented = load("collector_audit_instrumented", original_path)
        points, branch_counts = instrument(instrumented, source)
        trace = make_trace(original, original_executed, exceptions, exception_branches, case_outer_exceptions)

        for index, case in enumerate(cases, 1):
            expected = case["expected"]
            previous_trace = sys.gettrace()
            case_outer_exceptions.clear()
            sys.settrace(trace)
            try:
                actual = support.run_case(original, case)
            finally:
                sys.settrace(previous_trace)
            if comparable(expected, actual) != actual:
                raise AssertionError(f"Original corpus mismatch: {case['mode']}:{case['name']}")
            original_matches += 1
            if not case_outer_exceptions and any(event[0] == "_owned" for event in actual["events"]):
                exception_branches["outer_try_normal_completion_or_early_return"] += 1
            if not args.differential_only:
                if support.run_case(instrumented, case) != actual:
                    raise AssertionError(f"Diagnostic instrumentation changed outcome: {case['mode']}:{case['name']}")
                instrumented_matches += 1
            if not args.coverage_only:
                if support.run_case(current, case) != actual:
                    raise AssertionError(f"Differential mismatch: {case['mode']}:{case['name']}")
                current_matches += 1
            if index % 100 == 0:
                sys.stdout.write(json.dumps({"progress": index, "cases": len(cases)}) + "\n")
                sys.stdout.flush()
        line_report = classify_lines(source, original.collect_nisi_v02, original_executed)

    after = hashlib.sha256(current_path.read_bytes()).hexdigest()
    if before != after:
        raise RuntimeError("Source changed during audit")
    provenance = {
        "revision": base,
        "source": "nisi_v02.py",
        "function": TARGET,
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_unchanged_before_and_after": before == after == hashlib.sha256(source_bytes).hexdigest(),
        "python": sys.version.split()[0],
    }
    report = {"provenance": provenance}
    if not args.differential_only:
        exception_branches["inner_try_normal_completion"] = (
            exception_branches["inner_try_attempts"]
            - exception_branches["inner_try_matched_exception"]
            - exception_branches["inner_try_unmatched_exception"]
        )
        branch_records, unexplained = branch_records_for(points, branch_counts)
        coverage = {
            **provenance,
            "cases_traced": original_matches,
            "instrumented_outcomes_equal_original": instrumented_matches,
            **line_report,
            "branch_points": len(branch_records),
            "branch_outcomes_observed": sum(
                sum(record["counts"].get(outcome, 0) > 0 for outcome in ("true", "false", "entered"))
                for record in branch_records
            ),
            "unreachable_outcomes": sum(len(record["unobserved"]) for record in branch_records) - len(unexplained),
            "unexplained_unobserved_outcomes": unexplained,
            "branches": branch_records,
            "exception_events": [
                {"line": line, "type": kind, "count": count} for (line, kind), count in sorted(exceptions.items())
            ],
            "exception_branches": dict(exception_branches),
            "method": "Original-source sys.settrace line/exception events, plus separately compiled in-memory AST probes for each if, conditional expression, short-circuit operand, chained comparison link, loop entry and except handler. Diagnostic clone outcomes must equal original outputs and ordered helper events for every input.",
            "instrumentation_scope": "The only chained comparisons are builtin dict length bounds; diagnostic probes evaluate builtin len twice. The unmodified-source trace and differential comparison do not use instrumented source.",
        }
        report["coverage"] = coverage
        sys.stdout.write(
            json.dumps(
                {
                    "coverage": {
                        key: coverage[key]
                        for key in (
                            "cases_traced",
                            "physical_lines",
                            "bytecode_executable_lines",
                            "bytecode_executable_lines_hit",
                            "uncovered_executable_lines",
                            "branch_points",
                            "branch_outcomes_observed",
                            "unreachable_outcomes",
                            "unexplained_unobserved_outcomes",
                        )
                    }
                }
            )
            + "\n"
        )
    if not args.coverage_only:
        differential = {
            **provenance,
            "cases": len(cases),
            "original_corpus_matches": original_matches,
            "worktree_matches_original": current_matches,
            "mismatches": len(cases) - current_matches,
            "full_outputs_exceptions_and_ordered_events_compared": True,
        }
        report["differential"] = differential
        sys.stdout.write(json.dumps({"differential": differential}) + "\n")
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
