"""agiw: connect your Mac, Linux and Windows machines on the LAN.

Every command except `serve`, `doctor`, `service` and `uninstall` talks to this user's own peer link on
127.0.0.1, which must first prove it holds the control token in the state folder (peer_link.py). Status is
signed but not encrypted, and v0.1 dispatches no jobs: it only discovers, pairs and reads status.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import peer_link as pl

from agiw import __version__
from agiw import doctor as doctor_mod
from agiw.redact import redact, show_path
from agiw.service import ServiceError, ServiceManager
from agiw.state_lock import StateLock, StateLockBusy

EXIT_FAILED, EXIT_USAGE, EXIT_NOT_RUNNING = 1, 2, 3
EXIT_TEMPFAIL = 75
NOT_RUNNING = ("the peer link is not running on this machine; start it with `agiw serve` "
               "(or `agiw service install` to start it at login)")
WINDOWS_SOURCE = ("on Windows, pass --status-file with the Windows observer's status file "
                  "(<observer folder>\\state\\peer-link-status.json)")


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_FAILED):
        super().__init__(message)
        self.code = code


def control(state_dir: Path, port: int, method: str, path: str, body: Optional[dict] = None) -> dict:
    """One authenticated exchange with this user's own peer link."""
    try:
        status, doc = pl.control_request(state_dir, port, method, path, body)
    except PermissionError as exc:
        raise CliError(str(exc))
    except (ConnectionError, TimeoutError, OSError):
        raise CliError(NOT_RUNNING, EXIT_NOT_RUNNING)
    if status != 200:
        message = doc.get("error") if isinstance(doc, dict) else None
        raise CliError(str(message or f"the peer link answered {status}"))
    return doc


# ---------- status ----------

def machine_rows(snapshot: dict) -> List[dict]:
    """up / down / unknown for each paired machine, from signed replies only.

    up: a signed status reply verified within the last 15 s. down: heard on the LAN earlier but silent for
    over 60 s, with no signed reply. unknown: everything else (never heard since this peer link started,
    or heard but no fresh signed reply), with the reason."""
    rows = []
    for row in snapshot.get("peers", []):
        if not row.get("paired"):
            continue
        status = row.get("status") if isinstance(row.get("status"), dict) else None
        if row.get("state") == "connected" and status is not None:
            state, reason = "up", "signed status verified"
        elif row.get("state") == "offline" and row.get("lastHello") is not None:
            state, reason = "down", "silent on the LAN for over 60 s and no signed reply"
        elif row.get("state") == "offline":
            state, reason = "unknown", "not heard from since this peer link started"
        else:
            state, reason = "unknown", row.get("error") or "no signed reply yet"
        out = {"name": row.get("name"), "host": row.get("host"), "node": row.get("node"), "state": state,
               "reason": str(reason)[:200], "observedAt": None, "health": None, "modelsLoaded": None}
        if state == "up":
            models = status.get("models") if isinstance(status.get("models"), list) else []
            out.update(observedAt=status.get("observedAt"), health=status.get("health"),
                       modelsLoaded=sum(1 for m in models if isinstance(m, dict) and m.get("loadedState") == "loaded"))
        rows.append(out)
    return rows


def _table(rows: List[List[str]]) -> List[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip() for r in rows]


# ---------- commands ----------

def needs_status_file(args) -> bool:
    """Windows serves only its observer's status file (unchanged peer-link rule); say so in agiw's words."""
    return (args.host or pl.detect_host_kind()) == "windows" and not (args.status_file or args.lmstudio)


@contextmanager
def hold_state_lock(state_dir: Path, *, busy_code: int = EXIT_FAILED):
    lock = StateLock(state_dir)
    try:
        lock.acquire()
    except StateLockBusy as exc:
        raise CliError("the peer link is running with this state folder; stop it first; nothing was deleted",
                       busy_code) from exc
    except OSError as exc:
        raise CliError(f"the state folder could not be locked ({type(exc).__name__}); nothing was deleted") from exc
    try:
        yield lock
    finally:
        lock.release()


