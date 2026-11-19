"""Fleet jev-task adapter tests. The CLI is a generated fake Python script; ~/bin/jev-task is never run."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agiw import fleet_jev_task as jev
from agiw import fleet_repo_patch as rp
from agiw import windows_worker as ww

FAKE_TEMPLATE = """\
import json
import os
import sys
import time

MODE = "__MODE__"
HERE = os.path.dirname(os.path.abspath(__file__))


def log(record):
    with open(os.path.join(HERE, "calls.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\\n")


def main():
    argv = sys.argv[1:]
    phase = argv[0] if argv else ""
    text_path = argv[argv.index("--text") + 1] if "--text" in argv else ""
    text_mode = oct(os.stat(text_path).st_mode & 0o777) if text_path else ""
    log({"argv": argv, "env": sorted(os.environ), "textMode": text_mode})
    if MODE == "timeout":
        time.sleep(10)
        return 0
    if MODE == "unavailable_json":
        print(json.dumps({"status": "unavailable", "stage": "pre", "errorCode": "JEV_DRIVER_UNAVAILABLE"}))
        return 2
    if MODE == "unavailable_stderr":
        sys.stderr.write("[unavailable %s abc123: JEV_LEDGER_BUSY at %s; see ledger]\\n" % (phase, phase))
        return 2
    if MODE == "malformed_json":
        print('{"status": "ok", "plan":')
        return 0
    if MODE == "crash":
        return 1
    if MODE == "empty":
        return 0
    if MODE == "ok" and phase == "pre":
        print("# plan")
        print("1. inspect the allowlisted files")
        print("[pre abc123: 2/3 flagged, 0.1 s, ledger ledger.jsonl]")
        return 0
    if MODE == "ok":
        print(json.dumps({"status": "ok", "verdict": "READY"}))
        print("[post abc123: 1/3 flagged, 0.1 s, ledger ledger.jsonl]")
        return 0
    if MODE == "postfile" and phase == "post":
        with open(text_path + ".jevpost.json", "w", encoding="utf-8") as handle:
            json.dump({"statedVerdict": "READY"}, handle)
        print("post brief")
        print("[post abc123: 0/3 flagged, 0.1 s, ledger ledger.jsonl]")
        return 0
    if MODE == "postfile_json" and phase == "post":
        with open(text_path + ".jevpost.json", "w", encoding="utf-8") as handle:
            json.dump({"statedVerdict": "NOT_READY"}, handle)
        print(json.dumps({"status": "ok", "postbrief": "notes"}))
        return 0
    if MODE == "flagged" and phase == "post":
        print("post brief")
        print("[post abc123: 2/3 flagged, 0.1 s, ledger ledger.jsonl]")
        return 0
    if MODE == "clean" and phase == "post":
        print("post brief")
        print("[post abc123: 0/3 flagged, 0.1 s, ledger ledger.jsonl]")
        return 0
    if MODE == "postfile_suffix" and phase == "post":
        path = text_path[:-4] + ".jevpost.json" if text_path.endswith(".txt") else text_path + ".jevpost.json"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"statedVerdict": "READY"}, handle)
        print("post brief")
        print("[post abc123: 2/3 flagged, 0.1 s, ledger ledger.jsonl]")
        return 0
    if MODE == "postfile_none" and phase == "post":
        path = text_path[:-4] + ".jevpost.json" if text_path.endswith(".txt") else text_path + ".jevpost.json"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"statedVerdict": "none"}, handle)
        print("post brief")
        print("[post abc123: 2/3 flagged, 0.1 s, ledger ledger.jsonl]")
        return 0
    if MODE == "postfile_none_clean" and phase == "post":
        path = text_path[:-4] + ".jevpost.json" if text_path.endswith(".txt") else text_path + ".jevpost.json"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"statedVerdict": "none"}, handle)
        print("post brief")
        print("[post abc123: 0/3 flagged, 0.1 s, ledger ledger.jsonl]")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def write_fake(root, mode):
    directory = Path(tempfile.mkdtemp(dir=str(root), prefix="fake-" + mode + "-"))
    script = directory / "fake_jev_task.py"
    script.write_text(FAKE_TEMPLATE.replace("__MODE__", mode), encoding="utf-8")
    return script


