import json
import hashlib
import hmac
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import pl

SECRET = "/Users/fakeuser/secret"
ACCOUNT = "Users/fakeuser"


def status_file(components):
    """What file_status returns: the monitor JSON, unsanitized (peer_link.py:1094)."""
    return {
        "schemaVersion": 1,
        "kind": "agiw-peer",
        "host": "windows",
        "platform": "windows",
        "observedAt": "2026-01-01T00:00:00.000Z",
        "models": [{"id": "m", "host": "windows", "loadedState": "inactive"}],
        "components": components,
    }


class ComponentDetailStaysLocal(unittest.TestCase):
    def test_status_file_and_windows_detail_do_not_cross_the_link(self):
        # Windows pc_status keeps this detail (lanes.py:492-495); file_status serves it.
        doc = status_file([
            {"id": "nisi", "label": "Nisi route adapter", "state": "ready", "detail": SECRET},
            {"id": "jev", "label": "Jev", "state": "configured", "detail": ACCOUNT,
             "lastJudgedAgeSeconds": 12},
            {"id": "pair", "detail": SECRET},
        ])
        out = pl.project_status(pl.validate_status(doc, "windows"))
        blob = json.dumps(out)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(ACCOUNT, blob)
        self.assertEqual([row["id"] for row in out["components"]], ["nisi", "jev"])
        self.assertTrue(all("detail" not in row for row in out["components"]))
        self.assertEqual(out["components"][1]["lastJudgedAgeSeconds"], 12)

    def test_signed_lan_answer_omits_detail_and_keeps_a_valid_signature(self):
        doc = status_file([
            {"id": "nisi", "label": "Nisi", "state": "ready", "detail": SECRET},
            {"id": "jev", "label": "Jev", "state": "configured", "detail": ACCOUNT},
        ])
        now = time.time()
        doc["observedAt"] = pl._iso(now)
        node, requester, key, nonce, ts = "server-node", "peer-node", b"k" * 32, "d" * 32, str(now)
        keys = pl.Subkeys(key)
        path = pl.PREFIX + "/status"
        request = "\n".join(("GET", path, requester, node, ts, nonce)).encode()
        link = SimpleNamespace(node=node, host="windows", status_provider=lambda: doc,
                               trusted=lambda: {requester: {"key": key}},
                               nonces_for=lambda peer: pl.NonceCache())
        handler = pl.LinkHandler.__new__(pl.LinkHandler)
        handler.server = SimpleNamespace(link=link)
        handler.headers = {"X-Agiw-Node": requester, "X-Agiw-Ts": ts, "X-Agiw-Nonce": nonce,
                           "X-Agiw-Sig": hmac.new(keys.request, request, hashlib.sha256).hexdigest()}
        handler._send = mock.Mock()
        handler._status()  # real request authentication, projection and reply signing; no socket
        code, body, headers = handler._send.call_args[0]
        self.assertEqual(code, 200)
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn(ACCOUNT.encode(), body)
        self.assertTrue(all("detail" not in row for row in json.loads(body)["components"]))
        expected = hmac.new(keys.reply, pl._reply_header(nonce, node, requester) + body, hashlib.sha256).hexdigest()
        self.assertEqual(headers, {"X-Agiw-Node": node, "X-Agiw-Sig": expected})

    def test_lmstudio_and_minimal_documents_have_no_component_detail(self):
        for doc in (
            {"schemaVersion": 1, "kind": "agiw-peer", "host": "mac", "platform": "mac",
             "observedAt": "2026-01-01T00:00:00.000Z", "health": "ok",
             "producer": "agiw-peer-link lmstudio", "models": []},
            {"schemaVersion": 1, "kind": "agiw-peer", "host": "linux", "platform": "linux",
             "observedAt": "2026-01-01T00:00:00.000Z", "health": "unknown",
             "producer": "agiw-peer-link minimal", "models": []},
        ):
            out = pl.project_status(pl.validate_status(doc, doc["platform"]))
            self.assertNotIn("components", out)