def cmd_serve(args, out) -> int:
    if needs_status_file(args):
        raise CliError(WINDOWS_SOURCE, EXIT_USAGE)
    argv = ["--state-dir", str(args.state_dir), "--status-port", str(args.status_port), "run",
            "--beacon-port", str(args.beacon_port)]
    if args.host:
        argv += ["--host", args.host]
    if args.name:
        argv += ["--name", args.name]
    if args.status_file:
        argv += ["--status-file", str(args.status_file)]
    if args.lmstudio:
        argv += ["--lmstudio", args.lmstudio]
    # launchd and Task Scheduler restart unsuccessful exits, so their service
    # invocations treat a busy state folder as a clean stop. Foreground/systemd
    # invocations use EX_TEMPFAIL.
    with hold_state_lock(args.state_dir, busy_code=0 if args.service else EXIT_TEMPFAIL):
        return pl.main(argv)


def cmd_pair(args, out) -> int:
    doc = control(args.state_dir, args.status_port, "POST", "/control/invite", {"peer": args.name})
    out(f"PIN {doc['pin']}")
    out(f"On {doc['peer']}, run `agiw accept` and type this PIN within {doc['expiresInSeconds'] // 60} minutes.")
    return 0


def cmd_accept(args, out, read_pin: Callable[[str], str]) -> int:
    pin = read_pin("PIN shown on the other machine: ").strip()
    doc = control(args.state_dir, args.status_port, "POST", "/control/accept", {"peer": args.name or "", "pin": pin})
    out(f"paired with {doc['peer']}")
    return 0


def cmd_unpair(args, out) -> int:
    control(args.state_dir, args.status_port, "POST", "/control/unpair", {"peer": args.name})
    out(f"unpaired {args.name}; on that machine, run `agiw unpair` for this one too")
    return 0


def cmd_peers(args, out) -> int:
    doc = control(args.state_dir, args.status_port, "GET", "/control/peers")
    if args.json:
        out(json.dumps(doc, indent=1, sort_keys=True))
        return 0
    me = doc.get("self", {})
    out(f"this machine: {me.get('name')} ({me.get('host')})")
    rows = [["STATE", "NAME", "KIND", "PAIRED", "NOTE"]]
    for row in doc.get("peers", []):
        note = "invite waiting: run `agiw accept`" if row.get("inviteWaiting") else ""
        rows.append([str(row.get("state")), str(row.get("name") or "?"), str(row.get("host") or "?"),
                     "yes" if row.get("paired") else "no", note])
    if len(rows) == 1:
        out("no AGIW machines seen yet (both machines need `agiw serve` on the same LAN, udp/8769 allowed)")
    else:
        for line in _table(rows):
            out(line)
    for invite in doc.get("invitesShowingPin", []):
        out(f"showing a PIN for {invite.get('name')}")
    if doc.get("quarantined"):
        out(f"{len(doc['quarantined'])} pairing(s) set aside because their keys could have been read; pair again")
    return 0


def cmd_status(args, out) -> int:
    doc = control(args.state_dir, args.status_port, "GET", "/control/peers")
    me = doc.get("self", {})
    machines = machine_rows(doc)
    if args.json:
        out(json.dumps({"schemaVersion": 1, "protocol": doc.get("protocol"), "observedAt": doc.get("observedAt"),
                        "self": {"name": me.get("name"), "host": me.get("host"), "node": me.get("node"),
                                 "state": "up", "reason": "this peer link answered"},
                        "machines": machines, "signed": True, "encrypted": False}, indent=1, sort_keys=True))
        return 0
    rows = [["MACHINE", "KIND", "STATE", "DETAIL"], [str(me.get("name")), str(me.get("host")), "up", "this machine"]]
    for m in machines:
        detail = (f"{m['health'] or 'health unknown'}, {m['modelsLoaded']} model(s) loaded" if m["state"] == "up"
                  else m["reason"])
        rows.append([str(m["name"] or "?"), str(m["host"] or "?"), m["state"], detail])
    for line in _table(rows):
        out(redact(line))
    if not machines:
        out("no paired machines yet: `agiw peers`, then `agiw pair <name>`")
    return 0


def cmd_doctor(args, out, manager: ServiceManager) -> int:
    installed = manager.definition_path().exists()
    return doctor_mod.run(args.state_dir, args.status_port, args.beacon_port, kind=manager.kind,
                          probe=doctor_mod.probe_port, service_installed=installed, write=out,
                          home=str(manager.home))


def serve_args(args) -> List[str]:
    """Options the login service passes to `agiw serve`, as absolute paths."""
    out = ["--state-dir", str(Path(args.state_dir).expanduser().resolve())]
    if args.status_port != pl.STATUS_PORT:
        out += ["--status-port", str(args.status_port)]
    return out


