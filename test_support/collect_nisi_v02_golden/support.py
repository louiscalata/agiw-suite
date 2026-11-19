"""Isolated inputs and ordered patch targets for the collector's recorded cases."""

import copy
import hashlib
import json
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from test_support.exception_outcome import pinned_message

COMMIT = "1234567890abcdef1234567890abcdef12345678"
GENERATION = "abcdef0123456789abcdef0123456789abcdef01"
NOW = 1800000000.0
RUNTIME_FILES = (
    "package.json",
    "workflow/contracts.mjs",
    "adapters/local-chat.mjs",
    "hosts/local-models/opencode-orchestration-v1.mjs",
)
HELPERS = ("_owned", "_latest_receipt", "_json", "_bytes", "_sha", "_relative_file", "_when", "_probe", "_json_bytes")


def _write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    path.chmod(0o600)
    return hashlib.sha256(contents).hexdigest()


def _json_raw(value, size=None):
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    if size is not None:
        if size < len(raw):
            raise ValueError("Fixture JSON size is smaller than its content")
        raw += b" " * (size - len(raw))
    return raw


def _raw(case, key, default):
    value = case.get(key)
    if isinstance(value, dict):
        return bytes.fromhex(value["hex"])
    return default if value is None else value.encode()


def _expand(value, home):
    if isinstance(value, str):
        return value.replace("<lane folder>", str(home))
    if isinstance(value, list):
        return [_expand(item, home) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, home) for key, item in value.items()}
    return value


def _edit(value, changes, home):
    for change in changes:
        parent = value
        for key in change["path"][:-1]:
            parent = parent[key]
        key = change["path"][-1]
        if change.get("delete"):
            parent.pop(key, None)
        else:
            parent[key] = _expand(copy.deepcopy(change["value"]), home)
    return value


def _runtime(module, case, home, runtime, base, identity):
    package = _edit(
        {"name": "nisi", "version": "0.2.0-private.0", "private": True}, case.get("package_edits", []), home
    )
    names = list(RUNTIME_FILES)
    names += [f"extra/{index:03d}.mjs" for index in range(max(0, case.get("pin_count", 4) - 4))]
    if case.get("reverse_runtime"):
        names.reverse()
    pins = {}
    for name in names:
        raw = _json_raw(package, case.get("package_size")) if name == "package.json" else b"fixture runtime\n"
        if name == "workflow/contracts.mjs" and "runtime_size" in case:
            raw = b"x" * case["runtime_size"]
        if name == "package.json":
            raw = _raw(case, "package_raw", raw)
        pins[name] = _write(runtime / name, raw)
    archive_sha = _write(base / f"{identity}.tar", b"a" * case.get("archive_size", 23))
    manifest = {"files": pins}
    if case["mode"] == "generation":
        manifest.update(generation=identity, schemaVersion=2)
    else:
        manifest.update(commit=identity, archiveSha256=archive_sha)
    manifest = _edit(manifest, case.get("manifest_edits", []), home)
    raw = _raw(case, "manifest_raw", _json_raw(manifest, case.get("manifest_size")))
    manifest_sha = _write(runtime / "INSTALL-MANIFEST.json", raw)
    return archive_sha, manifest_sha


