"""Pinned, source-only idle-analyzer successor and read-only observer attestation.

No installer, service mutation, network calls or general interpreter allowance.
The successor preserves the historical passive-coordinator gate; it exempts only
one byte-verified observer service PID while that exact identity remains stable.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

IDLE_UNIT = "agiw-idle-analysis.service"
OBSERVER_UNIT = "agiw-linux-observer.service"
PINS = {
    "analyzer.py": "ef068501d46d4d74b854da173ebcdbb3cf4a9fb01ec9e3ecc209743bd70ea5a8",
    "passive_attestation.py": "9a0a56ced049137904f637edca6e39c5489bd99d09dcb0b450d964a046d32571",
    "coordinator-pins.json": "1daf3bd357e74b0e3531172422a99fbdc09ee3f567a9aab084ab098658ea6d7b",
    "input-manifest.json": "859dcf74113f6e16d5727203a929715b3eb2be8464e5001691dc9eb6ddaf4fa9",
}
MODULE = "agiw_linux/idle_allowance.py"
HEX = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_file(path, limit=2 * 1024 * 1024, owner=None):
    """No-follow descriptor traversal, stable bounded single regular file."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("idle allowance path invalid")
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.parts[1:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_mode & 0o022 or before.st_size > limit
                    or owner is not None and before.st_uid != owner):
                raise ValueError("idle allowance file identity invalid")
            raw = stream.read(limit + 1)
            token = lambda m: (m.st_dev, m.st_ino, m.st_size, m.st_mtime_ns, m.st_ctime_ns, m.st_mode, m.st_uid)
            if (len(raw) > limit or token(before) != token(os.fstat(stream.fileno()))
                    or token(before) != token(os.stat(path.name, dir_fd=parent, follow_symlinks=False))):
                raise ValueError("idle allowance file changed")
            return raw
    finally:
        os.close(parent)


def layout(home, base, commit, files, *, config_home=None, state_home=None):
    if not HEX.fullmatch(commit) or MODULE not in files:
        raise ValueError("idle allowance release invalid")
    config_home = Path(config_home) if config_home is not None else Path(home) / ".config"
    state_home = Path(state_home) if state_home is not None else Path(home) / ".local/state"
    for path in (home, base, config_home, state_home):
        if not Path(path).is_absolute() or not re.fullmatch(r"[A-Za-z0-9_./ -]+", str(path)) or ".." in Path(path).parts:
            raise ValueError("idle allowance requires simple absolute paths")
    runtime = Path(home) / ".local/share/agiw-runtimes" / ("idle-analysis-observer-" + commit)
    config = {"home": str(home), "base": str(base), "commit": commit,
              "configHome": str(config_home), "stateHome": str(state_home),
              "files": {name: digest(raw) for name, raw in files.items()}}
    return runtime, config


def unit_bytes(runtime):
    return ("[Unit]\nDescription=AGIW bounded local static error analysis with explicit idle gates\n\n"
            '[Service]\nType=oneshot\nWorkingDirectory="' + str(runtime) + '"\n'
            'ExecStart=/usr/bin/python3 -I -B "' + str(runtime / "service-launcher.py") + '"\n'
            "Nice=19\nIOSchedulingClass=idle\nCPUQuota=10%\nMemoryMax=256M\nTasksMax=16\n"
            "TimeoutStartSec=35\nUMask=0077\nNoNewPrivileges=yes\nRestrictAddressFamilies=AF_UNIX\n"
            "ProtectSystem=strict\nProtectHome=read-only\n"
            "ReadWritePaths=/srv/storage/work/agiw-idle-analysis-v1\n"
            "ProtectKernelTunables=yes\nProtectControlGroups=yes\nPrivateTmp=yes\n").encode()


