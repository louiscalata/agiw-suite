"""Regression fixtures for the round-three GPU and caller findings."""
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from test_linsys import Fixture
from agiw_linux import collect, linsys, server


class GpuReadRegression(Fixture):
    def add_non_pci_card0(self):
        device = "/sys/devices/virtual/drm/card0/device"
        self.put(device + "/uevent", "DRIVER=simpledrm\n")
        self.link("/sys/class/drm/card0/device", self.root / device.lstrip("/"))

    def test_unrecognized_non_pci_layout_without_identity_is_unknown(self):
        device = "/sys/devices/unclassified/drm/card0/device"
        self.put(device + "/uevent", "DRIVER=unknown\n")
        self.link("/sys/class/drm/card0/device", self.root / device.lstrip("/"))
        self.assertIsNone(linsys.read_gpus(self.root))

    def test_non_pci_card_before_amd_card_does_not_hide_gpu(self):
        self.add_non_pci_card0()
        device = self.gpu("card1")
        self.put(device + "/gpu_busy_percent", "17\n")
        rows = linsys.read_gpus(self.root)
        self.assertEqual([row["index"] for row in rows], [1])
        self.assertEqual(rows[0]["vendorId"], "0x1002")
        self.assertEqual(rows[0]["utilizationPercent"], 17)

    def test_non_pci_card_does_not_hide_unreadable_pci_candidate(self):
        self.add_non_pci_card0()
        device = self.gpu("card1")
        (self.root / device.lstrip("/") / "vendor").unlink()
        self.assertIsNone(linsys.read_gpus(self.root))

    def test_page_sized_metadata_does_not_hide_short_sysfs_attributes(self):
        device = self.gpu()
        self.put(device + "/gpu_busy_percent", "17\n")
        self.put(device + "/hwmon/hwmon2/temp1_input", "40000\n")
        original = os.fstat

        def page_size(fd):
            values = list(original(fd))
            values[6] = 4096
            return os.stat_result(values)

        with patch.object(linsys.os, "fstat", side_effect=page_size):
            rows = linsys.read_gpus(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["utilizationPercent"], 17)
        self.assertEqual(rows[0]["temperatureC"], 40)

    def test_failed_or_capped_drm_scan_is_unknown(self):
        self.assertIsNone(linsys.read_gpus(self.root))
        directory = self.root / "sys/class/drm"
        directory.mkdir(parents=True)
        original = linsys.os.scandir

        def denied(path):
            if Path(path) == directory:
                raise PermissionError("fixture DRM denial")
            return original(path)

        with patch.object(linsys.os, "scandir", side_effect=denied):
            self.assertIsNone(linsys.read_gpus(self.root))
        for index in range(129):
            (directory / f"entry{index}").touch()
        self.assertIsNone(linsys.read_gpus(self.root))

    def test_partial_card_identification_is_unknown(self):
        device = self.gpu()
        vendor = self.root / device.lstrip("/") / "vendor"
        driver = self.root / device.lstrip("/") / "driver"
        for file in ("vendor", "driver"):
            with self.subTest(file=file):
                vendor.write_text("0x1002\n")
                (vendor if file == "vendor" else driver).unlink()
                self.assertIsNone(linsys.read_gpus(self.root))

    def test_completed_empty_scan_is_explicit(self):
        (self.root / "sys/class/drm").mkdir(parents=True)
        row = linsys.LinuxSystem(self.root).snapshot()
        self.assertEqual(row["gpus"], [])
        self.assertTrue(row["gpusComplete"])
        self.assertEqual(row["gpusState"], "known")

    def test_gpu_failure_survives_projection_and_cache(self):
        self.put("/proc/sys/kernel/random/boot_id", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        with patch.object(collect.probes, "read_codex_state", return_value={}), \
                patch.object(collect.probes, "read_pair_files", return_value={}):
            snapshot = collect.Collector(root=self.root, home=self.root, clock=lambda: 100).collect()
        projected = collect.project_status(snapshot)
        for row in (snapshot, projected):
            self.assertEqual(row["health"], "unknown")
            self.assertEqual(row["gpusState"], "unknown")
            self.assertFalse(row["gpusComplete"])
            self.assertIsNone(row["gpus"])
        state = self.root / "state"
        server.write_status(state, projected)
        self.assertEqual(server.cached_status(state / "linux-status.json", 101), projected)

    def test_projection_never_calls_truncated_or_malformed_scan_complete(self):
        value = collect.unknown_snapshot(now=100)
        gpu = {"name": "AMD GPU (1002:67df)", "sources": {
            key: "/sys/class/drm/card7/device/" + file for key, file in
            (("utilizationPercent", "gpu_busy_percent"), ("memoryUsedMiB", "mem_info_vram_used"),
             ("memoryTotalMiB", "mem_info_vram_total"))}}
        value.update(gpus=[gpu], gpusComplete=True, gpusState="known", health="up")
        self.assertTrue(collect.project_status(value)["gpusComplete"])
        for rows in ([None], [gpu] * 9, None, [{"name": "GPU"}]):
            value["gpus"] = rows
            projected = collect.project_status(value)
            self.assertFalse(projected["gpusComplete"])
            self.assertEqual(projected["gpusState"], "unknown")
            self.assertEqual(projected["health"], "unknown")

    def test_incomplete_cached_gpu_row_cannot_claim_machine_up(self):
        value = collect.project_status(collect.unknown_snapshot(now=100))
        value.update(gpus=[{"name": "GPU"}], gpusComplete=True, gpusState="known", health="up")
        state = self.root / "state"
        server.write_status(state, value)
        cached = server.cached_status(state / "linux-status.json", 101)
        self.assertFalse(cached["gpusComplete"])
        self.assertEqual(cached["health"], "unknown")

    def test_coordinator_and_ollama_evidence_survive_cache(self):
        snapshot = collect.unknown_snapshot(now=100)
        snapshot["components"] = [
            {"id": "coordinator", "state": "disabled", "health": "down", "reason": "unit-disabled-observed"},
            {"id": "pair-ollama", "health": "unknown", "reason": "application-health-unverified",
             "processPresent": True, "listenerPresent": True}]
        value = collect.project_status(snapshot)
        state = self.root / "state"
        server.write_status(state, value)
        self.assertEqual(server.cached_status(state / "linux-status.json", 101), value)


class CallerOwnerRegression(Fixture):
    def test_v8_coordinator_script_is_recognized_with_python_no_bytecode_flag(self):
        runtime = "linux-scheduler-authority-v8-jev114-qwen35-boot-v1"
        script = f"/home/fixture/.local/share/agiw-runtimes/{runtime}/coordinator_v8.py"
        self.process(101, "python3", ("-B", script, "serve",
                                       "--release-sha256=85adaf746dd8775029d9505eadca04eb0282d9af56d056f2fce9004a58c43e9d",
                                       "--upgrade-directory=/home/fixture/.local/state/upgrade",
                                       "--upgrade-binding-sha256=" + "a" * 64), "/usr/bin/python3")
        row = linsys.read_processes(self.root)[0]
        self.assertEqual(row["kind"], "coordinator")
        self.assertTrue(row["coordinator"])

    def test_v8_coordinator_recognition_rejects_unmapped_script_paths(self):
        runtime = "linux-scheduler-authority-v8-jev114-qwen35-boot-v1"
        script = f"/home/fixture/.local/share/agiw-runtimes/{runtime}/coordinator_v8.py"
        cases = (("/opt/agiw-runtimes/" + runtime + "/coordinator_v8.py", ("-B",), "serve"),
                 (f"/home/fixture/.local/share/agiw-runtimes/{runtime}/other.py", ("-B",), "serve"),
                 (script + "/extra", ("-B",), "serve"),
                 (script, ("-X", "dev"), "serve"),
                 (script, ("-B",), "not-serve"))
        for pid, (path, flags, command) in enumerate(cases, 101):
            with self.subTest(path=path, flags=flags, command=command):
                if (self.root / "proc").exists():
                    shutil.rmtree(self.root / "proc")
                self.process(pid, "python3", (*flags, path, command), "/usr/bin/python3")
                self.assertEqual(linsys.read_processes(self.root)[0]["kind"], "unknown")

    def connection(self, port=8081, kind="pair"):
        if kind == "pair":
            self.process(101, "llama-server", ("--port", str(port), "--models-dir", linsys.PAIR_MODELS), "/opt/pair/llama-server")
        elif kind == "wrapper":
            self.process(101, "llama-server", ("--port", str(port), "--host", "127.0.0.1"), linsys.GPU_EXECUTABLE)
        else:
            self.process(101, kind)
        self.process(102, "codex")
        for pid, fd, inode in ((101, 1, 111), (101, 2, 112), (102, 1, 113)):
            self.link(f"/proc/{pid}/fd/{fd}", f"socket:[{inode}]")
        header = "sl local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n"
        self.put("/proc/net/tcp", header +
                 f"0: 0100007F:{port:04X} 00000000:0000 0A 0:0 00:0 0 1000 0 111\n" +
                 f"1: 0100007F:{port:04X} 0100007F:C350 01 0:0 00:0 0 1000 0 112\n" +
                 f"2: 0100007F:C350 0100007F:{port:04X} 01 0:0 00:0 0 1000 0 113\n")
        self.put("/proc/net/tcp6", header)

    def test_foreign_listener_never_assigns_pair_caller(self):
        self.connection(kind="foreign-server")
        row = linsys.read_network(self.root, linsys.read_processes(self.root))
        self.assertEqual(row["callers"], [])
        self.assertEqual(row["callersState"], "unknown")
        self.assertEqual(collect.network_status(row)["callersState"], "unknown")

    def test_validated_pair_ollama_and_wrapper_owners_allow_attribution(self):
        for port, kind, lane in ((8081, "pair", "pair-llamacpp"), (11435, "ollama", "pair-ollama"),
                                 (41234, "wrapper", "gpu-wrapper")):
            with self.subTest(lane=lane):
                # Each case uses a fresh proc fixture.
                if (self.root / "proc").exists():
                    shutil.rmtree(self.root / "proc")
                self.connection(port, kind)
                row = linsys.read_network(self.root, linsys.read_processes(self.root), {port: lane})
                self.assertEqual(row["callers"], [{"pid": 102, "name": "codex", "lane": lane, "connections": 1}])
                self.assertEqual(row["callersState"], "known")

    def test_partial_table_cannot_authorize_lane_attribution(self):
        self.connection()
        (self.root / "proc/net/tcp6").unlink()
        row = linsys.read_network(self.root, linsys.read_processes(self.root))
        self.assertEqual(row["callers"], [])
        self.assertEqual(row["callersState"], "unknown")

    def test_duplicate_listener_does_not_authorize_lane_attribution(self):
        self.connection()
        with (self.root / "proc/net/tcp").open("a") as stream:
            stream.write("3: 00000000:1F91 00000000:0000 0A 0:0 00:0 0 1000 0 111\n")
        row = linsys.read_network(self.root, linsys.read_processes(self.root))
        self.assertEqual(row["callers"], [])
        self.assertEqual(row["callersState"], "unknown")


if __name__ == "__main__":
    unittest.main()
