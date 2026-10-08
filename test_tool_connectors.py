"""Offline registry and real loopback MCP lifecycle tests. No tool is called."""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import server
from tool_connectors import (ConnectorError, ToolConnectors, _opencode_discovery,
                             test_streamable_http)


class MCPFixture(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, mode='json'):
        super().__init__(('127.0.0.1', 0), MCPHandler)
        self.mode = mode
        self.calls = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_address[1]}/mcp'

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class MCPHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        method = data['method']
        self.server.calls.append((method, dict(self.headers)))
        if self.path != '/mcp' or self.headers['Origin'] != f'http://127.0.0.1:{self.server.server_address[1]}':
            return self._send(403)
        if method == 'initialize':
            if self.server.mode == 'redirect':
                return self._send(302, headers={'Location': 'http://example.invalid/mcp'})
            if self.server.mode in ('sse-resume', 'sse-bad-id', 'sse-retry-over-limit', 'sse-repeat-prime'):
                event_id = b'bad id' if self.server.mode == 'sse-bad-id' else b'cursor-a'
                retry = b'2000' if self.server.mode == 'sse-retry-over-limit' else b'1'
                wire = b'id: ' + event_id + b'\nretry: ' + retry + b'\ndata:\n\n'
                return self._send(200, wire, 'text/event-stream',
                                  headers={'MCP-Session-Id': 'fixture-session'})
            result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'fixture', 'version': '1.0.0'}}
            if self.server.mode == 'bad-version':
                result['protocolVersion'] = ['2025-11-25']
            body = {'jsonrpc': '2.0', 'id': 99 if self.server.mode == 'wrong-id' else 1,
                    'result': result}
            if self.server.mode == 'oversize':
                body['result']['junk'] = 'x' * 70000
            return self._response(body, headers={'MCP-Session-Id': 'fixture-session'})
        if (self.headers.get('MCP-Session-Id') != 'fixture-session'
                or self.headers.get('MCP-Protocol-Version') != '2025-11-25'):
            return self._send(400)
        if method == 'notifications/initialized':
            return self._send(202)
        if method == 'tools/list':
            body = {'jsonrpc': '2.0', 'id': data['id'], 'result': {'tools': [
                {'name': 'fixture_echo', 'inputSchema': {'type': 'object', 'properties': {}}}]}}
            if self.server.mode == 'sse':
                wire = b': stream primed\n\n' + b'data: ' + json.dumps(body).encode() + b'\n\n'
                return self._send(200, wire, 'text/event-stream',
                                  headers={'MCP-Session-Id': 'fixture-session'})
            if self.server.mode == 'wrong-session':
                return self._response(body, headers={'MCP-Session-Id': 'different-session'})
            return self._response(body)
        return self._send(400)

    def do_GET(self):
        self.server.calls.append(('GET', dict(self.headers)))
        if self.server.mode not in ('sse-resume', 'sse-repeat-prime'):
            return self._send(405)
        if self.headers.get('Last-Event-ID') != 'cursor-a' or self.headers.get('Accept') != 'text/event-stream':
            return self._send(400)
        if (self.headers.get('MCP-Session-Id') != 'fixture-session'
                or self.headers.get('MCP-Protocol-Version') != '2025-11-25'):
            return self._send(400)
        if self.server.mode == 'sse-repeat-prime':
            return self._send(200, b'id: cursor-b\ndata:\n\n', 'text/event-stream')
        body = {'jsonrpc': '2.0', 'id': 1, 'result': {
            'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
            'serverInfo': {'name': 'fixture', 'version': '1.0.0'}}}
        return self._send(200, b'data: ' + json.dumps(body).encode() + b'\n\n',
                          'text/event-stream', headers={'MCP-Session-Id': 'fixture-session'})

    def do_DELETE(self):
        self.server.calls.append(('DELETE', dict(self.headers)))
        return self._send(405)

    def _response(self, value, *, headers=None):
        return self._send(200, json.dumps(value).encode(), 'application/json', headers=headers)

    def _send(self, status, body=b'', content_type='text/plain', headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)


class ConnectorFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'tool-connectors.json'
        self.opencode = self.root / 'opencode.jsonc'
        self.opencode.write_text('''{
          // Client secrets and commands must never leave discovery.
          "mcp": {
            "pc-llm": {"type":"local","command":["/synthetic/not-real/tool", "secret-arg"],
                       "environment":{"TOKEN":"secret-value"},"enabled":true},
            "playwright": {"type":"local","command":["npx"],"enabled":false,},
          },
        }''')

    def fixture(self, mode='json'):
        fixture = MCPFixture(mode)
        self.addCleanup(fixture.close)
        return fixture

    def registry(self, **kwargs):
        registry = ToolConnectors(self.config, self.opencode, **kwargs)
        self.addCleanup(registry.stop)
        return registry


class ConnectorTests(ConnectorFixture, unittest.TestCase):

    def test_opencode_discovery_redacts_untrusted_command_environment_and_agent_claims(self):
        status, rows = _opencode_discovery(self.opencode)
        self.assertEqual(status, {'state': 'readable', 'scope': 'global-file-only'})
        self.assertEqual([row['id'] for row in rows], ['opencode:pc-llm', 'opencode:playwright'])
        self.assertEqual([row['enabledInOpenCode'] for row in rows], [True, False])
        self.assertTrue(all(row['agentPermission'] == 'unknown' for row in rows))
        exposed = json.dumps(rows)
        for forbidden in ('/synthetic/not-real/tool', 'secret-arg', 'TOKEN', 'secret-value'):
            self.assertNotIn(forbidden, exposed)

    def test_global_json_has_precedence_with_jsonc_fallback_and_limited_scope(self):
        primary = self.root / 'opencode.json'
        backup = self.root / 'opencode.backup'
        self.opencode.rename(backup)
        status, rows = _opencode_discovery(primary)
        self.assertEqual((status['state'], len(rows)), ('missing', 0))
        backup.rename(self.opencode)
        status, rows = _opencode_discovery(primary)
        self.assertEqual(status['scope'], 'global-file-only')
        self.assertEqual(len(rows), 2)
        primary.write_text('{"mcp":{"primary":{"type":"local","command":["tool"]}}}')
        status, rows = _opencode_discovery(primary)
        self.assertEqual([row['id'] for row in rows], ['opencode:primary'])

    def test_nested_json_is_reported_without_dropping_a_request(self):
        nested = '[' * 1100 + '0' + ']' * 1100
        self.opencode.write_text(nested)
        status, rows = _opencode_discovery(self.opencode)
        self.assertEqual(status['state'], 'unavailable')
        self.assertEqual(rows, [])
        self.config.write_text(nested)
        self.config.chmod(0o600)
        with self.assertRaises(ConnectorError) as raised:
            self.registry().read()
        self.assertEqual(raised.exception.code, 'CONFIG_INVALID')

    def test_only_literal_loopback_endpoints_can_be_saved(self):
        registry = self.registry()
        for url in ('http://localhost:3000/mcp', 'http://[::1]:3000/mcp',
                    'http://127.0.0.2:3000/mcp', 'http://127.0.0.1:3000@evil.invalid/mcp',
                    'http://127.0.0.1:3000/mcp?token=x', 'http://127.0.0.1:3000/mcp#fragment',
                    'http://127.0.0.1:3000/mcp?', 'http://127.0.0.1:3000/mcp#',
                    'https://127.0.0.1:3000/mcp', 'http://127.0.0.1:3000/../mcp',
                    'http://127.0.0.1:80/mcp'):
            with self.subTest(url=url), self.assertRaises(ConnectorError) as raised:
                registry.add('example', url)
            self.assertEqual(raised.exception.code, 'INVALID_URL')
        self.assertFalse(self.config.exists())

    def test_saved_registry_is_private_and_disconnect_is_idempotent(self):
        registry = self.registry()
        fixture = self.fixture()
        rows = registry.add('fixture', fixture.url)['connectors']
        self.assertEqual(next(row for row in rows if row['id'] == 'fixture')['detailCode'],
                         'AGIW_TEST_REGISTRY_ONLY')
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(registry.disconnect('fixture')['connectors'][0]['id'], 'opencode:pc-llm')
        registry.disconnect('fixture')
        self.assertEqual(json.loads(self.config.read_text())['managed'], [])

    def test_symlinked_saved_record_is_fail_closed_without_clobber(self):
        outside = self.root / 'outside.json'
        outside.write_text('original')
        self.config.symlink_to(outside)
        registry = self.registry()
        with self.assertRaises(ConnectorError) as raised:
            registry.add('fixture', self.fixture().url)
        self.assertIn(raised.exception.code, {'CONFIG_UNSAFE', 'SAVE_FAILED'})
        self.assertEqual(outside.read_text(), 'original')

    def test_bounded_mcp_lifecycle_json_sse_and_resumption_only_lists_tools(self):
        for mode in ('json', 'sse', 'sse-resume'):
            with self.subTest(mode=mode):
                fixture = self.fixture(mode)
                result = test_streamable_http(fixture.url)
                self.assertEqual((result['state'], result['toolCount'], result['protocolVersion']),
                                 ('ready', 1, '2025-11-25'))
                methods = [method for method, _ in fixture.calls]
                self.assertEqual(methods, ['initialize'] + (['GET'] if mode == 'sse-resume' else [])
                                 + ['notifications/initialized', 'tools/list', 'DELETE'])
                for method, headers in fixture.calls:
                    if method in ('DELETE', 'GET'):
                        continue
                    self.assertIn('application/json', headers['Accept'])
                    self.assertIn('text/event-stream', headers['Accept'])
                    if method != 'initialize':
                        self.assertEqual(headers['MCP-Session-Id'], 'fixture-session')
                        self.assertEqual(headers['MCP-Protocol-Version'], '2025-11-25')
                if mode == 'sse-resume':
                    self.assertEqual(fixture.calls[1][1]['Last-Event-ID'], 'cursor-a')
                    self.assertEqual(fixture.calls[1][1]['MCP-Session-Id'], 'fixture-session')
                    self.assertEqual(fixture.calls[1][1]['MCP-Protocol-Version'], '2025-11-25')

    def test_redirect_wrong_id_wrong_session_and_oversize_fail_closed(self):
        for mode, code in (('redirect', 'REDIRECT_REFUSED'), ('wrong-id', 'INVALID_MCP_RESPONSE'),
                           ('wrong-session', 'INVALID_SESSION'), ('oversize', 'MCP_RESPONSE_TOO_LARGE'),
                           ('sse-bad-id', 'INVALID_EVENT_ID'),
                           ('sse-retry-over-limit', 'SSE_RETRY_UNSUPPORTED'),
                           ('sse-repeat-prime', 'SSE_RESUME_LIMIT'),
                           ('bad-version', 'UNSUPPORTED_VERSION')):
            with self.subTest(mode=mode), self.assertRaises(ConnectorError) as raised:
                test_streamable_http(self.fixture(mode).url)
            self.assertEqual(raised.exception.code, code)

    def test_async_disconnect_cancels_and_fences_late_success(self):
        entered = threading.Event()
        release = threading.Event()
        def delayed(_url, **kwargs):
            entered.set()
            release.wait(2)
            return {'state': 'ready', 'toolCount': 1, 'protocolVersion': '2025-11-25'}
        registry = self.registry(test_fn=delayed)
        fixture = self.fixture()
        registry.add('fixture', fixture.url)
        self.assertEqual(registry.test('fixture')['connectors'][0]['transportTest']['state'], 'checking')
        self.assertTrue(entered.wait(1))
        with self.assertRaises(ConnectorError) as raised:
            registry.test('fixture')
        self.assertEqual(raised.exception.code, 'TEST_BUSY')
        registry.disconnect('fixture')
        release.set()
        registry._worker.join(1)
        registry.add('fixture', fixture.url)
        state = next(row for row in registry.read()['connectors'] if row['id'] == 'fixture')
        self.assertEqual(state['transportTest']['state'], 'not-tested')

    def test_thread_start_failure_does_not_latch_busy(self):
        registry = self.registry()
        registry.add('fixture', self.fixture().url)
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('fixture')):
            with self.assertRaises(ConnectorError) as raised:
                registry.test('fixture')
        self.assertEqual(raised.exception.code, 'TEST_UNAVAILABLE')
        self.assertIsNone(registry._testing)

    def test_one_shot_result_expires_instead_of_claiming_live_readiness(self):
        registry = self.registry()
        fixture = self.fixture()
        registry.add('fixture', fixture.url)
        registry._results['fixture'] = (fixture.url, {
            'state': 'ready', 'toolCount': 1, 'protocolVersion': '2025-11-25',
            'checkedAtUnix': time.time() - 61})
        row = next(row for row in registry.read()['connectors'] if row['id'] == 'fixture')
        self.assertEqual(row['transportTest'], {'state': 'not-tested'})


