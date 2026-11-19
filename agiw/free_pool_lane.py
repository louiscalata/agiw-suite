"""Send reviewed project code or specs to the Codex Free Pool.

Calls go through free_pool_call.py so its budget ceiling, credit-exhausted
fail-closed path, and model routing apply. This module never calls the
Responses API directly and never stores a key. The source label is a caller
attestation, not provenance detection. The content filter is a backstop, not a
substitute for reviewing files before submission.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from fnmatch import fnmatchcase
from pathlib import Path

ALLOWED_PROJECTS = {"nisi", "jaryo", "opos", "agiw-suite"}
REFUSED_SOURCES = {"linear", "notion", "granola"}
# general/mini request the official 10M mini so small jobs stay off the Sol bucket.
# hard requests Astra and is invoked with --hard. The script may still reroute.
MODELS = {"general": "gpt-5.4-mini", "mini": "gpt-5.4-mini", "hard": "gpt-6.2-astra"}
CALL_ENV = "AGIW_FREE_POOL_CALL"
MAX_CHARS = 400_000
MAX_OUTPUT_TOKENS = 400
EXIT_CREDIT = 9
EXIT_BUDGET = 10
EXIT_TIMEOUT = 11
EXIT_CODES = {
    2: "KEY_REFUSED",
    4: "HTTP_REFUSED",
    5: "RESPONSE_INVALID",
    6: "MODEL_REFUSED",
    7: "PAYER_REFUSED",
    8: "CONTENT_GATE_REFUSED",
    EXIT_CREDIT: "CREDIT_EXHAUSTED",
    EXIT_BUDGET: "BUDGET_REFUSED",
    EXIT_TIMEOUT: "CLIENT_TIMEOUT",
}

_SECRET_FILES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*.token",
    "credentials*",
    "secrets*",
    "*.keychain*",
    ".netrc",
    ".npmrc",
    ".pypirc",
)
_SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|sk-proj-|(?:ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_]+"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}|xox[abpr]-|xapp-|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:Bearer)[ \t]+[A-Za-z0-9.~+/_=-]{20,}|AIza[0-9A-Za-z_-]{35}"
)
_ASSIGNMENT = re.compile(
    r"(?:\b|_)(?:api[_-]?key|secret|token|password|passwd|auth|credential|private[_-]?key)"
    r"['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9/+_=-]{16,})",
    re.IGNORECASE,
)
_LOG_LINE = re.compile(
    r"^\s*(?:<\d{1,3}>(?:\d+ )?)?\[?(?:\d{4}-\d\d-\d\d[T ]\d\d:\d\d"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2} +\d\d:\d\d:\d\d"
    r"|(?:INFO|WARN(?:ING)?|ERROR|DEBUG)\b)",
    re.IGNORECASE,
)
_LOG_KEYS = {"level", "severity", "ts", "timestamp", "time"}
_KEY_FORMAT = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_ENV_ASSIGNMENT = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")


class PoolRefused(RuntimeError):  # noqa: N818 - public exception name
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _check_route(request):
    if not isinstance(request, dict):
        raise PoolRefused("REQUEST_INVALID")
    project, source, tier = (request.get(field) for field in ("project", "source", "tier"))
    if not isinstance(project, str) or project not in ALLOWED_PROJECTS:
        raise PoolRefused("PROJECT_REFUSED")
    if not isinstance(source, str) or source in REFUSED_SOURCES or source not in {"code", "spec"}:
        raise PoolRefused("SOURCE_REFUSED")
    if not isinstance(tier, str) or tier not in MODELS:
        raise PoolRefused("TIER_REFUSED")


def _contains_secret(text):
    return bool(_SECRET.search(text) or any(match[1].lower().strip("x") for match in _ASSIGNMENT.finditer(text)))


def _check_name(name):
    if not isinstance(name, str) or not name.strip() or any(ord(char) < 32 for char in name):
        raise PoolRefused("REQUEST_INVALID")
    basename = re.split(r"[/\\]", name)[-1].lower().rstrip(" .")
    if not basename:
        raise PoolRefused("REQUEST_INVALID")
    if any(fnmatchcase(basename, pattern) for pattern in _SECRET_FILES):
        raise PoolRefused("SECRET_FILE_REFUSED")
    if basename.endswith(".log"):
        raise PoolRefused("RAW_LOG_REFUSED")
    if _contains_secret(name):
        raise PoolRefused("SECRET_VALUE_REFUSED")
    try:
        name.encode("utf-8")
    except UnicodeError:
        raise PoolRefused("REQUEST_INVALID") from None


def _is_log_line(line):
    if _LOG_LINE.match(line):
        return True
    if not line.lstrip().startswith("{"):
        return False
    try:
        row = json.loads(line)
    except (ValueError, RecursionError):
        return False
    return isinstance(row, dict) and not _LOG_KEYS.isdisjoint(row)


def _check_text(text):
    if _contains_secret(text):
        raise PoolRefused("SECRET_VALUE_REFUSED")
    log_lines = frames = 0
    traceback = "Traceback (most recent call last):" in text
    for line in text.lstrip("\ufeff").splitlines():
        log_lines += _is_log_line(line)
        frames += line.startswith('  File "')
        if log_lines >= 5 or (traceback and frames >= 3):
            raise PoolRefused("RAW_LOG_REFUSED")


def _checked_input(request, extra_parts=()):
    _check_route(request)
    parts = request.get("parts")
    if set(request) != {"project", "source", "tier", "parts"} or not isinstance(parts, list):
        raise PoolRefused("REQUEST_INVALID")
    total = 0
    checked = []
    for part in (*parts, *extra_parts):
        if not isinstance(part, dict) or set(part) != {"name", "text"} or not isinstance(part["text"], str):
            raise PoolRefused("REQUEST_INVALID")
        name, text = part["name"], part["text"]
        _check_name(name)
        total += len(text)
        if total > MAX_CHARS:
            raise PoolRefused("SIZE_REFUSED")
        checked.append((name, text))
    body = "\n\n".join(text for _, text in checked)
    if not body.strip():
        raise PoolRefused("EMPTY_REFUSED")
    try:
        body.encode("utf-8")
    except UnicodeError:
        raise PoolRefused("REQUEST_INVALID") from None
    _check_text(body)
    return body, checked


def check_content(request):
    """Raise a code-only PoolRefused unless the supplied parts pass the filter."""
    _checked_input(request)


def _with_prompt(request, prompt, name="prompt"):
    body, checked = _checked_input(request, ({"name": name, "text": prompt},))
    if not prompt.strip():
        raise PoolRefused("EMPTY_REFUSED")
    return body, checked[:-1]


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_free_pool_call(configured=None, env=None):
    """Return an explicit or PATH-resolved free_pool_call.py path, or None."""
    environ = os.environ if env is None else env
    value = configured if configured is not None else environ.get(CALL_ENV)
    if isinstance(value, str) and value.strip():
        path = os.path.abspath(os.path.expanduser(value.strip()))
        return path if os.path.isfile(path) else None
    found = shutil.which("free_pool_call.py") or shutil.which("free_pool_call")
    return os.path.abspath(found) if found else None


def _command(script):
    if script.lower().endswith(".py"):
        return [sys.executable, script]
    return [script]


def _child_env(workdir=None, env_file=None, environ=None):
    """Minimal child environment: no extra host secrets, no key on the argv.

    OPENAI_API_KEY is forwarded only when already present (operator sourced the
    env file). Otherwise the script reads CODEX_API_FREE_POOL_ENV or its default path.
    """
    host = os.environ if environ is None else environ
    env = {"PATH": host.get("PATH", "")}
    if host.get("HOME"):
        env["HOME"] = host["HOME"]
    if env_file is not None:
        env["CODEX_API_FREE_POOL_ENV"] = str(env_file)
    if host.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = host["OPENAI_API_KEY"]
    if os.name == "nt":
        root = host.get("SYSTEMROOT") or r"C:\Windows"
        env["SYSTEMROOT"] = root
        env["WINDIR"] = host.get("WINDIR") or root
        if workdir:
            env["TEMP"] = workdir
            env["TMP"] = workdir
            if host.get("USERPROFILE"):
                env["USERPROFILE"] = host["USERPROFILE"]
        for key in ("COMSPEC", "PATHEXT"):
            if key in host:
                env[key] = host[key]
    elif workdir:
        env["TMPDIR"] = workdir
    elif "TMPDIR" in host:
        env["TMPDIR"] = host["TMPDIR"]
    return env


def _write_prompt(workdir, text):
    path = os.path.join(workdir, "prompt.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def _cleanup(workdir):
    def retry(func, path, _exc):
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


def _json_object(stdout):
    text = (stdout or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates = [text] + ([lines[-1], lines[0]] if lines else [])
    for candidate in candidates:
        try:
            data = json.loads(candidate, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate")
    return obj


def _reject_constant(_value):
    raise ValueError("nonfinite")


def _token(value, pattern=r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise PoolRefused("RESPONSE_INVALID")
    return value


def _usage_counts(usage, fields):
    if not isinstance(usage, dict):
        raise PoolRefused("RESPONSE_INVALID")
    counts = {field: usage.get(field) for field in fields}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise PoolRefused("RESPONSE_INVALID")
    return counts


def _script_fields(data):
    if not isinstance(data, dict):
        raise PoolRefused("RESPONSE_INVALID")
    text = data.get("output_text")
    if not isinstance(text, str) or not text.strip():
        raise PoolRefused("RESPONSE_INVALID")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise PoolRefused("RESPONSE_INVALID") from None
    served = data.get("model") or data.get("model_served")
    response_id = data.get("response_id") or data.get("id")
    served = _token(served)
    response_id = _token(response_id)
    if any(_contains_secret(value) for value in (served, response_id, text)):
        raise PoolRefused("SECRET_VALUE_REFUSED")
    usage = data.get("usage")
    counts = _usage_counts(usage, ("input_tokens", "output_tokens", "total_tokens"))
    for field, detail in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
        if isinstance(usage, dict) and field in usage:
            counts[field] = _usage_counts(usage[field], (detail,))
    result = {"model_served": served, "response_id": response_id, "output_text": text, "usage": counts}
    billing = data.get("billing") if isinstance(data.get("billing"), dict) else {}
    payer = data.get("payer", billing.get("payer"))
    if payer is not None:
        result["payer"] = _token(payer, r"[A-Za-z0-9][A-Za-z0-9._-]{0,32}")
    return result


def _map_exit(code):
    return EXIT_CODES.get(code, "CALLER_REFUSED")


class FreePoolLane:
    def __init__(self, api_key_source=None, opener=None, timeout_s=300, *, env_file=None, free_pool_call=None):
        # api_key_source and opener are accepted for call-compatibility but unused:
        # the official caller is free_pool_call.py, which loads the key itself.
        self.env_file = env_file
        self.free_pool_call = free_pool_call
        self.timeout_s = timeout_s

    def _invoke(self, script, body, request, model):
        workdir = tempfile.mkdtemp(prefix="agiw-pool-")
        try:
            prompt_path = _write_prompt(workdir, body)
            args = _command(script) + [
                prompt_path,
                "--model",
                model,
                "--project",
                request["project"],
                "--source",
                request["source"],
                "--private",
                "--timeout",
                str(self.timeout_s),
                "--max-output",
                str(MAX_OUTPUT_TOKENS),
            ]
            if request["tier"] == "hard":
                args.append("--hard")
            try:
                proc = subprocess.run(
                    args,
                    cwd=workdir,
                    env=_child_env(workdir, self.env_file),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise PoolRefused("CLIENT_TIMEOUT") from exc
            except OSError as exc:
                raise PoolRefused("CALLER_UNAVAILABLE") from exc
            if proc.returncode != 0:
                raise PoolRefused(_map_exit(proc.returncode))
            data = _json_object(proc.stdout)
            if data is None:
                raise PoolRefused("RESPONSE_INVALID")
            return _script_fields(data)
        finally:
            _cleanup(workdir)

    def run(self, request, prompt):
        receipt = {
            "kind": "agiw.free-pool-receipt.v1",
            "project": None,
            "tier": None,
            "model_requested": None,
            "model_served": None,
            "response_id": None,
            "prompt_sha256": None,
            "parts": [],
            "output_text": "",
            "usage": None,
            "outcome": "BLOCKED",
            "code": None,
        }
        try:
            body, parts = _with_prompt(request, prompt)
            project, tier = request["project"], request["tier"]
            model = MODELS[tier]
            if type(self.timeout_s) not in (int, float) or not 0 < self.timeout_s < float("inf"):
                raise PoolRefused("TIMEOUT_REFUSED")
            script = resolve_free_pool_call(self.free_pool_call)
            if script is None:
                raise PoolRefused("CALLER_UNAVAILABLE")
            receipt.update(
                project=project,
                tier=tier,
                model_requested=model,
                prompt_sha256=_sha256(prompt),
                parts=[{"name": name, "sha256": _sha256(text), "chars": len(text)} for name, text in parts],
            )
            result = self._invoke(script, body, request, model)
            output = result["output_text"]
            if prompt in output or any(text.strip() and text in output for _, text in parts):
                raise PoolRefused("OUTPUT_ECHO_REFUSED")
            receipt.update(result, outcome="COMPLETED", code="OK")
        except PoolRefused as exc:
            receipt["code"] = exc.code
        return receipt


def _load_part(filename, limit=MAX_CHARS):
    path = Path(filename)
    _check_name(path.name)
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise PoolRefused("INPUT_UNAVAILABLE") from None
    _check_name(target.name)
    try:
        if not target.is_file():
            raise PoolRefused("INPUT_UNAVAILABLE")
        with target.open(encoding="utf-8", newline="") as stream:
            text = stream.read(limit + 1)
    except (OSError, ValueError):
        raise PoolRefused("INPUT_UNAVAILABLE") from None
    if len(text) > limit:
        raise PoolRefused("SIZE_REFUSED")
    return {"name": path.name, "text": text}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PoolRefused("ARGUMENT_REFUSED")


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--source", choices=("code", "spec"), required=True)
    parser.add_argument("--tier", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--part", action="append", default=[])
    parser.add_argument("--env-file")
    parser.add_argument("--free-pool-call", default=None)
    parser.add_argument("--dry-run", action="store_true")
    try:
        args = parser.parse_args(argv)
        request = {"project": args.project, "tier": args.tier, "source": args.source, "parts": []}
        _check_route(request)
        prompt = _load_part(args.prompt_file)
        remaining = MAX_CHARS - len(prompt["text"])
        for path in args.part:
            part = _load_part(path, remaining)
            request["parts"].append(part)
            remaining -= len(part["text"])
        if args.dry_run:
            _with_prompt(request, prompt["text"], prompt["name"])
            sys.stdout.write("OK\n")
            return 0
        receipt = FreePoolLane(env_file=args.env_file, free_pool_call=args.free_pool_call).run(request, prompt["text"])
        sys.stdout.write(json.dumps(receipt) + "\n")
        return 0 if receipt["outcome"] == "COMPLETED" else 2
    except PoolRefused as exc:
        sys.stdout.write(exc.code + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
