"""Bounded, read-only Linux observations; every filesystem source is injectable."""
from __future__ import annotations

import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

from .lanes import memory_level

MAX_PROCESSES = 2048
MAX_FDS = 256
MAX_ROWS = 4096
MIB = 2 ** 20
DISKS = ("/", "/srv/storage", "/srv/agiw-backup-seagate", "/srv/agiw-backup-wd")
NAME = re.compile(r"[A-Za-z0-9_.+ -]{1,96}\Z")
GPU_EXECUTABLE = "/srv/storage/work/claude-llama-vulkan/bin/llama-server"
PAIR_MODELS = "/srv/storage/models/pair-llamacpp-local-20261013-v1"


def read_text(path: Path | str, max_bytes: int = 65536) -> str | None:
    """Open regular files relative to pinned parents, refusing every symlink.

    O_NONBLOCK makes a FIFO substitution harmless. Virtual file sizes do not
    describe their contents; the bounded read enforces the cap.
    """
    if type(max_bytes) is not int or not 0 < max_bytes <= 2 ** 20:
        raise ValueError("invalid read bound")
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if ".." in path.parts:
        return None
    parent = file_fd = None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        parent = os.open(path.anchor, flags | os.O_DIRECTORY)
        for part in path.parts[1:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
        file_fd = os.open(path.name, flags, dir_fd=parent)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            return None
        chunks, size = [], 0
        while size <= max_bytes:
            chunk = os.read(file_fd, min(16384, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > max_bytes:
            return None
        return b"".join(chunks).decode("utf-8")
    except (OSError, ValueError, UnicodeError):
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent is not None:
            os.close(parent)


def read_json(path: Path | str, max_bytes: int = 65536) -> Any | None:
    text = read_text(path, max_bytes)
    if text is None:
        return None
    try:
        value = json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 32 or (isinstance(item, float) and not math.isfinite(item)):
                return None
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return value
    except (ValueError, RecursionError):
        return None


def _path(root: Path | str, name: str) -> Path:
    return Path(root) / name.lstrip("/")


def _kernel_path(root: Path | str, name: str, tree: str) -> Path | None:
    """Kernel class links are expected; they may resolve only within that tree."""
    try:
        base = _path(root, tree).resolve(strict=True)
        path = _path(root, name).resolve(strict=True)
        path.relative_to(base)
        return path
    except (OSError, ValueError, RuntimeError):
        return None


def _kernel_text(root: Path | str, name: str, max_bytes: int = 65536) -> str | None:
    tree = name.lstrip("/").split("/", 1)[0]
    if tree not in ("sys", "proc"):
        return None
    path = _kernel_path(root, name, tree)
    return read_text(path, max_bytes) if path is not None else None


def _entries(path: Path, limit: int) -> list[Path] | None:
    try:
        with os.scandir(path) as entries:
            rows = []
            for entry in entries:
                rows.append(Path(entry.path))
                if len(rows) > limit:
                    return None
        return sorted(rows, key=lambda entry: entry.name)
    except OSError:
        return None


def _number(text: str | None, low: float = 0, high: float = 2 ** 63) -> float | None:
    try:
        value = float(text.strip()) if text is not None else None
        return value if value is not None and math.isfinite(value) and low <= value <= high else None
    except (ValueError, OverflowError):
        return None


def _integer(value: str, high: int = 2 ** 63) -> int | None:
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 19 and int(value) <= high else None


def _value(root: Path | str, path: str, divisor: float = 1, high: float = 2 ** 63,
           low: float = 0) -> float | None:
    number = _number(_kernel_text(root, path, 256), low * divisor, high * divisor)
    return round(number / divisor, 3) if number is not None else None


def read_gpus(root: Path | str = Path("/")) -> list[dict[str, Any]] | None:
    """AMD cards identified by vendor and driver, never by a fixed card number."""
    rows = []
    cards = _entries(_path(root, "/sys/class/drm"), 128)
    if cards is None:
        return None
    for card in cards:
        if not re.fullmatch(r"card\d+", card.name):
            continue
        device = f"/sys/class/drm/{card.name}/device"
        device_path = _kernel_path(root, device, "sys")
        if device_path is None:
            return None
        try:
            device_parts = device_path.relative_to(_path(root, "/sys/devices")).parts
        except ValueError:
            return None
        # DRM also exposes non-PCI devices such as simpledrm. Their sysfs
        # paths are explicitly rooted under /sys/devices/virtual or /platform.
        # Unknown layouts stay incomplete; missing PCI identity must not look
        # like a completed empty scan.
        is_pci = any(re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", part)
                     for part in device_parts)
        if not is_pci and device_parts and device_parts[0] in ("virtual", "platform"):
            continue
        if not is_pci:
            return None
        vendor = (_kernel_text(root, device + "/vendor", 64) or "").strip().lower()
        if not re.fullmatch(r"0x[0-9a-f]{4}", vendor):
            return None
        if vendor != "0x1002":
            continue
        driver = _kernel_path(root, device + "/driver", "sys")
        if driver is None:
            return None
        if driver.name != "amdgpu":
            continue
        pci_device = (_kernel_text(root, device + "/device", 64) or "").strip().lower()
        pci_device = pci_device if re.fullmatch(r"0x[0-9a-f]{4}", pci_device) else None
        fields = {"utilizationPercent": ("gpu_busy_percent", 1, 100),
                  "memoryUsedMiB": ("mem_info_vram_used", MIB, 2 ** 30),
                  "memoryTotalMiB": ("mem_info_vram_total", MIB, 2 ** 30)}
        row = {"index": int(card.name[4:]), "name": "AMD GPU (1002:" + (pci_device[2:] if pci_device else "unknown") + ")",
               "vendorId": vendor, "deviceId": pci_device, "driver": "amdgpu", "source": "linux-sysfs",
               "utilizationKind": "instant", "temperatureC": None, "powerW": None, "fanRpm": None, "sources": {}}
        for key, (file, divisor, high) in fields.items():
            row[key] = _value(root, device + "/" + file, divisor, high)
            row["sources"][key] = device + "/" + file
        if row["memoryTotalMiB"] == 0:
            row["memoryTotalMiB"] = None
        if (row["memoryUsedMiB"] is not None and row["memoryTotalMiB"] is not None
                and row["memoryUsedMiB"] > row["memoryTotalMiB"]):
            row["memoryUsedMiB"] = None
        for hwmon in _entries(_path(root, device + "/hwmon"), 32) or []:
            if not re.fullmatch(r"hwmon\d+", hwmon.name):
                continue
            base = device + "/hwmon/" + hwmon.name
            for key, file, divisor, high in (("temperatureC", "temp1_input", 1000, 150),
                                             ("powerW", "power1_input", 1000000, 2000),
                                             ("fanRpm", "fan1_input", 1, 50000)):
                if row[key] is None:
                    value = _value(root, base + "/" + file, divisor, high)
                    if value is not None:
                        row[key] = value
                        row["sources"][key] = base + "/" + file
        rows.append(row)
    return rows


def _psi(text: str | None) -> dict[str, float | None]:
    result = {"someAvg10": None, "fullAvg10": None}
    for line in (text or "").splitlines():
        fields = line.split()
        if fields and fields[0] in ("some", "full"):
            for token in fields[1:]:
                if token.startswith("avg10="):
                    result[fields[0] + "Avg10"] = _number(token[6:], high=100)
    return result


def read_memory(root: Path | str = Path("/")) -> dict[str, Any]:
    values = {}
    for line in (_kernel_text(root, "/proc/meminfo") or "").splitlines():
        match = re.fullmatch(r"([A-Za-z_()]+):\s+(\d+) kB\s*", line)
        if match:
            value = _integer(match[2], 2 ** 53)
            if value is not None:
                values[match[1]] = value * 1024
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    valid = total is not None and 0 < total <= 2 ** 63 and available is not None and 0 <= available <= total
    percent = available * 100 / total if valid else None
    limit, committed = values.get("CommitLimit"), values.get("Committed_AS")
    commit = committed * 100 / limit if limit and committed is not None and committed <= 2 ** 63 else None
    psi = _psi(_kernel_text(root, "/proc/pressure/memory", 4096))
    level = memory_level(percent, commit) if valid else "unknown"
    # PSI escalates the shared available-memory/commit level. A missing PSI
    # reading is left unknown; it does not erase an observed RAM measurement.
    some, full = psi["someAvg10"], psi["fullAvg10"]
    if level != "unknown":
        if full is not None and full >= 10:
            level = "critical"
        elif (some is not None and some >= 10) or (full is not None and full >= 1):
            level = "critical" if level == "critical" else "tight"
        elif some is not None and some >= 1 and level == "ok":
            level = "watch"
    swap_total, swap_free = values.get("SwapTotal"), values.get("SwapFree")
    swap_used = swap_total - swap_free if swap_total is not None and swap_free is not None and 0 <= swap_free <= swap_total <= 2 ** 63 else None
    return {"level": level, "availablePercent": round(percent, 1) if percent is not None else None,
            "totalBytes": total if valid else None, "availableBytes": available if valid else None,
            "commitPercent": round(commit, 1) if commit is not None else None,
            "swapUsedBytes": swap_used, "compressedBytes": None, "gpuAllocBytes": None,
            "psi": psi, "source": "/proc/meminfo", "pressureSource": "/proc/pressure/memory",
            "consumers": [], "suggestions": [], "paused": [],
            "reasons": [f"{percent:.0f}% RAM available"] if percent is not None else []}


def read_cpu(root: Path | str = Path("/"), previous: dict | None = None) -> dict[str, Any]:
    lines = (_kernel_text(root, "/proc/stat") or "").splitlines()
    sample, percent = None, None
    for line in lines:
        if line.startswith("cpu "):
            fields = line.split()[1:]
            if len(fields) >= 4 and all(_integer(value) is not None for value in fields):
                values = [int(value) for value in fields[:8]]
                sample = {"total": sum(values), "idle": values[3] + (values[4] if len(values) > 4 else 0)}
    boot_id = (_kernel_text(root, "/proc/sys/kernel/random/boot_id", 128) or "").strip() or None
    before = previous.get("sample") if isinstance(previous, dict) else None
    if sample is not None and isinstance(before, dict) and all(type(before.get(k)) is int for k in ("total", "idle")):
        elapsed, idle = sample["total"] - before["total"], sample["idle"] - before["idle"]
        if elapsed > 0 and 0 <= idle <= elapsed and previous.get("bootId") == boot_id:
            percent = round(100 * (elapsed - idle) / elapsed, 1)
    loads = (_kernel_text(root, "/proc/loadavg", 256) or "").split()
    return {"utilizationPercent": percent, "sample": sample, "bootId": boot_id,
            "logicalCpus": sum(bool(re.match(r"cpu\d+ ", line)) for line in lines) or None,
            "loadAverage": [_number(value, high=1000000) for value in loads[:3]] if len(loads) >= 3 else None,
            "source": "/proc/stat", "pressure": _psi(_kernel_text(root, "/proc/pressure/cpu", 4096))}


def read_boot(root: Path | str = Path("/")) -> dict[str, Any]:
    boot_id = (_kernel_text(root, "/proc/sys/kernel/random/boot_id", 128) or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", boot_id):
        boot_id = None
    since = None
    for line in (_kernel_text(root, "/proc/stat") or "").splitlines():
        if re.fullmatch(r"btime \d+", line):
            since = _integer(line.split()[1])
    uptime = (_kernel_text(root, "/proc/uptime", 128) or "").split()
    return {"bootId": boot_id, "startedUnix": since, "uptimeSeconds": _number(uptime[0]) if uptime else None,
            "source": "/proc/sys/kernel/random/boot_id; /proc/stat"}


def read_temperatures(root: Path | str = Path("/")) -> list[dict[str, Any]]:
    result = []
    for hwmon in _entries(_path(root, "/sys/class/hwmon"), 128) or []:
        if not re.fullmatch(r"hwmon\d+", hwmon.name):
            continue
        base = "/sys/class/hwmon/" + hwmon.name
        chip = (_kernel_text(root, base + "/name", 128) or "").strip()
        if chip not in ("coretemp", "nvme", "amdgpu"):
            continue
        for sensor in _entries(hwmon, 256) or []:
            if not re.fullmatch(r"temp\d+_input", sensor.name):
                continue
            source = base + "/" + sensor.name
            label = (_kernel_text(root, source.replace("_input", "_label"), 128) or "").strip()
            result.append({"chip": chip, "label": label if NAME.fullmatch(label) else sensor.name,
                           "temperatureC": _value(root, source, 1000, 150, -40), "source": source})
    return result


def read_disks(root: Path | str = Path("/")) -> list[dict[str, Any]]:
    mounts = set()
    mount_text = _kernel_text(root, "/proc/self/mountinfo", 65536)
    for line in (mount_text or "").splitlines():
        fields = line.split()
        if len(fields) >= 10 and "-" in fields:
            mounts.add(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]))
    result = []
    for mount in DISKS:
        row = {"path": mount, "state": "unknown", "reason": "mount-table-unreadable" if mount_text is None else "not-mounted",
               "totalBytes": None, "availableBytes": None, "usedBytes": None, "source": "statvfs"}
        # An ordinary directory on / must not masquerade as a mounted data disk.
        if mount in mounts:
            try:
                info = os.statvfs(_path(root, mount))
                if info.f_frsize <= 0 or not 0 <= info.f_bavail <= info.f_bfree <= info.f_blocks:
                    raise ValueError("invalid statvfs")
                row.update(state="up", reason="observed", totalBytes=info.f_blocks * info.f_frsize,
                           availableBytes=info.f_bavail * info.f_frsize,
                           usedBytes=(info.f_blocks - info.f_bfree) * info.f_frsize)
            except (OSError, ValueError):
                row["reason"] = "unreadable"
        result.append(row)
    return result


def _flags(argv: list[str]) -> dict[str, Any]:
    aliases = {"--port": "port", "--host": "host", "--ctx-size": "context", "-c": "context",
               "--threads": "threads", "-t": "threads", "--n-gpu-layers": "gpuLayers", "-ngl": "gpuLayers",
               "--models-max": "modelsMax", "--alias": "alias", "-a": "alias", "--model": "model", "-m": "model"}
    result = {}
    for index, item in enumerate(argv):
        key, _, equal = item.partition("=")
        if key not in aliases:
            continue
        value = equal if "=" in item else argv[index + 1] if index + 1 < len(argv) else ""
        name = aliases[key]
        if name in ("model", "alias"):
            value = Path(value).name if name == "model" else value
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+/-]{0,119}", value):
                result[name] = value
        elif name == "host":
            try:
                result[name] = str(ipaddress.ip_address(value))
            except ValueError:
                pass
        elif re.fullmatch(r"\d{1,7}", value) and 0 <= int(value) <= (65535 if name == "port" else 1000000):
            if name != "port" or int(value) > 0:
                result[name] = int(value)
    return result


def _process_start(text: str | None, pid: int) -> int | None:
    if text is None:
        return None
    # comm is parenthesized and may itself contain spaces or ')'. Fields after
    # its last closing parenthesis retain their fixed /proc stat positions.
    before, separator, after = text.rpartition(") ")
    if not separator or not before.startswith(f"{pid} ("):
        return None
    fields = after.split()
    if len(fields) < 20 or not re.fullmatch(r"[A-Za-z]", fields[0]):
        return None
    return _integer(fields[19])  # stat field 22, starttime (clock ticks since boot)


def _coordinator_identity(argv: list[str]) -> dict[str, str]:
    """Bounded runtime identity from a mapped invocation; never retain argv."""
    args = argv[:-1] if argv and argv[-1] == "" else argv
    if len(args) not in (5, 7) or args[:2] != ["/usr/bin/python3", "-B"] or args[3] != "serve":
        return {}
    script = re.fullmatch(
        r"/home/[A-Za-z0-9_-]+/\.local/share/agiw-runtimes/"
        r"(linux-scheduler-authority-v7|linux-scheduler-authority-v8-jev114-qwen35-boot-v1)/"
        r"(coordinator(?:_v8)?\.py)", args[2])
    release = re.fullmatch(r"--release-sha256=([0-9a-f]{64})", args[4])
    if not script or not release:
        return {}
    v7 = script[1] == "linux-scheduler-authority-v7"
    if script[2] != ("coordinator.py" if v7 else "coordinator_v8.py") or len(args) != (5 if v7 else 7):
        return {}
    if not v7 and (not re.fullmatch(r"--upgrade-directory=/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", args[5])
                   or not re.fullmatch(r"--upgrade-binding-sha256=[0-9a-f]{64}", args[6])):
        return {}
    return {"coordinatorRuntime": script[1], "coordinatorReleaseSha256": release[1]}


def read_processes(root: Path | str = Path("/")) -> list[dict[str, Any]] | None:
    """Process names and validated lane flags only; never return argv or environ."""
    result = []
    entries = _entries(_path(root, "/proc"), MAX_PROCESSES + 256)
    if entries is None:
        return None
    unreadable = False
    for entry in entries:
        if not entry.name.isdigit() or len(entry.name) > 10:
            continue
        start_before = _process_start(read_text(entry / "stat", 8192), int(entry.name))
        status = read_text(entry / "status", 16384)
        if status is None:
            unreadable = True
            continue
        values = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        name = values.get("Name", "").strip()
        name = name if NAME.fullmatch(name) else "unknown"
        row = {"pid": int(entry.name), "name": name, "exe": None, "kind": "unknown", "flags": {}, "port": None,
               "workingSetBytes": None, "startTimeTicks": None}
        rss = re.fullmatch(r"\s*(\d+) kB\s*", values.get("VmRSS", ""))
        if rss and _integer(rss[1], 2 ** 53) is not None:
            row["workingSetBytes"] = int(rss[1]) * 1024
        uid = values.get("Uid", "").split()
        if name == "unknown" or len(uid) != 4 or any(_integer(value) is None for value in uid):
            unreadable = True
        if uid and _integer(uid[0]) == os.getuid():
            command = read_text(entry / "cmdline", 32768)
            if command is None or command and not command.endswith("\0"):
                unreadable = True
            argv = (command or "").split("\0")
            try:
                executable = os.readlink(entry / "exe")
            except OSError:
                executable = ""
                if command:
                    unreadable = True
            basename = Path(executable).name
            if NAME.fullmatch(basename):
                row["exe"] = basename
                row["name"] = basename
            elif command:
                unreadable = True
            if basename == "llama-server":
                row["flags"] = _flags(argv[1:])
                row["port"] = row["flags"].get("port")
                if executable == GPU_EXECUTABLE:
                    row["kind"] = "gpu-wrapper"
                    row["exe"] = GPU_EXECUTABLE
                elif row["port"] == 8081 and any(argv[i] == "--models-dir" and argv[i + 1] == PAIR_MODELS
                                                for i in range(len(argv) - 1)):
                    row["kind"] = "pair-router"
            elif basename.startswith("nvpair-"):
                row["kind"] = "pair"
            elif basename == "ollama":
                row["kind"] = "ollama"
            elif basename.startswith("python"):
                # Any exact mapped script argument is a conservative candidate.
                # Only _coordinator_identity may attest the exact command shape;
                # extra interpreter flags must never turn a candidate into absence.
                candidate = any(re.fullmatch(
                    r"/home/[A-Za-z0-9_-]+/\.local/share/agiw-runtimes/"
                    r"(?:linux-scheduler-authority-v7/coordinator\.py|"
                    r"linux-scheduler-authority-v8-jev114-qwen35-boot-v1/coordinator_v8\.py)",
                    arg) for arg in argv[1:])
                if candidate:
                    row["coordinator"] = True
                    identity = _coordinator_identity(argv)
                    if identity:
                        row["kind"] = "coordinator"
                        row.update(identity)
        start_after = _process_start(read_text(entry / "stat", 8192), row["pid"])
        if start_before is not None and start_before == start_after:
            row["startTimeTicks"] = start_before
        else:
            # A reused PID invalidates all identity-derived fields, not merely
            # rate deltas. Never authorize a probe with the earlier executable.
            row.update(name="unknown", exe=None, kind="unknown", flags={}, port=None, workingSetBytes=None)
            row["identityComplete"] = False
            row.pop("coordinator", None)
            row.pop("coordinatorRuntime", None)
            row.pop("coordinatorReleaseSha256", None)
        result.append(row)
        if len(result) >= MAX_PROCESSES:
            return None
    return None if unreadable else result


def read_gpu_lock(root: Path | str = Path("/")) -> dict[str, Any]:
    """Match lock identity from metadata; never open or acquire the lane lock."""
    source = "/srv/storage/work/claude-llama-vulkan/gpu.lock"
    result = {"state": "unknown", "holderPid": None, "source": "/proc/locks", "path": source}
    try:
        info = _path(root, source).lstat()
        if not stat.S_ISREG(info.st_mode):
            return result
    except OSError:
        return result
    text = _kernel_text(root, "/proc/locks", 65536)
    if text is None:
        return result
    identity = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or "->" in fields:
            continue
        parts = fields[5].split(":")
        try:
            match = len(parts) == 3 and (int(parts[0], 16), int(parts[1], 16), int(parts[2])) == identity
            pid = int(fields[4])
        except ValueError:
            continue
        if match:
            return {**result, "state": "held", "holderPid": pid if pid > 0 else None}
    return {**result, "state": "unheld"}


def _tcp_address(value: str) -> tuple[str, int] | None:
    try:
        address, port = value.split(":")
        raw = bytes.fromhex(address)
        if len(raw) not in (4, 16):
            return None
        # /proc TCP words use native little-endian bytes on the mapped x86 host.
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
        parsed = ipaddress.ip_address(raw)
        parsed = parsed.ipv4_mapped or parsed if isinstance(parsed, ipaddress.IPv6Address) else parsed
        number = int(port, 16)
        return (str(parsed), number) if 0 <= number <= 65535 else None
    except (ValueError, TypeError):
        return None


def listener_owned(process: dict, network: dict | None, port: int) -> bool:
    """Require one observed loopback listener tied to the same stable process."""
    start = process.get("startTimeTicks")
    if type(start) is not int or start < 0 or not isinstance(network, dict) or network.get("state") != "up":
        return False
    listeners = [row for row in network.get("listeners", []) if isinstance(row, dict)
                 and row.get("localPort") == port and row.get("local") in ("127.0.0.1", "0.0.0.0", "::")]
    return len(listeners) == 1 and listeners[0].get("local") == "127.0.0.1" \
        and listeners[0].get("ownershipVerified") is True \
        and listeners[0].get("pid") == process.get("pid") \
        and listeners[0].get("startTimeTicks") == start


def read_network(root: Path | str = Path("/"), processes: list[dict] | None = None,
                 ports: dict[int, str] | None = None) -> dict[str, Any]:
    processes = read_processes(root) if processes is None else processes
    ports = {8081: "pair-llamacpp", 11435: "pair-ollama"} if ports is None else ports
    owners, rows, complete, owners_complete = {}, [], True, processes is not None
    fd_count = 0
    for process in processes or []:
        identity = process.get("startTimeTicks")
        process_path = _path(root, f"/proc/{process['pid']}")
        before = _process_start(read_text(process_path / "stat", 8192), process["pid"])
        if type(identity) is not int or identity != before:
            owners_complete = False
            continue
        fds = _entries(_path(root, f"/proc/{process['pid']}/fd"), MAX_FDS)
        process_sockets = []
        if fds is None:
            owners_complete = False
        for fd in fds or []:
            fd_count += 1
            if fd_count > MAX_ROWS:
                owners_complete = False
                break
            try:
                match = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(fd))
                if match:
                    process_sockets.append(match[1])
            except OSError:
                owners_complete = False
                continue
        after = _process_start(read_text(process_path / "stat", 8192), process["pid"])
        if after != identity:
            owners_complete = False
        else:
            for inode in process_sockets:
                # Shared socket descriptors do not establish a unique owner.
                if inode in owners and owners[inode] != process:
                    owners[inode] = {}
                    owners_complete = False
                else:
                    owners[inode] = process
        if fd_count > MAX_ROWS:
            break
    for name in ("tcp", "tcp6"):
        text = _kernel_text(root, "/proc/net/" + name, 2 ** 20)
        if text is None:
            complete = False
            continue
        lines = text.splitlines()
        header = lines[0].split() if lines else []
        if len(header) < 10 or header[:2] != ["sl", "local_address"] \
                or header[2] not in ("rem_address", "remote_address") or header[3] != "st" or "inode" not in header:
            complete = False
        for line in lines[1:MAX_ROWS + 1]:
            fields = line.split()
            if len(fields) < 10:
                if line.strip():
                    complete = False
                continue
            local, remote = _tcp_address(fields[1]), _tcp_address(fields[2])
            if local is None or remote is None:
                complete = False
                continue
            if fields[3] not in ("01", "0A"):
                continue
            process = owners.get(fields[9], {})
            rows.append({"state": "listen" if fields[3] == "0A" else "established",
                         "local": local[0], "localPort": local[1], "remote": remote[0], "remotePort": remote[1],
                         "pid": process.get("pid"), "name": process.get("name", "unknown"),
                         "startTimeTicks": process.get("startTimeTicks"), "ownershipVerified": bool(process)})
        if len(lines) > MAX_ROWS + 1:
            complete = False
    listeners, callers = [], []
    for row in rows:
        if row["state"] != "listen":
            continue
        exposed = not ipaddress.ip_address(row["local"]).is_loopback
        listeners.append({**row, "exposed": exposed,
                          "plaintext": True if exposed and row["localPort"] in (*range(14318, 14324), 8777, 65377) else None})
    network = {"state": "up" if complete else "unknown", "listeners": listeners}
    verified = {}
    for port, lane in ports.items():
        candidates = []
        for process in processes or []:
            kind = process.get("kind")
            identity = (lane == "pair-llamacpp" and port == 8081 and kind == "pair-router") \
                or (lane == "pair-ollama" and port == 11435 and kind == "ollama") \
                or (lane == "gpu-wrapper" and kind == "gpu-wrapper" and process.get("exe") == GPU_EXECUTABLE
                    and process.get("port") == port and process.get("flags", {}).get("host") == "127.0.0.1")
            if identity and listener_owned(process, network, port):
                candidates.append(process)
        if len(candidates) == 1:
            verified[port] = candidates[0]["pid"]
    if any(row["localPort"] in ports and row["localPort"] not in verified for row in listeners):
        owners_complete = False
    server_pids = set(verified.values())
    server_connections = {(row["local"], row["localPort"], row["remote"], row["remotePort"])
                          for row in rows if row["state"] == "established"
                          and row["localPort"] in verified and row["pid"] == verified[row["localPort"]]}
    for row in rows:
        if row["state"] != "established" or row["remotePort"] not in verified or row["pid"] in server_pids or row["pid"] == os.getpid():
            continue
        if (row["remote"], row["remotePort"], row["local"], row["localPort"]) in server_connections:
            callers.append({"pid": row["pid"], "name": row["name"], "lane": ports[row["remotePort"]], "connections": 1})
    interfaces = []
    dev = _kernel_text(root, "/proc/net/dev", 65536)
    for line in (dev or "").splitlines():
        if ":" not in line:
            continue
        name, raw = line.split(":", 1)
        fields = raw.split()
        if (NAME.fullmatch(name.strip()) and len(fields) >= 16
                and _integer(fields[0]) is not None and _integer(fields[8]) is not None):
            interfaces.append({"name": name.strip(), "receivedBytes": int(fields[0]), "sentBytes": int(fields[8])})
    return {"state": "up" if complete else "unknown", "reason": "observed" if complete else "tcp-table-unreadable-or-truncated",
            "listeners": listeners, "callers": callers, "callersState": "known" if complete and owners_complete else "unknown",
            "interfaces": interfaces, "source": "/proc/net/tcp; /proc/net/tcp6; /proc/net/dev"}


