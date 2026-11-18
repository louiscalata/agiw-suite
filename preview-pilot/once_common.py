"""Shared preflight and process helpers for the owned one-shot CPU smokes.

This module never starts a server or calls a model by itself. The caller owns
the ephemeral llama-server process and supplies explicit model/server paths.
"""
import argparse
import datetime
import hashlib
import os
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / 'preview-pilot'
EVIDENCE = PILOT / 'evidence'
TINY_MARKER = 'SmolLM2-135M'

if str(ROOT) not in __import__('sys').path:
    __import__('sys').path.insert(0, str(ROOT))
from agiw.fleet_local_pilot import clean_env, execute, get  # noqa: E402


class Refused(RuntimeError):
    pass


def parse_args(program, description):
    """Model and server paths are operator-supplied or come from named env vars."""
    p = argparse.ArgumentParser(description=description)
    p.add_argument('--model-path', default=None)
    p.add_argument('--llama-server', default=None)
    p.add_argument('--allow-tiny-model', action='store_true')
    p.add_argument('--rss-cap-mb', type=int, default=4096)
    p.add_argument('--program', default=program)
    args = p.parse_args()
    if not args.model_path and not os.environ.get('AGIW_PILOT_MODEL_PATH'):
        p.error('--model-path or AGIW_PILOT_MODEL_PATH is required')
    if not args.llama_server and not os.environ.get('AGIW_LLAMA_SERVER'):
        p.error('--llama-server or AGIW_LLAMA_SERVER is required')
    return args


def model_metadata(value, allow_tiny=False):
    """Require an explicit model file and record basename, size and sha256."""
    raw = value or os.environ.get('AGIW_PILOT_MODEL_PATH')
    if not raw:
        raise Refused('MODEL_PATH_REQUIRED')
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise Refused('MODEL_PATH_NOT_FILE')
    name = path.name
    if TINY_MARKER in name and not allow_tiny:
        raise Refused('TINY_MODEL_REQUIRES_ALLOW_TINY_MODEL')
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return {'basename': name, 'size_bytes': path.stat().st_size,
            'sha256': digest.hexdigest(), 'path': str(path)}


def resolve_server(value=None):
    raw = value or os.environ.get('AGIW_LLAMA_SERVER')
    if not raw:
        raise Refused('LLAMA_SERVER_REQUIRED')
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise Refused('LLAMA_SERVER_NOT_FILE')
    return str(path)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def started_utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def evidence_path(stem, suffix):
    EVIDENCE.mkdir(mode=0o700, parents=True, exist_ok=True)
    return EVIDENCE / ('%s-%d.%s' % (stem, time.time_ns(), suffix))


def timestamped_marker(stem, text):
    path = evidence_path(stem, 'marker')
    with path.open('x') as handle:
        handle.write(text)
    return str(path)


def start_server(exe, model, port, tag):
    log = evidence_path(tag + '-server', 'log').open('x')
    cmd = [exe, '-m', str(model), '--alias', 'agiw-owner-smoke',
           '--host', '127.0.0.1', '--port', str(port), '--device', 'none',
           '-ngl', '0', '--no-op-offload', '-t', '1', '-tb', '1',
           '--threads-http', '1', '-np', '1', '-c', '1024', '--slots',
           '--offline', '--no-warmup', '--no-webui', '--no-context-shift']
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            env=clean_env(), cwd=str(PILOT), start_new_session=True)
    return proc, log


def wait_health(port, proc, record, cap_mb):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError('SERVER_EXIT_' + str(proc.returncode))
        rss = int(subprocess.check_output(['/bin/ps', '-o', 'rss=', '-p', str(proc.pid)],
                                          text=True).strip())
        record['peakSampledRssKB'] = max(record.get('peakSampledRssKB', 0), rss)
        if rss > cap_mb * 1024:
            raise RuntimeError('MEMORY_CAP')
        try:
            if get('http://127.0.0.1:%d/health' % port).get('status') == 'ok':
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError('STARTUP_TIMEOUT')


def get_health(port):
    base = 'http://127.0.0.1:%d' % port
    return {'health': get(base + '/health'), 'models': get(base + '/v1/models'),
            'slotsBefore': get(base + '/slots')}


def reap(proc, record):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if proc is not None:
        record['ownedProcessExitCode'] = proc.returncode
        record['ownedProcessReaped'] = proc.poll() is not None


def write_result(tag, record):
    path = evidence_path(tag + '-result', 'json')
    path.write_text(__import__('json').dumps(record, indent=2) + '\n')
    print(__import__('json').dumps(record, indent=2))
    return str(path)
