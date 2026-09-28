import json
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

from model_control import ControlError, ModelControl, _find_lms, _observed, _run_cli, _target
from server import MonitorServer, SnapshotStore


def inventory(*, model_id='publisher/model', loaded=True, state='idle', queued=0,
              source='lms-ps', sampled=None):
    return {
        'sampledAt': time.time() if sampled is None else sampled,
        'models': [{'id': model_id, 'name': model_id, 'host': 'mac',
                    'loaded': loaded, 'state': state, 'queued': queued,
                    'source': source, 'ageSeconds': 0,
                    'modelKey': model_id,
                    'instanceId': model_id if source == 'lms-ps' else None,
                    'loadedInstanceIds': [model_id] if loaded else []}],
        'sources': [{'id': 'lmstudio-api', 'state': 'live'},
                    {'id': 'lms-ps', 'state': 'live'}],
    }


class MutableStore:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class SelectionTest(unittest.TestCase):
    def test_load_only_exact_unloaded_inventory_key(self):
        data = inventory(loaded=False, state='unloaded', source='lmstudio-api')
        self.assertEqual(_target(data, 'load', 'publisher/model')['id'], 'publisher/model')
        for selected in ('publisher', 'other/model', '; rm -rf /'):
            with self.subTest(selected=selected), self.assertRaises(ControlError):
                _target(data, 'load', selected)
        data['models'][0]['loaded'] = None
        with self.assertRaises(ControlError):
            _target(data, 'load', 'publisher/model')
        data = inventory(loaded=False, state='unloaded', source='lmstudio-api')
        data['models'][0]['modelKey'] = None
        with self.assertRaises(ControlError):
            _target(data, 'load', 'publisher/model')

    def test_unload_requires_exact_idle_instance_without_queue(self):
        base = inventory()
        self.assertEqual(_target(base, 'unload', 'publisher/model')['id'], 'publisher/model')
        for change in ({'state': 'generating'}, {'state': 'busy'},
                       {'state': 'loaded'}, {'queued': 1}, {'queued': None},
                       {'source': 'lmstudio-api'}, {'loaded': False},
                       {'instanceId': None}, {'modelKey': None},
                       {'loadedInstanceIds': None}, {'loadedInstanceIds': []}):
            with self.subTest(change=change):
                data = inventory()
                data['models'][0].update(change)
                with self.assertRaises(ControlError):
                    _target(data, 'unload', 'publisher/model')

    def test_stale_or_unverified_inventory_is_rejected(self):
        data = inventory(sampled=time.time() - 10)
        with self.assertRaises(ControlError) as result:
            _target(data, 'unload', 'publisher/model')
        self.assertEqual(result.exception.status, 503)
        data = inventory()
        data['sources'][1]['state'] = 'error'
        with self.assertRaises(ControlError):
            _target(data, 'unload', 'publisher/model')
        data = inventory()
        data['models'][0]['ageSeconds'] = 4
        with self.assertRaises(ControlError):
            _target(data, 'unload', 'publisher/model')

    def test_invalid_input_types_fail_without_starting_a_worker(self):
        control = ModelControl(MutableStore(inventory()), runner=lambda *_: self.fail('CLI ran'))
        for action, model_id in (([], 'publisher/model'), ('unload', []),
                                 ('bogus', 'publisher/model')):
            with self.subTest(action=action, model_id=model_id), self.assertRaises(ControlError):
                control.request(action, model_id)
        self.assertEqual(control.read()['status'], 'idle')