def serve_options(args) -> List[str]:
    out = []
    for flag, value in (("--host", args.host), ("--name", args.name), ("--lmstudio", args.lmstudio)):
        if value:
            out += [flag, value]
    if args.status_file:
        out += ["--status-file", str(Path(args.status_file).expanduser().resolve())]
    if args.beacon_port != pl.BEACON_PORT:
        out += ["--beacon-port", str(args.beacon_port)]
    return out


def make_manager(args, runner=None, kind: Optional[str] = None, home: Optional[Path] = None,
                 env: Optional[dict] = None) -> ServiceManager:
    options = serve_options(args) if getattr(args, "action", None) == "install" else []
    return ServiceManager(kind or pl.detect_host_kind(), serve_args(args), options, runner=runner, home=home, env=env)


def cmd_service(args, out, manager: ServiceManager) -> int:
    home = str(manager.home)
    if args.action == "install":
        if (args.host or manager.kind) == "windows" and not (args.status_file or args.lmstudio):
            raise CliError(WINDOWS_SOURCE + "; nothing was installed", EXIT_USAGE)
        steps = manager.install()
    elif args.action == "uninstall":
        steps = manager.uninstall()
    else:
        state = manager.status()
        if args.json:
            out(json.dumps({**state, "definition": show_path(Path(state["definition"]), home)}, indent=1,
                           sort_keys=True))
            return 0
        steps = [f"service ({state['kind']}): " + ("installed" if state["installed"] else "not installed") +
                 f", definition {show_path(Path(state['definition']), home)}",
                 "loaded: " + {True: "yes", False: "no", None: "unknown"}[state["loaded"]] +
                 ", running: " + {True: "yes", False: "no", None: "unknown"}[state["running"]]]
    for step in steps:
        out(redact(_show_paths_in(step, home), home))
    return 0


def _show_paths_in(step: str, home: Optional[str] = None) -> str:
    for prefix in ("wrote ", "removed "):
        if step.startswith(prefix):
            return prefix + show_path(Path(step[len(prefix):]), home)
    return step


# ---------- uninstall ----------

_KNOWN_FILES = ("identity.json", "control-token", "peers.json")


def _link_or_reparse(path: Path, info) -> bool:
    return (stat.S_ISLNK(info.st_mode) or path.is_symlink()
            or getattr(path, "is_junction", lambda: False)()
            or bool(getattr(info, "st_file_attributes", 0) & 0x400))


def purge_plan(state_dir: Path) -> tuple:
    """(files agiw created that --purge-keys deletes, folders removed if then empty, files left alone).
    Links are never followed; only names the peer link writes are deleted."""
    delete, folders, keep = [], [], []
    try:
        info = os.lstat(state_dir)
    except FileNotFoundError:
        return delete, folders, keep
    if not stat.S_ISDIR(info.st_mode) or _link_or_reparse(state_dir, info):
        raise CliError("the state folder is a link or not a folder; nothing was deleted")
    for entry in sorted(os.listdir(state_dir)):
        path = state_dir / entry
        entry_info = os.lstat(path)  # os.lstat, not Path.lstat: the same call on every Python version
        if _link_or_reparse(path, entry_info):
            keep.append(path)
        elif entry == "trusted" and stat.S_ISDIR(entry_info.st_mode):
            for name in sorted(os.listdir(path)):
                record = path / name
                base = name.split(".quarantined-")[0]
                record_info = os.lstat(record)
                if _link_or_reparse(record, record_info):
                    keep.append(record)
                elif stat.S_ISREG(record_info.st_mode) and (
                        (base.endswith(".json") and pl.UUID_RE.match(base[:-5].casefold()))
                        or (name.startswith(".") and name.endswith(".tmp"))):
                    delete.append(record)
                else:
                    keep.append(record)
            folders.append(path)
        elif stat.S_ISREG(entry_info.st_mode) and (
                entry in _KNOWN_FILES or (entry.startswith(".") and entry.endswith(".tmp"))):
            delete.append(path)
        else:
            keep.append(path)
    folders.append(state_dir)
    return delete, folders, keep


def _purge_token(state_dir: Path) -> str:
    """Validate the existing token without repairing it or contacting a process."""
    if any(name != "control-token" and name.casefold() == "control-token" for name in os.listdir(state_dir)):
        raise PermissionError("a case variant of control-token exists")
    token = pl._unseal(pl._read_private(state_dir / "control-token").strip(), "agiw-peer control-token")
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("invalid control token")
    return token