def launcher_bytes(runtime, config, module):
    # All dynamic bytes below are re-derived by the installer from signed blobs.
    return ('''import pathlib,hashlib,os,stat,errno,socket,json
status=pathlib.Path('/proc/self/status').read_text();assert 'NoNewPrivs:\\t1' in status
for family in (socket.AF_INET,socket.AF_INET6):
 try:s=socket.socket(family,socket.SOCK_STREAM)
 except OSError as e:assert e.errno in (errno.EAFNOSUPPORT,errno.EPERM,errno.EACCES)
 else:s.close();raise SystemExit('IP_NETWORK_POLICY_NOT_ENFORCED')
''' + f"root=pathlib.Path({str(runtime)!r})\np=root/'analyzer.py'\n" + '''assert all(not x.is_symlink() for x in (p,*p.parents))
fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW);m=os.fstat(fd);b=os.read(fd,1048576);os.close(fd)
''' + f"assert stat.S_ISREG(m.st_mode) and m.st_uid==os.getuid() and not(m.st_mode&0o022) and hashlib.sha256(b).hexdigest()=={PINS['analyzer.py']!r}\n" + '''ns={'__name__':'trusted_idle_analyzer','__file__':str(p)};exec(compile(b,str(p),'exec'),ns)
hp=root/'passive_attestation.py';hb=ns['checked_read'](hp,owner=os.getuid())
''' + f"assert hashlib.sha256(hb).hexdigest()=={PINS['passive_attestation.py']!r}\n" + '''h={'__name__':'trusted_passive_attestation','__file__':str(hp)};exec(compile(hb,str(hp),'exec'),h)
op=root/'observer_allowance.py';ob=ns['checked_read'](op,owner=os.getuid())
''' + f"assert hashlib.sha256(ob).hexdigest()=={digest(module)!r}\n" + '''o={'__name__':'trusted_observer_allowance','__file__':str(op)};exec(compile(ob,str(op),'exec'),o)
''' + f"config=json.loads({json.dumps(config, sort_keys=True)!r})\n" + '''def gate(proc=pathlib.Path('/proc'),uid=None,own_pid=None):
 try:return o['process_gate'](config,h['process_gate'],proc,uid,own_pid,idle_comms=ns['IDLE_COMMS'])
 except Exception:raise ns['Refused']('OBSERVER_OR_PASSIVE_ATTESTATION_UNKNOWN') from None
ns['process_gate']=gate
import sys
sys.argv=[str(p),'--run']
raise SystemExit(ns['main']())
''').encode()


def bundle(home, base, commit, files, baseline, *, config_home=None, state_home=None):
    """Return reviewable successor files; caller writes only a staging directory.

    baseline must supply all four exact historical runtime files. This function
    never treats a declaration of compatibility as evidence and never installs.
    """
    if set(baseline) != set(PINS) or any(digest(baseline[name]) != pin for name, pin in PINS.items()):
        raise ValueError("idle analyzer baseline pin mismatch")
    runtime, config = layout(home, base, commit, files, config_home=config_home, state_home=state_home)
    return {**baseline, "observer_allowance.py": files[MODULE],
            "service-launcher.py": launcher_bytes(runtime, config, files[MODULE]),
            IDLE_UNIT: unit_bytes(runtime)}


def properties(unit, names):
    result = subprocess.run(["/usr/bin/systemctl", "--user", "show", unit,
                             "--property=" + ",".join(names)], capture_output=True,
                            text=True, timeout=2, check=False)
    if result.returncode or len(result.stdout) > 16384:
        raise ValueError("idle allowance unit visibility unknown")
    found = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in names or key in found:
            raise ValueError("idle allowance unit schema unknown")
        found[key] = value
    if set(found) != set(names):
        raise ValueError("idle allowance unit properties missing")
    return found


def verify_pairing(home, base, commit, files, *, config_home=None, state_home=None):
    """Called only after install.sh verifies selected commit signature and blobs."""
    runtime, config = layout(home, base, commit, files, config_home=config_home, state_home=state_home)
    expected = {"observer_allowance.py": files[MODULE],
                "service-launcher.py": launcher_bytes(runtime, config, files[MODULE])}
    for name, pin in PINS.items():
        if digest(read_file(runtime / name, owner=os.getuid())) != pin:
            raise ValueError("idle analyzer baseline pin mismatch")
    for name, raw in expected.items():
        if read_file(runtime / name, owner=os.getuid()) != raw:
            raise ValueError("idle analyzer observer pairing mismatch")
    fragment = Path(config["configHome"]) / "systemd/user" / IDLE_UNIT
    if read_file(fragment, owner=os.getuid()) != unit_bytes(runtime):
        raise ValueError("idle analyzer unit bytes mismatch")
    names = ("LoadState", "ActiveState", "FragmentPath", "DropInPaths", "NeedDaemonReload", "ExecStart")
    result = properties(IDLE_UNIT, names)
    prefix = "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I -B " + str(runtime / "service-launcher.py") + " ; ignore_errors=no ;"
    if (result["LoadState"] != "loaded" or result["ActiveState"] != "inactive"
            or result["FragmentPath"] != str(fragment) or result["DropInPaths"]
            or result["NeedDaemonReload"] != "no" or not result["ExecStart"].startswith(prefix)
            or result["ExecStart"].count("{") != 1 or result["ExecStart"].count("}") != 1
            or not result["ExecStart"].endswith(" }")):
        raise ValueError("effective idle analyzer pairing mismatch")


