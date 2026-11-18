"""Render or install a macOS LaunchAgent for the fleet job worker (agiw.windows_worker).

launchd starts agents with a minimal PATH, so node in ~/.local/bin or Homebrew is not found
at run time. The plist therefore pins AGIW_NODE, AGIW_GIT and a minimal PATH built from the
binaries resolved at install time. Nothing here runs launchctl unless --load is given, and
no token value is ever written: the worker reads its token from --token-file.
"""

import argparse
import os
import plistlib
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import fleet_local_pilot as pilot
from .service import LAUNCHD_LABEL

# Same reverse-DNS namespace as the peer-link agent, e.g. <namespace>.agiw.fleet-worker.<worker-id>
LABEL_PREFIX = LAUNCHD_LABEL.rsplit(".", 1)[0] + ".fleet-worker."
BASE_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


class ServiceError(RuntimeError):
    pass


def resolve_tools(env=None, which=shutil.which):
    """Absolute node and git paths, resolved now. Fail closed if either is missing."""
    env = dict(os.environ) if env is None else env
    node = env.get("AGIW_NODE") or which("node")
    if not node:
        for folder in pilot.NODE_FALLBACK_DIRS:
            candidate = posixpath.join(os.path.expanduser(folder).replace(os.sep, "/"), "node")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                node = candidate
                break
    git = env.get("AGIW_GIT") or which("git")
    if not node:
        raise ServiceError("NODE_NOT_FOUND")
    if not git:
        raise ServiceError("GIT_NOT_FOUND")
    return mac_path(node), mac_path(git)


def mac_path(tool):
    """The plist is for macOS launchd: paths stay POSIX on any host, and must already be absolute."""
    if not posixpath.isabs(tool):
        raise ServiceError("TOOL_PATH_NOT_ABSOLUTE")
    return posixpath.normpath(tool)


def minimal_path(*tools):
    dirs = []
    for tool in tools:
        folder = posixpath.dirname(tool)
        if folder and folder not in dirs:
            dirs.append(folder)
    return ":".join(dirs + [d for d in BASE_PATH if d not in dirs])


def worker_id_of(worker_args):
    args = list(worker_args)
    if "--token" in args or any(a.startswith("--token=") for a in args):
        raise ServiceError("TOKEN_VALUE_REFUSED_USE_TOKEN_FILE")
    if "--token-file" not in args and not any(a.startswith("--token-file=") for a in args):
        raise ServiceError("TOKEN_FILE_REQUIRED")
    for i, a in enumerate(args):
        if a == "--worker-id" and i + 1 < len(args):
            value = args[i + 1]
            break
        if a.startswith("--worker-id="):
            value = a.split("=", 1)[1]
            break
    else:
        raise ServiceError("WORKER_ID_REQUIRED")
    if value not in pilot.known_workers():
        raise ServiceError("UNKNOWN_WORKER_ID")
    return value


def render_plist(worker_args, *, home=None, python=None, env=None, which=shutil.which, repo_root=None):
    worker = worker_id_of(worker_args)
    node, git = resolve_tools(env, which)
    home = Path.home() if home is None else Path(home)
    log = posixpath.join(home.as_posix(), "Library", "Logs", "agiw", f"fleet-worker-{worker}.log")
    return worker, plistlib.dumps(
        {
            "Label": LABEL_PREFIX + worker,
            "ProgramArguments": [python or sys.executable, "-B", "-m", "agiw.windows_worker", *worker_args],
            "WorkingDirectory": Path(repo_root or pilot.ROOT).as_posix(),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 60,
            "ProcessType": "Background",
            "EnvironmentVariables": {
                "AGIW_NODE": node,
                "AGIW_GIT": git,
                "PATH": minimal_path(node, git),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
    )


def plist_path(worker, home=None):
    home = Path.home() if home is None else Path(home)
    if not re.fullmatch(r"[a-z0-9-]+", worker):
        raise ServiceError("UNKNOWN_WORKER_ID")
    return home / "Library" / "LaunchAgents" / (LABEL_PREFIX + worker + ".plist")


def install(worker_args, *, home=None, load=False, runner=subprocess.run, uid=None, **kw):
    """Write the LaunchAgent plist. launchd is macOS-only, so hosts without POSIX user ids refuse."""
    if uid is None:
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            raise ServiceError("LAUNCHD_REQUIRES_MACOS")
        uid = getuid()
    worker, data = render_plist(worker_args, home=home, **kw)
    target = plist_path(worker, home)
    target.parent.mkdir(parents=True, exist_ok=True)
    (Path(home or Path.home()) / "Library" / "Logs" / "agiw").mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".plist.tmp")
    tmp.write_bytes(data)
    os.chmod(str(tmp), 0o644)
    os.replace(str(tmp), str(target))
    command = ["launchctl", "bootstrap", f"gui/{uid}", str(target)]
    if load:
        runner(command, check=True)
    return target, command


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["render", "install"])
    p.add_argument("--load", action="store_true", help="also run launchctl bootstrap (otherwise only printed)")
    p.add_argument("worker_args", nargs=argparse.REMAINDER, help="-- then the agiw.windows_worker arguments")
    a = p.parse_args(argv)
    worker_args = a.worker_args[1:] if a.worker_args[:1] == ["--"] else a.worker_args
    try:
        if a.action == "render":
            _, data = render_plist(worker_args)
            sys.stdout.write(data.decode("utf-8"))
            return 0
        target, command = install(worker_args, load=a.load)
        pilot.emit("wrote " + str(target))
        pilot.emit(("loaded: " if a.load else "to load: ") + " ".join(command))
        return 0
    except (ServiceError, pilot.Refused, OSError, subprocess.CalledProcessError) as exc:
        pilot.emit("agiw.worker_service: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
