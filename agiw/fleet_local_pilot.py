"""Isolated M5 synthetic smoke: route policy -> Nisi -> loopback local-chat.
No automatic model loads, peer dispatch, hosted route or credential access.
"""

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .fleet_routing_plan import WORKERS, HostedEvidence, WorkerEvidence, plan
from .windows_worker_receipt_check import valid_route

try:  # POSIX lock; Windows has no fcntl and uses msvcrt.locking instead.
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "preview-pilot"
PIN = "be7a5c99338947baf130bc0d0364eef12cbef5cb"
PROGRAMS = ("nisi-smoke.mjs", "tiny-arithmetic.mjs", "tiny-extraction.mjs")


class RefusedError(RuntimeError):
    """A fail-closed refusal; the message is a stable code."""


Refused = RefusedError  # established name used by callers and tests

# What a local metadata/HTTP probe can raise: URL/socket errors, bad JSON, protocol errors, refusals.
PROBE_ERRORS = (OSError, ValueError, http.client.HTTPException, RuntimeError)


def emit(text, file=None):
    """CLI output (stdout unless a stream is given), one line."""
    (file or sys.stdout).write(str(text) + "\n")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise Refused("REDIRECT_REFUSED")


NODE_FALLBACK_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")


def node_binary():
    """AGIW_NODE, else PATH, else common user/Homebrew dirs (launchd's PATH is minimal). Fail closed."""
    chosen = os.environ.get("AGIW_NODE") or shutil.which("node")
    if not chosen and os.name != "nt":
        for folder in NODE_FALLBACK_DIRS:
            candidate = os.path.join(os.path.expanduser(folder), "node")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                chosen = candidate
                break
    if not chosen:
        raise Refused("NODE_NOT_FOUND")
    return chosen


def endpoint(value):
    u = urlsplit(value)
    if (
        u.scheme != "http"
        or u.hostname != "127.0.0.1"
        or u.username
        or u.password
        or u.query
        or u.fragment
        or u.path != "/v1/chat/completions"
        or not u.port
    ):
        raise Refused("LOOPBACK_ENDPOINT_REQUIRED")
    return f"http://127.0.0.1:{u.port}"


def get(url):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with op.open(url, timeout=2) as r:
        if r.status != 200:
            raise Refused("HEALTH_HTTP")
        data = r.read(65537)
        if len(data) > 65536:
            raise Refused("METADATA_CAP")
        return json.loads(data)


def _model_ids(items, loaded_only):
    ids = []
    for m in items:
        if not isinstance(m, dict):
            continue
        state = m.get("state")
        if loaded_only and state is not None and state != "loaded":
            continue
        ident = m.get("id")
        if isinstance(ident, str) and ident and len(ident) <= 200:
            ids.append(ident)
    if len(ids) > 64:
        raise Refused("MODELS_CAP")
    return ids


def detect_models(base_url, getter=get):
    """llama-server /v1/models first; LM Studio /api/v0/models only on 404/unavailable."""
    try:
        data = getter(base_url + "/v1/models")
    except PROBE_ERRORS:
        data, unavailable = None, True
    else:
        unavailable = False
    if not unavailable:
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise Refused("MODELS_INVALID")
        return _model_ids(data["data"], False), "llama-server"
    try:
        data = getter(base_url + "/api/v0/models")
    except PROBE_ERRORS as exc:
        raise Refused("MODELS_UNAVAILABLE") from exc
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise Refused("MODELS_INVALID")
    return _model_ids(data["data"], True), "lm-studio"


def _lmstudio_loaded(item, model):
    ctx = item.get("loaded_context_length") if isinstance(item, dict) else None
    return (
        isinstance(item, dict)
        and item.get("id") == model
        and item.get("state") == "loaded"
        and type(ctx) is int
        and ctx > 0
    )