class LinuxSystem:
    def __init__(self, root: Path | str = Path("/"), home: Path | str | None = None):
        self.root = Path(root)
        self.home = Path(home) if home is not None else Path.home()

    def processes(self) -> list[dict[str, Any]] | None:
        return read_processes(self.root)

    def snapshot(self, previous: dict | None = None) -> dict[str, Any]:
        processes = self.processes()
        ports = {row["port"]: "gpu-wrapper" for row in processes or [] if row.get("kind") == "gpu-wrapper" and row.get("port")}
        ports.update({8081: "pair-llamacpp", 11435: "pair-ollama"})
        gpus = read_gpus(self.root)
        units = None
        if self.root == Path("/") and sys.platform == "linux":
            from .probes import coordinator_units
            units = coordinator_units()
        return {"gpus": gpus, "gpusComplete": gpus is not None,
                "gpusState": "known" if gpus is not None else "unknown",
                "coordinatorUnits": units, "memory": read_memory(self.root),
                "cpu": read_cpu(self.root, previous.get("cpu") if previous else None),
                "processes": processes, "network": read_network(self.root, processes, ports),
                "boot": read_boot(self.root), "disks": read_disks(self.root), "temperatures": read_temperatures(self.root),
                "gpuLock": read_gpu_lock(self.root)}
