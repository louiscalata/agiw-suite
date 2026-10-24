"""Run `agiw serve` at login as a user-level service: a launchd LaunchAgent (macOS), a systemd --user unit
(Linux) or a Task Scheduler logon task (Windows). Nothing here asks for admin, sudo or UAC.

Definitions are rendered as text first, written under the user's own folders, and every system command goes
through an injectable runner, so tests render into a temporary folder and never start a real service.
"""
from __future__ import annotations

import csv
import io
import locale
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence
from xml.sax.saxutils import escape

LAUNCHD_LABEL = "io.github.louiscalata.agiw.peer-link"
SYSTEMD_UNIT = "agiw-peer-link.service"
WINDOWS_TASK = "\\AGIW\\PeerLink"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]


def real_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess":
    """The only place a service command is executed."""
    result = subprocess.run(list(argv), capture_output=True, timeout=60)
    encoding = locale.getpreferredencoding(False)
    return subprocess.CompletedProcess(result.args, result.returncode,
                                       result.stdout.decode(encoding, errors="replace"),
                                       result.stderr.decode(encoding, errors="replace"))


class ServiceError(RuntimeError):
    pass


def _systemd_quote(arg: str) -> str:
    """One ExecStart word: double-quoted, with systemd's specifier (%) and escape (\\) rules applied."""
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def _windows_quote(arg: str) -> str:
    """One argument for a Windows command line (CommandLineToArgvW rules)."""
    if arg and not re.search(r'[\s"]', arg):
        return arg
    out, backslashes = '"', 0
    for ch in arg:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            out += "\\" * (2 * backslashes + 1) + '"'
        else:
            out += "\\" * backslashes + ch
        backslashes = 0
    return out + "\\" * (2 * backslashes) + '"'


