"""The native shell's page and snapshot URLs are routes the observer serves.

The shell loads `/monitor` in WebKit and polls the snapshot for its badge. A poll URL that the
observer does not route returns 404 on every request, which once made the shell stop a healthy
observer. These tests compile the shell's own URL expression and request its result from a real
in-process observer.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from server import MonitorServer, SnapshotStore

SOURCE = Path(__file__).resolve().with_name('Monitor.swift')
PAGE_LITERAL = re.compile(r'URL\(string: "http://127\.0\.0\.1:\\\(port\)(/[^"]*)"\)')
SNAPSHOT_URL = re.compile(r'^        let url = (.+)$', re.MULTILINE)


def snapshot_expression():
    source = SOURCE.read_text()
    poll = source[source.index('    private func pollSnapshot()'):source.index('    private func updateBadge(')]
    expressions = SNAPSHOT_URL.findall(poll)
    if len(expressions) != 1:
        raise AssertionError(f'Expected one snapshot URL expression, found {expressions}')
    return expressions[0]


def page_path():
    paths = PAGE_LITERAL.findall(SOURCE.read_text())
    if len(paths) != 1:
        raise AssertionError(f'Expected one observer page URL in Monitor.swift, found {paths}')
    return paths[0]


class ObserverRoutes(unittest.TestCase):
    def setUp(self):
        self.server = MonitorServer(('127.0.0.1', 0), SnapshotStore())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        connection = HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=2)
        try:
            connection.request('GET', path, headers={'Accept': 'application/json'})
            response = connection.getresponse()
            return response.status, response.getheader('Content-Type'), response.read()
        finally:
            connection.close()

    def assert_snapshot_route(self, path):
        status, content_type, body = self.get(path)
        # An empty store answers 503 "starting"; both answers come from the snapshot route.
        self.assertIn(status, (200, 503), path)
        self.assertEqual(content_type, 'application/json', path)
        self.assertIsInstance(json.loads(body), dict)

    def test_the_page_the_shell_opens_is_served(self):
        status, content_type, _ = self.get(page_path())
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith('text/html'), content_type)

    def test_a_path_under_the_page_is_not_the_snapshot_route(self):
        # The URL the shell used to poll; it must stay unrouted so the test below is meaningful.
        status, _, _ = self.get(page_path().rstrip('/') + '/api/snapshot')
        self.assertEqual(status, 404)

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('xcrun'), 'Requires macOS Swift toolchain')
    def test_the_shell_polls_a_snapshot_route_the_observer_serves(self):
        port = self.server.server_address[1]
        with tempfile.TemporaryDirectory(prefix='monitor-routes-', dir=Path(tempfile.gettempdir()).resolve()) as temp:
            stage = Path(temp)
            (stage / 'main.swift').write_text(
                'import Foundation\n\nlet base = URL(string: "http://127.0.0.1:' + str(port) + page_path() + '")!\n'
                'let url = ' + snapshot_expression() + '\nprint(url.absoluteString)\n')
            environment = dict(os.environ, TMPDIR=str(stage), CFFIXED_USER_HOME=str(stage),
                               CLANG_MODULE_CACHE_PATH=str(stage / 'clang-cache'),
                               SWIFT_MODULECACHE_PATH=str(stage / 'swift-cache'))
            subprocess.run(['xcrun', 'swiftc', '-O', str(stage / 'main.swift'), '-o', str(stage / 'routes')],
                           check=True, capture_output=True, env=environment, timeout=180)
            url = subprocess.run([str(stage / 'routes')], check=True, capture_output=True, text=True,
                                 env=environment, timeout=30).stdout.strip()
        prefix = f'http://127.0.0.1:{port}'
        self.assertTrue(url.startswith(prefix + '/'), url)
        self.assert_snapshot_route(url[len(prefix):])


if __name__ == '__main__':
    unittest.main()
