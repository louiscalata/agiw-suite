"""Read-only projection of the installed private Nisi Inference route.

This checks the activation owner's recorded pins against the current local
runtime and host bridge. It does not discover or start a workflow, and its
historical model probe is never evidence of current inference.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time


_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_RUNTIME_ID = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}\Z")
_ENDPOINT = re.compile(r"http://(?:127\.0\.0\.1|\[::1\])(?::[1-9][0-9]{0,4})?/(?:api/v1/models|api/tags)\Z")
_ROLLBACK = re.compile(r"rollback-[0-9a-f]{8}-[A-Za-z0-9_-]{1,40}\Z")
_SAFE_RELATIVE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")
_RUNTIME_FILES = frozenset({
    "package.json", "workflow/contracts.mjs", "adapters/local-chat.mjs",
    "hosts/local-models/opencode-orchestration-v1.mjs",
})
_HOST_FILES = {
    "nisi_auto_preflight.mjs": ".codex/skills/local-llm-orchestrator/scripts/nisi_auto_preflight.mjs",
    "nisi_bridge.mjs": ".codex/skills/local-llm-orchestrator/scripts/nisi_bridge.mjs",
    "nisi_local.ts": ".config/opencode/tools/nisi_local.ts",
    "nisi_validate.mjs": ".codex/skills/local-llm-orchestrator/scripts/nisi_validate.mjs",
    "pipeline_integrations.py": ".codex/skills/local-llm-orchestrator/scripts/pipeline_integrations.py",
}
_WORK_STATES = frozenset({"RESPONSE_VALIDATED", "UNAVAILABLE", "NOT_RUN"})
_VALIDATION_STATES = frozenset({"PASS", "FAIL", "NOT_RUN", "UNAVAILABLE"})
_MODEL_KEYS = frozenset({
    "runtimeId", "modelId", "displayName", "type", "sizeBytes", "reportedDigest",
    "presence", "loadedState", "instanceIds", "locality", "inference", "license",
    "providerFee", "useAuthorization",
})


class _InvalidEvidence(Exception):
    pass


def _owned(path: Path, *, directory: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise _InvalidEvidence("MISSING") from exc
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise _InvalidEvidence("UNTRUSTED_MODE")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise _InvalidEvidence("NOT_DIRECTORY")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise _InvalidEvidence("NOT_REGULAR")
    return info


def _bytes(path: Path, limit: int) -> bytes:
    before = _owned(path)
    if before.st_size > limit:
        raise _InvalidEvidence("TOO_LARGE")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                    or opened.st_mode & 0o022 or opened.st_ino != before.st_ino
                    or opened.st_dev != before.st_dev):
                raise _InvalidEvidence("CHANGED_DURING_READ")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(fd, min(65536, limit + 1 - total))
                if not block:
                    break
                chunks.append(block)
                total += len(block)
                if total > limit:
                    raise _InvalidEvidence("TOO_LARGE")
            if os.fstat(fd).st_size != total:
                raise _InvalidEvidence("CHANGED_DURING_READ")
            return b"".join(chunks)
        finally:
            os.close(fd)
    except OSError as exc:
        raise _InvalidEvidence("READ_FAILED") from exc


def _json(path: Path, limit: int) -> dict:
    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise _InvalidEvidence("DUPLICATE_KEY")
            value[key] = item
        return value

    try:
        value = json.loads(_bytes(path, limit).decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _value: (_ for _ in ()).throw(_InvalidEvidence("NONFINITE")))
    except (UnicodeError, ValueError) as exc:
        raise _InvalidEvidence("INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise _InvalidEvidence("INVALID_JSON")
    return value


def _sha(path: Path, limit: int) -> str:
    return hashlib.sha256(_bytes(path, limit)).hexdigest()


def _relative_file(root: Path, name: str) -> Path:
    if (not isinstance(name, str) or len(name) > 160 or not _SAFE_RELATIVE.fullmatch(name)
            or any(part in (".", "..") for part in name.split("/"))):
        raise _InvalidEvidence("INVALID_FILE_NAME")
    candidate = root
    for part in name.split("/")[:-1]:
        candidate = candidate / part
        _owned(candidate, directory=True)
    return candidate / name.split("/")[-1]


def _when(value: object, now: float) -> tuple[str | None, float | None]:
    if not isinstance(value, str) or len(value) > 40:
        return None, None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            return None, None
        seconds = moment.timestamp()
        if not math.isfinite(seconds) or seconds > now + 60:
            return None, None
        return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"), round(max(0, now - seconds), 3)
    except (ValueError, OverflowError):
        return None, None


def _latest_receipt(base: Path) -> Path | None:
    _owned(base, directory=True)
    try:
        entries = list(base.iterdir())
    except OSError as exc:
        raise _InvalidEvidence("READ_FAILED") from exc
    if len(entries) > 128:
        raise _InvalidEvidence("TOO_MANY_ENTRIES")
    candidates = []
    for entry in entries:
        if not _ROLLBACK.fullmatch(entry.name):
            continue
        _owned(entry, directory=True)
        receipt = entry / "receipt.json"
        try:
            candidates.append((_owned(receipt).st_mtime_ns, receipt))
        except _InvalidEvidence as exc:
            if str(exc) != "MISSING":
                raise
    return max(candidates, default=(0, None))[1]


def _probe(receipt: dict, now: float) -> dict | None:
    verification = receipt.get("verification")
    if not isinstance(verification, dict):
        return None
    work, validation = verification.get("work"), verification.get("validation")
    if not isinstance(work, dict) or not isinstance(validation, dict):
        return None
    if (work.get("status") not in _WORK_STATES
            or any(validation.get(key) not in _VALIDATION_STATES
                   for key in ("contract", "syntax", "tests", "certification"))
            or type(validation.get("accepted")) is not bool):
        return None
    routed_at, age = _when(receipt.get("routedAt"), now)
    return {"kind": "historical-local-model-probe", "observedAt": routed_at,
            "ageSeconds": age, "workStatus": work["status"],
            "contract": validation["contract"], "syntax": validation["syntax"],
            "tests": validation["tests"], "certification": validation["certification"],
            "accepted": validation["accepted"]}


def collect_nisi_v02(now: float, *, home: Path | None = None) -> tuple[dict, dict]:
    """Return a bounded snapshot and source row; never execute Nisi or a model."""
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("finite timestamp required")
    home = Path.home() if home is None else Path(home)
    base = home / ".local/share/nisi-runtime"
    observed = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    snapshot = {"schemaVersion": 1, "observedAt": observed,
                "integration": "private-local-orchestration-route", "version": None,
                "commit": None, "runtimeIntegrity": "UNKNOWN", "hostBinding": "UNKNOWN",
                "driftedHostFiles": [], "activationStatus": "UNKNOWN",
                "activationVerifiedAt": None, "activationAgeSeconds": None,
                "activationProbe": None, "liveInference": "UNKNOWN",
                "workflowAcceptance": "UNKNOWN", "releaseAcceptance": "NOT_ESTABLISHED"}
    source = {"id": "nisi-v02-runtime", "label": "Nisi Inference private runtime",
              "state": "unavailable", "ageSeconds": None,
              "detail": "No verified private Nisi Inference activation receipt observed"}
    try:
        _owned(home, directory=True)
        _owned(home / ".local", directory=True)
        _owned(home / ".local/share", directory=True)
        receipt_path = _latest_receipt(base)
        if receipt_path is None:
            return snapshot, source
        receipt = _json(receipt_path, 65536)
        commit = receipt.get("commit")
        if (not isinstance(commit, str) or not _COMMIT.fullmatch(commit)
                or not receipt_path.parent.name.startswith("rollback-" + commit[:8] + "-")):
            raise _InvalidEvidence("INVALID_COMMIT")
        runtime = base / commit
        _owned(runtime, directory=True)
        if receipt.get("runtimeRoot") != str(runtime):
            raise _InvalidEvidence("RUNTIME_ROOT_MISMATCH")
        manifest = _json(runtime / "INSTALL-MANIFEST.json", 16384)
        pins = manifest.get("files")
        if (manifest.get("commit") != commit or not isinstance(pins, dict)
                or not _RUNTIME_FILES.issubset(pins) or not 1 <= len(pins) <= 64):
            raise _InvalidEvidence("INVALID_MANIFEST")
        stamp, age = _when(receipt.get("verifiedAt"), now)
        archive_sha = manifest.get("archiveSha256")
        if (not isinstance(archive_sha, str) or not _SHA.fullmatch(archive_sha)
                or receipt.get("runtimeArchiveSha256") != archive_sha):
            raise _InvalidEvidence("ARCHIVE_PIN_INVALID")
        if _sha(base / f"{commit}.tar", 8 * 1024 * 1024) != archive_sha:
            raise _InvalidEvidence("ARCHIVE_DRIFT")
        for name, expected in pins.items():
            if not isinstance(expected, str) or not _SHA.fullmatch(expected):
                raise _InvalidEvidence("INVALID_FILE_PIN")
            if _sha(_relative_file(runtime, name), 2 * 1024 * 1024) != expected:
                raise _InvalidEvidence("RUNTIME_DRIFT")
        package = _json(runtime / "package.json", 16384)
        if (package.get("name") != "nisi" or package.get("version") != "0.2.0-private.0"
                or package.get("private") is not True):
            raise _InvalidEvidence("PACKAGE_IDENTITY_MISMATCH")
        snapshot["commit"] = commit
        snapshot["version"] = package["version"]
        snapshot["runtimeIntegrity"] = "VERIFIED"
        snapshot["activationStatus"] = ("VERIFIED" if receipt.get("status") == "VERIFIED" and stamp
                                        else "NOT_VERIFIED")
        snapshot["activationVerifiedAt"] = stamp
        snapshot["activationAgeSeconds"] = age
        source["ageSeconds"] = age
        snapshot["activationProbe"] = _probe(receipt, now) if snapshot["activationStatus"] == "VERIFIED" else None
        files = receipt.get("files")
        if not isinstance(files, dict) or set(files) != set(_HOST_FILES):
            raise _InvalidEvidence("INVALID_HOST_PINS")
        drift = []
        for name, relative in _HOST_FILES.items():
            pin = files[name]
            expected_path = home / relative
            if (not isinstance(pin, dict) or pin.get("installedPath") != str(expected_path)
                    or not isinstance(pin.get("afterSha256"), str)
                    or not _SHA.fullmatch(pin["afterSha256"])):
                raise _InvalidEvidence("INVALID_HOST_PIN")
            try:
                current = _sha(_relative_file(home, relative), 2 * 1024 * 1024)
            except _InvalidEvidence:
                current = None
            if current != pin["afterSha256"]:
                drift.append(name)
        snapshot["driftedHostFiles"] = drift
        snapshot["hostBinding"] = "DRIFT" if drift else "VERIFIED"
        if snapshot["activationStatus"] != "VERIFIED":
            source.update(state="error", detail="Private Nisi Inference runtime verified; activation not verified")
        elif drift:
            source.update(state="error", detail="Private Nisi Inference runtime verified; installed host bridge changed since activation")
        else:
            source.update(state="recorded", detail="Private Nisi Inference runtime and host bridge pins verified; activity unobserved")
    except _InvalidEvidence as exc:
        source.update(state="error" if str(exc) != "MISSING" else "unavailable",
                      detail="Private Nisi Inference activation evidence unavailable or invalid")
    return snapshot, source


def _bounded_process(argv: list[str], home: Path, timeout: float, output_limit: int) -> tuple[int, bytes]:
    """Drain one trusted status child without allowing an output or time escape."""
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                                     "LANG": "C", "LC_ALL": "C"})
    output = bytearray()
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    completed = False
    try:
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _InvalidEvidence("PROBE_TIMEOUT")
            if not selector.select(remaining):
                raise _InvalidEvidence("PROBE_TIMEOUT")
            chunk = os.read(process.stdout.fileno(), min(65536, output_limit + 1 - len(output)))
            if not chunk:
                selector.unregister(process.stdout)
                break
            output.extend(chunk)
            if len(output) > output_limit:
                raise _InvalidEvidence("PROBE_OUTPUT_LIMIT")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _InvalidEvidence("PROBE_TIMEOUT")
        exit_code = process.wait(timeout=remaining)
        completed = exit_code == 0
        return exit_code, bytes(output)
    except subprocess.TimeoutExpired as exc:
        raise _InvalidEvidence("PROBE_TIMEOUT") from exc
    finally:
        selector.close()
        # A leader may exit while a descendant keeps the inherited stdout
        # pipe open. Kill the entire session on every incomplete observation.
        if not completed:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if process.stdout is not None:
            process.stdout.close()


def _node_binary(override: Path | None) -> Path:
    candidates = (Path(override),) if override is not None else (
        Path("/usr/local/bin/node"), Path("/opt/homebrew/bin/node"), Path("/usr/bin/node"))
    for candidate in candidates:
        try:
            info = candidate.lstat()
            if (stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid())
                    and not info.st_mode & 0o022 and info.st_mode & 0o111):
                return candidate
        except OSError:
            continue
    raise _InvalidEvidence("NODE_UNAVAILABLE")


def _inventory_counts(inventory: dict) -> tuple[int, int]:
    rows = inventory["runtimes"]
    seen_runtimes: set[str] = set()
    listed = 0
    model_count = 0
    for row in rows:
        if not isinstance(row, dict):
            raise _InvalidEvidence("INVENTORY_ROW_INVALID")
        runtime_id, provider, endpoint = row.get("runtimeId"), row.get("provider"), row.get("endpoint")
        if (not isinstance(runtime_id, str) or not _RUNTIME_ID.fullmatch(runtime_id)
                or runtime_id in seen_runtimes or provider not in {"lmstudio-v1", "ollama-tags"}
                or not isinstance(endpoint, str) or not _ENDPOINT.fullmatch(endpoint)
                or not endpoint.endswith("/api/v1/models" if provider == "lmstudio-v1" else "/api/tags")):
            raise _InvalidEvidence("INVENTORY_IDENTITY_INVALID")
        seen_runtimes.add(runtime_id)
        state, models = row.get("status"), row.get("models")
        if (state not in {"LISTED", "UNAVAILABLE", "TIMED_OUT", "CANCELLED", "AUTH_REQUIRED", "NOT_RUN"}
                or not isinstance(models, list) or len(models) > 256):
            raise _InvalidEvidence("INVENTORY_ROW_INVALID")
        if state != "LISTED":
            if models or row.get("responseSha256") is not None:
                raise _InvalidEvidence("UNLISTED_MODELS_INVALID")
            continue
        if (row.get("code") is not None or not isinstance(row.get("responseSha256"), str)
                or not _SHA.fullmatch(row["responseSha256"])):
            raise _InvalidEvidence("LISTED_RECEIPT_INVALID")
        listed += 1
        seen_models: set[str] = set()
        for model in models:
            if not isinstance(model, dict) or set(model) != _MODEL_KEYS:
                raise _InvalidEvidence("MODEL_SCHEMA_INVALID")
            model_id, display = model.get("modelId"), model.get("displayName")
            if (model.get("runtimeId") != runtime_id or not isinstance(model_id, str)
                    or not _MODEL_ID.fullmatch(model_id) or model_id in seen_models
                    or not isinstance(display, str) or not 1 <= len(display) <= 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in display)
                    or model.get("type") not in {"llm", "embedding", "UNKNOWN"}
                    or model.get("presence") != "PROVIDER_LISTED"
                    or model.get("loadedState") not in {"REPORTED_LOADED", "REPORTED_NOT_LOADED", "UNKNOWN"}
                    or model.get("locality") != "NOT_VERIFIED" or model.get("inference") != "NOT_TESTED"
                    or model.get("license") != "NOT_REVIEWED" or model.get("providerFee") != "UNKNOWN"
                    or model.get("useAuthorization") != "NONE"):
                raise _InvalidEvidence("MODEL_IDENTITY_INVALID")
            size, digest, instances = (model.get("sizeBytes"), model.get("reportedDigest"),
                                       model.get("instanceIds"))
            if ((size is not None and (type(size) is not int or size < 0))
                    or (digest is not None and (not isinstance(digest, str)
                                                or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", digest)))
                    or not isinstance(instances, list) or len(instances) > 64
                    or any(not isinstance(item, str) or not 1 <= len(item) <= 256
                           or any(ord(char) < 32 or ord(char) == 127 for char in item)
                           for item in instances)
                    or len(set(instances)) != len(instances)):
                raise _InvalidEvidence("MODEL_FIELDS_INVALID")
            seen_models.add(model_id)
        model_count += len(models)
    expected = ("NOT_CONFIGURED" if not rows else "LISTED" if listed == len(rows)
                else "PARTIAL" if listed else "UNAVAILABLE")
    if inventory["status"] != expected:
        raise _InvalidEvidence("INVENTORY_AGGREGATE_INVALID")
    return len(rows), model_count


def probe_nisi_v02_bridge(now: float, *, home: Path | None = None,
                          node_binary: Path | None = None, timeout: float = 2.0) -> dict:
    """Explicit button-only Nisi inventory check; never invoke work or recover."""
    if not isinstance(now, (int, float)) or not math.isfinite(now) or not 0 < timeout <= 5:
        raise ValueError("invalid probe bound")
    home = Path.home() if home is None else Path(home)
    observed = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    result = {"schemaVersion": 1, "kind": "agiw.nisi-v02.bridge-check.v1",
              "observedAt": observed, "status": "REFUSED", "inventoryStatus": "UNKNOWN",
              "runtimeCount": None, "modelCount": None, "recoveryRequired": None,
              "activationHostBinding": "UNKNOWN", "modelInference": "NOT_RUN",
              "workflowAcceptance": "NOT_RUN", "releaseAcceptance": "NOT_ESTABLISHED",
              "detail": "Installed private Nisi Inference bridge has not been checked"}
    installed, _ = collect_nisi_v02(now, home=home)
    result["activationHostBinding"] = installed["hostBinding"]
    result["driftedHostFiles"] = installed["driftedHostFiles"]
    if (installed["runtimeIntegrity"] != "VERIFIED"
            or "nisi_bridge.mjs" in installed["driftedHostFiles"]
            or installed["activationStatus"] != "VERIFIED"):
        result["detail"] = "Private Nisi Inference runtime or exact bridge script is not verified"
        return result
    try:
        node = _node_binary(node_binary)
        bridge = _relative_file(home, _HOST_FILES["nisi_bridge.mjs"])
        code, raw = _bounded_process([str(node), str(bridge), "status"], home, timeout, 2 * 1024 * 1024)
        if code != 0 or len(raw) > 2 * 1024 * 1024:
            raise _InvalidEvidence("BRIDGE_RETURNED_ERROR")
        def unique(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise _InvalidEvidence("DUPLICATE_KEY")
                value[key] = item
            return value
        envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                              parse_constant=lambda _value: (_ for _ in ()).throw(_InvalidEvidence("NONFINITE")))
        if (not isinstance(envelope, dict) or envelope.get("kind") != "codemode.nisi.bridge.v1"
                or envelope.get("operation") != "status" or envelope.get("status") != "RETURNED"
                or type(envelope.get("recoveryRequired")) is not bool):
            raise _InvalidEvidence("BRIDGE_SCHEMA_INVALID")
        inventory = envelope.get("result")
        if (not isinstance(inventory, dict) or type(inventory.get("schemaVersion")) is not int
                or inventory["schemaVersion"] != 1
                or inventory.get("scope") != "REGISTERED_RUNTIME_INVENTORY_ONLY"
                or inventory.get("status") not in {"LISTED", "PARTIAL", "UNAVAILABLE", "NOT_CONFIGURED"}
                or not isinstance(inventory.get("runtimes"), list)
                or len(inventory["runtimes"]) > 8):
            raise _InvalidEvidence("INVENTORY_SCHEMA_INVALID")
        runtime_count, model_count = _inventory_counts(inventory)
        result.update(status="RETURNED", inventoryStatus=inventory["status"],
                      runtimeCount=runtime_count, modelCount=model_count,
                      recoveryRequired=envelope["recoveryRequired"],
                      detail="Current private Nisi Inference bridge returned inventory only; inference untested")
    except _InvalidEvidence as exc:
        result.update(status="TIMED_OUT" if str(exc) == "PROBE_TIMEOUT" else "UNAVAILABLE",
                      detail="Current private Nisi Inference bridge status unavailable")
    except (OSError, UnicodeError, ValueError, TypeError):
        result.update(status="UNAVAILABLE", detail="Current private Nisi Inference bridge status unavailable")
    return result
