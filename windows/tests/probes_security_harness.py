"""Deterministic source comparisons and mutations for the Windows probe boundaries.

Run from the repository root with ``python -B -m windows.tests.probes_security_harness
--baseline BASE_COMMIT``. All transport and native process APIs are mocked. The
baseline is read from Git; neither the source file nor its index is modified.
"""

import argparse
import contextlib
import copy
import datetime as dt
import http.client
import io
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from windows.agiw_win import probes
from windows.tests import test_probes_security as regressions

FOCUSED = (
    "windows.tests.test_probes_security",
    "windows.tests.test_memory_parity",
    "windows.tests.test_pair_hands_off",
    "windows.tests.test_lane_attribution",
    "windows.tests.test_machines",
    "windows.tests.test_peer_link_reader",
    "windows.tests.test_fix7_probes",
    "windows.tests.test_fix9_probes",
    "windows.tests.test_probes.LaneProbe",
    "windows.tests.test_probes.SlowSlots",
    "windows.tests.test_probes.Hardening",
    "windows.tests.test_probes.Codemode",
    "windows.tests.test_probes.PeerLink",
    "windows.tests.test_probes.PeerInventoryCompleteness",
    "windows.tests.test_probes.PeerPresenceCompleteness",
    "windows.tests.test_probes.LegacyPeerCompleteness",
    "windows.tests.test_probes.Queue",
    "windows.tests.test_probes.PCPresenceModels",
    "windows.tests.test_probes.PCStatusComponents",
    "windows.tests.test_probes.PCStatusLimitations",
)


def load_source(source, label):
    module = types.ModuleType("windows.agiw_win._" + label)
    module.__package__ = "windows.agiw_win"
    module.__file__ = probes.__file__
    exec(compile(source, probes.__file__, "exec"), module.__dict__)
    return module


def baseline_source(commit):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}
    root = Path(probes.__file__).resolve().parents[2]
    return subprocess.check_output(
        ["git", "-C", str(root), "show", commit + ":windows/agiw_win/probes.py"], env=environment, text=True
    )


def run_tests(names, module=None):
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    with mock.patch.object(regressions, "probes", module or probes):
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=2).run(suite)
    return result


def result_record(result):
    return {
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skips": [{"test": case.id(), "reason": reason} for case, reason in result.skipped],
        "failure_tests": [case.id() for case, _ in result.failures],
        "error_tests": [case.id() for case, _ in result.errors],
    }


