"""Send reviewed project code or specs to the Codex Free Pool.

The source label is a caller attestation, not provenance detection. The content
filter is a backstop, not a substitute for reviewing files before submission.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from fnmatch import fnmatchcase
from pathlib import Path
from urllib import error
from urllib import request as http

from .fleet_routing_plan import POOL_MODELS

MODELS = POOL_MODELS
ALLOWED_PROJECTS = {"nisi", "jaryo", "opos", "agiw-suite"}
REFUSED_SOURCES = {"linear", "notion", "granola"}
ENDPOINT = "https://api.openai.com/v1/responses"
MAX_CHARS = 400_000
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_TOKENS = 4096

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


def _env_key(env_file):
    if env_file is None:
        return os.environ.get("OPENAI_API_KEY")
    key = None
    with Path(env_file).open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = _ENV_ASSIGNMENT.fullmatch(line)
            if match is None:
                raise PoolRefused("KEY_REFUSED")
            if match[1] == "OPENAI_API_KEY":
                if key is not None:
                    raise PoolRefused("KEY_REFUSED")
                key = match[2].strip()
                if len(key) >= 2 and key[0] in "\"'" and key[-1] == key[0]:
                    key = key[1:-1]
    return key


class _NoRedirect(http.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None  # urllib raises HTTPError rather than issuing another request


def _http_code(status):
    if type(status) is int and 300 <= status < 400:
        return "REDIRECT_REFUSED"
    return "HTTP_REFUSED"


def _unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise PoolRefused("RESPONSE_INVALID")
    return obj


def _reject_constant(_value):
    raise PoolRefused("RESPONSE_INVALID")


def _read_response(opener, req, timeout_s):
    try:
        with opener.open(req, timeout=timeout_s) as response:
            status = response.status
            raw = response.read(MAX_RESPONSE_BYTES + 1) if type(status) is int and status == 200 else None
    except error.HTTPError as exc:
        try:
            exc.close()
        finally:
            raise PoolRefused(_http_code(exc.code)) from None
    except Exception:  # noqa: BLE001 - transport failures may echo request headers or body
        raise PoolRefused("TRANSPORT_REFUSED") from None
    if type(status) is not int or status != 200:
        raise PoolRefused(_http_code(status))
    if not isinstance(raw, bytes):
        raise PoolRefused("RESPONSE_INVALID")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise PoolRefused("RESPONSE_SIZE_REFUSED")
    try:
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise PoolRefused("RESPONSE_INVALID") from None


def _output_text(output):
    if not isinstance(output, list):
        raise PoolRefused("RESPONSE_INVALID")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            raise PoolRefused("RESPONSE_INVALID")
        if item.get("type") == "reasoning":
            continue
        content = item.get("content")
        if item.get("type") != "message" or item.get("role") != "assistant" or not isinstance(content, list):
            raise PoolRefused("RESPONSE_INVALID")
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise PoolRefused("RESPONSE_INVALID")
            texts.append(part["text"])
    text = "".join(texts)
    if not text.strip():
        raise PoolRefused("RESPONSE_INVALID")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise PoolRefused("RESPONSE_INVALID") from None
    return text


def _usage_counts(usage, fields):
    if not isinstance(usage, dict):
        raise PoolRefused("RESPONSE_INVALID")
    counts = {field: usage.get(field) for field in fields}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise PoolRefused("RESPONSE_INVALID")
    return counts


def _response_fields(data, key):
    if not isinstance(data, dict) or data.get("status") != "completed" or data.get("error") is not None:
        raise PoolRefused("RESPONSE_INVALID")
    model, response_id = data.get("model"), data.get("id")
    for value in (model, response_id):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value):
            raise PoolRefused("RESPONSE_INVALID")
    text = _output_text(data.get("output"))
    if any(key in value or _contains_secret(value) for value in (model, response_id, text)):
        raise PoolRefused("SECRET_VALUE_REFUSED")
    usage = data.get("usage")
    counts = _usage_counts(usage, ("input_tokens", "output_tokens", "total_tokens"))
    for field, detail in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
        if field in usage:
            counts[field] = _usage_counts(usage[field], (detail,))
    return {"model_served": model, "response_id": response_id, "output_text": text, "usage": counts}


class FreePoolLane:
    def __init__(self, api_key_source=None, opener=None, timeout_s=300, *, env_file=None):
        self.api_key_source = api_key_source
        self.env_file = env_file
        self.opener = opener if opener is not None else http.build_opener(http.ProxyHandler({}), _NoRedirect())
        self.timeout_s = timeout_s

    def _api_key(self):
        try:
            key = _env_key(self.env_file) if self.api_key_source is None else self.api_key_source()
        except Exception:  # noqa: BLE001 - credential providers can raise with secret-bearing messages
            raise PoolRefused("KEY_REFUSED") from None
        if not isinstance(key, str) or not _KEY_FORMAT.fullmatch(key):
            raise PoolRefused("KEY_REFUSED")
        return key

    def _send(self, body, model, key):
        payload = {"model": model, "input": body, "max_output_tokens": MAX_OUTPUT_TOKENS, "store": False}
        req = http.Request(
            ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        req.add_unredirected_header("Authorization", f"Bearer {key}")
        return _read_response(self.opener, req, self.timeout_s)

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
            key = self._api_key()
            if key in body or any(key in name for name, _ in parts):
                raise PoolRefused("SECRET_VALUE_REFUSED")
            receipt.update(
                project=project,
                tier=tier,
                model_requested=model,
                prompt_sha256=_sha256(prompt),
                parts=[{"name": name, "sha256": _sha256(text), "chars": len(text)} for name, text in parts],
            )
            result = _response_fields(self._send(body, model, key), key)
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
        receipt = FreePoolLane(env_file=args.env_file).run(request, prompt["text"])
        sys.stdout.write(json.dumps(receipt) + "\n")
        return 0 if receipt["outcome"] == "COMPLETED" else 2
    except PoolRefused as exc:
        sys.stdout.write(exc.code + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
