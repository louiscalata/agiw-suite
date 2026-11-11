"""Browser-free static transport guards for both observer implementations.

The production handlers speak HTTP/1.0 and close each response. The stress case
reuses six HTTPConnection clients with keep-alive requests; those clients must
reconnect as directed, rather than silently retry an empty or truncated response.
"""
import concurrent.futures
from html.parser import HTMLParser
import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
import re
import socket
import sys
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urljoin, urlsplit

import server as mac_server

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / 'windows'))
try:
    from agiw_win import server as windows_server
finally:
    sys.path.pop(0)


class ModuleScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type') == 'module' and attrs.get('src'):
            self.paths.append(attrs['src'])


def module_payloads():
    """Walk this page's static import graph, including the shared Jaryo module."""
    parser = ModuleScripts()
    parser.feed((REPO / 'web/index.html').read_text())
    pending = list(parser.paths)
    payloads = {}
    imports = re.compile(
        r'''(?:^\s*(?:import|export)\s+[^;\n]*?\sfrom\s*|^\s*import\s*)['"]([^'"]+)['"]''',
        re.MULTILINE)
    while pending:
        route = pending.pop()
        if route in payloads:
            continue
        parsed = urlsplit(route)
        if parsed.scheme or parsed.netloc or not route.startswith('/'):
            raise AssertionError('Expected a local module route: ' + route)
        if route.startswith('/jaryo/') or route == '/usage-format.mjs':
            path = REPO / route.lstrip('/')
        else:
            path = REPO / 'web' / route.lstrip('/')
        payloads[route] = path.read_bytes()
        pending.extend(urljoin(route, dependency)
                       for dependency in imports.findall(payloads[route].decode()))
    return payloads