def mutation_specs():
    deadline = "DetachedResponseDeadline.test_http10_body_deadline_interrupts_detached_socket"
    slots = "MachineActivityEvidence.test_missing_slots_clear_counts_but_preserve_residency"
    share = "UnverifiedShareEvidence.test_unsigned_health_cannot_promote_machine"
    coverage = "PeerLinkCoverage.test_truncated_list_qualifies_counts"
    return (
        ("retain-response-socket", "response_socket = conn.sock", "response_socket = None", deadline),
        ("use-detached-socket", "sock = response_socket", "sock = None", deadline),
        ("interrupt-read-direction", "sock.shutdown(socket.SHUT_RDWR)", "sock.shutdown(socket.SHUT_WR)", deadline),
        ("deadline-budget", "threading.Timer(2 * timeout, expire)", "threading.Timer(timeout, expire)", deadline),
        ("cancel-deadline", "timer.cancel()", "pass", deadline),
        (
            "close-response",
            "            response.close()",
            "            pass",
            "DetachedResponseDeadline.test_detached_response_closed_after_success",
        ),
        (
            "normalize-naive-utc",
            "observed_at = observed_at.replace(tzinfo=dt.timezone.utc)",
            "observed_at = observed_at",
            "LegacyTimestampRegressions.test_naive_timestamp_is_utc_even_when_local_clock_differs",
        ),
        (
            "utc-offset",
            "observed_at = observed_at.replace(tzinfo=dt.timezone.utc)",
            "observed_at = observed_at.replace(tzinfo=dt.timezone(dt.timedelta(hours=1)))",
            "LegacyTimestampRegressions.test_naive_timestamp_is_utc_even_when_local_clock_differs",
        ),
        (
            "enumeration-failure",
            'raise OSError("EnumProcesses failed")',
            "return []",
            "ProcessEnumerationRegressions.test_enum_failure_raises_instead_of_empty",
        ),
        (
            "enumeration-success",
            "if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):",
            "if psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):",
            "ProcessEnumerationRegressions.test_successful_empty_enumeration_remains_known",
        ),
        (
            "consumer-uncertainty",
            "                consumers_known = False",
            "                consumers_known = True",
            "ProcessEnumerationRegressions.test_enum_failure_keeps_consumers_unknown",
        ),
        (
            "clear-busy-count",
            "machine.update(busyCount=None, promptTokensPerSecond=None, generationTokensPerSecond=None)",
            "machine.update(busyCount=0, promptTokensPerSecond=None, generationTokensPerSecond=None)",
            slots,
        ),
        (
            "clear-prompt-rate",
            "machine.update(busyCount=None, promptTokensPerSecond=None, generationTokensPerSecond=None)",
            "machine.update(busyCount=None, promptTokensPerSecond=12, generationTokensPerSecond=None)",
            slots,
        ),
        (
            "clear-generation-rate",
            "machine.update(busyCount=None, promptTokensPerSecond=None, generationTokensPerSecond=None)",
            "machine.update(busyCount=None, promptTokensPerSecond=None, generationTokensPerSecond=8)",
            slots,
        ),
        (
            "preserve-known-zero",
            "            if not activity:\n                machine.update",
            "            if activity:\n                machine.update",
            "MachineActivityEvidence.test_current_slots_preserve_known_zero",
        ),
        (
            "unverified-positive-health",
            'state == "up" and record.get("verified") is not True',
            "False",
            share,
        ),
        (
            "verification-requires-boolean",
            'state == "up" and record.get("verified") is not True',
            'state == "up" and not record.get("verified")',
            share,
        ),
        (
            "unverified-degraded-health",
            'state == "up" and record.get("verified") is not True',
            'state == "up" and health == "ok" and record.get("verified") is not True',
            share,
        ),
        (
            "truncation-boundary",
            'omitted_rows = max(0, len(doc["peers"]) - PEER_LINK_MAX_ROWS)',
            'omitted_rows = max(0, len(doc["peers"]) - PEER_LINK_MAX_ROWS - 1)',
            coverage,
        ),
        (
            "truncation-flag",
            "source.update(truncated=True, complete=False",
            "source.update(truncated=False, complete=False",
            coverage,
        ),
        (
            "incomplete-total",
            "source.update(truncated=True, complete=False",
            "source.update(truncated=True, complete=True",
            coverage,
        ),
        ("omitted-row-total", "omittedRows=omitted_rows)", "omittedRows=omitted_rows + 1)", coverage),
        (
            "nonexact-source-state",
            '"unavailable" if omitted_rows else "live",',
            '"live",',
            coverage,
        ),
        (
            "stale-truncation-disclosure",
            "        if omitted_rows:\n            source.update",
            "        if omitted_rows and fresh:\n            source.update",
            "PeerLinkCoverage.test_stale_truncated_list_records_omissions",
        ),
    )


def mutations(source):
    witnesses = []
    for name, old, new, test in mutation_specs():
        if source.count(old) != 1:
            raise AssertionError((name, "mutation anchor must be unique", source.count(old)))
        mutated = source.replace(old, new, 1)
        module = load_source(mutated, "mutation_" + name.replace("-", "_"))
        result = run_tests(["windows.tests.test_probes_security." + test], module)
        if result.wasSuccessful() or result.errors:
            raise AssertionError((name, "mutant must be killed by an assertion", result_record(result)))
        witnesses.append({"mutation": name, "test": test, "failures": len(result.failures)})
    return {"mutants": len(witnesses), "killed": len(witnesses), "survived": 0, "witnesses": witnesses}


