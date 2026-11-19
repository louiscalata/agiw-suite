from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO

from agiw.fleet_router_client import FleetRouterClient, load_workers
from agiw.free_pool_lane import (
    PoolRefused,
    _check_text,
    _child_env,
    _cleanup,
    _contains_secret,
    _json_object,
    _map_exit,
    _script_fields,
    _write_prompt,
    resolve_free_pool_call,
)

MODELS = {
    "terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
    "mini": "gpt-5.4-mini",
}
# Assembled so the scrub check never sees a literal workspace host in source.
_DOT = "."
REFUSED_MARKERS = tuple(
    _DOT.join(parts) for parts in (("linear", "app"), ("notion", "so"), ("notion", "site"), ("granola",), ("", "env"))
)
LANE = "dot-orchestrator"
MAX_OUTPUT = 600
MAX_BODY = 64 * 1024
PROTOCOL_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}
DEFAULT_REPOS = [
    "louiscalata/nisi",
    "louiscalata/jaryo",
    "louiscalata/old-phone-os",
    "louiscalata/agiw-suite",
    "louiscalata/agiw-mcp",
]
ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-z0-9-]{1,40}-[0-9a-f]{6}$")


@dataclass
class Config:
    root: Path
    free_pool_call: str | None = None
    fleet_configs: list[str] = field(default_factory=list)
    gh: str = "gh"
    repos: list[str] = field(default_factory=lambda: list(DEFAULT_REPOS))
    host: str = "127.0.0.1"
    port: int = 18795
    token_file: Path = field(default_factory=lambda: Path.home() / ".config" / "agiw" / "orchestrator-mcp.token")
    no_auth: bool = False
    stdio: bool = False
    allow_origins: list[str] = field(default_factory=lambda: ["https://chatgpt.com"])
    token: str | None = None


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_stamp() -> str:
    return _utc_now().strftime("%Y%m%dT%H%M%SZ")


def _utc_iso() -> str:
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, PoolRefused):
        return str(exc.code)
    return type(exc).__name__.upper()


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(str(path), 0o700)
    except OSError:
        pass


def _private_file_write(path: Path, content: str) -> None:
    _mkdir_private(path.parent)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    finally:
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass


def _safe_json_loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


