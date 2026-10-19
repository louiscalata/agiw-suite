#!/bin/sh
# Stage a user-owned release; --enable explicitly enables/starts the user unit.
# Usage: sh linux/install.sh [--commit HEX] [--enable]
#        sh linux/install.sh --rollback HEX [--enable]
set -eu
exec /usr/bin/python3 -I -B - "$0" "$@" <<'PY_INSTALLER'
"""Transactional, local-only installer. Never executes an observer while staging."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

UNIT = "agiw-linux-observer.service"
MARKER = "# Managed by agiw-linux-observer installer.\n"
MODULES = ("__init__.py", "lanes.py", "linsys.py", "probes.py", "collect.py", "server.py", "idle_allowance.py")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
ACTIVATION_SECONDS = 15.0


def safe_path(path):
    """Do not follow links, including dangling links and intermediate components."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("paths must be absolute and must not contain '..'")
    # This restricted alphabet also prevents systemd specifier/env injection.
    if not re.fullmatch(r"[A-Za-z0-9_./ -]+", str(path)):
        raise ValueError("path contains unsupported characters")
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise ValueError(f"symlink path refused: {part}")
    return path


def private_path(path, home, directory=True, create=False):
    path = safe_path(path)
    if path != home and home not in path.parents:
        raise ValueError("install and XDG paths must be below HOME")
    for part in reversed((path, *path.parents)):
        if part != home and home not in part.parents:
            continue
        try:
            info = part.lstat()
        except FileNotFoundError:
            if create:
                part.mkdir(mode=0o700)
                info = part.lstat()
            else:
                continue
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError(f"path must be user-owned and not group/world writable: {part}")
        if (part != path or directory) and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"directory required: {part}")
        if part == path and not directory and not stat.S_ISREG(info.st_mode):
            raise ValueError(f"regular file required: {part}")
    return path


def read_regular(path, limit=2 * 1024 * 1024):
    safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"bounded regular file required: {path}")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"file too large: {path}")
        return data