class FixtureEntry:
    def __init__(self, path, *, mtime, regular=True, directory=False):
        self.path = str(path)
        self.name = Path(path).name
        self.mtime = mtime
        self.regular = regular
        self.directory = directory

    def is_file(self, *, follow_symlinks):
        return self.regular

    def is_dir(self, *, follow_symlinks):
        return self.directory

    def stat(self, *, follow_symlinks):
        return types.SimpleNamespace(st_mtime=self.mtime)


def compare(expected, actual, category, case):
    if expected != actual:
        raise AssertionError((category, case, expected, actual))


def snapshot_case(randomizer):
    value = regressions.snapshot(randomizer.choice(("live", "unavailable", "error", "recorded")))
    value["sources"].extend(
        {"id": key, "state": randomizer.choice(("live", "unavailable")), "ageSeconds": randomizer.choice((0, 40))}
        for key in ("lane-processes", "route-queue", "codemode-runs", "hands-off", "pc-gpu", "mem-guard", "agiw-peers")
    )
    value.update(
        handsOff={"active": randomizer.choice((True, False, None)), "cause": "other-processes"},
        windowsWorker={"lanes": {"fast": {"up": True, "slotsBusy": 0}}, "gpus": []},
        windowsJobs={"liveAvailable": True},
        memory={"level": "tight", "consumersKnown": True},
        macPeer={"via": "sharedchami", "name": "mac"},
        peers=[],
    )
    return value


def compare_snapshot(base, randomizer, index):
    value = snapshot_case(randomizer)
    expected = base.enforce_snapshot_evidence(value)
    activity = probes.source_current(value["sources"][2])
    if not activity:
        expected["machines"][0].update(busyCount=None, promptTokensPerSecond=None, generationTokensPerSecond=None)
    compare(expected, probes.enforce_snapshot_evidence(value), "snapshot", index)
    compare(base.pc_status(value), probes.pc_status(value), "public-status", index)


def compare_lane(base, randomizer, index):
    spec = {"id": "fast", "port": 21001, "alias": "model", "device": "cpu"}
    replies = {
        "/health": randomizer.choice(({"status": "ok"}, {"status": "loading"}, {}, "invalid")),
        "/v1/models": randomizer.choice(({"data": [{"id": "model"}]}, {"data": []}, {"data": [{"id": "other"}]})),
        "/slots": randomizer.choice(
            (
                [{"id": 0, "is_processing": False}],
                [{"id": 0, "is_processing": True, "n_decoded": 4}],
                [],
                {"complete": False, "slots": []},
            )
        ),
    }
    calls = []

    def fetch(url, timeout, maximum):
        path = url.replace("http://127.0.0.1:21001", "")
        calls.append((path, timeout, maximum))
        return copy.deepcopy(replies[path])

    expected = base.probe_lane(spec, fetch=fetch)
    expected_calls = list(calls)
    calls.clear()
    compare(expected, probes.probe_lane(spec, fetch=fetch), "lane", index)
    compare(expected_calls, calls, "lane-read-order", index)


def compare_hands(base, randomizer, index):
    gpus = [
        {
            "index": number,
            "name": "gpu",
            "utilizationPercent": randomizer.choice((None, 0, 40, 41, 90)),
            "busiestEngine": randomizer.choice((None, "3d", "compute")),
            "engineShares": [],
        }
        for number in range(randomizer.randrange(4))
    ]
    kwargs = {
        "gpu_live": randomizer.choice((True, False)),
        "lane_pids": randomizer.choice((None, frozenset(), frozenset((1,)))),
        "notification_state": randomizer.choice((None, 0, 1, 2, 3, 4, 5, 6)),
        "gpu_complete": randomizer.choice((True, False)),
    }
    compare(
        base.hands_off(copy.deepcopy(gpus), **kwargs),
        probes.hands_off(copy.deepcopy(gpus), **kwargs),
        "hands-off",
        index,
    )