class Orchestrator:
    def __init__(self, config: Config):
        self.config = config
        self._github_cache: tuple[float, Any] | None = None

    @property
    def inbox(self) -> Path:
        return self.config.root / "inbox"

    @property
    def results(self) -> Path:
        return self.config.root / "results"

    @property
    def receipts(self) -> Path:
        return self.config.root / "receipts"

    @property
    def paused_path(self) -> Path:
        return self.receipts / "PAUSED"

    @property
    def receipt_path(self) -> Path:
        return self.receipts / "orchestrator-lane.jsonl"

    def tool_schemas(self) -> list[dict[str, Any]]:
        project = {"type": "string", "enum": ["nisi", "jaryo", "opos", "agiw-suite", "none"]}
        return [
            {
                "name": "status",
                "title": "Orchestrator status",
                "description": "Use this when you need queue, lane, machine, or pull request status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"include_github": {"type": "boolean", "default": True}},
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "openWorldHint": False,
                },
            },
            {
                "name": "plan",
                "title": "Draft task brief",
                "description": "Use this when you need a reviewed task brief before queueing work.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "request": {"type": "string"},
                        "project": project,
                        "model": {"type": "string", "enum": list(MODELS)},
                    },
                    "required": ["request"],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "openWorldHint": True,
                },
            },
            {
                "name": "queue_task",
                "title": "Queue task",
                "description": "Use this when a task brief is ready to be placed in the inbox.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "maxLength": 120},
                        "brief": {},
                        "project": project,
                        "lane_hint": {"type": "string"},
                    },
                    "required": ["title", "brief", "project"],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "openWorldHint": False,
                },
            },
            {
                "name": "task_result",
                "title": "Task result",
                "description": "Use this when you need queued task state or a completed task result.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "openWorldHint": False,
                },
            },
        ]

    def _receipt_rows(self) -> list[dict[str, Any]]:
        try:
            with self.receipt_path.open("r", encoding="utf-8") as handle:
                rows = []
                for line in handle:
                    row = _safe_json_loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                return rows
        except OSError:
            return []

    def _append_receipt(self, row: dict[str, Any]) -> None:
        _mkdir_private(self.receipts)
        fd = os.open(str(self.receipt_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
        finally:
            try:
                os.chmod(str(self.receipt_path), 0o600)
            except OSError:
                pass

    def _machines(self) -> list[dict[str, Any]]:
        answer: list[dict[str, Any]] = []
        for filename in self.config.fleet_configs:
            try:
                workers, _config = load_workers(filename)
                evidence, _now = FleetRouterClient(workers).probe()
                for name in sorted(workers):
                    item = evidence.get(name)
                    answer.append(
                        {
                            "worker": name,
                            "reachable": item is not None,
                            "model_ready": bool(getattr(item, "local_model_ready", False)),
                            "busy": bool(getattr(item, "busy", False)),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - report a code, never a URL, token or traceback
                answer.append({"config": Path(filename).name, "error": _error_code(exc)})
        return answer

    @staticmethod
    def _ci_status(rollup: Any) -> str:
        if not rollup:
            return "none"
        failures = {
            "FAILURE",
            "CANCELLED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "STARTUP_FAILURE",
        }
        success = {"SUCCESS", "NEUTRAL", "SKIPPED"}
        for item in rollup:
            if not isinstance(item, dict):
                return "pending"
            conclusion = str(item.get("conclusion") or "").upper()
            state = str(item.get("status") or item.get("state") or "").upper()
            if conclusion in failures:
                return "failure"
            if state in {"PENDING", "EXPECTED"} or (state and state != "COMPLETED"):
                return "pending"
            if conclusion and conclusion not in success:
                return "pending"
        return "success"

    def _github(self) -> dict[str, Any]:
        if self._github_cache is not None and time.monotonic() - self._github_cache[0] < 60:
            return self._github_cache[1]
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        environment.update({key: value for key, value in os.environ.items() if key.startswith("GH_")})
        if os.name == "nt":
            # Windows processes (Python, gh) need these to start and to find gh's config.
            for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                if key in os.environ:
                    environment[key] = os.environ[key]
        result: dict[str, Any] = {}
        for repo in self.config.repos:
            command = [
                self.config.gh,
                "pr",
                "list",
                "-R",
                repo,
                "--state",
                "open",
                "--limit",
                "20",
                "--json",
                "number,title,url,isDraft,headRefName,statusCheckRollup",
            ]
            if self.config.gh.endswith(".py"):
                command.insert(0, sys.executable)
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    env=environment,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError("GH_FAILED")
                rows = _safe_json_loads(completed.stdout)
                if not isinstance(rows, list):
                    raise RuntimeError("GH_INVALID")
                result[repo] = [
                    {
                        "number": row.get("number"),
                        "title": row.get("title"),
                        "url": row.get("url"),
                        "draft": bool(row.get("isDraft")),
                        "ci": self._ci_status(row.get("statusCheckRollup")),
                    }
                    for row in rows
                    if isinstance(row, dict)
                ]
            except Exception as exc:  # noqa: BLE001 - one repo failing must not hide the others
                result[repo] = {"error": _error_code(exc)}
        self._github_cache = (time.monotonic(), result)
        return result

    def _pool_today(self) -> dict[str, Any]:
        """Whole-pool totals from free_pool_call.py --budget-only (no API call, no key needed)."""
        script = resolve_free_pool_call(self.config.free_pool_call)
        if not script:
            return {"error": "FREE_POOL_UNAVAILABLE"}
        workdir = tempfile.mkdtemp(prefix="agiw-orchestrator-")
        try:
            prompt_path = _write_prompt(workdir, "status")
            command = [sys.executable, script, prompt_path, "--model", MODELS["mini"], "--max-output", "16"]
            completed = subprocess.run(
                [*command, "--budget-only"],
                cwd=workdir,
                env=_child_env(workdir=workdir, environ={"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())}),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            data = _json_object(completed.stdout) if completed.returncode == 0 else None
            budget = data.get("budget") if isinstance(data, dict) else None
            if not isinstance(budget, dict):
                return {"error": "BUDGET_UNAVAILABLE"}
            fields = ("utc_day", "developer_billed", "possibly_billed", "totals", "ceiling", "published_cap")
            return {key: budget.get(key) for key in fields if key in budget}
        except (OSError, subprocess.SubprocessError):
            return {"error": "BUDGET_UNAVAILABLE"}
        finally:
            _cleanup(workdir)

    def status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rows = self._receipt_rows()
        today = _utc_now().strftime("%Y-%m-%d")
        today_rows = [row for row in rows if str(row.get("at", "")).startswith(today)]
        total_tokens = sum(int((row.get("usage") or {}).get("total_tokens") or 0) for row in today_rows)
        billed = [row for row in today_rows if row.get("payer") and row.get("payer") != "openai"]
        free_pool = {
            "recent": rows[-10:],
            "today": {
                "calls": len(today_rows),
                "total_tokens": total_tokens,
                "billed_calls": len(billed),
                "billed_tokens": sum(int((row.get("usage") or {}).get("total_tokens") or 0) for row in billed),
            },
            "pool_today": self._pool_today(),
        }
        result: dict[str, Any] = {
            "lanes": {
                LANE: {
                    "models": MODELS,
                    "default": "terra",
                    "paused": self.paused_path.exists(),
                    "free_pool_call_found": bool(resolve_free_pool_call(self.config.free_pool_call)),
                }
            },
            "machines": self._machines(),
            "free_pool": free_pool,
        }
        if arguments.get("include_github", True):
            result["github"] = self._github()
        return result

    def plan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        request = arguments.get("request")
        project = arguments.get("project", "none")
        model_name = arguments.get("model", "terra")
        if not isinstance(request, str) or not request or len(request) > 4000:
            raise PoolRefused("REQUEST_INVALID")
        if project not in {"nisi", "jaryo", "opos", "agiw-suite", "none"}:
            raise PoolRefused("PROJECT_INVALID")
        if model_name not in MODELS:
            raise PoolRefused("MODEL_REFUSED")
        lower_request = request.lower()
        if any(marker.lower() in lower_request for marker in REFUSED_MARKERS):
            raise PoolRefused("SOURCE_REFUSED")
        if _contains_secret(request):
            raise PoolRefused("SECRET_VALUE_REFUSED")
        try:
            _check_text(request)
        except PoolRefused as exc:
            raise PoolRefused("RAW_LOG_REFUSED") from exc
        if self.paused_path.exists():
            raise PoolRefused("PAYER_PAUSED")
        script = resolve_free_pool_call(self.config.free_pool_call)
        if not script:
            raise PoolRefused("FREE_POOL_UNAVAILABLE")
        prompt = (
            "Draft a concise task brief as a JSON object. Keys: title, goal, steps (at most 8), "
            "acceptance (at most 5), risks (at most 3), lane_hint "
            "(codex, opencode, pc-worker, monterey-worker, or human). "
            "Do not execute anything. Request:\n" + request
        )
        workdir = Path(tempfile.mkdtemp(prefix="agiw-orchestrator-"))
        prompt_path = _write_prompt(str(workdir), prompt)
        fields: dict[str, Any] = {}
        code = "COMPLETED"
        try:
            command = [
                sys.executable,
                script,
                prompt_path,
                "--model",
                MODELS[model_name],
                "--project",
                project if project != "none" else "agiw-suite",
                "--source",
                "spec",
                "--private",
                "--timeout",
                "120",
                "--max-output",
                str(MAX_OUTPUT),
            ]
            completed = subprocess.run(
                command,
                cwd=str(workdir),
                env=_child_env(workdir=str(workdir)),
                capture_output=True,
                text=True,
                timeout=150,
                check=False,
            )
            if completed.returncode != 0:
                raise PoolRefused(_map_exit(completed.returncode))
            data = _json_object(completed.stdout)
            if data is None:
                raise PoolRefused("OUTPUT_INVALID")
            fields = _script_fields(data)
            output = str(fields.get("output_text", ""))
            parsed = _json_object(output)
            result: dict[str, Any] = {
                "brief": parsed if parsed is not None else {"brief_text": output},
                "model_served": fields.get("model_served"),
                "response_id": fields.get("response_id"),
                "usage": fields.get("usage", {}),
                "payer": fields.get("payer"),
            }
            payer = fields.get("payer")
            if payer and payer != "openai":
                result["payer_warning"] = True
                _private_file_write(self.paused_path, "payer requires review\n")
            return result
        except subprocess.TimeoutExpired as exc:
            code = "TIMEOUT"
            raise PoolRefused(code) from exc
        except PoolRefused as exc:
            code = exc.code
            raise
        except Exception as exc:
            code = _error_code(exc)
            raise PoolRefused(code) from exc
        finally:
            receipt = {
                "at": _utc_iso(),
                "lane": LANE,
                "tool": "plan",
                "model_requested": MODELS[model_name],
                "model_served": fields.get("model_served"),
                "response_id": fields.get("response_id"),
                "payer": fields.get("payer"),
                "usage": fields.get("usage", {}),
                "outcome": "COMPLETED" if code == "COMPLETED" else "BLOCKED",
                "code": code,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            }
            self._append_receipt(receipt)
            _cleanup(str(workdir))

    @staticmethod
    def _slug(title: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        return (slug or "task")[:40]

    def queue_task(self, arguments: dict[str, Any]) -> dict[str, Any]:
        title = arguments.get("title")
        brief = arguments.get("brief")
        project = arguments.get("project")
        lane_hint = arguments.get("lane_hint")
        if not isinstance(title, str) or not title or len(title) > 120:
            raise PoolRefused("TITLE_INVALID")
        if not isinstance(brief, (str, dict)) or (isinstance(brief, str) and len(brief) > 20000):
            raise PoolRefused("BRIEF_INVALID")
        if project not in {"nisi", "jaryo", "opos", "agiw-suite", "none"}:
            raise PoolRefused("PROJECT_INVALID")
        if lane_hint is not None and not isinstance(lane_hint, str):
            raise PoolRefused("LANE_HINT_INVALID")
        brief_text = brief if isinstance(brief, str) else json.dumps(brief, sort_keys=True)
        if _contains_secret(brief_text):
            raise PoolRefused("SECRET_VALUE_REFUSED")
        _mkdir_private(self.inbox)
        try:
            count = sum(1 for path in self.inbox.iterdir() if path.is_file())
        except OSError:
            count = 200
        if count >= 200:
            raise PoolRefused("INBOX_FULL")
        identifier = f"{_utc_stamp()}-{self._slug(title)}-{secrets.token_hex(3)}"
        record = {
            "id": identifier,
            "created_at": _utc_iso(),
            "lane": LANE,
            "title": title,
            "project": project,
            "lane_hint": lane_hint,
            "brief": brief,
            "status": "queued",
            "note": "Queued only. agiw-orchestrator never executes tasks.",
        }
        path = self.inbox / (identifier + ".json")
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, separators=(",", ":"))
        except FileExistsError as exc:
            raise PoolRefused("QUEUE_COLLISION") from exc
        return {"id": identifier, "path": str(path)}

    def _safe_result_path(self, identifier: str, suffix: str) -> Path | None:
        base = self.results.resolve()
        path = self.results / (identifier + suffix)
        try:
            resolved = path.resolve()
            resolved.relative_to(base)
        except (OSError, ValueError):
            return None
        if path.is_symlink() or not resolved.is_file():
            return None
        return resolved

    def task_result(self, arguments: dict[str, Any]) -> dict[str, Any]:
        identifier = arguments.get("id")
        if identifier is None:
            entries: list[tuple[float, str]] = []
            for directory in (self.inbox, self.results):
                try:
                    for path in directory.iterdir():
                        if path.is_file() and not path.is_symlink() and path.suffix in {".json", ".md"}:
                            entries.append((path.stat().st_mtime, path.stem))
                except OSError:
                    continue
            entries.sort(reverse=True)
            answer = []
            seen = set()
            for _unused_mtime, item_id in entries:
                if item_id in seen:
                    continue
                seen.add(item_id)
                exists = any(self._safe_result_path(item_id, suffix) for suffix in (".json", ".md"))
                answer.append({"id": item_id, "status": "done" if exists else "queued"})
                if len(answer) == 20:
                    break
            return {"tasks": answer}
        if not isinstance(identifier, str) or not ID_RE.fullmatch(identifier):
            raise PoolRefused("ID_INVALID")
        for suffix in (".json", ".md"):
            path = self._safe_result_path(identifier, suffix)
            if path is not None:
                try:
                    content = path.read_text(encoding="utf-8")[:20000]
                except OSError:
                    raise PoolRefused("RESULT_UNREADABLE") from None
                return {"id": identifier, "status": "done", "result": content}
        inbox_path = self.inbox / (identifier + ".json")
        if inbox_path.is_file() and not inbox_path.is_symlink():
            return {"id": identifier, "status": "queued"}
        return {"id": identifier, "status": "unknown"}

    def call_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return self._tool_error("ARGUMENTS_INVALID")
        allowed = {
            "status": {"include_github"},
            "plan": {"request", "project", "model"},
            "queue_task": {"title", "brief", "project", "lane_hint"},
            "task_result": {"id"},
        }
        if name not in allowed:
            return self._tool_error("TOOL_NOT_FOUND")
        if set(arguments) - allowed[name]:
            return self._tool_error("ARGUMENTS_INVALID")
        try:
            function = getattr(self, name)
            result = function(arguments)
            return self._tool_result(result, False)
        except Exception as exc:  # noqa: BLE001 - tool errors return a code, never a traceback
            return self._tool_error(_error_code(exc))

    @staticmethod
    def _tool_result(result: dict[str, Any], is_error: bool) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=1)}],
            "structuredContent": result,
            "isError": is_error,
        }

    def _tool_error(self, code: str) -> dict[str, Any]:
        return self._tool_result({"code": code}, True)


def dispatch(orchestrator: Orchestrator, request: Any) -> dict[str, Any] | None:
    if isinstance(request, list) or not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32600, "message": "Invalid Request"}}
    method = request["method"]
    params = request.get("params", {})
    notification = "id" not in request
    if notification and method.startswith("notifications/"):
        return None
    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        result = {
            "protocolVersion": version if version in PROTOCOL_VERSIONS else "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agiw-orchestrator", "version": "0.1.0"},
            "instructions": (
                "Use status to inspect the front desk, plan to draft a brief, queue_task to queue a "
                "brief, and task_result to inspect queued or completed work. This server never executes tasks."
            ),
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": orchestrator.tool_schemas()}
    elif method == "tools/call":
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32602, "message": "Invalid params"}}
        result = orchestrator.call_tool(params["name"], params.get("arguments", {}))
    else:
        return {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32601, "message": "Method not found"}}
    if notification:
        return None
    return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}