class OperationTest(unittest.TestCase):
    def test_worker_guard_spans_cli_and_fresh_inventory_confirmation(self):
        store = MutableStore(inventory())
        held, released, runner_started, proceed = (threading.Event() for _ in range(4))

        @contextmanager
        def guard():
            held.set()
            try:
                yield
            finally:
                released.set()

        def runner(*_):
            self.assertTrue(held.is_set())
            runner_started.set()
            self.assertTrue(proceed.wait(2))

        control = ModelControl(store, runner=runner)
        self.assertEqual(control.request('unload', 'publisher/model', action_guard=guard)['status'], 'running')
        self.assertTrue(runner_started.wait(1))
        self.assertFalse(released.is_set())
        proceed.set()
        until = time.monotonic() + 2
        while 'waiting for fresh inventory' not in control.read()['message'] and time.monotonic() < until:
            time.sleep(.01)
        self.assertIn('waiting for fresh inventory', control.read()['message'])
        self.assertFalse(released.is_set())
        store.data = inventory(loaded=False, state='unloaded', source='lmstudio-api')
        until = time.monotonic() + 2
        while control.read()['status'] == 'running' and time.monotonic() < until:
            time.sleep(.01)
        self.assertEqual(control.read()['status'], 'succeeded')
        self.assertTrue(released.is_set())

    def test_worker_guard_refusal_never_calls_lms(self):
        calls = []

        @contextmanager
        def guard():
            raise ControlError(409, 'Router admission is busy; auto-unload stopped.')
            yield

        control = ModelControl(MutableStore(inventory()), runner=lambda *args: calls.append(args))
        control.request('unload', 'publisher/model', action_guard=guard)
        until = time.monotonic() + 2
        while control.read()['status'] == 'running' and time.monotonic() < until:
            time.sleep(.01)
        self.assertEqual(control.read()['status'], 'failed')
        self.assertIn('Router admission is busy', control.read()['message'])
        self.assertEqual(calls, [])

    def test_cli_lookup_rejects_symlinked_or_group_writable_binary(self):
        with tempfile.TemporaryDirectory() as temp, patch('model_control.Path.home', return_value=Path(temp)):
            binary = Path(temp) / '.lmstudio' / 'bin' / 'lms'
            binary.parent.mkdir(parents=True)
            binary.write_text('mock')
            binary.chmod(0o700)
            self.assertEqual(_find_lms(), str(binary))
            binary.chmod(0o720)
            with self.assertRaises(ControlError):
                _find_lms()
            binary.chmod(0o700)
            binary.rename(binary.with_name('target'))
            binary.symlink_to('target')
            with self.assertRaises(ControlError):
                _find_lms()

    def test_unload_needs_api_instance_confirmation_not_cli_absence_alone(self):
        after = time.time() - 1
        still_loaded = inventory(source='lmstudio-api')
        self.assertFalse(_observed(still_loaded, 'unload', 'publisher/model',
                                   'publisher/model', after))
        still_loaded['models'][0]['loadedInstanceIds'] = None
        self.assertFalse(_observed(still_loaded, 'unload', 'publisher/model',
                                   'publisher/model', after))
        still_loaded['models'][0]['loadedInstanceIds'] = []
        still_loaded['models'][0]['loaded'] = False
        self.assertTrue(_observed(still_loaded, 'unload', 'publisher/model',
                                  'publisher/model', after))

    def test_single_operation_and_fresh_observation_required_for_success(self):
        store = MutableStore(inventory())
        started = threading.Event()
        release = threading.Event()
        calls = []

        def runner(action, model_id):
            calls.append((action, model_id))
            started.set()
            release.wait(1)

        control = ModelControl(store, runner=runner)
        accepted = control.request('unload', 'publisher/model')
        self.assertTrue(started.wait(1))
        self.assertEqual(accepted['status'], 'running')
        with self.assertRaises(ControlError) as duplicate:
            control.request('unload', 'publisher/model')
        self.assertEqual(duplicate.exception.status, 409)
        # A change sampled before the command returns must not certify it.
        store.data = inventory(loaded=False, state='unloaded', source='lmstudio-api')
        release.set()
        until = time.monotonic() + 1
        while 'waiting for fresh inventory' not in control.read()['message'] and time.monotonic() < until:
            time.sleep(.01)
        self.assertEqual(control.read()['status'], 'running')
        self.assertEqual(control.read()['message'].count('waiting for fresh inventory'), 1)
        store.data = inventory(loaded=False, state='unloaded', source='lmstudio-api')
        until = time.monotonic() + 2
        while control.read()['status'] == 'running' and time.monotonic() < until:
            time.sleep(.01)
        self.assertEqual(control.read()['status'], 'succeeded')
        self.assertEqual(calls, [('unload', 'publisher/model')])

    def test_cli_error_is_reported_without_claiming_a_state_change(self):
        store = MutableStore(inventory())

        def runner(*_):
            raise ControlError(504, 'LM Studio unload timed out after 30 seconds.')

        control = ModelControl(store, runner=runner)
        control.request('unload', 'publisher/model')
        until = time.monotonic() + 2
        while control.read()['status'] == 'running' and time.monotonic() < until:
            time.sleep(.01)
        self.assertEqual(control.read()['status'], 'failed')
        self.assertIn('timed out', control.read()['message'])

    def test_cli_uses_argument_vector_without_shell(self):
        with patch('model_control._find_lms', return_value='/fake/lms'), \
             patch('model_control.subprocess.Popen', side_effect=OSError('mock blocked')) as popen:
            with self.assertRaises(OSError):
                _run_cli('load', 'publisher/model')
            load_call = popen.call_args
            with self.assertRaises(OSError):
                _run_cli('unload', 'publisher/model')
        self.assertEqual(load_call.args[0],
                         ['/fake/lms', 'load', 'publisher/model', '--yes'])
        self.assertEqual(popen.call_args.args[0],
                         ['/fake/lms', 'unload', 'publisher/model'])
        self.assertNotIn('shell', popen.call_args.kwargs)