def compare_timestamp(randomizer, index):
    observed = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=randomizer.randrange(31_536_000))
    offset = randomizer.choice((None, -8, 0, 5))
    stamp = (
        observed.replace(tzinfo=None).isoformat()
        if offset is None
        else observed.astimezone(dt.timezone(dt.timedelta(hours=offset))).isoformat()
    )
    age = randomizer.choice((-10, -2, 0, 10, 31, 40000))
    peer = regressions.LegacyTimestampRegressions.read_legacy(stamp, observed.timestamp() + age)
    if age < -probes.PEER_FUTURE_SKEW:
        compare(None, peer, "timestamp-future", index)
    else:
        compare(observed.timestamp(), peer["observedUnix"], "timestamp-utc", index)
        compare(max(age, 0), peer["ageSeconds"], "timestamp-age", index)
        compare(max(age, 0) <= probes.PEER_FRESH_SECONDS, peer["fresh"], "timestamp-fresh", index)


def compare_share(base, randomizer, index):
    record = regressions.share_record(
        health=randomizer.choice(("ok", "degraded", "down", "unknown")),
        fresh=randomizer.choice((True, False)),
        verified=randomizer.choice((True, False, None, "true")),
        ageSeconds=randomizer.choice((0, 10, 60)),
    )
    expected = base._peer_machine(record, "macos", None)
    if expected["state"] == "up" and record["verified"] is not True:
        expected.update(
            state="unknown",
            reason="unverified-report",
            reasonCode="unavailable",
            loadedCount=None,
            busyCount=None,
            modelsComplete=False,
        )
        expected["detail"] += "; machine health unverified"
    compare(expected, probes._peer_machine(record, "macos", None), "share-health", index)


def compare_peer_link(base, randomizer, index):
    count = randomizer.randrange(81)
    age = randomizer.choice((-8, -2, 0, 30, 31, 200))
    document = regressions.peer_document(count, age)
    with mock.patch.object(base, "_read_peer_link_file", return_value=document):
        expected_rows, expected_source = base.read_peer_link(regressions.NOW, enabled=True)
    with mock.patch.object(probes, "_read_peer_link_file", return_value=document):
        actual = probes.read_peer_link(regressions.NOW, enabled=True)
    omitted = max(0, count - probes.PEER_LINK_MAX_ROWS)
    if omitted:
        expected_source.update(
            state="unavailable", ageSeconds=None, truncated=True, complete=False, omittedRows=omitted
        )
        expected_source["detail"] += f"; counts among {len(expected_rows)} retained peers; {omitted} omitted"
    compare((expected_rows, expected_source), actual, "peer-list", index)


def compare_queue(base, randomizer, index):
    root = Path("queue-fixture")
    states = {}
    for state in ("pending", "claimed", "completed"):
        states[state] = [
            FixtureEntry(
                root / state / f"item-{number}.json",
                mtime=regressions.NOW - randomizer.choice((-5, 0, 100, 1000, 90000)),
                regular=randomizer.choice((True, True, False)),
            )
            for number in range(randomizer.randrange(8))
        ]

    def scan(path):
        return contextlib.nullcontext(states[Path(path).name])

    results = []
    for module in (base, probes):
        with (
            mock.patch.object(module, "is_link_or_junction", return_value=False),
            mock.patch.object(Path, "is_dir", return_value=True),
            mock.patch.object(module.os, "scandir", side_effect=scan),
            mock.patch.object(module, "read_json", return_value={}),
            mock.patch.object(module, "QUEUE_MAX_ENTRIES", 5),
        ):
            results.append(module.route_queue(root, now=regressions.NOW))
    compare(results[0], results[1], "queue-scan", index)