def readiness(url, model, getter=get):
    base = endpoint(url)
    try:
        health = getter(base + "/health")
    except PROBE_ERRORS:
        health = None
    if isinstance(health, dict) and health.get("status") == "ok":
        models, _source = detect_models(base, getter=getter)
        if model not in models:
            raise Refused("MODEL_NOT_VERIFIED")
        # llama.cpp /slots must explicitly establish every slot is idle. Missing is unknown.
        slots = getter(base + "/slots")
        if not isinstance(slots, list) or not slots or any(s.get("is_processing") is not False for s in slots):
            raise Refused("IDLE_NOT_VERIFIED")
        return WorkerEvidence(time.time(), True, True, True, False)
    # LM Studio serves /v1/models but has no llama-server /health or /slots; those
    # routes return 200 with an Unexpected endpoint error object.
    try:
        catalog = getter(base + "/api/v0/models")
    except PROBE_ERRORS as exc:
        raise Refused("HEALTH_NOT_OK") from exc
    items = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(items, list) or not any(_lmstudio_loaded(item, model) for item in items):
        raise Refused("MODEL_NOT_VERIFIED" if isinstance(items, list) else "HEALTH_NOT_OK")
    return WorkerEvidence(time.time(), True, True, True, False)


ROUTER_CONFIG = PILOT / "fleet-workers.json"
# Per-lane model-call timeout (seconds). Monterey generates at ~1 tok/s, so it needs far longer.
# Lane defaults (max_tokens, model_timeout_s, job_timeout_s) live in fleet-workers.json "lanes".
DEFAULT_MODEL_TIMEOUT_S = 30
MAX_MODEL_TIMEOUT_S = 900
MIN_MAX_TOKENS, MAX_MAX_TOKENS = 16, 4096


def lane_defaults(worker_id, path=None):
    config = json.loads(Path(path or ROUTER_CONFIG).read_text())
    lanes = config.get("lanes", {}) if isinstance(config, dict) else {}
    if not isinstance(lanes, dict):
        raise Refused("ROUTER_CONFIG_INVALID")
    lane = lanes.get(worker_id, {})
    if not isinstance(lane, dict):
        raise Refused("ROUTER_CONFIG_INVALID")
    return lane


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value == value


def model_timeout_s(worker_id, value=None):
    chosen = lane_defaults(worker_id).get("model_timeout_s", DEFAULT_MODEL_TIMEOUT_S) if value is None else value
    if not _number(chosen) or not 1 <= chosen <= MAX_MODEL_TIMEOUT_S:
        raise Refused("MODEL_TIMEOUT_INVALID")
    return int(chosen)


def max_tokens(worker_id, value=None):
    """Output-token cap: job/worker value, else the lane default, else None (program default)."""
    chosen = lane_defaults(worker_id).get("max_tokens") if value is None else value
    if chosen is None:
        return None
    if isinstance(chosen, bool) or not isinstance(chosen, int) or not MIN_MAX_TOKENS <= chosen <= MAX_MAX_TOKENS:
        raise Refused("MAX_TOKENS_INVALID")
    return chosen


RESPONSE_FORMATS = ("json-object", "json-schema")


def response_format(worker_id, value=None):
    """Opt-in structured output: job/worker value, else the lane default, else None (program default)."""
    chosen = lane_defaults(worker_id).get("response_format") if value is None else value
    if chosen is None:
        return None
    if chosen not in RESPONSE_FORMATS:
        raise Refused("RESPONSE_FORMAT_INVALID")
    return chosen


REQUEST_MODES = ("chat", "completion-grammar")
COMPLETION_CONSTRAINTS = ("grammar", "json-schema")
_TEMPLATE_TEXT = re.compile(r"[\x20-\x7e\n]{0,160}")


def request_mode(worker_id, value=None):
    """'chat' (default) or the opt-in 'completion-grammar': job/worker value, else the lane default."""
    chosen = lane_defaults(worker_id).get("request_mode") if value is None else value
    if chosen is None:
        return "chat"
    if chosen not in REQUEST_MODES:
        raise Refused("REQUEST_MODE_INVALID")
    return chosen


def completion_settings(worker_id):
    """Lane-only completion-grammar settings, validated; None means the program default."""
    lane = lane_defaults(worker_id)
    out = {}
    for key, cfg_key, code in (
        ("completion_prefill", "completionPrefill", "COMPLETION_PREFILL_INVALID"),
        ("prompt_suffix", "promptSuffix", "PROMPT_SUFFIX_INVALID"),
    ):
        value = lane.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not _TEMPLATE_TEXT.fullmatch(value):
            raise Refused(code)
        out[cfg_key] = value
    constraint = lane.get("completion_constraint")
    if constraint is not None:
        if constraint not in COMPLETION_CONSTRAINTS:
            raise Refused("COMPLETION_CONSTRAINT_INVALID")
        out["completionConstraint"] = constraint
    return out


