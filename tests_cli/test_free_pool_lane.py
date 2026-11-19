import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.response import addinfourl

from agiw import fleet_routing_plan
from agiw import free_pool_lane as pool

FAKE_KEY = "fixture-only-key"
PART_TEXT = "def answer(): return 42"
PROMPT = "Review the implementation."


def request(**changes):
    return {"project": "agiw-suite", "source": "code", "tier": "general", "parts": [part()], **changes}


def part(name="code.py", text=PART_TEXT):
    return {"name": name, "text": text}


def response_data(text="Looks good.", **changes):
    return {
        "id": "resp_fixture",
        "status": "completed",
        "model": "served-model-fixture",
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
        ],
        "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        **changes,
    }


class Response(io.BytesIO):
    def __init__(self, raw, status=200):
        super().__init__(raw)
        self.status = status
        self.read_limit = None

    def read(self, size=-1):
        self.read_limit = size
        return super().read(size)


class ContentFilterTests(unittest.TestCase):
    def assert_refused(self, code, payload):
        with self.assertRaises(pool.PoolRefused) as caught:
            pool.check_content(payload)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_route_refusals_and_malformed_requests(self):
        self.assert_refused("PROJECT_REFUSED", request(project="unknown"))
        self.assert_refused("PROJECT_REFUSED", request(project=[]))
        for source in ["linear", "notion", "granola", "unknown", None]:
            with self.subTest(source=source):
                self.assert_refused("SOURCE_REFUSED", request(source=source))
        self.assert_refused("TIER_REFUSED", request(tier="auto"))
        self.assert_refused("TIER_REFUSED", request(tier={}))
        for payload in (None, request(parts={}), request(parts=[part(text=None)]), request(extra=True)):
            with self.subTest(payload=payload):
                self.assert_refused("REQUEST_INVALID", payload)
        self.assert_refused("REQUEST_INVALID", request(parts=[part(text="\ud800")]))

    def test_secret_file_basenames_case_insensitive_on_both_path_styles(self):
        names = (
            ".env",
            ".env.test",
            "site.pem",
            "site.key",
            "site.p12",
            "site.pfx",
            "id_rsa.pub",
            "id_ed25519",
            "foo.token",
            "credentials.json",
            "secrets.yaml",
            "login.keychain-db",
            ".netrc",
            ".npmrc",
            ".pypirc",
        )
        for name in names:
            with self.subTest(name=name):
                self.assert_refused("SECRET_FILE_REFUSED", request(parts=[part(f"folder\\nested/{name.upper()}")]))

    def test_secret_values(self):
        values = (
            "sk-" + "A" * 20,
            "sk-proj-fixture",
            "ghp_fixture",
            "gho_fixture",
            "ghs_fixture",
            "github_pat_fixture",
            "AKIA" + "A" * 16,
            "ASIA" + "A" * 16,
            "xoxb-fixture",
            "xapp-fixture",
            "-----BEGIN RSA PRIVATE KEY-----",
            "Bearer " + "ABCD." * 4,
            "AIza" + "Z" * 35,
            '"auth": "ABCDEF0123456789"',
            "credential = 'ABCDEF0123456789'",
            "private_key: ABCDEF0123456789",
            "OPENAI_API_KEY=ABCDEF0123456789",
            "secret: ABCDEF0123456789",
            "token: ABCDEF0123456789",
            "password: ABCDEF0123456789",
            "passwd: ABCDEF0123456789",
        )
        for index, text in enumerate(values):
            with self.subTest(index=index):
                self.assert_refused("SECRET_VALUE_REFUSED", request(parts=[part(text=text)]))
        self.assert_refused("SECRET_VALUE_REFUSED", request(parts=[part("github_pat_fixture.py")]))

    def test_placeholders_and_allowed_routes(self):
        text = (
            'api_key = "<redacted>"\n'
            'secret = "${VAR}"\n'
            'token = os.environ["MY_LONG_TOKEN_NAME"]\n'
            "password = process.env.PASSWORD\n"
            'passwd = ""\n'
            'auth = "xxxxxxxxxxxxxxxxxxxxxxxx"\n'
        )
        for project in ("nisi", "jaryo", "opos", "agiw-suite"):
            with self.subTest(project=project):
                pool.check_content(request(project=project, source="spec", parts=[part(text=text)]))

    def test_logs(self):
        self.assert_refused("RAW_LOG_REFUSED", request(parts=[part("run.LOG")]))
        for line in ("2026-11-18T00:00:00Z start", "Nov 18 12:03:45 start", "[WARN] start", '{"severity": "info"}'):
            with self.subTest(line=line):
                pool.check_content(request(parts=[part(text=(line + "\n") * 4)]))
                self.assert_refused("RAW_LOG_REFUSED", request(parts=[part(text=(line + "\n") * 5)]))
        text = "\n".join(json.dumps({key: 1}) for key in ("level", "severity", "ts", "timestamp", "time"))
        self.assert_refused("RAW_LOG_REFUSED", request(parts=[part(text=text)]))
        trace = "Traceback (most recent call last):\n" + '  File "test.py", line 1\n' * 3
        self.assert_refused("RAW_LOG_REFUSED", request(parts=[part(text=trace)]))

    def test_size_and_empty(self):
        pool.check_content(request(parts=[part(text="é" * pool.MAX_CHARS)]))
        self.assert_refused("SIZE_REFUSED", request(parts=[part(text="é" * pool.MAX_CHARS), part(text="x")]))
        self.assert_refused("EMPTY_REFUSED", request(parts=[]))
        self.assert_refused("EMPTY_REFUSED", request(parts=[part(text=" \n")]))


