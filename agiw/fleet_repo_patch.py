"""Bounded repo-patch program for the fleet preview worker.

Clones one public GitHub repo at one commit into a fresh temp dir, asks a patcher (by default the
worker's loopback model) for new contents of allowlisted files only, and returns the staged unified
diff. It never pushes, never writes outside the temp clone and always removes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from . import fleet_local_pilot as pilot

PROGRAM = "repo-patch"


class Refused(RuntimeError):  # noqa: N818 - matches the sibling fleet modules
    pass


def _ref(code):
    raise Refused(code)


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
    return {
        "repo": repo,
        "commit": commit.lower(),
        "allowlist": allowlist,
        "task": task,
        "max_diff_bytes": max_diff_bytes,
        "max_changed_lines": max_changed_lines,
        "timeout_seconds": timeout_seconds,
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
        files[rel] = p.read_text(encoding="utf-8", errors="surrogateescape")
    return files


def _parse_diff_limits(diff_text, max_changed_lines):
    if max_changed_lines is None:
        return
    count = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            count += 1
    if count > max_changed_lines:
        _ref("DIFF_TOO_MANY_LINES")


def _blocked(spec, code, stderr=None):
    result = {
        "outcome": "BLOCKED",
        "code": code,
        "repo": spec["repo"],
        "commit": spec["commit"],
        "changedFiles": [],
        "diff": "",
        "diffSha256": hashlib.sha256(b"").hexdigest(),
        "diffBytes": 0,
    }
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    if stderr:
        stderr = re.sub(r"://[^\s/?#]*@", "://[redacted]@", stderr)
        stderr = pilot.sanitize_text(stderr, limit=2048)
        if stderr:
            result["git_stderr"] = stderr.encode("utf-8", "replace")[:2048].decode("utf-8", "ignore")
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
            target.write_text(content, encoding="utf-8")


def _staged_diff(spec, workdir, tmp):
    """Judge what git sees on disk, not what the patcher claims it changed."""
    t = spec["timeout_seconds"]
    _git(["add", "-A"], workdir, tmp, t)
    changed = _git_out(["diff", "--cached", "--name-only", "-z"], workdir, tmp, t)
    changed_files = [x for x in changed.decode("utf-8", "surrogateescape").split("\x00") if x]
    if any(path not in spec["allowlist"] for path in changed_files):
        _ref("ALLOWLIST_VIOLATION")
    diff = _git_out(["diff", "--cached", "--binary", "--no-color", "--no-ext-diff"], workdir, tmp, t)
    if len(diff) > spec["max_diff_bytes"]:
        _ref("DIFF_TOO_LARGE")
    _parse_diff_limits(diff.decode("utf-8", "surrogateescape"), spec["max_changed_lines"])
    return changed_files, diff


def run(spec, patcher, *, allow_local_source=False, tmp_root=None):
    """Clone, patch and diff inside a fresh temp dir that is always removed. Never pushes."""
    spec = validate_spec(spec, allow_local_source=allow_local_source)
    if tmp_root is not None:
        Path(tmp_root).mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=tmp_root)).resolve()
    try:
        (tmp / "home").mkdir()
        workdir = _clone(spec, tmp)
        files = _read_files(workdir, spec["allowlist"])
        try:
            out = patcher(files=dict(files), task=spec["task"], workdir=workdir)
        except Refused:
            raise
        except Exception:  # noqa: BLE001 - any patcher failure is a BLOCKED job, never a crash
            return _blocked(spec, "PATCH_FAILED")
        _apply(workdir, spec["allowlist"], out)
        changed_files, diff = _staged_diff(spec, workdir, tmp)
        return {
            "outcome": "COMPLETED" if diff else "FAILED",
            "code": None if diff else "NO_CHANGE",
            "repo": spec["repo"],
            "commit": spec["commit"],
            "changedFiles": changed_files,
            "diff": diff.decode("utf-8", "surrogateescape"),
            "diffSha256": hashlib.sha256(diff).hexdigest(),
            "diffBytes": len(diff),
        }
    except subprocess.CalledProcessError as exc:
        code = "COMMIT_NOT_FOUND" if "checkout" in exc.cmd else "GIT_FAILED"
        return _blocked(spec, code, exc.stderr)
    except subprocess.TimeoutExpired as exc:
        return _blocked(spec, "GIT_TIMEOUT", exc.stderr)
    except OSError:
        return _blocked(spec, "IO_FAILED")
    except Refused as exc:
        return _blocked(spec, str(exc))
    finally:
        _rmtree(tmp)


def _strip_fences(text):
    t = text.strip()
    if t.startswith("```"):
        t = "\n".join(t.splitlines()[1:])
        if t.rstrip().endswith("```"):
            t = "\n".join(t.splitlines()[:-1])
    return t.strip()


def chat_patcher(model_endpoint, model, timeout):
    pilot.endpoint(model_endpoint)  # loopback /v1/chat/completions only
    endpoint = model_endpoint
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), pilot.NoRedirect())

    def patcher(files, task, workdir):
        prompt = {
            "model": model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": 'Return ONLY JSON {"files":{"<path>":"<full new content>"}} for allowlisted paths.',
                },
                {"role": "user", "content": json.dumps({"files": files, "task": task}, sort_keys=True)},
            ],
        }
        body = json.dumps(prompt).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read(1024 * 1024 + 1)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            # Keep the refused code unchanged; chain the cause for debugging without putting
            # response or URL text into the exception message.
            raise Refused("PATCH_OUTPUT_INVALID") from exc
        if len(raw) > 1024 * 1024:
            raise Refused("PATCH_OUTPUT_INVALID")
        try:
            envelope = json.loads(raw.decode("utf-8"))
            content = envelope["choices"][0]["message"]["content"]
            data = json.loads(_strip_fences(content))
            files_out = data["files"]
            if not isinstance(files_out, dict):
                raise ValueError
            return files_out
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise Refused("PATCH_OUTPUT_INVALID") from None

    return patcher


def execute_job(
    payload, *, model_endpoint, model, report_store=None, patcher=None, allow_local_source=False, tmp_root=None
):
    if patcher is None:
        timeout = validate_spec(payload, allow_local_source=allow_local_source)["timeout_seconds"]
        patcher = chat_patcher(model_endpoint, model, timeout)
    result = run(payload, patcher, allow_local_source=allow_local_source, tmp_root=tmp_root)
    run_id = str(uuid.uuid4())
    store = pilot.report_store_dir(report_store)
    diagnostics = {"git_stderr": result["git_stderr"]} if "git_stderr" in result else {}
    report = {
        "runId": run_id,
        "outcome": result["outcome"],
        "code": result["code"],
        "repo": result["repo"],
        "commit": result["commit"],
        "changedFiles": result["changedFiles"],
        "diffSha256": result["diffSha256"],
        "diff": result["diff"],
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
        },
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
