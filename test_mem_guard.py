"""mem_guard tests. Every probe, clock, path, runner, signal sender and notifier is injected.

The only real system state touched: one `/bin/sleep 60` child per registry/watchdog test, spawned
here and killed in tearDown. Signals go through SafeKill, which forwards only to that child and
fails the test for any other pid (virtual descendant pids above the macOS pid range are recorded,
never sent).

The review-regression tests at the end spawn up to two more `/bin/sleep 60` children of their own
(also killed in cleanup) and use virtual pids for everything else.
"""
import errno
import io
import json
import os
import signal
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import mem_guard
from mem_guard import GiB

RAM = 68719476736  # hw.memsize, real capture 2026-09-26 (64 GiB)

# Real captures. vm_stat: the incident lines from 2026-09-26 ~20:40Z (from the spec) and a later
# full capture taken inside this workflow's sandbox (trimmed). swapusage: `sysctl vm.swapusage`,
# 2026-09-26. The sandbox refused `ps` and the memorystatus sysctls, so PS_SAMPLE is synthetic,
# modelled on the incident's consumers with real macOS executable paths.
VM_STAT_INCIDENT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    67084.
Pages wired down:                             360443.
Pages occupied by compressor:                1595121.
"""
VM_STAT_LIVE = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    14562.
Pages active:                                 785177.
Pages inactive:                               781765.
Pages speculative:                              1562.
Pages throttled:                                   0.
Pages wired down:                             369241.
Pages purgeable:                                4700.
"Translation faults":                     2275887238.
Pages copy-on-write:                        97531780.
File-backed pages:                           1014356.
Anonymous pages:                              554148.
Pages stored in compressor:                  2526340.
Pages occupied by compressor:                2183458.
Decompressions:                             26078985.
Swapouts:                                    5536262.
"""
SWAP_TEXT = "total = 17408.00M  used = 15695.25M  free = 1712.75M  (encrypted)"
SWAP_TOTAL = 17408 * (1 << 20)
SWAP_USED = int(15695.25 * (1 << 20))
SIM = "/Library/Developer/CoreSimulator/Volumes/iOS_23A5287/Library/Developer/CoreSimulator/Profiles/Runtimes/iOS 26.0.simruntime/Contents/Resources/RuntimeRoot"
PS_SAMPLE = f"""  612  1843200 {SIM}/usr/libexec/backboardd
  613  2457600 {SIM}/System/Library/CoreServices/SpringBoard.app/SpringBoard
  614   409600 {SIM}/usr/sbin/launchd_sim
  982   912384 /Applications/LM Studio.app/Contents/MacOS/LM Studio
  990  1048576 /Users/example/.lmstudio/bin/lms
 1201   655360 /Applications/Claude.app/Contents/MacOS/Claude
 1202   524288 /Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/Contents/MacOS/Claude Helper (Renderer)
 1301   393216 /Applications/Codex.app/Contents/MacOS/Codex
 1302   381952 /opt/homebrew/bin/codex
 1401   262144 /Applications/Safari.app/Contents/MacOS/Safari
 1402   196608 /System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent
 1501   716800 /Applications/OneDrive.app/Contents/MacOS/OneDrive
 1502   102400 /Applications/OneDrive.app/Contents/MacOS/OneDrive
 1601   307200 /System/Library/Frameworks/CoreServices.framework/Frameworks/Metadata.framework/Versions/A/Support/mds_stores
 1701   204800 /System/Library/CoreServices/Finder.app/Contents/MacOS/Finder
 1801    51200 /usr/sbin/cfprefsd
garbage line
"""
SIMCTL_JSON = json.dumps({"devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
        {"name": "iPhone 18 Pro", "state": "Booted", "udid": "A"},
        {"name": "iPhone 17 Pro", "state": "Booted", "udid": "B"}],
    "com.apple.CoreSimulator.SimRuntime.watchOS-12-0": [{"name": "Watch", "state": "Shutdown"}]}})


def mem_state(pressure=1, avail=50, swap=0, compressed=0, vm_free=200 * GiB, ram=RAM):
    return {"pressure": pressure, "availablePercent": avail, "ramBytes": ram, "swapUsedBytes": swap,
            "swapTotalBytes": SWAP_TOTAL, "compressedBytes": compressed, "wiredBytes": 5 * GiB,
            "vmFreeBytes": vm_free, "gpuAllocBytes": None, "sampledAt": 1000.0}


OK = mem_state(1, 50)
WATCH = mem_state(1, 30)
TIGHT = mem_state(2, 30)
CRITICAL = mem_state(4, 5)
UNKNOWN = mem_state(None, None)
CONSUMERS = mem_guard.group_consumers(mem_guard.parse_ps(PS_SAMPLE), simulators=["iPhone 18 Pro", "iPhone 17 Pro"])


