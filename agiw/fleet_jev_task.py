"""Bounded jev-task CLI adapter for the fleet repo-patch analyzers.

The adapter is injected, never auto-discovered: nothing runs at import, and ``auto`` (explicit path or
``AGIW_JEV_TASK=auto``) is the only way PATH is consulted. Each call writes the already-projected text
from ``fleet_repo_patch`` to a 0600 temp file, runs ``jev-task pre`` or ``jev-task post`` with
``--driver never``, parses the markdown brief / optional JSON / summary line, and deletes the temp tree.

The child gets a minimal environment (PATH, temp directories, and on Windows SYSTEMROOT/WINDIR plus
COMSPEC/PATHEXT when present), so keys, tokens and .env contents are not forwarded. SYSTEMROOT is
always set on Windows (from the host, or C:\\Windows if missing), matching repo-patch git's env. The tool's personal ledger is never read or written by
this module; nothing from its stderr, command line or the input file is returned.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

DISABLED = ("", "off", "0", "false")
DEFAULT_TIMEOUT = 30.0
_VERDICT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_SUMMARY_RE = re.compile(r"^\[(?:pre|post)\b[^\]]*\]$")
_FLAGGED_RE = re.compile(r"(\d+)\s*/\s*\d+\s+flagged")
_UNAVAILABLE_RE = re.compile(r"\[unavailable\s+\S+\s+[^:\]]*:\s*(?P<code>[^;\]]+?)\s+at\s+(?P<stage>[^;\]]+?)\s*;")


def _unavailable(code="JEV_UNAVAILABLE", stage="no_analyzer"):
    return {"status": "unavailable", "stage": stage, "errorCode": code}


def resolve_jev_task_bin(configured=None, env=None):
    """Resolve the configured binary to a filesystem path, or None when disabled/missing.

    None and the strings "", "off", "0" and "false" disable the adapter. "auto" is the only value
    that calls ``shutil.which``; anything else is an explicit path (``~`` expanded) that must exist.
    """
    environ = os.environ if env is None else env
    value = configured if configured is not None else environ.get("AGIW_JEV_TASK")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.lower() in DISABLED:
        return None
    if value.lower() == "auto":
        return shutil.which("jev-task") or None
    path = os.path.expanduser(value)
    return path if os.path.isfile(path) else None


def _timeout_seconds(timeout):
    if timeout is None:
        raw = os.environ.get("AGIW_JEV_TASK_TIMEOUT", "")
        try:
            timeout = float(raw)
        except ValueError:
            timeout = DEFAULT_TIMEOUT
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def make_analyzers(bin_path, timeout=30):
    """Return ``(pre, post)`` callables taking one projected string and returning a small dict.

    A missing binary yields the established no-analyzer result instead of raising, so a worker can
    build the pair unconditionally and repo-patch records ``no_analyzer`` as before.
    """
    if not bin_path or not isinstance(bin_path, str) or not os.path.isfile(bin_path):
        return (lambda _text: _unavailable(stage="no_analyzer"),) * 2
    bin_path = os.path.abspath(bin_path)  # the child runs with cwd set to its temp dir
    seconds = _timeout_seconds(timeout)
    state = {}

    def pre(text):
        job_id = uuid.uuid4().hex[:12]
        state["pre_id"] = job_id
        return _invoke(bin_path, "pre", text, seconds, job_id)

    def post(text):
        job_id = uuid.uuid4().hex[:12]
        # Link to the pre run from this pair when there was one; a standalone post links to itself.
        return _invoke(bin_path, "post", text, seconds, job_id, state.get("pre_id"))

    return pre, post


def _command(bin_path, args):
    if bin_path.lower().endswith(".py"):
        return [sys.executable, bin_path] + list(args)
    return [bin_path] + list(args)


def _child_env(workdir=None, name=os.name, environ=os.environ):
    """Minimal process env, aligned with repo-patch's Windows git env (#213).

    PATH is always set. Temp directories point at the call's workdir when one is given.
    On Windows, SYSTEMROOT is required to start a process: use the host value, or C:\\Windows
    if it is missing. WINDIR follows SYSTEMROOT when unset. COMSPEC and PATHEXT are copied
    only when present. Keys, tokens and other host variables are not forwarded.
    """
    env = {"PATH": environ.get("PATH", "")}
    if name == "nt":
        root = environ.get("SYSTEMROOT") or r"C:\Windows"
        env["SYSTEMROOT"] = root
        env["WINDIR"] = environ.get("WINDIR") or root
        if workdir:
            env["TEMP"] = workdir
            env["TMP"] = workdir
            env["USERPROFILE"] = workdir
        else:
            for key in ("TEMP", "TMP"):
                if key in environ:
                    env[key] = environ[key]
        for key in ("COMSPEC", "PATHEXT"):
            if key in environ:
                env[key] = environ[key]
    elif workdir:
        env["TMPDIR"] = workdir
    else:
        if "TMPDIR" in environ:
            env["TMPDIR"] = environ["TMPDIR"]
    return env


def _workdir():
    return tempfile.mkdtemp(prefix="agiw-jev-")


def _write_text(workdir, text):
    path = os.path.join(workdir, "input.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
        handle.write(text)
    return path


def _cleanup(workdir):
    def retry(func, path, _exc_info):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        try:
            func(path)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(workdir, onexc=lambda func, path, exc: retry(func, path, exc))
    else:
        shutil.rmtree(workdir, onerror=retry)


def _invoke(bin_path, phase, text, timeout, job_id, pre_id=None):
    workdir = _workdir()
    try:
        text_path = _write_text(workdir, text)
        args = [
            phase,
            "--id",
            job_id,
            "--text",
            text_path,
            "--driver",
            "never",
            "--kind",
            "draft" if phase == "pre" else "review",
        ]
        if phase == "post":
            args.extend(["--pre-id", pre_id or job_id])
        try:
            proc = subprocess.run(
                _command(bin_path, args),
                cwd=workdir,
                env=_child_env(workdir),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _unavailable("JEV_TIMEOUT", phase)
        except OSError:
            return _unavailable("JEV_UNAVAILABLE", phase)
        return _interpret(phase, proc.returncode, proc.stdout or "", proc.stderr or "", text_path)
    finally:
        _cleanup(workdir)


def _json_object(stdout):
    """The whole output, then its last/first non-empty line, as a JSON object."""
    text = stdout.strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates = [text] + ([lines[-1], lines[0]] if lines else [])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _ok_value(phase, value):
    if phase == "pre":
        return {"status": "ok", "plan": value}
    token = value.strip()[:64]
    if not _VERDICT_RE.fullmatch(token):
        return _unavailable("JEV_INVALID_OUTPUT", phase)
    return {"status": "ok", "verdict": token}


def _last_line(stdout):
    lines = stdout.rstrip().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-1].strip() if lines else ""


def _strip_summary(stdout):
    lines = stdout.rstrip().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _SUMMARY_RE.match(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


def _sibling_verdict(text_path):
    path = text_path + ".jevpost.json"
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > 65536:
            return None
        with open(path, encoding="utf-8", errors="surrogateescape") as handle:
            data = json.load(handle)
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("statedVerdict")
    if not isinstance(value, str):
        return None
    token = value.strip()
    if token in ("READY", "NOT_READY"):
        return token
    return token[:64] if _VERDICT_RE.fullmatch(token) else None


def _stderr_unavailable(phase, stderr):
    match = _UNAVAILABLE_RE.search(stderr)
    if match:
        code = match.group("code").strip() or "JEV_UNAVAILABLE"
        stage = match.group("stage").strip() or phase
        return _unavailable(code, stage)
    return _unavailable("JEV_UNAVAILABLE", phase)


def _interpret(phase, code, stdout, stderr, text_path):
    field = "plan" if phase == "pre" else "verdict"
    data = _json_object(stdout)
    if data is not None:
        status = data.get("status")
        if status == "unavailable":
            error = data.get("errorCode")
            stage = data.get("stage")
            return _unavailable(
                error.strip() if isinstance(error, str) and error.strip() else "JEV_UNAVAILABLE",
                stage.strip() if isinstance(stage, str) and stage.strip() else phase,
            )
        if status != "ok":
            return _unavailable("JEV_INVALID_OUTPUT", phase)
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return _ok_value(phase, value)
        if phase == "pre" or code != 0:
            return _unavailable("JEV_INVALID_OUTPUT", phase)
    if code in (2, 3):
        return _stderr_unavailable(phase, stderr)
    if code != 0:
        return _unavailable("JEV_INVALID_OUTPUT", phase)
    if data is None and stdout.lstrip().startswith("{"):
        return _unavailable("JEV_INVALID_OUTPUT", phase)  # a JSON result that did not parse
    if phase == "pre":
        plan = _strip_summary(stdout)
        return {"status": "ok", "plan": plan} if plan else _unavailable("JEV_INVALID_OUTPUT", phase)
    verdict = _sibling_verdict(text_path)
    if verdict is None:
        match = _FLAGGED_RE.search(_last_line(stdout))
        if match:
            verdict = "FLAGGED" if int(match.group(1)) > 0 else "OK"
    if verdict is None:
        return _unavailable("JEV_INVALID_OUTPUT", phase)
    return _ok_value(phase, verdict)
