import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agiw import fleet_routing_plan
from agiw import free_pool_lane as pool

FAKE_KEY = "fixture-only-key"
PART_TEXT = "def answer(): return 42"
PROMPT = "Review the implementation."

FAKE_SCRIPT = """\
import json, os, sys, time
MODE = "__MODE__"
HERE = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]
open(os.path.join(HERE, os.path.basename(__file__) + ".calls"), "a", encoding="utf-8").write(json.dumps(argv) + chr(10))
record = {"argv": argv, "env": {k: os.environ.get(k) for k in ("CODEX_API_FREE_POOL_ENV", "HOME") if k in os.environ}}
open(os.path.join(HERE, os.path.basename(__file__) + ".calls.json"), "w", encoding="utf-8").write(json.dumps(record))
if MODE == "hang":
    time.sleep(10)
    raise SystemExit(0)
if MODE == "budget":
    print(json.dumps({"exit": 10, "error": "budget ceiling"}))
    raise SystemExit(10)
if MODE == "credit":
    print(json.dumps({"exit": 9, "message": "credit exhausted, use Zen/local"}))
    raise SystemExit(9)
if MODE == "empty":
    print(json.dumps({"model": "served-model-fixture", "response_id": "resp_fixture", "output_text": "", "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}}))
    raise SystemExit(0)
if MODE == "malformed":
    print("{not-json")
    raise SystemExit(0)
if MODE == "secret":
    print(json.dumps({"model": "served-model-fixture", "response_id": "resp_fixture", "output_text": "sk-proj-fixture", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}))
    raise SystemExit(0)
if MODE == "echo":
    text = open(argv[0], encoding="utf-8").read()
    print(json.dumps({"model": "served-model-fixture", "response_id": "resp_fixture", "output_text": text, "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}))
    raise SystemExit(0)
print(json.dumps({
    "http_status": 200,
    "model": "served-model-fixture",
    "status": "completed",
    "billing": {"payer": "openai"},
    "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
    "response_id": "resp_fixture",
    "output_text": "Looks good.",
    "requested_model": "gpt-5.4-mini",
}))
raise SystemExit(0)
"""


def request(**changes):
    return {"project": "agiw-suite", "source": "code", "tier": "general", "parts": [part()], **changes}