GiB = 1 << 30
RAM = 64 * GiB


def memory(pressure=1, avail=60, consumers=None, models=None):
    """The monitor's snapshot['memory'] block for a 64 GiB Mac, built by mem_guard itself."""
    import mem_guard
    state = {'pressure': pressure, 'availablePercent': avail, 'ramBytes': RAM, 'swapUsedBytes': 0,
             'swapTotalBytes': 0, 'compressedBytes': 0, 'wiredBytes': 5 * GiB, 'vmFreeBytes': 200 * GiB,
             'gpuAllocBytes': None, 'sampledAt': time.time()}
    return mem_guard.memory_block(state, mem_guard.default_config(), consumers=consumers, models=models)


def unloaded(size=16 * GiB, mem=None):
    data = inventory(loaded=False, state='unloaded', source='lmstudio-api')
    data['models'][0]['sizeBytes'] = size
    if mem is not None:
        data['memory'] = mem
    return data


SIMULATORS = [{'group': 'ios-simulator', 'label': 'iOS Simulators (iPhone 18 Pro)',
               'residentBytes': 20 * GiB, 'processCount': 40}]


class MemoryGateTest(unittest.TestCase):
    def control(self, store, calls):
        import mem_guard
        return ModelControl(store, runner=lambda action, model_id: calls.append((action, model_id)),
                            memory_config=mem_guard.default_config)

    def wait_done(self, control):
        until = time.monotonic() + 2
        while control.read()['status'] == 'running' and time.monotonic() < until:
            time.sleep(.01)
        return control.read()

    def test_need_is_size_times_1_15_plus_1_gib_or_8_gib_without_a_size(self):
        from model_control import load_need_bytes
        self.assertEqual(load_need_bytes({'sizeBytes': 16 * GiB}), int(16 * GiB * 1.15) + GiB)
        for row in ({}, {'sizeBytes': None}, {'sizeBytes': 0}, {'sizeBytes': True}, {'sizeBytes': 1.5e9}):
            with self.subTest(row=row):
                self.assertEqual(load_need_bytes(row), 8 * GiB)

    def test_load_refused_under_tight_with_the_409_message_and_top_suggestion(self):
        calls = []
        control = self.control(MutableStore(unloaded(mem=memory(2, 30, consumers=SIMULATORS))), calls)
        with self.assertRaises(ControlError) as refused:
            control.request('load', 'publisher/model')
        self.assertEqual(refused.exception.status, 409)
        self.assertEqual(refused.exception.message,
                         'Not enough free memory to load publisher/model safely: memory is tight '
                         '(macOS memory pressure: warning); heavy work (19.4 GB) refused. '
                         'Shut down unused iOS Simulators (`xcrun simctl shutdown all`).')
        self.assertEqual(calls, [])
        self.assertEqual(control.read()['status'], 'idle', 'no worker started')

    def test_load_refused_under_ok_without_headroom_and_without_a_memory_block(self):
        calls = []
        # ok (60% of 64 GiB = 38.4 GiB available) but a 32 GiB model needs 37.8 GiB: below the 6.4 GiB floor.
        control = self.control(MutableStore(unloaded(size=32 * GiB, mem=memory(1, 60))), calls)
        with self.assertRaises(ControlError) as refused:
            control.request('load', 'publisher/model')
        self.assertEqual(refused.exception.status, 409)
        self.assertIn('38.4 GB available', refused.exception.message)
        self.assertTrue(refused.exception.message.endswith('refused.'), 'no suggestion to add')
        control = self.control(MutableStore(unloaded(mem=None)), calls)
        with self.assertRaises(ControlError) as unknown:
            control.request('load', 'publisher/model')
        self.assertEqual(unknown.exception.status, 409)
        self.assertIn('memory state unknown', unknown.exception.message)
        self.assertEqual(calls, [])

    def test_load_admitted_when_memory_is_ok(self):
        calls = []
        store = MutableStore(unloaded(mem=memory(1, 60)))
        control = self.control(store, calls)
        self.assertEqual(control.request('load', 'publisher/model')['status'], 'running')
        until = time.monotonic() + 1
        while not calls and time.monotonic() < until:
            time.sleep(.01)
        self.assertEqual(calls, [('load', 'publisher/model')])
        store.data = inventory(loaded=True, state='idle', source='lms-ps')
        self.assertEqual(self.wait_done(control)['status'], 'succeeded')

    def test_unload_is_never_gated(self):
        calls = []
        data = inventory()
        data['memory'] = memory(4, 5, consumers=SIMULATORS)
        self.assertEqual(data['memory']['level'], 'critical')
        store = MutableStore(data)
        control = self.control(store, calls)
        self.assertEqual(control.request('unload', 'publisher/model')['status'], 'running')
        until = time.monotonic() + 1
        while not calls and time.monotonic() < until:
            time.sleep(.01)
        self.assertEqual(calls, [('unload', 'publisher/model')])
        store.data = inventory(loaded=False, state='unloaded', source='lmstudio-api')
        self.assertEqual(self.wait_done(control)['status'], 'succeeded')

    def test_worker_rechecks_memory_right_before_running_lms_load(self):
        calls = []
        ok, critical = unloaded(mem=memory(1, 60)), unloaded(mem=memory(4, 5, consumers=SIMULATORS))

        class Sequence:
            def __init__(self):
                self.reads = 0
            def read(self):
                self.reads += 1
                return ok if self.reads == 1 else critical

        control = self.control(Sequence(), calls)
        self.assertEqual(control.request('load', 'publisher/model')['status'], 'running')
        done = self.wait_done(control)
        self.assertEqual(done['status'], 'failed')
        self.assertTrue(done['message'].startswith('Not enough free memory to load publisher/model safely: '
                                                   'memory is critical'), done['message'])
        self.assertEqual(calls, [], 'lms load never ran')


