"""Explicit, loopback-only MCP connector registry for the AGIW observer.

OpenCode discovery is metadata-only. Managed endpoints are AGIW's own test
registry: saving or testing one never edits OpenCode, grants a model tool,
launches a command, or invokes tools/call. A test speaks the 2025-11-25 MCP
Streamable HTTP lifecycle to an explicitly saved IPv4 loopback endpoint.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import threading
import time
from urllib.parse import urlsplit

CONFIG_NAME = 'tool-connectors.json'
MAX_CONFIG_BYTES = 4096
MAX_OPEN_CODE_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CONNECTORS = 8
TEST_RESULT_MAX_AGE_SECONDS = 60
PROTOCOL_VERSION = '2025-11-25'
SUPPORTED_VERSIONS = frozenset(('2025-11-25', '2025-06-18', '2025-03-26'))
_ID = re.compile(r'[a-z][a-z0-9-]{0,31}\Z')
_DISCOVERED_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
_TOOL_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_PATH = re.compile(r'/[A-Za-z0-9._~/-]{0,127}\Z')
_INCARNATION = re.compile(r'[0-9a-f]{32}\Z')


class ConnectorError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _unique(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('duplicate JSON field')
        value[key] = item
    return value


def _strict_json(raw: bytes):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
    except RecursionError as exc:
        raise ValueError('JSON nesting exceeds the safe parser limit') from exc


def _jsonc(raw: bytes):
    """Parse only JSON-with-comments/trailing-commas, without evaluating JS."""
    source = raw.decode('utf-8')
    out = []
    quoted = escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif char == '/' and source[index:index + 2] == '//':
            while index < len(source) and source[index] not in '\r\n':
                index += 1
            continue
        elif char == '/' and source[index:index + 2] == '/*':
            end = source.find('*/', index + 2)
            if end < 0:
                raise ValueError('unterminated comment')
            index = end + 2
            continue
        else:
            out.append(char)
        index += 1
    if quoted:
        raise ValueError('unterminated string')
    source = ''.join(out)
    out = []
    quoted = escaped = False
    for index, char in enumerate(source):
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        if char == ',':
            following = index + 1
            while following < len(source) and source[following].isspace():
                following += 1
            if following < len(source) and source[following] in '}]':
                continue
        out.append(char)
    return _strict_json(''.join(out).encode('utf-8'))


def _read_owner_file(path: Path, limit: int, *, private: bool) -> bytes:
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or before.st_nlink != 1 or before.st_mode & (0o077 if private else 0o022)
            or before.st_size > limit):
        raise ValueError('unsafe file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('file changed')
        chunks = []
        total = 0
        while True:
            block = os.read(fd, min(8192, limit + 1 - total))
            if not block:
                break
            total += len(block)
            if total > limit:
                raise ValueError('file too large')
            chunks.append(block)
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_size,
                opened.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns,
                                        after.st_size, after.st_ctime_ns):
            raise ValueError('file changed')
        return b''.join(chunks)
    finally:
        os.close(fd)


def _loopback_url(value: object) -> str:
    if type(value) is not str or len(value) > 180 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ConnectorError('INVALID_URL', 'Use an HTTP URL on 127.0.0.1 with a port.')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConnectorError('INVALID_URL', 'Use an HTTP URL on 127.0.0.1 with a port.') from exc
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or parsed.username is not None or parsed.password is not None
            or '?' in value or '#' in value or not 1024 <= (port or 0) <= 65535
            or parsed.netloc != f'127.0.0.1:{port}'
            or not _PATH.fullmatch(parsed.path)
            or '//' in parsed.path or any(part in ('.', '..') for part in parsed.path.split('/'))):
        raise ConnectorError('INVALID_URL', 'Use an HTTP URL on 127.0.0.1 with a port.')
    return value


def _safe_parent(path: Path, *, create: bool) -> None:
    home = Path.home()
    chain = [path.parent]
    while chain[-1] != home and home in chain[-1].parents:
        chain.append(chain[-1].parent)
    for directory in reversed(chain):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            if not create:
                raise
            directory.mkdir(mode=0o700)
            info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or directory == path.parent and info.st_mode & 0o077):
            raise ValueError('unsafe directory')


def read_private(path: Path, limit: int) -> bytes:
    """Read an owner-only regular file without following its final component."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o077 or before.st_nlink != 1
                or before.st_size > limit):
            raise ValueError('unsafe private file')
        raw = bytearray()
        while len(raw) <= limit:
            block = os.read(fd, min(8192, limit + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(fd)
        identity = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns',
                    'st_uid', 'st_mode', 'st_nlink')
        if len(raw) > limit or any(getattr(before, field) != getattr(after, field)
                                   for field in identity):
            raise ValueError('private file changed')
        return bytes(raw)
    finally:
        os.close(fd)


def write_private(path: Path, value: dict) -> None:
    """Atomically replace an owner-only JSON record within a private directory."""
    if path.exists() or path.is_symlink():
        read_private(path, MAX_CONFIG_BYTES)
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError('private record too large')
    temporary = path.with_name(path.name + '.tmp-' + os.urandom(8).hex())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def locked(directory: Path, name: str):
    """Serialize registry changes across observer processes."""
    fd = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError('unsafe connector lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _load_managed(path: Path) -> list[dict]:
    try:
        _safe_parent(path, create=False)
        raw = read_private(path, MAX_CONFIG_BYTES)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise ConnectorError('CONFIG_UNSAFE', 'Saved connector settings cannot be read safely.', 503) from exc
    try:
        value = _strict_json(raw)
        if (type(value) is not dict or set(value) != {'schemaVersion', 'managed'}
                or type(value['schemaVersion']) is not int or value['schemaVersion'] not in (1, 2)
                or type(value['managed']) is not list or len(value['managed']) > MAX_CONNECTORS):
            raise ValueError('schema')
        fields = {'id', 'url'} if value['schemaVersion'] == 1 else {'id', 'url', 'incarnation'}
        seen = set()
        for row in value['managed']:
            if (type(row) is not dict or set(row) != fields
                    or type(row['id']) is not str or not _ID.fullmatch(row['id'])
                    or row['id'] in seen):
                raise ValueError('row')
            if value['schemaVersion'] == 2 and (type(row['incarnation']) is not str
                    or not _INCARNATION.fullmatch(row['incarnation'])):
                raise ValueError('incarnation')
            _loopback_url(row['url'])
            seen.add(row['id'])
        return value['managed']
    except (UnicodeError, ValueError, ConnectorError) as exc:
        raise ConnectorError('CONFIG_INVALID', 'Saved connector settings are invalid.', 503) from exc


def _incarnated(rows: list[dict]) -> list[dict]:
    """Upgrade validated legacy rows; a new identity fences previous tests."""
    return [row if 'incarnation' in row else
            {**row, 'incarnation': secrets.token_hex(16)} for row in rows]


def _save_managed(path: Path, rows: list[dict]) -> None:
    try:
        if any(type(row.get('incarnation')) is not str
               or not _INCARNATION.fullmatch(row['incarnation']) for row in rows):
            raise ValueError('missing incarnation')
        _safe_parent(path, create=True)
        write_private(path, {'schemaVersion': 2, 'managed': rows})
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        raise ConnectorError('SAVE_FAILED', 'Connector settings could not be saved safely.', 503) from exc


def _opencode_discovery(path: Path) -> tuple[dict, list[dict]]:
    try:
        # OpenCode also accepts JSONC. Inspect one global file only; project,
        # custom and managed sources may change the effective client config.
        if path.name == 'opencode.json':
            try:
                path.lstat()
            except FileNotFoundError:
                path = path.with_name('opencode.jsonc')
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise ValueError('unsafe directory')
        value = _jsonc(_read_owner_file(path, MAX_OPEN_CODE_BYTES, private=False))
        entries = value.get('mcp', {}) if type(value) is dict else None
        if type(entries) is not dict or len(entries) > 64:
            raise ValueError('invalid mcp config')
        rows = []
        for name, config in entries.items():
            if type(name) is not str or not _DISCOVERED_ID.fullmatch(name):
                continue
            kind = config.get('type') if type(config) is dict else None
            if kind == 'local':
                command = config.get('command')
                shape = (type(command) is list and 1 <= len(command) <= 32
                         and all(type(arg) is str and 0 < len(arg) <= 1024 for arg in command))
            elif kind == 'remote':
                shape = type(config.get('url')) is str and 0 < len(config['url']) <= 2048
            else:
                shape = False
            valid = shape and type(config.get('enabled', True)) is bool
            rows.append({'id': 'opencode:' + name, 'source': 'opencode',
                         'transport': config.get('type') if valid else 'unknown',
                         'configured': valid,
                         'enabledInOpenCode': config.get('enabled', True) if valid else None,
                         'agentPermission': 'unknown',
                         'transportTest': {'state': 'not-tested'},
                         'detailCode': 'CONFIGURED_ONLY' if valid else 'INVALID_ENTRY'})
        return {'state': 'readable', 'scope': 'global-file-only'}, rows
    except FileNotFoundError:
        return {'state': 'missing'}, []
    except (OSError, ValueError, UnicodeError):
        return {'state': 'unavailable', 'code': 'OPEN_CODE_CONFIG_UNSAFE'}, []


def _mcp_result(body: bytes, request_id: int) -> dict:
    try:
        value = _strict_json(body)
    except (ValueError, UnicodeError) as exc:
        raise ConnectorError('INVALID_MCP_RESPONSE', 'MCP returned invalid JSON.', 502) from exc
    if (type(value) is not dict or value.get('jsonrpc') != '2.0'
            or type(value.get('id')) is not int or value['id'] != request_id):
        raise ConnectorError('INVALID_MCP_RESPONSE', 'MCP response ID or envelope is invalid.', 502)
    if 'error' in value:
        raise ConnectorError('MCP_ERROR', 'MCP rejected the verification request.', 502)
    if type(value.get('result')) is not dict:
        raise ConnectorError('INVALID_MCP_RESPONSE', 'MCP returned no result object.', 502)
    return value['result']


def _sse_body(response, request_id: int, budget: dict) -> tuple[bytes | None, str | None, int]:
    received = bytearray()
    at_stream_start = True
    last_id = None
    retry_ms = 0
    while budget['remaining'] >= 0 and budget['events'] < 64:
        chunk = response.read1(min(4096, budget['remaining'] + 1))
        if not chunk:
            if last_id is not None:
                return None, last_id, retry_ms
            raise ConnectorError('SSE_INCOMPLETE', 'MCP stream closed before its response.', 502)
        budget['remaining'] -= len(chunk)
        received.extend(chunk)
        if budget['remaining'] < 0:
            break
        if at_stream_start:
            if bytes(received) in (b'\xef', b'\xef\xbb'):
                continue
            if received.startswith(b'\xef\xbb\xbf'):
                del received[:3]
            at_stream_start = False
        while True:
            data = bytes(received).replace(b'\r\n', b'\n').replace(b'\r', b'\n')
            ending = data.find(b'\n\n')
            if ending < 0:
                break
            event, remaining = data[:ending], data[ending + 2:]
            received[:] = remaining
            budget['events'] += 1
            pieces = []
            for line in event.split(b'\n'):
                if line.startswith(b'data:'):
                    pieces.append(line[5:].lstrip(b' '))
                elif line.startswith(b'id:'):
                    candidate = line[3:].lstrip(b' ')
                    if (not 0 < len(candidate) <= 128
                            or not all(33 <= char <= 126 for char in candidate)):
                        raise ConnectorError('INVALID_EVENT_ID', 'MCP stream event ID is invalid.', 502)
                    last_id = candidate.decode('ascii')
                elif line.startswith(b'retry:'):
                    candidate = line[6:].strip()
                    if not candidate.isdigit() or len(candidate) > 4 or int(candidate) > 1000:
                        raise ConnectorError('SSE_RETRY_UNSUPPORTED', 'MCP stream retry exceeds the test limit.', 502)
                    retry_ms = int(candidate)
            if not pieces or not any(pieces):
                continue
            payload = b'\n'.join(pieces)
            try:
                value = _strict_json(payload)
            except (ValueError, UnicodeError) as exc:
                raise ConnectorError('INVALID_MCP_RESPONSE', 'MCP stream contained invalid JSON.', 502) from exc
            if type(value) is not dict or value.get('jsonrpc') != '2.0':
                raise ConnectorError('INVALID_MCP_RESPONSE', 'MCP stream envelope is invalid.', 502)
            if 'id' in value:
                if value['id'] != request_id:
                    raise ConnectorError('INTERACTIVE_SERVER_UNSUPPORTED', 'MCP server requested an unsupported interaction.', 502)
                return payload, None, 0
            if not (type(value.get('method')) is str and value['method'].startswith('notifications/')):
                raise ConnectorError('INTERACTIVE_SERVER_UNSUPPORTED', 'MCP server requested an unsupported interaction.', 502)
    raise ConnectorError('MCP_RESPONSE_TOO_LARGE', 'MCP response exceeded the test limit.', 502)


def _http_exchange(url: str, payload: dict | None, *, session: str | None = None,
                   version: str | None = None, method: str = 'POST', request_id: int | None = None,
                   stopping: threading.Event | None = None, active: set | None = None,
                   active_lock: threading.Lock | None = None, last_event_id: str | None = None,
                   sse_budget: dict | None = None) -> tuple[int, bytes, str | None]:
    """Direct AF_INET socket: no DNS, environment proxy, redirect, or auth."""
    parsed = urlsplit(url)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.5)
    timer = None
    connection = None
    try:
        if active is not None and active_lock is not None:
            with active_lock:
                if stopping is not None and stopping.is_set():
                    raise ConnectorError('STOPPED', 'Connector test stopped.', 503)
                active.add(sock)
        elif stopping is not None and stopping.is_set():
            raise ConnectorError('STOPPED', 'Connector test stopped.', 503)
        sock.connect(('127.0.0.1', parsed.port))
        if stopping is not None and stopping.is_set():
            raise ConnectorError('STOPPED', 'Connector test stopped.', 503)
        timer = threading.Timer(2.0, lambda: _abort_socket(sock))
        timer.daemon = True
        timer.start()
        connection = http.client.HTTPConnection('127.0.0.1', parsed.port, timeout=1.5)
        connection.sock = sock
        headers = {'Accept': 'text/event-stream' if method == 'GET' else 'application/json, text/event-stream',
                   'Origin': f'http://127.0.0.1:{parsed.port}', 'Connection': 'close'}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        if session is not None:
            headers['MCP-Session-Id'] = session
        if version is not None:
            headers['MCP-Protocol-Version'] = version
        if last_event_id is not None:
            headers['Last-Event-ID'] = last_event_id
        raw = json.dumps(payload, separators=(',', ':')).encode() if payload is not None else None
        connection.request(method, parsed.path, body=raw, headers=headers)
        response = connection.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            raise ConnectorError('REDIRECT_REFUSED', 'MCP endpoint redirected the test.', 502)
        if method == 'DELETE':
            return response.status, b'', None
        if method == 'GET' and response.status == 405:
            raise ConnectorError('SSE_RESUME_UNSUPPORTED', 'MCP stream cannot resume within the test.', 502)
        if response.status != (202 if request_id is None else 200):
            raise ConnectorError('MCP_HTTP_ERROR', 'MCP endpoint rejected the test request.', 502)
        ids = response.msg.get_all('MCP-Session-Id', [])
        if len(ids) > 1:
            raise ConnectorError('INVALID_SESSION', 'MCP returned an invalid session ID.', 502)
        new_session = ids[0] if ids else None
        if new_session is not None and (len(new_session) > 128 or not all(33 <= ord(c) <= 126 for c in new_session)):
            raise ConnectorError('INVALID_SESSION', 'MCP returned an invalid session ID.', 502)
        if session is not None and new_session is not None and new_session != session:
            raise ConnectorError('INVALID_SESSION', 'MCP changed its session ID.', 502)
        if request_id is None:
            if response.read(1):
                raise ConnectorError('INVALID_MCP_RESPONSE', 'MCP notification returned a body.', 502)
            return response.status, b'', new_session
        content_types = response.msg.get_all('Content-Type', [])
        if len(content_types) != 1:
            raise ConnectorError('UNSUPPORTED_TRANSPORT', 'MCP response content type is unsupported.', 502)
        content_type = content_types[0].split(';', 1)[0].strip().lower()
        if method == 'GET' and content_type != 'text/event-stream':
            raise ConnectorError('UNSUPPORTED_TRANSPORT', 'MCP resume did not return an event stream.', 502)
        length = response.msg.get('Content-Length')
        if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
            raise ConnectorError('MCP_RESPONSE_TOO_LARGE', 'MCP response exceeded the test limit.', 502)
        if content_type == 'application/json':
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ConnectorError('MCP_RESPONSE_TOO_LARGE', 'MCP response exceeded the test limit.', 502)
        elif content_type == 'text/event-stream':
            if sse_budget is None:
                sse_budget = {'remaining': MAX_RESPONSE_BYTES, 'events': 0,
                              'resumes': 0, 'started': time.monotonic()}
            body, token, retry_ms = _sse_body(response, request_id, sse_budget)
            if body is None:
                if (sse_budget['resumes'] >= 1 or time.monotonic() - sse_budget['started'] > 4):
                    raise ConnectorError('SSE_RESUME_LIMIT', 'MCP stream exceeded the resume limit.', 502)
                sse_budget['resumes'] += 1
                if retry_ms:
                    time.sleep(retry_ms / 1000)
                if (time.monotonic() - sse_budget['started'] > 4
                        or stopping is not None and stopping.is_set()):
                    raise ConnectorError('SSE_RESUME_LIMIT', 'MCP stream exceeded the resume limit.', 502)
                # The original stream is already closed. Resume only this request;
                # a session, if issued, remains memory-only and is never logged.
                connection.close()
                _, body, resumed_session = _http_exchange(
                    url, None, session=new_session or session, version=version or PROTOCOL_VERSION,
                    method='GET', request_id=request_id, stopping=stopping,
                    active=active, active_lock=active_lock, last_event_id=token,
                    sse_budget=sse_budget)
                if (new_session is not None and resumed_session is not None
                        and new_session != resumed_session):
                    raise ConnectorError('INVALID_SESSION', 'MCP changed its session ID.', 502)
                return 200, body, resumed_session or new_session
        else:
            raise ConnectorError('UNSUPPORTED_TRANSPORT', 'MCP response content type is unsupported.', 502)
        return response.status, body, new_session
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise ConnectorError('MCP_UNREACHABLE', 'MCP endpoint did not answer within the test limit.', 502) from exc
    finally:
        if timer is not None:
            timer.cancel()
        if active is not None and active_lock is not None:
            with active_lock:
                active.discard(sock)
        if connection is not None:
            connection.close()
        else:
            sock.close()


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _abort_socket(sock):
    _shutdown(sock)
    try:
        sock.close()
    except OSError:
        pass


def test_streamable_http(url: str, *, stopping=None, active=None, active_lock=None) -> dict:
    url = _loopback_url(url)
    kwargs = {'stopping': stopping, 'active': active, 'active_lock': active_lock}
    session = None
    version = None
    try:
        init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {'protocolVersion': PROTOCOL_VERSION, 'capabilities': {},
                           'clientInfo': {'name': 'agiw-tool-connector-check', 'version': '1.0.0'}}}
        _, body, session = _http_exchange(url, init, request_id=1, **kwargs)
        result = _mcp_result(body, 1)
        offered_version = result.get('protocolVersion')
        if (type(offered_version) is not str or offered_version not in SUPPORTED_VERSIONS
                or type(result.get('capabilities')) is not dict):
            raise ConnectorError('UNSUPPORTED_VERSION', 'MCP version or capabilities are unsupported.', 502)
        version = offered_version
        if type(result['capabilities'].get('tools')) is not dict:
            raise ConnectorError('NO_TOOLS_CAPABILITY', 'MCP server does not offer tools.', 502)
        _, _, observed_session = _http_exchange(
            url, {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            session=session, version=version, **kwargs)
        if observed_session is not None and observed_session != session:
            raise ConnectorError('INVALID_SESSION', 'MCP changed its session ID.', 502)
        count = 0
        seen = set()
        cursor = None
        for page in range(4):
            params = {} if cursor is None else {'cursor': cursor}
            _, body, observed_session = _http_exchange(
                url, {'jsonrpc': '2.0', 'id': page + 2,
                      'method': 'tools/list', 'params': params},
                session=session, version=version, request_id=page + 2, **kwargs)
            if observed_session is not None and observed_session != session:
                raise ConnectorError('INVALID_SESSION', 'MCP changed its session ID.', 502)
            listing = _mcp_result(body, page + 2)
            tools = listing.get('tools')
            if type(tools) is not list or count + len(tools) > 64:
                raise ConnectorError('INVALID_TOOL_LIST', 'MCP tool list is invalid or too large.', 502)
            for tool in tools:
                if (type(tool) is not dict or type(tool.get('name')) is not str
                        or not _TOOL_NAME.fullmatch(tool['name'])
                        or tool['name'] in seen or type(tool.get('inputSchema')) is not dict
                        or tool['inputSchema'].get('type') != 'object'):
                    raise ConnectorError('INVALID_TOOL_LIST', 'MCP tool list is invalid or too large.', 502)
                seen.add(tool['name'])
            count += len(tools)
            cursor = listing.get('nextCursor')
            if cursor is None:
                return {'state': 'ready', 'protocolVersion': version, 'toolCount': count,
                        'checkedAtUnix': round(time.time(), 3)}
            if type(cursor) is not str or not 0 < len(cursor) <= 512:
                raise ConnectorError('INVALID_TOOL_LIST', 'MCP tool cursor is invalid.', 502)
        raise ConnectorError('TOO_MANY_PAGES', 'MCP tool list exceeded the test limit.', 502)
    finally:
        if session is not None and not (stopping and stopping.is_set()):
            try:
                _http_exchange(url, None, session=session, version=version or PROTOCOL_VERSION,
                               method='DELETE', **kwargs)
            except ConnectorError:
                pass


class ToolConnectors:
    def __init__(self, config_path: Path | None = None, opencode_path: Path | None = None,
                 test_fn=None):
        self.path = config_path or Path.home() / '.local/state/inference-monitor' / CONFIG_NAME
        self.opencode_path = opencode_path or Path.home() / '.config/opencode/opencode.json'
        self.test_fn = test_fn or test_streamable_http
        self._lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active = set()
        self._stopping = threading.Event()
        self._testing = None
        self._test_cancel = None
        self._worker = None
        self._results = {}

    def read(self) -> dict:
        config_status, discovered = _opencode_discovery(self.opencode_path)
        with self._lock:
            managed = _load_managed(self.path)
            results = dict(self._results)
            testing = self._testing
        rows = []
        for item in managed:
            current = results.get(item['id'])
            identity = (item['id'], item['url'], item.get('incarnation'))
            state = (current[2] if current and current[:2] == identity[1:]
                     else {'state': 'not-tested'})
            checked_at = state.get('checkedAtUnix')
            if (state.get('state') in ('ready', 'error')
                    and (type(checked_at) not in (int, float)
                         or not 0 <= time.time() - checked_at <= TEST_RESULT_MAX_AGE_SECONDS)):
                state = {'state': 'not-tested'}
            if testing == identity:
                state = {'state': 'checking'}
            rows.append({'id': item['id'], 'source': 'agiw', 'transport': 'streamable-http',
                         'url': item['url'], 'configured': True, 'enabledInOpenCode': False,
                         'agentPermission': 'unknown', 'transportTest': state,
                         'detailCode': 'AGIW_TEST_REGISTRY_ONLY'})
        return {'schemaVersion': 1, 'opencodeConfig': config_status,
                'connectors': sorted(discovered + rows, key=lambda row: row['id'])}

    def add(self, identifier: object, url: object) -> dict:
        if type(identifier) is not str or not _ID.fullmatch(identifier):
            raise ConnectorError('INVALID_ID', 'Use a short lowercase connector ID.')
        url = _loopback_url(url)
        with self._lock:
            if self._stopping.is_set():
                raise ConnectorError('STOPPED', 'Connector registry is stopping.', 503)
            try:
                _safe_parent(self.path, create=True)
                with locked(self.path.parent, CONFIG_NAME + '.lock'):
                    rows = _load_managed(self.path)
                    old = next((row for row in rows if row['id'] == identifier), None)
                    if old is not None and old['url'] != url:
                        raise ConnectorError('ID_EXISTS', 'Disconnect this ID before using another URL.', 409)
                    if old is None:
                        if len(rows) >= MAX_CONNECTORS:
                            raise ConnectorError('LIMIT_REACHED', 'At most eight managed connectors can be saved.', 409)
                        rows = _incarnated(rows)
                        rows.append({'id': identifier, 'url': url,
                                     'incarnation': secrets.token_hex(16)})
                        _save_managed(self.path, rows)
                    elif 'incarnation' not in old:
                        _save_managed(self.path, _incarnated(rows))
            except (OSError, ValueError) as exc:
                raise ConnectorError('SAVE_FAILED', 'Connector settings could not be saved safely.', 503) from exc
        return self.read()

    def disconnect(self, identifier: object) -> dict:
        if type(identifier) is not str or not _ID.fullmatch(identifier):
            raise ConnectorError('INVALID_ID', 'Use a short lowercase connector ID.')
        with self._lock:
            if self._stopping.is_set():
                raise ConnectorError('STOPPED', 'Connector registry is stopping.', 503)
            try:
                _safe_parent(self.path, create=True)
                with locked(self.path.parent, CONFIG_NAME + '.lock'):
                    rows = _load_managed(self.path)
                    remaining = [row for row in rows if row['id'] != identifier]
                    if len(remaining) != len(rows):
                        if self._testing is not None and self._testing[0] == identifier:
                            self._test_cancel.set()
                            with self._active_lock:
                                for sock in tuple(self._active):
                                    _abort_socket(sock)
                        _save_managed(self.path, _incarnated(remaining))
                    self._results.pop(identifier, None)
            except (OSError, ValueError) as exc:
                raise ConnectorError('SAVE_FAILED', 'Connector settings could not be saved safely.', 503) from exc
        return self.read()

    def test(self, identifier: object) -> dict:
        if type(identifier) is not str or not _ID.fullmatch(identifier):
            raise ConnectorError('INVALID_ID', 'Use a short lowercase connector ID.')
        with self._lock:
            if self._stopping.is_set():
                raise ConnectorError('STOPPED', 'Connector registry is stopping.', 503)
            rows = _load_managed(self.path)
            item = next((row for row in rows if row['id'] == identifier), None)
            if item is None:
                raise ConnectorError('NOT_MANAGED', 'Only saved AGIW loopback connectors can be tested.', 404)
            if self._testing is not None:
                raise ConnectorError('TEST_BUSY', 'A connector test is already running.', 409)
            try:
                with locked(self.path.parent, CONFIG_NAME + '.lock'):
                    rows = _load_managed(self.path)
                    item = next((row for row in rows if row['id'] == identifier), None)
                    if item is None:
                        raise ConnectorError('NOT_MANAGED',
                                             'Only saved AGIW loopback connectors can be tested.', 404)
                    if 'incarnation' not in item:
                        rows = _incarnated(rows)
                        _save_managed(self.path, rows)
                        item = next(row for row in rows if row['id'] == identifier)
            except (OSError, ValueError) as exc:
                raise ConnectorError('CONFIG_UNSAFE',
                                     'Saved connector settings cannot be read safely.', 503) from exc
            cancel = threading.Event()
            identity = (identifier, item['url'], item['incarnation'])
            self._test_cancel = cancel
            self._testing = identity
            self._results.pop(identifier, None)
            self._worker = threading.Thread(target=self._run_test,
                                            args=(*identity, cancel), daemon=True)
            try:
                self._worker.start()
            except RuntimeError as exc:
                self._worker = None
                self._testing = None
                self._test_cancel = None
                raise ConnectorError('TEST_UNAVAILABLE', 'Connector test could not start.', 503) from exc
        return self.read()

    def _run_test(self, identifier: str, url: str, incarnation: str,
                  cancel: threading.Event) -> None:
        try:
            result = self.test_fn(url, stopping=cancel, active=self._active,
                                  active_lock=self._active_lock)
        except ConnectorError as exc:
            result = {'state': 'error', 'code': exc.code, 'checkedAtUnix': round(time.time(), 3)}
        except Exception:
            result = {'state': 'error', 'code': 'TEST_FAILED', 'checkedAtUnix': round(time.time(), 3)}
        with self._lock:
            if not self._stopping.is_set() and not cancel.is_set():
                try:
                    # Atomic private-file reads suffice here: read() checks the
                    # persisted identity again before exposing this local result.
                    # Do not contend with an unrelated writer's nonblocking lock.
                    rows = _load_managed(self.path)
                    if any((row['id'], row['url'], row.get('incarnation')) ==
                           (identifier, url, incarnation) for row in rows):
                        self._results[identifier] = (url, incarnation, result)
                except (ConnectorError, OSError, ValueError):
                    pass  # A changed or unsafe registry cannot certify this result.
            if self._testing == (identifier, url, incarnation):
                self._testing = None
                self._test_cancel = None

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            if self._test_cancel is not None:
                self._test_cancel.set()
        with self._active_lock:
            for sock in tuple(self._active):
                _abort_socket(sock)
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=.25)