_EVIDENCE_KEYS = {
    "httpStatus": int,
    "finishReason": str,
    "errorName": str,
    "errorCode": str,
    "errorMessage": str,
    "serverMessage": str,
    "elapsedMs": (int, float),
    "reportedModel": str,
    "requestedModel": str,
    "status": str,
    "timeoutMs": int,
    "disableThinking": bool,
    "code": str,
    "outputSample": str,
    "preambleStripped": str,
    "maxOutputTokens": int,
    "responseFormat": str,
    "attempts": int,
    "responseFormatFallback": str,
    "fallbackHttpStatus": int,
    "fallbackServerMessage": str,
    "requestMode": str,
    "templateRender": str,
    "stopType": str,
    "tokensPredicted": int,
    "constraint": str,
    "constraintFallback": str,
}
_REDACT = [
    (re.compile(r"(bearer|token|api[_-]?key|authorization|password|secret)\s*[:=]?\s*\S+", re.I), r"\1 [redacted]"),
    (re.compile(r'[A-Za-z]:\\[^\s\'"]+'), "[path]"),
    (re.compile(r'(^|[\s\'"(=])/[^\s\'"]+'), r"\1[path]"),
    (re.compile(r"[A-Za-z0-9+/_=-]{24,}"), "[redacted]"),
]


def sanitize_text(value, limit=200):
    if not isinstance(value, str) or not value:
        return None
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    for pattern, repl in _REDACT:
        text = pattern.sub(repl, text)
    return text.strip()[:limit] or None


def author_evidence(receipts):
    """Whitelisted, sanitized per-call evidence for receipts: no payloads, tokens or paths."""
    out = []
    for item in (receipts if isinstance(receipts, list) else [])[:4]:
        if not isinstance(item, dict):
            continue
        row = {}
        for key, kind in _EVIDENCE_KEYS.items():
            value = item.get(key)
            if isinstance(value, kind) and not (kind is int and isinstance(value, bool)):
                row[key] = (
                    sanitize_text(value, 300 if key == "outputSample" else 200) if isinstance(value, str) else value
                )
        out.append(row)
    return out


def preview_post_text(program, result):
    """Project a preview-program result into the bounded string jev-task post reviews.

    Paths, endpoints, tokens and raw payloads are omitted. The hashed stored report is not
    rewritten; post-analysis is recorded on the job receipt only.
    """
    result = result if isinstance(result, dict) else {}
    report = result.get("report") if isinstance(result.get("report"), dict) else {}
    name = program if isinstance(program, str) else ""
    parts = [f"program: {name}"]
    for key in ("outcome", "code"):
        value = report.get(key)
        if isinstance(value, str) and value:
            parts.append(f"{key}: {sanitize_text(value, 200)}")
        else:
            parts.append(f"{key}:")
    exit_code = result.get("childExitCode")
    if type(exit_code) is int and not isinstance(exit_code, bool):
        parts.append(f"childExitCode: {exit_code}")
    evidence = result.get("authorEvidence")
    if not isinstance(evidence, list):
        evidence = result.get("authorReceipts")
    evidence = author_evidence(evidence)
    if evidence:
        parts.append("authorEvidence:")
        parts.append(json.dumps(evidence, sort_keys=True, ensure_ascii=True))
    return "\n".join(parts) + "\n"


