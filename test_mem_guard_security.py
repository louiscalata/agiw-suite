"""Memory-guard boundary regressions with virtual processes and in-memory state."""

import contextlib
import copy
import io
import json
import shlex
import signal
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mem_guard

ROOT_PID = 20000
CHILD_PID = 20001
OWNER_PID = 30000
UID = 501
START = "Mon Nov 16 00:00:00 2026"


def memory_state(pressure=4, available=5):
    return {
        "pressure": pressure,
        "availablePercent": available,
        "ramBytes": 64 * mem_guard.GiB,
        "swapUsedBytes": 0,
        "swapTotalBytes": 20 * mem_guard.GiB,
        "compressedBytes": 0,
        "wiredBytes": 0,
        "vmFreeBytes": 32 * mem_guard.GiB,
        "gpuAllocBytes": None,
    }


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def hook_context(payload, *, probes=None):
    probes = probes or SimpleNamespace(
        read=mock.Mock(return_value=memory_state()), consumers=mock.Mock(return_value=[])
    )
    return SimpleNamespace(
        stdin=io.BytesIO(json.dumps(payload).encode()),
        stderr=io.StringIO(),
        cfg=mem_guard.default_config(),
        probes=probes,
        gpu=mock.Mock(return_value=None),
        level_file=SimpleNamespace(write=mock.Mock()),
        journal=SimpleNamespace(write=mock.Mock(return_value=True)),
        registry=SimpleNamespace(resume_overdue=mock.Mock(return_value=[])),
    )


class RegistryFixture:
    """Only its temporary root is real; records, process facts and signals are injected."""

    def __init__(self, root):
        self.registry = mem_guard.Registry(
            root,
            uid=UID,
            pid=OWNER_PID,
            runner=mock.Mock(side_effect=AssertionError("unexpected process command")),
            kill=mock.Mock(side_effect=AssertionError("unexpected signal")),
            clock=lambda: 1000,
        )
        self.entry = {"pid": ROOT_PID, "startTime": START, "comm": "job", "label": "fixture job"}
        self.tree = {ROOT_PID: (1, UID), CHILD_PID: (ROOT_PID, UID), OWNER_PID: (1, UID)}
        self.facts = {
            ROOT_PID: {"uid": UID, "ppid": 1, "startTime": START, "comm": "job"},
            CHILD_PID: {"uid": UID, "ppid": ROOT_PID, "startTime": START, "comm": "child"},
            OWNER_PID: {"uid": UID, "ppid": 1, "startTime": START, "comm": "observer"},
        }
        self.records = []
        self.signals = []
        self.order = []
        self.registry.list_pausable = mock.Mock(return_value=[self.entry])
        self.registry.tree = mock.Mock(return_value=self.tree)
        self.registry.identities = mock.Mock(side_effect=self.identities)
        self.registry._has_entry = mock.Mock(return_value=True)
        self.registry._read_paused = mock.Mock(side_effect=lambda: copy.deepcopy(self.records))
        self.registry._write_paused = mock.Mock(side_effect=self.write)
        self.registry._signal = mock.Mock(side_effect=self.send)
        self.registry._lock = mock.Mock(side_effect=lambda timeout=2: contextlib.nullcontext())

    def identities(self, pids):
        self.order.append(("identity", tuple(pids)))
        return {pid: copy.deepcopy(self.facts[pid]) for pid in pids if pid in self.facts}

    def write(self, jobs):
        self.order.append(("write",))
        self.records = copy.deepcopy(jobs)

    def send(self, pid, sig):
        self.order.append(("signal", pid))
        self.signals.append((pid, sig))
        return "sent"


class MemGuardSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = RegistryFixture(Path(self.temp.name))

    def test_shutdown_fences_pause_after_disk_wait_and_lock_timeout(self):
        fixture = self.fixture
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def write(jobs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("disk barrier not released")
            fixture.write(jobs)

        def locked(timeout=2):
            if threading.current_thread() is threading.main_thread():
                raise mem_guard.RegistryError("fixture lock timeout")
            return contextlib.nullcontext()

        def pause():
            try:
                fixture.registry.pause_all()
            except (RuntimeError, mem_guard.RegistryError, AssertionError) as exc:
                errors.append(exc)

        fixture.registry._write_paused.side_effect = write
        fixture.registry._lock.side_effect = locked
        worker = threading.Thread(target=pause)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        guard = mem_guard.Guard(
            cfg=mem_guard.default_config(), probes=mock.Mock(), registry=fixture.registry, watchdog=False
        )
        guard.close(0)
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(fixture.signals, [], "shutdown must fence a later signal despite the file-lock timeout")

    def test_shutdown_waits_for_signal_callback_already_started(self):
        fixture = self.fixture
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        def send(pid, sig):
            entered.set()
            if not release.wait(2):
                raise AssertionError("signal barrier not released")
            return fixture.send(pid, sig)

        fixture.registry._signal.side_effect = send
        pause = threading.Thread(target=fixture.registry.pause_all)
        pause.start()
        self.addCleanup(pause.join, 2)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        close = threading.Thread(target=lambda: (fixture.registry.stop_pausing(0), closed.set()))
        close.start()
        self.addCleanup(close.join, 2)
        self.assertFalse(closed.wait(0.02), "shutdown must wait until a started signal finishes")
        release.set()
        pause.join(2)
        close.join(2)
        self.assertFalse(pause.is_alive())
        self.assertFalse(close.is_alive())
        self.assertTrue(closed.is_set())
        signals = list(fixture.signals)
        self.assertEqual(fixture.registry.pause_all(), [])
        self.assertEqual(fixture.signals, signals)

    def test_identity_is_refreshed_after_write_before_every_signal(self):
        fixture = self.fixture
        jobs = fixture.registry.pause_all()
        self.assertEqual(fixture.signals, [(ROOT_PID, signal.SIGSTOP), (CHILD_PID, signal.SIGSTOP)])
        self.assertEqual(len(jobs), 1)
        self.assertIn(("write",), fixture.order)
        write_at = next(i for i, row in enumerate(fixture.order) if row == ("write",))
        for pid in (ROOT_PID, CHILD_PID):
            signal_at = fixture.order.index(("signal", pid))
            self.assertGreater(signal_at, write_at)
            self.assertEqual(fixture.order[signal_at - 1], ("identity", (pid,)))

    def test_replaced_root_identity_is_never_signalled(self):
        for field, value in (("startTime", "Tue Nov 17 00:00:00 2026"), ("uid", UID + 1), ("comm", "replacement")):
            with self.subTest(field=field):
                fixture = RegistryFixture(Path(self.temp.name))

                def write(jobs, fixture=fixture, field=field, value=value):
                    fixture.write(jobs)
                    fixture.facts[ROOT_PID][field] = value
                    fixture.facts.pop(CHILD_PID, None)

                fixture.registry._write_paused.side_effect = write
                self.assertEqual(fixture.registry.pause_all(), [])
                self.assertEqual(fixture.signals, [])
                self.assertEqual(fixture.records, [])

    def test_descendant_parent_is_rechecked_after_previous_signal(self):
        fixture = self.fixture

        def send(pid, sig):
            if pid == ROOT_PID:
                fixture.facts[CHILD_PID]["ppid"] = 40000
            return fixture.send(pid, sig)

        fixture.registry._signal.side_effect = send
        jobs = fixture.registry.pause_all()
        self.assertEqual(fixture.signals, [(ROOT_PID, signal.SIGSTOP)])
        self.assertEqual([member["pid"] for member in jobs[0]["members"]], [ROOT_PID])
        self.assertEqual(fixture.records, jobs)

    def test_unavailable_fresh_identity_is_never_signalled_or_recorded_as_paused(self):
        fixture = self.fixture
        initial = fixture.identities([OWNER_PID, ROOT_PID, CHILD_PID])
        fixture.registry.identities.side_effect = [initial, None, {}]
        self.assertEqual(fixture.registry.pause_all(), [])
        self.assertEqual(fixture.signals, [])
        self.assertEqual(fixture.records, [])

    def test_signal_failure_keeps_recovery_intent_but_proven_exit_is_pruned(self):
        fixture = self.fixture
        fixture.registry._signal.side_effect = lambda pid, sig: "failed"
        jobs = fixture.registry.pause_all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual([member["pid"] for member in jobs[0]["members"]], [ROOT_PID, CHILD_PID])
        self.assertEqual(fixture.records, jobs)
        fixture = RegistryFixture(Path(self.temp.name))
        fixture.registry._signal.side_effect = lambda pid, sig: "gone"
        self.assertEqual(fixture.registry.pause_all(), [])
        self.assertEqual(fixture.records, [])

    def test_shutdown_during_fresh_identity_probe_prevents_signal(self):
        fixture = self.fixture

        def identities(pids):
            if len(pids) == 1:
                fixture.registry._closing.set()
            return fixture.identities(pids)

        fixture.registry.identities.side_effect = identities
        self.assertEqual(fixture.registry.pause_all(), [])
        self.assertEqual(fixture.signals, [])

    def test_established_refusal_survives_consumer_and_suggestion_exceptions(self):
        probes = SimpleNamespace(
            read=mock.Mock(return_value=memory_state()), consumers=mock.Mock(side_effect=RuntimeError)
        )
        decision = mem_guard.check(mem_guard.GiB, probes=probes, cfg=mem_guard.default_config())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, "critical")
        probes.consumers.side_effect = None
        probes.consumers.return_value = []
        with mock.patch.object(mem_guard, "suggestions", side_effect=RuntimeError):
            decision = mem_guard.check(mem_guard.GiB, probes=probes, cfg=mem_guard.default_config())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, "critical")
        probes.consumers.side_effect = RuntimeError
        try:
            found = mem_guard.hook_decide(bash("codex exec task"), probes, mem_guard.default_config())
        except RuntimeError as exc:
            self.fail(f"optional diagnostics erased the refusal: {type(exc).__name__}")
        self.assertIsNotNone(found)
        self.assertFalse(found[0].allowed)
        self.assertEqual(found[2], [])

    def test_compounds_choose_heaviest_later_command_including_nested_shell(self):
        for command in (
            "codex exec task && lms load model",
            "xcodebuild build; ollama run model",
            "docker run image | lms server start",
            "sh -c 'codex exec task; lms load model'",
            "codex exec task; sh -c 'xcodebuild; ollama pull model'",
        ):
            with self.subTest(command=command):
                found = mem_guard.hook_match(bash(command))
                self.assertIsNotNone(found)
                self.assertEqual(found[1], "model")

    def test_docker_global_and_compose_option_values_do_not_hide_heavy_commands(self):
        for command in (
            "docker --context fixture run image",
            "docker --host unix:///fixture.sock run image",
            "docker -H unix:///fixture.sock --config fixture run image",
            "docker --context=fixture run image",
            "docker -Hfixture run image",
            "docker --config fixture --debug compose --project-name fixture up",
            "docker compose --file fixture.yml --env-file fixture.env up",
            "docker-compose --project-directory fixture --file fixture.yml up",
            "docker " + "--context fixture " * 16 + "run image",
        ):
            with self.subTest(command=command):
                try:
                    found = mem_guard.hook_match(bash(command))
                except ValueError as exc:
                    self.fail(f"Docker global options hid the command instead of classifying it: {exc}")
                self.assertIsNotNone(found)
                self.assertEqual(found[1], "docker")

    def test_docker_container_compose_run_and_start_forms_are_guarded(self):
        cases = {
            "docker container run image": "docker run",
            "docker --context fixture container run --rm image": "docker run",
            "docker start fixture": "docker start",
            "docker container start -a fixture": "docker start",
            "docker compose run --rm service": "docker compose run",
            "docker compose -f fixture.yml run -e A=1 service cmd": "docker compose run",
            "docker-compose run service": "docker-compose run",
            "docker compose start": "docker compose start",
            "docker-compose restart -t 5 service": "docker-compose restart",
        }
        for command, label in cases.items():
            with self.subTest(command=command):
                self.assertEqual(mem_guard.hook_match(bash(command)), (label, "docker"))
        for command in (
            "docker container ls",
            "docker container --help",
            "docker container run --help",
            "docker start fixture --help",
            "docker compose run --help",
            "docker-compose restart --help",
            "docker compose ps",
            "docker create image",
        ):
            with self.subTest(command=command):
                self.assertIsNone(mem_guard.hook_match(bash(command)))

    def test_eval_and_xargs_operands_are_classified_as_nested_commands(self):
        for command in (
            'eval "docker run image"',
            "eval docker run image",
            "eval -- 'docker compose up'",
            "printf x | xargs docker run image",
            "xargs -n 1 docker run",
            "xargs --max-procs 4 docker run",
            "xargs -a list.txt -d , docker container run",
            "xargs -tn1 docker run",
            "xargs -I{} docker run {}",
            "xargs sh -c 'docker run image'",
            "eval \"xargs -0 docker compose run svc\"",
        ):
            with self.subTest(command=command):
                found = mem_guard.hook_match(bash(command))
                self.assertEqual(found and found[1], "docker")
        for command in ("eval", "eval 'echo docker run'", "xargs", "xargs -n 1 echo", "xargs docker ps"):
            with self.subTest(command=command):
                self.assertIsNone(mem_guard.hook_match(bash(command)))
        command = "docker run image"
        for _ in range(4):
            command = "eval " + shlex.quote(command)
        ctx = hook_context(bash(command))
        ctx.probes.read.return_value = memory_state(1, 90)
        self.assertEqual(mem_guard._cmd_hook(ctx), 2)
        ctx = hook_context(bash("xargs xargs xargs xargs docker run image"))
        ctx.probes.read.return_value = memory_state(1, 90)
        self.assertEqual(mem_guard._cmd_hook(ctx), 2)

    def test_unparseable_commands_fail_closed_only_when_they_look_guarded(self):
        """Decision: a command the hook cannot parse (shlex error, token/size/nesting limit,
        unknown Docker option) is refused with exit 2 only when its raw text names Docker or another
        guarded executable (case, quotes and backslashes ignored). Clearly unrelated commands such
        as git or python are allowed, because a parser limitation must not block ordinary work."""
        for command in (
            "git commit -m 'unterminated",
            'python3 -c "print(1)',
            "git status; " + "printf x; " * 2100,
            "sh -c " + shlex.quote("sh -c " + shlex.quote("sh -c " + shlex.quote("sh -c 'git log'"))),
        ):
            with self.subTest(command_size=len(command)):
                ctx = hook_context(bash(command))
                ctx.probes.read.side_effect = AssertionError("unrelated command read probes")
                self.assertEqual(mem_guard._cmd_hook(ctx), 0)
                self.assertEqual(ctx.stderr.getvalue(), "")
        for command in (
            "docker run 'unterminated",
            "d'o'cker run image 'unterminated",
            "DOCKER_HOST=x docker --unknown-option fixture run image",
            "docker compose --unknown-option fixture up",
            "printf x; " * 2100 + "docker run image",
            'eval "docker run \'unterminated"',
            "xcodebuild -scheme 'unterminated",
        ):
            with self.subTest(command=command[:60]):
                ctx = hook_context(bash(command))
                ctx.probes.read.return_value = memory_state(1, 90)
                self.assertEqual(mem_guard._cmd_hook(ctx), 2)
                self.assertIn("hook check unavailable", ctx.stderr.getvalue())

    def test_fixed_refusal_labels_and_outputs_never_copy_argument_credentials(self):
        secret = "synthetic-credential-7265"
        for command in (
            f"lms load {secret}",
            f"xcodebuild -scheme {secret}",
            f"docker --context {secret} run image",
            f"docker compose --env-file {secret} up",
            f"npm ci --registry=https://user:{secret}@example.invalid",
            f"codex exec {secret}",
            f"ollama run {secret}",
            f"python3 -m mlx_lm.generate --token {secret}",
            f"simctl boot {secret}",
            f"open -a Simulator --args {secret}",
        ):
            with self.subTest(command=command):
                ctx = hook_context(bash(command))
                self.assertEqual(mem_guard._cmd_hook(ctx), 2)
                self.assertNotIn(secret, ctx.stderr.getvalue())
                self.assertNotIn(secret, json.dumps(ctx.journal.write.call_args_list, default=str))
                self.assertNotIn(secret, mem_guard.hook_match(bash(command))[0])

    def test_hook_probe_and_unknown_reading_refuse(self):
        for reading in (RuntimeError("synthetic-secret"), memory_state(None, None)):
            with self.subTest(reading=type(reading).__name__):
                ctx = hook_context(bash("codex exec fixture"))
                if isinstance(reading, Exception):
                    ctx.probes.read.side_effect = reading
                else:
                    ctx.probes.read.return_value = reading
                self.assertEqual(mem_guard._cmd_hook(ctx), 2)
                self.assertNotIn("synthetic-secret", ctx.stderr.getvalue())

    def test_hook_malformed_oversized_or_failed_input_refuses(self):
        for raw in (b"{", b"\xff", b"x" * (mem_guard.HOOK_MAX + 1)):
            with self.subTest(raw_size=len(raw)):
                ctx = hook_context(bash("xcodebuild"))
                ctx.stdin = io.BytesIO(raw)
                self.assertEqual(mem_guard._cmd_hook(ctx), 2)
        ctx = hook_context(bash("xcodebuild"))
        ctx.stdin = mock.Mock(read=mock.Mock(side_effect=OSError("synthetic-secret")))
        self.assertEqual(mem_guard._cmd_hook(ctx), 2)
        self.assertNotIn("synthetic-secret", ctx.stderr.getvalue())

    def test_hook_shell_parse_errors_and_complexity_limits_refuse(self):
        for command in (
            "codex exec 'unterminated",
            "codex exec fixture; " + "printf x; " * 2100 + "lms load fixture",
            "codex exec fixture; " + "x" * (64 << 10) + "; lms load fixture",
        ):
            with self.subTest(command_size=len(command)):
                ctx = hook_context(bash(command))
                ctx.probes.read.return_value = memory_state(1, 90)
                self.assertEqual(mem_guard._cmd_hook(ctx), 2)
        command = "codex exec fixture; " + "printf x; " * 1000 + "lms load fixture"
        self.assertEqual(mem_guard.hook_match(bash(command))[1], "model")

    def test_nested_shell_depth_exhaustion_refuses_instead_of_hiding_work(self):
        command = "lms load fixture"
        for _ in range(4):
            command = "sh -c " + shlex.quote(command)
        ctx = hook_context(bash(command))
        ctx.probes.read.return_value = memory_state(1, 90)
        self.assertEqual(mem_guard._cmd_hook(ctx), 2)

    def test_hook_invalid_payload_shapes_refuse_and_valid_unrelated_tools_allow(self):
        for payload in (
            None,
            [],
            {},
            {"tool_name": None},
            {"tool_name": "Bash"},
            {"tool_name": "Bash", "tool_input": []},
            bash(None),
            bash(8),
            bash(""),
            bash("  "),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(mem_guard._cmd_hook(hook_context(payload)), 2)
        for payload in (
            {"tool_name": "Read", "tool_input": {"file_path": "fixture"}},
            {"tool_name": "Other", "tool_input": {}},
        ):
            ctx = hook_context(payload)
            self.assertEqual(mem_guard._cmd_hook(ctx), 0)
            self.assertEqual(ctx.stderr.getvalue(), "")

    def test_hook_registry_or_refusal_output_failure_never_allows(self):
        for target in ("registry", "journal", "stderr"):
            with self.subTest(target=target):
                ctx = hook_context(bash("xcodebuild"))
                if target == "registry":
                    ctx.registry.resume_overdue.side_effect = RuntimeError("synthetic-secret")
                    ctx.probes.read.return_value = memory_state(1, 90)
                elif target == "stderr":
                    ctx.stderr = mock.Mock(write=mock.Mock(side_effect=RuntimeError("synthetic-secret")))
                else:
                    ctx.journal.write.side_effect = RuntimeError("synthetic-secret")
                self.assertEqual(mem_guard._cmd_hook(ctx), 2)
                if target != "stderr":
                    self.assertNotIn("synthetic-secret", ctx.stderr.getvalue())

    def test_unrelated_and_informational_commands_keep_silent_allow_behavior(self):
        for command in ("printf fixture", "docker --help", "docker ps", "xcodebuild -list", "npm --version"):
            with self.subTest(command=command):
                ctx = hook_context(bash(command))
                ctx.registry.resume_overdue.side_effect = AssertionError("unrelated command touched registry")
                ctx.probes.read.side_effect = AssertionError("unrelated command read probes")
                self.assertEqual(mem_guard._cmd_hook(ctx), 0)
                self.assertEqual(ctx.stderr.getvalue(), "")

    def test_successful_guarded_work_keeps_allow_behavior(self):
        ctx = hook_context(bash("lms load fixture"))
        ctx.probes.read.return_value = memory_state(1, 90)
        self.assertEqual(mem_guard._cmd_hook(ctx), 0)
        self.assertEqual(ctx.stderr.getvalue(), "")
        ctx.journal.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