def atomic_write(path, data, mode=0o600):
    fd, name = tempfile.mkstemp(prefix=".agiw-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(name, path)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def switch_link(base, release):
    # Private lock serializes this installer; the temporary link shares a filesystem.
    temporary = base / ".current-new"
    os.symlink(release, temporary)
    try:
        os.replace(temporary, base / "current")
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def verify_release(base, commit, home):
    if not COMMIT.fullmatch(commit):
        raise ValueError("release must be a full lowercase commit hash")
    target = private_path(base / commit, home)
    manifest_path = private_path(target / "install-manifest.json", home, directory=False)
    manifest = json.loads(read_regular(manifest_path))
    if manifest.get("commit") != commit or not isinstance(manifest.get("files"), dict):
        raise ValueError("invalid release manifest")
    required = {"agiw-linux-status", "agiw-linux-once", UNIT, *(f"agiw_linux/{name}" for name in MODULES)}
    if set(manifest["files"]) != required:
        raise ValueError("release manifest file list mismatch")
    for relative, digest in manifest["files"].items():
        path = private_path(target / relative, home, directory=False)
        if hashlib.sha256(read_regular(path)).hexdigest() != digest:
            raise ValueError(f"release hash mismatch: {relative}")
    # Refuse unexpected executable/import payloads in a retained release.
    actual = set()
    for path in target.rglob("*"):
        if path.is_symlink():
            raise ValueError("symlink in retained release")
        if path.is_dir():
            if path != target / "agiw_linux":
                raise ValueError("unexpected release directory")
        else:
            actual.add(str(path.relative_to(target)))
    if actual != required | {"install-manifest.json"}:
        raise ValueError("unexpected release files")
    return target


def systemctl(*args, check=True, timeout=20):
    result = subprocess.run(["/usr/bin/systemctl", "--user", *args], capture_output=True,
                            text=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"systemctl {' '.join(args)} failed ({result.returncode})")
    return result


def source_release(source, requested):
    # An export may be staged for inspection, but cannot be activated.
    try:
        head = subprocess.run(["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10, check=False)
    except FileNotFoundError:
        head = subprocess.CompletedProcess([], 127, "", "git is unavailable")
    if head.returncode == 0:
        commit = head.stdout.strip()
        if requested and requested != commit:
            raise ValueError("--commit does not match source HEAD")
        status = subprocess.run(["/usr/bin/git", "-C", str(source), "status", "--porcelain",
                                 "--untracked-files=all", "--", "."],
                                capture_output=True, text=True, timeout=10, check=False)
        if status.returncode or status.stdout.strip():
            raise ValueError("Linux source tree is dirty; use a committed release")
        provenance = "git-blobs-verified; signature-not-verified"
    else:
        commit = requested or ""
        provenance = "unverified-export; caller-supplied-commit"
    if not COMMIT.fullmatch(commit):
        raise ValueError("a full lowercase --commit hash or clean Git checkout is required")
    return commit, provenance


def verify_git_files(source, commit, files, *, signed=False):
    prefix = subprocess.run(["/usr/bin/git", "-C", str(source), "rev-parse", "--show-prefix"],
                            capture_output=True, text=True, timeout=10, check=False)
    if prefix.returncode:
        raise ValueError("activation requires a Git checkout and verified signed commit; exports are stage-only")
    if signed:
        signature = subprocess.run(["/usr/bin/git", "-C", str(source), "verify-commit", commit],
                                   capture_output=True, timeout=10, check=False)
        if signature.returncode:
            raise ValueError("selected commit signature verification failed")
    for relative, content in files.items():
        blob = subprocess.run(["/usr/bin/git", "-C", str(source), "show", f"{commit}:{prefix.stdout.strip()}{relative}"],
                              capture_output=True, timeout=10, check=False)
        if blob.returncode or blob.stdout != content:
            raise ValueError(f"source does not match selected commit blob: {relative}")


def verify_idle_observer_allowance(home, base, commit, candidate, config, state):
    """Execute only the allowance verifier already authenticated as a signed blob."""
    namespace = {"__name__": "trusted_idle_allowance"}
    try:
        raw = candidate["agiw_linux/idle_allowance.py"]
        exec(compile(raw, "signed-idle-allowance", "exec"), namespace)
        namespace["verify_pairing"](home, base, commit, candidate, config_home=config, state_home=state)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise ValueError("idle analyzer observer allowance not verified; activation blocked: "
                         + str(error)) from error


def prepare_idle_rollback(source, home, base, previous, config, state):
    """Authenticate the retained pairing before any observer promotion."""
    release = verify_release(base, previous, home)
    files = {name: read_regular(release / name) for name in
             ("agiw-linux-status", "agiw-linux-once", UNIT, *(f"agiw_linux/{name}" for name in MODULES))}
    verify_git_files(source, previous, files, signed=True)
    namespace = {"__name__": "trusted_idle_rollback"}
    exec(compile(files["agiw_linux/idle_allowance.py"], "signed-idle-rollback", "exec"), namespace)
    runtime, _ = namespace["layout"](home, base, previous, files, config_home=config, state_home=state)
    read = namespace["read_file"]
    baseline = {name: read(runtime / name, owner=os.getuid()) for name in namespace["PINS"]}
    expected = namespace["bundle"](home, base, previous, files, baseline, config_home=config, state_home=state)
    idle_unit = namespace["IDLE_UNIT"]
    for name, raw in expected.items():
        if name != idle_unit and read(runtime / name, owner=os.getuid()) != raw:
            raise ValueError("rollback idle analyzer pairing mismatch")
    return files, expected[idle_unit]


def check_unit_overrides(unit, *, loaded=False):
    dropins = unit.parent / (UNIT + ".d")
    if os.path.lexists(dropins) and (dropins.is_symlink() or not dropins.is_dir() or any(dropins.iterdir())):
        raise ValueError("unexpected systemd drop-ins; inspect before activation")
    # The manager includes global, runtime and prefix drop-ins, not just this directory.
    if systemctl("show", UNIT, "--property=DropInPaths", "--value").stdout.strip():
        raise ValueError("unexpected effective systemd drop-ins; inspect before activation")
    if loaded and systemctl("show", UNIT, "--property=FragmentPath", "--value").stdout.strip() != str(unit):
        raise ValueError("systemd loaded a different service unit")


def wait_for_heartbeat(state_dir, commit, activated_at):
    expires = time.monotonic() + ACTIVATION_SECONDS
    while time.monotonic() < expires:
        result = systemctl("is-active", UNIT, check=False, timeout=min(2, expires - time.monotonic()))
        try:
            path = state_dir / "linux-status.json"
            info = path.lstat()
            value = json.loads(read_regular(path, 65536))
            observed = value.get("observedUnix")
            fresh = type(observed) in (int, float) and activated_at < observed <= time.time()
            publisher = any(row.get("id") == "heartbeatUnit" and row.get("state") == "up"
                            for row in value.get("components", []) if isinstance(row, dict))
            if result.returncode == 0 and result.stdout.strip() == "active" and fresh and publisher \
                    and info.st_mtime > activated_at and info.st_uid == os.getuid() and not info.st_mode & 0o077 \
                    and value.get("schemaVersion") == 1 and value.get("platform") == "linux" \
                    and value.get("releaseCommit") == commit:
                return
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        time.sleep(.25)
    raise RuntimeError("activation deadline: no active unit with a fresh heartbeat from selected release")


def install(argv):
    if os.getuid() == 0 or os.geteuid() == 0:
        raise ValueError("refusing root; run as the observer user without sudo")
    if sys.platform != "linux":
        raise ValueError("the installer supports Linux only")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit")
    parser.add_argument("--rollback")
    parser.add_argument("--enable", action="store_true", help="enable and start the user unit")
    args = parser.parse_args(argv[2:])
    if args.commit and args.rollback:
        raise ValueError("--commit and --rollback are mutually exclusive")
    home = safe_path(os.environ.get("HOME", ""))
    private_path(home, home)
    # A private HOME can still be renamed by another user through an unsafe
    # ancestor. Root-owned sticky temporary roots are safe for fixture/ephemeral homes.
    for parent in home.parents:
        info = parent.stat()
        if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            raise ValueError("HOME has an unsafe writable or foreign-owned ancestor")
    data = private_path(Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share"), home)
    config = private_path(Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config"), home)
    state = private_path(Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state"), home)
    base = private_path(data / "agiw-linux-observer", home)
    unit_dir = private_path(config / "systemd/user", home)
    state_dir = private_path(state / "agiw", home)
    if state_dir.exists() and state_dir.stat().st_mode & 0o077:
        raise ValueError("observer state directory must be private (mode 0700)")
    unit = private_path(unit_dir / UNIT, home, directory=False)
    old_unit = read_regular(unit) if unit.exists() else None
    if old_unit is not None and not old_unit.startswith(MARKER.encode()):
        raise ValueError("refusing to replace an unmanaged service unit")
    # Validate existing link without following it or accepting an external target.
    current = base / "current"
    previous = None
    if os.path.lexists(current):
        if not current.is_symlink():
            raise ValueError("current must be an installer-managed symlink")
        previous = os.readlink(current)
        verify_release(base, previous, home)
    if os.path.lexists(base / ".current-new"):
        raise ValueError("unfinished current-link transaction; inspect before retrying")
    source = safe_path(Path(os.path.abspath(argv[1])).parent)
    if args.rollback:
        commit, provenance = args.rollback, "verified-retained-release"
        release = verify_release(base, commit, home)
        files = None
    else:
        commit, provenance = source_release(source, args.commit)
        files = {"agiw-linux-status": read_regular(source / "agiw-linux-status"),
                 "agiw-linux-once": read_regular(source / "agiw-linux-once"),
                 UNIT: read_regular(source / UNIT)}
        for name in MODULES:
            files[f"agiw_linux/{name}"] = read_regular(source / "agiw_linux" / name)
        if not provenance.startswith("unverified-export"):
            verify_git_files(source, commit, files)
        for name, content in files.items():
            if name not in (UNIT, "agiw-linux-once"):
                compile(content, name, "exec")  # Syntax check only; never import or execute.
        release = base / commit
    idle_rollback = None
    if args.enable:
        candidate = files if files is not None else {
            name: read_regular(release / name) for name in
            ("agiw-linux-status", "agiw-linux-once", UNIT, *(f"agiw_linux/{name}" for name in MODULES))}
        verify_git_files(source, commit, candidate, signed=True)
        verify_idle_observer_allowance(home, base, commit, candidate, config, state)
        if previous is not None:
            idle_rollback = prepare_idle_rollback(source, home, base, previous, config, state)
        provenance = "git-blobs-verified; signature-verified"
    enabled = active = False
    private_path(base, home, create=True)
    lock = base / ".install-lock"
    lock.mkdir(mode=0o700)  # Fail closed on concurrent installs, including stale locks.
    stage = None
    changed_link = changed_unit = activated = False
    try:
        # Snapshot checks above occur before taking the lock. A completed installer
        # between those reads and this acquisition must not permit a stale rollback.
        live_previous = os.readlink(current) if current.is_symlink() else None
        if live_previous != previous or (os.path.lexists(current) and not current.is_symlink()):
            raise ValueError("current changed during preparation; retry the install")
        private_path(unit, home, directory=False)
        live_unit = read_regular(unit) if unit.exists() else None
        if live_unit != old_unit:
            raise ValueError("unit changed during preparation; retry the install")
        if args.enable:
            check_unit_overrides(unit)
            result = systemctl("is-enabled", UNIT, check=False)
            if result.stdout.strip() not in {"enabled", "disabled", "not-found"}:
                raise RuntimeError("cannot establish previous unit enablement (or unit is masked)")
            enabled = result.stdout.strip() == "enabled"
            result = systemctl("is-active", UNIT, check=False)
            if result.stdout.strip() not in {"active", "inactive", "failed", "unknown"}:
                raise RuntimeError("cannot establish previous unit activity")
            active = result.stdout.strip() == "active"
        private_path(unit_dir, home, create=True)
        private_path(state_dir, home, create=True)
        if files is not None:
            hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
            if os.path.lexists(release):
                verify_release(base, commit, home)
                saved = json.loads(read_regular(release / "install-manifest.json"))
                if saved["files"] != hashes:
                    raise ValueError("commit directory already contains different content")
            else:
                stage = Path(tempfile.mkdtemp(prefix=".agiw-stage-", dir=base))
                (stage / "agiw_linux").mkdir(mode=0o700)
                for name, content in files.items():
                    atomic_write(stage / name, content, 0o700 if name in ("agiw-linux-status", "agiw-linux-once") else 0o600)
                atomic_write(stage / "install-manifest.json", json.dumps({
                    "commit": commit, "provenance": provenance, "files": hashes,
                }, sort_keys=True).encode() + b"\n")
                os.rename(stage, release)
                stage = None
        if args.enable:
            # Staging never changes the forced-command target or the active unit.
            template = read_regular(release / UNIT).decode()
            if not template.startswith(MARKER):
                raise ValueError("unrecognized service template")
            rendered = template.replace("%h/.local/share/agiw-linux-observer", str(base))
            rendered = rendered.replace("%h/.local/state", str(state))
            atomic_write(unit, rendered.encode())
            changed_unit = True
            switch_link(base, commit)
            changed_link = True
            activated = True
            systemctl("daemon-reload")
            check_unit_overrides(unit, loaded=True)
            verify_idle_observer_allowance(home, base, commit, candidate, config, state)
            activated_at = time.time()
            systemctl("enable", "--now", UNIT)
            if active:
                systemctl("restart", UNIT)
            wait_for_heartbeat(state_dir, commit, activated_at)
    except Exception as failure:
        rollback_errors = []
        # Stop the attempted release before restoring the previous service state.
        if activated:
            try:
                systemctl("stop", UNIT)
            except Exception as error:
                rollback_errors.append(str(error))
        for restore in ("link", "unit", "analyzer", "service", "pairing"):
            try:
                if restore == "link" and changed_link:
                    if previous is None:
                        current.unlink()
                    else:
                        switch_link(base, previous)
                if restore == "unit" and changed_unit:
                    if old_unit is None:
                        unit.unlink()
                    else:
                        atomic_write(unit, old_unit)
                if restore == "analyzer" and activated and idle_rollback is not None:
                    atomic_write(unit_dir / "agiw-idle-analysis.service", idle_rollback[1])
                if restore == "service" and activated:
                    if not enabled:
                        systemctl("disable", UNIT)
                    systemctl("daemon-reload")
                    if active:
                        if previous is None or old_unit is None:
                            raise RuntimeError("prior active release identity unavailable")
                        restored_at = time.time()
                        systemctl("start", UNIT)
                        wait_for_heartbeat(state_dir, previous, restored_at)
                if restore == "pairing" and activated and idle_rollback is not None:
                    verify_idle_observer_allowance(home, base, previous, idle_rollback[0], config, state)
            except Exception as error:
                rollback_errors.append(str(error))
        if rollback_errors:
            raise RuntimeError(f"install failed ({failure}); rollback incomplete: {'; '.join(rollback_errors)}") from failure
        raise RuntimeError(f"install failed and was rolled back: {failure}") from failure
    finally:
        if stage is not None:
            shutil.rmtree(stage)
        lock.rmdir()
    print(f"Selected {commit} ({provenance})")
    print("User unit active; fresh release heartbeat verified." if args.enable else
          "Files staged; current and user unit unchanged, not activated (use --enable).")


if __name__ == "__main__":
    try:
        install(sys.argv)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"agiw-linux-observer: {error}", file=sys.stderr)
        sys.exit(1)
PY_INSTALLER