def _check_child_plan(data, route):
    """Project evidence against the validated router summary; never persist extra child fields."""
    evidence = data.pop("jevPlan", None)
    if route["preanalysis"] == "local":
        if evidence is not None:
            raise Refused("JEV_PLAN_INVALID")
        return
    if data.get("code") == "JEV_PLAN_REFUSED" and data.get("report") is None and data["childExitCode"] == 1:
        return "JEV_PLAN_REFUSED"
    fields = ("plan", "confidence", "underspecified", "jevModel")
    if not isinstance(evidence, dict) or not all(
        k in evidence and not isinstance(evidence[k], bool) and evidence[k] == route["jev"][k] for k in fields
    ):
        raise Refused("JEV_PLAN_INVALID")
    digest = evidence.get("requestSha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise Refused("JEV_PLAN_INVALID")
    data["jevPlan"] = {k: evidence[k] for k in fields}
    data["jevPlan"].update(jevModel=sanitize_text(evidence["jevModel"]), requestSha256=digest)


def known_workers(path=None):
    """Worker ids the router config knows. Must equal fleet_routing_plan.WORKERS."""
    config = json.loads(Path(path or ROUTER_CONFIG).read_text())
    workers = config.get("workers") if isinstance(config, dict) else None
    if (
        not isinstance(config, dict)
        or config.get("schemaVersion") != 1
        or not isinstance(workers, list)
        or not workers
        or not all(isinstance(w, str) and w for w in workers)
        or len(set(workers)) != len(workers)
    ):
        raise Refused("ROUTER_CONFIG_INVALID")
    if set(workers) != set(WORKERS):
        raise Refused("ROUTER_CONFIG_MISMATCH")
    return tuple(workers)


def check_worker_id(worker_id):
    if not isinstance(worker_id, str) or worker_id not in known_workers():
        raise Refused("UNKNOWN_WORKER_ID")
    return worker_id


def _local_route(evidence, source="agent", now=None, worker_id="m5-macbook-pro"):
    check_worker_id(worker_id)
    result = plan(
        "local",
        "routine",
        {worker_id: evidence},
        HostedEvidence(),
        now=time.time() if now is None else now,
        source=source,
    )
    if result.worker != worker_id or result.preanalysis != "local":
        raise Refused(result.reason)
    return asdict(result)


route = _local_route  # Public standalone planner.


def _is_windows():
    return os.name == "nt"


def try_lock(handle):
    """Non-blocking exclusive preview lock. Fail closed when no lock primitive exists."""
    if fcntl is not None:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError) as exc:
            raise Refused("PREVIEW_ALREADY_RUNNING") from exc
        return "fcntl"
    if msvcrt is not None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise Refused("PREVIEW_ALREADY_RUNNING") from exc
        return "msvcrt"
    raise Refused("LOCK_UNAVAILABLE")


def git_binary():
    chosen = os.environ.get("AGIW_GIT") or shutil.which("git")
    if not chosen:
        raise Refused("GIT_NOT_FOUND")
    return chosen


def clean_env():
    home = str(PILOT / "empty-home")
    env = {"HOME": home, "TMPDIR": home, "PYTHONDONTWRITEBYTECODE": "1"}
    if _is_windows():
        root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        dirs = [os.path.join(root, "System32"), root]
        for tool in (
            os.environ.get("AGIW_NODE") or shutil.which("node"),
            os.environ.get("AGIW_GIT") or shutil.which("git"),
        ):
            if tool:
                dirs.append(os.path.dirname(tool))
        env.update(PATH=os.pathsep.join(dirs), SYSTEMROOT=root, TEMP=home, TMP=home, USERPROFILE=home)
    else:
        env["PATH"] = "/usr/bin:/bin:/usr/local/bin"
    return env


def _dirty_summary(porcelain):
    entries = [line[:3] + (sanitize_text(line[3:], 80) or "") for line in porcelain.splitlines()[:3]]
    more = len(porcelain.splitlines()) - len(entries)
    return "; ".join(entries) + (f" (+{more} more)" if more > 0 else "")