def read_calls(script):
    path = script.parent / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def git(args, cwd):
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
        check=True,
        text=True,
        env=env,
    )


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_disabled_values(self):
        for value in (None, "", "off", "OFF", "0", "false", "False"):
            with self.subTest(value=value):
                with mock.patch.object(jev.shutil, "which") as which:
                    self.assertIsNone(jev.resolve_jev_task_bin(value))
                    which.assert_not_called()

    def test_env_value_is_used_and_can_be_disabled(self):
        self.assertIsNone(jev.resolve_jev_task_bin(env={"AGIW_JEV_TASK": "off"}))
        with mock.patch.object(jev.shutil, "which", return_value="/fake/jev-task") as which:
            self.assertEqual(jev.resolve_jev_task_bin(env={"AGIW_JEV_TASK": "auto"}), "/fake/jev-task")
            which.assert_called_once_with("jev-task")
        self.assertIsNone(jev.resolve_jev_task_bin(env={}))

    def test_auto_uses_which_or_is_missing(self):
        with mock.patch.object(jev.shutil, "which", return_value=None) as which:
            self.assertIsNone(jev.resolve_jev_task_bin("auto"))
            which.assert_called_once_with("jev-task")
        with mock.patch.object(jev.shutil, "which", return_value="/opt/jev-task"):
            self.assertEqual(jev.resolve_jev_task_bin("auto"), "/opt/jev-task")

    def test_explicit_path_never_calls_which(self):
        real = self.root / "jev-task"
        real.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        with mock.patch.object(jev.shutil, "which", side_effect=AssertionError("which must not run")):
            self.assertEqual(jev.resolve_jev_task_bin(str(real)), str(real))
        self.assertIsNone(jev.resolve_jev_task_bin(str(self.root / "missing")))


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workdirs = []
        real_workdir = jev._workdir

        def tracked():
            path = real_workdir()
            self.workdirs.append(path)
            return path

        patcher = mock.patch.object(jev, "_workdir", side_effect=tracked)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for path in self.workdirs:
            self.assertFalse(os.path.exists(path), "temp workdir was not removed: " + path)

    def test_ok_pre_and_post(self):
        script = write_fake(self.root, "ok")
        pre, post = jev.make_analyzers(str(script), timeout=5)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "AGIW_JEV_TASK_TIMEOUT": "5"}):
            pre_result = pre("task\n\n--- README.md ---\nhello\n")
            post_result = post("+hello\n")
        self.assertEqual(pre_result["status"], "ok")
        self.assertIn("# plan", pre_result["plan"])
        self.assertIn("1. inspect the allowlisted files", pre_result["plan"])
        self.assertNotIn("[pre", pre_result["plan"])
        self.assertEqual(post_result, {"status": "ok", "verdict": "READY"})

        calls = read_calls(script)
        self.assertEqual(len(calls), 2)
        pre_call, post_call = calls
        self.assertEqual(pre_call["argv"][0], "pre")
        self.assertIn("--driver", pre_call["argv"])
        self.assertEqual(pre_call["argv"][pre_call["argv"].index("--driver") + 1], "never")
        self.assertEqual(pre_call["argv"][pre_call["argv"].index("--kind") + 1], "draft")
        self.assertNotIn("--pre-id", pre_call["argv"])
        self.assertEqual(post_call["argv"][0], "post")
        self.assertEqual(post_call["argv"][post_call["argv"].index("--kind") + 1], "review")
        pre_id = pre_call["argv"][pre_call["argv"].index("--id") + 1]
        post_id = post_call["argv"][post_call["argv"].index("--id") + 1]
        self.assertEqual(post_call["argv"][post_call["argv"].index("--pre-id") + 1], pre_id)
        self.assertNotEqual(pre_id, post_id)
        self.assertNotIn("OPENAI_API_KEY", pre_call["env"])
        self.assertNotIn("AGIW_JEV_TASK_TIMEOUT", pre_call["env"])
        if os.name == "posix":
            self.assertEqual(pre_call["textMode"], "0o600")

    def test_child_env_keeps_windows_systemroot(self):
        isolated = r"C:\tmp\agiw-jev-job"
        env = jev._child_env(workdir=isolated, name="nt", environ={"PATH": r"C:\Windows\System32"})
        self.assertEqual(env["SYSTEMROOT"], r"C:\Windows")
        self.assertEqual(env["WINDIR"], r"C:\Windows")
        self.assertEqual(env["TEMP"], isolated)
        self.assertEqual(env["TMP"], isolated)
        self.assertEqual(env["USERPROFILE"], isolated)
        self.assertEqual(env["PATH"], r"C:\Windows\System32")
        self.assertNotIn("COMSPEC", env)
        self.assertNotIn("OPENAI_API_KEY", env)

        env = jev._child_env(
            workdir=isolated,
            name="nt",
            environ={
                "PATH": r"D:\bin",
                "SYSTEMROOT": r"D:\Windows",
                "WINDIR": r"D:\Win",
                "COMSPEC": r"D:\Windows\System32\cmd.exe",
                "PATHEXT": ".EXE",
                "OPENAI_API_KEY": "sk-test",
            },
        )
        self.assertEqual(env["SYSTEMROOT"], r"D:\Windows")
        self.assertEqual(env["WINDIR"], r"D:\Win")
        self.assertEqual(env["COMSPEC"], r"D:\Windows\System32\cmd.exe")
        self.assertEqual(env["PATHEXT"], ".EXE")
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_unavailable_json_preserves_stage_and_code(self):
        pre, _post = jev.make_analyzers(str(write_fake(self.root, "unavailable_json")), timeout=5)
        self.assertEqual(
            pre("x"),
            {"status": "unavailable", "stage": "pre", "errorCode": "JEV_DRIVER_UNAVAILABLE"},
        )

    def test_unavailable_stderr_shape_is_parsed(self):
        _pre, post = jev.make_analyzers(str(write_fake(self.root, "unavailable_stderr")), timeout=5)
        self.assertEqual(
            post("x"),
            {"status": "unavailable", "stage": "post", "errorCode": "JEV_LEDGER_BUSY"},
        )

    def test_timeout_is_reported_and_temp_dir_removed(self):
        pre, _post = jev.make_analyzers(str(write_fake(self.root, "timeout")), timeout=0.2)
        result = pre("x")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["errorCode"], "JEV_TIMEOUT")
        self.assertEqual(result["stage"], "pre")
        self.assertEqual(len(self.workdirs), 1)

    def test_invalid_outputs(self):
        pre_malformed, _ = jev.make_analyzers(str(write_fake(self.root, "malformed_json")), timeout=5)
        self.assertEqual(pre_malformed("x")["errorCode"], "JEV_INVALID_OUTPUT")
        pre_empty, _ = jev.make_analyzers(str(write_fake(self.root, "empty")), timeout=5)
        self.assertEqual(pre_empty("x")["errorCode"], "JEV_INVALID_OUTPUT")
        _pre_crash, post_crash = jev.make_analyzers(str(write_fake(self.root, "crash")), timeout=5)
        self.assertEqual(post_crash("x")["errorCode"], "JEV_INVALID_OUTPUT")

    def test_verdict_fallbacks(self):
        for mode, expected in (
            ("flagged", "FLAGGED"),
            ("clean", "OK"),
            ("postfile", "READY"),
            ("postfile_json", "NOT_READY"),
            ("postfile_suffix", "READY"),
            ("postfile_none", "FLAGGED"),
            ("postfile_none_clean", "OK"),
        ):
            with self.subTest(mode=mode):
                _pre, post = jev.make_analyzers(str(write_fake(self.root, mode)), timeout=5)
                self.assertEqual(post("+x\n"), {"status": "ok", "verdict": expected})

    def test_missing_binary_is_no_analyzer(self):
        missing = str(self.root / "missing-jev-task")
        for bin_path in ("", None, missing):
            with self.subTest(bin_path=bin_path):
                pre, post = jev.make_analyzers(bin_path)
                for analyzer in (pre, post):
                    self.assertEqual(
                        analyzer("x"),
                        {"status": "unavailable", "stage": "no_analyzer", "errorCode": "JEV_UNAVAILABLE"},
                    )


