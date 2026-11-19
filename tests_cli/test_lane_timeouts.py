"""Per-lane model timeouts, client-side reasoning off, and sanitized adapter error evidence."""

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from agiw import fleet_local_pilot as pilot
from agiw import fleet_router_client as rc
from agiw import windows_worker as ww

ENDPOINT = "http://127.0.0.1:9/v1/chat/completions"


class TimeoutTests(unittest.TestCase):
    def test_lane_defaults(self):
        self.assertEqual(pilot.model_timeout_s("m5-macbook-pro"), 30)
        self.assertEqual(pilot.model_timeout_s("dual-gpu-pc"), 30)
        self.assertEqual(pilot.model_timeout_s("monterey-mac-pro"), 840)
        # 512 tokens at ~0.68 tok/s (~753 s) plus a ~60 s prompt must fit the Monterey timeout.
        self.assertGreaterEqual(pilot.model_timeout_s("monterey-mac-pro"), 512 / 0.68 + 60)

    def test_lane_max_tokens(self):
        self.assertEqual(pilot.max_tokens("monterey-mac-pro"), 512)
        self.assertIsNone(pilot.max_tokens("m5-macbook-pro"))
        self.assertEqual(pilot.max_tokens("m5-macbook-pro", 128), 128)
        for bad in (8, 5000, True, 64.5, "64"):
            with self.subTest(bad=bad), self.assertRaises(pilot.Refused):
                pilot.max_tokens("m5-macbook-pro", bad)

    def test_lane_response_format(self):
        self.assertEqual(pilot.response_format("monterey-mac-pro"), "json-schema")
        self.assertIsNone(pilot.response_format("m5-macbook-pro"))
        self.assertIsNone(pilot.response_format("dual-gpu-pc"))
        self.assertEqual(pilot.response_format("m5-macbook-pro", "json-schema"), "json-schema")
        for bad in ("json_schema", "grammar", True, 1, ""):
            with self.subTest(bad=bad), self.assertRaises(pilot.Refused):
                pilot.response_format("m5-macbook-pro", bad)
        rows = pilot.author_evidence(
            [
                {
                    "responseFormat": "json_object",
                    "attempts": 2,
                    "responseFormatFallback": "json_schema->json_object",
                    "fallbackHttpStatus": 400,
                    "fallbackServerMessage": "bad schema token=abcdefabcdefabcdefabcdefabcd",
                }
            ]
        )
        self.assertEqual(rows[0]["fallbackHttpStatus"], 400)
        self.assertNotIn("abcdefabcdefabcdef", rows[0]["fallbackServerMessage"])

    def test_output_sample_whitelisted_and_truncated(self):
        rows = pilot.author_evidence(
            [{"outputSample": "<|channel|>analysis " + "x " * 400, "preambleStripped": "harmony"}]
        )
        self.assertLessEqual(len(rows[0]["outputSample"]), 300)
        self.assertEqual(rows[0]["preambleStripped"], "harmony")
        self.assertEqual(pilot.model_timeout_s("monterey-mac-pro", 600), 600)

    def test_invalid_timeouts_refused(self):
        for bad in (0, -1, 901, True, "30", float("nan")):
            with self.subTest(bad=bad), self.assertRaises(pilot.Refused):
                pilot.model_timeout_s("m5-macbook-pro", bad)

    def test_execute_passes_timeout_and_reasoning_to_child(self):
        seen = {}

        def run(cmd, input=None, **kw):
            seen["cfg"] = json.loads(input)
            seen["timeout"] = kw["timeout"]
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "report": {"outcome": "BLOCKED", "code": "ADAPTER_EXCEPTION"},
                        "authorReceipts": [
                            {
                                "httpStatus": 500,
                                "errorCode": "HTTP_REFUSED",
                                "serverMessage": "bad format token=abcdef",
                                "payload": "x",
                            }
                        ],
                    }
                ),
                stderr="",
                returncode=1,
            )

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(pilot, "integrity", return_value=pilot.PIN),
            mock.patch.object(pilot, "node_binary", return_value="node"),
            mock.patch.object(pilot, "node_major", return_value=26),
            mock.patch.object(pilot.subprocess, "run", side_effect=run),
        ):
            data = pilot.execute(
                "fixture",
                ENDPOINT,
                "fixture-only",
                report_store=td,
                worker_id="monterey-mac-pro",
                disable_thinking=True,
            )
        self.assertEqual(seen["cfg"]["modelTimeoutMs"], 840000)
        self.assertEqual(seen["cfg"]["maxTokens"], 512)
        self.assertIs(seen["cfg"]["disableThinking"], True)
        self.assertEqual(seen["timeout"], 870)
        self.assertEqual(data["report"]["outcome"], "BLOCKED")
        self.assertEqual(
            data["authorEvidence"],
            [{"httpStatus": 500, "errorCode": "HTTP_REFUSED", "serverMessage": "bad format token [redacted]"}],
        )