def integrity():
    """Pinned Nisi checkout must be at PIN with no tracked or untracked changes. Called under the pilot lock.

    GIT_OPTIONAL_LOCKS=0 keeps `git status` read-only (no index.lock races with a concurrent git).
    On Windows the checkout is normally made with Git for Windows' system core.autocrlf=true; with
    system config disabled, a stale index stat cache made `git status` content-compare CRLF files as
    modified until something else refreshed the index. Pin autocrlf=true there so only real changes count.
    """
    vendor = PILOT / "vendor/nisi-0.3.1"
    git_bin = git_binary()
    env = dict(clean_env(), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    eol = ["-c", "core.autocrlf=true"] if _is_windows() else []

    def git(*a):
        try:
            return subprocess.check_output(
                [git_bin, *eol, "-C", str(vendor), *a], env=env, text=True, timeout=10, stderr=subprocess.PIPE
            ).strip()
        except subprocess.CalledProcessError as exc:
            raise Refused("NISI_GIT_FAILED: " + (sanitize_text(exc.stderr or "", 160) or str(exc.returncode))) from exc

    head = git("rev-parse", "HEAD")
    if head != PIN:
        raise Refused("NISI_PIN_MISMATCH")
    dirty = git("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
    if dirty:
        raise Refused("NISI_CHECKOUT_DIRTY: " + _dirty_summary(dirty))
    return PIN


_NODE_MAJOR = {}


def node_major(node_bin):
    if node_bin not in _NODE_MAJOR:
        try:
            out = subprocess.check_output([node_bin, "--version"], text=True, timeout=10, env=clean_env()).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise Refused("NODE_VERSION_UNKNOWN") from exc
        m = re.fullmatch(r"v(\d+)\.\d+\.\d+.*", out)
        if not m:
            raise Refused("NODE_VERSION_UNKNOWN")
        _NODE_MAJOR[node_bin] = int(m.group(1))
    return _NODE_MAJOR[node_bin]


def net_flags(major, mode):
    """Node >=25 denies network under --permission unless --allow-net, which cannot be scoped (values are
    ignored), so live mode passes bare --allow-net and the in-process net guard (net-guard.mjs) limits sockets
    to the configured 127.0.0.1:port. Node 22-24 does not gate network and rejects --allow-net, so only the
    guard applies. Fixture mode never gets network. Older Node is refused."""
    if major < 22:
        raise Refused("NODE_TOO_OLD")
    return ["--allow-net"] if (mode == "live" and major >= 25) else []


def report_store_dir(path=None):
    directory = Path(path) if path else (PILOT / "report-store")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory.resolve()


def _inside(directory, path):
    try:
        return os.path.commonpath([str(directory), str(Path(path).resolve())]) == str(directory)
    except (ValueError, OSError, TypeError):
        return False


def _verify_stored(store, data):
    report = data.get("report") if isinstance(data, dict) else None
    if not isinstance(report, dict):
        return "REPORT_MISSING"
    if report.get("reportStored") is not True:
        return "REPORT_NOT_STORED"
    path = data.get("reportStorePath")
    if not isinstance(path, str) or not path:
        return "REPORT_PATH_MISSING"
    if not _inside(store, path) or not Path(path).is_file():
        return "REPORT_NOT_STORED"
    try:
        written = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return "REPORT_UNREADABLE"
    if written.get("runId") != report.get("runId") or written.get("outcome") != report.get("outcome"):
        return "REPORT_MISMATCH"
    return None


def execute(
    mode,
    url,
    model,
    source="agent",
    program="nisi-smoke.mjs",
    report_store=None,
    node=None,
    worker_id="m5-macbook-pro",
    timeout_s=None,
    disable_thinking=False,
    max_output_tokens=None,
    response_format_mode=None,
    request_mode_value=None,
    route=None,
):
    if program not in PROGRAMS:
        raise Refused("PROGRAM_REFUSED")
    if route is not None and not valid_route(route, worker_id, program):
        raise Refused("ROUTE_REFUSED")
    check_worker_id(worker_id)
    timeout_s = model_timeout_s(worker_id, timeout_s)
    max_output_tokens = max_tokens(worker_id, max_output_tokens)
    response_format_mode = response_format(worker_id, response_format_mode)
    mode_of_request = request_mode(worker_id, request_mode_value)
    completion = completion_settings(worker_id) if mode_of_request != "chat" else {}
    if disable_thinking not in (True, False):
        raise Refused("DISABLE_THINKING_INVALID")
    endpoint(url)
    if mode not in ("fixture", "live"):
        raise Refused("MODE_REFUSED")
    store = report_store_dir(report_store)
    node_bin = node or node_binary()
    net = net_flags(node_major(node_bin), mode)
    with (PILOT / "pilot.lock").open("a+") as lock:
        try_lock(lock)
        integrity()
        ev = WorkerEvidence(time.time(), True, True, True, False) if mode == "fixture" else readiness(url, model)
        local = _local_route(ev, source, worker_id=worker_id)
        cfg = {
            "mode": mode,
            "endpoint": url,
            "model": model,
            "workerId": worker_id,
            "modelTimeoutMs": timeout_s * 1000,
            "disableThinking": disable_thinking,
            **({"maxTokens": max_output_tokens} if max_output_tokens else {}),
            **({"responseFormat": response_format_mode} if response_format_mode else {}),
            "requestMode": mode_of_request,
            **completion,
            "route": local if route is None else route,
            "reportStore": str(store),
        }
        # Node permission mode confines writes to the report store and reads to the
        # isolated preview plus that store. No provider env, HOME, child processes,
        # addons or worker permissions are supplied.
        cmd = [
            node_bin,
            "--permission",
            *net,
            "--allow-fs-read=" + str(PILOT),
            "--allow-fs-read=" + str(store),
            "--allow-fs-write=" + str(store),
            str(PILOT / program),
        ]
        r = subprocess.run(
            cmd,
            input=json.dumps(cfg),
            capture_output=True,
            text=True,
            env=clean_env(),
            cwd=PILOT,
            timeout=timeout_s + 30,
        )
        if not r.stdout:
            raise Refused("NISI_CHILD_FAILED: " + (sanitize_text(r.stderr[:300]) or ""))
        data = json.loads(r.stdout)
        if not isinstance(data, dict):
            raise Refused("NISI_OUTPUT_INVALID")
        data["childExitCode"] = r.returncode
        plan_failure = _check_child_plan(data, cfg["route"])
        data.update(pin=PIN, observedAt=time.time(), leaseScope="preview-only; not a backend reservation")
        report = data.get("report")
        data["authorEvidence"] = author_evidence(data.get("authorReceipts"))
        data["modelTimeoutS"] = timeout_s
        data["disableThinking"] = disable_thinking
        data["maxTokens"] = max_output_tokens
        data["responseFormat"] = response_format_mode
        data["requestMode"] = mode_of_request
        data["netPermission"] = ("allow-net+" if net else "none+") + f"guard:127.0.0.1:{urlsplit(url).port or 0}"
        if r.returncode and r.stderr:
            data["childStderr"] = sanitize_text(r.stderr[:300])
        failure = plan_failure or _verify_stored(store, data)
        if not isinstance(report, dict):
            report = data["report"] = {}
        data["reportStored"] = report.get("reportStored") is True and failure is None
        data["reportPath"] = data.get("reportStorePath")
        data["runId"] = report.get("runId")
        data["reportSha256"] = report.get("storedReportSha256")
        if failure:
            report["outcome"] = "BLOCKED"
            report["code"] = failure
        return data


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    p.add_argument("--endpoint", default="http://127.0.0.1:8081/v1/chat/completions")
    p.add_argument("--model", default="fixture-only")
    p.add_argument("--source", default="agent")
    p.add_argument("--report-store", default=None)
    a = p.parse_args()
    try:
        if a.mode == "live" and a.model in ("fixture-only", ""):
            raise Refused("MODEL_REQUIRED_IN_LIVE_MODE")
        data = execute(a.mode, a.endpoint, a.model, a.source, report_store=a.report_store)
        evidence = PILOT / "evidence"
        evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
        out = evidence / f"{a.mode}-{time.time_ns()}.json"
        out.write_text(json.dumps(data, indent=2) + "\n")
        report = data["report"]
        emit(
            json.dumps(
                {
                    "outcome": report["outcome"],
                    "mode": a.mode,
                    "modelCalls": data["modelCalls"],
                    "independentLLMReview": False,
                    "evidence": str(out),
                    "reportStored": data["reportStored"],
                    "reportPath": data["reportPath"],
                    "runId": data["runId"],
                }
            )
        )
    except (Refused, OSError, ValueError, subprocess.SubprocessError) as e:
        emit(json.dumps({"outcome": "BLOCKED", "reason": str(e)}))
        return 2
    return 0 if data["report"]["outcome"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
