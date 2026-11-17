"""Capture collector expectations exclusively from a pinned, original Git blob."""

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SUPPORT = Path(__file__).resolve().parent
WORKTREE = SUPPORT.parents[1]
BASE = "8097865fc365ef978f3cc341fe4f9f0568e5f7d1"
SOURCE_SHA256 = "310662fb1be0554340375f257304c3ff623bf61bbf6e41f2d443ea4520826eb6"
ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}
COMMIT = "1234567890abcdef1234567890abcdef12345678"
GENERATION = "abcdef0123456789abcdef0123456789abcdef01"
HOSTS = {
    "nisi_auto_preflight.mjs": ".codex/skills/local-llm-orchestrator/scripts/nisi_auto_preflight.mjs",
    "nisi_bridge.mjs": ".codex/skills/local-llm-orchestrator/scripts/nisi_bridge.mjs",
    "nisi_local.ts": ".config/opencode/tools/nisi_local.ts",
    "nisi_validate.mjs": ".codex/skills/local-llm-orchestrator/scripts/nisi_validate.mjs",
    "pipeline_integrations.py": ".codex/skills/local-llm-orchestrator/scripts/pipeline_integrations.py",
}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def edit(path, value=None, delete=False):
    return {"path": path.split("/"), "delete": True} if delete else {"path": path.split("/"), "value": value}


def _add_boundary_cases(add):
    # Boundaries precede happy paths and invalid-field permutations.
    for key, limit in (
        ("receipt_size", 65536),
        ("manifest_size", 16384),
        ("package_size", 16384),
        ("runtime_size", 2 * 1024 * 1024),
        ("host_size", 2 * 1024 * 1024),
    ):
        for label, delta in (("below", -1), ("equal", 0), ("above", 1)):
            add(f"{key}_{label}", **{key: limit + delta})
    for label, delta in (("below", -1), ("equal", 0), ("above", 1)):
        add(f"archive_size_{label}", modes=("commit",), archive_size=8 * 1024 * 1024 + delta)
    for count in (4, 5, 63, 64, 65):
        add(f"pin_count_{count}", pin_count=count)
    for label, value in (
        ("past", "2027-01-15T07:59:59.999Z"),
        ("equal_now", "2027-01-15T08:00:00Z"),
        ("future_below", "2027-01-15T08:00:59.999Z"),
        ("future_equal", "2027-01-15T08:01:00Z"),
        ("future_above", "2027-01-15T08:01:00.001Z"),
        ("naive", "2027-01-15T08:00:00"),
        ("offset", "2027-01-15T09:00:00+01:00"),
        ("none", None),
        ("empty", ""),
        ("odd", []),
        ("overlong", "x" * 41),
        ("length_40", "x" * 40),
        ("bad_date", "2027-99-99T08:00:00Z"),
    ):
        add(f"verified_at_{label}", receipt_edits=[edit("verifiedAt", value)])
    for label, now in (
        ("none", None),
        ("string", "1800000000"),
        ("empty_list", []),
        ("object", {}),
        ("nan", {"float": "nan"}),
        ("positive_infinity", {"float": "inf"}),
        ("negative_infinity", {"float": "-inf"}),
        ("huge_integer", {"power": 400}),
        ("datetime_overflow_positive", 1e20),
        ("datetime_overflow_negative", -1e20),
        ("year_overflow", 253402300800),
        ("bool_true", True),
        ("bool_false", False),
        ("zero", 0),
        ("negative", -1),
    ):
        add(f"now_{label}", now=now)
    for label, value in (("integer", 7), ("list", []), ("bool", False)):
        add(f"home_{label}", home_value=value)
    add("home_default", home="default")
    add("home_explicit_none", home_value=None)
    add("home_string", home="string")
    add("layout_empty", layout="empty")
    add("receipt_absent_early_return", layout="no_receipt")
    add("home_untrusted", untrusted_home=True)


