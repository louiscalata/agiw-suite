"""Bounded component and ephemeral HTTP regressions; no credentials or inference."""
import json
from http.client import HTTPConnection
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch
import bundled_components as components
import server

GOOD_RUNTIME = json.dumps({'version': '24.18.0', 'arch': 'arm64'})
GOOD_REPORT = json.dumps({'outcome': 'COMPLETED', 'reportStored': True, 'modelCalls': 0})


def completed(output):
    return subprocess.CompletedProcess([], 0, output, '')


class ComponentTests(unittest.TestCase):
    def run_worker(self, responses):
        owner = components.BundledComponents()
        with patch.object(components, 'NODE_PATHS', (Path('/first/node'), Path('/second/node'))), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(components.os, 'access', return_value=True), \
             patch.object(components, 'verify_payload', return_value={}), \
             patch.object(components.subprocess, 'run', side_effect=responses) as calls:
            owner._run_check()
        return owner, calls

    def test_unsuitable_first_node_falls_back_and_sanitizes_environment(self):
        with patch.dict(components.os.environ, {'NODE_OPTIONS': '--require bad', 'NODE_PATH': '/bad', 'TYPESAFE_API_KEY': 'synthetic-test-value'}):
            owner, calls = self.run_worker([completed('{"version":"20.0.0","arch":"arm64"}'), completed(GOOD_RUNTIME), completed(GOOD_REPORT)])
        self.assertEqual(owner.check['state'], 'passed')
        self.assertEqual(calls.call_args_list[-1].args[0][0], '/second/node')
        for call in calls.call_args_list:
            env = call.kwargs['env']
            for key in ('NODE_OPTIONS', 'NODE_PATH', 'TYPESAFE_API_KEY'):
                self.assertNotIn(key, env)
            self.assertEqual(env['PATH'], '/usr/bin:/bin')

    def test_invalid_reports_and_demo_timeout_fail_closed(self):
        for report in ('[]', '{}', '{"outcome":"COMPLETED","reportStored":true,"modelCalls":true}',
                       '{"outcome":"COMPLETED","reportStored":true,"modelCalls":1}', 'bad-json',
                       subprocess.TimeoutExpired('node', 20), subprocess.CalledProcessError(1, 'node')):
            with self.subTest(report=str(report)):
                owner, _ = self.run_worker([completed(GOOD_RUNTIME), report if isinstance(report, Exception) else completed(report)])
                self.assertEqual(owner.check['state'], 'failed')

    def test_probe_timeout_and_no_node_need_setup(self):
        owner, _ = self.run_worker([subprocess.TimeoutExpired('node', 5), completed('[]')])
        self.assertEqual(owner.check['state'], 'needs-node')
        owner = components.BundledComponents()
        with patch.object(components, 'verify_payload', return_value={}), patch.object(components, 'node_path', return_value=None), patch.object(components.subprocess, 'run') as call:
            owner._run_check()
        self.assertEqual(owner.check['state'], 'needs-node')
        call.assert_not_called()

    def test_failed_thread_start_is_retryable(self):
        owner = components.BundledComponents()
        with patch.object(components.threading.Thread, 'start', side_effect=RuntimeError('cannot start')):
            self.assertEqual(owner.request_check()['state'], 'failed')
        with patch.object(components.threading.Thread, 'start') as start:
            self.assertEqual(owner.request_check()['state'], 'running')
            start.assert_called_once()

    def test_concurrent_requests_start_one_worker(self):
        owner = components.BundledComponents()
        barrier = threading.Barrier(9)
        answers = []
        def request():
            barrier.wait()
            answers.append(owner.request_check())
        with patch.object(components, 'threading') as module:
            module.Thread.return_value.start.return_value = None
            threads = [threading.Thread(target=request) for _ in range(8)]
            for thread in threads: thread.start()
            barrier.wait()
            for thread in threads: thread.join(timeout=2)
            self.assertEqual(len(answers), 8)
            self.assertEqual(module.Thread.call_count, 1)
            self.assertTrue(all(answer['state'] == 'running' for answer in answers))


class ComponentEndpointTests(unittest.TestCase):
    def setUp(self):
        with patch.object(server, 'mac_online_code_controls_enabled', return_value=False):
            self.http = server.MonitorServer(('127.0.0.1', 0), server.SnapshotStore())
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def post(self, body, origin=True, content_type='application/json'):
        port = self.http.server_address[1]
        headers = {'Content-Type': content_type}
        if origin: headers['Origin'] = f'http://127.0.0.1:{port}' if origin is True else origin
        connection = HTTPConnection('127.0.0.1', port, timeout=2)
        connection.request('POST', '/api/components/nisi/check', body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_origin_and_strict_body(self):
        with patch.object(self.http.bundled_components, 'request_check') as request:
            for origin in (False, 'https://example.invalid'):
                self.assertEqual(self.post('{"action":"self-check"}', origin=origin)[0], 403)
            for body in ('{}', '[]', '{"action":"self-check","extra":1}', '{"action":"self-check","action":"self-check"}'):
                self.assertEqual(self.post(body)[0], 400)
            self.assertEqual(self.post('{"action":"self-check"}', content_type='text/plain')[0], 415)
            request.assert_not_called()

    def test_check_is_async_and_duplicate_is_deduplicated(self):
        with patch.object(components, 'threading') as module:
            first = self.post('{"action":"self-check"}')
            second = self.post('{"action":"self-check"}')
            self.assertEqual(first[0], 202)
            self.assertEqual(second[0], 202)
            self.assertEqual(first[1]['state'], 'running')
            module.Thread.return_value.start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