def part(name="code.py", text=PART_TEXT):
    return {"name": name, "text": text}


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
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def script(self, mode="ok"):
        path = self.root / f"free_pool_call_{mode}.py"
        path.write_text(FAKE_SCRIPT.replace("__MODE__", mode), encoding="utf-8")
        return str(path)

    def lane(self, mode="ok", **kwargs):
        kwargs.setdefault("free_pool_call", self.script(mode))
        return pool.FreePoolLane(**kwargs)

    def test_models_request_mini_and_astra_not_sol(self):
        self.assertEqual(pool.MODELS["general"], "gpt-5.4-mini")
        self.assertEqual(pool.MODELS["mini"], "gpt-5.4-mini")
        self.assertEqual(pool.MODELS["hard"], "gpt-6.2-astra")
        self.assertNotIn("gpt-6.2-sol", pool.MODELS.values())
        self.assertNotEqual(pool.MODELS, fleet_routing_plan.POOL_MODELS)

    def test_refusals_never_invoke_the_script(self):
        script = Path(self.script("ok"))
        calls = Path(str(script) + ".calls")
        lane = pool.FreePoolLane(free_pool_call=str(script))
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
                receipt = lane.run(payload, prompt)
                self.assertEqual((receipt["outcome"], receipt["code"]), ("BLOCKED", code))
                self.assertEqual(receipt["parts"], [])
        self.assertFalse(calls.exists())

    def test_missing_script_is_unavailable_without_a_network_call(self):
        receipt = pool.FreePoolLane(free_pool_call=str(self.root / "missing.py")).run(request(), PROMPT)
        self.assertEqual((receipt["outcome"], receipt["code"]), ("BLOCKED", "CALLER_UNAVAILABLE"))

    def test_success_receipt_from_script_for_each_tier(self):
        for tier, model, hard in (
            ("general", "gpt-5.4-mini", False),
            ("mini", "gpt-5.4-mini", False),
            ("hard", "gpt-6.2-astra", True),
        ):
            with self.subTest(tier=tier):
                script = self.script("ok")
                calls = Path(script + ".calls")
                if calls.exists():
                    calls.unlink()
                receipt = pool.FreePoolLane(free_pool_call=script).run(request(tier=tier), PROMPT)
                self.assertEqual(receipt["kind"], "agiw.free-pool-receipt.v1")
                self.assertEqual(receipt["project"], "agiw-suite")
                self.assertEqual(receipt["tier"], tier)
                self.assertEqual((receipt["outcome"], receipt["code"]), ("COMPLETED", "OK"))
                self.assertEqual(receipt["model_requested"], model)
                self.assertEqual(receipt["model_served"], "served-model-fixture")
                self.assertEqual(receipt["response_id"], "resp_fixture")
                self.assertEqual(receipt["usage"], {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
                self.assertEqual(receipt["output_text"], "Looks good.")
                self.assertEqual(receipt["payer"], "openai")
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
                argv = json.loads(calls.read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(argv[argv.index("--model") + 1], model)
                self.assertEqual(argv[argv.index("--project") + 1], "agiw-suite")
                self.assertEqual(argv[argv.index("--source") + 1], "code")
                self.assertIn("--private", argv)
                self.assertEqual("--hard" in argv, hard)
                self.assertNotIn("gpt-6.2-sol", argv)
                self.assertTrue(any((not flag.startswith("--")) and flag.endswith("prompt.txt") for flag in argv))
                for value in (FAKE_KEY, PART_TEXT, PROMPT, "OPENAI_API_KEY"):
                    self.assertNotIn(value, json.dumps(receipt))
                    self.assertNotIn(value, json.dumps(argv))

    def test_budget_ceiling_refusal(self):
        receipt = self.lane("budget").run(request(), PROMPT)
        self.assertEqual((receipt["outcome"], receipt["code"]), ("BLOCKED", "BUDGET_REFUSED"))
        self.assertEqual(receipt["output_text"], "")
        self.assertIsNone(receipt["usage"])

    def test_credit_exhausted_fails_closed(self):
        receipt = self.lane("credit").run(request(), PROMPT)
        self.assertEqual((receipt["outcome"], receipt["code"]), ("BLOCKED", "CREDIT_EXHAUSTED"))
        self.assertEqual(receipt["output_text"], "")

    def test_timeout_does_not_retry(self):
        lane = self.lane("hang", timeout_s=0.2)
        receipt = lane.run(request(), PROMPT)
        self.assertEqual((receipt["outcome"], receipt["code"]), ("BLOCKED", "CLIENT_TIMEOUT"))
        calls = Path(lane.free_pool_call + ".calls")
        self.assertEqual(len(calls.read_text(encoding="utf-8").splitlines()), 1)

    def test_script_output_echo_and_invalid_payloads_are_blocked(self):
        receipt = self.lane("echo").run(request(), PROMPT)
        self.assertEqual((receipt["outcome"], receipt["code"]), ("BLOCKED", "OUTPUT_ECHO_REFUSED"))
        self.assertNotIn(PROMPT, json.dumps(receipt))
        for mode, code in (
            ("empty", "RESPONSE_INVALID"),
            ("malformed", "RESPONSE_INVALID"),
            ("secret", "SECRET_VALUE_REFUSED"),
        ):
            with self.subTest(mode=mode):
                blocked = self.lane(mode).run(request(), PROMPT)
                self.assertEqual((blocked["outcome"], blocked["code"]), ("BLOCKED", code))
                self.assertNotIn(FAKE_KEY, json.dumps(blocked))

    def test_env_file_path_is_forwarded_and_key_stays_off_argv(self):
        env_file = self.root / "codex.env"
        env_file.write_text('export OPENAI_API_KEY="fixture-only-key"\n', encoding="utf-8")
        script = self.script("ok")
        receipt = pool.FreePoolLane(free_pool_call=script, env_file=str(env_file)).run(request(), PROMPT)
        self.assertEqual(receipt["outcome"], "COMPLETED")
        record = json.loads(Path(script + ".calls.json").read_text(encoding="utf-8"))
        self.assertEqual(record["env"].get("CODEX_API_FREE_POOL_ENV"), str(env_file))
        self.assertNotIn("fixture-only-key", json.dumps(record["argv"]))
        self.assertNotIn("fixture-only-key", json.dumps(receipt))

    def test_invalid_timeout_never_invokes_the_script(self):
        script = self.script("ok")
        lane = pool.FreePoolLane(free_pool_call=script)
        lane.timeout_s = float("nan")
        self.assertEqual(lane.run(request(), PROMPT)["code"], "TIMEOUT_REFUSED")
        self.assertFalse(Path(script + ".calls").exists())


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