def _add_runtime_cases(add):
    add("valid")
    add("runtime_reverse_order", reverse_runtime=True)
    add("host_drift_first_last", drift=[next(iter(HOSTS.values())), list(HOSTS.values())[-1]])
    add("all_host_drift", drift=list(HOSTS.values()))
    add("host_missing", missing=[list(HOSTS.values())[1]])
    add("runtime_drift", drift=[f".local/share/nisi-runtime/{COMMIT}/workflow/contracts.mjs"], modes=("commit",))
    add(
        "runtime_drift", drift=[f".local/share/nisi-runtime/{GENERATION}/workflow/contracts.mjs"], modes=("generation",)
    )
    add("archive_drift", modes=("commit",), drift=[f".local/share/nisi-runtime/{COMMIT}.tar"])
    for mode, identity in (("commit", COMMIT), ("generation", GENERATION)):
        add("runtime_missing", modes=(mode,), missing=[f".local/share/nisi-runtime/{identity}/adapters/local-chat.mjs"])
        add("folder_prefix_bad", modes=(mode,), receipt_folder="rollback-deadbeef-fixture")
        add("folder_prefix_short", modes=(mode,), receipt_folder=f"rollback-{identity[:7]}-fixture")
        add("folder_prefix_upper", modes=(mode,), receipt_folder=f"rollback-{identity[:8].upper()}-fixture")
        for label, value in (
            ("none", None),
            ("integer", 9),
            ("empty", ""),
            ("short", identity[:-1]),
            ("long", identity + "a"),
            ("uppercase", identity.upper()),
            ("newline", identity + "\n"),
        ):
            add(f"identity_{label}", modes=(mode,), receipt_edits=[edit(mode, value)])
    for value, label in ((COMMIT, "commit_present"), ("wrong", "kind_wrong"), (None, "kind_none")):
        add(
            label, modes=("generation",), receipt_edits=[edit("commit" if label == "commit_present" else "kind", value)]
        )
    add("generation_empty", modes=("generation",), receipt_edits=[edit("generation", "")])
    for label, value in (("empty", ""), ("zero", 0), ("false", False)):
        add(f"commit_generation_{label}", modes=("commit",), receipt_edits=[edit("generation", value)])
    for label, value in (("none", None), ("wrong", "elsewhere"), ("odd", [])):
        add(f"runtime_root_{label}", receipt_edits=[edit("runtimeRoot", value)])
    for label, value in (
        ("none", None),
        ("odd", []),
        ("empty", ""),
        ("short", "a" * 63),
        ("long", "a" * 65),
        ("uppercase", "A" * 64),
        ("newline", "a" * 64 + "\n"),
        ("mismatch", "0" * 64),
    ):
        add(f"manifest_sha_{label}", modes=("generation",), receipt_edits=[edit("manifestSha256", value)])
        add(f"archive_sha_{label}", modes=("commit",), manifest_edits=[edit("archiveSha256", value)])
        add(f"file_pin_{label}", manifest_edits=[{"path": ["files", "workflow/contracts.mjs"], "value": value}])


def _add_manifest_cases(add):
    add("archive_receipt_pin_mismatch", modes=("commit",), receipt_edits=[edit("runtimeArchiveSha256", "f" * 64)])
    add(
        "archive_matching_invalid_pins",
        modes=("commit",),
        manifest_edits=[edit("archiveSha256", "xyz")],
        receipt_edits=[edit("runtimeArchiveSha256", "xyz")],
    )
    add(
        "package_pin_integer_hash",
        manifest_edits=[{"path": ["files", "package.json"], "value": int("1" * 64)}],
    )
    for label, value in (("none", None), ("list", []), ("empty", {}), ("string", "files")):
        add(f"pin_map_{label}", manifest_edits=[edit("files", value)])
        add(f"host_map_{label}", receipt_edits=[edit("files", value)])
    for mode in ("commit", "generation"):
        add("manifest_identity_mismatch", modes=(mode,), manifest_edits=[edit(mode, "f" * 40)])
    for label, value in (("missing", None), ("wrong", 1), ("string", "2"), ("boolean", True), ("float", 2.0)):
        add(f"schema_{label}", modes=("generation",), manifest_edits=[edit("schemaVersion", value)])
    for name in (
        "package.json",
        "workflow/contracts.mjs",
        "adapters/local-chat.mjs",
        "hosts/local-models/opencode-orchestration-v1.mjs",
    ):
        add(
            "required_pin_missing_" + name.replace("/", "_").replace(".", "_"),
            manifest_edits=[{"path": ["files", name], "delete": True}],
        )
    for label, name in (
        ("parent", "../outside"),
        ("absolute", "/outside"),
        ("dot", "./extra"),
        ("double_slash", "extra//file"),
        ("control", "extra/line\n"),
        ("unicode", "extra/é"),
        ("length_160", "e" * 160),
        ("length_161", "e" * 161),
    ):
        add(f"hostile_file_{label}", manifest_edits=[{"path": ["files", name], "value": "a" * 64}])


