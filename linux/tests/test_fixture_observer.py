"""Exercise real source readers through collection and the one-shot projection."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_linux import collect, linsys, probes, server


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RecordingCollector(collect.Collector):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.samples = []

    def collect(self):
        value = super().collect()
        self.samples.append(value)
        return value


class FixtureObserverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=getattr(self, "fixture_parent", Path(__file__).parent))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home/observer"
        self.home.mkdir(parents=True)
        self.clock = Clock()
        self.calls = []
        self.slot_polls = 0
        self.tasks = (7, 7)
        self.served_model = "gpu-code"
        self.refused = False
        self.put("/proc/stat", "cpu 100 0 100 800 0 0 0 0\ncpu0 100 0 100 800\nbtime 900\n")
        self.put("/proc/sys/kernel/random/boot_id", "11111111-2222-4333-8444-555555555555\n")
        self.put("/proc/uptime", "100.0 80.0\n")
        self.put("/proc/meminfo", "MemTotal: 1000 kB\nMemAvailable: 300 kB\nSwapTotal: 100 kB\nSwapFree: 80 kB\n")
        self.put("/proc/pressure/memory", "some avg10=0.00 avg60=0.00\nfull avg10=0.00 avg60=0.00\n")
        self.put("/proc/self/mountinfo", "1 0 8:1 / / rw - ext4 /dev/fixture rw\n")
        self.put("/proc/201/status", f"Name:\tllama-server\nUid:\t{os.getuid()} 0 0 0\nVmRSS:\t1000 kB\n")
        self.put("/proc/201/stat", "201 (llama-server) S " + "0 " * 18 + "1234 0 0\n")
        self.put("/proc/201/cmdline", "\0".join((linsys.GPU_EXECUTABLE, "--host", "127.0.0.1", "--port", "41000",
                                                "--alias", "gpu-code", "--model", "/models/Qwen3-8B-Q4_K_M.gguf")) + "\0")
        self.link("/proc/201/exe", linsys.GPU_EXECUTABLE)
        self.link("/proc/201/fd/3", "socket:[100]")
        self.put("/proc/301/status", f"Name:\tllama-server\nUid:\t{os.getuid()} 0 0 0\n")
        self.put("/proc/301/stat", "301 (llama-server) S " + "0 " * 18 + "2345 0 0\n")
        self.put("/proc/301/cmdline", "\0".join(("/opt/pair/llama-server", "--port", "8081", "--models-dir", linsys.PAIR_MODELS)) + "\0")
        self.link("/proc/301/exe", "/opt/pair/llama-server")
        self.link("/proc/301/fd/3", "socket:[200]")
        header = " sl local_address rem_address st tx_queue:rx_queue tr:tm->when retrnsmt uid timeout inode\n"
        self.put("/proc/net/tcp", header + "0: 0100007F:A028 00000000:0000 0A 0:0 00:0 0 1000 0 100\n1: 0100007F:1F91 00000000:0000 0A 0:0 00:0 0 1000 0 200\n")
        self.put("/proc/net/tcp6", header)
        self.put("/proc/net/dev", "enp10s0: 123 1 0 0 0 0 0 0 456 1 0 0 0 0 0 0\n")
        self.device = "/sys/devices/pci0000:00/0000:01:00.0"
        for file, value in {"vendor": "0x1002", "device": "0x67df", "gpu_busy_percent": "25",
                            "mem_info_vram_used": str(3 * 2 ** 30), "mem_info_vram_total": str(8 * 2 ** 30),
                            "hwmon/hwmon12/temp1_input": "42000", "hwmon/hwmon12/power1_input": "37000000"}.items():
            self.put(self.device + "/" + file, value)
        driver = self.root / "sys/bus/pci/drivers/amdgpu"
        driver.mkdir(parents=True)
        self.link(self.device + "/driver", driver)
        self.link("/sys/class/drm/card9/device", self.root / self.device.lstrip("/"))
        self.put("/sys/class/hwmon/hwmon0/name", "nvme")
        self.put("/sys/class/hwmon/hwmon0/temp1_input", "33900")
        log = self.put("/srv/storage/work/claude-llama-vulkan/logs/last-code.log",
                       "prompt eval time = 1000 ms / 80 tokens (12.50 ms per token, 80.00 tokens per second)\n"
                       "eval time = 1000 ms / 31 tokens (32.26 ms per token, 31.00 tokens per second)\n")
        os.utime(log, (900, 900))
        fence = self.home / ".local/state/codemode-router/install-fence.json"
        fence.parent.mkdir(parents=True)
        fence.write_text('{"state":"installed","generation":"fixture-generation"}', encoding="utf-8")
        os.utime(fence, (900, 900))
        self.statvfs = os.statvfs_result((4096, 4096, 1000, 400, 300, 100, 90, 90, 0, 255))

    def put(self, path, text):
        target = self.root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def link(self, path, target):
        link = self.root / path.lstrip("/")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)

    def fetch(self, url, timeout, max_bytes):
        self.assertEqual(timeout, probes.SLOTS_TIMEOUT if url.endswith("/slots") else probes.HTTP_TIMEOUT)
        self.assertLessEqual(max_bytes, probes.HTTP_MAX_BYTES)
        self.calls.append(url)
        if self.refused:
            raise ConnectionRefusedError("fixture refused")
        if url == "http://127.0.0.1:8081/health":
            return {"status": "ok"}
        if url == "http://127.0.0.1:8081/models":
            return {"data": [{"id": "agiw-linux-qwen05", "status": {"value": "unloaded"}}]}
        if url == "http://127.0.0.1:41000/health":
            return {"status": "ok"}
        if url == "http://127.0.0.1:41000/v1/models":
            return {"data": [{"id": self.served_model}]}
        if url == "http://127.0.0.1:41000/slots":
            index = self.slot_polls
            self.slot_polls += 1
            return [{"id": 0, "id_task": self.tasks[min(index, 1)], "is_processing": True,
                     "n_decoded": (10, 45)[min(index, 1)]}]
        self.fail("Unexpected URL: " + url)

    def observer(self, *, root=None, home=None):
        root, home = root or self.root, home or self.home
        return RecordingCollector(root=root, home=home, clock=self.clock, fetch=self.fetch, host="fixture-linux",
                                  system=linsys.LinuxSystem(root=root, home=home))

    def run_once(self, observer):
        original_open = os.open

        def readonly_open(path, flags, *args, **kwargs):
            self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND), 0,
                             "one-shot observer attempted a filesystem write")
            return original_open(path, flags, *args, **kwargs)

        with patch.object(os, "open", side_effect=readonly_open), \
                patch.object(linsys.os, "statvfs", return_value=self.statvfs) as disks, \
                patch.object(probes, "_http_get", side_effect=AssertionError("real HTTP prohibited")):
            raw = server.once(observer, state_dir=self.home / ".local/state/agiw", clock=self.clock, sleep=self.clock.sleep)
        self.assertLessEqual(len(raw), server.STATUS_MAX_BYTES)
        for call in disks.call_args_list:
            Path(call.args[0]).relative_to(self.root)
        return json.loads(raw)

    def test_two_source_samples_reach_one_shot_projection(self):
        observer = self.observer()
        status = self.run_once(observer)
        self.assertEqual(len(observer.samples), 2)
        self.assertEqual(self.slot_polls, 2)
        self.assertEqual(status["schemaVersion"], 1)
        self.assertEqual(status["platform"], "linux")
        self.assertEqual(status["health"], "up")
        self.assertEqual(status["observedUnix"], 1001.0)
        model = next(row for row in status["models"] if row["id"] == "gpu-code")
        self.assertEqual(model["loadedState"], "loaded")
        self.assertEqual(model["activity"], "generating")
        self.assertEqual(model["liveTokensPerSecond"], 35.0)
        self.assertEqual(model["slotsBusy"], 1)
        self.assertIsNone(observer.samples[0]["models"][-1]["liveTokensPerSecond"])
        pair = next(row for row in status["models"] if row["lane"] == "pair-llamacpp")
        self.assertEqual(pair["loadedState"], "inactive")
        self.assertEqual(pair["activity"], "unknown")
        self.assertIsNone(pair["liveTokensPerSecond"])
        self.assertIsNone(status["modelsTotal"])
        self.assertFalse(status["modelsComplete"])
        self.assertEqual(status["gpus"][0]["memoryUsedMiB"], 3072)
        self.assertEqual(status["gpus"][0]["temperatureC"], 42)
        self.assertEqual(status["memory"], {"level": "ok", "availablePercent": 30.0})
        sample = observer.samples[-1]
        self.assertNotIn("state", sample["authority"]["fence"])
        self.assertNotIn("generation", sample["authority"]["fence"])
        self.assertEqual(sample["historicalRates"][0]["generationTokensPerSecond"], 31)
        self.assertEqual(sample["historicalRates"][0]["ageSeconds"], 101)
        self.assertEqual(sample["temperatures"][0]["temperatureC"], 33.9)
        self.assertEqual(sample["disks"][0]["availableBytes"], 300 * 4096)
        self.assertEqual(sample["network"]["listeners"][0]["name"], "llama-server")
        self.assertEqual(sample["gpus"][0]["utilizationKind"], "instant")
        self.assertEqual(sample["cpu"]["sample"], {"total": 1000, "idle": 800})
        self.assertEqual([url for url in self.calls if ":8081/" in url],
                         ["http://127.0.0.1:8081/health", "http://127.0.0.1:8081/models"] * 2)
        self.assertFalse((self.home / ".local/state/agiw").exists())

    def test_changed_slot_task_does_not_create_live_rate(self):
        self.tasks = (7, 8)
        status = self.run_once(self.observer())
        wrapper = next(row for row in status["models"] if row["id"] == "gpu-code")
        self.assertEqual(self.slot_polls, 2)
        self.assertIsNone(wrapper["liveTokensPerSecond"])

    def test_missing_sources_remain_unknown_in_projection(self):
        missing = self.root / "missing"
        self.refused = True
        observer = self.observer(root=missing, home=missing / "home")
        status = self.run_once(observer)
        self.assertEqual(status["health"], "unknown")
        self.assertEqual(status["models"], [])
        self.assertIsNone(status["modelsTotal"])
        self.assertEqual(status["memory"], {"level": "unknown", "availablePercent": None})
        self.assertIsNone(status["gpus"])
        self.assertFalse(status["gpusComplete"])
        self.assertEqual(status["gpusState"], "unknown")
        self.assertTrue(all(row["state"] == "unknown" for row in status["components"]))
        self.assertEqual(observer.samples[-1]["network"]["callersState"], "unknown")
        self.assertFalse(missing.exists())

    def test_partial_process_inventory_does_not_authorize_wrapper_probe(self):
        (self.root / "proc/202").mkdir()
        observer = self.observer()
        status = self.run_once(observer)
        self.assertFalse(any(":41000/" in url for url in self.calls))
        self.assertFalse(any(row["lane"].startswith("gpu-wrapper") for row in status["models"]))
        self.assertIsNone(status["modelsTotal"])
        coordinator = next(row for row in status["components"] if row["id"] == "coordinator")
        self.assertEqual(coordinator["state"], "unknown")
        self.assertEqual(coordinator["reason"], "process-inventory-unavailable")

    def test_wrapper_identity_mismatch_is_unknown_and_not_probed_for_slots(self):
        self.served_model = "different-model"
        status = self.run_once(self.observer())
        wrapper = next(row for row in status["models"] if row["lane"].startswith("gpu-wrapper"))
        self.assertEqual(wrapper["loadedState"], "unknown")
        self.assertEqual(wrapper["activity"], "unknown")
        self.assertIsNone(wrapper["liveTokensPerSecond"])
        self.assertEqual(self.slot_polls, 0)

    def test_missing_metric_files_are_null_through_projection(self):
        (self.root / self.device.lstrip("/") / "gpu_busy_percent").unlink()
        (self.root / self.device.lstrip("/") / "hwmon/hwmon12/temp1_input").unlink()
        (self.root / "proc/meminfo").unlink()
        status = self.run_once(self.observer())
        self.assertIsNone(status["gpus"][0]["utilizationPercent"])
        self.assertIsNone(status["gpus"][0]["temperatureC"])
        self.assertEqual(status["gpus"][0]["memoryTotalMiB"], 8192)
        self.assertEqual(status["memory"], {"level": "unknown", "availablePercent": None})

    def test_reused_pid_does_not_inherit_live_rate(self):
        original_read = linsys.read_text

        def read(path, *args, **kwargs):
            value = original_read(path, *args, **kwargs)
            if Path(path) == self.root / "proc/201/stat" and self.clock.now > 1000:
                return value.replace("1234", "5678")
            return value

        with patch.object(linsys, "read_text", side_effect=read):
            status = self.run_once(self.observer())
        wrapper = next(row for row in status["models"] if row["id"] == "gpu-code")
        self.assertEqual(self.slot_polls, 2)
        self.assertIsNone(wrapper["liveTokensPerSecond"])

    def test_changed_boot_does_not_inherit_live_rate(self):
        original_read = linsys.read_text

        def read(path, *args, **kwargs):
            value = original_read(path, *args, **kwargs)
            if Path(path) == self.root / "proc/sys/kernel/random/boot_id" and self.clock.now > 1000:
                return "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n"
            return value

        with patch.object(linsys, "read_text", side_effect=read):
            status = self.run_once(self.observer())
        wrapper = next(row for row in status["models"] if row["id"] == "gpu-code")
        self.assertEqual(self.slot_polls, 2)
        self.assertIsNone(wrapper["liveTokensPerSecond"])

    def test_missing_process_start_ticks_keeps_live_rate_unknown(self):
        (self.root / "proc/201/stat").unlink()
        status = self.run_once(self.observer())
        self.assertFalse(any(row["lane"].startswith("gpu-wrapper") for row in status["models"]))
        self.assertEqual(self.slot_polls, 0)
        self.assertFalse(any(":41000/" in url for url in self.calls))
        self.assertIsNone(status["modelsTotal"])


if __name__ == "__main__":
    unittest.main()