@unittest.skipUnless(shutil.which("git"), "git not available")
class WorkerWiringTests(unittest.TestCase):
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
        git(["add", "-A"], self.work)
        git(["commit", "-m", "init"], self.work)
        git(["push", "-u", "origin", "main"], self.work)
        self.commit = git(["rev-parse", "HEAD"], self.work).stdout.strip()
        self.repo = str(self.bare)
        self.store = self.root / "reports"
        self.store.mkdir()

    def tearDown(self):
        self.assertEqual(os.listdir(self.tmp_root), [])

    def payload(self):
        return {
            "repo": self.repo,
            "commit": self.commit,
            "allowlist": ["README.md"],
            "task": "edit readme",
            "max_diff_bytes": 10000,
            "timeout_seconds": 5,
        }

    def repo_patch(self, captured):
        def invoke(payload, **kw):
            captured.update(kw)
            return rp.execute_job(
                payload,
                patcher=lambda files, task, workdir: {"README.md": files["README.md"] + "added\n"},
                allow_local_source=True,
                tmp_root=str(self.tmp_root),
                model_endpoint=kw["model_endpoint"],
                model=kw["model"],
                report_store=kw["report_store"],
                jev_pre=kw.get("jev_pre"),
                jev_post=kw.get("jev_post"),
            )

        return invoke

    def job(self):
        return {
            "job_id": "job-jev",
            "route": {"worker": "m5-macbook-pro", "preanalysis": "local", "reason": "strict-local"},
            "program": "repo-patch",
            "payload": self.payload(),
        }

    def make_worker(self, repo_patch):
        return ww.Worker(
            "m5-macbook-pro",
            "fixture-model",
            "http://127.0.0.1:9/v1/chat/completions",
            str(self.root / "receipts"),
            repo_patch=repo_patch,
            allow_local_source=True,
        )

    def test_worker_with_jev_task_records_ok_analysis(self):
        script = write_fake(self.root, "ok")
        captured = {}
        worker = ww.Worker(
            "m5-macbook-pro",
            "fixture-model",
            "http://127.0.0.1:9/v1/chat/completions",
            str(self.root / "receipts"),
            repo_patch=self.repo_patch(captured),
            allow_local_source=True,
            jev_task=str(script),
            jev_task_timeout=5,
        )
        receipt = worker.run_job(self.job())
        self.assertEqual(receipt["outcome"], "COMPLETED")
        self.assertIn("jev_pre", captured)
        self.assertIn("jev_post", captured)
        stored = json.loads(Path(receipt["reportPath"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["jevPre"]["status"], "ok")
        self.assertIn("# plan", stored["jevPre"]["plan"])
        self.assertEqual(stored["jevPost"]["status"], "ok")
        self.assertEqual(stored["jevPost"]["verdict"], "READY")
        self.assertEqual(len(read_calls(script)), 2)

    def test_default_worker_stays_offline(self):
        script = write_fake(self.root, "ok")
        captured = {}
        with mock.patch.object(ww.fleet_jev_task, "resolve_jev_task_bin") as resolve:
            worker = self.make_worker(self.repo_patch(captured))
            receipt = worker.run_job(self.job())
        resolve.assert_not_called()
        self.assertNotIn("jev_pre", captured)
        self.assertNotIn("jev_post", captured)
        self.assertFalse((script.parent / "calls.jsonl").exists())
        stored = json.loads(Path(receipt["reportPath"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["jevPre"]["stage"], "no_analyzer")
        self.assertEqual(stored["jevPost"]["stage"], "no_analyzer")


if __name__ == "__main__":
    unittest.main()