class RouteTests(ConnectorFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.registry_obj = self.registry()
        with patch.object(server, 'mac_online_code_controls_enabled', return_value=False):
            self.http = server.MonitorServer(('127.0.0.1', 0), server.SnapshotStore(),
                                             tool_connectors=self.registry_obj)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def request(self, method='GET', value=None, *, origin=True):
        port = self.http.server_address[1]
        conn = HTTPConnection('127.0.0.1', port, timeout=2)
        body = json.dumps(value) if value is not None else None
        headers = {'Content-Type': 'application/json'} if method == 'POST' else {}
        if method == 'POST' and origin:
            headers['Origin'] = f'http://127.0.0.1:{port}'
        conn.request(method, '/api/tool-connectors', body=body, headers=headers)
        response = conn.getresponse()
        result = (response.status, json.loads(response.read()))
        conn.close()
        return result

    def test_http_add_test_poll_disconnect_and_origin_gate(self):
        fixture = self.fixture('sse')
        body = {'action': 'add', 'id': 'fixture', 'url': fixture.url}
        self.assertEqual(self.request('POST', body, origin=False)[0], 403)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.request('POST', body)[0], 201)
        status, checking = self.request('POST', {'action': 'test', 'id': 'fixture'})
        self.assertEqual(status, 202)
        self.assertIn(next(row for row in checking['connectors'] if row['id'] == 'fixture')
                      ['transportTest']['state'], ('checking', 'ready'))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, result = self.request()
            state = next(row for row in result['connectors'] if row['id'] == 'fixture')['transportTest']
            if state['state'] != 'checking':
                break
            time.sleep(.01)
        self.assertEqual((status, state['state'], state['toolCount']), (200, 'ready', 1))
        self.assertEqual(self.request('POST', {'action': 'disconnect', 'id': 'fixture'})[0], 200)
        self.assertEqual([row['id'] for row in self.request()[1]['connectors']],
                         ['opencode:pc-llm', 'opencode:playwright'])

    def test_http_rejects_duplicate_or_untrusted_action_without_network(self):
        for body in ('{"action":"add","id":"a","id":"b","url":"http://127.0.0.1:3000/mcp"}',
                     '{"action":"test","id":"opencode:pc-llm"}',
                     '{"action":"add","id":"x","url":"http://example.invalid/mcp"}'):
            port = self.http.server_address[1]
            conn = HTTPConnection('127.0.0.1', port, timeout=2)
            conn.request('POST', '/api/tool-connectors', body=body,
                         headers={'Origin': f'http://127.0.0.1:{port}',
                                  'Content-Type': 'application/json'})
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            conn.close()
        self.assertFalse(self.config.exists())


if __name__ == '__main__':
    unittest.main()
