import json
import fcntl
import io
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from http.client import HTTPConnection
from contextlib import redirect_stdout
# LIVE_ENDPOINT is the installed monitor's endpoint.json (agiw-status reads it); no test may write it.
from server import ENDPOINT_PATH as LIVE_ENDPOINT, MonitorServer, SnapshotStore, sample


_endpoint_temp = None
_endpoint_patch = None


def setUpModule():
    # Safety net: any server.main() in this module publishes to a temp endpoint, never the live one.
    global _endpoint_temp, _endpoint_patch
    _endpoint_temp = tempfile.TemporaryDirectory()
    _endpoint_patch = patch('server.ENDPOINT_PATH', Path(_endpoint_temp.name) / 'endpoint.json')
    _endpoint_patch.start()


def tearDownModule():
    # Python 3.9's exit-time TemporaryDirectory warning otherwise hides fixture leaks.
    global _endpoint_temp, _endpoint_patch
    if _endpoint_patch is not None:
        _endpoint_patch.stop()
        _endpoint_patch = None
    if _endpoint_temp is not None:
        _endpoint_temp.cleanup()
        _endpoint_temp = None

def snapshot(state='idle', sampled=None):
    return {'schemaVersion':1,'sampledAt':sampled or time.time(), 'models':[
        {'id':'model-a','name':'Model A','host':'mac','state':state,'loaded':True,'ageSeconds':0,'source':'lms-ps'}],
        'sources':[{'id':'lms-ps','state':'live'}]}

class StoreTest(unittest.TestCase):
    def test_missing_activity_is_a_gap_not_zero(self):
        store=SnapshotStore();data=snapshot('loaded');data['sources']=[]
        store.publish(data)
        self.assertIsNone(store.read()['history'][-1]['active'])
        self.assertIsNone(store.read()['history'][-1]['loaded'])

    def test_live_cli_with_unknown_row_activity_is_not_confident_zero(self):
        store=SnapshotStore();store.publish(snapshot('loaded'))
        sample=store.read()
        self.assertFalse(sample['activityKnown'])
        self.assertIsNone(sample['history'][-1]['active'])

    def test_stale_loaded_row_is_not_counted_as_zero_activity(self):
        store=SnapshotStore();data=snapshot('idle');data['models'][0]['ageSeconds']=4
        store.publish(data)
        sample=store.read()
        self.assertFalse(sample['activityKnown'])
        self.assertIsNone(sample['history'][-1]['active'])

    def test_empty_successful_cli_roster_is_confirmed_zero(self):
        store=SnapshotStore();data=snapshot();data['models']=[]
        store.publish(data)
        sample=store.read()
        self.assertTrue(sample['activityKnown'])
        self.assertEqual(sample['history'][-1]['active'],0)

    def test_evidence_loss_and_recovery_are_explicit_events(self):
        store=SnapshotStore()
        store.publish(snapshot('idle'))
        fallback=snapshot('loaded');fallback['models'][0]['source']='lmstudio-api'
        fallback['sources']=[{'id':'lmstudio-api','state':'live'}, {'id':'lms-ps','state':'unavailable'}]
        store.publish(fallback)
        recovery=snapshot('generating')
        store.publish(recovery)
        events=store.read()['events']
        self.assertEqual(events[0]['kind'],'evidence-recovered')
        self.assertEqual(events[0]['before'],'loaded')
        self.assertEqual(events[0]['state'],'generating')
        self.assertEqual(events[1]['kind'],'evidence-loss')
        self.assertEqual(events[1]['label'],'Activity visibility lost; inventory only')
        self.assertEqual(events[1]['before'],'idle')
        self.assertEqual(events[1]['state'],'loaded')

    def test_confirmed_unload_and_load_are_state_changes_not_visibility_events(self):
        store=SnapshotStore()
        store.publish(snapshot('idle'))
        unloaded=snapshot('unloaded');unloaded['models'][0].update(
            loaded=False,source='lmstudio-api')
        unloaded['sources']=[{'id':'lmstudio-api','state':'live'}, {'id':'lms-ps','state':'live'}]
        store.publish(unloaded)
        loaded=snapshot('idle')
        store.publish(loaded)
        events=store.read()['events']
        self.assertEqual(events[0]['kind'],'state-change')
        self.assertEqual(events[0]['label'],'Observed runtime state changed')
        self.assertEqual(events[1]['kind'],'state-change')
        self.assertEqual(events[1]['before'],'idle')
        self.assertEqual(events[1]['state'],'unloaded')

    def test_transition_is_observed_not_invented(self):
        store=SnapshotStore();store.publish(snapshot());store.publish(snapshot('busy'))
        data=store.read();self.assertEqual(len(data['events']),1)
        self.assertEqual(data['events'][0]['before'],'idle')
        self.assertEqual(data['events'][0]['kind'],'state-change')
        self.assertEqual(data['history'][-1]['active'],1)
        data['models'][0]['state']='poisoned'
        self.assertEqual(store.read()['models'][0]['state'],'busy')

    def test_history_and_events_are_bounded(self):
        store=SnapshotStore()
        for n in range(160):store.publish(snapshot('busy' if n%2 else 'idle'))
        self.assertEqual(len(store.read()['history']),90)
        self.assertEqual(len(store.read()['events']),50)
        self.assertEqual(store.read()['intervalSeconds'],1)