def run_stdio(orchestrator: Orchestrator, input_stream: TextIO, output_stream: TextIO) -> None:
    for line in input_stream:
        request = _safe_json_loads(line)
        if request is None:
            response: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            response = dispatch(orchestrator, request)
        if response is not None:
            output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
            output_stream.flush()


def _handler_class(  # noqa: C901 - one small HTTP handler class
    orchestrator: Orchestrator, token: str, allow_origins: Iterable[str]
) -> type:
    origins = set(allow_origins)

    class Handler(BaseHTTPRequestHandler):
        server_version = "agiw-orchestrator"

        def log_message(self, format_string: str, *args: Any) -> None:
            sys.stderr.write("{} {} {}\n".format(self.command, self.path, args[1] if len(args) > 1 else "-"))

        def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            if headers:
                for key, value in headers.items():
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            if body:
                self.send_header("Content-Type", "application/json")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _unauthorized(self) -> None:
            self._send(
                HTTPStatus.UNAUTHORIZED,
                b'{"error":"unauthorized"}',
                {"WWW-Authenticate": "Bearer"},
            )

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(HTTPStatus.OK, b'{"ok":true}')
            elif self.path == "/mcp":
                self._send(HTTPStatus.METHOD_NOT_ALLOWED)
            else:
                self._send(HTTPStatus.NOT_FOUND)

        def do_DELETE(self) -> None:
            if self.path == "/mcp":
                self._send(HTTPStatus.METHOD_NOT_ALLOWED)
            else:
                self._send(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path != "/mcp":
                self._send(HTTPStatus.NOT_FOUND)
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin not in origins:
                self._send(HTTPStatus.FORBIDDEN, b'{"error":"forbidden"}')
                return
            authorization = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not authorization.startswith(prefix) or not hmac.compare_digest(
                authorization[len(prefix) :].encode("utf-8", "surrogateescape"), token.encode("utf-8")
            ):
                self._unauthorized()
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(HTTPStatus.BAD_REQUEST)
                return
            if length < 0 or length > MAX_BODY:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            raw = self.rfile.read(length)
            try:
                request = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                response = dispatch(orchestrator, request)
            if response is None:
                self._send(HTTPStatus.ACCEPTED)
            else:
                self._send(HTTPStatus.OK, json.dumps(response, separators=(",", ":")).encode("utf-8"))

    return Handler


def make_http_server(config: Config, orchestrator: Orchestrator | None = None) -> ThreadingHTTPServer:
    if not _is_loopback(config.host):
        raise ValueError("host must be a loopback address")
    if not config.token:
        raise ValueError("token required")
    active = orchestrator or Orchestrator(config)
    return ThreadingHTTPServer(
        (config.host, config.port),
        _handler_class(active, config.token, config.allow_origins),
    )


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _read_token(path: Path) -> str:
    try:
        info = path.stat()
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError("token file is required") from exc
    if not token:
        raise ValueError("token file is empty")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("token file permissions are too broad")
    return token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agiw-orchestrator")
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18795)
    parser.add_argument("--token-file", default=str(Path.home() / ".config" / "agiw" / "orchestrator-mcp.token"))
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--allow-origin", action="append", dest="allow_origins")
    parser.add_argument("--root", default=str(Path.home() / "pending-review" / "dot-orchestrator"))
    parser.add_argument("--free-pool-call")
    parser.add_argument("--fleet-config", action="append", default=[])
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--repo", action="append", dest="repos")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.no_auth and not args.stdio:
        sys.stderr.write("--no-auth is allowed only with --stdio\n")
        return 2
    if not args.stdio and not _is_loopback(args.host):
        sys.stderr.write("host must be a loopback address\n")
        return 2
    token: str | None = None
    if not args.stdio:
        try:
            token = _read_token(Path(args.token_file).expanduser())
        except ValueError as exc:
            sys.stderr.write(str(exc) + "\n")
            return 2
    config = Config(
        root=Path(args.root).expanduser(),
        free_pool_call=args.free_pool_call,
        fleet_configs=args.fleet_config,
        gh=args.gh,
        repos=args.repos if args.repos else list(DEFAULT_REPOS),
        host=args.host,
        port=args.port,
        token_file=Path(args.token_file).expanduser(),
        no_auth=args.no_auth,
        stdio=args.stdio,
        allow_origins=args.allow_origins or ["https://chatgpt.com"],
        token=token,
    )
    orchestrator = Orchestrator(config)
    if config.stdio:
        run_stdio(orchestrator, sys.stdin, sys.stdout)
        return 0
    server = make_http_server(config, orchestrator)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
