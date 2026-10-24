"""`agiw doctor`: read-only checks. It changes nothing (no firewall, permission or service edits), and what
it prints shows the home folder as ~ and never a key, token, share path or folder outside the home."""
from __future__ import annotations

import errno
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import peer_link as pl

from agiw.redact import redact, show_path

Row = Tuple[str, str]  # (ok | warn | fail | info, text)
LEVELS = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]", "info": "[info]"}
_IN_USE = {errno.EADDRINUSE, 10048, 10013}  # POSIX; WSAEADDRINUSE; WSAEACCES (an exclusive Windows bind)


def probe_port(port: int, proto: str, *, address: str = "0.0.0.0") -> str:
    """'free', 'in use' or 'unknown', by binding the port as the peer link would and closing at once."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM)
    try:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((address, port))
        return "free"
    except OSError as exc:
        return "in use" if exc.errno in _IN_USE or getattr(exc, "winerror", None) in _IN_USE else "unknown"
    finally:
        sock.close()


def firewall_hints(kind: str, tcp: int, udp: int) -> List[Row]:
    if kind == "mac":
        return [("info", f"firewall (macOS): if the firewall is on, allow incoming connections for the Python that "
                         f"runs agiw (System Settings > Network > Firewall > Options). Paired machines reach this "
                         f"one on tcp/{tcp}; discovery uses udp/{udp}.")]
    if kind == "linux":
        return [("info", f"firewall (Linux): if ufw is active, allow only your LAN, e.g. "
                         f"`sudo ufw allow from <your LAN>/24 to any port {tcp} proto tcp` and the same for "
                         f"`port {udp} proto udp`. Run these yourself; agiw changes no firewall settings.")]
    return [("info", f"firewall (Windows): add inbound rules yourself in an administrator PowerShell, Private "
                     f"profile only: New-NetFirewallRule -DisplayName 'AGIW peer link TCP' -Direction Inbound "
                     f"-Protocol TCP -LocalPort {tcp} -Profile Private -Action Allow, and the same with "
                     f"-Protocol UDP -LocalPort {udp}. agiw changes no firewall settings.")]


def _secret_files(state_dir: Path) -> List[Path]:
    names = [state_dir / "control-token", state_dir / "identity.json"]
    trusted = state_dir / "trusted"
    try:
        names += [trusted / n for n in os.listdir(trusted) if n.casefold().endswith(".json")]
    except OSError:
        pass
    return [p for p in names if p.is_file() and not p.is_symlink()]


def paired_count(state_dir: Path) -> int:
    try:
        return sum(1 for n in os.listdir(state_dir / "trusted") if n.endswith(".json") and pl.UUID_RE.match(n[:-5]))
    except OSError:
        return 0


def check_state_dir(state_dir: Path, home: Optional[str] = None) -> List[Row]:
    shown = show_path(state_dir, home)
    try:
        info = os.lstat(state_dir)
    except FileNotFoundError:
        return [("info", f"state folder {shown}: not created yet (`agiw serve` creates it, private to you)")]
    except OSError as exc:
        return [("fail", f"state folder {shown}: cannot be read ({type(exc).__name__})")]
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return [("fail", f"state folder {shown}: not a plain folder (a link or a file); the peer link refuses it")]
    rows: List[Row] = []
    if os.name == "nt":
        try:
            pl._win_check_private(str(state_dir), state_dir.name)
            rows.append(("ok", f"state folder {shown}: owned by you and closed to other accounts"))
        except OSError as exc:
            rows.append(("warn", f"state folder {shown}: not private ({type(exc).__name__}). The peer link tightens "
                                 f"it at start and retires any secret others could read; pair again if asked."))
    elif info.st_uid != os.getuid():
        rows.append(("fail", f"state folder {shown}: belongs to another user; the peer link refuses it"))
    elif info.st_mode & 0o077:
        rows.append(("warn", f"state folder {shown}: open to other users (mode {stat.S_IMODE(info.st_mode):o}); "
                             f"the peer link resets it to 700 at start and retires exposed secrets"))
    else:
        acl = None
        if sys.platform == "darwin":
            try:
                pl._only_me(state_dir, True)  # read-only: lists the folder's extended ACL
            except (OSError, subprocess.SubprocessError) as exc:
                acl = type(exc).__name__
        if acl:
            rows.append(("warn", f"state folder {shown}: has an extended ACL ({acl}); the peer link removes it at start"))
        else:
            rows.append(("ok", f"state folder {shown}: owned by you, mode 700"))
    loose = []
    for path in _secret_files(state_dir):
        if os.name == "nt":
            try:
                pl._win_check_private(str(path), path.name)
            except OSError:
                loose.append(path)
        elif os.lstat(path).st_mode & 0o077 or os.lstat(path).st_uid != os.getuid():
            loose.append(path)
        elif sys.platform == "darwin":
            try:
                pl._only_me(path, False)  # metadata/ACL only; never read or unseal the secret
            except (OSError, subprocess.SubprocessError):
                loose.append(path)
    if loose:
        rows.append(("warn", f"{len(loose)} key file(s) could have been read by others; the peer link replaces "
                             f"the control token and sets such pairings aside (pair those machines again)"))
    count = paired_count(state_dir)
    rows.append(("info", f"{count} paired machine(s) on record"))
    return rows


def check_ports(tcp: int, udp: int, probe: Callable[[int, str], str]) -> List[Row]:
    """Observe port occupancy only; a bind probe cannot establish the listener's identity."""
    rows: List[Row] = []
    for port, proto in ((tcp, "tcp"), (udp, "udp")):
        state = probe(port, proto)
        if proto == "tcp":
            if state == "in use":
                rows.append(("info", "peer link: port in use (probably the peer link; run `agiw status` to check)"))
            elif state == "free":
                rows.append(("info", "peer link: not running"))
            else:
                rows.append(("info", "peer link: could not be checked; run `agiw status` to check"))
        if state == "free":
            rows.append(("ok", f"{proto}/{port}: free for the peer link"))
        elif state == "in use":
            rows.append(("info", f"{proto}/{port}: in use"))
        else:
            rows.append(("info", f"{proto}/{port}: could not be checked"))
    return rows


def run(state_dir: Path, tcp: int, udp: int, *, kind: str,
        probe: Callable[[int, str], str] = probe_port, service_installed: Optional[bool] = None,
        write: Callable[[str], None] = print, home: Optional[str] = None,
        version: Tuple[int, ...] = tuple(sys.version_info[:3])) -> int:
    """Inspect local metadata and port occupancy without contacting the peer link or reading secrets."""
    rows: List[Row] = []
    shown_version = ".".join(str(v) for v in version[:3])
    rows.append(("ok" if tuple(version[:2]) >= (3, 9) else "fail", f"Python {shown_version} (3.9 or later needed)"))
    rows.append(("info", f"this machine: {kind} (agiw-peer/2, tcp/{tcp} status, udp/{udp} discovery)"))
    rows += check_state_dir(state_dir, home)
    rows += check_ports(tcp, udp, probe)
    rows += firewall_hints(kind, tcp, udp)
    if service_installed is not None:
        rows.append(("info", "login service: " + ("installed" if service_installed else
                                                  "not installed (`agiw service install` starts agiw at login)")))
    for level, text in rows:
        write(redact(f"{LEVELS[level]} {text}", home))
    return 1 if any(level == "fail" for level, _ in rows) else 0