class AutoUnloadRouteTest(unittest.TestCase):
    @staticmethod
    def quiet_route():
        return {'pipeline': {'status': 'idle', 'recoveryRequired': False,
                             'pendingMarkerObserved': False, 'pendingMarkerUnreadable': False,
                             'pipelines': []},
                'onlineCodeMode': {'state': 'inactive', 'active': False,
                                   'runCounts': {'running': 0, 'queued': 0, 'unresolved': 0}}}

    def test_worker_guard_holds_router_admission_during_route_check_and_action(self):
        import server
        import telemetry
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / 'owner.lock'
            lock.write_bytes(b'')
            lock.chmod(0o600)
            def check_route():
                contender = os.open(lock, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_SH | fcntl.LOCK_NB)
                finally:
                    os.close(contender)
                return self.quiet_route()
            install = lambda _: {'state': 'absent', 'inProgress': False}
            with server.router_unload_guard(lock_path=lock, route_read=check_route,
                                            install_read=install):
                self.assertTrue(telemetry._monitor_holds_router_owner())
            self.assertFalse(telemetry._monitor_holds_router_owner())
            contender = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender)

    def test_guard_is_still_held_by_model_worker_while_inventory_confirms(self):
        import server
        from model_control import ModelControl

        class Store:
            def __init__(self, data):
                self.data = data
            def read(self):
                return self.data

        def inventory(loaded=True):
            return {'sampledAt': time.time(), 'models': [
                {'id': 'lab/model', 'name': 'lab/model', 'host': 'mac', 'loaded': loaded,
                 'state': 'idle' if loaded else 'unloaded', 'queued': 0,
                 'source': 'lms-ps' if loaded else 'lmstudio-api', 'ageSeconds': 0,
                 'modelKey': 'lab/model', 'instanceId': 'lab/model' if loaded else None,
                 'loadedInstanceIds': ['lab/model'] if loaded else []}],
                'sources': [{'id': 'lms-ps', 'state': 'live'},
                            {'id': 'lmstudio-api', 'state': 'live'}]}

        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / 'owner.lock'
            lock.write_bytes(b'')
            lock.chmod(0o600)
            store = Store(inventory())
            entered, proceed = threading.Event(), threading.Event()
            def runner(*_):
                entered.set()
                self.assertTrue(proceed.wait(3))
            control = ModelControl(store, runner=runner)
            guard = lambda: server.router_unload_guard(
                lock_path=lock, route_read=self.quiet_route,
                install_read=lambda _: {'state': 'absent', 'inProgress': False})
            control.request('unload', 'lab/model', action_guard=guard)
            self.assertTrue(entered.wait(2))
            contender = os.open(lock, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_SH | fcntl.LOCK_NB)
                proceed.set()
                until = time.monotonic() + 3
                while 'waiting for fresh inventory' not in control.read()['message'] and time.monotonic() < until:
                    time.sleep(.01)
                self.assertIn('waiting for fresh inventory', control.read()['message'])
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_SH | fcntl.LOCK_NB)
                store.data = inventory(loaded=False)
                until = time.monotonic() + 3
                while control.read()['status'] == 'running' and time.monotonic() < until:
                    time.sleep(.01)
                self.assertEqual(control.read()['status'], 'succeeded')
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender)

    def test_worker_guard_fails_closed_on_busy_missing_or_unquiet_route(self):
        import server
        import telemetry
        from model_control import ControlError
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / 'owner.lock'
            install = lambda _: {'state': 'absent', 'inProgress': False}
            with self.assertRaises(ControlError):
                with server.router_unload_guard(lock_path=lock, route_read=self.quiet_route,
                                                install_read=install):
                    self.fail('missing lock admitted auto-unload')
            lock.write_bytes(b'')
            lock.chmod(0o600)
            contender = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(ControlError):
                    with server.router_unload_guard(lock_path=lock, route_read=self.quiet_route,
                                                    install_read=install):
                        self.fail('busy lock admitted auto-unload')
            finally:
                os.close(contender)
            active = self.quiet_route()
            active['onlineCodeMode']['runCounts']['queued'] = 1
            with self.assertRaises(ControlError):
                with server.router_unload_guard(lock_path=lock, route_read=lambda: active,
                                                install_read=install):
                    self.fail('queued route admitted auto-unload')
            self.assertFalse(telemetry._monitor_holds_router_owner())

    def test_worker_guard_rejects_installed_fence_bound_to_another_inode(self):
        import server
        from model_control import ControlError
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / 'owner.lock'
            lock.write_bytes(b'')
            lock.chmod(0o600)
            info = lock.stat()
            fence = Path(temp) / 'install-fence.json'
            fence.write_text(json.dumps({'ownerLock': {'dev': info.st_dev, 'ino': info.st_ino + 1}}))
            fence.chmod(0o600)
            install = lambda _: {'state': 'installed', 'inProgress': False, 'ownerLockBound': True}
            with self.assertRaises(ControlError):
                with server.router_unload_guard(lock_path=lock, route_read=self.quiet_route,
                                                install_read=install):
                    self.fail('mismatched fence admitted auto-unload')
            fence.write_text(json.dumps({'ownerLock': {'dev': info.st_dev, 'ino': info.st_ino}}))
            with server.router_unload_guard(lock_path=lock, route_read=self.quiet_route,
                                            install_read=install):
                pass

    def test_fresh_router_read_has_the_closed_shape_auto_unload_requires(self):
        import auto_unload
        import server
        import telemetry

        router = {'root': '/tmp/fixture', 'install': {'state': 'absent', 'generation': None,
                  'barrier': False, 'inProgress': False, 'ownerLockBound': None},
                  'admission': {'policy': 'multi', 'source': 'per-run-router',
                  'drainState': 'not-applicable', 'draining': False, 'sharedHolders': [],
                  'callerOverride': None}, 'rows': [], 'primary': None, 'truncated': False,
                  'error': None, 'legacyPresent': False, 'layout': 'per-run',
                  'collisions': [], 'lanes': {}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            root = path / 'router'
            for directory in (root, root / 'archive', root / 'checkpoints'):
                directory.mkdir(mode=0o700)
                directory.chmod(0o700)
            (root / 'owner.lock').write_bytes(b'')
            (root / 'owner.lock').chmod(0o600)
            with patch.object(telemetry, '_router_observation', return_value=router) as read, \
                 patch.object(telemetry, '_ROUTER_ROOT', root), \
                 patch.object(telemetry, '_PENDING_PATH', path / 'pending'), \
                 patch.object(telemetry, '_LAUNCHER_ROOT', path / 'launcher'), \
                 patch.object(telemetry, '_READINESS_PATH', path / 'readiness'):
                fresh = server.fresh_route()
        read.assert_called_once()
        self.assertEqual(fresh['pipeline']['status'], 'idle')
        self.assertEqual(fresh['onlineCodeMode']['runCounts'],
                         {'running': 0, 'queued': 0, 'unresolved': 0})
        self.assertIsNone(auto_unload.router_problem(fresh['pipeline'], fresh['onlineCodeMode']))

    def test_running_owner_controls_block_auto_unload(self):
        import server
        class Status:
            def __init__(self, status):
                self.status = status
                self.worker_active = False
                self.settlement = 'confirmed'
                self.cleanup_confirmed = True
            def read(self):
                return {'status': self.status, 'workerActive': self.worker_active,
                        'settlement': self.settlement, 'cleanupConfirmed': self.cleanup_confirmed}
        class Owner:
            online_code_repair = Status('running')
            model_control = Status('idle')
        owner = Owner()
        self.assertIn('Online Code', server.monitor_busy(owner))
        owner.online_code_repair.status = 'idle'
        owner.model_control.status = 'running'
        self.assertIn('model', server.monitor_busy(owner))
        owner.model_control.status = 'idle'
        self.assertIsNone(server.monitor_busy(owner))
        owner.model_control.worker_active = True
        self.assertIn('unconfirmed', server.monitor_busy(owner))
        owner.model_control.worker_active = False
        owner.model_control.settlement = 'unconfirmed'
        self.assertIn('unconfirmed', server.monitor_busy(owner))
        owner.model_control.settlement = 'confirmed'
        owner.model_control.cleanup_confirmed = False
        self.assertIn('unconfirmed', server.monitor_busy(owner))
        owner.model_control.cleanup_confirmed = True
        self.assertIsNone(server.monitor_busy(owner))

class ClientIntegrationTest(unittest.TestCase):
    def test_client_metadata_is_published_without_runtime_attribution(self):
        class OneTick:
            checks = 0
            def is_set(self):
                self.checks += 1
                return self.checks > 1
            def wait(self, _):
                return True

        source = snapshot('idle')
        client = {'id':'codex','label':'Codex','model':'gpt-6-sol',
                  'modelState':'observed','activity':'unknown','models':[]}
        metadata = {'id':'codex-metadata','state':'live'}
        store = SnapshotStore()
        with patch('server.collect_snapshot', return_value=source), \
             patch('server.collect_activity', return_value={'runs':[], 'sources':[]}), \
             patch('server.collect_clients', return_value=([client],[metadata])):
            sample(store, OneTick())
        published = store.read()
        self.assertEqual(published['clients'], [client])
        self.assertIn(metadata, published['sources'])
        self.assertTrue(published['activityKnown'])
        self.assertEqual(published['history'][-1]['active'], 0)

    def test_client_metadata_failure_does_not_stop_runtime_sample(self):
        class OneTick:
            checks = 0
            def is_set(self):
                self.checks += 1
                return self.checks > 1
            def wait(self, _):
                return True

        store = SnapshotStore()
        with patch('server.collect_snapshot', return_value=snapshot('idle')), \
             patch('server.collect_activity', return_value={'runs':[], 'sources':[]}), \
             patch('server.collect_clients', side_effect=RuntimeError('reader failed')):
            sample(store, OneTick())
        published = store.read()
        self.assertEqual(published['clients'], [])
        self.assertEqual(published['sources'][-1]['state'], 'error')
        self.assertEqual(published['history'][-1]['active'], 0)


class ShutdownTest(unittest.TestCase):
    def test_auto_unloader_exists_only_for_app_launch_and_closes_on_exit(self):
        import server
        threads = []

        class FakeThread:
            def __init__(self, *_, **kwargs):
                threads.append(kwargs)
            def start(self):
                pass

        class FakeControl:
            def __init__(self):
                self.calls = []
            def read(self):
                return {'status': 'idle'}
            def request(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {'status': 'running', 'operationId': 'fake'}
            def cancel(self):
                return self.read()
            def join(self, timeout):
                return True

        class FakeServer:
            server_address = ('127.0.0.1', 12345)
            def __init__(self):
                self.online_code_repair = None
                self.model_control = FakeControl()
                self.auto_unloader = None
            def serve_forever(self, **_):
                pass
            def server_close(self):
                pass

        class FakeUnloader:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.closed = False
            def close(self):
                self.closed = True

        class FakeGuard:
            def close(self):
                pass

        instances = []
        def build_auto(**kwargs):
            instance = FakeUnloader(**kwargs)
            instances.append(instance)
            return instance

        for args, expected in ((['server.py'], False),
                               (['server.py', '--parent-pid', '123'], True),
                               (['server.py', '--parent-pid', '456'], False)):
            fake_server = FakeServer()
            with self.subTest(args=args), \
                 patch.object(server, 'MonitorServer', return_value=fake_server), \
                 patch.object(server, 'AutoUnloader', side_effect=build_auto), \
                 patch.object(server, 'memory_guard', return_value=FakeGuard()), \
                 patch.object(server, 'mac_online_code_controls_enabled', return_value=True), \
                 patch.object(server.os, 'getppid', return_value=123), \
                 patch.object(server.threading, 'Thread', FakeThread), \
                 patch.object(server, 'write_endpoint'), \
                 patch.object(server, 'stop_windows_worker_probe'), \
                 patch.object(server.signal, 'signal'), \
                 patch.object(sys, 'argv', args), redirect_stdout(io.StringIO()):
                server.main()
            self.assertEqual(fake_server.auto_unloader is not None, expected)
            self.assertEqual(fake_server.model_control.calls, [], 'startup must not request a model action')
            if expected:
                self.assertTrue(instances[-1].closed)
                self.assertIs(threads[-2]['kwargs']['auto_unloader'], instances[-1])
                self.assertIsInstance(threads[-2]['kwargs']['guard'], FakeGuard)
                instances[-1].kwargs['unload_fn']('publisher/model')
                self.assertEqual(fake_server.model_control.calls[0][0], ('unload', 'publisher/model'))
                self.assertIs(fake_server.model_control.calls[0][1]['action_guard'],
                              server.router_unload_guard)

    def test_sigterm_cancels_owner_children_before_bounded_worker_join(self):
        import server
        handlers = {}
        stopped = threading.Event()
        events = []
        case = self

        class FakeGuard:
            @staticmethod
            def close():
                events.append('guard-close')
                return []

        guard = FakeGuard()

        class FakeRepair:
            @staticmethod
            def cancel():
                events.append('repair-cancel')

            @staticmethod
            def join(timeout):
                events.append(('repair-join', timeout))
                return True

        class FakeModelControl:
            @staticmethod
            def cancel():
                events.append('model-cancel')

            @staticmethod
            def join(timeout):
                events.append(('model-join', timeout))
                return True

        class FakeServer:
            server_address = ('127.0.0.1', 12345)
            online_code_repair = FakeRepair()
            model_control = FakeModelControl()

            def serve_forever(self, **_):
                handlers[signal.SIGTERM](None, None)
                case.assertIn('repair-cancel', events)
                case.assertIn('model-cancel', events)
                # Paused jobs are resumed inside the SIGTERM handler, before the poll loop ends.
                case.assertIn('guard-close', events)
                self.assert_shutdown_started()

            @staticmethod
            def assert_shutdown_started():
                if not stopped.wait(1):
                    raise AssertionError('server shutdown was not requested')

            @staticmethod
            def shutdown():
                stopped.set()

            @staticmethod
            def server_close():
                events.append('server-close')

        with patch.object(server, 'MonitorServer', return_value=FakeServer()), \
             patch.object(server, 'sample') as sampler, \
             patch.object(server, 'memory_guard', return_value=guard), \
             patch.object(server, 'cancel_windows_worker_probe') as cancel, \
             patch.object(server, 'stop_windows_worker_probe', return_value=True) as stop, \
             patch.object(server, 'write_endpoint') as endpoint, \
             patch.object(server.signal, 'signal',
                   side_effect=lambda sig, handler: handlers.setdefault(sig, handler)), \
             patch.object(sys, 'argv', ['server.py']):
            with redirect_stdout(io.StringIO()):
                server.main()
        # main() publishes its port for agiw-status; the test must never overwrite the live monitor's file.
        endpoint.assert_called_once_with(12345)
        cancel.assert_called_once_with()
        stop.assert_called_once_with(timeout=.45)
        self.assertIn(('repair-join', .45), events)
        self.assertIn(('model-join', .45), events)
        self.assertLess(events.index('repair-cancel'), events.index(('repair-join', .45)))
        self.assertLess(events.index(('repair-join', .45)), events.index('server-close'))
        self.assertLess(events.index('model-cancel'), events.index(('model-join', .45)))
        self.assertLess(events.index(('model-join', .45)), events.index('server-close'))
        # The sampler gets the monitor's guard; shutdown resumes anything it paused.
        self.assertIs(sampler.call_args.kwargs['guard'], guard)
        self.assertGreaterEqual(events.count('guard-close'), 1)  # the handler, then the finally (idempotent)
        self.assertLess(events.index('guard-close'), events.index('server-close'))


class EndpointTest(unittest.TestCase):
    """2026-09-27: a test's server.main() overwrote the live endpoint.json with port 12345, and
    agiw-status reported the running monitor unavailable until it was restored by hand."""

    def test_main_writes_only_the_patched_endpoint_never_the_live_one(self):
        import server

        class FakeServer:
            server_address = ('127.0.0.1', 12345)
            online_code_repair = None

            def serve_forever(self, **_):
                pass

            def server_close(self):
                pass

        def live_signature():
            try:
                stat = LIVE_ENDPOINT.stat()
            except FileNotFoundError:
                return None
            return stat.st_ino, stat.st_mtime_ns, stat.st_size

        real_replace = os.replace

        def replace(source, target, **options):
            # Fail before the rename if the patch stops reaching write_endpoint (e.g. an early-bound default).
            if Path(target) == LIVE_ENDPOINT:
                os.unlink(source)  # leave no temp file beside the live endpoint either
                raise AssertionError(f'server.main() tried to replace the live {target}')
            return real_replace(source, target, **options)

        before = live_signature()
        with tempfile.TemporaryDirectory() as temp:
            endpoint = Path(temp) / 'state' / 'endpoint.json'
            with patch.object(server, 'ENDPOINT_PATH', endpoint), \
                 patch.object(server, 'MonitorServer', return_value=FakeServer()), \
                 patch.object(server, 'sample'), \
                 patch.object(server, 'memory_guard', return_value=None), \
                 patch.object(server, 'stop_windows_worker_probe', return_value=True), \
                 patch.object(server.os, 'replace', side_effect=replace), \
                 patch.object(server.signal, 'signal'), \
                 patch.object(sys, 'argv', ['server.py']):
                with redirect_stdout(io.StringIO()):
                    server.main()
            published = json.loads(endpoint.read_text())
            self.assertEqual((published['port'], published['pid']), (12345, os.getpid()))
            self.assertEqual([p.name for p in endpoint.parent.iterdir()], ['endpoint.json'])
        self.assertEqual(live_signature(), before, f'a test rewrote the live {LIVE_ENDPOINT}')


class MemoryGuardSampleTest(unittest.TestCase):
    """sample() carries snapshot['memory'] and a 'mem-guard' source row; memory never breaks it."""

    class Stop:
        def __init__(self, rounds):
            self.rounds = rounds
            self.checks = 0
        def is_set(self):
            self.checks += 1
            return self.checks > self.rounds
        def wait(self, _):
            return True

    class Feed:
        def fingerprints(self):
            return {}
        def changed(self):
            return [], {}
        def set_baseline(self, _prints):
            pass
        def overlay(self, snapshot, _groups):
            return snapshot

    class Callers:
        def sample(self, _models):
            return None, {'id': 'local-callers', 'state': 'idle'}

    class FakeGuard:
        def __init__(self, fail=False):
            self.calls = []
            self.fail = fail
        def sample(self, gpu_alloc_bytes=None, models=None):
            self.calls.append((gpu_alloc_bytes, models))
            if self.fail:
                raise RuntimeError('probe exploded')
            return ({'level': 'tight', 'reasons': ['macOS memory pressure: warning'], 'consumers': [],
                     'paused': [], 'suggestions': [], 'sampledAt': 1.0},
                    {'id': 'mem-guard', 'label': 'Memory guard', 'state': 'live', 'ageSeconds': 0.0,
                     'detail': 'Memory tight'})

    def run_sampler(self, guard, rounds=1, full_interval=1.0, auto_unloader=None):
        store = SnapshotStore()
        gpu = {'allocatedBytes': 40 << 30, 'utilizationPercent': 3}
        with patch('server.collect_snapshot', side_effect=lambda: snapshot('idle')), \
             patch('server.collect_activity', return_value={'runs': [], 'sources': []}), \
             patch('server.collect_clients', return_value=([], [])), \
             patch('server.mac_gpu', return_value=(gpu, {'id': 'mac-gpu', 'state': 'live'})), \
             patch('server.LocalCallers', return_value=self.Callers()):
            sample(store, self.Stop(rounds), feed=self.Feed(), full_interval=full_interval,
                   guard=guard, auto_unloader=auto_unloader)
        return store.read()

    def test_auto_unload_ticks_only_after_a_complete_published_sample(self):
        class FakeUnloader:
            def __init__(self):
                self.calls = []
            def state(self):
                return {'enabled': True, 'blocked': 'waiting for the first sample'}
            def tick(self, data):
                self.calls.append(data)
        unloader = FakeUnloader()
        published = self.run_sampler(self.FakeGuard(), rounds=3, full_interval=0.0,
                                     auto_unloader=unloader)
        self.assertEqual(len(unloader.calls), 3)
        self.assertEqual(published['autoUnload'], unloader.state())
        self.assertEqual(unloader.calls[-1]['sampledAt'], published['sampledAt'])
        self.assertEqual(unloader.calls[-1]['models'], published['models'])
        self.assertEqual(unloader.calls[-1]['memory'], published['memory'])
        self.assertEqual(unloader.calls[-1]['activity'], published['activity'])
        unloader = FakeUnloader()
        self.run_sampler(self.FakeGuard(), rounds=3, full_interval=60.0, auto_unloader=unloader)
        self.assertEqual(len(unloader.calls), 1, 'fast overlays must not make unload decisions')

    def test_failed_full_sample_does_not_tick_auto_unload(self):
        class FakeUnloader:
            def __init__(self):
                self.calls = 0
            def state(self):
                return {'enabled': True}
            def tick(self, _data):
                self.calls += 1
        unloader = FakeUnloader()
        store = SnapshotStore()
        with patch('server.collect_snapshot', side_effect=RuntimeError('telemetry failed')):
            sample(store, self.Stop(2), feed=self.Feed(), full_interval=0.0,
                   guard=self.FakeGuard(), auto_unloader=unloader)
        self.assertEqual(unloader.calls, 0)
        self.assertIsNone(store.read())

    def test_block_and_source_row_come_from_the_guard_with_gpu_and_models(self):
        guard = self.FakeGuard()
        published = self.run_sampler(guard)
        self.assertEqual(published['memory']['level'], 'tight')
        rows = [s for s in published['sources'] if s['id'] == 'mem-guard']
        self.assertEqual([r['state'] for r in rows], ['live'])
        self.assertEqual(len(guard.calls), 1)
        gpu_bytes, models = guard.calls[0]
        self.assertEqual(gpu_bytes, 40 << 30, 'reuses macGpu.allocatedBytes')
        self.assertEqual([m['id'] for m in models], ['model-a'], 'reuses the loaded model rows')

    def test_one_guard_sample_per_full_sample_never_on_the_fast_path(self):
        guard = self.FakeGuard()
        self.run_sampler(guard, rounds=3, full_interval=0.0)
        self.assertEqual(len(guard.calls), 3)
        guard = self.FakeGuard()
        self.run_sampler(guard, rounds=3, full_interval=60.0)
        self.assertEqual(len(guard.calls), 1, 'the change-driven overlays in between never probe memory')

    def test_guard_failure_is_unknown_and_unavailable_not_a_crashed_sampler(self):
        for guard in (self.FakeGuard(fail=True), None):
            with self.subTest(guard=guard), patch('server.memory_guard', return_value=None):
                published = self.run_sampler(guard)
                self.assertIsNotNone(published, 'the sample was still published')
                self.assertEqual(published['memory']['level'], 'unknown')
                self.assertEqual([s['state'] for s in published['sources'] if s['id'] == 'mem-guard'],
                                 ['unavailable'])
                self.assertEqual([m['id'] for m in published['models']], ['model-a'])

    def test_real_guard_with_a_raising_probe_reads_unknown(self):
        import tempfile
        from pathlib import Path
        import mem_guard

        class RaisingProbes:
            def read(self, gpu_alloc_bytes=None):
                raise OSError('sysctl refused')
            def consumers(self, **_):
                raise OSError('ps refused')

        with tempfile.TemporaryDirectory() as tmp:
            registry = mem_guard.Registry(Path(tmp) / 'state')
            guard = mem_guard.Guard(cfg=mem_guard.default_config(), probes=RaisingProbes(),
                                    registry=registry, notifier=lambda *_: self.fail('notified'))
            published = self.run_sampler(guard)
            self.assertEqual(published['memory']['level'], 'unknown')
            self.assertEqual(set(published['memory']), {'level', 'reasons', 'pressure', 'availablePercent',
                                                        'ramBytes', 'swapUsedBytes', 'swapTotalBytes',
                                                        'compressedBytes', 'wiredBytes', 'vmFreeBytes',
                                                        'gpuAllocBytes', 'consumers', 'paused',
                                                        'suggestions', 'sampledAt'})
            self.assertEqual([s['state'] for s in published['sources'] if s['id'] == 'mem-guard'],
                             ['unavailable'])
            json.dumps(published, allow_nan=False)

    def test_failing_telemetry_never_stops_the_memory_watchdog(self):
        """Review finding: the tick ran after collect_snapshot() in the same try, so a telemetry
        exception skipped it on every sample and paused jobs were never resumed."""
        guard = self.FakeGuard()
        store = SnapshotStore()
        with patch('server.collect_snapshot', side_effect=RuntimeError('malformed state file')), \
             patch('server.LocalCallers', return_value=self.Callers()):
            sample(store, self.Stop(3), feed=self.Feed(), full_interval=0.0, guard=guard)
        self.assertEqual(len(guard.calls), 3, 'one watchdog tick per full sample')
        self.assertEqual(guard.calls[0], (None, None))
        self.assertIsNone(store.read(), 'the failed samples themselves are still not published')

    def test_without_a_guard_the_sampler_builds_a_read_only_one(self):
        with patch('server.memory_guard', return_value=self.FakeGuard()) as build:
            self.run_sampler(None)
        build.assert_called_once_with(watchdog=False)

class TransportTest(unittest.TestCase):
    def setUp(self):
        self.store=SnapshotStore();self.store.publish(snapshot())
        self.server=MonitorServer(('127.0.0.1',0),self.store)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def get(self,path,headers=None,method='GET'):
        c=HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=2)
        c.request(method,path,headers=headers or {});r=c.getresponse();data=r.read();status=r.status;headers=dict(r.getheaders());c.close()
        return status,data,headers
    def post_json(self, path, payload, headers=None):
        port = self.server.server_address[1]
        request_headers = {'Origin': f'http://127.0.0.1:{port}',
                           'Content-Type': 'application/json'}
        request_headers.update(headers or {})
        c=HTTPConnection('127.0.0.1',port,timeout=2)
        c.request('POST',path,body=payload,headers=request_headers)
        r=c.getresponse();data=r.read();status=r.status;c.close()
        return status,data
    def wait_active(self, count):
        until = time.monotonic() + 1
        while time.monotonic() < until and self.server._active_connections != count:
            time.sleep(.01)
        self.assertEqual(self.server._active_connections, count)
    def test_snapshot_is_read_only_and_uncached(self):
        status,raw,headers=self.get('/api/snapshot')
        self.assertEqual(status,200);self.assertEqual(json.loads(raw)['sequence'],1)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertNotIn('Access-Control-Allow-Origin',headers)
        self.assertEqual(self.get('/api/snapshot',method='POST')[0],404)
    def test_auto_unload_endpoint_is_app_only_and_requires_exact_same_origin_input(self):
        self.assertEqual(self.get('/api/models/auto-unload')[0], 501)
        self.assertEqual(self.post_json('/api/models/auto-unload', b'{"enabled":true}')[0], 501)

        class FakeUnloader:
            def __init__(self):
                self.enabled = False
                self.calls = []
            def state(self):
                return {'enabled': self.enabled, 'blocked': None}
            def set_enabled(self, enabled):
                self.calls.append(enabled)
                self.enabled = enabled
                return self.state()

        unloader = FakeUnloader()
        self.server.auto_unloader = unloader
        code, raw, headers = self.get('/api/models/auto-unload')
        self.assertEqual((code, json.loads(raw)), (200, unloader.state()))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.post_json('/api/models/auto-unload', b'{"enabled":false}',
                                        {'Origin': 'http://evil.example'})[0], 403)
        for bad in (b'{"enabled":"false"}', b'{"enabled":0}', b'{"enabled":false,"extra":1}',
                    b'{"enabled":false,"enabled":true}'):
            self.assertEqual(self.post_json('/api/models/auto-unload', bad)[0], 400)
        self.assertEqual(unloader.calls, [])
        code, raw = self.post_json('/api/models/auto-unload', b'{"enabled":true}')
        self.assertEqual((code, json.loads(raw)['enabled']), (200, True))
        self.assertEqual(json.loads(self.get('/api/models/auto-unload')[1])['enabled'], True)
        code, raw = self.post_json('/api/models/auto-unload', b'{"enabled":false}')
        self.assertEqual((code, json.loads(raw)['enabled']), (200, False))
        self.assertEqual(json.loads(self.get('/api/models/auto-unload')[1])['enabled'], False)
        self.assertEqual(unloader.calls, [True, False])

    def test_auto_unload_endpoint_reports_config_and_write_failures(self):
        class RefusingUnloader:
            def state(self):
                return {'enabled': False}
            def set_enabled(self, _enabled):
                raise ValueError('invalid config')
        self.server.auto_unloader = RefusingUnloader()
        code, raw = self.post_json('/api/models/auto-unload', b'{"enabled":true}')
        self.assertEqual((code, json.loads(raw)['message']), (409, 'invalid config'))
        self.server.auto_unloader.set_enabled = lambda _: (_ for _ in ()).throw(OSError('private path'))
        code, raw = self.post_json('/api/models/auto-unload', b'{"enabled":true}')
        self.assertEqual((code, json.loads(raw)['message']), (503, 'Could not save the auto-unload setting.'))
    def test_online_code_mode_module_is_served_as_script(self):
        status, raw, headers = self.get('/online-code-mode.mjs')
        self.assertEqual(status, 200)
        self.assertIn(b'onlineCodeModeView', raw)
        self.assertEqual(headers['Content-Type'], 'text/javascript; charset=utf-8')
    def test_online_code_repair_requires_explicit_same_origin_action(self):
        class StubRepair:
            count = 0
            def read(self):
                return {'status':'idle','message':'No check requested.','steps':[]}
            def request(self):
                self.count += 1
                return {'status':'running','message':'Checking.','steps':[]}
        repair = StubRepair()
        self.server.online_code_repair = repair
        code, raw, headers = self.get('/api/online-code-mode/repair')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['status'], 'idle')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(repair.count, 0)
        self.assertEqual(self.post_json('/api/online-code-mode/repair',
                         b'{"action":"check-and-repair"}', {'Origin':'http://evil.example'})[0], 403)
        self.assertEqual(self.post_json('/api/online-code-mode/repair',
                         b'{"action":"start-work"}')[0], 400)
        self.assertEqual(self.post_json('/api/online-code-mode/repair',
                         b'{"action":"check-and-repair","action":"check-and-repair"}')[0], 400)
        self.assertEqual(repair.count, 0)
        code, raw = self.post_json('/api/online-code-mode/repair',
                                   b'{"action":"check-and-repair"}')
        self.assertEqual(code, 202)
        self.assertEqual(json.loads(raw)['status'], 'running')
        self.assertEqual(repair.count, 1)

    def test_universal_entry_requires_exact_same_origin_readiness_action(self):
        class StubEntry:
            calls = 0
            def read(self):
                return {'status':'idle','message':'No check requested.','steps':[],
                        'operationId':0,'action':None}
            def request_entry(self):
                self.calls += 1
                return {'status':'running','message':'Invoking readiness.','steps':[],
                        'operationId':self.calls,'action':'readiness'}
        entry = StubEntry()
        self.server.online_code_repair = entry
        code, raw, headers = self.get('/api/online-code-mode/entry')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['action'], None)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.post_json('/api/online-code-mode/entry',
                         b'{"action":"readiness"}', {'Origin':'http://evil.example'})[0], 403)
        self.assertEqual(self.post_json('/api/online-code-mode/entry',
                         b'{"action":"check-and-repair"}')[0], 400)
        self.assertEqual(self.post_json('/api/online-code-mode/entry',
                         b'{"action":"readiness","action":"readiness"}')[0], 400)
        self.assertEqual(self.post_json('/api/online-code-mode/entry',
                         b'{"action":"readiness","task":"private"}')[0], 400)
        self.assertEqual(entry.calls, 0)
        code, raw = self.post_json('/api/online-code-mode/entry', b'{"action":"readiness"}')
        self.assertEqual(code, 202)
        self.assertEqual(json.loads(raw)['action'], 'readiness')
        self.assertEqual(entry.calls, 1)

    def test_inference_fix_requires_explicit_scope_and_same_origin(self):
        class StubFix:
            scopes = []
            def read(self):
                return {'status': 'idle', 'message': 'No fix requested.', 'steps': [],
                        'operationId': 0, 'action': None}
            def request_fix(self, scope):
                self.scopes.append(scope)
                return {'status': 'running', 'message': 'Checking.', 'steps': [],
                        'operationId': len(self.scopes), 'action': f'fix-{scope}'}
        control = StubFix()
        self.server.online_code_repair = control
        code, raw, headers = self.get('/api/inference/fix')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['status'], 'idle')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(control.scopes, [])
        self.assertEqual(self.post_json('/api/inference/fix', b'{"scope":"local"}',
                                        {'Origin':'http://evil.example'})[0], 403)
        for invalid in (b'{"scope":"all","task":"private"}', b'{"scope":"local","task":"private"}',
                        b'{"scope":"local","scope":"both"}', b'{"scope":null}',
                        b'{"action":"repair"}', b'{"scope":"NISI"}', b'{"scope":"nisi+jev"}',
                        b'{"scope":"nisi","confirm":true}', b'{"scope":["nisi"]}'):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.post_json('/api/inference/fix', invalid)[0], 400)
        self.assertEqual(control.scopes, [])
        for scope in ('all', 'local', 'route', 'both', 'nisi'):
            code, raw = self.post_json('/api/inference/fix',
                                       json.dumps({'scope': scope}).encode())
            self.assertEqual(code, 202)
            self.assertEqual(json.loads(raw)['action'], f'fix-{scope}')
        self.assertEqual(control.scopes, ['all', 'local', 'route', 'both', 'nisi'])

    def test_fix_nisi_scope_keeps_the_same_transport_checks(self):
        class StubFix:
            scopes = []
            def read(self):
                return {'status': 'idle', 'message': 'No fix requested.', 'steps': [],
                        'operationId': 0, 'action': None}
            def request_fix(self, scope):
                self.scopes.append(scope)
                return {'status': 'running', 'message': 'Checking.', 'steps': [],
                        'operationId': 1, 'action': f'fix-{scope}'}
        control = StubFix()
        self.server.online_code_repair = control
        body = b'{"scope":"nisi"}'
        self.assertEqual(self.post_json('/api/inference/fix', body, {'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.post_json('/api/inference/fix', body, {'Origin': 'null'})[0], 403)
        self.assertEqual(self.post_json('/api/inference/fix', body, {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.post_json('/api/inference/fix', b'{"scope":"nisi"' + b' ' * 1024 + b'}')[0], 413)
        port = self.server.server_address[1]
        c = HTTPConnection('127.0.0.1', port, timeout=2)
        c.putrequest('POST', '/api/inference/fix')
        c.putheader('Origin', f'http://127.0.0.1:{port}')
        c.putheader('Content-Type', 'application/json')
        c.endheaders()
        self.assertEqual(c.getresponse().status, 411)
        c.close()
        # A POST without any Origin header is refused too (no cross-site form or script fallback).
        c = HTTPConnection('127.0.0.1', port, timeout=2)
        c.request('POST', '/api/inference/fix', body=body, headers={'Content-Type': 'application/json'})
        self.assertEqual(c.getresponse().status, 403)
        c.close()
        self.assertEqual(control.scopes, [])
        with patch('server.mac_online_code_controls_enabled', return_value=False):
            self.assertEqual(self.post_json('/api/inference/fix', body)[0], 501)
        self.assertEqual(control.scopes, [])
        self.assertEqual(self.post_json('/api/inference/fix', body)[0], 202)
        self.assertEqual(control.scopes, ['nisi'])

    def test_fix_nisi_over_http_runs_one_operation_and_one_recover(self):
        import tempfile
        from pathlib import Path
        from online_code_repair import OnlineCodeRepair, NISI_RECOVER
        from test_online_code_repair import (NisiLauncher, GEMMA, QWEN, MARKER_SHA, NOW, lms_row,
                                             lsof_listing, private_file)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'codemode-nisi'
            state.mkdir(mode=0o700)
            private_file(state / 'owner.lock')
            private_file(root / 'router.lock')
            private_file(state / 'pending.json', json.dumps({
                'kind': 'codemode.nisi.pending.v1', 'started_unix': NOW - 3600,
                'input_sha256': MARKER_SHA}).encode())
            entered, release = threading.Event(), threading.Event()
            def lms():
                entered.set()
                release.wait(2)
                return [lms_row(GEMMA), lms_row(QWEN)]
            launcher = NisiLauncher(state)
            control = OnlineCodeRepair(launcher, nisi_pending_path=state / 'pending.json',
                                       readiness_path=root / 'readiness.json', sleep=lambda _: None,
                                       clock=lambda: NOW, lms_ps=lms, loopback_sockets=lsof_listing,
                                       router_lock_path=root / 'router.lock',
                                       fix_journal_path=root / 'fix-journal.jsonl')
            self.server.online_code_repair = control
            try:
                code, raw = self.post_json('/api/inference/fix', b'{"scope":"nisi"}')
                self.assertEqual(code, 202)
                first = json.loads(raw)
                self.assertEqual((first['status'], first['action']), ('running', 'fix-nisi'))
                self.assertTrue(entered.wait(2))
                code, raw = self.post_json('/api/inference/fix', b'{"scope":"nisi"}')
                self.assertEqual(json.loads(raw)['operationId'], first['operationId'])
                release.set()
                self.assertTrue(control.join(3))
                code, raw, _ = self.get('/api/inference/fix')
                final = json.loads(raw)
                self.assertEqual((code, final['status'], final['action']), (200, 'ready', 'fix-nisi'))
                self.assertIn('recover', [step['name'] for step in final['steps']])
                self.assertEqual(launcher.count(NISI_RECOVER), 1)
                self.assertFalse((state / 'pending.json').exists())
            finally:
                release.set()
                control.cancel()
                control.join(1)

    def test_pc_headless_requires_exact_same_origin_on_or_off_action(self):
        class StubHeadless:
            actions = []
            def read(self):
                return {'status': 'idle', 'message': 'No check requested yet.', 'steps': [],
                        'operationId': 0, 'action': None}
            def request_headless(self, action):
                self.actions.append(action)
                return {'status': 'running', 'message': 'Turning.', 'steps': [],
                        'operationId': len(self.actions), 'action': f'headless-{action}'}
        control = StubHeadless()
        self.server.online_code_repair = control
        code, raw, headers = self.get('/api/online-code-mode/headless')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['status'], 'idle')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.post_json('/api/online-code-mode/headless', b'{"action":"on"}',
                                        {'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.post_json('/api/online-code-mode/headless', b'{"action":"on"}',
                                        {'Content-Type': 'text/plain'})[0], 415)
        for invalid in (b'{"action":"toggle"}', b'{"action":"on","hours":12}', b'{"action":"on","action":"off"}',
                        b'{"action":true}', b'["on"]', b'{"action":"ON"}', b'{"state":"on"}'):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.post_json('/api/online-code-mode/headless', invalid)[0], 400)
        self.assertEqual(control.actions, [])
        for action in ('on', 'off'):
            code, raw = self.post_json('/api/online-code-mode/headless', json.dumps({'action': action}).encode())
            self.assertEqual(code, 202)
            self.assertEqual(json.loads(raw)['action'], f'headless-{action}')
        self.assertEqual(control.actions, ['on', 'off'])

    def test_online_code_actions_fail_closed_outside_mac_owner(self):
        class StubController:
            calls = 0
            def request(self):
                self.calls += 1
            def request_entry(self):
                self.calls += 1
            def request_fix(self, scope):
                self.calls += 1
            def request_headless(self, action):
                self.calls += 1
        controller = StubController()
        self.server.online_code_repair = controller
        with patch('server.mac_online_code_controls_enabled', return_value=False):
            for route, body in (('/api/online-code-mode/entry', b'{"action":"readiness"}'),
                                ('/api/online-code-mode/repair', b'{"action":"check-and-repair"}'),
                                ('/api/inference/fix', b'{"scope":"both"}'),
                                ('/api/online-code-mode/headless', b'{"action":"on"}')):
                with self.subTest(route=route):
                    self.assertEqual(self.get(route)[0], 501)
                    self.assertEqual(self.post_json(route, body)[0], 501)
            self.assertEqual(controller.calls, 0)
            self.assertEqual(self.get('/api/snapshot')[0], 200)

    def test_non_mac_server_does_not_construct_online_code_controller(self):
        with patch('server.mac_online_code_controls_enabled', return_value=False), \
             patch('server.OnlineCodeRepair', side_effect=AssertionError('must not construct')):
            other = MonitorServer(('127.0.0.1', 0), self.store)
        try:
            self.assertIsNone(other.online_code_repair)
        finally:
            other.server_close()
    def test_map_layout_module_is_served_as_script(self):
        status, raw, headers = self.get('/map-layout.mjs')
        self.assertEqual(status, 200)
        self.assertIn(b'focusedLaneLayout', raw)
        self.assertEqual(headers['Content-Type'], 'text/javascript; charset=utf-8')
    def test_rebinding_origin_and_traversal_rejected(self):
        self.assertEqual(self.get('/api/snapshot',{'Host':'evil.example'})[0],403)
        self.assertEqual(self.get('/api/snapshot',{'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.get('/../telemetry.py')[0],404)
        self.assertEqual(self.get('/telemetry.py')[0],404)

    def test_incomplete_headers_release_slot_after_read_timeout(self):
        self.server.socket_timeout_seconds = .12
        self.server.connection_deadline_seconds = .6
        held = socket.create_connection(self.server.server_address, timeout=1)
        held.settimeout(1)
        try:
            held.sendall(b'GET /api/snapshot HTTP/1.1\r\nHost: 127.0.0.1:')
            self.wait_active(1)
            self.wait_active(0)
            self.assertEqual(held.recv(32), b'')
            self.assertEqual(self.get('/api/snapshot')[0], 200)
        finally:
            held.close()

    def test_connection_cap_and_absolute_deadline_for_dripping_headers(self):
        self.assertEqual(self.server.max_connections, 8)
        self.server.socket_timeout_seconds = .2
        self.server.connection_deadline_seconds = .5
        held = []
        try:
            for _ in range(self.server.max_connections):
                sock = socket.create_connection(self.server.server_address, timeout=1)
                sock.settimeout(1)
                held.append(sock)
                sock.sendall(b'GET /api/snapshot HTTP/1.1\r\nHost: 127.0.0.1:')
            self.wait_active(8)
            extra = socket.create_connection(self.server.server_address, timeout=1)
            extra.settimeout(1)
            try:
                extra.sendall(b'GET /api/snapshot HTTP/1.0\r\n\r\n')
                self.assertEqual(extra.recv(32), b'')
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                extra.close()
            self.assertEqual(self.server._active_connections, 8)
            # Keep sending within the per-read timeout; the absolute deadline
            # still has to close every socket and release all eight slots.
            until = time.monotonic() + .7
            while time.monotonic() < until:
                for sock in held:
                    try:
                        sock.sendall(b'1')
                    except OSError:
                        pass
                time.sleep(.05)
            self.wait_active(0)
            self.assertEqual(self.get('/api/snapshot')[0], 200)
        finally:
            for sock in held:
                sock.close()

if __name__=='__main__':unittest.main()