def compare_runs(base, randomizer, index):
    root = Path("run-fixture")
    entries = []
    manifests = {}
    mtimes = {}
    for number in range(randomizer.randrange(8)):
        directory = root / f"20261116-000000-{number}"
        entry = FixtureEntry(directory, mtime=regressions.NOW - number, directory=True)
        entries.append(entry)
        manifest = directory / "manifest.json"
        mtimes[str(manifest)] = randomizer.choice((regressions.NOW - 100, regressions.NOW + 10))
        manifests[str(manifest)] = {
            "run_id": str(number),
            "verification": randomizer.choice(("CERTIFIED", "UNKNOWN")),
            "metrics": [
                {
                    "kind": "local",
                    "endpoint": "http://127.0.0.1:21001",
                    "stage": "edit",
                    "model": "model",
                    "tok": randomizer.randrange(100),
                    "sec": randomizer.choice((0, 1, 2)),
                    "finish": randomizer.choice(("stop", "length", "other")),
                }
                for _ in range(randomizer.randrange(10))
            ],
        }
    results = []
    for module in (base, probes):
        with (
            mock.patch.object(module, "is_link_or_junction", return_value=False),
            mock.patch.object(Path, "is_dir", return_value=True),
            mock.patch.object(module.os, "scandir", side_effect=lambda _: contextlib.nullcontext(entries)),
            mock.patch.object(Path, "stat", lambda path: types.SimpleNamespace(st_mtime=mtimes[str(path)])),
            mock.patch.object(module, "read_json", side_effect=lambda path, _: copy.deepcopy(manifests[str(path)])),
            mock.patch.object(module, "QUEUE_MAX_ENTRIES", 5),
            mock.patch.object(module, "CODEMODE_RUNS_MAX", 3),
        ):
            results.append(module.codemode_jobs(root, [{"port": 21001, "id": "fast"}], regressions.NOW))
    compare(results[0], results[1], "run-scan", index)


def compare_peer_scan(base, randomizer, index):
    root = Path("share-fixture")
    entries = []
    documents = {}
    for number in range(randomizer.randrange(8)):
        path = root / "agiw-peers" / f"peer-{number}.json"
        entries.append(FixtureEntry(path, mtime=regressions.NOW - number, regular=randomizer.choice((True, False))))
        documents[str(path)] = {
            "schemaVersion": 1,
            "host": f"peer-{number}",
            "platform": "macos",
            "observedUnix": regressions.NOW - randomizer.choice((0, 10, 100)),
            "health": "ok",
            "models": [],
            "modelsTotal": 0,
        }
    results = []
    for module in (base, probes):
        with (
            mock.patch.object(module, "is_link_or_junction", return_value=False),
            mock.patch.object(module.os, "scandir", side_effect=lambda _: contextlib.nullcontext(entries)),
            mock.patch.object(module, "read_json", side_effect=lambda path, _: copy.deepcopy(documents[str(path)])),
            mock.patch.object(module, "_legacy_mac_status", return_value=None),
            mock.patch.object(module, "host_id", return_value="local"),
            mock.patch.object(module, "PEER_SCAN_LIMIT", 6),
            mock.patch.object(module, "PEER_MAX_FILES", 4),
        ):
            results.append(module.read_peers(root, "self", regressions.NOW))
    compare(results[0], results[1], "peer-file-scan", index)


def compare_processes(randomizer, index):
    succeeds = randomizer.choice((True, False))
    api = mock.Mock(psapi=mock.Mock(EnumProcesses=mock.Mock(return_value=succeeds)), kernel32=mock.Mock())
    bound = randomizer.randrange(-2, 65540)
    with mock.patch.object(probes, "IS_WINDOWS", True), mock.patch.object(probes.ctypes, "windll", api, create=True):
        if succeeds:
            compare([], probes.top_processes(max_pids=bound), "process-enumeration", index)
        else:
            try:
                probes.top_processes(max_pids=bound)
            except OSError as error:
                if "EnumProcesses" not in str(error):
                    raise
            else:
                raise AssertionError(("process-enumeration", index, "failure became empty"))
        block, source = probes.memory_block(status_fn=regressions.ProcessEnumerationRegressions.memory_status)
    compare(succeeds, block["consumersKnown"], "process-consumer-coverage", index)
    compare("live", source["state"], "process-memory-independent", index)