class ServiceManager:
    """kind is mac, linux or windows. home, python, runner and the environment are injectable."""

    def __init__(self, kind: str, serve_args: Sequence[str] = (), serve_options: Sequence[str] = (), *,
                 home: Optional[Path] = None,
                 python: Optional[str] = None, runner: Optional[Runner] = None, env: Optional[dict] = None,
                 uid: Optional[int] = None, system_dir: Optional[str] = None):
        if kind not in ("mac", "linux", "windows"):
            raise ValueError("kind must be mac, linux or windows")
        self.kind = kind
        self.serve_args = list(serve_args)        # global options, before `serve`
        self.serve_options = list(serve_options)  # `serve` options
        self.home = Path.home() if home is None else Path(home)
        self.env = dict(os.environ) if env is None else dict(env)
        self.runner = runner or real_runner
        self.python = python or self._default_python()
        self._uid = uid
        self._system_dir = system_dir
        self._windows_task = None

    # ---------- what the service runs ----------

    def _default_python(self) -> str:
        exe = Path(sys.executable)
        if self.kind == "windows":
            # A venv may omit pythonw even though its base installation provides it.
            for quiet in (exe.with_name("pythonw.exe"), Path(sys.base_exec_prefix) / "pythonw.exe"):
                if quiet.is_file():
                    return str(quiet)
        return str(exe)

    def command(self) -> List[str]:
        service = ["--service"] if self.kind in ("mac", "windows") else []
        return [self.python, "-m", "agiw", *self.serve_args, "serve", *service, *self.serve_options]

    # ---------- where the definition lives ----------

    def _local_appdata(self) -> Path:
        value = self.env.get("LOCALAPPDATA")
        return Path(value) if value and Path(value).is_absolute() else self.home / "AppData" / "Local"

    def _config_home(self) -> Path:
        value = self.env.get("XDG_CONFIG_HOME")
        return Path(value) if value and Path(value).is_absolute() else self.home / ".config"

    def definition_path(self) -> Path:
        if self.kind == "mac":
            return self.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        if self.kind == "linux":
            return self._config_home() / "systemd" / "user" / SYSTEMD_UNIT
        return self._local_appdata() / "AGIW" / "service" / "agiw-peer-link.xml"

    def log_path(self) -> Optional[Path]:
        return self.home / "Library" / "Logs" / "agiw" / "peer-link.log" if self.kind == "mac" else None

    # ---------- rendering ----------

    def render(self) -> bytes:
        return {"mac": self._render_launchd, "linux": self._render_systemd, "windows": self._render_task}[self.kind]()

    def _render_launchd(self) -> bytes:
        log = str(self.log_path())
        return plistlib.dumps({
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": self.command(),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},  # restarted after a crash, not after a clean stop
            "ThrottleInterval": 60,
            "ProcessType": "Background",
            "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
            "StandardOutPath": log,
            "StandardErrorPath": log,
        })

    def _render_systemd(self) -> bytes:
        lines = [
            "[Unit]",
            "Description=AGIW peer link (agiw-peer/2): LAN discovery, PIN pairing and signed read-only status",
            "After=network-online.target",
            "StartLimitIntervalSec=300",
            "StartLimitBurst=5",
            "",
            "[Service]",
            "Type=simple",
            "ExecStart=" + " ".join(_systemd_quote(a) for a in self.command()),
            "Environment=PYTHONDONTWRITEBYTECODE=1",
            "Restart=on-failure",
            "RestartSec=30",
            "RestartPreventExitStatus=75",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
        return "\n".join(lines).encode("utf-8")

    def windows_user(self) -> str:
        user = self.env.get("USERNAME") or "user"
        domain = self.env.get("USERDOMAIN")
        return f"{domain}\\{user}" if domain else user

    def _render_task(self) -> bytes:
        command, *args = self.command()
        user = escape(self.windows_user())
        arguments = escape(" ".join(_windows_quote(a) for a in args))
        xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>AGIW peer link (agiw-peer/2): LAN discovery, PIN pairing and signed read-only status</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""
        return xml.encode("utf-16")  # schtasks /XML expects UTF-16 with a byte-order mark

    # ---------- system commands ----------

    def _uid_value(self) -> int:
        return self._uid if self._uid is not None else getattr(os, "getuid", lambda: 0)()

    def _launchctl(self) -> str:
        return "/bin/launchctl"

    def _systemctl(self) -> str:
        for candidate in ("/usr/bin/systemctl", "/bin/systemctl"):
            if os.path.exists(candidate):
                return candidate
        return "systemctl"

    def _schtasks(self) -> str:
        if self._system_dir is None:
            if os.name == "nt":
                import peer_link  # System32 as Windows reports it, never found through PATH
                self._system_dir = peer_link._win_system_dir()
            else:
                self._system_dir = "C:\\Windows\\System32"
        return self._system_dir.rstrip("\\") + "\\schtasks.exe"

    def windows_task(self) -> str:
        """The scheduler namespace is machine-wide; scope our task to the account SID."""
        if self._windows_task is None:
            whoami = self._schtasks().rsplit("\\", 1)[0] + "\\whoami.exe"
            try:
                result = self._run([whoami, "/user", "/fo", "csv", "/nh"], check=False)
                sid = next((field for row in csv.reader(io.StringIO(result.stdout or ""), strict=True)
                            for field in row if re.fullmatch(r"S-1-[0-9]+(-[0-9]+)+", field)), None)
            except (OSError, subprocess.SubprocessError, csv.Error) as exc:
                raise ServiceError("cannot read this account's Windows SID; no task was changed") from exc
            if result.returncode != 0 or sid is None:
                raise ServiceError("cannot read this account's Windows SID; no task was changed")
            self._windows_task = WINDOWS_TASK + "-" + sid
        return self._windows_task

    def _run(self, argv: Sequence[str], check: bool = True) -> "subprocess.CompletedProcess":
        result = self.runner(list(argv))
        if check and result.returncode != 0:
            tool = re.split(r"[\\/]", argv[0])[-1]
            reason = (result.stderr or result.stdout or "").strip()
            raise ServiceError(f"{tool} {argv[1]} failed (exit {result.returncode})" +
                               (f": {reason}" if reason else ""))
        return result

    def _write_definition(self) -> Path:
        path = self.definition_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(self.render())
        if os.name != "nt":
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        return path

    def _stop_and_query(self, stop: Sequence[str], query: Sequence[str], guidance: str):
        try:
            result = self._run(stop, check=False)
        except (OSError, subprocess.SubprocessError):
            result = None  # a timeout does not establish whether the stop took effect
        try:
            return result, self._run(query, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ServiceError(f"service definition was kept; {guidance}; state query failed: {exc}") from exc

    def _windows_state_command(self) -> List[str]:
        task = self.windows_task()
        prefix = "\\AGIW\\"
        name = task[len(prefix):] if task.startswith(prefix) else ""
        if not re.fullmatch(r"[A-Za-z0-9-]+", name):
            raise ServiceError("invalid Windows task name; no task was changed")
        # State is the ScheduledTasks enum, independent of the display language.
        # Reserve exit 3 for a ScheduledTasks query's ObjectNotFound error.
        # Missing cmdlets/modules can also use ObjectNotFound, so the query's
        # error ID is required before a task can be treated as safe to forget.
        script = (
            "try { (Get-ScheduledTask -TaskPath '\\AGIW\\' -TaskName '" + name + "' -ErrorAction Stop).State; exit 0 } "
            "catch { if ($_.CategoryInfo.Category -eq [System.Management.Automation.ErrorCategory]::ObjectNotFound "
            "-and $_.FullyQualifiedErrorId -like 'CmdletizationQuery_NotFound*,Get-ScheduledTask') "
            "{ exit 3 }; [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
        )
        powershell = self._schtasks().rsplit("\\", 1)[0] + "\\WindowsPowerShell\\v1.0\\powershell.exe"
        return [powershell, "-NoProfile", "-NonInteractive", "-Command", script]

    def _windows_state(self, command: Sequence[str]) -> str:
        try:
            result = self._run(command, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ServiceError(f"scheduled task state query failed: {exc}") from exc
        state = (result.stdout or "").strip()
        if result.returncode == 0 and state in ("Unknown", "Disabled", "Queued", "Ready", "Running"):
            return state
        if result.returncode == 3 and not state:
            return "missing"
        reason = (result.stderr or result.stdout or "no State returned").strip()
        raise ServiceError(f"scheduled task state query failed (exit {result.returncode}): {reason}")

    def _confirm_windows_running(self, task: str):
        command = self._windows_state_command()
        # /Run only confirms that the request was accepted. Allow five seconds
        # for the scheduler to move from Ready/Queued to Running. The attempt
        # cap also bounds retries when an injected sleep does not advance time.
        deadline = time.monotonic() + 5.0
        for attempt in range(6):
            try:
                state = self._windows_state(command)
                if state == "Running":
                    return
                reason = f"task State is {state}"
            except ServiceError as exc:
                reason = str(exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if attempt < 5:
                time.sleep(min(1.0, remaining))
        raise ServiceError(f"task {task} is installed but not confirmed running: {reason}")

    def _bootstrap_with_retry(self, bootstrap: Sequence[str], stopped):
        if self._run(bootstrap, check=False).returncode != 0:
            if stopped.returncode == 0:
                time.sleep(2.0)  # bootout finishes asynchronously
            self._run(bootstrap)

    def install(self) -> List[str]:
        """Write the definition and start it now; returns what was done, one line per step."""
        steps = []
        if self.kind == "mac":
            previous_path = self.definition_path()
            previous = previous_path.read_bytes() if previous_path.exists() else None
            self.log_path().parent.mkdir(parents=True, exist_ok=True)
            domain = f"gui/{self._uid_value()}"
            bootstrap = [self._launchctl(), "bootstrap", domain, str(previous_path)]
            path = self._write_definition()
            try:
                steps.append("wrote " + str(path))
                stopped = self._run([self._launchctl(), "bootout", f"{domain}/{LAUNCHD_LABEL}"], check=False)
                self._bootstrap_with_retry(bootstrap, stopped)
            except (ServiceError, OSError, subprocess.SubprocessError) as exc:
                if previous is None:
                    raise ServiceError(f"LaunchAgent installed but did not start: {exc}") from exc
                restored = False
                try:
                    path.write_bytes(previous)
                    restored = True
                    stopped = self._run([self._launchctl(), "bootout", f"{domain}/{LAUNCHD_LABEL}"], check=False)
                    self._bootstrap_with_retry(bootstrap, stopped)
                    loaded = self._run([self._launchctl(), "print", f"{domain}/{LAUNCHD_LABEL}"], check=False)
                    if loaded.returncode != 0:
                        raise ServiceError("the restored LaunchAgent is not loaded")
                except (ServiceError, OSError, subprocess.SubprocessError) as rollback:
                    state = ("previous definition restored; loading is unconfirmed" if restored else
                             "definition restoration incomplete; current definition could not be verified")
                    raise ServiceError(f"LaunchAgent replacement failed: {exc}; rollback failed: {rollback}; {state}") from exc
                raise ServiceError(f"LaunchAgent replacement failed: {exc}; rolled back to the previous loaded definition") from exc
            steps.append(f"loaded {LAUNCHD_LABEL} for this user (launchctl bootstrap {domain})")
        elif self.kind == "linux":
            path = self._write_definition()
            steps.append("wrote " + str(path))
            self._run([self._systemctl(), "--user", "daemon-reload"])
            self._run([self._systemctl(), "--user", "enable", SYSTEMD_UNIT])
            self._run([self._systemctl(), "--user", "restart", SYSTEMD_UNIT])
            try:
                active = self._run([self._systemctl(), "--user", "is-active", SYSTEMD_UNIT], check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ServiceError(f"{SYSTEMD_UNIT} is installed but not running; "
                                   f"active state could not be confirmed: {exc}") from exc
            if active.returncode != 0 or (active.stdout or "").strip() != "active":
                raise ServiceError(f"{SYSTEMD_UNIT} is installed but not running; check `systemctl --user status {SYSTEMD_UNIT}`")
            steps.append(f"enabled and started {SYSTEMD_UNIT} (systemctl --user)")
        else:
            task = self.windows_task()  # establish the account identity before writing anything
            path = self._write_definition()
            steps.append("wrote " + str(path))
            self._run([self._schtasks(), "/Create", "/TN", task, "/XML", str(path), "/F"])
            try:
                self._run([self._schtasks(), "/Run", "/TN", task])
            except (ServiceError, OSError, subprocess.SubprocessError) as exc:
                raise ServiceError(f"task {task} is installed but did not start: {exc}") from exc
            self._confirm_windows_running(task)
            steps.append(f"registered the logon task {task} for {self.windows_user()}; confirmed running")
        return steps

    def uninstall(self) -> List[str]:
        steps = []
        path = self.definition_path()
        if self.kind == "mac":
            target = f"gui/{self._uid_value()}/{LAUNCHD_LABEL}"
            guidance = f"stop it with `launchctl bootout {target}` and retry"
            result, query = self._stop_and_query([self._launchctl(), "bootout", target],
                                                  [self._launchctl(), "print", target], guidance)
            stopped = query.returncode != 0 and (query.returncode == 113 or bool(re.search(
                r"Could not find service", (query.stderr or "") + (query.stdout or ""), re.I)))
            if not stopped:
                raise ServiceError(f"service definition was kept; {guidance}")
            if result is not None and result.returncode == 0:
                steps.append(f"stopped {LAUNCHD_LABEL}")
        elif self.kind == "linux":
            guidance = f"stop it with `systemctl --user stop {SYSTEMD_UNIT}` and retry"
            _, query = self._stop_and_query([self._systemctl(), "--user", "disable", "--now", SYSTEMD_UNIT],
                                            [self._systemctl(), "--user", "is-active", SYSTEMD_UNIT], guidance)
            state = (query.stdout or "").strip()
            if (query.returncode, state) not in ((3, "inactive"), (3, "failed"), (3, "unknown"), (4, "unknown")):
                raise ServiceError(f"service definition was kept; {guidance}")
            if path.exists() or state != "unknown":
                steps.append(f"confirmed {SYSTEMD_UNIT} is not running")
        else:
            task = self.windows_task()
            query_command = self._windows_state_command()
            guidance = f"stop task {task} with `schtasks /End /TN {task}` and retry"
            try:
                state = self._windows_state(query_command)
                if state != "missing":
                    try:
                        self._run([self._schtasks(), "/End", "/TN", task], check=False)
                    except (OSError, subprocess.SubprocessError):
                        pass  # the subsequent state query determines whether stopping succeeded
                    state = self._windows_state(query_command)
                    if state not in ("missing", "Ready", "Disabled"):
                        raise ServiceError(f"task State is {state}")
                    if state != "missing":
                        self._run([self._schtasks(), "/Delete", "/TN", task, "/F"])
                    steps.append(f"removed the logon task {task}")
            except (ServiceError, OSError, subprocess.SubprocessError) as exc:
                raise ServiceError(f"service definition was kept; {guidance}; {exc}") from exc
        try:
            path.unlink()
            steps.append("removed " + str(path))
            if self.kind == "linux":
                self._run([self._systemctl(), "--user", "daemon-reload"], check=False)
        except FileNotFoundError:
            pass
        if not steps:
            steps.append("no agiw service was installed")
        return steps

    def status(self) -> dict:
        """installed: the definition file exists; running: the service manager says so (None = unknown)."""
        path = self.definition_path()
        out = {"kind": self.kind, "definition": str(path), "installed": path.exists(), "loaded": None, "running": None}
        if self.kind == "mac":
            result = self._run([self._launchctl(), "print", f"gui/{self._uid_value()}/{LAUNCHD_LABEL}"], check=False)
            out["loaded"] = result.returncode == 0
            out["running"] = bool(out["loaded"] and re.search(r"^\s*state = running\s*$", result.stdout or "", re.M))
        elif self.kind == "linux":
            enabled = self._run([self._systemctl(), "--user", "is-enabled", SYSTEMD_UNIT], check=False)
            active = self._run([self._systemctl(), "--user", "is-active", SYSTEMD_UNIT], check=False)
            out["loaded"] = (enabled.stdout or "").strip() == "enabled"
            out["running"] = (active.stdout or "").strip() == "active"
        else:
            try:
                state = self._windows_state(self._windows_state_command())
            except ServiceError:
                return out  # preserve unknown instead of reporting a query failure as stopped
            out["loaded"] = state != "missing"
            out["running"] = None if state == "Unknown" else state == "Running"
        return out
