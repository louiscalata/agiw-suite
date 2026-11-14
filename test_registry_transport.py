"""Registry observations through real HTTP transport on ephemeral loopback ports."""
import contextlib
import socketserver
import threading
import time
import unittest

from inference_collect import loopback_get
import remote_machines

BODY = b'{"data":[{"id":"server/model"}]}'

@contextlib.contextmanager
def http_server(response, *, delay=0, drip=False, body_drip=False):
    requests = []
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(1)
            incoming = b""
            while b"\r\n\r\n" not in incoming:
                block = self.request.recv(4096)
                if not block:
                    return
                incoming += block
            requests.append(incoming.split(b"\r\n", 1)[0])
            try:
                outgoing = response() if callable(response) else response
                if body_drip:
                    head, outgoing = outgoing.split(b"\r\n\r\n", 1)
                    self.request.sendall(head + b"\r\n\r\n")
                if drip:
                    for byte in outgoing:
                        self.request.sendall(bytes([byte]))
                        time.sleep(delay)
                else:
                    time.sleep(delay)
                    self.request.sendall(outgoing)
            except OSError:
                pass
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True
    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1/models", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


def response(body=BODY, *, headers=None, status="200 OK"):
    headers = [("Content-Length", str(len(body)))] if headers is None else headers
    return (f"HTTP/1.1 {status}\r\n".encode("ascii") +
            b"".join(f"{key}: {value}\r\n".encode("ascii") for key, value in headers) +
            b"Connection: close\r\n\r\n" + body)


def registry_for(url):
    return {"schemaVersion": 1, "machines": [{"id": "worker", "label": "Worker", "lanes": [
        {"id": "lane", "label": "Lane", "localPort": int(url.split(":")[2].split("/")[0])}]}]}