def _fixture(module, case, home):
    home.mkdir(mode=0o700)
    if case.get("layout") == "empty":
        return
    base = home / ".local/share/nisi-runtime"
    base.mkdir(parents=True, mode=0o700)
    if case.get("layout") == "no_receipt":
        return
    identity = GENERATION if case["mode"] == "generation" else COMMIT
    runtime = base / identity
    archive_sha, manifest_sha = _runtime(module, case, home, runtime, base, identity)
    hosts = {}
    for name, relative in module._HOST_FILES.items():
        hosts[name] = {
            "installedPath": str(home / relative),
            "afterSha256": _write(home / relative, b"h" * case.get("host_size", 17)),
        }
    receipt = {
        "runtimeRoot": str(runtime),
        "status": "VERIFIED",
        "verifiedAt": "2027-01-15T07:59:50Z",
        "routedAt": "2027-01-15T07:59:40Z",
        "files": hosts,
        "verification": {
            "work": {"status": "RESPONSE_VALIDATED", "prompt": "fixture private prompt"},
            "validation": {
                "contract": "PASS",
                "syntax": "PASS",
                "tests": "NOT_RUN",
                "certification": "NOT_RUN",
                "accepted": False,
            },
        },
    }
    if case["mode"] == "generation":
        receipt.update(kind="nisi.runtime.activation.v2", generation=identity, manifestSha256=manifest_sha)
    else:
        receipt.update(commit=identity, runtimeArchiveSha256=archive_sha)
    receipt = _edit(receipt, case.get("receipt_edits", []), home)
    folder = case.get("receipt_folder", f"rollback-{identity[:8]}-fixture")
    if case.get("reverse_host"):
        receipt["files"] = dict(reversed(list(receipt["files"].items())))
    receipt_path = base / folder / "receipt.json"
    _write(receipt_path, _raw(case, "receipt_raw", _json_raw(receipt, case.get("receipt_size"))))
    _extra_receipts(case, home, base, receipt_path, receipt, identity)
    for relative in case.get("drift", []):
        _write(home / relative, b"changed fixture")
    for relative in case.get("missing", []):
        (home / relative).unlink()
    for index, relative in enumerate(case.get("symlinks", [])):
        path = home / relative
        target = home / f"fixture-link-target-{index}"
        _write(target, path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    if case.get("untrusted_home"):
        home.chmod(0o777)


def _extra_receipts(case, home, base, receipt_path, receipt, identity):
    if "receipts" in case:
        os.utime(receipt_path, ns=(1000000000, 1000000000))
    for index, candidate in enumerate(case.get("receipts", [])):
        path = base / f"rollback-{identity[:8]}-{candidate.get('suffix', str(index))}" / "receipt.json"
        if candidate.get("absent"):
            path.parent.mkdir(mode=0o700)
            continue
        data = _edit(copy.deepcopy(receipt), candidate.get("edits", []), home)
        _write(path, _raw(candidate, "raw", _json_raw(data)))
        stamp = candidate.get("mtime_ns", 2000000000)
        os.utime(path, ns=(stamp, stamp))
    for index in range(case.get("ignored_entries", 0)):
        (base / f"ignored-{index:03d}").mkdir(mode=0o700)


def _normalize(value, home):
    if isinstance(value, Path):
        return str(value).replace(str(home), "<lane folder>")
    if isinstance(value, bytes):
        raw = value.replace(str(home).encode(), b"<lane folder>")
        if value.endswith(b" "):
            # Size-boundary JSON keeps its requested byte length after paths
            # shrink; canonical padding removes the temp prefix's influence.
            core = raw.rstrip(b" ")
            raw = core + b" " * (len(value) - len(core))
        return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if isinstance(value, str):
        return value.replace(str(home), "<lane folder>")
    if isinstance(value, (list, tuple)):
        return [_normalize(item, home) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item, home) for key, item in value.items()}
    return value


def _patched(module, name, original, case, home, events):
    calls = 0

    def wrapped(*args, **kwargs):
        nonlocal calls
        calls += 1
        events.append([name, _normalize(args, home), _normalize(kwargs, home)])
        for fault in case.get("faults", []):
            if fault["target"] != name or fault.get("call", 1) != calls:
                continue
            if "return" in fault:
                return _expand(copy.deepcopy(fault["return"]), home)
            errors = {"invalid": module._InvalidEvidence, "os": OSError, "value": ValueError, "type": TypeError}
            raise errors[fault.get("error", "invalid")](fault.get("message", "fixture error"))
        return original(*args, **kwargs)

    return wrapped


def _timestamp(value):
    if isinstance(value, dict):
        if "float" in value:
            return float(value["float"])
        if "power" in value:
            return 10 ** value["power"]
    return value


def run_case(module, case):
    """Run one corpus input with no access to a real home, service or child."""
    events = []
    with tempfile.TemporaryDirectory(prefix="collect-golden-") as temporary:
        home = Path(temporary) / "home"
        _fixture(module, case, home)
        originals = {name: getattr(module, name) for name in HELPERS}
        original_iterdir = module.Path.iterdir
        with ExitStack() as stack:
            for name, original in originals.items():
                stack.enter_context(
                    mock.patch.object(module, name, _patched(module, name, original, case, home, events))
                )

            def default_home():
                events.append(["Path.home", [], {}])
                return home

            def directory_entries(path):
                events.append(["Path.iterdir", [_normalize(path, home)], {}])
                return iter(sorted(original_iterdir(path), key=lambda entry: entry.name))

            stack.enter_context(mock.patch.object(module.Path, "home", side_effect=default_home))
            stack.enter_context(mock.patch.object(module.Path, "iterdir", directory_entries))
            child = stack.enter_context(mock.patch.object(module.subprocess, "Popen", side_effect=AssertionError))
            now = _timestamp(case.get("now", NOW))
            supplied_home = str(home) if case.get("home") == "string" else home
            if "home_value" in case:
                supplied_home = case["home_value"]
            try:
                result = (
                    module.collect_nisi_v02(now)
                    if case.get("home") == "default"
                    else module.collect_nisi_v02(now, home=supplied_home)
                )
                outcome = {"result": result}
            except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
                outcome = {"error": type(error).__name__}
                message = pinned_message(error, module)
                if message is not None:
                    outcome["message"] = message
            child.assert_not_called()
        outcome["events"] = events
        return _normalize(outcome, home)


def load_cases(mode):
    document = json.loads(Path(__file__).with_name("cases.json").read_text(encoding="utf-8"))
    return [case for case in document["cases"] if case["mode"] == mode]
