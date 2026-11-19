"""Loopback-only worker tests. All 'execution' is an injected fake; no inference runs."""

import json
import os
import stat
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from agiw import windows_worker as ww
from agiw.fleet_local_pilot import NoRedirect
from agiw.windows_worker_receipt_check import validate_job_receipt

ENDPOINT = "http://127.0.0.1:9/v1/chat/completions"
TOKEN = b"k" * 40


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.token_file = self.root / "token"
        self.token_file.write_bytes(TOKEN)
        os.chmod(str(self.token_file), 0o600)
        self.receipt_dir = self.root / "receipts"
        self.token = ww.load_token(str(self.token_file))

    def start_server(self, worker):
        server = ww.create_server(worker, self.token, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        return f"http://127.0.0.1:{server.server_address[1]}"

    def call(self, base, path, data=None, token=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        body = json.dumps(data).encode() if data is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(base + path, data=body, headers=headers, method="POST" if body is not None else "GET")
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def worker(self, execute=None, probe=None):
        return ww.Worker(
            "m5-macbook-pro", "fixture-model", ENDPOINT, str(self.receipt_dir), execute=execute, probe=probe
        )

    def job(self, worker="m5-macbook-pro", program="nisi-smoke.mjs"):
        return {
            "job_id": "job-1",
            "route": {"worker": worker, "preanalysis": "local", "reason": "strict-local"},
            "program": program,
        }

    def fake_success(self, **overrides):
        def execute(mode, url, model, source, program="nisi-smoke.mjs", report_store=None, worker_id=None, **_):
            result = {
                "reportStored": True,
                "reportPath": str(self.root / "report.json"),
                "report": {
                    "outcome": "COMPLETED",
                    "reportStored": True,
                    "runId": str(uuid.uuid4()),
                    "storedReportSha256": "a" * 64,
                },
            }
            result.update(overrides)
            return result

        return execute

    def test_401_without_and_wrong_token(self):
        base = self.start_server(self.worker())
        self.assertEqual(self.call(base, "/v1/ready")[0], 401)
        self.assertEqual(self.call(base, "/v1/ready", token="w" * 40)[0], 401)
        self.assertEqual(self.call(base, "/v1/jobs", self.job(), token="w" * 40)[0], 401)
        self.assertEqual(self.call(base, "/v1/jobs", self.job())[0], 401)

    def test_rejected_post_drains_body_and_closes(self):
        # Early refusals must still deliver their response; on Windows an unread body resets the socket.
        import socket
        from urllib.parse import urlsplit

        base = self.start_server(self.worker())
        port = urlsplit(base).port
        body = json.dumps(self.job()).encode() + b" " * 12000
        for _ in range(5):
            with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
                s.sendall(
                    b"POST /v1/jobs HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                    + b"w" * 40
                    + b"\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            self.assertTrue(data.startswith(b"HTTP/1.1 401"), data[:40])
            self.assertIn(b"Connection: close", data)

    def test_ready_endpoint(self):
        worker = self.worker(probe=lambda: (["fixture-model", "other"], "llama-server"))
        base = self.start_server(worker)
        status, data = self.call(base, "/v1/ready", token=TOKEN.decode())
        self.assertEqual(status, 200)
        self.assertEqual(data["worker_id"], "m5-macbook-pro")
        self.assertTrue(data["model_ready"])
        self.assertEqual(data["models"], ["fixture-model", "other"])
        self.assertFalse(data["busy"])
        self.assertLessEqual(abs(data["observed_at"] - time.time()), 5)

    def test_wrong_worker_and_unknown_program_refused(self):
        base = self.start_server(self.worker(execute=self.fake_success()))
        status, data = self.call(base, "/v1/jobs", self.job(worker="dual-gpu-pc"), token=TOKEN.decode())
        self.assertEqual((status, data["error"]), (400, "ROUTE_REFUSED"))
        status, data = self.call(base, "/v1/jobs", self.job(program="evil.mjs"), token=TOKEN.decode())
        self.assertEqual((status, data["error"]), (400, "PROGRAM_REFUSED"))

    def test_busy_is_409(self):
        entered, release = threading.Event(), threading.Event()

        def blocking(mode, url, model, source, program="nisi-smoke.mjs", report_store=None, worker_id=None, **_):
            entered.set()
            release.wait(3)

        base = self.start_server(self.worker(execute=blocking))
        first = threading.Thread(target=lambda: self.call(base, "/v1/jobs", self.job(), token=TOKEN.decode()))
        first.start()
        self.assertTrue(entered.wait(2))
        status, data = self.call(base, "/v1/jobs", self.job(), token=TOKEN.decode())
        self.assertEqual((status, data["error"]), (409, "BUSY"))
        release.set()
        first.join(3)
        self.assertFalse(first.is_alive())

    def test_success_writes_valid_receipt(self):
        base = self.start_server(self.worker(execute=self.fake_success()))
        status, receipt = self.call(base, "/v1/jobs", self.job(), token=TOKEN.decode())
        self.assertEqual(status, 200)
        self.assertEqual(receipt["outcome"], "COMPLETED")
        verdict = validate_job_receipt(
            receipt, expected_worker="m5-macbook-pro", expected_model="fixture-model", job_id="job-1", now=time.time()
        )
        self.assertTrue(verdict["valid"], verdict)
        files = list(self.receipt_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        if os.name == "posix":  # NTFS has no POSIX mode bits; the worker only chmods on POSIX
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        saved = json.loads(files[0].read_text())
        self.assertEqual(saved, receipt)
        self.assertTrue(validate_job_receipt(saved, expected_worker="m5-macbook-pro")["valid"])

    def test_failed_job_receipt_is_not_completed(self):
        def failing(mode, url, model, source, program="nisi-smoke.mjs", report_store=None, worker_id=None, **_):
            raise RuntimeError("fixture failure")

        base = self.start_server(self.worker(execute=failing))
        status, receipt = self.call(base, "/v1/jobs", self.job(), token=TOKEN.decode())
        self.assertEqual(status, 200)
        self.assertEqual(receipt["outcome"], "BLOCKED")
        self.assertFalse(receipt["reportStored"])

    def test_body_cap(self):
        base = self.start_server(self.worker(execute=self.fake_success()))
        oversized = self.job()
        oversized["payload"] = {"data": "x" * (ww.BODY_LIMIT + 1)}
        self.assertEqual(self.call(base, "/v1/jobs", oversized, token=TOKEN.decode())[0], 413)

    def test_bind_validation(self):
        self.assertEqual(ww.validate_bind("127.0.0.1"), "127.0.0.1")
        with self.assertRaises(ww.Refused):
            ww.validate_bind("0.0.0.0")
        self.assertEqual(ww.validate_bind("0.0.0.0", True), "0.0.0.0")

    def test_weak_and_symlink_token_refused(self):
        if os.name == "posix":  # mode bits are only checked where they exist
            weak = self.root / "weak"
            weak.write_bytes(TOKEN)
            os.chmod(str(weak), 0o644)
            with self.assertRaises(ww.Refused):
                ww.load_token(str(weak))
        link = self.root / "link"
        try:
            link.symlink_to(self.token_file)
        except OSError:  # Windows without symlink privilege
            link = None
        if link is not None:
            with self.assertRaises(ww.Refused):
                ww.load_token(str(link))
        short = self.root / "short"
        short.write_bytes(b"short")
        os.chmod(str(short), 0o600)
        with self.assertRaises(ww.Refused):
            ww.load_token(str(short))
        self.assertEqual(ww.load_token(str(self.token_file)), TOKEN)

    def test_authorized_uses_constant_time_compare(self):
        self.assertTrue(ww.authorized("Bearer " + TOKEN.decode(), TOKEN))
        self.assertFalse(ww.authorized("bearer " + TOKEN.decode(), TOKEN))
        self.assertFalse(ww.authorized("Bearer " + "k" * 39, TOKEN))


if __name__ == "__main__":
    unittest.main()