class RegistryTransportTests(unittest.TestCase):
    def observation(self, url, *, timeout=.5, limit=1024 * 1024):
        nodes = remote_machines.collect(registry_for(url), now=1000,
                    fetch=lambda target: loopback_get(target, timeout=timeout, limit=limit, strict=True))
        return next(node for node in nodes if node["role"] == "llama")

    def assert_unknown(self, wire, reason, **kwargs):
        with http_server(wire) as (url, requests):
            node = self.observation(url, **kwargs)
        self.assertEqual(node["health"], "not_measured")
        self.assertIsNone(node["modelId"])
        self.assertEqual(node["reason"], reason)
        self.assertEqual(requests, [b"GET /v1/models HTTP/1.1"])

    def test_complete_content_length_reports_exact_identity(self):
        with http_server(response()) as (url, requests):
            node = self.observation(url)
        self.assertEqual(node["health"], "ok")
        self.assertEqual(node["modelId"], "server/model")
        self.assertEqual(requests, [b"GET /v1/models HTTP/1.1"])

    def test_strict_identity_answer_projects_reachability_not_machine_state(self):
        for wire, identity in ((response(), "answering"),
                               (response(headers=[("Content-Length", str(len(BODY) + 19))]), "unknown")):
            with self.subTest(identity=identity), http_server(wire) as (url, requests):
                nodes = remote_machines.collect(registry_for(url), now=1000,
                            fetch=lambda target: loopback_get(target, timeout=.5, strict=True))
                snapshot = {"machines": [], "inference": {"nodes": nodes}}
                remote_machines.project_snapshot(snapshot)
                [row] = snapshot["machines"]
                self.assertEqual((row["state"], row["health"], row["reasonCode"]),
                                 ("unknown", "not_measured", "host_not_measured"))
                self.assertEqual(row["laneIdentity"], identity)
                self.assertEqual(row["lanesAnswering"], 1 if identity == "answering" else 0)
                self.assertEqual(requests, [b"GET /v1/models HTTP/1.1"])

    def test_valid_json_prefix_with_incomplete_content_length_is_unknown(self):
        self.assert_unknown(response(headers=[("Content-Length", str(len(BODY) + 19))]), "incomplete_body")

    def test_success_followed_by_failed_real_probe_clears_identity(self):
        replies = iter([response(), response(headers=[("Content-Length", str(len(BODY) + 19))])])
        with http_server(lambda: next(replies)) as (url, requests):
            first = self.observation(url)
            second = self.observation(url)
        self.assertEqual(len(requests), 2)
        self.assertEqual(first["modelId"], "server/model")
        self.assertEqual(second["health"], "not_measured")
        self.assertEqual(second["reason"], "incomplete_body")
        self.assertIsNone(second["modelId"])

    def test_invalid_utf8_never_becomes_replacement_identity(self):
        self.assert_unknown(response(BODY.replace(b"model", b"m\xffdel")), "invalid_utf8")

    def test_missing_length_and_ambiguous_framing_are_unknown(self):
        for headers, reason in (([], "missing_length"),
                ([("Content-Length", "-1")], "invalid_length"),
                ([("Content-Length", "bad")], "invalid_length"),
                ([("Content-Length", str(len(BODY))), ("Content-Length", str(len(BODY)))], "ambiguous_framing"),
                ([("Content-Length", str(len(BODY))), ("Transfer-Encoding", "chunked")], "ambiguous_framing"),
                ([("Transfer-Encoding", "gzip")], "ambiguous_framing")):
            with self.subTest(headers=headers):
                self.assert_unknown(response(headers=headers), reason)

    def test_complete_chunked_body_reports_identity(self):
        chunked = f"{len(BODY):x}\r\n".encode("ascii") + BODY + b"\r\n0\r\n\r\n"
        with http_server(response(chunked, headers=[("Transfer-Encoding", "chunked")])) as (url, _):
            node = self.observation(url)
        self.assertEqual(node["health"], "ok")
        self.assertEqual(node["modelId"], "server/model")

    def test_incomplete_or_invalid_chunked_body_is_unknown(self):
        prefix = f"{len(BODY):x}\r\n".encode("ascii") + BODY + b"\r\n"
        for chunked in (prefix, prefix + b"0\r\n", prefix + b"0\r\nTrailer: partial", b"nothex\r\n" + BODY):
            with self.subTest(chunked=chunked):
                self.assert_unknown(response(chunked, headers=[("Transfer-Encoding", "chunked")]), "incomplete_body")

    def test_declared_or_chunked_oversize_is_unknown(self):
        self.assert_unknown(response(), "oversize", limit=8)
        chunked = f"{len(BODY):x}\r\n".encode("ascii") + BODY + b"\r\n0\r\n\r\n"
        self.assert_unknown(response(chunked, headers=[("Transfer-Encoding", "chunked")]), "oversize", limit=8)

    def test_redirect_is_not_followed_and_non200_never_reports_identity(self):
        with http_server(response()) as (destination, destination_requests):
            wire = response(headers=[("Content-Length", str(len(BODY))), ("Location", destination)], status="302 Found")
            self.assert_unknown(wire, "upstream_error")
            self.assertEqual(destination_requests, [])
        self.assert_unknown(response(status="503 Unavailable"), "upstream_error")

    def test_hanging_headers_and_dribbled_body_obey_total_deadline(self):
        for wire, delay, drip, body_drip in ((response(), .3, False, False), (response(), .005, True, False),
                                            (response(), .01, True, True)):
            with self.subTest(body_drip=body_drip), http_server(wire, delay=delay, drip=drip, body_drip=body_drip) as (url, _):
                started = time.monotonic()
                node = self.observation(url, timeout=.08)
                elapsed = time.monotonic() - started
            self.assertEqual(node["health"], "not_measured")
            self.assertIsNone(node["modelId"])
            self.assertEqual(node["reason"], "timeout")
            self.assertLess(elapsed, .25)

    def test_invalid_ambiguous_or_nonprintable_model_identity_is_unknown(self):
        for body in (b'{"data":[{"id":"first","id":"last"}]}', b'{"data":[],"data":[{"id":"last"}]}',
                     b'{"data":[{"id":"a\\u0085b"}]}', b'{"data":[{"id":"a\\u202eb"}]}',
                     b'{"data":[{"id":"valid"}],"metadata":NaN}',
                     b'{"data":[{"id":"valid"}],"metadata":Infinity}',
                     b'{"data":[{"id":"' + b'x' * 1025 + b'"}]}', b'{"data":[]}',
                     b'{"data":[{"id":"a"},{"id":"b"}]}', b'invalid'):
            with self.subTest(body=body):
                self.assert_unknown(response(body), "identity_unknown")


if __name__ == "__main__":
    unittest.main()