def compare_deadline(randomizer, index):
    timeout = randomizer.uniform(0.001, 0.5)
    closing = randomizer.choice((True, False))
    headers = (
        randomizer.choice(
            (b"HTTP/1.0 200 OK\r\n\r\n", b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\n")
        )
        if closing
        else b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n"
    )
    timer, sock = mock.Mock(), mock.Mock()
    connection = http.client.HTTPConnection("127.0.0.1", timeout=timeout)
    connection._create_connection = mock.Mock(return_value=sock)
    detached = []

    class DeadlineStream(io.BytesIO):
        def expire(self):
            detached.append(connection.sock is None)
            timer.callback()

        def read(self, size=-1):
            self.expire()
            return b"" if sock.shutdown.called else super().read(size)

        def readinto(self, buffer):
            self.expire()
            return 0 if sock.shutdown.called else super().readinto(buffer)

    stream = DeadlineStream(headers + b"{}")
    sock.makefile.return_value = stream

    def make_timer(interval, callback):
        compare(2 * timeout, interval, "deadline-budget", index)
        timer.callback = callback
        return timer

    with (
        mock.patch.object(probes.http.client, "HTTPConnection", return_value=connection),
        mock.patch.object(probes.threading, "Timer", side_effect=make_timer),
    ):
        try:
            probes.http_get_json("http://127.0.0.1/status", timeout=timeout)
        except TimeoutError:
            pass
        else:
            raise AssertionError(("deadline", index, "body escaped expiry"))
    compare([closing], detached, "deadline-detachment", index)
    sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
    timer.cancel.assert_called_once_with()
    compare(True, stream.closed, "deadline-response-close", index)


def compare_publication(base, cases):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for index in range(cases):
            for module, label in ((base, "previous"), (probes, "current")):
                top = root / label
                top.mkdir(exist_ok=True)
                path = top / f"entry-{index}" / "nested" / "status.json"
                payload = {"schemaVersion": 1, "case": index, "models": [f"model-{index}"]}
                module.write_json_atomic(path, payload, stop_at=top)
                compare(payload, json.loads(path.read_text()), "publication", (index, label))


def differential(base):
    randomizer = random.Random(73019)
    categories = {
        "snapshot-publication": 0,
        "public-status": 0,
        "lane-read-and-slots": 0,
        "hands-off": 0,
        "legacy-timestamps": 0,
        "share-health": 0,
        "peer-link-coverage": 0,
        "route-queue": 0,
        "run-history": 0,
        "peer-file-scan": 0,
        "descriptor-publication": 0,
        "process-enumeration": 0,
        "http-deadlines": 0,
    }
    for index in range(600):
        compare_snapshot(base, randomizer, index)
        compare_lane(base, randomizer, index)
        compare_hands(base, randomizer, index)
        compare_timestamp(randomizer, index)
        compare_share(base, randomizer, index)
        compare_peer_link(base, randomizer, index)
        compare_queue(base, randomizer, index)
        compare_runs(base, randomizer, index)
        compare_peer_scan(base, randomizer, index)
        compare_processes(randomizer, index)
        compare_deadline(randomizer, index)
        for category in categories:
            if category != "descriptor-publication":
                categories[category] += 1
    compare_publication(base, 40)
    categories["descriptor-publication"] = 40
    return {
        "generated_comparisons": sum(categories.values()),
        "generated_input_cases": sum(categories.values()) - categories["public-status"],
        "mismatches": 0,
        "cases": categories,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline")
    parser.add_argument("--mode", choices=("all", "focused", "baseline", "mutations", "differential"), default="all")
    args = parser.parse_args()
    denied = []

    def audit(event, _arguments):
        if event in ("socket.bind", "socket.connect", "socket.getaddrinfo"):
            denied.append(event)
            raise AssertionError("live network I/O is outside this source harness")

    sys.addaudithook(audit)
    record = {"python": sys.version.split()[0], "mode": args.mode}
    source = Path(probes.__file__).read_text()
    if args.mode in ("all", "focused"):
        result = run_tests(FOCUSED)
        record["focused"] = result_record(result)
        if not result.wasSuccessful():
            raise AssertionError(record["focused"])
    if args.mode in ("all", "mutations"):
        record["mutations"] = mutations(source)
    if args.mode in ("all", "baseline", "differential"):
        if not args.baseline:
            parser.error("--baseline is required for a source comparison")
        base = load_source(baseline_source(args.baseline), "baseline")
        if args.mode == "baseline":
            record["baseline"] = result_record(run_tests(["windows.tests.test_probes_security"], base))
        else:
            record["differential"] = differential(base)
    record["network_attempts"] = len(denied)
    if denied:
        raise AssertionError(denied)
    sys.stdout.write(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    main()
