"""Mac-side SSH local forwards to the PC's loopback inference ports.

The PC servers bind to 127.0.0.1. The Mac reaches them with ``ssh -N -L``.
User, host and the identity file come from the environment or from
``~/.config/agiw/pc-tunnel.json``. This module never reads the key.

A restricted PC key refuses the forward until an admin adds permitopen for
127.0.0.1:1234, :1245 and :1236. This code does not check or change sshd.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading

DEFAULT_FORWARDS = (
    {"name": "router", "localPort": 1334, "remotePort": 1234, "id": "pc:router"},
    {"name": "gpt-oss-20b", "localPort": 1345, "remotePort": 1245, "id": "pc:gpt-oss-20b"},
    {"name": "qwen3.8-27b", "localPort": 1336, "remotePort": 1236, "id": "pc:qwen3.8-27b"},
)
_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}\Z")


def _port(value):
    return type(value) is int and 1 <= value <= 65535


def _identity(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or "\n" in value or "\x00" in value or not value.startswith("/"):
        raise ValueError("identity")
    if len(value) > 400:
        raise ValueError("identity")
    return value


def _forwards(value):
    if not isinstance(value, list) or not value or len(value) > 8:
        raise ValueError("forwards")
    rows = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("forwards")
        name = item.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("forwards")
        if not _port(item.get("localPort")) or not _port(item.get("remotePort")):
            raise ValueError("forwards")
        lane = item.get("id")
        if lane is not None and (not isinstance(lane, str) or not _NAME.fullmatch(lane.replace(":", ""))):
            raise ValueError("forwards")
        rows.append({"name": name, "localPort": item["localPort"], "remotePort": item["remotePort"],
                     "id": lane or f"pc:{name}"})
    return rows


def _blank():
    return {"user": None, "host": None, "identityFile": None,
            "forwards": [dict(item) for item in DEFAULT_FORWARDS]}


def _apply(config, parsed):
    if not isinstance(parsed, dict):
        raise ValueError("config")
    if set(parsed) - {"user", "host", "identityFile", "forwards"}:
        raise ValueError("config")
    if "user" in parsed:
        if not isinstance(parsed["user"], str) or not _NAME.fullmatch(parsed["user"]):
            raise ValueError("user")
        config["user"] = parsed["user"]
    if "host" in parsed:
        if not isinstance(parsed["host"], str) or not _HOST.fullmatch(parsed["host"]):
            raise ValueError("host")
        config["host"] = parsed["host"]
    if "identityFile" in parsed:
        config["identityFile"] = _identity(parsed["identityFile"])
    if "forwards" in parsed:
        config["forwards"] = _forwards(parsed["forwards"])


def _config_file(path, env, use_home):
    if path is not None:
        return path
    if env.get("AGIW_PC_SSH_CONFIG"):
        return env["AGIW_PC_SSH_CONFIG"]
    if not use_home:
        return None
    default = Path.home() / ".config" / "agiw" / "pc-tunnel.json"
    return default if default.is_file() else None


def load_config(path=None, environ=None):
    """Environment, then a JSON file. An invalid file is flagged. Host and user have no default."""
    env = os.environ if environ is None else environ
    config = _blank()
    if env.get("AGIW_PC_SSH_USER"):
        config["user"] = env["AGIW_PC_SSH_USER"]
    if env.get("AGIW_PC_SSH_HOST"):
        config["host"] = env["AGIW_PC_SSH_HOST"]
    if env.get("AGIW_PC_SSH_IDENTITY"):
        config["identityFile"] = env["AGIW_PC_SSH_IDENTITY"]
    file_path = _config_file(path, env, environ is None)
    if file_path:
        try:
            raw = Path(file_path).read_text(encoding="utf-8")
            if len(raw) > 8192:
                raise ValueError("config")
            _apply(config, json.loads(raw))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            config["error"] = "config_invalid"
            return config
    try:
        if config["user"] is not None and not _NAME.fullmatch(config["user"]):
            raise ValueError("user")
        if config["host"] is not None and not _HOST.fullmatch(config["host"]):
            raise ValueError("host")
        config["identityFile"] = _identity(config["identityFile"])
        config["forwards"] = _forwards(config["forwards"])
    except (TypeError, ValueError):
        config["error"] = "config_invalid"
    return config


def runnable(config):
    return not config.get("error") and bool(config.get("user") and config.get("host") and config.get("identityFile"))


def ssh_command(config):
    """``ssh -N -L`` argv. Raises when user, host or the identity file is missing."""
    if not runnable(config):
        raise ValueError("tunnel config is not runnable")
    argv = ["ssh", "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2",
            "-o", "IdentitiesOnly=yes",
            "-i", config["identityFile"]]
    for forward in config["forwards"]:
        argv.extend(["-L", f"127.0.0.1:{forward['localPort']}:127.0.0.1:{forward['remotePort']}"])
    argv.append(f"{config['user']}@{config['host']}")
    return argv


def port_accepts(port, timeout=0.2):
    sock = socket.socket()
    try:
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def describe(config, accepts):
    """Closed forwards are tunnel_down. That is not a claim that a model server is down."""
    if config.get("error"):
        return {"state": "down", "reason": "config_invalid", "detail": "PC tunnel config is invalid.", "forwards": []}
    if not config.get("user") or not config.get("host"):
        return {"state": "down", "reason": "host_unconfigured",
                "detail": "PC SSH user and host are not configured.", "forwards": []}
    forwards = []
    closed = []
    for item in config["forwards"]:
        opened = bool(accepts(item["localPort"]))
        forwards.append({"name": item["name"], "id": item["id"], "localPort": item["localPort"],
                         "remotePort": item["remotePort"], "open": opened})
        if not opened:
            closed.append(item["localPort"])
    if closed:
        identity = " An identity file is not configured." if not config.get("identityFile") else ""
        ports = ", ".join(str(port) for port in closed)
        return {"state": "down", "reason": "tunnel_down",
                "detail": f"SSH tunnel is down. Local forwards not accepting: {ports}.{identity}",
                "host": config["host"], "user": config["user"], "forwards": forwards}
    return {"state": "up", "reason": "forwarded",
            "detail": "SSH local forwards are accepting connections.",
            "host": config["host"], "user": config["user"], "forwards": forwards}


def targets(config, accepts):
    """Probe bases for the PC lanes. A closed or unconfigured forward has ``base`` None."""
    status = describe(config, accepts)
    source = config.get("forwards") if config.get("forwards") and not config.get("error") else DEFAULT_FORWARDS
    opened = {row["name"]: row["open"] for row in status.get("forwards") or []}
    return status, [{"id": item["id"], "name": item["name"], "service": f"127.0.0.1:{item['remotePort']}",
                     "base": f"http://127.0.0.1:{item['localPort']}" if opened.get(item["name"]) else None}
                    for item in source]


def supervise(config, stop, spawn=subprocess.Popen, pause=2.0):
    """Run ``ssh -N`` until ``stop`` is set. Does not start without user, host and an identity file."""
    if config.get("error"):
        return {"state": "down", "reason": "config_invalid"}
    if not config.get("user") or not config.get("host"):
        return {"state": "down", "reason": "host_unconfigured"}
    if not config.get("identityFile"):
        return {"state": "down", "reason": "identity_missing"}
    argv = ssh_command(config)
    while not stop.is_set():
        proc = spawn(argv)
        while not stop.is_set() and proc.poll() is None:
            stop.wait(0.5)
        if stop.is_set():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            break
        stop.wait(pause)
    return {"state": "stopped", "reason": "supervisor_stopped"}


def main(argv=None):
    del argv
    config = load_config()
    if not runnable(config):
        print(json.dumps(describe(config, port_accepts), sort_keys=True))
        return 1
    stop = threading.Event()

    def _stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    supervise(config, stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
