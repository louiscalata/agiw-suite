"""The real Components page and Tool Connections assets through the HTTP handler."""
from pathlib import Path
import socket
import threading
import unittest

import server


ROOT = Path(__file__).resolve().parent
ASSETS = {
    '/components': ('components.html', 'text/html; charset=utf-8'),
    '/tool-connections.mjs': ('tool-connections.mjs', 'text/javascript; charset=utf-8'),
    '/tool-connections.css': ('tool-connections.css', 'text/css; charset=utf-8'),
}


class NoListener:
    server_address = ('127.0.0.1', 18446)


def request(path):
    """Exercise Handler over a socket pair without starting the observer or a listener."""
    client, handler = socket.socketpair()
    client.settimeout(2)
    def serve():
        try:
            server.Handler(handler, ('127.0.0.1', 0), NoListener())
        finally:
            handler.close()
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        client.sendall(f'GET {path} HTTP/1.0\r\nHost: 127.0.0.1:18446\r\n\r\n'.encode())
        chunks = []
        while chunk := client.recv(65536):
            chunks.append(chunk)
        raw = b''.join(chunks)
    finally:
        client.close()
        worker.join(2)
    head, separator, body = raw.partition(b'\r\n\r\n')
    assert separator, 'HTTP response headers missing'
    lines = head.decode('latin-1').split('\r\n')
    status = int(lines[0].split()[1])
    headers = dict(line.split(': ', 1) for line in lines[1:])
    return status, headers, body


class ToolConnectionsAssetsTest(unittest.TestCase):
    def test_components_and_new_assets_are_exact_bytes_with_safe_mime(self):
        self.assertEqual(server.ROOT, ROOT / 'web')
        for path, (name, mime) in ASSETS.items():
            with self.subTest(path=path):
                self.assertEqual(server.ASSETS.get(path), (name, mime))
                status, headers, body = request(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Content-Type'], mime)
                self.assertEqual(headers['Content-Length'], str(len(body)))
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
                self.assertEqual(body, (ROOT / 'web' / name).read_bytes())

    def test_nearby_assets_are_not_served(self):
        for path in ('/web/tool-connections.mjs', '/tool-connections.js',
                     '/tool-connections.css.map', '/tool-connections.mjs/extra'):
            with self.subTest(path=path):
                self.assertEqual(request(path)[0], 404)

    def test_build_and_release_package_backend_and_new_web_assets(self):
        build = (ROOT / 'build.sh').read_text()
        release = (ROOT / 'package-release.sh').read_text()
        guarded = next(line for line in build.splitlines()
                       if line.startswith('for resource in bundle_nisi.py '))
        self.assertIn(' tool_connectors.py ', guarded)
        for resource in ('tool_connectors.py', 'web/tool-connections.mjs',
                         'web/tool-connections.css'):
            with self.subTest(resource=resource):
                self.assertIn(resource, release)
                self.assertIn(f'"$project_dir/{resource}"', build)
        for asset in ('tool-connections.mjs', 'tool-connections.css'):
            with self.subTest(asset=asset):
                self.assertIn(f'"$project_dir/web/{asset}"', build)


if __name__ == '__main__':
    unittest.main()