class SanitizeTests(unittest.TestCase):
    def test_tokens_paths_and_long_strings_redacted(self):
        text = pilot.sanitize_text("Bearer abc.def failed at /Users/someone/x.gguf and C:\\AGIW\\t.txt key " + "A" * 40)
        self.assertNotIn("abc.def", text)
        self.assertNotIn("/Users", text)
        self.assertNotIn("C:\\AGIW", text)
        self.assertNotIn("A" * 40, text)
        self.assertLessEqual(len(pilot.sanitize_text("x " * 500)), 200)

    def test_author_evidence_whitelist(self):
        rows = pilot.author_evidence(
            [
                {
                    "httpStatus": 200,
                    "finishReason": "length",
                    "content": "secret answer",
                    "headers": {"Authorization": "Bearer x"},
                    "elapsedMs": 5,
                }
            ]
        )
        self.assertEqual(rows, [{"httpStatus": 200, "finishReason": "length", "elapsedMs": 5}])


class WorkerTimeoutTests(unittest.TestCase):
    def test_worker_uses_lane_default_job_override_and_records_evidence(self):
        seen = []
        formats = []

        def execute(
            mode,
            url,
            model,
            source,
            program="nisi-smoke.mjs",
            report_store=None,
            worker_id=None,
            timeout_s=None,
            disable_thinking=False,
            max_output_tokens=None,
            response_format_mode=None,
            request_mode_value=None,
            route=None,
        ):
            seen.append((timeout_s, disable_thinking, max_output_tokens))
            formats.append(response_format_mode)
            return {
                "reportStored": True,
                "reportPath": "/r.json",
                "childExitCode": 1,
                "authorEvidence": [{"errorCode": "MODEL_TIMEOUT", "errorName": "TimeoutError"}],
                "report": {
                    "outcome": "BLOCKED",
                    "code": "ADAPTER_EXCEPTION",
                    "reportStored": True,
                    "runId": "72a784c2-903b-4b66-8afb-c44133cba631",
                    "storedReportSha256": "a" * 64,
                },
            }

        job = {
            "job_id": "j",
            "program": "tiny-arithmetic.mjs",
            "route": {"worker": "monterey-mac-pro", "preanalysis": "local", "reason": "strict-local"},
        }
        with tempfile.TemporaryDirectory() as td:
            worker = ww.Worker("monterey-mac-pro", "m", ENDPOINT, td, execute=execute)
            receipt = worker.run_job(dict(job))
            worker.run_job(dict(job, model_timeout_s=600, disable_thinking=True, max_tokens=256))
            worker.run_job(dict(job, response_format="json-object"))
            with self.assertRaises(ww.Refused):
                worker.run_job(dict(job, response_format="grammar"))
            with self.assertRaises(ww.Refused):
                worker.run_job(dict(job, max_tokens=8))
            with self.assertRaises(ww.Refused):
                worker.run_job(dict(job, model_timeout_s=5000))
            with self.assertRaises(ww.Refused):
                ww.Worker("m5-macbook-pro", "m", ENDPOINT, td, model_timeout_s=0)
        self.assertEqual(seen, [(840, False, 512), (600, True, 256), (840, False, 512)])
        self.assertEqual(formats, ["json-schema", "json-schema", "json-object"])
        self.assertEqual(receipt["responseFormat"], "json-schema")
        self.assertEqual(receipt["code"], "ADAPTER_EXCEPTION")
        self.assertEqual(receipt["modelTimeoutS"], 840)
        self.assertEqual(receipt["maxTokens"], 512)
        self.assertEqual(receipt["authorEvidence"][0]["errorCode"], "MODEL_TIMEOUT")
        self.assertEqual(receipt["outcome"], "BLOCKED")


class RouterTimeoutTests(unittest.TestCase):
    def test_job_timeouts(self):
        self.assertGreater(rc.job_timeout("monterey-mac-pro"), pilot.model_timeout_s("monterey-mac-pro"))
        self.assertLessEqual(pilot.model_timeout_s("monterey-mac-pro"), pilot.MAX_MODEL_TIMEOUT_S)
        self.assertEqual(rc.job_timeout("m5-macbook-pro"), 90)
        self.assertEqual(rc.job_timeout("dual-gpu-pc", 120), 120)
        with self.assertRaises(rc.Refused):
            rc.job_timeout("dual-gpu-pc", 0)

    def test_router_forwards_validated_response_format(self):
        import os
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            token = Path(td) / "t"
            token.write_bytes(b"x" * 40)
            os.chmod(str(token), 0o600)

            def load(extra):
                rows = {
                    "monterey-mac-pro": {"url": "http://127.0.0.1:18791", "token_file": str(token)},
                    "m5-macbook-pro": dict({"url": "http://127.0.0.1:18790", "token_file": str(token)}, **extra),
                }
                cfg = Path(td) / "c.json"
                cfg.write_text(json.dumps({"workers": rows}))
                return rc.load_workers(str(cfg))[0]

            workers = load({})
            self.assertEqual(workers["monterey-mac-pro"]["response_format"], "json-schema")
            self.assertIsNone(workers["m5-macbook-pro"]["response_format"])
            self.assertEqual(
                load({"response_format": "json-schema"})["m5-macbook-pro"]["response_format"], "json-schema"
            )
            with self.assertRaises(rc.Refused):
                load({"response_format": "regex"})


if __name__ == "__main__":
    unittest.main()
