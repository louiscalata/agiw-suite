"""Review regressions: in-memory handlers and isolated probe fixtures, never live services."""
import io
import json
import plistlib
import re
import socket
import threading
import time
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import inference_collect
import pc_tunnel
import server
from test_server import snapshot


def handler(path, store=None, body=None, capability=None, supplied=None):
    request = object.__new__(server.Handler)
    request.path = path
    request.headers = Message()
    for name, value in {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765',
                        'Content-Type': 'application/json'}.items():
        request.headers[name] = value
    raw = json.dumps(body or {}).encode()
    request.headers['Content-Length'] = str(len(raw))
    if supplied is not None:
        request.headers['X-AGIW-Control'] = supplied
    request.rfile = io.BytesIO(raw)
    request.server = SimpleNamespace(server_address=('127.0.0.1', 8765), store=store,
                                     control_capability=capability,
                                     jev_connection=SimpleNamespace(request_check=lambda: {'status': 'running'}))
    request.reply = lambda status, payload, mime: (status, payload)
    return request


class ReviewRegressions(unittest.TestCase):
    def test_public_app_labels_use_product_and_generic_volume_names(self):
        info = plistlib.loads(Path('Info.plist').read_bytes())
        self.assertTrue(info['CFBundleDisplayName'].startswith('AGIW '))
        self.assertNotIn('SharedChami', info['NSNetworkVolumesUsageDescription'])

    def test_all_control_routes_require_a_single_current_capability(self):
        routes = list(server.OWNER_ACTION_ROUTES) + ['/api/models/control', '/api/models/auto-unload',
                                                   '/api/components/nisi/check', '/api/components/jev/check']
        for route in routes:
            for supplied in (None, 'wrong-fixture', 'non-ascii-\u00e9'):
                with self.subTest(route=route, supplied=supplied):
                    request = handler(route, capability='fixture-only', supplied=supplied)
                    self.assertEqual(request.do_POST()[0], 403)
        request = handler('/api/components/jev/check', body={'action': 'connection-check'},
                          capability='fixture-only', supplied='fixture-only')
        self.assertEqual(request.do_POST()[0], 202)
        request.headers['X-AGIW-Control'] = 'fixture-only'
        self.assertEqual(request.do_POST()[0], 403)

    def test_native_capability_requires_parent_and_bounded_pipe_input(self):
        fixture = 'a' * 64  # public test data, never a generated capability
        with patch('server.os.getppid', return_value=123):
            self.assertIsNone(server.read_native_capability(None, io.StringIO(fixture + '\n')))
            self.assertIsNone(server.read_native_capability(124, io.StringIO(fixture + '\n')))
            self.assertTrue(server.read_native_capability(123, io.StringIO(fixture + '\n')) == fixture)
            for value in ('', fixture, fixture + 'b\n', 'not-valid\n'):
                self.assertIsNone(server.read_native_capability(123, io.StringIO(value)))

    def test_authorized_fix_without_content_length_reaches_transport_validation(self):
        request = handler('/api/inference/fix', capability='fixture-only', supplied='fixture-only')
        request.server.l2_open = True
        request.server.online_code_repair = SimpleNamespace(request_fix=lambda _: self.fail('must not dispatch'))
        del request.headers['Content-Length']
        with patch('server.mac_online_code_controls_enabled', return_value=True):
            self.assertEqual(request.do_POST()[0], 411)
            # Authentication still precedes transport validation.
            del request.headers['X-AGIW-Control']
            self.assertEqual(request.do_POST()[0], 403)

    def test_ci_covers_minimum_python(self):
        workflow = Path('.github/workflows/source-checks.yml').read_text()
        self.assertIn("python: ['3.9', '3.14']", workflow)
        self.assertIn("python-version: ${{ matrix.python }}", workflow)

    def test_monitor_links_to_served_control_dashboard(self):
        page = Path('web/monitor.html').read_text()
        self.assertRegex(page, r'<a\s+href="/"')
        request = handler('/')
        replies = []
        request.reply = lambda status, payload, mime: replies.append(status)
        request.do_GET()
        self.assertEqual(replies, [200])

    def test_sampler_reconciles_only_fresh_positive_settlement(self):
        from contextlib import ExitStack
        from model_control import ModelControl, ControlError
        from test_model_control import inventory
        from test_server import MemoryGuardSampleTest
        store = server.SnapshotStore()
        store.publish(inventory())
        def fail(*_):
            raise ControlError(502, 'fixture command failed')
        control = ModelControl(store, runner=fail)
        control.request('unload', 'publisher/model')
        self.assertTrue(control.join(1))
        self.assertEqual(control.read()['settlement'], 'unconfirmed')
        helper = MemoryGuardSampleTest()
        for loaded, expected in ((True, 'unconfirmed'), (False, 'confirmed')):
            data = inventory(loaded=loaded, state='idle' if loaded else 'unloaded',
                             source='lms-ps' if loaded else 'lmstudio-api')
            with ExitStack() as stack:
                for name, value in [('collect_snapshot', data), ('mac_gpu', ({}, {'id': 'mac-gpu'})),
                                    ('collect_activity', {'runs': [], 'sources': []}),
                                    ('collect_clients', ([], [])), ('LocalCallers', helper.Callers()),
                                    ('load_config', pc_tunnel.load_config(environ={}))]:
                    stack.enter_context(patch('server.' + name, return_value=value))
                stack.enter_context(patch('server.attach'))
                server.sample(store, helper.Stop(1), feed=helper.Feed(), guard=helper.FakeGuard(),
                              model_controller=control)
            self.assertEqual(control.read()['settlement'], expected)
        self.assertEqual(control.read()['status'], 'failed')

    def test_attach_reuses_verified_inventory_and_endpoint_without_reprobe(self):
        data = snapshot()
        data['models'][0].update(id='qwen', modelKey='qwen', context=8192)
        data['sources'].append({'id': 'lmstudio-api', 'state': 'live', 'route': 'direct',
                                'endpoint': '127.0.0.1:4321', 'modelsComplete': True,
                                'verifiedListener': {'kind': 'lmstudio', 'pid': 123, 'executable': '/fixture/lms'}})
        seen = []
        def fetch(url):
            seen.append(url)
            raise inference_collect.ProbeError('refused')
        document = inference_collect.attach(data, 'mac', fetch=fetch, targets=[], veritas_url='')
        node = next(row for row in document['nodes'] if row['id'] == 'mac:lmstudio')
        self.assertEqual((node['health'], node['modelId'], node['endpoint']),
                         ('ok', 'qwen', 'http://127.0.0.1:4321'))
        self.assertEqual(seen, [])

    def test_attach_does_not_query_unverified_lmstudio_listener(self):
        seen = []
        def fetch(url):
            seen.append(url)
            raise inference_collect.ProbeError('refused')
        document = inference_collect.attach(snapshot(), 'mac', fetch=fetch, targets=[], veritas_url='')
        self.assertEqual(seen, [])
        self.assertEqual(next(n for n in document['nodes'] if n['id'] == 'mac:lmstudio')['health'], 'not_measured')

    def test_collection_has_one_total_budget(self):
        clock = [0.0]
        def fetch(url, timeout=.4, **_):
            clock[0] += timeout
            raise inference_collect.ProbeError('timeout')
        with patch('inference_collect.time.monotonic', side_effect=lambda: clock[0]), \
             patch('inference_collect.loopback_get', side_effect=fetch):
            inference_collect.collect_map('pc', now=100)
        self.assertLessEqual(clock[0], .8)

    def test_memory_watchdog_runs_before_optional_map_probes(self):
        from test_server import MemoryGuardSampleTest
        helper = MemoryGuardSampleTest()
        guard = helper.FakeGuard()
        observed = []
        with patch('server.attach', side_effect=lambda *a, **k: observed.append(len(guard.calls))), \
             patch('server.load_config', return_value=pc_tunnel.load_config(environ={})):
            helper.run_sampler(guard)
        self.assertEqual(observed, [1])

    def test_swift_poll_url_is_served(self):
        source = Path('Monitor.swift').read_text()
        page = re.search(r'URL\(string: "(http://127.0.0.1:.*?/monitor)"\)', source).group(1)
        page = page.replace(r'\(port)', '8765')
        # Extract the actual expression, rather than supplying the desired route to the handler.
        poll = source.split('private func pollSnapshot()', 1)[1].split('private func updateBadge', 1)[0]
        if 'base.appendingPathComponent("api/snapshot")' in poll:
            path = '/monitor/api/snapshot'
        else:
            path = re.search(r'URL\(string: "([^"]+)", relativeTo: base\)', poll).group(1)
        store = server.SnapshotStore()
        store.publish(snapshot())
        self.assertEqual(handler(path, store).do_GET()[0], 200)
        self.assertTrue(page.endswith('/monitor'))

    def test_stale_inference_is_expired_on_read_without_mutating_store(self):
        store = server.SnapshotStore()
        data = snapshot(sampled=100)
        data['inference'] = {'observedAt': '1970-01-01T00:01:40Z', 'nodes': [{
            'id': 'mac:lmstudio', 'machine': 'mac', 'health': 'ok',
            'observedAt': '1970-01-01T00:01:40Z', 'generationTokensPerSecond': 80,
            'modelId': 'qwen', 'slotsBusy': 1}]}
        store.publish(data)
        with patch('server.time.time', return_value=104):
            status, payload = handler('/api/inference', store).do_GET()
        node = json.loads(payload)['nodes'][0]
        self.assertEqual(status, 200)
        self.assertEqual(node['health'], 'not_measured')
        self.assertIsNone(node['generationTokensPerSecond'])
        self.assertIsNone(node['slotsBusy'])
        self.assertEqual(store.snapshot['inference']['nodes'][0]['health'], 'ok')

    def test_control_rejects_forged_origin_without_capability(self):
        self.assertEqual(handler('/api/components/jev/check', body={'action': 'connection-check'}).do_POST()[0], 403)

    def test_expiration_removes_observed_edges_but_keeps_structural_links(self):
        from inference_stats import expire_map
        hosts = {'from': 'pc', 'to': 'pc:router', 'kind': 'hosts'}
        upstream = {'from': 'pc:router', 'to': 'pc:lane', 'kind': 'upstream', 'modelId': 'model'}
        caller = {'from': 'pc:router', 'to': 'client', 'kind': 'caller'}
        data = {'observedAt': '1970-01-01T00:01:40Z', 'nodes': [
            {'id': 'pc:router', 'machine': 'pc', 'health': 'ok'},
            {'id': 'pc:lane', 'machine': 'pc', 'health': 'ok'}], 'edges': [hosts, upstream, caller]}
        expire_map(data, 101)
        self.assertEqual(data['edges'], [hosts, upstream, caller])
        expire_map(data, 131)
        self.assertEqual(data['edges'], [hosts])

    def test_environment_overrides_file(self):
        config = {'user': 'file-user', 'host': 'file.example', 'identityFile': '/file/key'}
        env = {'AGIW_PC_SSH_USER': 'env-user', 'AGIW_PC_SSH_HOST': 'env.example',
               'AGIW_PC_SSH_IDENTITY': '/env/key'}
        with patch.object(Path, 'read_text', return_value=json.dumps(config)):
            result = pc_tunnel.load_config('/fixture/config.json', environ=env)
        self.assertEqual([result[key] for key in ('user', 'host', 'identityFile')],
                         ['env-user', 'env.example', '/env/key'])

    def test_lmstudio_accepts_another_loaded_model(self):
        fetch = lambda url: (200, json.dumps({'data': [{'id': 'qwen3.8-27b', 'state': 'loaded'}]}), 1)
        node = inference_collect._read_lmstudio(fetch, '2026-11-03T00:00:00Z')
        self.assertEqual((node['health'], node['modelId']), ('ok', 'qwen3.8-27b'))

    def test_unreported_load_state_is_unknown(self):
        fetch = lambda url: (404, '', 1) if '/api/v0/' in url else (200, '{"data":[]}', 1)
        node = inference_collect._read_lmstudio(fetch, '2026-11-03T00:00:00Z')
        self.assertEqual(node['health'], 'not_measured')

    def test_model_catalog_without_state_does_not_claim_unloaded(self):
        fetch = lambda url: (200, '{"data":[{"id":"qwen"}]}', 1)
        node = inference_collect._read_lmstudio(fetch, '2026-11-03T00:00:00Z')
        self.assertEqual(node['health'], 'not_measured')

    def test_pc_endpoint_is_the_fetched_forward(self):
        target = {'id': 'pc:router', 'service': '127.0.0.1:1234', 'base': 'http://127.0.0.1:1334'}
        node = inference_collect._read_pc(target, lambda url: (200, '{"data":[{"id":"qwen"}]}', 1),
                                         inference_collect.RateMemory(), 100, '1970-01-01T00:01:40Z')
        self.assertEqual(node['endpoint'], target['base'])

    def test_forwarded_endpoints_preserve_router_upstream_edges(self):
        def fetch(url):
            if url.endswith('/v1/models'):
                return 200, '{"data":[{"id":"gpt-oss-20b","upstream":"http://127.0.0.1:1245"}]}', 1
            if url.endswith('/props'):
                return 200, '{"model_alias":"gpt-oss-20b"}', 1
            return 404, '', 1
        targets = [{'id': 'pc:router', 'service': '127.0.0.1:1234', 'base': 'http://127.0.0.1:1334'},
                   {'id': 'pc:gpt-oss-20b', 'service': '127.0.0.1:1245', 'base': 'http://127.0.0.1:1345'}]
        document = inference_collect.collect_map('mac', fetch=fetch, targets=targets, veritas_url='')
        self.assertTrue(any(edge['from'] == 'pc:router' and edge['to'] == 'pc:gpt-oss-20b'
                            and edge['kind'] == 'upstream' for edge in document['edges']))

    def test_stale_tunnel_observation_is_not_still_up(self):
        from inference_stats import expire_map
        document = {'observedAt': '1970-01-01T00:01:40Z', 'nodes': [],
                    'tunnel': {'state': 'up', 'detail': 'SSH local forwards are accepting connections.'}}
        expire_map(document, 131)
        self.assertEqual(document['tunnel']['state'], 'not_measured')
        self.assertNotIn('accepting', document['tunnel']['detail'])

    def test_probe_total_deadline_bounds_a_dripping_body(self):
        reader, writer = socket.socketpair()
        def drip():
            try:
                writer.recv(4096)
                writer.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n')
                for _ in range(20):
                    time.sleep(.03)
                    writer.sendall(b'x')
            except OSError:
                pass
            finally:
                writer.close()
        def connect(connection):
            reader.settimeout(connection.timeout)
            connection.sock = reader
        worker = threading.Thread(target=drip)
        worker.start()
        started = time.monotonic()
        try:
            with patch('http.client.HTTPConnection.connect', connect):
                with self.assertRaises(inference_collect.ProbeError) as caught:
                    inference_collect.loopback_get('http://127.0.0.1:9999/health', timeout=.15)
            self.assertEqual(caught.exception.reason, 'timeout')
            self.assertLess(time.monotonic() - started, .5)
        finally:
            reader.close()
            worker.join(2)


if __name__ == '__main__':
    unittest.main()
