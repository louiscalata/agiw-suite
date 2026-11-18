import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from unittest.mock import Mock

if not shutil.which("git"):
    raise unittest.SkipTest("git not available")

from agiw import fleet_local_pilot
from agiw import fleet_repo_patch as rp
from agiw import windows_worker as ww
from agiw.windows_worker_receipt_check import validate_job_receipt


def git(args, cwd, check=True):
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "init.defaultBranch=main",
        ]
        + args,
        cwd=str(cwd),
        capture_output=True,
        check=check,
        text=True,
        env=env,
    )


class GitEnvTests(unittest.TestCase):
    def test_windows_env(self):
        tmp = Path("tmp")
        home = str(tmp / "home")
        env = rp._env(
            tmp,
            name="nt",
            environ={
                "PATH": r"C:\Git\cmd",
                "SYSTEMROOT": r"D:\Windows",
                "WINDIR": r"D:\WINDOWS",
                "COMSPEC": r"D:\Windows\System32\cmd.exe",
                "PATHEXT": ".EXE;.CMD",
                "HOME": r"C:\User",
                "USERPROFILE": r"C:\User",
                "TEMP": r"C:\User\Temp",
                "TMP": r"C:\User\Temp",
                "HOMEPATH": r"\User",
                "GITHUB_TOKEN": "fixture",
                "HTTPS_PROXY": "fixture",
                "GIT_ASKPASS": "fixture",
            },
        )
        self.assertEqual(
            env,
            {
                "PATH": r"C:\Git\cmd",
                "HOME": home,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "SYSTEMROOT": r"D:\Windows",
                "WINDIR": r"D:\WINDOWS",
                "COMSPEC": r"D:\Windows\System32\cmd.exe",
                "PATHEXT": ".EXE;.CMD",
                "USERPROFILE": home,
                "TEMP": home,
                "TMP": home,
            },
        )

    def test_unix_env(self):
        tmp = Path("tmp")
        env = rp._env(tmp, name="posix", environ={"PATH": "/bin", "HOME": "/user", "USERPROFILE": "/user"})
        self.assertEqual(
            env,
            {
                "PATH": "/bin",
                "HOME": str(tmp / "home"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": shutil.which("false") or "false",
                "GIT_OPTIONAL_LOCKS": "0",
            },
        )


class RepoPatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tmp_root = self.root / "tmp-root"
        self.tmp_root.mkdir()
        self.bare = self.root / "bare.git"
        self.work = self.root / "work"
        self.bare.mkdir()
        git(["init", "--bare", str(self.bare)], self.root)
        git(["clone", str(self.bare), str(self.work)], self.root)
        git(["checkout", "-b", "main"], self.work)
        (self.work / "README.md").write_text("hello\n", encoding="utf-8")
        (self.work / "src").mkdir()
        (self.work / "src" / "a.py").write_text('print("a")\n', encoding="utf-8")
        git(["add", "-A"], self.work)
        git(["commit", "-m", "init"], self.work)
        git(["push", "-u", "origin", "main"], self.work)
        self.commit = git(["rev-parse", "HEAD"], self.work).stdout.strip()
        self.refs = (
            git(["for-each-ref", "--format=%(refname):%(objectname)", "refs"], self.bare).stdout.strip().splitlines()
        )
        self.repo = str(self.bare)
        self.store = self.root / "reports"
        self.store.mkdir()

    def tearDown(self):
        self.assertEqual(os.listdir(self.tmp_root), [])

    def spec(self, **kw):
        base = {
            "repo": self.repo,
            "commit": self.commit,
            "allowlist": ["README.md"],
            "task": "edit readme",
            "max_diff_bytes": 10000,
            "timeout_seconds": 5,
        }
        base.update(kw)
        return base

    def test_happy_path(self):
        out = rp.run(
            self.spec(),
            lambda files, task, workdir: {"README.md": files["README.md"] + "new line\n"},
            allow_local_source=True,
            tmp_root=self.tmp_root,
        )
        self.assertEqual(out["outcome"], "COMPLETED")
        self.assertEqual(out["changedFiles"], ["README.md"])
        self.assertIn("+new line", out["diff"])
        self.assertEqual(out["jevPre"]["status"], "unavailable")
        self.assertEqual(out["jevPre"]["errorCode"], "JEV_UNAVAILABLE")
        self.assertEqual(out["jevPre"]["stage"], "no_analyzer")
        self.assertEqual(out["jevPost"]["status"], "unavailable")
        self.assertEqual(out["diffSha256"], hashlib.sha256(out["diff"].encode("utf-8", "surrogateescape")).hexdigest())

    def test_allowlist_violation_return_and_disk(self):
        out = rp.run(
            self.spec(),
            lambda files, task, workdir: {"src/a.py": "x"},
            allow_local_source=True,
            tmp_root=self.tmp_root,
        )
        self.assertEqual(out["outcome"], "BLOCKED")
        self.assertEqual(out["code"], "ALLOWLIST_VIOLATION")

        def patcher(files, task, workdir):
            (workdir / "src").mkdir(exist_ok=True)
            (workdir / "src" / "a.py").write_text("x\n", encoding="utf-8")
            return {}

        out = rp.run(
            self.spec(),
            patcher,
            allow_local_source=True,
            tmp_root=self.tmp_root,
        )
        self.assertEqual(out["outcome"], "BLOCKED")
        self.assertEqual(out["code"], "ALLOWLIST_VIOLATION")

    def test_oversize_and_lines(self):
        out = rp.run(
            self.spec(max_diff_bytes=1),
            lambda files, task, workdir: {"README.md": files["README.md"] + "x\n"},
            allow_local_source=True,
            tmp_root=self.tmp_root,
        )
        self.assertEqual(out["code"], "DIFF_TOO_LARGE")
        out = rp.run(
            self.spec(max_changed_lines=1),
            lambda files, task, workdir: {"README.md": "a\nb\nc\n"},
            allow_local_source=True,
            tmp_root=self.tmp_root,
        )
        self.assertEqual(out["code"], "DIFF_TOO_MANY_LINES")

    def test_no_push(self):
        pushes = []

        def patcher(files, task, workdir):
            for cmd in [
                ["git", "push", "origin", "HEAD:refs/heads/main"],
                ["git", "push", str(self.bare), "HEAD:refs/heads/evil"],
                ["git", "push", "--no-verify", str(self.bare), "HEAD:refs/heads/evil2"],
            ]:
                r = subprocess.run(cmd, cwd=str(workdir), capture_output=True)
                pushes.append(r.returncode)
            return {"README.md": files["README.md"] + "x\n"}

        before = git(["for-each-ref", "--format=%(refname):%(objectname)", "refs"], self.bare).stdout.strip()
        out = rp.run(self.spec(), patcher, allow_local_source=True, tmp_root=self.tmp_root)
        after = git(["for-each-ref", "--format=%(refname):%(objectname)", "refs"], self.bare).stdout.strip()
        self.assertEqual(out["outcome"], "COMPLETED")
        self.assertEqual(len(pushes), 3)
        self.assertTrue(all(code != 0 for code in pushes))
        self.assertEqual(before, after)

    def test_escape_and_cleanup(self):
        outside = self.root / "outside"
        outside.mkdir()
        esc = self.root / "escape.txt"

        def patcher(files, task, workdir):
            return {"../escape.txt": "x"}

        out = rp.run(self.spec(), patcher, allow_local_source=True, tmp_root=self.tmp_root)
        self.assertEqual(out["outcome"], "BLOCKED")
        self.assertEqual(out["code"], "ALLOWLIST_VIOLATION")
        self.assertFalse(esc.exists())

        def patcher2(files, task, workdir):
            (workdir / "link").symlink_to(outside, target_is_directory=True)
            return {"link/x.txt": "x"}

        out = rp.run(
            self.spec(allowlist=["README.md", "link/x.txt"]),
            patcher2,
            allow_local_source=True,
            tmp_root=self.tmp_root,
        )
        self.assertEqual(out["code"], "PATH_ESCAPE")
        self.assertEqual(list(outside.iterdir()), [])

        def boom(files, task, workdir):
            raise RuntimeError("boom")

        out = rp.run(self.spec(), boom, allow_local_source=True, tmp_root=self.tmp_root)
        self.assertEqual(out["outcome"], "BLOCKED")
        self.assertEqual(out["code"], "PATCH_FAILED")

    def test_validate_refusals(self):
        for payload in [
            self.spec(repo="http://github.com/a/b"),
            self.spec(repo="ssh://github.com/a/b"),
            self.spec(repo="https://evil.com/a/b"),
            self.spec(commit="1" * 39),
            self.spec(allowlist=["/abs"]),
            self.spec(allowlist=["../x"]),
            self.spec(max_diff_bytes=0),
        ]:
            with self.assertRaises(rp.Refused):
                rp.validate_spec(payload, allow_local_source=True)

        with self.assertRaisesRegex(rp.Refused, "^JEV_ANALYSIS_REFUSED$"):
            rp.validate_spec(self.spec(jev_analysis=1), allow_local_source=True)

    def test_project_queue_entry_shape(self):
        entry = {
            "repo": "github.com/example-owner/example-repo (PUBLIC, default branch main)",
            "bounded_scope": ["tests_cli/test_windows_worker_receipt_check.py"],
            "task": "Add one unit test for a stale job receipt.",
            "timeout_seconds": 600,
            "commit": "a" * 40,
            "max_diff_bytes": 20000,
        }
        spec = rp.validate_spec(entry)
        self.assertEqual(spec["repo"], "https://github.com/example-owner/example-repo")
        self.assertEqual(spec["allowlist"], ["tests_cli/test_windows_worker_receipt_check.py"])
        self.assertEqual(os.listdir(self.tmp_root), [])

    def test_unknown_commit(self):
        out = rp.run(
            self.spec(commit="0123456789abcdef0123456789abcdef01234567"),
            lambda files, task, workdir: {},
            allow_local_source=True,
            tmp_root=self.tmp_root,
        )
        self.assertEqual(out["outcome"], "BLOCKED")
        self.assertEqual(out["code"], "COMMIT_NOT_FOUND")
        self.assertEqual(out["jevPre"]["status"], "not_run")
        self.assertEqual(out["jevPost"]["status"], "not_run")
        self.assertEqual(os.listdir(self.tmp_root), [])

    def test_jev_failures_are_advisory(self):
        for pre, code in (
            (Mock(side_effect=RuntimeError("not recorded")), "JEV_ANALYZER_ERROR"),
            (Mock(return_value={"status": "unavailable"}), "JEV_UNAVAILABLE"),
            (Mock(return_value={"status": "ok", "plan": None}), "JEV_INVALID_OUTPUT"),
        ):
            with self.subTest(code=code):
                patcher = Mock(return_value={"README.md": "changed\n"})
                post = Mock(side_effect=TimeoutError)
                out = rp.run(
                    self.spec(),
                    patcher,
                    allow_local_source=True,
                    tmp_root=self.tmp_root,
                    jev_pre=pre,
                    jev_post=post,
                )
                self.assertEqual(out["outcome"], "COMPLETED")
                self.assertIn("+changed", out["diff"])
                self.assertEqual(out["jevPre"]["status"], "unavailable")
                self.assertEqual(out["jevPre"]["stage"], "pre")
                self.assertEqual(out["jevPre"]["errorCode"], code)
                self.assertNotIn("plan", out["jevPre"])
                self.assertNotIn("analysis", patcher.call_args.kwargs)
                self.assertEqual(patcher.call_args.kwargs["task"], "edit readme")
                self.assertEqual(out["jevPost"]["errorCode"], "JEV_TIMEOUT")
                self.assertNotIn("verdict", out["jevPost"])

    def test_jev_disabled(self):
        analyzer = Mock()
        out = rp.run(
            self.spec(jev_analysis=False),
            lambda files, task, workdir: {},
            allow_local_source=True,
            tmp_root=self.tmp_root,
            jev_pre=analyzer,
            jev_post=analyzer,
        )
        self.assertEqual(out["code"], "NO_CHANGE")
        self.assertEqual(out["jevPre"]["status"], "skipped")
        self.assertEqual(out["jevPost"]["status"], "skipped")
        analyzer.assert_not_called()

    def test_jev_post_projection_and_empty_diff(self):
        for content in ({}, {"README.md": "x" * 15000 + "\n"}):
            with self.subTest(empty=not content):
                post = Mock(return_value={"status": "ok", "verdict": "V" * 100})
                out = rp.run(
                    self.spec(max_diff_bytes=40000),
                    lambda files, task, workdir, content=content: content,
                    allow_local_source=True,
                    tmp_root=self.tmp_root,
                    jev_post=post,
                )
                self.assertEqual(out["outcome"], "COMPLETED" if content else "FAILED")
                self.assertEqual(out["jevPost"]["verdict"], "V" * 64)
                diff = out["diff"]
                projected = diff[:6000] + diff[-6000:] if content else ""
                post.assert_called_once_with(projected)
                self.assertEqual(out["jevPost"]["truncated"], bool(content))
                self.assertEqual(out["jevPost"]["inputChars"], len(diff))
                self.assertEqual(out["jevPost"]["inputSha256"], hashlib.sha256(diff.encode("utf-8")).hexdigest())

    def test_chat_patcher(self):
        requests = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps(
                    {
                        "choices": [
                            {"message": {"content": '```json\n{"files":{"README.md":"hi\\n"}}\n```'}},
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        host = "127.0.0.1"
        server = ThreadingHTTPServer((host, 0), H)
        server.address_family = socket.AF_INET
        ready = threading.Event()

        def serve():
            ready.set()
            server.serve_forever()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(2), "loopback test server failed to start")
            port = server.server_address[1]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                probe.settimeout(0.2)
                try:
                    if probe.connect_ex((host, port)) == 0:
                        break
                finally:
                    probe.close()
                time.sleep(0.02)
            else:
                self.fail("loopback test server did not accept 127.0.0.1")
            patcher = rp.chat_patcher(f"http://{host}:{port}/v1/chat/completions", "m", 2)
            pre = Mock(return_value={"status": "ok", "plan": "review the greeting " + "p" * 5000})
            out = rp.run(
                self.spec(),
                patcher,
                allow_local_source=True,
                tmp_root=self.tmp_root,
                jev_pre=pre,
            )
            self.assertEqual(out["outcome"], "COMPLETED")
            self.assertIn("+hi", out["diff"])
            pre.assert_called_once_with("edit readme\n\n--- README.md ---\nhello\n")
            plan = out["jevPre"]["plan"]
            self.assertEqual(len(plan), 4000)
            self.assertTrue(plan.endswith("\n[truncated]"))
            user = json.loads(requests[0]["messages"][1]["content"])
            self.assertEqual(user["jevPrePlan"], plan)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        with self.assertRaises(fleet_local_pilot.Refused):
            rp.chat_patcher("http://example.com/v1/chat/completions", "m", 1)

    def test_execute_job_and_worker(self):
        payload = self.spec()
        result = rp.execute_job(
            payload,
            patcher=lambda files, task, workdir: {"README.md": files["README.md"] + "z\n"},
            allow_local_source=True,
            tmp_root=self.tmp_root,
            report_store=str(self.store),
            model_endpoint="http://127.0.0.1:9/v1/chat/completions",
            model="m",
            jev_pre=Mock(return_value={"status": "ok", "plan": "check README"}),
            jev_post=Mock(return_value={"status": "ok", "verdict": "PROCEED-WITH-CHANGES"}),
        )
        self.assertTrue(result["reportStored"])
        report_path = Path(result["reportPath"])
        self.assertTrue(report_path.exists())
        stored = json.loads(report_path.read_text(encoding="utf-8"))
        for key, field, value in (
            ("jevPre", "plan", "check README"),
            ("jevPost", "verdict", "PROCEED-WITH-CHANGES"),
        ):
            self.assertEqual(stored[key]["status"], "ok")
            self.assertEqual(stored[key][field], value)
            self.assertEqual(result["report"][key], stored[key])
            self.assertEqual(result[key], stored[key])
        receipt = ww.Worker(
            "m5-macbook-pro",
            "fixture-model",
            "http://127.0.0.1:9/v1/chat/completions",
            str(self.root / "receipts"),
            repo_patch=lambda payload, **kw: rp.execute_job(
                payload,
                patcher=lambda files, task, workdir: {"README.md": files["README.md"] + "z\n"},
                allow_local_source=True,
                tmp_root=self.tmp_root,
                model_endpoint=kw["model_endpoint"],
                model=kw["model"],
                report_store=str(self.store),
            ),
            allow_local_source=True,
        ).run_job(
            {
                "job_id": "job-rp",
                "route": {"worker": "m5-macbook-pro", "preanalysis": "local", "reason": "strict-local"},
                "program": "repo-patch",
                "payload": payload,
            }
        )
        self.assertEqual(receipt["outcome"], "COMPLETED")
        self.assertIsNotNone(receipt["artifact"])
        self.assertIn("+z", receipt["artifact"]["content"])
        self.assertTrue(
            validate_job_receipt(
                receipt, expected_worker="m5-macbook-pro", expected_model="fixture-model", job_id="job-rp"
            )["valid"]
        )

    def test_git_failure_stderr_reaches_receipt(self):
        payload = self.spec(repo="https://github.com/example-owner/example-repo")
        token = "ghp_" + "x" * 32
        stderr = (
            b"fatal: Could not resolve host: github.com \xff\n"
            b"https://fixture-user:fixture-pass@example.invalid/repo\n" + token.encode("ascii") + b" detail" * 400
        )
        patcher = mock.Mock()
        with mock.patch.object(
            rp, "_git", side_effect=subprocess.CalledProcessError(128, ["git", "clone"], stderr=stderr)
        ):
            result = rp.run(payload, patcher, tmp_root=self.tmp_root)
            receipt = ww.Worker(
                "m5-macbook-pro",
                "fixture-model",
                "http://127.0.0.1:9/v1/chat/completions",
                str(self.root / "receipts"),
                repo_patch=lambda payload, **kw: rp.execute_job(
                    payload,
                    patcher=patcher,
                    tmp_root=self.tmp_root,
                    model_endpoint=kw["model_endpoint"],
                    model=kw["model"],
                    report_store=str(self.store),
                ),
            ).run_job(
                {
                    "job_id": "job-git-failed",
                    "route": {"worker": "m5-macbook-pro", "preanalysis": "local", "reason": "strict-local"},
                    "program": "repo-patch",
                    "payload": payload,
                }
            )
        patcher.assert_not_called()
        self.assertEqual(result["code"], "GIT_FAILED")
        self.assertEqual(receipt["code"], "GIT_FAILED")
        diagnostic = result["git_stderr"]
        self.assertIn("Could not resolve host: github.com \ufffd", diagnostic)
        self.assertIn("https://[redacted]@example.invalid/repo", diagnostic)
        for secret in ("fixture-user", "fixture-pass", token):
            self.assertNotIn(secret, diagnostic)
        self.assertEqual(len(diagnostic.encode("utf-8")), 2048)
        self.assertEqual(receipt["git_stderr"], diagnostic)
        stored = json.loads(Path(receipt["reportPath"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["git_stderr"], diagnostic)
        self.assertTrue(validate_job_receipt(receipt, expected_worker="m5-macbook-pro")["valid"])

    def test_fixed_program_receipt_has_no_artifact(self):
        def execute(mode, url, model, source, program="nisi-smoke.mjs", report_store=None, **_):
            return {
                "reportStored": True,
                "reportPath": str(self.root / "report.json"),
                "report": {
                    "outcome": "COMPLETED",
                    "reportStored": True,
                    "runId": "00000000-0000-4000-8000-000000000000",
                    "storedReportSha256": "a" * 64,
                },
            }

        w = ww.Worker(
            "m5-macbook-pro",
            "fixture-model",
            "http://127.0.0.1:9/v1/chat/completions",
            str(self.root / "receipts4"),
            execute=execute,
        )
        receipt = w.run_job(
            {
                "job_id": "job-3",
                "route": {"worker": "m5-macbook-pro", "preanalysis": "local", "reason": "strict-local"},
                "program": "nisi-smoke.mjs",
            }
        )
        self.assertEqual(receipt["outcome"], "COMPLETED")
        self.assertNotIn("artifact", receipt)

    def test_worker_refusal_no_payload(self):
        w = ww.Worker(
            "m5-macbook-pro",
            "fixture-model",
            "http://127.0.0.1:9/v1/chat/completions",
            str(self.root / "receipts2"),
            allow_local_source=True,
        )
        with self.assertRaises(ww.Refused) as ctx:
            w.validate_job(
                {
                    "job_id": "job-1",
                    "route": {"worker": "m5-macbook-pro", "preanalysis": "local", "reason": "strict-local"},
                    "program": "repo-patch",
                }
            )
        self.assertEqual(str(ctx.exception), "PAYLOAD_REFUSED")

    def test_worker_refusal_http_repo(self):
        w = ww.Worker(
            "m5-macbook-pro",
            "fixture-model",
            "http://127.0.0.1:9/v1/chat/completions",
            str(self.root / "receipts3"),
            allow_local_source=True,
        )
        with self.assertRaises(ww.Refused) as ctx:
            w.validate_job(
                {
                    "job_id": "job-2",
                    "route": {"worker": "m5-macbook-pro", "preanalysis": "local", "reason": "strict-local"},
                    "program": "repo-patch",
                    "payload": self.spec(repo="http://github.com/a/b"),
                }
            )
        self.assertEqual(str(ctx.exception), "REPO_REFUSED")
