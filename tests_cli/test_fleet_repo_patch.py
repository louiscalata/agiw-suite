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


class _FakeModel:
    """Loopback chat-completions stub: records request bodies, replies from a queue."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                outer.requests.append(json.loads(self.rfile.read(length)))
                content, finish = outer.replies.pop(0)
                body = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": finish}]}).encode(
                    "utf-8"
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class PromptAndDiagnosticsTests(unittest.TestCase):
    """Pure prompt, excerpt and line-ending helpers; no git, no model."""

    def test_output_contract_sentence_sits_before_files_and_after_task(self):
        sentence = "Respond only with SEARCH/REPLACE blocks, no explanation."
        prompt = rp._user_prompt({"README.md": "hello\n"}, "edit readme", ["README.md"], None)
        self.assertIn(sentence, rp._SYSTEM_PROMPT)
        self.assertLess(prompt.index(sentence), prompt.index("### README.md"))
        self.assertTrue(prompt.endswith(sentence))
        self.assertIn("### README.md", prompt)
        self.assertIn("### Task\nedit readme", prompt)
        analysis = rp._user_prompt({"README.md": "hello\n"}, "edit readme", ["README.md"], "plan text")
        self.assertIn("Jev pre-plan (advisory):\nplan text", analysis)
        self.assertTrue(analysis.endswith(sentence))
        self.assertLess(analysis.index("### Task"), analysis.index("plan text"))

    def test_prompt_says_no_tools_and_how_to_insert(self):
        prompts = [rp._SYSTEM_PROMPT, rp._user_prompt({"a.md": "x\n"}, "t", ["a.md"], None)]
        for prompt in prompts:
            lowered = prompt.lower()
            self.assertIn("no tools", lowered)
            self.assertIn("cannot run commands", lowered)
            self.assertIn("current contents of each file are included", lowered)
            self.assertIn("always return at least one search/replace block", lowered)
            self.assertIn("to add new text", lowered)
            # Q003: this sentence let the model refuse an insertion task outright.
            self.assertNotIn("cannot be verified", lowered)
        self.assertIn("Insertion example", rp._SYSTEM_PROMPT)

    def test_select_windows_finds_keyword_hits_deterministically(self):
        lines = [f"filler line {index} about nothing in particular" for index in range(1, 2001)]
        lines[1499] = "Licensed under the MIT licence."
        doc = "\n".join(lines) + "\n"
        first = rp._select_windows(doc, "make the licence wording match LICENSE")
        self.assertEqual(first, rp._select_windows(doc, "make the licence wording match LICENSE"))
        self.assertIn((1, rp._EXCERPT_HEAD), first)
        self.assertTrue(any(start <= 1500 <= end for start, end in first))
        self.assertEqual(first, sorted(first))

    def test_select_windows_without_hits_keeps_head_within_budget(self):
        doc = "\n".join(f"filler line {index} about nothing in particular" for index in range(1, 401)) + "\n"
        windows = rp._select_windows(doc, "make the licence wording match LICENSE")
        self.assertEqual(len(windows), 1)
        start, end = windows[0]
        self.assertEqual(start, 1)
        self.assertGreater(end, rp._EXCERPT_HEAD)
        rendered = "\n".join(doc.splitlines()[start - 1 : end])
        self.assertLessEqual(len(rendered), rp._EXCERPT_BUDGET)

    def test_small_files_are_not_excerpted(self):
        excerpts = {}
        prompt = rp._user_prompt({"README.md": "hello\n"}, "licence", ["README.md"], None, excerpts)
        self.assertEqual(excerpts, {})
        self.assertIn("### README.md\n```\nhello\n```", prompt)

    def test_match_line_endings(self):
        self.assertEqual(rp._match_line_endings("a\r\nb\r\n", "a\nb\n"), "a\r\nb\r\n")
        self.assertEqual(rp._match_line_endings("a\nb\n", "a\nb\n"), "a\nb\n")
        self.assertEqual(rp._match_line_endings("a\r\nb\r\n", "a\r\nb\r\n"), "a\r\nb\r\n")
        self.assertEqual(rp._match_line_endings("a\r\nb\r\n", "a\rb\r"), "a\rb\r")
        self.assertEqual(rp._match_line_endings(None, "a\n"), "a\n")


class RepoPatchDiagnosticsTests(unittest.TestCase):
    """Diff limits, excerpts and the bounded retry, using the RepoPatchTests fixtures."""

    def setUp(self):
        RepoPatchTests.setUp(self)

    def tearDown(self):
        RepoPatchTests.tearDown(self)

    spec = RepoPatchTests.spec

    def commit_file(self, name, text):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        git(["add", "-A"], self.work)
        git(["commit", "-m", f"update {name}"], self.work)
        git(["push", "origin", "main"], self.work)
        self.commit = git(["rev-parse", "HEAD"], self.work).stdout.strip()

    def run_patch(self, patcher, **kw):
        return rp.run(
            self.spec(**kw),
            patcher,
            allow_local_source=True,
            tmp_root=self.tmp_root,
        )

    def test_diff_too_large_records_diff_facts(self):
        out = self.run_patch(
            lambda files, task, workdir: {"README.md": "x\n" * 3000},
            max_diff_bytes=200,
        )
        self.assertEqual(out["outcome"], "BLOCKED")
        self.assertEqual(out["code"], "DIFF_TOO_LARGE")
        patch = out["patch"]
        self.assertEqual(patch["error"], "DIFF_TOO_LARGE")
        self.assertGreater(patch["diffBytes"], 200)
        self.assertEqual(patch["maxDiffBytes"], 200)
        self.assertGreater(patch["changedLines"], 0)
        self.assertGreaterEqual(patch["changedLines"], 3000)
        self.assertTrue(patch["diffExcerpt"])
        self.assertLessEqual(len(patch["diffExcerpt"]), 2048)
        # A stub patcher has no .last, so the diff facts are the whole patch dict.
        self.assertEqual(set(patch), {"error", "diffBytes", "maxDiffBytes", "changedLines", "diffExcerpt"})

    def test_diff_too_many_lines_records_diff_facts(self):
        out = self.run_patch(
            lambda files, task, workdir: {"README.md": "a\nb\nc\n"},
            max_changed_lines=1,
        )
        self.assertEqual(out["outcome"], "BLOCKED")
        self.assertEqual(out["code"], "DIFF_TOO_MANY_LINES")
        patch = out["patch"]
        self.assertEqual(patch["error"], "DIFF_TOO_MANY_LINES")
        self.assertEqual(patch["changedLines"], 4)
        self.assertGreater(patch["diffBytes"], 0)
        self.assertEqual(patch["maxDiffBytes"], 10000)
        self.assertTrue(patch["diffExcerpt"])

    def test_excerpted_file_round_trip(self):
        filler = [f"Filler paragraph {index} about nothing in particular." for index in range(1, 701)]
        filler.append("Licensed under the MIT licence.")
        self.commit_file("README.md", "\n".join(filler) + "\n")
        reply = (
            "README.md\n<<<<<<< SEARCH\nLicensed under the MIT licence.\n=======\n"
            "Licensed under the Apache License, Version 2.0.\n>>>>>>> REPLACE\n"
        )
        model = _FakeModel([(reply, "stop")])
        self.addCleanup(model.close)
        out = self.run_patch(
            rp.chat_patcher(model.url, "m", 5),
            task="make the licence wording match LICENSE",
        )
        self.assertEqual(out["outcome"], "COMPLETED")
        self.assertEqual(out["changedFiles"], ["README.md"])
        self.assertLessEqual(out["diffBytes"], 10000)
        self.assertIn("README.md", out["patch"]["excerpts"])
        self.assertTrue(out["patch"]["excerpts"]["README.md"])
        self.assertEqual(len(model.requests), 1)
        user = model.requests[0]["messages"][1]["content"]
        self.assertIn("(excerpt of 701 lines", user)
        self.assertIn("[lines ", user)
        self.assertNotIn("Filler paragraph 400 about nothing in particular.", user)
        self.assertIn("Filler paragraph 2 about nothing in particular.", user)

    def test_retry_after_length_reply_without_blocks(self):
        prose = "Let me search for files with README in the name..."
        reply = "README.md\n<<<<<<< SEARCH\nhello\n=======\nhi\n>>>>>>> REPLACE\n"
        model = _FakeModel([(prose, "length"), (reply, "stop")])
        self.addCleanup(model.close)
        out = self.run_patch(rp.chat_patcher(model.url, "m", 5))
        self.assertEqual(out["outcome"], "COMPLETED")
        self.assertEqual(len(model.requests), 2)
        patch = out["patch"]
        self.assertEqual(patch["retries"], 1)
        self.assertEqual(
            patch["firstAttempt"],
            {"error": "NO_EDITS", "finishReason": "length", "outputChars": len(prose)},
        )
        self.assertEqual(patch["format"], "search-replace")
        self.assertEqual(patch["edits"], 1)
        self.assertEqual(patch["finishReason"], "stop")
        self.assertIsNone(patch["error"])
        first, second = model.requests
        self.assertEqual(second["max_tokens"], first["max_tokens"])
        self.assertEqual(second["messages"][1], first["messages"][1])
        reminder = second["messages"][-1]
        self.assertEqual(reminder["role"], "user")
        self.assertIn("Allowed paths: README.md.", reminder["content"])
        self.assertIn("Make the best minimal edit", reminder["content"])

    def test_retry_once_when_finish_reason_is_stop(self):
        reply = "README.md\n<<<<<<< SEARCH\nhello\n=======\nhi\n>>>>>>> REPLACE\n"
        model = _FakeModel([("I cannot see any file contents.", "stop"), (reply, "stop")])
        self.addCleanup(model.close)
        out = self.run_patch(rp.chat_patcher(model.url, "m", 5), allowlist=["README.md", "docs/new.md"])
        self.assertEqual(out["outcome"], "COMPLETED")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(out["patch"]["retries"], 1)
        self.assertEqual(out["patch"]["firstAttempt"]["finishReason"], "stop")
        self.assertIn("Allowed paths: README.md, docs/new.md.", model.requests[1]["messages"][-1]["content"])

    def test_no_retry_for_other_errors(self):
        bad = "README.md\n<<<<<<< SEARCH\nnot in the file\n=======\nx\n>>>>>>> REPLACE\n"
        model = _FakeModel([(bad, "stop")])
        self.addCleanup(model.close)
        out = self.run_patch(rp.chat_patcher(model.url, "m", 5))
        self.assertEqual(out["outcome"], "BLOCKED")
        self.assertEqual(out["code"], "PATCH_OUTPUT_INVALID")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(out["patch"]["retries"], 0)
        self.assertEqual(out["patch"]["error"], "SEARCH_NOT_FOUND")
        self.assertNotIn("firstAttempt", out["patch"])

    def test_only_one_retry(self):
        model = _FakeModel([("Let me search for files...", "length"), ("Still searching for files...", "length")])
        self.addCleanup(model.close)
        out = self.run_patch(rp.chat_patcher(model.url, "m", 5))
        self.assertEqual(out["outcome"], "BLOCKED")
        self.assertEqual(out["code"], "PATCH_OUTPUT_INVALID")
        self.assertEqual(len(model.requests), 2)
        patch = out["patch"]
        self.assertEqual(patch["retries"], 1)
        self.assertEqual(patch["error"], "NO_EDITS")
        self.assertEqual(patch["finishReason"], "length")
        self.assertEqual(patch["outputChars"], len("Still searching for files..."))


class LineEndingIOTests(unittest.TestCase):
    """Q003: one small edit to a ~10 KB LF markdown doc must stage a small diff on every OS."""

    def setUp(self):
        RepoPatchTests.setUp(self)

    def tearDown(self):
        RepoPatchTests.tearDown(self)

    spec = RepoPatchTests.spec

    def _commit_doc(self, data):
        (self.work / "docs").mkdir(exist_ok=True)
        (self.work / "docs" / "architecture.md").write_bytes(data)
        git(["-c", "core.autocrlf=false", "add", "-A"], self.work)
        git(["commit", "-m", "doc"], self.work)
        git(["push", "origin", "main"], self.work)
        return git(["rev-parse", "HEAD"], self.work).stdout.strip()

    @staticmethod
    def _doc(newline):
        # Shaped like nisi docs/architecture.md at the Q003 commit: ~10 KB, LF, no BOM,
        # trailing newline, a few non-ASCII characters, no eol attribute.
        lines = ["# Architecture", ""]
        i = 0
        while len(newline.join(lines)) < 9800:
            lines.append(f"Section {i} \u2014 the engine stores a run report and hashes it, paragraph {i}.")
            i += 1
        return (newline.join(lines) + newline).encode("utf-8")

    def _run(self, commit):
        search = "Section 3 \u2014 the engine stores a run report and hashes it, paragraph 3."
        text = f"docs/architecture.md\n<<<<<<< SEARCH\n{search}\n=======\nSection 3 documents reportSha256.\n>>>>>>> REPLACE"

        def patcher(files, task, workdir):
            return rp.apply_edits(files, rp.parse_edits(text), ["docs/architecture.md"])

        return rp.run(
            self.spec(commit=commit, allowlist=["docs/architecture.md"], max_diff_bytes=6144),
            patcher,
            allow_local_source=True,
            tmp_root=self.tmp_root,
        )

    def test_windows_text_mode_write_does_not_rewrite_the_file(self):
        commit = self._commit_doc(self._doc("\n"))

        def windows_write_text(path, data, encoding=None, errors=None, newline=None):
            # What text mode does on Windows: every "\n" becomes os.linesep ("\r\n").
            raw = data.replace("\n", "\r\n").encode(encoding or "utf-8", errors or "strict")
            return path.write_bytes(raw)

        with mock.patch.object(Path, "write_text", windows_write_text):
            out = self._run(commit)
        self.assertEqual(out["outcome"], "COMPLETED", out.get("code"))
        self.assertLess(out["diffBytes"], 1024)
        changed = [line for line in out["diff"].splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")]
        self.assertEqual(len(changed), 2)
        self.assertNotIn("\r", out["diff"])

    def test_crlf_file_keeps_crlf_and_small_diff(self):
        commit = self._commit_doc(self._doc("\r\n"))
        out = self._run(commit)
        self.assertEqual(out["outcome"], "COMPLETED", out.get("code"))
        self.assertLess(out["diffBytes"], 1024)
        self.assertIn("+Section 3 documents reportSha256.\r", out["diff"])

    def test_bom_and_missing_final_newline_survive(self):
        data = b"\xef\xbb\xbf" + self._doc("\n").rstrip(b"\n")
        commit = self._commit_doc(data)
        out = self._run(commit)
        self.assertEqual(out["outcome"], "COMPLETED", out.get("code"))
        changed = [line for line in out["diff"].splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")]
        self.assertEqual(len(changed), 2)
        self.assertNotIn("No newline at end of file", out["diff"])


def _q003_doc(newline="\n"):
    """~10 KB markdown shaped like nisi docs/architecture.md: headings, a code fence, non-ASCII."""
    lines = ["# Architecture", "", "Overview of the engine \u2014 how runs flow.", ""]
    index = 0
    while len(newline.join(lines)) < 9700:
        if index % 15 == 0:
            lines += [f"## Stage {index // 15}", ""]
        elif index == 40:
            lines += ["```js", "const report = run(workflow);", "```"]
        else:
            lines.append(f"Paragraph {index}: the engine writes a run report and the store keeps it.")
        index += 1
    return newline.join(lines) + newline


def _q001_doc():
    """~28 KB README shaped like old-phone-os: 259 lines, some over 1,000 chars, noisy near-misses."""
    lines = ["<!-- SPDX-License-Identifier: GPL-3.0-or-later -->", "# Old Phone Demo", ""]
    filler = "Your account password report is reported publicly in the repository overview; " * 14
    while len(lines) < 254:
        number = len(lines) + 1
        if number % 25 == 0:
            lines.append(f"## Section {number}")
        elif number % 7 == 0:
            lines.append(f"Line {number}: " + filler)
        else:
            lines.append(f"Line {number}: setup notes for the simulator and the battery features.")
    lines += ["## Contributions and license", "", "Issues and pull requests are welcome.", ""]
    lines.append("GPL-3.0-or-later; [LICENSE](LICENSE) is retained verbatim.")
    return "\n".join(lines) + "\n"


Q001_TASK = (
    "Docs-only consistency pass on README.md: correct any stale commit count and make the licence wording "
    "match LICENSE exactly. Same finding class as audit draft 04; check it against the public repo first."
)
Q003_TASK = (
    "Docs-only: in docs/ (README or architecture), document that reportSha256 = "
    "sha256('nisi/run-report/v1\\0' + stableStringify(report)), not a hash of the stored file's bytes."
)


class PromptContentTests(unittest.TestCase):
    """Q003/Q001 reruns: the file text must reach the user message, and excerpts must hit the target."""

    def test_q003_doc_is_in_the_user_message_and_counted(self):
        doc = _q003_doc()
        self.assertGreater(len(doc), 9500)
        model = _FakeModel([("no blocks", "stop"), ("still none", "stop")])
        self.addCleanup(model.close)
        patcher = rp.chat_patcher(model.url, "m", 5, disable_thinking=True)
        with self.assertRaises(rp.Refused):
            patcher(
                files={"docs/architecture.md": doc}, task=Q003_TASK, workdir=None, allowlist=["docs/architecture.md"]
            )
        system, user = (message["content"] for message in model.requests[0]["messages"])
        self.assertIn(doc, user)
        self.assertNotIn("b'", user[:200])
        self.assertEqual(patcher.last["fileChars"], {"docs/architecture.md": len(doc)})
        self.assertEqual(patcher.last["promptChars"], len(system) + len(user))
        self.assertNotIn("excerpts", patcher.last)
        self.assertEqual(patcher.last["retries"], 1)

    def test_q003_crlf_doc_through_run_reaches_prompt_under_windows_text_mode(self):
        fixture = RepoPatchTests("setUp")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        doc = _q003_doc("\r\n")
        (fixture.work / "docs").mkdir()
        (fixture.work / "docs" / "architecture.md").write_bytes(doc.encode("utf-8"))
        git(["-c", "core.autocrlf=false", "add", "-A"], fixture.work)
        git(["commit", "-m", "doc"], fixture.work)
        git(["push", "origin", "main"], fixture.work)
        commit = git(["rev-parse", "HEAD"], fixture.work).stdout.strip()
        reply = "docs/architecture.md\n<<<<<<< SEARCH\n## Stage 1\n=======\n## Stage 1\n\nreportSha256 note.\n>>>>>>> REPLACE"
        model = _FakeModel([(reply, "stop")])
        self.addCleanup(model.close)

        def windows_write_text(path, data, encoding=None, errors=None, newline=None):
            return path.write_bytes(data.replace("\n", "\r\n").encode(encoding or "utf-8", errors or "strict"))

        with mock.patch.object(Path, "write_text", windows_write_text):
            out = rp.run(
                fixture.spec(commit=commit, allowlist=["docs/architecture.md"], max_diff_bytes=6144),
                rp.chat_patcher(model.url, "m", 5),
                allow_local_source=True,
                tmp_root=fixture.tmp_root,
            )
        user = model.requests[0]["messages"][1]["content"]
        self.assertIn(doc, user)
        self.assertEqual(out["outcome"], "COMPLETED", out.get("code"))
        self.assertLess(out["diffBytes"], 1024)
        self.assertEqual(out["patch"]["fileChars"], {"docs/architecture.md": len(doc)})

    def test_q001_excerpts_cover_licence_heading_and_sentence(self):
        doc = _q001_doc()
        self.assertGreater(len(doc), rp._EXCERPT_THRESHOLD)
        lines = doc.splitlines()
        heading = lines.index("## Contributions and license") + 1
        sentence = len(lines)
        windows = rp._select_windows(doc, Q001_TASK)
        self.assertEqual(windows, rp._select_windows(doc, Q001_TASK))
        self.assertTrue(any(start <= heading <= end for start, end in windows), windows)
        self.assertTrue(any(start <= sentence <= end for start, end in windows), windows)
        excerpts, file_chars = {}, {}
        prompt = rp._user_prompt({"README.md": doc}, Q001_TASK, ["README.md"], None, excerpts, file_chars=file_chars)
        self.assertIn("GPL-3.0-or-later; [LICENSE](LICENSE) is retained verbatim.", prompt)
        self.assertEqual(excerpts["README.md"], [list(window) for window in windows])
        self.assertGreater(file_chars["README.md"], 0)
        self.assertLessEqual(file_chars["README.md"], rp._EXCERPT_BUDGET)

    def test_keywords_strip_punctuation_and_match_whole_words(self):
        keywords = rp._task_keywords(Q001_TASK)
        for expected in ("licence", "license", "commit", "count", "readme.md", "readme"):
            self.assertIn(expected, keywords)
        for dropped in ("exactly.", "first.", "the", "pass", "check"):
            self.assertNotIn(dropped, keywords)
        self.assertEqual(rp._keyword_scores(["your account details", "commit count: 12"], "commit count"), {2: 2.0})
        self.assertIn("architecture", rp._task_keywords("edit docs/architecture.md"))

    def test_rare_keywords_outrank_common_ones(self):
        lines = ["the public repo"] * 30 + ["## License", "MIT licence text"]
        scores = rp._keyword_scores(lines, "public repo licence")
        self.assertGreater(scores[32], scores[1])

    def test_no_match_falls_back_to_head_within_budget(self):
        doc = "\n".join(f"filler line {index} about nothing in particular" for index in range(1, 2001)) + "\n"
        windows = rp._select_windows(doc, "zzz-unmatched-token")
        self.assertEqual(len(windows), 1)
        start, end = windows[0]
        self.assertEqual(start, 1)
        self.assertLessEqual(len("\n".join(doc.splitlines()[:end])), rp._EXCERPT_BUDGET)
        self.assertGreater(end, 200)

    def test_matching_heading_brings_its_section(self):
        lines = ["# Title"] + [f"filler {index}" for index in range(500)] + ["## Licence"]
        lines += [f"term {index}" for index in range(12)] + ["## Next"] + [f"tail {index}" for index in range(500)]
        doc = "\n".join(lines) + "\n"
        heading = lines.index("## Licence") + 1
        windows = rp._select_windows(doc + "x" * rp._EXCERPT_THRESHOLD, "licence")
        self.assertTrue(any(start <= heading and heading + 12 <= end for start, end in windows), windows)