class HttpControlTest(unittest.TestCase):
    def setUp(self):
        self.store = SnapshotStore()
        self.store.publish({**inventory(), 'schemaVersion': 1})
        self.server = MonitorServer(('127.0.0.1', 0), self.store)
        self.calls = []
        self.server.model_control.runner = lambda action, model_id: self.calls.append((action, model_id))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=2)
        connection.request(method, '/api/models/control', body=body, headers=headers or {})
        response = connection.getresponse()
        status, data = response.status, response.read()
        connection.close()
        return status, json.loads(data)

    def post(self, body=None, headers=None):
        port = self.server.server_address[1]
        default = {'Origin': f'http://127.0.0.1:{port}',
                   'Content-Type': 'application/json'}
        return self.request('POST', json.dumps(body or {'action': 'unload',
                                                        'modelId': 'publisher/model'}),
                            {**default, **(headers or {})})

    def test_same_origin_and_body_guards(self):
        self.assertEqual(self.request('POST', '{}', {'Content-Type': 'application/json'})[0], 403)
        self.assertEqual(self.post(headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.post(headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.post(headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.post(body={'action': 'unload', 'modelId': 'missing'})[0], 404)
        self.assertEqual(self.post(body={'action': 'unload', 'modelId': 'publisher/model',
                                         'extra': 1})[0], 400)
        port = self.server.server_address[1]
        headers = {'Origin': f'http://127.0.0.1:{port}', 'Content-Type': 'application/json'}
        duplicate = '{"action":"unload","modelId":"publisher/model","modelId":"other"}'
        self.assertEqual(self.request('POST', duplicate, headers)[0], 400)
        self.assertEqual(self.request('POST', 'x' * 1025, headers)[0], 413)
        self.assertEqual(self.calls, [])

    def test_click_request_is_async_and_status_is_separate_from_snapshot(self):
        initial_status, initial = self.request('GET')
        self.assertEqual((initial_status, initial['status']), (200, 'idle'))
        accepted_status, accepted = self.post()
        self.assertEqual((accepted_status, accepted['status']), (202, 'running'))
        self.assertEqual(accepted['modelId'], 'publisher/model')
        connection = HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=2)
        connection.request('GET', '/api/snapshot')
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        connection.close()
        until = time.monotonic() + 1
        while not self.calls and time.monotonic() < until:
            time.sleep(.01)
        self.assertEqual(self.calls, [('unload', 'publisher/model')])


if __name__ == '__main__':
    unittest.main()
