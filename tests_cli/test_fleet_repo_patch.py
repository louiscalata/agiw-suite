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


class EditFormatTests(unittest.TestCase):
    """Pure SEARCH/REPLACE parser and applier; no git, no model."""

    def reason(self, files, text, allowlist):
        with self.assertRaises(rp.Refused) as ctx:
            rp.apply_edits(files, rp.parse_edits(text, allowlist), allowlist)
        return str(ctx.exception), getattr(ctx.exception, "reason", None)

    def test_parse_two_blocks_two_files(self):
        text = (
            "README.md\n<<<<<<< SEARCH\nhello\n=======\nhi\n>>>>>>> REPLACE\n\n"
            "src/a.py\n<<<<<<< SEARCH\nprint outward\n=======\nprint in\n>>>>>>> REPLACE\n"
        )
        edits = rp.parse_edits(text)
        self.assertEqual(
            edits,
            [("README.md", "hello", "hi"), ("src/a.py", "print outward", "print in")],
        )

    def test_path_line_tolerates_wrappers(self):
        for line in ("`docs/a.md`", "**docs/a.md**", "File: docs/a.md", "Path: docs/a.md", "### docs/a.md"):
            with self.subTest(line=line):
                edits = rp.parse_edits(f"{line}\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
                self.assertEqual(edits[0][0], "docs/a.md")

    def test_single_allowlisted_path_used_when_path_missing(self):
        text = "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
        self.assertEqual(rp.parse_edits(text, ["README.md"])[0][0], "README.md")
        self.assertEqual(
            rp.parse_edits("<<<<<<<  SEARCH  \nold\n=====  \nnew\n>>>>>> REPLACE\n", ["README.md"])[0][1],
            "old",
        )
        with self.assertRaises(rp.Refused):
            rp.parse_edits(text, ["README.md", "src/a.py"])

    def test_fenced_reply_and_per_block_fences(self):
        text = (
            "```text\n"
            "docs/a.md\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
            "```\n"
            "docs/b.md\n```text\n<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n```\n"
            "```\n"
        )
        self.assertEqual(
            rp.parse_edits(text),
            [("docs/a.md", "old", "new"), ("docs/b.md", "x", "y")],
        )

    def test_thinking_is_stripped(self):
        closed = "<thinking>let me plan this out</thinking>\nREADME.md\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE"
        self.assertEqual(rp.parse_edits(closed), [("README.md", "a", "b")])
        unclosed = "<think_events>I should not finish\nREADME.md\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE"
        with self.assertRaises(rp.Refused):
            rp.parse_edits(unclosed)

    def test_missing_path_and_blocks_are_malformed(self):
        self.assertEqual(
            self.reason({}, "just some prose about the task", ["README.md"])[1],
            "NO_EDITS",
        )
        self.assertEqual(
            self.reason({}, "README.md\n<<<<<<< SEARCH\nold\n", ["README.md"]),
            ("PATCH_OUTPUT_INVALID", "MALFORMED_BLOCK"),
        )

    def test_search_must_be_unique(self):
        files = {"README.md": "same\nother\nsame\n"}
        self.assertEqual(
            self.reason(files, "README.md\n<<<<<<< SEARCH\nsame\n=======\nnew\n>>>>>>> REPLACE\n", ["README.md"]),
            ("PATCH_OUTPUT_INVALID", "SEARCH_NOT_UNIQUE"),
        )

    def test_search_must_be_found(self):
        files = {"README.md": "hello\n"}
        self.assertEqual(
            self.reason(files, "README.md\n<<<<<<< SEARCH\nabsent\n=======\nnew\n>>>>>>> REPLACE\n", ["README.md"]),
            ("PATCH_OUTPUT_INVALID", "SEARCH_NOT_FOUND"),
        )

    def test_path_outside_allowlist(self):
        files = {"README.md": "hello\n"}
        self.assertEqual(
            self.reason(files, "src/a.py\n<<<<<<< SEARCH\nhello\n=======\nnew\n>>>>>>> REPLACE\n", ["README.md"]),
            ("ALLOWLIST_VIOLATION", None),
        )

    def test_crlf_model_output_into_lf_file(self):
        files = {"README.md": "one\ntwo\nthree\n"}
        text = "README.md\r\n<<<<<<< SEARCH\r\ntwo\r\n=======\nTWO\r\n>>>>>>> REPLACE\r\n"
        self.assertEqual(rp.apply_edits(files, rp.parse_edits(text), ["README.md"]), {"README.md": "one\nTWO\nthree\n"})

    def test_lf_model_output_into_crlf_file_keeps_crlf(self):
        files = {"README.md": "one\r\ntwo\r\nthree\r\n"}
        text = "README.md\n<<<<<<< SEARCH\ntwo\n=======\nTWO\n>>>>>>> REPLACE\n"
        self.assertEqual(
            rp.apply_edits(files, rp.parse_edits(text), ["README.md"]),
            {"README.md": "one\r\nTWO\r\nthree\r\n"},
        )

    def test_whitespace_fallback_covers_indentation(self):
        files = {"src/a.py": "def f():\n    value = 1\n    return value\n"}
        text = "src/a.py\n<<<<<<< SEARCH\nvalue = 1\nreturn value\n=======\nvalue = 2\nreturn value\n>>>>>>> REPLACE\n"
        self.assertEqual(
            rp.apply_edits(files, rp.parse_edits(text), ["src/a.py"]),
            {"src/a.py": "def f():\nvalue = 2\nreturn value\n"},
        )

    def test_whitespace_fallback_refuses_ambiguity(self):
        files = {"src/a.py": "if x:\n    return 1\nif y:\n    return 1\n"}
        text = "src/a.py\n<<<<<<< SEARCH\nreturn 1\n=======\nreturn 2\n>>>>>>> REPLACE\n"
        self.assertEqual(
            self.reason(files, text, ["src/a.py"]),
            ("PATCH_OUTPUT_INVALID", "SEARCH_NOT_UNIQUE"),
        )

    def test_empty_search_creates_missing_file_only(self):
        text = "docs/new.md\n<<<<<<< SEARCH\n=======\nfresh body\n>>>>>>> REPLACE\n"
        self.assertEqual(
            rp.apply_edits({"README.md": "hi\n"}, rp.parse_edits(text), ["README.md", "docs/new.md"]),
            {"docs/new.md": "fresh body"},
        )
        self.assertEqual(
            self.reason({"docs/new.md": "already here\n"}, text, ["docs/new.md"]),
            ("PATCH_OUTPUT_INVALID", "EMPTY_SEARCH_EXISTING_FILE"),
        )

    def test_edits_apply_sequentially(self):
        files = {"README.md": "alpha\nbeta\n"}
        text = (
            "README.md\n<<<<<<< SEARCH\nalpha\n=======\nbeta\n>>>>>>> REPLACE\n"
            "README.md\n<<<<<<< SEARCH\nbeta\nbeta\n=======\ndone\n>>>>>>> REPLACE\n"
        )
        self.assertEqual(rp.apply_edits(files, rp.parse_edits(text), ["README.md"]), {"README.md": "done\n"})

    def test_whole_line_match_beats_substring_hits(self):
        files = {"README.md": "Version 1\nVersion 10\nVersion 11\n"}
        text = "README.md\n<<<<<<< SEARCH\nVersion 1\n=======\nVersion 2\n>>>>>>> REPLACE"
        self.assertEqual(
            rp.apply_edits(files, rp.parse_edits(text), ["README.md"]),
            {"README.md": "Version 2\nVersion 10\nVersion 11\n"},
        )

    def test_partial_line_search_used_only_when_unique(self):
        files = {"README.md": "Install with pip and run it.\nThen read the docs.\n"}
        text = "README.md\n<<<<<<< SEARCH\nwith pip\n=======\nwith pipx\n>>>>>>> REPLACE"
        self.assertEqual(
            rp.apply_edits(files, rp.parse_edits(text), ["README.md"]),
            {"README.md": "Install with pipx and run it.\nThen read the docs.\n"},
        )
        twice = {"README.md": "the docs\nmore of the docs\n"}
        text = "README.md\n<<<<<<< SEARCH\nthe do\n=======\nx\n>>>>>>> REPLACE"
        with self.assertRaises(rp.Refused) as ctx:
            rp.apply_edits(twice, rp.parse_edits(text), ["README.md"])
        self.assertEqual(str(ctx.exception), "PATCH_OUTPUT_INVALID")
        self.assertEqual(ctx.exception.reason, "SEARCH_NOT_UNIQUE")

    def test_prompt_fence_outlasts_backticks_in_file(self):
        prompt = rp._user_prompt({"README.md": "```sh\nmake\n```\n"}, "t", ["README.md"], None)
        self.assertIn("### README.md\n````\n```sh\nmake\n```\n````", prompt)

    def test_legacy_json_files_reply(self):
        self.assertEqual(
            rp._legacy_json_files('```json\n{"files":{"README.md":"hi\\n"}}\n```'),
            {"README.md": "hi\n"},
        )
        self.assertIsNone(rp._legacy_json_files("README.md\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE"))
        with self.assertRaises(rp.Refused):
            rp._legacy_json_files('{"task": "no files key"}')


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
            user = requests[0]["messages"][1]["content"]
            self.assertIn(plan, user)
            self.assertIn("### README.md", user)
            self.assertIn("### Task\nedit readme", user)
            self.assertEqual(out["patch"]["format"], "json-files")
            self.assertEqual(out["patch"]["edits"], 1)
            self.assertGreaterEqual(requests[0]["max_tokens"], 1024)
            self.assertIsNone(out["patch"]["outputExcerpt"])
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        with self.assertRaises(fleet_local_pilot.Refused):
            rp.chat_patcher("http://example.com/v1/chat/completions", "m", 1)

    def chat_server(self, reply):
        requests = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps({"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]}).encode(
                    "utf-8"
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))
        return f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions", requests

    def add_doc(self, name="docs/guide.md", lines=240):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(f"line {index:04d} of the guide with distinct content\n" for index in range(lines))
        path.write_text(text, encoding="utf-8")
        git(["add", "-A"], self.work)
        git(["commit", "-m", "doc"], self.work)
        git(["push", "origin", "main"], self.work)
        self.commit = git(["rev-parse", "HEAD"], self.work).stdout.strip()
        return name, text

    def test_large_doc_small_edit_stays_under_diff_cap(self):
        name, text = self.add_doc()
        self.assertGreaterEqual(len(text), 10000)
        target = "line 0100 of the guide with distinct content"
        reply = f"{name}\n<<<<<<< SEARCH\n{target}\n=======\nline 0100 of the guide, now corrected\n>>>>>>> REPLACE\n"
        base, _requests = self.chat_server(reply)
        patcher = rp.chat_patcher(base, "m", 5)
        out = rp.run(
            self.spec(allowlist=[name], max_diff_bytes=6144, task="fix the typo on line 100"),
            patcher,
            allow_local_source=True,
            tmp_root=self.tmp_root,
        )
        self.assertEqual(out["outcome"], "COMPLETED")
        self.assertEqual(out["changedFiles"], [name])
        self.assertLess(out["diffBytes"], 6144)
        self.assertIn("now corrected", out["diff"])
        self.assertEqual(out["patch"]["format"], "search-replace")
        self.assertEqual(out["patch"]["edits"], 1)

    def test_request_carries_max_tokens_and_thinking_flag(self):
        reply = "README.md\n<<<<<<< SEARCH\nhello\n=======\nhi\n>>>>>>> REPLACE\n"
        base, requests = self.chat_server(reply)
        for disable in (False, True):
            with self.subTest(disable_thinking=disable):
                patcher = rp.chat_patcher(base, "m", 5, disable_thinking=disable)
                out = rp.run(self.spec(), patcher, allow_local_source=True, tmp_root=self.tmp_root)
                self.assertEqual(out["outcome"], "COMPLETED")
                body = requests[-1]
                self.assertGreaterEqual(body["max_tokens"], 1024)
                self.assertLessEqual(body["max_tokens"], 4096)
                self.assertEqual(body["max_tokens"], out["patch"]["maxTokens"])
                self.assertEqual(body["temperature"], 0)
                if disable:
                    self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
                    self.assertEqual(body["reasoning_format"], "none")
                else:
                    self.assertNotIn("chat_template_kwargs", body)

    def test_malformed_reply_blocks_with_diagnostics(self):
        base, _requests = self.chat_server("README.md\n<<<<<<< SEARCH\nhello\n")
        patcher = rp.chat_patcher(base, "m", 5)
        out = rp.run(self.spec(), patcher, allow_local_source=True, tmp_root=self.tmp_root)
        self.assertEqual(out["outcome"], "BLOCKED")
        self.assertEqual(out["code"], "PATCH_OUTPUT_INVALID")
        self.assertEqual(out["patch"]["error"], "MALFORMED_BLOCK")
        self.assertEqual(out["patch"]["format"], "search-replace")
        self.assertIsNotNone(out["patch"]["outputExcerpt"])
        self.assertLessEqual(len(out["patch"]["outputExcerpt"]), 2048)
        result = rp.execute_job(
            self.spec(),
            patcher=patcher,
            allow_local_source=True,
            tmp_root=self.tmp_root,
            report_store=str(self.store),
            model_endpoint="http://127.0.0.1:9/v1/chat/completions",
            model="m",
        )
        self.assertEqual(result["patch"]["error"], "MALFORMED_BLOCK")
        stored = json.loads(Path(result["reportPath"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["patch"]["error"], "MALFORMED_BLOCK")

    def test_worker_passes_thinking_and_tokens_to_repo_patch(self):
        seen = {}

        def repo_patch(payload, **kw):
            seen.update(kw)
            return {
                "reportStored": True,
                "reportPath": str(self.root / "report.json"),
                "report": {
                    "outcome": "COMPLETED",
                    "reportStored": True,
                    "runId": "00000000-0000-4000-8000-000000000000",
                    "storedReportSha256": "a" * 64,
                },
                "patch": {"format": "search-replace", "edits": 1, "maxTokens": 900},
            }

        receipt = ww.Worker(
            "m5-macbook-pro",
            "fixture-model",
            "http://127.0.0.1:9/v1/chat/completions",
            str(self.root / "receipts-patch"),
            repo_patch=repo_patch,
            disable_thinking=True,
            max_tokens=900,
            allow_local_source=True,
        ).run_job(
            {
                "job_id": "job-patch",
                "route": {"worker": "m5-macbook-pro"},
                "program": "repo-patch",
                "payload": self.spec(),
            }
        )
        self.assertIs(seen["disable_thinking"], True)
        self.assertEqual(seen["max_tokens"], 900)
        self.assertEqual(receipt["maxTokens"], 900)
        self.assertEqual(receipt["patch"]["format"], "search-replace")

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
                "route": {"worker": "m5-macbook-pro"},
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
                    "route": {"worker": "m5-macbook-pro"},
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
        receipt = w.run_job({"job_id": "job-3", "route": {"worker": "m5-macbook-pro"}, "program": "nisi-smoke.mjs"})
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
            w.validate_job({"job_id": "job-1", "route": {"worker": "m5-macbook-pro"}, "program": "repo-patch"})
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
                    "route": {"worker": "m5-macbook-pro"},
                    "program": "repo-patch",
                    "payload": self.spec(repo="http://github.com/a/b"),
                }
            )
        self.assertEqual(str(ctx.exception), "REPO_REFUSED")
