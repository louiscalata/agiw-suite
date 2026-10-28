"""Read the local monitor's Mac and Windows summary without starting any observer."""
from __future__ import annotations

import argparse
import errno
import http.client
import io
import json
import math
import os
import re
import stat
import time
from pathlib import Path

from machine_state import FRESH_SECONDS, TRUST_LEVELS, expire_machines, machine_summary, observation_age

ENDPOINT_PATH = Path.home() / '.local/state/inference-monitor/endpoint.json'
MAX_ENDPOINT = 4096
MAX_SNAPSHOT = 1024 * 1024
MAX_MACHINES = 65
TIMEOUT = 2.0
# Linux stays readable when a report is present. It is not required for a summary.
REQUIRED_PLATFORMS = ('mac', 'windows')
PLATFORMS = {'mac': 'mac', 'windows': 'windows', 'linux': 'linux'}
HOST_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z')


class StatusUnavailable(ValueError):
    """A fixed reason code, never response text or a local path."""


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _json(raw):
    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('non-finite number')
        return result

    def invalid_constant(value):
        raise ValueError('non-finite number')

    try:
        data = json.loads(raw, parse_float=finite, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise StatusUnavailable('non_json') from None
    pending = [(data, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise StatusUnavailable('schema_invalid')
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return data


def _endpoint_port(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                    or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
                raise StatusUnavailable('untrusted')
            if info.st_size > MAX_ENDPOINT:
                raise StatusUnavailable('oversize')
            raw = os.read(fd, MAX_ENDPOINT + 1)
            if len(raw) > MAX_ENDPOINT:
                raise StatusUnavailable('oversize')
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StatusUnavailable('untrusted') from None
        raise StatusUnavailable('unavailable') from None
    data = _json(raw)
    if not isinstance(data, dict):
        raise StatusUnavailable('schema_invalid')
    port = data.get('port')
    if type(port) is not int or not 1 <= port <= 65535:
        raise StatusUnavailable('schema_invalid')
    if data.get('host', '127.0.0.1') != '127.0.0.1':
        raise StatusUnavailable('untrusted')
    if 'pid' in data and (type(data['pid']) is not int or data['pid'] <= 0):
        raise StatusUnavailable('schema_invalid')
    if 'startedUnix' in data and (not _number(data['startedUnix']) or data['startedUnix'] < 0):
        raise StatusUnavailable('schema_invalid')
    return port


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


class _DeadlineReader(io.RawIOBase):
    def __init__(self, raw, sock, deadline):
        self.raw = raw
        self.sock = sock
        self.deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_remaining(self.deadline))
        count = self.raw.readinto(buffer)
        _remaining(self.deadline)
        return count

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline

    def makefile(self, mode):
        # Enforce the deadline beneath HTTP parsing, including headers and chunk framing.
        raw = self.sock.makefile(mode, buffering=0)
        return io.BufferedReader(_DeadlineReader(raw, self.sock, self.deadline))


def _snapshot(port, timeout):
    deadline = time.monotonic() + timeout
    # HTTPConnection ignores proxy settings and cannot follow a redirect to a remote host.
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    connection.response_class = lambda sock, **kwargs: http.client.HTTPResponse(
        _DeadlineSocket(sock, deadline), **kwargs)
    response = None
    try:
        connection.timeout = _remaining(deadline)
        connection.connect()
        connection.sock.settimeout(_remaining(deadline))
        connection.request('GET', '/api/snapshot', headers={'Accept': 'application/json'})
        _remaining(deadline)
        response = connection.getresponse()
        _remaining(deadline)
        if response.status != 200:
            raise StatusUnavailable('unavailable')
        if response.getheader('Content-Encoding', 'identity') != 'identity':
            raise StatusUnavailable('non_json')
        length = response.getheader('Content-Length')
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                raise StatusUnavailable('schema_invalid') from None
            if size < 0:
                raise StatusUnavailable('schema_invalid')
            if size > MAX_SNAPSHOT:
                raise StatusUnavailable('oversize')
        raw = response.read(MAX_SNAPSHOT + 1)
        _remaining(deadline)
        if len(raw) > MAX_SNAPSHOT:
            raise StatusUnavailable('oversize')
        return _json(raw)
    except TimeoutError:
        raise StatusUnavailable('timeout') from None
    except ConnectionRefusedError:
        raise StatusUnavailable('refused') from None
    except (OSError, http.client.HTTPException):
        raise StatusUnavailable('unavailable') from None
    finally:
        if response is not None:
            response.close()
        connection.close()


def _unknown(reason, now):
    return {'schemaVersion': 1, 'sampledAt': None, 'machines': [
        machine_summary(platform, platform, now=now, reason=reason)
        for platform in REQUIRED_PLATFORMS]}


def _summary(data, now):
    if (not isinstance(data, dict) or type(data.get('schemaVersion')) is not int
            or data['schemaVersion'] != 1 or not _number(data.get('sampledAt'))):
        raise StatusUnavailable('schema_invalid')
    age = now - data['sampledAt']
    if age < -2:
        raise StatusUnavailable('clock_skew')
    machines = data.get('machines')
    if not isinstance(machines, list) or not len(REQUIRED_PLATFORMS) <= len(machines) <= MAX_MACHINES:
        raise StatusUnavailable('schema_invalid')
    seen = set()
    result = []
    for row in machines:
        if not isinstance(row, dict):
            raise StatusUnavailable('schema_invalid')
        host = row.get('host')
        platform = row.get('platform')
        if platform == 'macos':
            platform = 'mac'
        if (not isinstance(host, str) or not HOST_NAME.fullmatch(host)
                or not isinstance(platform, str) or platform not in FRESH_SECONDS
                or (platform, host) in seen):
            raise StatusUnavailable('schema_invalid')
        seen.add((platform, host))
        if (row.get('state') not in ('up', 'down', 'unknown')
                or not isinstance(row.get('reasonCode'), str) or not row['reasonCode']):
            raise StatusUnavailable('schema_invalid')
        trust = row.get('trust')
        if not isinstance(trust, str) or trust not in TRUST_LEVELS:
            raise StatusUnavailable('schema_invalid')
        if row['state'] != 'unknown' and trust == 'unverified':
            raise StatusUnavailable('untrusted')
        row_age = row.get('ageSeconds')
        if row_age is not None and (not _number(row_age) or row_age < 0):
            raise StatusUnavailable('schema_invalid')
        if row['state'] != 'unknown' and row_age is None:
            raise StatusUnavailable('schema_invalid')
        for key in ('loadedCount', 'busyCount', 'promptTokensPerSecond', 'generationTokensPerSecond'):
            value = row.get(key)
            if value is not None and (not _number(value) or value < 0
                                      or (key.endswith('Count') and type(value) is not int)):
                raise StatusUnavailable('schema_invalid')
            if row['state'] == 'unknown' and value is not None:
                raise StatusUnavailable('schema_invalid')
        current_age = (observation_age(row, now) if row.get('observedAt') is not None
                       else row_age + max(0, age) if row_age is not None else None)
        if row['state'] != 'unknown' and current_age is None:
            raise StatusUnavailable('schema_invalid')
        # Legacy rows carry age at sample time; current rows retain their original timestamp.
        projected = {**row, 'ageSeconds': current_age}
        if row.get('modelsComplete') is not True:
            projected.update(modelsComplete=False, loadedCount=None, busyCount=None)
        if row['state'] == 'unknown':
            projected.update(handsOff=None)
        result.extend(expire_machines([projected], now))
    seen_platforms = {platform for platform, _host in seen}
    if ('mac', 'mac') not in seen or not set(REQUIRED_PLATFORMS) <= seen_platforms:
        raise StatusUnavailable('schema_invalid')
    return {'schemaVersion': 1, 'sampledAt': data['sampledAt'], 'machines': result}


def summary(endpoint_path=None, timeout=TIMEOUT, now=None):
    """Return the snapshot's machines[], or explicit unknown rows after a failed read."""
    try:
        port = _endpoint_port(ENDPOINT_PATH if endpoint_path is None else Path(endpoint_path))
        data = _snapshot(port, timeout)
        return _summary(data, time.time() if now is None else now)
    except StatusUnavailable as exc:
        return _unknown(str(exc), time.time() if now is None else now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='print the Mac and Windows summary as JSON')
    parser.add_argument('--endpoint', type=Path, help='read a different local endpoint.json')
    args = parser.parse_args(argv)
    data = summary(args.endpoint)
    if args.json:
        print(json.dumps(data, allow_nan=False, separators=(',', ':')))
    else:
        for row in data['machines']:
            print(f"{row['host']}: {row['state']} ({row['reasonCode']})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