class TransportCases:
    module = None

    def start_observer(self):
        """Use the real handlers with an empty fixture store, never main's sampler."""
        module = self.module
        store = module.SnapshotStore()
        if module is mac_server:
            with mock.patch.object(module, 'mac_online_code_controls_enabled', return_value=False):
                observer = module.MonitorServer(('127.0.0.1', 0), store)
        else:
            observer = module.MonitorServer(('127.0.0.1', 0), store, REPO / 'web')
        handler_threads = []
        handler_lock = threading.Lock()
        original_thread = observer.process_request_thread

        def record_handler(request, address):
            with handler_lock:
                handler_threads.append(threading.current_thread())
            return original_thread(request, address)
        observer.process_request_thread = record_handler
        thread = threading.Thread(target=observer.serve_forever,
                                  kwargs={'poll_interval': .01}, daemon=True)
        thread.start()

        def stop():
            observer.stopping.set()
            observer.shutdown()
            observer.server_close()
            thread.join(2)
            self.assertFalse(thread.is_alive(), 'observer did not stop')
            with handler_lock:
                handlers = list(handler_threads)
            for handler in handlers:
                handler.join(2)
                self.assertFalse(handler.is_alive(), 'request handler did not stop')
        self.addCleanup(stop)
        return observer

    def open_streams(self, observer):
        for _ in range(observer.max_streams):
            connection = http.client.HTTPConnection(*observer.server_address, timeout=5)
            self.addCleanup(connection.close)
            connection.request('GET', '/api/stream')
            response = connection.getresponse()
            self.addCleanup(response.close)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Content-Type'), 'text/event-stream; charset=utf-8')

    def assert_response(self, response, payload, mime='text/javascript; charset=utf-8'):
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Type'), mime)
        self.assertEqual(response.getheader('Content-Length'), str(len(payload)))
        self.assertEqual(response.read(), payload)
        self.assertEqual(response.version, 10)
        self.assertTrue(response.will_close)

    def test_module_graph_over_reused_clients_with_live_streams(self):
        observer = self.start_observer()
        self.open_streams(observer)
        payloads = module_payloads()
        self.assertTrue({'/map-layout.mjs', '/monitor-board.mjs', '/jaryo/theme.mjs'} <= payloads.keys())
        payloads['/?demo=1'] = (REPO / 'web/index.html').read_bytes()
        clients = threading.local()
        opened = []
        opened_lock = threading.Lock()

        def fetch(route):
            if not hasattr(clients, 'connection'):
                clients.connection = http.client.HTTPConnection(*observer.server_address, timeout=5)
                with opened_lock:
                    opened.append(clients.connection)
            connection = clients.connection
            connection.request('GET', route, headers={'Connection': 'keep-alive'})
            response = connection.getresponse()
            try:
                mime = 'text/html; charset=utf-8' if route == '/?demo=1' else 'text/javascript; charset=utf-8'
                self.assert_response(response, payloads[route], mime)
            finally:
                response.close()

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                for _ in range(20):
                    # Waiting for every future propagates any empty/truncated response;
                    # the test has no transport retries.
                    futures = [pool.submit(fetch, route) for route in sorted(payloads)]
                    for future in futures:
                        future.result(timeout=10)
        finally:
            for connection in opened:
                connection.close()

    def test_module_burst_waits_for_short_lived_connection_contention(self):
        observer = self.start_observer()
        self.open_streams(observer)
        paths = ['/map-layout.mjs', '/monitor-board.mjs', '/app.js',
                 '/monitor-tabs.mjs', '/online-code-mode.mjs']
        payloads = module_payloads()
        held = []
        started = threading.Condition()
        started_count = [0]
        original_thread = observer.process_request_thread

        def record_thread(request, address):
            with started:
                started_count[0] += 1
                started.notify_all()
            return original_thread(request, address)
        observer.process_request_thread = record_thread
        for route in paths:
            sock = socket.create_connection(observer.server_address, timeout=5)
            self.addCleanup(sock.close)
            sock.sendall(('GET %s HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n'
                          % (route, observer.server_address[1])).encode())
            held.append((route, sock))
        with started:
            self.assertTrue(started.wait_for(lambda: started_count[0] == len(held), timeout=2))

        arrived = threading.Event()
        original_request = observer.process_request

        def record_request(request, address):
            arrived.set()
            return original_request(request, address)
        observer.process_request = record_request

        connection = http.client.HTTPConnection(*observer.server_address, timeout=5)
        self.addCleanup(connection.close)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            def final_module():
                connection.request('GET', '/simple-view.mjs', headers={'Connection': 'keep-alive'})
                response = connection.getresponse()
                try:
                    self.assert_response(response, payloads['/simple-view.mjs'])
                finally:
                    response.close()
            future = pool.submit(final_module)
            self.assertTrue(arrived.wait(2), 'sixth module request never reached admission')
            # Keep all eight slots occupied briefly after the sixth module arrives.
            # Main closes it immediately; bounded admission lets these requests finish.
            time.sleep(.05)
            for _, sock in held:
                sock.sendall(b'\r\n')
            for route, sock in held:
                response = http.client.HTTPResponse(sock)
                try:
                    response.begin()
                    self.assert_response(response, payloads[route])
                finally:
                    response.close()
            future.result(timeout=10)

    def test_admission_waits_for_a_slot_without_opening_a_socket(self):
        """Exercise the real admission method even where loopback bind is denied."""
        attempted = threading.Event()

        class ObservedSlots(threading.BoundedSemaphore):
            observing = False

            def acquire(self, *args, **kwargs):
                if self.observing:
                    attempted.set()
                return super().acquire(*args, **kwargs)

        observer = self.module.MonitorServer.__new__(self.module.MonitorServer)
        observer._slots = ObservedSlots(observer.max_connections)
        for _ in range(observer.max_connections):
            observer._slots.acquire()
        observer._slots.observing = True
        observer._active_lock = threading.Lock()
        observer._active_connections = observer.max_connections
        observer.shutdown_request = mock.Mock()
        request = object()
        address = ('127.0.0.1', 12345)

        def release():
            if attempted.wait(2):
                time.sleep(.05)
                with observer._active_lock:
                    observer._active_connections -= 1
                observer._slots.release()
        releaser = threading.Thread(target=release, daemon=True)
        releaser.start()
        try:
            with mock.patch.object(ThreadingHTTPServer, 'process_request') as dispatch:
                observer.process_request(request, address)
                dispatch.assert_called_once_with(request, address)
                observer.shutdown_request.assert_not_called()
                self.assertLessEqual(observer._active_connections, observer.max_connections)
                self.assertFalse(observer._slots.acquire(blocking=False), 'admission did not claim the released slot')
        finally:
            attempted.set()
            releaser.join(2)
            self.assertFalse(releaser.is_alive(), 'slot releaser did not stop')

    def test_sustained_saturation_still_refuses_within_a_bound(self):
        observer = self.module.MonitorServer.__new__(self.module.MonitorServer)
        observer._slots = threading.BoundedSemaphore(observer.max_connections)
        for _ in range(observer.max_connections):
            observer._slots.acquire()
        observer._active_lock = threading.Lock()
        observer._active_connections = observer.max_connections
        observer.shutdown_request = mock.Mock()
        request = object()
        started = time.monotonic()
        with mock.patch.object(ThreadingHTTPServer, 'process_request') as dispatch:
            observer.process_request(request, ('127.0.0.1', 12345))
            elapsed = time.monotonic() - started
            dispatch.assert_not_called()
            observer.shutdown_request.assert_called_once_with(request)
        self.assertLess(elapsed, 1.5, 'saturated admission blocked the accept loop too long')
        self.assertEqual(observer._active_connections, observer.max_connections)
        self.assertFalse(observer._slots.acquire(blocking=False))


class MacStaticModuleTransportTests(TransportCases, unittest.TestCase):
    module = mac_server


class WindowsStaticModuleTransportTests(TransportCases, unittest.TestCase):
    module = windows_server


if __name__ == '__main__':
    unittest.main()