def _add_package_and_receipt_cases(add):
    for key, values in (
        ("name", (None, "other", "Nisi", "nisi\n", [])),
        ("version", (None, "0.2.0", "0.2.0-private.1", {})),
        ("private", (None, False, 1, "true", [], {})),
    ):
        for index, value in enumerate(values):
            add(f"package_{key}_{index}", package_edits=[edit(key, value)])
        add(f"package_{key}_missing", package_edits=[edit(key, delete=True)])
    for index, status in enumerate((None, "NOT_VERIFIED", "verified", "VERIFIED\n", True, {}, "")):
        add(f"activation_status_{index}", receipt_edits=[edit("status", status)])
    add(
        "activation_not_verified_with_drift", receipt_edits=[edit("status", "NOT_VERIFIED")], drift=list(HOSTS.values())
    )
    add("activation_missing_stamp_with_drift", receipt_edits=[edit("verifiedAt", None)], drift=list(HOSTS.values()))
    for label, value in (("none", None), ("list", []), ("empty", {}), ("invalid_work", {"work": {}, "validation": {}})):
        add(f"probe_{label}", receipt_edits=[edit("verification", value)])
    for key in ("contract", "syntax", "tests", "certification", "accepted"):
        add(f"probe_invalid_{key}", receipt_edits=[edit("verification/validation/" + key, "unexpected")])
    for name in (next(iter(HOSTS)), list(HOSTS)[-1]):
        tag = name.replace(".", "_")
        for label, value in (("none", None), ("list", []), ("string", "pin")):
            add(f"host_pin_{tag}_{label}", receipt_edits=[{"path": ["files", name], "value": value}])
        for key, values in (
            ("installedPath", (None, [], "<lane folder>/wrong")),
            ("afterSha256", (None, [], "", "a" * 63, "A" * 64, "f" * 64)),
        ):
            for index, value in enumerate(values):
                add(f"host_pin_{tag}_{key}_{index}", receipt_edits=[{"path": ["files", name, key], "value": value}])
        add(f"host_pin_{tag}_missing", receipt_edits=[{"path": ["files", name], "delete": True}])
    add("host_extra_key", receipt_edits=[{"path": ["files", "extra"], "value": {}}])


def _add_fault_cases(add):
    add(
        "host_map_reverse_with_drift",
        faults=[{"target": "_probe", "return": {"fixture": "probe"}}],
        drift=list(reversed(list(HOSTS.values()))),
    )
    # Fault injection targets public module-global seams; other helpers remain real.
    for target, calls in (
        ("_owned", range(1, 5)),
        ("_latest_receipt", (1,)),
        ("_json", (1, 2, 3)),
        ("_bytes", (1, 2, 3)),
        ("_sha", (1, 2, 6, 7)),
        ("_relative_file", (1, 5)),
        ("_when", (1,)),
        ("_probe", (1,)),
        ("_json_bytes", (1, 2)),
    ):
        for call in calls:
            for error, message in (("invalid", "MISSING"), ("invalid", "UNTRUSTED_MODE"), ("os", "fixture os error")):
                add(
                    f"fault_{target.strip('_')}_{call}_{error}_{message.lower().replace(' ', '_')}",
                    faults=[{"target": target, "call": call, "error": error, "message": message}],
                )
    for target, call in (("_json", 1), ("_json", 2), ("_json", 3), ("_json_bytes", 1), ("_json_bytes", 2)):
        for label, value in (("none", None), ("list", []), ("string", "odd")):
            add(
                f"return_{target.strip('_')}_{call}_{label}", faults=[{"target": target, "call": call, "return": value}]
            )
    # A multi-error case establishes validation precedence before the common path.
    add(
        "precedence_receipt_before_package",
        receipt_edits=[edit("runtimeRoot", "wrong")],
        package_edits=[edit("name", "wrong")],
    )
    add("precedence_activation_before_drift", receipt_edits=[edit("status", "unknown")], drift=list(HOSTS.values()))