def observer_identity(config, proc=Path('/proc')):
    """Read only the observer's own unit, exact code, and bounded kernel metadata."""
    home, base = Path(config['home']), Path(config['base'])
    commit = config['commit']
    if not HEX.fullmatch(commit) or os.readlink(base / 'current') != commit:
        raise ValueError('observer release mismatch')
    # No manifest is trusted: this hash map is embedded in the pinned launcher.
    for relative, pin in config['files'].items():
        if digest(read_file(base / commit / relative, owner=os.getuid())) != pin:
            raise ValueError('observer bytes mismatch')
    expected_unit = read_file(base / commit / OBSERVER_UNIT, owner=os.getuid()).decode()
    expected_unit = expected_unit.replace('%h/.local/share/agiw-linux-observer', str(base))
    expected_unit = expected_unit.replace('%h/.local/state', config['stateHome'])
    if read_file(Path(config['configHome']) / 'systemd/user' / OBSERVER_UNIT, owner=os.getuid()) != expected_unit.encode():
        raise ValueError('observer service bytes mismatch')
    actual_files = set()
    for path in (base / commit).rglob('*'):
        if path.is_symlink():
            raise ValueError('observer unexpected symlink')
        if path.is_dir():
            if path != base / commit / 'agiw_linux':
                raise ValueError('observer unexpected import directory')
        else:
            actual_files.add(str(path.relative_to(base / commit)))
    if actual_files != set(config['files']) | {'install-manifest.json'}:
        raise ValueError('observer unexpected release files')
    names = ('MainPID', 'ActiveState', 'SubState', 'FragmentPath', 'DropInPaths',
             'ControlGroup', 'InvocationID', 'NeedDaemonReload')
    unit = properties(OBSERVER_UNIT, names)
    group = f'/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{OBSERVER_UNIT}'
    if (unit['ActiveState'] != 'active' or unit['SubState'] != 'running'
            or unit['FragmentPath'] != str(Path(config['configHome']) / 'systemd/user' / OBSERVER_UNIT)
            or unit['DropInPaths'] or unit['NeedDaemonReload'] != 'no'
            or unit['ControlGroup'] != group or not re.fullmatch('[0-9a-f]{32}', unit['InvocationID'])
            or not unit['MainPID'].isdigit() or int(unit['MainPID']) <= 1):
        raise ValueError('observer unit identity mismatch')
    pid = int(unit['MainPID'])
    root = proc / str(pid)
    if root.stat().st_uid != os.getuid():
        raise ValueError('observer uid mismatch')
    def kernel(name):
        with (root / name).open('rb') as stream:
            value = stream.read(8193)
        if len(value) > 8192:
            raise ValueError('observer metadata oversized')
        return value
    raw_stat = kernel('stat')
    head, sep, tail = raw_stat.rpartition(b') ')
    fields = tail.split()
    if not sep or not head.startswith((str(pid) + ' (').encode()) or len(fields) < 20 or fields[0] not in (b'S', b'R'):
        raise ValueError('observer process identity unknown')
    expected = b'\0'.join((b'/usr/bin/python3', b'-I', b'-B',
        str(base / 'current/agiw-linux-status').encode(), b'--serve')) + b'\0'
    status = dict(line.split(b':', 1) for line in kernel('status').splitlines() if b':' in line)
    if (kernel('cmdline') != expected or kernel('cgroup') != ('0::' + group + '\n').encode()
            or status.get(b'Uid', b'').split() != [str(os.getuid()).encode()] * 4
            or status.get(b'NoNewPrivs', b'').strip() != b'1'
            or status.get(b'CapEff', b'').strip() != b'0000000000000000'):
        raise ValueError('observer metadata mismatch')
    interpreter = Path('/usr/bin/python3').resolve(strict=True)
    if os.readlink(root / 'exe') != str(interpreter):
        raise ValueError('observer interpreter mismatch')
    executable, trusted = os.stat(root / 'exe'), interpreter.stat()
    if trusted.st_uid != 0 or trusted.st_mode & 0o022 or (executable.st_dev, executable.st_ino) != (trusted.st_dev, trusted.st_ino):
        raise ValueError('observer interpreter identity mismatch')
    return pid, fields[19], unit['InvocationID'], executable.st_dev, executable.st_ino


def observer_absence():
    """Only explicit manager evidence permits the unmodified historical gate."""
    unit = properties(OBSERVER_UNIT, ('LoadState', 'ActiveState', 'SubState', 'MainPID'))
    if (unit['LoadState'] not in ('loaded', 'not-found') or unit['MainPID'] != '0'
            or (unit['ActiveState'], unit['SubState']) not in (('inactive', 'dead'), ('failed', 'failed'))):
        raise ValueError('observer absence unknown')
    return unit


def process_gate(config, original_gate, proc=Path('/proc'), uid=None, own_pid=None, *, idle_comms):
    """Exclude one attested PID, or use the historical gate with no exemptions."""
    try:
        before = observer_identity(config, proc)
    except (OSError, ValueError):
        absent = observer_absence()
        original_gate(proc, uid, own_pid, idle_comms=idle_comms)
        if observer_absence() != absent:
            raise ValueError('observer changed during process gate')
        return
    class FilteredProc:
        def iterdir(self):
            return (path for path in proc.iterdir() if path.name != str(before[0]))
    original_gate(FilteredProc(), uid, own_pid, idle_comms=idle_comms)
    if observer_identity(config, proc) != before:
        raise ValueError('observer changed during process gate')
