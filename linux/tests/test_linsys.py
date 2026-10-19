"""Linux observations from fixture trees, without probing the host or node."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_linux import linsys


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def put(self, path, text):
        target = self.root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def link(self, path, target):
        link = self.root / path.lstrip("/")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        return link

    def gpu(self, card="card7", vendor="0x1002", driver="amdgpu"):
        device = f"/sys/devices/pci0000:00/0000:01:00.0/{card}"
        self.put(device + "/vendor", vendor + "\n")
        self.put(device + "/device", "0x67df\n")
        driver_path = self.root / "sys/bus/pci/drivers" / driver
        driver_path.mkdir(parents=True, exist_ok=True)
        self.link(device + "/driver", driver_path)
        self.link(f"/sys/class/drm/{card}/device", self.root / device.lstrip("/"))
        return device

    def process(self, pid, name, args=(), exe=None, uid=None, start=1234):
        self.put(f"/proc/{pid}/status", f"Name:\t{name}\nUid:\t{os.getuid() if uid is None else uid} 0 0 0\nVmRSS:\t1024 kB\n")
        self.put(f"/proc/{pid}/stat", f"{pid} ({name}) S " + "0 " * 18 + f"{start} 0 0\n")
        self.put(f"/proc/{pid}/cmdline", "\0".join((exe or name, *args)) + "\0")
        self.link(f"/proc/{pid}/exe", exe or "/usr/bin/" + name)


class SafeReadTest(Fixture):
    def test_bounded_regular_read_and_json(self):
        path = self.put("state/item.json", '{"state":"recorded"}')
        self.assertEqual(linsys.read_json(path), {"state": "recorded"})
        self.assertIsNone(linsys.read_text(path, 3))
        self.assertIsNone(linsys.read_text(path.parent))
        self.assertIsNone(linsys.read_text(path.parent / "missing"))

    def test_final_symlink_refused(self):
        target = self.put("data", "fixture")
        link = self.link("state/item", target)
        self.assertIsNone(linsys.read_text(link))

    def test_parent_symlink_refused(self):
        target = self.put("data/item", "fixture")
        self.link("state", target.parent)
        self.assertIsNone(linsys.read_text(self.root / "state/item"))

    def test_parent_directory_swap_cannot_redirect_read(self):
        path = self.put("state/item", "original")
        outside = self.put("outside/item", "redirected")
        original_open = os.open
        changed = False

        def swap(name, flags, *args, **kwargs):
            nonlocal changed
            descriptor = original_open(name, flags, *args, **kwargs)
            if name == "state" and kwargs.get("dir_fd") is not None and not changed:
                changed = True
                path.parent.rename(self.root / "old")
                path.parent.symlink_to(outside.parent)
            return descriptor

        with patch.object(linsys.os, "open", side_effect=swap):
            self.assertEqual(linsys.read_text(path), "original")

    def test_fifo_is_refused_without_blocking(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        self.assertIsNone(linsys.read_text(fifo))

    def test_invalid_json_and_nonfinite_refused(self):
        for value in ("{", '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}', "[" * 2000 + "]" * 2000):
            with self.subTest(value=value[:20]):
                self.assertIsNone(linsys.read_json(self.put("item.json", value)))

    def test_read_cap_bounds(self):
        for value in (0, -1, 2 ** 20 + 1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                linsys.read_text(self.root / "item", value)


class GpuTest(Fixture):
    def test_dynamic_card_and_instant_sources(self):
        device = self.gpu()
        values = {"gpu_busy_percent": "0", "mem_info_vram_used": "364773376", "mem_info_vram_total": "8589934592",
                  "hwmon/hwmon19/temp1_input": "40000", "hwmon/hwmon19/power1_input": "37006000", "hwmon/hwmon19/fan1_input": "902"}
        for file, value in values.items():
            self.put(device + "/" + file, value)
        rows = linsys.read_gpus(self.root)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["index"], 7)
        self.assertEqual(row["name"], "AMD GPU (1002:67df)")
        self.assertEqual(row["utilizationPercent"], 0)
        self.assertEqual(row["utilizationKind"], "instant")
        self.assertEqual(row["memoryUsedMiB"], 347.875)
        self.assertEqual(row["memoryTotalMiB"], 8192)
        self.assertEqual(row["temperatureC"], 40)
        self.assertEqual(row["powerW"], 37.006)
        self.assertEqual(row["fanRpm"], 902)
        self.assertTrue(row["sources"]["powerW"].endswith("power1_input"))

    def test_requires_both_vendor_and_driver(self):
        self.gpu("card1", vendor="0x10de")
        self.gpu("card2", driver="radeon")
        self.assertEqual(linsys.read_gpus(self.root), [])

    def test_missing_fields_remain_unknown(self):
        self.gpu()
        row = linsys.read_gpus(self.root)[0]
        for field in ("utilizationPercent", "memoryUsedMiB", "memoryTotalMiB", "temperatureC", "powerW"):
            self.assertIsNone(row[field])

    def test_invalid_and_inconsistent_values(self):
        device = self.gpu()
        self.put(device + "/gpu_busy_percent", "101")
        self.put(device + "/mem_info_vram_used", "12345")
        self.put(device + "/mem_info_vram_total", "1024")
        row = linsys.read_gpus(self.root)[0]
        self.assertIsNone(row["utilizationPercent"])
        self.assertIsNone(row["memoryUsedMiB"])

    def test_sysfs_link_cannot_escape_sys_tree(self):
        device = self.gpu()
        outside = self.put("outside", "50")
        self.link(device + "/gpu_busy_percent", outside)
        self.assertIsNone(linsys.read_gpus(self.root)[0]["utilizationPercent"])

    def test_zero_vram_total_is_unknown(self):
        device = self.gpu()
        self.put(device + "/mem_info_vram_total", "0")
        self.assertIsNone(linsys.read_gpus(self.root)[0]["memoryTotalMiB"])


class MemoryTest(Fixture):
    def memory(self, available=300, extra=""):
        self.put("/proc/meminfo", f"MemTotal: 1000 kB\nMemAvailable: {available} kB\nSwapTotal: 100 kB\nSwapFree: 80 kB\n" + extra)
        return linsys.read_memory(self.root)

    def test_memory_threshold_parity(self):
        for available, expected in ((200, "ok"), (120, "watch"), (60, "tight"), (0, "critical")):
            with self.subTest(available=available):
                row = self.memory(available)
                self.assertEqual(row["level"], expected)
                self.assertEqual(row["swapUsedBytes"], 20 * 1024)

    def test_commit_corrobates_pressure(self):
        self.assertEqual(self.memory(extra="CommitLimit: 1000 kB\nCommitted_AS: 950 kB\n")["level"], "tight")

    def test_psi_escalates_only(self):
        for some, full, expected in ((1, 0, "watch"), (10, 0, "tight"), (0, 1, "tight"), (0, 10, "critical")):
            with self.subTest(some=some, full=full):
                self.put("/proc/pressure/memory", f"some avg10={some} avg60=0.00\nfull avg10={full} avg60=0.00\n")
                self.assertEqual(self.memory()["level"], expected)
                self.assertEqual(self.memory(0)["level"], "critical")

    def test_missing_memory_never_zero_or_ok(self):
        row = linsys.read_memory(self.root)
        self.assertEqual(row["level"], "unknown")
        self.assertIsNone(row["availablePercent"])
        self.assertIsNone(row["swapUsedBytes"])
        self.assertIsNone(row["psi"]["someAvg10"])

    def test_invalid_memory_and_psi(self):
        self.put("/proc/pressure/memory", "some avg10=NaN\nfull avg10=101")
        row = self.memory(1001)
        self.assertEqual(row["level"], "unknown")
        self.assertIsNone(row["psi"]["someAvg10"])
        self.assertIsNone(row["psi"]["fullAvg10"])

    def test_missing_psi_does_not_erase_ram(self):
        self.assertEqual(self.memory()["level"], "ok")

    def test_oversize_integer_is_unknown(self):
        self.put("/proc/meminfo", "MemTotal: " + "9" * 5000 + " kB\nMemAvailable: 123 kB\n")
        self.assertEqual(linsys.read_memory(self.root)["level"], "unknown")


class CpuBootTest(Fixture):
    def test_cpu_requires_two_polls_and_excludes_guest_double_count(self):
        self.put("/proc/stat", "cpu 10 0 10 80 0 0 0 0 10 0\ncpu0 10 0 10 80\n")
        before = linsys.read_cpu(self.root)
        self.assertIsNone(before["utilizationPercent"])
        self.put("/proc/stat", "cpu 30 0 30 140 0 0 0 0 20 0\ncpu0 30 0 30 140\n")
        after = linsys.read_cpu(self.root, before)
        self.assertEqual(after["utilizationPercent"], 40)
        self.assertEqual(after["logicalCpus"], 1)

    def test_cpu_counter_reset_stays_unknown(self):
        self.put("/proc/stat", "cpu 10 0 0 80\n")
        before = {"sample": {"total": 1000, "idle": 900}}
        self.assertIsNone(linsys.read_cpu(self.root, before)["utilizationPercent"])

    def test_cpu_boot_change_never_inherits_sample(self):
        self.put("/proc/stat", "cpu 10 0 0 80\n")
        self.put("/proc/sys/kernel/random/boot_id", "before")
        before = linsys.read_cpu(self.root)
        self.put("/proc/stat", "cpu 20 0 0 160\n")
        self.put("/proc/sys/kernel/random/boot_id", "after")
        self.assertIsNone(linsys.read_cpu(self.root, before)["utilizationPercent"])

    def test_missing_cpu(self):
        row = linsys.read_cpu(self.root)
        self.assertIsNone(row["sample"])
        self.assertIsNone(row["logicalCpus"])
        self.assertIsNone(row["loadAverage"])

    def test_load_averages_and_cpu_pressure(self):
        self.put("/proc/loadavg", "0.10 0.20 0.30 1/50 100\n")
        self.put("/proc/pressure/cpu", "some avg10=2.50 avg60=0.00")
        row = linsys.read_cpu(self.root)
        self.assertEqual(row["loadAverage"], [.1, .2, .3])
        self.assertEqual(row["pressure"]["someAvg10"], 2.5)

    def test_boot_identity_start_uptime(self):
        self.put("/proc/sys/kernel/random/boot_id", "11111111-2222-4333-8444-555555555555\n")
        self.put("/proc/stat", "btime 12345678\n")
        self.put("/proc/uptime", "120.5 200.0\n")
        row = linsys.read_boot(self.root)
        self.assertEqual(row["bootId"], "11111111-2222-4333-8444-555555555555")
        self.assertEqual(row["startedUnix"], 12345678)
        self.assertEqual(row["uptimeSeconds"], 120.5)

    def test_invalid_boot_remains_unknown(self):
        self.put("/proc/sys/kernel/random/boot_id", "not a uuid")
        row = linsys.read_boot(self.root)
        self.assertIsNone(row["bootId"])
        self.assertIsNone(row["startedUnix"])


class TemperatureDiskTest(Fixture):
    def test_known_chips_labels_and_unknown_sensor(self):
        for name, chip, value in (("hwmon0", "nvme", "33900"), ("hwmon7", "coretemp", "bad")):
            self.put(f"/sys/class/hwmon/{name}/name", chip)
            self.put(f"/sys/class/hwmon/{name}/temp1_input", value)
            self.put(f"/sys/class/hwmon/{name}/temp1_label", "Package id 0")
        rows = linsys.read_temperatures(self.root)
        self.assertEqual([r["temperatureC"] for r in rows], [33.9, None])
        self.assertEqual(rows[0]["label"], "Package id 0")

    def test_disk_mount_table_required(self):
        (self.root / "srv/storage").mkdir(parents=True)
        with patch.object(linsys.os, "statvfs") as read:
            rows = linsys.read_disks(self.root)
        read.assert_not_called()
        self.assertTrue(all(row["state"] == "unknown" for row in rows))

    def test_only_mounted_disk_reads_statvfs(self):
        self.put("/proc/self/mountinfo", "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n2 0 8:2 / /srv/storage rw - ext4 /dev/sdb1 rw\n")
        fake = os.statvfs_result((4096, 4096, 1000, 400, 300, 100, 90, 90, 0, 255))
        with patch.object(linsys.os, "statvfs", return_value=fake) as read:
            rows = linsys.read_disks(self.root)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(rows[0]["availableBytes"], 300 * 4096)
        self.assertEqual(rows[1]["usedBytes"], 600 * 4096)
        self.assertEqual(rows[2]["reason"], "not-mounted")

    def test_statvfs_failure_is_unknown(self):
        self.put("/proc/self/mountinfo", "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n")
        with patch.object(linsys.os, "statvfs", side_effect=PermissionError):
            self.assertEqual(linsys.read_disks(self.root)[0]["reason"], "unreadable")


class ProcessTest(Fixture):
    def test_missing_proc_is_unknown(self):
        self.assertIsNone(linsys.read_processes(self.root))

    def test_wrapper_classification_flags_and_no_arguments(self):
        self.process(101, "llama-server", ("--port", "41234", "--host", "127.0.0.1", "-ngl", "99", "--api-key", "PRIVATE-FIXTURE",
                                         "--prompt", "DO-NOT-OUTPUT"), linsys.GPU_EXECUTABLE)
        row = linsys.read_processes(self.root)[0]
        self.assertEqual(row["kind"], "gpu-wrapper")
        self.assertEqual(row["port"], 41234)
        self.assertEqual(row["flags"]["gpuLayers"], 99)
        self.assertNotIn("PRIVATE-FIXTURE", json.dumps(row))
        self.assertNotIn("DO-NOT-OUTPUT", json.dumps(row))
        self.assertEqual(row["workingSetBytes"], 1024 * 1024)

    def test_pair_router_classification_needs_mapped_model_dir(self):
        self.process(101, "llama-server", ("--port", "8081", "--models-dir", linsys.PAIR_MODELS), "/opt/pair/llama-server")
        self.process(102, "llama-server", ("--port", "8081", "--models-dir", "/unrelated"), "/opt/other/llama-server")
        self.assertEqual([r["kind"] for r in linsys.read_processes(self.root)], ["pair-router", "unknown"])

    def test_coordinator_requires_mapped_script(self):
        self.process(101, "python3", ("/home/user/.local/share/agiw-runtimes/linux-scheduler-authority-v7/coordinator.py", "serve"), "/usr/bin/python3")
        self.process(102, "python3", ("other.py", "--prompt", "coordinator.py"), "/usr/bin/python3")
        rows = linsys.read_processes(self.root)
        self.assertTrue(rows[0]["coordinator"])
        self.assertEqual(rows[1]["kind"], "unknown")

    def test_coordinator_identity_is_bounded_and_accounts_for_retired_process(self):
        from agiw_linux import probes
        from test_coordinator_units import ABSENT, V7_RUNTIME, V7_SHA, V8_RUNTIME, V8_SHA, unit
        for runtime, release in ((V7_RUNTIME, V7_SHA), (V8_RUNTIME, V8_SHA)):
            with self.subTest(runtime=runtime):
                active = unit(active="active", sub="running", runtime=runtime, release=release)
                command = active["ExecStart"].split("argv[]=", 1)[1].split(" ;", 1)[0].split()
                (self.root / "proc/101/exe").unlink(missing_ok=True)
                self.process(101, "python3", command[1:], "/usr/bin/python3")
                rows = linsys.read_processes(self.root)
                self.assertEqual(rows[0].get("coordinatorRuntime"), runtime)
                self.assertEqual(rows[0].get("coordinatorReleaseSha256"), release)
                self.assertNotIn("/home/fixture", json.dumps(rows))
                result = probes.coordinator_status(rows, units={"user": active, "system": ABSENT})
                self.assertEqual(result["state"], "absent" if runtime == V7_RUNTIME else "unknown")

    def test_coordinator_identity_rejects_ambiguous_arguments_and_pid_reuse(self):
        from test_coordinator_units import V7_RUNTIME, V7_SHA
        script = f"/home/user/.local/share/agiw-runtimes/{V7_RUNTIME}/coordinator.py"
        valid = ("-B", script, "serve", "--release-sha256=" + V7_SHA)
        for arguments in (valid + ("--prompt", "PRIVATE-FIXTURE"), valid[:-1],
                          valid + ("--release-sha256=" + V7_SHA,),
                          ("-B", script.replace("/home/user", "/opt/rogue"), *valid[2:]),
                          ("-B", script, "status", valid[3])):
            with self.subTest(arguments=arguments):
                (self.root / "proc/101/exe").unlink(missing_ok=True)
                self.process(101, "python3", arguments, "/usr/bin/python3")
                row = linsys.read_processes(self.root)[0]
                self.assertNotIn("coordinatorRuntime", row)
                self.assertNotIn("coordinatorReleaseSha256", row)
                self.assertNotIn("PRIVATE-FIXTURE", json.dumps(row))
        (self.root / "proc/101/exe").unlink(missing_ok=True)
        self.process(101, "python3", valid, "/usr/bin/python3")
        with patch.object(linsys, "_process_start", side_effect=[1234, 5678]):
            row = linsys.read_processes(self.root)[0]
        self.assertNotIn("coordinatorRuntime", row)
        self.assertNotIn("coordinatorReleaseSha256", row)

    def test_other_uid_argv_not_read(self):
        self.process(101, "llama-server", ("--port", "8081"), "/opt/pair/llama-server", uid=os.getuid() + 1)
        original_read = linsys.read_text
        seen = []

        def read(path, *args, **kwargs):
            seen.append(Path(path).name)
            return original_read(path, *args, **kwargs)

        with patch.object(linsys, "read_text", side_effect=read):
            row = linsys.read_processes(self.root)[0]
        self.assertNotIn("cmdline", seen)
        self.assertNotIn("environ", seen)
        self.assertIsNone(row["port"])

    def test_bad_port_and_hostname_not_retained(self):
        self.process(101, "llama-server", ("--port", "999999", "--host", "token-private.example"), linsys.GPU_EXECUTABLE)
        row = linsys.read_processes(self.root)[0]
        self.assertIsNone(row["port"])
        self.assertNotIn("host", row["flags"])

    def test_unreadable_process_inventory_is_unknown(self):
        (self.root / "proc/100").mkdir(parents=True)
        self.assertIsNone(linsys.read_processes(self.root))

    def test_process_count_bounded(self):
        self.process(101, "codex")
        self.process(102, "codex")
        with patch.object(linsys, "MAX_PROCESSES", 1):
            self.assertIsNone(linsys.read_processes(self.root))

    def test_partial_process_inventory_is_unknown(self):
        self.process(101, "codex")
        (self.root / "proc/102").mkdir()
        self.assertIsNone(linsys.read_processes(self.root))

    def test_unreadable_own_command_is_unknown(self):
        self.process(101, "llama-server", exe=linsys.GPU_EXECUTABLE)
        (self.root / "proc/101/cmdline").unlink()
        self.assertIsNone(linsys.read_processes(self.root))

    def test_identity_flags_retain_only_alias_and_model_basename(self):
        self.process(101, "llama-server", ("--alias", "gpu-code", "--model", "/private/model/path/Qwen3-8B-Q4_K_M.gguf"), linsys.GPU_EXECUTABLE)
        row = linsys.read_processes(self.root)[0]
        self.assertEqual(row["flags"]["alias"], "gpu-code")
        self.assertEqual(row["flags"]["model"], "Qwen3-8B-Q4_K_M.gguf")
        self.assertNotIn("/private/model", json.dumps(row))

    def test_process_start_ticks_and_pid_reuse(self):
        self.process(101, "llama-server", exe=linsys.GPU_EXECUTABLE)
        self.assertEqual(linsys.read_processes(self.root)[0]["startTimeTicks"], 1234)
        self.put("/proc/101/stat", "101 (llama-server) S " + "0 " * 18 + "5678 0 0\n")
        self.assertEqual(linsys.read_processes(self.root)[0]["startTimeTicks"], 5678)

    def test_process_start_parser_handles_parentheses_and_rejects_malformed(self):
        self.process(101, "llama-server", exe=linsys.GPU_EXECUTABLE)
        path = self.put("/proc/101/stat", "101 (name with ) spaces) S " + "0 " * 18 + "5678 0 0\n")
        self.assertEqual(linsys.read_processes(self.root)[0]["startTimeTicks"], 5678)
        for text in ("101 (llama-server) S 0", "102 (llama-server) S " + "0 " * 18 + "5678",
                     "101 (llama-server) S " + "0 " * 18 + "-1",
                     "101 (llama-server) S " + "0 " * 18 + "9" * 100):
            with self.subTest(text=text[:40]):
                path.write_text(text, encoding="utf-8")
                self.assertIsNone(linsys.read_processes(self.root)[0]["startTimeTicks"])

    def test_missing_process_stat_keeps_identity_unknown(self):
        self.process(101, "llama-server", exe=linsys.GPU_EXECUTABLE)
        (self.root / "proc/101/stat").unlink()
        self.assertIsNone(linsys.read_processes(self.root)[0]["startTimeTicks"])

    def test_process_start_changed_during_read_is_unknown(self):
        self.process(101, "llama-server", exe=linsys.GPU_EXECUTABLE)
        original_read = linsys.read_text
        reads = 0

        def read(path, *args, **kwargs):
            nonlocal reads
            if Path(path) == self.root / "proc/101/stat":
                reads += 1
                return "101 (llama-server) S " + "0 " * 18 + f"{1000 + reads} 0 0\n"
            return original_read(path, *args, **kwargs)

        with patch.object(linsys, "read_text", side_effect=read):
            self.assertIsNone(linsys.read_processes(self.root)[0]["startTimeTicks"])


class LockTest(Fixture):
    def test_holder_matched_without_opening_lock(self):
        lock = self.put("/srv/storage/work/claude-llama-vulkan/gpu.lock", "")
        info = lock.stat()
        self.put("/proc/locks", f"1: FLOCK ADVISORY WRITE 123 {os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n")
        original_open = os.open

        def checked_open(path, *args, **kwargs):
            self.assertNotEqual(Path(path).name, "gpu.lock")
            return original_open(path, *args, **kwargs)

        for path in ("gpu.lock", str(lock)):
            with self.assertRaises(AssertionError):
                checked_open(path, os.O_RDONLY)
        with patch.object(linsys.os, "open", side_effect=checked_open):
            row = linsys.read_gpu_lock(self.root)
        self.assertEqual(row["holderPid"], 123)
        self.assertEqual(row["state"], "held")

    def test_absent_lock_unknown_and_observed_no_holder_unheld(self):
        self.assertEqual(linsys.read_gpu_lock(self.root)["state"], "unknown")
        self.put("/srv/storage/work/claude-llama-vulkan/gpu.lock", "")
        self.put("/proc/locks", "")
        self.assertEqual(linsys.read_gpu_lock(self.root)["state"], "unheld")


class NetworkTest(Fixture):
    HEADER = "  sl  local_address rem_address st tx_queue:rx_queue tr:tm->when retrnsmt uid timeout inode\n"

    def table(self, rows):
        self.put("/proc/net/tcp", self.HEADER + "\n".join(rows) + "\n")
        self.put("/proc/net/tcp6", self.HEADER)

    def socket(self, pid, fd, inode):
        self.link(f"/proc/{pid}/fd/{fd}", f"socket:[{inode}]")

    def test_listener_exposure_and_reverse_matched_caller(self):
        self.process(101, "llama-server", ("--port", "8081", "--models-dir", linsys.PAIR_MODELS), "/opt/pair/llama-server")
        self.process(102, "codex")
        processes = linsys.read_processes(self.root)
        self.socket(101, 1, 111)
        self.socket(101, 2, 112)
        self.socket(102, 1, 113)
        self.table(["0: 0100007F:1F91 00000000:0000 0A 0:0 00:0 0 1000 0 111",
                    "1: 0100007F:1F91 0100007F:C350 01 0:0 00:0 0 1000 0 112",
                    "2: 0100007F:C350 0100007F:1F91 01 0:0 00:0 0 1000 0 113",
                    "3: 00000000:37F1 00000000:0000 0A 0:0 00:0 0 1000 0 114"])
        row = linsys.read_network(self.root, processes)
        self.assertEqual(row["state"], "up")
        self.assertFalse(row["listeners"][0]["exposed"])
        self.assertTrue(row["listeners"][1]["exposed"])
        self.assertTrue(row["listeners"][1]["plaintext"])
        self.assertEqual(row["callers"], [{"pid": 102, "name": "codex", "lane": "pair-llamacpp", "connections": 1}])

    def test_unmatched_connection_not_claimed_as_local(self):
        self.table(["0: 0100007F:C350 0100007F:1F91 01 0:0 00:0 0 1000 0 113"])
        self.assertEqual(linsys.read_network(self.root, [{"pid": 102, "name": "codex"}])["callers"], [])

    def test_missing_network_is_unknown(self):
        self.assertEqual(linsys.read_network(self.root, [])["state"], "unknown")

    def test_missing_fd_ownership_does_not_claim_no_callers(self):
        self.table([])
        row = linsys.read_network(self.root, [{"pid": 101, "name": "codex"}])
        self.assertEqual(row["callersState"], "unknown")

    def test_fd_work_is_bounded(self):
        self.table([])
        self.process(101, "codex")
        self.socket(101, 1, 101)
        self.socket(101, 2, 102)
        with patch.object(linsys, "MAX_ROWS", 1):
            row = linsys.read_network(self.root, linsys.read_processes(self.root))
        self.assertEqual(row["callersState"], "unknown")

    def test_ipv6_loopback_and_mapped_address(self):
        self.assertEqual(linsys._tcp_address("00000000000000000000000001000000:1F91"), ("::1", 8081))
        self.assertEqual(linsys._tcp_address("0000000000000000FFFF00000100007F:1F91"), ("127.0.0.1", 8081))

    def test_nic_counters(self):
        self.table([])
        self.put("/proc/net/dev", "enp10s0: 123 1 0 0 0 0 0 0 456 1 0 0 0 0 0 0\n")
        row = linsys.read_network(self.root, [])
        self.assertEqual(row["interfaces"], [{"name": "enp10s0", "receivedBytes": 123, "sentBytes": 456}])

    def test_malformed_tcp_skipped_and_cap_unknown(self):
        self.table(["bad", "0: bad bad 01 0:0 00:0 0 1000 0 113"])
        self.assertEqual(linsys.read_network(self.root, [])["listeners"], [])
        with patch.object(linsys, "MAX_ROWS", 1):
            self.assertEqual(linsys.read_network(self.root, [])["state"], "unknown")


class SnapshotTest(Fixture):
    def test_missing_tree_snapshot_is_unknown_and_no_disk_probe(self):
        with patch.object(linsys.os, "statvfs") as disk:
            row = linsys.LinuxSystem(self.root).snapshot()
        disk.assert_not_called()
        self.assertIsNone(row["processes"])
        self.assertEqual(row["memory"]["level"], "unknown")
        self.assertEqual(row["network"]["state"], "unknown")

    def test_snapshot_previous_cpu(self):
        self.put("/proc/stat", "cpu 10 0 0 90\n")
        observer = linsys.LinuxSystem(self.root)
        before = observer.snapshot()
        self.put("/proc/stat", "cpu 30 0 0 170\n")
        self.assertEqual(observer.snapshot(before)["cpu"]["utilizationPercent"], 20)


if __name__ == "__main__":
    unittest.main()