class FreePoolLaneTests(unittest.TestCase):
    def setUp(self):
        self.opener = Mock()
        self.key_source = Mock(return_value=FAKE_KEY)
        self.lane = pool.FreePoolLane(self.key_source, self.opener)

    def test_models_share_the_fleet_policy_mapping(self):
        self.assertIs(pool.MODELS, fleet_routing_plan.POOL_MODELS)

    def test_refusals_never_load_key_or_open_network(self):
        cases = [
            ("PROJECT_REFUSED", request(project="other"), PROMPT),
            ("SOURCE_REFUSED", request(source="notion"), PROMPT),
            ("TIER_REFUSED", request(tier="other"), PROMPT),
            ("REQUEST_INVALID", request(), None),
            ("SECRET_FILE_REFUSED", request(parts=[part(".env")]), PROMPT),
            ("SECRET_VALUE_REFUSED", request(), "sk-proj-fixture"),
            ("RAW_LOG_REFUSED", request(), "DEBUG line\n" * 5),
            ("SIZE_REFUSED", request(parts=[part(text="x" * pool.MAX_CHARS)]), PROMPT),
            ("EMPTY_REFUSED", request(), " "),
        ]
        for code, payload, prompt in cases:
            with self.subTest(code=code):
                receipt = self.lane.run(payload, prompt)
                self.assertEqual((receipt["outcome"], receipt["code"]), ("BLOCKED", code))
                self.assertEqual(receipt["parts"], [])
        self.key_source.assert_not_called()
        self.opener.open.assert_not_called()

    def test_one_post_for_each_tier_and_metadata_only_receipt(self):
        for tier, model in (("general", "gpt-6.2-sol"), ("hard", "gpt-6.2-astra")):
            with self.subTest(tier=tier):
                self.opener.reset_mock()
                self.opener.open.return_value = response = Response(json.dumps(response_data()).encode())
                receipt = self.lane.run(request(tier=tier), PROMPT)
                self.opener.open.assert_called_once()
                req = self.opener.open.call_args.args[0]
                self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 300})
                self.assertEqual(req.get_method(), "POST")
                self.assertEqual(req.full_url, "https://api.openai.com/v1/responses")
                self.assertEqual(req.get_header("Authorization"), f"Bearer {FAKE_KEY}")
                self.assertEqual(req.get_header("Content-type"), "application/json")
                body = json.loads(req.data)
                self.assertEqual(set(body), {"model", "input", "store", "max_output_tokens"})
                self.assertEqual(body["model"], model)
                self.assertIs(body["store"], False)
                self.assertGreater(body["max_output_tokens"], 0)
                self.assertEqual(body["input"], f"{PART_TEXT}\n\n{PROMPT}")
                self.assertEqual(response.read_limit, 4 * 1024 * 1024 + 1)
                self.assertEqual(receipt["kind"], "agiw.free-pool-receipt.v1")
                self.assertEqual(receipt["project"], "agiw-suite")
                self.assertEqual(receipt["tier"], tier)
                self.assertEqual((receipt["outcome"], receipt["code"]), ("COMPLETED", "OK"))
                self.assertEqual(receipt["model_requested"], model)
                self.assertEqual(receipt["model_served"], "served-model-fixture")
                self.assertEqual(receipt["response_id"], "resp_fixture")
                self.assertEqual(receipt["usage"], response_data()["usage"])
                self.assertEqual(receipt["output_text"], "Looks good.")
                self.assertEqual(receipt["prompt_sha256"], hashlib.sha256(PROMPT.encode()).hexdigest())
                self.assertEqual(
                    receipt["parts"],
                    [
                        {
                            "name": "code.py",
                            "sha256": hashlib.sha256(PART_TEXT.encode()).hexdigest(),
                            "chars": len(PART_TEXT),
                        }
                    ],
                )
                for value in (FAKE_KEY, PART_TEXT, PROMPT):
                    self.assertNotIn(value, json.dumps(receipt))

    def test_key_is_not_echoed_from_errors_or_success_payloads(self):
        self.key_source.side_effect = RuntimeError(FAKE_KEY)
        receipt = self.lane.run(request(), PROMPT)
        self.assertEqual(receipt["code"], "KEY_REFUSED")
        self.assertNotIn(FAKE_KEY, json.dumps(receipt))
        self.opener.open.assert_not_called()

        self.key_source.side_effect = None
        receipt = self.lane.run(request(), FAKE_KEY)
        self.assertEqual(receipt["code"], "SECRET_VALUE_REFUSED")
        self.opener.open.assert_not_called()

        for error in (OSError(FAKE_KEY), pool.PoolRefused(FAKE_KEY)):
            self.opener.open.side_effect = error
            receipt = self.lane.run(request(), PROMPT)
            self.assertEqual(receipt["code"], "TRANSPORT_REFUSED")
            self.assertNotIn(FAKE_KEY, json.dumps(receipt))

        self.opener.open.side_effect = None
        self.opener.open.return_value = Response(json.dumps(response_data(text=FAKE_KEY)).encode())
        receipt = self.lane.run(request(), PROMPT)
        self.assertEqual(receipt["code"], "SECRET_VALUE_REFUSED")
        self.assertNotIn(FAKE_KEY, json.dumps(receipt))

    def test_status_malformed_incomplete_and_oversized_response(self):
        cases = [
            ("REDIRECT_REFUSED", b"", 302),
            ("HTTP_REFUSED", b"", 401),
            ("RESPONSE_INVALID", b"not json", 200),
            ("RESPONSE_INVALID", b'{"status":"incomplete","status":"completed"}', 200),
            ("RESPONSE_INVALID", json.dumps(response_data(status="incomplete")).encode(), 200),
            ("RESPONSE_INVALID", json.dumps(response_data(usage=None)).encode(), 200),
            ("RESPONSE_SIZE_REFUSED", b" " * (pool.MAX_RESPONSE_BYTES + 1), 200),
        ]
        for code, raw, status in cases:
            with self.subTest(code=code):
                self.opener.reset_mock()
                self.opener.open.return_value = Response(raw, status)
                receipt = self.lane.run(request(), PROMPT)
                self.assertEqual((receipt["outcome"], receipt["code"]), ("BLOCKED", code))
                self.opener.open.assert_called_once()

        self.opener.open.side_effect = HTTPError(pool.ENDPOINT, 307, FAKE_KEY, {}, None)
        receipt = self.lane.run(request(), PROMPT)
        self.assertEqual(receipt["code"], "REDIRECT_REFUSED")
        self.assertNotIn(FAKE_KEY, json.dumps(receipt))

    def test_model_cannot_echo_verbatim_input_into_receipt(self):
        for text in (PART_TEXT, PROMPT):
            self.opener.open.return_value = Response(json.dumps(response_data(text=f"Echo: {text}")).encode())
            receipt = self.lane.run(request(), PROMPT)
            self.assertEqual((receipt["outcome"], receipt["code"]), ("BLOCKED", "OUTPUT_ECHO_REFUSED"))
            self.assertNotIn(text, json.dumps(receipt))

    def test_default_opener_does_not_follow_redirect_or_use_proxy(self):
        headers = Message()
        headers["Location"] = "https://redirect.invalid/"
        response = addinfourl(io.BytesIO(), headers, pool.ENDPOINT, 302)
        response.msg = "Found"
        with patch.dict(os.environ, {"https_proxy": "http://proxy.invalid:8080"}, clear=True):
            with patch("urllib.request.HTTPSHandler.https_open", return_value=response) as https_open:
                receipt = pool.FreePoolLane(self.key_source).run(request(), PROMPT)
        self.assertEqual(receipt["code"], "REDIRECT_REFUSED")
        https_open.assert_called_once()
        self.assertEqual(https_open.call_args.args[0].host, "api.openai.com")
        response.close()

    def test_missing_invalid_key_and_timeout_never_open_network(self):
        for key in (None, "", "bad\nheader"):
            with self.subTest(key=key):
                self.key_source.return_value = key
                self.assertEqual(self.lane.run(request(), PROMPT)["code"], "KEY_REFUSED")
        self.lane.timeout_s = float("nan")
        self.assertEqual(self.lane.run(request(), PROMPT)["code"], "TIMEOUT_REFUSED")
        self.opener.open.assert_not_called()