def peer_link_answers(state_dir: Path, port: int, probe=None) -> bool:
    """Refuse busy or uncertain observations; no request is sent to the peer link."""
    probe = doctor_mod.probe_port if probe is None else probe

    def refuse(reason):
        raise CliError(f"{reason}. Stop any running `agiw serve` using this state folder "
                       "(Ctrl-C, or `agiw service uninstall`), and pass its --status-port to uninstall; "
                       "nothing was deleted from the state folder.")

    try:
        (state_dir / "control-token").lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        refuse(f"the control token could not be checked ({type(exc).__name__})")
    else:
        try:
            _purge_token(state_dir)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            refuse(f"the control token could not be safely read ({type(exc).__name__})")
    ports = list(dict.fromkeys(((port, "tcp"), (pl.STATUS_PORT, "tcp"), (pl.BEACON_PORT, "udp"))))
    for checked_port, proto in ports:
        try:
            state = probe(checked_port, proto)
        except OSError as exc:
            refuse(f"{proto}/{checked_port} could not be checked ({type(exc).__name__})")
        if state != "free":
            refuse(f"{proto}/{checked_port} is {state}")
    return False


def cmd_uninstall(args, out, manager: ServiceManager, probe=None) -> int:
    home = str(manager.home)
    service_steps = manager.uninstall()
    for step in service_steps:
        out("login service: " + redact(_show_paths_in(step, home), home))
    service_result = ("the login service was removed" if any(
        step.startswith(("removed ", "stopped ")) for step in service_steps)
        else "; ".join(service_steps) or "no agiw service was installed")
    if args.purge_keys and args.yes:
        # Stop the login service before trying to take its state-folder lock.
        # All CliError refusals below occur before any key file is deleted.
        try:
            with hold_state_lock(args.state_dir) as lock:
                result = _cmd_uninstall(args, out, manager, probe)
                try:
                    lock.remove_if_state_gone()
                except OSError as exc:
                    out(f"could not remove the state lock file: {show_path(lock.path, home)} "
                        f"({type(exc).__name__}: {redact(str(exc), home)})")
                    return EXIT_FAILED
                return result
        except CliError as exc:
            raise CliError(f"{service_result}; no key file was deleted: {exc}", exc.code) from exc
    return _cmd_uninstall(args, out, manager, probe)


def _cmd_uninstall(args, out, manager: ServiceManager, probe=None) -> int:
    home = str(manager.home)
    if not args.purge_keys:
        out("pairing keys kept; `agiw uninstall --purge-keys` lists what would be deleted")
        return 0
    if args.yes and peer_link_answers(args.state_dir, args.status_port, probe=probe):
        raise CliError("the peer link is still running here; stop it (Ctrl-C, or `agiw service uninstall`) "
                       "before deleting its keys. Nothing was deleted from the state folder.")
    delete, folders, keep = purge_plan(args.state_dir)
    windows_registry = os.name == "nt"
    verb = "deleting" if args.yes else "would delete"
    for path in delete:
        out(f"{verb}: {show_path(path, home)}")
    if windows_registry:
        out(f"{verb}: this account's pairing generations under HKCU\\Software\\AGIW\\PeerLink\\TrustGenerations")
    for path in keep:
        out(f"left in place (not created by agiw): {show_path(path, home)}")
    if not args.yes:
        out("keys: dry run, no key files were deleted. Add --yes to delete these files. "
            "Your other machines keep their side; run `agiw unpair <this machine>` there.")
        return 0
    if not delete and not windows_registry:
        for folder in folders:
            try:
                folder.rmdir()
            except OSError:
                pass
        out("no agiw keys found to delete")
        return 0
    deleted, failed = 0, 0
    for path in delete:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            failed += 1
            out(f"could not delete: {show_path(path, home)} "
                f"({type(exc).__name__}: {redact(str(exc), home)})")
        else:
            deleted += 1
    revoke_error = None
    if windows_registry:
        try:
            pl._gen_revoke_all()
        except OSError as exc:
            revoke_error = exc
    for folder in folders:
        try:
            folder.rmdir()
        except OSError:
            pass  # not empty: what agiw did not create stays
    failures = f"; {failed} file(s) could not be deleted" if failed else ""
    out(f"deleted {deleted} file(s){failures}. Run `agiw unpair <this machine>` on your other machines.")
    if revoke_error is not None:
        out("revoking the Windows pairing generations failed "
            f"({type(revoke_error).__name__}: {redact(str(revoke_error), home)})")
    return EXIT_FAILED if failed or revoke_error is not None else 0


