#!/usr/bin/env python3
"""Guarded caller for the OpenAI complimentary-token ("free pool") project.

One script and one shared config (free_pool_routing.json) for every machine. A small per-machine file
(free_pool_machine.json, next to this script) names the machine, its share of the fleet budget and where
receipts live. Nothing here prints, logs or copies the API key.

Guards, all fail closed:
- Content gate: Linear, Notion and Granola content, secrets, .env files, raw logs and security work are refused.
- Model list: only models on OpenAI's complimentary-token list (help article 10306912) plus the gated empirical
  6.2 group. -pro ids, gpt-5.3-codex and deprecated or shut-down models are refused. Dated snapshots map to their
  base model's group; only the listed snapshot is sent.
- Per-request size caps per model (max total and max output tokens). Larger requests are rerouted, never sent.
- Per-group daily ceilings x this machine's share (60/25/15). The projected total of each request is checked
  before it is sent, because a request that crosses the daily cap is billed in full.
- $1 per run worst-case cost cap at list prices.
- Empirical group: blocked for the rest of the UTC day once any developer-billed row exists.
- Post-call tripwire: billing.payer other than "openai" disables that model until 00:00 UTC and exits 7.
- Credit exhausted (402, insufficient_quota or billing errors) exits 9 and blocks later calls until cleared.
- A client timeout exits 11, is recorded as possibly billed and is never retried.
Blocked models fail over to the next model in the same task kind's ordered list.

Exit codes: 0 ok, 2 usage, 4 API error, 5 empty, 6 model refused, 7 payer not openai (model disabled for
the UTC day), 8 content gate refused, 9 credit exhausted, 10 budget or size guard refused, 11 client timeout.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import socket
import sys
import time
from fnmatch import fnmatchcase
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

HERE = Path(__file__).resolve().parent
ROUTING_FILE = HERE / "free_pool_routing.json"
MACHINE_FILE = HERE / "free_pool_machine.json"
ENDPOINT = "https://api.openai.com/v1/responses"
DEFAULT_KEY_ENV_PATH = os.path.join(os.path.expanduser("~"), ".config", "agiw", "codex-api-free-pool.env")
LEDGER_NAME = "free-pool-usage-utcday.jsonl"
CREDIT_LEDGER_NAME = "credit-ledger.jsonl"
CREDIT_STATE_NAME = "free-pool-credit-state.json"
MAX_CHARS = 400_000

EXIT_USAGE = 2
EXIT_API = 4
EXIT_EMPTY = 5
EXIT_MODEL = 6
EXIT_PAYER = 7
EXIT_GATE = 8
EXIT_CREDIT = 9
EXIT_BUDGET = 10
EXIT_TIMEOUT = 11

FOUR_PROJECTS = {
    "nisi": "nisi",
    "jaryo": "jaryo",
    "opos": "opos",
    "old phone os": "opos",
    "old-phone-os": "opos",
    "oldphoneos": "opos",
    "agiw-suite": "agiw-suite",
    "agiw suite": "agiw-suite",
    "agiwsuite": "agiw-suite",
}
REFUSED_SOURCES = {"linear", "notion", "granola"}
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
    "auth.json",
)
_ENV_EXAMPLES = (".env.example", ".env.sample", ".env.template")
_SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|sk-proj-|(?:ghp_|gho_|ghs_|ghu_|ghr_|github_pat_)[A-Za-z0-9_]{16,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}|xox[abpr]-|xapp-|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:Bearer)[ \t]+[A-Za-z0-9.~+/_=-]{20,}|AIza[0-9A-Za-z_-]{35}|lin_api_|ntn_[A-Za-z0-9]{20,}"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"
)
_ASSIGNMENT = re.compile(
    r"(?:\b|_)(?:api[_-]?key|secret|token|password|passwd|auth|credential|private[_-]?key)"
    r"['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9/+_=-]{16,})",
    re.IGNORECASE,
)
_SHARE_URL = re.compile(
    r"https?://(?:[a-z0-9.-]+\.)?(?:linear\.app|notion\.so|notion\.site|notion\.com|granola\.so|granola\.ai)\b", re.I
)
_LOG_LINE = re.compile(
    r"^\s*(?:<\d{1,3}>(?:\d+ )?)?\[?(?:\d{4}-\d\d-\d\d[T ]\d\d:\d\d"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2} +\d\d:\d\d:\d\d"
    r"|(?:INFO|WARN(?:ING)?|ERROR|DEBUG)\b)",
    re.IGNORECASE,
)
_LOG_KEYS = {"level", "severity", "ts", "timestamp", "time"}
_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_SECURITY_KINDS = {"security", "security_review", "pentest", "vulnerability"}


class Refused(Exception):  # noqa: N818 - public name kept for callers
    def __init__(self, code, exit_code, **detail):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.detail = detail


# ---------------------------------------------------------------- content gate


def _contains_secret(text):
    return bool(_SECRET.search(text) or any(m[1].lower().strip("x") for m in _ASSIGNMENT.finditer(text)))


def check_name(name):
    base = re.split(r"[/\\]", name or "")[-1].lower().rstrip(" .")
    if not base:
        return
    if base not in _ENV_EXAMPLES and any(fnmatchcase(base, p) for p in _SECRET_FILES):
        raise Refused("SECRET_FILE_REFUSED", EXIT_GATE)
    if base.endswith((".log", ".rawlog", ".log.txt", ".log.gz")):
        raise Refused("RAW_LOG_REFUSED", EXIT_GATE)
    if _contains_secret(name):
        raise Refused("SECRET_VALUE_REFUSED", EXIT_GATE)


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


def check_text(text):
    if not text.strip():
        raise Refused("EMPTY_REFUSED", EXIT_GATE)
    if len(text) > MAX_CHARS:
        raise Refused("SIZE_REFUSED", EXIT_GATE)
    if _SHARE_URL.search(text):
        raise Refused("SOURCE_REFUSED", EXIT_GATE, reason="Linear, Notion or Granola content")
    if _contains_secret(text):
        raise Refused("SECRET_VALUE_REFUSED", EXIT_GATE)
    logs = frames = 0
    traceback_seen = "Traceback (most recent call last):" in text
    for line in text.lstrip("\ufeff").splitlines():
        logs += _is_log_line(line)
        frames += line.startswith('  File "')
        if logs >= 5 or (traceback_seen and frames >= 3):
            raise Refused("RAW_LOG_REFUSED", EXIT_GATE)


def canon_project(name):
    if not name:
        return None
    key = re.sub(r"\s+", " ", str(name).strip().lower()).replace("_", "-")
    return FOUR_PROJECTS.get(key) or FOUR_PROJECTS.get(key.replace("-", " "))


def gate(names, text, meta):
    """Raise Refused unless the request may go to the free pool."""
    source = str(meta.get("source") or "").strip().lower()
    if source in REFUSED_SOURCES:
        raise Refused("SOURCE_REFUSED", EXIT_GATE, reason="Linear, Notion or Granola content")
    if meta.get("security") or str(meta.get("kind") or "").lower() in _SECURITY_KINDS:
        raise Refused("SECURITY_REFUSED", EXIT_GATE, reason="security work stays off the free pool")
    for name in names:
        check_name(name)
    if text is not None:
        check_text(text)


def content_gate(prompt_path, text, meta):
    """Compatibility wrapper: (ok, result) like the older M5 script."""
    try:
        gate([prompt_path or ""], text or "", meta or {})
    except Refused as exc:
        return False, {"precheck": "refuse", "reason": exc.code, **exc.detail}
    return True, {"precheck": "pass", "project": canon_project((meta or {}).get("project"))}


def load_task_json(path):
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------- config


def load_routing(path=None):
    path = Path(path or os.environ.get("AGIW_FREE_POOL_ROUTING") or ROUTING_FILE)
    with path.open(encoding="utf-8") as handle:
        cfg = json.load(handle)
    models, groups = cfg["models"], cfg["groups"]
    for key, entry in models.items():
        if entry["group"] not in groups:
            raise ValueError(f"model {key} names unknown group {entry['group']}")
    for kind, order in cfg["routes"].items():
        for key in order:
            if key not in models:
                raise ValueError(f"route {kind} names unknown model {key}")
    return cfg


def _local_path(value):
    """Expand ~ and resolve relative paths against this script's folder, not the caller's cwd."""
    path = Path(os.path.expanduser(str(value)))
    return str(path if path.is_absolute() else HERE / path)


def load_machine(cfg, path=None):
    path = Path(path or os.environ.get("AGIW_FREE_POOL_MACHINE") or MACHINE_FILE)
    machine = {}
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            machine = json.load(handle)
    fleet = cfg.get("fleet", {})
    mid = machine.get("machine_id") or "unknown"
    share = fleet.get("split", {}).get(mid, fleet.get("default_share", 0.15))
    out = dict(machine)
    out["machine_id"] = mid
    out["share"] = float(share)
    out["receipts_dir"] = _local_path(machine.get("receipts_dir") or "receipts")
    out["disabled_file"] = _local_path(machine.get("disabled_file") or "free-pool-disabled.json")
    out["extra_ledgers"] = [_local_path(p) for p in machine.get("extra_ledgers", [])]
    return out


def model_base(model_id):
    return _DATE_SUFFIX.sub("", (model_id or "").strip().lower())


def hard_block_reason(cfg, model_id):
    mid = (model_id or "").strip().lower()
    blocks = cfg.get("hard_blocks", {})
    if any(s in mid for s in blocks.get("substrings", [])) or mid.startswith(tuple(blocks.get("prefixes", []))):
        return f"{mid} is hard-blocked ({blocks.get('reason', 'billed')})"
    return None


def resolve_model(cfg, requested):
    """Return the config key for a requested id, or raise Refused (exit 6)."""
    mid = (requested or "").strip().lower()
    why = hard_block_reason(cfg, mid)
    if why:
        raise Refused("MODEL_REFUSED", EXIT_MODEL, reason=why)
    models = cfg["models"]
    key = mid if mid in models else model_base(mid)
    if key not in models:
        key = next((k for k, m in models.items() if mid == m.get("listed_id")), None)
    if key is None:
        raise Refused("MODEL_REFUSED", EXIT_MODEL, reason=f"{mid} is not on the complimentary-token list")
    entry = models[key]
    if _DATE_SUFFIX.search(mid) and mid not in {entry.get("listed_id"), *entry.get("listed_snapshots", [])}:
        raise Refused("MODEL_REFUSED", EXIT_MODEL, reason=f"{mid} is not the listed snapshot of {key}")
    return key


def classify_group(cfg, model_id):
    mid = (model_id or "").strip().lower()
    models = cfg["models"]
    for key in (mid, model_base(mid)):
        if key in models:
            return models[key]["group"]
    for entry in models.values():
        if mid == entry.get("listed_id") or mid in entry.get("listed_snapshots", []):
            return entry["group"]
    return "unlisted_other"


def normalize_group(cfg, name):
    for group, entry in cfg["groups"].items():
        if name == group or name in entry.get("legacy_names", []):
            return group
    return name


def request_caps(cfg, key):
    entry = cfg["models"][key]
    default = cfg.get("limits", {}).get("default_request_caps", {})
    total = entry.get("max_total_tokens", default.get("max_total_tokens", 2900))
    output = entry.get("max_output_tokens", default.get("max_output_tokens", 1000))
    return int(total), int(output)


# ---------------------------------------------------------------- ledgers


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def utc_day(now=None):
    return (now or utc_now()).strftime("%Y-%m-%d")


def _row_day(row):
    if row.get("utc_day"):
        return row["utc_day"]
    stamp = row.get("at") or row.get("time")
    if not isinstance(stamp, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", None):
        try:
            when = datetime.datetime.strptime(stamp, fmt) if fmt else datetime.datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return utc_day(when.astimezone(datetime.timezone.utc))
    return None


def _read_rows(path):
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _row_tokens(row):
    for field in ("total_tokens", "tokens", "est_tokens"):
        value = row.get(field)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return 0


def ledger_paths(machine):
    own = os.path.join(machine["receipts_dir"], LEDGER_NAME)
    credit = os.path.join(machine["receipts_dir"], CREDIT_LEDGER_NAME)
    return [own, credit, *machine.get("extra_ledgers", [])]


def usage_today(cfg, machine, day=None):
    """Return ({group: tokens}, {group: [billed model ids]}) for this machine's UTC day."""
    day = day or utc_day()
    best, finished = {}, set()
    for path in dict.fromkeys(ledger_paths(machine)):
        for row in _read_rows(path):
            if _row_day(row) != day:
                continue
            if row.get("kind") != "reservation" and row.get("reservation_id"):
                finished.add(row["reservation_id"])
            rid = row.get("response_id") or row.get("reservation_id") or row.get("entry_id")
            rid = rid or hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
            if row.get("kind") == "reservation":
                rid = "reservation:" + rid
            if rid not in best or _row_tokens(row) > _row_tokens(best[rid]):
                best[rid] = row
    totals, billed = {}, {}
    for rid, row in best.items():
        if rid.startswith("reservation:") and row.get("reservation_id") in finished:
            continue
        model = row.get("resolved") or row.get("model") or row.get("bucket") or ""
        group = normalize_group(cfg, row.get("group") or classify_group(cfg, model))
        totals[group] = totals.get(group, 0) + _row_tokens(row)
        if row.get("payer") == "developer":
            billed.setdefault(group, []).append(model_base(model))
    return totals, billed


