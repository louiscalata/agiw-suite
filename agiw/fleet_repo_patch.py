"""Bounded repo-patch program for the fleet preview worker.

Clones one public GitHub repo at one commit into a fresh temp dir, asks a patcher (by default the
worker's loopback model) for new contents of allowlisted files only, and returns the staged unified
diff. It never pushes, never writes outside the temp clone and always removes it.

The default patcher asks the loopback model for SEARCH/REPLACE blocks: a path line, a `<<<<<<< SEARCH`
marker, the exact current lines, `=======`, the replacement and `>>>>>>> REPLACE`. Several blocks may
touch one or several allowlisted files, so a small model edits a few lines instead of rewriting a whole
document. Each SEARCH must occur exactly once, with one whitespace-tolerant retry. A reply that is JSON
`{"files": {"<path>": "<content>"}}` keeps the older full-file behaviour. Either way git judges what
lands on disk and the staged diff limits still apply.

Jev pre/post analysis is on by default; set jev_analysis=false to skip both phases.
Skipped means explicitly disabled; not_run means the job blocked before that phase.
Unavailable means no analyzer or a failed analysis; patching and diff reporting proceed
without a plan or verdict from that phase. Analyzers are injected, not auto-discovered:
repo-patch does not invoke a CLI, a hosted service, or a personal ledger for analysis.
The fleet worker may inject the optional jev-task CLI adapter from `agiw.fleet_jev_task`.
Analysis input is limited to 12,000 characters (first/last 6,000); receipts record
its size, hash and truncation flag, not the input. Plans are capped at 4,000 characters
and verdicts at 64.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urlsplit

from . import fleet_local_pilot as pilot

PROGRAM = "repo-patch"


class Refused(RuntimeError):  # noqa: N818 - matches the sibling fleet modules
    pass


def _ref(code):
    raise Refused(code)


class _DiffRefused(Refused):
    """A diff-limit refusal that also carries the facts needed to explain it."""

    def __init__(self, code, diff_bytes, changed_lines, excerpt):
        super().__init__(code)
        self.diff_bytes = diff_bytes
        self.changed_lines = changed_lines
        self.excerpt = excerpt


def _diff_excerpt(diff_text):
    return pilot.sanitize_text(diff_text[:4096], limit=2048)


def _changed_line_count(diff_text):
    """Count + and - lines, but not the +++/--- file headers."""
    count = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            count += 1
    return count


def _is_git_repo(path):
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            capture_output=True,
            check=False,
            text=True,
        )
        return r.returncode == 0
    except OSError:
        return False


def _normalize_repo(repo, allow_local_source=False):
    if not isinstance(repo, str) or not repo:
        _ref("REPO_REFUSED")
    if allow_local_source and os.path.isabs(repo):
        p = Path(repo)
        if not p.exists() or not _is_git_repo(p):
            _ref("REPO_REFUSED")
        return str(p.resolve())
    # Project-queue entries annotate the repo, e.g. "github.com/owner/name (PUBLIC, default branch main)".
    s = re.sub(r"\s+\([^()]*\)$", "", repo.strip())
    if s.startswith("github.com/"):
        s = "https://" + s
    if s.endswith(".git"):
        s = s[:-4]
    u = urlsplit(s)
    if u.scheme != "https" or u.hostname != "github.com" or u.username or u.password or u.query or u.fragment:
        _ref("REPO_REFUSED")
    parts = u.path.strip("/").split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts):
        _ref("REPO_REFUSED")
    return f"https://github.com/{parts[0]}/{parts[1]}"


def _validate_allowlist(items):
    if not isinstance(items, list) or not (1 <= len(items) <= 32):
        _ref("ALLOWLIST_REFUSED")
    out = []
    seen = set()
    for item in items:
        if not isinstance(item, str) or not item or "\x00" in item or "\\" in item or item.startswith("-"):
            _ref("ALLOWLIST_REFUSED")
        p = PurePosixPath(item)
        if p.is_absolute() or ".." in p.parts or ".git" in p.parts or any(part in ("", ".") for part in p.parts):
            _ref("ALLOWLIST_REFUSED")
        s = p.as_posix()
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def validate_spec(payload, allow_local_source=False):
    if not isinstance(payload, dict):
        _ref("REPO_REFUSED")
    repo = _normalize_repo(payload.get("repo"), allow_local_source=allow_local_source)
    commit = payload.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        _ref("COMMIT_REFUSED")
    # "bounded_scope" is the project-queue name for the same file list.
    allowlist = _validate_allowlist(payload.get("allowlist", payload.get("bounded_scope")))
    task = payload.get("task")
    if not isinstance(task, str) or not (1 <= len(task) <= 8000):
        _ref("TASK_REFUSED")
    max_diff_bytes = payload.get("max_diff_bytes")
    if type(max_diff_bytes) is not int or not (1 <= max_diff_bytes <= 262144):
        _ref("MAX_DIFF_REFUSED")
    max_changed_lines = payload.get("max_changed_lines")
    if max_changed_lines is not None and (type(max_changed_lines) is not int or not (1 <= max_changed_lines <= 10000)):
        _ref("MAX_CHANGED_LINES_REFUSED")
    timeout_seconds = payload.get("timeout_seconds", 600)
    if type(timeout_seconds) is not int or not (1 <= timeout_seconds <= 900):
        _ref("TIMEOUT_REFUSED")
    jev_analysis = payload.get("jev_analysis", True)
    if not isinstance(jev_analysis, bool):
        _ref("JEV_ANALYSIS_REFUSED")
    return {
        "repo": repo,
        "commit": commit.lower(),
        "allowlist": allowlist,
        "task": task,
        "max_diff_bytes": max_diff_bytes,
        "max_changed_lines": max_changed_lines,
        "timeout_seconds": timeout_seconds,
        "jev_analysis": jev_analysis,
    }


# Keep git from launching background helpers that lock the temp checkout on Windows.
_GIT_FLAGS = (
    "-c",
    "gc.auto=0",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "maintenance.auto=false",
    "-c",
    "credential.helper=",
)


def _env(tmp, name=os.name, environ=os.environ):
    home = str(tmp / "home")
    env = {
        "PATH": environ.get("PATH", ""),
        "HOME": home,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if name == "nt":
        root = environ.get("SYSTEMROOT") or r"C:\Windows"
        env.update(SYSTEMROOT=root, WINDIR=environ.get("WINDIR") or root, TEMP=home, TMP=home, USERPROFILE=home)
        for key in ("COMSPEC", "PATHEXT"):
            if key in environ:
                env[key] = environ[key]
    else:
        env["GIT_ASKPASS"] = shutil.which("false") or "false"
    return env


def _git(args, cwd, tmp, timeout):
    subprocess.run(
        ["git"] + list(_GIT_FLAGS) + list(args),
        cwd=str(cwd),
        env=_env(tmp),
        capture_output=True,
        check=True,
        timeout=timeout,
    )


def _git_out(args, cwd, tmp, timeout):
    r = subprocess.run(
        ["git"] + list(_GIT_FLAGS) + list(args),
        cwd=str(cwd),
        env=_env(tmp),
        capture_output=True,
        check=True,
        timeout=timeout,
    )
    return r.stdout


def _chmod_writable(path):
    try:
        st = os.lstat(path)
        mode = stat.S_IMODE(st.st_mode) | stat.S_IWRITE
        if stat.S_ISDIR(st.st_mode):
            mode |= stat.S_IREAD | stat.S_IEXEC  # keep the dir searchable after adding write
        os.chmod(path, mode)
    except OSError:
        # Windows read-only files may still accept the classic S_IWRITE-only chmod.
        try:
            os.chmod(path, stat.S_IWRITE)
        except OSError:
            pass


def _chmod_tree_writable(path):
    """Clear read-only bits before rmtree. Does not follow symlinks."""
    _chmod_writable(path)
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            return
    except OSError:
        return
    try:
        entries = list(os.scandir(path))
    except OSError:
        return
    for entry in entries:
        _chmod_tree_writable(entry.path)


def _clear_readonly_and_retry(func, path, _exc):
    """Clear the Windows read-only bit and retry unlink/rmdir. Do not retry os.open (fd rmtree)."""
    _chmod_writable(path)
    if func in (os.unlink, os.remove, os.rmdir):
        try:
            func(path)
        except OSError:
            pass


def _rmtree_onerror(func, path, exc_info):
    _clear_readonly_and_retry(func, path, exc_info[1] if exc_info else None)


def _rmtree(path):
    """Remove a temp checkout. Git objects are often read-only; Windows may also hold a lock briefly."""
    path = str(path)
    if sys.version_info >= (3, 12):
        kwargs = {"onexc": _clear_readonly_and_retry}
    else:
        kwargs = {"onerror": _rmtree_onerror}
    delays = (0.05, 0.15, 0.4)
    for attempt, delay in enumerate((0,) + delays):
        if delay:
            time.sleep(delay)
        if not os.path.lexists(path):
            return
        _chmod_tree_writable(path)
        try:
            shutil.rmtree(path, **kwargs)
        except OSError:
            if attempt == len(delays):
                shutil.rmtree(path, ignore_errors=True)
                return
            continue
        if not os.path.lexists(path):
            return
    shutil.rmtree(path, ignore_errors=True)


def _inside(root, path):
    try:
        root = str(Path(root).resolve())
        return os.path.commonpath([root, str(Path(path).resolve())]) == root
    except (ValueError, OSError, TypeError):
        return False


def _safe_target(workdir, rel):
    rel_path = PurePosixPath(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts or any(part in ("", ".") for part in rel_path.parts):
        _ref("PATH_ESCAPE")
    cur = Path(workdir)
    parts = list(rel_path.parts)
    for i, part in enumerate(parts):
        cur = cur / part
        if cur.exists() or cur.is_symlink():
            try:
                st = os.lstat(str(cur))
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(st.st_mode):
                    _ref("PATH_ESCAPE")
        if i < len(parts) - 1:
            if cur.exists():
                if not cur.is_dir():
                    _ref("PATH_ESCAPE")
            else:
                try:
                    os.mkdir(str(cur))
                except FileExistsError:
                    if not cur.is_dir():
                        _ref("PATH_ESCAPE")
                if os.path.islink(str(cur)):
                    _ref("PATH_ESCAPE")
    if not _inside(workdir, cur):
        _ref("PATH_ESCAPE")
    return cur


def _read_files(workdir, allowlist):
    files = {}
    total = 0
    for rel in allowlist:
        p = workdir / rel
        try:
            st = os.lstat(str(p))
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
            _ref("FILE_REFUSED")
        total += st.st_size
        if total > 262144:
            _ref("FILE_REFUSED")
        # Bytes, not text mode: universal-newline reads turn CRLF into LF, so writing back would
        # rewrite every line of a CRLF file.
        files[rel] = p.read_bytes().decode("utf-8", "surrogateescape")
    return files


def _parse_diff_limits(diff_text, max_changed_lines, diff_bytes=None):
    if max_changed_lines is None:
        return
    count = _changed_line_count(diff_text)
    if count > max_changed_lines:
        if diff_bytes is None:
            diff_bytes = len(diff_text.encode("utf-8", "surrogateescape"))
        raise _DiffRefused("DIFF_TOO_MANY_LINES", diff_bytes, count, _diff_excerpt(diff_text))


def _blocked(spec, code, analysis, stderr=None, patch=None):
    result = {
        "outcome": "BLOCKED",
        "code": code,
        "repo": spec["repo"],
        "commit": spec["commit"],
        "changedFiles": [],
        "diff": "",
        "diffSha256": hashlib.sha256(b"").hexdigest(),
        "diffBytes": 0,
        **analysis,
    }
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    if stderr:
        stderr = re.sub(r"://[^\s/?#]*@", "://[redacted]@", stderr)
        stderr = pilot.sanitize_text(stderr, limit=2048)
        if stderr:
            result["git_stderr"] = stderr.encode("utf-8", "replace")[:2048].decode("utf-8", "ignore")
    if isinstance(patch, dict):
        result["patch"] = patch
    return result


def _clone(spec, tmp):
    """Clone into tmp/repo, pin the commit, then make every push fail (any transport, hooks or not)."""
    t = spec["timeout_seconds"]
    workdir = tmp / "repo"
    _git(["clone", "--quiet", "--no-checkout", "--filter=blob:none", spec["repo"], str(workdir)], tmp, tmp, t)
    _git(["-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", spec["commit"]], workdir, tmp, t)
    if _git_out(["rev-parse", "HEAD"], workdir, tmp, t).decode().strip().lower() != spec["commit"]:
        _ref("COMMIT_NOT_FOUND")
    _git(["config", "gc.auto", "0"], workdir, tmp, t)
    _git(["config", "core.fsmonitor", "false"], workdir, tmp, t)
    _git(["config", "maintenance.auto", "false"], workdir, tmp, t)
    _git(["config", "protocol.allow", "never"], workdir, tmp, t)
    _git(["remote", "set-url", "--push", "origin", "no-push://disabled"], workdir, tmp, t)
    hooks = workdir / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    pre_push = hooks / "pre-push"
    pre_push.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(str(pre_push), 0o755)
    _git(["config", "core.hooksPath", str(hooks)], workdir, tmp, t)
    return workdir


def _apply(workdir, allowlist, out):
    if not isinstance(out, dict):
        _ref("PATCH_OUTPUT_INVALID")
    for rel, content in out.items():
        if not isinstance(rel, str) or rel not in allowlist:
            _ref("ALLOWLIST_VIOLATION")
        if content is not None and not isinstance(content, str):
            _ref("PATCH_OUTPUT_INVALID")
        target = _safe_target(workdir, rel)
        if content is None:
            if target.exists() or target.is_symlink():
                target.unlink()
        else:
            # Bytes, not text mode: on Windows write_text turns every LF into CRLF, and with no
            # eol attribute git then stages a whole-file rewrite (Q003: 1 edit, 262 changed lines).
            target.write_bytes(content.encode("utf-8", "surrogateescape"))


def _staged_diff(spec, workdir, tmp):
    """Judge what git sees on disk, not what the patcher claims it changed."""
    t = spec["timeout_seconds"]
    _git(["add", "-A"], workdir, tmp, t)
    changed = _git_out(["diff", "--cached", "--name-only", "-z"], workdir, tmp, t)
    changed_files = [x for x in changed.decode("utf-8", "surrogateescape").split("\x00") if x]
    if any(path not in spec["allowlist"] for path in changed_files):
        _ref("ALLOWLIST_VIOLATION")
    diff = _git_out(["diff", "--cached", "--binary", "--no-color", "--no-ext-diff"], workdir, tmp, t)
    diff_text = diff.decode("utf-8", "surrogateescape")
    changed_lines = _changed_line_count(diff_text)
    if len(diff) > spec["max_diff_bytes"]:
        raise _DiffRefused("DIFF_TOO_LARGE", len(diff), changed_lines, _diff_excerpt(diff_text))
    _parse_diff_limits(diff_text, spec["max_changed_lines"], len(diff))
    return changed_files, diff


def _pending_analysis(enabled, phase):
    if not enabled:
        return {"status": "skipped", "reason": "jev_analysis=false"}
    return {"status": "not_run", "reason": f"job blocked before {phase}-analysis"}


def _bound_plan(plan):
    marker = "\n[truncated]"
    return plan if len(plan) <= 4000 else plan[: 4000 - len(marker)] + marker


def _analyze(analyzer, text, phase, field):
    metadata = {
        "truncated": len(text) > 12000,
        "inputChars": len(text),
        "inputSha256": hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest(),
    }
    if metadata["truncated"]:
        text = text[:6000] + text[-6000:]

    def unavailable(code, stage=phase):
        return {"status": "unavailable", "stage": stage, "errorCode": code, **metadata}

    if analyzer is None:
        return unavailable("JEV_UNAVAILABLE", "no_analyzer")
    try:
        result = analyzer(text)
        if not isinstance(result, dict):
            return unavailable("JEV_INVALID_OUTPUT")
        if result.get("status") == "unavailable":
            code = result.get("errorCode")
            reported_stage = result.get("stage")
            if not isinstance(code, str) or not code.strip():
                code = "JEV_UNAVAILABLE"
            if not isinstance(reported_stage, str) or not reported_stage.strip():
                reported_stage = phase
            return unavailable(code.strip(), reported_stage.strip())
        value = result.get(field)
        if result.get("status") != "ok" or not isinstance(value, str) or not value.strip():
            return unavailable("JEV_INVALID_OUTPUT")
        if field == "plan":
            value = _bound_plan(value)
        else:
            value = value.strip()[:64]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
                return unavailable("JEV_INVALID_OUTPUT")
    except (TimeoutError, subprocess.TimeoutExpired):
        return unavailable("JEV_TIMEOUT")
    except Exception:  # noqa: BLE001 - advisory only; never retain exception text or raw analyzer output
        return unavailable("JEV_ANALYZER_ERROR")
    return {"status": "ok", field: value, **metadata}


def _accepts(patcher, name):
    try:
        params = inspect.signature(patcher).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        or (p.name == name and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY))
        for p in params
    )


def _accepts_analysis(patcher):
    return _accepts(patcher, "analysis")


def _last_patch(patcher):
    """Patch diagnostics recorded by the closure, when the patcher keeps any."""
    patch = getattr(patcher, "last", None)
    return patch if isinstance(patch, dict) else None


def _patch_field(patcher, *, no_change=False):
    patch = _last_patch(patcher)
    if not patch:
        return {}
    if not no_change:
        return {"patch": patch}
    # An empty diff is the only way to see what a successful reply wrote: keep the excerpt here
    # and nowhere else, so a COMPLETED receipt still shows outputExcerpt None.
    patch = dict(patch)
    patch["outputExcerpt"] = getattr(patcher, "last_output_excerpt", None)
    return {"patch": patch}


def run(spec, patcher, *, allow_local_source=False, tmp_root=None, jev_pre=None, jev_post=None):
    """Clone, patch and diff in a temp dir that is always removed. Never pushes.

    Optional offline analyzers take one projected string and return a dict with status
    and, on success, a plan (pre: task/files) or verdict (post: diff). Do not echo input.
    """
    spec = validate_spec(spec, allow_local_source=allow_local_source)
    analysis = {f"jev{phase.title()}": _pending_analysis(spec["jev_analysis"], phase) for phase in ("pre", "post")}
    if tmp_root is not None:
        Path(tmp_root).mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=tmp_root)).resolve()
    try:
        (tmp / "home").mkdir()
        workdir = _clone(spec, tmp)
        files = _read_files(workdir, spec["allowlist"])
        if spec["jev_analysis"]:
            text = "\n\n".join(
                [spec["task"]] + [f"--- {path} ---\n{content}".replace("\r\n", "\n") for path, content in files.items()]
            )
            analysis["jevPre"] = _analyze(jev_pre, text, "pre", "plan")
        try:
            kwargs = {"files": dict(files), "task": spec["task"], "workdir": workdir}
            plan = analysis["jevPre"].get("plan")
            if plan is not None and _accepts_analysis(patcher):
                kwargs["analysis"] = plan
            if _accepts(patcher, "allowlist"):
                kwargs["allowlist"] = list(spec["allowlist"])
            out = patcher(**kwargs)
        except Refused:
            raise
        except Exception:  # noqa: BLE001 - any patcher failure is a BLOCKED job, never a crash
            return _blocked(spec, "PATCH_FAILED", analysis, patch=_last_patch(patcher))
        _apply(workdir, spec["allowlist"], out)
        changed_files, diff = _staged_diff(spec, workdir, tmp)
        diff_text = diff.decode("utf-8", "surrogateescape")
        if spec["jev_analysis"]:
            analysis["jevPost"] = _analyze(jev_post, diff_text, "post", "verdict")
        return {
            "outcome": "COMPLETED" if diff else "FAILED",
            "code": None if diff else "NO_CHANGE",
            "repo": spec["repo"],
            "commit": spec["commit"],
            "changedFiles": changed_files,
            "diff": diff_text,
            "diffSha256": hashlib.sha256(diff).hexdigest(),
            "diffBytes": len(diff),
            **analysis,
            **_patch_field(patcher, no_change=not diff),
        }
    except subprocess.CalledProcessError as exc:
        code = "COMMIT_NOT_FOUND" if "checkout" in exc.cmd else "GIT_FAILED"
        return _blocked(spec, code, analysis, exc.stderr)
    except subprocess.TimeoutExpired as exc:
        return _blocked(spec, "GIT_TIMEOUT", analysis, exc.stderr)
    except OSError:
        return _blocked(spec, "IO_FAILED", analysis)
    except _DiffRefused as exc:
        code = str(exc)
        patch = dict(_last_patch(patcher) or {})
        patch["error"] = code
        patch["diffBytes"] = exc.diff_bytes
        patch["maxDiffBytes"] = spec["max_diff_bytes"]
        patch["changedLines"] = exc.changed_lines
        patch["diffExcerpt"] = exc.excerpt
        return _blocked(spec, code, analysis, patch=patch)
    except Refused as exc:
        return _blocked(spec, str(exc), analysis, patch=_last_patch(patcher))
    finally:
        _rmtree(tmp)


def _strip_fences(text):
    """Strip one enclosing code fence; kept for callers of the old JSON-only patcher."""
    t = text.strip()
    if t.startswith("```"):
        t = "\n".join(t.splitlines()[1:])
        if t.rstrip().endswith("```"):
            t = "\n".join(t.splitlines()[:-1])
    return t.strip()


_SEARCH_MARK = re.compile(r"^<{5,9}\s*(?:SEARCH)?\s*$", re.I)
_DIVIDER_MARK = re.compile(r"^={5,9}\s*$")
_REPLACE_MARK = re.compile(r"^>{5,9}\s*(?:REPLACE)?\s*$", re.I)
_FENCE_LINE = re.compile(r"^\s*```")
_THINKING = re.compile(r"(?is)<think[^>]*>.*?</think[^>]*>")
_THINKING_OPEN = re.compile(r"(?is)<think[^>]*>")
_PATH_PREFIX = re.compile(r"^(?:file|path)\s*:\s*", re.I)


class _EditRefused(Refused):
    """A refusal plus the machine reason recorded on the patch diagnostics."""

    def __init__(self, code, reason):
        super().__init__(code)
        self.reason = reason


def _refused(code, reason):
    raise _EditRefused(code, reason)


def _lf(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _first_non_blank(lines, start=0):
    for index in range(start, len(lines)):
        if lines[index].strip():
            return index
    return None


def _last_non_blank(lines, stop=None):
    for index in range((len(lines) if stop is None else stop) - 1, -1, -1):
        if lines[index].strip():
            return index
    return None


def _clean_edit_text(text):
    """Normalize newlines, drop reasoning and remove one enclosing code fence."""
    if not isinstance(text, str) or not text.strip():
        _refused("PATCH_OUTPUT_INVALID", "NO_EDITS")
    cleaned = _THINKING.sub(" ", _lf(text))
    unclosed = _THINKING_OPEN.search(cleaned)
    if unclosed:
        cleaned = cleaned[: unclosed.start()]
    lines = cleaned.split("\n")
    first = _first_non_blank(lines)
    if first is not None and _FENCE_LINE.match(lines[first]):
        del lines[first]
        last = _last_non_blank(lines)
        if last is not None and _FENCE_LINE.match(lines[last]):
            del lines[last]
    return "\n".join(lines)


def _legacy_json_files(text):
    """The old full-file reply, or None when this is not a JSON files object."""
    try:
        data = json.loads(_clean_edit_text(text))
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("files"), dict):
        return data["files"]
    _refused("PATCH_OUTPUT_INVALID", "INVALID_JSON")


def _edit_path(line):
    value = line.strip().strip("`").strip()
    if len(value) > 4 and value.startswith("**") and value.endswith("**"):
        value = value[2:-2].strip()
    value = _PATH_PREFIX.sub("", value.lstrip("#").strip()).strip("`*").strip()
    return value or None


def _path_before(lines, index):
    """The path is the last non-empty line before the SEARCH marker, skipping fences."""
    for position in range(index - 1, -1, -1):
        line = lines[position]
        if not line.strip() or _FENCE_LINE.match(line):
            continue
        if _SEARCH_MARK.match(line) or _DIVIDER_MARK.match(line) or _REPLACE_MARK.match(line):
            return None
        return _edit_path(line)
    return None


def _next_mark(lines, start, pattern):
    for index in range(start, len(lines)):
        if pattern.match(lines[index]):
            return index
        if pattern is not _SEARCH_MARK and _SEARCH_MARK.match(lines[index]):
            return None
    return None


def _sole_allowlisted(allowlist):
    paths = [path for path in (allowlist or []) if isinstance(path, str) and path]
    return paths[0] if len(paths) == 1 else None


def parse_edits(text, allowlist=None):
    """Parse SEARCH/REPLACE blocks into (path, search, replace) triples."""
    lines = _clean_edit_text(text).split("\n")
    edits = []
    position = 0
    while position < len(lines):
        if not _SEARCH_MARK.match(lines[position]):
            position += 1
            continue
        divider = _next_mark(lines, position + 1, _DIVIDER_MARK)
        end = None if divider is None else _next_mark(lines, divider + 1, _REPLACE_MARK)
        if divider is None or end is None:
            _refused("PATCH_OUTPUT_INVALID", "MALFORMED_BLOCK")
        search = "\n".join(lines[position + 1 : divider])
        replace = "\n".join(lines[divider + 1 : end])
        edits.append((_path_before(lines, position), search, replace))
        position = end + 1
    if not edits:
        _refused("PATCH_OUTPUT_INVALID", "NO_EDITS")
    resolved = []
    for path, search, replace in edits:
        if path is None:
            path = _sole_allowlisted(allowlist)
            if path is None:
                _refused("PATCH_OUTPUT_INVALID", "MALFORMED_BLOCK")
        resolved.append((path, search, replace))
    return resolved


def _trimmed_lines(lines):
    out = [line.strip() for line in lines]
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return out


def _line_hits(haystack, needle):
    if not needle or len(needle) > len(haystack):
        return []
    return [at for at in range(len(haystack) - len(needle) + 1) if haystack[at : at + len(needle)] == needle]


def _replace_lines(lines, at, count, replace):
    body = [] if replace == "" else replace.split("\n")
    return "\n".join(lines[:at] + body + lines[at + count :])


def _replace_once(content, search, replace):
    """Replace the one place SEARCH names; refuse when it is missing or ambiguous.

    Tried in order, each only when the previous found nothing: whole lines exactly, whole lines
    with surrounding whitespace (and SEARCH's leading/trailing blank lines) ignored, then a raw
    substring for a SEARCH that quotes part of a line. Any step with several hits refuses.
    """
    lines = content.split("\n")
    exact = _line_hits(lines, search.split("\n"))
    if len(exact) > 1:
        _refused("PATCH_OUTPUT_INVALID", "SEARCH_NOT_UNIQUE")
    if exact:
        return _replace_lines(lines, exact[0], len(search.split("\n")), replace)
    needle = _trimmed_lines(search.split("\n"))
    trimmed = _line_hits([line.strip() for line in lines], needle)
    if len(trimmed) > 1:
        _refused("PATCH_OUTPUT_INVALID", "SEARCH_NOT_UNIQUE")
    if trimmed:
        return _replace_lines(lines, trimmed[0], len(needle), replace)
    hits = content.count(search) if search.strip() else 0
    if hits > 1:
        _refused("PATCH_OUTPUT_INVALID", "SEARCH_NOT_UNIQUE")
    if hits == 1:
        at = content.index(search)
        return content[:at] + replace + content[at + len(search) :]
    _refused("PATCH_OUTPUT_INVALID", "SEARCH_NOT_FOUND")


def apply_edits(files, edits, allowlist):
    """Apply edits in order; return the full new content of each edited path."""
    if not isinstance(edits, list) or not edits:
        _refused("PATCH_OUTPUT_INVALID", "NO_EDITS")
    allowed = {path for path in (allowlist or []) if isinstance(path, str)}
    current = {}
    crlf = {}
    for path, content in (files or {}).items():
        if not isinstance(content, str):
            _refused("PATCH_OUTPUT_INVALID", "INVALID_JSON")
        current[path] = _lf(content)
        crlf[path] = "\r\n" in content
    out = {}
    for edit in edits:
        if not isinstance(edit, (list, tuple)) or len(edit) != 3:
            _refused("PATCH_OUTPUT_INVALID", "MALFORMED_BLOCK")
        path, search, replace = edit
        if not isinstance(path, str) or path not in allowed:
            _ref("ALLOWLIST_VIOLATION")
        if not isinstance(search, str) or not isinstance(replace, str):
            _refused("PATCH_OUTPUT_INVALID", "MALFORMED_BLOCK")
        existing = current.get(path)
        if search:
            if existing is None:
                _refused("PATCH_OUTPUT_INVALID", "SEARCH_NOT_FOUND")
            updated = _replace_once(existing, _lf(search), _lf(replace))
        else:
            if existing is not None:
                _refused("PATCH_OUTPUT_INVALID", "EMPTY_SEARCH_EXISTING_FILE")
            updated = _lf(replace)
        current[path] = updated
        out[path] = updated.replace("\n", "\r\n") if crlf.get(path) else updated
    return out


def _normalize_edit_text(text):
    """Compare SEARCH and REPLACE as text: LF only, no trailing spaces, no edge blank lines."""
    lines = [line.rstrip() for line in _lf(text).split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _noop_edit_count(files, edits, out):
    """Count applied edits that changed nothing, so a receipt can name a reply that did no work."""
    count = 0
    for edit in edits:
        _path, search, replace = edit
        if _normalize_edit_text(search) == _normalize_edit_text(replace):
            count += 1
    originals = {path: _lf(content) for path, content in (files or {}).items() if isinstance(content, str)}
    if all(isinstance(content, str) and originals.get(path) == _lf(content) for path, content in out.items()):
        # Nothing on disk moved, so every edit that produced this result was a no-op.
        return len(edits)
    return count


def _noop_file_count(files, out):
    """The legacy full-file reply: a file whose new content equals the old one changed nothing."""
    originals = {path: _lf(content) for path, content in (files or {}).items() if isinstance(content, str)}
    return sum(1 for path, content in out.items() if isinstance(content, str) and originals.get(path) == _lf(content))


_NO_TOOLS = (
    "You have no tools. You cannot run commands, browse, or open other files. "
    "The current contents of each file are included in the user message under "
    "its ### <path> heading (as excerpts for large files)."
)

_EDIT_GUIDANCE = (
    "Always return at least one SEARCH/REPLACE block. Do the parts of the task "
    "the shown text supports and skip parts that need outside information.\n"
    "To ADD new text, SEARCH one or two existing lines next to where the text "
    "belongs and REPLACE them with those same lines plus the new lines."
)

_RESPOND_ONLY = "Respond only with SEARCH/REPLACE blocks, no explanation."

_FORMAT_BLOCK = f"""Format, repeated once per edit, for one or several files (angle brackets mark placeholders):

<path/from/allowlist>
<<<<<<< SEARCH
<exact existing lines copied from that file>
=======
<those lines with your change applied>
>>>>>>> REPLACE

{_RESPOND_ONLY}"""

_NEVER_EXAMPLE = "Never output the example itself. Use only the allowed paths and lines copied from the files shown."

# Q002: a small model returned the prompt's old worked example (greeting.txt hi -> hello) as its
# answer, so the prompt shows only the shape, with placeholders that cannot be a real edit.
_SYSTEM_PROMPT = f"""You edit files by returning SEARCH/REPLACE blocks.

{_NO_TOOLS}

{_EDIT_GUIDANCE}

{_FORMAT_BLOCK}

Rules:
- Make the smallest edits that complete the task. Never rewrite a whole file.
- Copy every SEARCH line exactly from the file, keeping the same whitespace.
- Keep each SEARCH short but unique: a few lines that appear once in the file.
- Put the file path on its own line directly above <<<<<<< SEARCH.
- To insert text, SEARCH <one existing line near the spot> and REPLACE it with <that same line>
  followed by <the new lines>.
- To create a missing file, leave SEARCH empty and put the new content in REPLACE.
- The REPLACE text must differ from the SEARCH text.
- Output only the blocks. No commentary, no JSON, no whole-file rewrites.
- {_NEVER_EXAMPLE}"""

# Paths from this and earlier prompt examples; a reply naming one (outside the allowlist) copied
# the example instead of editing the job's files.
_EXAMPLE_PATHS = frozenset({"path/to/file.md", "greeting.txt", "notes.md", "path/from/allowlist"})
_PLACEHOLDER = re.compile(r"<[^<>\n]*(?:path|lines|exact|change|allowlist)[^<>\n]*>", re.I)

_RETRY_NOTES = {
    "NO_EDITS": "Your previous reply contained no SEARCH/REPLACE blocks.",
    "EXAMPLE_COPY": (
        "Your previous reply copied the format example instead of editing the files. Never output the example itself."
    ),
    "OFF_ALLOWLIST": "Your previous reply edited paths that are not allowed.",
}

_RETRY_REMINDER = (
    "{note} "
    "The file contents are in the message above. "
    "Make the best minimal edit for the task now. "
    "To add text, SEARCH an existing line near where it belongs and REPLACE it "
    "with that line plus the new text. "
    "The REPLACE text must differ from the SEARCH text. "
    "Allowed paths: {paths}. " + _RESPOND_ONLY
)

_NOOP_QUOTE_LIMIT = 600


def _retry_note(reason, paths, noop_searches=()):
    """The one corrective message sent before the single retry."""
    if reason == "NOOP_EDITS":
        quoted = "\n---\n".join(text[:_NOOP_QUOTE_LIMIT] for text in noop_searches[:3])
        note = (
            "Your previous reply changed nothing: its REPLACE text was identical to this SEARCH text:\n"
            f"{quoted}\n"
            "The REPLACE must differ from that SEARCH. If the shown text needs no change for one part of "
            "the task, edit another part it supports."
        )
    else:
        note = _RETRY_NOTES.get(reason, _RETRY_NOTES["NO_EDITS"])
    return _RETRY_REMINDER.format(note=note, paths=", ".join(paths))


def _misdirected(edits, allowlist):
    """EXAMPLE_COPY or OFF_ALLOWLIST when no parsed edit targets an allowed path, else None."""
    allowed = set(allowlist or [])
    if not edits or any(path in allowed for path, _search, _replace in edits):
        return None
    for path, search, replace in edits:
        if path.lower() in _EXAMPLE_PATHS or _PLACEHOLDER.search(path + "\n" + search + "\n" + replace):
            return "EXAMPLE_COPY"
    return "OFF_ALLOWLIST"


_EXCERPT_THRESHOLD = 16000
_EXCERPT_BUDGET = 16000
_EXCERPT_CONTEXT = 4
_EXCERPT_HEAD = 10
_EXCERPT_SECTION = 40

_EXCERPT_STOPWORDS = frozenset(
    """
    this that with from make only docs file each must should into than then
    there their they have match exactly check first same class against correct any
    the and for not all are but its about also when what which while where will
    would could just made docs-only pass finding stale task edit edits files doc
    one two per via use used
    """.split()
)

_EXCERPT_SPELLINGS = {
    "licence": "license",
    "license": "licence",
    "colour": "color",
    "color": "colour",
    "behaviour": "behavior",
    "behavior": "behaviour",
}


def _task_keywords(task):
    tokens = re.findall(r"[a-z0-9][a-z0-9_./-]*[a-z0-9]|[a-z0-9]", (task or "").lower())
    words = set()

    def add(word):
        if len(word) >= 3 and word not in _EXCERPT_STOPWORDS:
            words.add(word)

    for token in tokens:
        token = token.rstrip(".,;:!?)")
        if len(token) < 3 or token in _EXCERPT_STOPWORDS:
            continue
        add(token)
        if "." in token or "/" in token:
            basename = token.rsplit("/", 1)[-1]
            add(basename)
            add(basename.rsplit(".", 1)[0])

    # Work from a snapshot so plural and spelling expansion is bounded.
    for word in tuple(words):
        base = word
        if len(word) >= 5 and word.isalpha():
            if word.endswith("s"):
                base = word[:-1]
                add(base)
            else:
                add(word + "s")
        alternate = _EXCERPT_SPELLINGS.get(base)
        if alternate:
            add(alternate)
            add(alternate + "s")
    return tuple(sorted(words))


def _merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _window_text(lines, window):
    start, end = window
    return "\n".join(lines[start - 1 : end])


def _window_cost(lines, window, prefix=None):
    start, end = window
    if prefix is None:
        text_chars = len(_window_text(lines, window))
    else:
        # Each prefix entry counts one separator per line; omit the last one.
        text_chars = prefix[end] - prefix[start - 1] - 1
    return text_chars + len(f"[lines {start}-{end}]") + 2


def _windows_cost(lines, windows, prefix=None):
    return sum(_window_cost(lines, window, prefix) for window in windows)


def _line_index(lines):
    """Per-line lookups: char prefix sums, heading levels and the nearest heading above."""
    prefix = [0]
    levels = [0] * (len(lines) + 1)
    above = [0] * (len(lines) + 1)
    nearest = 0
    for number, line in enumerate(lines, start=1):
        prefix.append(prefix[-1] + len(line) + 1)
        above[number] = nearest
        heading = re.match(r"^ {0,3}(#{1,6})(?=\s|$)", line)
        if heading:
            levels[number] = len(heading.group(1))
            nearest = number
    return prefix, levels, above


def _keyword_scores(lines, task):
    """Line number -> sum of rarity weights (1 / lines hit) of the task keywords on that line."""
    lowered = [line.lower() for line in lines]
    scores = {}
    for keyword in _task_keywords(task):
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])")
        matches = [number for number, line in enumerate(lowered, 1) if pattern.search(line)]
        for number in matches:
            scores[number] = scores.get(number, 0.0) + 1.0 / len(matches)
    return scores


def _section(levels, number, count):
    """A matched heading through the line before the next same-or-higher heading, capped."""
    end = min(count, number + _EXCERPT_SECTION - 1)
    for following in range(number + 1, end + 1):
        if 0 < levels[following] <= levels[number]:
            return number, following - 1
    return number, end


def _select_windows(content, task):
    """Select deterministic, merged, 1-based inclusive excerpt ranges.

    Lines are scored by rare task keywords (word-boundary matches; a matching heading counts
    double) and taken best first, each with a little context and its nearest heading; a
    matching heading brings its section. The head always stays. With no matches, the head
    grows to fill the budget. Ties go to the earlier line.
    """
    lines = content.splitlines()
    count = len(lines)
    if not count:
        return []
    prefix, levels, above = _line_index(lines)
    scores = _keyword_scores(lines, task)

    def add(windows, candidate, force=False):
        merged = _merge_ranges(windows + [candidate])
        if force or _windows_cost(lines, merged, prefix) <= _EXCERPT_BUDGET:
            return merged
        return windows

    windows = [(1, min(_EXCERPT_HEAD, count))]
    ranked = sorted(scores, key=lambda number: (-scores[number] * (2 if levels[number] else 1), number))
    for number in ranked:
        if levels[number]:
            windows = add(windows, _section(levels, number, count))
        if len(lines[number - 1]) > _EXCERPT_BUDGET:
            # Keep the exact SEARCH anchor, but don't add context around an oversized line.
            windows = add(windows, (number, number), force=True)
        else:
            context = (max(1, number - _EXCERPT_CONTEXT), min(count, number + _EXCERPT_CONTEXT))
            windows = add(windows, context)
        if above[number]:
            windows = add(windows, (above[number], above[number]))
    if not scores:
        while windows[0][1] < count:
            extended = add(windows, (1, windows[0][1] + 1))
            if extended == windows:
                break
            windows = extended
    return windows


def _fence_for(text):
    runs = re.findall(r"`{3,}", text)
    return "`" * (max(len(run) for run in runs) + 1) if runs else "```"


def _file_excerpt(content, task):
    if len(content) <= _EXCERPT_THRESHOLD:
        return None
    return content.splitlines(), _select_windows(content, task)


def _estimate_max_tokens(task, visible_file_chars):
    estimate = 512 + len(task) // 4 + visible_file_chars // 6
    return max(1024, min(pilot.MAX_MAX_TOKENS, estimate))


def _auto_max_tokens(files, task):
    """Estimate from the same file text used by the user prompt."""
    total = 0
    for content in (files or {}).values():
        if not isinstance(content, str):
            continue
        excerpt = _file_excerpt(content, task)
        if excerpt is None:
            total += len(content)
        else:
            lines, windows = excerpt
            total += sum(len(_window_text(lines, window)) for window in windows)
    return _estimate_max_tokens(task, total)


def _user_prompt(files, task, allowlist, analysis, excerpts=None, *, file_chars=None):
    allowed = ", ".join(allowlist or list(files))
    parts = [_NO_TOOLS, _EDIT_GUIDANCE, _FORMAT_BLOCK, f"Allowed paths: {allowed}"]
    for path, content in files.items():
        if file_chars is not None:
            file_chars[path] = 0
        if not isinstance(content, str):
            continue
        excerpt = _file_excerpt(content, task)
        if excerpt is not None:
            lines, windows = excerpt
            if excerpts is not None:
                excerpts[path] = [[start, end] for start, end in windows]
            heading = f"### {path} (excerpt of {len(lines)} lines; copy SEARCH lines exactly from these excerpts)"
            blocks = [heading]
            for start, end in windows:
                text = _window_text(lines, (start, end))
                fence = _fence_for(text)
                blocks.append(f"[lines {start}-{end}]\n{fence}\n{text}\n{fence}")
                if file_chars is not None:
                    file_chars[path] += len(text)
            parts.append("\n\n".join(blocks))
            continue

        body = content if content.endswith("\n") else content + "\n"
        fence = _fence_for(content)
        parts.append(f"### {path}\n{fence}\n{body}{fence}")
        if file_chars is not None:
            file_chars[path] = len(content)

    missing = [path for path in (allowlist or []) if path not in files]
    if missing:
        listed = "\n".join(f"- {path}" for path in missing)
        parts.append("### Paths that do not exist yet (create them with an empty SEARCH)\n" + listed)
    parts.append(f"### Task\n{task}")
    if analysis:
        parts.append("Jev pre-plan (advisory):\n" + _bound_plan(analysis))
    parts.append(f"Allowed paths: {allowed}. {_NEVER_EXAMPLE}")
    parts.append(_FORMAT_BLOCK)
    return "\n\n".join(parts)


def _match_line_endings(original, new):
    """Keep a CRLF file CRLF when a legacy full-file reply arrives with LF-only content."""
    if not isinstance(original, str) or not isinstance(new, str):
        return new
    if "\r\n" in original and "\r" not in new:
        return new.replace("\n", "\r\n")
    return new


def _chat_reply(opener, endpoint, prompt, timeout, last):
    """POST one chat completion and return (content, finish_reason). Refusals record `last`."""
    body = json.dumps(prompt).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
    try:
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read(1024 * 1024 + 1)
        except HTTPError as exc:
            last["httpStatus"] = exc.code
            raise
        except (TimeoutError, socket.timeout):
            last["timeout"] = True
            raise
    except (OSError, ValueError) as exc:
        last["error"] = "HTTP_ERROR"
        raise Refused("PATCH_OUTPUT_INVALID") from exc
    if len(raw) > 1024 * 1024:
        last["error"] = "BAD_ENVELOPE"
        raise Refused("PATCH_OUTPUT_INVALID")
    try:
        envelope = json.loads(raw.decode("utf-8"))
        choice = envelope["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        last["error"] = "BAD_ENVELOPE"
        raise Refused("PATCH_OUTPUT_INVALID") from None
    if not isinstance(content, str):
        last["error"] = "BAD_ENVELOPE"
        raise Refused("PATCH_OUTPUT_INVALID")
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    return content, (finish if isinstance(finish, str) else None)


def chat_patcher(model_endpoint, model, timeout, *, max_tokens=None, disable_thinking=False):
    pilot.endpoint(model_endpoint)  # loopback /v1/chat/completions only
    endpoint = model_endpoint
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), pilot.NoRedirect())
    thinking_off = disable_thinking is True

    def patcher(files, task, workdir, *, analysis=None, allowlist=None):
        excerpts = {}
        file_chars = {}
        user = _user_prompt(files, task, allowlist, analysis, excerpts, file_chars=file_chars)
        if max_tokens is None:
            tokens = _estimate_max_tokens(task, sum(file_chars.values()))
        else:
            tokens = max(pilot.MIN_MAX_TOKENS, min(pilot.MAX_MAX_TOKENS, int(max_tokens)))
        last = {
            "format": None,
            "edits": 0,
            "noopEdits": 0,
            "maxTokens": tokens,
            "finishReason": None,
            "error": None,
            "outputChars": 0,
            "outputExcerpt": None,
            "retries": 0,
            "promptChars": len(_SYSTEM_PROMPT) + len(user),
            "fileChars": file_chars,
        }
        patcher.last = last
        patcher.last_output_excerpt = None
        noop_searches = []
        if excerpts:
            last["excerpts"] = excerpts

        def ask(messages):
            prompt = {"model": model, "temperature": 0, "max_tokens": tokens, "messages": messages}
            if thinking_off:
                prompt["chat_template_kwargs"] = {"enable_thinking": False}
                prompt["reasoning_format"] = "none"
                prompt["reasoning_effort"] = "none"
            return _chat_reply(opener, endpoint, prompt, timeout, last)

        def attempt(messages):
            """Return the parsed files dict, or the Refused instance that stopped this reply."""
            content, finish = ask(messages)
            last["finishReason"] = finish
            last["outputChars"] = len(content)
            last["outputExcerpt"] = None
            last["noopEdits"] = 0
            excerpt = pilot.sanitize_text(content, limit=2048)
            # Remembered on the patcher, not in `last`: a successful reply keeps outputExcerpt None
            # unless run() needs it to explain an empty diff.
            patcher.last_output_excerpt = excerpt
            try:
                files_out = _legacy_json_files(content)
                if files_out is not None:
                    merged = {path: _match_line_endings(files.get(path), value) for path, value in files_out.items()}
                    last["format"] = "json-files"
                    last["edits"] = len(files_out)
                    last["noopEdits"] = _noop_file_count(files, merged)
                    last["error"] = None
                    return merged
                last["format"] = "search-replace"
                edits = parse_edits(content, allowlist)
                misdirected = _misdirected(edits, allowlist)
                if misdirected:
                    # Same code as before so receipts stay valid; the reason drives the one retry.
                    raise _EditRefused("ALLOWLIST_VIOLATION", misdirected)
                out = apply_edits(files, edits, allowlist or [])
                noops = _noop_edit_count(files, edits, out)
                same = [
                    search
                    for _path, search, replace in edits
                    if _normalize_edit_text(search) == _normalize_edit_text(replace)
                ]
                # Edits that cancel out are no-ops too; quote their SEARCH text instead.
                noop_searches[:] = same or ([search for _path, search, _replace in edits] if noops else [])
            except Refused as exc:
                last["error"] = getattr(exc, "reason", None)
                last["outputExcerpt"] = excerpt
                return exc
            last["edits"] = len(edits)
            last["noopEdits"] = noops
            last["error"] = None
            return out

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        result = attempt(messages)
        waste = None
        if isinstance(result, _EditRefused) and result.reason in ("NO_EDITS", "EXAMPLE_COPY", "OFF_ALLOWLIST"):
            waste = result.reason
        elif not isinstance(result, Refused) and last["edits"] > 0 and last["noopEdits"] == last["edits"]:
            waste = "NOOP_EDITS"
        if waste and last["finishReason"] in ("length", "stop"):
            first_excerpt = patcher.last_output_excerpt
            last["retries"] = 1
            last["firstAttempt"] = {
                "error": waste,
                "finishReason": last["finishReason"],
                "outputChars": last["outputChars"],
                "outputExcerpt": first_excerpt,
            }
            reminder = _retry_note(waste, list(allowlist or files.keys()), noop_searches)
            result = attempt(messages + [{"role": "user", "content": reminder}])
        if isinstance(result, Refused):
            raise result
        return result

    return patcher


def execute_job(
    payload,
    *,
    model_endpoint,
    model,
    report_store=None,
    patcher=None,
    allow_local_source=False,
    tmp_root=None,
    jev_pre=None,
    jev_post=None,
    max_tokens=None,
    disable_thinking=False,
):
    if patcher is None:
        timeout = validate_spec(payload, allow_local_source=allow_local_source)["timeout_seconds"]
        patcher = chat_patcher(model_endpoint, model, timeout, max_tokens=max_tokens, disable_thinking=disable_thinking)
    result = run(
        payload,
        patcher,
        allow_local_source=allow_local_source,
        tmp_root=tmp_root,
        jev_pre=jev_pre,
        jev_post=jev_post,
    )
    analysis = {key: result[key] for key in ("jevPre", "jevPost")}
    run_id = str(uuid.uuid4())
    store = pilot.report_store_dir(report_store)
    diagnostics = {"git_stderr": result["git_stderr"]} if "git_stderr" in result else {}
    if isinstance(result.get("patch"), dict):
        diagnostics["patch"] = result["patch"]
    report = {
        "runId": run_id,
        "outcome": result["outcome"],
        "code": result["code"],
        "repo": result["repo"],
        "commit": result["commit"],
        "changedFiles": result["changedFiles"],
        "diffSha256": result["diffSha256"],
        "diff": result["diff"],
        **analysis,
        **diagnostics,
    }
    data = json.dumps(report, sort_keys=True).encode("utf-8") + b"\n"
    fd, tmp = tempfile.mkstemp(prefix="repo-patch-", suffix=".json", dir=str(store))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        final = store / (f"repo-patch-{run_id}.json")
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {
        "report": {
            "outcome": result["outcome"],
            "code": result["code"],
            "reportStored": True,
            "runId": run_id,
            "storedReportSha256": hashlib.sha256(data).hexdigest(),
            **analysis,
        },
        **analysis,
        "reportStored": True,
        "reportPath": str(final),
        **diagnostics,
        "artifact": {
            "name": "patch.diff",
            "mediaType": "text/x-diff",
            "encoding": "utf-8",
            "sha256": result["diffSha256"],
            "bytes": result["diffBytes"],
            "changedFiles": result["changedFiles"],
            "content": result["diff"],
        },
    }