# ---------- parser ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agiw", description="Pair your Mac, Linux and Windows machines with a PIN and read their signed, "
                                 "read-only status over the LAN (agiw-peer/2). Status is signed, not encrypted; "
                                 "v0.1 dispatches no jobs.")
    parser.add_argument("--version", action="version", version=f"agiw {__version__} ({pl.PROTOCOL})")
    parser.add_argument("--state-dir", type=Path, default=None,
                        help="peer-link state folder (default: the per-OS user folder)")
    parser.add_argument("--status-port", type=int, default=pl.STATUS_PORT, help=f"default {pl.STATUS_PORT}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    def serve_flags(p):
        p.add_argument("--host", choices=pl.HOST_KINDS, help="this machine's kind (default: detected)")
        p.add_argument("--name", help="name shown to other machines (default: the host name)")
        source = p.add_mutually_exclusive_group()
        source.add_argument("--status-file", type=Path, help="schema-1 status JSON kept fresh by a local observer")
        source.add_argument("--lmstudio", help="local LM Studio base URL (Mac default http://127.0.0.1:1234)")
        p.add_argument("--beacon-port", type=int, default=pl.BEACON_PORT, help=f"default {pl.BEACON_PORT}")

    serve = sub.add_parser("serve", help="run the peer link in the foreground (what the login service runs)")
    serve_flags(serve)
    serve.add_argument("--service", action="store_true", help=argparse.SUPPRESS)
    pair = sub.add_parser("pair", help="pair with a discovered machine: shows a 6-digit PIN here")
    pair.add_argument("name", help="its name (or node id) as `agiw peers` lists it")
    accept = sub.add_parser("accept", help="type the PIN another machine is showing")
    accept.add_argument("name", nargs="?", default="", help="needed only when several invites wait")
    unpair = sub.add_parser("unpair", help="forget a paired machine")
    unpair.add_argument("name")
    peers = sub.add_parser("peers", help="discovered and paired machines")
    peers.add_argument("--json", action="store_true")
    status = sub.add_parser("status", help="up / down / unknown for each paired machine, from signed replies")
    status.add_argument("--json", action="store_true")
    doctor = sub.add_parser("doctor", help="read-only local checks: ports, firewall hints, folder and token privacy")
    doctor.add_argument("--beacon-port", type=int, default=pl.BEACON_PORT, help=argparse.SUPPRESS)
    service = sub.add_parser("service", help="start agiw at login (user level; no admin rights)")
    service.add_argument("action", choices=("install", "uninstall", "status"))
    service.add_argument("--json", action="store_true", help="with status")
    serve_flags(service)
    uninstall = sub.add_parser("uninstall", help="remove the login service; --purge-keys also lists the keys")
    uninstall.add_argument("--purge-keys", action="store_true", help="list pairing keys to delete (dry run)")
    uninstall.add_argument("--yes", action="store_true", help="with --purge-keys: really delete them")
    return parser


def main(argv: Optional[Sequence[str]] = None, *, out: Optional[Callable[[str], None]] = None,
         read_pin: Optional[Callable[[str], str]] = None, runner=None, kind: Optional[str] = None,
         home: Optional[Path] = None, env: Optional[dict] = None, probe=None) -> int:
    """Output, PIN input, service context and the uninstall port probe are injectable for tests."""
    out = out or (lambda line: print(line, flush=True))
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.state_dir is None:
        args.state_dir = pl.default_state_dir()
    try:
        if args.command == "uninstall" and args.yes and not args.purge_keys:
            raise CliError("--yes requires --purge-keys; nothing was changed", EXIT_USAGE)
        if args.command == "serve":
            return cmd_serve(args, out)
        if args.command == "pair":
            return cmd_pair(args, out)
        if args.command == "accept":
            return cmd_accept(args, out, read_pin or getpass.getpass)
        if args.command == "unpair":
            return cmd_unpair(args, out)
        if args.command == "peers":
            return cmd_peers(args, out)
        if args.command == "status":
            return cmd_status(args, out)
        manager = make_manager(args, runner, kind, home, env)
        if args.command == "doctor":
            return cmd_doctor(args, out, manager)
        if args.command == "service":
            return cmd_service(args, out, manager)
        return cmd_uninstall(args, out, manager, probe=probe)
    except CliError as exc:
        print("agiw: " + redact(str(exc)), file=sys.stderr)
        return exc.code
    except ServiceError as exc:
        print("agiw: " + redact(str(exc)), file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