def _append(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


class _Lock:
    """Small cross-process lock (O_EXCL file) around plan + reservation."""

    def __init__(self, directory, wait_s=15.0, stale_s=120.0):
        self.path = os.path.join(directory, ".free-pool-plan.lock")
        self.wait_s, self.stale_s = wait_s, stale_s

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.monotonic() + self.wait_s
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale_s:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise Refused("LEDGER_LOCK_BUSY", EXIT_BUDGET) from None
                time.sleep(0.1)

    def __exit__(self, *_exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


def load_disabled(machine, day=None):
    day = day or utc_day()
    try:
        with open(machine["disabled_file"], encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {model_base(k): v for k, v in raw.items() if not isinstance(v, dict) or v.get("utc_day") in (None, day)}


def disable_for_day(machine, model_id, detail):
    path = machine["disabled_file"]
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw[model_base(model_id)] = {"utc_day": utc_day(), "reason": "billing.payer was not openai", **detail}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


def credit_blocked(machine):
    try:
        with open(os.path.join(machine["receipts_dir"], CREDIT_STATE_NAME), encoding="utf-8") as handle:
            return bool(json.load(handle).get("credit_exhausted"))
    except (OSError, ValueError, AttributeError):
        return False


def mark_credit_exhausted(machine, status):
    path = os.path.join(machine["receipts_dir"], CREDIT_STATE_NAME)
    os.makedirs(machine["receipts_dir"], exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"credit_exhausted": True, "at": utc_now().isoformat(), "http_status": status}, handle)


# ---------------------------------------------------------------- planning


def estimate_input(text, cfg):
    overhead = int(cfg.get("limits", {}).get("request_overhead_tokens", 64))
    return math.ceil(len(text.encode("utf-8")) / 3) + overhead


def worst_cost(cfg, key, est_in, max_out):
    price_in, price_out = cfg["models"][key].get("price_usd_per_1m", [100.0, 100.0])
    return est_in * price_in / 1e6 + max_out * price_out / 1e6


def candidates(cfg, kind=None, model=None):
    if kind:
        if kind not in cfg["routes"]:
            raise Refused("KIND_REFUSED", EXIT_USAGE, reason=f"unknown task kind {kind}")
        return list(cfg["routes"][kind])
    key = resolve_model(cfg, model)
    return [key, *cfg.get("legacy_model_fallback", {}).get(key, [])]


def block_reason(cfg, key, req, state):
    """Return why `key` cannot take this request, or None."""
    entry, group = cfg["models"][key], cfg["models"][key]["group"]
    hard = hard_block_reason(cfg, entry.get("listed_id", key))
    if hard:
        return hard
    if entry.get("doc_status") in cfg.get("doc_status_blocked", []):
        return f"{entry.get('doc_status')} per OpenAI docs"
    if not entry.get("enabled"):
        return entry.get("disabled_reason") or "disabled in config"
    if key in state["disabled"]:
        return "disabled until 00:00 UTC after a payer other than openai"
    if entry.get("requires_hard") and not req["hard"]:
        return "needs --hard"
    if req["kind"] in cfg.get("code_kinds", []) and not entry.get("code_trusted"):
        return entry.get("code_trust_note") or "not trusted for code until its probe passes"
    group_cfg = cfg["groups"][group]
    if group_cfg.get("block_if_billed_today") and state["billed"].get(group):
        return f"{group} had a developer-billed call today"
    return size_or_budget_reason(cfg, key, req, state)


def size_or_budget_reason(cfg, key, req, state):
    group = cfg["models"][key]["group"]
    max_total, max_output = request_caps(cfg, key)
    total = req["est_in"] + req["max_out"]
    if req["max_out"] > max_output:
        return f"--max-output {req['max_out']} is over this model's free cap {max_output}"
    if total > max_total:
        return f"worst case {total} tokens is over this model's free request cap {max_total}"
    group_cfg = cfg["groups"][group]
    margin = float(cfg.get("limits", {}).get("near_cap_margin", 0.05))
    allowed = int(group_cfg["fleet_ceiling"] * state["share"] * (1 - margin))
    used = state["used"].get(group, 0)
    if used + total > allowed:
        return f"{group} would reach {used + total} of this machine's {allowed} (near cap)"
    cap = float(cfg.get("limits", {}).get("run_cost_cap_usd", 1.0))
    if state["spent_usd"] + worst_cost(cfg, key, req["est_in"], req["max_out"]) > cap:
        return f"worst-case cost would pass the ${cap:.2f} run cap"
    return None


def plan(cfg, req, state):
    """Pick the first eligible model. Returns (key, route). Raises Refused(exit 10) if none fits."""
    route = []
    order = candidates(cfg, req["kind"], req["model"])
    if req.get("no_reroute"):
        order = order[:1]
    for key in order:
        why = block_reason(cfg, key, req, state)
        route.append({"model": key, "skipped": why} if why else {"model": key, "chosen": True})
        if why is None:
            return key, route
    raise Refused("BUDGET_REFUSED", EXIT_BUDGET, reason="no free model fits this request", route=route)


# ---------------------------------------------------------------- API call


def _read_env_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return ""
    for line in lines:
        text = line.strip()
        if text.startswith("export "):
            text = text[7:].strip()
        name, sep, value = text.partition("=")
        if sep and name.strip() == "OPENAI_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    return ""


def load_api_key(machine=None):
    """Return the free-pool key from the environment or the machine's private key file. Never print it."""
    machine = machine or {}
    for name in ("OPENAI_FREE_POOL_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    env_file = os.environ.get("CODEX_API_FREE_POOL_ENV") or machine.get("key_env_file") or DEFAULT_KEY_ENV_PATH
    key = _read_env_file(os.path.expanduser(env_file))
    if key:
        return key
    key_file = machine.get("key_file")
    if key_file:
        try:
            return Path(os.path.expanduser(key_file)).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


_urlopen = urlrequest.urlopen


def _credit_error(status, body):
    err = body.get("error") if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    text = " ".join(str(err.get(k) or "") for k in ("code", "type", "message")).lower()
    return status == 402 or "insufficient_quota" in text or "billing" in text or "credit" in text


def post(machine, model_id, prompt, max_out, timeout, effort=None):
    key = load_api_key(machine)
    if not key:
        raise Refused("KEY_UNAVAILABLE", EXIT_USAGE)
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if machine.get("openai_project"):
        headers["OpenAI-Project"] = machine["openai_project"]
    if machine.get("openai_org"):
        headers["OpenAI-Organization"] = machine["openai_org"]
    payload = {"model": model_id, "input": prompt, "max_output_tokens": max_out, "store": False}
    if effort:
        payload["reasoning"] = {"effort": effort}
    body = json.dumps(payload)
    req = urlrequest.Request(ENDPOINT, data=body.encode("utf-8"), method="POST", headers=headers)
    del key, headers
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read(8 * 1024 * 1024))
    except urlerror.HTTPError as exc:
        try:
            parsed = json.loads(exc.read(65536) or b"{}")
        except ValueError:
            parsed = {}
        if _credit_error(exc.code, parsed):
            raise Refused("CREDIT_EXHAUSTED", EXIT_CREDIT, http_status=exc.code) from None
        err = parsed.get("error") if isinstance(parsed, dict) else None
        code = str((err or {}).get("code") or "") if isinstance(err, dict) else ""
        code = code if re.fullmatch(r"[a-z0-9_]{1,40}", code) else "unknown"
        raise Refused("API_ERROR", EXIT_API, http_status=exc.code, error_code=code) from None
    except (socket.timeout, TimeoutError):
        raise Refused("CLIENT_TIMEOUT", EXIT_TIMEOUT) from None
    except urlerror.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
            raise Refused("CLIENT_TIMEOUT", EXIT_TIMEOUT) from None
        raise Refused("NETWORK_ERROR", EXIT_API) from None


def output_text(data):
    parts = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for chunk in item.get("content") or []:
            if isinstance(chunk, dict) and chunk.get("type") in ("output_text", "text"):
                parts.append(str(chunk.get("text") or ""))
    return "".join(parts)


# ---------------------------------------------------------------- run


def _base_row(cfg, machine, req, key):
    entry = cfg["models"][key]
    return {
        "at": utc_now().isoformat(),
        "utc_day": utc_day(),
        "machine_id": machine["machine_id"],
        "source": req["source"],
        "lane": req["lane"],
        "task_kind": req["kind"],
        "project": req["project"],
        "model": key,
        "request_id_model": entry.get("request_id", key),
        "listed_id": entry.get("listed_id", key),
        "group": entry["group"],
        "prompt_sha256": req["prompt_sha256"],
        "via": "free_pool_call.py",
        "config_version": cfg.get("version"),
    }


def _reserve(cfg, machine, req):
    with _Lock(machine["receipts_dir"]):
        used, billed = usage_today(cfg, machine)
        state = {
            "used": used,
            "billed": billed,
            "share": machine["share"],
            "disabled": load_disabled(machine),
            "spent_usd": 0.0,
        }
        key, route = plan(cfg, req, state)
        row = _base_row(cfg, machine, req, key)
        reservation_id = hashlib.sha256(f"{row['at']}{time.time_ns()}{os.getpid()}".encode()).hexdigest()[:20]
        row.update(kind="reservation", reservation_id=reservation_id, est_tokens=req["est_in"] + req["max_out"])
        _append(os.path.join(machine["receipts_dir"], LEDGER_NAME), row)
    return key, route, reservation_id


def _finish(cfg, machine, req, key, reservation_id, **fields):
    row = _base_row(cfg, machine, req, key)
    row.update(kind="usage", reservation_id=reservation_id, **fields)
    _append(os.path.join(machine["receipts_dir"], LEDGER_NAME), row)
    return row


def _served_ok(entry, served):
    listed = entry.get("listed_id", "")
    served = (served or "").lower()
    if served == listed or served in entry.get("listed_snapshots", []):
        return True
    return not _DATE_SUFFIX.search(listed) and model_base(served) == listed


def call(cfg, machine, req):
    """Plan, reserve, call once and record. Returns (exit_code, result dict)."""
    if credit_blocked(machine):
        raise Refused("CREDIT_EXHAUSTED", EXIT_CREDIT, reason="credit state file is set; clear it after checking")
    key, route, reservation_id = _reserve(cfg, machine, req)
    entry = cfg["models"][key]
    try:
        data = post(
            machine,
            entry.get("request_id", key),
            req["prompt"],
            req["max_out"],
            req["timeout"],
            entry.get("reasoning_effort"),
        )
    except Refused as exc:
        possibly_billed = exc.exit_code == EXIT_TIMEOUT
        tokens = req["est_in"] + req["max_out"] if possibly_billed else 0
        _finish(
            cfg,
            machine,
            req,
            key,
            reservation_id,
            total_tokens=tokens,
            outcome=exc.code,
            possibly_billed=possibly_billed,
        )
        if exc.exit_code == EXIT_CREDIT:
            mark_credit_exhausted(machine, exc.detail.get("http_status"))
        exc.detail.setdefault("route", route)
        raise
    return _record_response(cfg, machine, req, key, reservation_id, route, data)


def _record_response(cfg, machine, req, key, reservation_id, route, data):
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    billing = data.get("billing") if isinstance(data.get("billing"), dict) else {}
    payer = billing.get("payer")
    total = usage.get("total_tokens")
    if not isinstance(total, int):
        total = req["est_in"] + req["max_out"]
    row = _finish(
        cfg,
        machine,
        req,
        key,
        reservation_id,
        resolved=data.get("model"),
        response_id=data.get("id"),
        total_tokens=total,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        payer=payer,
        service_tier=data.get("service_tier"),
        served_model_ok=_served_ok(cfg["models"][key], data.get("model")),
        outcome="COMPLETED",
    )
    text = output_text(data)
    result = {
        "status": "ok",
        "model": data.get("model"),
        "model_key": key,
        "requested_model": cfg["models"][key].get("request_id", key),
        "response_id": data.get("id"),
        "usage": usage,
        "payer": payer,
        "billing": billing,
        "route": route,
        "receipt": row,
        "output_text": text,
    }
    if not row["served_model_ok"]:
        # The alias moved to a snapshot that is not on the complimentary list: treat it like a billed response.
        payer = payer if payer != "openai" else "unlisted-snapshot"
    if payer != "openai":
        _append(os.path.join(machine["receipts_dir"], CREDIT_LEDGER_NAME), row)
        disable_for_day(machine, key, {"response_id": data.get("id"), "source": req["source"], "lane": req["lane"]})
        result["status"] = "payer_refused"
        result["guard"] = f"billing.payer={payer}: {key} disabled until 00:00 UTC; stop this run"
        return EXIT_PAYER, result
    if not text.strip():
        result["status"] = "empty"
        return EXIT_EMPTY, result
    return 0, result


def check_model(m, disabled):
    """Compatibility check for older callers. Exits 6 when the model may not be used directly.

    Empirical-group models are refused here because their billing depends on request size, which only
    free_pool_call.py's own guard can check. That also refuses Agents-session callers such as the Agiw Helper.
    """
    cfg = load_routing()
    try:
        key = resolve_model(cfg, m)
    except Refused as exc:
        sys.stdout.write(json.dumps({"refused": m, "reason": exc.detail.get("reason", exc.code)}) + "\n")
        sys.exit(EXIT_MODEL)
    entry = cfg["models"][key]
    why = None
    if entry["group"] not in ("official_1M", "official_10M"):
        why = "empirical model: only free_pool_call.py's per-request size guard may send it"
    elif entry.get("doc_status") in cfg.get("doc_status_blocked", []) or not entry.get("enabled"):
        why = "disabled in free_pool_routing.json"
    elif model_base(m) in {model_base(k) for k in (disabled or {})}:
        why = "disabled after developer billing"
    if why:
        sys.stdout.write(json.dumps({"refused": m, "reason": why}) + "\n")
        sys.exit(EXIT_MODEL)


# ---------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"free_pool_call: {message}\n")
        sys.exit(EXIT_USAGE)


def parse_args(argv):
    p = _Parser(description="Guarded free-pool caller (see module docstring).")
    p.add_argument("prompt_path", nargs="?", help="prompt file (same as --prompt-file)")
    p.add_argument("--prompt-file")
    p.add_argument("--prompt")
    p.add_argument("--file", action="append", default=[], help="attach a reviewed code/spec file")
    p.add_argument("--kind", help="task kind: " + ", ".join(sorted(load_routing_kinds())))
    p.add_argument("--model", help="legacy: request a model; reroutes through legacy_model_fallback")
    p.add_argument("--source", default="", help="required for real calls, e.g. code, spec, public, claude-code")
    p.add_argument("--lane", default="", help="required for real calls, e.g. m5-free-pool or agiw-free-pool-lane")
    p.add_argument("--project", default="")
    p.add_argument("--existing-public", default="", metavar="OWNER/REPO")
    p.add_argument("--destined-public", default="", metavar="TARGET")
    p.add_argument("--security", action="store_true")
    p.add_argument("--hard", action="store_true")
    p.add_argument("--max-output", type=int, default=None)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--no-reroute", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="gate and plan only; no network")
    p.add_argument("--precheck-only", action="store_true", help="print the chosen model; no network")
    p.add_argument("--budget-only", action="store_true", help="print today's usage per group")
    p.add_argument("--est-tokens", type=int, default=None)
    p.add_argument("--record", nargs=2, metavar=("MODEL", "TOKENS"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--task-json", default="")
    p.add_argument("--routing", default=None)
    p.add_argument("--machine-config", default=None)
    for legacy in ("--private", "--strict-free", "--opt-in-astra"):
        p.add_argument(legacy, action="store_true", help="accepted for compatibility; no effect")
    for legacy in ("--repo", "--org", "--project-id", "--openai-project", "--budget-config"):
        p.add_argument(legacy, default="", help="accepted for compatibility; ignored")
    return p.parse_args(argv)


def load_routing_kinds():
    try:
        return load_routing()["routes"].keys()
    except (OSError, ValueError, KeyError):
        return []


def _write(obj):
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")


def _read_prompt(args):
    names, chunks = [], []
    path = args.prompt_file or args.prompt_path
    if path:
        names.append(path)
        chunks.append(Path(path).read_text(encoding="utf-8"))
    elif args.prompt is not None:
        chunks.append(args.prompt)
    for extra in args.file:
        names.append(extra)
        chunks.append(f"--- {Path(extra).name} ---\n" + Path(extra).read_text(encoding="utf-8"))
    return names, "\n\n".join(chunks)


def _budget_report(cfg, machine):
    used, billed = usage_today(cfg, machine)
    margin = float(cfg.get("limits", {}).get("near_cap_margin", 0.05))
    groups = {
        name: {
            "used": used.get(name, 0),
            "allowed": int(g["fleet_ceiling"] * machine["share"] * (1 - margin)),
            "billed_models_today": sorted(set(billed.get(name, []))),
        }
        for name, g in cfg["groups"].items()
    }
    return {"utc_day": utc_day(), "machine_id": machine["machine_id"], "share": machine["share"], "groups": groups}


def _record_external(cfg, machine, args):
    key = resolve_model(cfg, args.record[0])
    row = {
        "kind": "usage",
        "at": utc_now().isoformat(),
        "utc_day": utc_day(),
        "machine_id": machine["machine_id"],
        "model": key,
        "group": cfg["models"][key]["group"],
        "total_tokens": int(args.record[1]),
        "source": args.source or "external-record",
        "lane": args.lane or "external-record",
        "entry_id": hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:16],
        "possibly_billed": True,
    }
    _append(os.path.join(machine["receipts_dir"], LEDGER_NAME), row)
    _write({"recorded": row["total_tokens"], "model": key})


def _request(cfg, args, text, meta):
    limits = cfg.get("limits", {})
    max_out = args.max_output if args.max_output is not None else int(limits.get("default_max_output", 1000))
    if max_out <= 0 or max_out > int(limits.get("max_output_ceiling", 16000)):
        raise Refused("MAX_OUTPUT_REFUSED", EXIT_USAGE)
    est_in = estimate_input(text, cfg) if text else max(0, (args.est_tokens or 0) - max_out)
    return {
        "kind": args.kind,
        "model": args.model,
        "hard": bool(args.hard),
        "no_reroute": bool(args.no_reroute),
        "source": args.source.strip() or None,
        "lane": args.lane.strip() or None,
        "project": canon_project(meta.get("project")) or (args.existing_public or None),
        "prompt": text,
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "est_in": est_in,
        "max_out": max_out,
        "timeout": args.timeout,
    }


def _check_real_call(args, req):
    if not (req["source"] and req["lane"]):
        raise Refused("TAGS_REQUIRED", EXIT_USAGE, reason="--source and --lane are required on real calls")
    if not req["project"]:
        raise Refused("PROJECT_REFUSED", EXIT_GATE, reason="--project must be nisi, jaryo, opos or agiw-suite")
    if not req["prompt"].strip():
        raise Refused("EMPTY_REFUSED", EXIT_GATE)
    if not 0 < args.timeout < 3600:
        raise Refused("TIMEOUT_REFUSED", EXIT_USAGE)


def _offline(cfg, machine, args, req):
    used, billed = usage_today(cfg, machine)
    state = {"used": used, "billed": billed, "share": machine["share"], "disabled": load_disabled(machine)}
    state["spent_usd"] = 0.0
    key, route = plan(cfg, req, state)
    if args.precheck_only and not args.json:
        sys.stdout.write(key + "\n")
    else:
        _write({"precheck": "pass", "model": key, "route": route, "est_tokens": req["est_in"] + req["max_out"]})
    return 0


def run(argv=None):
    args = parse_args(argv)
    cfg = load_routing(args.routing)
    machine = load_machine(cfg, args.machine_config)
    if args.budget_only:
        _write(_budget_report(cfg, machine))
        return 0
    if args.record:
        _record_external(cfg, machine, args)
        return 0
    if not (args.kind or args.model):
        args.model = "gpt-5.4-mini" if not args.hard else "gpt-6.2-astra"
    meta = load_task_json(args.task_json)
    meta.setdefault("project", args.project)
    meta["source"] = meta.get("source") or args.source
    meta["security"] = meta.get("security") or args.security
    meta["kind"] = args.kind
    names, text = _read_prompt(args)
    gate(names, text if (text or not (args.dry_run or args.precheck_only)) else None, meta)
    req = _request(cfg, args, text, meta)
    if args.dry_run or args.precheck_only:
        return _offline(cfg, machine, args, req)
    _check_real_call(args, req)
    code, result = call(cfg, machine, req)
    _write(result)
    return code


def main(argv=None):
    try:
        return run(argv)
    except Refused as exc:
        _write({"refused": exc.code, "exit": exc.exit_code, **exc.detail})
        return exc.exit_code
    except (OSError, ValueError) as exc:
        _write({"refused": "INPUT_UNAVAILABLE", "exit": EXIT_USAGE, "error": type(exc).__name__})
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
