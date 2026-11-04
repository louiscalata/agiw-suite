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



if __name__ == '__main__':
    unittest.main()