def completed(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


class Runner:
    """Fake subprocess.run keyed by the joined argv; unknown commands fail the test."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(" ".join(args))
        assert kwargs.get("timeout"), "every command needs a timeout"
        answer = self.table.get(" ".join(args))
        if answer is None:
            raise AssertionError(f"unexpected command {args}")
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def count(self, prefix):
        return sum(1 for call in self.calls if call.startswith(prefix))


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeProbes:
    def __init__(self, state, consumers=None):
        self.state = state
        self.rows = consumers
        self.reads = 0

    def read(self, gpu_alloc_bytes=None):
        self.reads += 1
        if isinstance(self.state, BaseException):
            raise self.state
        return dict(self.state)

    def consumers(self, models=None, gpu_alloc_bytes=None):
        return self.rows


class Notes:
    def __init__(self):
        self.sent = []

    def __call__(self, title, body):
        self.sent.append((title, body))


class ParserTests(unittest.TestCase):
    def test_vm_stat_incident_capture(self):
        vm = mem_guard.parse_vm_stat(VM_STAT_INCIDENT)
        self.assertEqual(vm["pageSize"], 16384)
        self.assertEqual(vm["compressedBytes"], 1595121 * 16384)
        self.assertEqual(vm["wiredBytes"], 360443 * 16384)
        self.assertEqual(vm["freeBytes"], 67084 * 16384)
        self.assertEqual(mem_guard.gb(vm["compressedBytes"]), "24.3 GB")

    def test_vm_stat_full_capture_ignores_other_lines(self):
        vm = mem_guard.parse_vm_stat(VM_STAT_LIVE)
        self.assertEqual((vm["compressedBytes"], vm["wiredBytes"]), (2183458 * 16384, 369241 * 16384))

    def test_vm_stat_without_header_or_odd_page_size_is_rejected(self):
        with self.assertRaises(ValueError):
            mem_guard.parse_vm_stat(VM_STAT_INCIDENT.splitlines()[1])
        with self.assertRaises(ValueError):
            mem_guard.parse_vm_stat(VM_STAT_INCIDENT.replace("16384", "12345"))

    def test_swapusage_text_capture(self):
        self.assertEqual(mem_guard.parse_swapusage_text(SWAP_TEXT), (SWAP_TOTAL, SWAP_USED))
        for bad in ("", "total = 1.00M", "total = 1.00M  used = 9.00G  free = 0M"):
            with self.assertRaises(ValueError):
                mem_guard.parse_swapusage_text(bad)

    def test_xsw_usage_struct(self):
        raw = struct.pack("=QQQIi", SWAP_TOTAL, SWAP_TOTAL - SWAP_USED, SWAP_USED, 16384, 1)
        self.assertEqual(mem_guard.parse_xsw_usage(raw), (SWAP_TOTAL, SWAP_USED))
        with self.assertRaises(ValueError):
            mem_guard.parse_xsw_usage(raw[:20])
        with self.assertRaises(ValueError):
            mem_guard.parse_xsw_usage(struct.pack("=QQQIi", 10, 0, 11, 16384, 1))

    def test_simctl_booted_names(self):
        self.assertEqual(mem_guard.parse_simctl_booted(SIMCTL_JSON), ["iPhone 18 Pro", "iPhone 17 Pro"])


class ConsumerTests(unittest.TestCase):
    def test_groups_on_ps_output(self):
        rows = {(r["group"], r["label"]): r for r in CONSUMERS}
        sim = rows[("ios-simulator", "iOS Simulators (iPhone 18 Pro, iPhone 17 Pro)")]
        self.assertEqual((sim["residentBytes"], sim["processCount"], sim["devices"]),
                         ((1843200 + 2457600 + 409600) * 1024, 3, ["iPhone 18 Pro", "iPhone 17 Pro"]))
        self.assertEqual(rows[("llm-server", "Local LLM server")]["processCount"], 2)
        self.assertEqual(rows[("claude", "Claude")]["residentBytes"], (655360 + 524288) * 1024)
        self.assertEqual(rows[("codex", "Codex")]["processCount"], 2)
        self.assertEqual(rows[("browser", "Browsers")]["processCount"], 2)
        others = [r["label"] for r in CONSUMERS if r["group"] == "other"]
        self.assertEqual(others, ["OneDrive", "mds_stores", "Finder"], "other: top 3 by RSS, grouped by name")
        self.assertEqual(rows[("other", "OneDrive")]["processCount"], 2)
        weights = [r["residentBytes"] for r in CONSUMERS]
        self.assertEqual(weights, sorted(weights, reverse=True))
        for row in CONSUMERS:
            self.assertLessEqual(set(row), {"group", "label", "residentBytes", "processCount", "devices",
                                            "models", "gpuAllocBytes"})

    def test_llm_row_carries_loaded_models_and_gpu_allocation(self):
        models = [{"id": "gemma-4-26b", "loaded": True, "host": "mac", "state": "idle"},
                  {"id": "qwen3.8-27b", "loaded": True, "host": "mac", "state": "generating"},
                  {"id": "remote", "loaded": True, "host": "pc"}, {"id": "cold", "loaded": False}]
        rows = mem_guard.group_consumers(mem_guard.parse_ps(PS_SAMPLE), models=models, gpu_alloc_bytes=40 * GiB)
        self.assertEqual(rows[0]["group"], "llm-server", "GPU allocation counts when ordering")
        self.assertEqual(rows[0]["models"], ["gemma-4-26b", "qwen3.8-27b"])
        self.assertIn("GPU allocation 40.0 GB", mem_guard.describe_consumer(rows[0]))

    def test_group_of(self):
        cases = {"/opt/homebrew/bin/llama-server": "llm-server", "/usr/local/bin/ollama": "llm-server",
                 "/Applications/Bionic.app/Contents/MacOS/Bionic": "llm-server",
                 "/Users/x/.local/share/claude/versions/2.1/claude": "claude",
                 "/Applications/ChatGPT.app/Contents/Resources/codex": "codex",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome": "browser",
                 "/Applications/Firefox.app/Contents/MacOS/firefox": "browser", "/usr/sbin/cfprefsd": "other"}
        for comm, group in cases.items():
            self.assertEqual(mem_guard.group_of(comm), group, comm)

    def test_suggestions_are_text_ordered_by_size(self):
        models = [{"id": "small", "loaded": True, "state": "idle", "sizeBytes": 2 * GiB},
                  {"id": "big", "loaded": True, "state": "loaded", "sizeBytes": 16 * GiB},
                  {"id": "busy", "loaded": True, "state": "generating", "sizeBytes": 30 * GiB}]
        tips = mem_guard.suggestions(CONSUMERS, models, paused_count=2)
        self.assertEqual(tips, ["Unload idle model big from the monitor",
                                "Shut down unused iOS Simulators (`xcrun simctl shutdown all`)",
                                "Unload idle model small from the monitor", "2 background jobs paused"])
        self.assertEqual(mem_guard.suggestions([], None, 0), [])


class ProbeTests(unittest.TestCase):
    SYSCTL = {"kern.memorystatus_vm_pressure_level": struct.pack("=i", 2),
              "kern.memorystatus_level": struct.pack("=i", 18),
              "hw.memsize": struct.pack("=Q", RAM),
              "vm.swapusage": struct.pack("=QQQIi", SWAP_TOTAL, SWAP_TOTAL - SWAP_USED, SWAP_USED, 16384, 1)}

    class StatVfs:
        def __init__(self, fail_vm=False):
            self.calls = []
            self.fail_vm = fail_vm

        def __call__(self, path):
            self.calls.append(path)
            if self.fail_vm and path == mem_guard.VM_PATH:
                raise FileNotFoundError(path)
            return os.statvfs_result((4096, 4096, 100, 50, 25 * GiB // 4096, 10, 5, 5, 0, 255))

    def probes(self, sysctl=None, table=None, statvfs=None, clock=None):
        runner = Runner(table if table is not None else {"/usr/bin/vm_stat": completed(VM_STAT_INCIDENT)})
        clock = clock or Clock()
        probes = mem_guard.Probes(sysctl=sysctl if sysctl is not None else self.SYSCTL.get, runner=runner,
                                  statvfs=statvfs or self.StatVfs(), clock=clock, wall=lambda: 5.0)
        return probes, runner, clock

    def test_read_decodes_every_probe(self):
        probes, runner, _ = self.probes()
        state = probes.read(gpu_alloc_bytes=40 * GiB)
        self.assertEqual({k: state[k] for k in ("pressure", "availablePercent", "ramBytes", "swapTotalBytes",
                                                "swapUsedBytes", "compressedBytes", "wiredBytes", "vmFreeBytes",
                                                "gpuAllocBytes", "sampledAt")},
                         {"pressure": 2, "availablePercent": 18, "ramBytes": RAM, "swapTotalBytes": SWAP_TOTAL,
                          "swapUsedBytes": SWAP_USED, "compressedBytes": 1595121 * 16384,
                          "wiredBytes": 360443 * 16384, "vmFreeBytes": 25 * GiB, "gpuAllocBytes": 40 * GiB,
                          "sampledAt": 5.0})
        self.assertEqual(runner.calls, ["/usr/bin/vm_stat"], "ctypes answered, so no sysctl subprocess")

    def test_cli_fallback_when_ctypes_fails_and_backoff_after_cli_failure(self):
        table = {"/usr/bin/vm_stat": completed(VM_STAT_INCIDENT),
                 "/usr/sbin/sysctl -n kern.memorystatus_vm_pressure_level": completed("4\n"),
                 "/usr/sbin/sysctl -n kern.memorystatus_level": completed("", 1),
                 "/usr/sbin/sysctl -n hw.memsize": completed(f"{RAM}\n"),
                 "/usr/sbin/sysctl -n vm.swapusage": completed(SWAP_TEXT + "\n")}
        probes, runner, clock = self.probes(sysctl=lambda name, size: None, table=table)
        state = probes.read()
        self.assertEqual((state["pressure"], state["availablePercent"], state["ramBytes"], state["swapUsedBytes"]),
                         (4, None, RAM, SWAP_USED))
        probes.read()
        self.assertEqual(runner.count("/usr/sbin/sysctl -n kern.memorystatus_level"), 1, "failed CLI backs off")
        clock.advance(31)
        probes.read()
        self.assertEqual(runner.count("/usr/sbin/sysctl -n kern.memorystatus_level"), 2)
        self.assertEqual(runner.count("/usr/sbin/sysctl -n hw.memsize"), 1, "RAM is read once")

    def test_hw_pagesize_is_the_fallback_when_vm_stat_has_no_header(self):
        values = dict(self.SYSCTL)
        values["hw.pagesize"] = struct.pack("=Q", 16384)  # 8 bytes, as this Mac returned it
        headless = VM_STAT_INCIDENT.split("\n", 1)[1]
        probes, _, _ = self.probes(sysctl=values.get, table={"/usr/bin/vm_stat": completed(headless)})
        self.assertEqual(probes.read()["compressedBytes"], 1595121 * 16384)
        probes, _, _ = self.probes(table={"/usr/bin/vm_stat": completed(headless)})
        self.assertIsNone(probes.read()["compressedBytes"], "no header and no hw.pagesize -> unknown")

    def test_out_of_range_values_are_unknown(self):
        values = dict(self.SYSCTL)
        values["kern.memorystatus_vm_pressure_level"] = struct.pack("=i", 3)
        values["kern.memorystatus_level"] = struct.pack("=i", 140)
        probes, _, _ = self.probes(sysctl=values.get)
        state = probes.read(gpu_alloc_bytes=-5)
        self.assertEqual((state["pressure"], state["availablePercent"], state["gpuAllocBytes"]), (None, None, None))

    def test_slow_probes_are_cached(self):
        statvfs = self.StatVfs(fail_vm=True)
        probes, runner, clock = self.probes(statvfs=statvfs)
        probes.read()
        clock.advance(4.9)
        probes.read()
        self.assertEqual(runner.count("/usr/bin/vm_stat"), 1)
        self.assertEqual(statvfs.calls, [mem_guard.VM_PATH, "/"], "VM volume missing -> falls back to /")
        clock.advance(0.2)
        probes.read()
        self.assertEqual(runner.count("/usr/bin/vm_stat"), 2)
        clock.advance(5.0)
        probes.read()
        self.assertEqual(len(statvfs.calls), 4, "statvfs at most every 10 s")

    def test_every_probe_failing_yields_unknown_without_raising(self):
        def boom(*_a, **_k):
            raise PermissionError("sandbox")
        probes = mem_guard.Probes(sysctl=boom, runner=boom, statvfs=boom, clock=Clock(), wall=lambda: 1.0)
        state = probes.read()
        self.assertEqual({k: state[k] for k in mem_guard.STATE_KEYS}, dict.fromkeys(mem_guard.STATE_KEYS))
        self.assertEqual(mem_guard.classify(state)[0], "unknown")
        self.assertIsNone(probes.consumers())

    def test_consumers_ps_every_10s_and_simulators_every_30s(self):
        table = {"/usr/bin/vm_stat": completed(VM_STAT_INCIDENT),
                 "/bin/ps -axo pid=,rss=,comm=": completed(PS_SAMPLE),
                 "/usr/bin/xcrun simctl list devices booted -j": completed(SIMCTL_JSON)}
        probes, runner, clock = self.probes(table=table)
        rows = probes.consumers(models=[{"id": "gemma", "loaded": True}])
        self.assertEqual(rows[0]["devices"], ["iPhone 18 Pro", "iPhone 17 Pro"])
        self.assertEqual(next(r for r in rows if r["group"] == "llm-server")["models"], ["gemma"])
        clock.advance(9)
        probes.consumers()
        self.assertEqual((runner.count("/bin/ps"), runner.count("/usr/bin/xcrun")), (1, 1))
        clock.advance(2)
        probes.consumers()
        self.assertEqual((runner.count("/bin/ps"), runner.count("/usr/bin/xcrun")), (2, 1))
        clock.advance(20)
        probes.consumers()
        self.assertEqual((runner.count("/bin/ps"), runner.count("/usr/bin/xcrun")), (3, 2))

    def test_simctl_is_not_run_without_simulator_processes(self):
        table = {"/bin/ps -axo pid=,rss=,comm=": completed(" 1 1024 /usr/sbin/cfprefsd\n")}
        probes, runner, _ = self.probes(table=table)
        self.assertEqual(probes.consumers()[0]["label"], "cfprefsd")
        self.assertEqual(runner.calls, ["/bin/ps -axo pid=,rss=,comm="])


class ClassifyTests(unittest.TestCase):
    def level(self, **kw):
        return mem_guard.classify(mem_state(**kw))[0]

    def test_pressure(self):
        self.assertEqual([self.level(pressure=p) for p in (1, 2, 4)], ["ok", "tight", "critical"])
        self.assertEqual(mem_guard.classify(mem_state(pressure=2))[1], ["macOS memory pressure: warning"])
        self.assertEqual(mem_guard.classify(mem_state(pressure=4))[1], ["macOS memory pressure: critical"])

    def test_available_percent_boundaries(self):
        cases = {9.9: "critical", 10: "tight", 19.9: "tight", 20: "watch", 34.9: "watch", 35: "ok"}
        for avail, level in cases.items():
            self.assertEqual(self.level(avail=avail), level, avail)

    def test_swap_boundaries(self):
        cases = {0.50: "critical", 0.4999: "tight", 0.25: "tight", 0.2499: "watch", 0.10: "watch", 0.0999: "ok"}
        for fraction, level in cases.items():
            self.assertEqual(self.level(swap=int(RAM * fraction + (1 if fraction in (0.5, 0.25, 0.1) else 0))),
                             level, fraction)

    def test_compressor_boundaries(self):
        # Corroborated (availability below the watch threshold): the full tight/watch ladder.
        cases = {0.40: "tight", 0.3999: "watch", 0.25: "watch", 0.2499: "watch"}
        for fraction, level in cases.items():
            self.assertEqual(self.level(avail=34, compressed=int(RAM * fraction + (1 if fraction in (0.4, 0.25) else 0))),
                             level, fraction)
        # Uncorroborated (pressure normal, 50% available): the compressor alone reaches watch only.
        cases = {0.40: "watch", 0.3999: "watch", 0.25: "watch", 0.2499: "ok"}
        for fraction, level in cases.items():
            self.assertEqual(self.level(compressed=int(RAM * fraction + (1 if fraction in (0.4, 0.25) else 0))),
                             level, fraction)

    def test_compressor_alone_never_makes_tight(self):
        """Review finding (spec deviation): this Mac sat at compressor 51% of RAM with pressure normal
        and 60% available; the spec rule made that 'tight' and refused every build and model load."""
        live = mem_state(pressure=1, avail=60, compressed=2118427 * 16384)
        self.assertEqual(mem_guard.classify(live), ("watch", ["compressor 32.3 GB (51% of RAM)"]))
        self.assertTrue(mem_guard.admit(4 * GiB, "heavy", live).allowed)
        self.assertEqual(mem_guard.classify(dict(live, pressure=2))[0], "tight")
        self.assertEqual(mem_guard.classify(dict(live, availablePercent=30))[0], "tight")
        self.assertEqual(mem_guard.classify(dict(live, availablePercent=None))[0], "watch")

    def test_vm_volume_free(self):
        self.assertEqual(self.level(vm_free=10 * GiB - 1), "critical")
        self.assertEqual(self.level(vm_free=10 * GiB), "ok")

    def test_incident_reads_tight_with_plain_reasons(self):
        state = mem_state(pressure=2, avail=None, swap=SWAP_USED, compressed=1595121 * 16384, vm_free=None)
        level, reasons = mem_guard.classify(state)
        self.assertEqual(level, "tight")
        self.assertEqual(reasons, ["macOS memory pressure: warning", "swap 15.3 GB (24% of RAM)",
                                   "compressor 24.3 GB (38% of RAM)"])

    def test_unknown_when_pressure_and_availability_both_missing(self):
        level, reasons = mem_guard.classify(mem_state(None, None, swap=RAM))
        self.assertEqual(level, "unknown")
        self.assertTrue(reasons[0].startswith("memory state unknown"))
        self.assertIn("swap 64.0 GB (100% of RAM)", reasons, "evidence is still reported")
        self.assertEqual(mem_guard.classify(mem_state(None, 50))[0], "ok")
        self.assertEqual(mem_guard.classify(mem_state(1, None))[0], "ok")
        self.assertEqual(mem_guard.classify(None)[0], "unknown")

    def test_missing_inputs_never_raise_the_level(self):
        state = mem_state(1, 50, swap=None, compressed=None, vm_free=None)
        self.assertEqual(mem_guard.classify(state), ("ok", []))
        self.assertEqual(mem_guard.classify(mem_state(1, 50, swap=RAM, compressed=RAM, ram=None))[0], "ok")
        self.assertEqual(mem_guard.classify({"pressure": [2], "availablePercent": "5"})[0], "unknown")

    def test_config_thresholds_and_notes(self):
        cfg = mem_guard.default_config()
        cfg["watch_available_percent"] = 60.0
        cfg["notes"] = ["config value floor_gib ignored (out of range)"]
        self.assertEqual(mem_guard.classify(mem_state(1, 50), cfg),
                         ("watch", ["only 50% of memory available", "config value floor_gib ignored (out of range)"]))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "mem-guard.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        self.path.write_text(data if isinstance(data, str) else json.dumps(data))
        os.chmod(self.path, 0o600)

    def test_missing_file_is_defaults_without_notes(self):
        cfg = mem_guard.load_config(self.path)
        self.assertEqual({k: cfg[k] for k in mem_guard.DEFAULTS}, mem_guard.DEFAULTS)
        self.assertEqual(cfg["notes"], [])

    def test_known_keys_apply_unknown_keys_are_ignored(self):
        self.write({"floor_gib": 8, "notifications": False, "rm -rf": 1, "tight_available_percent": 25.5})
        cfg = mem_guard.load_config(self.path)
        self.assertEqual((cfg["floor_gib"], cfg["notifications"], cfg["tight_available_percent"]), (8.0, False, 25.5))
        self.assertNotIn("rm -rf", cfg)
        self.assertEqual(cfg["notes"], [])

    def test_out_of_range_or_wrong_type_keeps_the_default(self):
        self.write({"floor_gib": 5000, "watch_swap_fraction": -1, "critical_available_percent": True,
                    "notifications": "yes", "pause_after_seconds": 30})
        cfg = mem_guard.load_config(self.path)
        self.assertEqual((cfg["floor_gib"], cfg["watch_swap_fraction"], cfg["critical_available_percent"],
                          cfg["notifications"], cfg["pause_after_seconds"]), (4.0, 0.10, 10.0, True, 30.0))
        self.assertEqual(len(cfg["notes"]), 4)

    def test_symlink_oversize_wrong_owner_and_non_object_are_ignored(self):
        real = Path(self.tmp.name) / "real.json"
        real.write_text(json.dumps({"floor_gib": 8}))
        self.path.symlink_to(real)
        cfg = mem_guard.load_config(self.path)
        self.assertEqual(cfg["floor_gib"], 4.0)
        self.assertTrue(cfg["notes"][0].startswith("config ignored"), cfg["notes"])
        self.path.unlink()
        for content, uid in ((json.dumps({"floor_gib": 8, "pad": "x" * (17 << 10)}), None),
                             (json.dumps({"floor_gib": 8}), os.getuid() + 1),
                             ("[1, 2]", None), ("{not json", None)):
            self.write(content)
            cfg = mem_guard.load_config(self.path, uid=uid)
            self.assertEqual(cfg["floor_gib"], 4.0, content[:20])
            self.assertTrue(cfg["notes"] and cfg["notes"][0].startswith("config ignored"), cfg["notes"])
            self.assertIn("config ignored", mem_guard.classify(OK, cfg)[1][-1])

    def test_directory_or_fifo_is_ignored_without_blocking(self):
        fifo = Path(self.tmp.name) / "fifo.json"
        os.mkfifo(fifo)
        cfg = mem_guard.load_config(fifo)
        self.assertTrue(cfg["notes"][0].startswith("config ignored"))
        cfg = mem_guard.load_config(Path(self.tmp.name))
        self.assertTrue(cfg["notes"][0].startswith("config ignored"))


class AdmitTests(unittest.TestCase):
    FLOOR = int(0.10 * RAM)  # max(4 GiB, 10% of 64 GiB)

    def check(self, state, need, kind=None):
        decision = mem_guard.admit(need, kind, state)
        self.assertIsInstance(decision.reason, str)
        self.assertTrue(decision.reason)
        return decision

    def test_matrix(self):
        roomy_tight = mem_state(2, 30)            # 19.2 GiB available
        cramped_tight = mem_state(2, 10.5)        # 6.72 GiB available
        watch_cramped = mem_state(1, 21)          # 13.44 GiB available
        cases = [
            (CRITICAL, GiB, None, False), (CRITICAL, 8 * GiB, None, False),
            (mem_state(4, 90), GiB // 2, "light", False),
            (roomy_tight, 8 * GiB, None, False), (roomy_tight, GiB, None, True),
            (cramped_tight, GiB, None, False), (mem_state(2, None), GiB, None, True),
            (mem_state(2, None), 4 * GiB, None, False),
            (WATCH, GiB, None, True), (WATCH, 8 * GiB, None, True), (watch_cramped, 8 * GiB, None, False),
            (watch_cramped, GiB, None, True),
            (UNKNOWN, GiB, None, True), (UNKNOWN, 8 * GiB, None, False),
            (OK, 8 * GiB, None, True), (OK, 30 * GiB, None, False), (OK, GiB, None, True),
            (mem_state(1, 11), GiB, None, False),  # ok level is not checked here: 11% -> tight
            (mem_state(1, None), GiB, None, True), (mem_state(1, None), 8 * GiB, None, False),
        ]
        for state, need, kind, allowed in cases:
            decision = self.check(state, need, kind)
            self.assertEqual(decision.allowed, allowed, (state["pressure"], state["availablePercent"], need))

    def test_ok_level_refuses_when_headroom_is_below_the_floor(self):
        state = mem_state(1, 36)  # 23.04 GiB available, level ok
        self.assertEqual(mem_guard.classify(state)[0], "ok")
        self.assertTrue(self.check(state, 16 * GiB).allowed)
        refused = self.check(state, 17 * GiB)
        self.assertFalse(refused.allowed)
        self.assertIn("floor 6.4 GB", refused.reason)

    def test_floor_is_at_least_4_gib(self):
        small = mem_state(1, 50, ram=16 * GiB)  # 8 GiB available
        self.assertTrue(self.check(small, 4 * GiB).allowed)
        self.assertFalse(self.check(small, 4 * GiB + 1).allowed)

    def test_kind_defaults_from_size_and_explicit_kind_only_tightens(self):
        self.assertEqual(self.check(OK, 2 * GiB - 1).kind, "light")
        self.assertEqual(self.check(OK, 2 * GiB).kind, "heavy")
        self.assertEqual(self.check(OK, GiB, "heavy").kind, "heavy")
        self.assertEqual(self.check(OK, 3 * GiB, "light").kind, "heavy")
        self.assertFalse(self.check(mem_state(2, 30), GiB, "heavy").allowed)

    def test_decision_fields_and_reasons(self):
        decision = self.check(mem_state(2, 30), 8 * GiB)
        self.assertEqual((decision.allowed, decision.level, decision.availableBytes, decision.needBytes),
                         (False, "tight", int(0.30 * RAM), 8 * GiB))
        self.assertIn("memory is tight (macOS memory pressure: warning)", decision.reason)
        self.assertIn("memory state unknown", self.check(UNKNOWN, GiB).reason)
        self.assertEqual(set(decision.to_dict()), {"allowed", "level", "reason", "availableBytes", "kind",
                                                   "needBytes", "suggestion"})

    def test_never_raises(self):
        for state in ("not a dict", 42, {"ramBytes": "x", "availablePercent": float("nan")}):
            self.assertTrue(mem_guard.admit(GiB, None, state).allowed)
            self.assertFalse(mem_guard.admit(8 * GiB, None, state).allowed)
        for need in (None, "8", float("inf"), -1):
            decision = mem_guard.admit(need, None, OK)
            self.assertEqual(decision.kind, "heavy", need)

    def test_check_reads_fresh_state_and_adds_the_top_suggestion(self):
        probes = FakeProbes(TIGHT, CONSUMERS)
        refused = mem_guard.check(8 * GiB, "heavy", probes=probes)
        self.assertFalse(refused.allowed)
        self.assertEqual(refused.suggestion, "Shut down unused iOS Simulators (`xcrun simctl shutdown all`)")
        self.assertTrue(mem_guard.check(GiB, probes=FakeProbes(OK)).allowed)
        self.assertFalse(mem_guard.check(8 * GiB, probes=FakeProbes(RuntimeError("boom"))).allowed)
        self.assertTrue(mem_guard.check(GiB, probes=FakeProbes(RuntimeError("boom"))).allowed)


class JournalAndNotifierTests(unittest.TestCase):
    def test_journal_is_private_and_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "events.jsonl"
            journal = mem_guard.Journal(path, clock=lambda: 7.0, max_bytes=200)
            for index in range(8):
                self.assertTrue(journal.write({"type": "level", "n": index}))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
            rotated = Path(str(path) + ".1")
            self.assertTrue(rotated.exists())
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(lines[-1], {"at": 7.0, "type": "level", "n": 7})
            self.assertLessEqual(os.path.getsize(rotated), 200 + 60)

    def test_journal_never_raises(self):
        self.assertFalse(mem_guard.Journal("/dev/null/nope/events.jsonl").write({"type": "x"}))

    def test_osascript_gets_title_and_body_as_argv_only(self):
        runner = Runner({})
        seen = []

        def capture(args, **kwargs):
            seen.append((args, kwargs))
            return completed()
        evil = '" & do shell script "touch /tmp/pwned" & "'
        notifier = mem_guard.OsascriptNotifier(runner=capture)
        self.assertTrue(notifier("AGIW memory", evil))
        args, kwargs = seen[0]
        self.assertEqual(args[:7], ["/usr/bin/osascript", "-e", "on run argv", "-e",
                                    "display notification (item 2 of argv) with title (item 1 of argv)",
                                    "-e", "end run"])
        self.assertEqual(args[7:], ["AGIW memory", evil])
        self.assertTrue(all(evil not in part for part in args[:7]))
        self.assertEqual(kwargs["timeout"], 5)
        notifier("--x", "-e do shell script")
        self.assertEqual(seen[1][0][7:], ["x", "e do shell script"], "no argv may look like an option")
        self.assertEqual(runner.calls, [])

    def test_notifier_failure_is_swallowed(self):
        def boom(*_a, **_k):
            raise subprocess.TimeoutExpired("osascript", 5)
        self.assertFalse(mem_guard.OsascriptNotifier(runner=boom)("t", "b"))


# -------------------------------------------------------------------------------------------------
# Registry and watchdog: one real sleep child, identity from a fake ps, signals through SafeKill.

VIRTUAL = 4_000_001  # above the macOS pid range: recorded, never sent
FAKE_PARENT = 4_000_900
START = "Sat Sep 26 13:39:02 2026"


class FakePs:
    def __init__(self, me, uid):
        self.me, self.uid = me, uid
        self.procs = {me: (FAKE_PARENT, uid, START, "/usr/bin/python3"),
                      FAKE_PARENT: (1, uid, START, "/bin/zsh")}
        self.fail = False
        self.calls = []

    def add(self, pid, ppid, comm="/bin/sleep", start=START, uid=None):
        self.procs[pid] = (ppid, self.uid if uid is None else uid, start, comm)

    def check_env(self, env):
        assert env.get("LC_ALL") == "C" and env.get("TZ") == "UTC0", env

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        assert kwargs.get("timeout")
        self.check_env(kwargs.get("env", {}))
        if self.fail:
            raise PermissionError("ps refused")
        if args[:2] == ["/bin/ps", "-axo"] and args[2] == "pid=,ppid=,uid=":
            return completed("".join(f"{pid:>7} {p[0]:>7} {p[1]:>5}\n" for pid, p in self.procs.items()))
        if args[:3] == ["/bin/ps", "-o", "pid=,ppid=,uid=,lstart=,comm="] and args[3] == "-p":
            wanted = [int(x) for x in args[4].split(",")]
            rows = "".join(f"{pid:>7} {self.procs[pid][0]:>7} {self.procs[pid][1]:>5} {self.lstart(pid)}     "
                           f"{self.procs[pid][3]}\n" for pid in wanted if pid in self.procs)
            return completed(rows, 0 if all(pid in self.procs for pid in wanted) else 1)
        raise AssertionError(f"unexpected command {args}")

    def lstart(self, pid):
        return self.procs[pid][2]


class SafeKill:
    def __init__(self, real, virtual=()):
        self.real, self.virtual = set(real), set(virtual)
        self.calls = []

    def __call__(self, pid, sig):
        if pid not in self.real and pid not in self.virtual:
            raise AssertionError(f"test tried to signal pid {pid}")
        self.calls.append((pid, sig))
        if pid in self.real:
            os.kill(pid, sig)


def wait_status(pid, flag, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got, status = os.waitpid(pid, flag | os.WNOHANG)
        if got == pid and predicate(status):
            return True
        time.sleep(0.01)
    return False


def stopped(pid):
    return wait_status(pid, os.WUNTRACED, os.WIFSTOPPED)


def continued(pid):
    return wait_status(pid, os.WCONTINUED, os.WIFCONTINUED)


class ChildCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "mem-guard"
        self.child = subprocess.Popen(["/bin/sleep", "60"])
        self.me = os.getpid()
        self.uid = os.getuid()
        self.ps = FakePs(self.me, self.uid)
        self.ps.add(self.child.pid, self.me)
        self.kill = SafeKill([self.child.pid], [VIRTUAL, VIRTUAL + 1])
        self.clock = Clock(0.0)
        self.registry = mem_guard.Registry(self.root, runner=self.ps, kill=self.kill, clock=self.clock)

    def tearDown(self):
        self.child.kill()
        self.child.wait()
        self.tmp.cleanup()

    def events(self):
        path = self.root / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class RegistryTests(ChildCase):
    def test_register_writes_a_private_identity_entry(self):
        entry = self.registry.register(self.child.pid, "astra-review\x07 " + "x" * 200)
        path = self.root / "pausable" / f"{self.child.pid}.json"
        on_disk = json.loads(path.read_text())
        self.assertEqual(on_disk, entry)
        self.assertEqual((entry["schemaVersion"], entry["pid"], entry["startTime"], entry["comm"]),
                         (1, self.child.pid, START, "sleep"))
        self.assertEqual(len(entry["label"]), 80)
        self.assertTrue(entry["label"].isprintable())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "pausable").st_mode), 0o700)
        self.assertEqual(self.registry.register(self.child.pid, "again"), entry, "same identity is idempotent")
        self.assertEqual([e["pid"] for e in self.registry.list_pausable()], [self.child.pid])
        self.assertEqual(self.kill.calls, [])

    def test_register_refuses_unsafe_targets(self):
        self.ps.add(VIRTUAL, 1, uid=self.uid + 1)
        for pid in (0, 1, -5, "12", self.me, FAKE_PARENT, VIRTUAL, VIRTUAL + 7):
            with self.assertRaises(mem_guard.RegistryError, msg=str(pid)):
                self.registry.register(pid, "x")
        self.ps.fail = True
        with self.assertRaises(mem_guard.RegistryError):
            self.registry.register(self.child.pid, "x")
        self.assertFalse((self.root / "pausable").exists() and any((self.root / "pausable").iterdir()))

    def test_pid_reuse_or_new_executable_drops_the_entry_without_signalling(self):
        self.registry.register(self.child.pid, "job")
        self.ps.add(self.child.pid, self.me, start="Sat Sep 26 14:00:00 2026")
        self.assertEqual(self.registry.list_pausable(), [])
        self.assertFalse((self.root / "pausable" / f"{self.child.pid}.json").exists())
        self.ps.add(self.child.pid, self.me)
        self.registry.register(self.child.pid, "job")
        self.ps.add(self.child.pid, self.me, comm="/usr/bin/python3")
        self.assertEqual(self.registry.pause_all(), [])
        self.assertEqual(self.kill.calls, [])

    def test_unreadable_process_table_deletes_nothing_and_signals_nothing(self):
        self.registry.register(self.child.pid, "job")
        self.ps.fail = True
        self.assertEqual(self.registry.list_pausable(), [])
        self.assertEqual(self.registry.pause_all(), [])
        self.assertTrue((self.root / "pausable" / f"{self.child.pid}.json").exists())
        self.assertEqual(self.kill.calls, [])

    def test_pause_stops_the_job_and_same_user_descendants_then_resume_continues_them(self):
        self.ps.add(VIRTUAL, self.child.pid)                      # a descendant
        self.ps.add(VIRTUAL + 1, VIRTUAL, uid=0)                  # another user's: never signalled
        self.registry.register(self.child.pid, "job")
        jobs = self.registry.pause_all()
        self.assertEqual([m["pid"] for m in jobs[0]["members"]], [self.child.pid, VIRTUAL])
        self.assertTrue(stopped(self.child.pid))
        self.assertEqual(self.kill.calls, [(self.child.pid, signal.SIGSTOP), (VIRTUAL, signal.SIGSTOP)])
        paused_path = self.root / "paused.json"
        self.assertEqual(stat.S_IMODE(os.stat(paused_path).st_mode), 0o600)
        record = json.loads(paused_path.read_text())["jobs"][0]
        self.assertEqual((record["pid"], record["startTime"], record["comm"], record["pausedAt"]),
                         (self.child.pid, START, "sleep", 0.0))
        self.assertEqual(self.registry.paused(), [{"pid": self.child.pid, "label": "job", "comm": "sleep",
                                                   "pausedAt": 0.0, "members": 2}])
        self.assertEqual(self.registry.pause_all(), [], "already paused")
        resumed = self.registry.resume()
        self.assertEqual(resumed[0]["resumed"], [VIRTUAL, self.child.pid])
        self.assertTrue(continued(self.child.pid))
        self.assertFalse(paused_path.exists())
        self.assertNotIn(VIRTUAL + 1, [pid for pid, _sig in self.kill.calls])

    def test_resume_rechecks_identity(self):
        self.registry.register(self.child.pid, "job")
        self.registry.pause_all()
        self.assertTrue(stopped(self.child.pid))
        self.kill.calls.clear()
        self.ps.add(self.child.pid, self.me, start="Sat Sep 26 15:00:00 2026")
        self.assertEqual(self.registry.resume()[0]["resumed"], [])
        self.assertEqual(self.kill.calls, [], "a pid whose start time changed is never signalled")

    def test_resume_keeps_the_record_when_ps_fails(self):
        self.registry.register(self.child.pid, "job")
        self.registry.pause_all()
        self.ps.fail = True
        self.assertEqual(self.registry.resume(), [])
        self.assertEqual(len(self.registry.paused()), 1)
        self.ps.fail = False
        self.assertEqual(len(self.registry.resume()), 1)
        self.assertTrue(continued(self.child.pid))

    def test_unregister_resumes_a_paused_job_first(self):
        self.registry.register(self.child.pid, "job")
        self.registry.pause_all()
        self.assertTrue(stopped(self.child.pid))
        self.assertTrue(self.registry.unregister(self.child.pid))
        self.assertTrue(continued(self.child.pid))
        self.assertEqual((self.registry.paused(), self.registry.list_pausable()), ([], []))
        self.assertFalse(self.registry.unregister(self.child.pid))

    def test_nothing_to_do_stays_read_only(self):
        self.assertEqual((self.registry.resume(), self.registry.unregister(self.child.pid),
                          self.registry.paused(), self.registry.list_pausable()), ([], False, [], []))
        self.assertFalse(self.root.exists(), "no state directory is created when there is nothing to do")

    def test_protects_own_process_and_ancestors_even_if_an_entry_names_them(self):
        (self.root / "pausable").mkdir(parents=True, mode=0o700)
        for pid in (self.me, FAKE_PARENT):
            path = self.root / "pausable" / f"{pid}.json"
            path.write_text(json.dumps({"schemaVersion": 1, "pid": pid, "startTime": START,
                                        "comm": self.ps.procs[pid][3].rsplit("/", 1)[-1], "label": "x"}))
            os.chmod(path, 0o600)
        self.assertEqual(self.registry.pause_all(), [])
        self.assertEqual(self.kill.calls, [])


class WatchdogTests(ChildCase):
    def dog(self, cfg=None, notes=None):
        self.notes = notes if notes is not None else Notes()
        return mem_guard.Watchdog(self.clock, None, self.notes, self.registry, cfg)

    def critical(self):
        state = dict(CRITICAL)
        state["consumers"] = CONSUMERS
        return state

    def test_pause_after_10s_critical_and_resume_after_30s_watch(self):
        self.registry.register(self.child.pid, "astra-review repo")
        dog = self.dog()
        self.assertEqual(dog.startup_events, [])
        dog.tick(self.critical())
        self.clock.advance(9.9)
        dog.tick(self.critical())
        self.assertEqual(self.kill.calls, [])
        self.clock.advance(0.1)
        events = dog.tick(self.critical())
        self.assertEqual([e["type"] for e in events], ["pause"])
        self.assertTrue(stopped(self.child.pid))
        self.clock.advance(1)
        dog.tick(WATCH)
        self.clock.advance(29.9)
        self.assertEqual(dog.tick(WATCH), [])
        self.clock.advance(0.1)
        events = dog.tick(WATCH)
        self.assertEqual([(e["type"], e.get("why")) for e in events], [("resume", "recovered")])
        self.assertTrue(continued(self.child.pid))
        self.assertEqual(self.registry.paused(), [])
        kinds = [(e["type"], e.get("to") or e.get("why")) for e in self.events()]
        self.assertEqual(kinds, [("level", "critical"), ("pause", None), ("level", "watch"), ("resume", "recovered")])

    def test_interrupted_critical_restarts_the_timer_and_tight_is_not_calm(self):
        self.registry.register(self.child.pid, "job")
        dog = self.dog()
        for state, step in ((self.critical(), 6), (TIGHT, 1), (self.critical(), 9.5)):
            dog.tick(state)
            self.clock.advance(step)
        dog.tick(self.critical())
        self.assertEqual(self.kill.calls, [], "9.5 s since critical came back")
        self.clock.advance(0.5)
        dog.tick(self.critical())
        self.assertTrue(stopped(self.child.pid))
        for _ in range(10):
            self.clock.advance(10)
            dog.tick(TIGHT)
        self.assertEqual(len(self.registry.paused()), 1, "tight never resumes; only watch or better")

    def test_identity_change_means_never_paused(self):
        self.registry.register(self.child.pid, "job")
        self.ps.add(self.child.pid, self.me, start="Sat Sep 26 16:00:00 2026")
        dog = self.dog()
        for _ in range(5):
            dog.tick(self.critical())
            self.clock.advance(10)
        self.assertEqual(self.kill.calls, [])
        self.assertEqual(self.registry.paused(), [])

    def test_resume_on_start_from_a_leftover_paused_file(self):
        self.registry.register(self.child.pid, "job")
        self.registry.pause_all()
        self.assertTrue(stopped(self.child.pid))
        dog = self.dog()
        self.assertEqual([(e["type"], e["why"]) for e in dog.startup_events], [("resume", "startup")])
        self.assertTrue(continued(self.child.pid))
        self.assertFalse((self.root / "paused.json").exists())
        self.assertEqual(self.events()[-1]["why"], "startup")

    def test_twenty_minute_limit_resumes_notifies_and_does_not_repause_in_the_same_episode(self):
        self.registry.register(self.child.pid, "job")
        dog = self.dog()
        dog.tick(self.critical())
        self.clock.advance(10)
        dog.tick(self.critical())
        self.assertTrue(stopped(self.child.pid))
        self.clock.advance(1199)
        dog.tick(self.critical())
        self.assertEqual(len(self.registry.paused()), 1)
        self.clock.advance(1)
        events = dog.tick(self.critical())
        self.assertEqual([(e["type"], e.get("why")) for e in events], [("resume", "limit"), ("notify", None)])
        self.assertTrue(continued(self.child.pid))
        self.assertEqual(self.notes.sent[-1],
                         ("AGIW memory", "1 paused job resumed after the 20-minute limit; memory is still critical."))
        for _ in range(6):
            self.clock.advance(5)
            dog.tick(self.critical())
        self.assertEqual(self.registry.paused(), [], "exempt for the rest of this critical episode")
        # Review finding: one tight sample used to end the episode, and the job was paused again
        # about 10 s later, then after every 20-minute cycle (stopped ~99% of the time).
        dog.tick(TIGHT)
        for _ in range(3):
            self.clock.advance(5)
            dog.tick(self.critical())
        self.assertEqual(self.registry.paused(), [], "one tight sample does not end the episode")
        # Sustained calm ends the episode, but the hold persisted at the limit resume still keeps the
        # job running for another max_pause_seconds, for this watchdog and for a restarted one.
        dog.tick(WATCH)
        self.clock.advance(30)
        dog.tick(WATCH)
        for _ in range(3):
            dog.tick(self.critical())
            self.clock.advance(10)
        dog.tick(self.critical())
        self.assertEqual(self.registry.paused(), [], "held after the limit resume")
        entry = json.loads((self.root / "pausable" / f"{self.child.pid}.json").read_text())
        self.assertEqual(entry["noRepauseUntil"], 1210.0 + 1200.0)
        restarted = self.dog()
        restarted.tick(self.critical())
        self.clock.advance(10)
        restarted.tick(self.critical())
        self.assertEqual(self.registry.paused(), [], "a restarted watchdog respects the hold")
        self.clock.t = 2411.0
        restarted.tick(self.critical())
        self.assertEqual(len(self.registry.paused()), 1, "after the hold a critical episode may pause it again")

    def test_close_resumes_everything(self):
        self.registry.register(self.child.pid, "job")
        dog = self.dog()
        dog.tick(self.critical())
        self.clock.advance(10)
        dog.tick(self.critical())
        self.assertTrue(stopped(self.child.pid))
        self.assertEqual([e["why"] for e in dog.close()], ["shutdown"])
        self.assertTrue(continued(self.child.pid))

    def test_notifications_escalation_cooldown_and_recovery(self):
        dog = self.dog()
        dog.tick(OK)
        self.assertEqual(self.notes.sent, [])
        dog.tick(dict(TIGHT, consumers=CONSUMERS))
        self.assertEqual(len(self.notes.sent), 1)
        title, body = self.notes.sent[0]
        self.assertEqual(title, "AGIW memory")
        self.assertTrue(body.startswith("Memory is tight: macOS memory pressure: warning."), body)
        self.assertIn("Top: iOS Simulators (iPhone 18 Pro, iPhone 17 Pro) about 4.5 GB, Local LLM server about 1.9 GB.", body)
        self.assertIn("Shut down unused iOS Simulators (`xcrun simctl shutdown all`).", body)
        dog.tick(TIGHT)
        self.clock.advance(60)
        dog.tick(WATCH)
        dog.tick(TIGHT)
        self.assertEqual(len(self.notes.sent), 1, "5-minute cooldown per level")
        dog.tick(self.critical())
        self.assertEqual(len(self.notes.sent), 2, "critical is its own level")
        self.assertTrue(self.notes.sent[1][1].startswith("Memory is critical"))
        self.clock.advance(300)
        dog.tick(WATCH)
        dog.tick(TIGHT)
        self.assertEqual(len(self.notes.sent), 3, "cooldown expired")
        dog.tick(OK)
        self.assertEqual(self.notes.sent[-1][1], "Memory recovered: back to ok after a critical episode.")
        dog.tick(WATCH)
        dog.tick(OK)
        self.assertEqual(len(self.notes.sent), 4, "recovery is announced once per critical episode")

    def test_notifications_can_be_disabled(self):
        cfg = mem_guard.default_config()
        cfg["notifications"] = False
        dog = self.dog(cfg)
        dog.tick(self.critical())
        dog.tick(OK)
        self.assertEqual(self.notes.sent, [])

    def test_tick_never_raises(self):
        class Broken:
            def read(self):
                raise RuntimeError("probe exploded")

        def bad_notifier(title, body):
            raise RuntimeError("osascript exploded")
        dog = mem_guard.Watchdog(self.clock, Broken(), bad_notifier, self.registry, None)
        self.assertEqual(dog.tick(), [])
        self.assertEqual([e["type"] for e in dog.tick(self.critical())], ["level", "notify"])
        self.assertEqual([e["type"] for e in dog.tick("garbage")], ["level"])


class GuardTests(unittest.TestCase):
    KEYS = {"level", "reasons", "pressure", "availablePercent", "ramBytes", "swapUsedBytes", "swapTotalBytes",
            "compressedBytes", "wiredBytes", "vmFreeBytes", "gpuAllocBytes", "consumers", "paused", "suggestions",
            "sampledAt"}

    def guard(self, probes, tmp):
        registry = mem_guard.Registry(Path(tmp) / "state", runner=Runner({}), kill=SafeKill([]))
        return mem_guard.Guard(cfg=mem_guard.default_config(), probes=probes, registry=registry,
                               notifier=Notes(), clock=Clock())

    def test_sample_is_a_well_formed_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            probes = FakeProbes(TIGHT, CONSUMERS)
            block, source = self.guard(probes, tmp).sample(gpu_alloc_bytes=40 * GiB,
                                                          models=[{"id": "m", "loaded": True, "state": "idle"}])
            self.assertEqual(set(block), self.KEYS)
            self.assertEqual((block["level"], block["consumers"], block["paused"]), ("tight", CONSUMERS, []))
            self.assertEqual(block["suggestions"][0], "Shut down unused iOS Simulators (`xcrun simctl shutdown all`)")
            self.assertIn("Unload idle model m from the monitor", block["suggestions"])
            self.assertEqual((source["id"], source["state"]), ("mem-guard", "live"))
            json.dumps(block, allow_nan=False)
            ok_block, _ = self.guard(FakeProbes(OK, CONSUMERS), tmp).sample()
            self.assertEqual(ok_block["consumers"], [], "consumers only at watch or worse")

    def test_probe_exception_or_unknown_state_is_unavailable_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            for probes in (FakeProbes(RuntimeError("probe exploded")), FakeProbes(UNKNOWN)):
                block, source = self.guard(probes, tmp).sample()
                self.assertEqual(set(block), self.KEYS)
                self.assertEqual((block["level"], source["id"], source["state"]), ("unknown", "mem-guard", "unavailable"))

    def test_close_is_bounded_when_a_tick_is_stuck(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = self.guard(FakeProbes(OK), tmp)
            guard._tick_lock.acquire()  # a tick stuck in a slow probe on the sampler thread
            try:
                started = time.monotonic()
                self.assertEqual(guard.close(timeout=0.05), [])
                self.assertLess(time.monotonic() - started, 1.0)
            finally:
                guard._tick_lock.release()
            self.assertTrue(guard._closed)


class GuardCloseTests(ChildCase):
    def test_close_resumes_and_later_samples_never_pause_again(self):
        self.registry.register(self.child.pid, "astra-review repo")
        guard = mem_guard.Guard(cfg=mem_guard.default_config(), probes=FakeProbes(CRITICAL, CONSUMERS),
                                registry=self.registry, notifier=Notes(), clock=self.clock)
        guard.sample()
        self.clock.advance(10)
        block, _source = guard.sample()
        self.assertTrue(stopped(self.child.pid))
        self.assertEqual([job["pid"] for job in block["paused"]], [self.child.pid])
        self.assertEqual([event["why"] for event in guard.close()], ["shutdown"])
        self.assertTrue(continued(self.child.pid))
        for _ in range(3):
            self.clock.advance(10)
            block, _source = guard.sample()
            self.assertEqual(block["level"], "critical", "the block is still computed after close")
        self.assertEqual(self.registry.paused(), [])
        self.assertEqual([sig for _pid, sig in self.kill.calls], [signal.SIGSTOP, signal.SIGCONT])


# -------------------------------------------------------------------------------------------------
# CLI and hook


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "state"
        self.clock = Clock(100.0)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv, state=OK, consumers=CONSUMERS, stdin=b"", registry=None, sleep=None, notes=None):
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        probes = state if isinstance(state, FakeProbes) else FakeProbes(state, consumers)
        ctx = mem_guard.Context(state_root=self.root, cfg=mem_guard.default_config(), probes=probes,
                                registry=registry, notifier=notes or Notes(), clock=self.clock,
                                sleep=sleep or self.clock.advance, gpu=lambda: None,
                                stdin=io.BytesIO(stdin), stdout=self.stdout, stderr=self.stderr)
        return mem_guard.main(argv, ctx)

    def journal(self):
        path = self.root / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class StatusAndAdmitCliTests(CliCase):
    def test_status_exit_codes_and_json(self):
        for state, code in ((OK, 0), (WATCH, 0), (TIGHT, 10), (CRITICAL, 11), (UNKNOWN, 3)):
            self.assertEqual(self.run_cli(["status", "--json"], state), code)
            block = json.loads(self.stdout.getvalue())
            self.assertEqual(set(block), GuardTests.KEYS)
        self.assertEqual(self.run_cli(["status"], TIGHT), 10)
        text = self.stdout.getvalue()
        self.assertIn("Memory: TIGHT", text)
        self.assertIn("iOS Simulators (iPhone 18 Pro, iPhone 17 Pro) about 4.5 GB in 3 processes", text)
        self.assertIn("Paused jobs: none", text)

    def test_admit_exit_codes_and_refusal_journal(self):
        self.assertEqual(self.run_cli(["admit", "--need-gb", "1"], TIGHT), 0)
        self.assertTrue(self.stdout.getvalue().startswith("admitted: "))
        self.assertEqual(self.run_cli(["admit", "--need-gb", "1", "--kind", "heavy", "--label", "sim boot"], TIGHT), 75)
        self.assertTrue(self.stdout.getvalue().startswith("refused: memory is tight"))
        self.assertEqual(len(self.stdout.getvalue().strip().splitlines()), 1)
        refusal = self.journal()[-1]
        self.assertEqual((refusal["type"], refusal["via"], refusal["label"], refusal["kind"], refusal["level"]),
                         ("refusal", "admit", "sim boot", "heavy", "tight"))
        self.assertEqual(self.run_cli(["admit", "--need-gb", "-1"], OK), 2)

    def test_admit_waits_polling_every_2s(self):
        probes = FakeProbes(CRITICAL)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            self.clock.advance(seconds)
            if len(sleeps) == 3:
                probes.state = OK
        self.assertEqual(self.run_cli(["admit", "--need-gb", "1", "--wait", "600"], probes, sleep=sleep), 0)
        self.assertEqual((sleeps, probes.reads), ([2.0, 2.0, 2.0], 4))
        probes.state = CRITICAL
        sleeps.clear()
        self.assertEqual(self.run_cli(["admit", "--need-gb", "1", "--wait", "5"], probes,
                                      sleep=lambda s: (sleeps.append(s), self.clock.advance(s))), 75)
        self.assertEqual(sleeps, [2.0, 2.0, 1.0])


class RegistryCliTests(CliCase, ChildCase):
    def setUp(self):
        ChildCase.setUp(self)
        self.clock = Clock(100.0)
        self.registry = mem_guard.Registry(self.root, runner=self.ps, kill=self.kill, clock=self.clock)

    def tearDown(self):
        ChildCase.tearDown(self)

    def test_register_unregister_resume_all_and_watch(self):
        pid = str(self.child.pid)
        self.assertEqual(self.run_cli(["register", "--pid", pid, "--label", "astra-review repo"], registry=self.registry), 0)
        self.assertEqual(self.run_cli(["register", "--pid", str(self.me), "--label", "x"], registry=self.registry), 1)
        self.assertIn("register failed", self.stderr.getvalue())
        self.assertEqual(self.run_cli(["watch", "--interval", "5", "--ticks", "4"], CRITICAL,
                                      registry=self.registry), 0)
        out = [json.loads(line) for line in self.stdout.getvalue().splitlines()]
        self.assertEqual([e["type"] for e in out], ["level", "notify", "pause", "resume"])
        self.assertEqual(out[-1]["why"], "shutdown", "watch resumes what it paused when it exits")
        self.assertTrue(continued(self.child.pid))
        self.registry.pause_all()
        self.assertEqual(self.run_cli(["resume-all"], registry=self.registry), 0)
        self.assertEqual(self.stdout.getvalue().strip(), "resumed 1 paused job")
        self.assertEqual(self.journal()[-1]["why"], "manual")
        self.assertEqual(self.run_cli(["resume-all"], registry=self.registry), 0)
        self.assertEqual(self.run_cli(["unregister", "--pid", pid], registry=self.registry), 0)
        self.assertEqual(self.stdout.getvalue().strip(), f"unregistered pid {pid}")
        self.assertEqual(self.registry.list_pausable(), [])


def hook_payload(tool, **tool_input):
    return json.dumps({"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": tool,
                       "tool_input": tool_input}).encode()


class HookTests(CliCase):
    TABLE = [
        (hook_payload("Bash", command="xcodebuild -scheme App test"), TIGHT, 2),
        (hook_payload("Bash", command="ls -la"), CRITICAL, 0),
        (b"{not json", CRITICAL, 0),
        (b"", CRITICAL, 0),
        (hook_payload("Workflow", script="x"), CRITICAL, 2),
        (hook_payload("Workflow", script="x"), TIGHT, 0),
        (hook_payload("Agent", prompt="x"), CRITICAL, 2),
        (hook_payload("Bash", command="xcodebuild test"), OK, 0),
        (hook_payload("Bash", command="lms load qwen3.8-27b"), WATCH, 0),
        (hook_payload("Bash", command="lms load qwen3.8-27b"), mem_state(1, 21), 2),
        (hook_payload("Bash", command="xcodebuild test"), UNKNOWN, 0),
        (hook_payload("mcp__Claude_Code_iOS_Simulator__control", action="launch", app_path="/x.app"), TIGHT, 2),
        (hook_payload("mcp__Claude_Code_iOS_Simulator__control", action="screenshot"), CRITICAL, 0),
        (hook_payload("mcp__Claude_Code_iOS_Simulator__build", scheme="App"), TIGHT, 2),
        (hook_payload("Read", file_path="/x"), CRITICAL, 0),
        (json.dumps(["Bash"]).encode(), CRITICAL, 0),
        (hook_payload("Bash", command="astra-review ."), CRITICAL, 2),
        (hook_payload("Bash", command="astra-review ."), TIGHT, 0),
    ]

    def test_table(self):
        for payload, state, code in self.TABLE:
            with self.subTest(payload=payload[:90], level=mem_guard.classify(state)[0]):
                self.assertEqual(self.run_cli(["hook"], state, stdin=payload), code)
                if code == 0:
                    self.assertEqual((self.stdout.getvalue(), self.stderr.getvalue()), ("", ""))

    def test_refusal_paragraph_names_level_consumers_and_what_to_do(self):
        self.assertEqual(self.run_cli(["hook"], TIGHT, stdin=hook_payload("Bash", command="cd ios && xcodebuild test")), 2)
        message = self.stderr.getvalue()
        self.assertEqual(message.count("\n"), 1, "one paragraph")
        self.assertIn("refused `xcodebuild test`", message)
        self.assertIn("memory is tight (macOS memory pressure: warning)", message)
        self.assertIn("iOS Simulators (iPhone 18 Pro, iPhone 17 Pro) about 4.5 GB", message)
        self.assertIn("What to do: Shut down unused iOS Simulators (`xcrun simctl shutdown all`)", message)
        refusal = self.journal()[-1]
        self.assertEqual((refusal["type"], refusal["via"], refusal["level"]), ("refusal", "hook", "tight"))

    def test_internal_errors_and_bad_input_fail_open(self):
        heavy = hook_payload("Bash", command="xcodebuild")
        self.assertEqual(self.run_cli(["hook"], FakeProbes(RuntimeError("probe exploded")), stdin=heavy), 0)
        self.assertEqual(self.run_cli(["hook"], FakeProbes(CRITICAL, None), stdin=heavy), 2, "no consumers still refuses")
        self.assertIn("Top consumers: unknown", self.stderr.getvalue())
        self.assertEqual(self.run_cli(["hook"], CRITICAL, stdin=b" " * (mem_guard.HOOK_MAX + 1)), 0)
        self.assertEqual(self.run_cli(["hook", "--bogus"], CRITICAL, stdin=heavy), 2, "extra args are ignored")
        self.assertEqual(self.run_cli(["hook"], CRITICAL, stdin=b"\xff\xfe"), 0)

    def test_command_matcher(self):
        cases = {
            "xcodebuild -scheme X build": "build", "cd ios && xcodebuild test": "build",
            "(cd ios; /usr/bin/xcodebuild)": "build", "npm ci": "build", "timeout 600 npm ci --silent": "build",
            "lms load gemma-4-26b --context-length 65536": "model", "~/.lmstudio/bin/lms load x": "model",
            "lms server start": "model", "ollama pull llama3": "model", "ollama run qwen": "model",
            "python3 -m mlx_lm.generate --model m": "model", "mlx_lm.server --port 8080": "model",
            "bash -lc 'lms load qwen'": "model", "FOO=1 sudo -E xcodebuild": "build",
            "xcrun simctl boot 'iPhone 17 Pro'": "simulator", "open -a Simulator": "simulator",
            "open -a /Applications/Xcode.app/Contents/Developer/Applications/Simulator.app": "simulator",
            "open -b com.apple.iphonesimulator": "simulator",
            "docker run --rm img": "docker", "docker compose up -d": "docker", "docker-compose up": "docker",
            "astra-review /repo": "review", "codex exec 'review this'": "review", "codex -m gpt exec x": "review",
            "echo hi; codex exec x": "review", "echo $(lms load x)": "model",
        }
        for command, category in cases.items():
            found = mem_guard.hook_match({"tool_name": "Bash", "tool_input": {"command": command}})
            self.assertEqual(found and found[1], category, command)
            self.assertTrue(any(s in command for s in mem_guard.HOOK_TRIGGER_SUBSTRINGS),
                            f"the bash prefilter must pass {command!r}")
        for command in ("ls -la", "grep -r xcodebuild .", "echo 'lms load x'", "lms ps", "lms unload x",
                        "xcrun simctl shutdown all", "xcrun simctl list", "docker ps", "npm install", "npm test",
                        "codex review", "ollama list", "open README.md", "cat Simulator.log", "git commit -m 'npm ci'",
                        "echo 'unbalanced", ""):
            self.assertIsNone(mem_guard.hook_match({"tool_name": "Bash", "tool_input": {"command": command}}),
                              command)
        for payload in ({"tool_name": "mcp__Claude_Code_iOS_Simulator__control", "tool_input": {"action": "launch"}},
                        {"tool_name": "mcp__Claude_Code_iOS_Simulator__build"}, {"tool_name": "Workflow"},
                        {"tool_name": "Agent"}):
            self.assertIsNotNone(mem_guard.hook_match(payload))
            self.assertTrue(any(s in json.dumps(payload) for s in mem_guard.HOOK_TRIGGER_SUBSTRINGS))
        self.assertEqual({k: v[1] for k, v in mem_guard.HOOK_NEEDS.items()},
                         {"model": "heavy", "simulator": "heavy", "build": "heavy", "docker": "heavy",
                          "review": "light", "agent": "light"})


# -------------------------------------------------------------------------------------------------
# Review regressions (2026-09-26 review of the first build). Each test reproduces one finding.


def spawn_sleeper(case):
    """Another real `/bin/sleep 60` child of this test process, killed in cleanup."""
    proc = subprocess.Popen(["/bin/sleep", "60"])

    def reap():
        proc.kill()
        proc.wait()
    case.addCleanup(reap)
    return proc


def not_stopped(pid, timeout=0.3):
    return not wait_status(pid, os.WUNTRACED, os.WIFSTOPPED, timeout)


def write_entry(root, pid, comm, start=START, **extra):
    directory = root / "pausable"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{pid}.json"
    path.write_text(json.dumps({"schemaVersion": 1, "pid": pid, "startTime": start, "comm": comm, "label": "x",
                                **extra}))
    os.chmod(path, 0o600)


def lstart_text(moment):
    """`ps -o lstart` as LC_ALL=C prints it (normalised to single spaces)."""
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return (f"{days[moment.tm_wday]} {months[moment.tm_mon - 1]} {moment.tm_mday} "
            f"{moment.tm_hour:02d}:{moment.tm_min:02d}:{moment.tm_sec:02d} {moment.tm_year}")


class TzPs(FakePs):
    """Like the real ps: lstart is printed in the TZ of the environment it runs in; without TZ, in
    the Mac's current zone (`offset` seconds from UTC)."""

    EPOCH = 1790000000

    def __init__(self, me, uid):
        super().__init__(me, uid)
        self.offset = 0
        self.tz = None

    def check_env(self, env):
        self.tz = env.get("TZ")

    def lstart(self, pid):
        if pid in (self.me, FAKE_PARENT):
            return self.procs[pid][2]
        offset = 0 if self.tz in ("UTC0", "UTC") else self.offset
        return lstart_text(time.gmtime(self.EPOCH + offset))


class IdentityReviewTests(ChildCase):
    def test_member_that_execs_before_sigstop_is_still_resumed(self):
        """[high] comm was read before SIGSTOP; a member that exec'd in between was never continued."""
        grandchild = spawn_sleeper(self)
        self.ps.add(grandchild.pid, self.child.pid, comm="/bin/bash")
        self.kill.real.add(grandchild.pid)
        self.registry.register(self.child.pid, "codex")
        forward = self.kill

        def kill(pid, sig):
            if sig == signal.SIGSTOP and pid == grandchild.pid:
                self.ps.add(grandchild.pid, self.child.pid, comm="/usr/local/bin/node")  # the exec lands here
            forward(pid, sig)
        self.registry._kill = kill
        self.registry.pause_all()
        self.assertTrue(stopped(grandchild.pid))
        resumed = self.registry.resume()
        self.assertEqual(set(resumed[0]["resumed"]), {self.child.pid, grandchild.pid})
        self.assertTrue(continued(grandchild.pid))
        self.assertEqual(self.registry.paused(), [])

    def test_time_zone_change_while_paused(self):
        """[medium] lstart was local time: a zone change made every paused job unrecognisable."""
        ps = TzPs(self.me, self.uid)
        ps.add(self.child.pid, self.me)
        registry = mem_guard.Registry(self.root, runner=ps, kill=self.kill, clock=self.clock)
        ps.offset = -7 * 3600  # America/Los_Angeles
        registry.register(self.child.pid, "job")
        registry.pause_all()
        self.assertTrue(stopped(self.child.pid))
        ps.offset = 9 * 3600  # Asia/Tokyo
        registry.resume()
        self.assertTrue(continued(self.child.pid))
        self.assertEqual(registry.paused(), [])
        self.assertEqual(ps.tz, "UTC0")

    def test_start_times_recorded_in_local_time_still_match(self):
        utc = lstart_text(time.gmtime(TzPs.EPOCH))
        local = lstart_text(time.localtime(TzPs.EPOCH))
        self.assertEqual(mem_guard._lstart_text(time.gmtime(TzPs.EPOCH)), utc)
        self.assertTrue(mem_guard.same_start(utc, utc))
        self.assertTrue(mem_guard.same_start(local, utc), "a record written before ps ran with TZ=UTC0")
        self.assertFalse(mem_guard.same_start(lstart_text(time.gmtime(TzPs.EPOCH + 1)), utc))
        self.assertFalse(mem_guard.same_start("garbage", utc))
        self.assertIsNone(mem_guard._start_epoch("Sat Foo 26 13:39:02 2026"))

    def test_member_with_an_empty_basename_is_resumed(self):
        """[low] a member whose comm cleaned to '' was written, dropped on read and never continued."""
        grandchild = spawn_sleeper(self)
        self.ps.add(grandchild.pid, self.child.pid, comm="tool/")
        self.kill.real.add(grandchild.pid)
        self.registry.register(self.child.pid, "job")
        self.registry.pause_all()
        self.assertTrue(stopped(grandchild.pid))
        members = [m["pid"] for m in self.registry.paused_jobs()[0]["members"]]
        self.assertIn(grandchild.pid, members, "what was stopped reads back")
        self.registry.resume()
        self.assertTrue(continued(grandchild.pid))

    def test_descendant_replaced_between_tree_and_identity_is_not_stopped(self):
        """[low] a descendant that exited after the tree walk, its pid reused by an unrelated process."""
        ps = self.ps
        self.ps.add(VIRTUAL, self.child.pid)
        self.registry.register(self.child.pid, "job")
        tree_call = ps.__class__.__call__

        def swapping(args, **kwargs):
            result = tree_call(ps, args, **kwargs)
            if args[:2] == ["/bin/ps", "-axo"]:
                ps.add(VIRTUAL, 1, comm="/System/Applications/Notes.app/Contents/MacOS/Notes",
                       start="Sat Sep 26 13:40:00 2026")
            return result
        self.registry._runner = swapping
        self.registry.pause_all()
        self.assertIn((self.child.pid, signal.SIGSTOP), self.kill.calls)
        self.assertNotIn((VIRTUAL, signal.SIGSTOP), self.kill.calls)


class RegistrationReviewTests(ChildCase):
    """[medium] any same-user pid could be registered (apps included) while a script could not
    register itself."""

    SCRIPT, SHELL, TERMINAL, APP, APP_CHILD, OTHER_JOB = 4_000_100, 4_000_101, 4_000_102, 4_000_200, 4_000_201, 4_000_300

    def setUp(self):
        super().setUp()
        ps = self.ps
        ps.procs[self.me] = (self.SCRIPT, self.uid, START, "/usr/bin/python3")  # mem-guard, run by the script
        ps.add(self.SCRIPT, self.SHELL, comm="/bin/bash")
        ps.add(self.SHELL, self.TERMINAL, comm="-zsh")
        ps.add(self.TERMINAL, 1, comm="/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal")
        ps.add(self.child.pid, self.SCRIPT)                       # the script's `$!`
        ps.add(self.APP, 1, comm="/Applications/ChatGPT.app/Contents/MacOS/ChatGPT")
        ps.add(self.APP_CHILD, self.SCRIPT, comm="/Applications/Foo.app/Contents/MacOS/Foo")
        ps.add(self.OTHER_JOB, self.SHELL, comm="/usr/bin/python3")

    def test_a_script_may_register_itself_and_what_it_started(self):
        self.assertEqual(self.registry.register(self.SCRIPT, "batch")["pid"], self.SCRIPT)
        self.assertEqual(self.registry.register(self.child.pid, "codex")["pid"], self.child.pid)

    def test_apps_and_unrelated_processes_are_refused(self):
        for pid, why in ((self.APP, "not started by the calling script"), (self.APP_CHILD, "is an app"),
                         (self.OTHER_JOB, "not started by the calling script"),
                         (self.SHELL, "not started by the calling script"),
                         (self.TERMINAL, "not started by the calling script")):
            with self.subTest(pid=pid), self.assertRaises(mem_guard.RegistryError) as caught:
                self.registry.register(pid, "x")
            self.assertIn(why, str(caught.exception))
        self.assertEqual(self.registry.list_pausable(), [])

    def test_a_caller_started_by_launchd_registers_nothing(self):
        self.ps.procs[self.me] = (1, self.uid, START, "/usr/bin/python3")
        with self.assertRaises(mem_guard.RegistryError):
            self.registry.register(self.APP, "x")


class ResumeReviewTests(ChildCase):
    def test_failed_sigcont_keeps_the_record(self):
        """[low] a member whose SIGCONT failed (EPERM) was dropped from paused.json while stopped."""
        self.registry.register(self.child.pid, "job")
        self.registry.pause_all()
        self.assertTrue(stopped(self.child.pid))
        refuse = {"on": True}

        def kill(pid, sig):
            if sig == signal.SIGCONT and refuse["on"]:
                raise PermissionError(errno.EPERM, "Operation not permitted")
            self.kill(pid, sig)
        sandboxed = mem_guard.Registry(self.root, runner=self.ps, kill=kill, clock=self.clock)
        result = sandboxed.resume()
        self.assertEqual([job["members"] for job in self.registry.paused()], [1], "still recorded")
        self.assertEqual((result[0]["resumed"], result[0]["failed"]), ([], [self.child.pid]))
        out, err = io.StringIO(), io.StringIO()
        ctx = mem_guard.Context(state_root=self.root, cfg=mem_guard.default_config(), probes=FakeProbes(OK),
                                registry=sandboxed, notifier=Notes(), clock=self.clock, gpu=lambda: None,
                                stdout=out, stderr=err)
        self.assertEqual(mem_guard.main(["resume-all"], ctx), 1)
        self.assertIn("could not continue 1 process", err.getvalue())
        refuse["on"] = False
        self.assertEqual(sandboxed.resume()[0]["resumed"], [self.child.pid])
        self.assertTrue(continued(self.child.pid))
        self.assertEqual(self.registry.paused(), [])

    def test_every_stopped_pid_is_in_the_record_that_reads_back(self):
        """[low] members could number 513 (read limit 512) and paused.json could outgrow its 256 KiB
        read limit: stopped processes were then missing from what resume reads."""
        roots = (4_100_000, 4_200_000)
        comm = "c" * 250
        virtual = set()
        for root in roots:
            self.ps.add(root, self.me, comm="/x/" + comm)
            virtual.add(root)
            for offset in range(1, 601):
                self.ps.add(root + offset, root, comm="/x/" + comm)
                virtual.add(root + offset)
            write_entry(self.root, root, comm)
        kill = SafeKill([], virtual)
        registry = mem_guard.Registry(self.root, runner=self.ps, kill=kill, clock=self.clock)
        jobs = registry.pause_all()
        sent = {pid for pid, sig in kill.calls if sig == signal.SIGSTOP}
        recorded = {m["pid"] for job in registry.paused_jobs() for m in job["members"]}
        self.assertTrue(sent)
        self.assertLessEqual(sent, recorded, "nothing is stopped that resume cannot read back")
        self.assertEqual(len(jobs), 1, "the second job would not fit paused.json: it is not paused at all")
        self.assertLessEqual(len(jobs[0]["members"]), 512)
        self.assertLessEqual(os.path.getsize(self.root / "paused.json"), 256 << 10)

    def test_a_short_write_never_installs_a_truncated_record(self):
        """[low] os.write's count was ignored: a half-written paused.json was installed, then SIGSTOP."""
        self.registry.register(self.child.pid, "job")
        real_write = os.write
        target = {}

        def disk_full(fd, data):
            data = bytes(data)
            if data.startswith(b'{"jobs"') and not target:
                target["fd"] = fd
                return real_write(fd, data[: len(data) // 2])
            if target.get("fd") == fd:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)
        with mock.patch.object(mem_guard.os, "write", disk_full):
            with self.assertRaises(OSError):
                self.registry.pause_all()
        self.assertEqual(self.kill.calls, [], "nothing is stopped when the record cannot be written")
        self.assertFalse((self.root / "paused.json").exists())

        def halves(fd, data):
            data = bytes(data)
            return real_write(fd, data[: max(1, len(data) // 2)])
        with mock.patch.object(mem_guard.os, "write", halves):
            self.registry.pause_all()
        self.assertTrue(stopped(self.child.pid))
        self.assertEqual([job["pid"] for job in self.registry.paused_jobs()], [self.child.pid])

    def test_symlinked_pausable_dir_is_never_listed_or_cleaned(self):
        """[low] list_pausable followed a symlinked pausable/ and deleted '<digits>.json' files there."""
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "2024.json").write_text("not an entry")
        self.root.mkdir(mode=0o700)
        os.symlink(elsewhere, self.root / "pausable")
        self.assertEqual(self.registry.list_pausable(), [])
        self.assertFalse(self.registry.has_entries())
        self.assertFalse(self.registry.unregister(2024))
        self.assertEqual(self.registry.pause_all(), [])
        self.assertTrue((elsewhere / "2024.json").exists())
        with self.assertRaises(mem_guard.RegistryError):
            self.registry.register(self.child.pid, "job")

    def test_deeply_nested_state_files_read_as_missing(self):
        self.root.mkdir(mode=0o700)
        path = self.root / "paused.json"
        path.write_text("[" * 5000 + "]" * 5000)
        os.chmod(path, 0o600)
        self.assertEqual(self.registry.paused(), [])


class CloseReviewTests(ChildCase):
    def test_a_pause_racing_close_never_lands(self):
        """[medium] close() gave up waiting on the tick and returned; the in-flight pause then stopped
        the job after the monitor had 'resumed everything'."""
        self.registry.register(self.child.pid, "job")
        entered, gate = threading.Event(), threading.Event()
        runner = self.registry._runner

        def slow_ps(args, **kwargs):
            if args[:2] == ["/bin/ps", "-axo"]:
                entered.set()
                gate.wait(5)
            return runner(args, **kwargs)
        self.registry._runner = slow_ps
        guard = mem_guard.Guard(cfg=mem_guard.default_config(), probes=FakeProbes(CRITICAL), registry=self.registry,
                                notifier=Notes(), clock=self.clock)
        worker = threading.Thread(target=self.registry.pause_all)
        worker.start()
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        guard.close()
        self.assertLess(time.monotonic() - started, 0.5)
        gate.set()
        worker.join(5)
        self.assertTrue(not_stopped(self.child.pid))
        self.assertNotIn((self.child.pid, signal.SIGSTOP), self.kill.calls)
        self.assertEqual(self.registry.paused(), [])

    def test_a_pause_already_writing_its_record_is_resumed_by_close(self):
        """The other side of the race: a pause past its last check, still writing paused.json,
        must be seen (and resumed) by close, not missed because the file was not there yet."""
        self.registry.register(self.child.pid, "job")
        entered, gate = threading.Event(), threading.Event()
        write = self.registry._write_paused

        def slow_write(jobs):
            entered.set()
            gate.wait(5)
            write(jobs)
        self.registry._write_paused = slow_write
        guard = mem_guard.Guard(cfg=mem_guard.default_config(), probes=FakeProbes(CRITICAL), registry=self.registry,
                                notifier=Notes(), clock=self.clock)
        worker = threading.Thread(target=self.registry.pause_all)
        worker.start()
        self.assertTrue(entered.wait(2))
        closed = []
        closer = threading.Thread(target=lambda: closed.append(guard.close()))
        closer.start()
        time.sleep(0.1)
        gate.set()
        worker.join(5)
        closer.join(5)
        self.assertTrue(continued(self.child.pid))
        self.assertEqual([event["why"] for event in closed[0]], ["shutdown"])
        self.assertEqual(self.registry.paused(), [])

    def test_a_slow_notifier_never_blocks_the_sampler_or_close(self):
        """[medium] osascript (5 s timeout) ran inside the tick on the sampler thread."""
        release = threading.Event()
        self.addCleanup(release.set)
        guard = mem_guard.Guard(cfg=mem_guard.default_config(), probes=FakeProbes(CRITICAL, CONSUMERS),
                                registry=self.registry, notifier=lambda title, body: release.wait(5), clock=self.clock)
        started = time.monotonic()
        block, _source = guard.sample()
        self.assertEqual(block["level"], "critical")
        self.assertLess(time.monotonic() - started, 0.5)
        started = time.monotonic()
        guard.close()
        self.assertLess(time.monotonic() - started, 0.5)


class PauseLimitReviewTests(CliCase, ChildCase):
    """[medium] only a live watchdog enforced the 20-minute limit: a pauser that died left the job
    stopped until someone ran resume-all."""

    def setUp(self):
        ChildCase.setUp(self)
        self.clock = Clock(100.0)
        self.registry = mem_guard.Registry(self.root, runner=self.ps, kill=self.kill, clock=self.clock)

    def tearDown(self):
        ChildCase.tearDown(self)

    def pause_with_a_watchdog_that_then_dies(self):
        self.registry.register(self.child.pid, "astra-review repo")
        dog = mem_guard.Watchdog(self.clock, None, Notes(), self.registry, None)
        dog.tick(CRITICAL)
        self.clock.advance(10)
        dog.tick(CRITICAL)
        self.assertTrue(stopped(self.child.pid))

    def test_status_resumes_a_job_past_its_limit(self):
        self.pause_with_a_watchdog_that_then_dies()
        self.clock.advance(25 * 60)
        self.assertEqual(self.run_cli(["status"], CRITICAL, registry=self.registry), 11)
        self.assertTrue(continued(self.child.pid))
        self.assertEqual(self.registry.paused(), [])
        self.assertEqual(self.journal()[-1]["why"], "overdue")

    def test_hook_slow_path_resumes_a_job_past_its_limit(self):
        self.pause_with_a_watchdog_that_then_dies()
        self.clock.advance(25 * 60)
        self.assertEqual(self.run_cli(["hook"], TIGHT, registry=self.registry,
                                      stdin=hook_payload("Bash", command="xcodebuild -scheme App build")), 2)
        self.assertTrue(continued(self.child.pid))

    def test_a_gone_pauser_is_resumed_by_any_cli_call(self):
        pauser_pid = 4_000_700
        self.ps.add(pauser_pid, FAKE_PARENT, comm="/usr/bin/python3")
        pauser = mem_guard.Registry(self.root, runner=self.ps, kill=self.kill, clock=self.clock, pid=pauser_pid)
        self.registry.register(self.child.pid, "job")
        pauser.pause_all()
        self.assertTrue(stopped(self.child.pid))
        self.assertEqual(self.run_cli(["admit", "--need-gb", "1"], OK, registry=self.registry), 0)
        self.assertEqual(len(self.registry.paused()), 1, "the pauser is alive and the limit not reached")
        del self.ps.procs[pauser_pid]
        self.assertEqual(self.run_cli(["status"], OK, registry=self.registry), 0)
        self.assertTrue(continued(self.child.pid))
        self.assertEqual(self.registry.paused(), [])

    def test_resume_overdue_command_for_scripts_that_wait_on_a_job(self):
        self.pause_with_a_watchdog_that_then_dies()
        self.assertEqual(self.run_cli(["resume-overdue"], OK, registry=self.registry), 0)
        self.assertEqual(self.stdout.getvalue().strip(), "resumed 0 overdue paused jobs")
        self.assertEqual(len(self.registry.paused()), 1)
        self.assertEqual(self.registry.paused_jobs()[0]["resumeBy"], 110.0 + 1200.0)
        self.clock.advance(1200)
        self.assertEqual(self.run_cli(["resume-overdue"], OK, registry=self.registry), 0)
        self.assertEqual(self.stdout.getvalue().strip(), "resumed 1 overdue paused job")
        self.assertTrue(continued(self.child.pid))


class ProbeCostReviewTests(unittest.TestCase):
    """[medium] each 1 Hz tick regrouped the whole process table and waited on ps/simctl/vm_stat."""

    ROWS = "".join(f" {1000 + i} 1024 /usr/libexec/helper{i}\n" for i in range(1500))

    def test_consumers_group_the_process_table_once_per_ps_read(self):
        table = {"/bin/ps -axo pid=,rss=,comm=": completed(self.ROWS)}
        clock = Clock()
        probes = mem_guard.Probes(sysctl=ProbeTests.SYSCTL.get, runner=Runner(table), statvfs=ProbeTests.StatVfs(),
                                  clock=clock, wall=lambda: 1.0)
        counted = []
        real = mem_guard.group_of
        with mock.patch.object(mem_guard, "group_of", side_effect=lambda comm: counted.append(1) or real(comm)):
            for _ in range(9):
                rows = probes.consumers(models=[{"id": "m", "loaded": True}], gpu_alloc_bytes=40 * GiB)
                clock.advance(1)
        self.assertLessEqual(len(counted), 1500, "one grouping for one ps read")
        self.assertEqual(len(rows), 3)

    def test_background_probes_never_wait_on_a_subprocess(self):
        answers = {"/usr/bin/vm_stat": completed(VM_STAT_INCIDENT),
                   "/bin/ps -axo pid=,rss=,comm=": completed(PS_SAMPLE),
                   "/usr/bin/xcrun simctl list devices booted -j": completed(SIMCTL_JSON)}
        threads = []

        def slow(args, **kwargs):
            threads.append(threading.current_thread())
            time.sleep(0.2)
            return answers[" ".join(args)]
        probes = mem_guard.Probes(sysctl=ProbeTests.SYSCTL.get, runner=slow, statvfs=ProbeTests.StatVfs(),
                                  clock=Clock(), wall=lambda: 1.0, background=True)
        started = time.monotonic()
        state = probes.read()
        self.assertIsNone(probes.consumers())
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertIsNone(state["compressedBytes"], "not read yet")
        self.assertTrue(probes.settle(3))
        self.assertEqual(probes.read()["compressedBytes"], 1595121 * 16384)
        probes.consumers()
        self.assertTrue(probes.settle(3))
        self.assertEqual(probes.consumers()[0]["devices"], ["iPhone 18 Pro", "iPhone 17 Pro"])
        self.assertTrue(threads)
        self.assertNotIn(threading.current_thread(), threads)


class ConfigReviewTests(unittest.TestCase):
    def test_deeply_nested_config_is_ignored_not_a_crash(self):
        """[low] Python 3.9's json raised RecursionError out of load_config."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mem-guard.json"
            path.write_text('{"floor_gib": ' + "[" * 3000 + "]" * 3000 + "}")
            os.chmod(path, 0o600)
            cfg = mem_guard.load_config(path)
            self.assertEqual(cfg["floor_gib"], 4.0)
            self.assertTrue(cfg["notes"], "the file is reported, whichever way it failed")


class HookReviewTests(CliCase):
    def match(self, command):
        found = mem_guard.hook_match({"tool_name": "Bash", "tool_input": {"command": command}})
        return found and found[1]

    def test_comments_hide_nothing_after_their_line(self):
        """[medium] newlines became ' ; ' before shlex, so a '#' swallowed the rest of the command."""
        cases = {"# Build the app\nxcodebuild -scheme Foo build": "build",
                 "cd ~/proj  # go there\nlms load qwen3.8-27b": "model",
                 "# boot the phone\nxcrun simctl boot 'iPhone 17 Pro'": "simulator",
                 "echo $#; xcodebuild -scheme App build": "build",
                 "[ ${#x} -gt 0 ] && xcodebuild -scheme App build": "build",
                 "curl -s localhost:1234/v1/models#x; lms load q": "model",
                 "echo a#b; xcodebuild build": "build",
                 "echo '# not a comment' && npm ci": "build",
                 "xcodebuild \\\n  -scheme App build": "build"}
        for command, category in cases.items():
            self.assertEqual(self.match(command), category, command)
        for command in ("# xcodebuild build", "ls  # then npm ci", "echo hi # lms load x"):
            self.assertIsNone(self.match(command), command)
        for command in ("# Build the app\nxcodebuild -scheme Foo build", "cd ~/proj  # go there\nnpm ci"):
            self.assertEqual(self.run_cli(["hook"], CRITICAL, stdin=hook_payload("Bash", command=command)), 2)

    def test_heredoc_bodies_are_not_commands(self):
        """[medium/low] heredoc bodies were lexed as commands: writing a script that mentions npm ci
        was refused as a build."""
        for command in ("cat > ci.sh <<'EOF'\nset -e\nnpm ci\nEOF",
                        "cat > BUILDING.md <<'EOF'\nxcodebuild -scheme Foo\nnpm ci\nEOF",
                        "git commit -F - <<EOF\nnpm ci: don't run it here\nEOF",
                        "cat <<-EOF > x\n\tlms load big\n\tEOF\nls",
                        'python3 - <<"PY"\nprint("xcodebuild build")\nPY'):
            self.assertIsNone(self.match(command), command)
            self.assertEqual(self.run_cli(["hook"], TIGHT, stdin=hook_payload("Bash", command=command)), 0)
        self.assertEqual(self.match("python3 - <<'EOF'\nprint('hi')\nEOF\nxcodebuild -scheme X build"), "build")
        self.assertEqual(self.match("cat <<EOF\nbody\nEOF\nnpm ci"), "build")
        self.assertEqual(self.match("cat <<< 'here string'; npm ci"), "build")

    def test_wrapper_option_values_are_skipped(self):
        """[low] `nice -n 10 xcodebuild` took '10' as the command name."""
        for command in ("nice -n 10 xcodebuild build", "timeout -s KILL 600 xcodebuild build",
                        "sudo -u louis xcodebuild build", "env -u FOO npm ci", "caffeinate -t 60 xcodebuild build",
                        "xargs -n 1 lms load", "nice -n 5 -- npm ci"):
            self.assertIsNotNone(self.match(command), command)

    def test_information_only_commands_are_not_heavy(self):
        """[low] xcodebuild -version / -list and --help invocations were refused as 4 GB builds."""
        for command in ("xcodebuild -version", "xcodebuild -list -project A.xcodeproj", "xcodebuild -showsdks",
                        "xcodebuild -showBuildSettings -scheme X", "lms load --help", "docker run --help",
                        "codex exec --help", "npm ci -h"):
            self.assertIsNone(self.match(command), command)
        for command in ("xcodebuild -scheme X build", "docker run -h myhost img", "xcodebuild test"):
            self.assertIsNotNone(self.match(command), command)

    def test_refusal_names_the_llm_server_by_its_gpu_allocation(self):
        """[low] the hook ranked the LLM server by RSS alone, so its 40 GB of GPU memory was missing."""
        ps_text = (f"  612 {52 << 20} {SIM}/usr/libexec/backboardd\n"
                   f"  982 {800 << 10} /Applications/LM Studio.app/Contents/MacOS/LM Studio\n"
                   f" 1301 {3600 << 10} /Applications/Codex.app/Contents/MacOS/Codex\n"
                   f" 1201 {3500 << 10} /Applications/Claude.app/Contents/MacOS/Claude\n")
        table = {"/usr/bin/vm_stat": completed(VM_STAT_INCIDENT), "/bin/ps -axo pid=,rss=,comm=": completed(ps_text),
                 "/usr/bin/xcrun simctl list devices booted -j": completed(SIMCTL_JSON)}
        probes = mem_guard.Probes(sysctl=ProbeTests.SYSCTL.get, runner=Runner(table), statvfs=ProbeTests.StatVfs(),
                                  clock=Clock(), wall=lambda: 1.0)
        err = io.StringIO()
        ctx = mem_guard.Context(state_root=self.root, cfg=mem_guard.default_config(), probes=probes, notifier=Notes(),
                                clock=self.clock, gpu=lambda: 40 * GiB,
                                stdin=io.BytesIO(hook_payload("Bash", command="xcodebuild -scheme App build")),
                                stdout=io.StringIO(), stderr=err)
        self.assertEqual(mem_guard.main(["hook"], ctx), 2)
        self.assertIn("Local LLM server about 0.8 GB (+ GPU allocation 40.0 GB)", err.getvalue())


if __name__ == "__main__":
    unittest.main()