class FreePoolCliTests(unittest.TestCase):
    def test_dry_run_only_prints_code_and_never_loads_key_or_transport(self):
        with TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.txt"
            args = ["--project", "nisi", "--source", "code", "--tier", "hard", "--prompt-file", str(prompt)]
            for text, code, status in ((PROMPT, "OK", 0), ("sk-proj-fixture", "SECRET_VALUE_REFUSED", 2)):
                with self.subTest(code=code):
                    prompt.write_text(text, encoding="utf-8")
                    stdout = io.StringIO()
                    with patch.object(pool, "FreePoolLane") as lane, redirect_stdout(stdout):
                        self.assertEqual(pool.main(args + ["--dry-run"]), status)
                    lane.assert_not_called()
                    self.assertEqual(stdout.getvalue(), code + "\n")

    def test_cli_passes_source_to_route_filter(self):
        with TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.txt"
            prompt.write_text(PROMPT, encoding="utf-8")
            args = ["--project", "nisi", "--tier", "hard", "--prompt-file", str(prompt), "--dry-run"]
            for source in ("code", "spec"):
                with self.subTest(source=source):
                    stdout = io.StringIO()
                    with patch.object(pool, "_check_route", wraps=pool._check_route) as check, redirect_stdout(stdout):
                        self.assertEqual(pool.main(args + ["--source", source]), 0)
                    check.assert_called_with({"project": "nisi", "tier": "hard", "source": source, "parts": []})
                    self.assertEqual(stdout.getvalue(), "OK\n")

    def test_cli_requires_source_and_rejects_unsupported_values(self):
        args = ["--project", "nisi", "--tier", "hard", "--prompt-file", "unused.txt", "--dry-run"]
        for source_args in ([], ["--source", "notion"]):
            with self.subTest(source_args=source_args):
                stdout = io.StringIO()
                with patch.object(pool, "_load_part") as load_part, redirect_stdout(stdout):
                    self.assertEqual(pool.main(args + source_args), 2)
                load_part.assert_not_called()
                self.assertEqual(stdout.getvalue(), "ARGUMENT_REFUSED\n")

    def test_cli_refuses_secret_prompt_filename_missing_input_and_bad_arguments(self):
        with TemporaryDirectory() as directory:
            for name, code in ((".env.production", "SECRET_FILE_REFUSED"), ("missing.txt", "INPUT_UNAVAILABLE")):
                stdout = io.StringIO()
                args = ["--project", "nisi", "--source", "code", "--tier", "hard"]
                args += ["--prompt-file", str(Path(directory) / name), "--dry-run"]
                with redirect_stdout(stdout):
                    self.assertEqual(pool.main(args), 2)
                self.assertEqual(stdout.getvalue(), code + "\n")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(pool.main([FAKE_KEY]), 2)
        self.assertEqual(stdout.getvalue(), "ARGUMENT_REFUSED\n")

    def test_default_credential_from_environment_or_file(self):
        opener = Mock()
        with patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY}):
            opener.open.return_value = Response(json.dumps(response_data()).encode())
            receipt = pool.FreePoolLane(opener=opener).run(request(), PROMPT)
        self.assertEqual(receipt["outcome"], "COMPLETED")
        self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), f"Bearer {FAKE_KEY}")
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(f'# fixture\nexport OPENAI_API_KEY="{FAKE_KEY}"\n', encoding="utf-8")
            opener.open.return_value = Response(json.dumps(response_data()).encode())
            with patch.dict(os.environ, {"OPENAI_API_KEY": "wrong-fixture"}):
                lane = pool.FreePoolLane(opener=opener, env_file=env_file)
                self.assertEqual(lane.run(request(), PROMPT)["outcome"], "COMPLETED")
                self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), f"Bearer {FAKE_KEY}")
                env_file.write_text("OPENAI_API_KEY=fixture1\nOPENAI_API_KEY=fixture2\n", encoding="utf-8")
                opener.reset_mock()
                self.assertEqual(lane.run(request(), PROMPT)["code"], "KEY_REFUSED")
                opener.open.assert_not_called()