def _add_filesystem_cases(add):
    for value, label in ((1800000000.000001, "fractional"), (-0.000001, "negative_fractional")):
        add(f"now_{label}", now=value)
    for key in ("receipt_raw", "manifest_raw", "package_raw"):
        for label, raw in (
            ("truncated", "{"),
            ("empty", ""),
            ("list", "[]"),
            ("null", "null"),
            ("duplicate", '{"key":1,"key":2}'),
            ("nonfinite", '{"value":NaN}'),
            ("invalid_utf8", {"hex": "fffe"}),
        ):
            add(f"{key}_{label}", **{key: raw})
    for mode, identity in (("commit", COMMIT), ("generation", GENERATION)):
        add("symlink_runtime", modes=(mode,), symlinks=[f".local/share/nisi-runtime/{identity}/workflow/contracts.mjs"])
        add("symlink_manifest", modes=(mode,), symlinks=[f".local/share/nisi-runtime/{identity}/INSTALL-MANIFEST.json"])
        add(
            "symlink_receipt",
            modes=(mode,),
            symlinks=[f".local/share/nisi-runtime/rollback-{identity[:8]}-fixture/receipt.json"],
        )
    add("symlink_host", symlinks=[next(iter(HOSTS.values()))])
    add("host_map_reverse_order", reverse_host=True, drift=list(HOSTS.values()))
    add(
        "receipt_newest_valid",
        receipt_edits=[edit("status", "NOT_VERIFIED")],
        receipts=[{"edits": [edit("status", "VERIFIED")]}],
    )
    add("receipt_older_ignored", receipts=[{"mtime_ns": 500000000, "edits": [edit("status", "NOT_VERIFIED")]}])
    add(
        "receipt_equal_mtime_order",
        receipts=[{"suffix": "zulu", "mtime_ns": 1000000000, "edits": [edit("status", "NOT_VERIFIED")]}],
    )
    add("receipt_newer_invalid", receipts=[{"raw": "{"}])
    add("receipt_candidate_missing", receipts=[{"absent": True}])
    for count in (124, 125, 126):
        add(f"directory_entries_{count + 3}", ignored_entries=count)


def inputs():
    cases = []

    def add(name, modes=("commit", "generation"), **options):
        for mode in modes:
            cases.append({"name": name, "mode": mode, **copy.deepcopy(options)})

    _add_boundary_cases(add)
    _add_runtime_cases(add)
    _add_manifest_cases(add)
    _add_package_and_receipt_cases(add)
    _add_fault_cases(add)
    _add_filesystem_cases(add)
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=SUPPORT / "cases.json")
    parser.add_argument("--compare-python", action="append", default=[], metavar="EXECUTABLE")
    args = parser.parse_args()
    source = subprocess.check_output(["git", "-C", str(WORKTREE), "show", f"{BASE}:nisi_v02.py"], env=ENV)
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("Pinned source digest differs")
    if (WORKTREE / "nisi_v02.py").read_bytes() != source:
        raise RuntimeError("Source differs from pinned original")
    support = load("collect_golden_support", SUPPORT / "support.py")
    with tempfile.TemporaryDirectory(prefix="collector-original-") as temporary:
        path = Path(temporary) / "nisi_v02.py"
        path.write_bytes(source)
        module = load("collect_original", path)
        cases = inputs()
        for case in cases:
            case["expected"] = support.run_case(module, case)
    document = {
        "schema_version": 1,
        "generated_from": {
            "revision": BASE,
            "source": "nisi_v02.py",
            "sha256": hashlib.sha256(source).hexdigest(),
            "function": "collect_nisi_v02",
        },
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False) + "\n")
    for executable in args.compare_python:
        with tempfile.TemporaryDirectory(prefix="collector-compare-") as temporary:
            other = Path(temporary) / "cases.json"
            subprocess.run(
                [executable, "-B", str(Path(__file__).resolve()), "--output", str(other)],
                check=True,
                env=ENV,
            )
            if other.read_bytes() != args.output.read_bytes():
                raise RuntimeError("Interpreter corpus bytes differ")
    sys.stdout.write(
        json.dumps(
            {
                "cases": len(cases),
                "commit": sum(c["mode"] == "commit" for c in cases),
                "generation": sum(c["mode"] == "generation" for c in cases),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
